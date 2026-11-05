"""Expanded audit actions retain bounded intent and exclude private audit columns."""

import json

import httpx
import pytest

from app.auth import Principal
from app.llm import OllamaAdapter
from app.schemas import Action, SearchFilters
from app.service import AuditSearchService
from app.store import Store
from tests.conftest import FakeEmbedder


def no_generation_adapter(settings):
    def unexpected(request):
        raise AssertionError("Explicit bounded intent must retain the existing fast path")

    return OllamaAdapter(settings, transport=httpx.MockTransport(unexpected))


@pytest.mark.parametrize(
    "question,action",
    [
        ("Баға өзгерістерін көрсет", Action.GRADE_CHANGE),
        ("Ведомость өзгерістерін көрсет", Action.GRADE_CHANGE),
        ("Құжат жүктеу оқиғаларын тап", Action.DOCUMENT_UPLOAD),
        ("Құжатты жою оқиғаларын тап", Action.DOCUMENT_DELETE),
        ("Құжатты қарау оқиғаларын тап", Action.DOCUMENT_VIEW),
        ("Құжатты жүктеп алу оқиғаларын тап", Action.DOCUMENT_VIEW),
        ("Document download events", Action.DOCUMENT_VIEW),
        ("Сессия бұғаттау оқиғаларын тап", Action.SESSION_BLOCK),
        ("Сессияны бұғаттан шығару оқиғаларын тап", Action.SESSION_UNBLOCK),
        ("Session unblock events", Action.SESSION_UNBLOCK),
        ("Сессияны аяқтау оқиғаларын тап", Action.SESSION_TERMINATE),
        ("Session terminate events", Action.SESSION_TERMINATE),
        ("Сессия мерзімі аяқталған оқиғаларды тап", Action.SESSION_EXPIRED),
        ("Session expired events", Action.SESSION_EXPIRED),
        ("Күдікті сессия оқиғаларын тап", Action.SESSION_FLAGGED),
        ("Құқық өзгерістерін көрсет", Action.PERMISSION_CHANGE),
        ("Рөл өзгерістерін көрсет", Action.USER_CHANGE),
    ],
)
def test_current_business_actions_are_grounded(settings, now, question, action):
    result = no_generation_adapter(settings).parse(question, now)
    assert result.action == action
    assert result.user_login is None
    assert result.result is None


@pytest.mark.parametrize("label", ["login: teacher_one", "login=teacher_one", "логин: teacher_one"])
def test_login_label_does_not_override_grade_action(settings, now, label):
    result = no_generation_adapter(settings).parse("Баға өзгерістерін тап, " + label, now)
    assert result.action == Action.GRADE_CHANGE
    assert result.user_login == "teacher_one"
    assert result.result is None


@pytest.mark.parametrize(
    "login", ["failure_teacher", "success_admin", "password_login", "today_user"]
)
@pytest.mark.parametrize("template", ["login: {login}", "{login} пайдаланушысының"])
def test_identifier_words_do_not_add_intent(settings, now, login, template):
    question = "Құжат жүктеу оқиғаларын тап, " + template.format(login=login)
    result = no_generation_adapter(settings).parse(question, now)
    assert result.action == Action.DOCUMENT_UPLOAD
    assert result.user_login == login
    assert result.result is None
    assert result.start_at is None and result.end_at is None
    assert result.semantic_query is None


def test_explicit_result_and_time_remain_grounded_outside_login_metadata(settings, now):
    result = no_generation_adapter(settings).parse(
        "Соңғы аптада сәтсіз кірулерді тап, login: success_admin", now
    )
    assert result.action == Action.LOGIN and result.result == "failure"
    assert result.user_login == "success_admin"
    assert result.start_at is not None and result.end_at == now


