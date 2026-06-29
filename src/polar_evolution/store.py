from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator
from urllib.parse import urlparse, urlunparse

from polar_evolution.agent_system import normalize_agent_system_target_path
from polar_evolution.context import (
    artifact_manifest,
    artifact_matches,
    artifact_type,
    read_file_uri_text,
    requested_context_artifact_ids,
    sort_candidates,
)
from polar_evolution.files import ArtifactFileStore
from polar_evolution.ids import new_id
from polar_evolution.models import (
    ArtifactPromotionUpdateRequest,
    AdapterMergeSpec,
    ArtifactRegisterRequest,
    ArtifactResponse,
    ArtifactState,
    ArtifactType,
    ContextResolveRequest,
    ContextResolveResponse,
    DatasetCreateRequest,
    DatasetCreateResponse,
    EventIngestRequest,
    EventIngestResponse,
    FeedbackApplicationCreateRequest,
    FeedbackApplicationResponse,
    FeedbackApplicationTargetType,
    HumanFeedbackCreateRequest,
    HumanFeedbackResponse,
    HumanFeedbackStatus,
    HumanQueryDecisionCreateRequest,
    HumanQueryDecisionResponse,
    JobCreateRequest,
    JobCreateResponse,
    JobState,
    ReviewAdjudicationRequest,
    ReviewClaimRequest,
    ReviewPacketResponse,
    ReviewRequestCreateRequest,
    ReviewRequestResponse,
    ReviewStatus,
    WorkerClaimRequest,
    WorkerClaimResponse,
    WorkerCompleteRequest,
    WorkerFailRequest,
    WorkerHeartbeatRequest,
)
from polar_evolution.time import utc_now_iso


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    event_type TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    task_id TEXT,
    session_id TEXT,
    policy_version TEXT,
    rollout_step INTEGER,
    agent_harness TEXT,
    agent_model TEXT,
    base_model TEXT,
    status TEXT,
    reward REAL,
    payload_path TEXT NOT NULL,
    UNIQUE(source, event_type, source_event_id)
);
CREATE TABLE IF NOT EXISTS datasets (
    dataset_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    purpose TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    query_json TEXT NOT NULL,
    manifest_path TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    trace_count INTEGER NOT NULL,
    artifact_id TEXT
);
CREATE TABLE IF NOT EXISTS dataset_events (
    dataset_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    PRIMARY KEY(dataset_id, event_id)
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    method TEXT NOT NULL,
    state TEXT NOT NULL,
    priority INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    claimed_by TEXT,
    lease_id TEXT,
    lease_expires_at TEXT,
    input_artifact_ids_json TEXT NOT NULL,
    config_json TEXT NOT NULL,
    error TEXT,
    attempt_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    uri TEXT NOT NULL,
    manifest_path TEXT NOT NULL,
    lineage_json TEXT NOT NULL,
    compatibility_json TEXT NOT NULL,
    scores_json TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    promoted INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS artifact_lineage (
    parent_artifact_id TEXT NOT NULL,
    child_artifact_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    PRIMARY KEY(parent_artifact_id, child_artifact_id, relation)
);
CREATE TABLE IF NOT EXISTS contexts (
    context_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    request_json TEXT NOT NULL,
    response_json TEXT NOT NULL,
    selected_artifact_ids_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_packets (
    packet_id TEXT PRIMARY KEY,
    packet_hash TEXT NOT NULL UNIQUE,
    packet_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_requests (
    review_id TEXT PRIMARY KEY,
    review_type TEXT NOT NULL,
    status TEXT NOT NULL,
    artifact_ids_json TEXT NOT NULL,
    candidate_ids_json TEXT NOT NULL,
    job_id TEXT,
    task_id TEXT,
    round_index INTEGER,
    method TEXT,
    artifact_type TEXT,
    packet_id TEXT NOT NULL,
    packet_hash TEXT NOT NULL,
    artifact_hashes_json TEXT NOT NULL,
    query_decision_id TEXT,
    assigned_to TEXT,
    reviewer_role TEXT,
    adjudication_rationale TEXT,
    priority INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS human_feedback (
    feedback_id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL,
    reviewer_id TEXT NOT NULL,
    reviewer_role TEXT,
    status TEXT NOT NULL,
    decision TEXT NOT NULL,
    score REAL,
    confidence REAL,
    rationale TEXT NOT NULL,
    raw_payload_json TEXT NOT NULL,
    normalized_payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback_applications (
    application_id TEXT PRIMARY KEY,
    feedback_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    consumed_by_method TEXT NOT NULL,
    consumed_in_job_id TEXT,
    effect_summary TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS human_query_decisions (
    query_decision_id TEXT PRIMARY KEY,
    artifact_ids_json TEXT NOT NULL,
    candidate_ids_json TEXT NOT NULL,
    task_id TEXT,
    round_index INTEGER,
    method TEXT,
    decision TEXT NOT NULL,
    reason_codes_json TEXT NOT NULL,
    estimated_value_of_information REAL,
    estimated_human_cost REAL,
    budget_context_json TEXT NOT NULL,
    actual_latency_seconds REAL,
    feedback_changed_promotion INTEGER,
    feedback_changed_next_candidate INTEGER,
    downstream_delta REAL,
    review_id TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_review_requests_query_decision_id_unique
ON review_requests(query_decision_id)
WHERE query_decision_id IS NOT NULL;
"""

MAX_ARTIFACT_ID_ATTEMPTS = 10
MAX_DATASET_ID_ATTEMPTS = 10
MAX_CONTEXT_ID_ATTEMPTS = 10
DEFAULT_HEARTBEAT_LEASE_SECONDS = 600
ACTIVE_JOB_STATES = {str(JobState.CLAIMED), str(JobState.RUNNING)}


class JobLeaseError(ValueError):
    pass


def _text_metadata(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True)
    except TypeError:
        return str(value)


def _json_dumps(value: Any, *, indent: int | None = None) -> str:
    return json.dumps(value, indent=indent, sort_keys=True, allow_nan=False)


def _canonical_json_hash(payload: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_json_dumps(payload).encode("utf-8")).hexdigest()


def _validate_finite_floats(value: Any, path: str) -> None:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite float at {path}: {value!r}")
        return
    if isinstance(value, dict):
        for key, child in value.items():
            _validate_finite_floats(child, f"{path}.{key}")
        return
    if isinstance(value, list | tuple):
        for index, child in enumerate(value):
            _validate_finite_floats(child, f"{path}[{index}]")


def _utc_dt_to_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_utc_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _write_json_strict_exclusive(
    files: ArtifactFileStore,
    path: Path,
    payload: dict[str, Any],
) -> Path:
    path = path.resolve()
    if files.root != path and files.root not in path.parents:
        raise ValueError(f"path escapes artifact root: {path}")
    serialized = _json_dumps(payload, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(serialized)
    except FileExistsError:
        raise
    except Exception:
        if path.exists():
            path.unlink(missing_ok=True)
        raise
    return path


def _write_jsonl_strict_exclusive(
    files: ArtifactFileStore,
    path: Path,
    records: list[dict[str, Any]],
) -> Path:
    path = path.resolve()
    if files.root != path and files.root not in path.parents:
        raise ValueError(f"path escapes artifact root: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            for record in records:
                handle.write(_json_dumps(record))
                handle.write("\n")
    except FileExistsError:
        raise
    except Exception:
        if path.exists():
            path.unlink(missing_ok=True)
        raise
    return path


def _normalize_feedback_payload(
    request: HumanFeedbackCreateRequest,
    *,
    reviewer_role: str | None = None,
) -> dict[str, Any]:
    normalized: dict[str, Any] = {"decision": request.decision}
    if request.score is not None:
        normalized["score"] = request.score
    if request.confidence is not None:
        normalized["confidence"] = request.confidence
    if request.rationale:
        normalized["rationale"] = request.rationale
    if reviewer_role:
        normalized["reviewer_role"] = reviewer_role
    for field in (
        "observed_issues",
        "suggested_changes",
        "risks",
        "validation_checks",
        "labels",
    ):
        value = getattr(request, field)
        if value:
            normalized[field] = value
    return normalized


_HUMAN_FEEDBACK_DATASET_STATUSES = {
    HumanFeedbackStatus.AVAILABLE_FOR_EVOLUTION.value,
}
_HUMAN_FEEDBACK_LIST_FIELDS = (
    "observed_issues",
    "suggested_changes",
    "risks",
    "validation_checks",
    "labels",
)
_LOCAL_ARTIFACT_URI_LABEL = "[LOCAL_ARTIFACT_URI]"
_LOCAL_ARTIFACT_PATH_LABEL = "[LOCAL_ARTIFACT_PATH]"
_REDACTED_LABEL = "[REDACTED]"
_URI_RE = re.compile(
    r"\b[A-Za-z][A-Za-z0-9+.-]*://(?:<redacted>|[^\s\"'<>])+",
    re.IGNORECASE,
)
_RELATIVE_URI_REF_RE = re.compile(
    r"(?<![\w:/])(?:[A-Za-z0-9._~!$&'()*+,;=@%-]+/)*"
    r"[A-Za-z0-9._~!$&'()*+,;=@%-]+[?#](?:<redacted>|[^\s\"'<>])+"
)
_QUERY_OR_FRAGMENT_REF_RE = re.compile(
    r"(?<![\w])(?:[?#](?:<redacted>|[^\s\"'<>])+)"
)
_POSIX_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w:/])/(?!/)(?:[^\s,;:/]+/)*[^\s,;]+")
_WINDOWS_UNC_PATH_RE = re.compile(
    r"\\\\[^\s\\/:*?\"<>|,;]+\\(?:[^\\/:*?\"<>|\r\n,;]+\\)*[^\s\\/:*?\"<>|,;]+"
)
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(
    r"\b[A-Za-z]:[\\/](?:[^\\/:*?\"<>|\r\n,;]+[\\/])*[^\s\\/:*?\"<>|,;]+"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"\b[A-Za-z0-9_]*(?:"
    r"api[_-]?key|access[_-]?key(?:[_-]?id)?|accesskeyid|token|password|secret|"
    r"authorization"
    r")[A-Za-z0-9_]*\s*[:=]\s*(?:bearer|basic)?\s*[^\s,;]+",
    re.IGNORECASE,
)
_SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|access[_-]?key(?:[_-]?id)?|accesskeyid|token|password|secret|"
    r"authorization)",
    re.IGNORECASE,
)
_AUTHORIZATION_VALUE_RE = re.compile(
    r"\bAuthorization\s*:\s*(?:Bearer|Basic)?\s*[^\s,;]+",
    re.IGNORECASE,
)
_BEARER_VALUE_RE = re.compile(
    r"\bBearer\s+[A-Za-z0-9._~+/=-]+",
    re.IGNORECASE,
)
_SENSITIVE_SCHEME_VALUE_RE = re.compile(
    r"\b(?:bearer|basic)\s*:\s*[^\s,;]+",
    re.IGNORECASE,
)
_AWS_ACCESS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{12,}\b")


def _sanitize_review_boundary_payload(value: Any, *, uri_context: bool = False) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, child in value.items():
            text_key = str(key)
            sanitized_key = _sanitize_review_boundary_key(text_key)
            if not sanitized_key:
                continue
            if _SENSITIVE_KEY_RE.search(text_key):
                sanitized[sanitized_key] = _REDACTED_LABEL
                continue
            sanitized[sanitized_key] = _sanitize_review_boundary_payload(
                child,
                uri_context=uri_context or _is_uri_field_key(text_key),
            )
        return sanitized
    if isinstance(value, list):
        return [
            _sanitize_review_boundary_payload(item, uri_context=uri_context)
            for item in value
        ]
    if isinstance(value, tuple):
        return [
            _sanitize_review_boundary_payload(item, uri_context=uri_context)
            for item in value
        ]
    if isinstance(value, str):
        return _sanitize_review_boundary_text(value, uri_context=uri_context)
    return value


def _sanitize_review_boundary_key(key: str) -> str:
    text = key.strip()
    if not text:
        return ""
    if _looks_like_absolute_local_path(text):
        return _LOCAL_ARTIFACT_PATH_LABEL
    if _looks_like_uri_reference(text):
        return _sanitize_uri_reference(text)
    return _sanitize_review_boundary_text(text)


def _sanitize_review_target_ids(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    sanitized: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        text = _sanitize_review_metadata_text(value)
        if text:
            sanitized.append(text)
    return sanitized


def _sanitize_review_artifact_hashes(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    sanitized: dict[str, str] = {}
    for key, child in value.items():
        if not isinstance(key, str) or not isinstance(child, str):
            continue
        sanitized_key = _sanitize_review_metadata_text(key)
        sanitized_value = _sanitize_review_metadata_text(child)
        if sanitized_key and sanitized_value:
            sanitized[sanitized_key] = sanitized_value
    return sanitized


def _sanitize_review_metadata_text(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    if _looks_like_absolute_local_path(text):
        return _LOCAL_ARTIFACT_PATH_LABEL
    if _has_sensitive_uri_scheme(text):
        return _REDACTED_LABEL
    if _looks_like_uri_reference(text):
        return _sanitize_uri_reference(text)
    return _sanitize_review_boundary_text(text)


def _has_sensitive_uri_scheme(value: str) -> bool:
    parsed = urlparse(value.strip())
    return _is_sensitive_uri_scheme(parsed.scheme)


def _is_sensitive_uri_scheme(scheme: str) -> bool:
    normalized = scheme.lower()
    if not normalized:
        return False
    return normalized in {"bearer", "basic"} or bool(
        _SENSITIVE_KEY_RE.search(normalized)
    )


def _is_uri_field_key(key: object) -> bool:
    text = str(key).strip().lower()
    normalized = re.sub(r"[^a-z0-9]+", "_", text)
    parts = [part for part in normalized.split("_") if part]
    if any(part in {"uri", "uris", "url", "urls", "path", "paths"} for part in parts):
        return True
    return text.endswith(("uri", "uris", "url", "urls", "path", "paths"))


def _sanitize_review_boundary_text(value: str, *, uri_context: bool = False) -> str:
    text = value.strip()
    if not text:
        return ""
    if uri_context and _looks_like_absolute_local_path(text):
        return _LOCAL_ARTIFACT_PATH_LABEL
    if uri_context and _looks_like_uri_reference(text):
        return _sanitize_uri_reference(text)
    text = _URI_RE.sub(_sanitize_uri_match, text)
    text = _RELATIVE_URI_REF_RE.sub(_sanitize_relative_uri_match, text)
    text = _QUERY_OR_FRAGMENT_REF_RE.sub("<redacted>", text)
    text = _POSIX_ABSOLUTE_PATH_RE.sub(_redact_posix_path_match, text)
    text = _WINDOWS_UNC_PATH_RE.sub(_LOCAL_ARTIFACT_PATH_LABEL, text)
    text = _WINDOWS_ABSOLUTE_PATH_RE.sub(_LOCAL_ARTIFACT_PATH_LABEL, text)
    text = _AUTHORIZATION_VALUE_RE.sub(_REDACTED_LABEL, text)
    text = _BEARER_VALUE_RE.sub(_REDACTED_LABEL, text)
    text = _SENSITIVE_SCHEME_VALUE_RE.sub(_REDACTED_LABEL, text)
    text = _SECRET_ASSIGNMENT_RE.sub(_REDACTED_LABEL, text)
    text = _AWS_ACCESS_KEY_RE.sub(_REDACTED_LABEL, text)
    return text.strip()


def _sanitize_uri_match(match: re.Match[str]) -> str:
    return _sanitize_uri_reference(match.group(0).rstrip(".,);]"))


def _sanitize_relative_uri_match(match: re.Match[str]) -> str:
    candidate = match.group(0).rstrip(".,);]")
    if _looks_like_uri_reference(candidate):
        return _sanitize_uri_reference(candidate)
    return match.group(0)


def _sanitize_uri_reference(uri: str) -> str:
    parsed = urlparse(uri)
    if _is_sensitive_uri_scheme(parsed.scheme):
        return _REDACTED_LABEL
    if parsed.scheme == "file":
        return _LOCAL_ARTIFACT_URI_LABEL
    host = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError:
        port = None
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port is not None else host
    query = "<redacted>" if parsed.query or parsed.fragment else ""
    return urlunparse(
        (
            parsed.scheme,
            netloc,
            parsed.path,
            parsed.params,
            query,
            "",
        )
    )


def _looks_like_uri_reference(value: str) -> bool:
    stripped = value.strip()
    if not stripped or any(char.isspace() for char in stripped):
        return False
    parsed = urlparse(stripped)
    return bool(
        (parsed.scheme and (parsed.netloc or parsed.path))
        or parsed.netloc
        or ((parsed.query or parsed.fragment) and parsed.path)
    )


def _looks_like_absolute_local_path(value: str) -> bool:
    stripped = value.strip()
    if not stripped or any(char in stripped for char in "\r\n\x00"):
        return False
    if _WINDOWS_UNC_PATH_RE.fullmatch(stripped) or _WINDOWS_ABSOLUTE_PATH_RE.fullmatch(
        stripped
    ):
        return True
    return stripped.startswith("/")


def _redact_human_feedback_text(value: str) -> str:
    return _sanitize_review_boundary_text(value)


def _redact_posix_path_match(match: re.Match[str]) -> str:
    return _LOCAL_ARTIFACT_PATH_LABEL


def _sanitize_human_feedback_for_dataset(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        items = [value]
    elif isinstance(value, list):
        items = [item for item in value if isinstance(item, dict)]
    else:
        return []

    sanitized_items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in items:
        status = item.get("status")
        if not isinstance(status, str) or status not in _HUMAN_FEEDBACK_DATASET_STATUSES:
            continue
        source = item.get("normalized_payload")
        if not isinstance(source, dict):
            source = item
        sanitized: dict[str, Any] = {}
        feedback_id = item.get("feedback_id") or source.get("feedback_id")
        if isinstance(feedback_id, str) and feedback_id.strip():
            sanitized["feedback_id"] = _redact_human_feedback_text(feedback_id)
        sanitized["status"] = HumanFeedbackStatus.AVAILABLE_FOR_EVOLUTION.value
        decision = source.get("decision")
        if isinstance(decision, str) and decision.strip():
            sanitized["decision"] = _redact_human_feedback_text(decision)
        confidence = source.get("confidence")
        if isinstance(confidence, int | float):
            sanitized["confidence"] = float(confidence)
        score = _bounded_human_feedback_score(source.get("score"))
        if score is None and source is not item:
            score = _bounded_human_feedback_score(item.get("score"))
        if score is not None:
            sanitized["score"] = score
        for field in _HUMAN_FEEDBACK_LIST_FIELDS:
            values = _string_list_for_dataset_feedback(source.get(field))
            if values:
                sanitized[field] = values
        if sanitized:
            dedupe_key = str(sanitized.get("feedback_id") or json.dumps(sanitized, sort_keys=True))
            if dedupe_key in seen_ids:
                continue
            seen_ids.add(dedupe_key)
            sanitized_items.append(sanitized)
    return sanitized_items


def _string_list_for_dataset_feedback(value: Any) -> list[str]:
    if isinstance(value, str):
        text = _redact_human_feedback_text(value)
        return [text] if text else []
    if isinstance(value, list | tuple):
        values: list[str] = []
        for item in value:
            if not isinstance(item, str):
                continue
            text = _redact_human_feedback_text(item)
            if text:
                values.append(text)
        return values
    return []


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, list | tuple):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return []


def _feedback_application_target_type(manifest: dict[str, Any]) -> str:
    value = str(
        manifest.get("human_feedback_application_target_type")
        or manifest.get("feedback_application_target_type")
        or FeedbackApplicationTargetType.PROMPT_SEED.value
    ).strip()
    allowed = {item.value for item in FeedbackApplicationTargetType}
    return value if value in allowed else FeedbackApplicationTargetType.PROMPT_SEED.value


def _feedback_application_effect_summary(
    manifest: dict[str, Any],
    *,
    artifact: ArtifactResponse,
    method: str,
) -> str:
    value = manifest.get("human_feedback_application_summary") or manifest.get(
        "feedback_application_summary"
    )
    if isinstance(value, str) and value.strip():
        return value
    return (
        f"Consumed human feedback while running {method} to produce "
        f"{artifact.type} artifact {artifact.artifact_id}."
    )


def _bounded_human_feedback_score(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    score = float(value)
    if not math.isfinite(score) or score < 0.0 or score > 1.0:
        return None
    return score


def _add_sanitized_human_feedback(
    sanitized: list[dict[str, Any]],
    seen: set[str],
    value: Any,
) -> None:
    for item in _sanitize_human_feedback_for_dataset(value):
        feedback_id = item.get("feedback_id")
        if isinstance(feedback_id, str) and feedback_id:
            existing = next(
                (
                    candidate
                    for candidate in sanitized
                    if candidate.get("feedback_id") == feedback_id
                ),
                None,
            )
            if existing is not None:
                _merge_sanitized_human_feedback(existing, item)
                continue
            seen.add(feedback_id)
            sanitized.append(item)
            continue
        dedupe_key = json.dumps(item, sort_keys=True)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        sanitized.append(item)


def _merge_sanitized_human_feedback(
    target: dict[str, Any],
    source: dict[str, Any],
) -> None:
    target["status"] = HumanFeedbackStatus.AVAILABLE_FOR_EVOLUTION.value
    for field in ("feedback_id", "decision", "confidence", "score"):
        if field not in target and source.get(field) not in (None, "", []):
            target[field] = source[field]
    for field in _HUMAN_FEEDBACK_LIST_FIELDS:
        values = source.get(field)
        if not isinstance(values, list):
            continue
        target_values = target.setdefault(field, [])
        if not isinstance(target_values, list):
            continue
        for value in values:
            if isinstance(value, str) and value not in target_values:
                target_values.append(value)


def _pop_human_feedback_aliases(
    mapping: dict[str, Any],
    sanitized: list[dict[str, Any]],
    seen: set[str],
    *,
    keys: tuple[str, ...],
) -> None:
    for key in keys:
        if key in mapping:
            _add_sanitized_human_feedback(sanitized, seen, mapping.pop(key))


def _sanitize_evolution_feedback_mapping(
    value: Any,
    sanitized: list[dict[str, Any]],
    seen: set[str],
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    _pop_human_feedback_aliases(value, sanitized, seen, keys=("human", "human_feedback"))
    return value


def _sanitize_human_feedback_in_event_payload(event_payload: dict[str, Any]) -> None:
    sanitized: list[dict[str, Any]] = []
    seen: set[str] = set()

    _pop_human_feedback_aliases(event_payload, sanitized, seen, keys=("human", "human_feedback"))
    _sanitize_evolution_feedback_mapping(
        event_payload.get("evolution_feedback"),
        sanitized,
        seen,
    )

    session_result = event_payload.get("session_result")
    if isinstance(session_result, dict):
        _pop_human_feedback_aliases(
            session_result,
            sanitized,
            seen,
            keys=("human", "human_feedback"),
        )
        _sanitize_evolution_feedback_mapping(
            session_result.get("evolution_feedback"),
            sanitized,
            seen,
        )
        metadata = session_result.get("metadata")
        if isinstance(metadata, dict):
            _pop_human_feedback_aliases(
                metadata,
                sanitized,
                seen,
                keys=("human", "human_feedback"),
            )
            _sanitize_evolution_feedback_mapping(
                metadata.get("evolution_feedback"),
                sanitized,
                seen,
            )

    if sanitized:
        if not isinstance(session_result, dict):
            session_result = {}
            event_payload["session_result"] = session_result
        metadata = session_result.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            session_result["metadata"] = metadata
        existing_evolution_feedback = metadata.get("evolution_feedback")
        if isinstance(existing_evolution_feedback, dict):
            evolution_feedback = existing_evolution_feedback
        else:
            evolution_feedback = {}
            if _non_empty_evolution_feedback_value(existing_evolution_feedback):
                evolution_feedback["shared"] = existing_evolution_feedback
            metadata["evolution_feedback"] = evolution_feedback
        evolution_feedback["human"] = sanitized
    else:
        if isinstance(session_result, dict):
            metadata = session_result.get("metadata")
            if isinstance(metadata, dict):
                evolution_feedback = metadata.get("evolution_feedback")
                if isinstance(evolution_feedback, dict):
                    evolution_feedback.pop("human", None)


def _non_empty_evolution_feedback_value(value: Any) -> bool:
    if isinstance(value, dict) or value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list | tuple | set):
        return bool(value)
    return True


_REVIEW_STATUSES = {status.value for status in ReviewStatus}
_ADJUDICATION_TRANSITIONS = {
    ReviewStatus.SUBMITTED.value: {
        ReviewStatus.VALIDATED.value,
        ReviewStatus.ADJUDICATED.value,
        ReviewStatus.NEEDS_REVISION.value,
        ReviewStatus.REJECTED_INVALID.value,
    },
    ReviewStatus.VALIDATED.value: {
        ReviewStatus.ADJUDICATED.value,
        ReviewStatus.CONFLICT.value,
    },
    ReviewStatus.ADJUDICATED.value: {ReviewStatus.ARCHIVED_ONLY.value},
}
_RESOLVABLE_REVIEW_STATUSES = {
    ReviewStatus.SUBMITTED.value,
    ReviewStatus.VALIDATED.value,
    ReviewStatus.ADJUDICATED.value,
    ReviewStatus.NEEDS_REVISION.value,
    ReviewStatus.REJECTED_INVALID.value,
    ReviewStatus.CONFLICT.value,
}
_STALEABLE_REVIEW_STATUSES = {
    ReviewStatus.CREATED.value,
    ReviewStatus.QUEUED.value,
    ReviewStatus.SUBMITTED.value,
}
_CONSUMABLE_FEEDBACK_STATUSES = {
    HumanFeedbackStatus.AVAILABLE_FOR_EVOLUTION.value,
    HumanFeedbackStatus.CONSUMED.value,
}
_ACTIVE_FEEDBACK_STATUSES = (
    HumanFeedbackStatus.SUBMITTED.value,
    HumanFeedbackStatus.VALIDATED.value,
    HumanFeedbackStatus.NORMALIZED.value,
    HumanFeedbackStatus.REDACTED.value,
    HumanFeedbackStatus.INDEXED.value,
    HumanFeedbackStatus.AVAILABLE_FOR_EVOLUTION.value,
)


def _require_review_transition(
    row: sqlite3.Row,
    *,
    review_id: str,
    action: str,
    allowed_statuses: set[str],
) -> str:
    status = str(row["status"])
    if status not in allowed_statuses:
        raise ValueError(f"cannot {action} review {review_id} from status {status}")
    return status


def _archive_active_feedback(
    conn: sqlite3.Connection,
    *,
    review_id: str,
    status: HumanFeedbackStatus,
) -> None:
    conn.execute(
        """
        UPDATE human_feedback
        SET status = ?
        WHERE review_id = ?
          AND status IN (?, ?, ?, ?, ?, ?)
        """,
        (status.value, review_id, *_ACTIVE_FEEDBACK_STATUSES),
    )


def _insert_human_query_decision_row(
    conn: sqlite3.Connection,
    *,
    query_decision_id: str,
    request_payload: dict[str, Any],
    review_id: str | None,
    created_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO human_query_decisions (
            query_decision_id, artifact_ids_json, candidate_ids_json,
            task_id, round_index, method, decision, reason_codes_json,
            estimated_value_of_information, estimated_human_cost,
            budget_context_json, actual_latency_seconds,
            feedback_changed_promotion, feedback_changed_next_candidate,
            downstream_delta, review_id, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            query_decision_id,
            _json_dumps(request_payload["artifact_ids"]),
            _json_dumps(request_payload["candidate_ids"]),
            request_payload["task_id"],
            request_payload["round_index"],
            request_payload["method"],
            request_payload["decision"],
            _json_dumps(request_payload["reason_codes"]),
            request_payload["estimated_value_of_information"],
            request_payload["estimated_human_cost"],
            _json_dumps(request_payload["budget_context"]),
            None,
            None,
            None,
            None,
            review_id,
            created_at,
        ),
    )


class EvolutionStore:
    def __init__(self, *, db_path: str | Path, artifact_root: str | Path) -> None:
        self.db_path = Path(db_path)
        self.files = ArtifactFileStore(artifact_root)

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.files.initialize()
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._ensure_schema(conn)
            conn.commit()

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        review_columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(review_requests)").fetchall()
        }
        if "adjudication_rationale" not in review_columns:
            conn.execute("ALTER TABLE review_requests ADD COLUMN adjudication_rationale TEXT")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_review_requests_query_decision_id_unique
            ON review_requests(query_decision_id)
            WHERE query_decision_id IS NOT NULL
            """
        )
        conn.execute(
            """
            DELETE FROM feedback_applications
            WHERE rowid NOT IN (
                SELECT MIN(rowid)
                FROM feedback_applications
                GROUP BY
                    feedback_id,
                    target_type,
                    target_id,
                    consumed_by_method,
                    COALESCE(consumed_in_job_id, '')
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_applications_natural_key_unique
            ON feedback_applications(
                feedback_id,
                target_type,
                target_id,
                consumed_by_method,
                COALESCE(consumed_in_job_id, '')
            )
            """
        )

    def create_review_request(
        self,
        request: ReviewRequestCreateRequest,
    ) -> ReviewRequestResponse:
        raw_payload = request.model_dump(mode="python")
        _validate_finite_floats(raw_payload, "request")
        request_payload = request.model_dump(mode="json")
        inline_query_decision = request_payload.get("query_decision")
        inline_query_decision_payload: dict[str, Any] | None = None
        if inline_query_decision is not None:
            if request_payload["query_decision_id"] is not None:
                raise ValueError(
                    "review request cannot include both query_decision and query_decision_id"
                )
            inline_request = HumanQueryDecisionCreateRequest.model_validate(
                inline_query_decision
            )
            _validate_finite_floats(
                inline_request.model_dump(mode="python"),
                "query_decision",
            )
            inline_query_decision_payload = inline_request.model_dump(mode="json")
        packet = _sanitize_review_boundary_payload(request_payload["packet"])
        request_payload["packet"] = packet
        packet_hash = _canonical_json_hash(packet)
        packet_json = _json_dumps(packet)
        request_payload["artifact_ids"] = _sanitize_review_target_ids(
            request_payload["artifact_ids"]
        )
        request_payload["candidate_ids"] = _sanitize_review_target_ids(
            request_payload["candidate_ids"]
        )
        request_payload["artifact_hashes"] = _sanitize_review_artifact_hashes(
            request_payload["artifact_hashes"]
        )
        artifact_ids_json = _json_dumps(request_payload["artifact_ids"])
        candidate_ids_json = _json_dumps(request_payload["candidate_ids"])
        artifact_hashes_json = _json_dumps(request_payload["artifact_hashes"])
        review_id = new_id("rev")
        now = utc_now_iso()

        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                if inline_query_decision_payload is not None:
                    query_decision_id = new_id("hqd")
                    _insert_human_query_decision_row(
                        conn,
                        query_decision_id=query_decision_id,
                        request_payload=inline_query_decision_payload,
                        review_id=review_id,
                        created_at=now,
                    )
                    request_payload["query_decision_id"] = query_decision_id
                if (
                    request_payload["query_decision_id"] is not None
                    and inline_query_decision_payload is None
                ):
                    query_row = conn.execute(
                        """
                        SELECT query_decision_id, review_id
                        FROM human_query_decisions
                        WHERE query_decision_id = ?
                        """,
                        (request_payload["query_decision_id"],),
                    ).fetchone()
                    if query_row is None:
                        raise ValueError(
                            f"unknown query decision: {request_payload['query_decision_id']}"
                        )
                    if query_row["review_id"] is not None:
                        raise ValueError(
                            f"query decision already linked to review: {query_row['review_id']}"
                        )
                    existing_review = conn.execute(
                        """
                        SELECT review_id
                        FROM review_requests
                        WHERE query_decision_id = ?
                        """,
                        (request_payload["query_decision_id"],),
                    ).fetchone()
                    if existing_review is not None:
                        raise ValueError(
                            "query decision already linked to review: "
                            f"{existing_review['review_id']}"
                        )
                packet_row = conn.execute(
                    "SELECT packet_id FROM review_packets WHERE packet_hash = ?",
                    (packet_hash,),
                ).fetchone()
                if packet_row is None:
                    packet_id = new_id("rpacket")
                    conn.execute(
                        """
                        INSERT INTO review_packets (
                            packet_id, packet_hash, packet_json, created_at
                        )
                        VALUES (?, ?, ?, ?)
                        """,
                        (packet_id, packet_hash, packet_json, now),
                    )
                else:
                    packet_id = str(packet_row["packet_id"])

                conn.execute(
                    """
                    INSERT INTO review_requests (
                        review_id, review_type, status, artifact_ids_json,
                        candidate_ids_json, job_id, task_id, round_index, method,
                        artifact_type, packet_id, packet_hash, artifact_hashes_json,
                        query_decision_id, assigned_to, reviewer_role,
                        adjudication_rationale, priority, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        review_id,
                        request_payload["review_type"],
                        ReviewStatus.QUEUED.value,
                        artifact_ids_json,
                        candidate_ids_json,
                        request_payload["job_id"],
                        request_payload["task_id"],
                        request_payload["round_index"],
                        request_payload["method"],
                        request_payload["artifact_type"],
                        packet_id,
                        packet_hash,
                        artifact_hashes_json,
                        request_payload["query_decision_id"],
                        None,
                        None,
                        None,
                        request_payload["priority"],
                        now,
                        now,
                    ),
                )
                if request_payload["query_decision_id"] is not None:
                    conn.execute(
                        """
                        UPDATE human_query_decisions
                        SET review_id = ?
                        WHERE query_decision_id = ?
                        """,
                        (review_id, request_payload["query_decision_id"]),
                    )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
        return self.get_review_request(review_id)

    def get_review_packet(self, packet_id: str) -> ReviewPacketResponse:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM review_packets
                WHERE packet_id = ?
                """,
                (packet_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown review packet: {packet_id}")
        return _review_packet_response_from_row(row)

    def list_review_packets(self) -> list[ReviewPacketResponse]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM review_packets
                ORDER BY created_at ASC, packet_id ASC
                """
            ).fetchall()
        return [_review_packet_response_from_row(row) for row in rows]

    def get_review_request(self, review_id: str) -> ReviewRequestResponse:
        with self.connect() as conn:
            row = self._review_request_row(conn, review_id)
        if row is None:
            raise ValueError(f"unknown review: {review_id}")
        return _review_request_response_from_row(row)

    def list_review_requests(
        self,
        *,
        status: str | None = None,
        task_id: str | None = None,
        assigned_to: str | None = None,
    ) -> list[ReviewRequestResponse]:
        clauses: list[str] = []
        params: list[object] = []
        if status is not None:
            clauses.append("rr.status = ?")
            params.append(status)
        if task_id is not None:
            clauses.append("rr.task_id = ?")
            params.append(task_id)
        if assigned_to is not None:
            clauses.append("rr.assigned_to = ?")
            params.append(assigned_to)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT rr.*, rp.packet_json
                FROM review_requests rr
                JOIN review_packets rp ON rp.packet_id = rr.packet_id
                {where}
                ORDER BY rr.priority DESC, rr.created_at ASC, rr.review_id ASC
                """,
                params,
            ).fetchall()
        return [_review_request_response_from_row(row) for row in rows]

    def claim_review_request(
        self,
        review_id: str,
        request: ReviewClaimRequest,
    ) -> ReviewRequestResponse:
        now = utc_now_iso()
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = self._review_request_row(conn, review_id)
                if row is None:
                    raise ValueError(f"unknown review: {review_id}")
                _require_review_transition(
                    row,
                    review_id=review_id,
                    action="claim",
                    allowed_statuses={ReviewStatus.QUEUED.value, ReviewStatus.IN_REVIEW.value},
                )
                reviewer_role = request.reviewer_role
                if row["status"] == ReviewStatus.IN_REVIEW.value:
                    assigned_to = row["assigned_to"]
                    if assigned_to is not None and assigned_to != request.reviewer_id:
                        raise ValueError(
                            f"review already claimed by another reviewer: {review_id}"
                        )
                    existing_role = row["reviewer_role"]
                    if (
                        existing_role is not None
                        and request.reviewer_role is not None
                        and existing_role != request.reviewer_role
                    ):
                        raise ValueError(
                            f"review already claimed with a different reviewer role: {review_id}"
                        )
                    reviewer_role = existing_role or request.reviewer_role
                conn.execute(
                    """
                    UPDATE review_requests
                    SET status = ?, assigned_to = ?, reviewer_role = ?, updated_at = ?
                    WHERE review_id = ?
                    """,
                    (
                        ReviewStatus.IN_REVIEW.value,
                        request.reviewer_id,
                        reviewer_role,
                        now,
                        review_id,
                    ),
                )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
        return self.get_review_request(review_id)

    def submit_human_feedback(
        self,
        review_id: str,
        request: HumanFeedbackCreateRequest,
    ) -> HumanFeedbackResponse:
        raw_payload = request.model_dump(mode="python")
        _validate_finite_floats(raw_payload, "request")
        request_payload = request.model_dump(mode="json")
        raw_payload_json = _json_dumps(request_payload["raw_payload"])
        feedback_id = new_id("hfb")
        now = utc_now_iso()
        status = HumanFeedbackStatus.AVAILABLE_FOR_EVOLUTION.value

        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                review_row = self._review_request_row(conn, review_id)
                if review_row is None:
                    raise ValueError(f"unknown review: {review_id}")
                _require_review_transition(
                    review_row,
                    review_id=review_id,
                    action="submit feedback for",
                    allowed_statuses={ReviewStatus.IN_REVIEW.value},
                )
                assigned_to = review_row["assigned_to"]
                if assigned_to is not None and assigned_to != request_payload["reviewer_id"]:
                    raise ValueError(f"review claimed by a different reviewer: {review_id}")
                effective_reviewer_role = request_payload["reviewer_role"]
                claimed_reviewer_role = review_row["reviewer_role"]
                if claimed_reviewer_role is not None:
                    if (
                        request_payload["reviewer_role"] is not None
                        and request_payload["reviewer_role"] != claimed_reviewer_role
                    ):
                        raise ValueError(
                            f"feedback reviewer role does not match claimed role: {review_id}"
                        )
                    effective_reviewer_role = str(claimed_reviewer_role)
                normalized_payload = _sanitize_review_boundary_payload(
                    _normalize_feedback_payload(
                        request,
                        reviewer_role=effective_reviewer_role,
                    )
                )
                if not isinstance(normalized_payload, dict):
                    normalized_payload = {}
                stored_rationale = _sanitize_review_boundary_text(
                    request_payload["rationale"] or ""
                )
                normalized_payload_json = _json_dumps(normalized_payload)
                conn.execute(
                    """
                    INSERT INTO human_feedback (
                        feedback_id, review_id, reviewer_id, reviewer_role, status,
                        decision, score, confidence, rationale, raw_payload_json,
                        normalized_payload_json, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        feedback_id,
                        review_id,
                        request_payload["reviewer_id"],
                        effective_reviewer_role,
                        status,
                        request_payload["decision"],
                        request_payload["score"],
                        request_payload["confidence"],
                        stored_rationale,
                        raw_payload_json,
                        normalized_payload_json,
                        now,
                    ),
                )
                conn.execute(
                    """
                    UPDATE review_requests
                    SET status = ?, updated_at = ?
                    WHERE review_id = ?
                    """,
                    (ReviewStatus.SUBMITTED.value, now, review_id),
                )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM human_feedback WHERE feedback_id = ?",
                (feedback_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown feedback: {feedback_id}")
        return _human_feedback_response_from_row(row)

    def list_human_feedback(
        self,
        *,
        review_id: str | None = None,
    ) -> list[HumanFeedbackResponse]:
        with self.connect() as conn:
            if review_id is not None:
                if self._review_request_row(conn, review_id) is None:
                    raise ValueError(f"unknown review: {review_id}")
                rows = conn.execute(
                    """
                    SELECT * FROM human_feedback
                    WHERE review_id = ?
                    ORDER BY created_at ASC, feedback_id ASC
                    """,
                    (review_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM human_feedback
                    ORDER BY created_at ASC, feedback_id ASC
                    """
                ).fetchall()
        return [_human_feedback_response_from_row(row) for row in rows]

    def adjudicate_review_request(
        self,
        review_id: str,
        request: ReviewAdjudicationRequest,
    ) -> ReviewRequestResponse:
        target_status = str(request.status)
        rationale = (
            None
            if request.rationale is None
            else _sanitize_review_boundary_text(request.rationale)
        )
        now = utc_now_iso()
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = self._review_request_row(conn, review_id)
                if row is None:
                    raise ValueError(f"unknown review: {review_id}")
                current_status = str(row["status"])
                allowed_targets = _ADJUDICATION_TRANSITIONS.get(current_status, set())
                if target_status not in allowed_targets:
                    raise ValueError(
                        f"cannot adjudicate review {review_id} from status "
                        f"{current_status} to {target_status}"
                    )
                conn.execute(
                    """
                    UPDATE review_requests
                    SET status = ?, adjudication_rationale = ?, updated_at = ?
                    WHERE review_id = ?
                    """,
                    (target_status, rationale, now, review_id),
                )
                if target_status == ReviewStatus.REJECTED_INVALID.value:
                    _archive_active_feedback(
                        conn,
                        review_id=review_id,
                        status=HumanFeedbackStatus.REJECTED_INVALID,
                    )
                elif target_status == ReviewStatus.ARCHIVED_ONLY.value:
                    _archive_active_feedback(
                        conn,
                        review_id=review_id,
                        status=HumanFeedbackStatus.ARCHIVED_ONLY,
                    )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
        return self.get_review_request(review_id)

    def resolve_review_request(self, review_id: str) -> ReviewRequestResponse:
        now = utc_now_iso()
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = self._review_request_row(conn, review_id)
                if row is None:
                    raise ValueError(f"unknown review: {review_id}")
                _require_review_transition(
                    row,
                    review_id=review_id,
                    action="resolve",
                    allowed_statuses=_RESOLVABLE_REVIEW_STATUSES,
                )
                conn.execute(
                    """
                    UPDATE review_requests
                    SET status = ?, updated_at = ?
                    WHERE review_id = ?
                    """,
                    (ReviewStatus.RESOLVED.value, now, review_id),
                )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
        return self.get_review_request(review_id)

    def mark_review_stale(self, review_id: str) -> ReviewRequestResponse:
        now = utc_now_iso()
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = self._review_request_row(conn, review_id)
                if row is None:
                    raise ValueError(f"unknown review: {review_id}")
                status = str(row["status"])
                if status not in _STALEABLE_REVIEW_STATUSES:
                    raise ValueError(
                        f"cannot mark review {review_id} stale from status {status}"
                    )
                conn.execute(
                    """
                    UPDATE review_requests
                    SET status = ?, updated_at = ?
                    WHERE review_id = ?
                    """,
                    (ReviewStatus.STALE.value, now, review_id),
                )
                conn.execute(
                    """
                    UPDATE human_feedback
                    SET status = ?
                    WHERE review_id = ?
                      AND status IN (?, ?)
                    """,
                    (
                        HumanFeedbackStatus.ARCHIVED_ONLY.value,
                        review_id,
                        HumanFeedbackStatus.AVAILABLE_FOR_EVOLUTION.value,
                        HumanFeedbackStatus.CONSUMED.value,
                    ),
                )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
        return self.get_review_request(review_id)

    def create_feedback_application(
        self,
        request: FeedbackApplicationCreateRequest,
    ) -> FeedbackApplicationResponse:
        request_payload = request.model_dump(mode="json")
        for key in ("target_id", "consumed_by_method", "consumed_in_job_id"):
            value = request_payload.get(key)
            if isinstance(value, str):
                request_payload[key] = _sanitize_review_metadata_text(value)
        request_payload["effect_summary"] = _sanitize_review_boundary_text(
            request_payload["effect_summary"]
        )
        application_id = new_id("hfa")
        now = utc_now_iso()
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                feedback_row = self._feedback_row(conn, request.feedback_id)
                if feedback_row is None:
                    raise ValueError(f"unknown feedback: {request.feedback_id}")
                if feedback_row["status"] not in _CONSUMABLE_FEEDBACK_STATUSES:
                    raise ValueError(f"feedback is not available for evolution: {request.feedback_id}")
                review_row = conn.execute(
                    """
                    SELECT status
                    FROM review_requests
                    WHERE review_id = ?
                    """,
                    (feedback_row["review_id"],),
                ).fetchone()
                if review_row is None:
                    raise ValueError(f"unknown review: {feedback_row['review_id']}")
                if review_row["status"] in {
                    ReviewStatus.STALE.value,
                    ReviewStatus.REJECTED_INVALID.value,
                    ReviewStatus.ARCHIVED_ONLY.value,
                }:
                    raise ValueError(
                        f"feedback parent review is not available for evolution: {request.feedback_id}"
                    )
                existing_application = conn.execute(
                    """
                    SELECT *
                    FROM feedback_applications
                    WHERE feedback_id = ?
                      AND target_type = ?
                      AND target_id = ?
                      AND consumed_by_method = ?
                      AND COALESCE(consumed_in_job_id, '') = COALESCE(?, '')
                    """,
                    (
                        request_payload["feedback_id"],
                        request_payload["target_type"],
                        request_payload["target_id"],
                        request_payload["consumed_by_method"],
                        request_payload["consumed_in_job_id"],
                    ),
                ).fetchone()
                if existing_application is not None:
                    if existing_application["effect_summary"] != request_payload["effect_summary"]:
                        raise ValueError(
                            "feedback application already exists with a different effect summary: "
                            f"{request.feedback_id}"
                        )
                    conn.commit()
                    return _feedback_application_response_from_row(existing_application)
                conn.execute(
                    """
                    INSERT INTO feedback_applications (
                        application_id, feedback_id, target_type, target_id,
                        consumed_by_method, consumed_in_job_id, effect_summary,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        application_id,
                        request_payload["feedback_id"],
                        request_payload["target_type"],
                        request_payload["target_id"],
                        request_payload["consumed_by_method"],
                        request_payload["consumed_in_job_id"],
                        request_payload["effect_summary"],
                        now,
                    ),
                )
                if feedback_row["status"] == HumanFeedbackStatus.AVAILABLE_FOR_EVOLUTION.value:
                    conn.execute(
                        """
                        UPDATE human_feedback
                        SET status = ?
                        WHERE feedback_id = ?
                        """,
                        (HumanFeedbackStatus.CONSUMED.value, request.feedback_id),
                    )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM feedback_applications WHERE application_id = ?",
                (application_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown feedback application: {application_id}")
        return _feedback_application_response_from_row(row)

    def list_feedback_applications(
        self,
        *,
        feedback_id: str | None = None,
    ) -> list[FeedbackApplicationResponse]:
        with self.connect() as conn:
            if feedback_id is not None:
                if self._feedback_row(conn, feedback_id) is None:
                    raise ValueError(f"unknown feedback: {feedback_id}")
                rows = conn.execute(
                    """
                    SELECT * FROM feedback_applications
                    WHERE feedback_id = ?
                    ORDER BY created_at ASC, application_id ASC
                    """,
                    (feedback_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM feedback_applications
                    ORDER BY created_at ASC, application_id ASC
                    """
                ).fetchall()
        return [_feedback_application_response_from_row(row) for row in rows]

    def create_human_query_decision(
        self,
        request: HumanQueryDecisionCreateRequest,
    ) -> HumanQueryDecisionResponse:
        raw_payload = request.model_dump(mode="python")
        _validate_finite_floats(raw_payload, "request")
        request_payload = request.model_dump(mode="json")
        query_decision_id = new_id("hqd")
        now = utc_now_iso()
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                _insert_human_query_decision_row(
                    conn,
                    query_decision_id=query_decision_id,
                    request_payload=request_payload,
                    review_id=None,
                    created_at=now,
                )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
        return self.get_human_query_decision(query_decision_id)

    def get_human_query_decision(
        self,
        query_decision_id: str,
    ) -> HumanQueryDecisionResponse:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM human_query_decisions WHERE query_decision_id = ?",
                (query_decision_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown query decision: {query_decision_id}")
        return _human_query_decision_response_from_row(row)

    def _review_request_row(
        self,
        conn: sqlite3.Connection,
        review_id: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT rr.*, rp.packet_json
            FROM review_requests rr
            JOIN review_packets rp ON rp.packet_id = rr.packet_id
            WHERE rr.review_id = ?
            """,
            (review_id,),
        ).fetchone()

    def _feedback_row(
        self,
        conn: sqlite3.Connection,
        feedback_id: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM human_feedback WHERE feedback_id = ?",
            (feedback_id,),
        ).fetchone()

    def ingest_event(self, request: EventIngestRequest) -> EventIngestResponse:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT event_id FROM events
                WHERE source = ? AND event_type = ? AND source_event_id = ?
                """,
                (request.source, request.event_type, request.source_event_id),
            ).fetchone()
            if existing is not None:
                conn.rollback()
                return EventIngestResponse(
                    event_id=str(existing["event_id"]),
                    ingested=False,
                    duplicate=True,
                )

            event_id = new_id("evt")
            request_payload = json.loads(json.dumps(request.model_dump(mode="json")))
            created_at = request_payload["created_at"] or utc_now_iso()
            ingested_at = utc_now_iso()
            payload_path = self.files.event_payload_path(event_id)
            self.files.write_json(payload_path, request_payload)
            agent_harness = _text_metadata(request.agent.get("harness"))
            agent_model = _text_metadata(request.agent.get("model_name"))
            conn.execute(
                """
                INSERT INTO events (
                    event_id, source, event_type, source_event_id, created_at,
                    ingested_at, task_id, session_id, policy_version,
                    rollout_step, agent_harness, agent_model, base_model,
                    status, reward, payload_path
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    request.source,
                    request.event_type,
                    request.source_event_id,
                    created_at,
                    ingested_at,
                    request.task_id,
                    request.session_id,
                    request.policy_version,
                    request.rollout_step,
                    agent_harness,
                    agent_model,
                    request.base_model,
                    request.status,
                    request.reward,
                    str(payload_path),
                ),
            )
            conn.commit()
            return EventIngestResponse(event_id=event_id, ingested=True, duplicate=False)

    def register_artifact(self, request: ArtifactRegisterRequest) -> ArtifactResponse:
        raw_payload = request.model_dump(mode="python")
        for field in ("manifest", "lineage", "compatibility", "scores", "tags"):
            _validate_finite_floats(raw_payload[field], field)

        request_payload = request.model_dump(mode="json")
        artifact_type = str(request_payload["type"])
        if artifact_type == str(ArtifactType.AGENT_SYSTEM):
            manifest = dict(request_payload["manifest"])
            manifest["target_path"] = normalize_agent_system_target_path(
                manifest.get("target_path")
            )
            request_payload["manifest"] = manifest
        lineage_json = _json_dumps(request_payload["lineage"])
        compatibility_json = _json_dumps(request_payload["compatibility"])
        scores_json = _json_dumps(request_payload["scores"])
        tags_json = _json_dumps(request_payload["tags"])

        for _ in range(MAX_ARTIFACT_ID_ATTEMPTS):
            artifact_id = new_id("art")
            created_at = utc_now_iso()
            manifest_path = self.files.artifact_manifest_path(artifact_type, artifact_id)
            manifest_payload = {
                "artifact_id": artifact_id,
                "type": artifact_type,
                "name": request_payload["name"],
                "uri": request_payload["uri"],
                "manifest": request_payload["manifest"],
                "lineage": request_payload["lineage"],
                "compatibility": request_payload["compatibility"],
                "scores": request_payload["scores"],
                "tags": request_payload["tags"],
                "promoted": request_payload["promoted"],
            }
            manifest_created = False
            with self.connect() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    existing = conn.execute(
                        "SELECT 1 FROM artifacts WHERE artifact_id = ?",
                        (artifact_id,),
                    ).fetchone()
                    if existing is not None or manifest_path.exists():
                        conn.rollback()
                        continue
                    conn.execute(
                        """
                        INSERT INTO artifacts (
                            artifact_id, type, name, version, state, created_at, uri,
                            manifest_path, lineage_json, compatibility_json, scores_json,
                            tags_json, promoted
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            artifact_id,
                            artifact_type,
                            request_payload["name"],
                            1,
                            str(ArtifactState.ACTIVE),
                            created_at,
                            request_payload["uri"],
                            str(manifest_path),
                            lineage_json,
                            compatibility_json,
                            scores_json,
                            tags_json,
                            1 if request_payload["promoted"] else 0,
                        ),
                    )
                    try:
                        _write_json_strict_exclusive(
                            self.files,
                            manifest_path,
                            manifest_payload,
                        )
                    except FileExistsError:
                        conn.rollback()
                        continue
                    manifest_created = True
                    conn.commit()
                except Exception:
                    try:
                        conn.rollback()
                    except sqlite3.Error:
                        pass
                    if manifest_created:
                        try:
                            manifest_path.unlink(missing_ok=True)
                        except OSError:
                            pass
                    raise
            return ArtifactResponse(
                artifact_id=artifact_id,
                type=artifact_type,
                name=request_payload["name"],
                version=1,
                state=ArtifactState.ACTIVE,
                uri=request_payload["uri"],
                manifest=request_payload["manifest"],
                compatibility=request_payload["compatibility"],
                scores=request_payload["scores"],
                tags=request_payload["tags"],
                promoted=request_payload["promoted"],
            )
        raise RuntimeError("could not allocate unique artifact id")

    def get_artifact(self, artifact_id: str) -> ArtifactResponse:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown artifact: {artifact_id}")
        return _artifact_response_from_row(row)

    def update_artifact_promotion(
        self,
        artifact_id: str,
        *,
        promoted: bool,
    ) -> ArtifactResponse:
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM artifacts WHERE artifact_id = ?",
                    (artifact_id,),
                ).fetchone()
                if row is None:
                    raise ValueError(f"unknown artifact: {artifact_id}")
                manifest_path = Path(str(row["manifest_path"]))
                try:
                    manifest_payload = json.loads(
                        manifest_path.read_text(encoding="utf-8")
                    )
                except FileNotFoundError as exc:
                    raise ValueError(
                        f"artifact {artifact_id} manifest file is missing: {manifest_path}"
                    ) from exc
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"artifact {artifact_id} manifest file is not valid JSON: "
                        f"{manifest_path}"
                    ) from exc
                if not isinstance(manifest_payload, dict):
                    raise ValueError(
                        f"artifact {artifact_id} manifest file is not a JSON object: "
                        f"{manifest_path}"
                    )
                manifest_payload["promoted"] = bool(promoted)
                manifest_path.write_text(
                    _json_dumps(manifest_payload, indent=2),
                    encoding="utf-8",
                )
                conn.execute(
                    "UPDATE artifacts SET promoted = ? WHERE artifact_id = ?",
                    (1 if promoted else 0, artifact_id),
                )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
        return self.get_artifact(artifact_id)

    def update_artifact_promotion_from_request(
        self,
        artifact_id: str,
        request: ArtifactPromotionUpdateRequest,
    ) -> ArtifactResponse:
        return self.update_artifact_promotion(
            artifact_id,
            promoted=request.promoted,
        )

    def _promoted_artifact_rows(self) -> list[dict[str, object]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM artifacts
                WHERE promoted = 1 AND state IN (?, ?)
                """,
                (str(ArtifactState.ACTIVE), str(ArtifactState.EXPERIMENTAL)),
            ).fetchall()
        return [dict(row) for row in rows]

    def resolve_context(self, request: ContextResolveRequest) -> ContextResolveResponse:
        raw_payload = request.model_dump(mode="python")
        _validate_finite_floats(raw_payload, "request")

        requested_artifact_ids = requested_context_artifact_ids(request)
        rows = [
            row
            for row in self._promoted_artifact_rows()
            if _artifact_id_allowed(row, requested_artifact_ids)
            and artifact_matches(request, row)
        ]
        rows = sort_candidates(rows)

        selected_memory: list[dict[str, object]] = []
        rendered_parts: list[str] = []
        memory_chars = 0
        selected_agent_system: list[dict[str, object]] = []
        agent_system_parts: list[str] = []
        agent_system_chars = 0
        skills: list[dict[str, object]] = []
        adapters: list[dict[str, object]] = []
        selected_ids: list[str] = []

        for row in rows:
            kind = artifact_type(row)
            artifact_id = str(row["artifact_id"])
            if kind == ArtifactType.TEXT_MEMORY and memory_chars < request.limits.max_memory_chars:
                text = read_file_uri_text(str(row["uri"]))
                separator_chars = 2 if rendered_parts else 0
                remaining = request.limits.max_memory_chars - memory_chars - separator_chars
                if not text or remaining <= 0:
                    continue
                clipped = text[:remaining]
                rendered_parts.append(clipped)
                memory_chars += separator_chars + len(clipped)
                selected_memory.append({"artifact_id": artifact_id, "name": row["name"]})
                selected_ids.append(artifact_id)
            elif (
                kind == ArtifactType.AGENT_SYSTEM
                and agent_system_chars < request.limits.max_agent_system_chars
            ):
                text = read_file_uri_text(str(row["uri"]))
                separator_chars = 2 if agent_system_parts else 0
                remaining = (
                    request.limits.max_agent_system_chars - agent_system_chars - separator_chars
                )
                if not text or remaining <= 0:
                    continue
                clipped = text[:remaining]
                manifest = artifact_manifest(row)
                try:
                    target_path = normalize_agent_system_target_path(manifest.get("target_path"))
                except ValueError:
                    continue
                agent_system_parts.append(clipped)
                agent_system_chars += separator_chars + len(clipped)
                selected_agent_system.append(
                    {
                        "artifact_id": artifact_id,
                        "name": row["name"],
                        "target_path": target_path,
                        "rendered_text": clipped,
                    }
                )
                selected_ids.append(artifact_id)
            elif (
                kind == ArtifactType.SKILL_BUNDLE
                and len(skills) < request.limits.max_skill_bundles
            ):
                skills.append(
                    {
                        "artifact_id": artifact_id,
                        "name": row["name"],
                        "uri": row["uri"],
                    }
                )
                selected_ids.append(artifact_id)
            elif (
                kind == ArtifactType.PARAMETRIC_MEMORY
                and len(adapters) < request.limits.max_adapters
            ):
                manifest = artifact_manifest(row)
                adapter_format = manifest.get("adapter_format")
                if not isinstance(adapter_format, str) or not adapter_format:
                    adapter_format = "lora"
                adapter_id = manifest.get("adapter_id")
                if not isinstance(adapter_id, str) or not adapter_id.strip():
                    adapter_id = row["name"]
                adapters.append(
                    {
                        "artifact_id": artifact_id,
                        "adapter_id": adapter_id,
                        "uri": row["uri"],
                        "weight": 1.0,
                        "format": adapter_format,
                    }
                )
                selected_ids.append(artifact_id)

        for _ in range(MAX_CONTEXT_ID_ATTEMPTS):
            context_id = new_id("ctx")
            response = ContextResolveResponse(
                context_id=context_id,
                memory={
                    "artifact_ids": [str(item["artifact_id"]) for item in selected_memory],
                    "rendered_text": "\n\n".join(rendered_parts),
                },
                agent_system={
                    "artifact_ids": [str(item["artifact_id"]) for item in selected_agent_system],
                    "rendered_text": "\n\n".join(agent_system_parts),
                    "target_path": (
                        selected_agent_system[0]["target_path"]
                        if selected_agent_system
                        else "AGENTS.md"
                    ),
                    "targets": selected_agent_system,
                },
                skills=skills,
                adapter_merge_spec=AdapterMergeSpec(
                    base_model=request.base_model,
                    merge_mode="runtime_lora" if adapters else "reference_only",
                    adapters=adapters,
                ),
                selection={
                    "artifact_ids": selected_ids,
                    "reasons": [
                        "matched requested promoted compatible artifacts"
                        if requested_artifact_ids is not None
                        else "matched promoted compatible artifacts"
                    ],
                },
            )
            request_payload = request.model_dump(mode="json")
            response_payload = response.model_dump(mode="json")
            request_json = _json_dumps(request_payload)
            response_json = _json_dumps(response_payload)
            selected_ids_json = _json_dumps(selected_ids)
            snapshot_path = self.files.context_snapshot_path(context_id)
            snapshot_created = False
            with self.connect() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    existing = conn.execute(
                        "SELECT 1 FROM contexts WHERE context_id = ?",
                        (context_id,),
                    ).fetchone()
                    if existing is not None or snapshot_path.exists():
                        conn.rollback()
                        continue
                    conn.execute(
                        """
                        INSERT INTO contexts (
                            context_id, created_at, request_json, response_json,
                            selected_artifact_ids_json
                        )
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            context_id,
                            utc_now_iso(),
                            request_json,
                            response_json,
                            selected_ids_json,
                        ),
                    )
                    try:
                        _write_json_strict_exclusive(
                            self.files,
                            snapshot_path,
                            {
                                "request": request_payload,
                                "response": response_payload,
                            },
                        )
                    except FileExistsError:
                        conn.rollback()
                        continue
                    snapshot_created = True
                    conn.commit()
                except Exception:
                    try:
                        conn.rollback()
                    except sqlite3.Error:
                        pass
                    if snapshot_created:
                        try:
                            snapshot_path.unlink(missing_ok=True)
                        except OSError:
                            pass
                    raise
            return response
        raise RuntimeError("could not allocate unique context id")

    def create_job(self, request: JobCreateRequest) -> JobCreateResponse:
        raw_payload = request.model_dump(mode="python")
        _validate_finite_floats(raw_payload["config"], "config")
        request_payload = request.model_dump(mode="json")
        input_artifact_ids = request_payload["input_artifact_ids"]
        input_artifact_ids_json = _json_dumps(input_artifact_ids)
        config_json = _json_dumps(request_payload["config"])

        job_id = new_id("job")
        now = utc_now_iso()
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._validate_input_artifacts_exist(conn, input_artifact_ids)
                conn.execute(
                    """
                    INSERT INTO jobs (
                        job_id, job_type, method, state, priority, created_at,
                        updated_at, input_artifact_ids_json, config_json,
                        attempt_count
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        request_payload["job_type"],
                        request_payload["method"],
                        str(JobState.PENDING),
                        request_payload["priority"],
                        now,
                        now,
                        input_artifact_ids_json,
                        config_json,
                        0,
                    ),
                )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
        return JobCreateResponse(job_id=job_id, state=JobState.PENDING)

    def _validate_input_artifacts_exist(
        self,
        conn: sqlite3.Connection,
        artifact_ids: list[str],
    ) -> None:
        unique_ids = list(dict.fromkeys(artifact_ids))
        if not unique_ids:
            return
        rows = conn.execute(
            "SELECT artifact_id FROM artifacts WHERE artifact_id IN (%s)"
            % ",".join("?" for _ in unique_ids),
            unique_ids,
        ).fetchall()
        existing_ids = {str(row["artifact_id"]) for row in rows}
        missing_ids = [
            artifact_id for artifact_id in unique_ids if artifact_id not in existing_ids
        ]
        if missing_ids:
            label = "artifact_id" if len(missing_ids) == 1 else "artifact_ids"
            raise ValueError(f"unknown input {label}: {', '.join(missing_ids)}")

    def claim_job(self, request: WorkerClaimRequest) -> WorkerClaimResponse:
        now_dt = datetime.now(UTC)
        lease_expires_at = _utc_dt_to_iso(now_dt + timedelta(seconds=request.lease_seconds))
        where = "state = ?"
        params: list[object] = [str(JobState.PENDING)]
        if request.capabilities:
            where += f" AND job_type IN ({','.join('?' for _ in request.capabilities)})"
            params.extend(request.capabilities)

        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._requeue_expired_jobs(conn, now_dt)
                row = conn.execute(
                    f"""
                    SELECT * FROM jobs
                    WHERE {where}
                    ORDER BY priority DESC, created_at ASC, job_id ASC
                    LIMIT 1
                    """,
                    params,
                ).fetchone()
                if row is None:
                    conn.rollback()
                    return WorkerClaimResponse(job=None)

                lease_id = new_id("lease")
                cursor = conn.execute(
                    """
                    UPDATE jobs
                    SET state = ?, claimed_by = ?, lease_id = ?, lease_expires_at = ?,
                        updated_at = ?, attempt_count = attempt_count + 1
                    WHERE job_id = ? AND state = ?
                    """,
                    (
                        str(JobState.CLAIMED),
                        request.worker_id,
                        lease_id,
                        lease_expires_at,
                        utc_now_iso(),
                        row["job_id"],
                        str(JobState.PENDING),
                    ),
                )
                if cursor.rowcount != 1:
                    conn.rollback()
                    return WorkerClaimResponse(job=None)
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise

        return WorkerClaimResponse(
            job={
                "job_id": row["job_id"],
                "lease_id": lease_id,
                "job_type": row["job_type"],
                "method": row["method"],
                "input_artifacts": self._worker_claim_input_artifacts(
                    json.loads(str(row["input_artifact_ids_json"]))
                ),
                "config": json.loads(str(row["config_json"])),
                "priority": row["priority"],
                "state": JobState.CLAIMED,
            }
        )

    def _requeue_expired_jobs(self, conn: sqlite3.Connection, now: datetime) -> None:
        rows = conn.execute(
            """
            SELECT job_id, lease_expires_at
            FROM jobs
            WHERE state IN (?, ?) AND lease_expires_at IS NOT NULL
            """,
            (str(JobState.CLAIMED), str(JobState.RUNNING)),
        ).fetchall()
        now = now.astimezone(UTC)
        for row in rows:
            try:
                lease_expires_at = _parse_utc_iso(str(row["lease_expires_at"]))
            except ValueError:
                conn.execute(
                    """
                    UPDATE jobs
                    SET state = ?, claimed_by = NULL, lease_id = NULL,
                        lease_expires_at = NULL, updated_at = ?, error = ?
                    WHERE job_id = ?
                    """,
                    (
                        str(JobState.FAILED),
                        utc_now_iso(),
                        f"invalid lease_expires_at: {row['lease_expires_at']}",
                        row["job_id"],
                    ),
                )
                continue
            if lease_expires_at <= now:
                conn.execute(
                    """
                    UPDATE jobs
                    SET state = ?, claimed_by = NULL, lease_id = NULL,
                        lease_expires_at = NULL, updated_at = ?,
                        error = COALESCE(error, ?)
                    WHERE job_id = ?
                    """,
                    (
                        str(JobState.PENDING),
                        utc_now_iso(),
                        f"lease expired at {_utc_dt_to_iso(lease_expires_at)}",
                        row["job_id"],
                    ),
                )

    def _worker_claim_input_artifacts(self, artifact_ids: list[str]) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []
        with self.connect() as conn:
            for artifact_id in artifact_ids:
                artifact = conn.execute(
                    "SELECT artifact_id, type, uri, name FROM artifacts WHERE artifact_id = ?",
                    (artifact_id,),
                ).fetchone()
                if artifact is not None:
                    artifacts.append(
                        {
                            "artifact_id": artifact["artifact_id"],
                            "type": artifact["type"],
                            "uri": artifact["uri"],
                            "name": artifact["name"],
                        }
                    )
        return artifacts

    def _assert_job_lease(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        lease_id: str,
    ) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise JobLeaseError(f"unknown job: {job_id}")
        if row["state"] not in ACTIVE_JOB_STATES or row["lease_id"] != lease_id:
            raise JobLeaseError(f"invalid lease for job: {job_id}")

        lease_expires_at = row["lease_expires_at"]
        if lease_expires_at is not None:
            try:
                expires_at = _parse_utc_iso(str(lease_expires_at))
            except ValueError as exc:
                raise JobLeaseError(f"invalid lease_expires_at for job: {job_id}") from exc
            if expires_at <= datetime.now(UTC):
                raise JobLeaseError(f"lease expired for job: {job_id}")
        return row

    def heartbeat_job(
        self,
        job_id: str,
        request: WorkerHeartbeatRequest,
    ) -> dict[str, object]:
        if request.progress is not None:
            _validate_finite_floats(request.progress, "progress")

        lease_expires_at = _utc_dt_to_iso(
            datetime.now(UTC) + timedelta(seconds=DEFAULT_HEARTBEAT_LEASE_SECONDS)
        )
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._assert_job_lease(conn, job_id, request.lease_id)
                conn.execute(
                    """
                    UPDATE jobs
                    SET state = ?, updated_at = ?, lease_expires_at = ?
                    WHERE job_id = ?
                    """,
                    (str(JobState.RUNNING), utc_now_iso(), lease_expires_at, job_id),
                )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
        return {
            "job_id": job_id,
            "state": str(JobState.RUNNING),
            "progress": request.progress,
            "lease_expires_at": lease_expires_at,
        }

    def complete_job(
        self,
        job_id: str,
        request: WorkerCompleteRequest,
    ) -> dict[str, object]:
        raw_payload = request.model_dump(mode="python")
        _validate_finite_floats(raw_payload["report"], "report")

        force_unpromoted_outputs = False
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                job_row = self._assert_job_lease(conn, job_id, request.lease_id)
                job_config = json.loads(str(job_row["config_json"]))
                if isinstance(job_config, dict):
                    force_unpromoted_outputs = job_config.get("promoted") is False
                conn.rollback()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise

        registered_artifact_ids: list[str] = []
        registered_artifacts: list[ArtifactResponse] = []
        try:
            for artifact_request in request.artifacts:
                request_to_register = (
                    artifact_request.model_copy(update={"promoted": False})
                    if force_unpromoted_outputs
                    else artifact_request
                )
                artifact = self.register_artifact(request_to_register)
                registered_artifact_ids.append(artifact.artifact_id)
                registered_artifacts.append(artifact)

            with self.connect() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    job_row = self._assert_job_lease(conn, job_id, request.lease_id)
                    input_artifact_ids = json.loads(str(job_row["input_artifact_ids_json"]))
                    unique_input_artifact_ids = list(dict.fromkeys(input_artifact_ids))
                    self._validate_input_artifacts_exist(conn, unique_input_artifact_ids)
                    for input_artifact_id in unique_input_artifact_ids:
                        for output_artifact_id in registered_artifact_ids:
                            conn.execute(
                                """
                                INSERT OR IGNORE INTO artifact_lineage (
                                    parent_artifact_id, child_artifact_id, relation
                                )
                                VALUES (?, ?, ?)
                                """,
                                (input_artifact_id, output_artifact_id, "job_input"),
                            )
                    method = str(job_row["method"])
                    for artifact in registered_artifacts:
                        self._materialize_feedback_applications_for_artifact(
                            conn,
                            artifact=artifact,
                            job_id=job_id,
                            method=method,
                        )
                    conn.execute(
                        """
                        UPDATE jobs
                        SET state = ?, updated_at = ?, claimed_by = NULL, lease_id = NULL,
                            lease_expires_at = NULL, error = NULL
                        WHERE job_id = ?
                        """,
                        (str(JobState.SUCCEEDED), utc_now_iso(), job_id),
                    )
                    conn.commit()
                except Exception:
                    try:
                        conn.rollback()
                    except sqlite3.Error:
                        pass
                    raise
        except JobLeaseError as exc:
            if registered_artifact_ids:
                try:
                    self._cleanup_registered_artifacts(registered_artifact_ids)
                except Exception as cleanup_exc:
                    exc.add_note(f"artifact cleanup failed: {cleanup_exc}")
            raise
        except Exception as exc:
            cleanup_error: Exception | None = None
            if registered_artifact_ids:
                try:
                    self._cleanup_registered_artifacts(registered_artifact_ids)
                except Exception as cleanup_exc:
                    cleanup_error = cleanup_exc
            try:
                self._record_job_completion_failure(job_id, request.lease_id, error=exc)
            except Exception as record_exc:
                exc.add_note(f"job failure recording failed: {record_exc}")
            if cleanup_error is not None:
                exc.add_note(f"artifact cleanup failed: {cleanup_error}")
            raise

        return {
            "job_id": job_id,
            "state": str(JobState.SUCCEEDED),
            "artifact_ids": registered_artifact_ids,
        }

    def _materialize_feedback_applications_for_artifact(
        self,
        conn: sqlite3.Connection,
        *,
        artifact: ArtifactResponse,
        job_id: str,
        method: str,
    ) -> None:
        feedback_ids = _string_list(artifact.manifest.get("human_feedback_ids"))
        if not feedback_ids:
            return
        target_type = _feedback_application_target_type(artifact.manifest)
        consumed_by_method = method
        effect_summary = _feedback_application_effect_summary(
            artifact.manifest,
            artifact=artifact,
            method=consumed_by_method,
        )
        now = utc_now_iso()
        for feedback_id in dict.fromkeys(feedback_ids):
            feedback_row = self._feedback_row(conn, feedback_id)
            if feedback_row is None:
                continue
            if feedback_row["status"] not in _CONSUMABLE_FEEDBACK_STATUSES:
                continue
            review_row = conn.execute(
                """
                SELECT status
                FROM review_requests
                WHERE review_id = ?
                """,
                (feedback_row["review_id"],),
            ).fetchone()
            if review_row is None or review_row["status"] in {
                ReviewStatus.STALE.value,
                ReviewStatus.REJECTED_INVALID.value,
                ReviewStatus.ARCHIVED_ONLY.value,
            }:
                continue
            request_payload = {
                "feedback_id": feedback_id,
                "target_type": target_type,
                "target_id": _sanitize_review_metadata_text(artifact.artifact_id),
                "consumed_by_method": _sanitize_review_metadata_text(consumed_by_method),
                "consumed_in_job_id": _sanitize_review_metadata_text(job_id),
                "effect_summary": _sanitize_review_boundary_text(effect_summary),
            }
            existing_application = conn.execute(
                """
                SELECT 1
                FROM feedback_applications
                WHERE feedback_id = ?
                  AND target_type = ?
                  AND target_id = ?
                  AND consumed_by_method = ?
                  AND COALESCE(consumed_in_job_id, '') = COALESCE(?, '')
                """,
                (
                    request_payload["feedback_id"],
                    request_payload["target_type"],
                    request_payload["target_id"],
                    request_payload["consumed_by_method"],
                    request_payload["consumed_in_job_id"],
                ),
            ).fetchone()
            if existing_application is None:
                conn.execute(
                    """
                    INSERT INTO feedback_applications (
                        application_id, feedback_id, target_type, target_id,
                        consumed_by_method, consumed_in_job_id, effect_summary,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_id("hfa"),
                        request_payload["feedback_id"],
                        request_payload["target_type"],
                        request_payload["target_id"],
                        request_payload["consumed_by_method"],
                        request_payload["consumed_in_job_id"],
                        request_payload["effect_summary"],
                        now,
                    ),
                )
            if feedback_row["status"] == HumanFeedbackStatus.AVAILABLE_FOR_EVOLUTION.value:
                conn.execute(
                    """
                    UPDATE human_feedback
                    SET status = ?
                    WHERE feedback_id = ?
                    """,
                    (HumanFeedbackStatus.CONSUMED.value, feedback_id),
                )

    def fail_job(self, job_id: str, request: WorkerFailRequest) -> dict[str, object]:
        next_state = JobState.PENDING if request.retryable else JobState.FAILED
        error = request.error
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._assert_job_lease(conn, job_id, request.lease_id)
                conn.execute(
                    """
                    UPDATE jobs
                    SET state = ?, updated_at = ?, claimed_by = NULL, lease_id = NULL,
                        lease_expires_at = NULL, error = ?
                    WHERE job_id = ?
                    """,
                    (str(next_state), utc_now_iso(), error, job_id),
                )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
        return {"job_id": job_id, "state": str(next_state), "error": error}

    def _cleanup_registered_artifacts(self, artifact_ids: list[str]) -> None:
        for artifact_id in artifact_ids:
            artifact_manifest_path: Path | None = None
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                artifact_row = conn.execute(
                    "SELECT type, manifest_path FROM artifacts WHERE artifact_id = ?",
                    (artifact_id,),
                ).fetchone()
                if artifact_row is not None:
                    artifact_manifest_path = Path(str(artifact_row["manifest_path"]))
                    conn.execute(
                        """
                        DELETE FROM artifact_lineage
                        WHERE parent_artifact_id = ? OR child_artifact_id = ?
                        """,
                        (artifact_id, artifact_id),
                    )
                    conn.execute("DELETE FROM artifacts WHERE artifact_id = ?", (artifact_id,))
                conn.commit()
            if artifact_manifest_path is not None:
                artifact_manifest_path.unlink(missing_ok=True)

    def _record_job_completion_failure(
        self,
        job_id: str,
        lease_id: str,
        *,
        error: BaseException,
    ) -> None:
        message = str(error) or error.__class__.__name__
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    UPDATE jobs
                    SET state = ?, updated_at = ?, claimed_by = NULL, lease_id = NULL,
                        lease_expires_at = NULL, error = ?
                    WHERE job_id = ? AND lease_id = ? AND state IN (?, ?)
                    """,
                    (
                        str(JobState.FAILED),
                        utc_now_iso(),
                        message,
                        job_id,
                        lease_id,
                        str(JobState.CLAIMED),
                        str(JobState.RUNNING),
                    ),
                )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise

    def _event_rows_for_dataset(
        self,
        conn: sqlite3.Connection,
        request: DatasetCreateRequest,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[object] = []
        if request.query.event_types:
            clauses.append("event_type IN (%s)" % ",".join("?" for _ in request.query.event_types))
            params.extend(request.query.event_types)
        if request.query.status:
            clauses.append("status IN (%s)" % ",".join("?" for _ in request.query.status))
            params.extend(request.query.status)
        if request.query.reward_min is not None:
            clauses.append("reward >= ?")
            params.append(request.query.reward_min)
        if request.query.policy_version:
            clauses.append("policy_version = ?")
            params.append(request.query.policy_version)
        where = " AND ".join(clauses) if clauses else "1 = 1"
        return conn.execute(
            f"SELECT * FROM events WHERE {where} ORDER BY ingested_at, event_id LIMIT ?",
            (*params, request.limits.max_events),
        ).fetchall()

    def _read_event_payload_file(self, row: dict[str, Any]) -> dict[str, Any]:
        event_id = str(row["event_id"])
        payload_path = Path(str(row["payload_path"]))
        try:
            payload_text = payload_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ValueError(f"event {event_id} payload file is missing: {payload_path}") from exc
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"event {event_id} payload file is not valid JSON: {payload_path}"
            ) from exc

        if not isinstance(payload, dict):
            return {}
        return payload

    def _traces_from_event_payload(self, event_payload: dict[str, Any]) -> list[Any]:
        session_result = event_payload.get("session_result")
        if not isinstance(session_result, dict):
            return []
        trajectory = session_result.get("trajectory")
        if not isinstance(trajectory, dict):
            return []
        traces = trajectory.get("traces")
        if not isinstance(traces, list):
            return []
        return traces

    def _dataset_record_for_event_row(self, row: dict[str, Any]) -> dict[str, Any]:
        payload = self._read_event_payload_file(row)
        event_payload = payload.get("payload")
        if not isinstance(event_payload, dict):
            event_payload = {}
        _sanitize_human_feedback_in_event_payload(event_payload)
        traces = self._traces_from_event_payload(event_payload)
        return {
            "event_id": row["event_id"],
            "source": row["source"],
            "event_type": row["event_type"],
            "source_event_id": row["source_event_id"],
            "created_at": row["created_at"],
            "ingested_at": row["ingested_at"],
            "task_id": row["task_id"],
            "session_id": row["session_id"],
            "policy_version": row["policy_version"],
            "rollout_step": row["rollout_step"],
            "agent_harness": row["agent_harness"],
            "agent_model": row["agent_model"],
            "base_model": row["base_model"],
            "status": row["status"],
            "reward": row["reward"],
            "trace_count": len(traces),
            "traces": traces,
            "payload": event_payload,
        }

    def _trace_count_for_event_row(self, row: dict[str, Any]) -> int:
        return int(self._dataset_record_for_event_row(row)["trace_count"])

    def create_dataset(self, request: DatasetCreateRequest) -> DatasetCreateResponse:
        raw_payload = request.model_dump(mode="python")
        _validate_finite_floats(raw_payload["query"], "query")
        _validate_finite_floats(raw_payload["limits"], "limits")
        if request.query.task_tags:
            raise ValueError("query.task_tags is not supported until events store task tags")

        request_payload = request.model_dump(mode="json")
        query_json = _json_dumps(request_payload["query"])

        with self.connect() as conn:
            rows = [dict(row) for row in self._event_rows_for_dataset(conn, request)]

        event_ids: list[str] = []
        dataset_records: list[dict[str, Any]] = []
        trace_count = 0
        for row in rows:
            if trace_count >= request.limits.max_traces:
                break
            record = self._dataset_record_for_event_row(row)
            trace_count += int(record["trace_count"])
            event_ids.append(str(record["event_id"]))
            dataset_records.append(record)

        for _ in range(MAX_DATASET_ID_ATTEMPTS):
            dataset_id = new_id("ds")
            created_at = utc_now_iso()
            manifest_path = self.files.dataset_manifest_path(dataset_id)
            records_path = manifest_path.with_name("records.jsonl")
            manifest = {
                "dataset_id": dataset_id,
                "name": request_payload["name"],
                "purpose": request_payload["purpose"],
                "query": request_payload["query"],
                "limits": request_payload["limits"],
                "event_ids": event_ids,
                "event_count": len(event_ids),
                "trace_count": trace_count,
                "records_path": records_path.name,
                "records_uri": records_path.as_uri(),
            }
            _validate_finite_floats(manifest, "manifest")

            manifest_created = False
            records_created = False
            with self.connect() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    existing = conn.execute(
                        "SELECT 1 FROM datasets WHERE dataset_id = ?",
                        (dataset_id,),
                    ).fetchone()
                    if existing is not None or manifest_path.exists() or records_path.exists():
                        conn.rollback()
                        continue
                    conn.execute(
                        """
                        INSERT INTO datasets (
                            dataset_id, name, purpose, state, created_at, query_json,
                            manifest_path, event_count, trace_count, artifact_id
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            dataset_id,
                            request_payload["name"],
                            request_payload["purpose"],
                            "active",
                            created_at,
                            query_json,
                            str(manifest_path),
                            len(event_ids),
                            trace_count,
                            None,
                        ),
                    )
                    conn.executemany(
                        "INSERT INTO dataset_events (dataset_id, event_id) VALUES (?, ?)",
                        [(dataset_id, event_id) for event_id in event_ids],
                    )
                    try:
                        _write_json_strict_exclusive(self.files, manifest_path, manifest)
                        manifest_created = True
                        _write_jsonl_strict_exclusive(self.files, records_path, dataset_records)
                        records_created = True
                    except FileExistsError:
                        conn.rollback()
                        if manifest_created:
                            manifest_path.unlink(missing_ok=True)
                        continue
                    conn.commit()
                except Exception:
                    try:
                        conn.rollback()
                    except sqlite3.Error:
                        pass
                    if records_created:
                        try:
                            records_path.unlink(missing_ok=True)
                        except OSError:
                            pass
                    if manifest_created:
                        try:
                            manifest_path.unlink(missing_ok=True)
                        except OSError:
                            pass
                    raise

            try:
                artifact = self.register_artifact(
                    ArtifactRegisterRequest(
                        type=ArtifactType.DATASET,
                        name=request.name,
                        uri=manifest_path.as_uri(),
                        manifest=manifest,
                        lineage={"event_ids": event_ids},
                        compatibility={"purpose": request.purpose},
                        tags=[request.purpose],
                        promoted=True,
                    )
                )
            except Exception:
                self._cleanup_dataset_create_failure(dataset_id, manifest_path)
                raise

            try:
                self._backfill_dataset_artifact_id(dataset_id, artifact.artifact_id)
            except Exception:
                self._cleanup_dataset_create_failure(
                    dataset_id,
                    manifest_path,
                    artifact_id=artifact.artifact_id,
                )
                raise

            return DatasetCreateResponse(
                dataset_id=dataset_id,
                artifact_id=artifact.artifact_id,
                event_count=len(event_ids),
                trace_count=trace_count,
            )
        raise RuntimeError("could not allocate unique dataset id")

    def _backfill_dataset_artifact_id(self, dataset_id: str, artifact_id: str) -> None:
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE datasets SET artifact_id = ? WHERE dataset_id = ?",
                    (artifact_id, dataset_id),
                )
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise

    def _cleanup_dataset_create_failure(
        self,
        dataset_id: str,
        dataset_manifest_path: Path,
        *,
        artifact_id: str | None = None,
    ) -> None:
        artifact_manifest_path: Path | None = None
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if artifact_id is not None:
                artifact_manifest_path = self.files.artifact_manifest_path(
                    str(ArtifactType.DATASET),
                    artifact_id,
                )
                artifact_row = conn.execute(
                    "SELECT manifest_path FROM artifacts WHERE artifact_id = ?",
                    (artifact_id,),
                ).fetchone()
                if artifact_row is not None:
                    artifact_manifest_path = Path(str(artifact_row["manifest_path"]))
                conn.execute(
                    """
                    DELETE FROM artifact_lineage
                    WHERE parent_artifact_id = ? OR child_artifact_id = ?
                    """,
                    (artifact_id, artifact_id),
                )
                conn.execute("DELETE FROM artifacts WHERE artifact_id = ?", (artifact_id,))
            conn.execute("DELETE FROM dataset_events WHERE dataset_id = ?", (dataset_id,))
            conn.execute("DELETE FROM datasets WHERE dataset_id = ?", (dataset_id,))
            conn.commit()

        dataset_manifest_path.unlink(missing_ok=True)
        dataset_manifest_path.with_name("records.jsonl").unlink(missing_ok=True)
        if artifact_manifest_path is not None:
            artifact_manifest_path.unlink(missing_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()


def _artifact_id_allowed(
    row: dict[str, object],
    requested_artifact_ids: set[str] | None,
) -> bool:
    if requested_artifact_ids is None:
        return True
    if artifact_type(row) == ArtifactType.PARAMETRIC_MEMORY:
        return True
    artifact_id = row.get("artifact_id")
    return isinstance(artifact_id, str) and artifact_id in requested_artifact_ids


def _artifact_response_from_row(row: sqlite3.Row) -> ArtifactResponse:
    manifest_path = Path(str(row["manifest_path"]))
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = manifest_payload.get("manifest") if isinstance(manifest_payload, dict) else {}
    if not isinstance(manifest, dict):
        manifest = {}
    return ArtifactResponse(
        artifact_id=str(row["artifact_id"]),
        type=row["type"],
        name=str(row["name"]),
        version=int(row["version"]),
        state=row["state"],
        uri=str(row["uri"]),
        manifest=manifest,
        compatibility=json.loads(str(row["compatibility_json"])),
        scores=json.loads(str(row["scores_json"])),
        tags=json.loads(str(row["tags_json"])),
        promoted=bool(row["promoted"]),
    )


def _review_request_response_from_row(row: sqlite3.Row) -> ReviewRequestResponse:
    return ReviewRequestResponse(
        review_id=str(row["review_id"]),
        review_type=str(row["review_type"]),
        status=str(row["status"]),
        artifact_ids=json.loads(str(row["artifact_ids_json"])),
        candidate_ids=json.loads(str(row["candidate_ids_json"])),
        job_id=row["job_id"],
        task_id=row["task_id"],
        round_index=row["round_index"],
        method=row["method"],
        artifact_type=row["artifact_type"],
        packet_id=str(row["packet_id"]),
        packet_hash=str(row["packet_hash"]),
        packet=json.loads(str(row["packet_json"])),
        artifact_hashes=json.loads(str(row["artifact_hashes_json"])),
        query_decision_id=row["query_decision_id"],
        assigned_to=row["assigned_to"],
        reviewer_role=row["reviewer_role"],
        adjudication_rationale=row["adjudication_rationale"],
        priority=int(row["priority"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _review_packet_response_from_row(row: sqlite3.Row) -> ReviewPacketResponse:
    return ReviewPacketResponse(
        packet_id=str(row["packet_id"]),
        packet_hash=str(row["packet_hash"]),
        packet=json.loads(str(row["packet_json"])),
        created_at=str(row["created_at"]),
    )


def _human_feedback_response_from_row(row: sqlite3.Row) -> HumanFeedbackResponse:
    return HumanFeedbackResponse(
        feedback_id=str(row["feedback_id"]),
        review_id=str(row["review_id"]),
        reviewer_id=str(row["reviewer_id"]),
        reviewer_role=row["reviewer_role"],
        status=str(row["status"]),
        decision=str(row["decision"]),
        score=row["score"],
        confidence=row["confidence"],
        rationale=str(row["rationale"]),
        normalized_payload=json.loads(str(row["normalized_payload_json"])),
        created_at=str(row["created_at"]),
    )


def _feedback_application_response_from_row(row: sqlite3.Row) -> FeedbackApplicationResponse:
    return FeedbackApplicationResponse(
        application_id=str(row["application_id"]),
        feedback_id=str(row["feedback_id"]),
        target_type=str(row["target_type"]),
        target_id=str(row["target_id"]),
        consumed_by_method=str(row["consumed_by_method"]),
        consumed_in_job_id=row["consumed_in_job_id"],
        effect_summary=str(row["effect_summary"]),
        created_at=str(row["created_at"]),
    )


def _human_query_decision_response_from_row(row: sqlite3.Row) -> HumanQueryDecisionResponse:
    feedback_changed_promotion = row["feedback_changed_promotion"]
    feedback_changed_next_candidate = row["feedback_changed_next_candidate"]
    return HumanQueryDecisionResponse(
        query_decision_id=str(row["query_decision_id"]),
        artifact_ids=json.loads(str(row["artifact_ids_json"])),
        candidate_ids=json.loads(str(row["candidate_ids_json"])),
        task_id=row["task_id"],
        round_index=row["round_index"],
        method=row["method"],
        decision=str(row["decision"]),
        reason_codes=json.loads(str(row["reason_codes_json"])),
        estimated_value_of_information=row["estimated_value_of_information"],
        estimated_human_cost=row["estimated_human_cost"],
        budget_context=json.loads(str(row["budget_context_json"])),
        actual_latency_seconds=row["actual_latency_seconds"],
        feedback_changed_promotion=(
            None if feedback_changed_promotion is None else bool(feedback_changed_promotion)
        ),
        feedback_changed_next_candidate=(
            None
            if feedback_changed_next_candidate is None
            else bool(feedback_changed_next_candidate)
        ),
        downstream_delta=row["downstream_delta"],
        review_id=row["review_id"],
        created_at=str(row["created_at"]),
    )
