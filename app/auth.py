"""Server sessions, slow password hashes, CSRF and live RBAC authorization."""

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, Request

from app.errors import AppError
from app.network import client_ip

SESSION_COOKIE = "arm_session"
CSRF_COOKIE = "arm_csrf"
IP_CHANGE_REASON = "IP мекенжайы қысқа аралықта өзгерді; әкімші тексеруі қажет."
FAILURE_REASON = "Қысқа аралықта бірнеше сәтсіз кіру тіркелді; әкімші тексеруі қажет."
STUDENT_PERMISSIONS = frozenset({"Dashboard.View", "Learning.View", "Learning.Submit"})


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**15,
        r=8,
        p=3,
        dklen=32,
        maxmem=128 * 1024 * 1024,
    )
    return (
        "scrypt$32768$8$3$"
        + base64.b64encode(salt).decode()
        + "$"
        + base64.b64encode(digest).decode()
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if algorithm != "scrypt" or (n, r, p) != ("32768", "8", "3"):
            return False
        raw_salt = base64.b64decode(salt, validate=True)
        expected_digest = base64.b64decode(expected, validate=True)
        if len(raw_salt) != 16 or len(expected_digest) != 32:
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=raw_salt,
            n=2**15,
            r=8,
            p=3,
            dklen=32,
            maxmem=128 * 1024 * 1024,
        )
        return hmac.compare_digest(actual, expected_digest)
    except (ValueError, TypeError):
        return False


# Unknown accounts undergo the same expensive password operation as known accounts.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(32))


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: int
    login: str
    display_name: str
    role_code: str
    permissions: frozenset[str]
    session_id: str = ""
    student_id: int | None = None
    approval_status: str = "approved"

    @property
    def role_snapshot(self) -> str:
        return self.role_code


def principal_from_connection(
    connection: sqlite3.Connection, user: sqlite3.Row, session_id: str = ""
) -> Principal:
    permissions = frozenset(
        row[0]
        for row in connection.execute(
            "SELECT permission_code FROM RolePermissions WHERE role_code = ?",
            (user["role_code"],),
        )
    )
    approval_status = user["approval_status"]
    if approval_status != "approved" or user["role_code"] == "pending":
        permissions = frozenset()
    elif user["role_code"] == "student":
        permissions = permissions & STUDENT_PERMISSIONS
    return Principal(
        user_id=user["user_id"],
        login=user["login"],
        display_name=user["display_name"],
        role_code=user["role_code"],
        permissions=permissions,
        session_id=session_id,
        student_id=user["student_id"],
        approval_status=approval_status,
    )


def public_user(
    connection: sqlite3.Connection, user: sqlite3.Row, *, private: bool = False
) -> dict[str, object]:
    role = connection.execute(
        "SELECT label FROM Roles WHERE code = ?", (user["role_code"],)
    ).fetchone()
    principal = principal_from_connection(connection, user)
    result: dict[str, object] = {
        "user_id": principal.user_id,
        "login": principal.login,
        "display_name": principal.display_name,
        "role_code": principal.role_code,
        "role_label": role[0] if role is not None else principal.role_code,
        "active": bool(user["active"]),
        "permissions": sorted(principal.permissions),
        "requested_role": user["requested_role"],
        "approval_status": user["approval_status"],
        "approval_reason": user["approval_reason"] if private else "",
        "request_review_status": (
            "needs_review"
            if user["approval_status"] == "pending" and user["request_review_required"]
            else user["approval_status"]
        ),
        "student_id": user["student_id"],
        "created_at": user["created_at"],
        "approved_at": user["approved_at"],
    }
    if private:
        from app.student_claims import private_claim

        result["student_claim"] = private_claim(connection, user["user_id"])
    return result


def mark_rule_review(connection: sqlite3.Connection, session_id: str, audit_ids: list[int]) -> None:
    row = connection.execute(
        "SELECT review_audit_ids FROM Sessions WHERE session_id=?", (session_id,)
    ).fetchone()
    if row is None:
        raise RuntimeError("Flagged session not found")
    previous = json.loads(row[0])
    if not isinstance(previous, list) or any(type(value) is not int for value in previous):
        raise ValueError("Invalid stored session evidence")
    evidence = list(dict.fromkeys([*previous, *audit_ids]))[-100:]
    connection.execute(
        "UPDATE Sessions SET review_status='awaiting_admin',review_source='rule',"
        "review_audit_ids=? WHERE session_id=?",
        (json.dumps(evidence), session_id),
    )


