"""Transactional catalogs, archive-safe references and additive legacy migration."""

import json
import secrets
import sqlite3
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.routing import NoMatchFound

from app.auth import current_principal, hash_password, principal_from_connection, verify_password
from app.bootstrap import create_admin
from app.catalogs import CATALOGS, router
from app.config import Settings
from app.main import create_app
from app.store import Store, iso_utc, utc_now
from tests.conftest import FakeEmbedder, FakeLLM
from tests.test_auth_system import add_user, sign_in


@pytest.fixture(scope="module")
def catalog_credentials():
    # Password strength is independently covered by auth tests. One real random
    # hash is sufficient here; DB RBAC, sessions, cookies and CSRF stay actual.
    password = secrets.token_urlsafe(24)
    encoded = hash_password(password)
    assert verify_password(password, encoded)
    return password, encoded


@pytest.fixture
def catalogs(tmp_path, monkeypatch, catalog_credentials):
    password, encoded = catalog_credentials

    def cached_hash(value):
        return encoded if value == password else hash_password(value)

    def cached_verify(value, stored):
        return True if value == password and stored == encoded else verify_password(value, stored)

    monkeypatch.setattr("app.bootstrap.hash_password", cached_hash)
    monkeypatch.setattr("app.auth_routes.hash_password", cached_hash)
    monkeypatch.setattr("app.auth_routes.verify_password", cached_verify)
    settings = Settings(
        _env_file=None, database_path=tmp_path / "catalog.sqlite3", upload_dir=tmp_path / "uploads"
    )
    store = Store(settings)
    admin = create_admin(store, "first_admin", "Catalog administrator", password)
    application = create_app(settings=settings, llm=FakeLLM(), embedder=FakeEmbedder())
    try:
        application.url_path_for("catalogs")
    except NoMatchFound:
        application.include_router(router)
    with TestClient(
        application, client=("127.0.0.1", 51000), raise_server_exceptions=False
    ) as client:
        sign_in(client, "first_admin", password)
        yield SimpleNamespace(client=client, store=store, password=password, admin=admin)


def create(catalogs, kind, name="New Catalog", **extra):
    return catalogs.client.post(f"/api/admin/catalogs/{kind}", json={"name": name, **extra})


def change(catalogs, kind, item_id, **changes):
    return catalogs.client.patch(f"/api/admin/catalogs/{kind}/{item_id}", json=changes)


@pytest.mark.parametrize("kind", ["groups", "subjects", "terms"])
def test_catalog_create_rename_archive_restore_and_safe_audit(catalogs, kind):
    extra = {"academic_year": "  2030–2031  "} if kind == "terms" else {}
    response = create(catalogs, kind, "  New\t  Catalog  ", **extra)
    assert response.status_code == 201, response.text
    item = response.json()["item"]
    _, id_column = CATALOGS[kind]
    item_id = item[id_column]
    assert item["name"] == "New Catalog" and item["active"] is True
    assert set(item) == {id_column, "name", "active"} | (
        {"academic_year"} if kind == "terms" else set()
    )
    if kind == "terms":
        assert item["academic_year"] == "2030–2031"
    renamed = change(catalogs, kind, item_id, name="Renamed Catalog")
    assert renamed.status_code == 200 and renamed.json()["item"]["name"] == "Renamed Catalog"
    archived = change(catalogs, kind, item_id, active=False)
    assert archived.status_code == 200 and archived.json()["item"]["active"] is False
    listing = catalogs.client.get("/api/admin/catalogs").json()
    assert next(row for row in listing[kind] if row[id_column] == item_id)["active"] is False
    restored = change(catalogs, kind, item_id, active=True)
    assert restored.status_code == 200 and restored.json()["item"]["active"] is True
    assert catalogs.client.delete(f"/api/admin/catalogs/{kind}/{item_id}").status_code == 405
    with catalogs.store.connect() as connection:
        audits = connection.execute(
            "SELECT * FROM AuditLogs WHERE object_type=? AND object_id=? ORDER BY audit_id",
            (f"catalog_{kind}", str(item_id)),
        ).fetchall()
    assert [row["action"] for row in audits] == [
        "catalog_create",
        "catalog_update",
        "catalog_archive",
        "catalog_update",
    ]
    for row in audits:
        assert row["user_id"] == "first_admin" and row["role"] == "admin"
        assert row["result"] == "success" and row["ip_address"] == "127.0.0.1"
        assert row["occurred_at"].endswith("Z")
        for field in ("old_value", "new_value"):
            if row[field] is not None:
                assert set(json.loads(row[field])) <= {"name", "academic_year", "active"}


