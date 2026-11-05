"""Authentication and transactional user/role administration."""

import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.auth import (
    _DUMMY_HASH,
    CSRF_COOKIE,
    FAILURE_REASON,
    SESSION_COOKIE,
    Principal,
    current_principal,
    hash_password,
    iso_utc,
    mark_rule_review,
    principal_from_connection,
    public_user,
    require_permission,
    token_hash,
    utc_now,
    verify_password,
)
from app.config import Settings
from app.errors import AppError
from app.network import client_ip
from app.safety import safe_reason
from app.student_claims import (
    StudentClaimInput,
    claim_revision,
    masked_claim,
    match_claim,
    normalized_name,
    private_claim,
    safe_review_reason,
    validate_claim,
    write_claim,
)

auth_router = APIRouter(prefix="/api/auth", tags=["Authentication"])
admin_router = APIRouter(prefix="/api/admin", tags=["Administration"])
Login = Annotated[str, Field(min_length=3, max_length=32, pattern=r"^[a-z][a-z0-9_.-]{2,31}$")]
Password = Annotated[str, Field(min_length=12, max_length=128)]


class Registration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    login: Login
    display_name: str = Field(min_length=2, max_length=80)
    password: Password
    requested_role: Literal["teacher", "student"] = "teacher"
    student_claim_name: str | None = Field(default=None, min_length=2, max_length=120)
    student_claim_number: str | None = Field(default=None, min_length=3, max_length=32)
    student_claim_group_id: int | None = Field(default=None, strict=True, ge=1, le=2**63 - 1)
    student_claim_email: str | None = Field(default=None, max_length=254)

    @field_validator("student_claim_name", mode="before")
    @classmethod
    def claim_name(cls, value: object) -> object:
        return StudentClaimInput.clean_name(value)

    @field_validator("student_claim_number", mode="before")
    @classmethod
    def claim_number(cls, value: object) -> object:
        return StudentClaimInput.clean_number(value)

    @field_validator("student_claim_email", mode="before")
    @classmethod
    def claim_email(cls, value: object) -> object:
        return StudentClaimInput.clean_email(value)

    def get_student_claim(self) -> StudentClaimInput | None:
        if self.requested_role != "student":
            return None
        return StudentClaimInput.model_validate(
            self.model_dump(include=set(StudentClaimInput.model_fields))
        )

    @model_validator(mode="after")
    def role_specific_claim(self) -> "Registration":
        if self.requested_role == "teacher":
            if set(StudentClaimInput.model_fields) & self.model_fields_set:
                raise ValueError("Teacher registration cannot contain student claims")
        else:
            self.get_student_claim()
        return self

    @field_validator("login", mode="before")
    @classmethod
    def normalize_login(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("display_name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    login: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("login", mode="before")
    @classmethod
    def normalize_login(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value


class UserChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role_code: str | None = Field(default=None, min_length=1, max_length=32)
    active: bool | None = Field(default=None, strict=True)
    student_id: int | None = Field(default=None, strict=True, ge=1, le=2**63 - 1)

    @model_validator(mode="after")
    def at_least_one_change(self) -> "UserChange":
        if (
            self.role_code is None
            and self.active is None
            and "student_id" not in self.model_fields_set
        ):
            raise ValueError("Кемінде бір өзгеріс көрсетіңіз")
        return self


class RoleChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    permissions: list[str] = Field(max_length=50)

    @field_validator("permissions")
    @classmethod
    def unique_permissions(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("Құқықтар қайталанбауы тиіс")
        return value


class RequestApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role_code: str = Field(min_length=1, max_length=32)
    student_id: int | None = Field(default=None, strict=True, ge=1, le=2**63 - 1)
    create_student: bool = Field(default=False, strict=True)
    confirm_distinct_student: bool = Field(default=False, strict=True)
    confirmed_group_id: int | None = Field(default=None, strict=True, ge=1, le=2**63 - 1)
    confirm_student_match: bool = Field(default=False, strict=True)
    acknowledge_mismatch: bool = Field(default=False, strict=True)
    claim_revision: str | None = Field(
        default=None, min_length=64, max_length=64, pattern="^[0-9a-f]{64}$"
    )


class RequestRejection(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    reason: str = Field(min_length=5, max_length=300)

    @field_validator("reason")
    @classmethod
    def redact_reason(cls, value: str) -> str:
        redacted = safe_reason(value).strip()
        if len(redacted) < 5:
            raise ValueError("Бас тартудың қысқаша себебін көрсетіңіз")
        return redacted


def _require_admin(principal: Principal) -> None:
    if principal.role_code != "admin":
        raise AppError("forbidden", "Бұл өзгерісті тек әкімші орындай алады.", 403)


def _admin_current(connection: sqlite3.Connection, actor: Principal) -> None:
    row = connection.execute(
        "SELECT u.role_code,u.active,u.approval_status,s.status,s.expires_at "
        "FROM Users u JOIN Sessions s ON s.user_id=u.user_id "
        "WHERE u.user_id=? AND s.session_id=?",
        (actor.user_id, actor.session_id),
    ).fetchone()
    if (
        row is None
        or row["role_code"] != "admin"
        or not row["active"]
        or row["approval_status"] != "approved"
        or row["status"] != "active"
        or row["expires_at"] <= iso_utc(utc_now())
        or connection.execute(
            "SELECT 1 FROM RolePermissions WHERE role_code='admin' "
            "AND permission_code='Users.Manage'"
        ).fetchone()
        is None
    ):
        raise AppError("forbidden", "Әкімші рұқсаты немесе сессиясы өзгерді.", 403)


def _validate_student_link(
    connection: sqlite3.Connection, role_code: str, student_id: int | None, user_id: int
) -> None:
    if role_code != "student":
        if student_id is not None:
            raise AppError(
                "invalid_student_link", "Студент жазбасы тек студент рөліне байланысады.", 422
            )
        return
    if student_id is None:
        raise AppError("student_link_required", "Бар студент жазбасын таңдаңыз.", 422)
    if (
        connection.execute("SELECT 1 FROM Students WHERE student_id=?", (student_id,)).fetchone()
        is None
    ):
        raise AppError("invalid_student_link", "Студент жазбасы табылмады.", 422)
    if (
        connection.execute(
            "SELECT 1 FROM Users WHERE student_id=? AND user_id<>?", (student_id, user_id)
        ).fetchone()
        is not None
    ):
        raise AppError(
            "student_already_linked", "Бұл студент басқа аккаунтқа байланыстырылған.", 409
        )


def _create_or_reuse_student(
    connection: sqlite3.Connection, payload: RequestApproval, user_id: int
) -> tuple[int, bool]:
    """Resolve a reviewed claim inside the approval write transaction.

    Existing academic records are never rewritten from applicant data. A possible
    duplicate needs manual selection; homonyms with different codes require a
    separate, explicit decision that the applicants are distinct people.
    """
    claim = private_claim(connection, user_id)
    if claim is None:
        raise AppError("student_claim_required", "Алдымен студент өтінімін толықтырыңыз.", 422)
    group_id = payload.confirmed_group_id
    if (
        group_id is None
        or connection.execute(
            "SELECT 1 FROM Groups WHERE group_id=? AND active=1", (group_id,)
        ).fetchone()
        is None
    ):
        raise AppError(
            "student_group_confirmation_required", "Дұрыс оқу тобын тізімнен растап таңдаңыз.", 422
        )
    # Legacy student codes can have mixed case. Compare canonically without
    # altering legacy records or adding a destructive uniqueness migration.
    students = connection.execute("SELECT * FROM Students ORDER BY student_id").fetchall()
    code_matches = [
        student
        for student in students
        if student["student_code"].strip().upper() == claim["number"]
    ]
    if len(code_matches) == 1:
        student = code_matches[0]
        if (
            normalized_name(student["display_name"]) == normalized_name(claim["name"])
            and student["group_id"] == group_id
        ):
            student_id = int(student["student_id"])
            _validate_student_link(connection, "student", student_id, user_id)
            return student_id, False
    name_collision = any(
        normalized_name(student["display_name"]) == normalized_name(claim["name"])
        for student in students
    )
    if code_matches or (name_collision and not payload.confirm_distinct_student):
        raise AppError(
            "student_selection_required",
            "Сәйкес болуы мүмкін студент бар. Бар жазбаны қолмен таңдаңыз немесе кодтары бөлек "
            "аттас адамдардың басқа адам екенін тексеріп, бөлек растаңыз.",
            409,
        )
    cursor = connection.execute(
        "INSERT INTO Students(display_name,student_code,group_id) VALUES (?,?,?)",
        (claim["name"], claim["number"], group_id),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid), True


@auth_router.post("/register", status_code=201)
def register(payload: Registration, request: Request) -> dict[str, Any]:
    store = request.app.state.store
    claim = payload.get_student_claim()
    encoded = hash_password(payload.password)
    try:
        with store.connect(write=True) as connection:
            if claim is not None:
                validate_claim(connection, claim, request.app.state.settings)
            cursor = connection.execute(
                "INSERT INTO Users(login,display_name,password_hash,role_code,active,created_at,"
                "requested_role,approval_status) VALUES (?, ?, ?, 'pending', 0, ?, ?, 'pending')",
                (
                    payload.login,
                    payload.display_name,
                    encoded,
                    iso_utc(utc_now()),
                    payload.requested_role,
                ),
            )
            if claim is not None:
                write_claim(connection, int(cursor.lastrowid or 0), claim)
            store.audit(
                connection,
                None,
                "user_register",
                "user",
                str(cursor.lastrowid),
                "success",
                "Жаңа аккаунт әкімші растауын күтуде.",
                client_ip(request),
                new_value={
                    "requested_role": payload.requested_role,
                    "student_claim": (
                        {"name_provided": True, "group_id": claim.student_claim_group_id}
                        if claim is not None
                        else None
                    ),
                },
            )
    except sqlite3.IntegrityError as exc:
        if "Users.login" in str(exc):
            raise AppError("login_unavailable", "Бұл логинді пайдалану мүмкін емес.", 409) from exc
        if "StudentClaims.student_claim_number" in str(exc):
            raise AppError(
                "student_claim_number_unavailable",
                "Бұл студенттік нөмірге өтінім бар. "
                "Жеке өтінімді тексеріңіз немесе әкімшіге хабарласыңыз.",
                409,
            ) from exc
        raise
    return {
        "message": "Тіркелу аяқталды. Жеке күй бетінде өтінімнің қаралуын күте аласыз.",
        "requested_role": payload.requested_role,
        "approval_status": "pending",
    }


@auth_router.get("/registration-options")
def registration_options(request: Request) -> dict[str, Any]:
    """Public group names only; never expose the academic catalog or student roster."""
    with request.app.state.store.connect() as connection:
        groups = [
            dict(row)
            for row in connection.execute(
                "SELECT group_id,name FROM Groups WHERE active=1 ORDER BY name"
            )
        ]
    settings = request.app.state.settings
    return {
        "groups": groups,
        "student_email_required": settings.student_email_required,
        "student_email_domain": settings.student_email_domain,
    }


@auth_router.put("/student-claim")
def resubmit_student_claim(
    payload: StudentClaimInput,
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
) -> dict[str, Any]:
    store = request.app.state.store
    try:
        with store.connect(write=True) as connection:
            session = connection.execute(
                "SELECT status,expires_at FROM Sessions WHERE session_id=? AND user_id=?",
                (principal.session_id, principal.user_id),
            ).fetchone()
            if (
                session is None
                or session["status"] != "active"
                or session["expires_at"] <= iso_utc(utc_now())
            ):
                raise AppError("unauthenticated", "Сессия жарамсыз. Қайта кіріңіз.", 401)
            user = connection.execute(
                "SELECT * FROM Users WHERE user_id=?", (principal.user_id,)
            ).fetchone()
            if (
                user is None
                or user["requested_role"] != "student"
                or user["approval_status"] == "approved"
                or user["student_id"] is not None
            ):
                raise AppError(
                    "student_claim_locked",
                    "Расталған студент байланысын өзіңіз өзгерте алмайсыз.",
                    403,
                )
            validate_claim(connection, payload, request.app.state.settings)
            previous_claim = private_claim(connection, principal.user_id)
            write_claim(connection, principal.user_id, payload)
            connection.execute(
                "UPDATE Users SET approval_status='pending',role_code='pending',active=0,"
                "student_id=NULL,request_review_required=0,approval_reason='' WHERE user_id=?",
                (principal.user_id,),
            )
            store.audit(
                connection,
                principal,
                "student_claim_update",
                "user",
                str(principal.user_id),
                "success",
                "Студент мәліметі тексеру үшін қайта ұсынылды.",
                client_ip(request),
                principal.session_id,
                old_value={
                    "approval_status": user["approval_status"],
                    "group_id": previous_claim["group_id"] if previous_claim else None,
                },
                new_value={
                    "approval_status": "pending",
                    "student_id": None,
                    "group_id": payload.student_claim_group_id,
                    "name_changed": previous_claim is None
                    or previous_claim["name"] != payload.student_claim_name,
                    "number_changed": previous_claim is None
                    or previous_claim["number"] != payload.student_claim_number,
                },
            )
            updated = connection.execute(
                "SELECT * FROM Users WHERE user_id=?", (principal.user_id,)
            ).fetchone()
            return {"user": public_user(connection, updated, private=True)}
    except sqlite3.IntegrityError as exc:
        if "StudentClaims.student_claim_number" in str(exc):
            raise AppError(
                "student_claim_number_unavailable", "Бұл студенттік нөмірге өтінім бар.", 409
            ) from exc
        raise


def _flag_failure_sessions(
    connection: sqlite3.Connection,
    store: Any,
    user: sqlite3.Row,
    ip: str,
    now: datetime,
    settings: Settings,
    new_failure: bool = False,
) -> None:
    since = iso_utc(now - timedelta(seconds=settings.suspicious_window_seconds))
    count = connection.execute(
        "SELECT COUNT(*) FROM LoginAttempts WHERE success = 0 AND occurred_at >= ? "
        "AND (ip_address = ? OR login = ? COLLATE NOCASE)",
        (since, ip, user["login"]),
    ).fetchone()[0]
    if count < settings.suspicious_failure_threshold:
        return
    sessions = connection.execute(
        "SELECT session_id,suspicious_reason FROM Sessions WHERE user_id = ? AND status = 'active' "
        "AND (instr(suspicious_reason, ?) = 0 "
        "OR (review_status='false_positive' AND ?))",
        (user["user_id"], FAILURE_REASON, int(new_failure)),
    ).fetchall()
    reason = FAILURE_REASON
    actor = principal_from_connection(connection, user)
    for session in sessions:
        combined_reason = session["suspicious_reason"]
        if reason not in combined_reason:
            combined_reason = " ".join(filter(None, (combined_reason, reason)))
        connection.execute(
            "UPDATE Sessions SET suspicious_reason = ? WHERE session_id = ?",
            (combined_reason, session["session_id"]),
        )
        audit_id = store.audit(
            connection,
            actor,
            "session_flagged",
            "session",
            session["session_id"],
            "success",
            reason,
            ip,
            session["session_id"],
        )
        login_evidence = [
            row[0]
            for row in connection.execute(
                "SELECT audit_id FROM AuditLogs WHERE action='login' AND result='failure' "
                "AND occurred_at>=? AND (ip_address=? OR user_id=?) "
                "ORDER BY audit_id DESC LIMIT 20",
                (since, ip, user["login"]),
            )
        ]
        mark_rule_review(connection, session["session_id"], [*reversed(login_evidence), audit_id])


@auth_router.post("/login")
def login(payload: Credentials, request: Request, response: Response) -> dict[str, Any]:
    store, settings = request.app.state.store, request.app.state.settings
    ip, now = client_ip(request), utc_now()
    error: AppError | None = None
    result: dict[str, Any] = {}
    raw_session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with store.connect(write=True) as connection:
        since = iso_utc(now - timedelta(seconds=settings.login_window_seconds))
        failures = connection.execute(
            "SELECT COUNT(*) FROM LoginAttempts WHERE success = 0 AND occurred_at >= ? "
            "AND (ip_address = ? OR login = ? COLLATE NOCASE)",
            (since, ip, payload.login),
        ).fetchone()[0]
        if failures >= settings.login_max_attempts:
            store.audit(
                connection,
                None,
                "login",
                "account",
                "rate_limit",
                "denied",
                "Қысқа аралықтағы сәтсіз кірулер шегінен асқан сұрау қабылданбады.",
                ip,
            )
            error = AppError(
                "rate_limited", "Кіру әрекеті тым көп. Біраз уақыттан кейін қайталаңыз.", 429
            )
        else:
            user = connection.execute(
                "SELECT * FROM Users WHERE login = ? COLLATE NOCASE", (payload.login,)
            ).fetchone()
            encoded = user["password_hash"] if user is not None else _DUMMY_HASH
            valid_password = verify_password(payload.password, encoded)
            success = (
                user is not None
                and valid_password
                and (bool(user["active"]) or user["approval_status"] in {"pending", "rejected"})
            )
            connection.execute(
                "INSERT INTO LoginAttempts(login,ip_address,occurred_at,success) VALUES (?,?,?,?)",
                (payload.login, ip, iso_utc(now), int(success)),
            )
            if not success:
                actor = principal_from_connection(connection, user) if user is not None else None
                store.audit(
                    connection,
                    actor,
                    "login",
                    "account",
                    str(user["user_id"]) if user is not None else payload.login,
                    "failure",
                    "Кіру деректері расталмады немесе аккаунт белсенді емес.",
                    ip,
                )
                if user is not None:
                    _flag_failure_sessions(
                        connection, store, user, ip, now, settings, new_failure=True
                    )
                error = AppError(
                    "invalid_credentials",
                    "Логин немесе құпиясөз қате, не аккаунт белсенді емес.",
                    401,
                )
            else:
                session_id = secrets.token_hex(16)
                connection.execute(
                    "INSERT INTO Sessions(session_id,token_hash,csrf_hash,user_id,created_at,"
                    "last_seen_at,expires_at,initial_ip,last_ip,status) "
                    "VALUES (?,?,?,?,?,?,?,?,?,'active')",
                    (
                        session_id,
                        token_hash(raw_session),
                        token_hash(csrf),
                        user["user_id"],
                        iso_utc(now),
                        iso_utc(now),
                        iso_utc(now + timedelta(seconds=settings.session_ttl_seconds)),
                        ip,
                        ip,
                    ),
                )
                actor = principal_from_connection(connection, user, session_id)
                store.audit(
                    connection,
                    actor,
                    "login",
                    "session",
                    session_id,
                    "success",
                    "Жүйеге кіру сәтті аяқталды."
                    if user["approval_status"] == "approved"
                    else "Өтінімнің жеке күйіне арналған шектеулі сессия ашылды.",
                    ip,
                    session_id,
                )
                _flag_failure_sessions(connection, store, user, ip, now, settings)
                result = {"user": public_user(connection, user, private=True), "csrf_token": csrf}
    if error is not None:
        raise error
    response.set_cookie(
        SESSION_COOKIE,
        raw_session,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=settings.session_ttl_seconds,
        httponly=False,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return result


@auth_router.get("/me")
def me(
    request: Request, principal: Annotated[Principal, Depends(current_principal)]
) -> dict[str, Any]:
    with request.app.state.store.connect() as connection:
        user = connection.execute(
            "SELECT * FROM Users WHERE user_id = ?", (principal.user_id,)
        ).fetchone()
        return {"user": public_user(connection, user, private=True)}


@auth_router.get("/status")
def account_status(
    request: Request, principal: Annotated[Principal, Depends(current_principal)]
) -> dict[str, Any]:
    return me(request, principal)


@auth_router.post("/logout")
def logout(
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(current_principal)],
) -> dict[str, str]:
    store = request.app.state.store
    with store.connect(write=True) as connection:
        connection.execute(
            "UPDATE Sessions SET status = 'revoked' WHERE session_id = ?", (principal.session_id,)
        )
        store.audit(
            connection,
            principal,
            "logout",
            "session",
            principal.session_id,
            "success",
            "Пайдаланушы жүйеден шықты.",
            client_ip(request),
            principal.session_id,
        )
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=request.app.state.settings.cookie_secure,
        httponly=True,
        samesite="strict",
    )
    response.delete_cookie(
        CSRF_COOKIE, path="/", secure=request.app.state.settings.cookie_secure, samesite="strict"
    )
    return {"message": "Жүйеден шықтыңыз."}


@admin_router.get("/users")
def users(
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("Users.Manage"))],
    q: str = Query(default="", max_length=80),
    role_code: str | None = None,
    active: bool | None = None,
) -> dict[str, Any]:
    clauses: list[str] = []
    values: list[str | int] = []
    if q:
        clauses.append("(login LIKE ? ESCAPE '\\' OR display_name LIKE ? ESCAPE '\\')")
        pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        values.extend([pattern, pattern])
    if role_code is not None:
        clauses.append("role_code = ?")
        values.append(role_code)
    if active is not None:
        clauses.append("active = ?")
        values.append(int(active))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with request.app.state.store.connect() as connection:
        rows = connection.execute(
            "SELECT * FROM Users" + where + " ORDER BY user_id", values
        ).fetchall()
        return {"items": [public_user(connection, user) for user in rows], "total": len(rows)}


@admin_router.patch("/users/{user_id}")
def change_user(
    user_id: int,
    payload: UserChange,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("Users.Manage"))],
) -> dict[str, Any]:
    return _change_user(user_id, payload, request, principal)


