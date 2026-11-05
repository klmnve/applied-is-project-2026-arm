"""Official roster scope, literal Unicode search and live database authorization."""

import secrets
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.auth import current_principal, principal_from_connection
from app.config import Settings
from app.errors import AppError
from app.store import ROLES, Store, iso_utc
from app.students import router
from tests.test_auth_system import add_user, sign_in
from tests.test_auth_system import auth_system as auth_system


@pytest.fixture
def roster(tmp_path):
    store = Store(Settings(database_path=tmp_path / "roster.sqlite3"))
    store.initialize()
    store.seed_academics()
    accounts = [
        (1, "admin", 1, "approved"),
        (2, "head", 1, "approved"),
        (3, "teacher", 1, "approved"),
        (4, "teacher", 1, "approved"),
        (5, "student", 1, "approved"),
        (6, "auditor", 1, "approved"),
        (7, "pending", 0, "pending"),
        (8, "admin", 0, "approved"),
        (9, "head", 1, "pending"),
    ]
    principals = {}
    with store.connect(write=True) as connection:
        for user_id, role, active, approval in accounts:
            connection.execute(
                "INSERT INTO Users(user_id,login,display_name,password_hash,role_code,"
                "active,created_at,approval_status,requested_role) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    user_id,
                    f"roster_user_{user_id}",
                    f"Roster account {user_id}",
                    "unit-test-hash-unused",
                    role,
                    active,
                    iso_utc(),
                    approval,
                    "student" if user_id == 7 else "teacher",
                ),
            )
            user = connection.execute("SELECT * FROM Users WHERE user_id=?", (user_id,)).fetchone()
            principals[user_id] = principal_from_connection(connection, user, "roster-session")
        connection.executemany(
            "INSERT INTO TeachingAssignments(user_id,subject_id,group_id,term_id) VALUES (?,?,?,?)",
            [(3, 1, 1, 1), (3, 2, 1, 1), (3, 3, 3, 2)],
        )
    selected = {"actor": principals[1]}
    application = FastAPI()
    application.state.store = store
    application.include_router(router)
    application.dependency_overrides[current_principal] = lambda: selected["actor"]

    @application.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError):
        return JSONResponse({"error": {"code": exc.code}}, status_code=exc.status_code)

    with TestClient(application) as client:
        yield SimpleNamespace(client=client, store=store, selected=selected, principals=principals)


def use(roster, user_id):
    roster.selected["actor"] = roster.principals[user_id]


@pytest.mark.parametrize("user_id", [1, 2])
def test_admin_and_head_see_only_official_student_dto(roster, user_id):
    use(roster, user_id)
    response = roster.client.get("/api/students")
    assert response.status_code == 200
    body = response.json()
    assert (body["total"], body["page"], body["page_size"]) == (12, 1, 25)
    assert len(body["items"]) == 12
    assert {group["group_id"] for group in body["groups"]} == {1, 2, 3}
    assert all(set(group) == {"group_id", "name"} for group in body["groups"])
    assert all(
        set(item) == {"student_id", "display_name", "student_code", "group_id", "group_name"}
        for item in body["items"]
    )


def test_teacher_scope_is_distinct_groups_and_intersects_search(roster):
    use(roster, 3)
    body = roster.client.get("/api/students").json()
    assert body["total"] == 8
    assert {item["group_id"] for item in body["items"]} == {1, 3}
    assert [group["group_id"] for group in body["groups"]] == [1, 3]
    scoped = roster.client.get("/api/students", params={"group_id": 3, "q": "SYN-009"})
    assert scoped.status_code == 200
    assert [item["student_id"] for item in scoped.json()["items"]] == [9]
    hidden = roster.client.get("/api/students", params={"q": "SYN-005"}).json()
    assert hidden["items"] == [] and hidden["total"] == 0
    assert roster.client.get("/api/students", params={"group_id": 2}).status_code == 403


def test_teacher_without_assignments_has_empty_choices_and_no_students(roster):
    use(roster, 4)
    body = roster.client.get("/api/students").json()
    assert body == {"items": [], "groups": [], "total": 0, "page": 1, "page_size": 25}
    assert roster.client.get("/api/students", params={"group_id": 1}).status_code == 403


@pytest.mark.parametrize("user_id", [5, 6, 7, 8, 9])
def test_forbidden_roles_inactive_and_unapproved_cannot_use_matrix_grants(roster, user_id):
    use(roster, user_id)
    role = roster.principals[user_id].role_code
    with roster.store.connect(write=True) as connection:
        connection.executemany(
            "INSERT OR IGNORE INTO RolePermissions(role_code,permission_code) VALUES (?,?)",
            [(role, "Grades.View"), (role, "Users.Manage")],
        )
    assert roster.client.get("/api/students").status_code == 403


