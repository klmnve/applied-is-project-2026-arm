"""Exercise role and approval UI against the isolated smoke application's APIs."""

import json
import secrets
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, Page, expect

CATALOG = json.loads(
    (Path(__file__).resolve().parents[1] / "app/static/locales.json").read_text("utf-8")
)


def _text(key: str, locale: str) -> str:
    return str(CATALOG[key][locale])


def _csrf(context: BrowserContext) -> dict[str, str]:
    return {
        "X-CSRF-Token": next(
            cookie["value"] for cookie in context.cookies() if cookie["name"] == "arm_csrf"
        )
    }


def _register(admin: BrowserContext, origin: str, role: str) -> tuple[str, str, int]:
    login = "audit_" + role + "_" + secrets.token_hex(4)
    password = secrets.token_urlsafe(24)
    claim = {}
    if role == "student":
        groups = admin.request.get(origin + "/api/auth/registration-options").json()["groups"]
        claim = {
            "student_claim_name": "Unverified duplicate applicant",
            "student_claim_number": "CLAIM-" + secrets.token_hex(6).upper(),
            "student_claim_group_id": groups[0]["group_id"],
        }
    response = admin.request.post(
        origin + "/api/auth/register",
        data={
            "login": login,
            "password": password,
            "display_name": "Role audit " + role,
            "requested_role": "student" if role == "student" else "teacher",
            **claim,
        },
    )
    assert response.status == 201, "Isolated registration failed"
    users = admin.request.get(origin + "/api/admin/users", params={"q": login}).json()["items"]
    user = next(row for row in users if row["login"] == login)
    return login, password, int(user["user_id"])


def _close_help(page: Page) -> None:
    dialogs = page.locator("dialog[data-context-help='true'][open]")
    if dialogs.count():
        dialogs.last.locator(".dialog-close").click()
        expect(dialogs).to_have_count(0)


def _login(page: Page, origin: str, login: str, password: str, locale: str) -> None:
    page.goto(origin)
    expect(page.locator("#auth-screen")).to_be_visible(timeout=30000)
    page.locator("#locale-auth").select_option(locale)
    page.locator("#login").fill(login)
    page.locator("#password").fill(password)
    page.locator("#auth-submit").click()
    expect(page.locator("#app-screen")).to_be_visible(timeout=30000)
    expect(page.locator("#page-content")).to_have_attribute("aria-busy", "false", timeout=30000)
    expect(page.locator("dialog[data-context-help='true']")).to_be_visible(timeout=10000)
    assert 2 <= page.locator(".context-steps li").count() <= 5
    _close_help(page)


def _route(page: Page, route: str) -> None:
    _close_help(page)
    item = page.locator(f".nav-link[data-route='{route}']")
    item.click()
    expect(item).to_have_attribute("aria-current", "page")
    expect(page.locator("#page-content")).to_have_attribute("aria-busy", "false", timeout=30000)
    expect(page.locator(".error-panel")).to_have_count(0)


def _language(page: Page, locale: str) -> None:
    _close_help(page)
    page.locator("#locale-app").select_option(locale)
    expect(page.locator("html")).to_have_attribute("lang", locale)
    expect(page.locator("#page-content")).to_have_attribute("aria-busy", "false", timeout=30000)


