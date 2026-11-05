"""Academic records and safe documents, protected by server permissions and scope."""

import os
import re
import sqlite3
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi import Path as ApiPath
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from app.auth import Principal, current_principal, require_permission
from app.auth_routes import _admin_current, _require_admin
from app.errors import AppError
from app.network import client_ip
from app.store import Store, iso_utc, utc_now

router = APIRouter(prefix="/api", tags=["Academic system"])
PositiveId = Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]


class GradeChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int = Field(strict=True, ge=0, le=100)
    version: int = Field(strict=True, ge=0)


class AssignmentChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: PositiveId
    subject_id: PositiveId
    group_id: PositiveId
    term_id: PositiveId
    assigned: bool = Field(strict=True)


class AssignmentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject_id: PositiveId
    group_id: PositiveId
    term_id: PositiveId


def assignment_items(
    connection: sqlite3.Connection, user_id: int | None = None, active_only: bool = False
) -> list[dict[str, Any]]:
    source = "ActiveTeachingAssignments" if active_only else "TeachingAssignments"
    rows = connection.execute(
        "SELECT a.*,u.login AS user_login,u.display_name AS user_name,"
        "s.name AS subject_name,g.name AS group_name,t.name AS term_name,t.academic_year,"
        "EXISTS(SELECT 1 FROM ActiveTeachingAssignments live "
        "WHERE live.assignment_id=a.assignment_id) AS active "
        f"FROM {source} a JOIN Users u ON u.user_id=a.user_id "
        "JOIN Subjects s ON s.subject_id=a.subject_id JOIN Groups g ON g.group_id=a.group_id "
        "JOIN Terms t ON t.term_id=a.term_id"
        + (" WHERE a.user_id=?" if user_id is not None else "")
        + " ORDER BY u.login,g.name,s.name,t.term_id,a.assignment_id",
        (user_id,) if user_id is not None else (),
    ).fetchall()
    return [{**dict(row), "active": bool(row["active"])} for row in rows]


def _assignment_references(
    connection: sqlite3.Connection, subject_id: int, group_id: int, term_id: int
) -> None:
    for table, key, value in (
        ("Subjects", "subject_id", subject_id),
        ("Groups", "group_id", group_id),
        ("Terms", "term_id", term_id),
    ):
        if (
            connection.execute(
                f"SELECT 1 FROM {table} WHERE {key}=? AND active=1", (value,)
            ).fetchone()
            is None
        ):
            raise AppError(
                "invalid_assignment", "Белсенді пән, топ және оқу кезеңін таңдаңыз.", 422
            )


def _check_teacher_filters(
    connection: sqlite3.Connection,
    actor: Principal,
    group_id: int | None,
    subject_id: int | None,
    term_id: int | None,
) -> None:
    if actor.role_code != "teacher" or all(
        value is None for value in (group_id, subject_id, term_id)
    ):
        return
    clauses = ["user_id=?"]
    values = [actor.user_id]
    for key, value in (("group_id", group_id), ("subject_id", subject_id), ("term_id", term_id)):
        if value is not None:
            clauses.append(key + "=?")
            values.append(value)
    if (
        connection.execute(
            "SELECT 1 FROM ActiveTeachingAssignments WHERE " + " AND ".join(clauses), values
        ).fetchone()
        is None
    ):
        raise AppError("forbidden", "Бұл пән/топ/оқу кезеңіне қол жеткізу рұқсаты жоқ.", 403)


def _store(request: Request) -> Store:
    return request.app.state.store  # type: ignore[no-any-return]


def _catalog_principal(
    actor: Annotated[Principal, Depends(current_principal)],
) -> Principal:
    if not ({"Grades.View", "Users.Manage", "Learning.View"} & actor.permissions):
        raise AppError("forbidden", "Оқу анықтамалығын қарау рұқсаты жоқ.", 403)
    return actor


def _scope(
    connection: sqlite3.Connection, actor: Principal, subject_id: int, group_id: int, term_id: int
) -> bool:
    if actor.role_code == "teacher":
        return (
            connection.execute(
                "SELECT 1 FROM ActiveTeachingAssignments WHERE user_id=? AND subject_id=? "
                "AND group_id=? AND term_id=?",
                (actor.user_id, subject_id, group_id, term_id),
            ).fetchone()
            is not None
        )
    return actor.role_code in {"admin", "head"}


