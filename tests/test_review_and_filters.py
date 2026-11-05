import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.llm import OllamaAdapter
from app.main import create_app
from app.schemas import SearchFilters, SummaryClaim, SummaryPlan
from app.service import AuditSearchService
from tests.conftest import FakeEmbedder, FakeLLM
from tests.test_core_system import core_system as core_system
from tests.test_core_system import post


def test_every_translated_clickable_ai_example_preserves_its_explicit_criteria(settings, now):
    """Exercise the actual public examples, including natural sign-in morphology."""
    catalog = json.loads((Path(__file__).parents[1] / "app/static/locales.json").read_text("utf-8"))
    adapter = OllamaAdapter(
        settings, httpx.MockTransport(lambda r: pytest.fail("No model guessing"))
    )
    for locale in ("kk", "ru", "en"):
        for index in range(1, 15):
            filters = adapter.parse(catalog[f"ai.example.{index}"][locale], now)
            context = f"{locale} example {index}: {filters}"
            if index in {1, 2, 3, 13}:
                assert filters.action == "login", context
            if index == 1:
                assert filters.result is None and filters.start_at.hour == 0, context
            elif index == 2:
                assert filters.start_at == now - timedelta(days=7), context
                assert filters.result == "failure", context
            elif index == 3:
                assert filters.ip_address == "203.0.113.10" and filters.result == "failure", context
            elif index in {4, 5, 6, 7}:
                assert (
                    filters.action
                    == {
                        4: "session_flagged",
                        5: "session_block",
                        6: "grade_change",
                        7: "grade_change",
                    }[index]
                ), context
                if index == 7:
                    assert (filters.object_type, filters.object_id) == ("grade", "1"), context
            elif index == 8:
                assert set(filters.actions) == {"document_upload", "document_delete"}, context
            elif index == 9:
                assert set(filters.actions) == {
                    "user_change",
                    "role_change",
                    "permission_change",
                }, context
            elif index == 10:
                assert filters.result == "denied", context
            elif index == 11:
                assert filters.user_login == "demo_teacher" and filters.action is None, context
            elif index == 12:
                assert filters.start_at.isoformat() == "2026-10-01T00:00:00+00:00", context
                assert filters.end_at.isoformat() == "2026-10-15T00:00:00+00:00", context
            elif index == 14:
                assert (
                    "submission_review" in filters.actions and "task_create" in filters.actions
                ), context


@pytest.mark.parametrize(
    "payload",
    [
        {"ip_address": "192.0.2.1' OR 1=1 --"},
        {"ip_address": "999.0.0.1"},
        {"ip_address": "fe80::1%ethernet"},
        {"object_type": "grade;DROP"},
        {"object_id": "1' OR 1=1"},
        {"action": "login", "actions": ["logout"]},
        {"actions": ["login", "login"]},
        {"actions": ["drop_table"]},
        {"sql": "DELETE FROM AuditLogs"},
    ],
)
def test_closed_exact_filter_schema_rejects_injection_and_ambiguity(payload):
    with pytest.raises(ValidationError):
        SearchFilters.model_validate(payload)


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Бүгін кім жүйеге кірді немесе кіре алмады?", {"action": "login", "result": None}),
        ("Last 7 days failed logins", {"action": "login", "result": "failure"}),
        ("Қай IP-ден бірнеше рет қате пароль енгізілді?", {"action": "login", "result": "failure"}),
        ("Какие сессии подозрительны?", {"action": "session_flagged"}),
        ("Who blocked a session?", {"action": "session_block"}),
        ("Какие оценки изменились?", {"action": "grade_change"}),
        (
            "Grade changes object: grade/17 IP: 203.0.113.17",
            {
                "action": "grade_change",
                "object_type": "grade",
                "object_id": "17",
                "ip_address": "203.0.113.17",
            },
        ),
        (
            "login: teacher.one IP: [2001:db8::2]",
            {"user_login": "teacher.one", "ip_address": "2001:db8::2"},
        ),
        ("Denied actions", {"result": "denied"}),
        ("action: submission_review", {"action": "submission_review"}),
    ],
)
def test_multilingual_explicit_criteria_do_not_need_model_guesses(
    settings, now, question, expected
):
    def no_model(request):
        raise AssertionError("Explicit criteria must be grounded by server code")

    filters = OllamaAdapter(settings, httpx.MockTransport(no_model)).parse(question, now)
    for key, value in expected.items():
        assert getattr(filters, key) == value


