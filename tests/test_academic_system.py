import json
import sqlite3
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.academic import router
from app.auth import Principal, current_principal
from app.config import Settings
from app.errors import AppError
from app.store import ROLES, Store, iso_utc, utc_now


@pytest.fixture
def academic(tmp_path):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "academic.sqlite3",
        upload_dir=tmp_path / "uploads",
        max_upload_bytes=1024,
    )
    store = Store(settings)
    store.initialize()
    store.seed_academics()
    identities = {}
    with store.connect(write=True) as connection:
        for user_id, role in enumerate(
            ("admin", "teacher", "teacher", "head", "auditor", "pending"), 1
        ):
            login = f"test_{role}_{user_id}"
            connection.execute(
                "INSERT INTO Users "
                "(user_id,login,display_name,password_hash,role_code,active,created_at) "
                "VALUES (?,?,?,?,?,1,?)",
                (user_id, login, f"Синтетикалық {role}", "unit-test-hash-unused", role, iso_utc()),
            )
            identities[user_id] = Principal(
                user_id=user_id,
                login=login,
                display_name=f"Синтетикалық {role}",
                role_code=role,
                permissions=frozenset(ROLES[role][1]),
                session_id=f"test-session-{user_id}",
            )
            connection.execute(
                "INSERT INTO Sessions (session_id,token_hash,csrf_hash,user_id,created_at,"
                "last_seen_at,expires_at,initial_ip,last_ip) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    f"test-session-{user_id}",
                    f"unused-token-hash-{user_id}",
                    f"unused-csrf-hash-{user_id}",
                    user_id,
                    iso_utc(),
                    iso_utc(),
                    iso_utc(utc_now() + timedelta(hours=1)),
                    "192.0.2.40",
                    "192.0.2.40",
                ),
            )
    active = {"actor": identities[1]}
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
        application, raise_server_exceptions=False, client=("192.0.2.40", 50000)
    ) as client:
        yield SimpleNamespace(
            client=client, store=store, settings=settings, identities=identities, active=active
        )


def assign(academic, user_id=2, group_id=1, subject_id=1, term_id=1, assigned=True):
    previous = academic.active["actor"]
    academic.active["actor"] = academic.identities[1]
    response = academic.client.put(
        "/api/academic/assignments",
        json={
            "user_id": user_id,
            "group_id": group_id,
            "subject_id": subject_id,
            "term_id": term_id,
            "assigned": assigned,
        },
    )
    academic.active["actor"] = previous
    return response


def grade(academic, group_id=1, subject_id=1, term_id=1):
    with academic.store.connect() as connection:
        row = connection.execute(
            "SELECT g.* FROM Grades g JOIN Students s ON s.student_id=g.student_id "
            "WHERE s.group_id=? AND g.subject_id=? AND g.term_id=? ORDER BY g.grade_id LIMIT 1",
            (group_id, subject_id, term_id),
        ).fetchone()
    return dict(row)


def upload(
    academic,
    filename="notes.txt",
    content=b"Synthetic learning document",
    mime="text/plain",
    **scope,
):
    return academic.client.post(
        "/api/documents",
        data={
            "subject_id": scope.get("subject_id", 1),
            "group_id": scope.get("group_id", 1),
            "term_id": scope.get("term_id", 1),
        },
        files={"file": (filename, content, mime)},
    )


def test_teacher_lists_and_edits_only_exact_assignment(academic):
    assert assign(academic).status_code == 200
    academic.active["actor"] = academic.identities[2]
    response = academic.client.get("/api/grades")
    assert response.status_code == 200
    assert response.json()["total"] == 4
    assert all(
        (item["group_id"], item["subject_id"], item["term_id"], item["can_edit"]) == (1, 1, 1, True)
        for item in response.json()["items"]
    )
    for scope in ({"group_id": 2}, {"subject_id": 2}, {"term_id": 2}):
        other = grade(academic, **scope)
        denied = academic.client.patch(
            f"/api/grades/{other['grade_id']}", json={"value": 90, "version": other["version"]}
        )
        assert denied.status_code == 403
        history = academic.client.get(f"/api/grades/{other['grade_id']}/history")
        assert history.status_code == 403
    with academic.store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM GradeHistory").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM AuditLogs WHERE action='grade_change' AND result='denied'"
            ).fetchone()[0]
            == 3
        )


