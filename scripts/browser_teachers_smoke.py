"""Exercise teacher approval, catalog management and exact scopes against real APIs."""

import secrets
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from browser_student_claims_smoke import close_dialogs, csrf, refresh_owner, text
from browser_student_roster_smoke import register_staff
from browser_student_roster_smoke import route as navigate
from playwright.sync_api import BrowserContext, Page, Response, expect, sync_playwright


def response_matches(response: Response, path: str, method: str) -> bool:
    return urlparse(response.url).path == path and response.request.method == method


def top(page: Page) -> Any:
    return page.locator("dialog[open]").last


def route(page: Page, name: str) -> None:
    already_open = urlparse(page.url).fragment == name
    navigate(page, name)
    if already_open:
        page.locator(".heading-actions button").filter(has_text="↻").click()
        expect(page.locator("#page-content")).to_have_attribute("aria-busy", "false")
        close_dialogs(page)


def catalog_view(page: Page, kind: str) -> None:
    route(page, "catalogs")
    page.locator(f".page-tabs [data-view='{kind}']").click()
    expect(page.locator("#page-content")).to_have_attribute("aria-busy", "false")
    expect(page.locator(f".page-tabs [data-view='{kind}']")).to_have_attribute(
        "aria-pressed", "true"
    )


def create_catalog(page: Page, kind: str, name: str, language: str) -> dict[str, Any]:
    catalog_view(page, kind)
    page.get_by_role("button", name=text("catalogs.add", language), exact=True).click()
    top(page).get_by_label(text("catalogs.name", language), exact=True).fill(name)
    if kind == "terms":
        top(page).get_by_label(text("catalogs.academicYear", language), exact=True).fill(
            "2026-2027"
        )
    with page.expect_response(
        partial(response_matches, path=f"/api/admin/catalogs/{kind}", method="POST")
    ) as response:
        top(page).locator("button[type=submit]").click()
    assert response.value.status == 201
    expect(page.locator("dialog[open]")).to_have_count(0)
    expect(page.locator("tbody")).to_contain_text(name)
    return dict(response.value.json()["item"])


def toggle_catalog(page: Page, kind: str, item: dict[str, Any], language: str) -> None:
    catalog_view(page, kind)
    row = page.get_by_role("row").filter(has_text=item["name"])
    row.get_by_role(
        "button",
        name=text("catalogs.archive" if item["active"] else "catalogs.restore", language),
        exact=True,
    ).click()
    top(page).locator("button[type=submit]").click()
    expect(page.locator("dialog[open]")).to_have_count(0)
    item["active"] = not item["active"]
    expect(row).to_contain_text(
        text("status.active" if item["active"] else "status.archived", language)
    )


def teacher_search(
    page: Page, login: str, view: str, language: str, group: str = "", subject: str = ""
) -> None:
    route(page, "teachers")
    page.locator(f".page-tabs [data-view='{view}']").click()
    expect(page.locator("#page-content")).to_have_attribute("aria-busy", "false")
    page.locator("#teachers-search").fill(login)
    if view == "teachers":
        page.locator("#teachers-group").select_option(group)
        page.locator("#teachers-subject").select_option(subject)
    else:
        expect(page.locator("#teachers-group")).to_be_disabled()
        expect(page.locator("#teachers-subject")).to_be_disabled()
    with page.expect_response(
        partial(response_matches, path="/api/admin/teachers", method="GET")
    ) as response:
        page.locator(".teacher-filters button[type=submit]").click()
    assert response.value.status == 200
    parameters = parse_qs(urlparse(response.value.url).query)
    assert parameters["q"] == [login] and parameters["view"] == [view]
    if group:
        assert parameters["group_id"] == [group]
    if subject:
        assert parameters["subject_id"] == [subject]
    expect(page.locator("#page-content")).to_have_attribute("aria-busy", "false")
    expect(page.locator("tbody")).to_contain_text(login)
    expect(page.locator("#page-content h1")).to_have_text(text("teachers.title", language))


def add_assignment(page: Page, group: int, subject: int, term: int, language: str) -> None:
    top(page).get_by_role(
        "button", name=text("teachers.addAssignment", language), exact=True
    ).click()
    dialog = top(page)
    for key, value in (("Оқу тобы", group), ("Пән", subject), ("Оқу кезеңі", term)):
        dialog.get_by_label(text(key, language), exact=True).select_option(str(value))
    with page.expect_response(
        partial(response_matches, path="/api/academic/assignments", method="PUT")
    ) as response:
        dialog.locator("button[type=submit]").click()
    assert response.value.status == 200
    expect(page.locator("dialog[open]")).to_have_count(1)
    expect(
        top(page).get_by_role("button", name=text("teachers.addAssignment", language), exact=True)
    ).to_be_visible()


def literal_group_labels(
    admin: Page,
    admin_context: BrowserContext,
    group: dict[str, Any],
    username: str,
    language: str,
    artifacts: Path,
    errors: list[str],
) -> None:
    original = group["name"]
    literal = "Белсенді" if language == "ru" else "status.active"

    def rename(old: str, new: str) -> None:
        close_dialogs(admin)
        catalog_view(admin, "groups")
        row = admin.get_by_role("row").filter(has_text=old)
        row.get_by_role("button", name=text("Өзгерту", language), exact=True).click()
        top(admin).get_by_label(text("catalogs.name", language), exact=True).fill(new)
        with admin.expect_response(
            partial(
                response_matches,
                path=f"/api/admin/catalogs/groups/{group['group_id']}",
                method="PATCH",
            )
        ) as response:
            top(admin).locator("button[type=submit]").click()
        assert response.value.status == 200
        expect(admin.locator("dialog[open]")).to_have_count(0)
        expect(admin.get_by_role("row").filter(has_text=new).locator("td").first).to_have_text(new)

    browser = admin_context.browser
    assert browser is not None
    guest = browser.new_context(base_url=admin.url.split("#")[0])
    public = guest.new_page()
    public.on("pageerror", lambda error: errors.append(str(error)))
    public.goto(admin.url.split("#")[0])
    expect(public.locator("#auth-screen")).to_be_visible()
    public.locator("#locale-auth").select_option(language)
    public.locator("#register-tab").click()
    public.locator("#requested-role").select_option("student")
    expect(
        public.locator(f"#student-claim-group option[value='{group['group_id']}']")
    ).to_have_text(original)
    public.locator("#login-tab").click()
    pending = browser.new_context(base_url=admin.url.split("#")[0])
    login_name = "group_claim_" + secrets.token_hex(4)
    password = secrets.token_urlsafe(24)
    assert (
        pending.request.post(
            "/api/auth/register",
            data={
                "login": login_name,
                "password": password,
                "display_name": "Group catalog proof",
                "requested_role": "student",
                "student_claim_name": "Group catalog claimant",
                "student_claim_number": "GROUP-" + secrets.token_hex(5).upper(),
                "student_claim_group_id": group["group_id"],
            },
        ).status
        == 201
    )
    assert (
        pending.request.post(
            "/api/auth/login", data={"login": login_name, "password": password}
        ).status
        == 200
    )
    owner = pending.new_page()
    owner.on("pageerror", lambda error: errors.append(str(error)))
    owner.goto(admin.url.split("#")[0])
    expect(owner.locator("dialog[data-context-help][open]")).to_be_visible()
    close_dialogs(owner)
    owner.locator("#locale-app").select_option(language)
    expect(owner.locator("#page-content")).to_have_attribute("aria-busy", "false")

    def correction_label(expected: str | None) -> None:
        owner.get_by_role("button", name=text("claim.correct", language), exact=True).click()
        choice = top(owner).locator(f"#correct-claim-group option[value='{group['group_id']}']")
        if expected is None:
            expect(choice).to_have_count(0)
            expect(top(owner).locator("#correct-claim-group")).to_have_value("")
        else:
            expect(choice).to_have_text(expected)
        close_dialogs(owner)

    correction_label(original)
    rename(original, literal)
    public.locator("#register-tab").click()
    expect(
        public.locator(f"#student-claim-group option[value='{group['group_id']}']")
    ).to_have_text(literal)
    correction_label(literal)
    route(admin, "students")
    expect(admin.locator(f"#students-group option[value='{group['group_id']}']")).to_have_text(
        literal
    )
    teacher_search(admin, username, "teachers", language)
    expect(admin.locator(f"#teachers-group option[value='{group['group_id']}']")).to_have_text(
        literal
    )
    admin.get_by_role("row").filter(has_text=username).get_by_role(
        "button", name=text("teachers.profile", language), exact=True
    ).click()
    expect(top(admin).locator("tbody")).to_contain_text(literal)
    top(admin).get_by_role(
        "button", name=text("teachers.addAssignment", language), exact=True
    ).click()
    choice = top(admin).get_by_label(text("Оқу тобы", language), exact=True)
    expect(choice.locator(f"option[value='{group['group_id']}']")).to_have_text(literal)
    choice.select_option(str(group["group_id"]))
    admin.screenshot(
        path=str(artifacts / f"teacher-group-label-{language}.png"), animations="disabled"
    )
    archived = {**group, "name": literal}
    close_dialogs(admin)
    toggle_catalog(admin, "groups", archived, language)
    public.locator("#login-tab").click()
    public.locator("#register-tab").click()
    expect(
        public.locator(f"#student-claim-group option[value='{group['group_id']}']")
    ).to_have_count(0)
    correction_label(None)
    toggle_catalog(admin, "groups", archived, language)
    public.locator("#login-tab").click()
    public.locator("#register-tab").click()
    expect(
        public.locator(f"#student-claim-group option[value='{group['group_id']}']")
    ).to_have_text(literal)
    correction_label(literal)
    guest.close()
    pending.close()
    rename(literal, original)
    teacher_search(admin, username, "teachers", language)
    admin.get_by_role("row").filter(has_text=username).get_by_role(
        "button", name=text("teachers.profile", language), exact=True
    ).click()
    expect(top(admin).locator("tbody")).to_contain_text(original)