def _can_edit(connection: sqlite3.Connection, actor: Principal, row: sqlite3.Row) -> bool:
    return "Grades.Edit" in actor.permissions and (
        actor.role_code == "admin"
        or (
            actor.role_code == "teacher"
            and _scope(connection, actor, row["subject_id"], row["group_id"], row["term_id"])
        )
    )


def _audit(
    store: Store,
    connection: sqlite3.Connection,
    request: Request,
    actor: Principal,
    action: str,
    object_type: str,
    object_id: int,
    result: str,
    reason: str,
    old_value: Any = None,
    new_value: Any = None,
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
        session_id=actor.session_id,
        old_value=old_value,
        new_value=new_value,
    )


def _denied(
    request: Request, actor: Principal, action: str, object_type: str, object_id: int
) -> AppError:
    # Called after the read connection closes, so this evidence commits independently.
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
            "denied",
            "Пайдаланушыға осы пән/топ/семестр нысаны бойынша әрекет етуге рұқсат жоқ.",
        )
    return AppError("forbidden", "Бұл пәнге, топқа немесе семестрге қол жеткізу рұқсаты жоқ.", 403)


_GRADE_SELECT = (
    "SELECT g.grade_id,g.student_id,s.display_name AS student_name,s.student_code,"
    "s.group_id,gr.name AS group_name,g.subject_id,su.name AS subject_name,"
    "g.term_id,t.name AS term_name,g.value,g.version,g.updated_at "
    "FROM Grades g JOIN Students s ON s.student_id=g.student_id "
    "JOIN Groups gr ON gr.group_id=s.group_id "
    "JOIN Subjects su ON su.subject_id=g.subject_id JOIN Terms t ON t.term_id=g.term_id"
)


def _grade_item(
    connection: sqlite3.Connection, actor: Principal, row: sqlite3.Row
) -> dict[str, Any]:
    item = dict(row)
    item["can_edit"] = _can_edit(connection, actor, row)
    return item


@router.get("/academic/catalog")
def catalog(
    request: Request, actor: Annotated[Principal, Depends(_catalog_principal)]
) -> dict[str, Any]:
    store = _store(request)
    with store.connect() as connection:
        if actor.role_code == "teacher":
            assignments = connection.execute(
                "SELECT * FROM ActiveTeachingAssignments WHERE user_id=? ORDER BY assignment_id",
                (actor.user_id,),
            ).fetchall()
            groups = connection.execute(
                "SELECT * FROM Groups WHERE group_id IN "
                "(SELECT group_id FROM ActiveTeachingAssignments WHERE user_id=?) ORDER BY name",
                (actor.user_id,),
            ).fetchall()
            subjects = connection.execute(
                "SELECT * FROM Subjects WHERE subject_id IN "
                "(SELECT subject_id FROM ActiveTeachingAssignments WHERE user_id=?) ORDER BY name",
                (actor.user_id,),
            ).fetchall()
            terms = connection.execute(
                "SELECT * FROM Terms WHERE term_id IN "
                "(SELECT term_id FROM ActiveTeachingAssignments WHERE user_id=?) "
                "ORDER BY term_id DESC",
                (actor.user_id,),
            ).fetchall()
            students = connection.execute(
                "SELECT * FROM Students WHERE group_id IN "
                "(SELECT group_id FROM ActiveTeachingAssignments WHERE user_id=?) "
                "ORDER BY display_name",
                (actor.user_id,),
            ).fetchall()
        elif actor.role_code in {"admin", "head"}:
            groups = connection.execute("SELECT * FROM Groups ORDER BY name").fetchall()
            subjects = connection.execute("SELECT * FROM Subjects ORDER BY name").fetchall()
            terms = connection.execute("SELECT * FROM Terms ORDER BY term_id DESC").fetchall()
            students = connection.execute("SELECT * FROM Students ORDER BY display_name").fetchall()
            assignments = connection.execute(
                "SELECT * FROM TeachingAssignments ORDER BY assignment_id"
            ).fetchall()
        else:
            raise AppError("forbidden", "Оқу деректерін қарау рұқсаты жоқ.", 403)
        if not actor.permissions & {"Grades.View", "Users.Manage"}:
            students = []
    return {
        "groups": [dict(row) for row in groups],
        "subjects": [dict(row) for row in subjects],
        "terms": [dict(row) for row in terms],
        "students": [dict(row) for row in students],
        "assignments": [dict(row) for row in assignments],
    }


