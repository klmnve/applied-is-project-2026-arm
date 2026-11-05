"""Approval state is independent from credentials, role grants and student identity."""

import json
import secrets
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.auth import CSRF_COOKIE, SESSION_COOKIE, hash_password
from app.config import Settings
from app.store import FICTIONAL_STUDENTS, Store
from tests.test_auth_system import add_user, sign_in
from tests.test_auth_system import auth_system as auth_system


def register(client, store, login="applicant", requested_role="teacher", password=None):
    password = password or secrets.token_urlsafe(20)
    claim = {}
    if requested_role == "student":
        with store.connect() as connection:
            index = connection.execute(
                "SELECT COUNT(*) FROM Users WHERE requested_role='student'"
            ).fetchone()[0]
            student = connection.execute(
                "SELECT * FROM Students ORDER BY student_id LIMIT 1 OFFSET ?", (index,)
            ).fetchone()
        claim = {
            "student_claim_name": student["display_name"],
            "student_claim_number": student["student_code"],
            "student_claim_group_id": student["group_id"],
        }
    response = client.post(
        "/api/auth/register",
        json={
            "login": login,
            "display_name": "Өтінім иесі",
            "password": password,
            "requested_role": requested_role,
            **claim,
        },
    )
    assert response.status_code == 201, response.text
    with store.connect() as connection:
        user_id = connection.execute(
            "SELECT user_id FROM Users WHERE login=?", (login,)
        ).fetchone()[0]
    return user_id, password


def student_approval(client, user_id, **changes):
    detail = client.get(f"/api/admin/requests/{user_id}")
    assert detail.status_code == 200, detail.text
    return client.post(
        f"/api/admin/requests/{user_id}/approve",
        json={
            "role_code": "student",
            "student_id": 1,
            "confirm_student_match": True,
            "claim_revision": detail.json()["claim_revision"],
            **changes,
        },
    )


@pytest.mark.parametrize("role", ["student", "teacher"])
def test_requested_role_never_grants_access_before_approval(auth_system, role):
    client, store, _, _ = auth_system
    user_id, password = register(client, store, requested_role=role)
    response = sign_in(client, "applicant", password)
    user = response.json()["user"]
    assert user["requested_role"] == role
    assert user["approval_status"] == "pending" and user["role_code"] == "pending"
    assert user["active"] is False and user["permissions"] == [] and user["student_id"] is None
    assert (
        client.get("/api/auth/me").status_code == client.get("/api/auth/status").status_code == 200
    )
    for path in (
        "/api/dashboard",
        "/api/grades",
        "/api/documents",
        "/api/audit",
        "/api/sessions",
        "/api/admin/users",
        "/api/academic/catalog",
    ):
        assert client.get(path).status_code == 403, path
    assert client.patch(f"/api/admin/users/{user_id}", json={"active": True}).status_code == 403
    assert client.post("/api/auth/logout").status_code == 200


@pytest.mark.parametrize("role", ["admin", "head", "auditor", "pending"])
def test_registration_cannot_request_privileged_roles(auth_system, role):
    client, _, _, _ = auth_system
    response = client.post(
        "/api/auth/register",
        json={
            "login": "bad_applicant",
            "display_name": "Өтінім иесі",
            "password": secrets.token_urlsafe(20),
            "requested_role": role,
        },
    )
    assert response.status_code == 422


def test_admin_approval_changes_live_session_role_and_audits_decision(auth_system):
    client, store, admin_password, _ = auth_system
    user_id, password = register(client, store)
    with TestClient(client.app, client=("127.0.0.2", 52000)) as applicant:
        sign_in(applicant, "applicant", password)
        sign_in(client, "first_admin", admin_password)
        pending = client.get("/api/admin/requests").json()["items"]
        assert [row["user_id"] for row in pending] == [user_id]
        assert pending[0]["requested_role"] == "teacher" and pending[0]["created_at"]
        assert client.patch(f"/api/admin/users/{user_id}", json={"active": True}).status_code == 409
        approved = client.post(
            f"/api/admin/requests/{user_id}/approve", json={"role_code": "teacher"}
        )
        assert approved.status_code == 200
        user = applicant.get("/api/auth/me").json()["user"]
        assert user["approval_status"] == "approved" and user["role_code"] == "teacher"
        assert user["active"] is True and "Learning.Manage" in user["permissions"]
        assert applicant.get("/api/grades").json()["items"] == []
        assert (
            client.post(
                f"/api/admin/requests/{user_id}/approve", json={"role_code": "admin"}
            ).status_code
            == 409
        )
    with store.connect() as connection:
        decision = connection.execute(
            "SELECT * FROM AuditLogs WHERE action='user_approve'"
        ).fetchone()
        assert decision["user_id"] == "first_admin" and decision["object_id"] == str(user_id)
        assert decision["ip_address"] == "127.0.0.1"
        assert json.loads(decision["new_value"])["role_code"] == "teacher"
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM AuditLogs WHERE action='role_change'"
            ).fetchone()[0]
            == 1
        )