def test_terms_require_year_and_allow_same_name_in_different_years(catalogs):
    assert create(catalogs, "terms", "Spring", academic_year="2030").status_code == 201
    assert create(catalogs, "terms", "spring", academic_year="2031").status_code == 201
    duplicate = create(catalogs, "terms", "  SPRING  ", academic_year="2030")
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "catalog_conflict"
    first = create(catalogs, "terms", "Autumn", academic_year="2031").json()["item"]
    conflict = change(catalogs, "terms", first["term_id"], name="SPRING")
    assert conflict.status_code == 409
    updated = change(catalogs, "terms", first["term_id"], academic_year="2032")
    assert updated.status_code == 200


@pytest.mark.parametrize("kind", ["groups", "subjects", "terms"])
def test_normalized_casefold_duplicates_and_reactivation_conflict(catalogs, kind):
    extra = {"academic_year": "2030"} if kind == "terms" else {}
    first = create(catalogs, kind, "Straße   Class", **extra).json()["item"]
    _, id_column = CATALOGS[kind]
    assert create(catalogs, kind, "STRASSE CLASS", **extra).status_code == 409
    assert change(catalogs, kind, first[id_column], active=False).status_code == 200
    second = create(catalogs, kind, "STRASSE CLASS", **extra)
    assert second.status_code == 201
    assert change(catalogs, kind, first[id_column], active=True).status_code == 409
    body = catalogs.client.get("/api/admin/catalogs").json()[kind]
    assert next(row for row in body if row[id_column] == first[id_column])["active"] is False


def test_exact_archived_name_legacy_unique_constraint_returns_clear_conflict(catalogs):
    item = create(catalogs, "groups", "Archived Exact").json()["item"]
    assert change(catalogs, "groups", item["group_id"], active=False).status_code == 200
    response = create(catalogs, "groups", "Archived Exact")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "catalog_conflict"


@pytest.mark.parametrize(
    "kind,payload",
    [
        ("groups", {}),
        ("groups", {"name": " "}),
        ("groups", {"name": "X"}),
        ("groups", {"name": "x" * 121}),
        ("groups", {"name": 23}),
        ("groups", {"name": "Unsafe\x00Name"}),
        ("groups", {"name": "Unsafe\u202eName"}),
        ("groups", {"name": "Valid", "academic_year": "2030"}),
        ("groups", {"name": "Valid", "active": True}),
        ("terms", {"name": "Valid"}),
        ("terms", {"name": "Valid", "academic_year": None}),
        ("terms", {"name": "Valid", "academic_year": "x"}),
        ("terms", {"name": "Valid", "academic_year": "x" * 33}),
        ("terms", {"name": "Valid", "academic_year": 2030}),
        ("unknown", {"name": "Valid"}),
    ],
)
def test_strict_create_fields(catalogs, kind, payload):
    response = catalogs.client.post(f"/api/admin/catalogs/{kind}", json=payload)
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"name": None},
        {"active": None},
        {"active": 0},
        {"active": "false"},
        {"academic_year": "2030"},
        {"group_id": 5},
        {"name": " "},
    ],
)
def test_strict_patch_fields(catalogs, payload):
    response = catalogs.client.patch("/api/admin/catalogs/groups/1", json=payload)
    assert response.status_code == 422


@pytest.mark.parametrize("item_id", [0, -1, 2**63, "abc"])
def test_bad_item_ids_are_422_and_missing_is_404(catalogs, item_id):
    assert change(catalogs, "groups", item_id, active=False).status_code == 422
    assert change(catalogs, "groups", 999999, active=False).status_code == 404


def test_sql_metacharacters_are_stored_as_literal_values(catalogs):
    name = "Group'); DROP TABLE Users; --"
    response = create(catalogs, "groups", name)
    assert response.status_code == 201 and response.json()["item"]["name"] == name
    with catalogs.store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM Users").fetchone()[0] == 1


@pytest.mark.parametrize("role", ["teacher", "head", "auditor", "student", "pending"])
def test_nonadmin_cannot_manage_even_with_users_permission(catalogs, role):
    password = secrets.token_urlsafe(20)
    add_user(catalogs.store, f"catalog_{role}", password, role=role, active=role != "pending")
    with catalogs.store.connect(write=True) as connection:
        if role == "pending":
            connection.execute(
                "UPDATE Users SET approval_status='pending' WHERE login=?", (f"catalog_{role}",)
            )
        connection.execute(
            "INSERT OR IGNORE INTO RolePermissions(role_code,permission_code) VALUES (?,?)",
            (role, "Users.Manage"),
        )
    with TestClient(catalogs.client.app, client=("127.0.0.1", 51002)) as client:
        sign_in(client, f"catalog_{role}", password)
        assert client.get("/api/admin/catalogs").status_code == 403
        assert (
            client.post("/api/admin/catalogs/groups", json={"name": "Blocked"}).status_code == 403
        )
        assert (
            client.patch("/api/admin/catalogs/groups/1", json={"active": False}).status_code == 403
        )


