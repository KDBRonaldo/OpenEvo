from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType
from typing import Mapping

import pytest


TARGETS = ("agent_system", "skill_bundle", "text_memory")
EVENT_TYPES = sorted(
    {
        "task_admitted",
        "attempt_appended",
        "dataset_sealed",
        "evolution_revision_committed",
        "runtime_context_committed",
        "project_head_activated",
        "transition_changed",
    }
)
SOURCE = "1" * 40


def _load_validator() -> ModuleType:
    path = Path("scripts/ci/validate_desktop_real_science_e2e.py").resolve()
    spec = importlib.util.spec_from_file_location("validate_desktop_real_science_e2e", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _app_bundle_smoke() -> dict[str, object]:
    return {
        "schema_version": 3,
        "native_executable": "OpenEvo Desktop",
        "bundled_external_bin": "openevo-desktop-sidecar",
        "renderer_ready": True,
        "sidecar_ready": True,
        "bundled_external_bin_resolved": True,
        "native_listener_fd_handoff": True,
        "native_executable_fd_handoff": True,
        "process_group_cleanup": True,
        "mach_o": {},
        "launch_origin": "mounted_dmg",
        "source_dmg": {
            "filename": "OpenEvo-Desktop-0.1.10-aarch64.dmg",
            "sha256": _digest("desktop_dmg"),
        },
        "binary_sha256": {
            "native_executable": _digest("candidate-native"),
            "bundled_external_bin": _digest("candidate-sidecar"),
        },
    }


def _candidate() -> tuple[dict[str, object], bytes]:
    smoke_bytes = _canonical_bytes(_app_bundle_smoke())
    files = [
        ("core_wheel", "openevo-0.1.10-py3-none-any.whl", 1001),
        ("framework_lock", "framework-lock.json", 1002),
        ("daemon_bundle", "openevo-daemon-linux-x86_64", 1003),
        ("daemon_manifest", "openevo-daemon-bundle.json", 1004),
        ("desktop_dmg", "OpenEvo-Desktop-0.1.10-aarch64.dmg", 1005),
        ("packaged_web_manifest", "packaged-web-manifest.json", 1006),
        ("playwright_evidence", "playwright-candidate-evidence.json", 1007),
        ("app_bundle_smoke", "app-bundle-smoke.json", len(smoke_bytes)),
    ]
    candidate = {
        "schema_version": 10,
        "source_commit": SOURCE,
        "version": "0.1.10",
        "release": {
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
        },
        "macos": {
            "architecture": "aarch64",
            "minimum_system_version": "12.0",
            "native_architectures": {},
            "rust_target": "aarch64-apple-darwin",
            "rust_toolchain": "1.88.0",
            "ssh_askpass_helper": {
                "architecture": "arm64",
                "byte_size": 88,
                "mode": "0755",
                "relative_path": "Contents/MacOS/openevo-ssh-askpass",
                "sha256": _digest("candidate-helper"),
                "signature": "adhoc",
            },
        },
        "managed_runtime": {
            "archive": {
                "filename": "openevo-science-runtime.tar.gz",
                "sha256": _digest("managed_runtime_archive"),
                "byte_size": 1008,
            }
        },
        "core": {},
        "daemon": {},
        "desktop_contract": {
            "release_version": "0.1.10",
            "mutation_major": 2,
            "openapi_sha256": "4cd120dab0797e223ba892b0382fd61f8e4156318df9ab6676236c201191a98a",
            "event_schema_sha256": "515b6d90e9ebdf3f5b4f7c4a57a1924dc85011536d9396b1ab3a5dc73fc48b6b",
            "feature_flags": [
                "core_control_v2",
                "daemon_bundle_v2",
                "event_replay_v2",
                "host_key_review",
                "lifecycle_operations_v2",
                "lifecycle_process_logs_v2",
                "mutation_idempotency_v2",
                "native_askpass",
                "system_openssh_profiles",
                "task_admission_v2",
            ],
            "feature_set_sha256": "67b6ad24f67de611f32c365079fcf8384c800d0855effaa64e1ff24251a7acda",
        },
        "lifecycle_evidence": {
            "schema_version": 1,
            "operation_kind": "project_create",
            "reservation_status": 202,
            "maximum_reservation_latency_ms": 15000,
            "minimum_terminal_duration_ms": 15000,
            "minimum_ordered_phase_count": 2,
            "allowed_process_log_sources": [
                "daemon_stderr",
                "daemon_stdout",
                "ssh_stderr",
                "ssh_stdout",
            ],
            "require_sse_reconnect": True,
            "require_relaunch_recovery": True,
            "require_stable_action_id": True,
            "require_single_core_project": True,
            "require_single_applied_mutation": True,
            "require_secret_canary_absence": True,
            "require_renderer_secret_canary_absence": True,
        },
        "files": [
            {
                "role": role,
                "filename": filename,
                "sha256": hashlib.sha256(smoke_bytes).hexdigest()
                if role == "app_bundle_smoke"
                else _digest(role),
                "byte_size": size,
            }
            for role, filename, size in files
        ],
    }
    return candidate, smoke_bytes


def _head(label: str, generation: int, predecessor: str | None, artifact_count: int) -> dict[str, object]:
    return {
        "project_head_id_sha256": _digest(f"{label}-head"),
        "generation": generation,
        "predecessor_project_head_id_sha256": predecessor,
        "manifest_sha256": _digest(f"{label}-head-manifest"),
        "workspace_snapshot": {
            "workspace_snapshot_id_sha256": _digest(f"{label}-workspace"),
            "manifest_sha256": _digest(f"{label}-workspace-manifest"),
            "entry_count": generation,
            "byte_size": generation * 10,
        },
        "evolution_revision": {
            "evolution_revision_id_sha256": _digest(f"{label}-evolution"),
            "manifest_sha256": _digest(f"{label}-evolution-manifest"),
            "artifact_count": artifact_count,
        },
        "runtime_context_snapshot": {
            "runtime_context_snapshot_id_sha256": _digest(f"{label}-context"),
            "manifest_sha256": _digest(f"{label}-context-manifest"),
            "runtime_contract_sha256": _digest(f"{label}-runtime-contract"),
            "registry_sha256": _digest("registry"),
        },
        "effective_execution_snapshot": {
            "effective_execution_snapshot_id_sha256": _digest(f"{label}-execution"),
            "snapshot_sha256": _digest(f"{label}-execution-snapshot"),
            "producer_id_sha256": _digest(f"{label}-producer"),
            "mode": "codex_subscription_transcript",
            "capture_mode": "transcript",
            "token_level_metrics_available": False,
        },
    }


def _task(ordinal: int, predecessor: dict[str, object], successor: dict[str, object]) -> dict[str, object]:
    return {
        "ordinal": ordinal,
        "task_id_sha256": _digest(f"task-{ordinal}"),
        "state": "completed",
        "task_admission_id_sha256": _digest(f"admission-id-{ordinal}"),
        "admission_sha256": _digest(f"admission-{ordinal}"),
        "authoritative_attempt_id_sha256": _digest(f"attempt-{ordinal}"),
        "attempt_count": 1,
        "predecessor_project_head": predecessor,
        "context_project_head": predecessor,
        "successor_project_head": successor,
        "transition_id_sha256": _digest(f"transition-{ordinal}"),
        "transition_state": "committed",
        "timeline_event_types": EVENT_TYPES,
        "timeline_event_count": len(EVENT_TYPES),
    }


def _payload(candidate: Mapping[str, object], candidate_digest: str) -> dict[str, object]:
    roles = {item["role"]: item for item in candidate["files"]}  # type: ignore[index]
    runtime = candidate["managed_runtime"]["archive"]  # type: ignore[index]
    helper = candidate["macos"]["ssh_askpass_helper"]  # type: ignore[index]
    head0 = _head("head-0", 0, None, 0)
    head1 = _head("head-1", 1, head0["project_head_id_sha256"], 3)  # type: ignore[arg-type]
    head2 = _head("head-2", 2, head1["project_head_id_sha256"], 3)  # type: ignore[arg-type]
    methods = {
        "agent_system": "auto",
        "skill_bundle": "reference_skill_bundle",
        "text_memory": "reference_text_memory",
    }
    release_contract = json.loads(Path("desktop/release-contract.json").read_text(encoding="utf-8"))["v0110"]
    features = release_contract["required_desktop_feature_flags"]
    feature_digest = hashlib.sha256(
        json.dumps(features, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")
    ).hexdigest()
    return {
        "schema_version": "3",
        "kind": "openevo_desktop_real_science_e2e",
        "issue": 220,
        "real_process_boundary": True,
        "outcome": "passed",
        "started_at": "2026-07-26T01:00:00Z",
        "release_assets": {
            "sidecar": {"sha256": _digest("candidate-sidecar"), "byte_size": 9001},
            "ssh_askpass_helper": {"sha256": helper["sha256"], "byte_size": helper["byte_size"]},
            "core_wheel": {
                "sha256": roles["core_wheel"]["sha256"],
                "byte_size": roles["core_wheel"]["byte_size"],
                "filename": roles["core_wheel"]["filename"],
                "distribution": "openevo",
                "version": "0.1.10",
            },
            "framework_lock": {
                "sha256": roles["framework_lock"]["sha256"],
                "byte_size": roles["framework_lock"]["byte_size"],
                "distribution_digest": roles["core_wheel"]["sha256"],
            },
            "managed_runtime_archive": {"sha256": runtime["sha256"], "byte_size": runtime["byte_size"]},
            "daemon_bundle": {"sha256": roles["daemon_bundle"]["sha256"], "byte_size": roles["daemon_bundle"]["byte_size"]},
            "daemon_manifest": {"sha256": roles["daemon_manifest"]["sha256"], "byte_size": roles["daemon_manifest"]["byte_size"]},
            "external_release_assets": {
                "source_commit": SOURCE,
                "registry_digest": _digest("registry"),
                "manifest_sha256": _digest("external-manifest"),
                "byte_size": 333,
            },
            "exact_external_release_assets_verified": True,
            "slim_sidecar_excludes_remote_release_assets_verified": True,
        },
        "renderer_candidate_binding": {
            "source_commit": SOURCE,
            "candidate_version": "0.1.10",
            "release_candidate_manifest_sha256": candidate_digest,
            "desktop_dmg_sha256": roles["desktop_dmg"]["sha256"],
            "app_bundle_smoke_sha256": roles["app_bundle_smoke"]["sha256"],
            "candidate_packaged_sidecar_sha256": _digest("candidate-sidecar"),
            "candidate_ssh_askpass_helper_sha256": helper["sha256"],
            "candidate_native_sidecar_smoke_verified": True,
            "exact_candidate_packaged_sidecar_verified": True,
            "exact_candidate_ssh_askpass_helper_verified": True,
            "packaged_web_manifest_sha256": roles["packaged_web_manifest"]["sha256"],
            "playwright_candidate_evidence_sha256": roles["playwright_evidence"]["sha256"],
            "packaged_web_build_digest": _digest("packaged-web-build"),
            "source_checkout_verified": True,
        },
        "desktop": {
            "source_commit": SOURCE,
            "release_version": "0.1.10",
            "mutation_major": 2,
            "openapi_sha256": release_contract["accepted_desktop_openapi_digests"][0],
            "event_schema_sha256": release_contract["accepted_desktop_event_schema_digests"][0],
            "build_id": _digest("build-id"),
            "provider_kind": "desktop_sidecar",
            "build_channel": "release",
            "feature_flags": features,
            "feature_set_sha256": feature_digest,
            "required_core_api_major": 2,
            "mutation_compatible": True,
            "v2_only_negotiation_verified": True,
            "authenticated_session_probe": True,
            "unauthenticated_session_rejected": True,
        },
        "run_mode": "two_task_subscription_release",
        "verification_scope": [
            "exact_candidate_app_sidecar",
            "system_openssh_remote_workspace",
            "daemon_core_v2",
            "codex_subscription_transcript",
            "atomic_successor_project_heads",
            "next_task_runtime_context_reuse",
            "packaged_renderer_v2_observability",
        ],
        "task_count": 2,
        "remote": {
            "connection_authority": "system_openssh",
            "catalog_selection_verified": True,
            "system_openssh_final_authority_verified": True,
            "core_api_major": 2,
            "core_registry_sha256": _digest("registry"),
        },
        "project": {
            "project_id_sha256": _digest("project"),
            "execution": {
                "mode": "codex_subscription_transcript",
                "capture_mode": "transcript",
                "token_level_metrics_available": False,
                "harness_id": "codex",
                "codex_model": "gpt-5.3-codex-spark",
                "reasoning_effort": "high",
                "task_network_allow_internet": True,
            },
            "target_ids": list(TARGETS),
            "selected_methods": methods,
            "registry_sha256": _digest("registry"),
            "validation_check_counts": [7, 7],
            "initial_project_head": head0,
            "active_project_head": head2,
        },
        "tasks": [_task(1, head0, head1), _task(2, head1, head2)],
        "reuse": {
            "first_context_excluded_own_successor": True,
            "second_admission_pinned_first_successor": True,
            "second_context_pinned_first_successor": True,
            "second_runtime_context_equals_first_successor": True,
        },
        "lifecycle": {
            "operation_kind": "project_create",
            "reservation_status": 202,
            "reservation_latency_ms": 250,
            "terminal_duration_ms": 16250,
            "action_id_sha256": _digest("project-create-action"),
            "operation_id_sha256": _digest("project-create-operation"),
            "request_sha256": _digest("project-create-request"),
            "ordered_phases": [
                "queued",
                "remote_preflight",
                "creating_remote_project",
                "verifying_project",
                "activating",
                "finalizing",
            ],
            "process_logs": {
                "entry_count": 2,
                "sources": ["daemon_stdout", "ssh_stderr"],
                "content_sha256": _digest("bounded-process-logs"),
            },
            "sse_reconnect_verified": True,
            "relaunch_recovery_verified": True,
            "stable_action_id_after_relaunch": True,
            "stable_operation_id_after_relaunch": True,
            "mutation_reissued_after_relaunch": False,
            "core_authority": {
                "project_count": 1,
                "project_mapping_count": 1,
                "applied_create_project_mutation_count": 1,
            },
            "secret_canary_sha256": _digest("secret-canary"),
            "secret_canary_absent": True,
        },
        "renderer": {
            "schema_version": "2",
            "kind": "openevo_desktop_live_renderer_observability",
            "outcome": "passed",
            "provider_kind": "desktop_sidecar",
            "source_commit": SOURCE,
            "packaged_web_build_digest": _digest("packaged-web-build"),
            "desktop_api_major": 2,
            "renderer_ready": True,
            "builtin_sample_count": 2,
            "project_id_sha256": _digest("project"),
            "task_count": 2,
            "task_id_sha256": [_digest("task-1"), _digest("task-2")],
            "active_project_head_generation": 2,
            "evolution_artifact_count": 3,
            "system_openssh_workspace_verified": True,
            "remote_target_controls_verified": True,
            "secret_canary_absent": True,
            "selected_methods": methods,
            "observed_route_kinds": ["desktop_v2", "packaged_web"],
            "screenshot_sha256": _digest("screenshot"),
        },
        "renderer_observability_verified": True,
        "renderer_boundary": "packaged_web_to_live_desktop_v2",
        "candidate_tauri_launch_verified": True,
        "cleanup": {
            "active_task_cleanup_required": False,
            "active_task_cancel_requested": False,
            "active_task_terminal": True,
            "active_task_cleanup_succeeded": True,
            "desktop_disconnect_succeeded": True,
            "sidecar_shutdown_succeeded": True,
            "core_ownership_release_requested": True,
        },
        "finished_at": "2026-07-26T02:00:00Z",
    }


def _fixture(module: ModuleType, tmp_path: Path):
    candidate, smoke_bytes = _candidate()
    candidate_bytes = _canonical_bytes(candidate)
    candidate_path = tmp_path / "release-candidate.json"
    candidate_path.write_bytes(candidate_bytes)
    smoke_path = tmp_path / "app-bundle-smoke.json"
    smoke_path.write_bytes(smoke_bytes)
    candidate_digest = hashlib.sha256(candidate_bytes).hexdigest()
    payload = _payload(candidate, candidate_digest)
    evidence_bytes = _canonical_bytes(payload)
    evidence_path = tmp_path / "desktop-real-science-e2e.json"
    evidence_path.write_bytes(evidence_bytes)
    return {
        "candidate": candidate,
        "candidate_path": candidate_path,
        "candidate_digest": candidate_digest,
        "smoke_path": smoke_path,
        "payload": payload,
        "evidence_path": evidence_path,
        "evidence_digest": hashlib.sha256(evidence_bytes).hexdigest(),
    }


def _validate(module: ModuleType, fixture: Mapping[str, object]) -> dict[str, object]:
    return module.validate_evidence(
        fixture["evidence_path"],
        candidate_manifest_path=fixture["candidate_path"],
        candidate_app_bundle_smoke_path=fixture["smoke_path"],
        expected_sha256=fixture["evidence_digest"],
        expected_source_commit=SOURCE,
        expected_candidate_manifest_sha256=fixture["candidate_digest"],
    )


def _rewrite_evidence(fixture: dict[str, object], payload: dict[str, object]) -> None:
    content = _canonical_bytes(payload)
    fixture["evidence_path"].write_bytes(content)  # type: ignore[union-attr]
    fixture["evidence_digest"] = hashlib.sha256(content).hexdigest()


def test_exact_candidate_two_task_v3_evidence_passes(tmp_path: Path) -> None:
    module = _load_validator()
    fixture = _fixture(module, tmp_path)

    assert _validate(module, fixture)["outcome"] == "passed"


def test_schema_v2_real_science_evidence_is_rejected(tmp_path: Path) -> None:
    module = _load_validator()
    fixture = _fixture(module, tmp_path)
    payload = copy.deepcopy(fixture["payload"])
    payload["schema_version"] = "2"
    _rewrite_evidence(fixture, payload)

    with pytest.raises(module.EvidenceError, match="schema"):
        _validate(module, fixture)


def test_candidate_unsigned_macos_policy_change_fails_closed(tmp_path: Path) -> None:
    module = _load_validator()
    fixture = _fixture(module, tmp_path)
    candidate = copy.deepcopy(fixture["candidate"])
    candidate["release"]["notarized"] = True
    content = _canonical_bytes(candidate)
    fixture["candidate_path"].write_bytes(content)
    fixture["candidate_digest"] = hashlib.sha256(content).hexdigest()

    with pytest.raises(module.EvidenceError, match="signing policy"):
        _validate(module, fixture)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("reuse", "second_context_pinned_first_successor"), False),
        (("remote", "connection_authority"), "manual"),
        (("renderer", "desktop_api_major"), 1),
        (("renderer", "secret_canary_absent"), False),
        (("project", "selected_methods", "agent_system"), "concrete"),
        (("tasks", 1, "predecessor_project_head", "generation"), 0),
        (("project", "active_project_head", "evolution_revision", "artifact_count"), 2),
    ],
)
def test_incomplete_v2_release_claim_fails_closed(
    tmp_path: Path,
    path: tuple[object, ...],
    value: object,
) -> None:
    module = _load_validator()
    fixture = _fixture(module, tmp_path)
    payload = copy.deepcopy(fixture["payload"])
    owner: object = payload
    for segment in path[:-1]:
        owner = owner[segment]  # type: ignore[index]
    owner[path[-1]] = value  # type: ignore[index]
    _rewrite_evidence(fixture, payload)

    with pytest.raises(module.EvidenceError):
        _validate(module, fixture)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("lifecycle", "reservation_latency_ms"), 15000),
        (("lifecycle", "terminal_duration_ms"), 15000),
        (("lifecycle", "ordered_phases"), ["finalizing"]),
        (("lifecycle", "process_logs", "sources"), ["desktop"]),
        (("lifecycle", "sse_reconnect_verified"), False),
        (("lifecycle", "relaunch_recovery_verified"), False),
        (("lifecycle", "stable_action_id_after_relaunch"), False),
        (("lifecycle", "stable_operation_id_after_relaunch"), False),
        (("lifecycle", "mutation_reissued_after_relaunch"), True),
        (("lifecycle", "core_authority", "project_count"), 2),
        (("lifecycle", "core_authority", "project_mapping_count"), 2),
        (
            (
                "lifecycle",
                "core_authority",
                "applied_create_project_mutation_count",
            ),
            2,
        ),
        (("lifecycle", "secret_canary_absent"), False),
    ],
)
def test_incomplete_v3_lifecycle_claim_fails_closed(
    tmp_path: Path,
    path: tuple[object, ...],
    value: object,
) -> None:
    module = _load_validator()
    fixture = _fixture(module, tmp_path)
    payload = copy.deepcopy(fixture["payload"])
    owner: object = payload
    for segment in path[:-1]:
        owner = owner[segment]  # type: ignore[index]
    owner[path[-1]] = value  # type: ignore[index]
    _rewrite_evidence(fixture, payload)

    with pytest.raises(module.EvidenceError, match="lifecycle"):
        _validate(module, fixture)