def run_group_labels_browser(
    origin: str, admin_password: str, work: Path, artifacts: Path, browser_path: str | None
) -> dict[str, Any]:
    del work
    expect.set_options(timeout=30000)
    errors: list[str] = []
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
        username, _ = register_staff(admin_context, "teacher")
        user_id = admin_context.request.get("/api/admin/users", params={"q": username}).json()[
            "items"
        ][0]["user_id"]
        for language in ("ru", "en"):
            close_dialogs(admin)
            admin.locator("#locale-app").select_option(language)
            expect(admin.locator("#page-content")).to_have_attribute("aria-busy", "false")
            group = create_catalog(
                admin, "groups", "Literal group " + secrets.token_hex(5), language
            )
            catalog = admin_context.request.get("/api/admin/catalogs").json()
            assigned = admin_context.request.put(
                "/api/academic/assignments",
                headers=csrf(admin_context),
                data={
                    "user_id": user_id,
                    "group_id": group["group_id"],
                    "subject_id": catalog["subjects"][0]["subject_id"],
                    "term_id": catalog["terms"][0]["term_id"],
                    "assigned": True,
                },
            )
            assert assigned.status == 200
            literal_group_labels(admin, admin_context, group, username, language, artifacts, errors)
            print(
                "PASS literal group catalog/profile/assignment/registration/roster: " + language,
                flush=True,
            )
        close_dialogs(admin)
        pending = browser.new_context(base_url=origin)
        username = "forced_teacher_" + secrets.token_hex(4)
        password = secrets.token_urlsafe(24)
        assert (
            pending.request.post(
                "/api/auth/register",
                data={
                    "login": username,
                    "password": password,
                    "display_name": "Forced teacher approval",
                    "requested_role": "teacher",
                },
            ).status
            == 201
        )
        assert (
            pending.request.post(
                "/api/auth/login", data={"login": username, "password": password}
            ).status
            == 200
        )
        user_id = pending.request.get("/api/auth/me").json()["user"]["user_id"]
        denied = admin_context.request.post(
            f"/api/admin/requests/{user_id}/approve",
            headers=csrf(admin_context),
            data={"role_code": "head"},
        )
        assert denied.status == 422 and denied.json()["error"]["code"] == "teacher_role_required"
        route(admin, "requests")
        admin.get_by_role("row").filter(has_text=username).get_by_role(
            "button", name=text("requests.approve", "en"), exact=True
        ).click()
        expect(
            top(admin).get_by_label(text("requests.actualRole", "en"), exact=True)
        ).to_have_count(0)
        with admin.expect_response(
            partial(response_matches, path=f"/api/admin/teachers/{user_id}/approve", method="POST")
        ) as forced:
            top(admin).locator("button[type=submit]").click()
        assert forced.value.status == 200 and forced.value.request.post_data_json == {}
        expect(admin.locator("dialog[open]")).to_have_count(0)
        assert pending.request.get("/api/auth/me").json()["user"]["role_code"] == "teacher"
        assert (
            admin_context.request.patch(
                f"/api/admin/users/{user_id}",
                headers=csrf(admin_context),
                data={"role_code": "head"},
            ).status
            == 200
        )
        assert (
            admin_context.request.get("/api/admin/users", params={"q": username}).json()["items"][
                0
            ]["role_code"]
            == "head"
        )
        pending.close()
        rejected = browser.new_context(base_url=origin)
        username = "forced_reject_" + secrets.token_hex(4)
        password = secrets.token_urlsafe(24)
        assert (
            rejected.request.post(
                "/api/auth/register",
                data={
                    "login": username,
                    "password": password,
                    "display_name": "Teacher rejection entry",
                    "requested_role": "teacher",
                },
            ).status
            == 201
        )
        assert (
            rejected.request.post(
                "/api/auth/login", data={"login": username, "password": password}
            ).status
            == 200
        )
        rejected_id = rejected.request.get("/api/auth/me").json()["user"]["user_id"]
        route(admin, "requests")
        admin.get_by_role("row").filter(has_text=username).get_by_role(
            "button", name=text("requests.reject", "en"), exact=True
        ).click()
        top(admin).locator("textarea").fill("The teaching affiliation requires correction.")
        with admin.expect_response(
            partial(
                response_matches, path=f"/api/admin/teachers/{rejected_id}/reject", method="POST"
            )
        ) as decision:
            top(admin).locator("button[type=submit]").click()
        assert decision.value.status == 200
        expect(admin.locator("dialog[open]")).to_have_count(0)
        assert rejected.request.get("/api/auth/me").status == 401
        assert (
            rejected.request.post(
                "/api/auth/login", data={"login": username, "password": password}
            ).status
            == 200
        )
        assert rejected.request.get("/api/auth/me").json()["user"]["approval_status"] == "rejected"
        rejected.close()
        assert not errors
        browser.close()
    return {
        "status": "passed",
        "locales": ["ru", "en"],
        "literal_group_names_preserved": True,
        "registration_options_refresh_on_reentry": True,
        "correction_options_refresh_on_reopen": True,
        "archived_groups_removed_without_reload": True,
        "catalog_profile_assignment_registration_roster": True,
        "original_names_restored": True,
        "generic_teacher_approval_forced": True,
        "role_change_after_approval_only": True,
        "generic_teacher_rejection_uses_dedicated_endpoint": True,
        "javascript_errors": 0,
    }


