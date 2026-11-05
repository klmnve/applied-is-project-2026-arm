"""Transactional application storage; upgrade retains every legacy audit row."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.config import Settings

if TYPE_CHECKING:
    from app.auth import Principal

PERMISSIONS = {
    "Dashboard.View": "Басқару панелін қарау",
    "Users.Manage": "Пайдаланушыларды басқару",
    "Roles.Manage": "Рөлдер мен құқықтарды басқару",
    "Grades.View": "Ведомостарды қарау",
    "Grades.Edit": "Бекітілген бағаларды өзгерту",
    "Documents.View": "Құжаттарды қарау",
    "Documents.Upload": "Құжат жүктеу",
    "Documents.Delete": "Құжатты жою",
    "Sessions.Manage": "Сессияларды басқару",
    "Audit.View": "Аудит журналын қарау",
    "Learning.View": "Тапсырмалар мен оқу жұмыстарын қарау",
    "Learning.Manage": "Тапсырмалар мен жұмыстарды бағалау",
    "Learning.Submit": "Өз оқу жұмысын тапсыру",
}
ROLES = {
    "admin": ("Әкімші", set(PERMISSIONS) - {"Learning.Submit"}),
    "teacher": (
        "Оқытушы",
        {
            "Dashboard.View",
            "Grades.View",
            "Grades.Edit",
            "Documents.View",
            "Documents.Upload",
            "Learning.View",
            "Learning.Manage",
        },
    ),
    "head": (
        "Кафедра меңгерушісі",
        {"Dashboard.View", "Grades.View", "Documents.View", "Audit.View", "Learning.View"},
    ),
    "auditor": ("Аудитор", {"Dashboard.View", "Audit.View"}),
    "pending": ("Күтілуде", {"Dashboard.View"}),
    "student": ("Студент", {"Dashboard.View", "Learning.View", "Learning.Submit"}),
}
FICTIONAL_STUDENTS = (
    "Айдар Нұртаев",
    "Мәдина Сәрсенова",
    "Ерасыл Айбаров",
    "Аружан Дәулетова",
    "Данияр Қасымбек",
    "Інжу Мейірхан",
    "Санжар Төлегенов",
    "Әсел Нұралы",
    "Мирас Алпысов",
    "Аяулым Бекенова",
    "Нұрислам Жансерік",
    "Томирис Арманова",
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_utc(value: datetime | None = None) -> str:
    current = value if value is not None else utc_now()
    if current.tzinfo is None:
        raise ValueError("UTC offset required")
    return current.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def upgrade(connection: sqlite3.Connection) -> None:
    """Upgrade transactionally, including additive extensions to schema version 3.

    Claims, catalog archives and approval dates are detected by schema presence
    so existing version-3 clients remain compatible. No executescript calls or
    implicit DDL commits are used.
    """
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version < 2:
        existing = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='AuditLogs'"
        ).fetchone()
        if existing:
            connection.execute("ALTER TABLE AuditLogs RENAME TO AuditLogs_legacy_v1")
        connection.execute("""CREATE TABLE AuditLogs (
            audit_id INTEGER PRIMARY KEY, occurred_at TEXT NOT NULL, user_id TEXT NOT NULL,
            role TEXT NOT NULL, action TEXT NOT NULL, object_type TEXT NOT NULL,
            object_id TEXT NOT NULL,
            result TEXT NOT NULL CHECK(result IN ('success','failure','denied')),
            reason TEXT NOT NULL, ip_address TEXT NOT NULL DEFAULT 'unknown',
            old_value TEXT, new_value TEXT, session_id TEXT
        )""")
        if existing:
            connection.execute("""INSERT INTO AuditLogs
                (audit_id,occurred_at,user_id,role,action,object_type,object_id,result,reason)
                SELECT audit_id,occurred_at,user_id,role,action,object_type,object_id,result,reason
                FROM AuditLogs_legacy_v1""")
            connection.execute("DROP TABLE AuditLogs_legacy_v1")
        connection.execute("PRAGMA user_version=2")
    statements = [
        "CREATE INDEX IF NOT EXISTS audit_time_idx ON AuditLogs(occurred_at,audit_id)",
        "CREATE INDEX IF NOT EXISTS audit_actor_idx ON AuditLogs(user_id,occurred_at)",
        """CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON AuditLogs
           BEGIN SELECT RAISE(ABORT,'Audit log is append only'); END""",
        """CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON AuditLogs
           BEGIN SELECT RAISE(ABORT,'Audit log is append only'); END""",
        "CREATE TABLE IF NOT EXISTS Roles(code TEXT PRIMARY KEY,label TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS Permissions(code TEXT PRIMARY KEY,label TEXT NOT NULL)",
        """CREATE TABLE IF NOT EXISTS RolePermissions(
           role_code TEXT NOT NULL REFERENCES Roles(code),
           permission_code TEXT NOT NULL REFERENCES Permissions(code),
           PRIMARY KEY(role_code,permission_code))""",
        """CREATE TABLE IF NOT EXISTS Users(
           user_id INTEGER PRIMARY KEY,login TEXT NOT NULL UNIQUE COLLATE NOCASE,
           display_name TEXT NOT NULL,password_hash TEXT NOT NULL,
           role_code TEXT NOT NULL REFERENCES Roles(code),active INTEGER NOT NULL DEFAULT 0
           CHECK(active IN (0,1)),created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS Sessions(
           session_id TEXT PRIMARY KEY,token_hash TEXT NOT NULL UNIQUE,csrf_hash TEXT NOT NULL,
           user_id INTEGER NOT NULL REFERENCES Users(user_id),created_at TEXT NOT NULL,
           last_seen_at TEXT NOT NULL,expires_at TEXT NOT NULL,initial_ip TEXT NOT NULL,
           last_ip TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'active'
           CHECK(status IN ('active','blocked','revoked','expired')),
           block_reason TEXT NOT NULL DEFAULT '',suspicious_reason TEXT NOT NULL DEFAULT '')""",
        "CREATE INDEX IF NOT EXISTS sessions_user_idx ON Sessions(user_id,status)",
        """CREATE TABLE IF NOT EXISTS LoginAttempts(attempt_id INTEGER PRIMARY KEY,
           login TEXT NOT NULL,ip_address TEXT NOT NULL,occurred_at TEXT NOT NULL,
           success INTEGER NOT NULL CHECK(success IN (0,1)))""",
        "CREATE INDEX IF NOT EXISTS attempts_time_idx "
        "ON LoginAttempts(occurred_at,login,ip_address)",
        "CREATE TABLE IF NOT EXISTS Groups(group_id INTEGER PRIMARY KEY,name TEXT NOT NULL UNIQUE)",
        """CREATE TABLE IF NOT EXISTS Students(student_id INTEGER PRIMARY KEY,
           group_id INTEGER NOT NULL REFERENCES Groups(group_id),display_name TEXT NOT NULL,
           student_code TEXT NOT NULL UNIQUE)""",
        "CREATE TABLE IF NOT EXISTS Subjects("
        "subject_id INTEGER PRIMARY KEY,name TEXT NOT NULL UNIQUE)",
        """CREATE TABLE IF NOT EXISTS Terms(term_id INTEGER PRIMARY KEY,
           name TEXT NOT NULL,academic_year TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS TeachingAssignments(assignment_id INTEGER PRIMARY KEY,
           user_id INTEGER NOT NULL REFERENCES Users(user_id),
           subject_id INTEGER NOT NULL REFERENCES Subjects(subject_id),
           group_id INTEGER NOT NULL REFERENCES Groups(group_id),
           term_id INTEGER NOT NULL REFERENCES Terms(term_id),
           UNIQUE(user_id,subject_id,group_id,term_id))""",
        """CREATE TABLE IF NOT EXISTS Grades(grade_id INTEGER PRIMARY KEY,
           student_id INTEGER NOT NULL REFERENCES Students(student_id),
           subject_id INTEGER NOT NULL REFERENCES Subjects(subject_id),
           term_id INTEGER NOT NULL REFERENCES Terms(term_id),value INTEGER
           CHECK(value IS NULL OR (typeof(value)='integer' AND value BETWEEN 0 AND 100)),
           updated_at TEXT NOT NULL,updated_by INTEGER REFERENCES Users(user_id),
           version INTEGER NOT NULL DEFAULT 0,UNIQUE(student_id,subject_id,term_id))""",
        """CREATE TABLE IF NOT EXISTS GradeHistory(history_id INTEGER PRIMARY KEY,
           grade_id INTEGER NOT NULL REFERENCES Grades(grade_id),old_value INTEGER,
           new_value INTEGER NOT NULL CHECK(new_value BETWEEN 0 AND 100),
           actor_id INTEGER NOT NULL REFERENCES Users(user_id),occurred_at TEXT NOT NULL,
           ip_address TEXT NOT NULL,audit_id INTEGER NOT NULL REFERENCES AuditLogs(audit_id))""",
        """CREATE TABLE IF NOT EXISTS Documents(document_id INTEGER PRIMARY KEY,
           original_name TEXT NOT NULL,stored_name TEXT NOT NULL UNIQUE,mime_type TEXT NOT NULL,
           size_bytes INTEGER NOT NULL CHECK(size_bytes>0),
           subject_id INTEGER NOT NULL REFERENCES Subjects(subject_id),
           group_id INTEGER NOT NULL REFERENCES Groups(group_id),
           term_id INTEGER NOT NULL REFERENCES Terms(term_id),
           uploaded_by INTEGER NOT NULL REFERENCES Users(user_id),created_at TEXT NOT NULL,
           deleted_at TEXT,deleted_by INTEGER REFERENCES Users(user_id))""",
    ]
    for statement in statements:
        connection.execute(statement)
    for table in ("Groups", "Subjects", "Terms"):
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if "active" not in columns:
            connection.execute(
                f"ALTER TABLE {table} ADD COLUMN active INTEGER NOT NULL DEFAULT 1 "
                "CHECK(active IN (0,1))"
            )
    user_columns = {row[1] for row in connection.execute("PRAGMA table_info(Users)")}
    introduce_claim_review = "request_review_required" not in user_columns
    introduce_approval_date = "approved_at" not in user_columns
    for name, declaration in (
        ("requested_role", "TEXT CHECK(requested_role IN ('teacher','student'))"),
        (
            "approval_status",
            "TEXT NOT NULL DEFAULT 'approved' "
            "CHECK(approval_status IN ('pending','approved','rejected'))",
        ),
        ("approval_reason", "TEXT NOT NULL DEFAULT ''"),
        ("approved_at", "TEXT"),
        ("student_id", "INTEGER REFERENCES Students(student_id)"),
        (
            "request_review_required",
            "INTEGER NOT NULL DEFAULT 0 CHECK(request_review_required IN (0,1))",
        ),
    ):
        if name not in user_columns:
            connection.execute(f"ALTER TABLE Users ADD COLUMN {name} {declaration}")
    # SQLite INTEGER affinity coerces numeric strings/floats before CHECK. The
    # intentionally untyped group column keeps its input storage class so the
    # typeof check enforces a true integer as well as the foreign key.
    trim_chars = (
        "char(9,10,11,12,13,28,29,30,31,32,133,160,5760,8192,8193,8194,8195,"
        "8196,8197,8198,8199,8200,8201,8202,8232,8233,8239,8287,12288)"
    )
    controls = "('*['||char(1)||'-'||char(31)||char(127)||'-'||char(159)||']*')"
    email_spaces = (
        "('*['||char(32,160,5760,8192,8193,8194,8195,8196,8197,8198,8199,"
        "8200,8201,8202,8232,8233,8239,8287,12288)||']*')"
    )
    connection.execute(f"""CREATE TABLE IF NOT EXISTS StudentClaims(
        user_id INTEGER PRIMARY KEY REFERENCES Users(user_id),
        student_claim_name TEXT NOT NULL CHECK(
            typeof(student_claim_name)='text'
            AND length(student_claim_name) BETWEEN 2 AND 120
            AND student_claim_name=trim(student_claim_name,{trim_chars})
            AND instr(student_claim_name,char(0))=0
            AND student_claim_name NOT GLOB {controls}),
        student_claim_number TEXT NOT NULL COLLATE NOCASE UNIQUE CHECK(
            typeof(student_claim_number)='text'
            AND length(student_claim_number) BETWEEN 3 AND 32
            AND student_claim_number=upper(student_claim_number) COLLATE BINARY
            AND student_claim_number GLOB '[A-Z0-9]*'
            AND student_claim_number NOT GLOB '*[^A-Z0-9_-]*'
            AND instr(student_claim_number,char(0))=0),
        student_claim_group_id NOT NULL REFERENCES Groups(group_id) CHECK(
            typeof(student_claim_group_id)='integer' AND student_claim_group_id>0),
        student_claim_email TEXT CHECK(student_claim_email IS NULL OR (
            typeof(student_claim_email)='text'
            AND length(student_claim_email) BETWEEN 3 AND 254
            AND student_claim_email=trim(student_claim_email,{trim_chars})
            AND instr(student_claim_email,char(0))=0
            AND student_claim_email LIKE '_%@_%'
            AND instr(substr(student_claim_email,instr(student_claim_email,'@')+1),'@')=0
            AND student_claim_email NOT GLOB {controls}
            AND student_claim_email NOT GLOB {email_spaces}))
    )""")
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS users_student_idx ON Users(student_id) "
        "WHERE student_id IS NOT NULL"
    )
    session_columns = {row[1] for row in connection.execute("PRAGMA table_info(Sessions)")}
    for name, declaration in (
        ("review_status", "TEXT NOT NULL DEFAULT 'none'"),
        ("review_source", "TEXT NOT NULL DEFAULT 'rule'"),
        ("review_audit_ids", "TEXT NOT NULL DEFAULT '[]'"),
    ):
        if name not in session_columns:
            connection.execute(f"ALTER TABLE Sessions ADD COLUMN {name} {declaration}")
    for code, label in PERMISSIONS.items():
        connection.execute(
            "INSERT OR IGNORE INTO Permissions(code,label) VALUES (?,?)", (code, label)
        )
    for code, (label, permissions) in ROLES.items():
        inserted = connection.execute(
            "INSERT OR IGNORE INTO Roles(code,label) VALUES (?,?)", (code, label)
        )
        if inserted.rowcount:
            connection.executemany(
                "INSERT INTO RolePermissions(role_code,permission_code) VALUES (?,?)",
                [(code, permission) for permission in sorted(permissions)],
            )
    if version < 3:
        connection.execute(
            "UPDATE Users SET requested_role=CASE WHEN role_code='student' THEN 'student' "
            "WHEN role_code IN ('teacher','pending') THEN 'teacher' ELSE NULL END, "
            "approval_status=CASE WHEN role_code='pending' THEN 'pending' ELSE 'approved' END, "
            "active=CASE WHEN role_code='pending' THEN 0 ELSE active END"
        )
        for role_code, permissions in (
            ("admin", {"Learning.View", "Learning.Manage"}),
            ("teacher", {"Learning.View", "Learning.Manage"}),
            ("head", {"Learning.View"}),
        ):
            connection.executemany(
                "INSERT OR IGNORE INTO RolePermissions(role_code,permission_code) VALUES (?,?)",
                [(role_code, permission) for permission in sorted(permissions)],
            )
        flagged_sessions = connection.execute(
            "SELECT session_id FROM Sessions WHERE suspicious_reason<>'' AND status='active'"
        ).fetchall()
        for row in flagged_sessions:
            audit_ids = [
                item[0]
                for item in connection.execute(
                    "SELECT audit_id FROM AuditLogs WHERE session_id=? "
                    "AND action='session_flagged' "
                    "ORDER BY audit_id",
                    (row[0],),
                )
            ]
            connection.execute(
                "UPDATE Sessions SET review_status='awaiting_admin',review_source='rule',"
                "review_audit_ids=? WHERE session_id=?",
                (json.dumps(audit_ids), row[0]),
            )
        connection.execute("PRAGMA user_version=3")
    if introduce_claim_review:
        # Existing unverified requests need deliberate review. Approved/linked
        # accounts and every original field remain unchanged; never invent claims.
        connection.execute(
            "UPDATE Users SET request_review_required=1 "
            "WHERE requested_role='student' AND approval_status IN ('pending','rejected') "
            "AND student_id IS NULL AND NOT EXISTS "
            "(SELECT 1 FROM StudentClaims c WHERE c.user_id=Users.user_id)"
        )
    if introduce_approval_date:
        # An old account's creation date is not proof of approval. Copy only an
        # existing successful approval event for this exact target account;
        # imported/bootstrap accounts without such evidence retain NULL.
        connection.execute(
            "UPDATE Users SET approved_at=(SELECT a.occurred_at FROM AuditLogs a "
            "WHERE a.action='user_approve' AND a.object_type='user' "
            "AND a.object_id=CAST(Users.user_id AS TEXT) AND a.result='success' "
            "ORDER BY a.occurred_at DESC,a.audit_id DESC LIMIT 1) "
            "WHERE approval_status='approved' AND approved_at IS NULL"
        )
    connection.execute(
        "CREATE VIEW IF NOT EXISTS ActiveTeachingAssignments AS SELECT a.* "
        "FROM TeachingAssignments a JOIN Users u ON u.user_id=a.user_id "
        "JOIN Groups g ON g.group_id=a.group_id "
        "JOIN Subjects s ON s.subject_id=a.subject_id "
        "JOIN Terms t ON t.term_id=a.term_id "
        "WHERE u.active=1 AND u.approval_status='approved' AND u.role_code='teacher' "
        "AND g.active=1 AND s.active=1 AND t.active=1"
    )
    from app.learning import ensure_schema

    ensure_schema(connection)


class Store:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = settings.database_path

    @contextmanager
    def connect(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect(write=True) as connection:
            upgrade(connection)

    def seed_academics(self) -> None:
        with self.connect(write=True) as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO Groups(group_id,name) VALUES (?,?)",
                [(1, "CS-101"), (2, "CS-102"), (3, "IS-201")],
            )
            connection.executemany(
                "INSERT OR IGNORE INTO Subjects(subject_id,name) VALUES (?,?)",
                [(1, "Ақпараттық қауіпсіздік"), (2, "Дерекқорлар"), (3, "Веб-қосымшалар")],
            )
            connection.executemany(
                "INSERT OR IGNORE INTO Terms(term_id,name,academic_year) VALUES (?,?,?)",
                [(1, "Күзгі семестр", "2026–2027"), (2, "Көктемгі семестр", "2026–2027")],
            )
            for student_id in range(1, 13):
                connection.execute(
                    """INSERT OR IGNORE INTO Students
                    (student_id,group_id,display_name,student_code) VALUES (?,?,?,?)""",
                    (
                        student_id,
                        (student_id - 1) // 4 + 1,
                        FICTIONAL_STUDENTS[student_id - 1],
                        f"SYN-{student_id:03}",
                    ),
                )
                connection.execute(
                    "UPDATE Students SET display_name=? WHERE student_id=? AND student_code=? "
                    "AND display_name=?",
                    (
                        FICTIONAL_STUDENTS[student_id - 1],
                        student_id,
                        f"SYN-{student_id:03}",
                        f"Синтетикалық студент {student_id:02}",
                    ),
                )
                for subject_id in range(1, 4):
                    for term_id in (1, 2):
                        connection.execute(
                            """INSERT OR IGNORE INTO Grades
                            (student_id,subject_id,term_id,value,updated_at) VALUES (?,?,?,?,?)""",
                            (
                                student_id,
                                subject_id,
                                term_id,
                                60 + (student_id * 3 + subject_id) % 36,
                                iso_utc(),
                            ),
                        )

    def audit(
        self,
        connection: sqlite3.Connection,
        actor: "Principal | None",
        action: str,
        object_type: str,
        object_id: str,
        result: str,
        reason: str,
        ip: str,
        session_id: str | None = None,
        old_value: object = None,
        new_value: object = None,
    ) -> int:
        # Callers choose only business metadata; never accept password/token/cookie fields.
        def value_json(value: object) -> str | None:
            return (
                json.dumps(value, ensure_ascii=False, sort_keys=True) if value is not None else None
            )

        cursor = connection.execute(
            """INSERT INTO AuditLogs
            (occurred_at,user_id,role,action,object_type,object_id,result,reason,ip_address,
             old_value,new_value,session_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                iso_utc(),
                actor.login if actor else "anonymous",
                actor.role_snapshot if actor else "anonymous",
                action,
                object_type,
                str(object_id),
                result,
                reason,
                ip,
                value_json(old_value),
                value_json(new_value),
                session_id if session_id is not None else (actor.session_id if actor else None),
            ),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def audit_row(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["actor_login"] = result["user_id"]
        for field in ("old_value", "new_value"):
            if result[field] is not None:
                result[field] = json.loads(result[field])
        return result