@pytest.mark.parametrize(
    "statement,values",
    [
        ("UPDATE Users SET role_code='teacher' WHERE user_id=?", "user"),
        ("UPDATE Users SET active=0 WHERE user_id=?", "user"),
        ("UPDATE Users SET approval_status='pending' WHERE user_id=?", "user"),
        ("UPDATE Sessions SET status='blocked' WHERE session_id=?", "session"),
        ("UPDATE Sessions SET status='revoked' WHERE session_id=?", "session"),
        (
            "UPDATE Sessions SET expires_at='2000-01-01T00:00:00.000000Z' WHERE session_id=?",
            "session",
        ),
        ("DELETE FROM RolePermissions WHERE role_code='admin' AND permission_code=?", "permission"),
    ],
)
def test_transaction_rechecks_current_authority_after_dependency_was_resolved(
    catalogs, statement, values
):
    with catalogs.store.connect() as connection:
        user = connection.execute("SELECT * FROM Users WHERE login='first_admin'").fetchone()
        session = connection.execute(
            "SELECT session_id FROM Sessions WHERE user_id=?", (user["user_id"],)
        ).fetchone()[0]
        actor = principal_from_connection(connection, user, session)
    catalogs.client.app.dependency_overrides[current_principal] = lambda: actor
    parameter = {"user": actor.user_id, "session": session, "permission": "Users.Manage"}[values]
    with catalogs.store.connect(write=True) as connection:
        connection.execute(statement, (parameter,))
    assert catalogs.client.get("/api/admin/catalogs").status_code == 403
    assert create(catalogs, "groups", "Must Not Exist").status_code == 403
    assert change(catalogs, "groups", 1, active=False).status_code == 403
    with catalogs.store.connect() as connection:
        assert (
            connection.execute("SELECT 1 FROM Groups WHERE name='Must Not Exist'").fetchone()
            is None
        )
        assert connection.execute("SELECT active FROM Groups WHERE group_id=1").fetchone()[0] == 1


def test_unauthenticated_and_csrf_protection(catalogs):
    with TestClient(catalogs.client.app, client=("127.0.0.1", 51003)) as anonymous:
        assert anonymous.get("/api/admin/catalogs").status_code == 401
        assert (
            anonymous.post("/api/admin/catalogs/groups", json={"name": "Blocked"}).status_code
            == 401
        )
    catalogs.client.headers.pop("X-CSRF-Token")
    assert create(catalogs, "groups", "Blocked").status_code == 403


@pytest.mark.parametrize("operation", ["create", "update", "archive"])
def test_failed_audit_rolls_back_catalog_mutation(catalogs, operation):
    with catalogs.store.connect(write=True) as connection:
        before = dict(connection.execute("SELECT * FROM Groups WHERE group_id=1").fetchone())
        connection.execute(
            "CREATE TRIGGER synthetic_catalog_audit_failure BEFORE INSERT ON AuditLogs "
            "WHEN NEW.action LIKE 'catalog_%' BEGIN SELECT RAISE(ABORT,'synthetic failure'); END"
        )
    if operation == "create":
        response = create(catalogs, "groups", "Audit Rollback")
    else:
        response = change(
            catalogs,
            "groups",
            1,
            **({"name": "Changed"} if operation == "update" else {"active": False}),
        )
    assert response.status_code >= 500
    with catalogs.store.connect() as connection:
        assert (
            dict(connection.execute("SELECT * FROM Groups WHERE group_id=1").fetchone()) == before
        )
        assert (
            connection.execute("SELECT 1 FROM Groups WHERE name='Audit Rollback'").fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM AuditLogs WHERE action LIKE 'catalog_%'"
            ).fetchone()[0]
            == 0
        )


