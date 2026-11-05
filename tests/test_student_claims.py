"""Unverified student claims, deliberate admin matching and private correction."""

import json
import logging
import secrets
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.auth import verify_password
from app.db import AuditRepository
from app.schemas import Action, SearchFilters, SummaryClaim, SummaryPlan
from app.service import render_summary
from app.store import iso_utc, utc_now
from tests.test_auth_system import add_user, sign_in
from tests.test_auth_system import auth_system as auth_system


def student_claim(store, student_id=1, **changes):
    with store.connect() as connection:
        row = connection.execute(
            "SELECT * FROM Students WHERE student_id=?", (student_id,)
        ).fetchone()
    return {
        "student_claim_name": row["display_name"],
        "student_claim_number": row["student_code"],
        "student_claim_group_id": row["group_id"],
        **changes,
    }


def register_student(client, store, login="claim_applicant", student_id=1, **changes):
    password = secrets.token_urlsafe(20)
    claim = student_claim(store, student_id, **changes)
    response = client.post(
        "/api/auth/register",
        json={
            "login": login,
            "display_name": "Account display differs from claimed legal name",
            "password": password,
            "requested_role": "student",
            **claim,
        },
    )
    assert response.status_code == 201, response.text
    with store.connect() as connection:
        user_id = connection.execute(
            "SELECT user_id FROM Users WHERE login=?", (login,)
        ).fetchone()[0]
    return SimpleNamespace(user_id=user_id, login=login, password=password, claim=claim)


def approve(client, applicant, student_id=1, **changes):
    if "claim_revision" not in changes:
        detail = client.get(f"/api/admin/requests/{applicant.user_id}")
        if detail.status_code == 200:
            changes["claim_revision"] = detail.json()["claim_revision"]
    return client.post(
        f"/api/admin/requests/{applicant.user_id}/approve",
        json={
            "role_code": "student",
            "student_id": student_id,
            "confirm_student_match": True,
            **changes,
        },
    )


def private_user(client):
    response = client.get("/api/auth/me")
    assert response.status_code == 200, response.text
    return response.json()["user"]


def assert_unlinked_pending(store, user_id, review_required=0):
    with store.connect() as connection:
        row = connection.execute(
            "SELECT role_code,approval_status,active,student_id,request_review_required "
            "FROM Users WHERE user_id=?",
            (user_id,),
        ).fetchone()
    assert tuple(row) == ("pending", "pending", 0, None, review_required)


def test_student_claim_is_normalized_private_and_never_becomes_a_student_link(auth_system):
    client, store, _, _ = auth_system
    claim = student_claim(store)
    applicant = register_student(
        client,
        store,
        student_claim_name="  " + claim["student_claim_name"] + "  ",
        student_claim_number="  syn-001  ",
        student_claim_email="student@campus.example",
    )
    assert_unlinked_pending(store, applicant.user_id)
    with store.connect() as connection:
        stored = connection.execute("SELECT * FROM StudentClaims").fetchone()
        assert stored["student_claim_name"] == claim["student_claim_name"]
        assert stored["student_claim_number"] == "SYN-001"
        assert stored["student_claim_group_id"] == 1
        assert stored["student_claim_email"] == "student@campus.example"
        user = connection.execute(
            "SELECT * FROM Users WHERE user_id=?", (applicant.user_id,)
        ).fetchone()
        assert user["display_name"] != stored["student_claim_name"]
        assert verify_password(applicant.password, user["password_hash"])
    signed_in = sign_in(client, applicant.login, applicant.password).json()["user"]
    assert signed_in["request_review_status"] == "pending"
    own = private_user(client)
    assert own["student_claim"] == {
        "name": claim["student_claim_name"],
        "number": "SYN-001",
        "group_id": 1,
        "group_name": "CS-101",
        "email": "student@campus.example",
    }
    for path in (
        "/api/academic/catalog",
        "/api/tasks",
        "/api/grades",
        "/api/documents",
        "/api/audit",
        "/api/admin/requests",
        "/api/admin/users",
        "/api/dashboard",
    ):
        response = client.get(path)
        assert response.status_code == 403, path
        assert claim["student_claim_name"] not in response.text


