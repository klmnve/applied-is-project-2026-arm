import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.auth import Principal, current_principal
from app.config import Settings
from app.errors import AppError
from app.learning import router
from app.store import ROLES, Store, iso_utc


@pytest.fixture
def learning(tmp_path, monkeypatch):
    clock = {"now": datetime(2026, 11, 1, 9, tzinfo=UTC)}
    monkeypatch.setattr("app.learning.utc_now", lambda: clock["now"])
    monkeypatch.setattr("app.store.utc_now", lambda: clock["now"])
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "learning.sqlite3",
        upload_dir=tmp_path / "uploads",
        max_upload_bytes=1024,
    )
    store = Store(settings)
    store.initialize()
    store.seed_academics()
    identities = {}
    user_specs = (
        (1, "admin", None),
        (2, "teacher", None),
        (3, "teacher", None),
        (4, "student", 1),
        (5, "student", 2),
        (6, "student", 5),
        (7, "head", None),
        (8, "pending", None),
    )
    with store.connect(write=True) as connection:
        for user_id, role, student_id in user_specs:
            approval = "pending" if role == "pending" else "approved"
            login = f"learn_{role}_{user_id}"
            connection.execute(
                "INSERT INTO Users "
                "(user_id,login,display_name,password_hash,role_code,active,created_at,"
                "approval_status,student_id) VALUES (?,?,?,?,?,1,?,?,?)",
                (
                    user_id,
                    login,
                    f"Оқу {role}",
                    "unused-unit-test-hash",
                    role,
                    iso_utc(),
                    approval,
                    student_id,
                ),
            )
            identities[user_id] = Principal(
                user_id=user_id,
                login=login,
                display_name=f"Оқу {role}",
                role_code=role,
                permissions=frozenset(ROLES[role][1]),
                session_id=f"learning-session-{user_id}",
                student_id=student_id,
                approval_status=approval,
            )
        connection.executemany(
            "INSERT INTO TeachingAssignments(user_id,subject_id,group_id,term_id) VALUES (?,?,?,?)",
            [(2, 1, 1, 1), (3, 2, 2, 1)],
        )
    active = {"actor": identities[2]}
    application = FastAPI()
    application.state.settings = settings
    application.state.store = store
    application.include_router(router)
    application.dependency_overrides[current_principal] = lambda: active["actor"]

    @application.exception_handler(AppError)
    async def public_error(request: Request, error: AppError):
        return JSONResponse(
            status_code=error.status_code,
            content={"error": {"code": error.code, "message": error.message}},
        )

    with TestClient(
        application, raise_server_exceptions=False, client=("192.0.2.60", 50000)
    ) as client:
        yield SimpleNamespace(
            client=client,
            store=store,
            settings=settings,
            identities=identities,
            active=active,
            clock=clock,
        )


def task_payload(system, **changes):
    body = {
        "title": "Деректерді қорғау жұмысы",
        "description": "Өз шешіміңізді түсіндіріңіз.",
        "rules": "Дедлайнға дейін қайта тапсыруға болады.",
        "subject_id": 1,
        "group_id": 1,
        "term_id": 1,
        "published_at": iso_utc(system.clock["now"] - timedelta(hours=1)),
        "deadline": iso_utc(system.clock["now"] + timedelta(days=1)),
        "allow_late": False,
    }
    return {**body, **changes}


def create_task(system, **changes):
    return system.client.post("/api/tasks", json=task_payload(system, **changes))


def submit(system, task_id, user_id=4, text="Менің жұмысым.", version=0, file=None, **extra):
    previous = system.active["actor"]
    system.active["actor"] = system.identities[user_id]
    kwargs = {"data": {"text": text, "version": str(version), **extra}}
    if file is not None:
        kwargs["files"] = {"file": file}
    response = system.client.put(f"/api/tasks/{task_id}/submission", **kwargs)
    system.active["actor"] = previous
    return response


def review(
    system, submission_id, score=88, published=False, version=0, feedback="Жақсы негізделген."
):
    return system.client.patch(
        f"/api/submissions/{submission_id}/review",
        json={
            "score": score,
            "published": published,
            "version": version,
            "feedback": feedback,
        },
    )


def test_task_create_normalizes_offset_dates_and_enforces_exact_teacher_scope(learning):
    response = create_task(
        learning,
        published_at="2026-11-01T13:00:00+05:00",
        deadline="2026-11-02T14:00:00+05:00",
    )
    assert response.status_code == 201
    assert response.json()["published_at"] == "2026-11-01T08:00:00.000000Z"
    assert response.json()["deadline"] == "2026-11-02T09:00:00.000000Z"
    for change in ({"group_id": 2}, {"subject_id": 2}, {"term_id": 2}):
        assert create_task(learning, **change).status_code == 403
    learning.active["actor"] = learning.identities[3]
    assert learning.client.get("/api/tasks").json()["items"] == []
    assert learning.client.get(f"/api/tasks/{response.json()['task_id']}").status_code == 403


