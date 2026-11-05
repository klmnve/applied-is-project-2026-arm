"""Administrator teacher workspace, reusing account approval and exact assignments."""

import sqlite3
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from app.academic import assignment_items
from app.auth import Principal, public_user, require_permission
from app.auth_routes import (
    RequestApproval,
    RequestRejection,
    UserChange,
    _admin_current,
    _approve_registration,
    _change_user,
    _reject_registration,
    _require_admin,
)
from app.errors import AppError
from app.student_claims import normalized_name

router = APIRouter(prefix="/api/admin/teachers", tags=["Teacher administration"])
TeacherId = Annotated[int, Path(ge=1, le=2**63 - 1)]


class TeacherApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TeacherState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: bool = Field(strict=True)


def _catalog(connection: sqlite3.Connection) -> dict[str, Any]:
    return {
        "groups": [dict(row) for row in connection.execute("SELECT * FROM Groups ORDER BY name")],
        "subjects": [
            dict(row) for row in connection.execute("SELECT * FROM Subjects ORDER BY name")
        ],
        "terms": [
            dict(row) for row in connection.execute("SELECT * FROM Terms ORDER BY term_id DESC")
        ],
    }


def _teacher(connection: sqlite3.Connection, user_id: int) -> sqlite3.Row:
    row: sqlite3.Row | None = connection.execute(
        "SELECT * FROM Users WHERE user_id=?", (user_id,)
    ).fetchone()
    if row is None or not (
        (row["role_code"] == "teacher" and row["approval_status"] == "approved")
        or (row["requested_role"] == "teacher" and row["approval_status"] != "approved")
    ):
        raise AppError("teacher_not_found", "Оқытушы немесе оның өтінімі табылмады.", 404)
    return row


@router.get("")
def teachers(
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Users.Manage"))],
    view: Literal["requests", "teachers"] = "teachers",
    status: Literal["pending", "rejected", "active", "inactive", "all"] | None = None,
    q: Annotated[str, Query(max_length=120), BeforeValidator(normalized_name)] = "",
    group_id: Annotated[int | None, Query(ge=1, le=2**63 - 1)] = None,
    subject_id: Annotated[int | None, Query(ge=1, le=2**63 - 1)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> dict[str, Any]:
    _require_admin(actor)
    status = status or ("pending" if view == "requests" else "active")
    if status not in (
        {"pending", "rejected", "all"} if view == "requests" else {"active", "inactive", "all"}
    ):
        raise AppError("invalid_teacher_filter", "Осы қойындыға сәйкес күйді таңдаңыз.", 422)
    clauses = [
        "u.requested_role='teacher' AND u.approval_status<>'approved'"
        if view == "requests"
        else "u.role_code='teacher' AND u.approval_status='approved'"
    ]
    values: list[str | int] = []
    if status in {"pending", "rejected"}:
        clauses.append("u.approval_status=?")
        values.append(status)
    elif status in {"active", "inactive"}:
        clauses.append("u.active=?")
        values.append(int(status == "active"))
    if q:
        clauses.append(
            "(instr(teacher_fold(u.display_name),?)>0 OR instr(teacher_fold(u.login),?)>0)"
        )
        values.extend((q, q))
    if group_id is not None or subject_id is not None:
        assignment_clauses = ["a.user_id=u.user_id"]
        for column, value in (("group_id", group_id), ("subject_id", subject_id)):
            if value is not None:
                assignment_clauses.append("a." + column + "=?")
                values.append(value)
        clauses.append(
            "EXISTS (SELECT 1 FROM TeachingAssignments a WHERE "
            + " AND ".join(assignment_clauses)
            + ")"
        )
    with request.app.state.store.connect() as connection:
        connection.execute("BEGIN")
        _admin_current(connection, actor)
        connection.create_function("teacher_fold", 1, normalized_name, deterministic=True)
        for table, key, value in (
            ("Groups", "group_id", group_id),
            ("Subjects", "subject_id", subject_id),
        ):
            if (
                value is not None
                and connection.execute(f"SELECT 1 FROM {table} WHERE {key}=?", (value,)).fetchone()
                is None
            ):
                raise AppError(
                    "invalid_teacher_filter", "Пәнді немесе топты каталогтан таңдаңыз.", 422
                )
        where = " WHERE " + " AND ".join(clauses)
        total = connection.execute("SELECT COUNT(*) FROM Users u" + where, values).fetchone()[0]
        offset = (page - 1) * page_size
        rows = (
            connection.execute(
                "SELECT u.* FROM Users u"
                + where
                + " ORDER BY u.created_at DESC,u.user_id DESC LIMIT ? OFFSET ?",
                [*values, page_size, min(offset, total)],
            ).fetchall()
            if offset < total
            else []
        )
        items = []
        for row in rows:
            item = public_user(connection, row)
            item["assignments"] = assignment_items(connection, row["user_id"])
            items.append(item)
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            **_catalog(connection),
        }


@router.get("/{user_id}")
def teacher_profile(
    user_id: TeacherId,
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Users.Manage"))],
) -> dict[str, Any]:
    _require_admin(actor)
    with request.app.state.store.connect() as connection:
        connection.execute("BEGIN")
        _admin_current(connection, actor)
        row = _teacher(connection, user_id)
        teacher = public_user(connection, row)
        teacher["approval_reason"] = row["approval_reason"]
        return {
            "teacher": teacher,
            "assignments": assignment_items(connection, user_id),
            **_catalog(connection),
        }


@router.post("/{user_id}/approve")
def approve_teacher(
    user_id: TeacherId,
    payload: TeacherApproval,
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Users.Manage"))],
) -> dict[str, Any]:
    return _approve_registration(
        user_id, RequestApproval(role_code="teacher"), request, actor, "teacher"
    )


@router.post("/{user_id}/reject")
def reject_teacher(
    user_id: TeacherId,
    payload: RequestRejection,
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Users.Manage"))],
) -> dict[str, Any]:
    return _reject_registration(user_id, payload, request, actor, "teacher")


@router.patch("/{user_id}")
def teacher_state(
    user_id: TeacherId,
    payload: TeacherState,
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Users.Manage"))],
) -> dict[str, Any]:
    return _change_user(user_id, UserChange(active=payload.active), request, actor, "teacher")
