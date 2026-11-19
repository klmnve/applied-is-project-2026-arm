"""Lab 8: real loopback HTTP, isolated SQLite and defensive security checks.

No production database or external service is contacted. The saved evidence contains
responses only, with tokens redacted; credentials stay inside this test process.
"""

import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
import requests

from app.auth import hash_password, token_hash
from app.bootstrap import create_admin
from app.config import Settings
from app.store import Store, iso_utc

ROOT = Path(__file__).resolve().parents[1]
MISSING_ID = 9_000_000_000
PRIVATE_DIAGNOSTIC = "LAB8_SYNTHETIC_PRIVATE_EXCEPTION_MARKER"


def resource_data(code, **changes):
    return {
        "name": "Зертхана сынағының жабдығы",
        "inventory_code": code,
        "category_id": 1,
        "room_id": 1,
        "quantity": 2,
        "unit_cost": 75000.0,
    } | changes


@dataclass
class LiveSystem:
    base_url: str
    database: Path
    log_path: Path
    password: str = field(repr=False)
    teacher_password: str = field(repr=False)
    evidence: list = field(default_factory=list, repr=False)
    secret_values: set = field(default_factory=set, repr=False)

    def sanitize(self, value):
        if isinstance(value, dict):
            return {
                key: "[REDACTED]"
                if key in {"password", "password_hash", "csrf_token", "token_hash", "csrf_hash"}
                else self.sanitize(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.sanitize(item) for item in value]
        if isinstance(value, str):
            for secret in sorted(self.secret_values, key=len, reverse=True):
                if secret:
                    value = value.replace(secret, "[REDACTED]")
            return value.replace(str(self.database.parent), "[TEST_DIRECTORY]").replace(
                str(ROOT), "[PROJECT_DIRECTORY]"
            )
        return value

    def request(self, client, case, method, path, expected, **kwargs):
        response = client.request(method, self.base_url + path, timeout=15, **kwargs)
        body = response.json() if response.content else None
        self.secret_values.update(value for value in client.cookies.values() if value)
        self.evidence.append(
            {
                "case": case,
                "kind": "real_socket_http",
                "method": method,
                "path": path,
                "expected_status": expected,
                "observed_status": response.status_code,
                "status_matches": response.status_code == expected,
                "response": self.sanitize(body),
            }
        )
        assert response.status_code == expected, (
            f"{case}: expected {expected}, observed {response.status_code}"
        )
        return response

    def client(self, role=None):
        client = requests.Session()
        client.trust_env = False
        client.headers["Accept-Language"] = "kk"
        if role:
            login = "lab8_admin" if role == "admin" else "lab8_teacher"
            password = self.password if role == "admin" else self.teacher_password
            response = self.request(
                client,
                f"{role}_authentication",
                "POST",
                "/api/auth/login",
                200,
                json={"login": login, "password": password},
            )
            client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        return client

    def snapshot(self):
        with sqlite3.connect(self.database) as connection:
            return {
                "resources": connection.execute(
                    "SELECT * FROM departmentresource ORDER BY id"
                ).fetchall(),
                "resource_audit": connection.execute(
                    "SELECT * FROM AuditLogs WHERE object_type='department_resource' "
                    "ORDER BY audit_id"
                ).fetchall(),
            }


@pytest.fixture(scope="module")
def live_system(tmp_path_factory):
    directory = tmp_path_factory.mktemp("lab8-real-http")
    settings = Settings(
        _env_file=None, database_path=directory / "arm.sqlite3", upload_dir=directory / "uploads"
    )
    store = Store(settings)
    password, teacher_password = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    create_admin(store, "lab8_admin", "Синтетикалық зертхана әкімшісі", password)
    with store.connect(write=True) as connection:
        connection.execute(
            "INSERT INTO Users(login,display_name,password_hash,role_code,active,created_at,"
            "approval_status) VALUES (?,?,?,'teacher',1,?,'approved')",
            ("lab8_teacher", "Синтетикалық оқытушы", hash_password(teacher_password), iso_utc()),
        )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    log_path = directory / "uvicorn.log"
    system = LiveSystem(base_url, settings.database_path, log_path, password, teacher_password)
    system.secret_values.update({password, teacher_password, PRIVATE_DIAGNOSTIC})
    env = os.environ | {
        "DATABASE_PATH": str(settings.database_path),
        "UPLOAD_DIR": str(settings.upload_dir),
        "ALLOWED_ORIGINS": base_url,
        "LOGIN_MAX_ATTEMPTS": "100",
        "PYTHONUNBUFFERED": "1",
        "COOKIE_SECURE": "false",
    }
    process = None
    with log_path.open("w", encoding="utf-8") as log:
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "app.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--no-use-colors",
                ],
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=log,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            deadline = time.monotonic() + 45
            with requests.Session() as probe:
                probe.trust_env = False
                while time.monotonic() < deadline:
                    assert process.poll() is None, "Isolated server exited during startup"
                    try:
                        if probe.get(base_url + "/api/health", timeout=1).status_code == 200:
                            break
                    except requests.RequestException:
                        pass
                    time.sleep(0.1)
                else:
                    pytest.fail("Isolated HTTP server did not become ready")
            yield system
        finally:
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            log.flush()
            output = Path(os.getenv("LAB8_EVIDENCE_DIR", "artifacts/lab8"))
            if not output.is_absolute():
                output = ROOT / output
            output.mkdir(parents=True, exist_ok=True)
            report = {
                "generated_at_utc": datetime.now(UTC).isoformat(),
                "transport": "requests over actual loopback TCP to uvicorn",
                "database": "isolated temporary SQLite with synthetic accounts",
                "note": "Malformed input is 422 under this API contract; resources use PATCH.",
                "http_observations": sum(
                    item["kind"] == "real_socket_http" for item in system.evidence
                ),
                "observations": system.evidence,
            }
            (output / "http-integration.json").write_text(
                json.dumps(system.sanitize(report), indent=2, ensure_ascii=False), encoding="utf-8"
            )
            (output / "server-sanitized.log").write_text(
                system.sanitize(log_path.read_text(encoding="utf-8", errors="replace")),
                encoding="utf-8",
            )


