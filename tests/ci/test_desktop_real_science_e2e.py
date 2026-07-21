from __future__ import annotations

import argparse
from contextlib import nullcontext
import hashlib
import importlib.util
from io import BytesIO
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
from types import ModuleType, SimpleNamespace
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


def _proxy_environment_canaries() -> dict[str, str]:
    return {
        "ALL_PROXY": "socks5://proxy.example:1080",
        "HTTP_PROXY": "http://proxy.example:8080",
        "HTTPS_PROXY": "http://proxy.example:8443",
        "NO_PROXY": "localhost,127.0.0.1",
        "all_proxy": "socks5://lower-proxy.example:1080",
        "http_proxy": "http://lower-proxy.example:8080",
        "https_proxy": "http://lower-proxy.example:8443",
        "no_proxy": "localhost,.example.test",
    }


def _workflow(module: ModuleType, *, smoke: bool = False):
    return module.DesktopScienceWorkflow(
        object(),
        host="compute.example.org",
        port=22,
        user="researcher",
        host_key_algorithm="ssh-ed25519",
        expected_host_key_fingerprint="SHA256:" + "A" * 43 + "=",
        codex_model="gpt-5.3-codex-spark",
        reasoning_effort="high",
        task_title="Structural test",
        task_objective="No real execution occurs in this structural test.",
        poll_seconds=0.01,
        activation_timeout_seconds=1,
        run_timeout_seconds=1,
        smoke=smoke,
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


def test_smoke_and_structural_check_are_closed_mutually_exclusive_modes(
    tmp_path: Path,
) -> None:
    output = tmp_path / "must-not-exist.json"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/e2e/desktop_real_science_e2e.py",
            "--smoke",
            "--structural-check",
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "not allowed with argument" in result.stderr
    assert not output.exists()


def test_smoke_does_not_add_the_evolution_context_canary() -> None:
    module = _load_runner()

    full = _workflow(module)
    smoke = _workflow(module, smoke=True)

    assert module.CONTEXT_CANARY_INSTRUCTION in full._task_objective
    assert module.CONTEXT_CANARY_INSTRUCTION not in smoke._task_objective


def test_renderer_expectations_bind_successful_sessions_and_latest_artifacts() -> None:
    module = _load_runner()
    workflow = _workflow(module)
    workflow.project_id = "desktop-project"
    workflow._method_ids = {
        target_id: f"method_{target_id}" for target_id in module.REQUIRED_TARGET_IDS
    }
    workflow._allowed_concrete_method_ids = {
        target_id: frozenset({f"method_{target_id}"})
        for target_id in module.REQUIRED_TARGET_IDS
    }
    first_revision = _revision("revision-1", 1)
    successor = _revision("revision-3", 3)

    def observation(ordinal: int, artifact_suffix: str, transition: object):
        return module.SessionObservation(
            evidence={
                "timeline": {
                    "phase_values": [
                        "admission",
                        "preparation",
                        "execution",
                        "evolution",
                        "revision",
                        "terminal",
                    ]
                },
                "logs": {"count": ordinal + 1},
            },
            run={
                "id": f"run-{ordinal}",
                "status": "succeeded",
                "pinned_revision": first_revision,
                "revision_transition": transition,
            },
            context={},
            artifacts=tuple(
                {
                    "id": f"artifact-{target_id}-{artifact_suffix}",
                    "artifact_type": target_id,
                    "target_id": target_id,
                    "content_sha256": ("a" if ordinal == 1 else "b") * 64,
                    "produced_revision": transition["successor_revision"],
                }
                for target_id in module.REQUIRED_TARGET_IDS
            ),
            document_sha256_by_target={
                target_id: ("c" if ordinal == 1 else "d") * 64
                for target_id in module.REQUIRED_TARGET_IDS
            },
            runtime_context_receipt_sha256=None,
        )

    workflow._session_observations = (
        observation(1, "old", {"successor_revision": _revision("revision-2", 2)}),
        observation(2, "latest", {"successor_revision": successor}),
    )

    expectations = workflow.renderer_expectations()

    assert expectations["project_id"] == "desktop-project"
    assert expectations["project_name"] == module.RELEASE_PROJECT_DISPLAY_NAME
    assert expectations["codex_model"] == module.RELEASE_CODEX_MODEL
    assert expectations["reasoning_effort"] == module.RELEASE_REASONING_EFFORT
    assert expectations["method_ids"] == workflow._method_ids
    assert expectations["project_head_generation"] == 3
    assert [item["run_id"] for item in expectations["sessions"]] == ["run-1", "run-2"]
    assert {
        item["artifact_id"] for item in expectations["artifacts"]
    } == {
        f"artifact-{target_id}-latest" for target_id in module.REQUIRED_TARGET_IDS
    }
    assert {
        item["target_id"] for item in expectations["artifacts"]
    } == set(module.REQUIRED_TARGET_IDS)
    assert {item["artifact_content_sha256"] for item in expectations["artifacts"]} == {
        "b" * 64
    }
    assert {item["runtime_document_sha256"] for item in expectations["artifacts"]} == {
        "d" * 64
    }


def test_renderer_result_validation_binds_live_observations() -> None:
    module = _load_runner()
    digest = "a" * 64
    expectations = {
        "project_id": "desktop-project",
        "sessions": [
            {
                "ordinal": 1,
                "run_id": "run-1",
                "timeline_phase_values": [
                    "admission",
                    "preparation",
                    "execution",
                    "evolution",
                    "revision",
                    "terminal",
                ],
                "minimum_log_count": 2,
            },
            {
                "ordinal": 2,
                "run_id": "run-2",
                "timeline_phase_values": [
                    "admission",
                    "preparation",
                    "execution",
                    "evolution",
                    "revision",
                    "terminal",
                ],
                "minimum_log_count": 3,
            },
        ],
        "project_head_generation": 3,
        "artifacts": [
            {
                "artifact_id": f"artifact-{target_id}",
                "artifact_type": target_id,
                "target_id": target_id,
                "artifact_content_sha256": digest,
                "runtime_document_sha256": "c" * 64,
            }
            for target_id in module.REQUIRED_TARGET_IDS
        ],
    }
    payload = {
        "schema_version": "1",
        "kind": "openevo_desktop_live_renderer_observability",
        "outcome": "passed",
        "provider_kind": "desktop_sidecar",
        "source_commit": "f" * 40,
        "packaged_web_build_digest": digest,
        "renderer_ready": True,
        "builtin_sample_count": 2,
        "project_id_sha256": module._digest_text("desktop-project"),
        "session_count": 2,
        "timeline": {
            "count": 6,
            "phase_values": [
                "admission",
                "evolution",
                "execution",
                "preparation",
                "revision",
                "terminal",
            ],
        },
        "logs": {"count": 3},
        "project_head_generation": 3,
        "independent_target_controls_verified": True,
        "remote_method_selection_verified": True,
        "artifacts": [
            {
                "artifact_id_sha256": module._digest_text(f"artifact-{target_id}"),
                "artifact_type": target_id,
                "target_id": target_id,
                "document_count": 1,
                "total_utf8_bytes": 100,
                "content_sha256": digest,
                "runtime_document_sha256": "c" * 64,
            }
            for target_id in module.REQUIRED_TARGET_IDS
        ],
        "screenshot_sha256": "b" * 64,
    }

    assert module._validate_renderer_result(
        payload,
        expectations=expectations,
        source_commit="f" * 40,
        packaged_web_build_digest=digest,
        screenshot_sha256="b" * 64,
    ) == payload

    payload["renderer_ready"] = False
    with pytest.raises(module.E2EFailure, match="renderer_result_identity_mismatch"):
        module._validate_renderer_result(
            payload,
            expectations=expectations,
            source_commit="f" * 40,
            packaged_web_build_digest=digest,
            screenshot_sha256="b" * 64,
        )

    payload["renderer_ready"] = True
    payload["artifacts"][0]["runtime_document_sha256"] = "d" * 64
    with pytest.raises(module.E2EFailure, match="renderer_artifact_observation_invalid"):
        module._validate_renderer_result(
            payload,
            expectations=expectations,
            source_commit="f" * 40,
            packaged_web_build_digest=digest,
            screenshot_sha256="b" * 64,
        )


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


def test_local_api_requires_explicit_contract_for_empty_error_response() -> None:
    module = _load_runner()

    class EmptyResponse:
        status = 403

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _size: int = -1) -> bytes:
            return b""

    class FakeOpener:
        def open(self, _request: object, *, timeout: int):
            assert timeout == 30
            return EmptyResponse()

    api = module.LocalApi("http://127.0.0.1:12345", "a" * 64)
    api._opener = FakeOpener()

    assert (
        api.request(
            "GET",
            "/openevo-native/session",
            stage="empty_probe",
            expected_status=403,
            authenticated=False,
            expected_empty_body=True,
        )
        is None
    )
    with pytest.raises(module.E2EFailure, match="invalid_json_response"):
        api.request(
            "GET",
            "/openevo-native/session",
            stage="empty_probe_without_contract",
            expected_status=403,
            authenticated=False,
        )


