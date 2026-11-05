import secrets
import sqlite3
from contextlib import ExitStack
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.auth import SESSION_COOKIE, hash_password
from app.bootstrap import create_admin
from app.config import Settings
from app.errors import AppError
from app.main import create_app
from app.store import Store, iso_utc


class MissingLLM:
    def parse(self, question, now):
        raise AppError("model_unavailable", "Локалды модель қолжетімсіз.", 503)

    def summarize(self, records):
        raise AppError("model_unavailable", "Локалды модель қолжетімсіз.", 503)

    def readiness(self):
        return {"status": "unavailable", "message": "Локалды модель қолжетімсіз.", "setup": ""}


class MissingEmbedder:
    def encode(self, texts):
        raise AppError("model_unavailable", "Локалды embedding қолжетімсіз.", 503)

    def readiness(self):
        return {"status": "unavailable", "message": "Локалды embedding қолжетімсіз.", "setup": ""}


@pytest.fixture
def core_system(tmp_path):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "core.sqlite3",
        upload_dir=tmp_path / "uploads",
        login_max_attempts=10,
        suspicious_failure_threshold=3,
    )
    initial_store = Store(settings)
    passwords = {role: secrets.token_urlsafe(18) for role in ("admin", "teacher", "auditor")}
    create_admin(initial_store, "core_admin", "Синтетикалық әкімші", passwords["admin"])
    with initial_store.connect(write=True) as connection:
        for role in ("teacher", "auditor"):
            connection.execute(
                "INSERT INTO Users (login,display_name,password_hash,role_code,active,created_at) "
                "VALUES (?,?,?,?,1,?)",
                (
                    f"core_{role}",
                    f"Синтетикалық {role}",
                    hash_password(passwords[role]),
                    role,
                    iso_utc(),
                ),
            )
    application = create_app(settings, llm=MissingLLM(), embedder=MissingEmbedder())
    with ExitStack() as stack:
        clients = {}
        csrf = {}
        sessions = {}
        for index, role in enumerate(("admin", "teacher", "auditor"), 1):
            client = stack.enter_context(
                TestClient(
                    application,
                    raise_server_exceptions=False,
                    client=(f"192.0.2.{index * 10}", 51000),
                )
            )
            response = client.post(
                "/api/auth/login", json={"login": f"core_{role}", "password": passwords[role]}
            )
            assert response.status_code == 200
            clients[role] = client
            csrf[role] = response.json()["csrf_token"]
            with application.state.store.connect() as connection:
                sessions[role] = connection.execute(
                    "SELECT s.session_id FROM Sessions s JOIN Users u ON u.user_id=s.user_id "
                    "WHERE u.login=? ORDER BY s.created_at DESC LIMIT 1",
                    (f"core_{role}",),
                ).fetchone()[0]
        yield SimpleNamespace(
            app=application,
            store=application.state.store,
            settings=settings,
            clients=clients,
            csrf=csrf,
            sessions=sessions,
            passwords=passwords,
        )


def post(system, role, path, payload, **headers):
    return system.clients[role].post(
        path, json=payload, headers={"X-CSRF-Token": system.csrf[role], **headers}
    )


