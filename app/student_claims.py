"""Unverified applicant identity, private projections and explicit staff matching."""

import hashlib
import json
import re
import sqlite3
import unicodedata
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import Settings
from app.errors import AppError
from app.safety import safe_reason

_NUMBER = re.compile(r"[A-Z0-9][A-Z0-9_-]{2,31}", re.ASCII)
_DOMAIN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.ASCII)
_LOCAL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}", re.ASCII)


def valid_domain(value: str) -> bool:
    return (
        len(value) <= 253
        and "." in value
        and all(_DOMAIN.fullmatch(label) for label in value.split("."))
    )


def normalized_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


class StudentClaimInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    student_claim_name: str = Field(min_length=2, max_length=120)
    student_claim_number: str = Field(min_length=3, max_length=32)
    student_claim_group_id: int = Field(strict=True, ge=1, le=2**63 - 1)
    student_claim_email: str | None = Field(default=None, max_length=254)

    @field_validator("student_claim_name", mode="before")
    @classmethod
    def clean_name(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        if any(unicodedata.category(character).startswith("C") for character in value):
            raise ValueError("Invalid name")
        return " ".join(value.strip().split())

    @field_validator("student_claim_number", mode="before")
    @classmethod
    def clean_number(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        raw = value.strip()
        number = raw.upper()
        if not raw.isascii() or not _NUMBER.fullmatch(number):
            raise ValueError("Invalid student number")
        return number

    @field_validator("student_claim_email", mode="before")
    @classmethod
    def clean_email(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        email = value.strip()
        if not email:
            return None
        if email.count("@") != 1:
            raise ValueError("Invalid email")
        local, domain = email.rsplit("@", 1)
        domain = domain.lower()
        if (
            not _LOCAL.fullmatch(local)
            or local.startswith(".")
            or local.endswith(".")
            or ".." in local
            or not valid_domain(domain)
        ):
            raise ValueError("Invalid email")
        return local + "@" + domain


def validate_claim(
    connection: sqlite3.Connection, claim: StudentClaimInput, settings: Settings
) -> None:
    if (
        connection.execute(
            "SELECT 1 FROM Groups WHERE group_id=? AND active=1", (claim.student_claim_group_id,)
        ).fetchone()
        is None
    ):
        raise AppError("invalid_student_claim_group", "Оқу тобын тізімнен таңдаңыз.", 422)
    if settings.student_email_required and claim.student_claim_email is None:
        raise AppError("student_email_required", "Оқу орнының email мекенжайын енгізіңіз.", 422)
    if (
        claim.student_claim_email is not None
        and settings.student_email_domain
        and claim.student_claim_email.rsplit("@", 1)[1] != settings.student_email_domain
    ):
        raise AppError(
            "student_email_domain", "Оқу орнының рұқсат етілген email-ін енгізіңіз.", 422
        )


def write_claim(connection: sqlite3.Connection, user_id: int, claim: StudentClaimInput) -> None:
    connection.execute(
        "INSERT INTO StudentClaims(user_id,student_claim_name,student_claim_number,"
        "student_claim_group_id,student_claim_email) VALUES (?,?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET student_claim_name=excluded.student_claim_name,"
        "student_claim_number=excluded.student_claim_number,"
        "student_claim_group_id=excluded.student_claim_group_id,"
        "student_claim_email=excluded.student_claim_email",
        (
            user_id,
            claim.student_claim_name,
            claim.student_claim_number,
            claim.student_claim_group_id,
            claim.student_claim_email,
        ),
    )


def private_claim(connection: sqlite3.Connection, user_id: int) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT c.*,g.name AS group_name FROM StudentClaims c "
        "JOIN Groups g ON g.group_id=c.student_claim_group_id WHERE c.user_id=?",
        (user_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "name": row["student_claim_name"],
        "number": row["student_claim_number"],
        "group_id": row["student_claim_group_id"],
        "group_name": row["group_name"],
        "email": row["student_claim_email"],
    }


def masked_claim(connection: sqlite3.Connection, user_id: int) -> dict[str, Any] | None:
    claim = private_claim(connection, user_id)
    if claim is None:
        return None
    return {
        "name": claim["name"],
        "number_masked": "••••" + claim["number"][-2:],
        "group_id": claim["group_id"],
        "group_name": claim["group_name"],
        "has_claim": True,
    }


def claim_revision(connection: sqlite3.Connection, user_id: int) -> str:
    """Bind the admin dialog to the private claim it actually displayed."""
    encoded = json.dumps(private_claim(connection, user_id), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def match_claim(claim: dict[str, Any] | None, student: sqlite3.Row) -> dict[str, bool]:
    if claim is None:
        return {"name": False, "number": False, "group": False}
    return {
        "name": normalized_name(claim["name"]) == normalized_name(student["display_name"]),
        "number": claim["number"] == student["student_code"].strip().upper(),
        "group": claim["group_id"] == student["group_id"],
    }


def safe_review_reason(connection: sqlite3.Connection, user_id: int, reason: str) -> str:
    """A decision explanation may be copied into general audit; remove private claim values."""
    del user_id
    text = safe_reason(reason)
    # Staff can mention another applicant when explaining duplicate identities.
    # Such a reason must not publish that person's number/email to the applicant
    # or global audit either. Longest-first prevents partial replacements.
    values = {
        value
        for row in connection.execute(
            "SELECT student_claim_number,student_claim_email,student_claim_name FROM StudentClaims"
        )
        for value in row
        if value
    }
    values.update(row[0] for row in connection.execute("SELECT student_code FROM Students"))
    if values:
        pattern = re.compile(
            "|".join(re.escape(value) for value in sorted(values, key=len, reverse=True)), re.I
        )
        text = pattern.sub("[жеке дерек жасырылды]", text)
    return text[:300]
