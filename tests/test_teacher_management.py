"""Teacher approvals, exact assignments and live scope use real authenticated APIs."""

import json
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.store import iso_utc, utc_now
from tests.test_auth_system import sign_in
from tests.test_student_roster_approval import (
    manual_approval,
    register,
)
from tests.test_student_roster_approval import (
    roster as roster,
)
from tests.test_student_roster_approval import (
    roster_credentials as roster_credentials,
)


@contextmanager
def signed_in(system, account):
    with TestClient(system.app, client=("192.0.2.61", 51111)) as client:
        sign_in(client, account.login, system.password)
        yield client


def teacher(system, login="teacher_primary", **changes):
    applicant = register(system, login, role="teacher", **changes)
    response = system.client.post(f"/api/admin/teachers/{applicant.user_id}/approve", json={})
    assert response.status_code == 200, response.text
    return applicant


def assignment(system, account, subject=1, group=1, term=1, assigned=True):
    return system.client.put(
        "/api/academic/assignments",
        json={
            "user_id": account.user_id,
            "subject_id": subject,
            "group_id": group,
            "term_id": term,
            "assigned": assigned,
        },
    )


def assignment_rows(system, account):
    with system.store.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM TeachingAssignments WHERE user_id=? ORDER BY assignment_id",
                (account.user_id,),
            )
        ]


def task_payload(subject=1, group=1, term=1):
    now = utc_now()
    return {
        "title": "Exact teacher assignment",
        "description": "Synthetic authorized task",
        "rules": "Only the assigned class may submit.",
        "subject_id": subject,
        "group_id": group,
        "term_id": term,
        "published_at": iso_utc(now - timedelta(minutes=1)),
        "deadline": iso_utc(now + timedelta(days=1)),
    }


def grade_row(system, subject=1, group=1, term=1):
    with system.store.connect() as connection:
        return dict(
            connection.execute(
                "SELECT g.* FROM Grades g JOIN Students s ON s.student_id=g.student_id "
                "WHERE g.subject_id=? AND s.group_id=? AND g.term_id=? "
                "ORDER BY g.grade_id LIMIT 1",
                (subject, group, term),
            ).fetchone()
        )


def test_pending_teacher_has_no_academic_access_until_dedicated_approval(roster):
    applicant = register(roster, "pending_teacher", role="teacher")
    with roster.store.connect() as connection:
        row = connection.execute(
            "SELECT role_code,active,approval_status,approved_at FROM Users WHERE user_id=?",
            (applicant.user_id,),
        ).fetchone()
    assert tuple(row) == ("pending", 0, "pending", None)
    assert assignment_rows(roster, applicant) == []
    with signed_in(roster, applicant) as client:
        for path in (
            "/api/tasks",
            "/api/academic/workload",
            "/api/grades",
            "/api/students",
            "/api/documents",
            "/api/academic/catalog",
        ):
            assert client.get(path).status_code == 403, path
        response = roster.client.post(f"/api/admin/teachers/{applicant.user_id}/approve", json={})
        assert response.status_code == 200, response.text
        user = client.get("/api/auth/me").json()["user"]
        assert (user["role_code"], user["active"], user["approval_status"]) == (
            "teacher",
            True,
            "approved",
        )
        assert datetime.fromisoformat(user["approved_at"].replace("Z", "+00:00")).utcoffset() == (
            timedelta(0)
        )
        assert client.get("/api/academic/workload").json() == {"items": [], "total": 0}
        assert client.post("/api/tasks", json=task_payload()).status_code == 403
    with roster.store.connect() as connection:
        audit = connection.execute(
            "SELECT * FROM AuditLogs WHERE action='user_approve' AND object_id=?",
            (str(applicant.user_id),),
        ).fetchone()
        stored = connection.execute(
            "SELECT approved_at FROM Users WHERE user_id=?", (applicant.user_id,)
        ).fetchone()[0]
    assert audit["user_id"] == "roster_admin" and audit["ip_address"] == "192.0.2.50"
    assert stored == user["approved_at"]


@pytest.mark.parametrize(
    "body", [{"role_code": "admin"}, {"active": True}, {"student_id": 1}, {"is_admin": True}]
)
def test_dedicated_teacher_approval_rejects_client_privilege_fields(roster, body):
    applicant = register(roster, role="teacher")
    response = roster.client.post(f"/api/admin/teachers/{applicant.user_id}/approve", json=body)
    assert response.status_code == 422
    with roster.store.connect() as connection:
        assert tuple(
            connection.execute(
                "SELECT role_code,active,approval_status FROM Users WHERE user_id=?",
                (applicant.user_id,),
            ).fetchone()
        ) == ("pending", 0, "pending")


