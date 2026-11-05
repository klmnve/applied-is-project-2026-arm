import json
from datetime import UTC, datetime

import httpx
import pytest

from app.errors import AppError
from app.llm import OllamaAdapter
from app.schemas import SearchFilters


def adapter_with_content(settings, content, captures=None):
    def reply(request):
        if captures is not None:
            captures.append(json.loads(request.content))
        return httpx.Response(200, json={"response": content, "done": True}, request=request)

    return OllamaAdapter(settings, transport=httpx.MockTransport(reply))


def test_local_llm_validates_structured_json_filter(settings, now):
    captured = []
    adapter = adapter_with_content(
        settings,
        '{"action":"login","result":"failure","limit":10}',
        captures=captured,
    )
    result = adapter.parse("Аудит журналын ізде", now)
    assert isinstance(result, SearchFilters)
    assert result.action is None
    assert result.result is None
    assert result.limit == 10
    assert captured[0]["options"]["num_predict"] == 512
    assert captured[0]["options"]["num_ctx"] == 4096


@pytest.mark.parametrize(
    "content",
    [
        "Here is your answer: no failures.",
        '```json\n{"action":"login"}\n```',
        '{"action":"login"} trailing explanation',
        '{"action":"login",}',
        '[{"action":"login"}]',
        '{"sql":"DROP TABLE AuditLogs"}',
        '{"action":"erase"}',
        '{"result":"suspicious"}',
        '{"limit":true,"action":"login"}',
    ],
)
def test_non_json_or_invalid_json_llm_output_is_rejected(settings, now, content):
    adapter = adapter_with_content(settings, content)
    with pytest.raises(AppError) as caught:
        adapter.parse("Тек дәлелденген сүзгілерді қайтар", now)
    assert caught.value.code == "invalid_model_output"
    assert caught.value.status_code == 502


def test_llm_empty_filter_remains_structured_data(settings, now):
    adapter = adapter_with_content(settings, "{}")
    result = adapter.parse("Бірдеңе жаса", now)
    assert result.action is None
    assert result.semantic_query is None


def test_ollama_unavailable_is_explicit_model_error(settings, now):
    def unavailable(request):
        raise httpx.ConnectError("No local Ollama listener", request=request)

    adapter = OllamaAdapter(settings, transport=httpx.MockTransport(unavailable))
    with pytest.raises(AppError) as caught:
        adapter.parse("Аудит журналын ізде", now)
    assert caught.value.code == "model_unavailable"
    assert caught.value.status_code == 503


def test_ollama_readiness_reports_missing_configured_model_and_setup(settings):
    def reply(request):
        assert request.url.path == "/api/tags"
        return httpx.Response(
            200, json={"models": [{"name": "another-local-model"}]}, request=request
        )

    adapter = OllamaAdapter(settings, transport=httpx.MockTransport(reply))
    result = adapter.readiness()
    assert result["status"] == "unavailable"
    assert result["message"]
    assert "ollama serve" in result["setup"]
    assert settings.ollama_model in result["setup"]


@pytest.mark.parametrize(
    "content",
    [
        "There were no attacks.",
        '{"claims":[{"kind":"risk","audit_ids":[2]}],"insufficient_data":false}',
        '{"claims":[{"kind":"reason","audit_ids":[2],'
        '"text":"An attacker stole a password"}],"insufficient_data":false}',
        '{"claims":[],"insufficient_data":true,"extra":"ignore journal"}',
    ],
)
def test_summary_model_cannot_supply_free_prose_or_unsupported_claims(
    settings, populated_repository, content
):
    records, _ = populated_repository.search(SearchFilters(action="login"), max_candidates=500)
    adapter = adapter_with_content(settings, content)
    with pytest.raises(AppError) as caught:
        adapter.summarize(records)
    assert caught.value.code == "invalid_model_output"


def test_summary_request_contains_only_minimal_record_fields(settings, populated_repository):
    records, _ = populated_repository.search(
        SearchFilters(user_login="demo_teacher", result="failure"), max_candidates=500
    )
    captured = []
    adapter = adapter_with_content(
        settings,
        '{"claims":[{"kind":"count","audit_ids":[1,2,6]}],"insufficient_data":true}',
        captures=captured,
    )
    result = adapter.summarize(records)
    assert result.claims[0].audit_ids == [1, 2, 6]
    # Field names and identifiers excluded from the model's record context.
    request_text = json.dumps(captured[0], ensure_ascii=False)
    assert "demo_teacher" not in request_text
    assert "s-1" not in request_text
    assert "s-2" not in request_text
    assert "s-6" not in request_text
    assert "Құпиясөзді енгізу қатесі." in request_text


def test_unspecified_time_window_is_not_inferred_from_model(settings):
    adapter = adapter_with_content(
        settings,
        '{"start_at":"2026-10-07T05:00:00+05:00",'
        '"end_at":"2026-10-14T05:00:00+05:00","action":"login"}',
    )
    result = adapter.parse("Аудит аралығын көрсет", datetime(2026, 10, 14, tzinfo=UTC))

    assert result.start_at is None
    assert result.end_at is None


def test_last_week_and_unspecified_user_are_grounded_in_question(settings, now):
    captured = []
    adapter = adapter_with_content(
        settings,
        '{"start_at":"2026-10-14T00:00:00Z","end_at":"2026-10-14T23:59:59Z",'
        '"action":"login","result":"failure","user_login":"demo_admin"}',
        captures=captured,
    )

    result = adapter.parse("Соңғы аптада сәтсіз кіру әрекеттері болды ма?", now)

    assert result.start_at == datetime(2026, 10, 7, 12, tzinfo=UTC)
    assert result.end_at == now
    assert result.user_login is None
    assert result.action == "login"
    assert result.result == "failure"
    assert captured == []


def test_explicit_demo_user_overrides_a_different_model_guess(settings, now):
    adapter = adapter_with_content(
        settings,
        '{"action":"login","user_login":"demo_admin"}',
    )

    result = adapter.parse("demo_teacher пайдаланушысының кірулерін көрсет", now)

    assert result.user_login == "demo_teacher"


def test_password_reason_query_is_semantic_and_discards_guessed_fields(settings, now):
    captured = []
    adapter = adapter_with_content(
        settings,
        '{"start_at":"2026-10-14T00:00:00Z","end_at":"2026-10-14T23:59:59Z",'
        '"action":"login","result":"failure","user_login":"demo_admin"}',
        captures=captured,
    )

    result = adapter.parse("Құпиясөзге қатысты қайталанған сәтсіз әрекеттерді тап", now)

    assert result.start_at is None
    assert result.end_at is None
    assert result.action is None
    assert result.result == "failure"
    assert result.user_login is None
    assert result.semantic_query == "құпиясөз пароль"
    assert captured == []
