"""Exercise the real app in an isolated database and a fresh headless browser.

No credentials are saved or printed. The temporary database/uploads are removed;
screenshots and a server log remain under artifacts/browser-smoke.
"""

import argparse
import json
import os
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from contextlib import closing
from pathlib import Path

import httpx
from playwright.sync_api import BrowserContext, Page, expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.bootstrap import create_admin  # noqa: E402
from app.config import Settings  # noqa: E402
from app.store import Store  # noqa: E402


def find_browser(requested: str | None) -> str | None:
    if requested:
        path = Path(requested).resolve()
        if not path.is_file():
            raise FileNotFoundError("The specified browser executable does not exist")
        return str(path)
    for path in (
        Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
        Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
        Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
    ):
        if path.is_file():
            return str(path)
    return None  # Playwright's separately installed Chromium, if available.


def csrf(context: BrowserContext) -> dict[str, str]:
    token = next(cookie["value"] for cookie in context.cookies() if cookie["name"] == "arm_csrf")
    return {"X-CSRF-Token": token}


def login(page: Page, login_name: str, password: str) -> None:
    page.locator("#login").fill(login_name)
    page.locator("#password").fill(password)
    page.locator("#auth-submit").click()
    expect(page.locator("#app-screen")).to_be_visible()
    expect(page.locator("#page-content")).to_have_attribute("aria-busy", "false")
    dismiss_help(page)


def dismiss_help(page: Page) -> None:
    help_dialog = page.locator('dialog[open][data-context-help="true"]')
    if help_dialog.count():
        help_dialog.locator(".dialog-footer button").click()
        expect(help_dialog).to_have_count(0)


def navigate(page: Page, route: str) -> None:
    dismiss_help(page)
    if page.locator("#nav-toggle").is_visible():
        page.locator("#nav-toggle").click()
    page.locator(f".nav-link[href='#{route}']").click()
    expect(page).to_have_url(re.compile(rf"#{route}$"))
    expect(page.locator(f".nav-link[href='#{route}']")).to_have_attribute("aria-current", "page")
    expect(page.locator("#page-content")).to_have_attribute("aria-busy", "false")
    dismiss_help(page)
    if page.locator(".error-panel").count():
        raise AssertionError(f"Page {route} failed: {page.locator('.error-panel').inner_text()}")


def dismiss_toasts(page: Page) -> None:
    for _ in range(page.locator("#toast-region button").count()):
        page.locator("#toast-region button").first.click()


