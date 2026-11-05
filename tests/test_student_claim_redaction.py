"""Replacement markers are never rescanned as applicant data."""

import sqlite3

from app.student_claims import safe_review_reason


def test_cross_claim_redaction_is_one_pass_bounded_and_preserves_plain_explanation():
    with sqlite3.connect(":memory:") as connection:
        connection.execute(
            "CREATE TABLE StudentClaims(student_claim_number TEXT,student_claim_email TEXT,"
            "student_claim_name TEXT)"
        )
        connection.execute("CREATE TABLE Students(student_code TEXT)")
        connection.executemany(
            "INSERT INTO StudentClaims VALUES (?,?,?)",
            [
                ("CLAIM-A001", "first@campus.test", "же"),
                ("CLAIM-B002", "second@campus.test", "де"),
                ("CLAIM-C003", None, "жа"),
                ("CLAIM-D004", None, "сы"),
            ],
        )
        connection.execute("INSERT INTO Students VALUES ('LEGACY-009')")
        text = safe_review_reason(
            connection,
            1,
            "Check CLAIM-B002 and second@campus.test with LEGACY-009; же де жа сы.",
        )
        assert text.startswith("Check ") and " and " in text and " with " in text
        assert text.count("[жеке дерек жасырылды]") == 7
        assert "CLAIM-B002" not in text and "second@campus.test" not in text
        assert "LEGACY-009" not in text and len(text) <= 300
        assert len(safe_review_reason(connection, 1, "же " * 100)) <= 300