def test_staff_permission_intersection_is_live_and_never_expands_teacher_scope(roster):
    use(roster, 2)
    with roster.store.connect(write=True) as connection:
        connection.execute(
            "DELETE FROM RolePermissions WHERE role_code='head' "
            "AND permission_code IN ('Grades.View','Users.Manage')"
        )
    assert roster.client.get("/api/students").status_code == 403
    with roster.store.connect(write=True) as connection:
        connection.execute("INSERT INTO RolePermissions VALUES ('head','Users.Manage')")
    assert roster.client.get("/api/students").json()["total"] == 12
    use(roster, 3)
    with roster.store.connect(write=True) as connection:
        connection.execute(
            "DELETE FROM RolePermissions WHERE role_code='teacher' "
            "AND permission_code='Grades.View'"
        )
        connection.execute("INSERT INTO RolePermissions VALUES ('teacher','Users.Manage')")
    assert roster.client.get("/api/students").json()["total"] == 8


@pytest.mark.parametrize(
    "statement, values",
    [
        ("UPDATE Users SET active=0 WHERE user_id=?", (2,)),
        ("UPDATE Users SET approval_status='rejected' WHERE user_id=?", (2,)),
        ("UPDATE Users SET role_code='auditor' WHERE user_id=?", (2,)),
        ("DELETE FROM Users WHERE user_id=?", (2,)),
    ],
)
def test_authority_is_read_from_database_instead_of_cached_principal(roster, statement, values):
    use(roster, 2)
    assert roster.client.get("/api/students").status_code == 200
    with roster.store.connect(write=True) as connection:
        connection.execute(statement, values)
    assert roster.client.get("/api/students").status_code == 403


def test_unicode_name_and_code_search_normalize_whitespace_and_intersect_group(roster):
    query = "  МӘДИНА\tСӘРСЕНОВА\n "
    body = roster.client.get("/api/students", params={"q": query, "group_id": 1}).json()
    assert [item["student_id"] for item in body["items"]] == [2]
    other = roster.client.get("/api/students", params={"q": query, "group_id": 2}).json()
    assert other["total"] == 0 and other["items"] == []
    code = roster.client.get("/api/students", params={"q": "syn-001"}).json()
    assert [item["student_id"] for item in code["items"]] == [1]
    with roster.store.connect(write=True) as connection:
        connection.executemany(
            "INSERT INTO Students(student_id,display_name,student_code,group_id) VALUES (?,?,?,?)",
            [(100, "Straße User", "UNICODE-SS", 2), (101, "  Ａｄａ   Lovelace  ", "COMPAT-1", 2)],
        )
    folded = roster.client.get("/api/students", params={"q": "STRASSE"}).json()
    assert [item["student_id"] for item in folded["items"]] == [100]
    compatible = roster.client.get("/api/students", params={"q": "ada lovelace"}).json()
    assert compatible["items"][0]["display_name"] == "  Ａｄａ   Lovelace  "


def test_search_treats_sql_and_wildcard_text_literally(roster):
    with roster.store.connect(write=True) as connection:
        connection.execute("INSERT INTO Students VALUES (100,1,'A_Real%Name','REAL_X%07')")
    for query in ("_", "%", "real_x%"):
        body = roster.client.get("/api/students", params={"q": query}).json()
        assert [item["student_id"] for item in body["items"]] == [100]
    attack = roster.client.get("/api/students", params={"q": "%' OR 1=1 --"}).json()
    assert attack["total"] == 0 and attack["items"] == []
    assert roster.client.get("/api/students").json()["total"] == 13


def test_pending_claims_are_not_official_students_or_searchable(roster):
    with roster.store.connect(write=True) as connection:
        connection.execute(
            "INSERT INTO StudentClaims(user_id,student_claim_name,student_claim_number,"
            "student_claim_group_id,student_claim_email) VALUES (7,?,?,1,?)",
            ("Unverified Applicant", "PRIVATE-9999", "private@example.invalid"),
        )
    body = roster.client.get("/api/students").json()
    assert body["total"] == 12
    for query in ("Unverified Applicant", "PRIVATE-9999", "private@example.invalid"):
        assert roster.client.get("/api/students", params={"q": query}).json()["total"] == 0
    assert "PRIVATE-9999" not in str(body)


