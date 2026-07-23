#!/usr/bin/env python3
"""Classify bounded stock startup output without retaining raw text."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Iterable, NamedTuple


_POLICY_PATH = Path(__file__).resolve().parents[2] / "desktop/startup-output-classifiers-v1.json"
_CLOSED_POLICY_KEYS = {
    "classifiers",
    "max_line_bytes",
    "pyinstaller_error_prefix",
    "pyinstaller_error_separator",
    "pyinstaller_pid_max_digits",
    "schema_version",
}
_CLOSED_CLASSIFIER_KEYS = {"code", "required_fragments", "stage"}
_SAFE_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")


class StartupClassifierPolicyError(RuntimeError):
    """The checked-in stock-output classification policy is invalid."""


class StartupClassification(NamedTuple):
    stage: str
    code: str


def _load_policy() -> dict[str, object]:
    try:
        payload = json.loads(_POLICY_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StartupClassifierPolicyError("startup classifier policy is unreadable") from exc
    if type(payload) is not dict or set(payload) != _CLOSED_POLICY_KEYS:
        raise StartupClassifierPolicyError("startup classifier policy is not closed")
    classifiers = payload.get("classifiers")
    if (
        payload.get("schema_version") != "1"
        or payload.get("max_line_bytes") != 2048
        or payload.get("pyinstaller_error_prefix") != "[PYI-"
        or payload.get("pyinstaller_error_separator") != ":ERROR] "
        or payload.get("pyinstaller_pid_max_digits") != 10
        or type(classifiers) is not list
        or len(classifiers) != 1
    ):
        raise StartupClassifierPolicyError("startup classifier policy identity is invalid")
    classifier = classifiers[0]
    if type(classifier) is not dict or set(classifier) != _CLOSED_CLASSIFIER_KEYS:
        raise StartupClassifierPolicyError("startup classifier entry is not closed")
    fragments = classifier.get("required_fragments")
    if (
        _SAFE_NAME.fullmatch(str(classifier.get("stage"))) is None
        or _SAFE_NAME.fullmatch(str(classifier.get("code"))) is None
        or type(fragments) is not list
        or len(fragments) != 4
        or any(type(value) is not str or not value.isascii() or not value for value in fragments)
    ):
        raise StartupClassifierPolicyError("startup classifier entry is invalid")
    return payload


_POLICY = _load_policy()
STARTUP_OUTPUT_LINE_MAX_BYTES = int(_POLICY["max_line_bytes"])
_PREFIX = str(_POLICY["pyinstaller_error_prefix"]).encode("ascii")
_SEPARATOR = str(_POLICY["pyinstaller_error_separator"]).encode("ascii")
_PID_MAX_DIGITS = int(_POLICY["pyinstaller_pid_max_digits"])
_CLASSIFIER = _POLICY["classifiers"][0]
_REQUIRED_FRAGMENTS = tuple(
    value.encode("ascii") for value in _CLASSIFIER["required_fragments"]
)
_CLASSIFICATION = StartupClassification(
    stage=str(_CLASSIFIER["stage"]),
    code=str(_CLASSIFIER["code"]),
)


def classify_stock_loader_line(line: bytes) -> StartupClassification | None:
    if not isinstance(line, bytes) or not 0 < len(line) <= STARTUP_OUTPUT_LINE_MAX_BYTES:
        return None
    if not line.startswith(_PREFIX):
        return None
    separator = line.find(_SEPARATOR, len(_PREFIX))
    if separator < 0:
        return None
    pid = line[len(_PREFIX) : separator]
    if not 0 < len(pid) <= _PID_MAX_DIGITS or not pid.isdigit() or pid.startswith(b"0"):
        return None
    cursor = separator + len(_SEPARATOR)
    for fragment in _REQUIRED_FRAGMENTS:
        cursor = line.find(fragment, cursor)
        if cursor < 0:
            return None
        cursor += len(fragment)
    return _CLASSIFICATION


def unknown_output_fingerprint(lines: Iterable[bytes]) -> tuple[int, str] | None:
    digest = hashlib.sha256()
    count = 0
    for line in lines:
        if not isinstance(line, bytes) or len(line) > STARTUP_OUTPUT_LINE_MAX_BYTES:
            raise ValueError("unknown startup output line is invalid")
        count += 1
        digest.update(len(line).to_bytes(8, "big"))
        digest.update(line)
    return (count, digest.hexdigest()) if count else None