def run_browser(
    origin: str, admin_password: str, work: Path, artifacts: Path, browser_path: str | None
) -> dict[str, object]:
    errors: list[str] = []
    expect.set_options(timeout=20000)
    teacher_login = "browser_teacher_" + secrets.token_hex(3)
    teacher_password = secrets.token_urlsafe(24)
    student_login = "browser_student_" + secrets.token_hex(3)
    student_password = secrets.token_urlsafe(24)
    catalog = json.loads((ROOT / "app/static/locales.json").read_text(encoding="utf-8"))
    with closing(sqlite3.connect(work / "audit.db")) as fixture_connection:
        fixture_connection.row_factory = sqlite3.Row
        student_fixture = dict(
            fixture_connection.execute("SELECT * FROM Students WHERE student_id=1").fetchone()
        )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=browser_path, headless=True)
        admin_context = browser.new_context(
            base_url=origin, viewport={"width": 1440, "height": 1000}
        )
        teacher_context = browser.new_context(
            base_url=origin, viewport={"width": 1440, "height": 1000}
        )
        student_context = browser.new_context(
            base_url=origin, viewport={"width": 1440, "height": 1000}
        )
        page = admin_context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        teacher_page = teacher_context.new_page()
        teacher_page.on("pageerror", lambda error: errors.append(str(error)))
        student_page = student_context.new_page()
        student_page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(origin)
        expect(page.locator("#auth-screen")).to_be_visible()
        example_requests: list[object] = []
        page.on(
            "request",
            lambda request: (
                example_requests.append(request)
                if request.url.endswith("/api/audit/search")
                else None
            ),
        )
        for language in ("ru", "en", "kk"):
            page.locator("#locale-auth").select_option(language)
            page.reload()
            expect(page.locator("html")).to_have_attribute("lang", language)
            expect(page.locator("#locale-auth")).to_have_value(language)
            page.locator("#register-tab").click()
            page.locator("#login").fill("InvalidLogin")
            assert page.locator("#login").evaluate("node => node.validity.patternMismatch")
            page.locator("#login").fill("valid_login-1")
            assert page.locator("#login").evaluate("node => node.checkValidity()")
            expect(page.locator("#requested-role option")).to_have_count(2)
            expect(page.locator("#requested-role option[value='student']")).to_have_text(
                catalog["role.student"][language]
            )
            page.locator("#password-toggle").click()
            expect(page.locator("#password-toggle")).to_have_attribute(
                "aria-label", catalog["Құпиясөзді жасыру"][language]
            )
            page.locator("#password-toggle").click()
            page.locator("#login-tab").click()
        page.locator("#register-tab").click()
        page.locator("#display-name").fill("Браузер тексеру оқытушысы")
        page.locator("#login").fill(teacher_login)
        page.locator("#password").fill(teacher_password)
        page.locator("#password-toggle").click()
        expect(page.locator("#password")).to_have_attribute("type", "text")
        page.locator("#password-toggle").click()
        expect(page.locator("#password")).to_have_attribute("type", "password")
        expect(page.locator("#student-claim-name")).to_be_disabled()
        page.locator("#auth-submit").click()
        expect(page.locator("#auth-message")).to_be_visible()
        expect(page.locator("#auth-title")).to_have_text("Жүйеге кіру")
        teacher_page.goto(origin)
        login(teacher_page, teacher_login, teacher_password)
        expect(teacher_page.locator(".approval-page")).to_be_visible()
        expect(teacher_page.locator(".nav-link")).to_have_count(0)
        assert teacher_context.request.get("/api/dashboard").status == 403

        student_page.goto(origin)
        student_page.locator("#locale-auth").select_option("en")
        student_page.locator("#register-tab").click()
        student_page.locator("#display-name").fill("Browser student")
        student_page.locator("#login").fill(student_login)
        student_page.locator("#password").fill(student_password)
        student_page.locator("#requested-role").select_option("student")
        expect(student_page.locator("#student-claim-group option[value='1']")).to_have_count(1)
        student_page.locator("#student-claim-name").fill(student_fixture["display_name"])
        student_page.locator("#student-claim-number").fill(student_fixture["student_code"].lower())
        student_page.locator("#student-claim-group").select_option(str(student_fixture["group_id"]))
        student_page.locator("#auth-submit").click()
        expect(student_page.locator("#auth-message")).to_be_visible()
        login(student_page, student_login, student_password)
        expect(student_page.locator(".approval-page")).to_be_visible()
        expect(student_page.locator("html")).to_have_attribute("lang", "en")
        assert student_context.request.get("/api/tasks").status == 403
        student_page.screenshot(path=str(artifacts / "approval-pending-en.png"), full_page=True)
        page.locator("#login").fill("browser_admin")
        page.locator("#password").fill(secrets.token_urlsafe(24))
        page.locator("#auth-submit").click()
        expect(page.locator("#auth-message")).to_contain_text("Логин немесе құпиясөз")
        login(page, "browser_admin", admin_password)
        kpis = admin_context.request.get("/api/dashboard").json()["kpis"]
        assert page.locator(".kpi-value").count() == len(
            [v for v in kpis.values() if v is not None]
        )
        page.screenshot(path=str(artifacts / "dashboard-desktop.png"), full_page=True)
        print(
            "PASS language persistence, registration requests, restricted login and dashboard",
            flush=True,
        )

        for route in (
            "users",
            "requests",
            "teachers",
            "catalogs",
            "students",
            "roles",
            "grades",
            "tasks",
            "documents",
            "sessions",
            "audit",
            "assistant",
            "dashboard",
        ):
            navigate(page, route)
            expect(page.locator("#page-content h1")).to_have_count(1)
        print("PASS all thirteen page routes without JavaScript errors", flush=True)

        for language in ("ru", "en", "kk"):
            page.locator("#locale-app").select_option(language)
            expect(page.locator("html")).to_have_attribute("lang", language)
            expect(page.locator("#page-content")).to_have_attribute("aria-busy", "false")
            navigate(page, "assistant")
            expect(page.locator(".expanded-examples button")).to_have_count(14)
            for index in range(14):
                example = page.locator(".expanded-examples button").nth(index)
                expect(example).to_have_text(catalog[f"ai.example.{index + 1}"][language])
                before = len(example_requests)
                with page.expect_response(
                    lambda response: (
                        response.url.endswith("/api/audit/search")
                        and response.request.method == "POST"
                    )
                ) as searched:
                    if index == 0:
                        example.evaluate(
                            "node => { node.click(); node.click(); "
                            "document.querySelector('#ai-question').form.requestSubmit(); }"
                        )
                    else:
                        example.click()
                assert searched.value.status == 503
                assert searched.value.request.post_data_json == {
                    "question": catalog[f"ai.example.{index + 1}"][language]
                }
                assert searched.value.request.headers["accept-language"] == language
                expect(page.locator(".ai-message.error")).to_have_text(
                    re.compile(re.escape(catalog["error.model_unavailable"][language]))
                )
                expect(example).to_be_enabled()
                assert len(example_requests) == before + 1
            before = len(example_requests)
            page.locator("#ai-question").fill("   ")
            page.locator(".assistant-search button[type=submit]").click()
            expect(page.locator(".ai-message.error")).to_have_text(
                catalog["error.required"][language]
            )
            assert len(example_requests) == before
            page.locator("#help-button").click()
            expect(page.locator(".context-steps li")).to_have_count(2)
            dismiss_help(page)
        automatic_examples = len(example_requests)

        navigate(page, "requests")
        for account, role in ((teacher_login, "teacher"), (student_login, "student")):
            page.get_by_role("row").filter(has_text=account).get_by_role(
                "button", name=catalog["requests.approve"]["kk"], exact=True
            ).click()
            dialog = page.get_by_role("dialog")
            if role == "student":
                dialog.get_by_label(catalog["requests.actualRole"]["kk"], exact=True).select_option(
                    role
                )
                dialog.get_by_label(catalog["requests.student"]["kk"], exact=True).select_option(
                    "1"
                )
                dialog.get_by_role(
                    "checkbox", name=catalog["claim.confirmMatch"]["kk"], exact=True
                ).check()
            else:
                expect(
                    dialog.get_by_label(catalog["requests.actualRole"]["kk"], exact=True)
                ).to_have_count(0)
            dialog.get_by_role("button", name=catalog["requests.approve"]["kk"], exact=True).click()
            expect(page.get_by_role("dialog")).to_have_count(0)
        assert admin_context.request.get("/api/admin/requests?status=pending").json()["total"] == 0
        student_page.get_by_role(
            "button", name=catalog["approval.refresh"]["en"], exact=True
        ).click()
        expect(student_page.locator(".approval-page")).to_have_count(0)
        expect(student_page.locator("#page-content")).to_have_attribute("aria-busy", "false")
        dismiss_help(student_page)
        assert student_context.request.get("/api/auth/me").json()["user"]["student_id"] == 1
        assert student_context.request.get("/api/admin/users").status == 403

        from browser_roles_smoke import run_roles_flow

        roles_result = run_roles_flow(
            origin, admin_context, artifacts, errors, navigate=navigate, dismiss_help=dismiss_help
        )

        navigate(page, "roles")
        permission = page.get_by_role("checkbox", name="Оқытушы: Құжат жүктеу", exact=True)
        permission.uncheck()
        page.locator(".permission-matrix thead th").filter(has_text="Оқытушы").get_by_role(
            "button"
        ).click()
        page.get_by_role("dialog").get_by_role(
            "button", name="Өзгерісті сақтау", exact=True
        ).click()
        expect(page.get_by_role("dialog")).to_have_count(0)
        roles = admin_context.request.get("/api/admin/roles").json()["roles"]
        assert (
            "Documents.Upload"
            not in next(row for row in roles if row["code"] == "teacher")["permissions"]
        )

        navigate(page, "users")
        teacher_row = page.get_by_role("row").filter(has_text=teacher_login)
        teacher_row.get_by_role("button", name="Пән бекіту", exact=True).click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("Оқу тобы", exact=True).select_option("1")
        dialog.get_by_label("Пән", exact=True).select_option("1")
        dialog.get_by_label("Оқу кезеңі", exact=True).select_option("1")
        dialog.get_by_role("button", name="Бекіту", exact=True).click()
        expect(page.get_by_role("dialog")).to_have_count(0)
        teacher = next(
            row
            for row in admin_context.request.get("/api/admin/users").json()["items"]
            if row["login"] == teacher_login
        )
        assert teacher["active"] and teacher["role_code"] == "teacher"
        assert any(
            row["user_id"] == teacher["user_id"]
            for row in admin_context.request.get("/api/academic/assignments").json()["items"]
        )
        print(
            "PASS role matrix, approvals, student link and teacher assignment via UI",
            flush=True,
        )

        teacher_page.get_by_role(
            "button", name=catalog["approval.refresh"]["kk"], exact=True
        ).click()
        expect(teacher_page.locator(".approval-page")).to_have_count(0)
        expect(teacher_page.locator("#page-content")).to_have_attribute("aria-busy", "false")
        dismiss_help(teacher_page)
        expect(teacher_page.locator(".nav-link[href='#users']")).to_have_count(0)
        assert teacher_context.request.get("/api/admin/users").status == 403
        assert teacher_context.request.get("/api/audit").status == 403
        fresh_documents = teacher_context.new_page()
        fresh_documents.on("pageerror", lambda error: errors.append(str(error)))
        fresh_documents.goto(origin + "/#documents")
        expect(fresh_documents.locator("#page-content")).to_have_attribute("aria-busy", "false")
        dismiss_help(fresh_documents)
        expect(
            fresh_documents.get_by_role(
                "button", name=catalog["documents.createTask"]["kk"], exact=True
            )
        ).to_be_visible()
        fresh_documents.close()
        navigate(teacher_page, "grades")
        teacher_page.get_by_label("Оқу тобы", exact=True).select_option("1")
        teacher_page.get_by_label("Пән", exact=True).select_option("1")
        teacher_page.get_by_label("Оқу кезеңі", exact=True).select_option("1")
        teacher_page.get_by_role("button", name="Қолдану", exact=True).click()
        expect(teacher_page.locator("#page-content")).to_have_attribute("aria-busy", "false")
        grades = teacher_context.request.get(
            "/api/grades?group_id=1&subject_id=1&term_id=1"
        ).json()["items"]
        grade = grades[0]
        new_value = 99 if grade["value"] != 99 else 98
        teacher_page.get_by_role("button", name="Өзгерту", exact=True).first.click()
        dialog = teacher_page.get_by_role("dialog")
        dialog.get_by_label("Жаңа баға", exact=True).fill(str(new_value))
        dialog.get_by_role("button", name="Бағаны сақтау", exact=True).click()
        expect(teacher_page.get_by_role("dialog")).to_have_count(0)
        expect(teacher_page.locator(".grade-value").first).to_have_text(str(new_value))
        teacher_page.get_by_role("button", name="Тарих", exact=True).first.click()
        expect(teacher_page.get_by_role("dialog")).to_contain_text(teacher_login)
        history = teacher_context.request.get(f"/api/grades/{grade['grade_id']}/history").json()[
            "items"
        ][0]
        assert history["old_value"] == grade["value"] and history["new_value"] == new_value
        assert history["actor_login"] == teacher_login and history["ip_address"] == "127.0.0.1"
        teacher_page.get_by_role("dialog").get_by_role("button", name="Жабу", exact=True).click()
        print("PASS teacher permission denial, scoped grade edit and recorded history", flush=True)

        navigate(teacher_page, "documents")
        expect(teacher_page.locator("#page-content")).to_contain_text(
            catalog["documents.separateFlow"]["kk"]
        )
        before_tasks = teacher_context.request.get("/api/tasks").json()["total"]
        teacher_page.get_by_role(
            "button", name=catalog["documents.createTask"]["kk"], exact=True
        ).click()
        expect(teacher_page).to_have_url(re.compile(r"#tasks$"))
        dialog = teacher_page.locator("dialog[open]").last
        expect(dialog.get_by_label(catalog["learning.deadline"]["kk"], exact=True)).to_be_visible()
        expect(
            dialog.get_by_label(catalog["learning.initialMaterial"]["kk"], exact=True)
        ).to_be_visible()
        dialog.locator(".dialog-close").click()
        expect(teacher_page.locator("dialog[open]")).to_have_count(0)
        assert teacher_context.request.get("/api/tasks").json()["total"] == before_tasks

        from browser_learning_smoke import run_learning_flow

        learning_result = run_learning_flow(
            origin,
            admin_context,
            teacher_context,
            teacher_page,
            student_context,
            student_page,
            artifacts,
            errors,
            navigate=navigate,
            dismiss_help=dismiss_help,
            database_path=work / "audit.db",
        )
        student_page.set_viewport_size({"width": 390, "height": 844})
        for language in ("ru", "en"):
            student_page.locator("#locale-app").select_option(language)
            expect(student_page.locator("#page-content")).to_have_attribute("aria-busy", "false")
            expect(student_page.locator("#page-content h1")).to_have_text(
                catalog["learning.title"][language]
            )
            assert student_page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        student_page.reload()
        expect(student_page.locator("#locale-app")).to_have_value("en")
        expect(student_page.locator("#page-content")).to_have_attribute("aria-busy", "false")
        student_page.screenshot(path=str(artifacts / "learning-student-mobile-en.png"))

        navigate(page, "audit")
        page.get_by_label("Пайдаланушы", exact=True).fill(teacher_login)
        page.get_by_label("Әрекет", exact=True).select_option("grade_change")
        page.get_by_role("button", name="Сүзгілеу", exact=True).click()
        expect(page.locator("#page-content")).to_have_attribute("aria-busy", "false")
        dismiss_toasts(page)
        page.get_by_role("button", name=f"#{history['audit_id']}", exact=True).click()
        dialog = page.get_by_role("dialog")
        expect(dialog).to_contain_text(teacher_login)
        expect(dialog).to_contain_text("127.0.0.1")
        expect(dialog).to_contain_text("Алдыңғы мән")
        expect(dialog).to_contain_text("Жаңа мән")
        page.screenshot(path=str(artifacts / "audit-detail.png"), full_page=True)
        audit = admin_context.request.get(f"/api/audit/{history['audit_id']}").json()
        assert audit["old_value"] == grade["value"] and audit["new_value"] == new_value
        dialog.get_by_role("button", name="Жабу", exact=True).click()

        navigate(page, "documents")
        page.get_by_role("button", name="+  Құжат жүктеу", exact=True).click()
        dialog = page.get_by_role("dialog")
        expect(dialog).to_contain_text(catalog["documents.uploadPurpose"]["kk"])
        expect(dialog.get_by_label(catalog["learning.deadline"]["kk"], exact=True)).to_have_count(0)
        before_document_tasks = admin_context.request.get("/api/tasks").json()["total"]
        document_text = "Браузер тексеруі. <b>Бұл мәтін HTML ретінде орындалмайды.</b>"
        dialog.get_by_label("Файл", exact=True).set_input_files(
            {
                "name": "smoke-proof.txt",
                "mimeType": "text/plain",
                "buffer": document_text.encode("utf-8"),
            }
        )
        dialog.get_by_label("Оқу тобы", exact=True).select_option("1")
        dialog.get_by_label("Пән", exact=True).select_option("1")
        dialog.get_by_label("Оқу кезеңі", exact=True).select_option("1")
        dialog.get_by_role("button", name="Жүктеу", exact=True).click()
        expect(page.get_by_role("dialog")).to_have_count(0)
        assert admin_context.request.get("/api/tasks").json()["total"] == before_document_tasks
        document_row = page.get_by_role("row").filter(has_text="smoke-proof.txt")
        document_row.get_by_role("button", name="Ашу", exact=True).click()
        dialog = page.get_by_role("dialog")
        expect(dialog.locator(".text-preview")).to_have_text(document_text)
        assert dialog.locator(".text-preview b").count() == 0
        with page.expect_download() as download:
            dialog.get_by_role("link", name="↓  Файлды жүктеп алу", exact=True).click()
        download.value.save_as(str(work / "downloaded.txt"))
        assert (work / "downloaded.txt").read_text(encoding="utf-8") == document_text
        dialog.get_by_role("button", name="Жабу", exact=True).click()
        document_row.get_by_role("button", name="Жою", exact=True).click()
        page.get_by_role("dialog").get_by_role("button", name="Құжатты жою", exact=True).click()
        expect(page.get_by_role("row").filter(has_text="smoke-proof.txt")).to_have_count(0)
        assert not admin_context.request.get("/api/documents").json()["items"]
        print(
            "PASS audit details, actual TXT upload, safe preview, download and soft deletion",
            flush=True,
        )

        for _ in range(3):
            failed = admin_context.request.post(
                "/api/auth/login",
                data={"login": teacher_login, "password": secrets.token_urlsafe(24)},
            )
            assert failed.status == 401
        assert teacher_context.request.get("/api/auth/me").status == 200
        navigate(page, "sessions")
        session_row = page.get_by_role("row").filter(has_text=teacher_login)
        expect(session_row).to_contain_text(catalog["status.awaiting_admin"]["kk"])
        session_row.get_by_role(
            "button", name=catalog["sessions.aiReview"]["kk"], exact=True
        ).click()
        expect(page.locator("#toast-region .toast.error")).to_contain_text(
            catalog["error.model_unavailable"]["kk"]
        )
        assert teacher_context.request.get("/api/auth/me").status == 200
        dismiss_toasts(page)
        session_row.get_by_role(
            "button", name=catalog["sessions.dismiss"]["kk"], exact=True
        ).click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("Әрекет себебі", exact=True).fill("Браузер тексеруі: жалған дабыл")
        dialog.get_by_role("button", name=catalog["sessions.dismiss"]["kk"], exact=True).click()
        expect(page.get_by_role("dialog")).to_have_count(0)
        expect(session_row).to_contain_text(catalog["status.false_positive"]["kk"])
        assert teacher_context.request.get("/api/auth/me").status == 200
        print("PASS active review evidence, optional AI failure and manual false alarm", flush=True)
        session_row.get_by_role("button", name="Бұғаттау", exact=True).click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("Әрекет себебі", exact=True).fill(
            "Браузер тексеруінде сессияны тоқтату"
        )
        dialog.get_by_role("button", name="Растау", exact=True).click()
        expect(page.get_by_role("dialog")).to_have_count(0)
        assert teacher_context.request.get("/api/auth/me").status == 401
        session_row = page.get_by_role("row").filter(has_text=teacher_login)
        session_row.get_by_role("button", name="Бұғаттан шығару", exact=True).click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("Әрекет себебі", exact=True).fill("Тексеру аяқталды, қайта кіру қажет")
        dialog.get_by_role("button", name="Растау", exact=True).click()
        expect(page.get_by_role("dialog")).to_have_count(0)
        assert teacher_context.request.get("/api/auth/me").status == 401
        sessions = admin_context.request.get("/api/sessions").json()["items"]
        assert next(row for row in sessions if row["login"] == teacher_login)["status"] == "revoked"
        print("PASS UI session block invalidates token; unblock never revives it", flush=True)

        navigate(page, "assistant")
        page.get_by_label("Іздеу сұрағы", exact=True).fill(
            "Соңғы аптада сәтсіз кіру әрекеттері болған ба?"
        )
        page.get_by_role("button", name="Журналдан іздеу", exact=True).click()
        expect(page.locator(".ai-message.error")).to_contain_text("қазір қолжетімсіз")
        assert "ollama serve" not in page.locator("body").inner_text()
        navigate(page, "dashboard")
        expect(page.locator(".kpi-card").first).to_be_visible()
        page.set_viewport_size({"width": 390, "height": 844})
        navigate(page, "grades")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert page.locator("#sidebar").evaluate("node => node.inert")
        dismiss_toasts(page)
        page.screenshot(path=str(artifacts / "grades-mobile.png"), full_page=True)
        page.screenshot(path=str(artifacts / "grades-mobile-viewport.png"), full_page=False)
        page.locator("#nav-toggle").click()
        expect(page.locator("#sidebar")).to_have_class("sidebar open")
        page.keyboard.press("Escape")
        expect(page.locator("#nav-toggle")).to_be_focused()
        page.locator("#user-menu > summary").click()
        page.locator("#logout-button").click()
        expect(page.locator("#auth-screen")).to_be_visible()
        expect(page.locator("#locale-auth")).to_have_value("kk")
        assert admin_context.request.get("/api/auth/me").status == 401
        page.screenshot(path=str(artifacts / "login-mobile.png"), full_page=True)
        assert not errors, f"Browser JavaScript errors: {errors}"
        browser.close()
        print(
            "PASS explicit unavailable AI, working core, mobile layout/focus and logout", flush=True
        )
        return {
            "status": "passed",
            "pages_opened": 13,
            "locales": ["kk", "ru", "en"],
            "registration_approvals": ["teacher", "student"],
            "learning": learning_result,
            "role_approvals": roles_result,
            "automatic_ai_examples": automatic_examples,
            "javascript_errors": len(errors),
            "real_ai_inference": False,
            "screenshots": len(list(artifacts.glob("*.png"))),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-path")
    parser.add_argument("--student-claims-only", action="store_true")
    parser.add_argument("--student-roster-only", action="store_true")
    parser.add_argument("--teachers-only", action="store_true")
    parser.add_argument("--teacher-group-labels-only", action="store_true")
    parser.add_argument("--require-student-email", action="store_true")
    args = parser.parse_args()
    artifacts = ROOT / "artifacts" / "browser-smoke"
    artifacts.mkdir(parents=True, exist_ok=True)
    # A failed new run must never leave an earlier successful result in place.
    result_name = (
        "teacher-group-labels-result.json"
        if args.teacher_group_labels_only
        else "teachers-result.json"
        if args.teachers_only
        else "student-roster-result.json"
        if args.student_roster_only
        else "student-claims-email-result.json"
        if args.require_student_email
        else "student-claims-result.json"
        if args.student_claims_only
        else "result.json"
    )
    result_path = artifacts / result_name
    result_path.unlink(missing_ok=True)
    (ROOT / ".tools").mkdir(exist_ok=True)
    browser_path = find_browser(args.browser_path)
    with tempfile.TemporaryDirectory(prefix="browser-run-", dir=ROOT / ".tools") as directory:
        work = Path(directory).resolve()
        assert work.is_relative_to((ROOT / ".tools").resolve()) and work.name.startswith(
            "browser-run-"
        )
        settings = Settings(
            _env_file=None,
            database_path=work / "audit.db",
            upload_dir=work / "uploads",  # type: ignore[call-arg]
        )
        store = Store(settings)
        store.initialize()
        store.seed_academics()
        password = secrets.token_urlsafe(24)
        create_admin(store, "browser_admin", "Браузер тексеру әкімшісі", password)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        environment = {
            **os.environ,
            "DATABASE_PATH": str(settings.database_path),
            "UPLOAD_DIR": str(settings.upload_dir),
            "COOKIE_SECURE": "false",
            "ALLOWED_ORIGINS": origin,
            "OLLAMA_BASE_URL": "http://127.0.0.1:1",
            "LOGIN_MAX_ATTEMPTS": "20",
            "STUDENT_EMAIL_REQUIRED": "true" if args.require_student_email else "false",
            "STUDENT_EMAIL_DOMAIN": "example.edu" if args.require_student_email else "",
        }
        log_name = (
            "teacher-group-labels-server.log"
            if args.teacher_group_labels_only
            else "teachers-server.log"
            if args.teachers_only
            else "student-roster-server.log"
            if args.student_roster_only
            else "student-claims-email-server.log"
            if args.require_student_email
            else "student-claims-server.log"
            if args.student_claims_only
            else "server.log"
        )
        with (artifacts / log_name).open("w", encoding="utf-8") as output:
            server = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "app.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=ROOT,
                env=environment,
                stdout=output,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            try:
                deadline = time.monotonic() + 45
                with httpx.Client(trust_env=False, timeout=1) as client:
                    while True:
                        if server.poll() is not None:
                            raise RuntimeError(
                                "Smoke server exited; see artifacts/browser-smoke/server.log"
                            )
                        try:
                            if client.get(f"{origin}/api/health").status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        if time.monotonic() > deadline:
                            raise TimeoutError("Smoke server startup timed out")
                        time.sleep(0.1)
                try:
                    if args.teacher_group_labels_only:
                        from browser_teachers_smoke import run_group_labels_browser

                        result = run_group_labels_browser(
                            origin, password, work, artifacts, browser_path
                        )
                    elif args.teachers_only:
                        from browser_teachers_smoke import run_teachers_browser

                        result = run_teachers_browser(
                            origin, password, work, artifacts, browser_path
                        )
                    elif args.student_roster_only:
                        from browser_student_roster_smoke import run_roster_browser

                        result = run_roster_browser(origin, password, work, artifacts, browser_path)
                    elif args.student_claims_only:
                        from browser_student_claims_smoke import run_claims_browser

                        result = run_claims_browser(origin, password, work, artifacts, browser_path)
                    else:
                        result = run_browser(origin, password, work, artifacts, browser_path)
                except Exception as error:
                    # Playwright's default assertion dump includes form values.
                    # Keep failure locations without printing random test passwords.
                    locations = " -> ".join(
                        f"{Path(frame.filename).name}:{frame.lineno}"
                        for frame in traceback.extract_tb(error.__traceback__)
                        if Path(frame.filename).is_relative_to(ROOT / "scripts")
                    )
                    raise RuntimeError(
                        f"Browser smoke failed: {type(error).__name__} at {locations}"
                    ) from None
                result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
                if args.teacher_group_labels_only and result.get("status") == "passed":
                    teachers_path = artifacts / "teachers-result.json"
                    if teachers_path.exists():
                        previous = json.loads(teachers_path.read_text(encoding="utf-8"))
                        if previous.get("status") == "passed":
                            previous["literal_group_names_preserved"] = True
                            previous["group_catalog_consistency_probe"] = result
                            teachers_path.write_text(
                                json.dumps(previous, indent=2) + "\n", encoding="utf-8"
                            )
                print(json.dumps(result), flush=True)
            finally:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=10)


if __name__ == "__main__":
    main()