def create_teacher_task(
    page: Page, context: BrowserContext, assignment: dict[str, Any], language: str
) -> int:
    route(page, "tasks")
    page.get_by_role("button", name=text("learning.create", language), exact=True).click()
    dialog = top(page)
    expect(dialog.get_by_label(text("Оқу тобы", language), exact=True)).to_have_count(0)
    dialog.locator("#task-assignment").select_option(str(assignment["assignment_id"]))
    assert dialog.locator("#task-assignment option").count() == 3
    dialog.get_by_label(text("learning.taskTitle", language), exact=True).fill(
        "Exact teacher scope " + secrets.token_hex(3)
    )
    dialog.get_by_label(text("learning.description", language), exact=True).fill(
        "Complete the task in the assigned teaching combination."
    )
    dates = page.evaluate("""() => {
      const local = value => {
        const d = new Date(value);
        return new Date(d - d.getTimezoneOffset()*60000).toISOString().slice(0,19);
      };
      return {publication:local(Date.now()-3600000),deadline:local(Date.now()+86400000)};
    }""")
    dialog.get_by_label(text("learning.publication", language), exact=True).fill(
        dates["publication"]
    )
    dialog.get_by_label(text("learning.deadline", language), exact=True).fill(dates["deadline"])
    with page.expect_response(
        partial(response_matches, path="/api/tasks", method="POST")
    ) as response:
        dialog.locator("button[type=submit]").click()
    assert response.value.status == 201
    expect(page.locator("dialog[open]")).to_have_count(0)
    assert context.request.get("/api/academic/workload").json()["total"] == 2
    return int(response.value.json()["task_id"])