def _change_user(
    user_id: int,
    payload: UserChange,
    request: Request,
    principal: Principal,
    required_role: str | None = None,
) -> dict[str, Any]:
    store = request.app.state.store
    link_or_role_change = (
        payload.role_code is not None
        or "student_id" in payload.model_fields_set
        or required_role is not None
    )
    if link_or_role_change:
        _require_admin(principal)
    with store.connect(write=True) as connection:
        if link_or_role_change:
            _admin_current(connection, principal)
        user = connection.execute("SELECT * FROM Users WHERE user_id = ?", (user_id,)).fetchone()
        if user is None:
            raise AppError("not_found", "Пайдаланушы табылмады.", 404)
        if required_role is not None and user["role_code"] != required_role:
            raise AppError("teacher_not_found", "Оқытушы табылмады.", 404)
        if user["approval_status"] != "approved":
            raise AppError(
                "approval_required", "Өтінімді мақұлдау немесе бас тарту бөлімінде қараңыз.", 409
            )
        if payload.role_code is not None:
            _require_admin(principal)
            _admin_current(connection, principal)
        new_role = payload.role_code if payload.role_code is not None else user["role_code"]
        new_active = int(payload.active) if payload.active is not None else user["active"]
        new_student = user["student_id"] if new_role == "student" else None
        if "student_id" in payload.model_fields_set:
            new_student = payload.student_id
        if "student_id" in payload.model_fields_set or new_student != user["student_id"]:
            _require_admin(principal)
            _admin_current(connection, principal)
        _validate_student_link(connection, new_role, new_student, user_id)
        if new_role == "pending":
            raise AppError("invalid_role", "Бекітілген аккаунтқа нақты рөл таңдаңыз.", 422)
        if connection.execute("SELECT 1 FROM Roles WHERE code = ?", (new_role,)).fetchone() is None:
            raise AppError("invalid_role", "Мұндай рөл жоқ.", 422)
        if (
            user["role_code"] == "admin"
            and user["active"]
            and (new_role != "admin" or not new_active)
        ):
            count = connection.execute(
                "SELECT COUNT(*) FROM Users WHERE role_code = 'admin' AND active = 1 "
                "AND approval_status='approved'"
            ).fetchone()[0]
            if count <= 1:
                raise AppError(
                    "last_admin",
                    "Соңғы белсенді әкімшіні тоқтатуға немесе рөлін алуға болмайды.",
                    409,
                )
        connection.execute(
            "UPDATE Users SET role_code = ?, active = ?, student_id=? WHERE user_id = ?",
            (new_role, new_active, new_student, user_id),
        )
        if not new_active:
            revoked = connection.execute(
                "SELECT session_id FROM Sessions WHERE user_id = ? AND status = 'active'",
                (user_id,),
            ).fetchall()
            connection.execute(
                "UPDATE Sessions SET status = 'revoked' WHERE user_id = ? AND status = 'active'",
                (user_id,),
            )
            for session in revoked:
                store.audit(
                    connection,
                    principal,
                    "session_terminate",
                    "session",
                    session["session_id"],
                    "success",
                    "Аккаунт тоқтатылғандықтан сессия аяқталды.",
                    client_ip(request),
                    session["session_id"],
                )
        store.audit(
            connection,
            principal,
            "user_change",
            "user",
            str(user_id),
            "success",
            "Пайдаланушының рөлі немесе белсенділігі өзгертілді.",
            client_ip(request),
            principal.session_id,
            old_value={
                "role_code": user["role_code"],
                "active": bool(user["active"]),
                "student_id": user["student_id"],
            },
            new_value={
                "role_code": new_role,
                "active": bool(new_active),
                "student_id": new_student,
            },
        )
        if user["role_code"] != new_role:
            store.audit(
                connection,
                principal,
                "role_change",
                "user",
                str(user_id),
                "success",
                "Пайдаланушының нақты рөлі өзгертілді.",
                client_ip(request),
                principal.session_id,
                old_value={"role_code": user["role_code"]},
                new_value={"role_code": new_role},
            )
        if user["student_id"] != new_student:
            store.audit(
                connection,
                principal,
                "student_link",
                "user",
                str(user_id),
                "success",
                "Аккаунттың студент жазбасымен байланысы өзгертілді.",
                client_ip(request),
                principal.session_id,
                old_value={"student_id": user["student_id"]},
                new_value={"student_id": new_student},
            )
        updated = connection.execute("SELECT * FROM Users WHERE user_id = ?", (user_id,)).fetchone()
        return {"user": public_user(connection, updated)}


