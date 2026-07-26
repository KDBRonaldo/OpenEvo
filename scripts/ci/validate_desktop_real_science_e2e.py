#!/usr/bin/env python3
"""Validate exact-candidate Desktop v2 real-science evidence before publication."""

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
RELEASE_CANDIDATE_SCHEMA_VERSION = 9
MAX_EVIDENCE_BYTES = 128 * 1024
EVIDENCE_SCHEMA_IDENTITY = {"schema_version": "2"}
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SOURCE_PATTERN = re.compile(r"^[0-9a-f]{40}$")
TARGETS = ("agent_system", "skill_bundle", "text_memory")
REQUIRED_TASK_EVENT_TYPES = frozenset(
    {
        "task_admitted",
        "attempt_appended",
        "dataset_sealed",
        "evolution_revision_committed",
        "runtime_context_committed",
        "project_head_activated",
    }
)
VERIFICATION_SCOPE = [
    "exact_candidate_app_sidecar",
    "system_openssh_remote_workspace",
    "daemon_core_v2",
    "codex_subscription_transcript",
    "atomic_successor_project_heads",
    "next_task_runtime_context_reuse",
    "packaged_renderer_v2_observability",
]


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
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or any(character in value for character in ("\x00", "\r", "\n"))
    ):
        raise EvidenceError(f"{label} is not non-empty canonical text")
    return value


def _asset_identity(value: object, label: str) -> dict[str, object]:
    asset = _exact_mapping(value, label, {"sha256", "byte_size"})
    _sha256(asset["sha256"], f"{label} digest")
    _positive_int(asset["byte_size"], f"{label} byte size")
    return asset


def _stable_json_file(path: Path, *, maximum_bytes: int, label: str) -> tuple[bytes, object]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise EvidenceError(f"{label} is unreadable") from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or not 0 < metadata.st_size <= maximum_bytes
    ):
        raise EvidenceError(f"{label} is not a stable regular file")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise EvidenceError(f"{label} is unreadable") from exc
    if len(content) != metadata.st_size:
        raise EvidenceError(f"{label} changed while it was read")
    try:
        payload = json.loads(content.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"{label} is not valid JSON") from exc
    return content, payload


def _read_candidate_manifest(
    path: Path,
    *,
    expected_sha256: str,
    expected_source_commit: str,
) -> dict[str, object]:
    content, payload = _stable_json_file(
        path,
        maximum_bytes=1024 * 1024,
        label="candidate manifest",
    )
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise EvidenceError("candidate manifest digest mismatch")
    if content != _canonical_json(payload):
        raise EvidenceError("candidate manifest is not canonical JSON")
    manifest = _exact_mapping(
        payload,
        "candidate manifest",
        {
            "core",
            "daemon",
            "files",
            "macos",
            "managed_runtime",
            "release",
            "schema_version",
            "source_commit",
            "version",
        },
    )
    if (
        manifest["schema_version"] != RELEASE_CANDIDATE_SCHEMA_VERSION
        or manifest["source_commit"] != expected_source_commit
        or manifest["version"] != "0.1.9"
    ):
        raise EvidenceError("candidate manifest identity is invalid")
    release = _exact_mapping(
        manifest["release"],
        "candidate release",
        {
            "app_bundle_signature",
            "channel",
            "developer_id_signed",
            "macos_code_signing",
            "notarized",
            "quarantine_removal_tested",
        },
    )
    if release != {
        "app_bundle_signature": "adhoc",
        "channel": "unsigned-preview",
        "developer_id_signed": False,
        "macos_code_signing": {
            "disable_library_validation": False,
            "hardened_runtime": False,
            "identity": "adhoc",
        },
        "notarized": False,
        "quarantine_removal_tested": True,
    }:
        raise EvidenceError("candidate macOS signing policy is invalid")

    files = manifest["files"]
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

    managed_runtime = _mapping(manifest["managed_runtime"], "managed runtime")
    archive = _exact_mapping(
        managed_runtime.get("archive"),
        "managed runtime archive",
        {"filename", "sha256", "byte_size"},
    )
    _nonempty_text(archive["filename"], "managed runtime filename")
    _sha256(archive["sha256"], "managed runtime digest")
    _positive_int(archive["byte_size"], "managed runtime byte size")

    macos = _exact_mapping(
        manifest["macos"],
        "candidate macOS identity",
        {
            "architecture",
            "minimum_system_version",
            "native_architectures",
            "rust_target",
            "rust_toolchain",
            "ssh_askpass_helper",
        },
    )
    helper = _exact_mapping(
        macos["ssh_askpass_helper"],
        "candidate SSH askpass helper",
        {"architecture", "byte_size", "mode", "relative_path", "sha256", "signature"},
    )
    if (
        helper["mode"] != "0755"
        or helper["relative_path"] != "Contents/MacOS/openevo-ssh-askpass"
        or helper["signature"] != "adhoc"
    ):
        raise EvidenceError("candidate SSH askpass helper identity is invalid")
    _sha256(helper["sha256"], "candidate SSH askpass helper digest")
    _positive_int(helper["byte_size"], "candidate SSH askpass helper byte size")
    manifest["_roles"] = roles
    manifest["_runtime_archive"] = archive
    manifest["_ssh_askpass_helper"] = helper
    return manifest


