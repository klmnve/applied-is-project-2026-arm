from datetime import UTC, datetime, timedelta

from app.schemas import SearchFilters


def test_seed_is_synthetic_and_idempotent(repository, now):
    inserted = repository.seed_demo(now=now)
    initial_count = repository.count()
    assert inserted >= 30
    assert initial_count >= 30
    assert repository.seed_demo(now=now) == 0
    assert repository.count() == initial_count
    records, truncated = repository.search(SearchFilters(limit=50), max_candidates=500)
    assert not truncated
    assert {record.user_id for record in records} <= {
        "demo_admin",
        "demo_teacher",
        "demo_assistant",
    }
    assert len({record.user_id for record in records}) >= 3
    assert len({record.action for record in records}) >= 4
    assert all(record.occurred_at.utcoffset().total_seconds() == 0 for record in records)


def test_sql_filters_date_action_result_and_user_with_half_open_interval(populated_repository):
    filters = SearchFilters(
        start_at=datetime(2026, 10, 7, tzinfo=UTC),
        end_at=datetime(2026, 10, 14, tzinfo=UTC),
        action="login",
        result="failure",
        user_login="demo_teacher",
    )
    records, truncated = populated_repository.search(filters, max_candidates=500)
    assert [record.audit_id for record in records] == [2]
    assert not truncated


def test_repository_parameter_binding_does_not_expand_sql_injection(populated_repository):
    # The public schema rejects this; bypass it to verify the SQL boundary independently.
    filters = SearchFilters.model_construct(
        start_at=None,
        end_at=None,
        action=None,
        result=None,
        user_login="demo_teacher' OR 1=1 --",
        semantic_query=None,
        limit=20,
    )
    records, _ = populated_repository.search(filters, max_candidates=500)
    assert records == []
    assert populated_repository.count() == 6


def test_repository_reports_truncated_candidates(populated_repository):
    records, truncated = populated_repository.search(SearchFilters(), max_candidates=2)
    assert len(records) == 2
    assert truncated is True


def test_seed_preserves_microsecond_precision_at_range_boundaries(repository):
    anchor = datetime(2026, 10, 14, 12, 0, 0, 123456, tzinfo=UTC)
    repository.seed_demo(now=anchor)
    first_event_time = anchor - timedelta(hours=1)
    included, _ = repository.search(
        SearchFilters(
            start_at=first_event_time, end_at=first_event_time + timedelta(microseconds=1)
        ),
        max_candidates=500,
    )
    assert [record.audit_id for record in included] == [1001]
    assert included[0].occurred_at.microsecond == 123456
    excluded, _ = repository.search(
        SearchFilters(
            start_at=first_event_time - timedelta(microseconds=1), end_at=first_event_time
        ),
        max_candidates=500,
    )
    assert excluded == []