def test_legacy_migration_preserves_nine_fields_and_is_idempotent_append_only(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "legacy.sqlite3")
    legacy = (
        7,
        "2025-10-01T02:03:04.123456Z",
        "legacy_synthetic",
        "teacher",
        "login",
        "session",
        "legacy-object",
        "failure",
        "Бұрынғы синтетикалық аудит мәтіні.",
    )
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute(
            "CREATE TABLE AuditLogs (audit_id INTEGER PRIMARY KEY,occurred_at TEXT NOT NULL,"
            "user_id TEXT NOT NULL,role TEXT NOT NULL,action TEXT NOT NULL "
            "CHECK(action IN ('login','logout')),object_type TEXT NOT NULL,object_id TEXT NOT NULL,"
            "result TEXT NOT NULL CHECK(result IN ('success','failure','denied')),"
            "reason TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO AuditLogs VALUES (?,?,?,?,?,?,?,?,?)", legacy)
    store = Store(settings)
    store.initialize()
    store.initialize()
    with store.connect() as connection:
        migrated = connection.execute("SELECT * FROM AuditLogs").fetchall()
        assert len(migrated) == 1
        assert tuple(migrated[0])[:9] == legacy
        assert migrated[0]["ip_address"] == "unknown"
        assert migrated[0]["old_value"] is None
        assert migrated[0]["new_value"] is None
        assert migrated[0]["session_id"] is None
        assert connection.execute("PRAGMA user_version").fetchone()[0] >= 2
        assert connection.execute("SELECT COUNT(*) FROM Users").fetchone()[0] == 0
    with pytest.raises(sqlite3.IntegrityError, match="append only"):
        with store.connect(write=True) as connection:
            connection.execute("UPDATE AuditLogs SET reason=? WHERE audit_id=7", ("changed",))
    with pytest.raises(sqlite3.IntegrityError, match="append only"):
        with store.connect(write=True) as connection:
            connection.execute("DELETE FROM AuditLogs WHERE audit_id=7")
    with store.connect() as connection:
        assert tuple(connection.execute("SELECT * FROM AuditLogs").fetchone())[:9] == legacy


def test_sessions_list_exposes_public_metadata_without_session_secrets(core_system):
    system = core_system
    response = system.clients["admin"].get("/api/sessions")
    assert response.status_code == 200
    assert response.json()["total"] == 3
    with system.store.connect() as connection:
        private = connection.execute("SELECT token_hash,csrf_hash FROM Sessions").fetchall()
    for role, client in system.clients.items():
        assert client.cookies[SESSION_COOKIE] not in response.text
        assert system.csrf[role] not in response.text
    for row in private:
        assert row["token_hash"] not in response.text
        assert row["csrf_hash"] not in response.text
    for item in response.json()["items"]:
        assert {
            "session_id",
            "login",
            "created_at",
            "last_seen_at",
            "last_ip",
            "status",
        } <= item.keys()
        assert not ({"token_hash", "csrf_hash", "password_hash", "cookie"} & item.keys())


def test_manual_block_unblock_never_resurrects_old_token_and_fresh_login_works(core_system):
    system = core_system
    teacher = system.clients["teacher"]
    old_cookie = teacher.cookies[SESSION_COOKIE]
    endpoint = f"/api/sessions/{system.sessions['teacher']}"
    assert (
        post(
            system, "admin", endpoint + "/block", {"reason": "Әкімшінің тексеруі қажет."}
        ).status_code
        == 200
    )
    assert teacher.get("/api/auth/me").status_code == 401
    unblocked = post(
        system, "admin", endpoint + "/unblock", {"reason": "Тексеру аяқталды; қайта кіріңіз."}
    )
    assert unblocked.status_code == 200
    assert unblocked.json()["status"] == "revoked"
    assert teacher.get("/api/auth/me").status_code == 401
    fresh = teacher.post(
        "/api/auth/login", json={"login": "core_teacher", "password": system.passwords["teacher"]}
    )
    assert fresh.status_code == 200
    assert teacher.get("/api/auth/me").status_code == 200
    assert (
        teacher.get(
            "/api/auth/me", headers={"Cookie": f"{SESSION_COOKIE}={old_cookie}"}
        ).status_code
        == 401
    )
    with system.store.connect() as connection:
        row = connection.execute(
            "SELECT status FROM Sessions WHERE session_id=?", (system.sessions["teacher"],)
        ).fetchone()
        assert row["status"] == "revoked"
        actions = {
            record[0]
            for record in connection.execute(
                "SELECT action FROM AuditLogs WHERE object_id=?", (system.sessions["teacher"],)
            )
        }
    assert {"session_block", "session_unblock"} <= actions


