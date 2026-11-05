"""Scoped tasks, student work and human review; model inference never grades work."""

import hashlib
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Self
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.academic import (
    _check_teacher_filters,
    _document_path,
    _scope,
    _upload_name,
    _validate_content,
)
from app.auth import Principal, require_permission
from app.errors import AppError
from app.network import client_ip
from app.schemas import utc_datetime
from app.store import Store, iso_utc, utc_now

router = APIRouter(prefix="/api", tags=["Learning tasks"])
PositiveId = Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]


def ensure_schema(connection: sqlite3.Connection) -> None:
    """Run inside Store's upgrade transaction, without implicit DDL commits."""
    connection.execute(
        """CREATE TABLE IF NOT EXISTS Tasks(
        task_id INTEGER PRIMARY KEY,title TEXT NOT NULL,description TEXT NOT NULL,
        rules TEXT NOT NULL DEFAULT '',subject_id INTEGER NOT NULL REFERENCES Subjects(subject_id),
        group_id INTEGER NOT NULL REFERENCES Groups(group_id),
        term_id INTEGER NOT NULL REFERENCES Terms(term_id),
        published_at TEXT NOT NULL,deadline TEXT NOT NULL,
        allow_late INTEGER NOT NULL DEFAULT 0 CHECK(allow_late IN (0,1)),
        created_by INTEGER NOT NULL REFERENCES Users(user_id),created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 0,
        material_name TEXT,material_stored_name TEXT,material_mime_type TEXT,
        material_size_bytes INTEGER CHECK(material_size_bytes IS NULL OR material_size_bytes>0))"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS Submissions(
        submission_id INTEGER PRIMARY KEY,task_id INTEGER NOT NULL REFERENCES Tasks(task_id),
        student_id INTEGER NOT NULL REFERENCES Students(student_id),
        submitted_by INTEGER NOT NULL REFERENCES Users(user_id),
        text_content TEXT NOT NULL DEFAULT '',
        file_name TEXT,stored_name TEXT,mime_type TEXT,
        size_bytes INTEGER CHECK(size_bytes IS NULL OR size_bytes>0),
        submitted_at TEXT NOT NULL,updated_at TEXT NOT NULL,
        is_late INTEGER NOT NULL DEFAULT 0 CHECK(is_late IN (0,1)),
        version INTEGER NOT NULL DEFAULT 0,feedback TEXT NOT NULL DEFAULT '',score INTEGER
        CHECK(score IS NULL OR (typeof(score)='integer' AND score BETWEEN 0 AND 100)),
        published INTEGER NOT NULL DEFAULT 0 CHECK(published IN (0,1)),
        reviewed_by INTEGER REFERENCES Users(user_id),reviewed_at TEXT,
        UNIQUE(task_id,student_id))"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS tasks_scope_idx "
        "ON Tasks(group_id,subject_id,term_id,published_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS submissions_task_idx ON Submissions(task_id,submitted_at)"
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS SubmissionReviewHistory(
        history_id INTEGER PRIMARY KEY,submission_id INTEGER NOT NULL
        REFERENCES Submissions(submission_id),actor_id INTEGER NOT NULL REFERENCES Users(user_id),
        occurred_at TEXT NOT NULL,ip_address TEXT NOT NULL,
        old_score INTEGER,new_score INTEGER NOT NULL CHECK(new_score BETWEEN 0 AND 100),
        old_feedback TEXT NOT NULL,new_feedback TEXT NOT NULL,
        old_published INTEGER NOT NULL CHECK(old_published IN (0,1)),
        new_published INTEGER NOT NULL CHECK(new_published IN (0,1)),
        old_version INTEGER NOT NULL,new_version INTEGER NOT NULL,
        audit_id INTEGER NOT NULL UNIQUE REFERENCES AuditLogs(audit_id))"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS review_history_submission_idx "
        "ON SubmissionReviewHistory(submission_id,history_id)"
    )


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=3, max_length=180)
    description: str = Field(min_length=1, max_length=10000)
    rules: str = Field(default="", max_length=2000)
    subject_id: PositiveId
    group_id: PositiveId
    term_id: PositiveId
    published_at: datetime
    deadline: datetime
    allow_late: bool = Field(default=False, strict=True)

    @field_validator("published_at", "deadline", mode="before")
    @classmethod
    def parse_time(cls, value: object) -> datetime:
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if not isinstance(value, datetime):
            raise ValueError("An ISO datetime with an explicit UTC offset is required")
        return utc_datetime(value)

    @model_validator(mode="after")
    def ordered_times(self) -> Self:
        if self.deadline <= self.published_at:
            raise ValueError("deadline must be after published_at")
        return self


class TaskChange(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str | None = Field(default=None, min_length=3, max_length=180)
    description: str | None = Field(default=None, min_length=1, max_length=10000)
    rules: str | None = Field(default=None, max_length=2000)
    subject_id: PositiveId | None = None
    group_id: PositiveId | None = None
    term_id: PositiveId | None = None
    published_at: datetime | None = None
    deadline: datetime | None = None
    allow_late: bool | None = Field(default=None, strict=True)
    version: int = Field(strict=True, ge=0)

    @field_validator("published_at", "deadline", mode="before")
    @classmethod
    def parse_time(cls, value: object) -> datetime:
        return TaskCreate.parse_time(value)

    @model_validator(mode="after")
    def actual_change(self) -> Self:
        fields = self.model_fields_set - {"version"}
        if not fields or any(getattr(self, name) is None for name in fields):
            raise ValueError("At least one non-null task field must be supplied")
        return self


class ReviewChange(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    feedback: str = Field(default="", max_length=5000)
    score: int = Field(strict=True, ge=0, le=100)
    published: bool = Field(default=False, strict=True)
    version: int = Field(strict=True, ge=0)


def _store(request: Request) -> Store:
    store: Store = request.app.state.store
    return store


def _audit(
    store: Store,
    connection: sqlite3.Connection,
    request: Request,
    actor: Principal,
    action: str,
    object_type: str,
    object_id: int,
    reason: str,
    result: str = "success",
    old_value: object = None,
    new_value: object = None,
) -> int:
    return store.audit(
        connection,
        actor,
        action,
        object_type,
        str(object_id),
        result,
        reason,
        client_ip(request),
        old_value=old_value,
        new_value=new_value,
    )


def _deny(
    request: Request, actor: Principal, action: str, object_type: str, object_id: int
) -> AppError:
    store = _store(request)
    with store.connect(write=True) as connection:
        _audit(
            store,
            connection,
            request,
            actor,
            action,
            object_type,
            object_id,
            "Тапсырма немесе жұмыс бойынша рөл/пән/топ/иелік шектеуі сақталды.",
            "denied",
        )
    return AppError("forbidden", "Бұл тапсырмаға немесе студент жұмысына рұқсат жоқ.", 403)


def _student_group(connection: sqlite3.Connection, actor: Principal) -> int | None:
    if (
        actor.role_code != "student"
        or actor.student_id is None
        or actor.approval_status != "approved"
    ):
        return None
    row = connection.execute(
        "SELECT s.group_id FROM Users u JOIN Students s ON s.student_id=u.student_id "
        "WHERE u.user_id=? AND u.student_id=? AND u.role_code='student' "
        "AND u.approval_status='approved' AND u.active=1",
        (actor.user_id, actor.student_id),
    ).fetchone()
    return int(row["group_id"]) if row is not None else None


def _actor_current(connection: sqlite3.Connection, actor: Principal, permission: str) -> bool:
    """Recheck approvals, role and permission under the business transaction's lock."""
    row = connection.execute(
        "SELECT role_code,approval_status,active,student_id FROM Users WHERE user_id=?",
        (actor.user_id,),
    ).fetchone()
    return (
        row is not None
        and bool(row["active"])
        and row["approval_status"] == "approved"
        and row["role_code"] == actor.role_code
        and (actor.role_code != "student" or row["student_id"] == actor.student_id)
        and permission in actor.permissions
        and connection.execute(
            "SELECT 1 FROM RolePermissions WHERE role_code=? AND permission_code=?",
            (actor.role_code, permission),
        ).fetchone()
        is not None
    )


def _can_manage(
    connection: sqlite3.Connection, actor: Principal, task: sqlite3.Row | dict[str, Any]
) -> bool:
    return (
        actor.approval_status == "approved"
        and _actor_current(connection, actor, "Learning.Manage")
        and "Learning.Manage" in actor.permissions
        and (
            actor.role_code == "admin"
            or (
                actor.role_code == "teacher"
                and _scope(connection, actor, task["subject_id"], task["group_id"], task["term_id"])
            )
        )
    )


def _can_view(connection: sqlite3.Connection, actor: Principal, task: sqlite3.Row) -> bool:
    if actor.approval_status != "approved" or not _actor_current(
        connection, actor, "Learning.View"
    ):
        return False
    if actor.role_code in {"admin", "head"}:
        return True
    if actor.role_code == "teacher":
        return _scope(connection, actor, task["subject_id"], task["group_id"], task["term_id"])
    return (
        actor.role_code == "student"
        and _student_group(connection, actor) == task["group_id"]
        and iso_utc(utc_now()) >= task["published_at"]
    )


def _references(connection: sqlite3.Connection, data: dict[str, Any]) -> None:
    for table, column in (("Subjects", "subject_id"), ("Groups", "group_id"), ("Terms", "term_id")):
        row = connection.execute(
            f"SELECT active FROM {table} WHERE {column}=?", (data[column],)
        ).fetchone()
        if row is None:
            raise AppError("invalid_task", "Пән, топ немесе семестр табылмады.", 422)
        if not row["active"]:
            raise AppError("inactive_catalog", "Белсенді пән, топ және оқу кезеңін таңдаңыз.", 422)


def _text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file_audit_fields(
    file_ref: str | None,
    mime_type: str | None,
    size_bytes: int | None,
    content: bytes | None = None,
) -> dict[str, Any]:
    # Only opaque server-generated references enter audit metadata, never paths/names.
    valid = file_ref is not None and re.fullmatch(r"[0-9a-f]{32}\.(?:pdf|txt|png|jpe?g)", file_ref)
    result: dict[str, Any] = {
        "file_ref": file_ref if valid else None,
        "mime_type": mime_type if valid else None,
        "size_bytes": size_bytes if valid else None,
    }
    if content is not None:
        result["content_sha256"] = hashlib.sha256(content).hexdigest()
    return result


def _task_audit_fields(task: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    result = {
        key: task[key]
        for key in (
            "title",
            "subject_id",
            "group_id",
            "term_id",
            "published_at",
            "deadline",
            "version",
        )
    }
    for key in ("published_at", "deadline"):
        if isinstance(result[key], datetime):
            result[key] = iso_utc(result[key])
    result["allow_late"] = bool(task["allow_late"])
    # Content digests prove a change without duplicating private instructional text.
    result["description_digest"] = _text_digest(task["description"])
    result["rules_digest"] = _text_digest(task["rules"])
    return result


_TASK_SELECT = (
    "SELECT t.*,s.name AS subject_name,g.name AS group_name,te.name AS term_name,"
    "u.display_name AS teacher_name,u.login AS teacher_login FROM Tasks t "
    "JOIN Subjects s ON s.subject_id=t.subject_id JOIN Groups g ON g.group_id=t.group_id "
    "JOIN Terms te ON te.term_id=t.term_id JOIN Users u ON u.user_id=t.created_by"
)
_SUBMISSION_SELECT = (
    "SELECT su.*,s.display_name AS student_name,s.student_code FROM Submissions su "
    "JOIN Students s ON s.student_id=su.student_id"
)


def _submission_item(
    actor: Principal, submission: sqlite3.Row, task: sqlite3.Row
) -> dict[str, Any]:
    item = {
        key: submission[key]
        for key in submission.keys()
        if key not in {"stored_name", "text_content"}
    }
    item["text"] = submission["text_content"]
    item["published"] = bool(submission["published"])
    item["is_late"] = bool(submission["is_late"])
    item["can_review"] = (
        actor.role_code in {"admin", "teacher"} and "Learning.Manage" in actor.permissions
    )
    item["can_resubmit"] = (
        actor.role_code == "student"
        and "Learning.Submit" in actor.permissions
        and actor.student_id == submission["student_id"]
        and (bool(task["allow_late"]) or iso_utc(utc_now()) < task["deadline"])
    )
    if actor.role_code == "student" and not submission["published"]:
        item.update(score=None, feedback="", reviewed_by=None, reviewed_at=None)
    item["status"] = (
        "reviewed"
        if item["score"] is not None and item["published"]
        else "late"
        if item["is_late"]
        else "submitted"
    )
    return item


def _task_item(
    connection: sqlite3.Connection, actor: Principal, task: sqlite3.Row
) -> dict[str, Any]:
    item = {key: task[key] for key in task.keys() if key != "material_stored_name"}
    now = iso_utc(utc_now())
    item["allow_late"] = bool(task["allow_late"])
    item["deadline_passed"] = now >= task["deadline"]
    item["status"] = (
        "upcoming"
        if now < task["published_at"]
        else ("closed" if now >= task["deadline"] and not task["allow_late"] else "open")
    )
    item["can_manage"] = _can_manage(connection, actor, task)
    item["my_submission"] = None
    item["submission_count"] = None
    if actor.role_code == "student":
        submission = connection.execute(
            _SUBMISSION_SELECT + " WHERE su.task_id=? AND su.student_id=?",
            (task["task_id"], actor.student_id),
        ).fetchone()
        if submission is not None:
            item["my_submission"] = _submission_item(actor, submission, task)
    else:
        item["submission_count"] = connection.execute(
            "SELECT COUNT(*) FROM Submissions WHERE task_id=?", (task["task_id"],)
        ).fetchone()[0]
    return item


@router.get("/tasks")
def tasks(
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Learning.View"))],
    group_id: Annotated[int | None, Query(ge=1)] = None,
    subject_id: Annotated[int | None, Query(ge=1)] = None,
    term_id: Annotated[int | None, Query(ge=1)] = None,
) -> dict[str, Any]:
    clauses: list[str] = []
    values: list[str | int] = []
    for column, value in (
        ("t.group_id", group_id),
        ("t.subject_id", subject_id),
        ("t.term_id", term_id),
    ):
        if value is not None:
            clauses.append(column + "=?")
            values.append(value)
    store = _store(request)
    with store.connect(write=True) as connection:
        if not _actor_current(connection, actor, "Learning.View"):
            raise AppError("forbidden", "Тапсырмаларды қарау рұқсаты өзгерді.", 403)
        if actor.approval_status != "approved":
            raise AppError("approval_pending", "Аккаунт мақұлдануы қажет.", 403)
        if actor.role_code == "student":
            group = _student_group(connection, actor)
            if group is None:
                raise AppError("student_unlinked", "Студент тіркелгісі байланыстырылмаған.", 403)
            clauses.extend(["t.group_id=?", "t.published_at<=?"])
            values.extend([group, iso_utc(utc_now())])
        elif actor.role_code == "teacher":
            clauses.append(
                "EXISTS (SELECT 1 FROM ActiveTeachingAssignments a WHERE a.user_id=? AND "
                "a.group_id=t.group_id AND a.subject_id=t.subject_id AND a.term_id=t.term_id)"
            )
            values.append(actor.user_id)
        elif actor.role_code not in {"admin", "head"}:
            raise AppError("forbidden", "Тапсырмаларды қарау рұқсаты жоқ.", 403)
        _check_teacher_filters(connection, actor, group_id, subject_id, term_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = connection.execute(
            _TASK_SELECT + where + " ORDER BY t.deadline,t.task_id DESC", values
        ).fetchall()
        items = [_task_item(connection, actor, row) for row in rows]
    return {"items": items, "total": len(items)}


@router.post("/tasks", status_code=201)
def create_task(
    payload: TaskCreate,
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Learning.Manage"))],
) -> dict[str, Any]:
    return _create_task(request, actor, payload)


def _future_deadline(deadline: datetime) -> None:
    if deadline <= utc_now():
        raise AppError("deadline_in_past", "Дедлайн болашақ уақытта болуы керек.", 422)


def _create_task(
    request: Request,
    actor: Principal,
    payload: TaskCreate,
    material: tuple[bytes, str, str, str] | None = None,
) -> dict[str, Any]:
    store, data = _store(request), payload.model_dump()
    _future_deadline(payload.deadline)
    destination: Path | None = None
    stored: str | None = None
    if material is not None:
        destination, stored = _save_file(request, material[0], material[2])
    denied = False
    result: dict[str, Any] = {}
    try:
        with store.connect(write=True) as connection:
            if not _can_manage(connection, actor, data):
                _audit(
                    store,
                    connection,
                    request,
                    actor,
                    "task_create",
                    "task",
                    0,
                    "Басқа пән/топ/семестрге тапсырма жасауға тыйым салынды.",
                    "denied",
                )
                denied = True
            else:
                _references(connection, data)
                _future_deadline(payload.deadline)
                now = iso_utc(utc_now())
                cursor = connection.execute(
                    "INSERT INTO Tasks "
                    "(title,description,rules,subject_id,group_id,term_id,published_at,"
                    "deadline,allow_late,created_by,created_at,updated_at,material_name,"
                    "material_stored_name,material_mime_type,material_size_bytes) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        payload.title,
                        payload.description,
                        payload.rules,
                        payload.subject_id,
                        payload.group_id,
                        payload.term_id,
                        iso_utc(payload.published_at),
                        iso_utc(payload.deadline),
                        int(payload.allow_late),
                        actor.user_id,
                        now,
                        now,
                        material[1] if material else None,
                        stored,
                        material[3] if material else None,
                        len(material[0]) if material else None,
                    ),
                )
                assert cursor.lastrowid is not None
                task_id = cursor.lastrowid
                _audit(
                    store,
                    connection,
                    request,
                    actor,
                    "task_create",
                    "task",
                    task_id,
                    "Оқу тапсырмасы дедлайн және тапсыру ережесімен жасалды.",
                    new_value=_task_audit_fields({**data, "version": 0}),
                )
                if material is not None:
                    _audit(
                        store,
                        connection,
                        request,
                        actor,
                        "task_material_upload",
                        "task",
                        task_id,
                        "Бастапқы материал тапсырмамен бірге тұтас сақталды.",
                        new_value={
                            **_file_audit_fields(
                                stored, material[3], len(material[0]), material[0]
                            ),
                            "has_material": True,
                            "version": 0,
                        },
                    )
                row = connection.execute(_TASK_SELECT + " WHERE t.task_id=?", (task_id,)).fetchone()
                assert row is not None
                result = _task_item(connection, actor, row)
        if denied:
            raise AppError("forbidden", "Бұл пәнге/топқа тапсырма жасауға рұқсат жоқ.", 403)
    except Exception:
        if destination is not None:
            destination.unlink(missing_ok=True)
        raise
    return result


@router.post("/tasks/with-material", status_code=201)
async def create_task_with_material(
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Learning.Manage"))],
    payload: Annotated[str, Form(max_length=20000)],
    file: Annotated[UploadFile, File()],
) -> dict[str, Any]:
    try:
        parsed = TaskCreate.model_validate_json(payload)
        _future_deadline(parsed.deadline)
        with _store(request).connect() as connection:
            allowed = _can_manage(connection, actor, parsed.model_dump())
            if allowed:
                _references(connection, parsed.model_dump())
        if not allowed:
            raise _deny(request, actor, "task_create", "task", 0)
        material = await _read_file(request, file)
        return _create_task(request, actor, parsed, material)
    except ValidationError as error:
        raise AppError(
            "invalid_task", "Тапсырма өрістері немесе уақыт аралығы жарамсыз.", 422
        ) from error
    finally:
        await file.close()


@router.get("/tasks/{task_id}")
def task_detail(
    task_id: int,
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Learning.View"))],
) -> dict[str, Any]:
    with _store(request).connect(write=True) as connection:
        row = connection.execute(_TASK_SELECT + " WHERE t.task_id=?", (task_id,)).fetchone()
        if row is None:
            raise AppError("not_found", "Тапсырма табылмады.", 404)
        allowed = _can_view(connection, actor, row)
        result = _task_item(connection, actor, row) if allowed else {}
    if not allowed:
        raise _deny(request, actor, "submission_view", "task", task_id)
    return result


@router.patch("/tasks/{task_id}")
def change_task(
    task_id: int,
    payload: TaskChange,
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Learning.Manage"))],
) -> dict[str, Any]:
    store = _store(request)
    error: AppError | None = None
    result: dict[str, Any] = {}
    with store.connect(write=True) as connection:
        row = connection.execute(_TASK_SELECT + " WHERE t.task_id=?", (task_id,)).fetchone()
        if row is None:
            raise AppError("not_found", "Тапсырма табылмады.", 404)
        data = dict(row)
        data.update(payload.model_dump(exclude_unset=True, exclude={"version"}))
        if not _can_manage(connection, actor, row) or not _can_manage(connection, actor, data):
            _audit(
                store,
                connection,
                request,
                actor,
                "task_update",
                "task",
                task_id,
                "Тапсырманы өзгерту пән/топ/семестр шектеуімен қабылданбады.",
                "denied",
            )
            error = AppError("forbidden", "Бұл тапсырманы өзгерту рұқсаты жоқ.", 403)
        elif row["version"] != payload.version:
            error = AppError("version_conflict", "Тапсырма өзгертілген. Бетті жаңартыңыз.", 409)
        else:
            _references(connection, data)
            publication = (
                data["published_at"]
                if isinstance(data["published_at"], datetime)
                else datetime.fromisoformat(data["published_at"])
            )
            deadline = (
                data["deadline"]
                if isinstance(data["deadline"], datetime)
                else datetime.fromisoformat(data["deadline"])
            )
            if deadline <= publication:
                raise AppError(
                    "invalid_task", "Дедлайн жариялану уақытынан кейін болуы керек.", 422
                )
            if "deadline" in payload.model_fields_set and iso_utc(deadline) != row["deadline"]:
                _future_deadline(deadline)
            changed_scope = any(
                data[key] != row[key] for key in ("subject_id", "group_id", "term_id")
            )
            if (
                changed_scope
                and connection.execute(
                    "SELECT 1 FROM Submissions WHERE task_id=? LIMIT 1", (task_id,)
                ).fetchone()
                is not None
            ):
                raise AppError(
                    "scope_locked",
                    "Жұмыстар тапсырылған тапсырманың оқу ауқымын ауыстыруға болмайды.",
                    409,
                )
            connection.execute(
                "UPDATE Tasks SET title=?,description=?,rules=?,subject_id=?,group_id=?,term_id=?,"
                "published_at=?,deadline=?,allow_late=?,version=version+1,updated_at=? "
                "WHERE task_id=?",
                (
                    data["title"],
                    data["description"],
                    data["rules"],
                    data["subject_id"],
                    data["group_id"],
                    data["term_id"],
                    iso_utc(publication),
                    iso_utc(deadline),
                    int(data["allow_late"]),
                    iso_utc(utc_now()),
                    task_id,
                ),
            )
            _audit(
                store,
                connection,
                request,
                actor,
                "task_update",
                "task",
                task_id,
                "Тапсырманың сипаттамасы немесе тапсыру ережесі өзгертілді.",
                old_value=_task_audit_fields(row),
                new_value=_task_audit_fields({**data, "version": row["version"] + 1}),
            )
            fresh = connection.execute(_TASK_SELECT + " WHERE t.task_id=?", (task_id,)).fetchone()
            assert fresh is not None
            result = _task_item(connection, actor, fresh)
    if error is not None:
        raise error
    return result


async def _read_file(request: Request, file: UploadFile) -> tuple[bytes, str, str, str]:
    try:
        name, extension, mime = _upload_name(file.filename, file.content_type)
        content = bytearray()
        while chunk := await file.read(64 * 1024):
            if len(content) + len(chunk) > request.app.state.settings.max_upload_bytes:
                raise AppError("file_too_large", "Файл рұқсат етілген көлемнен үлкен.", 413)
            content.extend(chunk)
        _validate_content(bytes(content), extension)
        return bytes(content), name, extension, mime
    finally:
        await file.close()


def _save_file(request: Request, content: bytes, extension: str) -> tuple[Path, str]:
    stored = uuid4().hex + extension
    destination = _document_path(request, stored)
    destination.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with destination.open("xb") as output:
            created = True
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    except OSError:
        if created:
            destination.unlink(missing_ok=True)
        raise
    return destination, stored


@router.post("/tasks/{task_id}/material")
async def upload_material(
    task_id: int,
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Learning.Manage"))],
    file: Annotated[UploadFile, File()],
    version: Annotated[int, Form(ge=0)],
) -> dict[str, Any]:
    store = _store(request)
    with store.connect() as connection:
        task = connection.execute(_TASK_SELECT + " WHERE t.task_id=?", (task_id,)).fetchone()
        if task is None:
            raise AppError("not_found", "Тапсырма табылмады.", 404)
        allowed = _can_manage(connection, actor, task)
    if not allowed:
        await file.close()
        raise _deny(request, actor, "task_material_upload", "task", task_id)
    content, name, extension, mime = await _read_file(request, file)
    destination, stored = _save_file(request, content, extension)
    try:
        with store.connect(write=True) as connection:
            task = connection.execute(_TASK_SELECT + " WHERE t.task_id=?", (task_id,)).fetchone()
            if task is None or not _can_manage(connection, actor, task):
                raise AppError("forbidden", "Тапсырмаға рұқсат өзгерді.", 403)
            if task["version"] != version:
                raise AppError("version_conflict", "Тапсырма өзгертілген. Бетті жаңартыңыз.", 409)
            connection.execute(
                "UPDATE Tasks SET material_name=?,material_stored_name=?,material_mime_type=?,"
                "material_size_bytes=?,version=version+1,updated_at=? WHERE task_id=?",
                (name, stored, mime, len(content), iso_utc(utc_now()), task_id),
            )
            _audit(
                store,
                connection,
                request,
                actor,
                "task_material_upload",
                "task",
                task_id,
                "Тапсырма материалы қауіпсіз файл ретінде жүктелді.",
                old_value={
                    **_file_audit_fields(
                        task["material_stored_name"],
                        task["material_mime_type"],
                        task["material_size_bytes"],
                    ),
                    "version": task["version"],
                },
                new_value={
                    **_file_audit_fields(stored, mime, len(content), content),
                    "has_material": True,
                    "version": task["version"] + 1,
                },
            )
            fresh = connection.execute(_TASK_SELECT + " WHERE t.task_id=?", (task_id,)).fetchone()
            assert fresh is not None
            result = _task_item(connection, actor, fresh)
    except Exception:
        # Cleanup only this request's file, then preserve the actual failure.
        destination.unlink(missing_ok=True)
        raise
    return result


@router.get("/tasks/{task_id}/material")
def task_material(
    task_id: int,
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Learning.View"))],
) -> FileResponse:
    store = _store(request)
    denied = False
    path: Path | None = None
    with store.connect(write=True) as connection:
        task = connection.execute(_TASK_SELECT + " WHERE t.task_id=?", (task_id,)).fetchone()
        if task is None:
            raise AppError("not_found", "Тапсырма табылмады.", 404)
        allowed = _can_view(connection, actor, task)
        denied = not allowed
        if allowed:
            if not task["material_stored_name"]:
                raise AppError("not_found", "Тапсырма материалы тіркелмеген.", 404)
            path = _document_path(request, task["material_stored_name"])
            if not path.is_file():
                raise AppError("not_found", "Материал файлы қолжетімсіз.", 404)
        _audit(
            store,
            connection,
            request,
            actor,
            "task_material_view",
            "task",
            task_id,
            "Өкілетті пайдаланушыға тапсырма материалы берілді."
            if allowed
            else "Тапсырма материалына қол жеткізу рұқсаты жеткіліксіз.",
            result="denied" if denied else "success",
        )
    if denied:
        raise AppError("forbidden", "Бұл тапсырма материалына рұқсат жоқ.", 403)
    assert path is not None
    return FileResponse(
        path,
        media_type=task["material_mime_type"],
        filename=task["material_name"],
        content_disposition_type="attachment",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/tasks/{task_id}/submissions")
def task_submissions(
    task_id: int,
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Learning.View"))],
) -> dict[str, Any]:
    store = _store(request)
    with store.connect(write=True) as connection:
        task = connection.execute(_TASK_SELECT + " WHERE t.task_id=?", (task_id,)).fetchone()
        if task is None:
            raise AppError("not_found", "Тапсырма табылмады.", 404)
        allowed = actor.role_code in {"admin", "teacher", "head"} and _can_view(
            connection, actor, task
        )
        rows = (
            connection.execute(
                _SUBMISSION_SELECT + " WHERE su.task_id=? ORDER BY su.updated_at DESC", (task_id,)
            ).fetchall()
            if allowed
            else []
        )
        result = [_submission_item(actor, row, task) for row in rows]
        _audit(
            store,
            connection,
            request,
            actor,
            "submission_view",
            "task",
            task_id,
            "Өкілетті қызметкер тапсырылған жұмыстар тізімін қарады."
            if allowed
            else "Тапсырылған жұмыстар тізімін қарауға рұқсат берілмеді.",
            result="success" if allowed else "denied",
        )
    if not allowed:
        raise AppError("forbidden", "Бұл тапсырма жұмыстарына рұқсат жоқ.", 403)
    return {"items": result, "total": len(result)}


@router.post("/tasks/{task_id}/submission")
@router.put("/tasks/{task_id}/submission")
async def submit_work(
    task_id: int,
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Learning.Submit"))],
    text: Annotated[str, Form(max_length=30000)] = "",
    version: Annotated[int, Form(ge=0)] = 0,
    file: Annotated[UploadFile | None, File()] = None,
) -> dict[str, Any]:
    store = _store(request)
    with store.connect() as connection:
        task = connection.execute(_TASK_SELECT + " WHERE t.task_id=?", (task_id,)).fetchone()
        if task is None:
            raise AppError("not_found", "Тапсырма табылмады.", 404)
        allowed = (
            actor.role_code == "student"
            and _can_view(connection, actor, task)
            and _actor_current(connection, actor, "Learning.Submit")
        )
    if not allowed:
        if file is not None:
            await file.close()
        raise _deny(request, actor, "submission_submit", "task", task_id)
    text = text.strip()
    destination: Path | None = None
    stored = name = mime = None
    content: bytes | None = None
    if file is not None:
        content, name, extension, mime = await _read_file(request, file)
        destination, stored = _save_file(request, content, extension)
    try:
        with store.connect(write=True) as connection:
            task = connection.execute(_TASK_SELECT + " WHERE t.task_id=?", (task_id,)).fetchone()
            if (
                task is None
                or not _can_view(connection, actor, task)
                or _student_group(connection, actor) is None
                or not _actor_current(connection, actor, "Learning.Submit")
            ):
                raise AppError(
                    "forbidden", "Тапсырмаға немесе студент тіркелгісіне рұқсат өзгерді.", 403
                )
            now = iso_utc(utc_now())
            late = now >= task["deadline"]
            if late and not task["allow_late"]:
                raise AppError("deadline_passed", "Тапсыру мерзімі аяқталды.", 409)
            previous = connection.execute(
                "SELECT * FROM Submissions WHERE task_id=? AND student_id=?",
                (task_id, actor.student_id),
            ).fetchone()
            if (previous is None and version != 0) or (
                previous is not None and previous["version"] != version
            ):
                raise AppError(
                    "version_conflict", "Жұмыс немесе пікір өзгертілген. Бетті жаңартыңыз.", 409
                )
            if (
                not text
                and stored is None
                and (previous is None or previous["stored_name"] is None)
            ):
                raise AppError("invalid_submission", "Жұмыс мәтінін немесе файл тіркеңіз.", 422)
            if previous is None:
                cursor = connection.execute(
                    "INSERT INTO Submissions "
                    "(task_id,student_id,submitted_by,text_content,file_name,"
                    "stored_name,mime_type,size_bytes,submitted_at,updated_at,is_late) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        task_id,
                        actor.student_id,
                        actor.user_id,
                        text,
                        name,
                        stored,
                        mime,
                        len(content) if content is not None else None,
                        now,
                        now,
                        int(late),
                    ),
                )
                assert cursor.lastrowid is not None
                submission_id, action = cursor.lastrowid, "submission_submit"
            else:
                submission_id, action = previous["submission_id"], "submission_resubmit"
                connection.execute(
                    "UPDATE Submissions SET text_content=?,file_name=?,stored_name=?,"
                    "mime_type=?,size_bytes=?,"
                    "updated_at=?,is_late=?,version=version+1,feedback='',score=NULL,published=0,"
                    "reviewed_by=NULL,reviewed_at=NULL WHERE submission_id=?",
                    (
                        text,
                        name if stored else previous["file_name"],
                        stored or previous["stored_name"],
                        mime if stored else previous["mime_type"],
                        len(content) if content is not None else previous["size_bytes"],
                        now,
                        int(late),
                        submission_id,
                    ),
                )
            _audit(
                store,
                connection,
                request,
                actor,
                action,
                "submission",
                submission_id,
                "Студент өз жұмысын тапсырды немесе мерзім ережесіне сай жаңартты.",
                old_value={
                    **_file_audit_fields(
                        previous["stored_name"], previous["mime_type"], previous["size_bytes"]
                    ),
                    "version": previous["version"],
                    "score": previous["score"],
                    "published": bool(previous["published"]),
                }
                if previous is not None
                else None,
                new_value={
                    **_file_audit_fields(
                        stored or (previous["stored_name"] if previous is not None else None),
                        mime
                        if stored
                        else (previous["mime_type"] if previous is not None else None),
                        len(content)
                        if content is not None
                        else (previous["size_bytes"] if previous is not None else None),
                        content,
                    ),
                    "task_id": task_id,
                    "version": previous["version"] + 1 if previous is not None else 0,
                    "is_late": late,
                    "has_file": bool(stored or (previous and previous["stored_name"])),
                },
            )
            row = connection.execute(
                _SUBMISSION_SELECT + " WHERE su.submission_id=?", (submission_id,)
            ).fetchone()
            assert row is not None
            result = _submission_item(actor, row, task)
    except Exception:
        # Re-raise unexpected adapter/storage errors after cleaning our new file.
        if destination is not None:
            destination.unlink(missing_ok=True)
        raise
    return result


def _read_submission(
    connection: sqlite3.Connection,
    actor: Principal,
    submission_id: int,
) -> tuple[sqlite3.Row, sqlite3.Row, bool]:
    submission = connection.execute(
        _SUBMISSION_SELECT + " WHERE su.submission_id=?", (submission_id,)
    ).fetchone()
    if submission is None:
        raise AppError("not_found", "Студент жұмысы табылмады.", 404)
    task = connection.execute(
        _TASK_SELECT + " WHERE t.task_id=?", (submission["task_id"],)
    ).fetchone()
    assert task is not None
    allowed = _can_view(connection, actor, task) and (
        actor.role_code in {"admin", "teacher", "head"}
        or (actor.role_code == "student" and actor.student_id == submission["student_id"])
    )
    return submission, task, allowed


@router.get("/submissions/{submission_id}")
def submission_detail(
    submission_id: int,
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Learning.View"))],
) -> dict[str, Any]:
    store = _store(request)
    with store.connect(write=True) as connection:
        submission, task, allowed = _read_submission(connection, actor, submission_id)
        _audit(
            store,
            connection,
            request,
            actor,
            "submission_view",
            "submission",
            submission_id,
            "Өкілетті пайдаланушы студент жұмысын қарады."
            if allowed
            else "Студент жұмысына қол жеткізу рұқсаты жеткіліксіз.",
            result="success" if allowed else "denied",
        )
        result = _submission_item(actor, submission, task) if allowed else {}
    if not allowed:
        raise AppError("forbidden", "Бұл студент жұмысына рұқсат жоқ.", 403)
    return result


@router.patch("/submissions/{submission_id}/review")
def review_submission(
    submission_id: int,
    payload: ReviewChange,
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Learning.Manage"))],
) -> dict[str, Any]:
    store = _store(request)
    error: AppError | None = None
    result: dict[str, Any] = {}
    with store.connect(write=True) as connection:
        submission = connection.execute(
            _SUBMISSION_SELECT + " WHERE su.submission_id=?", (submission_id,)
        ).fetchone()
        if submission is None:
            raise AppError("not_found", "Студент жұмысы табылмады.", 404)
        task = connection.execute(
            _TASK_SELECT + " WHERE t.task_id=?", (submission["task_id"],)
        ).fetchone()
        assert task is not None
        if not _can_manage(connection, actor, task):
            _audit(
                store,
                connection,
                request,
                actor,
                "submission_review",
                "submission",
                submission_id,
                "Студент жұмысын бағалау пән/топ/семестр шектеуімен қабылданбады.",
                "denied",
            )
            error = AppError("forbidden", "Бұл жұмысты бағалау рұқсаты жоқ.", 403)
        elif submission["version"] != payload.version:
            error = AppError(
                "version_conflict", "Жұмыс немесе пікір өзгертілген. Бетті жаңартыңыз.", 409
            )
        else:
            connection.execute(
                "UPDATE Submissions SET feedback=?,score=?,published=?,reviewed_by=?,reviewed_at=?,"
                "version=version+1 WHERE submission_id=?",
                (
                    payload.feedback,
                    payload.score,
                    int(payload.published),
                    actor.user_id,
                    iso_utc(utc_now()),
                    submission_id,
                ),
            )
            audit_id = _audit(
                store,
                connection,
                request,
                actor,
                "submission_review",
                "submission",
                submission_id,
                "Оқытушы студент жұмысын қолмен бағалап, пікірін және жариялану күйін сақтады.",
                old_value={
                    "score": submission["score"],
                    "published": bool(submission["published"]),
                    "version": submission["version"],
                    "feedback_digest": _text_digest(submission["feedback"]),
                },
                new_value={
                    "score": payload.score,
                    "published": payload.published,
                    "version": submission["version"] + 1,
                    "feedback_changed": payload.feedback != submission["feedback"],
                    "feedback_digest": _text_digest(payload.feedback),
                },
            )
            connection.execute(
                "INSERT INTO SubmissionReviewHistory "
                "(submission_id,actor_id,occurred_at,ip_address,old_score,new_score,"
                "old_feedback,new_feedback,old_published,new_published,old_version,"
                "new_version,audit_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    submission_id,
                    actor.user_id,
                    iso_utc(utc_now()),
                    client_ip(request),
                    submission["score"],
                    payload.score,
                    submission["feedback"],
                    payload.feedback,
                    int(submission["published"]),
                    int(payload.published),
                    submission["version"],
                    submission["version"] + 1,
                    audit_id,
                ),
            )
            fresh = connection.execute(
                _SUBMISSION_SELECT + " WHERE su.submission_id=?", (submission_id,)
            ).fetchone()
            assert fresh is not None
            result = _submission_item(actor, fresh, task)
    if error is not None:
        raise error
    return result


@router.get("/submissions/{submission_id}/history")
def submission_review_history(
    submission_id: int,
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Learning.View"))],
) -> dict[str, Any]:
    store = _store(request)
    with store.connect(write=True) as connection:
        _, _, visible = _read_submission(connection, actor, submission_id)
        allowed = visible and actor.role_code in {"admin", "teacher", "head"}
        rows = (
            connection.execute(
                "SELECT h.*,u.login AS actor_login,u.display_name AS actor_name "
                "FROM SubmissionReviewHistory h JOIN Users u ON u.user_id=h.actor_id "
                "WHERE h.submission_id=? ORDER BY h.history_id DESC",
                (submission_id,),
            ).fetchall()
            if allowed
            else []
        )
        _audit(
            store,
            connection,
            request,
            actor,
            "submission_view",
            "submission",
            submission_id,
            "Өкілетті қызметкер бағалау мен пікірдің қорғалған тарихын қарады."
            if allowed
            else "Бағалау тарихына қол жеткізу рұқсаты жеткіліксіз.",
            result="success" if allowed else "denied",
        )
        items = [dict(row) for row in rows]
        for item in items:
            item["old_published"] = bool(item["old_published"])
            item["new_published"] = bool(item["new_published"])
    if not allowed:
        raise AppError("forbidden", "Бағалау тарихын тек өкілетті қызметкер қарайды.", 403)
    return {"items": items, "total": len(items)}


@router.get("/submissions/{submission_id}/file")
def submission_file(
    submission_id: int,
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Learning.View"))],
) -> FileResponse:
    store = _store(request)
    path: Path | None = None
    with store.connect(write=True) as connection:
        submission, _, allowed = _read_submission(connection, actor, submission_id)
        if allowed:
            if not submission["stored_name"]:
                raise AppError("not_found", "Жұмысқа файл тіркелмеген.", 404)
            path = _document_path(request, submission["stored_name"])
            if not path.is_file():
                raise AppError("not_found", "Жұмыс файлы қолжетімсіз.", 404)
        _audit(
            store,
            connection,
            request,
            actor,
            "submission_file_view",
            "submission",
            submission_id,
            "Өкілетті пайдаланушыға студент жұмысының файлы берілді."
            if allowed
            else "Студент жұмысының файлына қол жеткізуге рұқсат берілмеді.",
            result="success" if allowed else "denied",
        )
    if not allowed:
        raise AppError("forbidden", "Бұл студент жұмысының файлына рұқсат жоқ.", 403)
    assert path is not None
    return FileResponse(
        path,
        media_type=submission["mime_type"],
        filename=submission["file_name"],
        content_disposition_type="attachment",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )
