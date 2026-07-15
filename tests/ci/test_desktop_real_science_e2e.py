from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import ModuleType
from zipfile import ZipFile

import pytest


def _load_runner() -> ModuleType:
    path = Path("scripts/e2e/desktop_real_science_e2e.py").resolve()
    spec = importlib.util.spec_from_file_location("desktop_real_science_e2e", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_wheel(path: Path, *, version: str = "0.1.0") -> None:
    with ZipFile(path, "w") as archive:
        archive.writestr(
            f"openevo-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: openevo\nVersion: {version}\n",
        )


def _write_lock(path: Path, wheel: Path, *, digest: str | None = None) -> None:
    payload = {
        "schema_version": "1",
        "distribution": "openevo",
        "distribution_version": "0.1.0",
        "distribution_digest": digest or hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "wheel_filename": wheel.name,
    }
    path.write_text(
        json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _revision(identifier: str, generation: int) -> dict[str, object]:
    return {
        "id": identifier,
        "project_id": "project-real-e2e",
        "generation": generation,
        "manifest_sha256": f"{generation + 1:064x}",
    }


def _workflow(module: ModuleType):
    return module.DesktopScienceWorkflow(
        object(),
        host="compute.example.org",
        port=22,
        user="researcher",
        host_key_algorithm="ssh-ed25519",
        expected_host_key_fingerprint="SHA256:" + "A" * 43 + "=",
        codex_model="gpt-5",
        task_title="Structural test",
        task_objective="No real execution occurs in this structural test.",
        poll_seconds=0.01,
        activation_timeout_seconds=1,
        run_timeout_seconds=1,
    )


def test_structural_check_is_explicitly_not_an_e2e_run(tmp_path: Path) -> None:
    output = tmp_path / "must-not-exist.json"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/e2e/desktop_real_science_e2e.py",
            "--structural-check",
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "E2E was not run" in result.stdout
    assert "passed; bounded evidence" not in result.stdout
    assert not output.exists()


def test_native_frame_uses_the_closed_credential_protocol() -> None:
    module = _load_runner()
    credentials = module.NativeCredentials.create()

    frame = credentials.frame()
    payload = json.loads(frame)

    assert frame.endswith(b"\n")
    assert len(frame) <= module.MAX_NATIVE_FRAME_BYTES
    assert set(payload) == {
        "protocol",
        "instance_id",
        "readiness_key",
        "session_token",
        "handoff_token",
    }
    assert payload["protocol"] == module.NATIVE_PROTOCOL
    assert credentials.session_token not in repr(credentials)


def test_wheel_lock_validation_binds_exact_bytes(tmp_path: Path) -> None:
    module = _load_runner()
    wheel = tmp_path / "openevo-0.1.0-py3-none-any.whl"
    lock = tmp_path / "framework-lock.json"
    _write_wheel(wheel)
    _write_lock(lock, wheel)

    assert module._validate_wheel_lock(wheel, lock) == (
        "openevo",
        "0.1.0",
        hashlib.sha256(wheel.read_bytes()).hexdigest(),
    )

    _write_lock(lock, wheel, digest="0" * 64)
    with pytest.raises(module.E2EFailure, match="framework_lock_wheel_mismatch"):
        module._validate_wheel_lock(wheel, lock)


def test_arguments_require_the_complete_exact_asset_triplet() -> None:
    module = _load_runner()
    args = argparse.Namespace(
        sidecar=Path("sidecar"),
        core_wheel=None,
        framework_lock=None,
        structural_check=False,
        host="compute.example.org",
        user="researcher",
        expected_host_key_fingerprint="SHA256:" + "A" * 43 + "=",
    )

    with pytest.raises(module.E2EFailure, match="release_asset_triplet_required"):
        module._validate_runtime_arguments(args)


def test_successor_reuse_requires_the_second_real_session_pin() -> None:
    module = _load_runner()
    workflow = _workflow(module)
    predecessor = _revision("revision-0", 0)
    successor = _revision("revision-1", 1)
    first_artifacts = tuple(
        {
            "id": f"artifact-session-1-{target_id}",
            "target_id": target_id,
            "produced_revision": successor,
            "selected": True,
            "promoted": False,
            "release_enabled": True,
            "lineage": {"source_artifact_ids": []},
        }
        for target_id in module.REQUIRED_TARGET_IDS
    )
    second_artifacts = tuple(
        {
            "id": f"artifact-session-2-{target_id}",
            "target_id": target_id,
            "produced_revision": _revision("revision-2", 2),
            "lineage": {"source_artifact_ids": [f"artifact-session-1-{target_id}"]},
        }
        for target_id in module.REQUIRED_TARGET_IDS
    )
    receipt_sha256 = "f" * 64
    first = module.SessionObservation(
        evidence={},
        run={"pinned_revision": predecessor},
        context={},
        artifacts=first_artifacts,
        document_sha256_by_target={
            target_id: "1" * 64 for target_id in module.REQUIRED_TARGET_IDS
        },
        runtime_context_receipt_sha256=None,
    )
    second = module.SessionObservation(
        evidence={},
        run={
            "pinned_revision": successor,
            "required_revision": {"revision": successor},
        },
        context={
            "artifacts": [
                {
                    "artifact_id": artifact["id"],
                    "artifact_type": artifact["target_id"],
                    "target_id": artifact["target_id"],
                    "revision": successor,
                }
                for artifact in first_artifacts
            ]
        },
        artifacts=second_artifacts,
        document_sha256_by_target={
            target_id: "2" * 64 for target_id in module.REQUIRED_TARGET_IDS
        },
        runtime_context_receipt_sha256=receipt_sha256,
    )

    reuse = workflow._assert_successor_reuse(first, second)

    assert reuse["session_2_pinned_session_1_successor"] is True
    assert reuse["session_1_artifacts_reused"] is True
    assert reuse["session_2_runtime_injection_verified"] is True
    assert reuse["runtime_context_receipt_sha256"] == receipt_sha256
    second.run["pinned_revision"] = _revision("revision-2", 2)
    with pytest.raises(module.E2EFailure, match="second_session_did_not_pin_successor"):
        workflow._assert_successor_reuse(first, second)


@pytest.mark.parametrize(
    ("payload", "private_values", "code"),
    [
        ({"session_token": "redacted"}, (), "forbidden_evidence_field"),
        ({"status": "/private/desktop/state"}, (), "host_path_in_evidence"),
        ({"status": "prefix-secret-value"}, ("secret-value",), "secret_in_evidence"),
        ({"mutation_token": "redacted"}, (), "forbidden_evidence_field"),
        ({"password": "redacted"}, (), "forbidden_evidence_field"),
        ({"passphrase": "redacted"}, (), "forbidden_evidence_field"),
        ({"private_key": "redacted"}, (), "forbidden_evidence_field"),
        ({"ssh_auth_sock": "redacted"}, (), "forbidden_evidence_field"),
        ({"bearer": "redacted"}, (), "forbidden_evidence_field"),
        ({"status": "file:///private/state"}, (), "sensitive_text_in_evidence"),
        ({"raw_log": "redacted"}, (), "evidence_field_not_allowlisted"),
    ],
)
def test_evidence_audit_rejects_sensitive_values(
    payload: dict[str, object],
    private_values: tuple[str, ...],
    code: str,
) -> None:
    module = _load_runner()

    with pytest.raises(module.E2EFailure, match=code):
        module._audit_evidence(payload, private_values=private_values)


def test_bounded_evidence_contains_only_redacted_identity(tmp_path: Path) -> None:
    module = _load_runner()
    output = tmp_path / "evidence.json"
    payload = {
        "schema_version": "1",
        "kind": "openevo_desktop_real_science_e2e",
        "outcome": "failed",
        "remote": {
            "host_sha256": "1" * 64,
            "user_sha256": "2" * 64,
        },
        "failure": {"stage": "project_activate", "code": "project_activation_failed"},
    }

    module._write_evidence(output, payload, private_values=("private-value",))

    assert output.stat().st_size <= module.MAX_EVIDENCE_BYTES
    assert json.loads(output.read_text(encoding="utf-8")) == payload
    assert "compute.example.org" not in output.read_text(encoding="utf-8")


def test_closed_evidence_schema_accepts_runtime_receipt_shape() -> None:
    module = _load_runner()
    digest = "a" * 64
    payload = {
        "project": {
            "target_ids": list(module.REQUIRED_TARGET_IDS),
            "method_ids": {
                target_id: f"method_{target_id}" for target_id in module.REQUIRED_TARGET_IDS
            },
            "registry_digest": digest,
        },
        "sessions": [
            {
                "ordinal": 2,
                "runtime_context_receipt_sha256": digest,
                "artifact_inspections": {
                    target_id: {
                        "artifact_id_sha256": digest,
                        "runtime_document_sha256": digest,
                        "document_count": 1,
                        "total_documents": 1,
                        "total_utf8_bytes": 1,
                        "truncated": False,
                    }
                    for target_id in module.REQUIRED_TARGET_IDS
                },
            }
        ],
        "reuse": {
            "session_2_runtime_injection_verified": True,
            "session_2_lineage_verified": True,
            "runtime_context_receipt_sha256": digest,
        },
    }

    module._audit_evidence(payload, private_values=())


def test_capability_selection_enables_all_three_remote_supported_methods() -> None:
    module = _load_runner()
    project = {
        "etag": '"' + "1" * 64 + '"',
        "state": "active",
        "remote": {
            "status": "ready",
            "active_revision": _revision("r0", 0),
            "registry_digest": "a" * 64,
        },
    }

    class FakeApi:
        patched: dict[str, object] | None = None

        def request(self, method: str, route: str, **kwargs: object):
            if method == "GET" and route.endswith("/capabilities"):
                return {
                    "project_etag": project["etag"],
                    "capabilities": {
                        "registry_digest": "a" * 64,
                        "targets": [
                            (
                                {
                                    "target_id": target_id,
                                    "effective_default_method_id": f"remote-{target_id}",
                                    "methods": [
                                        {
                                            "method_id": f"remote-{target_id}",
                                            "maturity": "experimental"
                                            if target_id == "text_memory"
                                            else "stable",
                                            "support": {"overall": "supported"},
                                            "implementation_identity_digest": "b" * 64,
                                            "default_config_json": '{"limit":1}',
                                        },
                                        {
                                            "method_id": f"experimental-{target_id}",
                                            "maturity": "experimental",
                                            "support": {"overall": "supported"},
                                            "implementation_identity_digest": "c" * 64,
                                            "default_config_json": "{}",
                                        },
                                    ],
                                    "accepted_methods": [
                                        {
                                            "method_id": "remote-agent_system",
                                            "support": {"overall": "supported"},
                                            "implementation_identity_digest": "b" * 64,
                                        }
                                    ],
                                    "selection_resolvers": [
                                        {
                                            "selection_value": "auto",
                                            "resolved_methods": [
                                                {
                                                    "method_id": "remote-agent_system",
                                                    "support": {"overall": "supported"},
                                                    "implementation_identity_digest": "b" * 64,
                                                }
                                            ],
                                        }
                                    ],
                                }
                                if target_id == "agent_system"
                                else {
                                    "target_id": target_id,
                                    "effective_default_method_id": f"remote-{target_id}",
                                    "methods": [
                                        {
                                            "method_id": f"remote-{target_id}",
                                            "maturity": "experimental"
                                            if target_id == "text_memory"
                                            else "stable",
                                            "support": {"overall": "supported"},
                                            "implementation_identity_digest": "b" * 64,
                                            "default_config_json": '{"limit":1}',
                                        },
                                        {
                                            "method_id": f"experimental-{target_id}",
                                            "maturity": "experimental",
                                            "support": {"overall": "supported"},
                                            "implementation_identity_digest": "c" * 64,
                                            "default_config_json": "{}",
                                        },
                                    ],
                                }
                            )
                            for target_id in module.REQUIRED_TARGET_IDS
                        ],
                    },
                }
            if method == "PATCH":
                self.patched = kwargs["body"]  # type: ignore[assignment]
                return {"etag": '"' + "2" * 64 + '"'}
            if method == "POST" and route.endswith("/activate"):
                return {"operation_id": "activate", "state": "succeeded"}
            if method == "GET" and "/projects/" in route:
                return project
            raise AssertionError((method, route))

    api = FakeApi()
    workflow = _workflow(module)
    workflow._api = api
    workflow.project_id = "project-real-e2e"

    capabilities = workflow._select_and_activate_targets(project)

    assert capabilities["registry_digest"] == "a" * 64
    targets = api.patched["evolution"]["targets"]  # type: ignore[index]
    assert set(targets) == set(module.REQUIRED_TARGET_IDS)
    for target_id, selection in targets.items():
        if target_id == "agent_system":
            assert selection == {
                "enabled": True,
                "method": "auto",
                "config": {},
            }
            continue
        assert selection == {
            "enabled": True,
            "method": f"remote-{target_id}",
            "config": {"limit": 1},
        }


def test_capability_selection_rejects_unsupported_agent_system_auto() -> None:
    module = _load_runner()
    project = {"etag": '"' + "1" * 64 + '"'}

    class FakeApi:
        def request(self, _method: str, route: str, **_kwargs: object):
            assert route.endswith("/capabilities")
            return {
                "project_etag": project["etag"],
                "capabilities": {
                    "registry_digest": "a" * 64,
                    "targets": [
                        {
                            "target_id": "agent_system",
                            "methods": [],
                            "accepted_methods": [
                                {
                                    "method_id": "agent-system-concrete",
                                    "support": {"overall": "unsupported"},
                                    "implementation_identity_digest": "b" * 64,
                                }
                            ],
                            "selection_resolvers": [
                                {
                                    "selection_value": "auto",
                                    "resolved_methods": [
                                        {
                                            "method_id": "agent-system-concrete",
                                            "support": {"overall": "unsupported"},
                                            "implementation_identity_digest": "b" * 64,
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                },
            }

    workflow = _workflow(module)
    workflow._api = FakeApi()
    workflow.project_id = "project-real-e2e"

    with pytest.raises(module.E2EFailure, match="agent_system_auto_not_supported"):
        workflow._select_and_activate_targets(project)


def test_release_negotiation_matches_native_host_contract() -> None:
    module = _load_runner()
    contract = json.loads(Path("desktop/release-contract.json").read_text(encoding="utf-8"))

    class FakeApi:
        calls: list[tuple[str, bool, int]] = []

        def request(self, _method: str, route: str, **kwargs: object):
            self.calls.append(
                (
                    route,
                    bool(kwargs.get("authenticated", True)),
                    int(kwargs.get("expected_status", 200)),
                )
            )
            if route == "/version":
                return {
                    "schema_version": "1",
                    "api_name": "openevo-desktop-local-api",
                    "preferred_major": 1,
                    "supported_majors": [1],
                    "openapi_sha256": contract["accepted_openapi_digests"][0],
                    "build_version": "0.1.0",
                    "source_commit": "abcdef0",
                    "build_channel": "release",
                    "provider_kind": "desktop_sidecar",
                    "feature_flags": contract["required_feature_flags"],
                }
            return None if kwargs.get("expected_status") == 204 else {}

    api = FakeApi()
    evidence = module._release_identity(api)

    assert evidence["provider_kind"] == "desktop_sidecar"
    assert ("/openevo-api/desktop/shell", False, 404) in api.calls
    assert ("/openevo-native/session", True, 204) in api.calls
    assert ("/openevo-native/session", False, 403) in api.calls


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"openapi_sha256": "0" * 64}, "desktop_contract_invalid"),
        ({"provider_kind": "contract_simulator"}, "not_release_desktop_sidecar"),
        ({"feature_flags": ["remote_profiles"]}, "desktop_contract_invalid"),
        ({"unknown_field": True}, "desktop_contract_invalid"),
    ],
)
def test_release_negotiation_rejects_non_native_contract(
    override: dict[str, object],
    code: str,
) -> None:
    module = _load_runner()
    contract = json.loads(Path("desktop/release-contract.json").read_text(encoding="utf-8"))

    class FakeApi:
        def request(self, _method: str, route: str, **_kwargs: object):
            assert route == "/version"
            return {
                "schema_version": "1",
                "api_name": "openevo-desktop-local-api",
                "preferred_major": 1,
                "supported_majors": [1],
                "openapi_sha256": contract["accepted_openapi_digests"][0],
                "build_version": "0.1.0",
                "source_commit": "abcdef0",
                "build_channel": "release",
                "provider_kind": "desktop_sidecar",
                "feature_flags": contract["required_feature_flags"],
                **override,
            }

    with pytest.raises(module.E2EFailure, match=code):
        module._release_identity(FakeApi())


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf"])
def test_timeout_arguments_require_finite_positive_values(value: str) -> None:
    module = _load_runner()

    with pytest.raises(SystemExit):
        module._parser().parse_args(["--poll-seconds", value])


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_build_process_group_cleanup_kills_descendant_after_leader_exit() -> None:
    module = _load_runner()
    code = """
import os, signal, sys, time
child = os.fork()
if child == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    sys.stdout.close()
    while True:
        time.sleep(1)
print(child, flush=True)
os._exit(0)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        start_new_session=True,
        text=True,
    )
    assert process.stdout is not None
    child_pid = int(process.stdout.readline().strip())
    process.stdout.close()
    assert (
        module._wait_for_build_process_group(
            process,
            process_group_id=process.pid,
            timeout_seconds=2,
        )
        == 0
    )

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            state = Path(f"/proc/{child_pid}/stat").read_text().split()[2]
        except (FileNotFoundError, IndexError):
            break
        if state == "Z":
            break
        time.sleep(0.02)
    else:
        pytest.fail("descendant survived process-group cleanup")