def _read_candidate_app_smoke(
    path: Path,
    *,
    role: Mapping[str, object],
    desktop_dmg_role: Mapping[str, object],
) -> str:
    content, payload = _stable_json_file(
        path,
        maximum_bytes=1024 * 1024,
        label="candidate app smoke",
    )
    if (
        path.name != role.get("filename")
        or len(content) != role.get("byte_size")
        or hashlib.sha256(content).hexdigest() != role.get("sha256")
    ):
        raise EvidenceError("candidate app smoke does not match the candidate manifest")
    smoke = _exact_mapping(
        payload,
        "candidate app smoke",
        {
            "binary_sha256",
            "bundled_external_bin",
            "bundled_external_bin_resolved",
            "launch_origin",
            "mach_o",
            "native_executable",
            "native_executable_fd_handoff",
            "native_listener_fd_handoff",
            "process_group_cleanup",
            "renderer_ready",
            "schema_version",
            "sidecar_ready",
            "source_dmg",
        },
    )
    binary = _exact_mapping(
        smoke["binary_sha256"],
        "candidate binary digests",
        {"native_executable", "bundled_external_bin"},
    )
    if (
        smoke["schema_version"] != 3
        or smoke["launch_origin"] != "mounted_dmg"
        or smoke["bundled_external_bin"] != "openevo-desktop-sidecar"
        or smoke["source_dmg"]
        != {
            "filename": desktop_dmg_role.get("filename"),
            "sha256": desktop_dmg_role.get("sha256"),
        }
        or any(
            smoke[key] is not True
            for key in (
                "renderer_ready",
                "sidecar_ready",
                "bundled_external_bin_resolved",
                "native_listener_fd_handoff",
                "native_executable_fd_handoff",
                "process_group_cleanup",
            )
        )
    ):
        raise EvidenceError("candidate app smoke does not prove the packaged app")
    _sha256(binary["native_executable"], "candidate native executable digest")
    return _sha256(binary["bundled_external_bin"], "candidate packaged sidecar digest")


def _require_asset_match(
    evidence: Mapping[str, object],
    candidate: Mapping[str, object],
    label: str,
) -> None:
    if (
        evidence.get("sha256") != candidate.get("sha256")
        or evidence.get("byte_size") != candidate.get("byte_size")
    ):
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