def test_session_terminate_immediately_invalidates_cookie(core_system):
    system = core_system
    response = post(
        system,
        "admin",
        f"/api/sessions/{system.sessions['teacher']}/terminate",
        {"reason": "Әкімші сессияны аяқтады."},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "revoked"
    assert system.clients["teacher"].get("/api/auth/me").status_code == 401


def test_session_control_requires_permission_and_rolls_back_if_audit_fails(
    core_system, monkeypatch
):
    system = core_system
    endpoint = f"/api/sessions/{system.sessions['auditor']}/block"
    assert (
        post(system, "teacher", endpoint, {"reason": "Рұқсатсыз әрекет мысалы."}).status_code == 403
    )
    assert system.clients["teacher"].get("/api/sessions").status_code == 403

    def fail_audit(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic audit failure")

    monkeypatch.setattr(system.store, "audit", fail_audit)
    assert post(system, "admin", endpoint, {"reason": "Тексеру үшін бұғаттау."}).status_code == 500
    with system.store.connect() as connection:
        target = connection.execute(
            "SELECT status,block_reason FROM Sessions WHERE session_id=?",
            (system.sessions["auditor"],),
        ).fetchone()
    assert target["status"] == "active"
    assert target["block_reason"] == ""


def test_failed_login_rule_flags_once_without_automatically_blocking(core_system, monkeypatch):
    system = core_system
    fixed_now = datetime(2026, 10, 15, 9, tzinfo=UTC)
    monkeypatch.setattr("app.auth_routes.utc_now", lambda: fixed_now)
    failures = TestClient(system.app, client=("198.51.100.25", 51001))
    try:
        for _ in range(system.settings.suspicious_failure_threshold):
            response = failures.post(
                "/api/auth/login",
                json={"login": "core_teacher", "password": secrets.token_urlsafe(18)},
            )
            assert response.status_code == 401
    finally:
        failures.close()
    with system.store.connect() as connection:
        session = connection.execute(
            "SELECT status,suspicious_reason FROM Sessions WHERE session_id=?",
            (system.sessions["teacher"],),
        ).fetchone()
        flags = connection.execute(
            "SELECT COUNT(*) FROM AuditLogs WHERE action='session_flagged' AND object_id=?",
            (system.sessions["teacher"],),
        ).fetchone()[0]
    assert session["status"] == "active"
    assert session["suspicious_reason"]
    assert flags == 1


def test_ip_change_rule_uses_peer_ip_and_flags_existing_session(core_system):
    system = core_system
    teacher = system.clients["teacher"]
    changed_peer = TestClient(system.app, client=("198.51.100.80", 51002))
    try:
        response = changed_peer.get(
            "/api/auth/me",
            headers={"Cookie": f"{SESSION_COOKIE}={teacher.cookies[SESSION_COOKIE]}"},
        )
    finally:
        changed_peer.close()
    assert response.status_code == 200
    with system.store.connect() as connection:
        session = connection.execute(
            "SELECT * FROM Sessions WHERE session_id=?", (system.sessions["teacher"],)
        ).fetchone()
    assert session["last_ip"] == "198.51.100.80"
    assert session["suspicious_reason"]
    assert session["status"] == "active"


def insert_audit_filter_events(system):
    rows = [
        (9001, "2026-01-06T23:59:59.999999Z", "grade_change", "success", "203.0.113.20"),
        (9002, "2026-01-07T00:00:00.000000Z", "grade_change", "success", "203.0.113.20"),
        (9003, "2026-01-13T23:59:59.999999Z", "grade_change", "success", "203.0.113.20"),
        (9004, "2026-01-14T00:00:00.000000Z", "grade_change", "success", "203.0.113.20"),
        (9005, "2026-01-09T00:00:00.000000Z", "grade_change", "denied", "203.0.113.20"),
        (9006, "2026-01-09T00:00:00.000000Z", "grade_change", "success", "203.0.113.21"),
    ]
    with system.store.connect(write=True) as connection:
        for audit_id, occurred_at, action, result, ip in rows:
            connection.execute(
                "INSERT INTO AuditLogs (audit_id,occurred_at,user_id,role,action,object_type,"
                "object_id,result,reason,ip_address,old_value,new_value) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    audit_id,
                    occurred_at,
                    "core_teacher",
                    "teacher",
                    action,
                    "grade",
                    "1",
                    result,
                    "Синтетикалық сүзгі оқиғасы.",
                    ip,
                    "75",
                    "88",
                ),
            )


def test_audit_filters_pagination_half_open_utc_and_full_detail(core_system):
    system = core_system
    insert_audit_filter_events(system)
    params = {
        "actor": "core_teacher",
        "action": "grade_change",
        "result": "success",
        "ip": "203.0.113.20",
        "start_at": "2026-01-07T05:00:00+05:00",
        "end_at": "2026-01-14T05:00:00+05:00",
        "page": 1,
        "page_size": 1,
    }
    first = system.clients["auditor"].get("/api/audit", params=params)
    assert first.status_code == 200
    assert first.json()["total"] == 2
    assert [row["audit_id"] for row in first.json()["items"]] == [9003]
    second = system.clients["auditor"].get("/api/audit", params={**params, "page": 2})
    assert [row["audit_id"] for row in second.json()["items"]] == [9002]
    detail = system.clients["auditor"].get("/api/audit/9003")
    assert detail.status_code == 200
    assert detail.json()["old_value"] == 75
    assert detail.json()["new_value"] == 88
    assert detail.json()["role"] == "teacher"
    assert detail.json()["actor_login"] == "core_teacher"
    assert detail.json()["ip_address"] == "203.0.113.20"
    assert system.clients["auditor"].delete("/api/audit/9003").status_code == 405


def test_audit_filters_reject_or_bind_sql_injection_and_invalid_dates(core_system):
    system = core_system
    insert_audit_filter_events(system)
    for params in ({"actor": "core_teacher' OR 1=1 --"}, {"ip": "203.0.113.20' OR 1=1 --"}):
        response = system.clients["admin"].get("/api/audit", params=params)
        assert response.status_code == 200
        assert response.json()["total"] == 0
    for params in (
        {"action": "grade_change' OR 1=1 --"},
        {"result": "success' OR 1=1 --"},
        {"start_at": "2026-01-07T00:00:00Z' OR 1=1 --"},
        {"start_at": "2026-01-07T00:00:00"},
        {"start_at": "2026-01-14T00:00:00Z", "end_at": "2026-01-07T00:00:00Z"},
    ):
        assert system.clients["admin"].get("/api/audit", params=params).status_code == 422
    with system.store.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM AuditLogs WHERE audit_id BETWEEN 9001 AND 9006"
            ).fetchone()[0]
            == 6
        )


