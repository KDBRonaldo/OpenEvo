from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType

import pytest


TARGETS = ("agent_system", "skill_bundle", "text_memory")
PHASES = tuple(
    sorted(("admission", "execution", "evolution", "preparation", "revision", "terminal"))
)


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


def _app_bundle_smoke() -> dict[str, object]:
    return {
        "schema_version": 3,
        "launch_origin": "mounted_dmg",
        "source_dmg": {
            "filename": "OpenEvo-Desktop-0.1.4-aarch64.dmg",
            "sha256": _digest("desktop_dmg"),
        },
        "bundled_external_bin": "openevo-desktop-sidecar",
        "sidecar_ready": True,
        "bundled_external_bin_resolved": True,
        "native_listener_fd_handoff": True,
        "native_executable_fd_handoff": True,
        "process_group_cleanup": True,
        "binary_sha256": {
            "native_executable": _digest("candidate-native"),
            "bundled_external_bin": _digest("candidate-sidecar"),
        },
    }


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def _candidate(source: str) -> dict[str, object]:
    app_smoke = _canonical_bytes(_app_bundle_smoke())
    files = [
        ("core_wheel", "openevo-0.1.4-py3-none-any.whl", 1001),
        ("framework_lock", "framework-lock.json", 1002),
        ("daemon_bundle", "openevo-daemon-linux-x86_64", 1003),
        ("daemon_manifest", "openevo-daemon-bundle.json", 1004),
        ("desktop_dmg", "OpenEvo-Desktop-0.1.4-aarch64.dmg", 1005),
        ("packaged_web_manifest", "packaged-web-manifest.json", 1006),
        ("playwright_evidence", "playwright-candidate-evidence.json", 1007),
        ("app_bundle_smoke", "app-bundle-smoke.json", len(app_smoke)),
    ]
    return {
        "schema_version": 6,
        "source_commit": source,
        "version": "0.1.4",
        "files": [
            {
                "role": role,
                "filename": filename,
                "sha256": hashlib.sha256(app_smoke).hexdigest()
                if role == "app_bundle_smoke"
                else _digest(role),
                "byte_size": size,
            }
            for role, filename, size in files
        ],
        "managed_runtime": {
            "archive": {
                "filename": "openevo-science-runtime.tar.gz",
                "sha256": _digest("managed_runtime_archive"),
                "byte_size": 1008,
            }
        },
    }


def _revision(label: str, generation: int) -> dict[str, object]:
    return {
        "id_sha256": _digest(f"{label}-id"),
        "generation": generation,
        "manifest_sha256": _digest(f"{label}-manifest"),
    }


def _artifact(target: str, ordinal: int, produced: dict[str, object]) -> dict[str, object]:
    return {
        "artifact_id_sha256": _digest(f"artifact-{ordinal}-{target}"),
        "artifact_type": target,
        "target_id": target,
        "method_id": (
            "method_agent_system_concrete" if target == "agent_system" else f"method_{target}"
        ),
        "content_sha256": _digest(f"content-{ordinal}-{target}"),
        "byte_size": 100 + ordinal,
        "selected": True,
        "promoted": target != "skill_bundle",
        "release_enabled": True,
        "source_artifact_count": 0 if ordinal == 1 else 1,
        "source_artifact_ids_sha256": [] if ordinal == 1 else [_digest(f"artifact-1-{target}")],
        "source_dataset_count": 1,
        "produced_revision": produced,
    }


def _session(
    ordinal: int,
    pinned: dict[str, object],
    produced: dict[str, object],
) -> dict[str, object]:
    artifacts = [_artifact(target, ordinal, produced) for target in TARGETS]
    inspections = {
        target: {
            "artifact_id_sha256": artifact["artifact_id_sha256"],
            "document_count": 1,
            "total_documents": 1,
            "total_utf8_bytes": 50 + ordinal,
            "truncated": False,
            "runtime_document_sha256": _digest(f"runtime-{ordinal}-{target}"),
        }
        for target, artifact in zip(TARGETS, artifacts, strict=True)
    }
    return {
        "ordinal": ordinal,
        "run_id_sha256": _digest(f"run-{ordinal}"),
        "status": "succeeded",
        "required_relation": "active",
        "required_revision": pinned,
        "pinned_revision": pinned,
        "timeline": {
            "count": len(PHASES),
            "content_sha256": [_digest(f"timeline-{ordinal}-{index}") for index in range(6)],
            "evidence_truncated": False,
            "phase_values": list(PHASES),
            "status_values": ["succeeded"],
        },
        "logs": {
            "count": 2,
            "content_sha256": [_digest(f"log-{ordinal}-{index}") for index in range(2)],
            "evidence_truncated": False,
            "stream_values": ["stderr", "stdout"],
            "level_values": ["info"],
        },
        "artifacts": artifacts,
        "artifact_count": 3,
        "artifact_evidence_truncated": False,
        "artifact_inspections": inspections,
        "runtime_context_receipt_sha256": None
        if ordinal == 1
        else _digest("runtime-context-receipt"),
        "runtime_context_receipt_core_provenance_verified": ordinal == 2,
        "context": {
            "status": "succeeded",
            "capture_mode": "transcript",
            "token_level_metrics_available": False,
            "artifact_count": 0 if ordinal == 1 else 3,
            "adapter_count": 0,
        },
        "transcript_dataset_lineage_observed": True,
    }


