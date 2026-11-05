import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.config import Settings
from app.db import AuditRepository
from app.schemas import SearchFilters, SummaryClaim, SummaryPlan


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(database_path=tmp_path / "test-audit.sqlite3")


@pytest.fixture
def repository(settings: Settings) -> AuditRepository:
    repo = AuditRepository(settings.database_path)
    repo.initialize()
    return repo


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 10, 14, 12, tzinfo=UTC)


@pytest.fixture
def populated_repository(repository: AuditRepository, settings: Settings):
    """Fixed synthetic events make SQL boundaries and semantic inputs observable."""
    events = [
        (
            1,
            "2026-10-06T23:59:59.000000Z",
            "demo_teacher",
            "teacher",
            "login",
            "session",
            "s-1",
            "failure",
            "Ескі кіру қатесі.",
        ),
        (
            2,
            "2026-10-07T00:00:00.000000Z",
            "demo_teacher",
            "teacher",
            "login",
            "session",
            "s-2",
            "failure",
            "Құпиясөзді енгізу қатесі.",
        ),
        (
            3,
            "2026-10-08T12:00:00.000000Z",
            "demo_admin",
            "admin",
            "export",
            "report",
            "r-1",
            "success",
            "Тапсырма экспортталды.",
        ),
        (
            4,
            "2026-10-09T12:00:00.000000Z",
            "demo_teacher",
            "teacher",
            "login",
            "session",
            "s-4",
            "success",
            "Кіру сәтті аяқталды.",
        ),
        (
            5,
            "2026-10-10T12:00:00.000000Z",
            "demo_assistant",
            "assistant",
            "password_change",
            "account",
            "a-1",
            "failure",
            "Пароль не соответствует правилам.",
        ),
        (
            6,
            "2026-10-14T00:00:00.000000Z",
            "demo_teacher",
            "teacher",
            "login",
            "session",
            "s-6",
            "failure",
            "Келесі кезеңдегі кіру қатесі.",
        ),
    ]
    with sqlite3.connect(settings.database_path) as connection:
        connection.executemany(
            "INSERT INTO AuditLogs "
            "(audit_id, occurred_at, user_id, role, action, "
            "object_type, object_id, result, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            events,
        )
    return repository


class FakeLLM:
    """An explicit test-only model adapter, injected by individual tests."""

    def __init__(self, filters=None, summary=None, parse_error=None, summary_error=None):
        self.filters = filters or SearchFilters(action="login", result="failure")
        self.summary = summary
        self.parse_error = parse_error
        self.summary_error = summary_error
        self.parse_calls = []
        self.summary_calls = []

    def parse(self, question: str, now: datetime) -> SearchFilters:
        self.parse_calls.append((question, now))
        if self.parse_error:
            raise self.parse_error
        return self.filters

    def summarize(self, records):
        self.summary_calls.append(records)
        if self.summary_error:
            raise self.summary_error
        if self.summary is not None:
            return self.summary
        return SummaryPlan(
            claims=[SummaryClaim(kind="count", audit_ids=[record.audit_id for record in records])],
            insufficient_data=True,
        )

    def readiness(self):
        return {"status": "ready", "message": "Test adapter is ready.", "setup": []}


class FakeEmbedder:
    def __init__(self, vectors=None, error=None):
        self.vectors = vectors or {}
        self.error = error
        self.calls = []

    def encode(self, texts):
        self.calls.append(list(texts))
        if self.error:
            raise self.error
        return [self.vectors.get(text, [1.0, 0.0]) for text in texts]

    def readiness(self):
        return {"status": "ready", "message": "Test adapter is ready.", "setup": []}
