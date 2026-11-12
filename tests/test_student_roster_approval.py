"""Official roster creation is an explicit, atomic administrator decision."""

import json
import secrets
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password, verify_password
from app.bootstrap import create_admin
from app.config import Settings
from app.db import AuditRepository
from app.main import create_app
from app.schemas import Action, SearchFilters, SummaryClaim, SummaryPlan
from app.service import render_summary
from app.store import Store, iso_utc, utc_now
from tests.conftest import FakeEmbedder, FakeLLM
from tests.test_auth_system import sign_in


@pytest.fixture(scope="module")
def roster_credentials():
    # Hashing strength is covered by auth tests. Cache one actually valid, random
    # password/hash pair here; cookies, CSRF, DB permissions and sessions stay real.
    password = secrets.token_urlsafe(24)
    encoded = hash_password(password)
    assert verify_password(password, encoded)
    return password, encoded


@pytest.fixture
def roster(tmp_path, monkeypatch, roster_credentials):
    password, encoded = roster_credentials

    def cached_hash(value):
        return encoded if value == password else hash_password(value)

    def cached_verify(value, stored):
        return True if value == password and stored == encoded else verify_password(value, stored)

    monkeypatch.setattr("app.bootstrap.hash_password", cached_hash)
    monkeypatch.setattr("app.auth_routes.hash_password", cached_hash)
    monkeypatch.setattr("app.auth_routes.verify_password", cached_verify)
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "roster.sqlite3",
        upload_dir=tmp_path / "uploads",
    )
    store = Store(settings)
    create_admin(store, "roster_admin", "Roster administrator", password)
    application = create_app(settings, llm=FakeLLM(), embedder=FakeEmbedder())
    with TestClient(
        application,
        client=("192.0.2.50", 51000),
        raise_server_exceptions=False,
    ) as client:
        sign_in(client, "roster_admin", password)
        yield SimpleNamespace(
            client=client,
            app=application,
            store=store,
            password=password,
            password_hash=encoded,
            settings=settings,
        )


def register(system, login="roster_applicant", role="student", **changes):
    claim = (
        {
            "student_claim_name": "  Асыл   Жаңабаева  ",
            "student_claim_number": "new-2026-001",
            "student_claim_group_id": 2,
        }
        if role == "student"
        else {}
    )
    payload = {
        "login": login,
        "password": system.password,
        "display_name": "Unverified account name",
        "requested_role": role,
        **claim,
        **changes,
    }
    response = system.client.post("/api/auth/register", json=payload)
    assert response.status_code == 201, response.text
    with system.store.connect() as connection:
        user_id = connection.execute(
            "SELECT user_id FROM Users WHERE login=?", (login,)
        ).fetchone()[0]
    return SimpleNamespace(user_id=user_id, login=login)


def legacy_student_request(system, login):
    """Model a migrated student request created before StudentClaims existed.

    Current signup requires a complete claim. A teacher signup is not a legacy
    student request and must retain the teacher-only approval restriction.
    """
    with system.store.connect(write=True) as connection:
        cursor = connection.execute(
            "INSERT INTO Users(login,display_name,password_hash,role_code,active,created_at,"
            "requested_role,approval_status) VALUES (?,?,?,'pending',0,?,'student','pending')",
            (login, "Legacy student applicant", system.password_hash, iso_utc()),
        )
        user_id = cursor.lastrowid
        assert user_id is not None
        assert (
            connection.execute("SELECT 1 FROM StudentClaims WHERE user_id=?", (user_id,)).fetchone()
            is None
        )
    return SimpleNamespace(user_id=user_id, login=login)


def approve_new(system, applicant, client=None, **changes):
    client = client if client is not None else system.client
    if "claim_revision" not in changes:
        detail = client.get(f"/api/admin/requests/{applicant.user_id}")
        if detail.status_code == 200:
            changes["claim_revision"] = detail.json()["claim_revision"]
    return client.post(
        f"/api/admin/requests/{applicant.user_id}/approve",
        json={
            "role_code": "student",
            "create_student": True,
            "confirmed_group_id": 2,
            "confirm_student_match": True,
            **changes,
        },
    )


def manual_approval(system, applicant, student_id):
    revision = system.client.get(f"/api/admin/requests/{applicant.user_id}").json()[
        "claim_revision"
    ]
    return system.client.post(
        f"/api/admin/requests/{applicant.user_id}/approve",
        json={
            "role_code": "student",
            "student_id": student_id,
            "confirm_student_match": True,
            "claim_revision": revision,
            "acknowledge_mismatch": True,
        },
    )


def official_students(system):
    with system.store.connect() as connection:
        return [
            dict(row) for row in connection.execute("SELECT * FROM Students ORDER BY student_id")
        ]


def assert_pending(system, applicant):
    with system.store.connect() as connection:
        row = connection.execute(
            "SELECT role_code,active,approval_status,student_id FROM Users WHERE user_id=?",
            (applicant.user_id,),
        ).fetchone()
    assert tuple(row) == ("pending", 0, "pending", None)


def test_pending_registration_and_profile_edits_never_add_or_overwrite_official_students(roster):
    before = official_students(roster)
    applicant = register(roster)
    with TestClient(roster.app, client=("192.0.2.51", 51001)) as owner:
        sign_in(owner, applicant.login, roster.password)
        own = owner.get("/api/auth/me").json()["user"]
        assert own["student_claim"]["name"] == "Асыл Жаңабаева"
        assert own["student_claim"]["group_id"] == 2 and own["student_id"] is None
        assert owner.get("/api/tasks").status_code == owner.get("/api/students").status_code == 403
        for invalid_group in (99999, 2**63):
            invalid = owner.put(
                "/api/auth/student-claim",
                json={
                    "student_claim_name": "Дана Жаңабаева",
                    "student_claim_number": "NEW-2026-001",
                    "student_claim_group_id": invalid_group,
                },
            )
            assert invalid.status_code == 422
        corrected = owner.put(
            "/api/auth/student-claim",
            json={
                "student_claim_name": "Дана Жаңабаева",
                "student_claim_number": "NEW-2026-001",
                "student_claim_group_id": 3,
            },
        )
        assert corrected.status_code == 200
    assert official_students(roster) == before
    assert_pending(roster, applicant)
    listing = roster.client.get("/api/students").json()
    assert listing["total"] == 12
    assert not any(row["display_name"] == "Дана Жаңабаева" for row in listing["items"])
    with roster.store.connect() as connection:
        change = connection.execute(
            "SELECT old_value,new_value FROM AuditLogs WHERE action='student_claim_update'"
        ).fetchone()
    assert json.loads(change["old_value"])["group_id"] == 2
    metadata = json.loads(change["new_value"])
    assert metadata["group_id"] == 3 and metadata["name_changed"] is True
    assert "Дана Жаңабаева" not in change["new_value"] and "NEW-2026-001" not in change["new_value"]


def test_approval_creates_one_official_student_and_live_owner_can_submit_own_work(roster):
    applicant = register(roster)
    with TestClient(roster.app, client=("192.0.2.51", 51001)) as owner:
        sign_in(owner, applicant.login, roster.password)
        response = approve_new(roster, applicant)
        assert response.status_code == 200, response.text
        student_id = response.json()["user"]["student_id"]
        assert student_id > 12
        with roster.store.connect() as connection:
            row = dict(
                connection.execute(
                    "SELECT * FROM Students WHERE student_id=?", (student_id,)
                ).fetchone()
            )
        assert (row["display_name"], row["student_code"], row["group_id"]) == (
            "Асыл Жаңабаева",
            "NEW-2026-001",
            2,
        )
        own = owner.get("/api/auth/me").json()["user"]
        assert own["active"] is True and own["approval_status"] == "approved"
        assert own["student_id"] == student_id and own["role_code"] == "student"
        assert owner.get("/api/students").status_code == 403
        roster_result = roster.client.get(
            "/api/students", params={"group_id": 2, "q": row["display_name"]}
        )
        assert roster_result.status_code == 200
        assert roster_result.json()["total"] == 1
        item = roster_result.json()["items"][0]
        assert item["student_id"] == student_id and item["group_name"] == "CS-102"
        groups = roster.client.get("/api/auth/registration-options").json()["groups"]
        assert item["group_name"] == next(
            group["name"] for group in groups if group["group_id"] == 2
        )
        now = utc_now()
        task_payload = {
            "title": "New official student task",
            "description": "Explain the solution.",
            "group_id": 2,
            "subject_id": 1,
            "term_id": 1,
            "published_at": iso_utc(now - timedelta(minutes=1)),
            "deadline": iso_utc(now + timedelta(days=1)),
        }
        task = roster.client.post("/api/tasks", json=task_payload).json()
        hidden = roster.client.post("/api/tasks", json=task_payload | {"group_id": 1}).json()
        assert [item["task_id"] for item in owner.get("/api/tasks").json()["items"]] == [
            task["task_id"]
        ]
        assert owner.get(f"/api/tasks/{hidden['task_id']}").status_code == 403
        work = owner.put(
            f"/api/tasks/{task['task_id']}/submission",
            data={"text": "My own new-student work", "student_id": "1"},
        )
        assert work.status_code == 200 and work.json()["student_id"] == student_id
    assert len(official_students(roster)) == 13
    repeated = approve_new(roster, applicant)
    assert repeated.status_code == 409 and len(official_students(roster)) == 13


def test_admin_confirmed_group_controls_official_row_without_rewriting_unverified_claim(roster):
    applicant = register(roster)
    response = approve_new(roster, applicant, confirmed_group_id=1)
    assert response.status_code == 200
    student_id = response.json()["user"]["student_id"]
    with roster.store.connect() as connection:
        assert (
            connection.execute(
                "SELECT group_id FROM Students WHERE student_id=?", (student_id,)
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT student_claim_group_id FROM StudentClaims WHERE user_id=?",
                (applicant.user_id,),
            ).fetchone()[0]
            == 2
        )
    assert (
        roster.client.get("/api/students", params={"group_id": 2, "q": "NEW-2026-001"}).json()[
            "total"
        ]
        == 0
    )
    assert (
        roster.client.get("/api/students", params={"group_id": 1, "q": "NEW-2026-001"}).json()[
            "total"
        ]
        == 1
    )


@pytest.mark.parametrize("confirm_distinct", [False, True])
def test_canonical_existing_code_name_and_confirmed_group_reuse_without_overwrite(
    roster, confirm_distinct
):
    with roster.store.connect(write=True) as connection:
        connection.execute("UPDATE Students SET student_code='syn-001' WHERE student_id=1")
        existing = dict(connection.execute("SELECT * FROM Students WHERE student_id=1").fetchone())
    applicant = register(
        roster,
        student_claim_name="  " + existing["display_name"].upper() + "  ",
        student_claim_number="SYN-001",
        student_claim_group_id=1,
    )
    before = official_students(roster)
    response = approve_new(
        roster,
        applicant,
        confirmed_group_id=1,
        confirm_distinct_student=confirm_distinct,
    )
    assert response.status_code == 200 and response.json()["user"]["student_id"] == 1
    assert official_students(roster) == before
    with roster.store.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM AuditLogs WHERE action='student_create'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("collision", ["code_name", "code_group", "name_only", "case_ambiguous"])
def test_ambiguous_existing_identity_requires_manual_selection_without_automatic_changes(
    roster, collision
):
    original = official_students(roster)[0]
    changes = {
        "student_claim_number": "SYN-001",
        "student_claim_name": original["display_name"],
        "student_claim_group_id": 1,
    }
    confirmed_group = 1
    if collision == "code_name":
        changes["student_claim_name"] = "Басқа Адамның Есімі"
    elif collision == "code_group":
        confirmed_group = 2
    elif collision == "name_only":
        changes["student_claim_number"] = "OTHER-2026-001"
    else:
        with roster.store.connect(write=True) as connection:
            connection.execute(
                "INSERT INTO Students(display_name,student_code,group_id) VALUES (?,?,?)",
                ("Case-collision legacy record", "syn-001", 2),
            )
    applicant = register(roster, **changes)
    before = official_students(roster)
    blocked = approve_new(roster, applicant, confirmed_group_id=confirmed_group)
    assert (
        blocked.status_code == 409
        and blocked.json()["error"]["code"] == "student_selection_required"
    )
    assert official_students(roster) == before
    assert_pending(roster, applicant)
    selected = manual_approval(roster, applicant, 1)
    assert selected.status_code == 200 and selected.json()["user"]["student_id"] == 1
    assert official_students(roster) == before


@pytest.mark.parametrize("group", [None, 99999, 0, "2", True, 2.0, 2**63])
def test_new_official_student_requires_an_explicit_strict_existing_group(roster, group):
    applicant = register(roster)
    response = approve_new(roster, applicant, confirmed_group_id=group)
    assert response.status_code == 422
    if group is None or group == 99999:
        assert response.json()["error"]["code"] == "student_group_confirmation_required"
    assert len(official_students(roster)) == 12
    assert_pending(roster, applicant)


def test_create_mode_flags_reject_invalid_types_role_and_manual_target_combinations(roster):
    applicant = register(roster)
    for invalid in (
        {"create_student": "true"},
        {"create_student": 1},
        {"student_id": 1},
        {"role_code": "teacher"},
        {"create_student": False, "student_id": 1},
        {"confirm_distinct_student": "true"},
        {"confirm_distinct_student": 1},
        {"confirm_distinct_student": None},
        {
            "create_student": False,
            "confirmed_group_id": None,
            "student_id": 1,
            "confirm_distinct_student": True,
        },
        {
            "role_code": "teacher",
            "create_student": False,
            "confirmed_group_id": None,
            "confirm_distinct_student": True,
        },
    ):
        response = approve_new(roster, applicant, **invalid)
        assert response.status_code == 422, invalid
        assert_pending(roster, applicant)
    assert len(official_students(roster)) == 12


def test_deliberate_distinct_person_confirmation_allows_legitimate_homonym_and_is_audited(roster):
    existing = official_students(roster)[0]
    applicant = register(
        roster,
        student_claim_name=existing["display_name"],
        student_claim_number="DISTINCT-2026-001",
        student_claim_group_id=2,
    )
    unconfirmed = approve_new(roster, applicant)
    assert unconfirmed.status_code == 409
    assert unconfirmed.json()["error"]["code"] == "student_selection_required"
    assert_pending(roster, applicant)
    confirmed = approve_new(roster, applicant, confirm_distinct_student=True)
    assert confirmed.status_code == 200
    new_id = confirmed.json()["user"]["student_id"]
    assert new_id != existing["student_id"]
    with roster.store.connect() as connection:
        original = dict(
            connection.execute(
                "SELECT * FROM Students WHERE student_id=?", (existing["student_id"],)
            ).fetchone()
        )
        created = connection.execute(
            "SELECT * FROM Students WHERE student_id=?", (new_id,)
        ).fetchone()
        event = connection.execute(
            "SELECT new_value FROM AuditLogs WHERE action='user_approve' AND object_id=?",
            (str(applicant.user_id),),
        ).fetchone()[0]
    assert original == existing
    assert created["display_name"] == existing["display_name"]
    assert created["student_code"] == "DISTINCT-2026-001" and created["group_id"] == 2
    assert json.loads(event)["distinct_student_confirmed"] is True
    assert "DISTINCT-2026-001" not in event and existing["display_name"] not in event
    assert len(official_students(roster)) == 13


