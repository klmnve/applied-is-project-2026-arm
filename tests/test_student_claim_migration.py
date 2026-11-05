"""Additive claim migration and database trust-boundary constraints."""

import sqlite3

import pytest

from app.config import Settings
from app.store import Store, iso_utc


def insert_claim(connection, **overrides):
    values = {
        "user_id": 1,
        "student_claim_name": "Ada Lovelace",
        "student_claim_number": "CLAIM-001",
        "student_claim_group_id": 1,
        "student_claim_email": None,
    }
    values.update(overrides)
    connection.execute(
        "INSERT INTO StudentClaims(user_id,student_claim_name,student_claim_number,"
        "student_claim_group_id,student_claim_email) VALUES (?,?,?,?,?)",
        tuple(values.values()),
    )


@pytest.fixture
def claim_store(tmp_path):
    store = Store(Settings(_env_file=None, database_path=tmp_path / "claims.sqlite3"))
    store.initialize()
    store.seed_academics()
    with store.connect(write=True) as connection:
        connection.executemany(
            "INSERT INTO Users(user_id,login,display_name,password_hash,role_code,active,"
            "created_at,requested_role,approval_status) "
            "VALUES (?,?,?,'unusable','pending',0,?,'student','pending')",
            [
                (1, "claim_one", "First account", iso_utc()),
                (2, "claim_two", "Second account", iso_utc()),
            ],
        )
    return store


def test_fresh_claim_schema_seeds_no_accounts_and_does_not_autolink(tmp_path):
    store = Store(Settings(_env_file=None, database_path=tmp_path / "fresh.sqlite3"))
    store.initialize()
    store.seed_academics()
    store.initialize()
    store.seed_academics()
    with store.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM Users").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM StudentClaims").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM Students").fetchone()[0] == 12
        assert connection.execute("SELECT COUNT(*) FROM Grades").fetchone()[0] == 72


def test_claim_is_unverified_even_when_name_number_and_group_match_existing_student(claim_store):
    with claim_store.connect(write=True) as connection:
        student = dict(connection.execute("SELECT * FROM Students WHERE student_id=1").fetchone())
        insert_claim(
            connection,
            student_claim_name=student["display_name"],
            student_claim_number=student["student_code"],
            student_claim_group_id=student["group_id"],
        )
    claim_store.initialize()
    claim_store.seed_academics()
    with claim_store.connect() as connection:
        user = connection.execute("SELECT * FROM Users WHERE user_id=1").fetchone()
        assert user["student_id"] is None
        assert (user["role_code"], user["active"], user["approval_status"]) == (
            "pending",
            0,
            "pending",
        )
        assert (
            dict(connection.execute("SELECT * FROM Students WHERE student_id=1").fetchone())
            == student
        )
        assert connection.execute("SELECT COUNT(*) FROM StudentClaims").fetchone()[0] == 1


@pytest.mark.parametrize(
    "name", [None, "", "A", " Ada", "Ada ", "\u00a0Ada", "Ada\u3000", "A" * 121]
)
def test_claim_name_requires_trimmed_bounded_text(claim_store, name):
    with pytest.raises(sqlite3.IntegrityError), claim_store.connect(write=True) as connection:
        insert_claim(connection, student_claim_name=name)


def test_claim_name_rejects_embedded_nul_and_all_c0_c1_controls(claim_store):
    for codepoint in [*range(32), *range(127, 160)]:
        with pytest.raises(sqlite3.IntegrityError), claim_store.connect(write=True) as connection:
            insert_claim(connection, student_claim_name="Ada" + chr(codepoint) + "Lovelace")


@pytest.mark.parametrize(
    "number",
    [
        None,
        "",
        "AB",
        "A" * 33,
        " ABC",
        "ABC ",
        "abc",
        "AbC",
        "_ABC",
        "-ABC",
        "AB.C",
        "AB/C",
        "AB C",
        "АВС",
        "ＡＢＣ",
        "ABC\x00TAIL",
        "ABC\n",
    ],
)
def test_claim_number_must_already_be_canonical_ascii(claim_store, number):
    with pytest.raises(sqlite3.IntegrityError), claim_store.connect(write=True) as connection:
        insert_claim(connection, student_claim_number=number)


@pytest.mark.parametrize("number", ["SYN-001", "STU_2026_001", "0A1", "A" * 32])
def test_claim_number_accepts_existing_syn_codes_and_valid_boundaries(claim_store, number):
    with claim_store.connect(write=True) as connection:
        insert_claim(connection, student_claim_number=number)
    with claim_store.connect() as connection:
        assert (
            connection.execute("SELECT student_claim_number FROM StudentClaims").fetchone()[0]
            == number
        )