def test_teacher_registration_forbids_every_student_field_even_when_null(auth_system):
    client, store, _, _ = auth_system
    payload = {
        "login": "teacher_without_claim",
        "display_name": "Teacher applicant",
        "password": secrets.token_urlsafe(20),
        "requested_role": "teacher",
    }
    for key, value in student_claim(store, student_claim_email="teacher@campus.example").items():
        for forbidden_value in (None, value):
            response = client.post("/api/auth/register", json=payload | {key: forbidden_value})
            assert response.status_code == 422, (key, forbidden_value)
    assert client.post("/api/auth/register", json=payload).status_code == 201
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM StudentClaims").fetchone()[0] == 0


def test_student_claim_validation_is_strict_and_sql_looking_numbers_are_not_executed(auth_system):
    client, store, _, _ = auth_system
    original = {
        "login": "invalid_claim",
        "display_name": "Student applicant",
        "password": secrets.token_urlsafe(20),
        "requested_role": "student",
        **student_claim(store),
    }
    changes = (
        {"student_claim_name": ""},
        {"student_claim_name": "x"},
        {"student_claim_name": "x" * 121},
        {"student_claim_name": "Name\x00Hidden"},
        {"student_claim_number": " "},
        {"student_claim_number": "ab"},
        {"student_claim_number": "A" * 33},
        {"student_claim_number": "АБВ-123"},
        {"student_claim_number": "A B"},
        {"student_claim_number": "A';DROP TABLE Users;--"},
        {"student_claim_number": "-ABC"},
        {"student_claim_group_id": "1"},
        {"student_claim_group_id": True},
        {"student_claim_group_id": 1.0},
        {"student_claim_group_id": 0},
        {"student_claim_group_id": 99999},
        {"student_claim_email": "not-an-email"},
        {"student_claim_email": "a@@campus.example"},
        {"student_claim_email": "a b@campus.example"},
    )
    for invalid in changes:
        response = client.post("/api/auth/register", json=original | invalid)
        assert response.status_code == 422, invalid
    for missing in ("student_claim_name", "student_claim_number", "student_claim_group_id"):
        response = client.post(
            "/api/auth/register",
            json={key: value for key, value in original.items() if key != missing},
        )
        assert response.status_code == 422, missing
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM Users").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM StudentClaims").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM Students").fetchone()[0] == 12


def test_registration_rejects_extra_privilege_fields_with_otherwise_valid_claim(auth_system):
    client, store, _, _ = auth_system
    payload = {
        "login": "cannot_promote",
        "display_name": "Student applicant",
        "password": secrets.token_urlsafe(20),
        "requested_role": "student",
        **student_claim(store),
    }
    for extra in (
        {"student_id": 1},
        {"active": True},
        {"approval_status": "approved"},
        {"role_code": "admin"},
        {"permissions": ["Users.Manage"]},
        {"request_review_required": 0},
        {"approved_by": 1},
        {"student_claim_verified": True},
    ):
        assert client.post("/api/auth/register", json=payload | extra).status_code == 422, extra
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM Users").fetchone()[0] == 1


def test_duplicate_claim_number_is_case_insensitive_without_creating_partial_account(auth_system):
    client, store, _, _ = auth_system
    first = register_student(client, store, student_claim_number="  syn-001  ")
    response = client.post(
        "/api/auth/register",
        json={
            "login": "duplicate_claim",
            "display_name": "Different account",
            "password": secrets.token_urlsafe(20),
            "requested_role": "student",
            **student_claim(store, student_claim_number="SYN-001"),
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "student_claim_number_unavailable"
    assert "SYN-001" not in response.text
    assert_unlinked_pending(store, first.user_id)
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM StudentClaims").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM Users").fetchone()[0] == 2


def test_registration_options_exposes_groups_email_policy_and_no_student_roster(auth_system):
    client, store, _, _ = auth_system
    response = client.get("/api/auth/registration-options")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"groups", "student_email_required", "student_email_domain"}
    assert body["student_email_required"] is False and body["student_email_domain"] == ""
    assert [(group["group_id"], group["name"]) for group in body["groups"]] == [
        (1, "CS-101"),
        (2, "CS-102"),
        (3, "IS-201"),
    ]
    with store.connect() as connection:
        students = connection.execute("SELECT display_name,student_code FROM Students").fetchall()
    for row in students:
        assert row["display_name"] not in response.text and row["student_code"] not in response.text
    assert "students" not in body and "users" not in body
    assert "student_claim" not in response.text and "password" not in response.text