@pytest.mark.parametrize("requested", ["student", "admin", "head", "auditor", "pending"])
def test_shared_approval_enforces_teacher_request_role_before_explicit_user_management(
    roster, requested
):
    applicant = register(roster, "shared_teacher_request", role="teacher")
    endpoint = f"/api/admin/requests/{applicant.user_id}/approve"
    payload = {"role_code": requested}
    if requested == "student":
        payload.update({"student_id": 1, "confirm_student_match": True})
    response = roster.client.post(endpoint, json=payload)
    assert response.status_code == 422, (requested, response.text)
    assert response.json()["error"]["code"] == "teacher_role_required"
    with roster.store.connect() as connection:
        assert tuple(
            connection.execute(
                "SELECT role_code,active,approval_status,student_id FROM Users WHERE user_id=?",
                (applicant.user_id,),
            ).fetchone()
        ) == ("pending", 0, "pending", None)
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM AuditLogs WHERE action='user_approve' AND object_id=?",
                (str(applicant.user_id),),
            ).fetchone()[0]
            == 0
        )
    approved = roster.client.post(endpoint, json={"role_code": "teacher"})
    assert approved.status_code == 200, approved.text
    assert approved.json()["user"]["role_code"] == "teacher"
    approved_at = approved.json()["user"]["approved_at"]
    for role in ("head", "auditor", "admin"):
        changed = roster.client.patch(
            f"/api/admin/users/{applicant.user_id}", json={"role_code": role}
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["user"]["role_code"] == role
        assert changed.json()["user"]["approved_at"] == approved_at
    with roster.store.connect() as connection:
        approval = connection.execute(
            "SELECT * FROM AuditLogs WHERE action='user_approve' AND object_id=?",
            (str(applicant.user_id),),
        ).fetchall()
        assert len(approval) == 1
        assert json.loads(approval[0]["new_value"])["role_code"] == "teacher"
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM AuditLogs WHERE action='role_change' AND object_id=?",
                (str(applicant.user_id),),
            ).fetchone()[0]
            >= 3
        )


@pytest.mark.parametrize("rejection_route", ["/api/admin/teachers", "/api/admin/requests"])
def test_dedicated_teacher_targets_and_rejection_cannot_reclassify_other_accounts(
    roster, rejection_route
):
    student = register(roster)
    approved = teacher(roster)
    rejected = register(roster, "teacher_rejected", role="teacher")
    for account in (student, approved):
        for operation, body in (("approve", {}), ("reject", {"reason": "Review required"})):
            response = roster.client.post(
                f"/api/admin/teachers/{account.user_id}/{operation}", json=body
            )
            assert response.status_code == 409
            expected = "teacher_request_required" if account is student else "request_reviewed"
            assert response.json()["error"]["code"] == expected
    assert roster.client.get(f"/api/admin/teachers/{student.user_id}").status_code == 404
    with roster.store.connect() as connection:
        admin_id = connection.execute(
            "SELECT user_id FROM Users WHERE role_code='admin'"
        ).fetchone()[0]
    assert roster.client.get(f"/api/admin/teachers/{admin_id}").status_code == 404
    with signed_in(roster, rejected) as pending_session:
        assert pending_session.get("/api/auth/me").json()["user"]["approval_status"] == "pending"
        response = roster.client.post(
            f"{rejection_route}/{rejected.user_id}/reject",
            json={"reason": "Missing qualification"},
        )
        assert response.status_code == 200
        assert pending_session.get("/api/auth/me").status_code == 401
        assert pending_session.get("/api/tasks").status_code == 401
    with roster.store.connect() as connection:
        assert (
            connection.execute(
                "SELECT status FROM Sessions WHERE user_id=?", (rejected.user_id,)
            ).fetchone()[0]
            == "revoked"
        )
    profile = roster.client.get(f"/api/admin/teachers/{rejected.user_id}").json()["teacher"]
    assert profile["approval_status"] == "rejected" and not profile["active"]
    assert profile["approved_at"] is None and profile["approval_reason"] == "Missing qualification"
    with signed_in(roster, rejected) as client:
        assert client.get("/api/tasks").status_code == 403
        assert client.get("/api/academic/workload").status_code == 403