def run_roles_flow(
    origin: str,
    admin_context: BrowserContext,
    artifacts: Path,
    errors: list[str],
    navigate: Any = None,
    dismiss_help: Any = None,
) -> dict[str, Any]:
    """No fixture passwords or cookies leave this function or enter the report."""
    del navigate, dismiss_help
    browser = admin_context.browser
    assert browser is not None
    expectations = {
        "head": {"dashboard", "students", "grades", "tasks", "documents", "audit", "assistant"},
        "auditor": {"dashboard", "audit", "assistant"},
    }
    views = 0
    for role, expected_routes in expectations.items():
        login, password, user_id = _register(admin_context, origin, role)
        approved = admin_context.request.post(
            f"{origin}/api/admin/requests/{user_id}/approve",
            headers=_csrf(admin_context),
            data={"role_code": "teacher"},
        )
        assert approved.status == 200
        changed = admin_context.request.patch(
            f"{origin}/api/admin/users/{user_id}",
            headers=_csrf(admin_context),
            data={"role_code": role},
        )
        assert changed.status == 200
        context = browser.new_context(base_url=origin, viewport={"width": 1440, "height": 1000})
        try:
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            _login(page, origin, login, password, "kk")
            for locale in ("kk", "ru", "en"):
                _language(page, locale)
                page.reload()
                expect(page.locator("#app-screen")).to_be_visible(timeout=30000)
                expect(page.locator("#page-content")).to_have_attribute("aria-busy", "false")
                expect(page.locator("#locale-app")).to_have_value(locale)
                assert page.evaluate("localStorage.getItem('arm_locale')") == locale
                actual = set(
                    page.locator(".nav-link").evaluate_all(
                        "nodes => nodes.map(node => node.dataset.route)"
                    )
                )
                assert actual == expected_routes, f"Unexpected {role} navigation"
                for route in sorted(expected_routes):
                    _route(page, route)
                    page.locator("#help-button").click()
                    expect(page.locator(".context-steps")).to_be_visible()
                    assert 2 <= page.locator(".context-steps li").count() <= 5
                    _close_help(page)
                    views += 1
                    if route == "tasks":
                        expect(
                            page.get_by_role(
                                "button", name=_text("learning.create", locale), exact=True
                            )
                        ).to_have_count(0)
            headers = _csrf(context)
            for endpoint in ("/api/admin/users", "/api/admin/requests", "/api/sessions"):
                response = context.request.get(origin + endpoint)
                assert response.status == 403
            forbidden = context.request.patch(
                origin + "/api/grades/1", headers=headers, data={"value": 99, "version": 0}
            )
            assert forbidden.status == 403 and forbidden.json()["error"]["code"] == "forbidden"
            page.screenshot(path=str(artifacts / f"role-{role}-en.png"), full_page=True)
        finally:
            context.close()

    # Teacher rejection ends the old session; a fresh login shows the private reason.
    login, password, rejected_id = _register(admin_context, origin, "teacher")
    rejected_context = browser.new_context(base_url=origin)
    try:
        applicant = rejected_context.new_page()
        applicant.on("pageerror", lambda error: errors.append(str(error)))
        _login(applicant, origin, login, password, "en")
        admin = admin_context.new_page()
        admin.on("pageerror", lambda error: errors.append(str(error)))
        admin.goto(origin + "/#requests")
        expect(admin.locator("#page-content")).to_have_attribute(
            "aria-busy", "false", timeout=30000
        )
        _close_help(admin)
        _language(admin, "en")
        row = admin.locator("tr").filter(has_text=login)
        expect(row).to_have_count(1)
        row.get_by_role("button", name=_text("requests.reject", "en"), exact=True).click()
        reason = "The supplied teaching affiliation needs correction."
        dialog = admin.locator("dialog").last
        dialog.locator("textarea").fill(reason)
        with admin.expect_response(
            lambda response: (
                response.url.endswith(f"/api/admin/teachers/{rejected_id}/reject")
                and response.request.method == "POST"
            )
        ) as decision:
            dialog.locator("button[type='submit']").click()
        assert decision.value.status == 200
        assert rejected_context.request.get(origin + "/api/auth/me").status == 401
        applicant.get_by_role("button", name=_text("approval.refresh", "en"), exact=True).click()
        expect(applicant.locator("#auth-screen")).to_be_visible()
        applicant.locator("#login").fill(login)
        applicant.locator("#password").fill(password)
        applicant.locator("#auth-submit").click()
        expect(applicant.locator(".approval-page")).to_be_visible()
        for locale in ("en", "ru", "kk"):
            _language(applicant, locale)
            applicant.reload()
            expect(applicant.locator("#page-content")).to_contain_text(
                _text("approval.rejectedTitle", locale), timeout=30000
            )
            expect(applicant.locator("#page-content")).to_contain_text(reason)
            expect(applicant.locator(".nav-link")).to_have_count(0)
            denied = rejected_context.request.get(origin + "/api/tasks")
            assert denied.status == 403
        me = rejected_context.request.get(origin + "/api/auth/me").json()["user"]
        assert me["approval_status"] == "rejected" and me["active"] is False
        applicant.screenshot(path=str(artifacts / "approval-rejected-kk.png"), full_page=True)
        admin.close()
    finally:
        rejected_context.close()

    # The same linked student cannot be approved for a second account, in any locale.
    users = admin_context.request.get(origin + "/api/admin/users").json()["items"]
    linked = next(
        row["student_id"]
        for row in users
        if row["role_code"] == "student"
        and row["approval_status"] == "approved"
        and row["student_id"] is not None
    )
    duplicate_login, _, duplicate_id = _register(admin_context, origin, "student")
    admin = admin_context.new_page()
    admin.on("pageerror", lambda error: errors.append(str(error)))
    try:
        admin.goto(origin + "/#requests")
        expect(admin.locator("#page-content")).to_have_attribute(
            "aria-busy", "false", timeout=30000
        )
        for locale in ("kk", "ru", "en"):
            _language(admin, locale)
            row = admin.locator("tr").filter(has_text=duplicate_login)
            row.get_by_role("button", name=_text("requests.approve", locale), exact=True).click()
            dialog = admin.locator("dialog").last
            dialog.get_by_label(_text("requests.student", locale), exact=True).select_option(
                str(linked)
            )
            dialog.get_by_role(
                "checkbox", name=_text("claim.confirmMatch", locale), exact=True
            ).check()
            dialog.get_by_role(
                "checkbox", name=_text("claim.acknowledgeMismatch", locale), exact=True
            ).check()
            with admin.expect_response(
                lambda response: (
                    response.url.endswith(f"/api/admin/requests/{duplicate_id}/approve")
                    and response.request.method == "POST"
                )
            ) as rejected:
                dialog.locator("button[type='submit']").click()
            assert rejected.value.status == 409
            expect(dialog.locator(".dialog-error")).to_be_visible()
            expect(dialog.locator(".dialog-error")).not_to_have_text("")
            dialog.locator(".dialog-close").click()
            persisted = admin_context.request.get(
                origin + "/api/admin/users", params={"q": duplicate_login}
            ).json()["items"][0]
            assert persisted["approval_status"] == "pending"
            assert persisted["active"] is False and persisted["student_id"] is None
    finally:
        admin.close()
    assert not errors, "Browser JavaScript errors in role approval checks"
    return {
        "status": "passed",
        "head_auditor_views": views,
        "locales": ["kk", "ru", "en"],
        "reject_ui": True,
        "duplicate_student_link_ui": True,
        "readonly_api_denials": True,
    }