@pytest.mark.parametrize(
    "changes",
    [
        {"deadline": "2026-11-02T09:00:00"},
        {"published_at": "2026-11-01T09:00:00"},
        {"deadline": 1793610000},
        {"deadline": "2026-10-30T09:00:00Z"},
        {"allow_late": "false"},
        {"group_id": "1"},
        {"title": " "},
        {"unexpected": "field"},
    ],
)
def test_task_validation_rejects_naive_numeric_or_invalid_fields(learning, changes):
    assert create_task(learning, **changes).status_code == 422
    with learning.store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM Tasks").fetchone()[0] == 0


def test_student_sees_only_published_tasks_for_own_group_and_own_submission(learning):
    first = create_task(learning).json()
    create_task(learning, published_at="2026-11-02T09:00:00Z", deadline="2026-11-03T09:00:00Z")
    learning.active["actor"] = learning.identities[1]
    outside = create_task(learning, group_id=2).json()
    learning.active["actor"] = learning.identities[4]
    listed = learning.client.get("/api/tasks").json()
    assert [item["task_id"] for item in listed["items"]] == [first["task_id"]]
    assert listed["items"][0]["submission_count"] is None
    assert learning.client.get(f"/api/tasks/{outside['task_id']}").status_code == 403
    assert learning.client.get("/api/tasks", params={"group_id": 2}).json()["items"] == []
    assert create_task(learning).status_code == 403


def test_submission_uses_server_student_identity_and_blocks_other_students_ids(learning):
    task = create_task(learning).json()
    result = submit(learning, task["task_id"], student_id="2").json()
    assert result["student_id"] == 1
    learning.active["actor"] = learning.identities[5]
    assert learning.client.get(f"/api/submissions/{result['submission_id']}").status_code == 403
    assert (
        learning.client.get(f"/api/submissions/{result['submission_id']}/file").status_code == 403
    )
    assert learning.client.get(f"/api/tasks/{task['task_id']}/submissions").status_code == 403
    assert learning.client.get("/api/tasks").json()["items"][0]["my_submission"] is None
    assert submit(learning, task["task_id"], user_id=6).status_code == 403


def test_deadline_default_blocks_new_work_and_resubmission_at_exact_cutoff(learning):
    task = create_task(learning).json()
    first = submit(learning, task["task_id"]).json()
    assert first["version"] == 0
    assert (
        submit(learning, task["task_id"], text="Жаңартылған жұмыс.", version=0).status_code == 200
    )
    learning.clock["now"] += timedelta(days=1)
    closed = submit(
        learning,
        task["task_id"],
        version=1,
        student_id="2",
        now="2000-01-01T00:00:00Z",
        submitted_at="2000-01-01T00:00:00Z",
        deadline="2099-01-01T00:00:00Z",
        allow_late="true",
    )
    assert closed.status_code == 409
    assert closed.json()["error"]["code"] == "deadline_passed"
    assert submit(learning, task["task_id"], user_id=5).status_code == 409
    learning.active["actor"] = learning.identities[4]
    listed = learning.client.get("/api/tasks").json()["items"][0]
    assert listed["status"] == "closed"
    assert listed["deadline_passed"] is True
    assert listed["my_submission"]["can_resubmit"] is False


def test_explicit_allow_late_allows_late_first_submission_and_resubmission(learning):
    task = create_task(learning, allow_late=True).json()
    learning.clock["now"] += timedelta(days=2)
    first = submit(learning, task["task_id"])
    assert first.status_code == 200
    assert first.json()["is_late"] is True
    updated = submit(learning, task["task_id"], text="Кеш жаңартылған жұмыс.", version=0)
    assert updated.status_code == 200
    assert updated.json()["version"] == 1
    assert updated.json()["is_late"] is True


