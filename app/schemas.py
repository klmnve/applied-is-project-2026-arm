"""The entire, closed JSON contract for interpreting audit-search questions."""

from datetime import UTC, datetime
from enum import StrEnum
from ipaddress import ip_address
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Action(StrEnum):
    LOGIN = "login"
    LOGOUT = "logout"
    PASSWORD_CHANGE = "password_change"
    RECORD_VIEW = "record_view"
    RECORD_UPDATE = "record_update"
    EXPORT = "export"
    GRADE_CHANGE = "grade_change"
    ASSIGNMENT_CHANGE = "assignment_change"
    CATALOG_CREATE = "catalog_create"
    CATALOG_UPDATE = "catalog_update"
    CATALOG_ARCHIVE = "catalog_archive"
    DOCUMENT_UPLOAD = "document_upload"
    DOCUMENT_VIEW = "document_view"
    DOCUMENT_DELETE = "document_delete"
    USER_REGISTER = "user_register"
    USER_CHANGE = "user_change"
    ROLE_CHANGE = "role_change"
    PERMISSION_CHANGE = "permission_change"
    SESSION_BLOCK = "session_block"
    SESSION_UNBLOCK = "session_unblock"
    SESSION_TERMINATE = "session_terminate"
    SESSION_FLAGGED = "session_flagged"
    SESSION_EXPIRED = "session_expired"
    ACCESS_DENIED = "access_denied"
    ADMIN_BOOTSTRAP = "admin_bootstrap"
    USER_APPROVE = "user_approve"
    USER_REJECT = "user_reject"
    USER_NEEDS_REVIEW = "user_needs_review"
    STUDENT_CLAIM_UPDATE = "student_claim_update"
    STUDENT_CREATE = "student_create"
    STUDENT_LINK = "student_link"
    TASK_CREATE = "task_create"
    TASK_UPDATE = "task_update"
    TASK_MATERIAL_UPLOAD = "task_material_upload"
    TASK_MATERIAL_VIEW = "task_material_view"
    SUBMISSION_SUBMIT = "submission_submit"
    SUBMISSION_RESUBMIT = "submission_resubmit"
    SUBMISSION_VIEW = "submission_view"
    SUBMISSION_FILE_VIEW = "submission_file_view"
    SUBMISSION_REVIEW = "submission_review"
    SESSION_DISMISS = "session_dismiss"
    SESSION_AI_REVIEW = "session_ai_review"


class Result(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"


AuditAction = Action
AuditResult = Result
DemoLogin = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{2,31}$")]
AuditId = Annotated[int, Field(strict=True, ge=1)]


def utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Time must include a UTC offset, for example 2026-10-14T00:00:00Z")
    return value.astimezone(UTC)


class SearchFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_at: datetime | None = None
    end_at: datetime | None = None
    action: Action | None = None
    actions: list[Action] | None = Field(default=None, min_length=1, max_length=20)
    result: Result | None = None
    user_login: DemoLogin | None = None
    ip_address: str | None = Field(default=None, max_length=45)
    object_type: str | None = Field(default=None, pattern=r"^[a-z][a-z_]{0,39}$")
    object_id: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.:-]+$"
    )
    semantic_query: str | None = Field(default=None, min_length=1, max_length=500)
    limit: int = Field(default=20, ge=1, le=50, strict=True)

    @field_validator("semantic_query", mode="before")
    @classmethod
    def trim_semantic_query(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("start_at", "end_at", mode="before")
    @classmethod
    def reject_numeric_timestamps(cls, value: object) -> object:
        if value is not None and not isinstance(value, (str, datetime)):
            raise ValueError("Time must be an ISO 8601 timestamp with an explicit UTC offset")
        if isinstance(value, str):
            # A before-validator changes strict JSON date handling in Pydantic.
            # Parse the ISO string explicitly, then let the UTC validator reject naive dates.
            return datetime.fromisoformat(value)
        return value

    @field_validator("start_at", "end_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        return utc_datetime(value) if value is not None else None

    @model_validator(mode="after")
    def ordered_interval(self) -> Self:
        if self.start_at is not None and self.end_at is not None and self.start_at >= self.end_at:
            raise ValueError("start_at must precede end_at; the interval is [start_at, end_at)")
        if self.action is not None and self.actions is not None:
            raise ValueError("Use action or actions, not both")
        if self.actions is not None and len(set(self.actions)) != len(self.actions):
            raise ValueError("actions must be unique")
        return self

    @field_validator("ip_address")
    @classmethod
    def canonical_ip(cls, value: str | None) -> str | None:
        if value is not None:
            if "%" in value:
                raise ValueError("IP zones are not supported")
            return str(ip_address(value))
        return None


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=1000)

    @field_validator("question", mode="before")
    @classmethod
    def trim_question(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class AuditRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audit_id: AuditId
    occurred_at: datetime
    user_id: str
    role: str
    action: Action
    object_type: str
    object_id: str
    result: Result
    reason: str
    score: float | None = Field(default=None, ge=-1, le=1, allow_inf_nan=False)

    @field_validator("occurred_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return utc_datetime(value)


class SummaryClaim(BaseModel):
    """A model selects records and claim types; it never supplies rendered prose."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["count", "event", "reason"]
    audit_ids: list[AuditId] = Field(min_length=1, max_length=50)

    @field_validator("audit_ids")
    @classmethod
    def unique_ids(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)):
            raise ValueError("audit_ids must be unique")
        return value


class SummaryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claims: list[SummaryClaim] = Field(default_factory=list, max_length=20)
    insufficient_data: bool = Field(default=True, strict=True)


class SummaryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    kind: Literal["count", "event", "reason"] = "event"
    audit_ids: list[AuditId] = Field(min_length=1, max_length=50)


class SearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "no_results"]
    filters: SearchFilters
    records: list[AuditRecord]
    # Exact metadata is displayed only to Audit.View users and never enters the model.
    evidence: list["AuditEvidence"] = Field(default_factory=list)
    summary: list[SummaryItem]
    insufficient_data: bool
    candidate_count: int = Field(ge=0)
    truncated: bool
    semantic_threshold: float | None = Field(default=None, ge=-1, le=1)


class AuditEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    audit_id: AuditId
    ip_address: str
