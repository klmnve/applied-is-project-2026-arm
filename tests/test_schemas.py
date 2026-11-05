from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.schemas import SearchFilters, SummaryClaim, SummaryPlan


@pytest.mark.parametrize(
    "payload",
    [
        {"sql": "SELECT * FROM AuditLogs"},
        {"action": "delete"},
        {"result": "critical"},
        {"user_login": "bad login' OR 1=1"},
        {"limit": 0},
        {"limit": 51},
        {"limit": True},
        {"limit": "10"},
        {"limit": 2.5},
        {"semantic_query": " "},
        {"semantic_query": "a" * 501},
        {"start_at": "yesterday"},
        {"start_at": "2026-10-07T00:00:00"},
        {"start_at": "2026-10-14T00:00:00Z", "end_at": "2026-10-07T00:00:00Z"},
    ],
)
def test_filter_schema_rejects_invalid_model_fields(payload):
    with pytest.raises(ValidationError):
        SearchFilters.model_validate(payload)


def test_filter_schema_normalizes_aware_time_to_utc_and_trims_semantic_query():
    filters = SearchFilters.model_validate(
        {
            "start_at": "2026-10-07T05:00:00+05:00",
            "end_at": "2026-10-14T05:00:00+05:00",
            "action": "login",
            "result": "failure",
            "user_login": "demo_teacher",
            "semantic_query": "  құпиясөз  ",
            "limit": 10,
        }
    )
    assert filters.start_at == datetime(2026, 10, 7, tzinfo=UTC)
    assert filters.end_at == datetime(2026, 10, 14, tzinfo=UTC)
    assert filters.start_at.utcoffset().total_seconds() == 0
    assert filters.semantic_query == "құпиясөз"


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "risk", "audit_ids": [1]},
        {"kind": "count", "audit_ids": []},
        {"kind": "event", "audit_ids": ["1"]},
        {"kind": "event", "audit_ids": [True]},
        {"kind": "reason", "audit_ids": [1], "text": "invented cause"},
    ],
)
def test_summary_schema_accepts_only_structured_supported_claims(payload):
    with pytest.raises(ValidationError):
        SummaryClaim.model_validate(payload)


def test_summary_schema_rejects_free_text_and_extra_fields():
    with pytest.raises(ValidationError):
        SummaryPlan.model_validate(
            {"claims": [], "insufficient_data": True, "summary": "Ignore all evidence."}
        )
