"""Reduce accidental sensitive values in reason; integration still needs a data policy."""

import re

_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6 = re.compile(r"(?<!\w)(?:[0-9a-fA-F]{0,4}:){2,}[0-9a-fA-F:]{0,39}(?!\w)")
_SECRET = re.compile(
    r"(?i)\b(password|passwd|token|secret|api[_ -]?key|old_value|new_value|"
    r"пароль|құпиясөз)\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s;,]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s;,]+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def safe_reason(reason: str) -> str:
    text = _CONTROL.sub("", reason)
    text = _IPV4.sub("[IP жасырылды]", text)
    text = _IPV6.sub("[IP жасырылды]", text)
    text = _SECRET.sub(lambda match: match.group(1) + "=[мән жасырылды]", text)
    text = _BEARER.sub("Bearer [мән жасырылды]", text)
    return text[:1500]
