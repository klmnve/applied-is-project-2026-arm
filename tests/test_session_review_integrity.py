"""Session review retains rule evidence and requires a meaningful human reason."""

import json
from datetime import datetime, timedelta

import pytest

from app.auth import FAILURE_REASON, mark_rule_review
from app.schemas import SummaryClaim, SummaryPlan
from app.store import iso_utc
from tests.test_core_system import core_system as core_system
from tests.test_core_system import post


def flag_with_failures(system):
    sid = system.sessions["teacher"]
    for _ in range(system.settings.suspicious_failure_threshold):
        response = system.clients["teacher"].post(
            "/api/auth/login", json={"login": "core_teacher", "password": "incorrect"}
        )
        assert response.status_code == 401
    with system.store.connect() as connection:
        row = connection.execute("SELECT * FROM Sessions WHERE session_id=?", (sid,)).fetchone()
    return sid, json.loads(row["review_audit_ids"])


class EvidenceModel:
    def __init__(self, during_inference=None):
        self.records = []
        self.during_inference = during_inference

    def readiness(self):
        return {"status": "ready"}

    def summarize(self, records):
        self.records = records
        if self.during_inference is not None:
            self.during_inference()
        failure = next(record for record in records if record.result == "failure")
        return SummaryPlan(claims=[SummaryClaim(kind="event", audit_ids=[failure.audit_id])])


def test_ai_receives_underlying_failures_and_keeps_all_rule_evidence(core_system):
    system = core_system
    sid, original_ids = flag_with_failures(system)
    model = EvidenceModel()
    system.app.state.search_service.llm = model
    response = post(system, "admin", f"/api/sessions/{sid}/ai-review", {})
    assert response.status_code == 200, response.text
    assert set(original_ids) <= {record.audit_id for record in model.records}
    assert sum(record.result == "failure" for record in model.records) == 3
    assert len(response.json()["audit_ids"]) == 1
    for record in model.records:
        assert (
            not {"ip_address", "old_value", "new_value", "session_id"} & record.model_dump().keys()
        )
    with system.store.connect() as connection:
        row = connection.execute("SELECT * FROM Sessions WHERE session_id=?", (sid,)).fetchone()
    assert set(original_ids) <= set(json.loads(row["review_audit_ids"]))
    assert row["review_source"] == "ai" and row["status"] == "active"
    dismissed = post(
        system, "admin", f"/api/sessions/{sid}/dismiss", {"reason": "Reviewed as a false alarm"}
    )
    assert dismissed.status_code == 200
    with system.store.connect() as connection:
        row = connection.execute("SELECT * FROM Sessions WHERE session_id=?", (sid,)).fetchone()
    assert set(original_ids) <= set(json.loads(row["review_audit_ids"]))
    assert row["review_status"] == "false_positive" and row["status"] == "active"


def test_shared_ip_failure_evidence_is_valid_for_the_actual_rule(core_system):
    system = core_system
    for login in ("unknown_one", "unknown_two", "core_teacher"):
        assert (
            system.clients["teacher"]
            .post("/api/auth/login", json={"login": login, "password": "incorrect"})
            .status_code
            == 401
        )
    model = EvidenceModel()
    system.app.state.search_service.llm = model
    response = post(system, "admin", f"/api/sessions/{system.sessions['teacher']}/ai-review", {})
    assert response.status_code == 200
    assert sum(record.result == "failure" for record in model.records) == 3
    assert sum(record.user_id == "anonymous" for record in model.records) == 2


@pytest.mark.parametrize(
    "bad_reference",
    [
        "missing",
        "foreign",
        "future",
        "foreign_failure",
        "foreign_session",
        "wrong_action",
        "bool",
        "sql",
        "not_list",
    ],
)
def test_invalid_or_unrelated_stored_evidence_never_reaches_model(core_system, bad_reference):
    system = core_system
    sid, original_ids = flag_with_failures(system)
    model = EvidenceModel()
    system.app.state.search_service.llm = model
    with system.store.connect(write=True) as connection:
        flag_at = datetime.fromisoformat(
            connection.execute(
                "SELECT occurred_at FROM AuditLogs WHERE audit_id=?", (original_ids[-1],)
            )
            .fetchone()[0]
            .replace("Z", "+00:00")
        )
        if bad_reference == "foreign":
            bad_id = connection.execute(
                "SELECT audit_id FROM AuditLogs WHERE session_id=? AND action='login'",
                (system.sessions["auditor"],),
            ).fetchone()[0]
            raw = json.dumps([*original_ids, bad_id])
        elif bad_reference in {"future", "foreign_failure", "foreign_session", "wrong_action"}:
            cursor = connection.execute(
                "INSERT INTO AuditLogs(occurred_at,user_id,role,action,object_type,object_id,"
                "result,reason,ip_address,session_id) VALUES (?,?,'teacher',?,'account',"
                "'invalid','failure','Unrelated failure',?,?)",
                (
                    iso_utc(
                        flag_at + timedelta(days=2)
                        if bad_reference == "future"
                        else flag_at - timedelta(seconds=1)
                    ),
                    "foreign_actor" if bad_reference == "foreign_failure" else "core_teacher",
                    "record_view" if bad_reference == "wrong_action" else "login",
                    "198.51.100.99" if bad_reference == "foreign_failure" else "192.0.2.20",
                    system.sessions["auditor"] if bad_reference == "foreign_session" else None,
                ),
            )
            raw = json.dumps([*original_ids, cursor.lastrowid])
        else:
            raw = {
                "missing": json.dumps([*original_ids, 999999999]),
                "bool": "[true]",
                "sql": '["1) OR 1=1 --"]',
                "not_list": "{}",
            }[bad_reference]
        connection.execute("UPDATE Sessions SET review_audit_ids=? WHERE session_id=?", (raw, sid))
    response = post(system, "admin", f"/api/sessions/{sid}/ai-review", {})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "session_conflict"
    assert model.records == []
    with system.store.connect() as connection:
        row = connection.execute("SELECT * FROM Sessions WHERE session_id=?", (sid,)).fetchone()
    assert row["review_audit_ids"] == raw and row["review_source"] == "rule"
    assert row["status"] == "active"