def _payload(
    source: str,
    candidate: dict[str, object],
    candidate_manifest_sha256: str,
) -> dict[str, object]:
    roles = {item["role"]: item for item in candidate["files"]}  # type: ignore[index]
    runtime = candidate["managed_runtime"]["archive"]  # type: ignore[index]
    revision_0 = _revision("revision-0", 0)
    revision_1 = _revision("revision-1", 1)
    revision_2 = _revision("revision-2", 2)
    sessions = [
        _session(1, revision_0, revision_1),
        _session(2, revision_1, revision_2),
    ]
    latest = sessions[1]
    latest_artifacts = {
        artifact["target_id"]: artifact
        for artifact in latest["artifacts"]  # type: ignore[index]
    }
    latest_inspections = latest["artifact_inspections"]  # type: ignore[assignment]
    return {
        "schema_version": "1",
        "kind": "openevo_desktop_real_science_e2e",
        "issue": 163,
        "real_process_boundary": True,
        "outcome": "passed",
        "started_at": "2026-07-20T01:00:00Z",
        "release_assets": {
            "sidecar": {"sha256": _digest("sidecar"), "byte_size": 1000},
            "core_wheel": {
                "sha256": roles["core_wheel"]["sha256"],
                "byte_size": roles["core_wheel"]["byte_size"],
                "filename": roles["core_wheel"]["filename"],
                "distribution": "openevo",
                "version": "0.1.4",
            },
            "framework_lock": {
                "sha256": roles["framework_lock"]["sha256"],
                "byte_size": roles["framework_lock"]["byte_size"],
                "distribution_digest": roles["core_wheel"]["sha256"],
            },
            "managed_runtime_archive": {
                "sha256": runtime["sha256"],
                "byte_size": runtime["byte_size"],
            },
            "daemon_bundle": {
                "sha256": roles["daemon_bundle"]["sha256"],
                "byte_size": roles["daemon_bundle"]["byte_size"],
            },
            "daemon_manifest": {
                "sha256": roles["daemon_manifest"]["sha256"],
                "byte_size": roles["daemon_manifest"]["byte_size"],
            },
            "external_release_assets": {
                "source_commit": source,
                "registry_digest": _digest("registry"),
                "manifest_sha256": _digest("release-assets-manifest"),
                "byte_size": 2048,
            },
            "exact_external_release_assets_verified": True,
            "slim_sidecar_excludes_remote_release_assets_verified": True,
        },
        "renderer_candidate_binding": {
            "source_commit": source,
            "candidate_version": "0.1.4",
            "release_candidate_manifest_sha256": candidate_manifest_sha256,
            "desktop_dmg_sha256": roles["desktop_dmg"]["sha256"],
            "app_bundle_smoke_sha256": roles["app_bundle_smoke"]["sha256"],
            "candidate_packaged_sidecar_sha256": _digest("candidate-sidecar"),
            "science_sidecar_sha256": _digest("sidecar"),
            "candidate_native_sidecar_smoke_verified": True,
            "cross_platform_source_equivalent_verified": True,
            "packaged_web_manifest_sha256": roles["packaged_web_manifest"]["sha256"],
            "playwright_candidate_evidence_sha256": roles["playwright_evidence"]["sha256"],
            "packaged_web_build_digest": _digest("packaged-web-build"),
            "source_checkout_verified": True,
        },
        "desktop": {
            "source_commit": source,
            "build_version": "0.1.4",
            "openapi_sha256": _digest("openapi"),
            "provider_kind": "desktop_sidecar",
            "build_channel": "release",
            "feature_flags": ["daemon_control_v1", "science_projects_v1"],
            "legacy_route_rejected": True,
            "authenticated_session_probe": True,
            "unauthenticated_session_rejected": True,
        },
        "run_mode": "two_session_subscription_release",
        "verification_scope": [
            "desktop_sidecar",
            "ssh_bootstrap",
            "daemon_core",
            "codex_subscription_transcript",
            "cross_session_artifact_reuse",
            "packaged_renderer_local_api_observability",
        ],
        "session_count": 2,
        "evolution_targets_enabled": True,
        "artifact_publication_verified": True,
        "cross_session_reuse_verified": True,
        "release_evolution_path_verified": True,
        "canonical_project_head_orchestration_verified": False,
        "codex_subscription_transcript_verified": True,
        "remote": {
            "ssh_connection_verified": True,
            "host_key_verified": True,
        },
        "project": {
            "project_id_sha256": _digest("project"),
            "execution_mode": "codex_subscription_transcript",
            "capture_mode": "transcript",
            "token_level_metrics_available": False,
            "codex_model": "gpt-5.3-codex-spark",
            "reasoning_effort": "high",
            "target_ids": list(TARGETS),
            "method_ids": {
                "agent_system": "auto",
                "skill_bundle": "method_skill_bundle",
                "text_memory": "method_text_memory",
            },
            "allowed_concrete_method_ids": {
                "agent_system": ["method_agent_system_concrete"],
                "skill_bundle": ["method_skill_bundle"],
                "text_memory": ["method_text_memory"],
            },
            "initial_zero_target_activation_verified": True,
            "registry_digest": _digest("registry"),
            "validation_check_count": 3,
        },
        "sessions": sessions,
        "reuse": {
            "successor_generation_delta": 1,
            "followup_admitted_after_successor_active": True,
            "session_1_excluded_own_successor": True,
            "session_2_pinned_session_1_successor": True,
            "session_1_artifacts_reused": True,
            "session_2_runtime_injection_verified": True,
            "session_2_lineage_verified": True,
            "runtime_context_receipt_sha256": _digest("runtime-context-receipt"),
            "reused_artifact_count": 3,
            "successor_project_head": revision_1,
        },
        "renderer": {
            "schema_version": "1",
            "kind": "openevo_desktop_live_renderer_observability",
            "outcome": "passed",
            "provider_kind": "desktop_sidecar",
            "source_commit": source,
            "packaged_web_build_digest": _digest("packaged-web-build"),
            "renderer_ready": True,
            "builtin_sample_count": 2,
            "project_id_sha256": _digest("project"),
            "session_count": 2,
            "timeline": {"count": 12, "phase_values": list(PHASES)},
            "logs": {"count": 2},
            "project_head_generation": 2,
            "independent_target_controls_verified": True,
            "remote_method_selection_verified": True,
            "artifacts": [
                {
                    "artifact_id_sha256": latest_artifacts[target]["artifact_id_sha256"],
                    "artifact_type": target,
                    "target_id": target,
                    "document_count": latest_inspections[target]["document_count"],
                    "total_utf8_bytes": latest_inspections[target]["total_utf8_bytes"],
                    "content_sha256": latest_artifacts[target]["content_sha256"],
                    "runtime_document_sha256": latest_inspections[target][
                        "runtime_document_sha256"
                    ],
                }
                for target in TARGETS
            ],
            "screenshot_sha256": _digest("screenshot"),
        },
        "renderer_observability_verified": True,
        "renderer_boundary": "packaged_web_to_live_local_api",
        "native_tauri_live_verified": False,
        "cleanup": {
            "active_run_cleanup_required": False,
            "active_run_cancel_requested": False,
            "active_run_cancelled": False,
            "active_run_cleanup_succeeded": True,
            "desktop_disconnect_succeeded": True,
            "sidecar_shutdown_succeeded": True,
            "core_ownership_release_requested": True,
        },
        "finished_at": "2026-07-20T02:00:00Z",
    }