@pytest.fixture(scope="module")
def admin(live_system):
    with live_system.client("admin") as client:
        yield client


@pytest.fixture(scope="module")
def teacher(live_system):
    with live_system.client("teacher") as client:
        yield client


def create_resource(system, client, code, **changes):
    return system.request(
        client,
        f"create_{code}",
        "POST",
        "/api/resources",
        201,
        json=resource_data(code, **changes),
    ).json()["item"]


def test_http_get_lists_and_reference_relationships(live_system, admin):
    listing = live_system.request(admin, "list_resources", "GET", "/api/resources", 200).json()
    categories = live_system.request(
        admin, "list_categories", "GET", "/api/resource-categories", 200
    ).json()["items"]
    rooms = live_system.request(admin, "list_rooms", "GET", "/api/resource-rooms", 200).json()[
        "items"
    ]
    assert listing["total"] >= 4 and listing["items"]
    assert all(
        row["category_id"] in {item["id"] for item in categories} for row in listing["items"]
    )
    assert all(row["room_id"] in {item["id"] for item in rooms} for row in listing["items"])


def test_http_post_commits_resource_and_audit_to_database(live_system, admin):
    item = create_resource(live_system, admin, "LAB8-CREATE")
    with sqlite3.connect(live_system.database) as connection:
        stored = connection.execute(
            "SELECT name,quantity,unit_cost FROM departmentresource WHERE id=?", (item["id"],)
        ).fetchone()
        audit = connection.execute(
            "SELECT user_id,action FROM AuditLogs WHERE object_type='department_resource' "
            "AND object_id=?",
            (str(item["id"]),),
        ).fetchall()
    assert stored == (item["name"], 2, 75000.0)
    assert audit == [("lab8_admin", "resource_create")]