def test_rejected_user_keeps_private_status_but_never_gets_closed_data(auth_system):
    client, store, admin_password, _ = auth_system
    user_id, password = register(client, store, requested_role="student")
    with TestClient(client.app, client=("127.0.0.2", 52000)) as applicant:
        sign_in(applicant, "applicant", password)
        sign_in(client, "first_admin", admin_password)
        response = client.post(
            f"/api/admin/requests/{user_id}/reject",
            json={"reason": "Студент жазбасын нақтылау қажет."},
        )
        assert response.status_code == 200
        user = applicant.get("/api/auth/status").json()["user"]
        assert user["approval_status"] == "rejected" and "нақтылау" in user["approval_reason"]
        assert applicant.get("/api/grades").status_code == 403
        assert applicant.get("/api/dashboard").json()["error"]["code"] == "approval_rejected"
        assert applicant.post("/api/auth/logout").status_code == 200
        assert (
            sign_in(applicant, "applicant", password).json()["user"]["approval_status"]
            == "rejected"
        )
    with store.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM AuditLogs WHERE action='user_reject'"
            ).fetchone()[0]
            == 1
        )


def test_student_approval_requires_existing_unique_link_and_relink_is_admin_audited(auth_system):
    client, store, password, _ = auth_system
    first_id, _ = register(client, store, "first_student", "student")
    second_id, _ = register(client, store, "second_student", "student")
    sign_in(client, "first_admin", password)
    assert student_approval(client, first_id, student_id=None).status_code == 422
    assert student_approval(client, first_id, student_id=99999).status_code == 422
    assert student_approval(client, first_id, student_id=True).status_code == 422
    assert student_approval(client, first_id).status_code == 200
    assert student_approval(client, second_id).status_code == 409
    response = client.patch(f"/api/admin/users/{first_id}", json={"student_id": 2})
    assert response.status_code == 200 and response.json()["user"]["student_id"] == 2
    assert (
        client.patch(f"/api/admin/users/{first_id}", json={"student_id": None}).status_code == 422
    )
    with store.connect() as connection:
        rows = connection.execute(
            "SELECT * FROM AuditLogs WHERE action='student_link' ORDER BY audit_id"
        ).fetchall()
        assert len(rows) == 2
        assert json.loads(rows[-1]["old_value"])["student_id"] == 1
        assert json.loads(rows[-1]["new_value"])["student_id"] == 2


def test_unapproved_state_overrides_accidental_admin_role_and_permissions(auth_system):
    client, store, _, _ = auth_system
    user_id, password = register(client, store)
    with store.connect(write=True) as connection:
        connection.execute(
            "UPDATE Users SET role_code='admin',active=1 WHERE user_id=?", (user_id,)
        )
    user = sign_in(client, "applicant", password).json()["user"]
    assert user["permissions"] == []
    assert client.get("/api/admin/users").status_code == 403
    assert client.get("/api/admin/roles").status_code == 403
    assert client.get("/api/audit").status_code == 403


def test_student_cannot_read_audit_or_admin_data_even_with_mistaken_matrix_grants(auth_system):
    client, store, admin_password, _ = auth_system
    user_id, password = register(client, store, requested_role="student")
    sign_in(client, "first_admin", admin_password)
    assert student_approval(client, user_id).status_code == 200
    with store.connect(write=True) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO RolePermissions(role_code,permission_code) "
            "SELECT 'student',code FROM Permissions"
        )
    with TestClient(client.app, client=("127.0.0.2", 52000)) as student:
        user = sign_in(student, "applicant", password).json()["user"]
        assert set(user["permissions"]) == {"Dashboard.View", "Learning.View", "Learning.Submit"}
        for path in (
            "/api/audit",
            "/api/admin/users",
            "/api/admin/roles",
            "/api/sessions",
            "/api/grades",
            "/api/documents",
        ):
            assert student.get(path).status_code == 403
        assert (
            student.patch(f"/api/admin/users/{user_id}", json={"student_id": 2}).status_code == 403
        )


