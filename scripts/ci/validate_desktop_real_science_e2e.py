#!/usr/bin/env python3
"""Validate exact-candidate Desktop real-science evidence before publication."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import stat
import sys
from types import ModuleType
from typing import Mapping


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RELEASE_CANDIDATE_SCHEMA_VERSION = 7
MAX_EVIDENCE_BYTES = 128 * 1024
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SOURCE_PATTERN = re.compile(r"^[0-9a-f]{40}$")
TARGETS = ("agent_system", "skill_bundle", "text_memory")
REQUIRED_PHASES = frozenset(
    {"admission", "preparation", "execution", "evolution", "revision", "terminal"}
)


class EvidenceError(RuntimeError):
    pass


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise EvidenceError(f"{label} is not an object")
    return value


def _exact_mapping(
    value: object,
    label: str,
    keys: set[str] | frozenset[str],
) -> dict[str, object]:
    result = dict(_mapping(value, label))
    if set(result) != set(keys):
        raise EvidenceError(f"{label} does not match its closed schema")
    return result


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise EvidenceError(f"{label} is not a SHA-256 digest")
    return value


def _positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise EvidenceError(f"{label} is not a positive integer")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise EvidenceError(f"{label} is not a non-negative integer")
    return value


def _nonempty_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise EvidenceError(f"{label} is not non-empty canonical text")
    return value


def _revision(value: object, label: str) -> dict[str, object]:
    revision = _exact_mapping(
        value,
        label,
        {"id_sha256", "generation", "manifest_sha256"},
    )
    _sha256(revision["id_sha256"], f"{label} ID")
    _nonnegative_int(revision["generation"], f"{label} generation")
    _sha256(revision["manifest_sha256"], f"{label} manifest")
    return revision


def _asset_identity(value: object, label: str) -> dict[str, object]:
    asset = _exact_mapping(value, label, {"sha256", "byte_size"})
    _sha256(asset["sha256"], f"{label} digest")
    _positive_int(asset["byte_size"], f"{label} byte size")
    return asset


def _read_candidate_manifest(
    path: Path,
    *,
    expected_sha256: str,
    expected_source_commit: str,
) -> dict[str, object]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise EvidenceError("candidate manifest is unreadable") from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or not 0 < metadata.st_size <= 1024 * 1024
    ):
        raise EvidenceError("candidate manifest is not a stable regular file")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise EvidenceError("candidate manifest is unreadable") from exc
    if metadata.st_size != len(content):
        raise EvidenceError("candidate manifest changed while it was read")
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise EvidenceError("candidate manifest digest mismatch")
    try:
        payload = json.loads(content.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError("candidate manifest is not valid JSON") from exc
    manifest = dict(_mapping(payload, "candidate manifest"))
    if (
        manifest.get("schema_version") != RELEASE_CANDIDATE_SCHEMA_VERSION
        or manifest.get("source_commit") != expected_source_commit
        or not isinstance(manifest.get("version"), str)
        or not manifest["version"]
    ):
        raise EvidenceError("candidate manifest identity is invalid")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise EvidenceError("candidate manifest file inventory is missing")
    roles: dict[str, dict[str, object]] = {}
    for value in files:
        item = _exact_mapping(
            value,
            "candidate file",
            {"role", "filename", "sha256", "byte_size"},
        )
        role = _nonempty_text(item["role"], "candidate file role")
        if role in roles:
            raise EvidenceError("candidate manifest contains duplicate roles")
        _nonempty_text(item["filename"], "candidate filename")
        _sha256(item["sha256"], "candidate file digest")
        _positive_int(item["byte_size"], "candidate file byte size")
        roles[role] = item
    required_roles = {
        "app_bundle_smoke",
        "core_wheel",
        "daemon_bundle",
        "daemon_manifest",
        "desktop_dmg",
        "framework_lock",
        "packaged_web_manifest",
        "playwright_evidence",
    }
    if not required_roles.issubset(roles):
        raise EvidenceError("candidate manifest is missing release roles")
    managed_runtime = _mapping(manifest.get("managed_runtime"), "managed runtime")
    archive = _exact_mapping(
        managed_runtime.get("archive"),
        "managed runtime archive",
        {"filename", "sha256", "byte_size"},
    )
    _nonempty_text(archive["filename"], "managed runtime filename")
    _sha256(archive["sha256"], "managed runtime digest")
    _positive_int(archive["byte_size"], "managed runtime byte size")
    manifest["_roles"] = roles
    manifest["_runtime_archive"] = archive
    return manifest


def _read_candidate_app_smoke(
    path: Path,
    *,
    role: Mapping[str, object],
    desktop_dmg_role: Mapping[str, object],
) -> str:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise EvidenceError("candidate app smoke is unreadable") from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or not 0 < metadata.st_size <= 1024 * 1024
        or metadata.st_size != role.get("byte_size")
    ):
        raise EvidenceError("candidate app smoke is not a stable candidate asset")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise EvidenceError("candidate app smoke is unreadable") from exc
    if (
        len(content) != metadata.st_size
        or hashlib.sha256(content).hexdigest() != role.get("sha256")
        or path.name != role.get("filename")
    ):
        raise EvidenceError("candidate app smoke does not match the candidate manifest")
    try:
        payload = json.loads(content.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError("candidate app smoke is not valid JSON") from exc
    smoke = dict(_mapping(payload, "candidate app smoke"))
    binary_sha256 = smoke.get("binary_sha256")
    source_dmg = smoke.get("source_dmg")
    if (
        smoke.get("schema_version") != 3
        or smoke.get("launch_origin") != "mounted_dmg"
        or smoke.get("bundled_external_bin") != "openevo-desktop-sidecar"
        or smoke.get("sidecar_ready") is not True
        or smoke.get("bundled_external_bin_resolved") is not True
        or smoke.get("native_listener_fd_handoff") is not True
        or smoke.get("native_executable_fd_handoff") is not True
        or smoke.get("process_group_cleanup") is not True
        or not isinstance(source_dmg, dict)
        or source_dmg
        != {
            "filename": desktop_dmg_role.get("filename"),
            "sha256": desktop_dmg_role.get("sha256"),
        }
        or not isinstance(binary_sha256, dict)
        or set(binary_sha256) != {"native_executable", "bundled_external_bin"}
    ):
        raise EvidenceError("candidate app smoke does not prove the packaged sidecar")
    _sha256(binary_sha256.get("native_executable"), "candidate native executable")
    return _sha256(
        binary_sha256.get("bundled_external_bin"),
        "candidate packaged sidecar",
    )


def _require_asset_match(
    evidence: Mapping[str, object],
    candidate: Mapping[str, object],
    label: str,
) -> None:
    if evidence.get("sha256") != candidate.get("sha256") or evidence.get(
        "byte_size"
    ) != candidate.get("byte_size"):
        raise EvidenceError(f"{label} does not match the candidate manifest")


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise EvidenceError(f"{label} is not a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError as exc:
        raise EvidenceError(f"{label} is not a UTC timestamp") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise EvidenceError(f"{label} is not a UTC timestamp")
    return parsed


def _string_set(value: object, label: str, *, allow_empty: bool = False) -> list[str]:
    if (
        not isinstance(value, list)
        or (not allow_empty and not value)
        or any(not isinstance(item, str) or not item for item in value)
        or value != sorted(set(value))
    ):
        raise EvidenceError(f"{label} is not a canonical string set")
    return value


def _event_inventory(
    value: object,
    label: str,
    *,
    category_keys: tuple[str, ...],
) -> dict[str, object]:
    inventory = _exact_mapping(
        value,
        label,
        {"count", "content_sha256", "evidence_truncated", *category_keys},
    )
    count = _positive_int(inventory["count"], f"{label} count")
    if inventory["evidence_truncated"] is not False:
        raise EvidenceError(f"{label} is truncated")
    digests = inventory["content_sha256"]
    if (
        not isinstance(digests, list)
        or len(digests) != count
        or any(SHA256_PATTERN.fullmatch(item) is None for item in digests if isinstance(item, str))
        or any(not isinstance(item, str) for item in digests)
    ):
        raise EvidenceError(f"{label} content inventory is incomplete")
    for key in category_keys:
        _string_set(inventory[key], f"{label} {key}")
    return inventory


def _artifact(
    value: object,
    label: str,
    *,
    ordinal: int,
) -> dict[str, object]:
    artifact = _exact_mapping(
        value,
        label,
        {
            "artifact_id_sha256",
            "artifact_type",
            "target_id",
            "method_id",
            "content_sha256",
            "byte_size",
            "selected",
            "promoted",
            "release_enabled",
            "source_artifact_count",
            "source_artifact_ids_sha256",
            "source_dataset_count",
            "produced_revision",
        },
    )
    target_id = artifact["target_id"]
    if target_id not in TARGETS or artifact["artifact_type"] != target_id:
        raise EvidenceError(f"{label} target identity is invalid")
    _nonempty_text(artifact["method_id"], f"{label} method identity")
    _sha256(artifact["artifact_id_sha256"], f"{label} ID")
    _sha256(artifact["content_sha256"], f"{label} content")
    _positive_int(artifact["byte_size"], f"{label} byte size")
    if (
        artifact["selected"] is not True
        or artifact["release_enabled"] is not True
        or not isinstance(artifact["promoted"], bool)
    ):
        raise EvidenceError(f"{label} selection state is invalid")
    source_artifacts = _nonnegative_int(
        artifact["source_artifact_count"], f"{label} source artifact count"
    )
    source_artifact_ids = artifact["source_artifact_ids_sha256"]
    if not isinstance(source_artifact_ids, list) or len(source_artifact_ids) != source_artifacts:
        raise EvidenceError(f"{label} source artifact identities are incomplete")
    for index, source_artifact_id in enumerate(source_artifact_ids):
        _sha256(source_artifact_id, f"{label} source artifact {index}")
    if len(set(source_artifact_ids)) != len(source_artifact_ids):
        raise EvidenceError(f"{label} source artifact identities are duplicated")
    _positive_int(artifact["source_dataset_count"], f"{label} source dataset count")
    if ordinal == 2 and source_artifacts < 1:
        raise EvidenceError(f"{label} does not reuse the predecessor artifact")
    artifact["produced_revision"] = _revision(
        artifact["produced_revision"], f"{label} produced revision"
    )
    return artifact


def _artifact_inspection(
    value: object,
    label: str,
    *,
    artifact: Mapping[str, object],
) -> dict[str, object]:
    inspection = _exact_mapping(
        value,
        label,
        {
            "artifact_id_sha256",
            "document_count",
            "total_documents",
            "total_utf8_bytes",
            "truncated",
            "runtime_document_sha256",
        },
    )
    if inspection["artifact_id_sha256"] != artifact["artifact_id_sha256"]:
        raise EvidenceError(f"{label} does not identify its artifact")
    document_count = _positive_int(inspection["document_count"], f"{label} documents")
    if inspection["total_documents"] != document_count:
        raise EvidenceError(f"{label} document inventory is incomplete")
    _positive_int(inspection["total_utf8_bytes"], f"{label} UTF-8 bytes")
    if inspection["truncated"] is not False:
        raise EvidenceError(f"{label} is truncated")
    _sha256(inspection["runtime_document_sha256"], f"{label} runtime document")
    return inspection


def _session(value: object, *, ordinal: int) -> dict[str, object]:
    label = f"session {ordinal}"
    session = _exact_mapping(
        value,
        label,
        {
            "ordinal",
            "run_id_sha256",
            "status",
            "required_relation",
            "required_revision",
            "pinned_revision",
            "timeline",
            "logs",
            "artifacts",
            "artifact_count",
            "artifact_evidence_truncated",
            "artifact_inspections",
            "runtime_context_receipt_sha256",
            "runtime_context_receipt_core_provenance_verified",
            "context",
            "transcript_dataset_lineage_observed",
        },
    )
    if (
        session["ordinal"] != ordinal
        or session["status"] != "succeeded"
        or session["required_relation"] != "active"
        or session["artifact_count"] != len(TARGETS)
        or session["artifact_evidence_truncated"] is not False
        or session["transcript_dataset_lineage_observed"] is not True
    ):
        raise EvidenceError(f"{label} verdict is incomplete")
    _sha256(session["run_id_sha256"], f"{label} run ID")
    required = _revision(session["required_revision"], f"{label} required revision")
    pinned = _revision(session["pinned_revision"], f"{label} pinned revision")
    if required != pinned:
        raise EvidenceError(f"{label} did not pin its required active revision")
    timeline = _event_inventory(
        session["timeline"],
        f"{label} timeline",
        category_keys=("phase_values", "status_values"),
    )
    if not REQUIRED_PHASES.issubset(set(timeline["phase_values"])):
        raise EvidenceError(f"{label} timeline is missing required phases")
    logs = _event_inventory(
        session["logs"],
        f"{label} logs",
        category_keys=("stream_values", "level_values"),
    )

    artifacts_value = session["artifacts"]
    if not isinstance(artifacts_value, list) or len(artifacts_value) != len(TARGETS):
        raise EvidenceError(f"{label} artifact inventory is incomplete")
    artifacts = [_artifact(item, label, ordinal=ordinal) for item in artifacts_value]
    artifacts_by_target = {str(item["target_id"]): item for item in artifacts}
    if len(artifacts_by_target) != len(TARGETS) or set(artifacts_by_target) != set(TARGETS):
        raise EvidenceError(f"{label} artifact targets are invalid")
    produced_revisions = {
        json.dumps(item["produced_revision"], sort_keys=True) for item in artifacts
    }
    if len(produced_revisions) != 1:
        raise EvidenceError(f"{label} artifacts do not share one successor revision")
    produced = dict(artifacts[0]["produced_revision"])
    if produced["generation"] != pinned["generation"] + 1:
        raise EvidenceError(f"{label} successor generation is invalid")

    inspections_value = _exact_mapping(
        session["artifact_inspections"],
        f"{label} artifact inspections",
        set(TARGETS),
    )
    inspections = {
        target_id: _artifact_inspection(
            inspections_value[target_id],
            f"{label} {target_id} inspection",
            artifact=artifacts_by_target[target_id],
        )
        for target_id in TARGETS
    }
    context = _exact_mapping(
        session["context"],
        f"{label} context",
        {
            "status",
            "capture_mode",
            "token_level_metrics_available",
            "artifact_count",
            "adapter_count",
        },
    )
    expected_context_artifacts = 0 if ordinal == 1 else len(TARGETS)
    if (
        context["status"] != "succeeded"
        or context["capture_mode"] != "transcript"
        or context["token_level_metrics_available"] is not False
        or context["artifact_count"] != expected_context_artifacts
        or context["adapter_count"] != 0
    ):
        raise EvidenceError(f"{label} runtime context is invalid")
    receipt = session["runtime_context_receipt_sha256"]
    if ordinal == 1:
        if (
            receipt is not None
            or session["runtime_context_receipt_core_provenance_verified"] is not False
        ):
            raise EvidenceError("session 1 unexpectedly reports successor injection")
    else:
        _sha256(receipt, "session 2 runtime-context receipt")
        if session["runtime_context_receipt_core_provenance_verified"] is not True:
            raise EvidenceError("session 2 runtime-context receipt lacks Core provenance")
    session["required_revision"] = required
    session["pinned_revision"] = pinned
    session["timeline"] = timeline
    session["logs"] = logs
    session["artifacts_by_target"] = artifacts_by_target
    session["inspections"] = inspections
    session["produced_revision"] = produced
    return session


def _load_runner() -> ModuleType:
    path = REPOSITORY_ROOT / "scripts/e2e/desktop_real_science_e2e.py"
    spec = importlib.util.spec_from_file_location("openevo_desktop_real_science_e2e", path)
    if spec is None or spec.loader is None:
        raise EvidenceError("real-science evidence policy is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def validate_evidence(
    path: Path,
    *,
    candidate_manifest_path: Path,
    candidate_app_bundle_smoke_path: Path,
    expected_sha256: str,
    expected_source_commit: str,
    expected_candidate_manifest_sha256: str,
) -> dict[str, object]:
    if SHA256_PATTERN.fullmatch(expected_sha256) is None:
        raise EvidenceError("expected evidence digest is invalid")
    if SOURCE_PATTERN.fullmatch(expected_source_commit) is None:
        raise EvidenceError("expected source commit is invalid")
    if SHA256_PATTERN.fullmatch(expected_candidate_manifest_sha256) is None:
        raise EvidenceError("expected candidate manifest digest is invalid")
    candidate = _read_candidate_manifest(
        candidate_manifest_path,
        expected_sha256=expected_candidate_manifest_sha256,
        expected_source_commit=expected_source_commit,
    )
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise EvidenceError("real-science evidence is unreadable") from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or not 0 < metadata.st_size <= MAX_EVIDENCE_BYTES
    ):
        raise EvidenceError("real-science evidence is not a stable regular file")
    try:
        payload_bytes = path.read_bytes()
    except OSError as exc:
        raise EvidenceError("real-science evidence is unreadable") from exc
    if metadata.st_size != len(payload_bytes):
        raise EvidenceError("real-science evidence changed while it was read")
    if hashlib.sha256(payload_bytes).hexdigest() != expected_sha256:
        raise EvidenceError("real-science evidence digest mismatch")
    try:
        payload = json.loads(payload_bytes.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError("real-science evidence is not valid JSON") from exc
    if payload_bytes != _canonical_json(payload):
        raise EvidenceError("real-science evidence is not canonical JSON")
    document = _exact_mapping(
        payload,
        "real-science evidence",
        {
            "schema_version",
            "kind",
            "issue",
            "real_process_boundary",
            "outcome",
            "started_at",
            "release_assets",
            "renderer_candidate_binding",
            "desktop",
            "run_mode",
            "verification_scope",
            "session_count",
            "evolution_targets_enabled",
            "artifact_publication_verified",
            "cross_session_reuse_verified",
            "release_evolution_path_verified",
            "canonical_project_head_orchestration_verified",
            "codex_subscription_transcript_verified",
            "remote",
            "project",
            "sessions",
            "reuse",
            "renderer",
            "renderer_observability_verified",
            "renderer_boundary",
            "native_tauri_live_verified",
            "cleanup",
            "finished_at",
        },
    )
    try:
        _load_runner()._audit_evidence(document, private_values=())
    except Exception as exc:
        raise EvidenceError("real-science evidence violates the privacy contract") from exc

    required_identity: dict[str, object] = {
        "schema_version": "1",
        "kind": "openevo_desktop_real_science_e2e",
        "issue": 163,
        "real_process_boundary": True,
        "outcome": "passed",
        "run_mode": "two_session_subscription_release",
        "session_count": 2,
        "evolution_targets_enabled": True,
        "artifact_publication_verified": True,
        "cross_session_reuse_verified": True,
        "release_evolution_path_verified": True,
        "canonical_project_head_orchestration_verified": False,
        "codex_subscription_transcript_verified": True,
        "renderer_observability_verified": True,
        "renderer_boundary": "packaged_web_to_live_local_api",
        "native_tauri_live_verified": False,
    }
    if any(document.get(key) != value for key, value in required_identity.items()):
        raise EvidenceError("real-science evidence does not contain the required release verdict")
    expected_scope = [
        "desktop_sidecar",
        "ssh_bootstrap",
        "daemon_core",
        "codex_subscription_transcript",
        "cross_session_artifact_reuse",
        "packaged_renderer_local_api_observability",
    ]
    if document["verification_scope"] != expected_scope:
        raise EvidenceError("real-science verification scope is incomplete")
    started = _timestamp(document["started_at"], "evidence start")
    finished = _timestamp(document["finished_at"], "evidence finish")
    if finished < started:
        raise EvidenceError("real-science evidence timestamps are inverted")

    roles = _mapping(candidate["_roles"], "candidate roles")
    runtime_role = _mapping(candidate["_runtime_archive"], "candidate runtime")
    candidate_packaged_sidecar_sha256 = _read_candidate_app_smoke(
        candidate_app_bundle_smoke_path,
        role=_mapping(roles["app_bundle_smoke"], "candidate app smoke role"),
        desktop_dmg_role=_mapping(roles["desktop_dmg"], "candidate DMG role"),
    )
    assets = _exact_mapping(
        document["release_assets"],
        "release assets",
        {
            "sidecar",
            "core_wheel",
            "framework_lock",
            "managed_runtime_archive",
            "daemon_bundle",
            "daemon_manifest",
            "external_release_assets",
            "exact_external_release_assets_verified",
            "slim_sidecar_excludes_remote_release_assets_verified",
        },
    )
    _asset_identity(assets["sidecar"], "Desktop sidecar")
    wheel = _exact_mapping(
        assets["core_wheel"],
        "Core wheel",
        {"sha256", "byte_size", "filename", "distribution", "version"},
    )
    _sha256(wheel["sha256"], "Core wheel digest")
    _positive_int(wheel["byte_size"], "Core wheel byte size")
    _nonempty_text(wheel["filename"], "Core wheel filename")
    if wheel["distribution"] != "openevo" or wheel["version"] != candidate["version"]:
        raise EvidenceError("Core wheel product identity is invalid")
    framework_lock = _exact_mapping(
        assets["framework_lock"],
        "framework lock",
        {"sha256", "byte_size", "distribution_digest"},
    )
    _sha256(framework_lock["sha256"], "framework lock digest")
    _positive_int(framework_lock["byte_size"], "framework lock byte size")
    if framework_lock["distribution_digest"] != wheel["sha256"]:
        raise EvidenceError("framework lock does not bind the Core wheel")
    managed_runtime = _asset_identity(assets["managed_runtime_archive"], "managed runtime")
    daemon_bundle = _asset_identity(assets["daemon_bundle"], "Daemon bundle")
    daemon_manifest = _asset_identity(assets["daemon_manifest"], "Daemon manifest")
    external_assets = _exact_mapping(
        assets["external_release_assets"],
        "external release assets",
        {"source_commit", "registry_digest", "manifest_sha256", "byte_size"},
    )
    if external_assets["source_commit"] != expected_source_commit:
        raise EvidenceError("external release assets do not bind the candidate source")
    _sha256(external_assets["registry_digest"], "external release asset registry")
    _sha256(external_assets["manifest_sha256"], "external release asset manifest")
    _positive_int(external_assets["byte_size"], "external release asset manifest byte size")
    if (
        assets["exact_external_release_assets_verified"] is not True
        or assets["slim_sidecar_excludes_remote_release_assets_verified"] is not True
    ):
        raise EvidenceError("external release assets were not verified")
    for evidence_asset, role, label in (
        (wheel, roles["core_wheel"], "Core wheel"),
        (framework_lock, roles["framework_lock"], "framework lock"),
        (daemon_bundle, roles["daemon_bundle"], "Daemon bundle"),
        (daemon_manifest, roles["daemon_manifest"], "Daemon manifest"),
        (managed_runtime, runtime_role, "managed runtime"),
    ):
        _require_asset_match(
            _mapping(evidence_asset, label),
            _mapping(role, f"candidate {label}"),
            label,
        )
    if wheel["filename"] != _mapping(roles["core_wheel"], "candidate wheel")["filename"]:
        raise EvidenceError("Core wheel filename does not match the candidate")

    desktop = _exact_mapping(
        document["desktop"],
        "Desktop identity",
        {
            "source_commit",
            "build_version",
            "openapi_sha256",
            "provider_kind",
            "build_channel",
            "feature_flags",
            "legacy_route_rejected",
            "authenticated_session_probe",
            "unauthenticated_session_rejected",
        },
    )
    _sha256(desktop["openapi_sha256"], "Desktop OpenAPI digest")
    _string_set(desktop["feature_flags"], "Desktop feature flags")
    if (
        desktop["source_commit"] != expected_source_commit
        or desktop["build_version"] != candidate["version"]
        or desktop["provider_kind"] != "desktop_sidecar"
        or desktop["build_channel"] != "release"
        or desktop["legacy_route_rejected"] is not True
        or desktop["authenticated_session_probe"] is not True
        or desktop["unauthenticated_session_rejected"] is not True
    ):
        raise EvidenceError("Desktop release identity is invalid")

    binding = _exact_mapping(
        document["renderer_candidate_binding"],
        "candidate binding",
        {
            "source_commit",
            "candidate_version",
            "release_candidate_manifest_sha256",
            "desktop_dmg_sha256",
            "app_bundle_smoke_sha256",
            "candidate_packaged_sidecar_sha256",
            "science_sidecar_sha256",
            "candidate_native_sidecar_smoke_verified",
            "cross_platform_source_equivalent_verified",
            "packaged_web_manifest_sha256",
            "playwright_candidate_evidence_sha256",
            "packaged_web_build_digest",
            "source_checkout_verified",
        },
    )
    for key in (
        "release_candidate_manifest_sha256",
        "desktop_dmg_sha256",
        "app_bundle_smoke_sha256",
        "candidate_packaged_sidecar_sha256",
        "science_sidecar_sha256",
        "packaged_web_manifest_sha256",
        "playwright_candidate_evidence_sha256",
        "packaged_web_build_digest",
    ):
        _sha256(binding[key], f"candidate binding {key}")
    if (
        binding["source_commit"] != expected_source_commit
        or binding["candidate_version"] != candidate["version"]
        or binding["release_candidate_manifest_sha256"] != expected_candidate_manifest_sha256
        or binding["source_checkout_verified"] is not True
        or binding["candidate_native_sidecar_smoke_verified"] is not True
        or binding["cross_platform_source_equivalent_verified"] is not True
        or binding["desktop_dmg_sha256"] != roles["desktop_dmg"]["sha256"]
        or binding["app_bundle_smoke_sha256"] != roles["app_bundle_smoke"]["sha256"]
        or binding["candidate_packaged_sidecar_sha256"] != candidate_packaged_sidecar_sha256
        or binding["science_sidecar_sha256"] != assets["sidecar"]["sha256"]
        or binding["packaged_web_manifest_sha256"] != roles["packaged_web_manifest"]["sha256"]
        or binding["playwright_candidate_evidence_sha256"]
        != roles["playwright_evidence"]["sha256"]
    ):
        raise EvidenceError("real-science evidence is not bound to the exact candidate")

    remote = _exact_mapping(
        document["remote"],
        "remote identity",
        {"ssh_connection_verified", "host_key_verified"},
    )
    if remote != {"ssh_connection_verified": True, "host_key_verified": True}:
        raise EvidenceError("remote SSH identity verification is incomplete")

    project = _exact_mapping(
        document["project"],
        "project evidence",
        {
            "project_id_sha256",
            "execution_mode",
            "capture_mode",
            "token_level_metrics_available",
            "codex_model",
            "reasoning_effort",
            "target_ids",
            "method_ids",
            "allowed_concrete_method_ids",
            "initial_zero_target_activation_verified",
            "registry_digest",
            "validation_check_count",
        },
    )
    _sha256(project["project_id_sha256"], "project ID")
    _sha256(project["registry_digest"], "project registry")
    if project["registry_digest"] != external_assets["registry_digest"]:
        raise EvidenceError("project registry differs from the packaged release assets")
    _positive_int(project["validation_check_count"], "project validation checks")
    method_ids = _exact_mapping(project["method_ids"], "method identities", set(TARGETS))
    allowed_concrete = _exact_mapping(
        project["allowed_concrete_method_ids"],
        "allowed concrete method identities",
        set(TARGETS),
    )
    for target_id, values in allowed_concrete.items():
        if (
            not isinstance(values, list)
            or not values
            or any(
                not isinstance(value, str) or not value or value.strip() != value
                for value in values
            )
            or values != sorted(set(values))
        ):
            raise EvidenceError(f"allowed concrete methods for {target_id} are invalid")
    if (
        project["execution_mode"] != "codex_subscription_transcript"
        or project["capture_mode"] != "transcript"
        or project["token_level_metrics_available"] is not False
        or project["codex_model"] != "gpt-5.3-codex-spark"
        or project["reasoning_effort"] != "high"
        or project["target_ids"] != list(TARGETS)
        or project["initial_zero_target_activation_verified"] is not True
        or method_ids["agent_system"] != "auto"
        or any(
            allowed_concrete[target_id] != [method_ids[target_id]]
            for target_id in ("skill_bundle", "text_memory")
        )
        or any(
            not isinstance(value, str) or not value or value.strip() != value
            for value in method_ids.values()
        )
    ):
        raise EvidenceError("real-science project profile is not the release profile")

    session_values = document["sessions"]
    if not isinstance(session_values, list) or len(session_values) != 2:
        raise EvidenceError("real-science evidence does not contain two sessions")
    sessions = [
        _session(value, ordinal=ordinal) for ordinal, value in enumerate(session_values, 1)
    ]
    first, second = sessions
    if first["run_id_sha256"] == second["run_id_sha256"]:
        raise EvidenceError("real-science sessions do not have distinct run identities")
    for session in sessions:
        for target_id, artifact in session["artifacts_by_target"].items():
            if artifact["method_id"] not in allowed_concrete[target_id]:
                raise EvidenceError(
                    "artifact method is not bound to the remote capability selection"
                )
    if second["pinned_revision"] != first["produced_revision"]:
        raise EvidenceError("session 2 did not pin session 1's successor")
    for target_id in TARGETS:
        first_artifact = first["artifacts_by_target"][target_id]
        second_artifact = second["artifacts_by_target"][target_id]
        if first_artifact["artifact_id_sha256"] == second_artifact["artifact_id_sha256"]:
            raise EvidenceError("successive sessions reused an output artifact identity")
        if (
            first_artifact["artifact_id_sha256"]
            not in second_artifact["source_artifact_ids_sha256"]
        ):
            raise EvidenceError("session 2 lineage is not bound to session 1 artifacts")

    reuse = _exact_mapping(
        document["reuse"],
        "reuse evidence",
        {
            "successor_generation_delta",
            "followup_admitted_after_successor_active",
            "session_1_excluded_own_successor",
            "session_2_pinned_session_1_successor",
            "session_1_artifacts_reused",
            "session_2_runtime_injection_verified",
            "session_2_lineage_verified",
            "runtime_context_receipt_sha256",
            "reused_artifact_count",
            "successor_project_head",
        },
    )
    required_reuse: dict[str, object] = {
        "successor_generation_delta": 1,
        "followup_admitted_after_successor_active": True,
        "session_1_excluded_own_successor": True,
        "session_2_pinned_session_1_successor": True,
        "session_1_artifacts_reused": True,
        "session_2_runtime_injection_verified": True,
        "session_2_lineage_verified": True,
        "reused_artifact_count": 3,
    }
    if any(reuse.get(key) != value for key, value in required_reuse.items()):
        raise EvidenceError("cross-session reuse evidence is incomplete")
    successor_revision = _revision(reuse["successor_project_head"], "reuse successor")
    if (
        successor_revision != first["produced_revision"]
        or reuse["runtime_context_receipt_sha256"] != second["runtime_context_receipt_sha256"]
    ):
        raise EvidenceError("cross-session reuse identities are inconsistent")

    renderer = _exact_mapping(
        document["renderer"],
        "renderer result",
        {
            "schema_version",
            "kind",
            "outcome",
            "provider_kind",
            "source_commit",
            "packaged_web_build_digest",
            "renderer_ready",
            "builtin_sample_count",
            "project_id_sha256",
            "session_count",
            "timeline",
            "logs",
            "project_head_generation",
            "independent_target_controls_verified",
            "remote_method_selection_verified",
            "artifacts",
            "screenshot_sha256",
        },
    )
    _sha256(renderer["screenshot_sha256"], "renderer screenshot")
    if (
        renderer["schema_version"] != "1"
        or renderer["kind"] != "openevo_desktop_live_renderer_observability"
        or renderer["outcome"] != "passed"
        or renderer["provider_kind"] != "desktop_sidecar"
        or renderer["source_commit"] != expected_source_commit
        or renderer["packaged_web_build_digest"] != binding["packaged_web_build_digest"]
        or renderer["renderer_ready"] is not True
        or renderer["builtin_sample_count"] != 2
        or renderer["independent_target_controls_verified"] is not True
        or renderer["remote_method_selection_verified"] is not True
        or renderer["project_id_sha256"] != project["project_id_sha256"]
        or renderer["session_count"] != 2
        or renderer["project_head_generation"] != second["produced_revision"]["generation"]
    ):
        raise EvidenceError("renderer result is not bound to the completed workflow")
    renderer_timeline = _exact_mapping(
        renderer["timeline"], "renderer timeline", {"count", "phase_values"}
    )
    expected_phase_values = {
        phase for session in sessions for phase in session["timeline"]["phase_values"]
    }
    if (
        renderer_timeline["count"]
        != sum(int(session["timeline"]["count"]) for session in sessions)
        or not isinstance(renderer_timeline["phase_values"], list)
        or set(renderer_timeline["phase_values"]) != expected_phase_values
        or len(renderer_timeline["phase_values"]) != len(expected_phase_values)
    ):
        raise EvidenceError("renderer timeline does not match both sessions")
    renderer_logs = _exact_mapping(renderer["logs"], "renderer logs", {"count"})
    if (
        not isinstance(renderer_logs["count"], int)
        or isinstance(renderer_logs["count"], bool)
        or renderer_logs["count"] < second["logs"]["count"]
    ):
        raise EvidenceError("renderer logs do not include the latest session")
    renderer_artifacts = renderer["artifacts"]
    if not isinstance(renderer_artifacts, list) or len(renderer_artifacts) != len(TARGETS):
        raise EvidenceError("renderer did not observe all release evolution targets")
    observed_renderer_targets: set[str] = set()
    for value in renderer_artifacts:
        item = _exact_mapping(
            value,
            "renderer artifact",
            {
                "artifact_id_sha256",
                "artifact_type",
                "target_id",
                "document_count",
                "total_utf8_bytes",
                "content_sha256",
                "runtime_document_sha256",
            },
        )
        target_id = item["target_id"]
        if not isinstance(target_id, str) or target_id not in TARGETS:
            raise EvidenceError("renderer artifact target is invalid")
        expected_artifact = second["artifacts_by_target"][target_id]
        expected_inspection = second["inspections"][target_id]
        if (
            target_id in observed_renderer_targets
            or item["artifact_type"] != target_id
            or item["artifact_id_sha256"] != expected_artifact["artifact_id_sha256"]
            or item["content_sha256"] != expected_artifact["content_sha256"]
            or item["document_count"] != expected_inspection["document_count"]
            or item["total_utf8_bytes"] != expected_inspection["total_utf8_bytes"]
            or item["runtime_document_sha256"] != expected_inspection["runtime_document_sha256"]
        ):
            raise EvidenceError("renderer artifact does not match session 2")
        observed_renderer_targets.add(target_id)
    if observed_renderer_targets != set(TARGETS):
        raise EvidenceError("renderer artifact targets are incomplete")

    cleanup = _exact_mapping(
        document["cleanup"],
        "cleanup evidence",
        {
            "active_run_cleanup_required",
            "active_run_cancel_requested",
            "active_run_cancelled",
            "active_run_cleanup_succeeded",
            "desktop_disconnect_succeeded",
            "sidecar_shutdown_succeeded",
            "core_ownership_release_requested",
        },
    )
    expected_cleanup = {
        "active_run_cleanup_required": False,
        "active_run_cancel_requested": False,
        "active_run_cancelled": False,
        "active_run_cleanup_succeeded": True,
        "desktop_disconnect_succeeded": True,
        "sidecar_shutdown_succeeded": True,
        "core_ownership_release_requested": True,
    }
    if cleanup != expected_cleanup:
        raise EvidenceError("real-science ownership cleanup is incomplete")
    return document


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--candidate-manifest", required=True, type=Path)
    parser.add_argument("--candidate-app-bundle-smoke", required=True, type=Path)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--expected-source-commit", required=True)
    parser.add_argument("--expected-candidate-manifest-sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        validate_evidence(
            args.evidence,
            candidate_manifest_path=args.candidate_manifest,
            candidate_app_bundle_smoke_path=args.candidate_app_bundle_smoke,
            expected_sha256=args.expected_sha256,
            expected_source_commit=args.expected_source_commit,
            expected_candidate_manifest_sha256=args.expected_candidate_manifest_sha256,
        )
    except EvidenceError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"OpenEvo Desktop real-science evidence passed: {args.evidence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