def test_auditor_can_read_audit_but_cannot_change_grades_users_or_sessions(core_system):
    system = core_system
    auditor = system.clients["auditor"]
    assert auditor.get("/api/audit").status_code == 200
    for endpoint in ("/api/grades", "/api/admin/users", "/api/sessions"):
        assert auditor.get(endpoint).status_code == 403
    grade = auditor.patch(
        "/api/grades/1",
        json={"value": 99, "version": 0},
        headers={"X-CSRF-Token": system.csrf["auditor"]},
    )
    user = auditor.patch(
        "/api/admin/users/2",
        json={"active": False},
        headers={"X-CSRF-Token": system.csrf["auditor"]},
    )
    assert grade.status_code == 403
    assert user.status_code == 403
    assert (
        post(
            system,
            "auditor",
            f"/api/sessions/{system.sessions['teacher']}/block",
            {"reason": "Рұқсатсыз әрекет."},
        ).status_code
        == 403
    )


def test_dashboard_kpis_match_database_and_do_not_leak_admin_data_to_teacher(core_system):
    system = core_system
    response = system.clients["admin"].get("/api/dashboard")
    assert response.status_code == 200
    with system.store.connect() as connection:
        expected = {
            "users": connection.execute("SELECT COUNT(*) FROM Users").fetchone()[0],
            "active_users": connection.execute(
                "SELECT COUNT(*) FROM Users WHERE active=1"
            ).fetchone()[0],
            "students": connection.execute("SELECT COUNT(*) FROM Students").fetchone()[0],
            "documents": connection.execute(
                "SELECT COUNT(*) FROM Documents WHERE deleted_at IS NULL"
            ).fetchone()[0],
            "audit_events": connection.execute("SELECT COUNT(*) FROM AuditLogs").fetchone()[0],
            "active_sessions": connection.execute(
                "SELECT COUNT(*) FROM Sessions WHERE status='active'"
            ).fetchone()[0],
            "suspicious_sessions": 0,
        }
    assert response.json()["kpis"] == expected
    assert "chart" not in response.json()
    teacher = system.clients["teacher"].get("/api/dashboard").json()
    assert teacher["kpis"]["users"] is None
    assert teacher["kpis"]["active_sessions"] is None
    assert teacher["recent_audit"] == []