def test_student_email_requirement_and_domain_are_enforced_only_for_students(auth_system):
    client, store, _, _ = auth_system
    settings = client.app.state.settings
    settings.student_email_required = True
    settings.student_email_domain = "campus.example"
    payload = {
        "login": "email_applicant",
        "display_name": "Student applicant",
        "password": secrets.token_urlsafe(20),
        "requested_role": "student",
        **student_claim(store),
    }
    missing = client.post("/api/auth/register", json=payload)
    assert (
        missing.status_code == 422 and missing.json()["error"]["code"] == "student_email_required"
    )
    wrong = client.post(
        "/api/auth/register",
        json=payload | {"student_claim_email": "student@outside.example"},
    )
    assert wrong.status_code == 422 and wrong.json()["error"]["code"] == "student_email_domain"
    assert (
        client.post(
            "/api/auth/register",
            json=payload | {"student_claim_email": "student@campus.example"},
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/api/auth/register",
            json={
                "login": "teacher_email_optional",
                "display_name": "Teacher applicant",
                "password": secrets.token_urlsafe(20),
                "requested_role": "teacher",
            },
        ).status_code
        == 201
    )
    options = client.get("/api/auth/registration-options").json()
    assert "campus.example" in json.dumps(options)
    assert options["student_email_required"] is True
    sign_in(client, "email_applicant", payload["password"])
    assert client.put("/api/auth/student-claim", json=student_claim(store)).status_code == 422
    wrong_correction = client.put(
        "/api/auth/student-claim",
        json=student_claim(store, student_claim_email="student@outside.example"),
    )
    assert wrong_correction.status_code == 422
    assert wrong_correction.json()["error"]["code"] == "student_email_domain"


def test_only_admin_detail_exposes_full_claim_and_general_lists_never_expose_email(auth_system):
    client, store, admin_password, _ = auth_system
    applicant = register_student(
        client,
        store,
        student_claim_number="PRIVATE-987654321",
        student_claim_email="private@campus.example",
    )
    sign_in(client, "first_admin", admin_password)
    listing = client.get("/api/admin/requests")
    assert listing.status_code == 200
    row = next(item for item in listing.json()["items"] if item["user_id"] == applicant.user_id)
    assert row["student_claim"]["number_masked"] != "PRIVATE-987654321"
    assert row["student_claim"]["name"] == applicant.claim["student_claim_name"]
    assert row["student_claim"]["group_name"] == "CS-101"
    assert row["student_claim"]["has_claim"] is True
    for response in (listing, client.get("/api/admin/users")):
        assert (
            "PRIVATE-987654321" not in response.text
            and "private@campus.example" not in response.text
        )
    users = client.get("/api/admin/users").json()["items"]
    assert all("student_claim" not in item for item in users)
    detail = client.get(f"/api/admin/requests/{applicant.user_id}")
    assert detail.status_code == 200
    assert detail.json()["student_claim"]["number"] == "PRIVATE-987654321"
    assert detail.json()["student_claim"]["email"] == "private@campus.example"
    assert detail.json()["user"]["student_claim"] == detail.json()["student_claim"]
    assert "password_hash" not in detail.text


