"""Official student roster with staff authorization and assigned-group scope."""

import sqlite3
import unicodedata
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, BeforeValidator

from app.auth import Principal, current_principal, principal_from_connection
from app.errors import AppError
from app.store import Store

router = APIRouter(prefix="/api", tags=["Official students"])


def _query_text(value: object) -> object:
    if isinstance(value, str):
        return " ".join(unicodedata.normalize("NFKC", value).split())
    return value


def _fold(value: object) -> str:
    text = _query_text(value)
    return text.casefold() if isinstance(text, str) else ""


class StudentItem(BaseModel):
    student_id: int
    display_name: str
    student_code: str
    group_id: int
    group_name: str


class StudentGroup(BaseModel):
    group_id: int
    name: str


class StudentRoster(BaseModel):
    items: list[StudentItem]
    groups: list[StudentGroup]
    total: int
    page: int
    page_size: int


def _staff(connection: sqlite3.Connection, actor: Principal) -> Principal:
    # Read role, approval and permissions again in the roster's snapshot. A role
    # or matrix edit applies even when the caller already had an open session.
    user = connection.execute("SELECT * FROM Users WHERE user_id=?", (actor.user_id,)).fetchone()
    if (
        user is None
        or not user["active"]
        or user["approval_status"] != "approved"
        or user["role_code"] not in {"admin", "head", "teacher"}
    ):
        raise AppError("forbidden", "Студенттер тізімін қарау рұқсаты жоқ.", 403)
    current = principal_from_connection(connection, user, actor.session_id)
    if not current.permissions & {"Grades.View", "Users.Manage"}:
        raise AppError("forbidden", "Студенттер тізімін қарау рұқсаты жоқ.", 403)
    return current


@router.get("/students", response_model=StudentRoster)
def students(
    request: Request,
    actor: Annotated[Principal, Depends(current_principal)],
    group_id: Annotated[int | None, Query(ge=1, le=2**63 - 1)] = None,
    q: Annotated[str, Query(max_length=120), BeforeValidator(_query_text)] = "",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> StudentRoster:
    store: Store = request.app.state.store
    with store.connect() as connection:
        # Count, items, group choices and authority describe one read snapshot.
        connection.execute("BEGIN")
        current = _staff(connection, actor)
        connection.create_function("student_roster_fold", 1, _fold, deterministic=True)
        if group_id is not None:
            if (
                connection.execute("SELECT 1 FROM Groups WHERE group_id=?", (group_id,)).fetchone()
                is None
            ):
                raise AppError("invalid_student_group", "Оқу тобын тізімнен таңдаңыз.", 422)
            if (
                current.role_code == "teacher"
                and connection.execute(
                    "SELECT 1 FROM ActiveTeachingAssignments WHERE user_id=? AND group_id=?",
                    (current.user_id, group_id),
                ).fetchone()
                is None
            ):
                raise AppError("forbidden", "Бұл оқу тобына қол жеткізу рұқсаты жоқ.", 403)
        clauses: list[str] = []
        values: list[str | int] = []
        group_sql = "SELECT g.group_id,g.name FROM Groups g"
        if current.role_code == "teacher":
            clauses.append(
                "EXISTS (SELECT 1 FROM ActiveTeachingAssignments a "
                "WHERE a.user_id=? AND a.group_id=s.group_id)"
            )
            values.append(current.user_id)
            groups = connection.execute(
                group_sql + " WHERE EXISTS (SELECT 1 FROM ActiveTeachingAssignments a "
                "WHERE a.user_id=? AND a.group_id=g.group_id) "
                "ORDER BY student_roster_fold(g.name),g.group_id",
                (current.user_id,),
            ).fetchall()
        else:
            groups = connection.execute(
                group_sql + " ORDER BY student_roster_fold(g.name),g.group_id"
            ).fetchall()
        if group_id is not None:
            clauses.append("s.group_id=?")
            values.append(group_id)
        if q:
            clauses.append(
                "(instr(student_roster_fold(s.display_name),?)>0 "
                "OR instr(student_roster_fold(s.student_code),?)>0)"
            )
            values.extend((_fold(q), _fold(q)))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        source = " FROM Students s JOIN Groups g ON g.group_id=s.group_id"
        total = int(connection.execute("SELECT COUNT(*)" + source + where, values).fetchone()[0])
        offset = (page - 1) * page_size
        rows = []
        # Avoid binding arbitrarily large Python integers to SQLite for a page
        # beyond the result set; it is an ordinary empty page.
        if offset < total:
            rows = connection.execute(
                "SELECT s.student_id,s.display_name,s.student_code,s.group_id,g.name AS group_name"
                + source
                + where
                + " ORDER BY student_roster_fold(s.display_name),s.student_id "
                "LIMIT ? OFFSET ?",
                [*values, page_size, offset],
            ).fetchall()
    return StudentRoster(
        items=[StudentItem.model_validate(dict(row)) for row in rows],
        groups=[StudentGroup.model_validate(dict(row)) for row in groups],
        total=total,
        page=page,
        page_size=page_size,
    )