@router.get("/grades")
def grades(
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Grades.View"))],
    group_id: Annotated[int | None, Query(ge=1)] = None,
    subject_id: Annotated[int | None, Query(ge=1)] = None,
    term_id: Annotated[int | None, Query(ge=1)] = None,
) -> dict[str, Any]:
    clauses: list[str] = []
    parameters: list[int] = []
    for column, value in (
        ("s.group_id", group_id),
        ("g.subject_id", subject_id),
        ("g.term_id", term_id),
    ):
        if value is not None:
            clauses.append(column + "=?")
            parameters.append(value)
    if actor.role_code == "teacher":
        clauses.append(
            "EXISTS (SELECT 1 FROM ActiveTeachingAssignments a WHERE a.user_id=? "
            "AND a.group_id=s.group_id AND a.subject_id=g.subject_id AND a.term_id=g.term_id)"
        )
        parameters.append(actor.user_id)
    elif actor.role_code not in {"admin", "head"}:
        raise AppError("forbidden", "Ведомость қарау рұқсаты жоқ.", 403)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with _store(request).connect() as connection:
        _check_teacher_filters(connection, actor, group_id, subject_id, term_id)
        rows = connection.execute(
            _GRADE_SELECT + where + " ORDER BY gr.name,s.display_name,su.name,t.term_id", parameters
        ).fetchall()
        items = [_grade_item(connection, actor, row) for row in rows]
    return {"items": items, "total": len(items)}


@router.patch("/grades/{grade_id}")
def change_grade(
    grade_id: int,
    payload: GradeChange,
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Grades.Edit"))],
) -> dict[str, Any]:
    store = _store(request)
    error: AppError | None = None
    updated: dict[str, Any] | None = None
    with store.connect(write=True) as connection:
        row = connection.execute(_GRADE_SELECT + " WHERE g.grade_id=?", (grade_id,)).fetchone()
        if row is None:
            raise AppError("not_found", "Баға жазбасы табылмады.", 404)
        if not _can_edit(connection, actor, row):
            _audit(
                store,
                connection,
                request,
                actor,
                "grade_change",
                "grade",
                grade_id,
                "denied",
                "Басқа пән/топ/семестр бағасын өзгертуге тыйым салынды.",
            )
            error = AppError("forbidden", "Бұл ведомость бағасын өзгерту рұқсаты жоқ.", 403)
        elif row["version"] != payload.version:
            error = AppError(
                "version_conflict", "Баға басқа сұраумен өзгерген. Ведомосты жаңартыңыз.", 409
            )
        else:
            occurred_at = iso_utc(utc_now())
            connection.execute(
                "UPDATE Grades SET value=?,updated_at=?,updated_by=?,version=version+1 "
                "WHERE grade_id=? AND version=?",
                (payload.value, occurred_at, actor.user_id, grade_id, payload.version),
            )
            audit_id = _audit(
                store,
                connection,
                request,
                actor,
                "grade_change",
                "grade",
                grade_id,
                "success",
                "Ведомость бағасы өкілетті пайдаланушы сұрауымен өзгертілді.",
                old_value=row["value"],
                new_value=payload.value,
            )
            connection.execute(
                "INSERT INTO GradeHistory "
                "(grade_id,old_value,new_value,actor_id,occurred_at,ip_address,audit_id) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    grade_id,
                    row["value"],
                    payload.value,
                    actor.user_id,
                    occurred_at,
                    client_ip(request),
                    audit_id,
                ),
            )
            fresh = connection.execute(
                _GRADE_SELECT + " WHERE g.grade_id=?", (grade_id,)
            ).fetchone()
            assert fresh is not None
            updated = _grade_item(connection, actor, fresh)
    if error is not None:
        raise error
    assert updated is not None
    return updated