@admin_router.get("/requests")
def registration_requests(
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("Users.Manage"))],
    status: Literal["pending", "needs_review", "approved", "rejected", "all"] = "pending",
) -> dict[str, Any]:
    _require_admin(principal)
    where = " WHERE requested_role IS NOT NULL"
    parameters: list[str] = []
    if status == "needs_review":
        where += " AND approval_status='pending' AND request_review_required=1"
    elif status != "all":
        where += " AND approval_status=?"
        parameters.append(status)
    with request.app.state.store.connect() as connection:
        _admin_current(connection, principal)
        rows = connection.execute(
            "SELECT * FROM Users" + where + " ORDER BY created_at DESC,user_id DESC", parameters
        ).fetchall()
        items = []
        for user in rows:
            item = public_user(connection, user)
            item["student_claim"] = masked_claim(connection, user["user_id"])
            items.append(item)
        return {"items": items, "total": len(rows)}


@admin_router.get("/requests/{user_id}")
def registration_request_detail(
    user_id: int,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("Users.Manage"))],
) -> dict[str, Any]:
    _require_admin(principal)
    with request.app.state.store.connect() as connection:
        # The displayed claim, its revision and candidate matches must describe
        # one SQLite snapshot even if the applicant edits concurrently.
        connection.execute("BEGIN")
        _admin_current(connection, principal)
        user = connection.execute("SELECT * FROM Users WHERE user_id=?", (user_id,)).fetchone()
        if user is None or user["requested_role"] is None:
            raise AppError("not_found", "Өтінім табылмады.", 404)
        claim = private_claim(connection, user_id)
        candidates = []
        for student in connection.execute(
            "SELECT s.*,g.name AS group_name,EXISTS(SELECT 1 FROM Users u "
            "WHERE u.student_id=s.student_id AND u.user_id<>?) AS already_linked "
            "FROM Students s JOIN Groups g ON g.group_id=s.group_id ORDER BY s.display_name",
            (user_id,),
        ):
            candidate = dict(student)
            candidate["already_linked"] = bool(candidate["already_linked"])
            candidate["matches"] = match_claim(claim, student)
            candidates.append(candidate)
        return {
            "user": public_user(connection, user, private=True),
            "student_claim": claim,
            "claim_revision": claim_revision(connection, user_id),
            "candidates": candidates,
            "groups": [
                dict(row)
                for row in connection.execute(
                    "SELECT group_id,name FROM Groups WHERE active=1 ORDER BY name"
                )
            ],
        }


@admin_router.post("/requests/{user_id}/needs-review")
def defer_registration(
    user_id: int,
    payload: RequestRejection,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("Users.Manage"))],
) -> dict[str, Any]:
    _require_admin(principal)
    store = request.app.state.store
    with store.connect(write=True) as connection:
        _admin_current(connection, principal)
        user = connection.execute("SELECT * FROM Users WHERE user_id=?", (user_id,)).fetchone()
        if user is None:
            raise AppError("not_found", "Өтінім табылмады.", 404)
        if user["approval_status"] == "approved":
            raise AppError("request_reviewed", "Бұл аккаунт бұрын мақұлданған.", 409)
        reason = safe_review_reason(connection, user_id, payload.reason)
        connection.execute(
            "UPDATE Users SET role_code='pending',active=0,approval_status='pending',"
            "request_review_required=1,approval_reason=?,student_id=NULL WHERE user_id=?",
            (reason, user_id),
        )
        store.audit(
            connection,
            principal,
            "user_needs_review",
            "user",
            str(user_id),
            "success",
            reason,
            client_ip(request),
            principal.session_id,
            old_value={
                "approval_status": user["approval_status"],
                "student_id": user["student_id"],
            },
            new_value={
                "approval_status": "pending",
                "request_review_required": True,
                "student_id": None,
            },
        )
        updated = connection.execute("SELECT * FROM Users WHERE user_id=?", (user_id,)).fetchone()
        return {"user": public_user(connection, updated, private=True)}