def test_real_business_audit_private_columns_never_enter_summary_prompt(settings, repository):
    store = Store(settings)
    store.initialize()
    actor = Principal(
        user_id=1,
        login="synthetic_teacher",
        display_name="Synthetic teacher",
        role_code="teacher",
        permissions=frozenset({"Audit.View"}),
        session_id="private-session-marker",
    )
    with store.connect(write=True) as connection:
        audit_id = store.audit(
            connection,
            actor,
            "grade_change",
            "grade",
            "private-object-marker",
            "success",
            "Синтетикалық баға өзгертілді.",
            "192.0.2.72",
            old_value={"private-old-marker": 20},
            new_value={"private-new-marker": 80},
        )
    requests = []

    def generate(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(
            200,
            request=request,
            json={
                "response": json.dumps({"claims": [{"kind": "event", "audit_ids": [audit_id]}]}),
                "done": True,
            },
        )

    adapter = OllamaAdapter(settings, transport=httpx.MockTransport(generate))
    response = AuditSearchService(repository, adapter, FakeEmbedder(), settings).search(
        "Баға өзгерістерін көрсет"
    )
    assert [record.audit_id for record in response.records] == [audit_id]
    assert response.summary[0].audit_ids == [audit_id]
    assert len(requests) == 1
    prompt = json.loads(requests[0]["prompt"])
    assert set(prompt["untrusted_records"][0]) == {
        "audit_id",
        "occurred_at",
        "action",
        "result",
        "reason",
    }
    serialized = json.dumps(requests, ensure_ascii=False)
    for marker in (
        "192.0.2.72",
        "private-old-marker",
        "private-new-marker",
        "private-session-marker",
        "private-object-marker",
        "synthetic_teacher",
        "old_value",
        "new_value",
        "ip_address",
    ):
        assert marker not in serialized
    assert requests[0]["options"]["num_predict"] == 512
    assert requests[0]["options"]["num_ctx"] == 4096


def test_arbitrary_registered_login_shape_is_valid_but_extra_ai_fields_are_not():
    assert SearchFilters(user_login="teacher.one-2").user_login == "teacher.one-2"
    assert SearchFilters(ip_address="192.0.2.72").ip_address == "192.0.2.72"
    with pytest.raises(ValueError):
        SearchFilters.model_validate({"user_login": "teacher.one-2", "sql": "SELECT * FROM Users"})


def structured_adapter(settings, payload, requests):
    def generate(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, request=request, json={"response": json.dumps(payload), "done": True}
        )

    return OllamaAdapter(settings, transport=httpx.MockTransport(generate))


def test_explicit_unfamiliar_reason_intent_uses_validated_local_model_filter(settings, now):
    requests = []
    adapter = structured_adapter(
        settings,
        {"semantic_query": "файл дайындау қатесі", "result": "success", "action": "export"},
        requests,
    )
    result = adapter.parse(
        "Сәтсіз оқиғалардың reason мәтінінен файл дайындау қатесіне қатысты жазбаларды тап", now
    )
    assert len(requests) == 1
    assert result.semantic_query == "файл дайындау қатесі"
    assert result.result == "failure"
    assert result.action is None


def test_explicit_iso_date_range_is_grounded_without_model_date_guesses(settings, now):
    requests = []
    adapter = structured_adapter(
        settings,
        {
            "start_at": "2026-10-01T00:00:00Z",
            "end_at": "2026-10-11T00:00:00Z",
            "action": "login",
            "user_login": "invented_admin",
        },
        requests,
    )
    result = adapter.parse("2026-10-01 мен 2026-10-10 аралығындағы баға өзгерістерін тап", now)
    assert requests == []
    assert result.start_at.isoformat() == "2026-10-01T00:00:00+00:00"
    assert result.end_at.isoformat() == "2026-10-11T00:00:00+00:00"
    assert result.action == Action.GRADE_CHANGE and result.user_login is None


def test_generic_question_discards_unrequested_reason_and_date_guesses(settings, now):
    requests = []
    adapter = structured_adapter(
        settings,
        {"start_at": "2026-10-01T00:00:00Z", "semantic_query": "invented topic"},
        requests,
    )
    result = adapter.parse("Аудит журналын ізде", now)
    assert len(requests) == 1
    assert result.start_at is None and result.end_at is None
    assert result.semantic_query is None