@router.get("/grades/{grade_id}/history")
def grade_history(
    grade_id: int,
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Grades.View"))],
) -> dict[str, Any]:
    with _store(request).connect() as connection:
        row = connection.execute(_GRADE_SELECT + " WHERE g.grade_id=?", (grade_id,)).fetchone()
        if row is None:
            raise AppError("not_found", "Баға жазбасы табылмады.", 404)
        allowed = _scope(connection, actor, row["subject_id"], row["group_id"], row["term_id"])
        rows = (
            connection.execute(
                "SELECT h.*,u.login AS actor_login,u.display_name AS actor_name "
                "FROM GradeHistory h JOIN Users u ON u.user_id=h.actor_id "
                "WHERE h.grade_id=? ORDER BY h.history_id DESC",
                (grade_id,),
            ).fetchall()
            if allowed
            else []
        )
    if not allowed:
        raise _denied(request, actor, "record_view", "grade", grade_id)
    return {"items": [dict(row) for row in rows]}


@router.get("/academic/assignments")
def assignments(
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Users.Manage"))],
    user_id: Annotated[int | None, Query(ge=1, le=2**63 - 1)] = None,
) -> dict[str, Any]:
    _require_admin(actor)
    with _store(request).connect() as connection:
        connection.execute("BEGIN")
        _admin_current(connection, actor)
        items = assignment_items(connection, user_id)
    return {"items": items}


@router.get("/academic/workload")
def workload(
    request: Request,
    actor: Annotated[Principal, Depends(current_principal)],
) -> dict[str, Any]:
    if actor.role_code != "teacher" or not actor.permissions & {"Grades.View", "Learning.View"}:
        raise AppError("forbidden", "Оқытушының оқу жүктемесіне рұқсат жоқ.", 403)
    with _store(request).connect() as connection:
        items = assignment_items(connection, actor.user_id, active_only=True)
    return {"items": items, "total": len(items)}


@router.put("/academic/assignments")
def change_assignment(
    payload: AssignmentChange,
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Users.Manage"))],
) -> dict[str, bool]:
    _require_admin(actor)
    store = _store(request)
    with store.connect(write=True) as connection:
        _admin_current(connection, actor)
        teacher = connection.execute(
            "SELECT role_code,active,approval_status FROM Users WHERE user_id=?", (payload.user_id,)
        ).fetchone()
        if teacher is None or (
            payload.assigned
            and (
                teacher["role_code"] != "teacher"
                or not teacher["active"]
                or teacher["approval_status"] != "approved"
            )
        ):
            raise AppError("invalid_assignment", "Тек бар оқытушыға пән бекітуге болады.", 422)
        if payload.assigned:
            _assignment_references(
                connection, payload.subject_id, payload.group_id, payload.term_id
            )
        values = (payload.user_id, payload.subject_id, payload.group_id, payload.term_id)
        previous = connection.execute(
            "SELECT * FROM TeachingAssignments WHERE user_id=? AND subject_id=? "
            "AND group_id=? AND term_id=?",
            values,
        ).fetchone()
        if payload.assigned:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO TeachingAssignments (user_id,subject_id,group_id,term_id) "
                "VALUES (?,?,?,?)",
                values,
            )
        else:
            cursor = connection.execute(
                "DELETE FROM TeachingAssignments WHERE user_id=? AND subject_id=? "
                "AND group_id=? AND term_id=?",
                values,
            )
        changed = cursor.rowcount > 0
        if changed:
            _audit(
                store,
                connection,
                request,
                actor,
                "assignment_change",
                "user",
                payload.user_id,
                "success",
                "Оқытушының пән/топ/семестрге қол жеткізу бекітілімі өзгертілді.",
                old_value=dict(previous) if previous else None,
                new_value={
                    key: value for key, value in payload.model_dump().items() if key != "assigned"
                }
                if payload.assigned
                else None,
            )
    return {"assigned": payload.assigned, "changed": changed}