@pytest.mark.parametrize("collision", ["name", "group", "case_ambiguous"])
def test_distinct_person_confirmation_never_overrides_an_official_code_conflict(roster, collision):
    existing = official_students(roster)[0]
    name, group = existing["display_name"], 1
    if collision == "name":
        name = "Different Person With An Existing Code"
    elif collision == "group":
        group = 2
    else:
        with roster.store.connect(write=True) as connection:
            connection.execute(
                "INSERT INTO Students(display_name,student_code,group_id) VALUES (?,?,?)",
                ("Legacy duplicate code case", "syn-001", 2),
            )
    applicant = register(
        roster,
        student_claim_name=name,
        student_claim_number="SYN-001",
        student_claim_group_id=group,
    )
    before = official_students(roster)
    response = approve_new(
        roster,
        applicant,
        confirmed_group_id=group,
        confirm_distinct_student=True,
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "student_selection_required"
    assert official_students(roster) == before
    assert_pending(roster, applicant)


def test_new_student_requires_current_claim_explicit_match_and_fresh_revision(roster):
    applicant = register(roster)
    old_revision = roster.client.get(f"/api/admin/requests/{applicant.user_id}").json()[
        "claim_revision"
    ]
    for missing, code in (
        ({"confirm_student_match": False}, "student_match_confirmation_required"),
        ({"claim_revision": None}, "request_revision_required"),
    ):
        response = approve_new(roster, applicant, **missing)
        assert response.status_code == 422 and response.json()["error"]["code"] == code
    with TestClient(roster.app, client=("192.0.2.51", 51001)) as owner:
        sign_in(owner, applicant.login, roster.password)
        assert (
            owner.put(
                "/api/auth/student-claim",
                json={
                    "student_claim_name": "Жаңартылған Өтінім Иесі",
                    "student_claim_number": "UPDATED-2026-001",
                    "student_claim_group_id": 2,
                },
            ).status_code
            == 200
        )
    stale = approve_new(roster, applicant, claim_revision=old_revision)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "request_changed"
    assert len(official_students(roster)) == 12
    assert_pending(roster, applicant)
    assert approve_new(roster, applicant).status_code == 200
    assert official_students(roster)[-1]["display_name"] == "Жаңартылған Өтінім Иесі"
    legacy = legacy_student_request(roster, login="legacy_no_student_claim")
    missing_claim = approve_new(roster, legacy)
    assert (
        missing_claim.status_code == 422
        and missing_claim.json()["error"]["code"] == "student_claim_required"
    )
    assert_pending(roster, legacy)


def test_reusing_an_already_linked_student_never_moves_it_to_the_new_account(roster):
    legacy = legacy_student_request(roster, login="legacy_linked_account")
    assert manual_approval(roster, legacy, 1).status_code == 200
    existing = official_students(roster)[0]
    applicant = register(
        roster,
        student_claim_name=existing["display_name"],
        student_claim_number="SYN-001",
        student_claim_group_id=1,
    )
    response = approve_new(roster, applicant, confirmed_group_id=1)
    assert (
        response.status_code == 409 and response.json()["error"]["code"] == "student_already_linked"
    )
    assert_pending(roster, applicant)
    with roster.store.connect() as connection:
        assert (
            connection.execute(
                "SELECT student_id FROM Users WHERE user_id=?", (legacy.user_id,)
            ).fetchone()[0]
            == 1
        )
    assert len(official_students(roster)) == 12


def test_nonadmins_and_signup_payload_cannot_create_or_activate_official_students(roster):
    applicant = register(roster)
    with TestClient(roster.app, client=("192.0.2.51", 51001)) as owner:
        sign_in(owner, applicant.login, roster.password)
        response = approve_new(roster, applicant, client=owner)
        assert response.status_code == 403
    for extra in (
        {"create_student": True},
        {"confirmed_group_id": 2},
        {"student_id": 13},
        {"role_code": "student"},
        {"active": True},
        {"approval_status": "approved"},
        {"student_claim_group_id": 2**63},
    ):
        response = roster.client.post(
            "/api/auth/register",
            json={
                "login": "signup_cannot_create",
                "password": roster.password,
                "display_name": "Applicant account",
                "requested_role": "student",
                "student_claim_name": "Another New Student",
                "student_claim_number": "OTHER-2026-002",
                "student_claim_group_id": 2,
                **extra,
            },
        )
        assert response.status_code == 422, extra
    assert len(official_students(roster)) == 12
    assert_pending(roster, applicant)


def test_concurrent_new_roster_approvals_create_only_one_record_for_the_same_normalized_name(
    roster,
):
    first = register(roster)
    second = register(
        roster,
        login="roster_second",
        student_claim_name="АСЫЛ ЖАҢАБАЕВА",
        student_claim_number="NEW-2026-002",
    )
    with ExitStack() as stack:
        admins = [
            stack.enter_context(
                TestClient(
                    roster.app, client=("192.0.2.50", 52000 + i), raise_server_exceptions=False
                )
            )
            for i in range(2)
        ]
        for client in admins:
            sign_in(client, "roster_admin", roster.password)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(approve_new, roster, applicant, client)
                for client, applicant in zip(admins, (first, second), strict=True)
            ]
            responses = [future.result(timeout=30) for future in futures]
    assert sorted(response.status_code for response in responses) == [200, 409]
    blocked = next(response for response in responses if response.status_code == 409)
    assert blocked.json()["error"]["code"] == "student_selection_required"
    assert len(official_students(roster)) == 13
    with roster.store.connect() as connection:
        users = connection.execute(
            "SELECT approval_status,student_id FROM Users WHERE user_id IN (?,?)",
            (first.user_id, second.user_id),
        ).fetchall()
    assert sum(row["approval_status"] == "approved" for row in users) == 1
    assert sum(row["student_id"] is not None for row in users) == 1