def test_unpublished_score_feedback_and_reviewer_never_leak_to_own_student(learning):
    task = create_task(learning).json()
    work = submit(learning, task["task_id"]).json()
    reviewed = review(learning, work["submission_id"], score=97, feedback="Жарияланбаған пікір.")
    assert reviewed.status_code == 200
    assert reviewed.json()["score"] == 97
    learning.active["actor"] = learning.identities[4]
    own = learning.client.get(f"/api/submissions/{work['submission_id']}").json()
    listed = learning.client.get("/api/tasks").json()["items"][0]["my_submission"]
    detail = learning.client.get(f"/api/tasks/{task['task_id']}").json()["my_submission"]
    for item in (own, listed, detail):
        assert item["score"] is None
        assert item["feedback"] == ""
        assert item["reviewed_by"] is None
        assert item["reviewed_at"] is None
        assert item["published"] is False
    assert review(learning, work["submission_id"]).status_code == 403
    learning.active["actor"] = learning.identities[2]
    assert (
        review(learning, work["submission_id"], score=97, published=True, version=1).status_code
        == 200
    )
    learning.active["actor"] = learning.identities[4]
    visible = learning.client.get(f"/api/submissions/{work['submission_id']}").json()
    assert visible["score"] == 97
    assert visible["feedback"]
    assert visible["status"] == "reviewed"


@pytest.mark.parametrize("score", [-1, 101, True, "80", 80.5])
def test_review_score_is_strict_server_validated_zero_to_hundred(learning, score):
    task = create_task(learning).json()
    work = submit(learning, task["task_id"]).json()
    assert review(learning, work["submission_id"], score=score).status_code == 422
    with learning.store.connect() as connection:
        assert connection.execute("SELECT score FROM Submissions").fetchone()[0] is None


def test_review_scope_and_optimistic_version_prevent_cross_teacher_or_stale_changes(learning):
    task = create_task(learning).json()
    work = submit(learning, task["task_id"]).json()
    learning.active["actor"] = learning.identities[3]
    assert review(learning, work["submission_id"]).status_code == 403
    assert learning.client.get(f"/api/tasks/{task['task_id']}/submissions").status_code == 403
    learning.active["actor"] = learning.identities[7]
    assert review(learning, work["submission_id"]).status_code == 403
    learning.active["actor"] = learning.identities[2]
    assert review(learning, work["submission_id"], published=True).status_code == 200
    assert review(learning, work["submission_id"], score=44, version=0).status_code == 409
    revised = submit(learning, task["task_id"], text="Қайта өңделген шешім.", version=1)
    assert revised.status_code == 200
    assert revised.json()["score"] is None
    assert revised.json()["published"] is False
    assert review(learning, work["submission_id"], score=55, version=1).status_code == 409


def test_review_audit_has_old_new_score_actor_utc_ip_without_feedback_content(learning):
    task = create_task(learning).json()
    work = submit(learning, task["task_id"]).json()
    assert learning.client.get(f"/api/submissions/{work['submission_id']}").status_code == 200
    assert (
        review(
            learning,
            work["submission_id"],
            score=81,
            published=False,
            feedback="PRIVATE_FEEDBACK_SENTINEL",
        ).status_code
        == 200
    )
    assert (
        review(learning, work["submission_id"], score=92, published=True, version=1).status_code
        == 200
    )
    with learning.store.connect() as connection:
        rows = connection.execute(
            "SELECT * FROM AuditLogs WHERE action='submission_review' ORDER BY audit_id"
        ).fetchall()
    assert len(rows) == 2
    assert json.loads(rows[1]["old_value"])["score"] == 81
    assert json.loads(rows[1]["new_value"])["score"] == 92
    assert all(row["user_id"] == learning.identities[2].login for row in rows)
    assert all(row["ip_address"] == "192.0.2.60" for row in rows)
    assert all(row["occurred_at"].endswith("Z") for row in rows)
    assert "PRIVATE_FEEDBACK_SENTINEL" not in str([tuple(row) for row in rows])