def test_matching_approval_requires_explicit_strict_confirmation_and_updates_live_session(
    auth_system,
):
    client, store, admin_password, _ = auth_system
    applicant = register_student(client, store)
    with TestClient(client.app, client=("127.0.0.2", 52000)) as owner:
        sign_in(owner, applicant.login, applicant.password)
        sign_in(client, "first_admin", admin_password)
        path = f"/api/admin/requests/{applicant.user_id}/approve"
        for confirmation in (None, False, "true", 1):
            payload = {"role_code": "student", "student_id": 1}
            if confirmation is not None:
                payload["confirm_student_match"] = confirmation
            response = client.post(path, json=payload)
            assert response.status_code == 422, confirmation
            assert_unlinked_pending(store, applicant.user_id)
        missing_revision = client.post(
            path,
            json={"role_code": "student", "student_id": 1, "confirm_student_match": True},
        )
        assert missing_revision.status_code == 422
        assert missing_revision.json()["error"]["code"] == "request_revision_required"
        assert_unlinked_pending(store, applicant.user_id)
        approved = approve(client, applicant)
        assert approved.status_code == 200
        own = private_user(owner)
        assert own["student_id"] == 1 and own["role_code"] == "student" and own["active"] is True
        assert own["approval_status"] == own["request_review_status"] == "approved"
        assert set(own["permissions"]) == {"Dashboard.View", "Learning.View", "Learning.Submit"}
        assert owner.get("/api/tasks").status_code == 200
        assert owner.get("/api/academic/catalog").status_code == 403
    with store.connect() as connection:
        row = connection.execute(
            "SELECT role_code,approval_status,active,student_id FROM Users WHERE user_id=?",
            (applicant.user_id,),
        ).fetchone()
        assert tuple(row) == ("student", "approved", 1, 1)
        audit = connection.execute(
            "SELECT * FROM AuditLogs WHERE action='student_link' ORDER BY audit_id DESC LIMIT 1"
        ).fetchone()
        assert audit["user_id"] == "first_admin" and audit["ip_address"] == "127.0.0.1"
        assert audit["occurred_at"].endswith("Z")
        assert json.loads(audit["new_value"])["student_id"] == 1


def test_each_claim_mismatch_blocks_linking_until_admin_deliberately_acknowledges(auth_system):
    client, store, admin_password, _ = auth_system
    applicant = register_student(client, store)
    with TestClient(client.app, client=("127.0.0.2", 52000)) as owner:
        sign_in(owner, applicant.login, applicant.password)
        sign_in(client, "first_admin", admin_password)
        mismatches = (
            {"student_claim_name": "A different student's full name"},
            {"student_claim_number": "OTHER-987654321"},
            {"student_claim_group_id": 2},
        )
        for change in mismatches:
            corrected = owner.put("/api/auth/student-claim", json=student_claim(store, **change))
            assert corrected.status_code == 200, corrected.text
            response = approve(client, applicant)
            assert (
                response.status_code == 409
                and response.json()["error"]["code"] == "student_claim_mismatch"
            )
            assert_unlinked_pending(store, applicant.user_id)
        for invalid_ack in ("true", 1, None):
            assert approve(client, applicant, acknowledge_mismatch=invalid_ack).status_code == 422
        acknowledged = approve(client, applicant, acknowledge_mismatch=True)
        assert acknowledged.status_code == 200
        own = private_user(owner)
        assert own["student_id"] == 1 and own["student_claim"]["group_id"] == 2


def test_legacy_student_without_claim_requires_confirmation_and_mismatch_acknowledgement(
    auth_system,
):
    client, store, admin_password, _ = auth_system
    legacy_id = add_user(
        store, "legacy_student_claim", secrets.token_urlsafe(20), role="pending", active=False
    )
    with store.connect(write=True) as connection:
        connection.execute(
            "UPDATE Users SET requested_role='student',approval_status='pending',"
            "request_review_required=1 "
            "WHERE user_id=?",
            (legacy_id,),
        )
    sign_in(client, "first_admin", admin_password)
    applicant = SimpleNamespace(user_id=legacy_id)
    assert approve(client, applicant).status_code == 409
    assert_unlinked_pending(store, legacy_id, review_required=1)
    assert approve(client, applicant, acknowledge_mismatch=True).status_code == 200