def test_candidate_lifecycle_contract_mutation_fails_closed(tmp_path: Path) -> None:
    module = _load_validator()
    fixture = _fixture(module, tmp_path)
    candidate = copy.deepcopy(fixture["candidate"])
    candidate["lifecycle_evidence"]["minimum_terminal_duration_ms"] = 0
    content = _canonical_bytes(candidate)
    fixture["candidate_path"].write_bytes(content)
    fixture["candidate_digest"] = hashlib.sha256(content).hexdigest()

    with pytest.raises(module.EvidenceError, match="lifecycle"):
        _validate(module, fixture)


def test_candidate_asset_mismatch_fails_closed(tmp_path: Path) -> None:
    module = _load_validator()
    fixture = _fixture(module, tmp_path)
    payload = copy.deepcopy(fixture["payload"])
    payload["release_assets"]["core_wheel"]["sha256"] = _digest("other-wheel")
    payload["release_assets"]["framework_lock"]["distribution_digest"] = _digest("other-wheel")
    _rewrite_evidence(fixture, payload)

    with pytest.raises(module.EvidenceError, match="candidate manifest"):
        _validate(module, fixture)


def test_evidence_digest_and_canonical_bytes_are_required(tmp_path: Path) -> None:
    module = _load_validator()
    fixture = _fixture(module, tmp_path)
    with pytest.raises(module.EvidenceError, match="digest"):
        module.validate_evidence(
            fixture["evidence_path"],
            candidate_manifest_path=fixture["candidate_path"],
            candidate_app_bundle_smoke_path=fixture["smoke_path"],
            expected_sha256="0" * 64,
            expected_source_commit=SOURCE,
            expected_candidate_manifest_sha256=fixture["candidate_digest"],
        )

    payload = fixture["payload"]
    noncanonical = json.dumps(payload, indent=2).encode("utf-8")
    fixture["evidence_path"].write_bytes(noncanonical)
    fixture["evidence_digest"] = hashlib.sha256(noncanonical).hexdigest()
    with pytest.raises(module.EvidenceError, match="canonical"):
        _validate(module, fixture)