def test_local_api_rejects_payload_when_empty_response_is_declared() -> None:
    module = _load_runner()

    class NonEmptyResponse:
        status = 403

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _size: int = -1) -> bytes:
            return b"{}"

    class FakeOpener:
        def open(self, _request: object, *, timeout: int):
            assert timeout == 30
            return NonEmptyResponse()

    api = module.LocalApi("http://127.0.0.1:12345", "a" * 64)
    api._opener = FakeOpener()

    with pytest.raises(module.E2EFailure, match="unexpected_empty_response_payload"):
        api.request(
            "GET",
            "/openevo-native/session",
            stage="non_empty_probe",
            expected_status=403,
            authenticated=False,
            expected_empty_body=True,
        )


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


def test_held_release_asset_rejects_path_replacement_and_copies_held_bytes(
    tmp_path: Path,
) -> None:
    module = _load_runner()
    source = tmp_path / "sidecar"
    source.write_bytes(b"verified-sidecar")
    authority = module.HeldReleaseAsset.open(source)
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"replacement")
    os.replace(replacement, source)

    with pytest.raises(module.E2EFailure, match="release_asset_authority_changed"):
        authority.verify_unchanged()
    with pytest.raises(module.E2EFailure, match="release_asset_authority_changed"):
        authority.copy_to(tmp_path / "launch", executable=True)
    assert not (tmp_path / "launch").exists()
    authority.close()


def test_held_release_asset_copy_is_digest_bound_and_executable(tmp_path: Path) -> None:
    module = _load_runner()
    source = tmp_path / "sidecar"
    source.write_bytes(b"verified-sidecar")
    authority = module.HeldReleaseAsset.open(source)
    launch = tmp_path / "launch"

    authority.copy_to(launch, executable=True)

    assert launch.read_bytes() == b"verified-sidecar"
    assert stat.S_IMODE(launch.stat().st_mode) == 0o500
    assert authority.evidence() == {
        "sha256": hashlib.sha256(b"verified-sidecar").hexdigest(),
        "byte_size": len(b"verified-sidecar"),
    }
    authority.close()


def test_arguments_require_the_complete_exact_asset_set() -> None:
    module = _load_runner()
    args = argparse.Namespace(
        sidecar=Path("sidecar"),
        core_wheel=None,
        framework_lock=None,
        daemon_bundle=Path("openevo-daemon-linux-x86_64"),
        daemon_manifest=Path("openevo-daemon-bundle.json"),
        managed_runtime_archive=Path("runtime.tar"),
        structural_check=False,
        host="compute.example.org",
        user="researcher",
        expected_host_key_fingerprint="SHA256:" + "A" * 43 + "=",
    )

    with pytest.raises(module.E2EFailure, match="core_release_pair_required"):
        module._validate_runtime_arguments(args)


def test_arguments_require_managed_runtime_archive_for_real_e2e() -> None:
    module = _load_runner()
    args = argparse.Namespace(
        sidecar=Path("sidecar"),
        core_wheel=Path("openevo.whl"),
        framework_lock=Path("framework-lock.json"),
        daemon_bundle=Path("openevo-daemon-linux-x86_64"),
        daemon_manifest=Path("openevo-daemon-bundle.json"),
        managed_runtime_archive=None,
        structural_check=False,
        host="compute.example.org",
        port=22,
        user="researcher",
        expected_host_key_fingerprint="SHA256:" + "A" * 43 + "=",
    )

    with pytest.raises(module.E2EFailure, match="managed_runtime_archive_required"):
        module._validate_runtime_arguments(args)


def test_renderer_requires_exact_candidate_binding_inputs() -> None:
    module = _load_runner()
    args = module._parser().parse_args(
        [
            "--host",
            "compute.example.org",
            "--user",
            "researcher",
            "--expected-host-key-fingerprint",
            "SHA256:" + "A" * 43 + "=",
            "--core-wheel",
            "openevo.whl",
            "--framework-lock",
            "framework-lock.json",
            "--daemon-bundle",
            "openevo-daemon-linux-x86_64",
            "--daemon-manifest",
            "openevo-daemon-bundle.json",
            "--managed-runtime-archive",
            "runtime.tar",
            "--verify-renderer",
        ]
    )

    with pytest.raises(module.E2EFailure, match="renderer_candidate_binding_required"):
        module._validate_runtime_arguments(args)