@pytest.mark.parametrize("group_id", [None, "1", 1.0, 1.5, 0, -1, 9999, "wrong", b"1"])
def test_claim_group_is_a_real_stored_integer_and_existing_foreign_key(claim_store, group_id):
    with pytest.raises(sqlite3.IntegrityError), claim_store.connect(write=True) as connection:
        insert_claim(connection, student_claim_group_id=group_id)


@pytest.mark.parametrize(
    "email",
    [
        "",
        " a@college.test",
        "a@college.test ",
        "no-at",
        "@college.test",
        "a@",
        "a@@college.test",
        "a b@college.test",
        "a@\u00a0college.test",
        "a\x01@college.test",
        "a\x00@college.test",
        "a" * 250 + "@xx.y",
    ],
)
def test_claim_email_is_optional_but_bounded_trimmed_and_not_control_text(claim_store, email):
    with pytest.raises(sqlite3.IntegrityError), claim_store.connect(write=True) as connection:
        insert_claim(connection, student_claim_email=email)


@pytest.mark.parametrize("email", [None, "a@b", "student@college.example", "a" * 250 + "@x.y"])
def test_claim_email_accepts_null_and_valid_length_boundaries(claim_store, email):
    with claim_store.connect(write=True) as connection:
        insert_claim(connection, student_claim_email=email)
    with claim_store.connect() as connection:
        assert (
            connection.execute("SELECT student_claim_email FROM StudentClaims").fetchone()[0]
            == email
        )


def test_number_is_globally_reserved_after_rejection_and_ownership_is_unique(claim_store):
    with claim_store.connect(write=True) as connection:
        insert_claim(connection)
        connection.execute("UPDATE Users SET approval_status='rejected' WHERE user_id=1")
    with pytest.raises(sqlite3.IntegrityError), claim_store.connect(write=True) as connection:
        insert_claim(connection, user_id=2)
    with pytest.raises(sqlite3.IntegrityError), claim_store.connect(write=True) as connection:
        insert_claim(connection, student_claim_number="DIFFERENT-002")
    with pytest.raises(sqlite3.IntegrityError), claim_store.connect(write=True) as connection:
        insert_claim(connection, user_id=9999, student_claim_number="DIFFERENT-002")
    with claim_store.connect() as connection:
        indexes = connection.execute("PRAGMA index_list(StudentClaims)").fetchall()
        assert any(
            row[2]
            and any(
                part[2] == "student_claim_number" and part[4] == "NOCASE"
                for part in connection.execute(f'PRAGMA index_xinfo("{row[1]}")')
            )
            for row in indexes
        )
        assert connection.execute("SELECT COUNT(*) FROM StudentClaims").fetchone()[0] == 1


def test_claim_constraints_also_apply_to_updates_and_review_flag(claim_store):
    with claim_store.connect(write=True) as connection:
        insert_claim(connection)
    for field, value in (
        ("student_claim_name", " New name "),
        ("student_claim_number", "lowercase"),
        ("student_claim_group_id", "1"),
        ("student_claim_email", "not-email"),
    ):
        with pytest.raises(sqlite3.IntegrityError), claim_store.connect(write=True) as connection:
            # Field names are the fixed test cases above; only values are parameters.
            connection.execute(f"UPDATE StudentClaims SET {field}=? WHERE user_id=1", (value,))
    for value in (None, -1, 2, "invalid"):
        with pytest.raises(sqlite3.IntegrityError), claim_store.connect(write=True) as connection:
            connection.execute(
                "UPDATE Users SET request_review_required=? WHERE user_id=1", (value,)
            )