def _write(module: ModuleType, path: Path, payload: object) -> str:
    encoded = module._canonical_json(payload)
    path.write_bytes(encoded)
    return hashlib.sha256(encoded).hexdigest()


def _fixture(
    module: ModuleType,
    tmp_path: Path,
) -> tuple[Path, str, Path, Path, str, str]:
    source = "b" * 40
    candidate = _candidate(source)
    candidate_path = tmp_path / "release-candidate.json"
    candidate_digest = _write(module, candidate_path, candidate)
    app_smoke = tmp_path / "app-bundle-smoke.json"
    _write(module, app_smoke, _app_bundle_smoke())
    evidence = tmp_path / "desktop-real-science-e2e.json"
    evidence_digest = _write(module, evidence, _payload(source, candidate, candidate_digest))
    return evidence, evidence_digest, candidate_path, app_smoke, candidate_digest, source


def test_exact_candidate_two_session_evidence_passes(tmp_path: Path) -> None:
    module = _load_validator()
    evidence, digest, candidate, app_smoke, candidate_digest, source = _fixture(module, tmp_path)

    validated = module.validate_evidence(
        evidence,
        candidate_manifest_path=candidate,
        candidate_app_bundle_smoke_path=app_smoke,
        expected_sha256=digest,
        expected_source_commit=source,
        expected_candidate_manifest_sha256=candidate_digest,
    )

    assert validated["outcome"] == "passed"


