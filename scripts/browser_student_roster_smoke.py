"""Real registration, approval and scoped roster UI in an isolated smoke database."""

import secrets
import sqlite3
from contextlib import closing
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from browser_student_claims_smoke import (
    approval_response,
    close_dialogs,
    csrf,
    refresh_owner,
    requests_page,
    text,
)
from playwright.sync_api import BrowserContext, Page, Response, expect, sync_playwright


def route(page: Page, name: str) -> None:
    close_dialogs(page)
    item = page.locator(f".nav-link[data-route='{name}']")
    if page.locator("#nav-toggle").is_visible():
        page.locator("#nav-toggle").click()
    item.click()
    expect(item).to_have_attribute("aria-current", "page")
    expect(page.locator("#page-content")).to_have_attribute("aria-busy", "false")
    close_dialogs(page)
    expect(page.locator(".error-panel")).to_have_count(0)


def roster_response(response: Response) -> bool:
    return urlparse(response.url).path == "/api/students" and response.request.method == "GET"


def filter_roster(page: Page, group: str, search: str) -> dict[str, Any]:
    page.locator("#students-search").fill(search)
    page.locator("#students-group").select_option(group)
    with page.expect_response(roster_response) as response:
        page.locator(".students-filters button[type=submit]").click()
    assert response.value.status == 200
    parameters = parse_qs(urlparse(response.value.url).query)
    assert parameters.get("page") == ["1"]
    assert parameters.get("group_id", [""]) == [group]
    assert parameters.get("q", [""]) == [search.strip()]
    expect(page.locator("#page-content")).to_have_attribute("aria-busy", "false")
    return dict(response.value.json())


def register_staff(admin: BrowserContext, role: str) -> tuple[str, str]:
    login = "roster_" + role + "_" + secrets.token_hex(4)
    password = secrets.token_urlsafe(24)
    registered = admin.request.post(
        "/api/auth/register",
        data={
            "login": login,
            "display_name": "Roster " + role,
            "password": password,
            "requested_role": "teacher",
        },
    )
    assert registered.status == 201
    user_id = admin.request.get("/api/admin/users", params={"q": login}).json()["items"][0][
        "user_id"
    ]
    approved = admin.request.post(
        f"/api/admin/requests/{user_id}/approve",
        headers=csrf(admin),
        data={"role_code": "teacher"},
    )
    assert approved.status == 200
    if role != "teacher":
        changed = admin.request.patch(
            f"/api/admin/users/{user_id}", headers=csrf(admin), data={"role_code": role}
        )
        assert changed.status == 200
    if role == "teacher":
        catalog = admin.request.get("/api/academic/catalog").json()
        assigned = admin.request.put(
            "/api/academic/assignments",
            headers=csrf(admin),
            data={
                "user_id": user_id,
                "group_id": catalog["groups"][0]["group_id"],
                "subject_id": catalog["subjects"][0]["subject_id"],
                "term_id": catalog["terms"][0]["term_id"],
                "assigned": True,
            },
        )
        assert assigned.status == 200
    return login, password