def test_http_patch_zero_boundary_preserves_omitted_fields(live_system, admin):
    item = create_resource(live_system, admin, "LAB8-PATCH")
    updated = live_system.request(
        admin,
        "patch_quantity_to_zero",
        "PATCH",
        f"/api/resources/{item['id']}",
        200,
        json={"quantity": 0, "unit_cost": 0.0},
    ).json()["item"]
    assert updated == item | {"quantity": 0, "unit_cost": 0.0}
    read = live_system.request(
        admin, "read_after_patch", "GET", f"/api/resources/{item['id']}", 200
    ).json()["item"]
    assert read == updated


def test_http_delete_is_physical_and_repeat_returns_404(live_system, admin):
    item = create_resource(live_system, admin, "LAB8-DELETE")
    path = f"/api/resources/{item['id']}"
    assert not live_system.request(admin, "delete_resource", "DELETE", path, 204).content
    live_system.request(admin, "read_deleted_resource", "GET", path, 404)
    live_system.request(admin, "patch_deleted_resource", "PATCH", path, 404, json={"quantity": 1})
    with sqlite3.connect(live_system.database) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM departmentresource WHERE id=?", (item["id"],)
            ).fetchone()
            is None
        )


def test_http_filtering_combines_search_cost_room_category_and_page(live_system, admin):
    item = create_resource(live_system, admin, "LAB8-FILTER", unit_cost=150.0)
    result = live_system.request(
        admin,
        "combined_resource_filters",
        "GET",
        "/api/resources",
        200,
        params={
            "search": "lab8-filter",
            "min_cost": 150,
            "max_cost": 150,
            "category_id": 1,
            "room_id": 1,
            "page": 1,
            "page_size": 1,
        },
    ).json()
    assert result["total"] == result["pages"] == 1
    assert result["items"] == [item]


def test_http_nonexistent_resource_and_user_ids_return_404(live_system, admin):
    live_system.request(admin, "missing_resource_id", "GET", f"/api/resources/{MISSING_ID}", 404)
    live_system.request(admin, "missing_user_id", "GET", f"/api/admin/teachers/{MISSING_ID}", 404)


def test_http_put_changes_role_permissions_and_restores_original(live_system, admin):
    roles = live_system.request(
        admin, "read_role_permissions", "GET", "/api/admin/roles", 200
    ).json()
    permissions = next(role["permissions"] for role in roles["roles"] if role["code"] == "auditor")
    changed = sorted(set(permissions) | {"Documents.View"})
    assert changed != sorted(permissions)
    try:
        result = live_system.request(
            admin,
            "put_changed_auditor_permissions",
            "PUT",
            "/api/admin/roles/auditor/permissions",
            200,
            json={"permissions": changed},
        ).json()
        assert result == {"role_code": "auditor", "permissions": changed}
        with sqlite3.connect(live_system.database) as connection:
            stored = [
                row[0]
                for row in connection.execute(
                    "SELECT permission_code FROM RolePermissions "
                    "WHERE role_code='auditor' ORDER BY permission_code"
                )
            ]
        assert stored == changed
    finally:
        live_system.request(
            admin,
            "restore_auditor_permissions",
            "PUT",
            "/api/admin/roles/auditor/permissions",
            200,
            json={"permissions": permissions},
        )


@pytest.mark.parametrize(
    "case,changes",
    [
        ("empty_body", None),
        ("negative_quantity", {"quantity": -1}),
        ("null_name", {"name": None}),
        ("boolean_quantity", {"quantity": True}),
        ("forged_system_id", {"id": 99}),
    ],
)
def test_http_rejects_invalid_input_without_resource_changes(live_system, admin, case, changes):
    before = live_system.snapshot()
    body = {} if changes is None else resource_data("LAB8-INVALID", **changes)
    response = live_system.request(admin, case, "POST", "/api/resources", 422, json=body)
    assert response.json()["error"]["code"] == "invalid_request"
    assert live_system.snapshot() == before


