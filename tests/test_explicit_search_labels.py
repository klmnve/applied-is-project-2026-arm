import httpx
import pytest

from app.errors import AppError
from app.llm import OllamaAdapter


@pytest.mark.parametrize(
    "criterion",
    [
        "IP: not-an-ip",
        "IP: localhost",
        "IP:",
        "IP: [broken]",
        "IP: 'unfinished",
        "login: ab",
        "login:",
        "login: 12345",
        "login: not@allowed",
        "login: 'unfinished",
        "object: grade",
        "object: grade/",
        "object: /1",
        "object: grade/1/2",
        "action: erase",
        "action:",
        "IP: 192.0.2.1 IP: 192.0.2.2",
        "login: one login: two",
    ],
)
def test_invalid_explicit_criteria_cannot_fall_back_to_a_broader_search(settings, now, criterion):
    adapter = OllamaAdapter(settings, httpx.MockTransport(lambda r: pytest.fail("No model call")))
    with pytest.raises(AppError) as error:
        adapter.parse(
            criterion if criterion.endswith(":") else f"{criterion} failed logins today", now
        )
    assert error.value.code == "invalid_search_filter" and error.value.status_code == 422


@pytest.mark.parametrize(
    ("question", "field", "expected"),
    [
        ("Failed logins IP: 192.0.2.4.", "ip_address", "192.0.2.4"),
        ("IP: [2001:db8::4].", "ip_address", "2001:db8::4"),
        ("IP: '2001:db8::4'", "ip_address", "2001:db8::4"),
        ("login: teacher.one.", "user_login", "teacher.one"),
        ('login: "teacher.one."', "user_login", "teacher.one."),
        ("login: 'failure_today'", "user_login", "failure_today"),
        ("login: actual.teacher compare with demo_admin", "user_login", "actual.teacher"),
        ("object: grade/1.", "object_id", "1"),
        ('object: "grade/1."', "object_id", "1."),
        ("action: submission_review.", "action", "submission_review"),
    ],
)
def test_explicit_criteria_handle_sentence_punctuation_and_quoted_identifiers(
    settings, now, question, field, expected
):
    adapter = OllamaAdapter(settings, httpx.MockTransport(lambda r: pytest.fail("No model call")))
    filters = adapter.parse(question, now)
    assert getattr(filters, field) == expected
    if expected == "failure_today":
        assert filters.action is None and filters.result is None and filters.start_at is None