def run_teachers_browser(
    origin: str, admin_password: str, work: Path, artifacts: Path, browser_path: str | None
) -> dict[str, Any]:
    del work
    expect.set_options(timeout=30000)
    errors: list[str] = []
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
        initial = admin_context.request.get("/api/admin/catalogs").json()
        seed_group = initial["groups"][0]
        seed_other_group = initial["groups"][1]
        seed_subject = initial["subjects"][0]
        seed_term = initial["terms"][0]
        for language in ("kk", "ru", "en"):
            close_dialogs(admin)
            admin.locator("#locale-app").select_option(language)
            expect(admin.locator("#page-content")).to_have_attribute("aria-busy", "false")
            token = secrets.token_hex(3)
            group = create_catalog(admin, "groups", f"UI {language} group {token}", language)
            subject = create_catalog(admin, "subjects", f"UI {language} subject {token}", language)
            term = create_catalog(admin, "terms", f"UI {language} term {token}", language)
            catalog_view(admin, "subjects")
            admin.get_by_role("row").filter(has_text=subject["name"]).get_by_role(
                "button", name=text("Өзгерту", language), exact=True
            ).click()
            subject["name"] += " revised"
            top(admin).get_by_label(text("catalogs.name", language), exact=True).fill(
                subject["name"]
            )
            top(admin).locator("button[type=submit]").click()
            expect(admin.locator("dialog[open]")).to_have_count(0)
            username = "teacher_ui_" + language + "_" + secrets.token_hex(4)
            password = secrets.token_urlsafe(24)
            context = browser.new_context(base_url=origin, viewport={"width": 1440, "height": 1000})
            owner = context.new_page()
            owner.on("pageerror", lambda error: errors.append(str(error)))
            owner.goto(origin)
            expect(owner.locator("#auth-screen")).to_be_visible()
            owner.locator("#locale-auth").select_option(language)
            owner.locator("#register-tab").click()
            expect(owner.locator("#student-claim-name")).to_be_disabled()
            owner.locator("#display-name").fill("Teacher UI " + language)
            owner.locator("#login").fill(username)
            owner.locator("#password").fill(password)
            with owner.expect_response(
                partial(response_matches, path="/api/auth/register", method="POST")
            ) as registered:
                owner.locator("#auth-submit").click()
            assert registered.value.status == 201
            registration = registered.value.request.post_data_json
            assert isinstance(registration, dict) and registration["requested_role"] == "teacher"
            assert not any(key.startswith("student_claim_") for key in registration)
            owner.locator("#login").fill(username)
            owner.locator("#password").fill(password)
            owner.locator("#auth-submit").click()
            expect(owner.locator(".approval-page")).to_be_visible()
            expect(owner.locator("dialog[data-context-help][open]")).to_be_visible()
            close_dialogs(owner)
            pending = context.request.get("/api/auth/me").json()["user"]
            assert pending["role_code"] == "pending" and pending["active"] is False
            assert context.request.get("/api/tasks").status == 403
            teacher_search(admin, username, "requests", language)
            admin.get_by_role("row").filter(has_text=username).get_by_role(
                "button", name=text("requests.approve", language), exact=True
            ).click()
            with admin.expect_response(
                partial(
                    response_matches,
                    path=f"/api/admin/teachers/{pending['user_id']}/approve",
                    method="POST",
                )
            ) as approved:
                top(admin).locator("button[type=submit]").click()
            assert approved.value.status == 200 and approved.value.request.post_data_json == {}
            expect(admin.locator("dialog[open]")).to_have_count(1)
            expect(top(admin)).to_contain_text(text("teachers.profile", language))
            refresh_owner(owner, language)
            approved_user = context.request.get("/api/auth/me").json()["user"]
            assert approved_user["role_code"] == "teacher" and approved_user["active"] is True
            expect(owner.locator("#page-content")).to_contain_text(
                text("teachers.noAssignments", language)
            )
            route(owner, "tasks")
            expect(
                owner.get_by_role("button", name=text("learning.create", language), exact=True)
            ).to_have_count(0)
            for endpoint in ("/api/admin/teachers", "/api/admin/catalogs"):
                assert context.request.get(endpoint).status == 403
            expect(owner.locator(".nav-link[data-route='teachers']")).to_have_count(0)
            expect(owner.locator(".nav-link[data-route='catalogs']")).to_have_count(0)
            add_assignment(
                admin, group["group_id"], subject["subject_id"], term["term_id"], language
            )
            add_assignment(
                admin,
                seed_group["group_id"],
                seed_subject["subject_id"],
                seed_term["term_id"],
                language,
            )
            add_assignment(
                admin,
                seed_group["group_id"],
                seed_subject["subject_id"],
                seed_term["term_id"],
                language,
            )
            detail = admin_context.request.get(f"/api/admin/teachers/{pending['user_id']}").json()
            assert len(detail["assignments"]) == 2
            workload = context.request.get("/api/academic/workload").json()
            assert workload["total"] == 2
            exact = next(
                item for item in workload["items"] if item["group_id"] == group["group_id"]
            )
            if language in {"ru", "en"}:
                literal_group_labels(
                    admin, admin_context, group, username, language, artifacts, errors
                )
            task_id = create_teacher_task(owner, context, exact, language)
            forged = context.request.post(
                "/api/tasks",
                headers=csrf(context),
                data={
                    "title": "Denied cross product",
                    "description": "This tuple was never assigned.",
                    "rules": "",
                    "group_id": group["group_id"],
                    "subject_id": seed_subject["subject_id"],
                    "term_id": term["term_id"],
                    "published_at": datetime.now(UTC).isoformat(),
                    "deadline": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                    "allow_late": False,
                },
            )
            assert forged.status == 403
            row = top(admin).get_by_role("row").filter(has_text=group["name"])
            row.get_by_role("button", name=text("Өзгерту", language), exact=True).click()
            top(admin).get_by_label(text("Оқу тобы", language), exact=True).select_option(
                str(seed_other_group["group_id"])
            )
            with admin.expect_response(
                partial(
                    response_matches,
                    path=f"/api/academic/assignments/{exact['assignment_id']}",
                    method="PATCH",
                )
            ) as changed:
                top(admin).locator("button[type=submit]").click()
            assert changed.value.status == 200
            expect(admin.locator("dialog[open]")).to_have_count(1)
            assert context.request.get(f"/api/tasks/{task_id}").status == 403
            assert admin_context.request.get(f"/api/tasks/{task_id}").status == 200
            row = top(admin).get_by_role("row").filter(has_text=seed_group["name"])
            row.get_by_role("button", name=text("Қайтарып алу", language), exact=True).click()
            top(admin).locator("button[type=submit]").click()
            expect(admin.locator("dialog[open]")).to_have_count(1)
            assert context.request.get("/api/academic/workload").json()["total"] == 1
            assert (
                context.request.get(
                    "/api/students", params={"group_id": seed_group["group_id"]}
                ).status
                == 403
            )
            close_dialogs(admin)
            teacher_search(
                admin,
                username,
                "teachers",
                language,
                str(seed_other_group["group_id"]),
                str(subject["subject_id"]),
            )
            admin.screenshot(
                path=str(artifacts / f"teachers-{language}.png"),
                full_page=True,
                animations="disabled",
            )
            route(owner, "grades")
            for key, value in (
                ("Оқу тобы", seed_other_group["group_id"]),
                ("Пән", subject["subject_id"]),
                ("Оқу кезеңі", term["term_id"]),
            ):
                owner.get_by_label(text(key, language), exact=True).select_option(str(value))
            owner.locator(".filter-bar button[type=submit]").click()
            expect(owner.locator("#page-content")).to_have_attribute("aria-busy", "false")
            toggle_catalog(admin, "subjects", subject, language)
            assert context.request.get("/api/academic/workload").json()["total"] == 0
            route(owner, "tasks")
            expect(owner.locator("#page-content")).to_contain_text(
                text("teachers.noAssignments", language)
            )
            expect(
                owner.get_by_role("button", name=text("learning.create", language), exact=True)
            ).to_have_count(0)
            route(owner, "grades")
            expect(owner.locator("#page-content")).to_contain_text(
                text("teachers.noAssignments", language)
            )
            expect(owner.locator("#page-content")).to_contain_text(
                text("grades.filtersReset", language)
            )
            for key in ("Оқу тобы", "Пән", "Оқу кезеңі"):
                expect(owner.get_by_label(text(key, language), exact=True)).to_have_value("")
            expect(owner.locator("tbody")).to_have_count(0)
            toggle_catalog(admin, "subjects", subject, language)
            assert context.request.get("/api/academic/workload").json()["total"] == 1
            route(owner, "dashboard")
            expect(owner.locator("#page-content")).to_contain_text(
                text("teachers.myWorkload", language)
            )
            owner.reload()
            expect(owner.locator("#page-content")).to_have_attribute("aria-busy", "false")
            expect(owner.locator("#locale-app")).to_have_value(language)
            expect(owner.locator("#page-content")).to_contain_text(subject["name"])
            owner.screenshot(
                path=str(artifacts / f"teacher-workload-{language}.png"),
                full_page=True,
                animations="disabled",
            )
            teacher_search(admin, username, "teachers", language)
            admin.get_by_role("row").filter(has_text=username).get_by_role(
                "button", name=text("teachers.profile", language), exact=True
            ).click()
            top(admin).get_by_role(
                "button", name=text("teachers.deactivate", language), exact=True
            ).click()
            top(admin).locator("button[type=submit]").click()
            expect(admin.locator("dialog[open]")).to_have_count(1)
            assert context.request.get("/api/auth/me").status == 401
            top(admin).get_by_role(
                "button", name=text("teachers.activate", language), exact=True
            ).click()
            top(admin).locator("button[type=submit]").click()
            expect(admin.locator("dialog[open]")).to_have_count(1)
            assert context.request.get("/api/auth/me").status == 401
            assert (
                context.request.post(
                    "/api/auth/login", data={"login": username, "password": password}
                ).status
                == 200
            )
            context.close()
            print(
                "PASS teacher approval, catalogs, exact workload/edit/revoke/archive/session: "
                + language,
                flush=True,
            )
        close_dialogs(admin)
        for role in ("head", "auditor"):
            username, password = register_staff(admin_context, role)
            context = browser.new_context(base_url=origin)
            assert (
                context.request.post(
                    "/api/auth/login", data={"login": username, "password": password}
                ).status
                == 200
            )
            assert context.request.get("/api/admin/teachers").status == 403
            assert context.request.get("/api/admin/catalogs").status == 403
            context.close()
        # Dedicated rejection ends the pending session; a fresh login shows only its private status.
        username = "rejected_teacher_" + secrets.token_hex(4)
        password = secrets.token_urlsafe(24)
        assert (
            admin_context.request.post(
                "/api/auth/register",
                data={
                    "login": username,
                    "display_name": "Rejected teacher",
                    "password": password,
                    "requested_role": "teacher",
                },
            ).status
            == 201
        )
        context = browser.new_context(base_url=origin)
        assert (
            context.request.post(
                "/api/auth/login", data={"login": username, "password": password}
            ).status
            == 200
        )
        user_id = context.request.get("/api/auth/me").json()["user"]["user_id"]
        teacher_search(admin, username, "requests", "en")
        admin.get_by_role("row").filter(has_text=username).get_by_role(
            "button", name=text("requests.reject", "en"), exact=True
        ).click()
        top(admin).locator("textarea").fill("Please correct the teacher application details.")
        top(admin).locator("button[type=submit]").click()
        expect(admin.locator("dialog[open]")).to_have_count(0)
        assert context.request.get("/api/auth/me").status == 401
        assert (
            context.request.post(
                "/api/auth/login", data={"login": username, "password": password}
            ).status
            == 200
        )
        assert context.request.get("/api/auth/me").json()["user"]["approval_status"] == "rejected"
        assert context.request.get("/api/tasks").status == 403
        assert admin_context.request.get(f"/api/admin/teachers/{user_id}").status == 200
        context.close()
        admin.set_viewport_size({"width": 390, "height": 844})
        route(admin, "teachers")
        admin.screenshot(
            path=str(artifacts / "teachers-mobile.png"), full_page=False, animations="disabled"
        )
        assert admin.evaluate("document.documentElement.scrollWidth<=innerWidth")
        assert not errors
        browser.close()
    return {
        "status": "passed",
        "locales": ["kk", "ru", "en"],
        "teacher_registration_pending": True,
        "dedicated_approval_forces_teacher": True,
        "catalog_create_rename_archive_restore": True,
        "multiple_exact_assignments_no_duplicates": True,
        "atomic_assignment_edit_and_revoke": True,
        "cross_product_task_denied": True,
        "task_selector_exact_assignments_only": True,
        "live_workload_and_archive_revocation": True,
        "revoked_grade_filters_recover": True,
        "literal_group_names_preserved": True,
        "deactivate_revokes_sessions_no_revival": True,
        "dedicated_rejection_revokes_pending_session": True,
        "admin_only_roles_denied": True,
        "reload_locale_persisted": True,
        "mobile_no_overflow": True,
        "javascript_errors": 0,
    }