def test_needs_review_and_rejected_claims_can_be_corrected_only_by_the_owner(auth_system):
    client, store, admin_password, _ = auth_system
    applicant = register_student(client, store)
    peer = register_student(client, store, login="claim_peer", student_id=2)
    with ExitStack() as stack:
        owner = stack.enter_context(TestClient(client.app, client=("127.0.0.2", 52000)))
        other = stack.enter_context(TestClient(client.app, client=("127.0.0.3", 52000)))
        sign_in(owner, applicant.login, applicant.password)
        sign_in(other, peer.login, peer.password)
        sign_in(client, "first_admin", admin_password)
        marked = client.post(
            f"/api/admin/requests/{applicant.user_id}/needs-review",
            json={"reason": "Please verify the student's requested group."},
        )
        assert marked.status_code == 200
        own = private_user(owner)
        assert (
            own["request_review_status"] == "needs_review" and own["approval_status"] == "pending"
        )
        assert "verify" in own["approval_reason"]
        assert_unlinked_pending(store, applicant.user_id, review_required=1)
        assert private_user(other)["approval_reason"] == ""
        duplicate = owner.put("/api/auth/student-claim", json=student_claim(store, 2))
        assert duplicate.status_code == 409
        assert duplicate.json()["error"]["code"] == "student_claim_number_unavailable"
        assert private_user(owner)["student_claim"]["number"] == "SYN-001"
        assert_unlinked_pending(store, applicant.user_id, review_required=1)
        corrected_claim = student_claim(store, student_claim_number="CORRECTED-1001")
        assert (
            owner.put(
                "/api/auth/student-claim", json=corrected_claim | {"user_id": peer.user_id}
            ).status_code
            == 422
        )
        corrected = owner.put("/api/auth/student-claim", json=corrected_claim)
        assert corrected.status_code == 200
        assert corrected.json()["user"]["request_review_status"] == "pending"
        assert_unlinked_pending(store, applicant.user_id)
        assert private_user(other)["student_claim"]["number"] == "SYN-002"
        rejected = client.post(
            f"/api/admin/requests/{applicant.user_id}/reject",
            json={"reason": "Student data needs correction."},
        )
        assert rejected.status_code == 200
        assert private_user(owner)["request_review_status"] == "rejected"
        assert owner.put("/api/auth/student-claim", json=student_claim(store)).status_code == 200
        assert_unlinked_pending(store, applicant.user_id)
        assert private_user(owner)["approval_reason"] == ""
    repository = AuditRepository(store.path)
    for action in (Action.USER_NEEDS_REVIEW, Action.STUDENT_CLAIM_UPDATE):
        records, truncated = repository.search(SearchFilters(action=action), max_candidates=100)
        assert records and truncated is False
        assert all(record.action == action for record in records)
        audit_id = records[0].audit_id
        plan = SummaryPlan(claims=[SummaryClaim(kind="event", audit_ids=[audit_id])])
        texts = []
        for locale in ("kk", "ru", "en"):
            summary = render_summary(plan, records, locale)
            assert summary[0].audit_ids == [audit_id]
            assert action.value not in summary[0].text
            texts.append(summary[0].text)
        assert len(set(texts)) == 3


def test_approved_student_and_teacher_cannot_change_student_claim_or_link(auth_system):
    client, store, admin_password, _ = auth_system
    applicant = register_student(client, store)
    teacher_password = secrets.token_urlsafe(20)
    teacher_id = add_user(store, "claim_teacher", teacher_password)
    sign_in(client, "first_admin", admin_password)
    assert approve(client, applicant).status_code == 200
    with ExitStack() as stack:
        student = stack.enter_context(TestClient(client.app, client=("127.0.0.2", 52000)))
        teacher = stack.enter_context(TestClient(client.app, client=("127.0.0.3", 52000)))
        sign_in(student, applicant.login, applicant.password)
        sign_in(teacher, "claim_teacher", teacher_password)
        for actor, user_id in ((student, applicant.user_id), (teacher, teacher_id)):
            response = actor.put("/api/auth/student-claim", json=student_claim(store, 2))
            assert (
                response.status_code == 403
                and response.json()["error"]["code"] == "student_claim_locked"
            )
            assert (
                actor.patch(f"/api/admin/users/{user_id}", json={"student_id": 2}).status_code
                == 403
            )
    with store.connect() as connection:
        assert (
            connection.execute(
                "SELECT student_id FROM Users WHERE user_id=?",
                (applicant.user_id,),
            ).fetchone()[0]
            == 1
        )


