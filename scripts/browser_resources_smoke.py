"""Exercise Streamlit through a real browser against an isolated ARM database."""

import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import httpx
from playwright.sync_api import Page, expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.bootstrap import create_admin  # noqa: E402
from app.config import Settings  # noqa: E402
from app.store import Store  # noqa: E402


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_ready(url: str, process: subprocess.Popen[str]) -> None:
    for _ in range(120):
        if process.poll() is not None:
            raise RuntimeError("A smoke server stopped before it became ready")
        try:
            if httpx.get(url, timeout=2, trust_env=False).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise RuntimeError("Smoke server readiness timed out")


def select_first_option(page: Page, name: str) -> None:
    page.get_by_role("combobox", name=name, exact=True).click()
    page.get_by_role("option").first.click()


def run_browser(
    page: Page, frontend: str, backend: str, password: str, output: Path
) -> dict[str, Any]:
    page.set_default_timeout(20000)
    expect.set_options(timeout=20000)
    page.goto(frontend)
    page.get_by_label("Логин", exact=True).fill("browser_resource_admin")
    page.get_by_label("Құпиясөз", exact=True).fill(password)
    page.get_by_role("button", name="Кіру", exact=True).click()
    expect(page.get_by_text("Жабдықтарды басқару", exact=True)).to_be_visible()
    expect(page.get_by_test_id("stDataFrame")).to_be_visible()
    page.screenshot(path=str(output / "resources-desktop.png"), full_page=True)

    page.get_by_role("tab", name="Қосу", exact=True).click()
    add = page.get_by_role("tabpanel", name="Қосу", exact=True)
    add.get_by_label("Жабдық атауы", exact=True).fill("Браузер сынағының мониторы")
    add.get_by_label("Түгендеу коды", exact=True).fill("BROWSER-LAB-001")
    # The selectbox popovers are rendered outside their containing tab.
    add.get_by_role("combobox", name="Санат", exact=True).click()
    page.get_by_role("option").first.click()
    add.get_by_role("combobox", name="Бөлме", exact=True).click()
    page.get_by_role("option").first.click()
    add.get_by_role("spinbutton", name="Саны", exact=True).fill("2")
    add.get_by_role("spinbutton", name="Бірлік құны, ₸", exact=True).fill("75000")
    add.get_by_role("button", name="Жабдық қосу", exact=True).click()
    expect(page.get_by_text("Жабдық қосылды. Тізім жаңартылды.", exact=True)).to_be_visible()

    search = page.get_by_label("Атауы немесе түгендеу коды", exact=True)
    search.fill("BROWSER-LAB-001")
    search.press("Enter")
    expect(page.get_by_text("Табылды: 1 · Бет: 1 / 1", exact=True)).to_be_visible()
    page.get_by_role("tab", name="Өзгерту", exact=True).click()
    edit = page.get_by_role("tabpanel", name="Өзгерту", exact=True)
    edit.get_by_role("spinbutton", name="Саны", exact=True).fill("7")
    edit.get_by_role("button", name="Өзгерісті сақтау", exact=True).click()
    expect(page.get_by_text("Өзгерістер сақталды. Тізім жаңартылды.", exact=True)).to_be_visible()

    with httpx.Client(base_url=backend, trust_env=False, timeout=20) as api:
        response = api.post(
            "/api/auth/login", json={"login": "browser_resource_admin", "password": password}
        )
        assert response.status_code == 200
        items = api.get("/api/resources", params={"search": "BROWSER-LAB-001"}).json()["items"]
        assert len(items) == 1 and items[0]["quantity"] == 7
        assert items[0]["unit_cost"] == 75000 and items[0]["name"] == "Браузер сынағының мониторы"

        page.get_by_role("tab", name="Жою", exact=True).click()
        delete = page.get_by_role("tabpanel", name="Жою", exact=True)
        expect(delete.get_by_role("button", name="Жабдықты жою", exact=True)).to_be_disabled()
        delete.get_by_text("Осы жабдықты жоюды растаймын", exact=True).click()
        expect(delete.get_by_role("checkbox")).to_be_checked()
        delete.get_by_role("button", name="Жабдықты жою", exact=True).click()
        expect(page.get_by_text("Жабдық жойылды. Тізім жаңартылды.", exact=True)).to_be_visible()
        expect(page.get_by_text("Табылды: 0 · Бет: 1 / 1", exact=True)).to_be_visible()
        assert api.get(f"/api/resources/{items[0]['id']}").status_code == 404

    page.get_by_role("button", name="Шығу", exact=True).click()
    expect(page.get_by_role("button", name="Кіру", exact=True)).to_be_visible()
    # A fresh mobile session lets Streamlit choose its native responsive sidebar.
    mobile = page.context.new_page()
    try:
        mobile.set_viewport_size({"width": 390, "height": 844})
        mobile.goto(frontend)
        mobile.get_by_label("Логин", exact=True).fill("browser_resource_admin")
        mobile.get_by_label("Құпиясөз", exact=True).fill(password)
        mobile.get_by_role("button", name="Кіру", exact=True).click()
        expect(mobile.get_by_text("Жабдықтарды басқару", exact=True)).to_be_visible()
        expect(mobile.get_by_test_id("stDataFrame")).to_be_visible()
        expect(mobile.get_by_role("button", name="Stop", exact=True)).not_to_be_visible()
        mobile.screenshot(path=str(output / "resources-mobile.png"), full_page=True)
        assert mobile.evaluate("document.documentElement.scrollWidth <= innerWidth + 2")
    finally:
        mobile.close()
    return {
        "login": True,
        "create": True,
        "search": True,
        "patch": True,
        "delete": True,
        "logout": True,
        "mobile_no_page_overflow": True,
        "real_http_backend": True,
    }


