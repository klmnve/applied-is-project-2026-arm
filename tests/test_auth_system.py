"""Real cookies, database RBAC, slow hashes, revocation and transaction boundaries."""

import json
import secrets
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.auth import (
    CSRF_COOKIE,
    FAILURE_REASON,
    IP_CHANGE_REASON,
    SESSION_COOKIE,
    hash_password,
    iso_utc,
    utc_now,
    verify_password,
)
from app.bootstrap import create_admin
from app.config import Settings
from app.errors import AppError
from app.main import create_app
from app.store import Store
from tests.conftest import FakeEmbedder, FakeLLM


@pytest.fixture
def auth_system(tmp_path):
    settings = Settings(database_path=tmp_path / "auth.sqlite3", login_max_attempts=5)
    store = Store(settings)
    store.initialize()
    password = secrets.token_urlsafe(20)
    admin = create_admin(store, "first_admin", "Синтетикалық әкімші", password)
    application = create_app(settings=settings, llm=FakeLLM(), embedder=FakeEmbedder())
    with TestClient(
        application, client=("127.0.0.1", 51000), raise_server_exceptions=False
    ) as client:
        yield client, store, password, admin


def sign_in(client, login, password):
    response = client.post("/api/auth/login", json={"login": login, "password": password})
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return response


def add_user(store, login, password, role="teacher", active=True):
    with store.connect(write=True) as connection:
        cursor = connection.execute(
            "INSERT INTO Users(login,display_name,password_hash,role_code,active,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                login,
                "Синтетикалық пайдаланушы",
                hash_password(password),
                role,
                int(active),
                iso_utc(utc_now()),
            ),
        )
        return cursor.lastrowid


def test_password_hashes_are_salted_slow_and_do_not_include_plaintext():
    password = secrets.token_urlsafe(20)
    first, second = hash_password(password), hash_password(password)
    assert first != second
    assert first.startswith("scrypt$32768$8$3$")
    assert password not in first
    assert verify_password(password, first)
    assert not verify_password(password + "!", first)
    assert not verify_password(password, "malformed")


def test_bootstrap_is_one_time_and_never_resets_existing_users(auth_system):
    _, store, password, admin = auth_system
    with pytest.raises(AppError, match="Bootstrap"):
        create_admin(store, "second_admin", "Басқа әкімші", secrets.token_urlsafe(20))
    with store.connect() as connection:
        rows = connection.execute("SELECT * FROM Users").fetchall()
        assert len(rows) == 1
        assert rows[0]["user_id"] == admin["user_id"]
        assert verify_password(password, rows[0]["password_hash"])


def test_bootstrap_refuses_registered_login_without_promoting_or_resetting(tmp_path):
    store = Store(Settings(database_path=tmp_path / "bootstrap.sqlite3"))
    store.initialize()
    password = secrets.token_urlsafe(20)
    add_user(store, "registered", password, role="pending", active=False)
    with pytest.raises(AppError) as error:
        create_admin(store, "registered", "Жаңа әкімші", secrets.token_urlsafe(20))
    assert error.value.code == "login_unavailable"
    with store.connect() as connection:
        row = connection.execute("SELECT * FROM Users").fetchone()
        assert row["role_code"] == "pending" and row["active"] == 0
        assert verify_password(password, row["password_hash"])


def test_registration_creates_inactive_pending_user_and_forbids_role_escalation(auth_system):
    client, store, _, _ = auth_system
    password = secrets.token_urlsafe(20)
    payload = {"login": "new_teacher", "display_name": "Синтетикалық оқытушы", "password": password}
    assert (
        client.post("/api/auth/register", json=payload | {"role_code": "admin"}).status_code == 422
    )
    assert client.post("/api/auth/register", json=payload).status_code == 201
    with store.connect() as connection:
        row = connection.execute("SELECT * FROM Users WHERE login='new_teacher'").fetchone()
        assert row["role_code"] == "pending" and row["active"] == 0
        assert row["password_hash"] != password
        assert verify_password(password, row["password_hash"])
    pending_login = client.post(
        "/api/auth/login", json={"login": "new_teacher", "password": password}
    )
    assert pending_login.status_code == 200
    assert pending_login.json()["user"]["approval_status"] == "pending"
    assert pending_login.json()["user"]["permissions"] == []
    assert client.get("/api/dashboard").status_code == 403
    assert client.post("/api/auth/register", json=payload).status_code == 409