@admin_router.post("/requests/{user_id}/approve")
def approve_registration(
    user_id: int,
    payload: RequestApproval,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("Users.Manage"))],
) -> dict[str, Any]:
    return _approve_registration(user_id, payload, request, principal)


def _approve_registration(
    user_id: int,
    payload: RequestApproval,
    request: Request,
    principal: Principal,
    required_requested_role: str | None = None,
) -> dict[str, Any]:
    _require_admin(principal)
    store = request.app.state.store
    with store.connect(write=True) as connection:
        _admin_current(connection, principal)
        user = connection.execute("SELECT * FROM Users WHERE user_id=?", (user_id,)).fetchone()
        if user is None:
            raise AppError("not_found", "Өтінім табылмады.", 404)
        if (
            required_requested_role is not None
            and user["requested_role"] != required_requested_role
        ):
            raise AppError("teacher_request_required", "Оқытушы тіркелу өтінімін таңдаңыз.", 409)
        if user["approval_status"] == "approved":
            raise AppError("request_reviewed", "Бұл аккаунт бұрын мақұлданған.", 409)
        if user["requested_role"] == "teacher" and payload.role_code != "teacher":
            raise AppError(
                "teacher_role_required",
                "Оқытушы өтінімін тек оқытушы рөлімен мақұлдауға болады.",
                422,
            )
        if (
            payload.role_code == "pending"
            or connection.execute(
                "SELECT 1 FROM Roles WHERE code=?", (payload.role_code,)
            ).fetchone()
            is None
        ):
            raise AppError("invalid_role", "Мақұлдау үшін нақты рөл таңдаңыз.", 422)
        if (
            (payload.create_student and payload.student_id is not None)
            or (payload.confirmed_group_id is not None and not payload.create_student)
            or (payload.confirm_distinct_student and not payload.create_student)
        ):
            raise AppError(
                "invalid_student_creation",
                "Студентті қосу немесе бар жазбаны таңдаудың бірін таңдаңыз.",
                422,
            )
        if payload.role_code != "student" and (
            payload.create_student or payload.confirmed_group_id is not None
        ):
            raise AppError(
                "invalid_student_creation", "Бұл өрістер тек студент рөліне арналған.", 422
            )
        student_id = payload.student_id
        if not payload.create_student:
            _validate_student_link(connection, payload.role_code, student_id, user_id)
        matches: dict[str, bool] | None = None
        student_created = False
        if payload.role_code == "student":
            if not payload.confirm_student_match:
                raise AppError(
                    "student_match_confirmation_required",
                    "Студент сәйкестігін тексеріп, растаңыз.",
                    422,
                )
            if payload.claim_revision is None:
                raise AppError(
                    "request_revision_required",
                    "Өтінімді ашып, соңғы мәліметтерді тексеріңіз.",
                    422,
                )
            if payload.claim_revision is not None and payload.claim_revision != claim_revision(
                connection, user_id
            ):
                raise AppError(
                    "request_changed", "Өтінім мәліметтері өзгерді. Қайта ашып тексеріңіз.", 409
                )
            if payload.create_student:
                student_id, student_created = _create_or_reuse_student(connection, payload, user_id)
            student = connection.execute(
                "SELECT * FROM Students WHERE student_id=?", (student_id,)
            ).fetchone()
            matches = match_claim(private_claim(connection, user_id), student)
            if (
                not payload.create_student
                and not all(matches.values())
                and not payload.acknowledge_mismatch
            ):
                raise AppError(
                    "student_claim_mismatch",
                    "Өтінім мен таңдалған студент сәйкес емес. "
                    "Айырмашылықты тексеріп, бөлек растаңыз.",
                    409,
                )
        connection.execute(
            "UPDATE Users SET role_code=?,active=1,approval_status='approved',approval_reason='',"
            "request_review_required=0,student_id=?,approved_at=? WHERE user_id=?",
            (payload.role_code, student_id, iso_utc(utc_now()), user_id),
        )
        if student_created:
            store.audit(
                connection,
                principal,
                "student_create",
                "student",
                str(student_id),
                "success",
                "Әкімші тексерген өтінімнен ресми студент жазбасы құрылды.",
                client_ip(request),
                principal.session_id,
                new_value={
                    "name_provided": True,
                    "group_id": payload.confirmed_group_id,
                    "linked_user_id": user_id,
                    "distinct_student_confirmed": payload.confirm_distinct_student,
                },
            )
        store.audit(
            connection,
            principal,
            "user_approve",
            "user",
            str(user_id),
            "success",
            "Тіркелу өтінімі әкімші шешімімен мақұлданды.",
            client_ip(request),
            principal.session_id,
            old_value={
                "approval_status": user["approval_status"],
                "role_code": user["role_code"],
                "student_id": user["student_id"],
            },
            new_value={
                "approval_status": "approved",
                "role_code": payload.role_code,
                "student_id": student_id,
                "claim_matches": matches,
                "mismatch_acknowledged": payload.acknowledge_mismatch,
                "student_created": student_created,
                "distinct_student_confirmed": payload.confirm_distinct_student and student_created,
                "confirmed_group_id": payload.confirmed_group_id,
            },
        )
        if user["role_code"] != payload.role_code:
            store.audit(
                connection,
                principal,
                "role_change",
                "user",
                str(user_id),
                "success",
                "Мақұлдау кезінде пайдаланушыға нақты рөл берілді.",
                client_ip(request),
                principal.session_id,
                old_value={"role_code": user["role_code"]},
                new_value={"role_code": payload.role_code},
            )
        if student_id is not None:
            store.audit(
                connection,
                principal,
                "student_link",
                "user",
                str(user_id),
                "success",
                "Аккаунт ресми студент жазбасымен әкімші арқылы байланыстырылды.",
                client_ip(request),
                principal.session_id,
                old_value={"student_id": user["student_id"]},
                new_value={"student_id": student_id},
            )
        updated = connection.execute("SELECT * FROM Users WHERE user_id=?", (user_id,)).fetchone()
        return {"user": public_user(connection, updated, private=True)}