def test_date_timestamps_and_action_groups_are_grounded(settings, now):
    adapter = OllamaAdapter(settings, httpx.MockTransport(lambda r: pytest.fail("No model call")))
    dates = adapter.parse("2026-10-01T05:00:00+05:00 to 2026-10-02T05:00:00+05:00 audit", now)
    assert dates.start_at.isoformat() == "2026-10-01T00:00:00+00:00"
    assert dates.end_at.isoformat() == "2026-10-02T00:00:00+00:00"
    tasks = adapter.parse("Recent task submissions and grades", now)
    assert "submission_review" in tasks.actions
    docs = adapter.parse("Who uploaded or deleted documents?", now)
    assert set(docs.actions) == {"document_upload", "document_delete"}
    russian_docs = adapter.parse("Кто загрузил или удалил документы?", now)
    assert set(russian_docs.actions) == {"document_upload", "document_delete"}


def test_ip_and_object_sql_filters_precede_model_and_private_metadata_stays_out(
    populated_repository, settings
):
    repository = populated_repository
    with repository._connect() as connection:
        connection.execute(
            "INSERT INTO AuditLogs (audit_id,occurred_at,user_id,role,action,object_type,object_id,"
            "result,reason,ip_address,old_value,new_value) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                40,
                "2026-10-14T10:00:00.000000Z",
                "teacher.one",
                "teacher",
                "grade_change",
                "grade",
                "17",
                "success",
                "Recorded human edit",
                "203.0.113.17",
                "42",
                "88",
            ),
        )
    llm = FakeLLM(
        filters=SearchFilters(ip_address="203.0.113.17", object_type="grade", object_id="17")
    )
    response = AuditSearchService(repository, llm, FakeEmbedder(), settings).search(
        "exact query", "en"
    )
    assert [record.audit_id for record in response.records] == [40]
    assert response.evidence[0].ip_address == "203.0.113.17"
    assert "203.0.113.17" not in json.dumps(
        [r.model_dump(mode="json") for r in llm.summary_calls[0]]
    )
    assert "audit records" in response.summary[0].text
    assert response.summary[0].audit_ids == [40]
    nothing, _ = repository.search(SearchFilters(ip_address="203.0.113.18"))
    assert nothing == []
    captured = []

    def semantic_intent(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            request=request,
            json={
                "done": True,
                "response": '{"semantic_query":"file failure"}',
            },
        )

    adapter = OllamaAdapter(settings, httpx.MockTransport(semantic_intent))
    parsed = adapter.parse(
        "Find reason text concerning file failure IP: 203.0.113.17 object: grade/17",
        response.records[0].occurred_at,
    )
    assert parsed.ip_address == "203.0.113.17"
    assert "203.0.113.17" not in captured[0]["prompt"]


@pytest.mark.parametrize(("locale", "phrase"), [("en", "Sign in"), ("ru", "Войдите")])
def test_api_errors_follow_accept_language_without_revealing_input(settings, locale, phrase):
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/dashboard", headers={"Accept-Language": locale})
        assert response.status_code == 401
        assert phrase in response.json()["error"]["message"]
        invalid = client.post(
            "/api/auth/register",
            headers={"Accept-Language": locale},
            json={"login": "secret-input", "requested_role": "admin"},
        )
        assert invalid.status_code == 422
        assert "secret-input" not in invalid.text
        assert "Өрістер" not in invalid.json()["error"]["message"]