@router.patch("/academic/assignments/{assignment_id}")
def update_assignment(
    assignment_id: Annotated[int, ApiPath(ge=1, le=2**63 - 1)],
    payload: AssignmentUpdate,
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Users.Manage"))],
) -> dict[str, Any]:
    _require_admin(actor)
    store = _store(request)
    with store.connect(write=True) as connection:
        _admin_current(connection, actor)
        previous = connection.execute(
            "SELECT * FROM TeachingAssignments WHERE assignment_id=?", (assignment_id,)
        ).fetchone()
        if previous is None:
            raise AppError("not_found", "Оқу тағайындауы табылмады.", 404)
        teacher = connection.execute(
            "SELECT * FROM Users WHERE user_id=?", (previous["user_id"],)
        ).fetchone()
        if (
            teacher is None
            or teacher["role_code"] != "teacher"
            or not teacher["active"]
            or teacher["approval_status"] != "approved"
        ):
            raise AppError("invalid_assignment", "Белсенді мақұлданған оқытушыны таңдаңыз.", 422)
        _assignment_references(connection, payload.subject_id, payload.group_id, payload.term_id)
        updated = {**dict(previous), **payload.model_dump()}
        if updated != dict(previous):
            try:
                connection.execute(
                    "UPDATE TeachingAssignments SET subject_id=?,group_id=?,term_id=? "
                    "WHERE assignment_id=?",
                    (payload.subject_id, payload.group_id, payload.term_id, assignment_id),
                )
            except sqlite3.IntegrityError as error:
                raise AppError(
                    "assignment_conflict", "Мұндай оқу тағайындауы бұрыннан бар.", 409
                ) from error
            _audit(
                store,
                connection,
                request,
                actor,
                "assignment_change",
                "assignment",
                assignment_id,
                "success",
                "Оқытушының нақты оқу тағайындауы өзгертілді.",
                old_value=dict(previous),
                new_value=updated,
            )
        item = next(
            item
            for item in assignment_items(connection, previous["user_id"])
            if item["assignment_id"] == assignment_id
        )
    return {"item": item}


_MIME = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def _upload_name(original: str | None, mime_type: str | None) -> tuple[str, str, str]:
    if (
        not original
        or len(original) > 200
        or original in {".", ".."}
        or "/" in original
        or "\\" in original
        or any(ord(character) < 32 or ord(character) == 127 for character in original)
    ):
        raise AppError("invalid_file", "Файл атауы қауіпсіз емес.", 422)
    extension = Path(original).suffix.lower()
    if (
        extension not in _MIME
        or (mime_type or "").split(";", 1)[0].lower().strip() != _MIME[extension]
    ):
        raise AppError(
            "invalid_file", "Тек PDF, UTF-8 TXT, PNG және JPEG файлдарын жүктеуге болады.", 422
        )
    return original, extension, _MIME[extension]


def _validate_content(content: bytes, extension: str) -> None:
    valid = False
    if extension == ".pdf":
        valid = content.startswith(b"%PDF-") and b"%%EOF" in content[-1024:]
    elif extension == ".png":
        valid = (
            content.startswith(b"\x89PNG\r\n\x1a\n")
            and content[12:16] == b"IHDR"
            and b"IEND" in content[-16:]
        )
    elif extension in {".jpg", ".jpeg"}:
        valid = content.startswith(b"\xff\xd8\xff") and content.endswith(b"\xff\xd9")
    elif extension == ".txt":
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            text = ""
        valid = (
            bool(text.strip())
            and "\x00" not in text
            and not re.search(
                r"<\s*(?:!doctype\s+html|html|script|svg|iframe|object)\b", text, re.IGNORECASE
            )
        )
    if not valid:
        raise AppError("invalid_file", "Файл мазмұны мәлімделген түріне сәйкес емес.", 422)


def _document_item(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys() if key != "stored_name"}


def _document_path(request: Request, stored_name: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}\.(?:pdf|txt|png|jpe?g)", stored_name):
        raise AppError(
            "invalid_document", "Сақталған құжат жолы қауіпсіздік талабына сай емес.", 500
        )
    directory = Path(request.app.state.settings.upload_dir).resolve()
    path = (directory / stored_name).resolve()
    if path.parent != directory:
        raise AppError("invalid_document", "Құжат жолы рұқсат етілген каталогтан тыс.", 500)
    return path


