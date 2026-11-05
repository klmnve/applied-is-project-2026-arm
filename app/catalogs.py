"""Administrative catalog editing with archive-only deletion semantics."""

import sqlite3
import unicodedata
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.auth import Principal, require_permission
from app.auth_routes import _admin_current, _require_admin
from app.errors import AppError
from app.network import client_ip
from app.store import Store

router = APIRouter(prefix="/api/admin/catalogs", tags=["Academic catalogs"])
CatalogKind = Literal["groups", "subjects", "terms"]
CATALOGS: dict[CatalogKind, tuple[str, str]] = {
    "groups": ("Groups", "group_id"),
    "subjects": ("Subjects", "subject_id"),
    "terms": ("Terms", "term_id"),
}


def _text(value: object) -> object:
    if isinstance(value, str):
        normalized = " ".join(value.split())
        if any(unicodedata.category(char).startswith("C") for char in normalized):
            raise ValueError("Каталог атауында басқару таңбалары болмауы керек")
        return normalized
    return value


def _key(value: str) -> str:
    return unicodedata.normalize("NFKC", " ".join(value.split())).casefold()


class CatalogCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(strict=True, min_length=2, max_length=120)
    academic_year: str | None = Field(default=None, strict=True, min_length=2, max_length=32)

    normalize_text = field_validator("name", "academic_year", mode="before")(_text)


class CatalogChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, strict=True, min_length=2, max_length=120)
    academic_year: str | None = Field(default=None, strict=True, min_length=2, max_length=32)
    active: bool | None = Field(default=None, strict=True)

    normalize_text = field_validator("name", "academic_year", mode="before")(_text)

    @model_validator(mode="after")
    def supplied_values(self) -> "CatalogChange":
        if not self.model_fields_set or any(
            getattr(self, field) is None for field in self.model_fields_set
        ):
            raise ValueError("Кемінде бір нақты өзгерісті көрсетіңіз")
        return self


def _item(row: sqlite3.Row, kind: CatalogKind) -> dict[str, Any]:
    _, id_column = CATALOGS[kind]
    item: dict[str, Any] = {
        id_column: row[id_column],
        "name": row["name"],
        "active": bool(row["active"]),
    }
    if kind == "terms":
        item["academic_year"] = row["academic_year"]
    return item


def _metadata(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key in {"name", "academic_year", "active"}}


def _validate_year(kind: CatalogKind, payload: CatalogCreate | CatalogChange) -> None:
    if (kind != "terms" and "academic_year" in payload.model_fields_set) or (
        kind == "terms" and isinstance(payload, CatalogCreate) and payload.academic_year is None
    ):
        raise AppError("invalid_catalog_fields", "Оқу жылын тек оқу кезеңі үшін көрсетіңіз.", 422)


def _check_duplicate(
    connection: sqlite3.Connection,
    kind: CatalogKind,
    name: str,
    academic_year: str | None,
    active: bool,
    except_id: int | None = None,
) -> None:
    if not active:
        return
    table, id_column = CATALOGS[kind]
    for row in connection.execute(f"SELECT * FROM {table} WHERE active=1"):
        if row[id_column] == except_id or _key(row["name"]) != _key(name):
            continue
        if kind != "terms" or _key(row["academic_year"]) == _key(academic_year or ""):
            raise AppError("catalog_conflict", "Мұндай белсенді каталог жазбасы бар.", 409)


@router.get("")
def catalogs(
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Users.Manage"))],
) -> dict[str, list[dict[str, Any]]]:
    _require_admin(actor)
    store: Store = request.app.state.store
    with store.connect() as connection:
        connection.execute("BEGIN")
        _admin_current(connection, actor)
        result: dict[str, list[dict[str, Any]]] = {}
        for kind, (table, id_column) in CATALOGS.items():
            rows = connection.execute(f"SELECT * FROM {table}").fetchall()
            rows = sorted(rows, key=lambda row: (_key(row["name"]), row[id_column]))
            result[kind] = [_item(row, kind) for row in rows]
        return result


@router.post("/{kind}", status_code=201)
def create_catalog(
    kind: CatalogKind,
    payload: CatalogCreate,
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Users.Manage"))],
) -> dict[str, Any]:
    _require_admin(actor)
    _validate_year(kind, payload)
    store: Store = request.app.state.store
    table, id_column = CATALOGS[kind]
    with store.connect(write=True) as connection:
        _admin_current(connection, actor)
        _check_duplicate(connection, kind, payload.name, payload.academic_year, True)
        try:
            if kind == "terms":
                cursor = connection.execute(
                    "INSERT INTO Terms(name,academic_year) VALUES (?,?)",
                    (payload.name, payload.academic_year),
                )
            else:
                cursor = connection.execute(
                    f"INSERT INTO {table}(name) VALUES (?)", (payload.name,)
                )
        except sqlite3.IntegrityError as exc:
            # Keep legacy exact-name UNIQUE constraints, including archived rows.
            raise AppError("catalog_conflict", "Мұндай каталог жазбасы бар.", 409) from exc
        row = connection.execute(
            f"SELECT * FROM {table} WHERE {id_column}=?", (cursor.lastrowid,)
        ).fetchone()
        item = _item(row, kind)
        store.audit(
            connection,
            actor,
            "catalog_create",
            f"catalog_{kind}",
            str(item[id_column]),
            "success",
            "Оқу каталогына жаңа жазба қосылды.",
            client_ip(request),
            new_value=_metadata(item),
        )
        return {"item": item}


@router.patch("/{kind}/{item_id}")
def change_catalog(
    kind: CatalogKind,
    item_id: Annotated[int, Path(ge=1, le=2**63 - 1)],
    payload: CatalogChange,
    request: Request,
    actor: Annotated[Principal, Depends(require_permission("Users.Manage"))],
) -> dict[str, Any]:
    _require_admin(actor)
    _validate_year(kind, payload)
    store: Store = request.app.state.store
    table, id_column = CATALOGS[kind]
    with store.connect(write=True) as connection:
        _admin_current(connection, actor)
        row = connection.execute(
            f"SELECT * FROM {table} WHERE {id_column}=?", (item_id,)
        ).fetchone()
        if row is None:
            raise AppError("not_found", "Каталог жазбасы табылмады.", 404)
        old = _item(row, kind)
        new = old | payload.model_dump(exclude_unset=True)
        _check_duplicate(
            connection, kind, new["name"], new.get("academic_year"), new["active"], item_id
        )
        columns = [
            field
            for field in ("name", "academic_year", "active")
            if field in payload.model_fields_set
        ]
        try:
            connection.execute(
                f"UPDATE {table} SET "
                + ",".join(f"{column}=?" for column in columns)
                + f" WHERE {id_column}=?",
                [*(new[column] for column in columns), item_id],
            )
        except sqlite3.IntegrityError as exc:
            raise AppError("catalog_conflict", "Мұндай каталог жазбасы бар.", 409) from exc
        store.audit(
            connection,
            actor,
            "catalog_archive" if old["active"] and not new["active"] else "catalog_update",
            f"catalog_{kind}",
            str(item_id),
            "success",
            "Оқу каталогының жазбасы өзгертілді.",
            client_ip(request),
            old_value=_metadata(old),
            new_value=_metadata(new),
        )
        return {"item": new}
