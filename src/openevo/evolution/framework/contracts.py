"""Shared types and canonical validation for evolution framework contracts."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping
from enum import Enum, StrEnum
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


MAX_CONTRIBUTION_TEXT = 1_048_576
MAX_CONTRACT_JSON_BYTES = 262_144
# One bounded text contribution can expand sixfold under JSON control-character
# escaping. Renderer payloads must still be able to carry that contribution whole.
MAX_RENDERER_PAYLOAD_BYTES = 6 * MAX_CONTRIBUTION_TEXT + 1_024
MAX_HANDLER_ARTIFACTS = 128
MAX_HANDLER_CONTRIBUTIONS = 256
MAX_PAYLOAD_ENTRIES = 256
MAX_PAYLOAD_ENTRY_BYTES = 8 * 1024 * 1024 * 1024
MAX_PAYLOAD_TOTAL_BYTES = 16 * 1024 * 1024 * 1024
MAX_PAYLOAD_TREE_DEPTH = 32
MAX_JAVASCRIPT_SAFE_INTEGER = (1 << 53) - 1
_MAX_CONTRACT_JSON_DEPTH = 16
_MAX_CONTRACT_JSON_NODES = 8192
_MAX_CONTRACT_JSON_COLLECTION_ITEMS = 4096

_STABLE_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}\Z", re.ASCII)
_URI_SCHEME_RE = re.compile(r"[a-z][a-z0-9+.-]{0,31}\Z", re.ASCII)
_MIME_RE = re.compile(
    r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*\Z",
    re.ASCII,
)
_ENV_RE = re.compile(r"OPENEVO_[A-Z][A-Z0-9_]{0,126}\Z", re.ASCII)
_CORE_URI_SCHEMES = frozenset({"file", "hf", "https", "s3"})
_DISTRIBUTION_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z", re.ASCII)
_VERSION_RE = re.compile(
    r"[A-Za-z0-9]+(?:[.!+-][A-Za-z0-9]+)*\Z",
    re.ASCII,
)
_CONTRACT_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z", re.ASCII)


class DescriptorKind(StrEnum):
    TARGET = "target"
    METHOD = "method"
    TARGET_HANDLER = "target_handler"


class Exposure(StrEnum):
    DESKTOP = "desktop"
    MAINTAINER = "maintainer"
    INTERNAL = "internal"


class Maturity(StrEnum):
    STABLE = "stable"
    EXPERIMENTAL = "experimental"


class RendererKind(StrEnum):
    MARKDOWN = "markdown"
    FILE_BUNDLE = "file_bundle"
    STRUCTURED_SUMMARY = "structured_summary"
    ADAPTER = "adapter"


class ExecutionMode(StrEnum):
    SUBSCRIPTION = "subscription"
    SELF_DEPLOYED = "self_deployed"


class CaptureMode(StrEnum):
    TRANSCRIPT = "transcript"
    TOKEN_LEVEL = "token_level"


class MethodInvocationABI(StrEnum):
    LEGACY_WORKER_JOB_V1 = "legacy_worker_job_v1"
    METHOD_CONTEXT_V1 = "method_context_v1"


class ProjectConfigInjectionSource(StrEnum):
    REFLECTOR_LLM = "reflector_llm"
    AGENT_MODEL = "agent_model"


class DestinationScope(StrEnum):
    TARGET_DATA = "target_data"
    HARNESS_SKILLS = "harness_skills"
    HARNESS_INSTRUCTION = "harness_instruction"


class PayloadKind(StrEnum):
    FILE = "file"
    DIRECTORY = "directory"


class EnvironmentValueKind(StrEnum):
    PATH = "path"
    DIRECTORY = "directory"
    JSON_PATHS = "json_paths"
    SCOPE_ROOT = "scope_root"


class ContributionKind(StrEnum):
    INSTRUCTION = "instruction"
    STAGED_PAYLOAD = "staged_payload"
    ADAPTER = "adapter"
    ENVIRONMENT = "environment"


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


def _text(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be empty")
    return value


def _identity_text(value: str, *, max_length: int) -> str:
    if (
        not value
        or len(value) > max_length
        or value != value.strip()
        or unicodedata.normalize("NFC", value) != value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or any(0xD800 <= ord(character) <= 0xDFFF for character in value)
    ):
        raise ValueError("must be normalized bounded identity text")
    return value


def _distribution_name(value: str) -> str:
    normalized = _identity_text(value, max_length=128)
    if _DISTRIBUTION_RE.fullmatch(normalized) is None:
        raise ValueError("must be a canonical distribution name")
    return normalized


def _distribution_version(value: str) -> str:
    normalized = _identity_text(value, max_length=128)
    if _VERSION_RE.fullmatch(normalized) is None:
        raise ValueError("must be a normalized distribution version")
    return normalized


def _contract_version(value: str) -> str:
    normalized = _identity_text(value, max_length=64)
    if _CONTRACT_VERSION_RE.fullmatch(normalized) is None:
        raise ValueError("must be a normalized contract version")
    return normalized


def _stable_id(value: str) -> str:
    if _STABLE_ID_RE.fullmatch(value) is None:
        raise ValueError("must be a stable identifier")
    return value


def _optional_stable_id(value: str | None) -> str | None:
    return None if value is None else _stable_id(value)


def _digest(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("must be a lowercase SHA-256 digest")
    return value


def _unique_strings(values: tuple[str, ...]) -> tuple[str, ...]:
    if any(not value.strip() for value in values):
        raise ValueError("values must not be empty")
    if len(values) != len(set(values)):
        raise ValueError("values must be unique")
    return tuple(sorted(values))


def _unique_ids(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized = _unique_strings(values)
    for value in normalized:
        _stable_id(value)
    return normalized


def _ordered_unique_ids(values: tuple[str, ...]) -> tuple[str, ...]:
    if len(values) != len(set(values)):
        raise ValueError("values must be unique")
    for value in values:
        _stable_id(value)
    return values


def _enum_tuple(values: tuple[Any, ...]) -> tuple[Any, ...]:
    if len(values) != len(set(values)):
        raise ValueError("values must be unique")
    return tuple(sorted(values, key=lambda value: str(value.value)))


def _validated_string(value: str, path: str) -> str:
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError(f"Unicode surrogate is forbidden at {path}")
    return value


def _json_value(value: Any, path: str = "$") -> Any:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, str):
        return _validated_string(value, path)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite number at {path}")
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError(f"JSON object keys must be strings at {path}")
        return {
            _validated_string(key, f"{path}.<key>"): _json_value(
                item, f"{path}.{key}"
            )
            for key, item in sorted(value.items())
        }
    if isinstance(value, (list, tuple)):
        return [
            _json_value(item, f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise TypeError(f"unsupported non-JSON value at {path}: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Serialize deterministic finite JSON for identity and plan storage."""

    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _bounded_canonical_json_object(
    value: Any,
    *,
    label: str,
    max_bytes: int = MAX_CONTRACT_JSON_BYTES,
) -> str:
    """Validate one bounded JSON object before recursive canonicalization."""

    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a JSON object")
    stack: list[tuple[Any, int]] = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        if isinstance(current, BaseModel):
            current = current.model_dump(mode="json")
        if isinstance(current, Enum):
            current = current.value
        nodes += 1
        if nodes > _MAX_CONTRACT_JSON_NODES:
            raise ValueError(f"{label} exceeds the JSON node budget")
        if depth > _MAX_CONTRACT_JSON_DEPTH:
            raise ValueError(f"{label} exceeds the JSON depth budget")
        if isinstance(current, Mapping):
            if len(current) > _MAX_CONTRACT_JSON_COLLECTION_ITEMS:
                raise ValueError(f"{label} exceeds the JSON collection budget")
            if not all(isinstance(key, str) for key in current):
                raise TypeError(f"{label} object keys must be strings")
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, (list, tuple)):
            if len(current) > _MAX_CONTRACT_JSON_COLLECTION_ITEMS:
                raise ValueError(f"{label} exceeds the JSON collection budget")
            stack.extend((item, depth + 1) for item in current)
        elif isinstance(current, str):
            _validated_string(current, label)
        elif current is None or isinstance(current, (bool, int)):
            continue
        elif isinstance(current, float):
            if not math.isfinite(current):
                raise ValueError(f"{label} contains a non-finite number")
        else:
            raise TypeError(
                f"{label} contains unsupported JSON value {type(current).__name__}"
            )

    encoded = canonical_json(value)
    if len(encoded.encode("utf-8")) > max_bytes:
        raise ValueError(f"{label} exceeds the JSON byte budget")
    return encoded


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def validate_relative_path(value: str) -> str:
    """Validate one normalized, control-free POSIX path relative to a safe root."""

    if (
        not value
        or value == "/"
        or len(value) > 4096
        or value != value.strip()
        or unicodedata.normalize("NFC", value) != value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or "\\" in value
        or value.startswith("/")
        or "//" in value
    ):
        raise ValueError("must be a normalized POSIX relative path")
    path = PurePosixPath(value)
    if (
        value == "."
        or (path.parts and ":" in path.parts[0])
        or any(part in {"", ".", ".."} for part in path.parts)
        or str(path) != value
    ):
        raise ValueError("must be a normalized POSIX relative path")
    return value