def test_login_sets_strict_http_only_cookie_and_only_hashes_live_in_database(auth_system):
    client, store, password, _ = auth_system
    response = sign_in(client, "FIRST_ADMIN", password)
    cookies = response.headers.get_list("set-cookie")
    assert any(
        "arm_session=" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
        for cookie in cookies
    )
    assert any("arm_csrf=" in cookie and "HttpOnly" not in cookie for cookie in cookies)
    raw_session, raw_csrf = client.cookies[SESSION_COOKIE], client.cookies[CSRF_COOKIE]
    with store.connect() as connection:
        row = connection.execute("SELECT * FROM Sessions").fetchone()
        assert row["token_hash"] != raw_session and row["csrf_hash"] != raw_csrf
        audits = json.dumps([dict(row) for row in connection.execute("SELECT * FROM AuditLogs")])
        assert password not in audits and raw_session not in audits and raw_csrf not in audits
        assert row["initial_ip"] == "127.0.0.1"
    assert response.json()["user"]["role_code"] == "admin"
    assert "password_hash" not in response.text
    assert client.get("/api/auth/me").status_code == 200


def test_wrong_unknown_and_inactive_credentials_have_identical_public_error(auth_system):
    client, store, password, _ = auth_system
    add_user(store, "inactive_user", password, active=False)
    wrong = client.post("/api/auth/login", json={"login": "first_admin", "password": "incorrect"})
    unknown = client.post(
        "/api/auth/login", json={"login": "missing_user", "password": "incorrect"}
    )
    inactive = client.post("/api/auth/login", json={"login": "inactive_user", "password": password})
    assert wrong.status_code == unknown.status_code == inactive.status_code == 401
    assert wrong.json() == unknown.json() == inactive.json()
    with store.connect() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM LoginAttempts WHERE success=0").fetchone()[0]
            == 3
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM AuditLogs WHERE action='login' AND result='failure' "
                "AND object_type='account'"
            ).fetchone()[0]
            == 3
        )


def test_database_rate_limit_blocks_after_configured_failures(auth_system):
    client, store, password, _ = auth_system
    for _ in range(5):
        response = client.post(
            "/api/auth/login", json={"login": "first_admin", "password": "incorrect"}
        )
        assert response.status_code == 401
    response = client.post("/api/auth/login", json={"login": "first_admin", "password": password})
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"
    assert int(response.headers["Retry-After"]) > 0
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM Sessions").fetchone()[0] == 0


def test_expired_session_is_persistently_invalidated(auth_system):
    client, store, password, _ = auth_system
    sign_in(client, "first_admin", password)
    with store.connect(write=True) as connection:
        connection.execute(
            "UPDATE Sessions SET expires_at=?", (iso_utc(utc_now() - timedelta(seconds=1)),)
        )
    response = client.get("/api/auth/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "session_expired"
    with store.connect() as connection:
        assert connection.execute("SELECT status FROM Sessions").fetchone()[0] == "expired"


def test_logout_revokes_server_token_and_clears_cookies(auth_system):
    client, store, password, _ = auth_system
    sign_in(client, "first_admin", password)
    raw_session = client.cookies[SESSION_COOKIE]
    assert client.post("/api/auth/logout").status_code == 200
    assert SESSION_COOKIE not in client.cookies and CSRF_COOKIE not in client.cookies
    client.cookies.set(SESSION_COOKIE, raw_session)
    assert client.get("/api/auth/me").status_code == 401
    with store.connect() as connection:
        assert connection.execute("SELECT status FROM Sessions").fetchone()[0] == "revoked"
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM AuditLogs WHERE action='logout' AND object_type='session'"
            ).fetchone()[0]
            == 1
        )


def test_csrf_requires_cookie_and_session_bound_header(auth_system):
    client, _, password, _ = auth_system
    sign_in(client, "first_admin", password)
    csrf = client.headers.pop("X-CSRF-Token")
    assert client.post("/api/auth/logout").status_code == 403
    client.headers["X-CSRF-Token"] = "forged"
    assert client.post("/api/auth/logout").status_code == 403
    assert client.get("/api/auth/me").status_code == 200
    client.headers["X-CSRF-Token"] = csrf
    assert client.post("/api/auth/logout").status_code == 200