def test_grade_audit_and_history_include_actor_ip_old_new_values(academic):
    assign(academic)
    academic.active["actor"] = academic.identities[2]
    before = grade(academic)
    response = academic.client.patch(
        f"/api/grades/{before['grade_id']}",
        json={"value": 91, "version": before["version"]},
        headers={"X-Forwarded-For": "198.51.100.200"},
    )
    assert response.status_code == 200
    assert response.json()["value"] == 91
    assert response.json()["version"] == before["version"] + 1
    history = academic.client.get(f"/api/grades/{before['grade_id']}/history").json()["items"]
    assert len(history) == 1
    assert history[0]["old_value"] == before["value"]
    assert history[0]["new_value"] == 91
    assert history[0]["actor_login"] == academic.identities[2].login
    assert history[0]["ip_address"] == "192.0.2.40"
    with academic.store.connect() as connection:
        audit = connection.execute(
            "SELECT * FROM AuditLogs WHERE audit_id=?", (history[0]["audit_id"],)
        ).fetchone()
    assert audit["user_id"] == academic.identities[2].login
    assert audit["role"]
    assert audit["occurred_at"].endswith("Z")
    assert audit["ip_address"] == "192.0.2.40"
    assert audit["object_id"] == str(before["grade_id"])
    assert json.loads(audit["old_value"]) == before["value"]
    assert json.loads(audit["new_value"]) == 91
    assert audit["result"] == "success"


def test_audit_failure_rolls_back_grade_and_history(academic, monkeypatch):
    before = grade(academic)

    def fail_audit(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic audit failure")

    monkeypatch.setattr(academic.store, "audit", fail_audit)
    response = academic.client.patch(
        f"/api/grades/{before['grade_id']}", json={"value": 99, "version": before["version"]}
    )
    assert response.status_code == 500
    assert grade(academic) == before
    with academic.store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM GradeHistory").fetchone()[0] == 0


def test_grade_version_conflict_does_not_overwrite_newer_value(academic):
    before = grade(academic)
    endpoint = f"/api/grades/{before['grade_id']}"
    assert academic.client.patch(endpoint, json={"value": 81, "version": 0}).status_code == 200
    assert academic.client.patch(endpoint, json={"value": 82, "version": 0}).status_code == 409
    assert grade(academic)["value"] == 81
    with academic.store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM GradeHistory").fetchone()[0] == 1


@pytest.mark.parametrize("value", [-1, 101, True, "80", 80.5])
def test_grade_value_validation_is_server_side(academic, value):
    before = grade(academic)
    response = academic.client.patch(
        f"/api/grades/{before['grade_id']}", json={"value": value, "version": 0}
    )
    assert response.status_code == 422
    assert grade(academic) == before


def test_auditor_pending_and_head_cannot_change_grades(academic):
    before = grade(academic)
    for user_id in (4, 5, 6):
        academic.active["actor"] = academic.identities[user_id]
        response = academic.client.patch(
            f"/api/grades/{before['grade_id']}", json={"value": 90, "version": 0}
        )
        assert response.status_code == 403
    academic.active["actor"] = academic.identities[4]
    assert academic.client.get("/api/grades").json()["total"] == 72
    academic.active["actor"] = academic.identities[5]
    assert academic.client.get("/api/grades").status_code == 403


def test_assignment_fk_role_validation_idempotency_and_revocation(academic):
    assert assign(academic).json()["changed"] is True
    assert assign(academic).json()["changed"] is False
    assert assign(academic, user_id=1).status_code == 422
    assert assign(academic, user_id=999).status_code == 422
    assert assign(academic, group_id=999).status_code == 422
    assert assign(academic, subject_id=999).status_code == 422
    assert assign(academic, term_id=999).status_code == 422
    assert assign(academic, assigned=False).json()["changed"] is True
    assert assign(academic, assigned=False).json()["changed"] is False
    with academic.store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM TeachingAssignments").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM AuditLogs WHERE action='assignment_change'"
            ).fetchone()[0]
            == 2
        )


def test_document_upload_download_and_soft_delete_are_audited(academic):
    response = upload(academic)
    assert response.status_code == 201
    document = response.json()
    assert "stored_name" not in document
    with academic.store.connect() as connection:
        stored_name = connection.execute(
            "SELECT stored_name FROM Documents WHERE document_id=?", (document["document_id"],)
        ).fetchone()[0]
    assert stored_name != "notes.txt"
    stored_path = academic.settings.upload_dir / stored_name
    assert stored_path.read_bytes() == b"Synthetic learning document"
    endpoint = f"/api/documents/{document['document_id']}"
    downloaded = academic.client.get(endpoint + "/content")
    assert downloaded.status_code == 200
    assert downloaded.content == stored_path.read_bytes()
    assert downloaded.headers["content-disposition"].startswith("attachment;")
    assert downloaded.headers["x-content-type-options"] == "nosniff"
    assert academic.client.delete(endpoint).status_code == 200
    assert stored_path.is_file()
    assert academic.client.get(endpoint + "/content").status_code == 404
    assert academic.client.get("/api/documents").json()["items"] == []
    with academic.store.connect() as connection:
        events = connection.execute(
            "SELECT * FROM AuditLogs WHERE object_type='document' ORDER BY audit_id"
        ).fetchall()
    assert [event["action"] for event in events] == [
        "document_upload",
        "document_view",
        "document_delete",
    ]
    assert all(event["ip_address"] == "192.0.2.40" for event in events)
    assert all(event["user_id"] == academic.identities[1].login for event in events)


@pytest.mark.parametrize(
    "filename,content,mime",
    [
        ("../escape.txt", b"synthetic text", "text/plain"),
        ("folder\\escape.txt", b"synthetic text", "text/plain"),
        ("page.html", b"<html>unsafe</html>", "text/html"),
        ("page.txt", b"<html>unsafe</html>", "text/plain"),
        ("fake.pdf", b"<script>unsafe</script>", "application/pdf"),
        ("fake.png", b"not an image", "image/png"),
        ("wrong.txt", b"synthetic text", "application/pdf"),
        ("nul.txt", b"hello\x00world", "text/plain"),
        ("bad.txt", b"\xff\xfe", "text/plain"),
    ],
)
def test_unsafe_uploads_are_rejected_without_file_or_db_record(academic, filename, content, mime):
    assert upload(academic, filename, content, mime).status_code == 422
    with academic.store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM Documents").fetchone()[0] == 0
    assert not academic.settings.upload_dir.exists()


def test_upload_size_bound_and_audit_failure_cleanup(academic, monkeypatch):
    assert upload(academic, content=b"x" * 1025).status_code == 413
    assert not academic.settings.upload_dir.exists()

    def fail_audit(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic audit failure")

    monkeypatch.setattr(academic.store, "audit", fail_audit)
    assert upload(academic).status_code == 500
    assert list(academic.settings.upload_dir.iterdir()) == []
    with academic.store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM Documents").fetchone()[0] == 0


def test_document_scope_cannot_be_bypassed_with_ids(academic):
    document = upload(academic).json()
    assign(academic, group_id=2)
    academic.active["actor"] = academic.identities[2]
    assert academic.client.get("/api/documents").json()["items"] == []
    assert (
        academic.client.get(f"/api/documents/{document['document_id']}/content").status_code == 403
    )
    assert upload(academic).status_code == 403
    academic.active["actor"] = academic.identities[5]
    assert academic.client.get("/api/documents").status_code == 403


def test_corrupt_stored_path_never_reads_outside_upload_directory(academic):
    document = upload(academic).json()
    outside = academic.settings.upload_dir.parent / "outside.txt"
    outside.write_text("private synthetic marker", encoding="utf-8")
    with academic.store.connect(write=True) as connection:
        connection.execute(
            "UPDATE Documents SET stored_name=? WHERE document_id=?",
            ("../outside.txt", document["document_id"]),
        )
    response = academic.client.get(f"/api/documents/{document['document_id']}/content")
    assert response.status_code == 500
    assert "private synthetic marker" not in response.text
    assert outside.read_text(encoding="utf-8") == "private synthetic marker"


def test_document_delete_audit_failure_rolls_back_soft_delete(academic, monkeypatch):
    document = upload(academic).json()

    def fail_audit(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic audit failure")

    monkeypatch.setattr(academic.store, "audit", fail_audit)
    response = academic.client.delete(f"/api/documents/{document['document_id']}")
    assert response.status_code == 500
    with academic.store.connect() as connection:
        stored = connection.execute(
            "SELECT * FROM Documents WHERE document_id=?", (document["document_id"],)
        ).fetchone()
    assert stored["deleted_at"] is None
    assert stored["deleted_by"] is None
    assert (academic.settings.upload_dir / stored["stored_name"]).is_file()


def test_teacher_delete_requires_permission_ownership_and_assignment(academic):
    assign(academic, user_id=2)
    assign(academic, user_id=3)
    academic.active["actor"] = academic.identities[2]
    document = upload(academic).json()
    endpoint = f"/api/documents/{document['document_id']}"
    assert academic.client.delete(endpoint).status_code == 403
    academic.active["actor"] = replace(
        academic.identities[3],
        permissions=academic.identities[3].permissions | {"Documents.Delete"},
    )
    assert academic.client.delete(endpoint).status_code == 403
    academic.active["actor"] = replace(
        academic.identities[2],
        permissions=academic.identities[2].permissions | {"Documents.Delete"},
    )
    assert academic.client.delete(endpoint).status_code == 200


def test_academic_seed_is_idempotent_without_creating_accounts(academic):
    academic.store.seed_academics()
    academic.store.seed_academics()
    with academic.store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM Groups").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM Students").fetchone()[0] == 12
        assert connection.execute("SELECT COUNT(*) FROM Subjects").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM Terms").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM Grades").fetchone()[0] == 72
        assert connection.execute("SELECT COUNT(*) FROM Users").fetchone()[0] == 6
