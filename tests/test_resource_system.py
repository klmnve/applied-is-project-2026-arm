"""SQLModel relationships, authenticated CRUD, validation and atomic audit."""

import json
import secrets
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.auth import hash_password, verify_password
from app.bootstrap import create_admin
from app.config import Settings
from app.database.models import DepartmentResource, ResourceCategory, ResourceRoom
from app.database.seed import seed_resources
from app.main import create_app
from app.store import Store, iso_utc
from tests.conftest import FakeEmbedder, FakeLLM
from tests.test_auth_system import sign_in


@pytest.fixture(scope="module")
def resource_credentials():
    password = secrets.token_urlsafe(24)
    return password, hash_password(password)


@pytest.fixture
def resources(tmp_path, monkeypatch, resource_credentials):
    password, encoded = resource_credentials
    monkeypatch.setattr("app.bootstrap.hash_password", lambda value: encoded)
    monkeypatch.setattr(
        "app.auth_routes.verify_password",
        lambda value, stored: (
            True if value == password and stored == encoded else verify_password(value, stored)
        ),
    )
    settings = Settings(
        _env_file=None, database_path=tmp_path / "arm.sqlite3", upload_dir=tmp_path / "uploads"
    )
    store = Store(settings)
    create_admin(store, "first_admin", "Resource administrator", password)
    app = create_app(settings=settings, llm=FakeLLM(), embedder=FakeEmbedder())
    with TestClient(app, client=("127.0.0.1", 51000), raise_server_exceptions=False) as client:
        sign_in(client, "first_admin", password)
        yield SimpleNamespace(
            client=client,
            store=app.state.store,
            engine=app.state.orm_engine,
            password=password,
            encoded=encoded,
        )


def payload(**changes):
    return {
        "name": "  Test equipment  ",
        "inventory_code": "TEST-001",
        "category_id": 1,
        "room_id": 1,
        "quantity": 3,
        "unit_cost": 12.5,
    } | changes


def test_crud_persists_with_actor_and_atomic_audit(resources):
    client = resources.client
    response = client.post("/api/resources", json=payload())
    assert response.status_code == 201, response.text
    item = response.json()["item"]
    item_id = item["id"]
    assert item["name"] == "Test equipment"
    assert item["category_name"] and item["room_name"]
    update = client.patch(f"/api/resources/{item_id}", json={"quantity": 0, "room_id": 2})
    assert update.status_code == 200, update.text
    assert update.json()["item"]["room_name"] != item["room_name"]
    assert client.get(f"/api/resources/{item_id}").json()["item"]["quantity"] == 0
    deleted = client.delete(f"/api/resources/{item_id}")
    assert deleted.status_code == 204 and deleted.content == b""
    assert client.get(f"/api/resources/{item_id}").status_code == 404
    assert client.delete(f"/api/resources/{item_id}").status_code == 404
    with resources.store.connect() as connection:
        assert (
            connection.execute("SELECT 1 FROM departmentresource WHERE id=?", (item_id,)).fetchone()
            is None
        )
        rows = connection.execute(
            "SELECT * FROM AuditLogs WHERE object_type='department_resource' ORDER BY audit_id"
        ).fetchall()
    assert [row["action"] for row in rows] == [
        "resource_create",
        "resource_update",
        "resource_delete",
    ]
    assert all(row["user_id"] == "first_admin" and row["session_id"] for row in rows)
    assert json.loads(rows[1]["old_value"])["quantity"] == 3
    assert json.loads(rows[1]["new_value"])["quantity"] == 0


def test_relationships_one_time_seed_and_existing_data_preservation(resources):
    with resources.store.connect() as connection:
        before = {
            name: [tuple(row) for row in connection.execute(f"SELECT * FROM {name}")]
            for name in ("Users", "AuditLogs", "Grades", "Students")
        }
    with Session(resources.engine) as session:
        category = session.get(ResourceCategory, 1)
        room = session.get(ResourceRoom, 1)
        assert category.resources and room.resources
        resource = category.resources[0]
        assert resource.category.id == category.id
        resource_id = resource.id
        session.delete(resource)
        session.commit()
    assert seed_resources(resources.engine) == {"categories": 0, "rooms": 0, "resources": 0}
    with Session(resources.engine) as session:
        assert session.get(DepartmentResource, resource_id) is None
        assert len(session.exec(select(ResourceCategory)).all()) == 3
        assert len(session.exec(select(ResourceRoom)).all()) == 3
        assert len(session.exec(select(DepartmentResource)).all()) == 3
    with resources.store.connect() as connection:
        for name, rows in before.items():
            assert [tuple(row) for row in connection.execute(f"SELECT * FROM {name}")] == rows
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("operation", ["create", "update", "delete"])
def test_business_change_rolls_back_when_audit_fails(resources, monkeypatch, operation):
    client = resources.client
    created = client.post("/api/resources", json=payload()).json()["item"]
    item_id = created["id"]

    def fail_audit(*args, **kwargs):
        raise sqlite3.OperationalError("Simulated audit failure")

    monkeypatch.setattr(resources.store, "audit", fail_audit)
    if operation == "create":
        response = client.post("/api/resources", json=payload(inventory_code="ROLLBACK-001"))
    elif operation == "update":
        response = client.patch(f"/api/resources/{item_id}", json={"quantity": 99})
    else:
        response = client.delete(f"/api/resources/{item_id}")
    assert response.status_code == 500
    with Session(resources.engine) as session:
        assert session.get(DepartmentResource, item_id).quantity == 3
        assert (
            session.exec(
                select(DepartmentResource).where(
                    DepartmentResource.inventory_code == "ROLLBACK-001"
                )
            ).first()
            is None
        )