def _release_contract() -> Mapping[str, object]:
    try:
        payload = json.loads(
            (REPOSITORY_ROOT / "desktop/release-contract.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError("release contract is unavailable") from exc
    contract = _mapping(payload, "release contract")
    return _mapping(contract.get("v019"), "v0.1.9 release contract")


def _project_head(value: object, label: str) -> dict[str, object]:
    head = _exact_mapping(
        value,
        label,
        {
            "project_head_id_sha256",
            "generation",
            "predecessor_project_head_id_sha256",
            "manifest_sha256",
            "workspace_snapshot",
            "evolution_revision",
            "runtime_context_snapshot",
            "effective_execution_snapshot",
        },
    )
    _sha256(head["project_head_id_sha256"], f"{label} ID")
    _nonnegative_int(head["generation"], f"{label} generation")
    predecessor = head["predecessor_project_head_id_sha256"]
    if predecessor is not None:
        _sha256(predecessor, f"{label} predecessor")
    _sha256(head["manifest_sha256"], f"{label} manifest")

    workspace = _exact_mapping(
        head["workspace_snapshot"],
        f"{label} workspace snapshot",
        {"workspace_snapshot_id_sha256", "manifest_sha256", "entry_count", "byte_size"},
    )
    _sha256(workspace["workspace_snapshot_id_sha256"], f"{label} workspace ID")
    _sha256(workspace["manifest_sha256"], f"{label} workspace manifest")
    _nonnegative_int(workspace["entry_count"], f"{label} workspace entry count")
    _nonnegative_int(workspace["byte_size"], f"{label} workspace byte size")

    revision = _exact_mapping(
        head["evolution_revision"],
        f"{label} Evolution Revision",
        {"evolution_revision_id_sha256", "manifest_sha256", "artifact_count"},
    )
    _sha256(revision["evolution_revision_id_sha256"], f"{label} Evolution Revision ID")
    _sha256(revision["manifest_sha256"], f"{label} Evolution Revision manifest")
    _nonnegative_int(revision["artifact_count"], f"{label} artifact count")

    runtime = _exact_mapping(
        head["runtime_context_snapshot"],
        f"{label} Runtime Context Snapshot",
        {
            "runtime_context_snapshot_id_sha256",
            "manifest_sha256",
            "runtime_contract_sha256",
            "registry_sha256",
        },
    )
    for key in runtime:
        _sha256(runtime[key], f"{label} Runtime Context {key}")

    execution = _exact_mapping(
        head["effective_execution_snapshot"],
        f"{label} Effective Execution Snapshot",
        {
            "effective_execution_snapshot_id_sha256",
            "snapshot_sha256",
            "producer_id_sha256",
            "mode",
            "capture_mode",
            "token_level_metrics_available",
        },
    )
    for key in (
        "effective_execution_snapshot_id_sha256",
        "snapshot_sha256",
        "producer_id_sha256",
    ):
        _sha256(execution[key], f"{label} execution {key}")
    if (
        execution["mode"] != "codex_subscription_transcript"
        or execution["capture_mode"] != "transcript"
        or execution["token_level_metrics_available"] is not False
    ):
        raise EvidenceError(f"{label} execution snapshot is not subscription transcript")
    return head


def _task(value: object, *, ordinal: int) -> dict[str, object]:
    label = f"Task {ordinal}"
    task = _exact_mapping(
        value,
        label,
        {
            "ordinal",
            "task_id_sha256",
            "state",
            "task_admission_id_sha256",
            "admission_sha256",
            "authoritative_attempt_id_sha256",
            "attempt_count",
            "predecessor_project_head",
            "context_project_head",
            "successor_project_head",
            "transition_id_sha256",
            "transition_state",
            "timeline_event_types",
            "timeline_event_count",
        },
    )
    if task["ordinal"] != ordinal or task["state"] != "completed":
        raise EvidenceError(f"{label} did not complete")
    for key in (
        "task_id_sha256",
        "task_admission_id_sha256",
        "admission_sha256",
        "authoritative_attempt_id_sha256",
        "transition_id_sha256",
    ):
        _sha256(task[key], f"{label} {key}")
    _positive_int(task["attempt_count"], f"{label} attempt count")
    if task["transition_state"] != "committed":
        raise EvidenceError(f"{label} successor transition was not committed")
    event_types = task["timeline_event_types"]
    if (
        not isinstance(event_types, list)
        or event_types != sorted(set(event_types))
        or not REQUIRED_TASK_EVENT_TYPES.issubset(event_types)
        or not isinstance(task["timeline_event_count"], int)
        or task["timeline_event_count"] < len(event_types)
    ):
        raise EvidenceError(f"{label} v2 lifecycle timeline is incomplete")
    task["predecessor_project_head"] = _project_head(
        task["predecessor_project_head"], f"{label} predecessor"
    )
    task["context_project_head"] = _project_head(
        task["context_project_head"], f"{label} context"
    )
    task["successor_project_head"] = _project_head(
        task["successor_project_head"], f"{label} successor"
    )
    return task


def _load_runner() -> ModuleType:
    path = REPOSITORY_ROOT / "scripts/e2e/desktop_real_science_e2e.py"
    spec = importlib.util.spec_from_file_location(
        "openevo_desktop_real_science_e2e", path
    )
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
    _sha256(expected_sha256, "expected evidence digest")
    if SOURCE_PATTERN.fullmatch(expected_source_commit) is None:
        raise EvidenceError("expected source commit is invalid")
    _sha256(expected_candidate_manifest_sha256, "expected candidate manifest digest")
    candidate = _read_candidate_manifest(
        candidate_manifest_path,
        expected_sha256=expected_candidate_manifest_sha256,
        expected_source_commit=expected_source_commit,
    )
    roles = _mapping(candidate["_roles"], "candidate roles")
    candidate_sidecar_sha256 = _read_candidate_app_smoke(
        candidate_app_bundle_smoke_path,
        role=_mapping(roles["app_bundle_smoke"], "candidate app smoke role"),
        desktop_dmg_role=_mapping(roles["desktop_dmg"], "candidate DMG role"),
    )

    payload_bytes, payload = _stable_json_file(
        path,
        maximum_bytes=MAX_EVIDENCE_BYTES,
        label="real-science evidence",
    )
    if hashlib.sha256(payload_bytes).hexdigest() != expected_sha256:
        raise EvidenceError("real-science evidence digest mismatch")
    if payload_bytes != _canonical_json(payload):
        raise EvidenceError("real-science evidence is not canonical JSON")
    preflight = _mapping(payload, "real-science evidence")
    if preflight.get("schema_version") != EVIDENCE_SCHEMA_IDENTITY["schema_version"]:
        raise EvidenceError("real-science evidence schema version is not v2")
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
            "task_count",
            "remote",
            "project",
            "tasks",
            "reuse",
            "renderer",
            "renderer_observability_verified",
            "renderer_boundary",
            "candidate_tauri_launch_verified",
            "cleanup",
            "finished_at",
        },
    )
    try:
        _load_runner()._audit_evidence(document, private_values=())
    except Exception as exc:
        raise EvidenceError("real-science evidence violates the privacy contract") from exc
    if (
        document["kind"] != "openevo_desktop_real_science_e2e"
        or document["issue"] != 163
        or document["real_process_boundary"] is not True
        or document["outcome"] != "passed"
        or document["run_mode"] != "two_task_subscription_release"
        or document["verification_scope"] != VERIFICATION_SCOPE
        or document["task_count"] != 2
    ):
        raise EvidenceError("real-science v2 verdict is incomplete")
    if _timestamp(document["finished_at"], "finish time") < _timestamp(
        document["started_at"], "start time"
    ):
        raise EvidenceError("real-science timestamps are reversed")

    assets = _exact_mapping(
        document["release_assets"],
        "release assets",
        {
            "sidecar",
            "ssh_askpass_helper",
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
    sidecar = _asset_identity(assets["sidecar"], "candidate sidecar")
    helper_asset = _asset_identity(assets["ssh_askpass_helper"], "candidate askpass helper")
    wheel = _exact_mapping(
        assets["core_wheel"],
        "Core wheel",
        {"sha256", "byte_size", "filename", "distribution", "version"},
    )
    _sha256(wheel["sha256"], "Core wheel digest")
    _positive_int(wheel["byte_size"], "Core wheel byte size")
    if wheel["distribution"] != "openevo" or wheel["version"] != "0.1.9":
        raise EvidenceError("Core wheel release identity is invalid")
    framework_lock = _exact_mapping(
        assets["framework_lock"],
        "framework lock",
        {"sha256", "byte_size", "distribution_digest"},
    )
    _sha256(framework_lock["sha256"], "framework lock digest")
    _positive_int(framework_lock["byte_size"], "framework lock byte size")
    if framework_lock["distribution_digest"] != wheel["sha256"]:
        raise EvidenceError("framework lock does not bind the Core wheel")
    runtime = _asset_identity(assets["managed_runtime_archive"], "managed runtime")
    daemon_bundle = _asset_identity(assets["daemon_bundle"], "Daemon bundle")
    daemon_manifest = _asset_identity(assets["daemon_manifest"], "Daemon manifest")
    _require_asset_match(wheel, _mapping(roles["core_wheel"], "candidate Core wheel"), "Core wheel")
    _require_asset_match(framework_lock, _mapping(roles["framework_lock"], "candidate framework lock"), "framework lock")
    _require_asset_match(runtime, _mapping(candidate["_runtime_archive"], "candidate runtime"), "managed runtime")
    _require_asset_match(daemon_bundle, _mapping(roles["daemon_bundle"], "candidate Daemon bundle"), "Daemon bundle")
    _require_asset_match(daemon_manifest, _mapping(roles["daemon_manifest"], "candidate Daemon manifest"), "Daemon manifest")
    helper = _mapping(candidate["_ssh_askpass_helper"], "candidate askpass helper")
    if (
        sidecar["sha256"] != candidate_sidecar_sha256
        or helper_asset["sha256"] != helper["sha256"]
        or helper_asset["byte_size"] != helper["byte_size"]
        or assets["exact_external_release_assets_verified"] is not True
        or assets["slim_sidecar_excludes_remote_release_assets_verified"] is not True
    ):
        raise EvidenceError("candidate packaged binary binding is invalid")
    external = _exact_mapping(
        assets["external_release_assets"],
        "external release assets",
        {"source_commit", "registry_digest", "manifest_sha256", "byte_size"},
    )
    registry_sha256 = _sha256(external["registry_digest"], "registry digest")
    _sha256(external["manifest_sha256"], "external asset manifest")
    _positive_int(external["byte_size"], "external asset manifest byte size")
    if external["source_commit"] != expected_source_commit:
        raise EvidenceError("external release assets use another source commit")

    binding = _exact_mapping(
        document["renderer_candidate_binding"],
        "renderer candidate binding",
        {
            "source_commit",
            "candidate_version",
            "release_candidate_manifest_sha256",
            "desktop_dmg_sha256",
            "app_bundle_smoke_sha256",
            "candidate_packaged_sidecar_sha256",
            "candidate_ssh_askpass_helper_sha256",
            "candidate_native_sidecar_smoke_verified",
            "exact_candidate_packaged_sidecar_verified",
            "exact_candidate_ssh_askpass_helper_verified",
            "packaged_web_manifest_sha256",
            "playwright_candidate_evidence_sha256",
            "packaged_web_build_digest",
            "source_checkout_verified",
        },
    )
    expected_binding = {
        "source_commit": expected_source_commit,
        "candidate_version": "0.1.9",
        "release_candidate_manifest_sha256": expected_candidate_manifest_sha256,
        "desktop_dmg_sha256": roles["desktop_dmg"]["sha256"],
        "app_bundle_smoke_sha256": roles["app_bundle_smoke"]["sha256"],
        "candidate_packaged_sidecar_sha256": candidate_sidecar_sha256,
        "candidate_ssh_askpass_helper_sha256": helper["sha256"],
        "candidate_native_sidecar_smoke_verified": True,
        "exact_candidate_packaged_sidecar_verified": True,
        "exact_candidate_ssh_askpass_helper_verified": True,
        "packaged_web_manifest_sha256": roles["packaged_web_manifest"]["sha256"],
        "playwright_candidate_evidence_sha256": roles["playwright_evidence"]["sha256"],
        "source_checkout_verified": True,
    }
    if any(binding.get(key) != value for key, value in expected_binding.items()):
        raise EvidenceError("renderer candidate binding does not identify the exact candidate")
    build_digest = _sha256(binding["packaged_web_build_digest"], "packaged web build")

    release_contract = _release_contract()
    desktop = _exact_mapping(
        document["desktop"],
        "Desktop identity",
        {
            "source_commit",
            "release_version",
            "mutation_major",
            "openapi_sha256",
            "event_schema_sha256",
            "build_id",
            "provider_kind",
            "build_channel",
            "feature_flags",
            "feature_set_sha256",
            "required_core_api_major",
            "mutation_compatible",
            "v2_only_negotiation_verified",
            "authenticated_session_probe",
            "unauthenticated_session_rejected",
        },
    )
    expected_features = release_contract.get("required_desktop_feature_flags")
    if (
        desktop["source_commit"] != expected_source_commit
        or desktop["release_version"] != "0.1.9"
        or desktop["mutation_major"] != 2
        or desktop["openapi_sha256"]
        not in release_contract.get("accepted_desktop_openapi_digests", [])
        or desktop["event_schema_sha256"]
        not in release_contract.get("accepted_desktop_event_schema_digests", [])
        or desktop["provider_kind"] != "desktop_sidecar"
        or desktop["build_channel"] != "release"
        or desktop["feature_flags"] != expected_features
        or desktop["required_core_api_major"] != 2
        or desktop["mutation_compatible"] is not True
        or desktop["v2_only_negotiation_verified"] is not True
        or desktop["authenticated_session_probe"] is not True
        or desktop["unauthenticated_session_rejected"] is not True
    ):
        raise EvidenceError("Desktop v2 release identity is invalid")
    _sha256(desktop["build_id"], "Desktop build ID")
    expected_feature_digest = hashlib.sha256(
        json.dumps(
            expected_features,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    ).hexdigest()
    if desktop["feature_set_sha256"] != expected_feature_digest:
        raise EvidenceError("Desktop feature-set digest is invalid")

    remote = _exact_mapping(
        document["remote"],
        "remote workspace",
        {
            "connection_authority",
            "catalog_selection_verified",
            "system_openssh_final_authority_verified",
            "core_api_major",
            "core_registry_sha256",
        },
    )
    if (
        remote["connection_authority"] != "system_openssh"
        or remote["catalog_selection_verified"] is not True
        or remote["system_openssh_final_authority_verified"] is not True
        or remote["core_api_major"] != 2
        or remote["core_registry_sha256"] != registry_sha256
    ):
        raise EvidenceError("remote workspace did not use system OpenSSH and Core v2")

    project = _exact_mapping(
        document["project"],
        "project evidence",
        {
            "project_id_sha256",
            "execution",
            "target_ids",
            "selected_methods",
            "registry_sha256",
            "validation_check_counts",
            "initial_project_head",
            "active_project_head",
        },
    )
    project_id_sha256 = _sha256(project["project_id_sha256"], "project ID")
    execution = _exact_mapping(
        project["execution"],
        "project execution",
        {
            "mode",
            "capture_mode",
            "token_level_metrics_available",
            "harness_id",
            "codex_model",
            "reasoning_effort",
            "task_network_allow_internet",
        },
    )
    if execution != {
        "mode": "codex_subscription_transcript",
        "capture_mode": "transcript",
        "token_level_metrics_available": False,
        "harness_id": "codex",
        "codex_model": "gpt-5.3-codex-spark",
        "reasoning_effort": "high",
        "task_network_allow_internet": True,
    }:
        raise EvidenceError("project execution is not the release Subscription profile")
    methods = _exact_mapping(project["selected_methods"], "selected methods", set(TARGETS))
    if (
        project["target_ids"] != list(TARGETS)
        or methods["agent_system"] != "auto"
        or any(not isinstance(methods[target], str) or not methods[target] for target in TARGETS)
        or project["registry_sha256"] != registry_sha256
        or not isinstance(project["validation_check_counts"], list)
        or len(project["validation_check_counts"]) != 2
        or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 1
            for value in project["validation_check_counts"]
        )
    ):
        raise EvidenceError("project registry validation or target selection is incomplete")
    initial_head = _project_head(project["initial_project_head"], "initial Project Head")
    active_head = _project_head(project["active_project_head"], "active Project Head")
    if (
        initial_head["generation"] != 0
        or initial_head["predecessor_project_head_id_sha256"] is not None
        or _mapping(initial_head["evolution_revision"], "initial revision")["artifact_count"] != 0
        or active_head["generation"] != 2
        or _mapping(active_head["evolution_revision"], "active revision")["artifact_count"] != 3
    ):
        raise EvidenceError("Project Head generation or output count is invalid")

    task_values = document["tasks"]
    if not isinstance(task_values, list) or len(task_values) != 2:
        raise EvidenceError("exactly two Tasks are required")
    first = _task(task_values[0], ordinal=1)
    second = _task(task_values[1], ordinal=2)
    first_predecessor = _mapping(first["predecessor_project_head"], "Task 1 predecessor")
    first_context = _mapping(first["context_project_head"], "Task 1 context")
    first_successor = _mapping(first["successor_project_head"], "Task 1 successor")
    second_predecessor = _mapping(second["predecessor_project_head"], "Task 2 predecessor")
    second_context = _mapping(second["context_project_head"], "Task 2 context")
    second_successor = _mapping(second["successor_project_head"], "Task 2 successor")
    if (
        first_predecessor != initial_head
        or first_context != initial_head
        or first_successor["generation"] != 1
        or first_successor["predecessor_project_head_id_sha256"]
        != initial_head["project_head_id_sha256"]
        or _mapping(first_successor["evolution_revision"], "Task 1 revision")["artifact_count"] != 3
        or second_predecessor != first_successor
        or second_context != first_successor
        or second_successor != active_head
        or second_successor["predecessor_project_head_id_sha256"]
        != first_successor["project_head_id_sha256"]
        or _mapping(second_successor["evolution_revision"], "Task 2 revision")["artifact_count"] != 3
    ):
        raise EvidenceError("two-Task Project Head and Runtime Context reuse relation is invalid")
    identity_keys = (
        "task_id_sha256",
        "task_admission_id_sha256",
        "authoritative_attempt_id_sha256",
        "transition_id_sha256",
    )
    if any(first[key] == second[key] for key in identity_keys):
        raise EvidenceError("Task identities were reused")

    reuse = _exact_mapping(
        document["reuse"],
        "reuse evidence",
        {
            "first_context_excluded_own_successor",
            "second_admission_pinned_first_successor",
            "second_context_pinned_first_successor",
            "second_runtime_context_equals_first_successor",
        },
    )
    if any(value is not True for value in reuse.values()):
        raise EvidenceError("next-Task Runtime Context reuse was not proven")

    renderer = _exact_mapping(
        document["renderer"],
        "renderer evidence",
        {
            "schema_version",
            "kind",
            "outcome",
            "provider_kind",
            "source_commit",
            "packaged_web_build_digest",
            "desktop_api_major",
            "renderer_ready",
            "builtin_sample_count",
            "project_id_sha256",
            "task_count",
            "task_id_sha256",
            "active_project_head_generation",
            "evolution_artifact_count",
            "system_openssh_workspace_verified",
            "remote_target_controls_verified",
            "selected_methods",
            "observed_route_kinds",
            "screenshot_sha256",
        },
    )
    if (
        renderer["schema_version"] != "2"
        or renderer["kind"] != "openevo_desktop_live_renderer_observability"
        or renderer["outcome"] != "passed"
        or renderer["provider_kind"] != "desktop_sidecar"
        or renderer["source_commit"] != expected_source_commit
        or renderer["packaged_web_build_digest"] != build_digest
        or renderer["desktop_api_major"] != 2
        or renderer["renderer_ready"] is not True
        or renderer["builtin_sample_count"] != 2
        or renderer["project_id_sha256"] != project_id_sha256
        or renderer["task_count"] != 2
        or renderer["task_id_sha256"]
        != [first["task_id_sha256"], second["task_id_sha256"]]
        or renderer["active_project_head_generation"] != 2
        or renderer["evolution_artifact_count"] != 3
        or renderer["system_openssh_workspace_verified"] is not True
        or renderer["remote_target_controls_verified"] is not True
        or renderer["selected_methods"] != methods
        or renderer["observed_route_kinds"] != ["desktop_v2", "packaged_web"]
    ):
        raise EvidenceError("packaged renderer v2 observation is incomplete")
    _sha256(renderer["screenshot_sha256"], "renderer screenshot")
    if (
        document["renderer_observability_verified"] is not True
        or document["renderer_boundary"] != "packaged_web_to_live_desktop_v2"
        or document["candidate_tauri_launch_verified"] is not True
    ):
        raise EvidenceError("candidate renderer/native boundary was not verified")

    cleanup = _exact_mapping(
        document["cleanup"],
        "cleanup evidence",
        {
            "active_task_cleanup_required",
            "active_task_cancel_requested",
            "active_task_terminal",
            "active_task_cleanup_succeeded",
            "desktop_disconnect_succeeded",
            "sidecar_shutdown_succeeded",
            "core_ownership_release_requested",
        },
    )
    if cleanup != {
        "active_task_cleanup_required": False,
        "active_task_cancel_requested": False,
        "active_task_terminal": True,
        "active_task_cleanup_succeeded": True,
        "desktop_disconnect_succeeded": True,
        "sidecar_shutdown_succeeded": True,
        "core_ownership_release_requested": True,
    }:
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
    print(f"OpenEvo Desktop v2 real-science evidence passed: {args.evidence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
