"""Real UI claims flow, used by browser_smoke.py --student-claims-only."""

import json
import secrets
import sqlite3
from contextlib import closing
from functools import partial
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, Locator, Page, Response, expect, sync_playwright

CATALOG = json.loads(
    (Path(__file__).resolve().parents[1] / "app/static/locales.json").read_text("utf-8")
)


def text(key: str, language: str) -> str:
    return str(CATALOG[key][language])


def csrf(context: BrowserContext) -> dict[str, str]:
    return {
        "X-CSRF-Token": next(row["value"] for row in context.cookies() if row["name"] == "arm_csrf")
    }


def approval_response(response: Response, user_id: int) -> bool:
    return (
        response.url.endswith(f"/api/admin/requests/{user_id}/approve")
        and response.request.method == "POST"
    )


def close_dialogs(page: Page) -> None:
    while page.locator("dialog[open]").count():
        before = page.locator("dialog[open]").count()
        page.locator("dialog[open]").last.locator(".dialog-close").click()
        page.wait_for_function(
            "count => document.querySelectorAll('dialog[open]').length < count", arg=before
        )


def refresh_owner(page: Page, language: str) -> None:
    page.get_by_role("button", name=text("approval.refresh", language), exact=True).click()
    expect(page.locator("#page-content")).to_have_attribute("aria-busy", "false")
    close_dialogs(page)


def requests_page(page: Page, language: str) -> None:
    close_dialogs(page)
    page.locator("#locale-app").select_option(language)
    expect(page.locator("#page-content")).to_have_attribute("aria-busy", "false")
    page.locator(".nav-link[data-route='requests']").click()
    expect(page.locator(".nav-link[data-route='requests']")).to_have_attribute(
        "aria-current", "page"
    )
    expect(page.locator("#page-content")).to_have_attribute("aria-busy", "false")
    page.get_by_role("button", name=text("↻  Жаңарту", language), exact=True).click()
    expect(page.locator("#page-content")).to_have_attribute("aria-busy", "false")


def claim_values(dialog: Locator, prefix: str, student: dict[str, Any], email: str) -> None:
    dialog.locator(f"#{prefix}-name").fill(student["display_name"])
    dialog.locator(f"#{prefix}-number").fill(" " + student["student_code"].lower() + " ")
    dialog.locator(f"#{prefix}-group").select_option(str(student["group_id"]))
    dialog.locator(f"#{prefix}-email").fill(email)