@pytest.mark.parametrize(
    "change",
    [
        {"id": 999},
        {"created_at": "2026-01-01"},
        {"name": " "},
        {"name": None},
        {"inventory_code": ""},
        {"quantity": -1},
        {"quantity": 1.5},
        {"quantity": True},
        {"unit_cost": -0.01},
        {"unit_cost": "12"},
        {"category_id": 999},
        {"room_id": 999},
    ],
)
def test_invalid_create_is_rejected_without_side_effects(resources, change):
    response = resources.client.post("/api/resources", json=payload(**change))
    assert response.status_code == 422, response.text
    assert resources.client.get("/api/resources").json()["total"] == 4


@pytest.mark.parametrize("change", [{}, {"id": 1}, {"name": None}, {"quantity": None}])
def test_invalid_partial_update(resources, change):
    assert resources.client.patch("/api/resources/1", json=change).status_code == 422


def test_duplicate_missing_foreign_keys_and_missing_ids(resources):
    client = resources.client
    assert client.post("/api/resources", json=payload()).status_code == 201
    assert client.post("/api/resources", json=payload()).status_code == 409
    assert client.patch("/api/resources/1", json={"inventory_code": "TEST-001"}).status_code == 409
    assert client.patch("/api/resources/1", json={"category_id": 999}).status_code == 422
    assert client.patch("/api/resources/999", json={"quantity": 2}).status_code == 404


def test_search_combines_filters_pagination_and_escapes_wildcards(resources):
    client = resources.client
    for code, cost in (("FILTER-A", 10), ("FILTER-B", 20), ("FILTER-C", 30)):
        assert (
            client.post(
                "/api/resources",
                json=payload(name="Қазақша монитор", inventory_code=code, unit_cost=cost),
            ).status_code
            == 201
        )
    result = client.get(
        "/api/resources",
        params={
            "search": "ҚАЗАҚША",
            "category_id": 1,
            "room_id": 1,
            "min_cost": 10,
            "max_cost": 20,
            "page_size": 1,
            "page": 2,
        },
    ).json()
    assert result["total"] == 2 and result["pages"] == 2
    assert result["items"][0]["inventory_code"] == "FILTER-B"
    assert client.get("/api/resources", params={"search": "%"}).json()["total"] == 0
    assert client.get("/api/resources", params={"search": "' OR 1=1 --"}).json()["total"] == 0
    assert client.get("/api/resources", params={"min_cost": 20, "max_cost": 10}).status_code == 422
    assert client.get("/api/resources", params={"page_size": 101}).status_code == 422
    assert len(client.get("/api/resource-categories").json()["items"]) == 3
    assert len(client.get("/api/resource-rooms").json()["items"]) == 3


def test_authentication_csrf_and_actual_admin_required(resources):
    client = resources.client
    csrf = client.headers.pop("X-CSRF-Token")
    assert client.post("/api/resources", json=payload()).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    client.cookies.clear()
    assert client.get("/api/resources").status_code == 401
    with resources.store.connect(write=True) as connection:
        connection.execute(
            "INSERT INTO Users(login,display_name,password_hash,role_code,active,created_at) "
            "VALUES (?,?,?,?,?,?)",
            ("resource_teacher", "Teacher", resources.encoded, "teacher", 1, iso_utc()),
        )
        # Even an accidentally granted management permission cannot turn a teacher into admin.
        connection.execute(
            "INSERT INTO RolePermissions(role_code,permission_code) "
            "VALUES ('teacher','Users.Manage')"
        )
    sign_in(client, "resource_teacher", resources.password)
    assert client.get("/api/resources").status_code == 200
    assert client.post("/api/resources", json=payload()).status_code == 403
    assert client.patch("/api/resources/1", json={"quantity": 1}).status_code == 403
    assert client.delete("/api/resources/1").status_code == 403