def test_nonadmin_with_users_permission_cannot_read_claims_approve_or_mark_review(auth_system):
    client, store, _, _ = auth_system
    applicant = register_student(client, store, student_claim_number="PRIVATE-912345678")
    password = secrets.token_urlsafe(20)
    add_user(store, "delegated_claim_teacher", password)
    with store.connect(write=True) as connection:
        connection.execute(
            "INSERT INTO RolePermissions(role_code,permission_code) "
            "VALUES ('teacher','Users.Manage')"
        )
    sign_in(client, "delegated_claim_teacher", password)
    for response in (
        client.get("/api/admin/requests"),
        client.get(f"/api/admin/requests/{applicant.user_id}"),
        approve(client, applicant, acknowledge_mismatch=True),
        client.post(
            f"/api/admin/requests/{applicant.user_id}/needs-review",
            json={"reason": "Verify these details."},
        ),
        client.post(
            f"/api/admin/requests/{applicant.user_id}/reject",
            json={"reason": "Cannot verify the match."},
        ),
        client.patch(f"/api/admin/users/{applicant.user_id}", json={"student_id": 1}),
    ):
        assert response.status_code == 403
        assert "PRIVATE-912345678" not in response.text
    assert_unlinked_pending(store, applicant.user_id)


def test_duplicate_student_link_is_409_before_any_mismatch_and_never_detaches_first(auth_system):
    client, store, admin_password, _ = auth_system
    first = register_student(client, store)
    second = register_student(client, store, login="claim_second", student_id=2)
    sign_in(client, "first_admin", admin_password)
    assert approve(client, first).status_code == 200
    duplicate = approve(client, second)
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "student_already_linked"
    without_confirmation = client.post(
        f"/api/admin/requests/{second.user_id}/approve",
        json={"role_code": "student", "student_id": 1},
    )
    assert without_confirmation.status_code == 409
    assert without_confirmation.json()["error"]["code"] == "student_already_linked"
    assert_unlinked_pending(store, second.user_id)
    with store.connect() as connection:
        assert (
            connection.execute(
                "SELECT student_id FROM Users WHERE user_id=?",
                (first.user_id,),
            ).fetchone()[0]
            == 1
        )


def test_concurrent_admin_approvals_cannot_bind_two_accounts_to_one_student(auth_system):
    client, store, admin_password, _ = auth_system
    first = register_student(client, store)
    second = register_student(client, store, login="race_second", student_id=2)
    with ExitStack() as stack:
        admins = [
            stack.enter_context(TestClient(client.app, client=("127.0.0.1", 52000 + i)))
            for i in range(2)
        ]
        for admin in admins:
            sign_in(admin, "first_admin", admin_password)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(approve, admin, applicant, acknowledge_mismatch=True)
                for admin, applicant in zip(admins, (first, second), strict=True)
            ]
            responses = [future.result(timeout=30) for future in futures]
    assert sorted(response.status_code for response in responses) == [200, 409]
    denied = next(response for response in responses if response.status_code == 409)
    assert denied.json()["error"]["code"] == "student_already_linked"
    with store.connect() as connection:
        rows = connection.execute(
            "SELECT approval_status,active,student_id FROM Users WHERE user_id IN (?,?)",
            (first.user_id, second.user_id),
        ).fetchall()
        assert sum(row["student_id"] == 1 for row in rows) == 1
        assert sorted((row["approval_status"], row["active"]) for row in rows) == [
            ("approved", 1),
            ("pending", 0),
        ]