@admin_router.post("/requests/{user_id}/reject")
def reject_registration(
    user_id: int,
    payload: RequestRejection,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("Users.Manage"))],
) -> dict[str, Any]:
    return _reject_registration(user_id, payload, request, principal)


def _reject_registration(
    user_id: int,
    payload: RequestRejection,
    request: Request,
    principal: Principal,
    required_requested_role: str | None = None,
) -> dict[str, Any]:
    _require_admin(principal)
    store = request.app.state.store
    with store.connect(write=True) as connection:
        _admin_current(connection, principal)
        user = connection.execute("SELECT * FROM Users WHERE user_id=?", (user_id,)).fetchone()
        if user is None:
            raise AppError("not_found", "Өтінім табылмады.", 404)
        if (
            required_requested_role is not None
            and user["requested_role"] != required_requested_role
        ):
            raise AppError("teacher_request_required", "Оқытушы тіркелу өтінімін таңдаңыз.", 409)
        if user["approval_status"] == "approved":
            raise AppError(
                "request_reviewed", "Бекітілген аккаунтты пайдаланушылар бөлімінде басқарыңыз.", 409
            )
        reason = safe_review_reason(connection, user_id, payload.reason)
        connection.execute(
            "UPDATE Users SET role_code='pending',active=0,approval_status='rejected',"
            "request_review_required=0,approval_reason=?,student_id=NULL WHERE user_id=?",
            (reason, user_id),
        )
        if user["requested_role"] == "teacher":
            sessions = connection.execute(
                "SELECT session_id FROM Sessions WHERE user_id=? AND status='active'", (user_id,)
            ).fetchall()
            connection.execute(
                "UPDATE Sessions SET status='revoked' WHERE user_id=? AND status='active'",
                (user_id,),
            )
            for session in sessions:
                store.audit(
                    connection,
                    principal,
                    "session_terminate",
                    "session",
                    session["session_id"],
                    "success",
                    "Оқытушы өтінімі қабылданбағандықтан сессия аяқталды.",
                    client_ip(request),
                    session["session_id"],
                )
        store.audit(
            connection,
            principal,
            "user_reject",
            "user",
            str(user_id),
            "success",
            reason,
            client_ip(request),
            principal.session_id,
            old_value={"approval_status": user["approval_status"]},
            new_value={"approval_status": "rejected"},
        )
        updated = connection.execute("SELECT * FROM Users WHERE user_id=?", (user_id,)).fetchone()
        return {"user": public_user(connection, updated, private=True)}