def test_approval_and_student_link_roll_back_if_audit_insert_fails(auth_system):
    client, store, admin_password, _ = auth_system
    user_id, _ = register(client, store, requested_role="student")
    sign_in(client, "first_admin", admin_password)
    with store.connect(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER fail_approval BEFORE INSERT ON AuditLogs "
            "WHEN NEW.action='user_approve' "
            "BEGIN SELECT RAISE(ABORT,'synthetic failure'); END"
        )
    assert student_approval(client, user_id).status_code == 500
    with store.connect() as connection:
        row = connection.execute(
            "SELECT approval_status,active,student_id FROM Users WHERE user_id=?", (user_id,)
        ).fetchone()
        assert tuple(row) == ("pending", 0, None)


def test_v3_migration_preserves_accounts_audit_and_assigns_approval_states(tmp_path):
    settings = Settings(database_path=tmp_path / "old-v2.sqlite3")
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute(
            "CREATE TABLE AuditLogs(audit_id INTEGER PRIMARY KEY,occurred_at TEXT,user_id TEXT,"
            "role TEXT,action TEXT,object_type TEXT,object_id TEXT,result TEXT,reason TEXT,"
            "ip_address TEXT,old_value TEXT,new_value TEXT,session_id TEXT)"
        )
        connection.execute(
            "INSERT INTO AuditLogs VALUES (8,'2026-10-14T00:00:00.000000Z','legacy','teacher',"
            "'login','account','1','success','preserve','127.0.0.1',NULL,NULL,NULL)"
        )
        connection.execute(
            "CREATE TABLE Users(user_id INTEGER PRIMARY KEY,login TEXT UNIQUE,display_name TEXT,"
            "password_hash TEXT,role_code TEXT,active INTEGER,created_at TEXT)"
        )
        connection.executemany(
            "INSERT INTO Users VALUES (?,?,?,?,?,?,?)",
            [
                (
                    1,
                    "legacy_admin",
                    "Legacy admin",
                    hash_password(secrets.token_urlsafe(20)),
                    "admin",
                    1,
                    "2026-10-14T00:00:00.000000Z",
                ),
                (
                    2,
                    "legacy_teacher",
                    "Legacy teacher",
                    "unused",
                    "teacher",
                    0,
                    "2026-10-14T00:00:00.000000Z",
                ),
                (
                    3,
                    "legacy_pending",
                    "Legacy pending",
                    "unused",
                    "pending",
                    0,
                    "2026-10-14T00:00:00.000000Z",
                ),
            ],
        )
        connection.execute("PRAGMA user_version=2")
    store = Store(settings)
    store.initialize()
    store.initialize()
    with store.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        rows = connection.execute(
            "SELECT role_code,active,approval_status FROM Users ORDER BY user_id"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("admin", 1, "approved"),
            ("teacher", 0, "approved"),
            ("pending", 0, "pending"),
        ]
        assert (
            connection.execute("SELECT reason FROM AuditLogs WHERE audit_id=8").fetchone()[0]
            == "preserve"
        )
        assert "Learning.Submit" not in {
            row[0]
            for row in connection.execute(
                "SELECT permission_code FROM RolePermissions WHERE role_code='admin'"
            )
        }


def test_fictional_names_are_stable_and_seed_preserves_custom_names_and_grades(auth_system):
    _, store, _, _ = auth_system
    with store.connect(write=True) as connection:
        connection.execute(
            "UPDATE Students SET display_name='Синтетикалық студент 01' WHERE student_id=1"
        )
        connection.execute(
            "UPDATE Students SET display_name='Пайдаланушы енгізген есім' WHERE student_id=2"
        )
        connection.execute("UPDATE Grades SET value=77 WHERE grade_id=1")
    store.seed_academics()
    store.seed_academics()
    with store.connect() as connection:
        names = [
            row[0]
            for row in connection.execute("SELECT display_name FROM Students ORDER BY student_id")
        ]
        assert names[0] == FICTIONAL_STUDENTS[0] and names[1] == "Пайдаланушы енгізген есім"
        assert len(names) == len(set(names)) == 12
        assert not any("Синтетикалық студент" in name for name in names)
        assert connection.execute("SELECT value FROM Grades WHERE grade_id=1").fetchone()[0] == 77