def validate_payload_source_path(value: str) -> str:
    """Validate an artifact-relative source path; ``.`` denotes its payload root."""

    return value if value == "." else validate_relative_path(value)


def validate_absolute_runtime_path(value: str) -> str:
    """Validate one normalized absolute POSIX path inside the agent runtime."""

    if (
        not value
        or value == "/"
        or len(value) > 4096
        or value != value.strip()
        or unicodedata.normalize("NFC", value) != value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or "\\" in value
        or not value.startswith("/")
        or "//" in value
    ):
        raise ValueError("must be a normalized absolute runtime path")
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts[1:]) or str(path) != value:
        raise ValueError("must be a normalized absolute runtime path")
    return value


def paths_conflict(left: str, right: str) -> bool:
    """Return whether normalized paths are equal or one is the other's ancestor."""

    left_parts = tuple(part.casefold() for part in PurePosixPath(left).parts)
    right_parts = tuple(part.casefold() for part in PurePosixPath(right).parts)
    shorter = min(len(left_parts), len(right_parts))
    return left_parts[:shorter] == right_parts[:shorter]


def _uri_scheme(value: str) -> str:
    if _URI_SCHEME_RE.fullmatch(value) is None or value not in _CORE_URI_SCHEMES:
        raise ValueError("unsupported or malformed URI scheme")
    return value