def test_http_malformed_json_is_422_and_does_not_echo_private_input(live_system, admin):
    marker = "LAB8_PRIVATE_MALFORMED_BODY"
    live_system.secret_values.add(marker)
    response = live_system.request(
        admin,
        "malformed_json",
        "POST",
        "/api/resources",
        422,
        data='{"name":"' + marker + '",',
        headers={"Content-Type": "application/json"},
    )
    assert marker not in response.text
    assert response.json()["error"]["code"] == "invalid_request"


def test_http_empty_password_is_rejected(live_system):
    with live_system.client() as client:
        live_system.request(
            client,
            "empty_password",
            "POST",
            "/api/auth/login",
            422,
            json={"login": "lab8_admin", "password": ""},
        )
        assert not client.cookies


def test_http_short_registration_password_is_rejected(live_system):
    with live_system.client() as client:
        live_system.request(
            client,
            "weak_short_password",
            "POST",
            "/api/auth/register",
            422,
            json={"login": "lab8_weak", "display_name": "Синтетикалық өтінім", "password": "short"},
        )
    with sqlite3.connect(live_system.database) as connection:
        assert connection.execute("SELECT 1 FROM Users WHERE login='lab8_weak'").fetchone() is None


def test_http_unknown_and_wrong_login_have_same_safe_error(live_system):
    with live_system.client() as client:
        unknown = live_system.request(
            client,
            "unknown_login",
            "POST",
            "/api/auth/login",
            401,
            json={"login": "lab8_missing_user", "password": "wrong-synthetic-password"},
        )
        wrong = live_system.request(
            client,
            "wrong_password",
            "POST",
            "/api/auth/login",
            401,
            json={"login": "lab8_admin", "password": "wrong-synthetic-password"},
        )
        assert unknown.json() == wrong.json()
        assert not client.cookies


def test_http_unauthenticated_protected_request_is_401(live_system):
    with live_system.client() as client:
        live_system.request(client, "anonymous_resources", "GET", "/api/resources", 401)


def test_http_ordinary_teacher_can_read_but_cannot_administer_resources(live_system, teacher):
    live_system.request(teacher, "teacher_read_resources", "GET", "/api/resources", 200)
    before = live_system.snapshot()
    live_system.request(
        teacher,
        "teacher_admin_write_denied",
        "POST",
        "/api/resources",
        403,
        json=resource_data("LAB8-DENIED"),
    )
    assert live_system.snapshot() == before


def test_http_mutation_without_csrf_is_denied(live_system, admin):
    before = live_system.snapshot()
    response = live_system.request(
        admin,
        "missing_csrf_header",
        "POST",
        "/api/resources",
        403,
        json=resource_data("LAB8-NO-CSRF"),
        headers={"X-CSRF-Token": None},
    )
    assert response.json()["error"]["code"] == "csrf_failed"
    assert live_system.snapshot() == before


def test_http_unique_conflict_leaves_resource_and_audit_unchanged(live_system, admin):
    create_resource(live_system, admin, "LAB8-DUPLICATE")
    before = live_system.snapshot()
    response = live_system.request(
        admin,
        "unique_inventory_conflict",
        "POST",
        "/api/resources",
        409,
        json=resource_data("LAB8-DUPLICATE", name="Басқа атау"),
    )
    assert response.json()["error"]["code"] == "resource_conflict"
    assert live_system.snapshot() == before


def test_database_required_field_failure_rolls_back_entire_transaction(live_system):
    before = live_system.snapshot()
    connection = sqlite3.connect(live_system.database)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO departmentresource"
            "(name,inventory_code,category_id,room_id,quantity,unit_cost) "
            "VALUES (?,?,1,1,1,10)",
            ("Уақытша жабдық", "LAB8-ROLLBACK-VALID"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO departmentresource"
                "(name,inventory_code,category_id,room_id,quantity,unit_cost) "
                "VALUES (NULL,?,1,1,1,10)",
                ("LAB8-ROLLBACK-INVALID",),
            )
        connection.rollback()
        assert not connection.in_transaction
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()
    assert live_system.snapshot() == before
    live_system.evidence.append(
        {
            "case": "required_field_transaction_rollback",
            "kind": "database",
            "rollback_confirmed": True,
            "rows_unchanged": True,
            "integrity_check": "ok",
            "foreign_key_violations": 0,
        }
    )


