"""Learning UI flow for browser_smoke; it uses only the caller's isolated app."""

import json
import secrets
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing as close_connection
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, Locator, Page, expect

ROOT = Path(__file__).resolve().parents[1]
MESSAGES: dict[str, dict[str, str]] = json.loads(
    (ROOT / "app/static/locales.json").read_text(encoding="utf-8")
)


def _label(key: str, locale: str) -> str:
    return MESSAGES[key][locale]


def _csrf(context: BrowserContext) -> dict[str, str]:
    token = next(cookie["value"] for cookie in context.cookies() if cookie["name"] == "arm_csrf")
    return {"X-CSRF-Token": token}


def _top(page: Page) -> Locator:
    return page.locator("dialog[open]").last


def _close_dialogs(page: Page) -> None:
    for _ in range(10):
        count = page.locator("dialog[open]").count()
        if not count:
            return
        _top(page).locator(".dialog-close").click()
        page.wait_for_function(
            "previous => document.querySelectorAll('dialog[open]').length < previous",
            arg=count,
        )
    raise AssertionError("Learning UI left too many nested dialogs")


def _dismiss_help(page: Page, callback: Callable[[Page], None] | None) -> None:
    if callback is not None:
        callback(page)
    elif page.locator("dialog[data-context-help][open]").count():
        page.locator("dialog[data-context-help][open] .dialog-close").click()


def _tasks(
    page: Page,
    locale: str,
    navigate: Callable[[Page, str], None] | None,
    dismiss_help: Callable[[Page], None] | None,
) -> None:
    _close_dialogs(page)
    if page.url.endswith("#tasks"):
        with page.expect_response(
            lambda item: item.url.endswith("/api/tasks") and item.request.method == "GET"
        ):
            page.get_by_role("button", name=_label("↻  Жаңарту", locale), exact=True).click()
    elif navigate is not None:
        navigate(page, "tasks")
    else:
        if page.locator("#nav-toggle").is_visible():
            page.locator("#nav-toggle").click()
        page.locator(".nav-link[href='#tasks']").click()
    expect(page.locator("#page-content")).to_have_attribute("aria-busy", "false")
    _dismiss_help(page, dismiss_help)
    expect(page.locator("#page-content h1")).to_have_text(_label("learning.title", locale))


def _card(page: Page, title: str) -> Locator:
    card = page.locator(".task-card").filter(
        has=page.get_by_role("heading", name=title, exact=True)
    )
    expect(card).to_have_count(1)
    return card


def _review_dialog(page: Page, title: str, locale: str) -> Locator:
    _card(page, title).get_by_role(
        "button", name=_label("learning.submissions", locale), exact=True
    ).click()
    _top(page).get_by_role(
        "button", name=_label("learning.review", locale), exact=True
    ).first.click()
    expect(_top(page).locator(".submission-text")).to_be_visible()
    return _top(page)


def _edit_dialog(page: Page, title: str, locale: str) -> Locator:
    _card(page, title).get_by_role(
        "button", name=_label("learning.details", locale), exact=True
    ).click()
    _top(page).get_by_role("button", name=_label("learning.edit", locale), exact=True).click()
    expect(page.locator("dialog[open]")).to_have_count(1)
    return _top(page)


def _reload_tasks(page: Page, locale: str, dismiss_help: Callable[[Page], None] | None) -> None:
    page.reload()
    expect(page.locator("#page-content h1")).to_have_text(_label("learning.title", locale))
    expect(page.locator("#page-content")).to_have_attribute("aria-busy", "false")
    _dismiss_help(page, dismiss_help)