@admin_router.get("/roles")
def roles(
    request: Request, principal: Annotated[Principal, Depends(current_principal)]
) -> dict[str, Any]:
    if not principal.permissions & {"Roles.Manage", "Users.Manage"}:
        raise AppError("forbidden", "Бұл әрекетке рұқсатыңыз жоқ.", 403)
    with request.app.state.store.connect() as connection:
        items = []
        for role in connection.execute("SELECT * FROM Roles ORDER BY code"):
            codes = [
                row[0]
                for row in connection.execute(
                    "SELECT permission_code FROM RolePermissions WHERE role_code = ? "
                    "ORDER BY permission_code",
                    (role["code"],),
                )
            ]
            items.append({"code": role["code"], "label": role["label"], "permissions": codes})
        permissions = [
            dict(row) for row in connection.execute("SELECT * FROM Permissions ORDER BY code")
        ]
        return {"roles": items, "permissions": permissions}


@admin_router.put("/roles/{role_code}/permissions")
def change_permissions(
    role_code: str,
    payload: RoleChange,
    request: Request,
    principal: Annotated[Principal, Depends(require_permission("Roles.Manage"))],
) -> dict[str, Any]:
    store = request.app.state.store
    with store.connect(write=True) as connection:
        if (
            connection.execute("SELECT 1 FROM Roles WHERE code = ?", (role_code,)).fetchone()
            is None
        ):
            raise AppError("not_found", "Рөл табылмады.", 404)
        available = {row[0] for row in connection.execute("SELECT code FROM Permissions")}
        selected = set(payload.permissions)
        if not selected <= available:
            raise AppError("invalid_permission", "Белгісіз құқық коды берілген.", 422)
        if role_code == "admin" and selected != available - {"Learning.Submit"}:
            raise AppError(
                "admin_permissions",
                "Әкімші рөлінің басқару құқықтарын алып тастауға болмайды.",
                409,
            )
        previous = [
            row[0]
            for row in connection.execute(
                "SELECT permission_code FROM RolePermissions WHERE role_code = ?", (role_code,)
            )
        ]
        connection.execute("DELETE FROM RolePermissions WHERE role_code = ?", (role_code,))
        connection.executemany(
            "INSERT INTO RolePermissions(role_code,permission_code) VALUES (?,?)",
            [(role_code, code) for code in sorted(selected)],
        )
        store.audit(
            connection,
            principal,
            "permission_change",
            "role",
            role_code,
            "success",
            "Рөлдің құқықтар матрицасы өзгертілді.",
            client_ip(request),
            principal.session_id,
            old_value={"permissions": sorted(previous)},
            new_value={"permissions": sorted(selected)},
        )
    return {"role_code": role_code, "permissions": sorted(selected)}