def legacy_database(path, version):
    """An actual pre-claim schema, without using today's migration to build it."""
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE Roles(code TEXT PRIMARY KEY,label TEXT NOT NULL)")
        connection.executemany(
            "INSERT INTO Roles VALUES (?,?)",
            [(code, "Custom " + code) for code in ("admin", "teacher", "student", "pending")],
        )
        connection.execute(
            "CREATE TABLE Groups(group_id INTEGER PRIMARY KEY,name TEXT NOT NULL UNIQUE)"
        )
        connection.execute("INSERT INTO Groups VALUES (1,'Custom old group')")
        connection.execute(
            "CREATE TABLE Students(student_id INTEGER PRIMARY KEY,group_id INTEGER NOT NULL "
            "REFERENCES Groups(group_id),display_name TEXT NOT NULL,"
            "student_code TEXT NOT NULL UNIQUE)"
        )
        connection.executemany(
            "INSERT INTO Students VALUES (?,1,?,?)",
            [(1, "Edited existing student", "SYN-001"), (9, "Edited other student", "OLD-900")],
        )
        connection.execute(
            "CREATE TABLE Subjects(subject_id INTEGER PRIMARY KEY,name TEXT NOT NULL UNIQUE)"
        )
        connection.execute("INSERT INTO Subjects VALUES (1,'Custom old subject')")
        connection.execute(
            "CREATE TABLE Terms(term_id INTEGER PRIMARY KEY,name TEXT NOT NULL,"
            "academic_year TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO Terms VALUES (1,'Custom old term','2025–2026')")
        connection.execute(
            "CREATE TABLE Users(user_id INTEGER PRIMARY KEY,login TEXT UNIQUE,display_name TEXT,"
            "password_hash TEXT,role_code TEXT,active INTEGER,created_at TEXT"
            + (
                ",requested_role TEXT,approval_status TEXT NOT NULL DEFAULT 'approved',"
                "approval_reason TEXT NOT NULL DEFAULT '',"
                "student_id INTEGER REFERENCES Students(student_id)"
                if version == 3
                else ""
            )
            + ")"
        )
        timestamp = "2026-10-14T00:00:00.000000Z"
        if version == 3:
            accounts = [
                (
                    1,
                    "approved_link",
                    "Approved account",
                    "preserved_hash",
                    "student",
                    1,
                    timestamp,
                    "student",
                    "approved",
                    "",
                    1,
                ),
                (
                    2,
                    "pending_student",
                    "Pending student",
                    "preserved_hash",
                    "pending",
                    0,
                    timestamp,
                    "student",
                    "pending",
                    "Old pending note",
                    None,
                ),
                (
                    3,
                    "rejected_student",
                    "Rejected student",
                    "preserved_hash",
                    "pending",
                    0,
                    timestamp,
                    "student",
                    "rejected",
                    "Old rejection",
                    None,
                ),
                (
                    4,
                    "pending_teacher",
                    "Pending teacher",
                    "preserved_hash",
                    "pending",
                    0,
                    timestamp,
                    "teacher",
                    "pending",
                    "Old teacher note",
                    None,
                ),
                (
                    5,
                    "inactive_teacher",
                    "Inactive teacher",
                    "preserved_hash",
                    "teacher",
                    0,
                    timestamp,
                    "teacher",
                    "approved",
                    "",
                    None,
                ),
                (
                    6,
                    "approved_unlinked",
                    "Approved unlinked",
                    "preserved_hash",
                    "student",
                    1,
                    timestamp,
                    "student",
                    "approved",
                    "",
                    None,
                ),
                (
                    7,
                    "pending_linked",
                    "Pending linked",
                    "preserved_hash",
                    "pending",
                    0,
                    timestamp,
                    "student",
                    "pending",
                    "Legacy inconsistent link",
                    9,
                ),
            ]
        else:
            accounts = [
                (1, "old_admin", "Old admin", "preserved_hash", "admin", 1, timestamp),
                (2, "old_pending", "Old pending", "preserved_hash", "pending", 0, timestamp),
                (3, "old_teacher", "Old teacher", "preserved_hash", "teacher", 0, timestamp),
            ]
        connection.executemany(
            "INSERT INTO Users VALUES (" + ",".join("?" for _ in accounts[0]) + ")", accounts
        )
        connection.execute(
            "CREATE TABLE Grades(grade_id INTEGER PRIMARY KEY,student_id INTEGER NOT NULL "
            "REFERENCES Students(student_id),subject_id INTEGER NOT NULL REFERENCES "
            "Subjects(subject_id),term_id INTEGER NOT NULL REFERENCES Terms(term_id),"
            "value INTEGER,updated_at TEXT NOT NULL,updated_by INTEGER REFERENCES Users(user_id),"
            "version INTEGER NOT NULL DEFAULT 0,UNIQUE(student_id,subject_id,term_id))"
        )
        connection.execute("INSERT INTO Grades VALUES (42,1,1,1,77,?,NULL,4)", (timestamp,))
        connection.execute(
            "CREATE TABLE AuditLogs(audit_id INTEGER PRIMARY KEY,occurred_at TEXT,user_id TEXT,"
            "role TEXT,action TEXT,object_type TEXT,object_id TEXT,result TEXT,reason TEXT,"
            "ip_address TEXT,old_value TEXT,new_value TEXT,session_id TEXT)"
        )
        connection.execute(
            "INSERT INTO AuditLogs VALUES (71,?,'old_teacher','teacher','record_update',"
            "'student','1','success',?,'127.0.0.1',?,?,NULL)",
            (timestamp, "Keep this audit unchanged", '{"value":1}', '{"value":2}'),
        )
        connection.execute(f"PRAGMA user_version={version}")
        return {
            "users": [dict(row) for row in connection.execute("SELECT * FROM Users")],
            "students": [dict(row) for row in connection.execute("SELECT * FROM Students")],
            "audit": dict(connection.execute("SELECT * FROM AuditLogs").fetchone()),
            "grade": dict(connection.execute("SELECT * FROM Grades").fetchone()),
        }