def main() -> None:
    output = ROOT / "artifacts" / "lab5-7" / "browser"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="arm-resource-ui-", ignore_cleanup_errors=True) as temp:
        directory = Path(temp)
        settings = Settings.model_validate(
            {"database_path": directory / "test.db", "upload_dir": directory / "uploads"}
        )
        password = secrets.token_urlsafe(32)
        create_admin(
            Store(settings), "browser_resource_admin", "Browser smoke administrator", password
        )
        backend_port, frontend_port = free_port(), free_port()
        while frontend_port == backend_port:
            frontend_port = free_port()
        backend = f"http://127.0.0.1:{backend_port}"
        frontend = f"http://127.0.0.1:{frontend_port}"
        env = os.environ | {
            "DATABASE_PATH": str(settings.database_path),
            "UPLOAD_DIR": str(settings.upload_dir),
            "BACKEND_URL": backend,
            "PUBLIC_BACKEND_URL": backend,
            "ALLOWED_ORIGINS": backend,
        }
        processes: list[subprocess.Popen[str]] = []
        with ExitStack() as stack:
            try:
                for service, command in (
                    (
                        "backend",
                        [
                            "-m",
                            "uvicorn",
                            "app.main:app",
                            "--host",
                            "127.0.0.1",
                            "--port",
                            str(backend_port),
                        ],
                    ),
                    (
                        "frontend",
                        [
                            "-m",
                            "streamlit",
                            "run",
                            "app/frontend/ui.py",
                            "--server.address=127.0.0.1",
                            f"--server.port={frontend_port}",
                            "--server.headless=true",
                        ],
                    ),
                ):
                    log = stack.enter_context(
                        (output / f"{service}.log").open("w", encoding="utf-8")
                    )
                    process = subprocess.Popen(
                        [sys.executable, *command],
                        cwd=ROOT,
                        env=env,
                        stdout=log,
                        stderr=log,
                        text=True,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    )
                    processes.append(process)
                wait_ready(backend + "/api/health", processes[0])
                wait_ready(frontend + "/_stcore/health", processes[1])
                with sync_playwright() as playwright:
                    chrome = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
                    browser = playwright.chromium.launch(
                        headless=True, executable_path=str(chrome) if chrome.exists() else None
                    )
                    try:
                        page = browser.new_page(viewport={"width": 1440, "height": 1000})
                        errors: list[str] = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        result = run_browser(page, frontend, backend, password, output)
                        assert not errors, errors
                        result["page_errors"] = errors
                        result["status"] = "passed"
                        (output / "result.json").write_text(
                            json.dumps(result, indent=2), encoding="utf-8"
                        )
                        print(json.dumps(result))
                    finally:
                        browser.close()
            finally:
                for process in reversed(processes):
                    process.terminate()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)


if __name__ == "__main__":
    main()