def test_human_session_review_and_ai_evidence_never_auto_block(core_system, monkeypatch):
    system = core_system
    sid = system.sessions["teacher"]
    for _ in range(system.settings.suspicious_failure_threshold):
        bad = system.clients["teacher"].post(
            "/api/auth/login",
            json={"login": "core_teacher", "password": "incorrect-and-unrecorded-password"},
        )
        assert bad.status_code == 401
    items = system.clients["admin"].get("/api/sessions").json()["items"]
    session = next(item for item in items if item["session_id"] == sid)
    assert session["status"] == "active"
    assert session["review_status"] == "awaiting_admin"
    assert session["review_source"] == "rule"
    assert session["review_audit_ids"]
    assert not ({"token_hash", "csrf_hash"} & session.keys())
    assert (
        post(
            system, "teacher", f"/api/sessions/{sid}/dismiss", {"reason": "Not authorized"}
        ).status_code
        == 403
    )
    assert post(system, "admin", f"/api/sessions/{sid}/ai-review", {}).status_code == 503

    class EvidenceLLM:
        invalid_ids = False

        def readiness(self):
            return {"status": "ready"}

        def summarize(self, records):
            assert all("ip_address" not in record.model_dump() for record in records)
            return SummaryPlan(
                claims=[
                    SummaryClaim(
                        kind="event",
                        audit_ids=[999999 if self.invalid_ids else records[0].audit_id],
                    )
                ]
            )

    model = EvidenceLLM()
    system.app.state.search_service.llm = model
    suggestion = post(
        system, "admin", f"/api/sessions/{sid}/ai-review", {}, **{"Accept-Language": "en"}
    )
    assert suggestion.status_code == 200, suggestion.text
    assert suggestion.json()["source"] == "ai_suggestion"
    assert suggestion.json()["review_status"] == "awaiting_admin"
    assert suggestion.json()["audit_ids"]
    assert system.clients["teacher"].get("/api/auth/me").status_code == 200
    model.invalid_ids = True
    invalid = post(system, "admin", f"/api/sessions/{sid}/ai-review", {})
    assert invalid.status_code == 502

    with monkeypatch.context() as patch:

        def fail_audit(*args, **kwargs):
            raise RuntimeError("audit unavailable")

        patch.setattr(system.store, "audit", fail_audit)
        assert (
            post(
                system,
                "admin",
                f"/api/sessions/{sid}/dismiss",
                {"reason": "Reviewed evidence, false positive"},
            ).status_code
            == 500
        )
    with system.store.connect() as connection:
        assert (
            connection.execute(
                "SELECT review_status FROM Sessions WHERE session_id=?", (sid,)
            ).fetchone()[0]
            == "awaiting_admin"
        )
    decision = post(
        system, "admin", f"/api/sessions/{sid}/dismiss", {"reason": "Reviewed as a false positive"}
    )
    assert decision.status_code == 200
    assert system.clients["teacher"].get("/api/auth/me").status_code == 200
    blocked = post(
        system,
        "admin",
        f"/api/sessions/{sid}/block",
        {"reason": "Administrator verified the evidence"},
    )
    assert blocked.status_code == 200
    assert system.clients["teacher"].get("/api/auth/me").status_code == 401
    with system.store.connect() as connection:
        rows = connection.execute(
            "SELECT * FROM AuditLogs WHERE object_id=? AND action IN "
            "('session_dismiss','session_block','session_ai_review')",
            (sid,),
        ).fetchall()
    assert {row["action"] for row in rows} == {
        "session_dismiss",
        "session_block",
        "session_ai_review",
    }
    assert all(row["user_id"] == "core_admin" and row["ip_address"] == "192.0.2.10" for row in rows)
    assert all(row["result"] == "success" and row["occurred_at"].endswith("Z") for row in rows)
