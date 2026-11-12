"""Run the real Streamlit page against an isolated HTTP stub."""

import copy
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests

from app.frontend.client import APIClient, APIError

UI_PATH = Path(__file__).resolve().parents[1] / "app" / "frontend" / "ui.py"


class Backend:
    def __init__(self):
        self.user = {
            "display_name": "Зертхана әкімшісі",
            "role_code": "admin",
            "permissions": ["Documents.View", "Users.Manage"],
        }
        self.items = [
            {
                "id": 1,
                "name": "Проектор",
                "inventory_code": "ARM-001",
                "category_id": 1,
                "room_id": 1,
                "quantity": 2,
                "unit_cost": 250000,
                "category_name": "Техника",
                "room_name": "101 кабинет",
            }
        ]
        self.sessions = []
        self.offline = False

    def session(self):
        value = FakeSession(self)
        self.sessions.append(value)
        return value


class FakeSession:
    def __init__(self, backend):
        self.backend = backend
        self.headers = {}
        self.cookies = requests.cookies.RequestsCookieJar()
        self.calls = []
        self.closed = False

    def close(self):
        self.closed = True

    def request(self, method, url, **kwargs):
        self.calls.append((method.upper(), urlsplit(url).path, copy.deepcopy(kwargs)))
        if self.backend.offline:
            raise requests.ConnectionError("private transport detail")
        status, payload = self.handle(method.upper(), urlsplit(url).path, kwargs)
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(payload).encode()
        return response

    def handle(self, method, path, kwargs):
        if path == "/api/auth/login":
            if kwargs["json"] != {"login": "admin", "password": "test-password"}:
                return 401, {"error": {"message": "Логин немесе құпиясөз қате."}}
            self.cookies.set("arm_session", "session-token")
            self.cookies.set("arm_csrf", "csrf-token")
            return 200, {"user": self.backend.user, "csrf_token": "csrf-token"}
        if not self.cookies.get("arm_session"):
            return 401, {"error": {"message": "Сессия аяқталды."}}
        if method not in {"GET", "HEAD"}:
            assert kwargs["headers"]["X-CSRF-Token"] == "csrf-token"
        if path == "/api/auth/me":
            return 200, {"user": self.backend.user}
        if path == "/api/auth/logout":
            self.cookies.clear()
            return 200, {"message": "Жүйеден шықтыңыз."}
        if path == "/api/resource-categories":
            return 200, {"items": [{"id": 1, "name": "Техника"}]}
        if path == "/api/resource-rooms":
            return 200, {"items": [{"id": 1, "name": "101 кабинет"}]}
        if path == "/api/resources" and method == "GET":
            params = kwargs.get("params", {})
            search = params.get("search", "").casefold()
            items = [item for item in self.backend.items if search in item["name"].casefold()]
            return 200, {
                "items": copy.deepcopy(items),
                "total": len(items),
                "page": 1,
                "page_size": 25,
                "pages": 1,
            }
        if path == "/api/resources" and method == "POST":
            item = dict(kwargs["json"], id=max(item["id"] for item in self.backend.items) + 1)
            item.update(category_name="Техника", room_name="101 кабинет")
            self.backend.items.append(item)
            return 201, {"item": item}
        if path.startswith("/api/resources/"):
            item_id = int(path.rsplit("/", 1)[1])
            item = next((item for item in self.backend.items if item["id"] == item_id), None)
            if item is None:
                return 404, {"error": {"message": "Жабдық табылмады."}}
            if method == "PATCH":
                item.update(kwargs["json"])
            if method == "DELETE":
                self.backend.items.remove(item)
                return 204, None
            return 200, {"item": copy.deepcopy(item)}
        raise AssertionError(f"Unexpected endpoint {method} {path}")


@pytest.fixture
def backend(monkeypatch):
    value = Backend()
    monkeypatch.setattr("app.frontend.client.requests.Session", value.session)
    return value


def test_rest_client_csrf_timeouts_and_clear_credentials(backend):
    client = APIClient("http://backend:8000/")
    client.request("POST", "/api/auth/login", json={"login": "admin", "password": "test-password"})
    client.request("PATCH", "/api/resources/1", json={"quantity": 3})
    _, _, kwargs = backend.sessions[0].calls[-1]
    assert kwargs["headers"] == {"X-CSRF-Token": "csrf-token"}
    assert kwargs["timeout"] == (5, 20)
    assert kwargs["allow_redirects"] is False
    assert backend.items[0]["quantity"] == 3
    client.close()
    assert not backend.sessions[0].cookies and backend.sessions[0].closed


def test_rest_client_transport_error_hides_internal_details(backend):
    backend.offline = True
    with pytest.raises(APIError) as exc:
        APIClient("http://backend:8000").request("GET", "/api/resources")
    assert "private transport detail" not in str(exc.value)
    assert "Серверге қосылу" in str(exc.value)


