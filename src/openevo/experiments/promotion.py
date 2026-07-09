from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from typing import Any
from urllib.parse import unquote, urlparse, urlunparse

from .models import PROMOTION_SUPPORT_FIELDS

PromotionReviewer = Callable[[dict[str, Any]], dict[str, Any]]
HumanPromotionInput = Callable[[dict[str, Any]], dict[str, Any]]

_FEEDBACK_SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|access[_-]?key(?:[_-]?id)?|accesskeyid|token|password|secret|"
    r"authorization)",
    re.IGNORECASE,
)
_FEEDBACK_URI_RE = re.compile(
    r"\b[A-Za-z][A-Za-z0-9+.-]*://(?:<redacted>|[^\s\"'<>])+",
    re.IGNORECASE,
)
_FEEDBACK_RELATIVE_URI_REF_RE = re.compile(
    r"(?<![\w:/])(?:[A-Za-z0-9._~!$&'()*+,;=@%-]+/)*"
    r"[A-Za-z0-9._~!$&'()*+,;=@%-]+[?#](?:<redacted>|[^\s\"'<>])+"
)
_FEEDBACK_POSIX_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![\w:/])/(?!/)(?:[^\s,;:/]+/)*[^\s,;]+"
)
_FEEDBACK_WINDOWS_UNC_PATH_RE = re.compile(
    r"\\\\[^\s\\/:*?\"<>|,;]+\\(?:[^\\/:*?\"<>|\r\n,;]+\\)*[^\s\\/:*?\"<>|,;]+"
)
_FEEDBACK_WINDOWS_ABSOLUTE_PATH_RE = re.compile(
    r"\b[A-Za-z]:[\\/](?:[^\\/:*?\"<>|\r\n,;]+[\\/])*[^\s\\/:*?\"<>|,;]+"
)
_FEEDBACK_SECRET_ASSIGNMENT_RE = re.compile(
    r"\b[A-Za-z0-9_]*(?:"
    r"api[_-]?key|access[_-]?key(?:[_-]?id)?|accesskeyid|token|password|secret|"
    r"authorization"
    r")[A-Za-z0-9_]*\s*[:=]\s*(?:bearer|basic)?\s*[^\s,;]+",
    re.IGNORECASE,
)
_FEEDBACK_AUTHORIZATION_VALUE_RE = re.compile(
    r"\bAuthorization\s*:\s*(?:Bearer|Basic)?\s*[^\s,;]+",
    re.IGNORECASE,
)
_FEEDBACK_BEARER_VALUE_RE = re.compile(
    r"\bBearer\s+[A-Za-z0-9._~+/=-]+",
    re.IGNORECASE,
)
_FEEDBACK_SENSITIVE_SCHEME_VALUE_RE = re.compile(
    r"\b(?:bearer|basic)\s*:\s*[^\s,;]+",
    re.IGNORECASE,
)
_FEEDBACK_AWS_ACCESS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{12,}\b")
_LOCAL_REVIEW_PACKET_FIELDS = {"review_path", "decision_path"}
_LOCAL_ARTIFACT_URI_LABEL = "[LOCAL_ARTIFACT_URI]"
_LOCAL_ARTIFACT_PATH_LABEL = "[LOCAL_ARTIFACT_PATH]"
_REDACTED_LABEL = "[REDACTED]"
_RESUMABLE_REVIEW_REQUEST_STATUSES = {
    "submitted",
    "validated",
    "adjudicated",
    "resolved",
}
_REJECTED_REVIEW_REQUEST_STATUSES = {
    "stale",
    "rejected_invalid",
    "archived_only",
}


def evaluate_promotion_gate(
    *,
    gate_config: Mapping[str, Any],
    artifact_type: str,
    method: str,
    task_id: str,
    round_index: int,
    job_id: str,
    job_payload: Mapping[str, Any],
    artifacts: Sequence[Mapping[str, Any]],
    output_root: Path,
    content_roots: Sequence[Path] | None = None,
    reviewer: PromotionReviewer | None = None,
    human_input: HumanPromotionInput | None = None,
) -> dict[str, Any]:
    mode = str(gate_config.get("mode") or "none")
    if mode == "none" or not _gate_targets_artifact(gate_config, artifact_type):
        return {
            "status": "skipped",
            "approved_artifact_ids": [
                str(artifact["artifact_id"])
                for artifact in artifacts
                if isinstance(artifact.get("artifact_id"), str)
            ],
            "reviews": [],
        }
    if mode not in {"human", "llm"}:
        raise ValueError(f"unsupported promotion gate mode: {mode}")

    reviews: list[dict[str, Any]] = []
    approved_artifact_ids: list[str] = []
    human_decisions_to_read: list[tuple[str, Path, Path, dict[str, Any]]] = []
    review_root = _review_root(gate_config, output_root)
    review_root.mkdir(parents=True, exist_ok=True)
    decision_root = _decision_root(gate_config, review_root)
    if mode == "human":
        decision_root.mkdir(parents=True, exist_ok=True)

    llm_reviewer = reviewer
    if mode == "llm" and llm_reviewer is None:
        llm_reviewer = LlmPromotionReviewer(gate_config)

    for artifact in artifacts:
        artifact_id = artifact.get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id:
            continue
        if str(artifact.get("type") or "") != artifact_type:
            reviews.append(
                {
                    "artifact_id": artifact_id,
                    "status": "skipped",
                    "failure_codes": ["artifact_type_not_targeted"],
                }
            )
            continue

        support = _promotion_support(artifact, job_payload)
        packet = _review_packet(
            gate_config=gate_config,
            mode=mode,
            artifact_type=artifact_type,
            method=method,
            task_id=task_id,
            round_index=round_index,
            job_id=job_id,
            job_payload=job_payload,
            artifact=artifact,
            support=support,
            content_roots=content_roots,
        )
        review_path = review_root / f"{_safe_component(task_id)}-round-{round_index}-{artifact_id}.json"
        missing_support = _missing_support_fields(support, gate_config)
        if missing_support:
            packet["decision"] = {
                "status": "rejected",
                "failure_codes": [f"missing_support:{field}" for field in missing_support],
                "rationale": "promotion_support is missing required algorithm support fields",
            }
            _write_json(review_path, packet)
            reviews.append(
                {
                    "artifact_id": artifact_id,
                    "status": "rejected",
                    "_review_path": str(review_path),
                    "review_path": review_path.name,
                    "failure_codes": packet["decision"]["failure_codes"],
                    "rationale": packet["decision"]["rationale"],
                }
            )
            continue

        if mode == "human":
            decision_path = decision_root / f"{artifact_id}.decision.json"
            _write_json(review_path, packet)
            human_decisions_to_read.append((artifact_id, review_path, decision_path, packet))
            continue
        else:
            assert llm_reviewer is not None
            _write_json(review_path, packet)
            decision = _llm_decision(
                llm_reviewer(normalize_review_packet(packet)),
                min_score=_float_config(gate_config.get("min_score"), 0.7),
            )

        decision["_review_path"] = str(review_path)
        decision["review_path"] = review_path.name
        reviews.append({"artifact_id": artifact_id, **decision})
        if decision["status"] == "approved":
            approved_artifact_ids.append(artifact_id)

    for artifact_id, review_path, decision in _wait_for_human_decisions(
        human_decisions_to_read,
        gate_config,
        human_input=human_input,
    ):
        decision["_review_path"] = str(review_path)
        decision["review_path"] = review_path.name
        reviews.append({"artifact_id": artifact_id, **decision})
        if decision["status"] == "approved":
            approved_artifact_ids.append(artifact_id)

    status = _aggregate_review_status(reviews)
    if status == "missing_target_artifact":
        reviews.insert(
            0,
            {
                "artifact_id": None,
                "status": "rejected",
                "failure_codes": [f"missing_target_artifact:{artifact_type}"],
                "rationale": f"promotion gate did not receive a {artifact_type} artifact",
            }
        )
        status = "rejected"
    return {
        "status": status,
        "approved_artifact_ids": approved_artifact_ids,
        "reviews": reviews,
    }