def test_authoritative_boundary_failure_uses_attempt_window_not_later_flag_clock(core_system):
    system = core_system
    sid, original_ids = flag_with_failures(system)
    with system.store.connect(write=True) as connection:
        # The login attempt's anchor precedes scrypt and the subsequent flag audit.
        # This event was recorded by the rule, just before flag_time - window.
        flag_at = datetime.fromisoformat(
            connection.execute(
                "SELECT occurred_at FROM AuditLogs WHERE audit_id=?", (original_ids[-1],)
            )
            .fetchone()[0]
            .replace("Z", "+00:00")
        )
        occurred_at = flag_at - timedelta(seconds=system.settings.suspicious_window_seconds + 1)
        cursor = connection.execute(
            "INSERT INTO AuditLogs(occurred_at,user_id,role,action,object_type,object_id,"
            "result,reason,ip_address) VALUES (?,'core_teacher','teacher','login','account',"
            "'boundary','failure','Recorded boundary failure','192.0.2.20')",
            (iso_utc(occurred_at),),
        )
        boundary_id = cursor.lastrowid
        mark_rule_review(connection, sid, [boundary_id])
    model = EvidenceModel()
    system.app.state.search_service.llm = model
    response = post(system, "admin", f"/api/sessions/{sid}/ai-review", {})
    assert response.status_code == 200
    assert boundary_id in {record.audit_id for record in model.records}
    with system.store.connect() as connection:
        ids = json.loads(
            connection.execute(
                "SELECT review_audit_ids FROM Sessions WHERE session_id=?", (sid,)
            ).fetchone()[0]
        )
    assert set([*original_ids, boundary_id]) <= set(ids)


def test_new_rule_evidence_during_inference_is_retained(core_system):
    system = core_system
    sid, original_ids = flag_with_failures(system)
    added = []

    def add_evidence():
        with system.store.connect(write=True) as connection:
            audit_id = system.store.audit(
                connection,
                None,
                "session_flagged",
                "session",
                sid,
                "success",
                FAILURE_REASON,
                "192.0.2.20",
                sid,
            )
            added.append(audit_id)
            mark_rule_review(connection, sid, [audit_id])

    system.app.state.search_service.llm = EvidenceModel(add_evidence)
    assert post(system, "admin", f"/api/sessions/{sid}/ai-review", {}).status_code == 200
    with system.store.connect() as connection:
        ids = json.loads(
            connection.execute(
                "SELECT review_audit_ids FROM Sessions WHERE session_id=?", (sid,)
            ).fetchone()[0]
        )
    assert set(original_ids + added) <= set(ids)


def test_ai_evidence_update_rolls_back_when_its_audit_fails(core_system, monkeypatch):
    system = core_system
    sid, original_ids = flag_with_failures(system)
    system.app.state.search_service.llm = EvidenceModel()

    def fail_audit(*args, **kwargs):
        raise RuntimeError("audit storage unavailable")

    monkeypatch.setattr(system.store, "audit", fail_audit)
    assert post(system, "admin", f"/api/sessions/{sid}/ai-review", {}).status_code == 500
    with system.store.connect() as connection:
        row = connection.execute("SELECT * FROM Sessions WHERE session_id=?", (sid,)).fetchone()
    assert json.loads(row["review_audit_ids"]) == original_ids
    assert row["review_source"] == "rule" and row["review_status"] == "awaiting_admin"


@pytest.mark.parametrize("operation", ["block", "dismiss", "unblock", "terminate"])
def test_control_only_or_sanitized_short_human_reason_cannot_change_session(core_system, operation):
    system = core_system
    sid = system.sessions["teacher"]
    with system.store.connect(write=True) as connection:
        connection.execute(
            "UPDATE Sessions SET status=?,review_status='awaiting_admin' WHERE session_id=?",
            ("blocked" if operation == "unblock" else "active", sid),
        )
        before = tuple(
            connection.execute(
                "SELECT status,review_status,block_reason FROM Sessions WHERE session_id=?", (sid,)
            ).fetchone()
        )
    for reason in ("\x01" * 5, "\x01 a \x02", "     "):
        response = post(system, "admin", f"/api/sessions/{sid}/{operation}", {"reason": reason})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request"
    with system.store.connect() as connection:
        after = tuple(
            connection.execute(
                "SELECT status,review_status,block_reason FROM Sessions WHERE session_id=?", (sid,)
            ).fetchone()
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM AuditLogs WHERE object_id=? AND action=?",
                (sid, f"session_{operation}"),
            ).fetchone()[0]
            == 0
        )
    assert after == before