def test_nonadmin_with_accidental_manage_permission_cannot_manage_teachers_or_assignments(roster):
    actor = teacher(roster)
    target = register(roster, "target_teacher", role="teacher")
    assert assignment(roster, actor).status_code == 200
    existing_id = assignment_rows(roster, actor)[0]["assignment_id"]
    with roster.store.connect(write=True) as connection:
        connection.execute(
            "INSERT INTO RolePermissions(role_code,permission_code) "
            "VALUES('teacher','Users.Manage')"
        )
    with signed_in(roster, actor) as client:
        assert "Users.Manage" in client.get("/api/auth/me").json()["user"]["permissions"]
        attempts = (
            ("get", "/api/admin/teachers", None),
            ("get", f"/api/admin/teachers/{target.user_id}", None),
            ("post", f"/api/admin/teachers/{target.user_id}/approve", {}),
            ("post", f"/api/admin/teachers/{target.user_id}/reject", {"reason": "Untrusted"}),
            ("patch", f"/api/admin/teachers/{actor.user_id}", {"active": False}),
            ("get", "/api/academic/assignments", None),
            (
                "put",
                "/api/academic/assignments",
                {
                    "user_id": actor.user_id,
                    "subject_id": 1,
                    "group_id": 1,
                    "term_id": 1,
                    "assigned": False,
                },
            ),
            (
                "patch",
                f"/api/academic/assignments/{existing_id}",
                {"subject_id": 2, "group_id": 2, "term_id": 2},
            ),
        )
        for method, path, body in attempts:
            kwargs = {} if body is None else {"json": body}
            response = getattr(client, method)(path, **kwargs)
            assert response.status_code == 403, (path, response.text)
    assert len(assignment_rows(roster, actor)) == 1
    assert (
        roster.client.get(f"/api/admin/teachers/{target.user_id}").json()["teacher"][
            "approval_status"
        ]
        == "pending"
    )


def test_exact_assignments_multiple_edit_conflict_idempotent_revoke_and_workload(roster):
    account = teacher(roster)
    assert assignment(roster, account).json()["changed"] is True
    assert assignment(roster, account).json()["changed"] is False
    assert assignment(roster, account, subject=2, group=2, term=2).status_code == 200
    first, second = assignment_rows(roster, account)
    with signed_in(roster, account) as client:
        workload = client.get("/api/academic/workload").json()
        assert workload["total"] == 2
        assert {(r["subject_id"], r["group_id"], r["term_id"]) for r in workload["items"]} == {
            (1, 1, 1),
            (2, 2, 2),
        }
        assert all(
            row["subject_name"]
            and row["group_name"]
            and row["term_name"]
            and row["academic_year"]
            and row["active"]
            for row in workload["items"]
        )
        endpoint = f"/api/academic/assignments/{first['assignment_id']}"
        changed = roster.client.patch(endpoint, json={"subject_id": 3, "group_id": 1, "term_id": 2})
        assert changed.status_code == 200, changed.text
        assert assignment_rows(roster, account)[0] == {**first, "subject_id": 3, "term_id": 2}
        duplicate = roster.client.patch(
            endpoint, json={"subject_id": 2, "group_id": 2, "term_id": 2}
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["error"]["code"] == "assignment_conflict"
        assert len(assignment_rows(roster, account)) == 2
        assert assignment(roster, account, 2, 2, 2, False).json()["changed"] is True
        assert assignment(roster, account, 2, 2, 2, False).json()["changed"] is False
        live = client.get("/api/academic/workload").json()
        assert live["total"] == 1 and live["items"][0]["assignment_id"] == first["assignment_id"]
    with roster.store.connect() as connection:
        audits = connection.execute(
            "SELECT * FROM AuditLogs WHERE action='assignment_change' AND result='success' "
            "ORDER BY audit_id"
        ).fetchall()
    assert len(audits) == 4
    assert all(
        row["user_id"] == "roster_admin" and row["ip_address"] == "192.0.2.50" for row in audits
    )
    update = next(
        row
        for row in audits
        if row["old_value"]
        and row["new_value"]
        and json.loads(row["old_value"]).get("subject_id") == 1
    )
    assert json.loads(update["old_value"])["term_id"] == 1
    assert json.loads(update["new_value"])["subject_id"] == 3
    assert second["assignment_id"] not in {
        r["assignment_id"] for r in assignment_rows(roster, account)
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"subject_id": True},
        {"group_id": "1"},
        {"term_id": 999},
        {"user_id": 99},
        {"group_id": 2**63},
    ],
)
def test_assignment_edit_validates_strict_complete_catalog_tuple(roster, changes):
    account = teacher(roster)
    assignment(roster, account)
    before = assignment_rows(roster, account)
    response = roster.client.patch(
        f"/api/academic/assignments/{before[0]['assignment_id']}",
        json={"subject_id": 1, "group_id": 1, "term_id": 1, **changes},
    )
    assert response.status_code == 422, response.text
    assert assignment_rows(roster, account) == before