def test_claim_registration_audit_failure_rolls_back_both_account_and_claim(auth_system):
    client, store, _, _ = auth_system
    with store.connect(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER fail_claim_registration BEFORE INSERT ON AuditLogs "
            "WHEN NEW.action='user_register' BEGIN SELECT RAISE(ABORT,'synthetic failure'); END"
        )
    response = client.post(
        "/api/auth/register",
        json={
            "login": "rollback_claim",
            "display_name": "Student applicant",
            "password": secrets.token_urlsafe(20),
            "requested_role": "student",
            **student_claim(store),
        },
    )
    assert response.status_code == 500
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM Users").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM StudentClaims").fetchone()[0] == 0


def test_claim_correction_audit_failure_rolls_back_claim_status_and_review_reason(auth_system):
    client, store, admin_password, _ = auth_system
    applicant = register_student(client, store)
    sign_in(client, "first_admin", admin_password)
    assert (
        client.post(
            f"/api/admin/requests/{applicant.user_id}/needs-review",
            json={"reason": "Please clarify your group."},
        ).status_code
        == 200
    )
    with TestClient(
        client.app, client=("127.0.0.2", 52000), raise_server_exceptions=False
    ) as owner:
        sign_in(owner, applicant.login, applicant.password)
        with store.connect(write=True) as connection:
            connection.execute(
                "CREATE TRIGGER fail_claim_correction BEFORE INSERT ON AuditLogs "
                "BEGIN SELECT RAISE(ABORT,'synthetic correction failure'); END"
            )
        response = owner.put(
            "/api/auth/student-claim",
            json=student_claim(store, student_claim_number="CHANGED-1001"),
        )
        assert response.status_code == 500
        own = private_user(owner)
        assert own["student_claim"]["number"] == "SYN-001"
        assert own["request_review_status"] == "needs_review"
        assert "clarify" in own["approval_reason"]
        assert_unlinked_pending(store, applicant.user_id, review_required=1)


def test_claim_and_password_never_leak_into_global_audit_errors_or_server_logs(auth_system, caplog):
    client, store, admin_password, _ = auth_system
    number, email = "PRIVATE-987612345", "private.person@campus.example"
    peer_number, peer_email = "OTHER-876543219", "other.person@campus.example"
    with caplog.at_level(logging.DEBUG):
        applicant = register_student(
            client, store, student_claim_number=number, student_claim_email=email
        )
        peer = register_student(
            client,
            store,
            login="private_claim_peer",
            student_id=2,
            student_claim_number=peer_number,
            student_claim_email=peer_email,
        )
        sign_in(client, "first_admin", admin_password)
        reviewed = client.post(
            f"/api/admin/requests/{applicant.user_id}/needs-review",
            json={"reason": f"Verify {number} and {email} against {peer_number} {peer_email}."},
        )
        assert reviewed.status_code == 200
        mismatch = approve(client, applicant)
        assert (
            mismatch.status_code == 409
            and number not in mismatch.text
            and email not in mismatch.text
        )
        rejected = client.post(
            f"/api/admin/requests/{applicant.user_id}/reject",
            json={"reason": f"Unable to match {number} {email} with {peer_number} {peer_email}."},
        )
        assert rejected.status_code == 200
        with TestClient(client.app, client=("127.0.0.2", 52000)) as owner:
            sign_in(owner, applicant.login, applicant.password)
            reason = private_user(owner)["approval_reason"]
            for private in (number, email, peer_number, peer_email):
                assert private not in reason
    with store.connect() as connection:
        audits = json.dumps([dict(row) for row in connection.execute("SELECT * FROM AuditLogs")])
    for private in (
        number,
        email,
        peer_number,
        peer_email,
        applicant.password,
        peer.password,
        admin_password,
    ):
        assert private not in audits and private not in caplog.text
    for path in ("/api/admin/requests?status=all", "/api/admin/users"):
        response = client.get(path)
        for private in (number, email, peer_number, peer_email):
            assert private not in response.text