_DOCUMENT_SELECT = (
    "SELECT d.*,u.login AS uploader_login,s.name AS subject_name,g.name AS group_name,"
    "t.name AS term_name FROM Documents d JOIN Users u ON u.user_id=d.uploaded_by "
    "JOIN Subjects s ON s.subject_id=d.subject_id JOIN Groups g ON g.group_id=d.group_id "
    "JOIN Terms t ON t.term_id=d.term_id"
)


@router.get("/documents")
def documents(
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Documents.View"))],
    group_id: Annotated[int | None, Query(ge=1)] = None,
    subject_id: Annotated[int | None, Query(ge=1)] = None,
    term_id: Annotated[int | None, Query(ge=1)] = None,
) -> dict[str, Any]:
    clauses = ["d.deleted_at IS NULL"]
    parameters: list[int] = []
    for column, value in (
        ("d.group_id", group_id),
        ("d.subject_id", subject_id),
        ("d.term_id", term_id),
    ):
        if value is not None:
            clauses.append(column + "=?")
            parameters.append(value)
    if actor.role_code == "teacher":
        clauses.append(
            "EXISTS (SELECT 1 FROM ActiveTeachingAssignments a WHERE a.user_id=? "
            "AND a.group_id=d.group_id AND a.subject_id=d.subject_id AND a.term_id=d.term_id)"
        )
        parameters.append(actor.user_id)
    elif actor.role_code not in {"admin", "head"}:
        raise AppError("forbidden", "Құжаттарды қарау рұқсаты жоқ.", 403)
    with _store(request).connect() as connection:
        _check_teacher_filters(connection, actor, group_id, subject_id, term_id)
        rows = connection.execute(
            _DOCUMENT_SELECT + " WHERE " + " AND ".join(clauses) + " ORDER BY d.document_id DESC",
            parameters,
        ).fetchall()
    return {"items": [_document_item(row) for row in rows], "total": len(rows)}


@router.post("/documents", status_code=201)
async def upload_document(
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Documents.Upload"))],
    file: Annotated[UploadFile, File()],
    subject_id: Annotated[int, Form(ge=1)],
    group_id: Annotated[int, Form(ge=1)],
    term_id: Annotated[int, Form(ge=1)],
) -> dict[str, Any]:
    store = _store(request)
    with store.connect() as connection:
        allowed = actor.role_code == "admin" or (
            actor.role_code == "teacher"
            and _scope(connection, actor, subject_id, group_id, term_id)
        )
        references_exist = all(
            connection.execute(
                f"SELECT 1 FROM {table} WHERE {key}=? AND active=1", (value,)
            ).fetchone()
            is not None
            for table, key, value in (
                ("Subjects", "subject_id", subject_id),
                ("Groups", "group_id", group_id),
                ("Terms", "term_id", term_id),
            )
        )
    if not allowed:
        raise _denied(request, actor, "document_upload", "document", 0)
    if not references_exist:
        raise AppError("invalid_document", "Пән, топ немесе семестр табылмады.", 422)
    original_name, extension, mime_type = _upload_name(file.filename, file.content_type)
    content = bytearray()
    try:
        while chunk := await file.read(64 * 1024):
            if len(content) + len(chunk) > request.app.state.settings.max_upload_bytes:
                raise AppError("file_too_large", "Файл рұқсат етілген көлемнен үлкен.", 413)
            content.extend(chunk)
    finally:
        await file.close()
    _validate_content(bytes(content), extension)
    stored_name = uuid4().hex + extension
    destination = _document_path(request, stored_name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with destination.open("xb") as output:
            created = True
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        with store.connect(write=True) as connection:
            # Recheck the assignment while holding the same write lock as the mutation.
            if actor.role_code != "admin" and not _scope(
                connection, actor, subject_id, group_id, term_id
            ):
                raise AppError("forbidden", "Пән/топ рұқсаты өзгерді. Бетті жаңартыңыз.", 403)
            _assignment_references(connection, subject_id, group_id, term_id)
            cursor = connection.execute(
                "INSERT INTO Documents (original_name,stored_name,mime_type,size_bytes,subject_id,"
                "group_id,term_id,uploaded_by,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    original_name,
                    stored_name,
                    mime_type,
                    len(content),
                    subject_id,
                    group_id,
                    term_id,
                    actor.user_id,
                    iso_utc(utc_now()),
                ),
            )
            assert cursor.lastrowid is not None
            document_id = cursor.lastrowid
            _audit(
                store,
                connection,
                request,
                actor,
                "document_upload",
                "document",
                document_id,
                "success",
                "Құжат қауіпсіз түрі мен көлемі тексерілгеннен кейін жүктелді.",
            )
            row = connection.execute(
                _DOCUMENT_SELECT + " WHERE d.document_id=?", (document_id,)
            ).fetchone()
            assert row is not None
            result = _document_item(row)
    except (sqlite3.Error, OSError, AppError):
        if created:
            # Only this request's freshly created random filename is eligible for cleanup.
            destination.unlink(missing_ok=True)
        raise
    return result