def test_anonymous_requests_and_demo_header_cannot_bypass_real_auth(auth_system):
    client, _, _, _ = auth_system
    assert client.get("/api/admin/users").status_code == 401
    assert client.get("/api/auth/me", headers={"X-Demo-User": "demo_admin"}).status_code == 401


@pytest.mark.parametrize("role", ["teacher", "auditor", "head", "pending"])
def test_non_admin_roles_cannot_use_administration_endpoints(auth_system, role):
    client, store, password, _ = auth_system
    add_user(store, "limited_user", password, role=role)
    sign_in(client, "limited_user", password)
    assert client.get("/api/admin/users").status_code == 403
    assert client.get("/api/admin/roles").status_code == 403
    assert (
        client.put(
            "/api/admin/roles/teacher/permissions", json={"permissions": ["Users.Manage"]}
        ).status_code
        == 403
    )


@pytest.mark.parametrize("change", [{"active": False}, {"role_code": "teacher"}])
def test_last_active_admin_cannot_be_deactivated_or_demoted(auth_system, change):
    client, store, password, admin = auth_system
    sign_in(client, "first_admin", password)
    response = client.patch(f"/api/admin/users/{admin['user_id']}", json=change)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "last_admin"
    with store.connect() as connection:
        row = connection.execute(
            "SELECT role_code,active FROM Users WHERE user_id=?", (admin["user_id"],)
        ).fetchone()
        assert tuple(row) == ("admin", 1)


def test_admin_permissions_cannot_remove_management_and_unknown_codes_fail(auth_system):
    client, _, password, _ = auth_system
    sign_in(client, "first_admin", password)
    assert (
        client.put(
            "/api/admin/roles/admin/permissions", json={"permissions": ["Dashboard.View"]}
        ).status_code
        == 409
    )
    assert (
        client.put(
            "/api/admin/roles/teacher/permissions", json={"permissions": ["Root.Everything"]}
        ).status_code
        == 422
    )


def test_live_permission_matrix_changes_apply_to_existing_sessions(auth_system):
    client, store, password, _ = auth_system
    add_user(store, "live_teacher", password)
    with TestClient(client.app, client=("127.0.0.2", 52000)) as teacher:
        sign_in(teacher, "live_teacher", password)
        assert teacher.get("/api/admin/users").status_code == 403
        sign_in(client, "first_admin", password)
        update = client.put(
            "/api/admin/roles/teacher/permissions",
            json={"permissions": ["Dashboard.View", "Users.Manage"]},
        )
        assert update.status_code == 200
        assert teacher.get("/api/admin/users").status_code == 200
        assert teacher.get("/api/admin/roles").status_code == 200
        assert (
            teacher.put(
                "/api/admin/roles/teacher/permissions", json={"permissions": []}
            ).status_code
            == 403
        )
        assert (
            client.put(
                "/api/admin/roles/teacher/permissions", json={"permissions": ["Dashboard.View"]}
            ).status_code
            == 200
        )
        assert teacher.get("/api/admin/users").status_code == 403


def test_account_deactivation_revokes_existing_sessions(auth_system):
    client, store, password, _ = auth_system
    user_id = add_user(store, "active_teacher", password)
    with TestClient(client.app, client=("127.0.0.2", 52000)) as teacher:
        sign_in(teacher, "active_teacher", password)
        sign_in(client, "first_admin", password)
        assert (
            client.patch(f"/api/admin/users/{user_id}", json={"active": False}).status_code == 200
        )
        assert teacher.get("/api/auth/me").status_code == 401


def test_user_update_and_audit_share_transaction_and_roll_back_on_audit_failure(auth_system):
    client, store, password, _ = auth_system
    user_id = add_user(store, "transaction_user", password)
    sign_in(client, "first_admin", password)
    with store.connect(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER fail_user_audit BEFORE INSERT ON AuditLogs "
            "WHEN NEW.action='user_change' BEGIN SELECT RAISE(ABORT,'synthetic failure'); END"
        )
    response = client.patch(
        f"/api/admin/users/{user_id}", json={"role_code": "auditor", "active": False}
    )
    assert response.status_code == 500
    with store.connect() as connection:
        row = connection.execute(
            "SELECT role_code,active FROM Users WHERE user_id=?", (user_id,)
        ).fetchone()
        assert tuple(row) == ("teacher", 1)