def test_assignment_edit_missing_fields_missing_id_and_ineligible_teacher(roster):
    account = teacher(roster)
    assignment(roster, account)
    assignment_id = assignment_rows(roster, account)[0]["assignment_id"]
    assert (
        roster.client.patch(
            f"/api/academic/assignments/{assignment_id}", json={"subject_id": 1, "group_id": 1}
        ).status_code
        == 422
    )
    assert (
        roster.client.patch(
            "/api/academic/assignments/99999", json={"subject_id": 1, "group_id": 1, "term_id": 1}
        ).status_code
        == 404
    )
    pending = register(roster, "pending_target", role="teacher")
    student = register(roster)
    assert assignment(roster, pending).status_code == 422
    assert assignment(roster, student).status_code == 422
    assert (
        roster.client.patch(
            f"/api/admin/teachers/{account.user_id}", json={"active": False}
        ).status_code
        == 200
    )
    assert assignment(roster, account, subject=2).status_code == 422


def test_teacher_exact_tuples_do_not_expand_to_cross_product_or_foreign_students(roster):
    account = teacher(roster)
    assignment(roster, account)
    assignment(roster, account, 2, 2, 2)
    with signed_in(roster, account) as client:
        for scope in ((1, 1, 1), (2, 2, 2)):
            response = client.post("/api/tasks", json=task_payload(*scope))
            assert response.status_code == 201, response.text
        for scope in ((1, 2, 1), (2, 1, 2), (1, 1, 2)):
            assert client.post("/api/tasks", json=task_payload(*scope)).status_code == 403
            query = {"subject_id": scope[0], "group_id": scope[1], "term_id": scope[2]}
            for path in ("/api/grades", "/api/tasks", "/api/documents"):
                response = client.get(path, params=query)
                assert response.status_code == 403, (path, scope, response.text)
        listing = client.get("/api/grades").json()
        assert listing["total"] == 8
        assert {(r["subject_id"], r["group_id"], r["term_id"]) for r in listing["items"]} == {
            (1, 1, 1),
            (2, 2, 2),
        }
        students = client.get("/api/students").json()
        assert students["total"] == 8 and {r["group_id"] for r in students["items"]} == {1, 2}
        assert client.get("/api/students", params={"group_id": 3}).status_code == 403
        foreign = grade_row(roster, subject=3, group=3)
        assert client.get(f"/api/grades/{foreign['grade_id']}/history").status_code == 403
        assert (
            client.patch(
                f"/api/grades/{foreign['grade_id']}",
                json={"value": 99, "version": foreign["version"]},
            ).status_code
            == 403
        )
    assert grade_row(roster, subject=3, group=3) == foreign


def test_live_assignment_revocation_denies_existing_task_submission_grade_and_roster(roster):
    account = teacher(roster)
    assignment(roster, account)
    student = register(roster)
    assert manual_approval(roster, student, 1).status_code == 200
    with signed_in(roster, account) as client:
        created = client.post("/api/tasks", json=task_payload())
        assert created.status_code == 201, created.text
        task_id = created.json()["task_id"]
        with signed_in(roster, student) as learner:
            submitted = learner.post(
                f"/api/tasks/{task_id}/submission", data={"text": "Synthetic student work"}
            )
            assert submitted.status_code == 200, submitted.text
            submission_id = submitted.json()["submission_id"]
        grade = grade_row(roster)
        assert client.get(f"/api/tasks/{task_id}").status_code == 200
        assert client.get(f"/api/submissions/{submission_id}").status_code == 200
        assert client.get(f"/api/grades/{grade['grade_id']}/history").status_code == 200
        assert assignment(roster, account, assigned=False).status_code == 200
        assert client.get("/api/academic/workload").json() == {"items": [], "total": 0}
        for path in (
            f"/api/tasks/{task_id}",
            f"/api/tasks/{task_id}/submissions",
            f"/api/submissions/{submission_id}",
            f"/api/grades/{grade['grade_id']}/history",
            "/api/students?group_id=1",
        ):
            assert client.get(path).status_code == 403, path
        assert (
            client.patch(
                f"/api/grades/{grade['grade_id']}", json={"value": 92, "version": grade["version"]}
            ).status_code
            == 403
        )
    with roster.store.connect() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM Tasks WHERE task_id=?", (task_id,)).fetchone()[
                0
            ]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM Submissions WHERE submission_id=?", (submission_id,)
            ).fetchone()[0]
            == 1
        )
    assert grade_row(roster) == grade


