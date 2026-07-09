from __future__ import annotations

import re
from collections.abc import Mapping

_URL_USERINFO_RE = re.compile(
    r"(?P<prefix>\b[A-Za-z][A-Za-z0-9+.-]*://)(?P<userinfo>[^\s/@]+@)"
)
_URL_QUERY_RE = re.compile(
    r"(?P<prefix>\b[A-Za-z][A-Za-z0-9+.-]*://[^\s?#]+)\?(?P<query>[^\s#]+)"
    r"(?P<fragment>#[^\s]*)?"
)
_SENSITIVE_ENV_KEY_PARTS = (
    "AUTH",
    "CREDENTIAL",
    "INDEX_URL",
    "KEY",
    "PASS",
    "PASSWORD",
    "PROXY",
    "SECRET",
    "TOKEN",
)
_REDACTED = "[REDACTED]"


def sanitize_remote_text(value: str, env: Mapping[str, str]) -> str:
    if not value:
        return ""
    sanitized = value
    for secret in _redaction_values(env):
        sanitized = sanitized.replace(secret, _REDACTED)
    sanitized = _URL_USERINFO_RE.sub(r"\g<prefix>[REDACTED]@", sanitized)
    sanitized = _URL_QUERY_RE.sub(
        lambda match: (
            f"{match.group('prefix')}?<redacted>{match.group('fragment') or ''}"
        ),
        sanitized,
    )
    return sanitized


def _redaction_values(env: Mapping[str, str]) -> list[str]:
    values: list[str] = []
    for key, value in env.items():
        text = value.strip()
        if len(text) < 4:
            continue
        key_upper = key.upper()
        if any(part in key_upper for part in _SENSITIVE_ENV_KEY_PARTS):
            values.append(text)
        elif _URL_USERINFO_RE.search(text):
            values.append(text)
    return sorted(set(values), key=len, reverse=True)
