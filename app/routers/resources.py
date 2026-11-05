"""Authenticated SQLModel CRUD with the existing ARM audit transaction."""

import sqlite3
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from sqlalchemy import func, or_, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app.auth import Principal, require_permission
from app.auth_routes import _admin_current, _require_admin
from app.database.db import get_session
from app.database.models import (
    MAX_ID,
    DepartmentResource,
    ResourceCategory,
    ResourceCreate,
    ResourceItem,
    ResourcePage,
    ResourceRead,
    ResourceReference,
    ResourceReferences,
    ResourceRoom,
    ResourceUpdate,
)
from app.errors import AppError
from app.network import client_ip
from app.store import Store

router = APIRouter(prefix="/api", tags=["Department resources"])
Reader = Annotated[Principal, Depends(require_permission("Documents.View"))]
Writer = Annotated[Principal, Depends(require_permission("Users.Manage"))]
DatabaseSession = Annotated[Session, Depends(get_session)]
ResourceId = Annotated[int, Path(ge=1, le=MAX_ID)]


def _item(resource: DepartmentResource) -> ResourceRead:
    assert resource.id is not None
    return ResourceRead(
        **resource.model_dump(),
        category_name=resource.category.name,
        room_name=resource.room.name,
    )


def _find(session: Session, resource_id: int) -> DepartmentResource:
    resource = session.get(DepartmentResource, resource_id)
    if resource is None:
        raise AppError("not_found", "Жабдық табылмады.", 404)
    return resource


def _begin_write(session: Session, actor: Principal) -> sqlite3.Connection:
    _require_admin(actor)
    session.execute(text("BEGIN IMMEDIATE"))
    connection = cast(sqlite3.Connection, session.connection().connection.driver_connection)
    _admin_current(connection, actor)
    return connection


def _references(session: Session, category_id: int, room_id: int) -> None:
    if session.get(ResourceCategory, category_id) is None:
        raise AppError("resource_category_missing", "Жабдық санаты табылмады.", 422)
    if session.get(ResourceRoom, room_id) is None:
        raise AppError("resource_room_missing", "Бөлме табылмады.", 422)


def _flush(session: Session) -> None:
    try:
        session.flush()
    except IntegrityError as exc:
        raise AppError("resource_conflict", "Бұл түгендеу коды қолданылған.", 409) from exc


def _audit(
    request: Request,
    connection: sqlite3.Connection,
    actor: Principal,
    action: str,
    resource_id: int,
    old: ResourceRead | None = None,
    new: ResourceRead | None = None,
) -> None:
    store = cast(Store, request.app.state.store)
    store.audit(
        connection,
        actor,
        action,
        "department_resource",
        str(resource_id),
        "success",
        {
            "resource_create": "Кафедра жабдығы қосылды.",
            "resource_update": "Кафедра жабдығы өзгертілді.",
            "resource_delete": "Кафедра жабдығы жойылды.",
        }[action],
        client_ip(request),
        session_id=actor.session_id,
        old_value=old.model_dump() if old is not None else None,
        new_value=new.model_dump() if new is not None else None,
    )


@router.get("/resource-categories", response_model=ResourceReferences)
def resource_categories(actor: Reader, session: DatabaseSession) -> ResourceReferences:
    rows = session.exec(select(ResourceCategory).order_by(col(ResourceCategory.id))).all()
    return ResourceReferences(
        items=[ResourceReference(id=row.id, name=row.name) for row in rows if row.id is not None]
    )


@router.get("/resource-rooms", response_model=ResourceReferences)
def resource_rooms(actor: Reader, session: DatabaseSession) -> ResourceReferences:
    rows = session.exec(select(ResourceRoom).order_by(col(ResourceRoom.id))).all()
    return ResourceReferences(
        items=[ResourceReference(id=row.id, name=row.name) for row in rows if row.id is not None]
    )