def run_roster_browser(
    origin: str, admin_password: str, work: Path, artifacts: Path, browser_path: str | None
) -> dict[str, Any]:
    expect.set_options(timeout=30000)
    errors: list[str] = []
    # Official fixture records exercise actual server pagination, without creating login accounts.
    with closing(sqlite3.connect(work / "audit.db")) as connection:
        group_id = connection.execute("SELECT group_id FROM Groups ORDER BY group_id").fetchone()[0]
        connection.executemany(
            "INSERT INTO Students(group_id,display_name,student_code) VALUES(?,?,?)",
            [
                (group_id, f"Pagination fixture {index:02}", f"PAGE-{index:03}")
                for index in range(26)
            ],
        )
        connection.commit()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=browser_path, headless=True)
        admin_context = browser.new_context(
            base_url=origin, viewport={"width": 1440, "height": 1000}
        )
        assert (
            admin_context.request.post(
                "/api/auth/login", data={"login": "browser_admin", "password": admin_password}
            ).status
            == 200
        )
        admin = admin_context.new_page()
        admin.on("pageerror", lambda error: errors.append(str(error)))
        admin.goto(origin)
        expect(admin.locator("dialog[data-context-help][open]")).to_be_visible()
        close_dialogs(admin)
        options = admin_context.request.get("/api/auth/registration-options").json()
        groups = options["groups"]
        assert len(groups) >= 2
        owners: list[BrowserContext] = []
        for index, language in enumerate(("kk", "ru", "en")):
            name = f"Roster Student {language} " + secrets.token_hex(3)
            code = "NEW-" + secrets.token_hex(5).upper()
            username = "new_student_" + secrets.token_hex(4)
            password = secrets.token_urlsafe(24)
            context = browser.new_context(base_url=origin, viewport={"width": 1280, "height": 1000})
            owners.append(context)
            owner = context.new_page()
            owner.on("pageerror", lambda error: errors.append(str(error)))
            owner.goto(origin)
            expect(owner.locator("#auth-screen")).to_be_visible()
            owner.locator("#locale-auth").select_option(language)
            owner.locator("#register-tab").click()
            expect(owner.locator("#student-claim-name")).to_be_disabled()
            owner.locator("#requested-role").select_option("student")
            owner.locator("#display-name").fill("Account " + language)
            owner.locator("#login").fill(username)
            owner.locator("#password").fill(password)
            owner.locator("#student-claim-name").fill("  " + name.replace(" ", "   ") + "  ")
            owner.locator("#student-claim-number").fill(code.lower())
            owner.locator("#student-claim-group").select_option(str(groups[0]["group_id"]))
            owner.locator("#auth-submit").click()
            expect(owner.locator("#auth-title")).to_have_text(text("Жүйеге кіру", language))
            owner.locator("#login").fill(username)
            owner.locator("#password").fill(password)
            owner.locator("#auth-submit").click()
            expect(owner.locator(".approval-page")).to_be_visible()
            expect(owner.locator("dialog[data-context-help][open]")).to_be_visible()
            close_dialogs(owner)
            pending = context.request.get("/api/auth/me").json()["user"]
            assert pending["student_claim"]["name"] == name
            assert pending["student_id"] is None and pending["active"] is False
            assert context.request.get("/api/students").status == 403
            expect(owner.locator(".nav-link[data-route='students']")).to_have_count(0)
            assert (
                admin_context.request.get("/api/students", params={"q": code}).json()["total"] == 0
            )
            reviewed_group = groups[index % len(groups)]
            other_group = next(
                row for row in groups if row["group_id"] != reviewed_group["group_id"]
            )
            with closing(sqlite3.connect(work / "audit.db")) as connection:
                connection.execute(
                    "INSERT INTO Students(group_id,display_name,student_code) VALUES(?,?,?)",
                    (other_group["group_id"], name, "OTHER-" + secrets.token_hex(5).upper()),
                )
                connection.commit()
            before = admin_context.request.get("/api/students").json()["total"]
            requests_page(admin, language)
            admin.get_by_role("row").filter(has_text=username).get_by_role(
                "button", name=text("requests.approve", language), exact=True
            ).click()
            dialog = admin.locator("dialog[open]").last
            expect(dialog.locator("#approval-student-mode")).to_have_value("existing")
            dialog.locator("#approval-student-mode").select_option("create")
            group = dialog.locator("#approval-confirmed-group")
            expect(group).to_have_value("")
            confirmation = dialog.get_by_role(
                "checkbox", name=text("approval.confirmCreate", language), exact=True
            )
            expect(confirmation).not_to_be_checked()
            expect(confirmation).to_be_disabled()
            assert not dialog.locator("form").evaluate("node => node.checkValidity()")
            group.select_option(str(reviewed_group["group_id"]))
            expect(confirmation).not_to_be_checked()
            expect(confirmation).to_be_enabled()
            confirmation.check()
            distinct = dialog.get_by_role(
                "checkbox", name=text("approval.confirmDistinct", language), exact=True
            )
            expect(distinct).to_be_visible()
            expect(distinct).not_to_be_checked()
            assert not dialog.locator("form").evaluate("node => node.checkValidity()")
            detail = admin_context.request.get(f"/api/admin/requests/{pending['user_id']}").json()
            ambiguous = admin_context.request.post(
                f"/api/admin/requests/{pending['user_id']}/approve",
                headers=csrf(admin_context),
                data={
                    "role_code": "student",
                    "create_student": True,
                    "confirmed_group_id": reviewed_group["group_id"],
                    "confirm_student_match": True,
                    "claim_revision": detail["claim_revision"],
                },
            )
            assert ambiguous.status == 409
            assert ambiguous.json()["error"]["code"] == "student_selection_required"
            distinct.check()
            group.select_option(str(other_group["group_id"]))
            expect(distinct).not_to_be_checked()
            expect(confirmation).not_to_be_checked()
            group.select_option(str(reviewed_group["group_id"]))
            confirmation.check()
            distinct.check()
            admin.screenshot(path=str(artifacts / f"student-distinct-confirm-{language}.png"))
            with admin.expect_response(
                partial(approval_response, user_id=pending["user_id"])
            ) as approved:
                dialog.locator("button[type=submit]").click()
            assert approved.value.status == 200
            payload = approved.value.request.post_data_json
            assert isinstance(payload, dict)
            assert payload["create_student"] is True and "student_id" not in payload
            assert payload["confirmed_group_id"] == reviewed_group["group_id"]
            assert payload["confirm_student_match"] is True and payload["claim_revision"]
            assert payload["confirm_distinct_student"] is True
            expect(admin.locator("dialog[open]")).to_have_count(0)
            refresh_owner(owner, language)
            approved_user = context.request.get("/api/auth/me").json()["user"]
            assert approved_user["student_id"] and approved_user["active"] is True
            official = admin_context.request.get("/api/students", params={"q": code}).json()
            assert official["total"] == 1 and official["items"][0]["display_name"] == name
            assert official["items"][0]["group_id"] == reviewed_group["group_id"]
            assert official["items"][0]["student_id"] == approved_user["student_id"]
            assert admin_context.request.get("/api/students").json()["total"] == before + 1
            route(admin, "students")
            filtered = filter_roster(admin, str(reviewed_group["group_id"]), code.lower())
            assert filtered["total"] == 1
            expect(admin.locator("tbody tr")).to_have_count(1)
            expect(admin.locator("tbody")).to_contain_text(name)
            wrong = next(row for row in groups if row["group_id"] != reviewed_group["group_id"])
            assert filter_roster(admin, str(wrong["group_id"]), code)["total"] == 0
            expect(admin.locator(".empty-state h3")).to_have_text(
                text("students.noResults", language)
            )
            assert filter_roster(admin, "", name)["total"] == 2
            expect(admin.locator("#students-group")).to_have_value("")
            admin.screenshot(path=str(artifacts / f"student-roster-{language}.png"), full_page=True)
            all_rows = filter_roster(admin, "", "")
            assert all_rows["total"] > 25 and len(all_rows["items"]) == 25
            with admin.expect_response(roster_response) as next_page:
                admin.get_by_role("button", name=text("Келесі  →", language), exact=True).click()
            assert next_page.value.json()["page"] == 2
            expect(admin.locator("#page-content")).to_have_attribute("aria-busy", "false")
            admin.get_by_role("button", name=text("←  Алдыңғы", language), exact=True).click()
            expect(admin.locator("#page-content")).to_have_attribute("aria-busy", "false")
            expect(
                admin.get_by_role("button", name=text("←  Алдыңғы", language), exact=True)
            ).to_be_disabled()
            print(
                "PASS new student approval, official roster/search/group/pagination: " + language,
                flush=True,
            )
        staff_views = 0
        teacher_credentials: tuple[str, str] | None = None
        for role in ("head", "teacher", "auditor"):
            username, password = register_staff(admin_context, role)
            if role == "teacher":
                teacher_credentials = (username, password)
            context = browser.new_context(base_url=origin, viewport={"width": 1440, "height": 1000})
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            assert (
                context.request.post(
                    "/api/auth/login", data={"login": username, "password": password}
                ).status
                == 200
            )
            page.goto(origin)
            expect(page.locator("dialog[data-context-help][open]")).to_be_visible()
            close_dialogs(page)
            for language in ("kk", "ru", "en"):
                page.locator("#locale-app").select_option(language)
                expect(page.locator("#page-content")).to_have_attribute("aria-busy", "false")
                if role == "auditor":
                    expect(page.locator(".nav-link[data-route='students']")).to_have_count(0)
                    assert context.request.get("/api/students").status == 403
                else:
                    route(page, "students")
                    expect(page.locator("#page-content h1")).to_have_text(
                        text("students.title", language)
                    )
                    scoped = context.request.get("/api/students").json()
                    if role == "teacher":
                        assert len(scoped["groups"]) == 1
                        assert all(
                            item["group_id"] == groups[0]["group_id"] for item in scoped["items"]
                        )
                        assert (
                            context.request.get(
                                "/api/students", params={"group_id": groups[1]["group_id"]}
                            ).status
                            == 403
                        )
                    page.locator("#help-button").click()
                    expect(page.locator(".context-steps li")).to_have_count(2)
                    close_dialogs(page)
                staff_views += 1
            if role == "teacher":
                filter_roster(page, str(groups[0]["group_id"]), "")
                catalog = admin_context.request.get("/api/academic/catalog").json()
                assignment = {
                    "user_id": context.request.get("/api/auth/me").json()["user"]["user_id"],
                    "group_id": groups[0]["group_id"],
                    "subject_id": catalog["subjects"][0]["subject_id"],
                    "term_id": catalog["terms"][0]["term_id"],
                }
                assert (
                    admin_context.request.put(
                        "/api/academic/assignments",
                        headers=csrf(admin_context),
                        data={**assignment, "assigned": False},
                    ).status
                    == 200
                )
                page.locator(".heading-actions button").first.click()
                expect(page.locator("#page-content")).to_contain_text(
                    text("students.groupReset", "en")
                )
                expect(page.locator("#page-content")).to_contain_text(
                    text("students.noGroups", "en")
                )
                expect(page.locator("#students-group")).to_have_value("")
                expect(page.locator(".error-panel")).to_have_count(0)
                assert (
                    admin_context.request.put(
                        "/api/academic/assignments",
                        headers=csrf(admin_context),
                        data={**assignment, "assigned": True},
                    ).status
                    == 200
                )
            context.close()
        admin.set_viewport_size({"width": 390, "height": 844})
        filter_roster(admin, "", "")
        assert admin.evaluate("document.documentElement.scrollWidth <= innerWidth")
        admin.screenshot(
            path=str(artifacts / "student-roster-mobile.png"),
            full_page=False,
            animations="disabled",
        )
        assert (
            admin.locator("#sidebar").evaluate("node => node.getBoundingClientRect().right") <= 0.5
        )
        admin.set_viewport_size({"width": 1440, "height": 1000})
        filter_roster(admin, str(groups[1]["group_id"]), "")
        admin.locator("#user-menu > summary").click()
        admin.locator("#logout-button").click()
        expect(admin.locator("#auth-screen")).to_be_visible()
        expect(admin.locator("#page-content > *")).to_have_count(0)
        assert teacher_credentials is not None
        admin.locator("#login").fill(teacher_credentials[0])
        admin.locator("#password").fill(teacher_credentials[1])
        admin.locator("#auth-submit").click()
        expect(admin.locator("#page-content h1")).to_have_text(text("students.title", "en"))
        close_dialogs(admin)
        expect(admin.locator("#students-group")).to_have_value("")
        expect(admin.locator(".error-panel")).to_have_count(0)
        expect(admin.locator("#students-group option")).to_have_count(2)
        assert not errors
        for context in owners:
            context.close()
        browser.close()
    return {
        "status": "passed",
        "locales": ["kk", "ru", "en"],
        "student_created_by_reviewed_approval": True,
        "homonym_requires_explicit_distinct_confirmation": True,
        "unapproved_absent_from_roster": True,
        "name_whitespace_collapsed": True,
        "account_student_group_link": True,
        "server_combined_group_search": True,
        "server_pagination": True,
        "staff_role_locale_checks": staff_views,
        "identity_change_resets_roster_filters": True,
        "revoked_assignment_filter_recovers": True,
        "student_auditor_roster_denied": True,
        "mobile_no_horizontal_overflow": True,
        "javascript_errors": 0,
    }