def test_renderer_candidate_binding_pins_release_and_packaged_web_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_runner()
    monkeypatch.setattr(module, "_validate_candidate_source_checkout", lambda _source: None)

    def write_bytes(name: str, payload: bytes) -> Path:
        path = tmp_path / name
        path.write_bytes(payload)
        return path

    def file_record(role: str, path: Path) -> dict[str, object]:
        payload = path.read_bytes()
        return {
            "byte_size": len(payload),
            "filename": path.name,
            "role": role,
            "sha256": hashlib.sha256(payload).hexdigest(),
        }

    sidecar = write_bytes("sidecar", b"sidecar")
    wheel = write_bytes("openevo-0.1.4-py3-none-any.whl", b"wheel")
    framework_lock = write_bytes("framework-lock.json", b"lock")
    runtime = write_bytes("runtime.tar", b"runtime")
    daemon = write_bytes("openevo-daemon-linux-x86_64", b"daemon")
    daemon_manifest = write_bytes("openevo-daemon-bundle.json", b"daemon-manifest")
    packaged_root = tmp_path / "packaged-web"
    packaged_root.mkdir()
    build_digest = "a" * 64
    packaged_manifest_payload = json.dumps(
        {"schema_version": "1", "build_digest": build_digest, "files": []},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    root_manifest = packaged_root / ".openevo-product-web.json"
    root_manifest.write_bytes(packaged_manifest_payload)
    packaged_manifest = write_bytes(
        "packaged-web-manifest.json", packaged_manifest_payload
    )
    packaged_manifest_sha256 = hashlib.sha256(packaged_manifest_payload).hexdigest()
    source_commit = "b" * 40
    playwright_payload = json.dumps(
        {
            "schema_version": 2,
            "composition": "packaged_web",
            "provider_kind": "desktop_sidecar",
            "source_commit": source_commit,
            "status": "passed",
            "packaged_web": {
                "build_digest": build_digest,
                "manifest": {
                    "filename": packaged_manifest.name,
                    "sha256": packaged_manifest_sha256,
                },
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    playwright_evidence = write_bytes(
        "playwright-candidate-evidence.json", playwright_payload
    )
    dmg = write_bytes("OpenEvo-Desktop-0.1.4-aarch64.dmg", b"dmg")
    app_smoke_payload = json.dumps(
        {
            "schema_version": 3,
            "launch_origin": "mounted_dmg",
            "source_dmg": {
                "filename": dmg.name,
                "sha256": hashlib.sha256(dmg.read_bytes()).hexdigest(),
            },
            "bundled_external_bin": "openevo-desktop-sidecar",
            "sidecar_ready": True,
            "bundled_external_bin_resolved": True,
            "native_listener_fd_handoff": True,
            "native_executable_fd_handoff": True,
            "process_group_cleanup": True,
            "binary_sha256": {
                "native_executable": hashlib.sha256(b"native").hexdigest(),
                "bundled_external_bin": hashlib.sha256(b"candidate-sidecar").hexdigest(),
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    app_smoke = write_bytes("app-bundle-smoke.json", app_smoke_payload)
    candidate_payload = {
        "schema_version": 6,
        "source_commit": source_commit,
        "version": "0.1.4",
        "files": [
            file_record("core_wheel", wheel),
            file_record("framework_lock", framework_lock),
            file_record("daemon_bundle", daemon),
            file_record("daemon_manifest", daemon_manifest),
            file_record("packaged_web_manifest", packaged_manifest),
            file_record("playwright_evidence", playwright_evidence),
            file_record("desktop_dmg", dmg),
            file_record("app_bundle_smoke", app_smoke),
        ],
        "managed_runtime": {"archive": file_record("runtime", runtime)},
    }
    release_candidate = write_bytes(
        "release-candidate.json",
        json.dumps(candidate_payload, sort_keys=True, separators=(",", ":")).encode(),
    )
    authorities = tuple(
        module.HeldReleaseAsset.open(path)
        for path in (sidecar, wheel, framework_lock, runtime, daemon, daemon_manifest)
    )
    assets = module.ReleaseAssets(
        sidecar=sidecar,
        wheel=wheel,
        framework_lock=framework_lock,
        managed_runtime_archive=runtime,
        daemon_bundle=daemon,
        daemon_manifest=daemon_manifest,
        evidence={},
        authorities=authorities,
    )

    binding = module._validate_renderer_candidate_binding(
        assets=assets,
        release_candidate_manifest=release_candidate,
        app_bundle_smoke=app_smoke,
        packaged_web_manifest=packaged_manifest,
        playwright_candidate_evidence=playwright_evidence,
        packaged_web_root=packaged_root,
    )
    try:
        assert binding.source_commit == source_commit
        assert binding.version == "0.1.4"
        assert binding.build_digest == build_digest
        assert binding.evidence["desktop_dmg_sha256"] == hashlib.sha256(b"dmg").hexdigest()
        assert binding.evidence["candidate_packaged_sidecar_sha256"] == hashlib.sha256(
            b"candidate-sidecar"
        ).hexdigest()
        assert binding.evidence["science_sidecar_sha256"] == hashlib.sha256(
            b"sidecar"
        ).hexdigest()
        module._audit_evidence(
            {"renderer_candidate_binding": binding.evidence}, private_values=()
        )
    finally:
        binding.close()
        assets.close()


def test_local_build_is_release_build_with_managed_runtime_archive(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_runner()
    runtime_archive = tmp_path / "managed-runtime.tar"
    runtime_archive.write_bytes(b"runtime")
    daemon_bundle = tmp_path / "openevo-daemon-linux-x86_64"
    daemon_manifest = tmp_path / "openevo-daemon-bundle.json"
    core_wheel = tmp_path / "openevo-0.1.0-py3-none-any.whl"
    framework_lock = tmp_path / "framework-lock.json"
    daemon_bundle.write_bytes(b"daemon")
    daemon_manifest.write_bytes(b"manifest")
    built_sidecar = tmp_path / "built-sidecar"
    built_sidecar.write_bytes(b"sidecar")
    captured: dict[str, object] = {}
    proxy_values = _proxy_environment_canaries()
    for name, value in proxy_values.items():
        monkeypatch.setenv(name, value)

    class FakeProcess:
        pid = 1234

    def fake_popen(command: list[str], **kwargs: object) -> FakeProcess:
        captured["command"] = command
        captured["environment"] = kwargs["env"]
        build_log = kwargs["stdout"]
        build_log.write(f"{built_sidecar}\n".encode())  # type: ignore[union-attr]
        build_log.flush()  # type: ignore[union-attr]
        return FakeProcess()

    def fake_inspect(
        sidecar: Path,
        wheel: Path,
        lock: Path,
        archive: Path,
        bundle: Path,
        manifest: Path,
        *,
        validation_root: Path,
    ):
        captured["validation_root"] = validation_root
        captured["inspected"] = (sidecar, wheel, lock, archive, bundle, manifest)
        return module.ReleaseAssets(
            sidecar,
            wheel,
            lock,
            archive,
            bundle,
            manifest,
            {},
        )

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(module.os, "getpgid", lambda _pid: FakeProcess.pid)
    monkeypatch.setattr(module, "_wait_for_build_process_group", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(module, "_inspect_release_assets", fake_inspect)

    assets = module._build_assets(
        tmp_path / "build",
        core_wheel,
        framework_lock,
        runtime_archive,
        daemon_bundle,
        daemon_manifest,
        timeout_seconds=1,
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert command[command.index("--managed-runtime-archive") + 1] == str(
        runtime_archive.resolve()
    )
    assert command[command.index("--core-wheel") + 1] == str(core_wheel.resolve())
    assert command[command.index("--framework-lock") + 1] == str(framework_lock.resolve())
    assert command[command.index("--daemon-bundle") + 1] == str(daemon_bundle.resolve())
    assert command[command.index("--daemon-manifest") + 1] == str(daemon_manifest.resolve())
    assert command.count("--release-build") == 1
    environment = captured["environment"]
    assert isinstance(environment, dict)
    assert {name: environment[name] for name in proxy_values} == proxy_values
    assert assets.managed_runtime_archive == runtime_archive.resolve()
    assert captured["validation_root"] == tmp_path / "build" / "validated-assets"
    assert captured["inspected"][-3:] == (  # type: ignore[index]
        runtime_archive.resolve(),
        daemon_bundle,
        daemon_manifest,
    )


def test_build_environment_inherits_proxy_only_for_release_asset_build(
    monkeypatch,
) -> None:
    module = _load_runner()
    proxy_values = _proxy_environment_canaries()
    for name, value in proxy_values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("OPENEVO_PROXY_CANARY", "must-not-be-inherited")

    release_environment = module._release_asset_build_environment()

    assert {name: release_environment[name] for name in proxy_values} == proxy_values
    assert "OPENEVO_PROXY_CANARY" not in release_environment
    assert all(name not in module._build_environment() for name in proxy_values)
    assert all(name not in module._sidecar_environment() for name in proxy_values)
    assert all(name not in module._renderer_environment() for name in proxy_values)


def test_candidate_source_checkout_does_not_inherit_release_proxy(
    monkeypatch,
) -> None:
    module = _load_runner()
    source_commit = "a" * 40
    proxy_values = _proxy_environment_canaries()
    calls: list[dict[str, str]] = []
    for name, value in proxy_values.items():
        monkeypatch.setenv(name, value)

    def fake_run(command: list[str], **kwargs: object):
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        calls.append(environment)
        stdout = f"{source_commit}\n".encode() if command[-1] == "HEAD" else b""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr=b"")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    module._validate_candidate_source_checkout(source_commit)

    assert len(calls) == 2
    assert all(name not in environment for environment in calls for name in proxy_values)


def test_sidecar_launch_does_not_inherit_release_proxy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_runner()
    proxy_values = _proxy_environment_canaries()
    for name, value in proxy_values.items():
        monkeypatch.setenv(name, value)
    captured: dict[str, object] = {}

    class FakeAuthority:
        @staticmethod
        def copy_to(target: Path, *, executable: bool) -> None:
            target.write_bytes(b"sidecar")
            target.chmod(0o700 if executable else 0o600)

    class FakeAssets:
        sidecar = tmp_path / "source-sidecar"

        @staticmethod
        def authority(_path: Path) -> FakeAuthority:
            return FakeAuthority()

    class FakeProcess:
        pid = 4321
        returncode = None

        def __init__(self) -> None:
            self.stdin = BytesIO()

    def fake_popen(_command: list[str], **kwargs: object) -> FakeProcess:
        captured["environment"] = kwargs["env"]
        return FakeProcess()

    monkeypatch.setattr(module, "_fixed_descriptors", lambda *_args: nullcontext())
    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(module.os, "getpgid", lambda _pid: FakeProcess.pid)
    monkeypatch.setattr(module, "_wait_sidecar_ready", lambda *_args, **_kwargs: None)

    native = module._launch_sidecar(FakeAssets(), tmp_path / "launch")
    try:
        environment = captured["environment"]
        assert isinstance(environment, dict)
        assert all(name not in environment for name in proxy_values)
    finally:
        native.process_log.close()


def test_renderer_launch_does_not_inherit_release_proxy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_runner()
    proxy_values = _proxy_environment_canaries()
    for name, value in proxy_values.items():
        monkeypatch.setenv(name, value)
    repository_root = tmp_path / "repo"
    playwright = repository_root / "desktop/node_modules/.bin/playwright"
    playwright.parent.mkdir(parents=True)
    playwright.write_text("#!/bin/sh\n", encoding="utf-8")
    playwright.chmod(0o700)
    config = repository_root / "desktop/playwright.release-live.config.ts"
    config.write_text("export default {};\n", encoding="utf-8")
    packaged_web_root = tmp_path / "packaged-web"
    packaged_web_root.mkdir()
    build_digest = "b" * 64
    (packaged_web_root / ".openevo-product-web.json").write_text(
        json.dumps(
            {"schema_version": "1", "build_digest": build_digest, "files": []}
        ),
        encoding="utf-8",
    )
    source_commit = "a" * 40
    captured: dict[str, object] = {}

    def fake_popen(_command: list[str], **kwargs: object):
        captured["environment"] = kwargs["env"]
        raise OSError("stop after capturing renderer environment")

    monkeypatch.setattr(module, "REPOSITORY_ROOT", repository_root)
    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)

    with pytest.raises(OSError, match="stop after capturing"):
        module._run_renderer_verification(
            native=SimpleNamespace(
                base_url="http://127.0.0.1:1234",
                credentials=SimpleNamespace(session_token="session-token"),
            ),
            workflow=SimpleNamespace(renderer_expectations=lambda: {}),
            desktop_identity={
                "source_commit": source_commit,
                "build_version": "0.1.4",
                "openapi_sha256": "c" * 64,
                "feature_flags": [],
            },
            candidate_binding=SimpleNamespace(
                packaged_web_root=packaged_web_root,
                source_commit=source_commit,
                version="0.1.4",
                build_digest=build_digest,
            ),
            root=tmp_path / "renderer",
            timeout_seconds=30,
            screenshot_output=None,
            progress=None,
        )

    environment = captured["environment"]
    assert isinstance(environment, dict)
    assert all(name not in environment for name in proxy_values)


@pytest.mark.parametrize(
    ("requested_seconds", "test_seconds", "process_seconds"),
    (
        (1.0, 30.0, 45.0),
        (30.0, 30.0, 45.0),
        (300.0, 300.0, 315.0),
        (600.0, 600.0, 615.0),
    ),
)
def test_renderer_timeout_reserves_process_exit_grace(
    requested_seconds: float,
    test_seconds: float,
    process_seconds: float,
) -> None:
    module = _load_runner()

    assert module._renderer_test_timeout_seconds(requested_seconds) == test_seconds
    assert module._renderer_process_timeout_seconds(requested_seconds) == process_seconds


def test_external_assets_bind_exact_embedded_managed_runtime(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_runner()
    sidecar = tmp_path / "openevo-desktop-sidecar"
    sidecar.write_bytes(b"sidecar")
    sidecar.chmod(0o700)
    wheel = tmp_path / "openevo-0.1.0-py3-none-any.whl"
    lock = tmp_path / "framework-lock.json"
    runtime_archive = tmp_path / "managed-runtime.tar"
    daemon_bundle = tmp_path / "openevo-daemon-linux-x86_64"
    daemon_manifest = tmp_path / "openevo-daemon-bundle.json"
    _write_wheel(wheel)
    _write_lock(lock, wheel)
    runtime_archive.write_bytes(b"exact-runtime")
    daemon_bundle.write_bytes(b"exact-daemon")
    daemon_bundle.chmod(0o700)
    daemon_manifest.write_bytes(b"exact-manifest")
    runtime_digest = hashlib.sha256(runtime_archive.read_bytes()).hexdigest()
    calls: list[tuple[object, ...]] = []

    class FakeBuilder:
        class Source:
            def __init__(self, path: Path) -> None:
                self.name = path.name
                self.path = path
                self.byte_size = path.stat().st_size
                self.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()

            def close(self) -> None:
                calls.append(("close", self.path))

        @staticmethod
        def _validate_managed_runtime_archive(archive: Path) -> tuple[int, str]:
            calls.append(("runtime", archive))
            return archive.stat().st_size, runtime_digest

        @staticmethod
        def _validate_fd_bound_bootloader(executable: Path) -> None:
            calls.append(("bootloader", executable))

        @staticmethod
        def _validate_embedded_core_wheel(executable: Path, core_wheel: Path) -> None:
            calls.append(("wheel", executable, core_wheel))

        @staticmethod
        def _validate_embedded_core_framework_lock(
            executable: Path,
            core_wheel: Path,
            framework_lock: Path,
            *,
            version: str,
        ) -> None:
            calls.append(("lock", executable, core_wheel, framework_lock, version))

        @staticmethod
        def _validate_embedded_managed_runtime_archive(
            executable: Path,
            archive: Path,
        ) -> None:
            calls.append(("embedded_runtime", executable, archive))

        @classmethod
        def _open_daemon_release_input_pair(
            cls,
            bundle: Path,
            manifest: Path,
            *,
            repo: Path,
        ):
            calls.append(("daemon_pair", bundle, manifest, repo))
            return cls.Source(bundle), cls.Source(manifest), {"core": {}}

        @staticmethod
        def _validate_daemon_manifest_core(
            manifest: dict[str, object],
            *,
            wheel: Path,
            framework_lock: Path,
            version: str,
        ) -> None:
            calls.append(("daemon_core", manifest, wheel, framework_lock, version))

        @staticmethod
        def _validate_embedded_daemon_release_inputs(
            executable: Path,
            bundle: object,
            manifest: object,
        ) -> None:
            calls.append(("embedded_daemon", executable, bundle, manifest))

    monkeypatch.setattr(module, "_load_sidecar_builder", lambda: FakeBuilder())

    assets = module._inspect_release_assets(
        sidecar,
        wheel,
        lock,
        runtime_archive,
        daemon_bundle,
        daemon_manifest,
        validation_root=tmp_path / "validated-assets",
    )

    validated_root = tmp_path / "validated-assets"
    validated_sidecar = validated_root / sidecar.name
    validated_runtime = validated_root / runtime_archive.name
    assert ("runtime", validated_runtime) in calls
    assert ("embedded_runtime", validated_sidecar, validated_runtime) in calls
    assert any(call[0] == "embedded_daemon" for call in calls)
    assert assets.sidecar == validated_sidecar
    assert assets.wheel == validated_root / wheel.name
    assert stat.S_IMODE(validated_root.stat().st_mode) == 0o700
    assert all(authority.path.parent == validated_root for authority in assets.authorities)
    assert assets.evidence["managed_runtime_archive"] == {
        "sha256": runtime_digest,
        "byte_size": runtime_archive.stat().st_size,
    }
    assert assets.evidence["exact_embedded_assets_verified"] is True
    assert (
        assets.evidence["daemon_bundle"]["sha256"]
        == hashlib.sha256(daemon_bundle.read_bytes()).hexdigest()
    )
    assert (
        assets.evidence["daemon_manifest"]["sha256"]
        == hashlib.sha256(daemon_manifest.read_bytes()).hexdigest()
    )
    module._audit_evidence(assets.evidence, private_values=())


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
        context={"artifacts": []},
        artifacts=first_artifacts,
        document_sha256_by_target={
            target_id: "1" * 64 for target_id in module.REQUIRED_TARGET_IDS
        },
        runtime_context_receipt_sha256=None,
    )
    second = module.SessionObservation(
        evidence={"runtime_context_receipt_core_provenance_verified": True},
        run={
            "pinned_revision": successor,
            "required_revision": {"relation": "active", "revision": successor},
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

    workflow._prepare_followup_successor(
        first,
        {"remote": {"status": "ready", "active_revision": successor}},
    )
    reuse = workflow._assert_successor_reuse(first, second)

    assert reuse["followup_admitted_after_successor_active"] is True
    assert reuse["session_1_excluded_own_successor"] is True
    assert reuse["session_2_pinned_session_1_successor"] is True
    assert reuse["session_1_artifacts_reused"] is True
    assert reuse["session_2_runtime_injection_verified"] is True
    assert reuse["runtime_context_receipt_sha256"] == receipt_sha256
    workflow._expected_followup_successor = None
    with pytest.raises(module.E2EFailure, match="followup_successor_not_prepared"):
        workflow._assert_successor_reuse(first, second)
    workflow._expected_followup_successor = successor
    second.run["required_revision"] = {"relation": "queued", "revision": successor}
    with pytest.raises(module.E2EFailure, match="followup_revision_not_active"):
        workflow._assert_successor_reuse(first, second)
    second.run["required_revision"] = {"relation": "active", "revision": successor}
    second.run["pinned_revision"] = _revision("revision-2", 2)
    with pytest.raises(module.E2EFailure, match="second_session_did_not_pin_successor"):
        workflow._assert_successor_reuse(first, second)


def test_successor_reuse_rejects_session_one_consuming_its_own_output() -> None:
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
            "release_enabled": True,
        }
        for target_id in module.REQUIRED_TARGET_IDS
    )
    first = module.SessionObservation(
        evidence={},
        run={"pinned_revision": predecessor},
        context={
            "artifacts": [
                {
                    "artifact_id": first_artifacts[0]["id"],
                    "artifact_type": first_artifacts[0]["target_id"],
                    "target_id": first_artifacts[0]["target_id"],
                    "revision": successor,
                }
            ]
        },
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
            "required_revision": {"relation": "active", "revision": successor},
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
        artifacts=tuple(
            {
                "id": f"artifact-session-2-{target_id}",
                "target_id": target_id,
                "lineage": {"source_artifact_ids": [f"artifact-session-1-{target_id}"]},
            }
            for target_id in module.REQUIRED_TARGET_IDS
        ),
        document_sha256_by_target={
            target_id: "2" * 64 for target_id in module.REQUIRED_TARGET_IDS
        },
        runtime_context_receipt_sha256="f" * 64,
    )

    workflow._prepare_followup_successor(
        first,
        {"remote": {"status": "ready", "active_revision": successor}},
    )
    with pytest.raises(module.E2EFailure, match="first_session_consumed_own_successor"):
        workflow._assert_successor_reuse(first, second)


def test_successful_session_requires_real_harness_execution_phase() -> None:
    module = _load_runner()
    workflow = _workflow(module)
    revision = _revision("revision-0", 0)
    observation = module.SessionObservation(
        evidence={
            "timeline": {
                "phase_values": ["evolution", "revision", "terminal"],
            },
            "logs": {"count": 1},
        },
        run={
            "status": "succeeded",
            "pinned_revision": revision,
            "execution_mode": "codex_subscription_transcript",
            "capture_mode": "transcript",
        },
        context={
            "capture_mode": "transcript",
            "token_level_metrics_available": False,
            "codex_model": "gpt-5.3-codex-spark",
            "reasoning_effort": "high",
        },
        artifacts=tuple(
            {
                "id": f"artifact-{target_id}",
                "target_id": target_id,
                "produced_revision": _revision("revision-1", 1),
            }
            for target_id in module.REQUIRED_TARGET_IDS
        ),
        document_sha256_by_target={
            target_id: "1" * 64 for target_id in module.REQUIRED_TARGET_IDS
        },
        runtime_context_receipt_sha256=None,
    )

    with pytest.raises(module.E2EFailure, match="terminal_evidence_missing"):
        workflow._assert_successful_session(observation, ordinal=1)


def test_successful_session_requires_typed_transcript_dataset_artifacts() -> None:
    module = _load_runner()
    workflow = _workflow(module)
    workflow._allowed_concrete_method_ids = {
        target_id: frozenset({f"method-{target_id}"})
        for target_id in module.REQUIRED_TARGET_IDS
    }
    predecessor = _revision("revision-0", 0)
    successor = _revision("revision-1", 1)

    def observation(*, artifact_type: str, source_dataset_ids: list[str]):
        return module.SessionObservation(
            evidence={
                "timeline": {
                    "phase_values": ["execution", "evolution", "revision", "terminal"],
                },
                "logs": {"count": 1},
            },
            run={
                "status": "succeeded",
                "pinned_revision": predecessor,
                "execution_mode": "codex_subscription_transcript",
                "capture_mode": "transcript",
            },
            context={
                "capture_mode": "transcript",
                "token_level_metrics_available": False,
            },
            artifacts=tuple(
                {
                    "id": f"artifact-{target_id}",
                    "artifact_type": artifact_type if target_id == "text_memory" else target_id,
                    "target_id": target_id,
                    "produced_revision": successor,
                    "membership_revisions": [successor],
                    "selected": True,
                    "release_enabled": True,
                    "content_sha256": "a" * 64,
                    "byte_size": 1,
                    "lineage": {
                        "method_id": f"method-{target_id}",
                        "job_id": f"job-{target_id}",
                        "source_dataset_ids": (
                            source_dataset_ids
                            if target_id == "text_memory"
                            else ["dataset-transcript"]
                        ),
                        "source_artifact_ids": [],
                    },
                }
                for target_id in module.REQUIRED_TARGET_IDS
            ),
            document_sha256_by_target={
                target_id: "b" * 64 for target_id in module.REQUIRED_TARGET_IDS
            },
            runtime_context_receipt_sha256=None,
        )

    valid = observation(
        artifact_type="text_memory",
        source_dataset_ids=["dataset-transcript"],
    )
    workflow._assert_successful_session(valid, ordinal=1)
    assert valid.evidence["transcript_dataset_lineage_observed"] is True

    with pytest.raises(
        module.E2EFailure,
        match="typed_transcript_artifact_contract_mismatch",
    ):
        workflow._assert_successful_session(
            observation(
                artifact_type="skill_bundle",
                source_dataset_ids=["dataset-transcript"],
            ),
            ordinal=1,
        )
    with pytest.raises(
        module.E2EFailure,
        match="typed_transcript_artifact_contract_mismatch",
    ):
        workflow._assert_successful_session(
            observation(artifact_type="text_memory", source_dataset_ids=[]),
            ordinal=1,
        )

    workflow._allowed_concrete_method_ids["agent_system"] = frozenset(
        {"different-agent-system-method"}
    )
    with pytest.raises(
        module.E2EFailure,
        match="typed_transcript_artifact_contract_mismatch",
    ):
        workflow._assert_successful_session(valid, ordinal=1)


def test_runtime_context_receipt_requires_core_log_provenance() -> None:
    module = _load_runner()
    digest = "f" * 64
    run_id = "run-real-e2e"
    attempt_id = "attempt-real-e2e"
    revision = _revision("revision-1", 1)
    run = {
        "id": run_id,
        "status": "succeeded",
        "current_attempt_id": attempt_id,
        "required_revision": {"relation": "active", "revision": revision},
        "pinned_revision": revision,
    }

    class Api:
        logs = [
            {
                "stream": "stdout",
                "service_id": "codex",
                "level": "info",
                "run_id": run_id,
                "attempt_id": attempt_id,
                "message": module.RUNTIME_CONTEXT_RECEIPT_PREFIX + digest,
            }
        ]

        def page(self, route: str, **_kwargs: object) -> list[dict[str, object]]:
            if route.endswith("/timeline"):
                return []
            if route.endswith("/logs"):
                return list(self.logs)
            if route.endswith("/artifacts"):
                return []
            raise AssertionError(route)

        def request(self, _method: str, route: str, **_kwargs: object):
            if route.endswith("/context"):
                return {
                    "status": "ready",
                    "capture_mode": "transcript",
                    "token_level_metrics_available": False,
                    "artifacts": [],
                    "adapters": [],
                }
            raise AssertionError(route)

    workflow = _workflow(module)
    api = Api()
    workflow._api = api

    spoofed = workflow._observe_session(run, ordinal=2)
    assert spoofed.runtime_context_receipt_sha256 is None
    assert spoofed.evidence["runtime_context_receipt_core_provenance_verified"] is False

    api.logs.append(
        {
            "stream": "core",
            "service_id": "core-control",
            "level": "info",
            "run_id": run_id,
            "attempt_id": attempt_id,
            "message": module.RUNTIME_CONTEXT_RECEIPT_PREFIX + digest,
        }
    )
    authoritative = workflow._observe_session(run, ordinal=2)
    assert authoritative.runtime_context_receipt_sha256 == digest
    assert authoritative.evidence["runtime_context_receipt_core_provenance_verified"] is True


def test_followup_accepts_the_durable_predecessor_projection() -> None:
    module = _load_runner()
    workflow = _workflow(module)
    predecessor = _revision("revision-0", 0)
    successor = _revision("revision-1", 1)
    first = module.SessionObservation(
        evidence={},
        run={"pinned_revision": predecessor},
        context={},
        artifacts=tuple(
            {
                "target_id": target_id,
                "produced_revision": successor,
            }
            for target_id in module.REQUIRED_TARGET_IDS
        ),
        document_sha256_by_target={},
        runtime_context_receipt_sha256=None,
    )

    workflow._prepare_followup_successor(
        first,
        {"remote": {"status": "ready", "active_revision": successor}},
    )
    workflow._prepare_followup_successor(
        first,
        {"remote": {"status": "ready", "active_revision": predecessor}},
    )

    with pytest.raises(module.E2EFailure, match="followup_project_authority_invalid"):
        workflow._prepare_followup_successor(
            first,
            {
                "remote": {
                    "status": "ready",
                    "active_revision": _revision("revision-2", 2),
                }
            },
        )


def test_artifact_content_retries_only_the_transient_publication_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_runner()

    class Api:
        calls = 0

        def request(self, *_args: object, **kwargs: object) -> dict[str, object]:
            self.calls += 1
            if self.calls < 3:
                raise module.E2EFailure(
                    str(kwargs["stage"]),
                    "artifact_content_invalid",
                    http_status=422,
                )
            return {"artifact_id": "artifact-1"}

    workflow = _workflow(module)
    workflow._api = Api()
    sleeps: list[float] = []
    monkeypatch.setattr(module.time, "sleep", sleeps.append)

    assert workflow._artifact_content("artifact-1", ordinal=2) == {
        "artifact_id": "artifact-1"
    }
    assert sleeps == list(module.ARTIFACT_CONTENT_RETRY_DELAYS_SECONDS[:2])


def test_artifact_content_does_not_retry_other_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_runner()

    class Api:
        def request(self, *_args: object, **kwargs: object) -> dict[str, object]:
            raise module.E2EFailure(
                str(kwargs["stage"]),
                "artifact_content_oversize",
                http_status=422,
            )

    workflow = _workflow(module)
    workflow._api = Api()
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    with pytest.raises(module.E2EFailure, match="artifact_content_oversize"):
        workflow._artifact_content("artifact-1", ordinal=2)


def test_smoke_session_requires_execution_without_evolution_outputs() -> None:
    module = _load_runner()
    workflow = _workflow(module, smoke=True)
    workflow._disabled_target_ids = frozenset({"skill_bundle"})
    revision = _revision("revision-0", 0)
    observation = module.SessionObservation(
        evidence={
            "timeline": {"phase_values": ["execution", "terminal"]},
            "logs": {"count": 1},
        },
        run={"status": "succeeded", "pinned_revision": revision},
        context={
            "capture_mode": "transcript",
            "token_level_metrics_available": False,
            "artifacts": [],
            "adapters": [],
        },
        artifacts=(),
        document_sha256_by_target={},
        runtime_context_receipt_sha256=None,
    )

    workflow._assert_smoke_session(observation)

    observation = module.SessionObservation(
        evidence=observation.evidence,
        run=observation.run,
        context=observation.context,
        artifacts=({"id": "unexpected-artifact", "target_id": "skill_bundle"},),
        document_sha256_by_target=observation.document_sha256_by_target,
        runtime_context_receipt_sha256=observation.runtime_context_receipt_sha256,
    )
    with pytest.raises(module.E2EFailure, match="smoke_evolution_artifact_present"):
        workflow._assert_smoke_session(observation)


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


def test_bounded_evidence_contains_no_enumerable_remote_identity(tmp_path: Path) -> None:
    module = _load_runner()
    output = tmp_path / "evidence.json"
    payload = {
        "schema_version": "1",
        "kind": "openevo_desktop_real_science_e2e",
        "outcome": "failed",
        "remote": {
            "ssh_connection_verified": True,
            "host_key_verified": True,
        },
        "failure": {"stage": "project_activate", "code": "project_activation_failed"},
    }

    module._write_evidence(output, payload, private_values=("private-value",))

    assert output.stat().st_size <= module.MAX_EVIDENCE_BYTES
    assert json.loads(output.read_text(encoding="utf-8")) == payload
    assert "compute.example.org" not in output.read_text(encoding="utf-8")
    assert "host_sha256" not in output.read_text(encoding="utf-8")
    assert "user_sha256" not in output.read_text(encoding="utf-8")


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
            "session_1_excluded_own_successor": True,
            "session_2_runtime_injection_verified": True,
            "session_2_lineage_verified": True,
            "runtime_context_receipt_sha256": digest,
        },
        "cleanup": {
            "active_run_cleanup_required": True,
            "active_run_cancel_requested": True,
            "active_run_cancelled": True,
            "active_run_cleanup_succeeded": True,
            "desktop_disconnect_succeeded": True,
            "sidecar_shutdown_succeeded": True,
            "core_ownership_release_requested": True,
        },
    }

    module._audit_evidence(payload, private_values=())


def test_closed_evidence_schema_accepts_single_session_smoke_shape() -> None:
    module = _load_runner()
    digest = "a" * 64
    payload = {
        "run_mode": "single_session_smoke",
        "verification_scope": [
            "desktop_sidecar",
            "ssh_bootstrap",
            "daemon_core",
            "codex_subscription_transcript",
        ],
        "session_count": 1,
        "evolution_targets_enabled": False,
        "artifact_publication_verified": False,
        "cross_session_reuse_verified": False,
        "release_evolution_path_verified": False,
        "canonical_project_head_orchestration_verified": False,
        "codex_subscription_transcript_verified": True,
        "project": {
            "target_ids": [],
            "disabled_target_ids": list(module.REQUIRED_TARGET_IDS),
            "method_ids": {},
            "registry_digest": digest,
        },
        "sessions": [{"ordinal": 1, "artifact_count": 0}],
    }

    module._audit_evidence(payload, private_values=())


def test_cleanup_cancels_active_run_before_disconnect(monkeypatch) -> None:
    module = _load_runner()
    calls: list[tuple[str, str] | tuple[str]] = []
    observations = iter(
        [
            {"id": "run-active", "status": "running", "etag": '"' + "1" * 64 + '"'},
            {"id": "run-active", "status": "cancelled", "etag": '"' + "3" * 64 + '"'},
        ]
    )

    class FakeApi:
        def request(self, method: str, route: str, **kwargs: object):
            calls.append((method, route))
            if method == "GET":
                return next(observations)
            assert method == "POST"
            assert kwargs["expected_status"] == 202
            headers = kwargs["headers"]
            assert headers["If-Match"] == '"' + "1" * 64 + '"'  # type: ignore[index]
            return {
                "id": "run-active",
                "status": "cancelling",
                "etag": '"' + "2" * 64 + '"',
            }

    workflow = _workflow(module)
    workflow._api = FakeApi()
    workflow._active_run = {
        "id": "run-active",
        "status": "running",
        "etag": '"' + "0" * 64 + '"',
    }
    workflow._disconnect = lambda: calls.append(("disconnect",)) or True
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    outcome = workflow.cleanup()

    assert calls == [
        ("GET", "/desktop/v1/runs/run-active"),
        ("POST", "/desktop/v1/runs/run-active/cancel"),
        ("GET", "/desktop/v1/runs/run-active"),
        ("disconnect",),
    ]
    assert outcome == {
        "active_run_cleanup_required": True,
        "active_run_cancel_requested": True,
        "active_run_cancelled": True,
        "active_run_cleanup_succeeded": True,
        "desktop_disconnect_succeeded": True,
    }


def test_cleanup_does_not_treat_non_cancelled_terminal_run_as_success(monkeypatch) -> None:
    module = _load_runner()
    observations = iter(
        [
            {"id": "run-active", "status": "running", "etag": '"' + "1" * 64 + '"'},
            {"id": "run-active", "status": "failed", "etag": '"' + "3" * 64 + '"'},
        ]
    )

    class FakeApi:
        def request(self, method: str, _route: str, **_kwargs: object):
            if method == "GET":
                return next(observations)
            return {
                "id": "run-active",
                "status": "cancelling",
                "etag": '"' + "2" * 64 + '"',
            }

    workflow = _workflow(module)
    workflow._api = FakeApi()
    workflow._active_run = {
        "id": "run-active",
        "status": "running",
        "etag": '"' + "0" * 64 + '"',
    }
    workflow._disconnect = lambda: True
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    outcome = workflow.cleanup()

    assert outcome["active_run_cancel_requested"] is True
    assert outcome["active_run_cancelled"] is False
    assert outcome["active_run_cleanup_succeeded"] is False
    assert outcome["desktop_disconnect_succeeded"] is True


def test_active_run_cleanup_timeout_and_interrupt_are_closed(monkeypatch) -> None:
    module = _load_runner()

    class TimeoutApi:
        def request(self, method: str, _route: str, **_kwargs: object):
            if method == "GET":
                return {
                    "id": "run-active",
                    "status": "running",
                    "etag": '"' + "1" * 64 + '"',
                }
            return {
                "id": "run-active",
                "status": "cancelling",
                "etag": '"' + "2" * 64 + '"',
            }

    timeout_workflow = _workflow(module)
    timeout_workflow._api = TimeoutApi()
    timeout_workflow._active_run = {
        "id": "run-active",
        "status": "running",
        "etag": '"' + "0" * 64 + '"',
    }
    monotonic = iter((0.0, 121.0))
    monkeypatch.setattr(module.time, "monotonic", lambda: next(monotonic))
    timed_out = timeout_workflow._cancel_active_run()
    assert timed_out["active_run_cancel_requested"] is True
    assert timed_out["active_run_cancelled"] is False
    assert timed_out["active_run_cleanup_succeeded"] is False

    class InterruptedApi:
        def request(self, *_args: object, **_kwargs: object):
            raise KeyboardInterrupt

    interrupted_workflow = _workflow(module)
    interrupted_workflow._api = InterruptedApi()
    interrupted_workflow._active_run = {
        "id": "run-active",
        "status": "running",
        "etag": '"' + "0" * 64 + '"',
    }
    interrupted = interrupted_workflow._cancel_active_run()
    assert interrupted["active_run_cancel_requested"] is False
    assert interrupted["active_run_cleanup_succeeded"] is False


def test_capability_selection_enables_all_three_remote_supported_methods() -> None:
    module = _load_runner()
    project = {
        "etag": '"' + "1" * 64 + '"',
        "state": "active",
        "execution": {
            "mode": "codex_subscription_transcript",
            "capture_mode": "transcript",
            "token_level_metrics_available": False,
            "codex_model": "gpt-5.3-codex-spark",
            "reasoning_effort": "high",
            "hf_model": None,
        },
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
                                    "effective_default_method_id": (
                                        "unsupported-skill_bundle"
                                        if target_id == "skill_bundle"
                                        else f"remote-{target_id}"
                                    ),
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
                                        {
                                            "method_id": f"unsupported-{target_id}",
                                            "maturity": "experimental",
                                            "support": {"overall": "unsupported"},
                                            "implementation_identity_digest": "d" * 64,
                                            "default_config_json": '{"must_not_select":true}',
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


def test_smoke_disables_every_remote_capability_target_before_run() -> None:
    module = _load_runner()
    project = {
        "etag": '"' + "1" * 64 + '"',
        "state": "active",
        "execution": {
            "mode": "codex_subscription_transcript",
            "capture_mode": "transcript",
            "token_level_metrics_available": False,
            "codex_model": "gpt-5.3-codex-spark",
            "reasoning_effort": "high",
            "hf_model": None,
        },
        "remote": {
            "status": "ready",
            "active_revision": _revision("r0", 0),
            "registry_digest": "a" * 64,
        },
        "evolution": {"targets": {}},
    }
    target_ids = [*module.REQUIRED_TARGET_IDS, "future_target"]

    class FakeApi:
        patched: dict[str, object] | None = None

        def request(self, method: str, route: str, **kwargs: object):
            if method == "GET" and route.endswith("/capabilities"):
                return {
                    "project_etag": project["etag"],
                    "capabilities": {
                        "registry_digest": "a" * 64,
                        "targets": [{"target_id": target_id} for target_id in target_ids],
                    },
                }
            if method == "PATCH":
                self.patched = kwargs["body"]  # type: ignore[assignment]
                return {"etag": '"' + "2" * 64 + '"'}
            if method == "POST" and route.endswith("/activate"):
                return {"operation_id": "activate", "state": "succeeded"}
            if method == "GET" and "/projects/" in route:
                targets = self.patched["evolution"]["targets"]  # type: ignore[index]
                return {
                    **project,
                    "etag": '"' + "2" * 64 + '"',
                    "evolution": {"targets": targets},
                }
            raise AssertionError((method, route))

    api = FakeApi()
    workflow = _workflow(module, smoke=True)
    workflow._api = api
    workflow.project_id = "project-real-e2e"

    capabilities, disabled_target_ids = workflow._disable_and_activate_targets(project)

    assert capabilities["registry_digest"] == "a" * 64
    assert disabled_target_ids == sorted(target_ids)
    assert api.patched == {
        "evolution": {
            "targets": {
                target_id: {"enabled": False, "method": None, "config": {}}
                for target_id in sorted(target_ids)
            }
        }
    }


def test_smoke_workflow_runs_exactly_one_session_and_labels_scope() -> None:
    module = _load_runner()
    workflow = _workflow(module, smoke=True)
    workflow.project_id = "project-real-e2e"
    revision = _revision("revision-0", 0)
    project = {
        "etag": '"' + "1" * 64 + '"',
        "evolution": {
            "targets": {
                target_id: {"enabled": False, "method": None, "config": {}}
                for target_id in module.REQUIRED_TARGET_IDS
            }
        },
    }
    capabilities = {"registry_digest": "a" * 64, "targets": []}
    observation = module.SessionObservation(
        evidence={"ordinal": 1},
        run={"status": "succeeded", "pinned_revision": revision},
        context={
            "capture_mode": "transcript",
            "token_level_metrics_available": False,
            "artifacts": [],
            "adapters": [],
        },
        artifacts=(),
        document_sha256_by_target={},
        runtime_context_receipt_sha256=None,
    )
    created_ordinals: list[int] = []

    workflow._create_and_confirm_profile = lambda: {"profile_id": "profile"}
    workflow._create_and_activate_project = lambda _profile: project
    workflow._disable_and_activate_targets = lambda _project: (
        capabilities,
        ["agent_system", "skill_bundle", "text_memory"],
    )
    workflow._get_project = lambda: project
    workflow._validate_project = lambda _project: {
        "registry_digest": "a" * 64,
        "checks": ["remote"],
    }

    def create_run(_project: dict[str, object], *, ordinal: int):
        created_ordinals.append(ordinal)
        return observation.run

    workflow._create_run = create_run
    workflow._wait_run = lambda run, *, ordinal: run
    workflow._observe_session = lambda _run, *, ordinal: observation
    workflow._assert_smoke_session = lambda observed: None

    evidence = workflow.run()

    assert created_ordinals == [1]
    assert evidence["run_mode"] == "single_session_smoke"
    assert evidence["session_count"] == 1
    assert evidence["evolution_targets_enabled"] is False
    assert evidence["artifact_publication_verified"] is False
    assert evidence["cross_session_reuse_verified"] is False
    assert evidence["release_evolution_path_verified"] is False
    assert evidence["canonical_project_head_orchestration_verified"] is False
    assert evidence["codex_subscription_transcript_verified"] is True
    assert evidence["sessions"] == [{"ordinal": 1}]
    assert evidence["project"]["target_ids"] == []  # type: ignore[index]
    assert evidence["project"]["method_ids"] == {}  # type: ignore[index]


def test_single_session_evolution_runs_once_and_keeps_release_targets_enabled() -> None:
    module = _load_runner()
    workflow = _workflow(module)
    workflow._single_session_evolution = True
    workflow.project_id = "project-real-e2e"
    workflow._method_ids = {
        target_id: f"method-{target_id}" for target_id in module.REQUIRED_TARGET_IDS
    }
    revision = _revision("revision-0", 0)
    project = {
        "etag": '"' + "1" * 64 + '"',
        "evolution": {
            "targets": {
                target_id: {"enabled": False, "method": None, "config": {}}
                for target_id in module.REQUIRED_TARGET_IDS
            }
        },
    }
    capabilities = {"registry_digest": "a" * 64, "targets": []}
    observation = module.SessionObservation(
        evidence={"ordinal": 1},
        run={"status": "succeeded", "pinned_revision": revision},
        context={
            "capture_mode": "transcript",
            "token_level_metrics_available": False,
            "artifacts": [],
            "adapters": [],
        },
        artifacts=(),
        document_sha256_by_target={},
        runtime_context_receipt_sha256=None,
    )
    created_ordinals: list[int] = []

    workflow._create_and_confirm_profile = lambda: {"profile_id": "profile"}
    workflow._create_and_activate_project = lambda _profile: project
    workflow._select_and_activate_targets = lambda _project: capabilities
    workflow._get_project = lambda: project
    workflow._validate_project = lambda _project: {
        "registry_digest": "a" * 64,
        "checks": ["remote"],
    }

    def create_run(_project: dict[str, object], *, ordinal: int):
        created_ordinals.append(ordinal)
        return observation.run

    workflow._create_run = create_run
    workflow._wait_run = lambda run, *, ordinal: run
    workflow._observe_session = lambda _run, *, ordinal: observation
    workflow._assert_successful_session = lambda observed, *, ordinal: None

    evidence = workflow.run()

    assert created_ordinals == [1]
    assert evidence["run_mode"] == "single_session_evolution_release"
    assert evidence["session_count"] == 1
    assert evidence["evolution_targets_enabled"] is True
    assert evidence["artifact_publication_verified"] is True
    assert evidence["cross_session_reuse_verified"] is False
    assert evidence["release_evolution_path_verified"] is False
    assert evidence["canonical_project_head_orchestration_verified"] is False
    assert evidence["codex_subscription_transcript_verified"] is True
    assert evidence["sessions"] == [{"ordinal": 1}]
    assert evidence["project"]["target_ids"] == list(  # type: ignore[index]
        module.REQUIRED_TARGET_IDS
    )


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


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("--poll-seconds", "31"),
        ("--progress-seconds", "61"),
        ("--activation-timeout-seconds", "1801"),
        ("--run-timeout-seconds", "10801"),
        ("--build-timeout-seconds", "2401"),
        ("--overall-timeout-seconds", "21601"),
    ],
)
def test_timeout_arguments_have_closed_upper_bounds(argument: str, value: str) -> None:
    module = _load_runner()

    with pytest.raises(SystemExit):
        module._parser().parse_args([argument, value])


def test_release_model_profile_rejects_other_codex_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_runner()
    common = [
        "--host",
        "172.17.0.10",
        "--user",
        "openevo",
        "--expected-host-key-fingerprint",
        "SHA256:gWwUMfG3M8znKorAt75ZwhPErkeG8aojtCjJ8kaNl3U",
        "--sidecar",
        "sidecar",
        "--core-wheel",
        "openevo.whl",
        "--framework-lock",
        "framework-lock.json",
        "--daemon-bundle",
        "openevo-daemon-linux-x86_64",
        "--daemon-manifest",
        "openevo-daemon-bundle.json",
        "--managed-runtime-archive",
        "managed-runtime.tar",
    ]
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/openevo-e2e-agent.sock")

    module._validate_runtime_arguments(module._parser().parse_args(common))
    with pytest.raises(module.E2EFailure, match="release_model_profile_required"):
        module._validate_runtime_arguments(
            module._parser().parse_args([*common, "--codex-model", "gpt-5"])
        )
    with pytest.raises(SystemExit):
        module._parser().parse_args([*common, "--reasoning-effort", "medium"])


def test_progress_reporter_is_redacted_and_deadline_is_fail_closed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_runner()
    reporter = module.ProgressReporter(
        interval_seconds=60,
        overall_timeout_seconds=0.01,
    )

    reporter.emit("session_1_poll", "running", force=True)
    output = capsys.readouterr()
    assert output.out == ""
    assert "stage=session_1_poll state=running" in output.err
    assert "remaining_seconds=" in output.err

    time.sleep(0.02)
    with pytest.raises(module.E2EFailure, match="overall_timeout"):
        reporter.remaining("session_1_poll")
    reporter.stop_deadline_enforcement()
    assert reporter.remaining("cleanup") == float("inf")


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