def test_approved_claim_identity_scopes_tasks_own_work_and_published_results(auth_system):
    client, store, admin_password, _ = auth_system
    first = register_student(client, store)
    peer = register_student(client, store, login="learning_claim_peer", student_id=2)
    sign_in(client, "first_admin", admin_password)
    assert approve(client, first).status_code == 200
    assert approve(client, peer, student_id=2).status_code == 200
    now = utc_now()
    task_payload = {
        "title": "Verified linked student work",
        "description": "Explain your reasoning.",
        "subject_id": 1,
        "group_id": 1,
        "term_id": 1,
        "published_at": iso_utc(now - timedelta(minutes=1)),
        "deadline": iso_utc(now + timedelta(days=1)),
        "allow_late": False,
    }
    task = client.post("/api/tasks", json=task_payload).json()
    hidden = client.post("/api/tasks", json=task_payload | {"group_id": 2}).json()
    with ExitStack() as stack:
        owner = stack.enter_context(TestClient(client.app, client=("127.0.0.2", 52000)))
        other = stack.enter_context(TestClient(client.app, client=("127.0.0.3", 52000)))
        sign_in(owner, first.login, first.password)
        sign_in(other, peer.login, peer.password)
        assert [row["task_id"] for row in owner.get("/api/tasks").json()["items"]] == [
            task["task_id"]
        ]
        assert owner.get(f"/api/tasks/{hidden['task_id']}").status_code == 403
        submitted = owner.put(
            f"/api/tasks/{task['task_id']}/submission",
            data={"text": "Own verified work", "student_id": "2"},
        )
        assert submitted.status_code == 200 and submitted.json()["student_id"] == 1
        work_id = submitted.json()["submission_id"]
        assert other.get(f"/api/submissions/{work_id}").status_code == 403
        assert other.get(f"/api/submissions/{work_id}/file").status_code == 403
        draft = client.patch(
            f"/api/submissions/{work_id}/review",
            json={"feedback": "Private draft", "score": 83, "published": False, "version": 0},
        )
        assert draft.status_code == 200
        own_work = owner.get(f"/api/submissions/{work_id}").json()
        assert own_work["score"] is None and own_work["feedback"] == ""
        assert (
            client.patch(
                f"/api/submissions/{work_id}/review",
                json={
                    "feedback": "Published own feedback",
                    "score": 91,
                    "published": True,
                    "version": 1,
                },
            ).status_code
            == 200
        )
        assert owner.get(f"/api/submissions/{work_id}").json()["score"] == 91
        assert other.get(f"/api/submissions/{work_id}").status_code == 403


def test_claim_correction_requires_session_csrf_and_rejects_claimed_link_extra(auth_system):
    client, store, _, _ = auth_system
    applicant = register_student(client, store)
    assert client.put("/api/auth/student-claim", json=student_claim(store)).status_code == 401
    sign_in(client, applicant.login, applicant.password)
    client.headers.pop("X-CSRF-Token")
    assert client.put("/api/auth/student-claim", json=student_claim(store)).status_code == 403
    sign_in(client, applicant.login, applicant.password)
    assert (
        client.put(
            "/api/auth/student-claim",
            json=student_claim(store) | {"student_id": 2},
        ).status_code
        == 422
    )
    assert_unlinked_pending(store, applicant.user_id)


def test_admin_cannot_approve_a_claim_snapshot_changed_by_the_owner(auth_system):
    client, store, admin_password, _ = auth_system
    applicant = register_student(client, store)
    sign_in(client, "first_admin", admin_password)
    path = f"/api/admin/requests/{applicant.user_id}"
    original = client.get(path).json()["claim_revision"]
    assert len(original) == 64
    with TestClient(client.app, client=("127.0.0.2", 52000)) as owner:
        sign_in(owner, applicant.login, applicant.password)
        assert (
            owner.put(
                "/api/auth/student-claim",
                json=student_claim(store, student_claim_number="CORRECTED-2002"),
            ).status_code
            == 200
        )
    stale = approve(client, applicant, claim_revision=original, acknowledge_mismatch=True)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "request_changed"
    assert_unlinked_pending(store, applicant.user_id)
    refreshed = client.get(path).json()["claim_revision"]
    assert refreshed != original and len(refreshed) == 64
    assert (
        approve(
            client,
            applicant,
            claim_revision=refreshed,
            acknowledge_mismatch=True,
        ).status_code
        == 200
    )
