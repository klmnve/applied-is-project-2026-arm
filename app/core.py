"""Dashboard, append-only audit reading and human-managed session controls."""

import json
import sqlite3
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.auth import FAILURE_REASON, Principal, principal_from_connection, require_permission
from app.errors import AppError, invalid_output
from app.i18n import request_locale
from app.network import client_ip
from app.safety import safe_reason
from app.schemas import Action, AuditRecord, Result, SummaryPlan, utc_datetime
from app.service import render_summary
from app.store import Store, iso_utc

router = APIRouter(prefix="/api", tags=["Administration and audit"])


def _store(request: Request) -> Store:
    store: Store = request.app.state.store
    return store


class SessionReason(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    reason: str = Field(min_length=5, max_length=300)

    @field_validator("reason")
    @classmethod
    def safe_text(cls, value: str) -> str:
        sanitized = safe_reason(value).strip()
        if len(sanitized) < 5:
            raise ValueError("Шешімнің қысқаша себебін көрсетіңіз")
        return sanitized


def _review_ids(raw: str) -> list[int]:
    try:
        ids = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise AppError("session_conflict", "Сессия дәлелдерін жаңартып көріңіз.", 409) from exc
    if (
        not isinstance(ids, list)
        or any(type(value) is not int or value < 1 for value in ids)
        or len(ids) != len(set(ids))
    ):
        raise AppError("session_conflict", "Сессия дәлелдерін жаңартып көріңіз.", 409)
    return ids


def _review_rows(
    connection: sqlite3.Connection, session_id: str, raw_ids: str
) -> tuple[list[int], list[sqlite3.Row]]:
    """Trust server-recorded references only after checking their session/rule scope.

    A rule's audit timestamp is later than its login-attempt window anchor. Do not
    reconstruct that historical lower bound from this different clock.
    """
    ids = _review_ids(raw_ids)
    if not ids:
        return ids, []
    owner = connection.execute(
        "SELECT u.login FROM Sessions s JOIN Users u ON u.user_id=s.user_id WHERE s.session_id=?",
        (session_id,),
    ).fetchone()
    if owner is None:
        raise AppError("not_found", "Сессия табылмады.", 404)
    fields = (
        "audit_id,occurred_at,user_id,role,action,object_type,object_id,result,reason,"
        "ip_address,session_id"
    )
    rows = connection.execute(
        "SELECT "
        + fields
        + " FROM AuditLogs WHERE audit_id IN ("
        + ",".join("?" for _ in ids)
        + ")",
        ids,
    ).fetchall()
    flags = connection.execute(
        "SELECT occurred_at,ip_address FROM AuditLogs WHERE session_id=? "
        "AND object_type='session' AND object_id=? AND action='session_flagged' "
        "AND result='success' AND reason=?",
        (session_id, session_id, FAILURE_REASON),
    ).fetchall()
    windows = [
        (
            utc_datetime(datetime.fromisoformat(flag["occurred_at"].replace("Z", "+00:00"))),
            flag["ip_address"],
        )
        for flag in flags
    ]
    if len(rows) != len(ids):
        raise AppError("session_conflict", "Сессия дәлелдерін жаңартып көріңіз.", 409)
    for row in rows:
        own_session = row["session_id"] == session_id or (
            row["object_type"] == "session" and row["object_id"] == session_id
        )
        occurred_at = utc_datetime(
            datetime.fromisoformat(row["occurred_at"].replace("Z", "+00:00"))
        )
        rule_failure = (
            row["action"] == "login"
            and row["result"] == "failure"
            and not row["session_id"]
            and any(
                occurred_at <= flagged_at
                and (row["user_id"] == owner["login"] or row["ip_address"] == flag_ip)
                for flagged_at, flag_ip in windows
            )
        )
        if not own_session and not rule_failure:
            raise AppError("session_conflict", "Сессия дәлелдерін жаңартып көріңіз.", 409)
    return ids, rows


@router.get("/dashboard")
def dashboard(
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Dashboard.View"))],
) -> dict[str, Any]:
    store = _store(request)
    kpis: dict[str, int | None] = {
        "users": None,
        "active_users": None,
        "students": None,
        "documents": None,
        "active_sessions": None,
        "suspicious_sessions": None,
        "audit_events": None,
    }
    with store.connect() as connection:
        if "Users.Manage" in actor.permissions:
            kpis["users"] = connection.execute("SELECT COUNT(*) FROM Users").fetchone()[0]
            kpis["active_users"] = connection.execute(
                "SELECT COUNT(*) FROM Users WHERE active=1"
            ).fetchone()[0]
        if "Grades.View" in actor.permissions:
            if actor.role_code == "teacher":
                kpis["students"] = connection.execute(
                    """SELECT COUNT(*) FROM Students s WHERE EXISTS
                    (SELECT 1 FROM ActiveTeachingAssignments a
                     WHERE a.group_id=s.group_id AND a.user_id=?)""",
                    (actor.user_id,),
                ).fetchone()[0]
            elif actor.role_code in {"admin", "head"}:
                kpis["students"] = connection.execute("SELECT COUNT(*) FROM Students").fetchone()[0]
        if "Documents.View" in actor.permissions:
            if actor.role_code == "teacher":
                kpis["documents"] = connection.execute(
                    """SELECT COUNT(*) FROM Documents d WHERE d.deleted_at IS NULL
                    AND EXISTS (SELECT 1 FROM ActiveTeachingAssignments a WHERE a.user_id=?
                    AND a.group_id=d.group_id AND a.subject_id=d.subject_id
                    AND a.term_id=d.term_id)""",
                    (actor.user_id,),
                ).fetchone()[0]
            elif actor.role_code in {"admin", "head"}:
                kpis["documents"] = connection.execute(
                    "SELECT COUNT(*) FROM Documents WHERE deleted_at IS NULL"
                ).fetchone()[0]
        if "Sessions.Manage" in actor.permissions:
            kpis["active_sessions"] = connection.execute(
                "SELECT COUNT(*) FROM Sessions WHERE status='active' AND expires_at>?", (iso_utc(),)
            ).fetchone()[0]
            kpis["suspicious_sessions"] = connection.execute(
                "SELECT COUNT(*) FROM Sessions WHERE suspicious_reason<>'' "
                "AND review_status='awaiting_admin' AND status='active' AND expires_at>?",
                (iso_utc(),),
            ).fetchone()[0]
        recent = []
        if "Audit.View" in actor.permissions:
            kpis["audit_events"] = connection.execute("SELECT COUNT(*) FROM AuditLogs").fetchone()[
                0
            ]
            recent = [
                store.audit_row(row)
                for row in connection.execute(
                    "SELECT * FROM AuditLogs ORDER BY occurred_at DESC,audit_id DESC LIMIT 6"
                )
            ]
        assignments = [
            dict(row)
            for row in connection.execute(
                """SELECT a.assignment_id,
            g.name AS group_name,s.name AS subject_name,t.name AS term_name
            FROM ActiveTeachingAssignments a JOIN Groups g ON g.group_id=a.group_id
            JOIN Subjects s ON s.subject_id=a.subject_id JOIN Terms t ON t.term_id=a.term_id
            WHERE a.user_id=? ORDER BY g.name,s.name""",
                (actor.user_id,),
            )
        ]
    return {"kpis": kpis, "recent_audit": recent, "my_assignments": assignments}


@router.get("/audit/options")
def audit_options(
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Audit.View"))],
) -> dict[str, Any]:
    with _store(request).connect() as connection:
        actors = [
            row[0]
            for row in connection.execute("SELECT DISTINCT user_id FROM AuditLogs ORDER BY user_id")
        ]
    return {
        "actions": [action.value for action in Action],
        "results": [result.value for result in Result],
        "actors": actors,
    }


@router.get("/audit")
def audit_list(
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("Audit.View"))],
    page: Annotated[int, Query(ge=1, le=100000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    actor: Annotated[str | None, Query(max_length=64)] = None,
    action: Action | None = None,
    result: Result | None = None,
    ip: Annotated[str | None, Query(max_length=45)] = None,
) -> dict[str, Any]:
    try:
        start = utc_datetime(start_at) if start_at else None
        end = utc_datetime(end_at) if end_at else None
    except ValueError as exc:
        raise AppError("invalid_request", "Күн аралығында UTC offset көрсетіңіз.", 422) from exc
    if start and end and start >= end:
        raise AppError("invalid_request", "Басталу уақыты аяқталудан ерте болуы керек.", 422)
    clauses: list[str] = []
    parameters: list[str | int] = []
    for column, value in (
        ("user_id", actor),
        ("action", action.value if action else None),
        ("result", result.value if result else None),
        ("ip_address", ip),
    ):
        if value is not None:
            clauses.append(column + "=?")
            parameters.append(value)
    if start:
        clauses.append("occurred_at>=?")
        parameters.append(iso_utc(start))
    if end:
        clauses.append("occurred_at<?")
        parameters.append(iso_utc(end))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    store = _store(request)
    with store.connect() as connection:
        total = connection.execute("SELECT COUNT(*) FROM AuditLogs" + where, parameters).fetchone()[
            0
        ]
        rows = connection.execute(
            "SELECT * FROM AuditLogs"
            + where
            + " ORDER BY occurred_at DESC,audit_id DESC LIMIT ? OFFSET ?",
            [*parameters, page_size, (page - 1) * page_size],
        ).fetchall()
    return {
        "items": [store.audit_row(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/audit/{audit_id}")
def audit_detail(
    audit_id: int,
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Audit.View"))],
) -> dict[str, Any]:
    store = _store(request)
    with store.connect() as connection:
        row = connection.execute("SELECT * FROM AuditLogs WHERE audit_id=?", (audit_id,)).fetchone()
    if row is None:
        raise AppError("not_found", "Аудит жазбасы табылмады.", 404)
    return store.audit_row(row)


@router.get("/sessions")
def sessions(
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Sessions.Manage"))],
    status: Literal["active", "blocked", "revoked", "expired"] | None = None,
    suspicious: bool | None = None,
) -> dict[str, Any]:
    clauses: list[str] = []
    values: list[str] = []
    if status is not None:
        clauses.append("effective_status=?")
        values.append(status)
    if suspicious is not None:
        clauses.append("suspicious_reason<>''" if suspicious else "suspicious_reason=''")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with _store(request).connect() as connection:
        rows = connection.execute(
            """SELECT * FROM (SELECT se.session_id,se.user_id,u.login,
            u.display_name,se.created_at,se.last_seen_at,se.expires_at,se.initial_ip,se.last_ip,
            se.block_reason,se.suspicious_reason,se.review_status,se.review_source,
            se.review_audit_ids,
            CASE WHEN se.status='active' AND se.expires_at<=? THEN 'expired'
                 ELSE se.status END AS effective_status
            FROM Sessions se JOIN Users u ON u.user_id=se.user_id)"""
            + where
            + " ORDER BY created_at DESC",
            [iso_utc(), *values],
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["status"] = item.pop("effective_status")
        item["review_audit_ids"] = json.loads(item["review_audit_ids"])
        items.append(item)
    return {"items": items, "total": len(items)}


def _change_session(
    request: Request, actor: Principal, session_id: str, reason: SessionReason, operation: str
) -> dict[str, str]:
    if len(session_id) > 128:
        raise AppError("not_found", "Сессия табылмады.", 404)
    store = _store(request)
    with store.connect(write=True) as connection:
        row = connection.execute(
            "SELECT status,expires_at,review_status FROM Sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is None:
            raise AppError("not_found", "Сессия табылмады.", 404)
        if operation == "block":
            if row["status"] != "active" or row["expires_at"] <= iso_utc():
                raise AppError("session_conflict", "Тек белсенді сессияны бұғаттауға болады.", 409)
            new_status, action = "blocked", "session_block"
        elif operation == "unblock":
            if row["status"] != "blocked":
                raise AppError("session_conflict", "Бұл сессия бұғатталмаған.", 409)
            # Never resurrect a token that might already have been stolen.
            new_status, action = "revoked", "session_unblock"
        else:
            if row["status"] not in {"active", "blocked"}:
                raise AppError("session_conflict", "Сессия аяқталған.", 409)
            new_status, action = "revoked", "session_terminate"
        connection.execute(
            "UPDATE Sessions SET status=?,block_reason=?,review_status=? WHERE session_id=?",
            (
                new_status,
                reason.reason,
                "blocked" if operation == "block" else row["review_status"],
                session_id,
            ),
        )
        store.audit(
            connection,
            actor,
            action,
            "session",
            session_id,
            "success",
            reason.reason,
            client_ip(request),
            old_value={"status": row["status"], "review_status": row["review_status"]},
            new_value={
                "status": new_status,
                "review_status": "blocked" if operation == "block" else row["review_status"],
            },
        )
    return {
        "status": new_status,
        "message": "Өзгеріс сақталды. Жарамсыз сессиямен қайта кіру қажет.",
    }


@router.post("/sessions/{session_id}/block")
def block_session(
    session_id: str,
    payload: SessionReason,
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Sessions.Manage"))],
) -> dict[str, str]:
    return _change_session(request, actor, session_id, payload, "block")


@router.post("/sessions/{session_id}/unblock")
def unblock_session(
    session_id: str,
    payload: SessionReason,
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Sessions.Manage"))],
) -> dict[str, str]:
    return _change_session(request, actor, session_id, payload, "unblock")


@router.post("/sessions/{session_id}/terminate")
def terminate_session(
    session_id: str,
    payload: SessionReason,
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Sessions.Manage"))],
) -> dict[str, str]:
    return _change_session(request, actor, session_id, payload, "terminate")


@router.post("/sessions/{session_id}/dismiss")
def dismiss_session(
    session_id: str,
    payload: SessionReason,
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Sessions.Manage"))],
) -> dict[str, str]:
    store = _store(request)
    with store.connect(write=True) as connection:
        row = connection.execute(
            "SELECT status,review_status FROM Sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is None:
            raise AppError("not_found", "Сессия табылмады.", 404)
        if row["review_status"] != "awaiting_admin":
            raise AppError("session_conflict", "Сессия тексеруді күтпейді.", 409)
        connection.execute(
            "UPDATE Sessions SET review_status='false_positive' WHERE session_id=?", (session_id,)
        )
        store.audit(
            connection,
            actor,
            "session_dismiss",
            "session",
            session_id,
            "success",
            payload.reason,
            client_ip(request),
            old_value={"review_status": row["review_status"], "status": row["status"]},
            new_value={"review_status": "false_positive", "status": row["status"]},
        )
    return {"review_status": "false_positive"}


@router.post("/sessions/{session_id}/ai-review")
def suggest_session_review(
    session_id: str,
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Sessions.Manage"))],
) -> dict[str, Any]:
    """Explain an existing rule flag; the model has no session mutation authority.

    Only sanitized audit projections enter the model. Its closed extractive plan
    may select evidence IDs; neither free prose nor a block decision is accepted.
    """
    if "Audit.View" not in actor.permissions:
        raise AppError("forbidden", "Аудитті қарау рұқсаты жоқ.", 403)
    store = _store(request)
    with store.connect() as connection:
        row = connection.execute(
            "SELECT status,review_status,review_audit_ids FROM Sessions WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise AppError("not_found", "Сессия табылмады.", 404)
        if row["status"] != "active" or row["review_status"] != "awaiting_admin":
            raise AppError("session_conflict", "Алдымен ереже белгілеген сессияны таңдаңыз.", 409)
        original_ids, rule_rows = _review_rows(connection, session_id, row["review_audit_ids"])
        recent_rows = connection.execute(
            "SELECT audit_id,occurred_at,user_id,role,action,object_type,object_id,result,reason "
            "FROM AuditLogs WHERE (session_id=? OR (object_type='session' AND object_id=?)) "
            "AND action<>'session_ai_review' ORDER BY occurred_at DESC,audit_id DESC LIMIT 30",
            (session_id, session_id),
        ).fetchall()
        # Rule references take priority over routine session activity. Bound model
        # context independently of the complete evidence retained for human review.
        selected = {
            item["audit_id"]: item
            for item in sorted(
                rule_rows,
                key=lambda item: (item["occurred_at"], item["audit_id"]),
                reverse=True,
            )[:30]
            if item["action"] != "session_ai_review"
        }
        for item in recent_rows:
            if len(selected) >= 30:
                break
            selected.setdefault(item["audit_id"], item)
        rows = sorted(
            selected.values(),
            key=lambda item: (item["occurred_at"], item["audit_id"]),
            reverse=True,
        )
    if not rows:
        raise AppError("no_evidence", "Бұл сессия бойынша аудит дәлелі жоқ.", 409)
    safe_fields = (
        "audit_id",
        "occurred_at",
        "user_id",
        "role",
        "action",
        "object_type",
        "object_id",
        "result",
        "reason",
    )
    records = [
        AuditRecord.model_validate(
            {**{field: row[field] for field in safe_fields}, "reason": safe_reason(row["reason"])}
        )
        for row in rows
    ]
    model = request.app.state.search_service.llm
    if model.readiness()["status"] != "ready":
        raise AppError("model_unavailable", "AI моделі қолжетімсіз.", 503)
    try:
        plan = SummaryPlan.model_validate_json(
            model.summarize(records).model_dump_json(), strict=True
        )
    except (ValidationError, ValueError, TypeError, AttributeError) as exc:
        raise invalid_output() from exc
    summary = render_summary(plan, records, request_locale(request))
    ids = sorted({audit_id for claim in summary for audit_id in claim.audit_ids})
    with store.connect(write=True) as connection:
        # Local inference can take minutes. Re-read authority before persisting
        # the suggestion, so a concurrent role change or logout takes effect.
        user = connection.execute(
            "SELECT * FROM Users WHERE user_id=?", (actor.user_id,)
        ).fetchone()
        session = connection.execute(
            "SELECT status,expires_at FROM Sessions WHERE session_id=? AND user_id=?",
            (actor.session_id, actor.user_id),
        ).fetchone()
        permissions = (
            principal_from_connection(connection, user).permissions if user else frozenset()
        )
        if (
            user is None
            or not user["active"]
            or session is None
            or session["status"] != "active"
            or session["expires_at"] <= iso_utc()
            or not {"Sessions.Manage", "Audit.View"} <= permissions
        ):
            raise AppError("forbidden", "Сессия немесе тексеру рұқсаты өзгерді.", 403)
        current = connection.execute(
            "SELECT status,review_status,review_audit_ids FROM Sessions WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if current["status"] != "active" or current["review_status"] != "awaiting_admin":
            raise AppError("session_conflict", "Сессия күйі өзгерді. Бетті жаңартыңыз.", 409)
        current_ids, _ = _review_rows(connection, session_id, current["review_audit_ids"])
        preserved_ids = sorted(set(original_ids) | set(current_ids) | set(ids))
        connection.execute(
            "UPDATE Sessions SET review_source='ai',review_audit_ids=? WHERE session_id=?",
            (json.dumps(preserved_ids), session_id),
        )
        store.audit(
            connection,
            actor,
            "session_ai_review",
            "session",
            session_id,
            "success",
            "AI selected recorded evidence for human review; no block decision.",
            client_ip(request),
            new_value={
                "audit_ids": ids,
                "review_status": "awaiting_admin",
                "source": "ai_suggestion",
            },
        )
    return {
        "source": "ai_suggestion",
        "recommendation": "review",
        "audit_ids": ids,
        "review_status": "awaiting_admin",
        "summary": [item.model_dump() for item in summary],
    }
