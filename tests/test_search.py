import sqlite3
from datetime import UTC, datetime

import pytest

from app.errors import AppError
from app.schemas import SearchFilters, SummaryClaim, SummaryPlan
from app.service import AuditSearchService
from tests.conftest import FakeEmbedder, FakeLLM


def test_semantic_search_encodes_only_prefiltered_reason_text(populated_repository, settings):
    query = "құпиясөз туралы"
    llm = FakeLLM(
        filters=SearchFilters(
            start_at=datetime(2026, 10, 7, tzinfo=UTC),
            end_at=datetime(2026, 10, 14, tzinfo=UTC),
            action="login",
            result="failure",
            semantic_query=query,
        )
    )
    embedder = FakeEmbedder()
    response = AuditSearchService(populated_repository, llm, embedder, settings).search(
        "Соңғы аптада құпиясөзге қатысты сәтсіз кірулерді тап"
    )
    assert [record.audit_id for record in response.records] == [2]
    encoded_texts = [text for batch in embedder.calls for text in batch]
    assert set(encoded_texts) == {query, "Құпиясөзді енгізу қатесі."}
    assert all("demo_" not in text and "s-2" not in text for text in encoded_texts)
    assert response.records[0].score == pytest.approx(1.0)
    assert [record.audit_id for record in llm.summary_calls[0]] == [2]


def test_semantic_threshold_returns_no_results_without_invented_summary(
    populated_repository, settings
):
    query = "сәйкес келмейтін сұрау"
    llm = FakeLLM(filters=SearchFilters(action="export", semantic_query=query))
    embedder = FakeEmbedder(vectors={query: [1.0, 0.0], "Тапсырма экспортталды.": [0.0, 1.0]})
    response = AuditSearchService(populated_repository, llm, embedder, settings).search(query)
    assert response.status == "no_results"
    assert response.records == []
    assert response.summary == []
    assert llm.summary_calls == []


def test_empty_sql_result_does_not_call_embedding_or_summarization(populated_repository, settings):
    llm = FakeLLM(filters=SearchFilters(action="record_update", semantic_query="құпиясөз"))
    embedder = FakeEmbedder(error=AssertionError("Empty SQL must not load the embedding model"))
    response = AuditSearchService(populated_repository, llm, embedder, settings).search(
        "Жаңартуларды тап"
    )
    assert response.status == "no_results"
    assert response.records == []
    assert response.summary == []
    assert embedder.calls == []
    assert llm.summary_calls == []
    assert len(llm.parse_calls) == 1


def test_summary_links_each_claim_to_returned_records_and_uses_recorded_reason(
    populated_repository, settings
):
    plan = SummaryPlan(
        claims=[
            SummaryClaim(kind="count", audit_ids=[1, 2, 6]),
            SummaryClaim(kind="reason", audit_ids=[2]),
        ],
        insufficient_data=True,
    )
    llm = FakeLLM(summary=plan)
    embedder = FakeEmbedder()
    response = AuditSearchService(populated_repository, llm, embedder, settings).search(
        "Сәтсіз кірулерді түсіндір"
    )
    record_ids = {record.audit_id for record in response.records}
    assert record_ids == {1, 2, 6}
    assert len(response.summary) == 2
    assert all(set(item.audit_ids) <= record_ids for item in response.summary)
    reason_item = next(item for item in response.summary if item.audit_ids == [2])
    assert "Құпиясөзді енгізу қатесі." in reason_item.text
    assert response.insufficient_data is True
    assert embedder.calls == []


def test_summary_referencing_unreturned_audit_id_is_rejected(populated_repository, settings):
    llm = FakeLLM(
        summary=SummaryPlan(
            claims=[SummaryClaim(kind="count", audit_ids=[999])], insufficient_data=False
        )
    )
    with pytest.raises(AppError) as caught:
        AuditSearchService(populated_repository, llm, FakeEmbedder(), settings).search(
            "Кірулерді тап"
        )
    assert caught.value.code == "invalid_model_output"
    assert caught.value.status_code == 502


def test_empty_filter_returns_unclear_question_before_sql_or_models(populated_repository, settings):
    llm = FakeLLM(filters=SearchFilters())
    embedder = FakeEmbedder()
    with pytest.raises(AppError) as caught:
        AuditSearchService(populated_repository, llm, embedder, settings).search("Бірдеңе жаса")
    assert caught.value.code == "unclear_question"
    assert caught.value.status_code == 422
    assert llm.summary_calls == []
    assert embedder.calls == []