def current_principal(request: Request) -> Principal:
    cached = getattr(request.state, "principal", None)
    if isinstance(cached, Principal):
        return cached
    raw_token = request.cookies.get(SESSION_COOKIE, "")
    if not raw_token or len(raw_token) > 256:
        raise AppError("unauthenticated", "Жүйеге кіріңіз.", 401)
    store, settings = request.app.state.store, request.app.state.settings
    now, ip = utc_now(), client_ip(request)
    error: AppError | None = None
    principal: Principal | None = None
    with store.connect(write=True) as connection:
        row = connection.execute(
            "SELECT s.*, u.login, u.display_name, u.role_code, u.active,"
            "u.student_id,u.approval_status "
            "FROM Sessions s JOIN Users u ON u.user_id = s.user_id WHERE s.token_hash = ?",
            (token_hash(raw_token),),
        ).fetchone()
        if row is None or row["status"] != "active":
            error = AppError(
                "unauthenticated", "Сессия жарамсыз немесе тоқтатылған. Қайта кіріңіз.", 401
            )
        elif not row["active"] and row["approval_status"] not in {"pending", "rejected"}:
            connection.execute(
                "UPDATE Sessions SET status = 'revoked' WHERE user_id = ? AND status = 'active'",
                (row["user_id"],),
            )
            error = AppError("unauthenticated", "Аккаунт белсенді емес.", 401)
        elif row["expires_at"] <= iso_utc(now):
            connection.execute(
                "UPDATE Sessions SET status = 'expired' WHERE session_id = ?",
                (row["session_id"],),
            )
            expired_actor = principal_from_connection(connection, row, row["session_id"])
            store.audit(
                connection,
                expired_actor,
                "session_expired",
                "session",
                row["session_id"],
                "success",
                "Сессияның әрекет ету мерзімі аяқталды.",
                ip,
                row["session_id"],
            )
            error = AppError("session_expired", "Сессия уақыты аяқталды. Қайта кіріңіз.", 401)
        else:
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                header = request.headers.get("X-CSRF-Token", "")
                cookie = request.cookies.get(CSRF_COOKIE, "")
                if (
                    not header
                    or len(header) > 256
                    or not cookie
                    or not hmac.compare_digest(header.encode(), cookie.encode())
                    or not hmac.compare_digest(token_hash(header), row["csrf_hash"])
                ):
                    error = AppError(
                        "csrf_failed", "Қауіпсіздік тексеруі өтпеді. Бетті жаңартыңыз.", 403
                    )
            if error is None:
                principal = principal_from_connection(connection, row, row["session_id"])
                recent = datetime.fromisoformat(row["last_seen_at"].replace("Z", "+00:00"))
                if (
                    row["last_ip"] != ip
                    and now - recent <= timedelta(seconds=settings.suspicious_window_seconds)
                    and (
                        IP_CHANGE_REASON not in row["suspicious_reason"]
                        or row["review_status"] == "false_positive"
                    )
                ):
                    reason = IP_CHANGE_REASON
                    combined_reason = row["suspicious_reason"]
                    if reason not in combined_reason:
                        combined_reason = " ".join(filter(None, (combined_reason, reason)))
                    connection.execute(
                        "UPDATE Sessions SET suspicious_reason = ? WHERE session_id = ?",
                        (combined_reason, row["session_id"]),
                    )
                    audit_id = store.audit(
                        connection,
                        principal,
                        "session_flagged",
                        "session",
                        row["session_id"],
                        "success",
                        reason,
                        ip,
                        row["session_id"],
                    )
                    mark_rule_review(connection, row["session_id"], [audit_id])
                connection.execute(
                    "UPDATE Sessions SET last_seen_at = ?, last_ip = ? WHERE session_id = ?",
                    (iso_utc(now), ip, row["session_id"]),
                )
    if error is not None:
        raise error
    if principal is None:
        raise AppError("unauthenticated", "Жүйеге кіріңіз.", 401)
    request.state.principal = principal
    return principal


def require_permission(code: str) -> Callable[..., Principal]:
    def dependency(
        request: Request, principal: Annotated[Principal, Depends(current_principal)]
    ) -> Principal:
        approval_pending = (
            principal.approval_status != "approved" or principal.role_code == "pending"
        )
        student_denied = principal.role_code == "student" and code not in STUDENT_PERMISSIONS
        if approval_pending or student_denied or code not in principal.permissions:
            with request.app.state.store.connect(write=True) as connection:
                request.app.state.store.audit(
                    connection,
                    principal,
                    "access_denied",
                    "permission",
                    code,
                    "denied",
                    "Әрекетке қажетті құқық берілмеген.",
                    client_ip(request),
                    principal.session_id,
                )
            if approval_pending:
                if principal.approval_status == "rejected":
                    raise AppError(
                        "approval_rejected", "Өтінім қабылданбаған. Жеке күй бетін қараңыз.", 403
                    )
                raise AppError("approval_required", "Өтінім әкімші мақұлдауын күтуде.", 403)
            raise AppError("forbidden", "Бұл әрекетке рұқсатыңыз жоқ.", 403)
        return principal

    return dependency


require_audit_view = require_permission("Audit.View")