def test_rule_flags_include_review_state_and_evidence_without_auto_block(auth_system):
    client, store, password, _ = auth_system
    sign_in(client, "first_admin", password)
    for _ in range(3):
        assert (
            client.post(
                "/api/auth/login", json={"login": "first_admin", "password": "wrong"}
            ).status_code
            == 401
        )
    with store.connect() as connection:
        row = connection.execute("SELECT * FROM Sessions").fetchone()
        assert row["status"] == "active" and row["review_status"] == "awaiting_admin"
        assert row["review_source"] == "rule"
        evidence = json.loads(row["review_audit_ids"])
        assert len(evidence) == 4
        assert (
            connection.execute(
                "SELECT action FROM AuditLogs WHERE audit_id=?", (evidence[-1],)
            ).fetchone()[0]
            == "session_flagged"
        )


def test_dismissed_failure_rule_rearms_only_after_a_new_failed_attempt(auth_system):
    client, store, password, _ = auth_system
    sign_in(client, "first_admin", password)
    for _ in range(3):
        assert (
            client.post(
                "/api/auth/login", json={"login": "first_admin", "password": "wrong"}
            ).status_code
            == 401
        )
    with store.connect() as connection:
        session_id = connection.execute("SELECT session_id FROM Sessions").fetchone()[0]
    assert (
        client.post(
            f"/api/sessions/{session_id}/dismiss",
            json={"reason": "Белгі тексеріліп, жалған дабыл деп танылды."},
        ).status_code
        == 200
    )
    sign_in(client, "first_admin", password)
    with store.connect() as connection:
        assert (
            connection.execute(
                "SELECT review_status FROM Sessions WHERE session_id=?", (session_id,)
            ).fetchone()[0]
            == "false_positive"
        )
    assert (
        client.post(
            "/api/auth/login", json={"login": "first_admin", "password": "wrong"}
        ).status_code
        == 401
    )
    with store.connect() as connection:
        row = connection.execute(
            "SELECT * FROM Sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        assert row["review_status"] == "awaiting_admin" and row["status"] == "active"
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM AuditLogs WHERE action='session_flagged' AND session_id=?",
                (session_id,),
            ).fetchone()[0]
            == 2
        )


def test_dismissed_ip_rule_rearms_on_a_new_ip_change(auth_system):
    client, store, password, _ = auth_system
    sign_in(client, "first_admin", password)
    with TestClient(client.app, client=("127.0.0.2", 52000)) as other_ip:
        for name in (SESSION_COOKIE, CSRF_COOKIE):
            other_ip.cookies.set(name, client.cookies[name])
        assert other_ip.get("/api/auth/me").status_code == 200
        with store.connect() as connection:
            session_id = connection.execute("SELECT session_id FROM Sessions").fetchone()[0]
        assert (
            client.post(
                f"/api/sessions/{session_id}/dismiss",
                json={"reason": "Желі ауысуы тексерілді, жалған дабыл."},
            ).status_code
            == 200
        )
        assert other_ip.get("/api/auth/me").status_code == 200
    with store.connect() as connection:
        row = connection.execute(
            "SELECT * FROM Sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        assert row["review_status"] == "awaiting_admin" and row["status"] == "active"


def test_users_permission_alone_cannot_assign_roles_or_promote_self(auth_system):
    client, store, password, _ = auth_system
    teacher_id = add_user(store, "delegated_teacher", password)
    with store.connect(write=True) as connection:
        connection.execute(
            "INSERT INTO RolePermissions(role_code,permission_code) "
            "VALUES ('teacher','Users.Manage')"
        )
    with TestClient(client.app, client=("127.0.0.2", 52000)) as teacher:
        sign_in(teacher, "delegated_teacher", password)
        assert teacher.get("/api/admin/users").status_code == 200
        for role_code in ("admin", "head", "auditor", "teacher"):
            response = teacher.patch(
                f"/api/admin/users/{teacher_id}", json={"role_code": role_code}
            )
            assert response.status_code == 403
    with store.connect() as connection:
        assert (
            connection.execute(
                "SELECT role_code FROM Users WHERE user_id=?", (teacher_id,)
            ).fetchone()[0]
            == "teacher"
        )