@pytest.mark.parametrize("kind", ["event", "reason"])
def test_multi_record_event_or_reason_claims_render_separately(
    populated_repository, settings, kind
):
    llm = FakeLLM(
        filters=SearchFilters(action="login"),
        summary=SummaryPlan(claims=[SummaryClaim(kind=kind, audit_ids=[1, 2])]),
    )
    response = AuditSearchService(populated_repository, llm, FakeEmbedder(), settings).search(
        "Кірулерді тап"
    )
    assert [item.audit_ids for item in response.summary] == [[1], [2]]
    if kind == "reason":
        assert all("reason (дерек ретінде)" in item.text for item in response.summary)


@pytest.mark.parametrize("stage", ["parse", "embedding", "summary"])
def test_unavailable_models_are_explicit_errors_at_every_stage(
    populated_repository, settings, stage
):
    unavailable = AppError("model_unavailable", "Local test model is unavailable", 503)
    llm = FakeLLM(
        filters=SearchFilters(
            action="login", semantic_query="құпиясөз" if stage == "embedding" else None
        ),
        parse_error=unavailable if stage == "parse" else None,
        summary_error=unavailable if stage == "summary" else None,
    )
    embedder = FakeEmbedder(error=unavailable if stage == "embedding" else None)
    with pytest.raises(AppError) as caught:
        AuditSearchService(populated_repository, llm, embedder, settings).search("Кірулерді тап")
    assert caught.value.code == "model_unavailable"
    assert caught.value.status_code == 503


def test_semantic_ranking_precedes_result_limit(populated_repository, settings):
    query = "құпиясөз"
    llm = FakeLLM(
        filters=SearchFilters(action="login", result="failure", semantic_query=query, limit=1)
    )
    embedder = FakeEmbedder(
        vectors={
            query: [1.0, 0.0],
            "Ескі кіру қатесі.": [0.0, 1.0],
            "Құпиясөзді енгізу қатесі.": [1.0, 0.0],
            "Келесі кезеңдегі кіру қатесі.": [0.0, 1.0],
        }
    )
    response = AuditSearchService(populated_repository, llm, embedder, settings).search(query)
    assert [record.audit_id for record in response.records] == [2]
    encoded_texts = {text for batch in embedder.calls for text in batch}
    assert {
        "Ескі кіру қатесі.",
        "Құпиясөзді енгізу қатесі.",
        "Келесі кезеңдегі кіру қатесі.",
    } <= encoded_texts


def test_sensitive_reason_values_are_removed_before_embedding_and_summary(repository, settings):
    reason = (
        "Демо дерек: IP 192.0.2.10; token=synthetic-token; password=synthetic-secret; "
        "old_value=synthetic-old; new_value=synthetic-new"
    )
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute(
            "INSERT INTO AuditLogs "
            "(audit_id, occurred_at, user_id, role, action, "
            "object_type, object_id, result, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                1,
                "2026-10-14T00:00:00.000000Z",
                "demo_admin",
                "admin",
                "login",
                "session",
                "synthetic-session",
                "failure",
                reason,
            ),
        )
    llm = FakeLLM(filters=SearchFilters(action="login", semantic_query="демо дерек"))
    embedder = FakeEmbedder()
    response = AuditSearchService(repository, llm, embedder, settings).search("Демо деректі тап")
    encoded_text = " ".join(text for batch in embedder.calls for text in batch)
    summary_context = " ".join(record.reason for record in llm.summary_calls[0])
    for marker in (
        "192.0.2.10",
        "synthetic-token",
        "synthetic-secret",
        "synthetic-old",
        "synthetic-new",
    ):
        assert marker not in encoded_text
        assert marker not in summary_context
        assert marker not in response.records[0].reason


@pytest.mark.parametrize(
    "vector", [[0.0, 0.0], [float("nan"), 0.0], [float("inf"), 0.0], [1e308, 1e308]]
)
def test_invalid_embedding_vectors_do_not_become_false_matches(
    populated_repository, settings, vector
):
    llm = FakeLLM(filters=SearchFilters(action="export", semantic_query="қате вектор"))
    embedder = FakeEmbedder(vectors={"Тапсырма экспортталды.": vector})
    with pytest.raises(AppError) as caught:
        AuditSearchService(populated_repository, llm, embedder, settings).search(
            "Экспорттың себебін тап"
        )
    assert caught.value.code == "model_unavailable"
    assert caught.value.status_code == 503
    assert llm.summary_calls == []