@router.get("/resources", response_model=ResourcePage)
def resources(
    actor: Reader,
    session: DatabaseSession,
    search: Annotated[str, Query(max_length=160)] = "",
    q: Annotated[str, Query(max_length=160)] = "",
    category_id: Annotated[int | None, Query(ge=1, le=MAX_ID)] = None,
    room_id: Annotated[int | None, Query(ge=1, le=MAX_ID)] = None,
    min_cost: Annotated[float | None, Query(ge=0, allow_inf_nan=False)] = None,
    max_cost: Annotated[float | None, Query(ge=0, allow_inf_nan=False)] = None,
    page: Annotated[int, Query(ge=1, le=1_000_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> ResourcePage:
    if search.strip() and q.strip() and search.strip() != q.strip():
        raise AppError("invalid_request", "Іздеу мәндерін бірдей көрсетіңіз.", 422)
    if min_cost is not None and max_cost is not None and min_cost > max_cost:
        raise AppError("invalid_request", "Ең төменгі құн ең жоғарғы құннан аспауы керек.", 422)
    statement = select(DepartmentResource)
    term = (search.strip() or q.strip()).casefold()
    if term:
        statement = statement.where(
            or_(
                func.casefold(DepartmentResource.name).contains(term, autoescape=True),
                func.casefold(DepartmentResource.inventory_code).contains(term, autoescape=True),
            )
        )
    if category_id is not None:
        statement = statement.where(DepartmentResource.category_id == category_id)
    if room_id is not None:
        statement = statement.where(DepartmentResource.room_id == room_id)
    if min_cost is not None:
        statement = statement.where(DepartmentResource.unit_cost >= min_cost)
    if max_cost is not None:
        statement = statement.where(DepartmentResource.unit_cost <= max_cost)
    total = session.exec(select(func.count()).select_from(statement.subquery())).one()
    rows = session.exec(
        statement.order_by(col(DepartmentResource.id))
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return ResourcePage(
        items=[_item(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
        pages=(total + page_size - 1) // page_size,
    )


@router.get("/resources/{resource_id}", response_model=ResourceItem)
def resource_detail(
    resource_id: ResourceId, actor: Reader, session: DatabaseSession
) -> ResourceItem:
    return ResourceItem(item=_item(_find(session, resource_id)))


@router.post("/resources", status_code=201, response_model=ResourceItem)
def create_resource(
    payload: ResourceCreate, request: Request, actor: Writer, session: DatabaseSession
) -> ResourceItem:
    connection = _begin_write(session, actor)
    _references(session, payload.category_id, payload.room_id)
    resource = DepartmentResource.model_validate(payload)
    session.add(resource)
    _flush(session)
    item = _item(resource)
    _audit(request, connection, actor, "resource_create", item.id, new=item)
    session.commit()
    return ResourceItem(item=item)


@router.patch("/resources/{resource_id}", response_model=ResourceItem)
def update_resource(
    resource_id: ResourceId,
    payload: ResourceUpdate,
    request: Request,
    actor: Writer,
    session: DatabaseSession,
) -> ResourceItem:
    connection = _begin_write(session, actor)
    resource = _find(session, resource_id)
    old = _item(resource)
    changes = payload.model_dump(exclude_unset=True)
    merged = ResourceCreate.model_validate(
        {field: getattr(resource, field) for field in ResourceCreate.model_fields} | changes
    )
    _references(session, merged.category_id, merged.room_id)
    resource.sqlmodel_update(changes)
    session.add(resource)
    _flush(session)
    # Expire cached relationships after a foreign-key change.
    session.expire(resource, ["category", "room"])
    new = _item(resource)
    _audit(request, connection, actor, "resource_update", resource_id, old=old, new=new)
    session.commit()
    return ResourceItem(item=new)


@router.delete("/resources/{resource_id}", status_code=204)
def delete_resource(
    resource_id: ResourceId, request: Request, actor: Writer, session: DatabaseSession
) -> Response:
    connection = _begin_write(session, actor)
    resource = _find(session, resource_id)
    old = _item(resource)
    session.delete(resource)
    session.flush()
    _audit(request, connection, actor, "resource_delete", resource_id, old=old)
    session.commit()
    return Response(status_code=204)