def test_archive_preserves_assignments_grades_tasks_and_dynamic_view(catalogs):
    teacher_id = add_user(catalogs.store, "catalog_archive_teacher", secrets.token_urlsafe(20))
    with catalogs.store.connect(write=True) as connection:
        connection.execute(
            "INSERT INTO TeachingAssignments(user_id,subject_id,group_id,term_id) VALUES (?,1,1,1)",
            (teacher_id,),
        )
        connection.execute(
            "INSERT INTO Tasks(title,description,subject_id,group_id,term_id,published_at,deadline,"
            "created_by,created_at,updated_at) VALUES ('History','Preserve',1,1,1,?,?,?,?,?)",
            (iso_utc(), iso_utc(utc_now() + timedelta(days=1)), teacher_id, iso_utc(), iso_utc()),
        )
        counts = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("Students", "Grades", "Tasks", "TeachingAssignments")
        )
    for kind in ("groups", "subjects", "terms"):
        assert change(catalogs, kind, 1, active=False).status_code == 200
        with catalogs.store.connect() as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM ActiveTeachingAssignments").fetchone()[0]
                == 0
            )
            assert (
                tuple(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("Students", "Grades", "Tasks", "TeachingAssignments")
                )
                == counts
            )
        assert change(catalogs, kind, 1, active=True).status_code == 200
        with catalogs.store.connect() as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM ActiveTeachingAssignments").fetchone()[0]
                == 1
            )


def legacy_without_catalog_extension(store):
    with store.connect(write=True) as connection:
        connection.execute("DROP VIEW ActiveTeachingAssignments")
        for table in ("Groups", "Subjects", "Terms"):
            connection.execute(f"ALTER TABLE {table} DROP COLUMN active")
        connection.execute("ALTER TABLE Users DROP COLUMN approved_at")