def test_minimal_verdict_claim_fails_closed(tmp_path: Path) -> None:
    module = _load_validator()
    source = "b" * 40
    candidate_payload = _candidate(source)
    candidate = tmp_path / "release-candidate.json"
    candidate_digest = _write(module, candidate, candidate_payload)
    app_smoke = tmp_path / "app-bundle-smoke.json"
    _write(module, app_smoke, _app_bundle_smoke())
    evidence = tmp_path / "desktop-real-science-e2e.json"
    digest = _write(
        module,
        evidence,
        {
            "schema_version": "1",
            "kind": "openevo_desktop_real_science_e2e",
            "outcome": "passed",
            "cross_session_reuse_verified": True,
        },
    )

    with pytest.raises(module.EvidenceError, match="closed schema"):
        module.validate_evidence(
            evidence,
            candidate_manifest_path=candidate,
            candidate_app_bundle_smoke_path=app_smoke,
            expected_sha256=digest,
            expected_source_commit=source,
            expected_candidate_manifest_sha256=candidate_digest,
        )


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("cross_session_reuse_verified",), False),
        (("release_assets", "exact_external_release_assets_verified"), False),
        (
            ("release_assets", "slim_sidecar_excludes_remote_release_assets_verified"),
            False,
        ),
        (
            ("release_assets", "external_release_assets", "source_commit"),
            "f" * 40,
        ),
        (
            ("release_assets", "external_release_assets", "registry_digest"),
            _digest("wrong-registry"),
        ),
        (("renderer_candidate_binding", "source_checkout_verified"), False),
        (
            ("renderer_candidate_binding", "candidate_packaged_sidecar_sha256"),
            _digest("wrong-candidate-sidecar"),
        ),
        (("reuse", "session_2_runtime_injection_verified"), False),
        (("sessions", 1, "context", "artifact_count"), 0),
        (("sessions", 1, "run_id_sha256"), _digest("run-1")),
        (("sessions", 1, "artifacts", 0, "source_artifact_count"), 0),
        (
            ("sessions", 1, "artifacts", 0, "source_artifact_ids_sha256"),
            [_digest("unrelated-artifact")],
        ),
        (
            ("sessions", 1, "artifacts", 0, "artifact_id_sha256"),
            _digest("artifact-1-agent_system"),
        ),
        (("renderer", "artifacts", 0, "content_sha256"), _digest("wrong")),
        (("cleanup", "sidecar_shutdown_succeeded"), False),
    ],
)
def test_incomplete_release_claim_fails_closed(
    tmp_path: Path,
    path: tuple[str | int, ...],
    value: object,
) -> None:
    module = _load_validator()
    evidence, _digest_value, candidate, app_smoke, candidate_digest, source = _fixture(
        module, tmp_path
    )
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    cursor = payload
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value
    digest = _write(module, evidence, payload)

    with pytest.raises(module.EvidenceError):
        module.validate_evidence(
            evidence,
            candidate_manifest_path=candidate,
            candidate_app_bundle_smoke_path=app_smoke,
            expected_sha256=digest,
            expected_source_commit=source,
            expected_candidate_manifest_sha256=candidate_digest,
        )


def test_candidate_asset_mismatch_fails_closed(tmp_path: Path) -> None:
    module = _load_validator()
    evidence, _digest_value, candidate, app_smoke, candidate_digest, source = _fixture(
        module, tmp_path
    )
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    payload["release_assets"]["daemon_bundle"]["sha256"] = _digest("other-daemon")
    digest = _write(module, evidence, payload)

    with pytest.raises(module.EvidenceError, match="candidate manifest"):
        module.validate_evidence(
            evidence,
            candidate_manifest_path=candidate,
            candidate_app_bundle_smoke_path=app_smoke,
            expected_sha256=digest,
            expected_source_commit=source,
            expected_candidate_manifest_sha256=candidate_digest,
        )


def test_evidence_digest_and_canonical_bytes_are_required(tmp_path: Path) -> None:
    module = _load_validator()
    evidence, _digest_value, candidate, app_smoke, candidate_digest, source = _fixture(
        module, tmp_path
    )
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    evidence.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    digest = hashlib.sha256(evidence.read_bytes()).hexdigest()

    with pytest.raises(module.EvidenceError, match="canonical"):
        module.validate_evidence(
            evidence,
            candidate_manifest_path=candidate,
            candidate_app_bundle_smoke_path=app_smoke,
            expected_sha256=digest,
            expected_source_commit=source,
            expected_candidate_manifest_sha256=candidate_digest,
        )