def test_teacher_list_profile_filters_dates_and_same_tuple_group_subject(roster):
    primary = teacher(roster, display_name="Әлия Мұғалім")
    second = teacher(roster, "second_teacher", display_name="Second Teacher")
    pending = register(roster, "pending_profile", role="teacher")
    rejected = register(roster, "rejected_profile", role="teacher")
    roster.client.post(f"/api/admin/teachers/{rejected.user_id}/reject", json={"reason": "Review"})
    assignment(roster, primary)
    assignment(roster, primary, 2, 2, 2)
    assignment(roster, second, 1, 2, 1)
    assert (
        roster.client.patch(
            f"/api/admin/teachers/{second.user_id}", json={"active": False}
        ).status_code
        == 200
    )
    base = "/api/admin/teachers"
    requests = roster.client.get(base, params={"view": "requests"}).json()
    assert {r["user_id"] for r in requests["items"]} == {pending.user_id}
    all_requests = roster.client.get(base, params={"view": "requests", "status": "all"}).json()
    assert {r["user_id"] for r in all_requests["items"]} == {pending.user_id, rejected.user_id}
    inactive = roster.client.get(base, params={"status": "inactive"}).json()
    assert [r["user_id"] for r in inactive["items"]] == [second.user_id]
    combined = roster.client.get(base, params={"group_id": 2, "subject_id": 1}).json()
    assert combined["total"] == 0
    combined_all = roster.client.get(
        base, params={"group_id": 2, "subject_id": 1, "status": "all"}
    ).json()
    assert [r["user_id"] for r in combined_all["items"]] == [second.user_id]
    found = roster.client.get(base, params={"q": "ӘЛИЯ"}).json()
    assert [r["user_id"] for r in found["items"]] == [primary.user_id]
    assert roster.client.get(base, params={"q": "' OR 1=1--"}).json()["total"] == 0
    page = roster.client.get(base, params={"status": "all", "page_size": 1, "page": 2}).json()
    assert page["total"] == 2 and len(page["items"]) == 1 and page["page"] == 2
    assert roster.client.get(base, params={"status": "pending"}).status_code == 422
    assert roster.client.get(base, params={"group_id": 999}).status_code == 422
    profile = roster.client.get(f"{base}/{primary.user_id}").json()
    assert len(profile["assignments"]) == 2
    assert all(row["active"] for row in profile["assignments"])
    assert profile["teacher"]["approved_at"] and profile["teacher"]["created_at"]
    assert {"groups", "subjects", "terms"} <= profile.keys()
    assert "student_claim" not in profile["teacher"]


def test_teacher_deactivation_revokes_current_session_permanently(roster):
    account = teacher(roster)
    assignment(roster, account)
    approved_at = roster.client.get(f"/api/admin/teachers/{account.user_id}").json()["teacher"][
        "approved_at"
    ]
    with signed_in(roster, account) as client:
        assert client.get("/api/academic/workload").json()["total"] == 1
        response = roster.client.patch(
            f"/api/admin/teachers/{account.user_id}", json={"active": False}
        )
        assert response.status_code == 200
        assert client.get("/api/auth/me").status_code == 401
        profile = roster.client.get(f"/api/admin/teachers/{account.user_id}").json()
        assert not profile["teacher"]["active"] and not profile["assignments"][0]["active"]
        assert len(assignment_rows(roster, account)) == 1
        assert (
            roster.client.patch(
                f"/api/admin/teachers/{account.user_id}", json={"active": True}
            ).status_code
            == 200
        )
        assert client.get("/api/auth/me").status_code == 401
        sign_in(client, account.login, roster.password)
        assert client.get("/api/academic/workload").json()["total"] == 1
    assert (
        roster.client.get(f"/api/admin/teachers/{account.user_id}").json()["teacher"]["approved_at"]
        == approved_at
    )