@pytest.mark.parametrize("version", [2, 3])
def test_additive_migration_preserves_legacy_columns_accounts_and_only_real_approval_dates(
    tmp_path, version
):
    store = Store(Settings(_env_file=None, database_path=tmp_path / "legacy.sqlite3"))
    store.initialize()
    store.seed_academics()
    with store.connect(write=True) as connection:
        connection.execute("ALTER TABLE Groups ADD COLUMN custom_note TEXT")
        connection.execute(
            "UPDATE Groups SET name='Custom Group',custom_note='keep' WHERE group_id=1"
        )
        connection.executemany(
            "INSERT INTO Users(user_id,login,display_name,password_hash,role_code,active,"
            "created_at,"
            "approval_status) VALUES (?,?,?,'old-hash',?,?,?,?)",
            [
                (
                    i,
                    f"legacy_{i}",
                    f"Legacy {i}",
                    "pending" if i == 3 else "teacher",
                    0 if i == 3 else 1,
                    "2025-01-01T00:00:00.000000Z",
                    "pending" if i == 3 else "approved",
                )
                for i in range(1, 4)
            ],
        )
        for target, occurred_at, action, result, object_type in (
            (1, "2025-02-01T01:00:00.000000Z", "user_approve", "success", "user"),
            (1, "2025-03-01T01:00:00.000000Z", "user_approve", "success", "user"),
            (1, "2025-04-01T01:00:00.000000Z", "user_approve", "failure", "user"),
            (2, "2025-03-01T01:00:00.000000Z", "admin_bootstrap", "success", "user"),
            (2, "2025-03-01T01:00:00.000000Z", "user_approve", "success", "session"),
            (3, "2025-03-01T01:00:00.000000Z", "user_approve", "success", "user"),
        ):
            connection.execute(
                "INSERT INTO AuditLogs(occurred_at,user_id,role,action,object_type,object_id,"
                "result,reason) VALUES (?,'legacy_admin','admin',?,?,?,?, 'Real legacy event')",
                (occurred_at, action, object_type, str(target), result),
            )
        snapshots = {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in ("Users", "Students", "Grades", "AuditLogs", "Groups", "Subjects", "Terms")
        }
    legacy_without_catalog_extension(store)
    with store.connect(write=True) as connection:
        connection.execute(f"PRAGMA user_version={version}")
    store.initialize()
    store.initialize()
    with store.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        users = connection.execute("SELECT * FROM Users ORDER BY user_id").fetchall()
        assert [row["approved_at"] for row in users] == ["2025-03-01T01:00:00.000000Z", None, None]
        for table, before in snapshots.items():
            rows = [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
            assert len(rows) == len(before)
            preserved = (
                set(before[0]) - {"approved_at", "requested_role", "approval_reason"}
                if before
                else set()
            )
            for old, new in zip(before, rows, strict=True):
                assert {key: new[key] for key in preserved} == {key: old[key] for key in preserved}
        assert (
            connection.execute("SELECT custom_note FROM Groups WHERE group_id=1").fetchone()[0]
            == "keep"
        )


def test_seed_and_reinitialization_never_reactivate_rename_or_fabricate_approval_dates(tmp_path):
    store = Store(Settings(_env_file=None, database_path=tmp_path / "seed.sqlite3"))
    store.initialize()
    store.seed_academics()
    with store.connect(write=True) as connection:
        connection.execute("UPDATE Groups SET name='Edited Group',active=0 WHERE group_id=1")
        connection.execute("UPDATE Subjects SET name='Edited Subject',active=0 WHERE subject_id=1")
        connection.execute(
            "UPDATE Terms SET name='Edited Term',academic_year='Custom Year',active=0 "
            "WHERE term_id=1"
        )
        connection.execute("UPDATE Students SET display_name='Edited Student' WHERE student_id=1")
        connection.execute(
            "INSERT INTO Groups(group_id,name,active) VALUES (99,'Custom archived',0)"
        )
        connection.execute(
            "INSERT INTO Users(user_id,login,display_name,password_hash,role_code,active,"
            "created_at) "
            "VALUES (1,'imported_teacher','Imported','old-hash','teacher',1,?)",
            (iso_utc(),),
        )
        store.audit(
            connection, None, "user_approve", "user", "1", "success", "Later event", "unknown"
        )
        before = {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in ("Groups", "Subjects", "Terms", "Students", "Grades", "Users", "AuditLogs")
        }
    for _ in range(2):
        store.initialize()
        store.seed_academics()
    with store.connect() as connection:
        for table, expected in before.items():
            assert [dict(row) for row in connection.execute(f"SELECT * FROM {table}")] == expected
        assert connection.execute("SELECT approved_at FROM Users").fetchone()[0] is None
        assert connection.execute("SELECT COUNT(*) FROM Students").fetchone()[0] == 12


@pytest.mark.parametrize("table", ["Groups", "Subjects", "Terms"])
def test_archive_column_database_constraints_and_no_seed_accounts(tmp_path, table):
    store = Store(Settings(_env_file=None, database_path=tmp_path / "constraints.sqlite3"))
    store.initialize()
    store.seed_academics()
    for value in (None, -1, 2):
        with pytest.raises(sqlite3.IntegrityError), store.connect(write=True) as connection:
            connection.execute(f"UPDATE {table} SET active=?", (value,))
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM Users").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    "assignment",
    ["active=0", "approval_status='pending'", "role_code='head'"],
)
def test_active_assignment_view_tracks_current_teacher_approval_and_role(tmp_path, assignment):
    store = Store(Settings(_env_file=None, database_path=tmp_path / "view.sqlite3"))
    store.initialize()
    store.seed_academics()
    with store.connect(write=True) as connection:
        connection.execute(
            "INSERT INTO Users(user_id,login,display_name,password_hash,role_code,active,"
            "created_at) VALUES (1,'view_teacher','Teacher','unusable','teacher',1,?)",
            (iso_utc(),),
        )
        connection.execute(
            "INSERT INTO TeachingAssignments(user_id,subject_id,group_id,term_id) VALUES (1,1,1,1)"
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM ActiveTeachingAssignments").fetchone()[0] == 1
        )
        connection.execute("UPDATE Users SET " + assignment + " WHERE user_id=1")
        assert (
            connection.execute("SELECT COUNT(*) FROM ActiveTeachingAssignments").fetchone()[0] == 0
        )
        assert connection.execute("SELECT COUNT(*) FROM TeachingAssignments").fetchone()[0] == 1


def test_catalog_additions_and_approval_backfill_roll_back_together_on_migration_error(tmp_path):
    store = Store(Settings(_env_file=None, database_path=tmp_path / "rollback.sqlite3"))
    store.initialize()
    store.seed_academics()
    with store.connect(write=True) as connection:
        connection.execute(
            "INSERT INTO Users(user_id,login,display_name,password_hash,role_code,active,"
            "created_at) VALUES (1,'migration_teacher','Teacher','unusable','teacher',1,?)",
            (iso_utc(),),
        )
        store.audit(
            connection, None, "user_approve", "user", "1", "success", "Existing event", "unknown"
        )
    legacy_without_catalog_extension(store)
    with store.connect(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER synthetic_migration_failure BEFORE UPDATE ON Users "
            "BEGIN SELECT RAISE(ABORT,'synthetic migration error'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.initialize()
    with store.connect() as connection:
        assert "approved_at" not in {
            row[1] for row in connection.execute("PRAGMA table_info(Users)")
        }
        for table in ("Groups", "Subjects", "Terms"):
            assert "active" not in {
                row[1] for row in connection.execute(f"PRAGMA table_info({table})")
            }
        assert connection.execute("SELECT COUNT(*) FROM Users").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM Students").fetchone()[0] == 12