def run_claims_browser(
    origin: str, admin_password: str, work: Path, artifacts: Path, browser_path: str | None
) -> dict[str, Any]:
    expect.set_options(timeout=30000)
    errors: list[str] = []
    with closing(sqlite3.connect(work / "audit.db")) as connection:
        connection.row_factory = sqlite3.Row
        students = [
            dict(row)
            for row in connection.execute("SELECT * FROM Students ORDER BY student_id LIMIT 3")
        ]
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
        options_response = admin_context.request.get("/api/auth/registration-options")
        assert options_response.status == 200
        options = options_response.json()
        assert set(options) == {"groups", "student_email_required", "student_email_domain"}
        assert all(set(group) == {"group_id", "name"} for group in options["groups"])
        initial_audit = admin_context.request.get("/api/audit", params={"page_size": 100}).json()[
            "items"
        ]
        initial_id = max((item["audit_id"] for item in initial_audit), default=0)
        admin = admin_context.new_page()
        admin.on("pageerror", lambda error: errors.append(str(error)))
        admin.goto(origin)
        expect(admin.locator("#page-content")).to_have_attribute("aria-busy", "false")
        expect(admin.locator("dialog[data-context-help][open]")).to_be_visible()
        close_dialogs(admin)
        private_values: list[str] = []
        for language, student in zip(("kk", "ru", "en"), students, strict=True):
            username = "claim_" + language + "_" + secrets.token_hex(4)
            password = secrets.token_urlsafe(24)
            email = username + "@example.edu"
            unverified_number = "CLAIM-" + secrets.token_hex(5).upper()
            owner_context = browser.new_context(
                base_url=origin, viewport={"width": 1280, "height": 1000}
            )
            owner = owner_context.new_page()
            owner.on("pageerror", lambda error: errors.append(str(error)))
            owner.goto(origin)
            expect(owner.locator("#auth-screen")).to_be_visible()
            owner.locator("#locale-auth").select_option(language)
            owner.locator("#register-tab").click()
            expect(owner.locator("#student-claim-name")).to_be_disabled()
            owner.locator("#requested-role").select_option("student")
            expect(owner.locator("#student-claim-group option[value='1']")).to_have_count(1)
            expect(owner.locator("#student-claim-name")).to_be_enabled()
            assert (
                owner.locator("#student-claim-email").evaluate("node => node.required")
                == options["student_email_required"]
            )
            owner.locator("#display-name").fill("Account display " + language)
            owner.locator("#login").fill(username)
            owner.locator("#password").fill(password)
            owner.locator("#student-claim-name").fill(student["display_name"])
            owner.locator("#student-claim-number").fill(unverified_number.lower())
            wrong_group = next(
                row["group_id"]
                for row in options["groups"]
                if row["group_id"] != student["group_id"]
            )
            owner.locator("#student-claim-group").select_option(str(wrong_group))
            owner.locator("#student-claim-email").fill(email)
            if options["student_email_required"]:
                owner.locator("#student-claim-email").fill(username + "@invalid.example")
                with owner.expect_response(
                    lambda response: (
                        response.url.endswith("/api/auth/register")
                        and response.request.method == "POST"
                    )
                ) as wrong_email:
                    owner.locator("#auth-submit").click()
                assert wrong_email.value.status == 422
                expect(owner.locator("#auth-message")).to_contain_text(
                    text("error.student_email_domain", language)
                )
                owner.locator("#student-claim-email").fill(email)
            with owner.expect_response(
                lambda response: (
                    response.url.endswith("/api/auth/register")
                    and response.request.method == "POST"
                )
            ) as registered:
                owner.locator("#auth-submit").click()
            assert registered.value.status == 201
            registration_payload = registered.value.request.post_data_json
            assert isinstance(registration_payload, dict)
            assert registration_payload["student_claim_number"] == unverified_number
            duplicate = admin_context.request.post(
                "/api/auth/register",
                data={
                    "login": "duplicate_" + secrets.token_hex(4),
                    "password": secrets.token_urlsafe(24),
                    "display_name": "Duplicate claim fixture",
                    "requested_role": "student",
                    "student_claim_name": student["display_name"],
                    "student_claim_number": unverified_number.lower(),
                    "student_claim_group_id": wrong_group,
                    "student_claim_email": email,
                },
            )
            assert (
                duplicate.status == 409
                and duplicate.json()["error"]["code"] == "student_claim_number_unavailable"
            )
            owner.locator("#login").fill(username)
            owner.locator("#password").fill(password)
            owner.locator("#auth-submit").click()
            expect(owner.locator(".approval-page")).to_be_visible()
            expect(owner.locator("dialog[data-context-help][open]")).to_be_visible()
            close_dialogs(owner)
            expect(owner.locator("#page-content")).to_contain_text(unverified_number)
            expect(owner.locator("#page-content")).to_contain_text(email)
            assert owner_context.request.get("/api/academic/catalog").status == 403
            assert owner_context.request.get("/api/tasks").status == 403
            me = owner_context.request.get("/api/auth/me").json()["user"]
            assert me["student_id"] is None and me["active"] is False
            user_id = me["user_id"]
            assert owner_context.request.get(f"/api/admin/requests/{user_id}").status == 403
            owner.locator("#user-menu > summary").click()
            owner.locator("#logout-button").click()
            expect(owner.locator("#auth-screen")).to_be_visible()
            expect(owner.locator("#page-content > *")).to_have_count(0)
            expect(owner.locator("#page-content")).to_have_text("")
            expect(owner.locator("dialog[open]")).to_have_count(0)
            owner.locator("#login").fill(username)
            owner.locator("#password").fill(password)
            owner.locator("#auth-submit").click()
            expect(owner.locator(".approval-page")).to_be_visible()
            close_dialogs(owner)
            expect(owner.locator("#page-content")).to_contain_text(unverified_number)
            expect(owner.locator("#page-content")).to_contain_text(email)
            requests_page(admin, language)
            row = admin.get_by_role("row").filter(has_text=username)
            expect(row).to_contain_text(student["display_name"])
            assert unverified_number not in row.inner_text() and email not in row.inner_text()
            public_user = admin_context.request.get(
                "/api/admin/users", params={"q": username}
            ).json()["items"][0]
            assert "student_claim" not in public_user
            row.get_by_role("button", name=text("requests.approve", language), exact=True).click()
            dialog = admin.locator("dialog[open]").last
            expect(dialog).to_contain_text(unverified_number)
            expect(dialog).to_contain_text(email)
            expect(
                dialog.get_by_label(text("requests.student", language), exact=True)
            ).to_have_value("")
            expect(
                dialog.get_by_role(
                    "checkbox", name=text("claim.confirmMatch", language), exact=True
                )
            ).not_to_be_checked()
            dialog.get_by_label(text("requests.student", language), exact=True).select_option(
                str(student["student_id"])
            )
            expect(dialog.locator(".claim-mismatch-ack")).to_be_visible()
            assert not dialog.locator("form").evaluate("node => node.checkValidity()")
            detail = admin_context.request.get(f"/api/admin/requests/{user_id}").json()
            denied = admin_context.request.post(
                f"/api/admin/requests/{user_id}/approve",
                headers=csrf(admin_context),
                data={
                    "role_code": "student",
                    "student_id": student["student_id"],
                    "confirm_student_match": True,
                    "claim_revision": detail["claim_revision"],
                },
            )
            assert (
                denied.status == 409 and denied.json()["error"]["code"] == "student_claim_mismatch"
            )
            dialog.get_by_role(
                "button", name=text("claim.needsReview", language), exact=True
            ).click()
            reason_dialog = admin.locator("dialog[open]").last
            reason_dialog.locator("textarea").fill(
                "Please verify the student number and study group."
            )
            reason_dialog.locator("button[type=submit]").click()
            expect(admin.locator("dialog[open]")).to_have_count(0)
            refresh_owner(owner, language)
            expect(owner.locator("#page-content h1")).to_have_text(
                text("claim.needsReviewTitle", language)
            )
            owner.screenshot(path=str(artifacts / f"claim-needs-review-{language}.png"))
            # Keep an old admin snapshot open while the owner corrects their own claim.
            requests_page(admin, language)
            admin.get_by_role("row").filter(has_text=username).get_by_role(
                "button", name=text("requests.approve", language), exact=True
            ).click()
            stale_dialog = admin.locator("dialog[open]").last
            stale_dialog.get_by_label(text("requests.student", language), exact=True).select_option(
                str(student["student_id"])
            )
            stale_dialog.get_by_role(
                "checkbox", name=text("claim.confirmMatch", language), exact=True
            ).check()
            stale_dialog.get_by_role(
                "checkbox", name=text("claim.acknowledgeMismatch", language), exact=True
            ).check()
            owner.get_by_role("button", name=text("claim.correct", language), exact=True).click()
            correction = owner.locator("dialog[open]").last
            claim_values(correction, "correct-claim", student, email)
            correction.locator("button[type=submit]").click()
            expect(owner.locator("dialog[open]")).to_have_count(0)
            expect(owner.locator("#page-content")).to_contain_text(student["student_code"])
            with admin.expect_response(partial(approval_response, user_id=user_id)) as stale:
                stale_dialog.locator("button[type=submit]").click()
            assert (
                stale.value.status == 409
                and stale.value.json()["error"]["code"] == "request_changed"
            )
            expect(stale_dialog.locator(".dialog-error")).to_be_visible()
            close_dialogs(admin)
            requests_page(admin, language)
            admin.get_by_role("row").filter(has_text=username).get_by_role(
                "button", name=text("requests.reject", language), exact=True
            ).click()
            rejected_dialog = admin.locator("dialog[open]").last
            rejected_dialog.locator("textarea").fill(
                "Please resubmit the corrected student information."
            )
            rejected_dialog.locator("button[type=submit]").click()
            expect(admin.locator("dialog[open]")).to_have_count(0)
            refresh_owner(owner, language)
            expect(owner.locator("#page-content h1")).to_have_text(
                text("approval.rejectedTitle", language)
            )
            owner.get_by_role("button", name=text("claim.correct", language), exact=True).click()
            corrected_again = owner.locator("dialog[open]").last
            claim_values(corrected_again, "correct-claim", student, email)
            corrected_again.locator("button[type=submit]").click()
            expect(owner.locator("dialog[open]")).to_have_count(0)
            assert (
                owner_context.request.get("/api/auth/me").json()["user"]["approval_status"]
                == "pending"
            )
            requests_page(admin, language)
            admin.get_by_role("row").filter(has_text=username).get_by_role(
                "button", name=text("requests.approve", language), exact=True
            ).click()
            matching = admin.locator("dialog[open]").last
            matching.get_by_label(text("requests.student", language), exact=True).select_option(
                str(student["student_id"])
            )
            expect(matching.locator(".claim-mismatch-ack")).to_be_hidden()
            matching.get_by_role(
                "checkbox", name=text("claim.confirmMatch", language), exact=True
            ).check()
            admin.screenshot(path=str(artifacts / f"claim-confirm-match-{language}.png"))
            matching.locator("button[type=submit]").click()
            expect(admin.locator("dialog[open]")).to_have_count(0)
            refresh_owner(owner, language)
            expect(owner.locator(".approval-page")).to_have_count(0)
            expect(owner.locator(".nav-link[data-route='tasks']")).to_be_visible()
            approved = owner_context.request.get("/api/auth/me").json()["user"]
            assert approved["student_id"] == student["student_id"] and approved["active"] is True
            locked = owner_context.request.put(
                "/api/auth/student-claim",
                headers=csrf(owner_context),
                data={
                    "student_claim_name": "Cannot replace a linked identity",
                    "student_claim_number": "LOCKED-" + secrets.token_hex(3).upper(),
                    "student_claim_group_id": student["group_id"],
                    "student_claim_email": email,
                },
            )
            assert locked.status == 403
            private_values.extend((unverified_number, student["student_code"], email))
            owner_context.close()
            print(
                "PASS student claim UI, private review, correction, stale snapshot and approval: "
                + language,
                flush=True,
            )
        new_rows = [
            row
            for row in admin_context.request.get("/api/audit", params={"page_size": 100}).json()[
                "items"
            ]
            if row["audit_id"] > initial_id
        ]
        audit_text = json.dumps(new_rows, ensure_ascii=False)
        assert all(value not in audit_text for value in private_values)
        server_log = artifacts / (
            "student-claims-email-server.log"
            if options["student_email_required"]
            else "student-claims-server.log"
        )
        assert all(
            value not in server_log.read_text(encoding="utf-8", errors="replace")
            for value in private_values
        )
        assert not errors
        browser.close()
    return {
        "status": "passed",
        "locales": ["kk", "ru", "en"],
        "email_required": options["student_email_required"],
        "public_options_no_roster": True,
        "masked_lists_private_details": True,
        "logout_private_content_cleared": True,
        "needs_review_correction": True,
        "rejected_claim_correction": True,
        "mismatch_explicit_acknowledgement": True,
        "stale_snapshot_rejected": True,
        "approval_without_relogin": True,
        "approved_claim_locked": True,
        "private_values_absent_from_audit": True,
        "private_values_absent_from_server_log": True,
        "javascript_errors": 0,
    }