def test_missing_ai_models_leave_auth_grades_documents_and_audit_working(core_system):
    system = core_system
    admin = system.clients["admin"]
    ai = post(system, "admin", "/api/audit/search", {"question": "Сәтсіз кірулерді тап"})
    assert ai.status_code == 503
    assert ai.json()["error"]["code"] == "model_unavailable"
    assert admin.get("/api/auth/me").status_code == 200
    assert admin.get("/api/grades").json()["total"] == 72
    assert admin.get("/api/documents").status_code == 200
    assert admin.get("/api/audit").status_code == 200
    assert admin.get("/api/dashboard").status_code == 200


def test_origin_and_csrf_checks_block_mutations_before_business_change(core_system):
    system = core_system
    admin = system.clients["admin"]
    denied = admin.patch(
        "/api/grades/1",
        json={"value": 99, "version": 0},
        headers={"Origin": "https://untrusted.example"},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "csrf_failed"
    denied_header = admin.patch("/api/grades/1", json={"value": 99, "version": 0})
    assert denied_header.status_code == 403
    assert denied_header.json()["error"]["code"] == "csrf_failed"
    forbidden_source = post(
        system, "admin", "/api/auth/logout", {}, **{"Sec-Fetch-Site": "cross-site"}
    )
    assert forbidden_source.status_code == 403
    assert admin.get("/api/auth/me").status_code == 200
    with system.store.connect() as connection:
        assert connection.execute("SELECT version FROM Grades WHERE grade_id=1").fetchone()[0] == 0


def test_client_ip_ignores_untrusted_forwarding_and_walks_trusted_proxy_chain(core_system):
    system = core_system
    admin = system.clients["admin"]
    assert admin.get("/api/auth/me", headers={"X-Forwarded-For": "203.0.113.1"}).status_code == 200
    with system.store.connect() as connection:
        assert (
            connection.execute(
                "SELECT last_ip FROM Sessions WHERE session_id=?", (system.sessions["admin"],)
            ).fetchone()[0]
            == "192.0.2.10"
        )
    system.settings.trusted_proxies = "192.0.2.0/24"
    response = admin.get(
        "/api/auth/me", headers={"X-Forwarded-For": "203.0.113.111, 198.51.100.77, 192.0.2.99"}
    )
    assert response.status_code == 200
    with system.store.connect() as connection:
        assert (
            connection.execute(
                "SELECT last_ip FROM Sessions WHERE session_id=?", (system.sessions["admin"],)
            ).fetchone()[0]
            == "198.51.100.77"
        )