def _mime_type(value: str) -> str:
    if _MIME_RE.fullmatch(value) is None:
        raise ValueError("must be a normalized MIME type")
    return value


def _environment_name(value: str) -> str:
    if _ENV_RE.fullmatch(value) is None:
        raise ValueError("must be a reserved OPENEVO_* environment name")
    return value


class EvolutionExecutionProfile(_Contract):
    execution_mode: ExecutionMode
    capture_mode: CaptureMode
    harness_id: str
    harness_capabilities: tuple[str, ...] = Field(default=(), max_length=256)
    runtime_capabilities: tuple[str, ...] = Field(default=(), max_length=256)

    _harness = field_validator("harness_id")(_stable_id)
    _capabilities = field_validator(
        "harness_capabilities", "runtime_capabilities"
    )(_unique_ids)

    @model_validator(mode="after")
    def _subscription_capture(self) -> EvolutionExecutionProfile:
        if (
            self.execution_mode is ExecutionMode.SUBSCRIPTION
            and self.capture_mode is not CaptureMode.TRANSCRIPT
        ):
            raise ValueError("subscription execution requires transcript capture")
        return self


class ImplementationRef(_Contract):
    distribution: str
    distribution_version: str
    distribution_digest: str
    entry_point: str
    contract_version: str = "1"

    _distribution = field_validator("distribution")(_distribution_name)
    _distribution_version = field_validator("distribution_version")(
        _distribution_version
    )
    _entry_point = field_validator("entry_point")(
        lambda value: _identity_text(value, max_length=512)
    )
    _contract = field_validator("contract_version")(_contract_version)
    _sha = field_validator("distribution_digest")(_digest)


class ImplementationIdentity(_Contract):
    descriptor_kind: DescriptorKind
    descriptor_id: str
    descriptor_digest: str
    implementation: ImplementationRef

    _id = field_validator("descriptor_id")(_stable_id)
    _sha = field_validator("descriptor_digest")(_digest)


__all__ = [
    "CaptureMode",
    "ContributionKind",
    "DescriptorKind",
    "DestinationScope",
    "EnvironmentValueKind",
    "EvolutionExecutionProfile",
    "ExecutionMode",
    "Exposure",
    "ImplementationIdentity",
    "ImplementationRef",
    "MAX_CONTRIBUTION_TEXT",
    "MAX_CONTRACT_JSON_BYTES",
    "MAX_HANDLER_ARTIFACTS",
    "MAX_HANDLER_CONTRIBUTIONS",
    "MAX_PAYLOAD_ENTRIES",
    "MAX_PAYLOAD_ENTRY_BYTES",
    "MAX_PAYLOAD_TOTAL_BYTES",
    "MAX_PAYLOAD_TREE_DEPTH",
    "MAX_RENDERER_PAYLOAD_BYTES",
    "Maturity",
    "MethodInvocationABI",
    "PayloadKind",
    "RendererKind",
    "canonical_digest",
    "canonical_json",
    "paths_conflict",
    "validate_relative_path",
    "validate_payload_source_path",
    "validate_absolute_runtime_path",
]