@pytest.mark.parametrize("failing_action", ["student_create", "user_approve", "student_link"])
def test_audit_failure_rolls_back_new_official_student_account_activation_and_link(
    roster, failing_action
):
    applicant = register(roster)
    before = official_students(roster)
    with roster.store.connect(write=True) as connection:
        # The action comes from this closed test parameter list, never an input payload.
        connection.execute(
            "CREATE TRIGGER fail_roster_approval BEFORE INSERT ON AuditLogs "
            f"WHEN NEW.action='{failing_action}' "
            "BEGIN SELECT RAISE(ABORT,'synthetic audit failure'); END"
        )
    response = approve_new(roster, applicant)
    assert response.status_code == 500
    assert official_students(roster) == before
    assert_pending(roster, applicant)
    with roster.store.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM AuditLogs "
                "WHERE action IN ('student_create','user_approve','student_link')"
            ).fetchone()[0]
            == 0
        )


def test_official_creation_audit_is_private_searchable_localized_and_seed_preserves_new_record(
    roster,
):
    applicant = register(roster, student_claim_email="roster.person@campus.example")
    response = approve_new(roster, applicant)
    assert response.status_code == 200
    student_id = response.json()["user"]["student_id"]
    with roster.store.connect() as connection:
        event = dict(
            connection.execute("SELECT * FROM AuditLogs WHERE action='student_create'").fetchone()
        )
        user_before = dict(
            connection.execute(
                "SELECT * FROM Users WHERE user_id=?", (applicant.user_id,)
            ).fetchone()
        )
        student_before = dict(
            connection.execute(
                "SELECT * FROM Students WHERE student_id=?", (student_id,)
            ).fetchone()
        )
        all_audits = json.dumps(
            [dict(row) for row in connection.execute("SELECT * FROM AuditLogs")],
            ensure_ascii=False,
        )
    assert event["user_id"] == "roster_admin" and event["role"] == "admin"
    assert event["occurred_at"].endswith("Z") and event["ip_address"] == "192.0.2.50"
    assert event["object_id"] == str(student_id) and event["result"] == "success"
    metadata = json.loads(event["new_value"])
    assert metadata["name_provided"] is True and metadata["group_id"] == 2
    assert metadata["linked_user_id"] == applicant.user_id
    for private in (
        "Асыл Жаңабаева",
        "NEW-2026-001",
        "roster.person@campus.example",
        roster.password,
    ):
        assert private not in all_audits
    records, truncated = AuditRepository(roster.store.path).search(
        SearchFilters(action=Action.STUDENT_CREATE),
        max_candidates=100,
    )
    assert not truncated and [record.audit_id for record in records] == [event["audit_id"]]
    plan = SummaryPlan(claims=[SummaryClaim(kind="event", audit_ids=[event["audit_id"]])])
    for locale in ("kk", "ru", "en"):
        item = render_summary(plan, records, locale)[0]
        assert item.audit_ids == [event["audit_id"]] and "student_create" not in item.text
    for _ in range(2):
        roster.store.initialize()
        roster.store.seed_academics()
    with roster.store.connect() as connection:
        assert (
            dict(
                connection.execute(
                    "SELECT * FROM Users WHERE user_id=?", (applicant.user_id,)
                ).fetchone()
            )
            == user_before
        )
        assert (
            dict(
                connection.execute(
                    "SELECT * FROM Students WHERE student_id=?", (student_id,)
                ).fetchone()
            )
            == student_before
        )
        assert connection.execute("SELECT COUNT(*) FROM Students").fetchone()[0] == 13
