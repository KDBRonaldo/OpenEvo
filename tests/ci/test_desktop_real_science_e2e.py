from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
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
        target_id="text_memory",
        method_id="text_memory_expel_reflector",
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
    artifact = {
        "id": "artifact-session-1",
        "target_id": "text_memory",
        "produced_revision": successor,
    }
    first = module.SessionObservation(
        evidence={},
        run={"pinned_revision": predecessor},
        context={},
        artifacts=(artifact,),
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
                    "target_id": "text_memory",
                    "revision": successor,
                }
            ]
        },
        artifacts=(),
    )

    reuse = workflow._assert_successor_reuse(first, second)

    assert reuse["session_2_pinned_session_1_successor"] is True
    assert reuse["session_1_artifact_reused"] is True
    second.run["pinned_revision"] = _revision("revision-2", 2)
    with pytest.raises(module.E2EFailure, match="second_session_did_not_pin_successor"):
        workflow._assert_successor_reuse(first, second)


@pytest.mark.parametrize(
    ("payload", "private_values", "code"),
    [
        ({"session_token": "redacted"}, (), "forbidden_evidence_field"),
        ({"value": "/private/desktop/state"}, (), "host_path_in_evidence"),
        ({"value": "prefix-secret-value"}, ("secret-value",), "secret_in_evidence"),
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