def run_learning_flow(
    origin: str,
    admin_context: BrowserContext,
    teacher_context: BrowserContext,
    teacher_page: Page,
    student_context: BrowserContext,
    student_page: Page,
    artifacts: Path,
    errors: list[str],
    navigate: Callable[[Page, str], None] | None = None,
    dismiss_help: Callable[[Page], None] | None = None,
    database_path: Path | None = None,
) -> dict[str, object]:
    """Create/submit/review through UI, and verify live ownership/deadline APIs."""
    teacher_locale = teacher_page.locator("html").get_attribute("lang") or "kk"
    student_locale = student_page.locator("html").get_attribute("lang") or "en"
    assert teacher_locale == "kk" and student_locale == "en"
    for context, page in ((teacher_context, teacher_page), (student_context, student_page)):
        context.new_cdp_session(page).send(
            "Emulation.setTimezoneOverride", {"timezoneId": "Asia/Almaty"}
        )
        assert page.evaluate("new Date().getTimezoneOffset()") == -300
    title = "Browser learning proof " + secrets.token_hex(3)
    material_text = "Assignment material: <b>plain recorded text</b>."
    answer_file = b"Student attachment: <b>plain recorded text</b>."
    first_text = "My first reasoned answer. <strong>Recorded text, not markup.</strong>"
    revised_text = "My revised answer with an explicit explanation."
    draft_feedback = "Draft feedback withheld until the result is published."
    final_feedback = "Your revised explanation is complete and well supported."
    screenshots: list[str] = []
    artifacts.mkdir(parents=True, exist_ok=True)
    print("STAGE learning UI: teacher create task and material", flush=True)
    _tasks(teacher_page, teacher_locale, navigate, dismiss_help)
    teacher_page.get_by_role(
        "button", name=_label("learning.create", teacher_locale), exact=True
    ).click()
    dialog = _top(teacher_page)
    dialog.get_by_label(_label("learning.taskTitle", teacher_locale), exact=True).fill(title)
    dialog.get_by_label(_label("learning.description", teacher_locale), exact=True).fill(
        "Explain the access-control decision using the supplied material."
    )
    dialog.get_by_label(_label("learning.rules", teacher_locale), exact=True).fill(
        "Submit or update your own answer before the deadline."
    )
    assigned = next(
        row
        for row in teacher_context.request.get("/api/academic/workload").json()["items"]
        if row["group_id"] == row["subject_id"] == row["term_id"] == 1
    )
    dialog.locator("#task-assignment").select_option(str(assigned["assignment_id"]))
    dates: dict[str, str] = teacher_page.evaluate("""() => {
      const local = value => {
        const d = new Date(value);
        const localTime = d.getTime() - d.getTimezoneOffset() * 60000;
        return new Date(localTime).toISOString().slice(0, 16);
      };
      return { publication: local(Date.now() - 3600000), deadline: local(Date.now() + 86400000) };
    }""")
    dialog.get_by_label(_label("learning.publication", teacher_locale), exact=True).fill(
        dates["publication"]
    )
    dialog.get_by_label(_label("learning.deadline", teacher_locale), exact=True).fill(
        dates["deadline"]
    )
    dialog.get_by_role(
        "checkbox", name=_label("learning.allowLate", teacher_locale), exact=True
    ).uncheck()
    initial_file = dialog.get_by_label(
        _label("learning.initialMaterial", teacher_locale), exact=True
    )
    initial_file.set_input_files(
        {"name": "fake.pdf", "mimeType": "application/pdf", "buffer": b"not a PDF"}
    )
    with teacher_page.expect_response(
        lambda item: item.url.endswith("/api/tasks/with-material") and item.request.method == "POST"
    ) as rejected_file:
        dialog.locator("button[type=submit]").click()
    assert rejected_file.value.status == 422
    expect(dialog.locator(".dialog-error")).to_be_visible()
    assert not any(
        item["title"] == title for item in teacher_context.request.get("/api/tasks").json()["items"]
    )
    initial_file.set_input_files(
        {"name": "initial-material.txt", "mimeType": "text/plain", "buffer": material_text.encode()}
    )
    with teacher_page.expect_response(
        lambda item: item.url.endswith("/api/tasks/with-material") and item.request.method == "POST"
    ) as created:
        dialog.locator("button[type=submit]").click()
    assert created.value.status == 201
    task: dict[str, Any] = created.value.json()
    task_id = task["task_id"]
    expect(teacher_page.locator("dialog[open]")).to_have_count(0)
    assert task["group_id"] == task["subject_id"] == task["term_id"] == 1
    assert task["allow_late"] is False and task["status"] == "open"
    expected_initial = (
        datetime.fromisoformat(dates["deadline"])
        .replace(tzinfo=timezone(timedelta(hours=5)))
        .astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    assert task["deadline"] == expected_initial
    assert task["material_name"] == "initial-material.txt" and task["version"] == 0
    _card(teacher_page, title).get_by_role(
        "button", name=_label("learning.details", teacher_locale), exact=True
    ).click()
    _top(teacher_page).get_by_role(
        "button", name=_label("learning.uploadMaterial", teacher_locale), exact=True
    ).click()
    _top(teacher_page).get_by_label(
        _label("learning.material", teacher_locale), exact=True
    ).set_input_files(
        {
            "name": "learning-material.txt",
            "mimeType": "text/plain",
            "buffer": material_text.encode(),
        }
    )
    with teacher_page.expect_response(
        lambda item: (
            item.url.endswith(f"/api/tasks/{task_id}/material") and item.request.method == "POST"
        )
    ) as uploaded:
        _top(teacher_page).locator("button[type=submit]").click()
    assert uploaded.value.status == 200
    expect(teacher_page.locator("dialog[open]")).to_have_count(1)
    _close_dialogs(teacher_page)
    task = teacher_context.request.get(f"{origin}/api/tasks/{task_id}").json()
    assert task["material_name"] == "learning-material.txt"

    print("STAGE learning UI: deadline edit, UTC reload and late policy", flush=True)
    dialog = _edit_dialog(teacher_page, title, teacher_locale)
    past_local = teacher_page.evaluate("""() => {
      const d = new Date(Date.now() - 300000);
      return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 19);
    }""")
    deadline_input = dialog.get_by_label(_label("learning.deadline", teacher_locale), exact=True)
    deadline_input.fill(past_local)
    dialog.locator("button[type=submit]").click()
    expect(dialog.locator(".dialog-error")).to_have_text(
        _label("error.deadline_in_past", teacher_locale)
    )
    assert (
        teacher_context.request.get(f"/api/tasks/{task_id}").json()["deadline"] == expected_initial
    )
    edited_local = teacher_page.evaluate("""() => {
      const d = new Date(Date.now() + 172800000); d.setSeconds(41, 0);
      return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 19);
    }""")
    deadline_input.fill(edited_local)
    dialog.get_by_role(
        "checkbox", name=_label("learning.allowLate", teacher_locale), exact=True
    ).check()
    with teacher_page.expect_response(
        lambda item: item.url.endswith(f"/api/tasks/{task_id}") and item.request.method == "PATCH"
    ) as edited:
        dialog.locator("button[type=submit]").click()
    assert edited.value.status == 200
    expected_deadline = (
        datetime.fromisoformat(edited_local)
        .replace(tzinfo=timezone(timedelta(hours=5)))
        .astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    assert edited.value.json()["deadline"] == expected_deadline
    expect(teacher_page.locator("dialog[open]")).to_have_count(0)
    _reload_tasks(teacher_page, teacher_locale, dismiss_help)
    expect(_card(teacher_page, title).locator(".task-deadline")).to_contain_text(edited_local[-8:])
    dialog = _edit_dialog(teacher_page, title, teacher_locale)
    expect(
        dialog.get_by_label(_label("learning.deadline", teacher_locale), exact=True)
    ).to_have_value(edited_local)
    expect(
        dialog.get_by_role(
            "checkbox", name=_label("learning.allowLate", teacher_locale), exact=True
        )
    ).to_be_checked()
    _close_dialogs(teacher_page)
    # Preserve server precision when editing unrelated metadata in a seconds-based control.
    precise_deadline = expected_deadline.replace(".000000Z", ".123456Z")
    current = teacher_context.request.get(f"/api/tasks/{task_id}").json()
    precise = teacher_context.request.patch(
        f"/api/tasks/{task_id}",
        data={"deadline": precise_deadline, "version": current["version"]},
        headers=_csrf(teacher_context),
    )
    assert precise.status == 200
    dialog = _edit_dialog(teacher_page, title, teacher_locale)
    dialog.get_by_label(_label("learning.rules", teacher_locale), exact=True).fill(
        "Preserve the stored deadline while updating these rules."
    )
    with teacher_page.expect_response(
        lambda item: item.url.endswith(f"/api/tasks/{task_id}") and item.request.method == "PATCH"
    ) as metadata_edit:
        dialog.locator("button[type=submit]").click()
    assert metadata_edit.value.status == 200
    assert metadata_edit.value.json()["deadline"] == precise_deadline
    expect(teacher_page.locator("dialog[open]")).to_have_count(0)
    if database_path is not None:
        with close_connection(sqlite3.connect(database_path)) as connection:
            stored_deadline, stored_late = connection.execute(
                "SELECT deadline,allow_late FROM Tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        assert stored_deadline == precise_deadline and stored_late == 1

    print("STAGE learning UI: student material, submission and revision", flush=True)
    _tasks(student_page, student_locale, navigate, dismiss_help)
    _reload_tasks(student_page, student_locale, dismiss_help)
    expect(_card(student_page, title).locator(".task-deadline")).to_contain_text(edited_local[-8:])
    _card(student_page, title).get_by_role(
        "button", name=_label("learning.details", student_locale), exact=True
    ).click()
    expect(_top(student_page).locator(".task-deadline")).to_contain_text(edited_local[-8:])
    _top(student_page).get_by_role(
        "button", name=_label("learning.openMaterial", student_locale), exact=True
    ).click()
    expect(_top(student_page).locator(".text-preview")).to_have_text(material_text)
    assert _top(student_page).locator(".text-preview b").count() == 0
    _close_dialogs(student_page)
    _card(student_page, title).get_by_role(
        "button", name=_label("learning.submit", student_locale), exact=True
    ).click()
    _top(student_page).get_by_label(_label("learning.workText", student_locale), exact=True).fill(
        first_text
    )
    _top(student_page).get_by_label(
        _label("learning.optionalFile", student_locale), exact=True
    ).set_input_files(
        {
            "name": "learning-answer.txt",
            "mimeType": "text/plain",
            "buffer": answer_file,
        }
    )
    with student_page.expect_response(
        lambda item: (
            item.url.endswith(f"/api/tasks/{task_id}/submission") and item.request.method == "PUT"
        )
    ) as submitted:
        _top(student_page).locator("button[type=submit]").click()
    assert submitted.value.status == 200
    work: dict[str, Any] = submitted.value.json()
    submission_id = work["submission_id"]
    assert work["student_id"] == 1 and work["file_name"] == "learning-answer.txt"
    expect(student_page.locator("dialog[open]")).to_have_count(0)
    _card(student_page, title).get_by_role(
        "button", name=_label("learning.resubmit", student_locale), exact=True
    ).click()
    expect(
        _top(student_page).get_by_label(_label("learning.workText", student_locale), exact=True)
    ).to_have_value(first_text)
    _top(student_page).get_by_label(_label("learning.workText", student_locale), exact=True).fill(
        revised_text
    )
    with student_page.expect_response(
        lambda item: (
            item.url.endswith(f"/api/tasks/{task_id}/submission") and item.request.method == "PUT"
        )
    ) as revised:
        _top(student_page).locator("button[type=submit]").click()
    assert revised.value.status == 200
    assert revised.value.json()["version"] == work["version"] + 1
    expect(student_page.locator("dialog[open]")).to_have_count(0)
    screenshot = "learning-student-submitted-en.png"
    student_page.screenshot(path=str(artifacts / screenshot), full_page=True)
    screenshots.append(screenshot)

    print("STAGE learning UI: teacher draft and published review", flush=True)
    _tasks(teacher_page, teacher_locale, navigate, dismiss_help)
    dialog = _review_dialog(teacher_page, title, teacher_locale)
    expect(dialog.locator(".submission-text")).to_have_text(revised_text)
    dialog.get_by_role(
        "button", name=_label("learning.openAttachment", teacher_locale), exact=True
    ).click()
    expect(_top(teacher_page).locator(".text-preview")).to_have_text(answer_file.decode())
    assert _top(teacher_page).locator(".text-preview b").count() == 0
    _top(teacher_page).locator(".dialog-close").click()
    expect(teacher_page.locator("dialog[open]")).to_have_count(2)
    dialog = _top(teacher_page)
    dialog.get_by_label(_label("learning.feedback", teacher_locale), exact=True).fill(
        draft_feedback
    )
    dialog.get_by_label(_label("learning.score", teacher_locale), exact=True).fill("73")
    dialog.get_by_role(
        "checkbox", name=_label("learning.published", teacher_locale), exact=True
    ).uncheck()
    with teacher_page.expect_response(
        lambda item: (
            item.url.endswith(f"/api/submissions/{submission_id}/review")
            and item.request.method == "PATCH"
        )
    ) as reviewed:
        dialog.locator("button[type=submit]").click()
    assert reviewed.value.status == 200 and reviewed.value.json()["published"] is False
    expect(teacher_page.locator("dialog[open]")).to_have_count(1)
    _close_dialogs(teacher_page)
    hidden = student_context.request.get(f"{origin}/api/submissions/{submission_id}").json()
    assert hidden["score"] is None and hidden["feedback"] == ""
    _tasks(student_page, student_locale, navigate, dismiss_help)
    _card(student_page, title).get_by_role(
        "button", name=_label("learning.details", student_locale), exact=True
    ).click()
    expect(_top(student_page)).to_contain_text(_label("learning.unpublished", student_locale))
    assert _top(student_page).locator(".task-score").count() == 0
    assert draft_feedback not in _top(student_page).inner_text()
    _close_dialogs(student_page)

    _tasks(teacher_page, teacher_locale, navigate, dismiss_help)
    dialog = _review_dialog(teacher_page, title, teacher_locale)
    dialog.get_by_label(_label("learning.feedback", teacher_locale), exact=True).fill(
        final_feedback
    )
    dialog.get_by_label(_label("learning.score", teacher_locale), exact=True).fill("91")
    dialog.get_by_role(
        "checkbox", name=_label("learning.published", teacher_locale), exact=True
    ).check()
    screenshot = "learning-teacher-review-kk.png"
    teacher_page.screenshot(path=str(artifacts / screenshot), full_page=True)
    screenshots.append(screenshot)
    with teacher_page.expect_response(
        lambda item: (
            item.url.endswith(f"/api/submissions/{submission_id}/review")
            and item.request.method == "PATCH"
        )
    ) as published:
        dialog.locator("button[type=submit]").click()
    assert published.value.status == 200 and published.value.json()["score"] == 91
    expect(teacher_page.locator("dialog[open]")).to_have_count(1)
    _close_dialogs(teacher_page)
    dialog = _review_dialog(teacher_page, title, teacher_locale)
    dialog.get_by_role(
        "button", name=_label("learning.reviewHistory", teacher_locale), exact=True
    ).click()
    expect(_top(teacher_page)).to_contain_text(draft_feedback)
    expect(_top(teacher_page)).to_contain_text(final_feedback)
    history = teacher_context.request.get(f"/api/submissions/{submission_id}/history").json()
    assert history["total"] == 2
    latest_review = history["items"][0]
    assert latest_review["old_feedback"] == draft_feedback
    assert latest_review["new_feedback"] == final_feedback
    assert latest_review["old_score"] == 73 and latest_review["new_score"] == 91
    assert latest_review["new_version"] == latest_review["old_version"] + 1
    _close_dialogs(teacher_page)
    assert student_context.request.get(f"/api/submissions/{submission_id}/history").status == 403
    _tasks(student_page, student_locale, navigate, dismiss_help)
    expect(_card(student_page, title).locator(".task-score")).to_contain_text("91")
    _card(student_page, title).get_by_role(
        "button", name=_label("learning.details", student_locale), exact=True
    ).click()
    expect(_top(student_page)).to_contain_text(final_feedback)
    expect(_top(student_page).locator(".task-score")).to_contain_text("91")
    screenshot = "learning-student-published-en.png"
    student_page.screenshot(path=str(artifacts / screenshot), full_page=True)
    screenshots.append(screenshot)
    _close_dialogs(student_page)

    _card(student_page, title).get_by_role(
        "button", name=_label("learning.resubmit", student_locale), exact=True
    ).click()
    expect(_top(student_page)).to_contain_text(
        _label("learning.resubmitReviewReset", student_locale)
    )
    expect(
        _top(student_page).get_by_label(_label("learning.workText", student_locale), exact=True)
    ).to_have_value(revised_text)
    _close_dialogs(student_page)

    print("STAGE learning API: peer privacy and deadline lock", flush=True)
    browser = student_context.browser
    assert browser is not None
    peer = browser.new_context(base_url=origin)
    try:
        peer_login, peer_password = (
            "learning_peer_" + secrets.token_hex(3),
            secrets.token_urlsafe(24),
        )
        peer_student = next(
            item
            for item in admin_context.request.get("/api/academic/catalog").json()["students"]
            if item["student_id"] == 2
        )
        registered = peer.request.post(
            "/api/auth/register",
            data={
                "login": peer_login,
                "password": peer_password,
                "display_name": "Қосымша студент",
                "requested_role": "student",
                "student_claim_name": peer_student["display_name"],
                "student_claim_number": peer_student["student_code"],
                "student_claim_group_id": peer_student["group_id"],
            },
        )
        assert registered.status == 201
        requests = admin_context.request.get("/api/admin/requests").json()["items"]
        peer_user = next(item for item in requests if item["login"] == peer_login)
        peer_detail = admin_context.request.get(
            f"/api/admin/requests/{peer_user['user_id']}"
        ).json()
        approved = admin_context.request.post(
            f"/api/admin/requests/{peer_user['user_id']}/approve",
            data={
                "role_code": "student",
                "student_id": 2,
                "confirm_student_match": True,
                "claim_revision": peer_detail["claim_revision"],
            },
            headers=_csrf(admin_context),
        )
        assert approved.status == 200
        assert (
            peer.request.post(
                "/api/auth/login", data={"login": peer_login, "password": peer_password}
            ).status
            == 200
        )
        peer_work = peer.request.put(
            f"/api/tasks/{task_id}/submission",
            form={"text": "Other student's private work.", "version": "0"},
            headers=_csrf(peer),
        )
        assert peer_work.status == 200
        peer_id = peer_work.json()["submission_id"]
        assert student_context.request.get(f"/api/submissions/{peer_id}").status == 403
        assert student_context.request.get(f"/api/submissions/{peer_id}/file").status == 403
        assert peer.request.get(f"/api/submissions/{submission_id}").status == 403
    finally:
        peer.close()

    current = teacher_context.request.get(f"/api/tasks/{task_id}").json()
    cutoff = (datetime.now(UTC) + timedelta(seconds=2)).isoformat()
    closing = teacher_context.request.patch(
        f"/api/tasks/{task_id}",
        data={
            "deadline": cutoff,
            "allow_late": False,
            "version": current["version"],
        },
        headers=_csrf(teacher_context),
    )
    assert closing.status == 200
    wait_until = time.monotonic() + 10
    while not student_context.request.get(f"/api/tasks/{task_id}").json()["deadline_passed"]:
        assert time.monotonic() < wait_until, "Server did not reach its saved deadline"
        time.sleep(0.1)
    own = student_context.request.get(f"/api/submissions/{submission_id}").json()
    denied = student_context.request.put(
        f"/api/tasks/{task_id}/submission",
        form={
            "text": "This late update must not be saved.",
            "version": str(own["version"]),
            "student_id": "2",
            "submitted_at": "2000-01-01T00:00:00Z",
            "now": "2000-01-01T00:00:00Z",
            "deadline": "2099-01-01T00:00:00Z",
            "allow_late": "true",
        },
        headers=_csrf(student_context),
    )
    assert denied.status == 409 and denied.json()["error"]["code"] == "deadline_passed"
    retained_work = student_context.request.get(f"/api/submissions/{submission_id}").json()
    assert retained_work["score"] == 91 and retained_work["text"] == revised_text
    _tasks(student_page, student_locale, navigate, dismiss_help)
    card = _card(student_page, title)
    expect(card).to_contain_text(_label("learning.overdue", student_locale))
    assert (
        card.get_by_role(
            "button", name=_label("learning.resubmit", student_locale), exact=True
        ).count()
        == 0
    )
    assert student_context.request.get("/api/audit").status == 403
    screenshot = "learning-student-deadline-en.png"
    student_page.screenshot(path=str(artifacts / screenshot), full_page=True)
    screenshots.append(screenshot)

    teacher = teacher_context.request.get("/api/auth/me").json()["user"]
    audits = admin_context.request.get(
        "/api/audit",
        params={
            "action": "submission_review",
            "actor": teacher["login"],
            "page_size": 100,
        },
    ).json()["items"]
    review_event = next(
        item
        for item in audits
        if item["object_id"] == str(submission_id) and item["new_value"]["score"] == 91
    )
    assert review_event["old_value"]["score"] == 73
    assert review_event["ip_address"] == "127.0.0.1"
    assert review_event["result"] == "success"
    assert not errors, f"Browser JavaScript errors during learning flow: {errors}"
    print(
        "PASS learning UI create/material/submit/resubmit/draft/publish, peer403 and deadline409",
        flush=True,
    )
    return {
        "status": "passed",
        "task_id": task_id,
        "submission_id": submission_id,
        "teacher_locale": teacher_locale,
        "student_locale": student_locale,
        "review_audit_id": review_event["audit_id"],
        "review_ip": review_event["ip_address"],
        "unpublished_score_hidden": True,
        "other_student_denied": True,
        "deadline_enforced": True,
        "atomic_initial_material": True,
        "invalid_initial_file_no_partial_task": True,
        "deadline_edit_utc_db_reload": database_path is not None,
        "deadline_precision_preserved": True,
        "past_deadline_error_visible": True,
        "allow_late_persisted": True,
        "private_feedback_history": True,
        "screenshots": screenshots,
    }