@pytest.mark.parametrize("operation", ["create", "update", "revoke"])
def test_assignment_audit_failure_rolls_back_whole_operation(roster, operation):
    account = teacher(roster)
    if operation != "create":
        assert assignment(roster, account).status_code == 200
    before = assignment_rows(roster, account)
    with roster.store.connect(write=True) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM AuditLogs WHERE action='assignment_change'"
        ).fetchone()[0]
        connection.execute(
            "CREATE TRIGGER fail_assignment_audit BEFORE INSERT ON AuditLogs "
            "WHEN NEW.action='assignment_change' BEGIN "
            "SELECT RAISE(ABORT,'synthetic assignment audit failure'); END"
        )
    if operation == "update":
        response = roster.client.patch(
            f"/api/academic/assignments/{before[0]['assignment_id']}",
            json={"subject_id": 2, "group_id": 2, "term_id": 2},
        )
    else:
        response = assignment(roster, account, assigned=operation == "create")
    assert response.status_code == 500, response.text
    assert assignment_rows(roster, account) == before
    with roster.store.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM AuditLogs WHERE action='assignment_change'"
            ).fetchone()[0]
            == count
        )


@pytest.mark.parametrize("catalog", ["groups", "subjects", "terms"])
def test_archived_catalog_disables_live_teacher_scope_without_deleting_history(roster, catalog):
    account = teacher(roster)
    assignment(roster, account)
    with signed_in(roster, account) as client:
        created = client.post("/api/tasks", json=task_payload())
        assert created.status_code == 201, created.text
        task_id = created.json()["task_id"]
        grade = grade_row(roster)
        changed = client.patch(
            f"/api/grades/{grade['grade_id']}", json={"value": 84, "version": grade["version"]}
        )
        assert changed.status_code == 200
        response = roster.client.patch(f"/api/admin/catalogs/{catalog}/1", json={"active": False})
        assert response.status_code == 200, response.text
        assert assignment(roster, account).status_code == 422
        assert client.get("/api/academic/workload").json() == {"items": [], "total": 0}
        assert client.get(f"/api/tasks/{task_id}").status_code == 403
        assert client.get(f"/api/grades/{grade['grade_id']}/history").status_code == 403
        assert client.get("/api/students").json()["total"] == 0
        assert client.get("/api/students", params={"group_id": 1}).status_code == 403
        profile = roster.client.get(f"/api/admin/teachers/{account.user_id}").json()
        assert len(profile["assignments"]) == 1 and not profile["assignments"][0]["active"]
        assert len(assignment_rows(roster, account)) == 1
        history = roster.client.get(f"/api/grades/{grade['grade_id']}/history")
        assert history.status_code == 200 and len(history.json()["items"]) == 1
        assert roster.client.get(f"/api/tasks/{task_id}").status_code == 200
        assert (
            roster.client.patch(
                f"/api/admin/catalogs/{catalog}/1", json={"active": True}
            ).status_code
            == 200
        )
        assert client.get("/api/academic/workload").json()["total"] == 1
        assert client.get(f"/api/tasks/{task_id}").status_code == 200
        assert client.get(f"/api/grades/{grade['grade_id']}/history").status_code == 200


def test_workload_accepts_learning_view_and_reads_permission_changes_live(roster):
    account = teacher(roster)
    assignment(roster, account)
    student = register(roster)
    assert manual_approval(roster, student, 1).status_code == 200
    with signed_in(roster, account) as client:
        with roster.store.connect(write=True) as connection:
            connection.execute(
                "DELETE FROM RolePermissions WHERE role_code='teacher' "
                "AND permission_code='Grades.View'"
            )
        assert client.get("/api/academic/workload").json()["total"] == 1
        response = client.get("/api/academic/catalog")
        assert response.status_code == 200, response.text
        catalog = response.json()
        assert {row["group_id"] for row in catalog["groups"]} == {1}
        assert {row["subject_id"] for row in catalog["subjects"]} == {1}
        assert {row["term_id"] for row in catalog["terms"]} == {1}
        assert catalog["students"] == []
        assert len(catalog["assignments"]) == 1
        assert client.post("/api/tasks", json=task_payload()).status_code == 201
        assert client.get("/api/students").status_code == 403
        with signed_in(roster, student) as learner:
            assert "Learning.View" in learner.get("/api/auth/me").json()["user"]["permissions"]
            assert learner.get("/api/academic/catalog").status_code == 403
        with roster.store.connect(write=True) as connection:
            connection.execute(
                "DELETE FROM RolePermissions WHERE role_code='teacher' "
                "AND permission_code='Learning.View'"
            )
        assert client.get("/api/academic/workload").status_code == 403
