import secrets
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password
from app.bootstrap import create_admin
from app.embeddings import LocalEmbedder
from app.errors import AppError
from app.main import create_app
from app.schemas import SearchFilters
from app.store import iso_utc
from tests.conftest import FakeEmbedder, FakeLLM


@contextmanager
def client_for(settings, repository, llm=None, embedder=None, role="admin"):
    app = create_app(
        settings=settings,
        repository=repository,
        llm=llm if llm is not None else FakeLLM(),
        embedder=embedder if embedder is not None else FakeEmbedder(),
    )
    app.state.store.initialize()
    password = secrets.token_urlsafe(18)
    create_admin(app.state.store, "audit_admin", "Аудит әкімшісі", password)
    login = "audit_admin"
    if role not in {"admin", "anonymous"}:
        login = "audit_" + role
        with app.state.store.connect(write=True) as connection:
            connection.execute(
                "INSERT INTO Users(login,display_name,password_hash,role_code,active,created_at) "
                "VALUES (?,?,?,?,1,?)",
                (login, "Аудит тексерушісі", hash_password(password), role, iso_utc()),
            )
    with TestClient(app, raise_server_exceptions=False) as client:
        if role != "anonymous":
            response = client.post("/api/auth/login", json={"login": login, "password": password})
            assert response.status_code == 200
            client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        yield client


@pytest.mark.parametrize("principal,status", [("teacher", 403), ("anonymous", 401)])
def test_audit_permission_checked_before_model_access(settings, repository, principal, status):
    llm = FakeLLM(parse_error=AssertionError("Unauthorized request must not reach a model"))
    with client_for(settings, repository, llm=llm, role=principal) as client:
        response = client.post(
            "/api/audit/search",
            json={"question": "Сәтсіз кірулерді тап"},
            headers={"X-Demo-User": "demo_admin"},
        )
    assert response.status_code == status
    assert response.json()["error"]["code"] in {"forbidden", "unauthenticated"}
    assert llm.parse_calls == []


@pytest.mark.parametrize("principal", ["admin", "auditor", "head"])
def test_authorized_search_returns_evidence_and_reference_ids(settings, repository, principal):
    llm = FakeLLM()
    with client_for(settings, repository, llm=llm, role=principal) as client:
        response = client.post("/api/audit/search", json={"question": "Сәтсіз кірулерді тап"})
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "ok"
    record_ids = {record["audit_id"] for record in result["records"]}
    assert record_ids
    assert all(set(item["audit_ids"]) <= record_ids for item in result["summary"])
    assert result["filters"]["action"] == "login"
    assert result["filters"]["result"] == "failure"
    assert len(llm.parse_calls) == 1


def test_api_returns_no_results_without_loading_models(settings, repository):
    llm = FakeLLM(filters=SearchFilters(action="login", user_login="demo_admin", result="denied"))
    embedder = FakeEmbedder(error=AssertionError("No embedding should be needed"))
    with client_for(settings, repository, llm=llm, embedder=embedder) as client:
        response = client.post(
            "/api/audit/search", json={"question": "Әкімшінің рұқсат берілмеген кірулерін тап"}
        )
    assert response.status_code == 200
    assert response.json()["status"] == "no_results"
    assert response.json()["records"] == []
    assert response.json()["summary"] == []
    assert llm.summary_calls == []
    assert embedder.calls == []


@pytest.mark.parametrize(
    "code,status",
    [("model_unavailable", 503), ("invalid_model_output", 502), ("unclear_question", 422)],
)
def test_api_has_distinct_public_error_responses(settings, repository, code, status):
    llm = FakeLLM(parse_error=AppError(code, "Пайдаланушыға түсінікті тест қатесі", status))
    with client_for(settings, repository, llm=llm) as client:
        response = client.post("/api/audit/search", json={"question": "Кірулерді тап"})
    assert response.status_code == status
    assert response.json() == {
        "error": {"code": code, "message": "Пайдаланушыға түсінікті тест қатесі"}
    }


def test_server_error_is_public_and_does_not_expose_internal_exception(settings, repository):
    llm = FakeLLM(parse_error=RuntimeError("private_internal_marker"))
    with client_for(settings, repository, llm=llm) as client:
        response = client.post("/api/audit/search", json={"question": "Кірулерді тап"})
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "server_error"
    assert "private_internal_marker" not in response.text


@pytest.mark.parametrize(
    "payload",
    [{"question": "  "}, {"question": "x" * 1001}, {"question": "кіру", "sql": "SELECT 1"}],
)
def test_api_validates_question_input_before_model_access(settings, repository, payload):
    llm = FakeLLM()
    with client_for(settings, repository, llm=llm) as client:
        response = client.post("/api/audit/search", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert llm.parse_calls == []


def test_health_is_alive_when_local_embedding_is_missing_and_readiness_explains_setup(
    settings, repository
):
    settings = settings.model_copy(
        update={"embedding_model": str(settings.database_path.parent / "missing-model")}
    )
    embedder = LocalEmbedder(settings)
    with client_for(settings, repository, embedder=embedder) as client:
        health = client.get("/api/health")
        readiness = client.get("/api/readiness")
    assert health.status_code == 200
    assert health.json()["status"] == "alive"
    assert "demo_authorization" not in health.json()
    assert readiness.status_code == 503
    assert readiness.json()["status"] == "not_ready"
    component = readiness.json()["components"]["embedding"]
    assert component["status"] == "unavailable"
    assert component["message"]
    assert "download_embedding" in component["setup"]


def test_missing_local_embedding_raises_without_mock_or_remote_download(settings):
    settings = settings.model_copy(
        update={"embedding_model": str(settings.database_path.parent / "missing-model")}
    )
    with pytest.raises(AppError) as caught:
        LocalEmbedder(settings).encode(["Тек reason мәтіні"])
    assert caught.value.code == "model_unavailable"
    assert caught.value.status_code == 503


def test_ready_endpoint_uses_actual_injected_component_states(settings, repository):
    with client_for(settings, repository) as client:
        response = client.get("/api/readiness")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert set(response.json()["components"]) == {"database", "ollama", "embedding"}
