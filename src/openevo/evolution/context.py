from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlparse

from openevo.evolution.models import ArtifactType


_SUBSCRIPTION_AUTH_MODES = {"subscription", "chatgpt_subscription"}


class ContextCompatibilityRequest(Protocol):
    agent: dict[str, Any]
    base_model: str | None
    metadata: dict[str, Any]


def _json_object(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _compatibility_object(value: object) -> dict[str, Any] | None:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _string_items(value: object) -> list[str] | None:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list | tuple | set):
        return None

    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return None
        items.append(item)
    return items


def _normalized_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized or None


def request_auth_mode(request: ContextCompatibilityRequest) -> str | None:
    settings = request.agent.get("settings")
    if isinstance(settings, dict):
        value = _normalized_text(settings.get("auth_mode"))
        if value:
            return value

    for value in (
        request.agent.get("auth"),
        request.agent.get("auth_mode"),
        request.metadata.get("auth_mode"),
    ):
        normalized = _normalized_text(value)
        if normalized:
            return normalized

    evolution_metadata = request.metadata.get("evolution")
    if isinstance(evolution_metadata, dict):
        return _normalized_text(evolution_metadata.get("auth_mode"))
    return None


def request_uses_subscription_auth(request: ContextCompatibilityRequest) -> bool:
    return request_auth_mode(request) in _SUBSCRIPTION_AUTH_MODES


def requested_context_artifact_ids(
    request: ContextCompatibilityRequest,
) -> set[str] | None:
    ordered = requested_context_artifact_order(request)
    return None if ordered is None else set(ordered)


def requested_context_artifact_order(
    request: ContextCompatibilityRequest,
) -> tuple[str, ...] | None:
    evolution_metadata = request.metadata.get("evolution")
    if not isinstance(evolution_metadata, dict):
        return None

    if "context_artifact_ids" not in evolution_metadata:
        return None
    raw_values = evolution_metadata.get("context_artifact_ids")
    if not isinstance(raw_values, (list, tuple)) or len(raw_values) > 256:
        raise ValueError("requested context artifact inventory is invalid")
    values: list[str] = []
    for value in raw_values:
        if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 256:
            raise ValueError("requested context artifact inventory is invalid")
        values.append(value)
    if len(values) != len(set(values)):
        raise ValueError("requested context artifact inventory contains duplicates")
    return tuple(values)


def artifact_matches(
    request: ContextCompatibilityRequest,
    row: dict[str, object],
) -> bool:
    compatibility = _compatibility_object(row.get("compatibility_json"))
    if compatibility is None:
        return False

    task_tags = _string_items(request.metadata.get("task_tags"))
    required_tags = _string_items(compatibility.get("task_tags"))
    if task_tags is None or required_tags is None:
        return False
    if required_tags and not set(task_tags).intersection(required_tags):
        return False

    compatible_base_models = _string_items(compatibility.get("base_model"))
    if compatible_base_models is None:
        return False
    if compatible_base_models:
        if request.base_model not in compatible_base_models:
            return False

    harnesses = _string_items(compatibility.get("agent_harness"))
    if harnesses is None:
        return False
    harness = request.agent.get("harness")
    if harnesses and harness not in harnesses:
        return False

    auth_modes = _string_items(compatibility.get("auth_mode"))
    if auth_modes is None:
        return False
    if auth_modes:
        auth_mode = request_auth_mode(request)
        if auth_mode is None or auth_mode not in {mode.lower() for mode in auth_modes}:
            return False
    return True


def read_file_uri_text(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return ""
    path = Path(unquote(parsed.path))
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def artifact_score(row: dict[str, object]) -> float:
    scores = _json_object(row.get("scores_json"))
    for key in ("quality", "heldout_reward_delta"):
        if key not in scores:
            continue
        try:
            score = float(scores[key])
        except (TypeError, ValueError):
            return 0.0
        if math.isfinite(score):
            return score
        return 0.0
    return 0.0


def sort_candidates(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    return sorted(
        rows,
        key=lambda row: (
            artifact_score(row),
            str(row.get("created_at") or ""),
            str(row.get("artifact_id") or ""),
        ),
        reverse=True,
    )


def artifact_type(row: dict[str, object]) -> ArtifactType:
    return ArtifactType(str(row["type"]))


def artifact_manifest(row: dict[str, object]) -> dict[str, Any]:
    manifest_path = row.get("manifest_path")
    if not manifest_path:
        return {}
    try:
        payload = json.loads(Path(str(manifest_path)).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    manifest = payload.get("manifest")
    if not isinstance(manifest, dict):
        return {}
    return manifest


def artifact_scores(row: dict[str, object]) -> dict[str, Any]:
    return _json_object(row.get("scores_json"))