def app():
    from streamlit.testing.v1 import AppTest

    return AppTest.from_file(str(UI_PATH), default_timeout=20).run()


def login(at):
    at.text_input(key="login_name").input("admin")
    at.text_input(key="login_password").input("test-password")
    at.button(key="login_submit").click().run()
    assert not at.exception
    return at


def test_ui_login_search_and_logout(backend):
    at = login(app())
    assert at.dataframe[0].value.iloc[0]["Атауы"] == "Проектор"
    at.text_input(key="search").input("жоқ ресурс").run()
    assert any("табылмады" in item.value for item in at.info)
    assert any(
        call[2].get("params", {}).get("search") == "жоқ ресурс"
        for call in backend.sessions[0].calls
    )
    at.button(key="logout").click().run()
    assert at.text_input(key="login_name")
    assert backend.sessions[0].closed and not backend.sessions[0].cookies


def test_ui_create_edit_delete_refreshes_table(backend):
    at = login(app())
    at.text_input(key="add_name").input("Монитор")
    at.text_input(key="add_inventory").input("ARM-002")
    at.selectbox(key="add_category").select(1)
    at.selectbox(key="add_room").select(1)
    at.number_input(key="add_quantity").set_value(5)
    at.number_input(key="add_cost").set_value(75000.0)
    at.button(key="add_submit").click().run()
    assert not at.exception
    assert len(backend.items) == len(at.dataframe[0].value) == 2
    assert any("қосылды" in item.value for item in at.success)
    at.selectbox(key="edit_resource").select(2).run()
    at.text_input(key="edit_2_name").input("Жаңа монитор")
    at.button(key="edit_submit").click().run()
    assert backend.items[1]["name"] == "Жаңа монитор"
    patch = next(call for call in reversed(backend.sessions[0].calls) if call[0] == "PATCH")
    assert patch[2]["json"] == {"name": "Жаңа монитор"}
    at.selectbox(key="delete_resource").select(2).run()
    at.checkbox(key="delete_confirm_2").check().run()
    at.button(key="delete_button_2").click().run()
    assert not at.exception
    assert len(backend.items) == len(at.dataframe[0].value) == 1


def test_ui_expired_session_clears_secrets_and_returns_to_login(backend):
    at = login(app())
    old_session = backend.sessions[0]
    old_session.cookies.clear()
    at.run()
    assert not at.exception and at.text_input(key="login_name")
    assert old_session.closed
    assert any("қайта кіріңіз" in item.value.lower() for item in at.warning)


def test_ui_offline_and_live_permission_changes(backend):
    at = login(app())
    backend.offline = True
    at.run()
    assert not at.exception and "Серверге қосылу" in at.error[0].value
    backend.offline = False
    backend.user.update(role_code="teacher", permissions=["Documents.View"])
    at.run()
    assert not at.exception and at.dataframe
    assert not any(item.key == "add_name" for item in at.text_input)
    backend.user.update(role_code="pending", permissions=[], approval_status="pending")
    at.run()
    assert not at.dataframe
    assert any("мақұлдауын күтуде" in item.value for item in at.info)


def test_ui_browser_sessions_do_not_share_credentials(backend):
    first = login(app())
    second = app()
    assert first.dataframe and second.text_input(key="login_name")
    assert len(backend.sessions) == 2
    assert backend.sessions[0].cookies.get("arm_session")
    assert not backend.sessions[1].cookies.get("arm_session")


def test_ui_bad_password_and_invalid_form_do_not_mutate_data(backend):
    at = app()
    at.text_input(key="login_name").input("admin")
    at.text_input(key="login_password").input("wrong-password")
    at.button(key="login_submit").click().run()
    assert at.error and not at.dataframe
    at = login(at)
    at.button(key="add_submit").click().run()
    assert at.error and len(backend.items) == 1
    assert not any(
        call[0] == "POST" and call[1] == "/api/resources" for call in backend.sessions[0].calls
    )


def test_ui_switching_delete_target_requires_fresh_confirmation(backend):
    backend.items.append(dict(backend.items[0], id=2, name="Монитор", inventory_code="ARM-002"))
    at = login(app())
    at.checkbox(key="delete_confirm_1").check().run()
    assert not at.button(key="delete_button_1").disabled
    at.selectbox(key="delete_resource").select(2).run()
    assert not at.checkbox(key="delete_confirm_2").value
    at.selectbox(key="delete_resource").select(1).run()
    assert not at.checkbox(key="delete_confirm_1").value
    assert at.button(key="delete_button_1").disabled
    assert len(backend.items) == 2


def test_ui_refresh_discards_stale_editor_snapshot(backend):
    at = login(app())
    assert at.text_input(key="edit_1_name").value == "Проектор"
    backend.items[0]["name"] = "Жаңартылған проектор"
    at.button(key="refresh").click().run()
    assert not at.exception
    assert at.text_input(key="edit_1_name").value == "Жаңартылған проектор"