@router.get("/documents/{document_id}/content")
def document_content(
    document_id: int,
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Documents.View"))],
) -> FileResponse:
    store = _store(request)
    denied = False
    path: Path | None = None
    row: sqlite3.Row | None = None
    with store.connect(write=True) as connection:
        row = connection.execute(
            "SELECT * FROM Documents WHERE document_id=? AND deleted_at IS NULL", (document_id,)
        ).fetchone()
        if row is None:
            raise AppError("not_found", "Құжат табылмады немесе жойылған.", 404)
        if not _scope(connection, actor, row["subject_id"], row["group_id"], row["term_id"]):
            denied = True
            _audit(
                store,
                connection,
                request,
                actor,
                "document_view",
                "document",
                document_id,
                "denied",
                "Құжатты қарау пән/топ/семестр шектеуімен қабылданбады.",
            )
        else:
            path = _document_path(request, row["stored_name"])
            if not path.is_file():
                raise AppError("not_found", "Құжат файлы қолжетімсіз.", 404)
            _audit(
                store,
                connection,
                request,
                actor,
                "document_view",
                "document",
                document_id,
                "success",
                "Өкілетті пайдаланушыға құжат файлы берілді.",
            )
    if denied:
        raise AppError("forbidden", "Бұл құжатты қарау рұқсаты жоқ.", 403)
    assert row is not None and path is not None
    return FileResponse(
        path,
        media_type=row["mime_type"],
        filename=row["original_name"],
        content_disposition_type="attachment",
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
    )


@router.delete("/documents/{document_id}")
def delete_document(
    document_id: int,
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Documents.Delete"))],
) -> dict[str, bool]:
    store = _store(request)
    denied = False
    with store.connect(write=True) as connection:
        row = connection.execute(
            "SELECT * FROM Documents WHERE document_id=?", (document_id,)
        ).fetchone()
        if row is None:
            raise AppError("not_found", "Құжат табылмады.", 404)
        allowed = actor.role_code == "admin" or (
            actor.role_code == "teacher"
            and row["uploaded_by"] == actor.user_id
            and _scope(connection, actor, row["subject_id"], row["group_id"], row["term_id"])
        )
        if not allowed:
            denied = True
            _audit(
                store,
                connection,
                request,
                actor,
                "document_delete",
                "document",
                document_id,
                "denied",
                "Құжатты жоюға рөл, меншік немесе пән/топ рұқсаты жеткіліксіз.",
            )
        elif row["deleted_at"] is None:
            connection.execute(
                "UPDATE Documents SET deleted_at=?,deleted_by=? WHERE document_id=?",
                (iso_utc(utc_now()), actor.user_id, document_id),
            )
            _audit(
                store,
                connection,
                request,
                actor,
                "document_delete",
                "document",
                document_id,
                "success",
                "Құжат жұмсақ жойылды; физикалық файл сақталды.",
            )
    if denied:
        raise AppError("forbidden", "Бұл құжатты жою рұқсаты жоқ.", 403)
    return {"deleted": True}