def test_task_and_review_audit_failure_roll_back_mutations(learning, monkeypatch):
    task = create_task(learning).json()
    work = submit(learning, task["task_id"]).json()

    def fail_audit(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic audit failure")

    monkeypatch.setattr(learning.store, "audit", fail_audit)
    assert create_task(learning).status_code == 500
    assert review(learning, work["submission_id"], score=99, published=True).status_code == 500
    with learning.store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM Tasks").fetchone()[0] == 1
        row = connection.execute("SELECT * FROM Submissions").fetchone()
    assert row["score"] is None
    assert row["version"] == 0
    with learning.store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM SubmissionReviewHistory").fetchone()[0] == 0


def test_safe_task_material_and_student_files_follow_scope_and_audit(learning):
    task = create_task(learning).json()
    material = learning.client.post(
        f"/api/tasks/{task['task_id']}/material",
        data={"version": 0},
        files={"file": ("instructions.txt", b"Safe task instructions", "text/plain")},
    )
    assert material.status_code == 200
    assert material.json()["version"] == 1
    assert "material_stored_name" not in material.json()
    learning.active["actor"] = learning.identities[4]
    fetched = learning.client.get(f"/api/tasks/{task['task_id']}/material")
    assert fetched.status_code == 200
    assert fetched.content == b"Safe task instructions"
    assert fetched.headers["content-disposition"].startswith("attachment;")
    learning.active["actor"] = learning.identities[6]
    assert learning.client.get(f"/api/tasks/{task['task_id']}/material").status_code == 403
    learning.active["actor"] = learning.identities[2]
    work = submit(
        learning, task["task_id"], file=("answer.txt", b"Safe student answer", "text/plain")
    ).json()
    assert "stored_name" not in work
    downloaded = learning.client.get(f"/api/submissions/{work['submission_id']}/file")
    assert downloaded.content == b"Safe student answer"
    learning.active["actor"] = learning.identities[5]
    assert learning.client.get(f"/api/submissions/{work['submission_id']}/file").status_code == 403
    with learning.store.connect() as connection:
        actions = {
            row[0]
            for row in connection.execute("SELECT action FROM AuditLogs WHERE result='success'")
        }
        material_audit = connection.execute(
            "SELECT new_value FROM AuditLogs "
            "WHERE action='task_material_upload' AND result='success'"
        ).fetchone()[0]
        work_audit = connection.execute(
            "SELECT new_value FROM AuditLogs WHERE action='submission_submit' AND result='success'"
        ).fetchone()[0]
    material_metadata, work_metadata = json.loads(material_audit), json.loads(work_audit)
    for metadata, content, version in (
        (material_metadata, b"Safe task instructions", 1),
        (work_metadata, b"Safe student answer", 0),
    ):
        assert len(metadata["file_ref"].split(".")[0]) == 32
        assert "/" not in metadata["file_ref"] and "\\" not in metadata["file_ref"]
        assert metadata["mime_type"] == "text/plain"
        assert metadata["size_bytes"] == len(content)
        assert metadata["content_sha256"] == hashlib.sha256(content).hexdigest()
        assert metadata["version"] == version
        assert content.decode() not in json.dumps(metadata)
    assert "instructions.txt" not in material_audit and "answer.txt" not in work_audit
    learning.active["actor"] = learning.identities[2]
    assert submit(learning, task["task_id"], text="Text revision.", version=0).status_code == 200
    replacement = b"Replacement student attachment"
    assert (
        submit(
            learning,
            task["task_id"],
            text="Updated attachment.",
            version=1,
            file=("replacement.txt", replacement, "text/plain"),
        ).status_code
        == 200
    )
    with learning.store.connect() as connection:
        revisions = connection.execute(
            "SELECT old_value,new_value FROM AuditLogs "
            "WHERE action='submission_resubmit' ORDER BY audit_id"
        ).fetchall()
    retained = json.loads(revisions[0]["new_value"])
    old, new = json.loads(revisions[1]["old_value"]), json.loads(revisions[1]["new_value"])
    assert retained["file_ref"] == work_metadata["file_ref"]
    assert "content_sha256" not in retained
    assert old["file_ref"] == work_metadata["file_ref"]
    assert old["mime_type"] == "text/plain" and old["size_bytes"] == len(b"Safe student answer")
    assert new["file_ref"] != old["file_ref"]
    assert new["content_sha256"] == hashlib.sha256(replacement).hexdigest()
    assert new["version"] == 2
    replacement_material = b"Replacement task material"
    assert (
        learning.client.post(
            f"/api/tasks/{task['task_id']}/material",
            data={"version": 1},
            files={"file": ("replacement-material.txt", replacement_material, "text/plain")},
        ).status_code
        == 200
    )
    with learning.store.connect() as connection:
        replaced = connection.execute(
            "SELECT old_value,new_value FROM AuditLogs WHERE action='task_material_upload' "
            "ORDER BY audit_id DESC LIMIT 1"
        ).fetchone()
    material_old, material_new = (
        json.loads(replaced["old_value"]),
        json.loads(replaced["new_value"]),
    )
    assert material_old["file_ref"] == material_metadata["file_ref"]
    assert material_old["mime_type"] == "text/plain"
    assert material_old["size_bytes"] == len(b"Safe task instructions")
    assert material_new["file_ref"] != material_old["file_ref"]
    assert material_new["content_sha256"] == hashlib.sha256(replacement_material).hexdigest()
    assert material_old["version"] == 1 and material_new["version"] == 2
    assert {
        "task_material_upload",
        "task_material_view",
        "submission_submit",
        "submission_file_view",
    } <= actions


@pytest.mark.parametrize(
    "filename,content,mime",
    [
        ("../escape.txt", b"safe", "text/plain"),
        ("page.html", b"<html>bad</html>", "text/html"),
        ("fake.pdf", b"not pdf", "application/pdf"),
        ("binary.txt", b"hello\x00world", "text/plain"),
        ("notes.txt", b"safe", "application/pdf"),
    ],
)
def test_learning_file_upload_validation_covers_materials_and_submissions(
    learning, filename, content, mime
):
    task = create_task(learning).json()
    material = learning.client.post(
        f"/api/tasks/{task['task_id']}/material",
        data={"version": 0},
        files={"file": (filename, content, mime)},
    )
    assert material.status_code == 422
    assert submit(learning, task["task_id"], file=(filename, content, mime)).status_code == 422
    assert not learning.settings.upload_dir.exists()


def test_large_or_failed_submission_upload_creates_no_work_or_orphan_file(learning, monkeypatch):
    task = create_task(learning).json()
    assert (
        submit(learning, task["task_id"], file=("large.txt", b"x" * 1025, "text/plain")).status_code
        == 413
    )

    def fail_audit(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic audit failure")

    monkeypatch.setattr(learning.store, "audit", fail_audit)
    assert (
        submit(
            learning, task["task_id"], file=("answer.txt", b"Safe student answer", "text/plain")
        ).status_code
        == 500
    )
    assert list(learning.settings.upload_dir.iterdir()) == []
    with learning.store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM Submissions").fetchone()[0] == 0


def test_task_edit_conflicts_scope_lock_and_pending_denial(learning):
    task = create_task(learning).json()
    edited = learning.client.patch(
        f"/api/tasks/{task['task_id']}", json={"version": 0, "title": "Жаңартылған тапсырма"}
    )
    assert edited.status_code == 200
    assert (
        learning.client.patch(
            f"/api/tasks/{task['task_id']}", json={"version": 0, "title": "Ескі өзгеріс"}
        ).status_code
        == 409
    )
    assert (
        learning.client.patch(
            f"/api/tasks/{task['task_id']}", json={"version": 1, "group_id": 2}
        ).status_code
        == 403
    )
    submit(learning, task["task_id"])
    learning.active["actor"] = learning.identities[1]
    assert (
        learning.client.patch(
            f"/api/tasks/{task['task_id']}", json={"version": 1, "group_id": 2}
        ).status_code
        == 409
    )
    learning.active["actor"] = replace(
        learning.identities[8],
        permissions=frozenset({"Learning.View", "Learning.Manage", "Learning.Submit"}),
    )
    assert learning.client.get("/api/tasks").status_code == 403
    assert create_task(learning).status_code == 403


def test_submission_path_confinement_rejects_tampered_database_filename(learning):
    task = create_task(learning).json()
    work = submit(
        learning, task["task_id"], file=("answer.txt", b"Safe answer", "text/plain")
    ).json()
    outside = learning.settings.upload_dir.parent / "outside.txt"
    outside.write_text("PRIVATE_OUTSIDE_SENTINEL", encoding="utf-8")
    with learning.store.connect(write=True) as connection:
        connection.execute(
            "UPDATE Submissions SET stored_name=? WHERE submission_id=?",
            ("../outside.txt", work["submission_id"]),
        )
    response = learning.client.get(f"/api/submissions/{work['submission_id']}/file")
    assert response.status_code == 500
    assert "PRIVATE_OUTSIDE_SENTINEL" not in response.text


@pytest.mark.parametrize("kind", ["detail", "file", "material", "list"])
def test_scope_revocation_between_session_check_and_view_audit_is_enforced(
    learning, monkeypatch, kind
):
    task = create_task(learning).json()
    work = submit(
        learning, task["task_id"], file=("answer.txt", b"Safe private answer", "text/plain")
    ).json()
    assert (
        learning.client.post(
            f"/api/tasks/{task['task_id']}/material",
            data={"version": 0},
            files={"file": ("instructions.txt", b"Safe instructions", "text/plain")},
        ).status_code
        == 200
    )
    original_connect = learning.store.connect
    changed = False

    @contextmanager
    def revoke_before_write(write=False):
        nonlocal changed
        if write and not changed:
            changed = True
            with original_connect(write=True) as connection:
                connection.execute("DELETE FROM TeachingAssignments WHERE user_id=2")
        with original_connect(write=write) as connection:
            yield connection

    monkeypatch.setattr(learning.store, "connect", revoke_before_write)
    paths = {
        "detail": f"/api/submissions/{work['submission_id']}",
        "file": f"/api/submissions/{work['submission_id']}/file",
        "material": f"/api/tasks/{task['task_id']}/material",
        "list": f"/api/tasks/{task['task_id']}/submissions",
    }
    response = learning.client.get(paths[kind])
    assert response.status_code == 403
    assert "Safe private answer" not in response.text
    with original_connect() as connection:
        assert (
            connection.execute(
                "SELECT result FROM AuditLogs ORDER BY audit_id DESC LIMIT 1"
            ).fetchone()[0]
            == "denied"
        )


def test_student_relink_between_session_check_and_work_view_denies_old_record(
    learning, monkeypatch
):
    task = create_task(learning).json()
    work = submit(learning, task["task_id"]).json()
    learning.active["actor"] = learning.identities[4]
    original_connect = learning.store.connect
    changed = False

    @contextmanager
    def relink_before_write(write=False):
        nonlocal changed
        if write and not changed:
            changed = True
            with original_connect(write=True) as connection:
                connection.execute("UPDATE Users SET student_id=3 WHERE user_id=4")
        with original_connect(write=write) as connection:
            yield connection

    monkeypatch.setattr(learning.store, "connect", relink_before_write)
    assert learning.client.get(f"/api/submissions/{work['submission_id']}").status_code == 403


@pytest.mark.parametrize("kind", ["material", "submission"])
def test_unexpected_audit_error_rolls_back_metadata_and_cleans_new_file(
    learning, monkeypatch, kind
):
    task = create_task(learning).json()

    def fail_audit(*args, **kwargs):
        raise RuntimeError("synthetic unexpected audit failure")

    monkeypatch.setattr(learning.store, "audit", fail_audit)
    if kind == "material":
        response = learning.client.post(
            f"/api/tasks/{task['task_id']}/material",
            data={"version": 0},
            files={"file": ("instructions.txt", b"Safe instructions", "text/plain")},
        )
    else:
        response = submit(
            learning, task["task_id"], file=("answer.txt", b"Safe answer", "text/plain")
        )
    assert response.status_code == 500
    assert list(learning.settings.upload_dir.iterdir()) == []
    with learning.store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM Submissions").fetchone()[0] == 0
        row = connection.execute("SELECT material_stored_name,version FROM Tasks").fetchone()
    assert row["material_stored_name"] is None
    assert row["version"] == 0


def test_task_edit_audit_preserves_title_scope_publication_and_private_content_evidence(learning):
    task = create_task(learning).json()
    learning.active["actor"] = learning.identities[1]
    response = learning.client.patch(
        f"/api/tasks/{task['task_id']}",
        json={
            "version": 0,
            "title": "Жаңа тапсырма атауы",
            "group_id": 2,
            "published_at": "2026-11-01T12:00:00+05:00",
            "description": "PRIVATE_DESCRIPTION_SENTINEL",
            "rules": "PRIVATE_RULES_SENTINEL",
        },
    )
    assert response.status_code == 200
    with learning.store.connect() as connection:
        row = connection.execute("SELECT * FROM AuditLogs WHERE action='task_update'").fetchone()
    old, new = json.loads(row["old_value"]), json.loads(row["new_value"])
    assert old["title"] == task["title"]
    assert new["title"] == "Жаңа тапсырма атауы"
    assert old["group_id"] == 1 and new["group_id"] == 2
    assert old["published_at"] == task["published_at"]
    assert new["published_at"] == "2026-11-01T07:00:00.000000Z"
    assert old["description_digest"] != new["description_digest"]
    assert old["rules_digest"] != new["rules_digest"]
    assert "PRIVATE_DESCRIPTION_SENTINEL" not in row["new_value"]
    assert "PRIVATE_RULES_SENTINEL" not in row["new_value"]


def test_server_filename_collision_never_removes_existing_file(learning, monkeypatch):
    task = create_task(learning).json()
    learning.settings.upload_dir.mkdir()
    existing = learning.settings.upload_dir / ("a" * 32 + ".txt")
    existing.write_bytes(b"Pre-existing safe content")
    monkeypatch.setattr("app.learning.uuid4", lambda: SimpleNamespace(hex="a" * 32))
    response = learning.client.post(
        f"/api/tasks/{task['task_id']}/material",
        data={"version": 0},
        files={"file": ("instructions.txt", b"New safe content", "text/plain")},
    )
    assert response.status_code == 500
    assert existing.read_bytes() == b"Pre-existing safe content"
    with learning.store.connect() as connection:
        assert connection.execute("SELECT material_stored_name FROM Tasks").fetchone()[0] is None


@pytest.mark.parametrize("allow_late", [False, True])
def test_create_requires_future_deadline_even_when_late_work_is_enabled(learning, allow_late):
    response = create_task(
        learning,
        published_at=iso_utc(learning.clock["now"] - timedelta(days=2)),
        deadline=iso_utc(learning.clock["now"]),
        allow_late=allow_late,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "deadline_in_past"
    with learning.store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM Tasks").fetchone()[0] == 0


def test_deadline_edit_persists_exact_utc_and_late_policy_but_rejects_new_past_time(learning):
    task = create_task(learning).json()
    response = learning.client.patch(
        f"/api/tasks/{task['task_id']}",
        json={"version": 0, "deadline": "2026-11-03T14:23:41.123456+05:00", "allow_late": True},
    )
    assert response.status_code == 200
    expected = "2026-11-03T09:23:41.123456Z"
    for endpoint in (f"/api/tasks/{task['task_id']}", "/api/tasks"):
        result = learning.client.get(endpoint).json()
        item = result["items"][0] if "items" in result else result
        assert item["deadline"] == expected and item["allow_late"] is True
    with learning.store.connect() as connection:
        row = connection.execute("SELECT deadline,allow_late FROM Tasks").fetchone()
        assert tuple(row) == (expected, 1)
    invalid = learning.client.patch(
        f"/api/tasks/{task['task_id']}",
        json={"version": 1, "deadline": iso_utc(learning.clock["now"] - timedelta(minutes=1))},
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "deadline_in_past"
    learning.clock["now"] += timedelta(days=4)
    unchanged = learning.client.patch(
        f"/api/tasks/{task['task_id']}",
        json={
            "version": 1,
            "title": "Closed task metadata edit",
            "deadline": expected,
            "allow_late": False,
        },
    )
    assert unchanged.status_code == 200
    assert unchanged.json()["deadline"] == expected and unchanged.json()["status"] == "closed"
    learning.active["actor"] = learning.identities[4]
    assert learning.client.get("/api/tasks").json()["items"][0]["deadline"] == expected


def test_atomic_initial_material_creates_one_scoped_task_and_two_linked_audits(learning):
    content = b"Initial atomic material"
    response = learning.client.post(
        "/api/tasks/with-material",
        data={"payload": json.dumps(task_payload(learning))},
        files={"file": ("initial.txt", content, "text/plain")},
    )
    assert response.status_code == 201
    task = response.json()
    assert task["material_name"] == "initial.txt" and task["version"] == 0
    with learning.store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM Tasks").fetchone()[0] == 1
        audits = connection.execute(
            "SELECT action,object_id,new_value FROM AuditLogs ORDER BY audit_id"
        ).fetchall()
    assert [row["action"] for row in audits] == ["task_create", "task_material_upload"]
    assert all(row["object_id"] == str(task["task_id"]) for row in audits)
    assert (
        json.loads(audits[1]["new_value"])["content_sha256"] == hashlib.sha256(content).hexdigest()
    )
    learning.active["actor"] = learning.identities[4]
    listed = learning.client.get("/api/tasks").json()["items"][0]
    assert listed["material_name"] == "initial.txt"
    assert learning.client.get(f"/api/tasks/{task['task_id']}/material").content == content
    learning.active["actor"] = learning.identities[6]
    assert learning.client.get(f"/api/tasks/{task['task_id']}/material").status_code == 403


@pytest.mark.parametrize("kind", ["json", "file", "size", "scope", "past", "reference"])
def test_atomic_invalid_material_or_payload_never_publishes_partial_task(learning, kind):
    payload = task_payload(learning)
    filename, content, mime = "initial.txt", b"Valid material", "text/plain"
    if kind == "file":
        filename, mime = "fake.pdf", "application/pdf"
    elif kind == "size":
        content = b"x" * 1025
    elif kind == "scope":
        payload["group_id"] = 2
    elif kind == "past":
        payload["deadline"] = iso_utc(learning.clock["now"])
    elif kind == "reference":
        learning.active["actor"] = learning.identities[1]
        payload["term_id"] = 999
    response = learning.client.post(
        "/api/tasks/with-material",
        data={"payload": "{invalid json" if kind == "json" else json.dumps(payload)},
        files={"file": (filename, content, mime)},
    )
    assert response.status_code == (403 if kind == "scope" else 413 if kind == "size" else 422)
    with learning.store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM Tasks").fetchone()[0] == 0
    assert not learning.settings.upload_dir.exists()


def test_atomic_material_audit_failure_rolls_back_task_both_audits_and_file(learning, monkeypatch):
    original = learning.store.audit

    def fail_material(connection, actor, action, *args, **kwargs):
        if action == "task_material_upload":
            raise sqlite3.OperationalError("synthetic material audit failure")
        return original(connection, actor, action, *args, **kwargs)

    monkeypatch.setattr(learning.store, "audit", fail_material)
    response = learning.client.post(
        "/api/tasks/with-material",
        data={"payload": json.dumps(task_payload(learning))},
        files={"file": ("initial.txt", b"Atomic material", "text/plain")},
    )
    assert response.status_code == 500
    with learning.store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM Tasks").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM AuditLogs").fetchone()[0] == 0
    assert list(learning.settings.upload_dir.iterdir()) == []


def test_atomic_material_rechecks_teacher_assignment_before_publishing(learning, monkeypatch):
    from app import learning as module

    original = module._save_file

    def revoke_after_staging(request, content, extension):
        result = original(request, content, extension)
        with learning.store.connect(write=True) as connection:
            connection.execute("DELETE FROM TeachingAssignments WHERE user_id=2")
        return result

    monkeypatch.setattr(module, "_save_file", revoke_after_staging)
    response = learning.client.post(
        "/api/tasks/with-material",
        data={"payload": json.dumps(task_payload(learning))},
        files={"file": ("initial.txt", b"Atomic scoped material", "text/plain")},
    )
    assert response.status_code == 403
    with learning.store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM Tasks").fetchone()[0] == 0
        assert connection.execute("SELECT result FROM AuditLogs").fetchone()[0] == "denied"
    assert list(learning.settings.upload_dir.iterdir()) == []


def test_private_review_history_has_exact_old_new_feedback_scores_versions_and_audit(learning):
    task = create_task(learning).json()
    work = submit(learning, task["task_id"]).json()
    first = "PRIVATE_DRAFT_FEEDBACK"
    final = "PRIVATE_PUBLISHED_FEEDBACK"
    assert review(learning, work["submission_id"], score=72, feedback=first).status_code == 200
    assert (
        review(
            learning,
            work["submission_id"],
            score=91,
            feedback=final,
            published=True,
            version=1,
        ).status_code
        == 200
    )
    result = learning.client.get(f"/api/submissions/{work['submission_id']}/history")
    assert result.status_code == 200 and result.json()["total"] == 2
    newest, oldest = result.json()["items"]
    assert (newest["old_feedback"], newest["new_feedback"]) == (first, final)
    assert (newest["old_score"], newest["new_score"]) == (72, 91)
    assert (newest["old_version"], newest["new_version"]) == (1, 2)
    assert newest["old_published"] is False and newest["new_published"] is True
    assert newest["actor_login"] == learning.identities[2].login
    assert newest["ip_address"] == "192.0.2.60" and newest["occurred_at"].endswith("Z")
    assert oldest["old_feedback"] == "" and oldest["old_score"] is None
    with learning.store.connect() as connection:
        event = connection.execute(
            "SELECT * FROM AuditLogs WHERE audit_id=?",
            (newest["audit_id"],),
        ).fetchone()
        assert event["action"] == "submission_review"
        assert json.loads(event["new_value"])["version"] == 2
        assert first not in event["old_value"] and final not in event["new_value"]
    for user_id, expected in ((1, 200), (7, 200), (3, 403), (4, 403), (5, 403), (6, 403)):
        learning.active["actor"] = learning.identities[user_id]
        response = learning.client.get(f"/api/submissions/{work['submission_id']}/history")
        assert response.status_code == expected
        if expected == 403:
            assert first not in response.text and final not in response.text
    learning.active["actor"] = replace(
        learning.identities[4],
        permissions=frozenset({"Learning.View", "Learning.Manage", "Learning.Submit"}),
    )
    assert (
        learning.client.get(f"/api/submissions/{work['submission_id']}/history").status_code == 403
    )


def test_review_history_insert_failure_rolls_back_grade_review_and_audit(learning):
    task = create_task(learning).json()
    work = submit(learning, task["task_id"]).json()
    with learning.store.connect(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER fail_review_history BEFORE INSERT ON SubmissionReviewHistory "
            "BEGIN SELECT RAISE(ABORT,'synthetic history failure'); END"
        )
    assert review(learning, work["submission_id"], score=99, published=True).status_code == 500
    with learning.store.connect() as connection:
        row = connection.execute("SELECT * FROM Submissions").fetchone()
        assert row["score"] is None and row["feedback"] == "" and row["version"] == 0
        assert connection.execute("SELECT COUNT(*) FROM SubmissionReviewHistory").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM AuditLogs WHERE action='submission_review'"
            ).fetchone()[0]
            == 0
        )