def test_pagination_is_stable_and_covers_duplicate_names_once(roster):
    with roster.store.connect(write=True) as connection:
        connection.executemany(
            "INSERT INTO Students(student_id,display_name,student_code,group_id) VALUES (?,?,?,1)",
            [(100 + number, "Same Name", f"PAGE-{number:03}") for number in range(1, 42)],
        )
    ids = []
    for page in range(1, 6):
        body = roster.client.get(
            "/api/students", params={"q": "Same Name", "group_id": 1, "page_size": 10, "page": page}
        ).json()
        assert (body["total"], body["page"], body["page_size"]) == (41, page, 10)
        ids.extend(item["student_id"] for item in body["items"])
    assert ids == list(range(101, 142))
    for page in (6, 2**100):
        response = roster.client.get(
            "/api/students", params={"q": "Same Name", "page_size": 10, "page": page}
        )
        assert response.status_code == 200
        assert response.json()["total"] == 41 and response.json()["items"] == []


@pytest.mark.parametrize(
    "params",
    [
        {"group_id": 0},
        {"group_id": -1},
        {"group_id": "wrong"},
        {"group_id": "1.5"},
        {"group_id": 2**63},
        {"page": 0},
        {"page": -1},
        {"page_size": 0},
        {"page_size": -1},
        {"page_size": 101},
        {"q": "x" * 121},
    ],
)
def test_invalid_query_parameters_are_422(roster, params):
    assert roster.client.get("/api/students", params=params).status_code == 422


def test_search_bound_applies_to_normalized_text_and_unknown_group_is_clear(roster):
    response = roster.client.get("/api/students", params={"q": " " * 150 + "x" * 120 + "\t"})
    assert response.status_code == 200
    unknown = roster.client.get("/api/students", params={"group_id": 99999})
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "invalid_student_group"


def test_empty_existing_group_and_empty_catalog_are_valid(roster):
    with roster.store.connect(write=True) as connection:
        connection.execute("INSERT INTO Groups(group_id,name) VALUES (77,'Empty Group')")
    body = roster.client.get("/api/students", params={"group_id": 77}).json()
    assert body["items"] == [] and body["total"] == 0
    assert any(group["group_id"] == 77 for group in body["groups"])
    use(roster, 3)
    assert roster.client.get("/api/students", params={"group_id": 77}).status_code == 403
    use(roster, 1)
    with roster.store.connect(write=True) as connection:
        connection.execute("DELETE FROM TeachingAssignments")
        connection.execute("DELETE FROM Grades")
        connection.execute("DELETE FROM Students")
        connection.execute("DELETE FROM Groups")
    empty = roster.client.get("/api/students")
    assert empty.status_code == 200
    assert empty.json() == {"items": [], "groups": [], "total": 0, "page": 1, "page_size": 25}


def test_full_app_cookie_auth_permission_revocation_and_account_deactivation(auth_system):
    admin_client, store, password, _ = auth_system
    assert admin_client.get("/api/students").status_code == 401
    assert (
        admin_client.get("/api/students", headers={"X-Demo-User": "demo_admin"}).status_code == 401
    )
    sign_in(admin_client, "first_admin", password)
    assert admin_client.get("/api/students").json()["total"] == 12
    teacher_password = secrets.token_urlsafe(20)
    teacher_id = add_user(store, "roster_live_teacher", teacher_password)
    with store.connect(write=True) as connection:
        connection.execute(
            "INSERT INTO TeachingAssignments(user_id,subject_id,group_id,term_id) VALUES (?,1,1,1)",
            (teacher_id,),
        )
    with TestClient(admin_client.app, client=("127.0.0.1", 51001)) as teacher_client:
        sign_in(teacher_client, "roster_live_teacher", teacher_password)
        assert teacher_client.get("/api/students").json()["total"] == 4
        permissions = sorted(ROLES["teacher"][1] - {"Grades.View"})
        revoked = admin_client.put(
            "/api/admin/roles/teacher/permissions", json={"permissions": permissions}
        )
        assert revoked.status_code == 200, revoked.text
        assert teacher_client.get("/api/students").status_code == 403
        restored = admin_client.put(
            "/api/admin/roles/teacher/permissions",
            json={"permissions": [*permissions, "Grades.View"]},
        )
        assert restored.status_code == 200
        assert teacher_client.get("/api/students").status_code == 200
        deactivated = admin_client.patch(f"/api/admin/users/{teacher_id}", json={"active": False})
        assert deactivated.status_code == 200, deactivated.text
        assert teacher_client.get("/api/students").status_code == 401