def test_http_audit_failure_rolls_back_and_hides_exception_from_uvicorn_log(live_system, admin):
    before = live_system.snapshot()
    # Controlled fault injection only in this disposable test database. The SQL
    # trigger is removed in finally; it never touches a user's working database.
    with sqlite3.connect(live_system.database) as connection:
        connection.execute(
            "CREATE TRIGGER lab8_fail_resource_audit BEFORE INSERT ON AuditLogs "
            "WHEN NEW.action='resource_create' BEGIN "
            "SELECT RAISE(ABORT,'LAB8_SYNTHETIC_PRIVATE_EXCEPTION_MARKER'); END"
        )
    try:
        response = live_system.request(
            admin,
            "audit_write_fault",
            "POST",
            "/api/resources",
            500,
            json=resource_data("LAB8-AUDIT-ROLLBACK"),
        )
        assert response.json()["error"]["code"] == "server_error"
        assert PRIVATE_DIAGNOSTIC not in response.text
    finally:
        with sqlite3.connect(live_system.database) as connection:
            connection.execute("DROP TRIGGER IF EXISTS lab8_fail_resource_audit")
    assert live_system.snapshot() == before
    # Uvicorn may close the affected connection after a downstream exception.
    with live_system.client() as probe:
        live_system.request(probe, "healthy_after_rollback", "GET", "/api/health", 200)
    log_text = live_system.log_path.read_text(encoding="utf-8", errors="replace")
    leaked = PRIVATE_DIAGNOSTIC in log_text
    live_system.evidence.append(
        {
            "case": "audit_failure_log_privacy",
            "kind": "security",
            "rollback_confirmed": True,
            "response_is_generic": True,
            "private_exception_marker_in_log": leaked,
        }
    )
    assert not leaked, "Synthetic private exception details leaked into Uvicorn logs"
    assert "Request failed: IntegrityError" in log_text


def test_http_cookies_hashes_and_logs_do_not_retain_raw_credentials(live_system, admin):
    raw_session = admin.cookies["arm_session"]
    raw_csrf = admin.cookies["arm_csrf"]
    with sqlite3.connect(live_system.database) as connection:
        stored = connection.execute(
            "SELECT token_hash,csrf_hash FROM Sessions WHERE token_hash=?",
            (token_hash(raw_session),),
        ).fetchone()
        password_hash = connection.execute(
            "SELECT password_hash FROM Users WHERE login='lab8_admin'"
        ).fetchone()[0]
        audit_text = json.dumps(connection.execute("SELECT * FROM AuditLogs").fetchall())
    assert stored == (token_hash(raw_session), token_hash(raw_csrf))
    assert password_hash.startswith("scrypt$") and live_system.password not in password_hash
    log_text = live_system.log_path.read_text(encoding="utf-8", errors="replace")
    secrets_absent = all(
        value not in log_text and value not in audit_text
        for value in (live_system.password, live_system.teacher_password, raw_session, raw_csrf)
    )
    assert secrets_absent, "Raw synthetic credentials must not enter logs or audit records"
    live_system.evidence.append(
        {
            "case": "credential_storage_and_logging",
            "kind": "security",
            "password_is_scrypt": True,
            "session_tokens_are_hashed": True,
            "raw_secrets_absent_in_logs_and_audit": True,
        }
    )


def test_http_logout_revokes_the_same_token(live_system):
    with live_system.client("admin") as client:
        raw_session = client.cookies["arm_session"]
        live_system.request(client, "logout", "POST", "/api/auth/logout", 200)
        live_system.request(client, "after_logout", "GET", "/api/resources", 401)
        client.cookies.set("arm_session", raw_session)
        live_system.request(client, "revoked_token_reuse", "GET", "/api/resources", 401)