class LlmPromotionReviewer:
    def __init__(self, gate_config: Mapping[str, Any]) -> None:
        llm = gate_config.get("llm")
        self.llm_config = _normalize_llm_config(llm if isinstance(llm, Mapping) else {})

    def __call__(self, packet: dict[str, Any]) -> dict[str, Any]:
        from openevo.evolution.methods import _generate_reflector_markdown

        prompt = _render_llm_review_prompt(packet)
        content = _generate_reflector_markdown(
            prompt,
            self.llm_config,
            system_message=(
                "You are a promotion gate reviewer for evolved agent artifacts. "
                "Return only a JSON object."
            ),
            codex_prompt=prompt,
            error_context="promotion_gate",
            temp_prefix="openevo-promotion-gate-",
        )
        return _parse_json_object(content)


def review_packet_hash(packet: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        normalize_review_packet(packet),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def normalize_review_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    normalized = {
        str(key): value
        for key, value in packet.items()
        if key not in _LOCAL_REVIEW_PACKET_FIELDS
    }
    normalized.setdefault("trusted_metadata", {})
    normalized.setdefault("untrusted_artifact_excerpts", [])
    normalized.setdefault("promotion_support", {})
    normalized.setdefault("questions", [])
    return normalized


def review_request_payload_from_packet(
    packet: Mapping[str, Any],
    *,
    artifact_ids: Sequence[str] | None = None,
    artifact_hashes: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    packet_artifact_ids = list(artifact_ids or _artifact_ids_from_review_packet(packet))
    normalized_packet = normalize_review_packet(packet)
    job = packet.get("job")
    payload = {
        "review_type": "promotion",
        "artifact_ids": packet_artifact_ids,
        "candidate_ids": [],
        "job_id": job.get("job_id") if isinstance(job, Mapping) else None,
        "task_id": _optional_text(packet.get("task_id")),
        "round_index": packet.get("round_index")
        if isinstance(packet.get("round_index"), int)
        else None,
        "method": _optional_text(packet.get("method")),
        "artifact_type": _optional_text(packet.get("artifact_type")),
        "packet": normalized_packet,
        "artifact_hashes": dict(artifact_hashes or {}),
    }
    return payload


def artifact_hashes_from_review_packet(
    packet: Mapping[str, Any],
    *,
    artifact_ids: Sequence[str] | None = None,
) -> dict[str, str]:
    packet_artifact_ids = list(artifact_ids or _artifact_ids_from_review_packet(packet))
    if not packet_artifact_ids:
        return {}
    normalized_packet = normalize_review_packet(packet)
    artifact = normalized_packet.get("artifact")
    artifact_content = normalized_packet.get("artifact_content")
    artifact_hash = _review_artifact_payload_hash(
        artifact=artifact if isinstance(artifact, Mapping) else {},
        artifact_content=artifact_content if isinstance(artifact_content, Mapping) else {},
    )
    return {artifact_id: artifact_hash for artifact_id in packet_artifact_ids}


def current_review_artifact_hash(
    artifact: Mapping[str, Any],
    *,
    gate_config: Mapping[str, Any] | None = None,
    content_roots: Sequence[Path] | None = None,
) -> str:
    artifact_metadata = {
        str(key): value
        for key, value in artifact.items()
        if key
        not in {
            "artifact_content",
            "current_artifact_hash",
            "review_hash",
            "artifact_hash",
        }
    }
    sanitized_artifact = _sanitize_artifact_metadata(dict(artifact_metadata))
    artifact_content: Mapping[str, Any] = {}
    if content_roots is not None:
        artifact_content = _artifact_content_packet(
            artifact,
            gate_config or {},
            content_roots=content_roots,
        )
        if _review_artifact_content_identity(artifact_content):
            return _review_artifact_payload_hash(
                artifact=sanitized_artifact
                if isinstance(sanitized_artifact, Mapping)
                else {},
                artifact_content=artifact_content,
            )
    for key in ("current_artifact_hash", "review_hash", "artifact_hash"):
        value = artifact.get(key)
        if isinstance(value, str) and value.startswith("sha256:"):
            return value
    return _review_artifact_payload_hash(
        artifact=sanitized_artifact if isinstance(sanitized_artifact, Mapping) else {},
        artifact_content=artifact_content,
    )


def _review_artifact_payload_hash(
    *,
    artifact: Mapping[str, Any],
    artifact_content: Mapping[str, Any],
) -> str:
    artifact_payload = {
        "artifact": dict(artifact),
    }
    content_identity = _review_artifact_content_identity(artifact_content)
    if content_identity:
        artifact_payload["artifact_content"] = content_identity
    encoded = json.dumps(
        artifact_payload,
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _review_artifact_content_identity(
    artifact_content: Mapping[str, Any],
) -> dict[str, Any]:
    content_sha256 = artifact_content.get("content_sha256")
    if not isinstance(content_sha256, str) or not content_sha256.startswith("sha256:"):
        return {}
    identity: dict[str, Any] = {"content_sha256": content_sha256}
    source_uri = artifact_content.get("source_uri")
    if isinstance(source_uri, str) and source_uri:
        identity["source_uri"] = source_uri
    return identity


def decision_from_backend_feedback(feedback: Mapping[str, Any]) -> dict[str, Any]:
    decision = str(feedback.get("decision") or "").strip().lower()
    score = _optional_contract_score(feedback.get("score"))
    score_in_contract = score is None or (math.isfinite(score) and 0.0 <= score <= 1.0)
    approved = decision == "approve" and score_in_contract
    failure_codes: list[str] = []
    if decision != "approve":
        failure_codes.append(_backend_feedback_failure_code(decision))
    if not score_in_contract:
        failure_codes.append("score_outside_contract")
    result = {
        "status": "approved" if approved else "rejected",
        "score": score if score is None or math.isfinite(score) else None,
        "confidence": _contract_score_or_none(feedback.get("confidence")),
        "failure_codes": failure_codes,
        "rationale": _sanitize_rationale(feedback.get("rationale")),
    }
    for key in ("feedback_id", "review_id"):
        value = feedback.get(key)
        if isinstance(value, str) and value:
            result[key] = value
    human_feedback = _sanitize_feedback_payload(
        _normalize_human_feedback(feedback.get("normalized_payload"))
    )
    if human_feedback:
        result["human_feedback"] = human_feedback
    return result


def resume_promotion_from_review_feedback(
    *,
    gate_config: Mapping[str, Any],
    artifact_type: str,
    artifacts: Sequence[Mapping[str, Any]],
    review_requests: Sequence[Mapping[str, Any]],
    feedback_by_review: Mapping[str, Sequence[Mapping[str, Any]]],
    content_roots: Sequence[Path] | None = None,
) -> dict[str, Any]:
    target_artifact_ids = [
        str(artifact["artifact_id"])
        for artifact in artifacts
        if artifact.get("type") == artifact_type
        and isinstance(artifact.get("artifact_id"), str)
        and artifact.get("artifact_id")
    ]
    artifacts_by_id = {
        str(artifact["artifact_id"]): artifact
        for artifact in artifacts
        if artifact.get("type") == artifact_type
        and isinstance(artifact.get("artifact_id"), str)
        and artifact.get("artifact_id")
    }
    if not _gate_targets_artifact(gate_config, artifact_type):
        return {
            "status": "skipped",
            "approved_artifact_ids": target_artifact_ids,
            "reviews": [],
        }
    if not target_artifact_ids:
        return {
            "status": "rejected",
            "approved_artifact_ids": [],
            "reviews": [
                {
                    "artifact_id": None,
                    "status": "rejected",
                    "failure_codes": [f"missing_target_artifact:{artifact_type}"],
                    "rationale": (
                        f"promotion resume did not receive a {artifact_type} artifact"
                    ),
                }
            ],
        }

    target_artifact_id_set = set(target_artifact_ids)
    reviews: list[dict[str, Any]] = []
    approved_artifact_ids: list[str] = []
    for request in review_requests:
        if not isinstance(request, Mapping):
            continue
        review_id = request.get("review_id")
        if not isinstance(review_id, str) or not review_id:
            continue
        packet = request.get("packet")
        if not _review_request_targets_artifact_type(request, packet, artifact_type):
            continue
        request_artifact_ids = [
            artifact_id
            for artifact_id in _string_list(request.get("artifact_ids"))
            if artifact_id in target_artifact_id_set
        ]
        if not request_artifact_ids:
            continue

        blocker = _review_request_resume_blocker(request.get("status"))
        if blocker is not None:
            reviews.append(
                {
                    "review_id": review_id,
                    "artifact_ids": request_artifact_ids,
                    "artifact_id": request_artifact_ids[0]
                    if len(request_artifact_ids) == 1
                    else None,
                    "status": blocker["status"],
                    "failure_codes": [
                        f"review_request_not_resumable:{blocker['review_status']}"
                    ],
                    "rationale": (
                        "review request is not in a post-review state that can "
                        f"resume promotion: {blocker['review_status']}"
                    ),
                }
            )
            continue

        hash_blocker = _review_request_artifact_hash_blocker(
            request,
            request_artifact_ids=request_artifact_ids,
            artifacts_by_id=artifacts_by_id,
            gate_config=gate_config,
            content_roots=content_roots,
        )
        if hash_blocker is not None:
            reviews.append(
                {
                    "review_id": review_id,
                    "artifact_ids": request_artifact_ids,
                    "artifact_id": request_artifact_ids[0]
                    if len(request_artifact_ids) == 1
                    else None,
                    **hash_blocker,
                }
            )
            continue

        feedback = _select_backend_feedback(feedback_by_review.get(review_id, []))
        if feedback is None:
            reviews.append(
                {
                    "review_id": review_id,
                    "artifact_ids": request_artifact_ids,
                    "artifact_id": request_artifact_ids[0]
                    if len(request_artifact_ids) == 1
                    else None,
                    "status": "pending_review",
                    "failure_codes": ["no_available_human_feedback"],
                    "rationale": (
                        f"waiting for available backend feedback on review: {review_id}"
                    ),
                }
            )
            continue

        decision = decision_from_backend_feedback(feedback)
        decision["review_id"] = review_id
        decision["artifact_ids"] = request_artifact_ids
        if len(request_artifact_ids) == 1:
            decision["artifact_id"] = request_artifact_ids[0]
        reviews.append(decision)
        if decision["status"] == "approved":
            for artifact_id in request_artifact_ids:
                if artifact_id not in approved_artifact_ids:
                    approved_artifact_ids.append(artifact_id)

    return {
        "status": _aggregate_review_status(reviews) if reviews else "pending_review",
        "approved_artifact_ids": approved_artifact_ids,
        "reviews": reviews,
    }


def _gate_targets_artifact(gate_config: Mapping[str, Any], artifact_type: str) -> bool:
    artifact_types = gate_config.get("artifact_types")
    if not isinstance(artifact_types, Sequence) or isinstance(artifact_types, str):
        return False
    return artifact_type in artifact_types


def _artifact_ids_from_review_packet(packet: Mapping[str, Any]) -> list[str]:
    artifact = packet.get("artifact")
    if not isinstance(artifact, Mapping):
        return []
    artifact_id = artifact.get("artifact_id")
    if not isinstance(artifact_id, str) or not artifact_id:
        return []
    return [artifact_id]


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _backend_feedback_failure_code(decision: str) -> str:
    normalized = decision or "missing_decision"
    if normalized == "reject":
        return "human_rejected"
    if normalized == "revise":
        return "human_requested_revision"
    if normalized == "abstain":
        return "human_abstained"
    if normalized == "comment_only":
        return "human_comment_only"
    return f"human_feedback_{normalized}"


def _contract_score_or_none(value: object) -> float | None:
    score = _optional_contract_score(value)
    if score is None or not math.isfinite(score) or not 0.0 <= score <= 1.0:
        return None
    return score


def _review_request_targets_artifact_type(
    request: Mapping[str, Any],
    packet: object,
    artifact_type: str,
) -> bool:
    request_artifact_type = request.get("artifact_type")
    if isinstance(request_artifact_type, str) and request_artifact_type:
        return request_artifact_type == artifact_type
    if isinstance(packet, Mapping):
        packet_artifact_type = packet.get("artifact_type")
        if isinstance(packet_artifact_type, str) and packet_artifact_type:
            return packet_artifact_type == artifact_type
    return True


def _string_list(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _select_backend_feedback(
    feedback_items: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    for item in feedback_items:
        if not isinstance(item, Mapping):
            continue
        if item.get("status") == "available_for_evolution":
            return item
    return None


def _review_request_artifact_hash_blocker(
    request: Mapping[str, Any],
    *,
    request_artifact_ids: Sequence[str],
    artifacts_by_id: Mapping[str, Mapping[str, Any]],
    gate_config: Mapping[str, Any],
    content_roots: Sequence[Path] | None,
) -> dict[str, Any] | None:
    artifact_hashes = request.get("artifact_hashes")
    if not isinstance(artifact_hashes, Mapping):
        artifact_hashes = {}
    packet = request.get("packet")
    packet_has_artifact = (
        isinstance(packet, Mapping)
        and isinstance(packet.get("artifact"), Mapping)
    )
    packet_artifact_hashes = (
        artifact_hashes_from_review_packet(
            packet,
            artifact_ids=request_artifact_ids,
        )
        if packet_has_artifact
        else {}
    )
    missing_artifact_ids: list[str] = []
    inconsistent_artifact_ids: list[str] = []
    mismatched_artifact_ids: list[str] = []
    for artifact_id in request_artifact_ids:
        reviewed_hash = artifact_hashes.get(artifact_id)
        if not isinstance(reviewed_hash, str) or not reviewed_hash:
            missing_artifact_ids.append(artifact_id)
            continue
        packet_hash = packet_artifact_hashes.get(artifact_id)
        if (
            isinstance(packet_hash, str)
            and packet_hash
            and packet_hash != reviewed_hash
        ):
            inconsistent_artifact_ids.append(artifact_id)
            continue
        artifact = artifacts_by_id.get(artifact_id)
        current_hash = (
            current_review_artifact_hash(
                artifact,
                gate_config=gate_config,
                content_roots=content_roots,
            )
            if isinstance(artifact, Mapping)
            else None
        )
        if current_hash != reviewed_hash:
            mismatched_artifact_ids.append(artifact_id)
    if inconsistent_artifact_ids:
        return {
            "status": "rejected",
            "failure_codes": ["artifact_hash_packet_mismatch"],
            "rationale": (
                "review request artifact hash does not match its review packet: "
                + ", ".join(inconsistent_artifact_ids)
            ),
        }
    if mismatched_artifact_ids:
        return {
            "status": "rejected",
            "failure_codes": ["artifact_hash_mismatch"],
            "rationale": (
                "review request artifact hash does not match the current artifact: "
                + ", ".join(mismatched_artifact_ids)
            ),
        }
    if missing_artifact_ids:
        return {
            "status": "pending_review",
            "failure_codes": ["artifact_hash_missing"],
            "rationale": (
                "review request is missing artifact hashes for: "
                + ", ".join(missing_artifact_ids)
            ),
        }
    return None


def _review_request_resume_blocker(status: object) -> dict[str, str] | None:
    review_status = str(status or "").strip().lower() or "missing"
    if review_status in _RESUMABLE_REVIEW_REQUEST_STATUSES:
        return None
    if review_status in _REJECTED_REVIEW_REQUEST_STATUSES:
        return {"review_status": review_status, "status": "rejected"}
    return {"review_status": review_status, "status": "pending_review"}


def _review_root(gate_config: Mapping[str, Any], output_root: Path) -> Path:
    configured = gate_config.get("review_dir")
    if isinstance(configured, str) and configured.strip():
        return Path(configured).expanduser()
    return output_root / "promotion_reviews"


def _decision_root(gate_config: Mapping[str, Any], review_root: Path) -> Path:
    configured = gate_config.get("decision_dir")
    if isinstance(configured, str) and configured.strip():
        return Path(configured).expanduser()
    return review_root


def _promotion_support(
    artifact: Mapping[str, Any],
    job_payload: Mapping[str, Any],
) -> dict[str, Any]:
    manifest = artifact.get("manifest")
    if isinstance(manifest, Mapping):
        support = manifest.get("promotion_support")
        if isinstance(support, Mapping):
            return dict(support)
    config = job_payload.get("config")
    if isinstance(config, Mapping):
        support = config.get("promotion_support")
        if isinstance(support, Mapping):
            return dict(support)
    return {}


def _missing_support_fields(
    support: Mapping[str, Any],
    gate_config: Mapping[str, Any],
) -> list[str]:
    if not bool(gate_config.get("require_support", True)):
        return []
    return [
        field
        for field in PROMOTION_SUPPORT_FIELDS
        if not _support_field_present(support.get(field))
    ]


def _support_field_present(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Sequence) and not isinstance(value, str):
        return any(_support_field_present(item) for item in value)
    return value is not None


def _review_packet(
    *,
    gate_config: Mapping[str, Any],
    mode: str,
    artifact_type: str,
    method: str,
    task_id: str,
    round_index: int,
    job_id: str,
    job_payload: Mapping[str, Any],
    artifact: Mapping[str, Any],
    support: Mapping[str, Any],
    content_roots: Sequence[Path] | None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "gate": {
            "mode": mode,
            "min_score": gate_config.get("min_score"),
            "require_support": gate_config.get("require_support", True),
        },
        "task_id": task_id,
        "round_index": round_index,
        "artifact_type": artifact_type,
        "method": method,
        "job": {
            "job_id": job_id,
            "payload": _sanitize_review_boundary_payload(dict(job_payload)),
        },
        "artifact": _sanitize_artifact_metadata(dict(artifact)),
        "artifact_content": _artifact_content_packet(
            artifact,
            gate_config,
            content_roots=content_roots,
        ),
        "promotion_support": _sanitize_review_boundary_payload(dict(support)),
    }


def _artifact_content_packet(
    artifact: Mapping[str, Any],
    gate_config: Mapping[str, Any],
    *,
    content_roots: Sequence[Path] | None,
) -> dict[str, Any]:
    max_chars = _int_config(gate_config.get("max_artifact_content_chars"), 12000)
    uri = artifact.get("uri")
    if max_chars <= 0:
        return {
            "available": False,
            "source_uri": _sanitize_uri(uri) if isinstance(uri, str) else None,
            "reason": "disabled",
            "excerpts": [],
        }
    if not isinstance(uri, str) or not uri:
        return {
            "available": False,
            "source_uri": None,
            "reason": "missing_uri",
            "excerpts": [],
        }
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return {
            "available": False,
            "source_uri": _sanitize_uri(uri),
            "reason": "unsupported_uri_scheme",
            "excerpts": [],
        }
    path = Path(unquote(parsed.path))
    if not _path_is_within_allowed_roots(path, content_roots):
        return {
            "available": False,
            "source_uri": _sanitize_uri(uri),
            "reason": "uri_outside_allowed_roots",
            "excerpts": [],
        }
    try:
        content_sha256 = _artifact_content_sha256(path, artifact=artifact)
        excerpts, truncated = _read_artifact_excerpts(
            path,
            artifact=artifact,
            max_chars=max_chars,
        )
    except OSError as exc:
        return {
            "available": False,
            "source_uri": _sanitize_uri(uri),
            "reason": exc.__class__.__name__,
            "excerpts": [],
        }
    return {
        "available": bool(excerpts),
        "source_uri": _sanitize_uri(uri),
        "content_sha256": content_sha256,
        "truncated": truncated,
        "excerpts": excerpts,
    }


def _path_is_within_allowed_roots(
    path: Path,
    allowed_roots: Sequence[Path] | None,
) -> bool:
    if not allowed_roots:
        return False
    try:
        resolved_path = path.expanduser().resolve()
    except OSError:
        return False
    for root in allowed_roots:
        try:
            resolved_root = root.expanduser().resolve()
            resolved_path.relative_to(resolved_root)
        except (OSError, ValueError):
            continue
        return True
    return False


def _read_artifact_excerpts(
    path: Path,
    *,
    artifact: Mapping[str, Any],
    max_chars: int,
) -> tuple[list[dict[str, Any]], bool]:
    if path.is_file():
        text, truncated = _read_text_excerpt(path, max_chars)
        return (
            [
                {
                    "path": _sanitize_review_boundary_key(path.name),
                    "text": _sanitize_feedback_text(text),
                    "truncated": truncated,
                }
            ],
            truncated,
        )
    if not path.is_dir():
        raise FileNotFoundError(str(path))

    excerpts: list[dict[str, Any]] = []
    truncated = False
    remaining = max_chars
    root = path.resolve()
    for relative_path in _artifact_directory_content_paths(artifact):
        candidate = (root / relative_path).resolve()
        try:
            display_path = candidate.relative_to(root)
        except ValueError:
            continue
        if not candidate.is_file() or remaining <= 0:
            continue
        text, file_truncated = _read_text_excerpt(candidate, remaining)
        excerpts.append(
            {
                "path": _sanitize_review_boundary_key(display_path.as_posix()),
                "text": _sanitize_feedback_text(text),
                "truncated": file_truncated,
            }
        )
        remaining -= len(text)
        truncated = truncated or file_truncated or remaining <= 0
        if remaining <= 0:
            break
    return excerpts, truncated


def _artifact_content_sha256(path: Path, *, artifact: Mapping[str, Any]) -> str:
    digest = hashlib.sha256()
    if path.is_file():
        _update_digest_for_file(digest, path, display_path=path.name)
        return f"sha256:{digest.hexdigest()}"
    if not path.is_dir():
        raise FileNotFoundError(str(path))
    root = path.resolve()
    for relative_path in _artifact_directory_content_paths(artifact):
        candidate = (root / relative_path).resolve()
        try:
            display_path = candidate.relative_to(root)
        except ValueError:
            continue
        if candidate.is_file():
            _update_digest_for_file(
                digest,
                candidate,
                display_path=display_path.as_posix(),
            )
    return f"sha256:{digest.hexdigest()}"


def _update_digest_for_file(
    digest: Any,
    path: Path,
    *,
    display_path: str,
) -> None:
    digest.update(display_path.encode("utf-8"))
    digest.update(b"\0")
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    digest.update(b"\0")


def _artifact_directory_content_paths(artifact: Mapping[str, Any]) -> list[Path]:
    manifest = artifact.get("manifest")
    paths: list[str] = []
    if isinstance(manifest, Mapping):
        for key in ("content_path", "entrypoint"):
            value = manifest.get(key)
            if isinstance(value, str) and value.strip():
                paths.append(value.strip())
        files = manifest.get("files")
        if isinstance(files, Sequence) and not isinstance(files, str):
            paths.extend(str(item).strip() for item in files if str(item).strip())
    if not paths and artifact.get("type") == "skill_bundle":
        paths.append("SKILL.md")
    return [Path(path) for path in dict.fromkeys(paths)]


def _read_text_excerpt(path: Path, max_chars: int) -> tuple[str, bool]:
    with path.open(encoding="utf-8", errors="replace") as handle:
        text = handle.read(max_chars + 1)
    if len(text) > max_chars:
        return text[:max_chars], True
    return text, False


def _wait_for_human_decisions(
    pending_decisions: Sequence[tuple[str, Path, Path, dict[str, Any]]],
    gate_config: Mapping[str, Any],
    *,
    human_input: HumanPromotionInput | None,
) -> list[tuple[str, Path, dict[str, Any]]]:
    input_mode = str(gate_config.get("human_input") or "auto")
    if input_mode not in {"auto", "file", "tui"}:
        raise ValueError(f"unsupported promotion_gate.human_input: {input_mode}")
    if input_mode == "tui" and human_input is None and not _stdio_is_interactive():
        return [
            (
                artifact_id,
                review_path,
                {
                    "status": "pending_review",
                    "failure_codes": ["pending_human_review", "tui_unavailable"],
                    "rationale": "interactive human input requested but no TTY is available",
                },
            )
            for artifact_id, review_path, _decision_path, _packet in pending_decisions
        ]
    if input_mode == "tui" or (
        input_mode == "auto" and (human_input is not None or _stdio_is_interactive())
    ):
        return _prompt_for_human_decisions(pending_decisions, human_input=human_input)
    return _wait_for_human_decision_files(pending_decisions, gate_config)


def _wait_for_human_decision_files(
    pending_decisions: Sequence[tuple[str, Path, Path, dict[str, Any]]],
    gate_config: Mapping[str, Any],
) -> list[tuple[str, Path, dict[str, Any]]]:
    timeout_seconds = _float_config(gate_config.get("decision_timeout_seconds"), 300.0)
    poll_interval_seconds = _float_config(
        gate_config.get("decision_poll_interval_seconds"),
        2.0,
    )
    deadline = time.monotonic() + timeout_seconds
    latest_decisions: dict[str, dict[str, Any]] = {}
    still_pending = {artifact_id for artifact_id, _, _, _ in pending_decisions}
    while still_pending:
        for artifact_id, _review_path, decision_path, _packet in pending_decisions:
            if artifact_id not in still_pending:
                continue
            decision = _read_human_decision(decision_path)
            latest_decisions[artifact_id] = decision
            if decision["status"] != "pending_review":
                still_pending.remove(artifact_id)
        if not still_pending:
            break
        if timeout_seconds <= 0 or time.monotonic() >= deadline:
            break
        time.sleep(min(poll_interval_seconds, max(0.0, deadline - time.monotonic())))
    return [
        (artifact_id, review_path, latest_decisions[artifact_id])
        for artifact_id, review_path, _decision_path, _packet in pending_decisions
    ]


def _prompt_for_human_decisions(
    pending_decisions: Sequence[tuple[str, Path, Path, dict[str, Any]]],
    *,
    human_input: HumanPromotionInput | None,
) -> list[tuple[str, Path, dict[str, Any]]]:
    provider = human_input or TerminalHumanPromotionInput()
    decisions: list[tuple[str, Path, dict[str, Any]]] = []
    for artifact_id, review_path, decision_path, packet in pending_decisions:
        raw_decision = provider(packet)
        if _is_pending_human_input(raw_decision):
            decision = {
                "status": "pending_review",
                "failure_codes": ["pending_human_review"],
                "rationale": _sanitize_rationale(raw_decision.get("rationale")),
            }
        else:
            sanitized_decision = _sanitize_human_decision_payload(raw_decision)
            _write_json(decision_path, sanitized_decision)
            decision = _human_decision_from_payload(sanitized_decision)
        decisions.append((artifact_id, review_path, decision))
    return decisions


class TerminalHumanPromotionInput:
    def __init__(self, *, stdin: Any | None = None, stdout: Any | None = None) -> None:
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout

    def __call__(self, packet: dict[str, Any]) -> dict[str, Any]:
        rationale = ""
        while True:
            self._render_packet(packet, rationale=rationale)
            choice = self._prompt("Approve artifact? [y]es/[n]o/[c]omment/[s]kip: ")
            normalized = choice.strip().lower()
            if normalized in {"y", "yes"}:
                return self._decision_payload(approved=True, rationale=rationale)
            if normalized in {"n", "no"}:
                return self._decision_payload(approved=False, rationale=rationale)
            if normalized in {"c", "comment"}:
                rationale = self._prompt("Comment/rationale: ").strip()
                continue
            if normalized in {"s", "skip", "pending"}:
                return {"status": "pending_review", "rationale": rationale}
            self._write("Enter y, n, c, or s.\n")

    def _render_packet(self, packet: Mapping[str, Any], *, rationale: str) -> None:
        artifact = packet.get("artifact") if isinstance(packet.get("artifact"), Mapping) else {}
        support = packet.get("promotion_support")
        content = packet.get("artifact_content")
        artifact_id = artifact.get("artifact_id") if isinstance(artifact, Mapping) else ""
        artifact_type = packet.get("artifact_type") or artifact.get("type")
        name = artifact.get("name") if isinstance(artifact, Mapping) else ""
        self._write("\n=== Promotion Review ===\n")
        self._write(f"Artifact: {artifact_id} ({artifact_type}) {name}\n")
        self._write(f"Task: {packet.get('task_id')} round {packet.get('round_index')}\n")
        self._write(f"Method: {packet.get('method')}\n")
        if isinstance(support, Mapping):
            self._write("\nPromotion support:\n")
            for key in PROMOTION_SUPPORT_FIELDS:
                self._write(f"- {key}: {_compact_display(support.get(key))}\n")
        if isinstance(content, Mapping) and content.get("available"):
            self._write("\nArtifact content excerpts:\n")
            for excerpt in content.get("excerpts", []):
                if not isinstance(excerpt, Mapping):
                    continue
                self._write(f"\n--- {excerpt.get('path')} ---\n")
                self._write(str(excerpt.get("text") or ""))
                self._write("\n")
        if rationale:
            self._write(f"\nCurrent comment: {rationale}\n")

    def _prompt(self, text: str) -> str:
        self._write(text)
        line = self.stdin.readline()
        if line == "":
            return "s"
        return line

    def _write(self, text: str) -> None:
        self.stdout.write(text)
        self.stdout.flush()

    def _decision_payload(self, *, approved: bool, rationale: str) -> dict[str, Any]:
        score = self._prompt("Score 0..1 (optional): ").strip()
        if not rationale:
            rationale = self._prompt("Rationale (optional): ").strip()
        decision: dict[str, Any] = {"approved": approved, "rationale": rationale}
        if score:
            parsed_score = _optional_contract_score(score)
            decision["score"] = parsed_score if math.isfinite(parsed_score) else score
        if self._yes_no("Add structured human feedback? [y/N]: "):
            feedback = {
                "observed_issues": self._feedback_items(
                    "Observed issues (semicolon-separated, optional): "
                ),
                "suggested_changes": self._feedback_items(
                    "Suggested changes (semicolon-separated, optional): "
                ),
                "risks": self._feedback_items("Risks (semicolon-separated, optional): "),
                "validation_checks": self._feedback_items(
                    "Validation checks (semicolon-separated, optional): "
                ),
            }
            compact_feedback = {
                key: value for key, value in feedback.items() if value
            }
            if compact_feedback:
                decision["human_feedback"] = compact_feedback
        return decision

    def _yes_no(self, text: str) -> bool:
        answer = self._prompt(text).strip().lower()
        return answer in {"y", "yes"}

    def _feedback_items(self, text: str) -> list[str]:
        raw = self._prompt(text)
        return [item.strip() for item in raw.split(";") if item.strip()]


def _read_human_decision(decision_path: Path) -> dict[str, Any]:
    if not decision_path.exists():
        return {
            "status": "pending_review",
            "failure_codes": ["pending_human_review"],
            "rationale": "waiting for human approval decision",
        }
    try:
        decision = _parse_json_object(decision_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return {
            "status": "pending_review",
            "failure_codes": ["pending_human_review", "invalid_human_decision"],
            "rationale": f"waiting for valid human approval decision: {exc}",
        }
    return _human_decision_from_payload(decision)


def _human_decision_from_payload(decision: Mapping[str, Any]) -> dict[str, Any]:
    score = _optional_contract_score(decision.get("score"))
    score_in_contract = score is None or (math.isfinite(score) and 0.0 <= score <= 1.0)
    reviewer_approved = decision.get("approved") is True
    approved = reviewer_approved and score_in_contract
    failure_codes: list[str] = []
    if not reviewer_approved:
        failure_codes.append("human_rejected")
    if not score_in_contract:
        failure_codes.append("score_outside_contract")
    result = {
        "status": "approved" if approved else "rejected",
        "score": score if score is None or math.isfinite(score) else None,
        "failure_codes": failure_codes,
        "rationale": _sanitize_rationale(decision.get("rationale")),
    }
    human_feedback = _sanitize_feedback_payload(
        _normalize_human_feedback(decision.get("human_feedback"))
    )
    if human_feedback:
        result["human_feedback"] = human_feedback
    return result


def _is_pending_human_input(decision: Mapping[str, Any]) -> bool:
    return str(decision.get("status") or "").strip() == "pending_review"


def _stdio_is_interactive() -> bool:
    stdin_is_tty = bool(getattr(sys.stdin, "isatty", lambda: False)())
    stdout_is_tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
    return stdin_is_tty and stdout_is_tty


def _compact_display(value: Any, *, limit: int = 700) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value.strip()
    else:
        text = json.dumps(value, ensure_ascii=False)
    if len(text) <= limit:
        return text
    return text[: limit - 15].rstrip() + "... [truncated]"


def _normalize_human_feedback(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    normalized: dict[str, Any] = {}
    for key, child in value.items():
        text_key = str(key).strip()
        if not text_key:
            continue
        normalized_child = _normalize_human_feedback_value(child)
        if normalized_child not in (None, "", []):
            normalized[text_key] = normalized_child
    return normalized


def _normalize_human_feedback_value(value: object) -> str | list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = [str(item).strip() for item in value if str(item).strip()]
        return items or None
    text = str(value).strip()
    return text or None


def _sanitize_human_decision_payload(decision: Mapping[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    approved = decision.get("approved")
    if isinstance(approved, bool):
        sanitized["approved"] = approved
    if "score" in decision:
        score = decision.get("score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            sanitized["score"] = score
        elif isinstance(score, str):
            sanitized["score"] = _sanitize_feedback_text(score)
    rationale = _sanitize_rationale(decision.get("rationale"))
    if rationale:
        sanitized["rationale"] = rationale
    human_feedback = _sanitize_feedback_payload(
        _normalize_human_feedback(decision.get("human_feedback"))
    )
    if human_feedback:
        sanitized["human_feedback"] = human_feedback
    return sanitized


def _sanitize_feedback_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, child in value.items():
            text_key = str(key).strip()
            if not text_key:
                continue
            sanitized_key = _sanitize_review_boundary_key(text_key)
            if not sanitized_key:
                continue
            if _FEEDBACK_SENSITIVE_KEY_RE.search(text_key):
                sanitized[sanitized_key] = _REDACTED_LABEL
                continue
            sanitized_child = _sanitize_feedback_payload(child)
            if sanitized_child not in (None, "", []):
                sanitized[sanitized_key] = sanitized_child
        return sanitized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        sanitized_items = [_sanitize_feedback_payload(item) for item in value]
        return [item for item in sanitized_items if item not in (None, "", [])]
    if isinstance(value, str):
        return _sanitize_feedback_text(value)
    return value


def _sanitize_feedback_text(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    text = _FEEDBACK_URI_RE.sub(_sanitize_feedback_uri_match, text)
    text = _FEEDBACK_RELATIVE_URI_REF_RE.sub(
        _sanitize_feedback_relative_uri_match,
        text,
    )
    text = _FEEDBACK_POSIX_ABSOLUTE_PATH_RE.sub(
        _sanitize_feedback_posix_path_match,
        text,
    )
    text = _FEEDBACK_WINDOWS_UNC_PATH_RE.sub(_LOCAL_ARTIFACT_PATH_LABEL, text)
    text = _FEEDBACK_WINDOWS_ABSOLUTE_PATH_RE.sub(_LOCAL_ARTIFACT_PATH_LABEL, text)
    text = _FEEDBACK_AUTHORIZATION_VALUE_RE.sub(_REDACTED_LABEL, text)
    text = _FEEDBACK_BEARER_VALUE_RE.sub(_REDACTED_LABEL, text)
    text = _FEEDBACK_SENSITIVE_SCHEME_VALUE_RE.sub(_REDACTED_LABEL, text)
    text = _FEEDBACK_SECRET_ASSIGNMENT_RE.sub(_REDACTED_LABEL, text)
    text = _FEEDBACK_AWS_ACCESS_KEY_RE.sub(_REDACTED_LABEL, text)
    return text.strip()


def _sanitize_rationale(value: object) -> str:
    return _sanitize_feedback_text(str(value or ""))


def sanitize_review_text(value: object, *, limit: int = 1000) -> str:
    text = _sanitize_feedback_text(str(value or ""))
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "... [truncated]"


def _sanitize_feedback_uri_match(match: re.Match[str]) -> str:
    return _sanitize_uri(match.group(0).rstrip(".,);]"))


def _sanitize_feedback_relative_uri_match(match: re.Match[str]) -> str:
    candidate = match.group(0).rstrip(".,);]")
    if _looks_like_uri_reference(candidate):
        return _sanitize_uri(candidate)
    return match.group(0)


def _sanitize_feedback_posix_path_match(match: re.Match[str]) -> str:
    return _LOCAL_ARTIFACT_PATH_LABEL


def _llm_decision(raw_decision: Mapping[str, Any], *, min_score: float) -> dict[str, Any]:
    score = _optional_contract_score(raw_decision.get("score"))
    reviewer_approved = raw_decision.get("approved") is True
    score_in_contract = score is not None and math.isfinite(score) and 0.0 <= score <= 1.0
    approved = reviewer_approved and score_in_contract and score >= min_score
    failure_codes: list[str] = []
    if not reviewer_approved:
        failure_codes.append("llm_rejected")
    if not score_in_contract:
        failure_codes.append("score_outside_contract")
    elif score < min_score:
        failure_codes.append("score_below_threshold")
    return {
        "status": "approved" if approved else "rejected",
        "score": score if score is not None and math.isfinite(score) else None,
        "failure_codes": failure_codes,
        "rationale": _sanitize_rationale(raw_decision.get("rationale")),
    }


def _aggregate_review_status(reviews: Sequence[Mapping[str, Any]]) -> str:
    targeted = [
        review
        for review in reviews
        if review.get("status") not in {None, "skipped"}
    ]
    if not targeted:
        return "missing_target_artifact"
    if any(review.get("status") == "pending_review" for review in targeted):
        return "pending_review"
    if all(review.get("status") == "approved" for review in targeted):
        return "approved"
    if any(review.get("status") == "approved" for review in targeted):
        return "partially_approved"
    return "rejected"


def _sanitize_artifact_metadata(artifact: dict[str, Any]) -> dict[str, Any]:
    sanitized = _sanitize_review_boundary_payload(artifact)
    if not isinstance(sanitized, dict):
        return artifact
    return sanitized


def _sanitize_review_boundary_payload(value: Any, *, uri_context: bool = False) -> Any:
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, child in value.items():
            text_key = str(key).strip()
            if not text_key:
                continue
            sanitized_key = _sanitize_review_boundary_key(text_key)
            if not sanitized_key:
                continue
            if _FEEDBACK_SENSITIVE_KEY_RE.search(text_key):
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
        if uri_context and _looks_like_absolute_local_path(value):
            return _LOCAL_ARTIFACT_PATH_LABEL
        if uri_context and _looks_like_uri_reference(value):
            return _sanitize_uri(value)
        return _sanitize_feedback_text(value)
    return value


def _sanitize_review_boundary_key(key: str) -> str:
    if _looks_like_absolute_local_path(key):
        return _LOCAL_ARTIFACT_PATH_LABEL
    if _looks_like_uri_reference(key):
        return _sanitize_uri(key)
    return _sanitize_feedback_text(key)


def _is_uri_field_key(key: object) -> bool:
    text = str(key).strip().lower()
    normalized = re.sub(r"[^a-z0-9]+", "_", text)
    parts = [part for part in normalized.split("_") if part]
    if any(part in {"uri", "uris", "url", "urls", "path", "paths"} for part in parts):
        return True
    return text.endswith(("uri", "uris", "url", "urls", "path", "paths"))


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
    if _FEEDBACK_WINDOWS_UNC_PATH_RE.fullmatch(
        stripped
    ) or _FEEDBACK_WINDOWS_ABSOLUTE_PATH_RE.fullmatch(stripped):
        return True
    return Path(stripped).is_absolute()


def _sanitize_uri(uri: str) -> str:
    parsed = urlparse(uri)
    if _is_sensitive_uri_scheme(parsed.scheme):
        return _REDACTED_LABEL
    if parsed.scheme == "file":
        return _LOCAL_ARTIFACT_URI_LABEL
    if not parsed.scheme and _looks_like_absolute_local_path(parsed.path):
        return _LOCAL_ARTIFACT_PATH_LABEL
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


def _is_sensitive_uri_scheme(scheme: str) -> bool:
    normalized = scheme.lower()
    if not normalized:
        return False
    return normalized in {"bearer", "basic"} or bool(
        _FEEDBACK_SENSITIVE_KEY_RE.search(normalized)
    )


def _render_llm_review_prompt(packet: Mapping[str, Any]) -> str:
    return (
        "Review whether this evolved artifact should be promoted into future agent "
        "rollouts. Judge the artifact and the algorithm's promotion_support. Approve "
        "only if the support names concrete trajectory findings, specific changes, "
        "expected benefits, risks, and validation checks without hard-coding held-out "
        "answers or task-specific shortcuts.\n\n"
        "Return only JSON with this schema:\n"
        '{"approved": boolean, "score": number between 0 and 1, "rationale": string}\n\n'
        f"Review packet:\n{json.dumps(packet, indent=2, sort_keys=True, allow_nan=False)}"
    )


def _normalize_llm_config(raw: Mapping[str, Any]) -> dict[str, Any]:
    provider = str(raw.get("provider") or "openai_chat")
    model = str(raw.get("model") or "").strip()
    if not model:
        raise ValueError("promotion_gate.llm.model is required for llm promotion gates")
    return {
        "provider": provider,
        "model": model,
        "temperature": _float_config(raw.get("temperature"), 0.0),
        "timeout_seconds": _float_config(raw.get("timeout_seconds"), 120.0),
        "max_tokens": raw.get("max_tokens"),
        "api_key": raw.get("api_key") or os.environ.get("OPENAI_API_KEY", ""),
        "base_url": (
            str(raw.get("base_url") or os.environ.get("OPENAI_BASE_URL", "")).rstrip("/")
            or "https://api.openai.com/v1"
        ),
        "codex_home": raw.get("codex_home") or raw.get("reflector_codex_home"),
        "codex_bin": str(raw.get("codex_bin") or "codex"),
    }


def _parse_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"promotion gate reviewer returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("promotion gate reviewer must return a JSON object")
    return payload


def _safe_component(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip(".-")
    return slug or "task"


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _float_config(value: object, default: float | None) -> float:
    if value is None:
        if default is None:
            return 0.0
        return float(default)
    if isinstance(value, bool):
        return float(default or 0.0)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default or 0.0)


def _optional_contract_score(value: object) -> float | None:
    if value is None:
        return None
    return _float_config(value, math.nan)


def _int_config(value: object, default: int) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