@pytest.mark.parametrize("version", [2, 3])
def test_additive_upgrade_preserves_legacy_rows_codes_links_and_seed_edits(tmp_path, version):
    path = tmp_path / f"legacy-{version}.sqlite3"
    original = legacy_database(path, version)
    store = Store(Settings(_env_file=None, database_path=path))
    store.initialize()
    store.initialize()
    store.seed_academics()
    store.seed_academics()
    store.initialize()
    with store.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM Users").fetchone()[0] == len(
            original["users"]
        )
        assert connection.execute("SELECT COUNT(*) FROM StudentClaims").fetchone()[0] == 0
        for prior in original["users"]:
            current = connection.execute(
                "SELECT * FROM Users WHERE user_id=?", (prior["user_id"],)
            ).fetchone()
            assert {key: current[key] for key in prior} == prior
            expected_review = int(version == 3 and prior["user_id"] in (2, 3))
            assert current["request_review_required"] == expected_review
        for prior in original["students"]:
            assert (
                dict(
                    connection.execute(
                        "SELECT * FROM Students WHERE student_id=?", (prior["student_id"],)
                    ).fetchone()
                )
                == prior
            )
        assert (
            dict(connection.execute("SELECT * FROM AuditLogs WHERE audit_id=71").fetchone())
            == original["audit"]
        )
        assert (
            dict(connection.execute("SELECT * FROM Grades WHERE grade_id=42").fetchone())
            == original["grade"]
        )
        assert (
            connection.execute("SELECT name FROM Groups WHERE group_id=1").fetchone()[0]
            == "Custom old group"
        )


def test_legacy_review_backfill_runs_once_and_never_rewrites_existing_claims(tmp_path):
    path = tmp_path / "once.sqlite3"
    legacy_database(path, 3)
    store = Store(Settings(_env_file=None, database_path=path))
    store.initialize()
    with store.connect(write=True) as connection:
        connection.execute("UPDATE Users SET request_review_required=0 WHERE user_id=2")
        insert_claim(connection, user_id=3, student_claim_number="OLD-CLAIM-003")
        claim = dict(connection.execute("SELECT * FROM StudentClaims WHERE user_id=3").fetchone())
    store.initialize()
    store.seed_academics()
    store.initialize()
    with store.connect() as connection:
        assert (
            connection.execute(
                "SELECT request_review_required FROM Users WHERE user_id=2"
            ).fetchone()[0]
            == 0
        )
        assert (
            dict(connection.execute("SELECT * FROM StudentClaims WHERE user_id=3").fetchone())
            == claim
        )


def test_additive_schema_and_backfill_roll_back_together_on_later_migration_failure(
    tmp_path, monkeypatch
):
    from app import learning

    path = tmp_path / "atomic.sqlite3"
    original = legacy_database(path, 3)
    store = Store(Settings(_env_file=None, database_path=path))

    def unavailable(connection):
        raise RuntimeError("simulated schema extension failure")

    with monkeypatch.context() as patch:
        patch.setattr(learning, "ensure_schema", unavailable)
        with pytest.raises(RuntimeError, match="simulated"):
            store.initialize()
    with store.connect() as connection:
        assert "request_review_required" not in {
            row[1] for row in connection.execute("PRAGMA table_info(Users)")
        }
        assert (
            connection.execute("SELECT 1 FROM sqlite_master WHERE name='StudentClaims'").fetchone()
            is None
        )
        assert [dict(row) for row in connection.execute("SELECT * FROM Users")] == original["users"]
    store.initialize()
    with store.connect() as connection:
        assert (
            connection.execute(
                "SELECT request_review_required FROM Users WHERE user_id=2"
            ).fetchone()[0]
            == 1
        )