def test_many_failed_logins_flag_sessions_without_automatic_blocking(auth_system):
    client, store, password, _ = auth_system
    sign_in(client, "first_admin", password)
    with store.connect(write=True) as connection:
        connection.execute("UPDATE Sessions SET suspicious_reason=?", (IP_CHANGE_REASON,))
    for _ in range(3):
        assert (
            client.post(
                "/api/auth/login", json={"login": "first_admin", "password": "incorrect"}
            ).status_code
            == 401
        )
    with store.connect() as connection:
        row = connection.execute("SELECT status,suspicious_reason FROM Sessions").fetchone()
        assert row["status"] == "active" and "сәтсіз кіру" in row["suspicious_reason"]
        assert IP_CHANGE_REASON in row["suspicious_reason"]
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM AuditLogs WHERE action='session_flagged'"
            ).fetchone()[0]
            == 1
        )
    assert client.get("/api/auth/me").status_code == 200


def test_ip_switch_rule_is_explicit_audited_and_does_not_block(auth_system):
    client, store, password, _ = auth_system
    sign_in(client, "first_admin", password)
    with store.connect(write=True) as connection:
        connection.execute("UPDATE Sessions SET suspicious_reason=?", (FAILURE_REASON,))
    with TestClient(client.app, client=("127.0.0.2", 53000)) as changed_ip:
        for name in (SESSION_COOKIE, CSRF_COOKIE):
            changed_ip.cookies.set(name, client.cookies[name])
        assert changed_ip.get("/api/auth/me").status_code == 200
    with store.connect() as connection:
        row = connection.execute("SELECT status,last_ip,suspicious_reason FROM Sessions").fetchone()
        assert row["status"] == "active" and row["last_ip"] == "127.0.0.2"
        assert "IP" in row["suspicious_reason"]
        assert FAILURE_REASON in row["suspicious_reason"]
        event = connection.execute(
            "SELECT * FROM AuditLogs WHERE action='session_flagged'"
        ).fetchone()
        assert event["ip_address"] == "127.0.0.2" and event["user_id"] == "first_admin"


def test_user_search_treats_injection_as_literal_text(auth_system):
    client, store, password, _ = auth_system
    sign_in(client, "first_admin", password)
    response = client.get("/api/admin/users", params={"q": "' OR 1=1 --"})
    assert response.status_code == 200 and response.json()["items"] == []
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM Users").fetchone()[0] == 1


def test_registration_rejects_weak_password_and_hostile_login(auth_system):
    client, _, _, _ = auth_system
    assert (
        client.post(
            "/api/auth/register",
            json={"login": "valid_user", "display_name": "Test", "password": "short"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/auth/register",
            json={
                "login": "user' OR 1=1 --",
                "display_name": "Test",
                "password": secrets.token_urlsafe(20),
            },
        ).status_code
        == 422
    )


def test_role_mutation_and_audit_roll_back_together(auth_system):
    client, store, password, _ = auth_system
    sign_in(client, "first_admin", password)
    with store.connect(write=True) as connection:
        before = [
            row[0]
            for row in connection.execute(
                "SELECT permission_code FROM RolePermissions WHERE role_code='teacher' "
                "ORDER BY permission_code"
            )
        ]
        connection.execute(
            "CREATE TRIGGER fail_role_audit BEFORE INSERT ON AuditLogs "
            "WHEN NEW.action='permission_change' BEGIN SELECT RAISE(ABORT,'synthetic failure'); END"
        )
    assert (
        client.put("/api/admin/roles/teacher/permissions", json={"permissions": []}).status_code
        == 500
    )
    with store.connect() as connection:
        after = [
            row[0]
            for row in connection.execute(
                "SELECT permission_code FROM RolePermissions WHERE role_code='teacher' "
                "ORDER BY permission_code"
            )
        ]
        assert after == before
