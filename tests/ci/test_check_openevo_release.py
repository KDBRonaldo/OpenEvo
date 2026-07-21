from __future__ import annotations

import importlib.util
import json
import hashlib
from io import BytesIO
from pathlib import Path
import plistlib
import re
import subprocess
from types import SimpleNamespace
import tomllib
from zipfile import ZipFile

import pytest
import yaml

from desktop.sidecar.release_capabilities import (
    RELEASE_EXECUTION_MODE_CAPABILITIES_V1,
)


RELEASE_CONTRACT = json.loads(
    (Path(__file__).resolve().parents[2] / "desktop/release-contract.json").read_text(
        encoding="utf-8"
    )
)
RELEASE_OPENAPI_SHA256 = RELEASE_CONTRACT["accepted_openapi_digests"][0]
RELEASE_FEATURE_FLAGS = RELEASE_CONTRACT["required_feature_flags"]


GOOD_METADATA = "\n".join(
    [
        "Metadata-Version: 2.4",
        "Name: openevo",
        "Version: 0.1.0",
        "Summary: OpenEvo Desktop and agent evolution orchestration.",
        "",
    ]
)

GOOD_ENTRY_POINTS = "\n".join(
    [
        "[console_scripts]",
        "openevo-backend = openevo.backend.launcher:main",
        "openevo-core-service = openevo.backend.service:main",
        "",
    ]
)


def _load_module():
    path = Path(__file__).resolve().parents[2] / "scripts/ci/check_openevo_release.py"
    spec = importlib.util.spec_from_file_location("check_openevo_release", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_release_candidate_module():
    path = Path(__file__).resolve().parents[2] / "scripts/ci/openevo_release_candidate.py"
    spec = importlib.util.spec_from_file_location("openevo_release_candidate", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_framework_wheel_smoke_module():
    path = Path(__file__).resolve().parents[2] / "scripts/ci/smoke_evolution_framework_wheel.py"
    spec = importlib.util.spec_from_file_location("smoke_evolution_framework_wheel", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_desktop_wheel_smoke_module():
    path = Path(__file__).resolve().parents[2] / "scripts/ci/smoke_openevo_desktop_wheel.py"
    spec = importlib.util.spec_from_file_location("smoke_openevo_desktop_wheel", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_sha256_module():
    path = Path(__file__).resolve().parents[2] / "scripts/ci/write_sha256.py"
    spec = importlib.util.spec_from_file_location("write_sha256", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_sidecar_smoke_module():
    path = Path(__file__).resolve().parents[2] / "scripts/ci/smoke_openevo_desktop_sidecar.py"
    spec = importlib.util.spec_from_file_location("smoke_openevo_desktop_sidecar", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_bundle_smoke_module():
    path = Path(__file__).resolve().parents[2] / "scripts/ci/smoke_openevo_desktop_bundle.py"
    spec = importlib.util.spec_from_file_location("smoke_openevo_desktop_bundle", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_remote_capability_smoke_module():
    path = Path(__file__).resolve().parents[2] / "scripts/ci/smoke_openevo_remote_capabilities.py"
    spec = importlib.util.spec_from_file_location("smoke_openevo_remote_capabilities", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_desktop_wheel_smoke_exercises_config_backed_lifecycle(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    smoke = _load_desktop_wheel_smoke_module()
    wheel = tmp_path / f"openevo-{smoke.OPENEVO_VERSION}-py3-none-any.whl"
    wheel.write_bytes(
        _nested_wheel_bytes(
            metadata=GOOD_METADATA.replace(
                "Version: 0.1.0",
                f"Version: {smoke.OPENEVO_VERSION}",
            )
        )
    )
    monkeypatch.setattr(
        "desktop.sidecar.api.discover_local_openevo_wheel",
        lambda: wheel,
    )

    assert smoke.main() == 0

    output = capsys.readouterr().out
    assert "Installed Core + source Desktop harness smoke passed" in output
    assert "Source Desktop config-backed lifecycle harness passed" in output


def test_write_sha256_writes_sibling_checksum(tmp_path: Path) -> None:
    writer = _load_sha256_module()
    artifact = tmp_path / "OpenEvo Desktop.dmg"
    artifact.write_bytes(b"desktop")

    checksum_path = writer.write_sha256(artifact)

    assert checksum_path.name == "OpenEvo Desktop.dmg.sha256"
    assert checksum_path.read_text(encoding="utf-8") == (
        f"{hashlib.sha256(b'desktop').hexdigest()}  OpenEvo Desktop.dmg\n"
    )


def test_sidecar_smoke_extracts_desktop_static_assets() -> None:
    smoke = _load_sidecar_smoke_module()

    assets = smoke._asset_references(
        '<html><head><link href="/assets/index.css"></head>'
        '<body><script src="assets/index.js"></script></body></html>'
    )

    assert assets == ["assets/index.css", "assets/index.js"]


def test_sidecar_smoke_rejects_core_owned_fields_in_project_contract() -> None:
    smoke = _load_sidecar_smoke_module()

    with pytest.raises(smoke.SmokeFailure, match="Core-owned field"):
        smoke._assert_project_method_contract(
            {
                "method_id": "reflect",
                "config_schema_json": json.dumps(
                    {
                        "type": "object",
                        "properties": {"reflector_llm": {"type": "object"}},
                        "additionalProperties": False,
                    }
                ),
                "default_config_json": "{}",
            }
        )


def test_sidecar_smoke_launches_process_and_checks_assets(tmp_path: Path) -> None:
    smoke = _load_sidecar_smoke_module()
    sidecar = tmp_path / "fake-openevo-desktop-sidecar"
    _write_fake_sidecar(sidecar)

    smoke.smoke_sidecar(sidecar, timeout_seconds=5)


def test_sidecar_smoke_rejects_unreviewed_openapi_digest() -> None:
    smoke = _load_sidecar_smoke_module()
    payload = _release_version_payload()
    payload["openapi_sha256"] = "b" * 64

    with pytest.raises(smoke.SmokeFailure, match="unreviewed OpenAPI digest"):
        smoke._assert_release_version(payload)


def test_sidecar_smoke_rejects_missing_required_feature() -> None:
    smoke = _load_sidecar_smoke_module()
    payload = _release_version_payload()
    payload["feature_flags"] = RELEASE_FEATURE_FLAGS[:-1]

    with pytest.raises(smoke.SmokeFailure, match="omitted required release features"):
        smoke._assert_release_version(payload)


def test_sidecar_smoke_rejects_invalid_desktop_state() -> None:
    smoke = _load_sidecar_smoke_module()

    with pytest.raises(smoke.SmokeFailure, match="invalid Desktop state"):
        smoke._assert_desktop_state({"schema_version": "1"})


def test_bundle_smoke_launches_tauri_main_and_requires_native_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _load_bundle_smoke_module()
    monkeypatch.setattr(smoke.sys, "platform", "linux")
    app = tmp_path / "OpenEvo Desktop.app"
    executable = app / "Contents" / "MacOS" / "OpenEvo Desktop"
    sidecar = app / "Contents" / "MacOS" / "openevo-desktop-sidecar"
    _write_fake_tauri_release_smoke(executable)
    sidecar.write_bytes(b"packaged externalBin")
    sidecar.chmod(0o755)
    with (app / "Contents" / "Info.plist").open("wb") as stream:
        plistlib.dump({"CFBundleExecutable": executable.name}, stream)

    evidence_path = tmp_path / "native-evidence.json"
    source_dmg = tmp_path / "OpenEvo-Desktop-0.1.0-aarch64.dmg"
    source_dmg.write_bytes(b"exact candidate dmg")
    evidence = smoke.smoke_bundle(
        tmp_path,
        launch_origin="mounted_dmg",
        source_dmg=source_dmg,
        timeout_seconds=5,
        evidence_out=evidence_path,
    )

    assert evidence["schema_version"] == 3
    assert evidence["launch_origin"] == "mounted_dmg"
    assert evidence["source_dmg"] == {
        "filename": source_dmg.name,
        "sha256": hashlib.sha256(source_dmg.read_bytes()).hexdigest(),
    }
    assert evidence["binary_sha256"] == {
        "native_executable": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "bundled_external_bin": hashlib.sha256(sidecar.read_bytes()).hexdigest(),
    }
    assert evidence["native_executable"] == executable.name
    assert evidence["bundled_external_bin"] == sidecar.name
    assert evidence["renderer_ready"] is True
    assert evidence["sidecar_ready"] is True
    assert evidence["native_listener_fd_handoff"] is True
    assert evidence["native_executable_fd_handoff"] is True
    assert evidence["process_group_cleanup"] is True
    assert evidence["mach_o"] == {
        "bundled_external_bin": {
            "file_output": "Mach-O 64-bit executable arm64",
            "slices": ["arm64"],
        },
        "native_executable": {
            "file_output": "Mach-O 64-bit executable arm64",
            "slices": ["arm64"],
        },
    }
    assert json.loads(evidence_path.read_text(encoding="utf-8")) == evidence


def test_bundle_smoke_failure_still_runs_bounded_group_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _load_bundle_smoke_module()
    monkeypatch.setattr(smoke.sys, "platform", "linux")
    app = tmp_path / "OpenEvo Desktop.app"
    executable = app / "Contents" / "MacOS" / "OpenEvo Desktop"
    sidecar = executable.with_name("openevo-desktop-sidecar")
    executable.parent.mkdir(parents=True)
    executable.write_text(
        "#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    sidecar.write_bytes(b"packaged externalBin")
    sidecar.chmod(0o755)
    with (app / "Contents" / "Info.plist").open("wb") as stream:
        plistlib.dump({"CFBundleExecutable": executable.name}, stream)
    source_dmg = tmp_path / "candidate.dmg"
    source_dmg.write_bytes(b"dmg")
    cleanup_calls: list[set[int]] = []
    original_cleanup = smoke._cleanup_launched_app

    def tracked_cleanup(process, process_groups, *, timeout_seconds):
        cleanup_calls.append(set(process_groups))
        return original_cleanup(
            process,
            process_groups,
            timeout_seconds=timeout_seconds,
        )

    monkeypatch.setattr(smoke, "_cleanup_launched_app", tracked_cleanup)

    with pytest.raises(smoke.SmokeFailure, match="Timed out waiting"):
        smoke.smoke_bundle(
            tmp_path,
            launch_origin="mounted_dmg",
            source_dmg=source_dmg,
            timeout_seconds=0.2,
        )

    assert len(cleanup_calls) == 1
    assert len(cleanup_calls[0]) == 1


def test_bundle_smoke_inspects_mach_o_with_file_and_lipo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _load_bundle_smoke_module()
    binary = tmp_path / "binary"
    binary.write_bytes(b"mach-o")
    calls: list[list[str]] = []

    def fake_run(arguments, **_kwargs):
        calls.append(arguments)
        if arguments[0] == "file":
            return subprocess.CompletedProcess(
                arguments,
                0,
                "Mach-O 64-bit executable arm64\n",
                "",
            )
        return subprocess.CompletedProcess(arguments, 0, "arm64\n", "")

    monkeypatch.setattr(smoke.subprocess, "run", fake_run)

    assert smoke.inspect_mach_o(binary) == {
        "file_output": "Mach-O 64-bit executable arm64",
        "slices": ["arm64"],
    }
    assert calls == [["file", "-b", str(binary)], ["lipo", "-archs", str(binary)]]


def test_bundle_smoke_rejects_non_mach_o_file_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _load_bundle_smoke_module()
    binary = tmp_path / "binary"
    binary.write_bytes(b"script")
    monkeypatch.setattr(
        smoke.subprocess,
        "run",
        lambda arguments, **_kwargs: subprocess.CompletedProcess(
            arguments,
            0,
            "POSIX shell script\n" if arguments[0] == "file" else "arm64\n",
            "",
        ),
    )

    with pytest.raises(smoke.SmokeFailure, match="not a Mach-O"):
        smoke.inspect_mach_o(binary)


def test_bundle_smoke_requires_openevo_desktop_app_bundle(tmp_path: Path) -> None:
    smoke = _load_bundle_smoke_module()
    other_sidecar = tmp_path / "Other.app" / "Contents" / "MacOS" / "openevo-desktop-sidecar"
    _write_fake_sidecar(other_sidecar)

    try:
        smoke.find_app_executable(tmp_path)
    except smoke.SmokeFailure as exc:
        assert "No OpenEvo Desktop.app bundle found" in str(exc)
    else:
        raise AssertionError("Expected missing OpenEvo Desktop.app bundle to fail")


def test_bundle_smoke_requires_the_closed_macos_loopback_ats_policy(
    tmp_path: Path,
) -> None:
    smoke = _load_bundle_smoke_module()
    app = tmp_path / "OpenEvo Desktop.app"
    info_plist = app / "Contents" / "Info.plist"
    info_plist.parent.mkdir(parents=True)
    valid = {
        "CFBundleExecutable": "OpenEvo Desktop",
        "NSAppTransportSecurity": {
            "NSExceptionDomains": {
                "127.0.0.1": {
                    "NSExceptionAllowsInsecureHTTPLoads": True,
                }
            },
        },
    }
    with info_plist.open("wb") as stream:
        plistlib.dump(valid, stream)

    smoke._validate_macos_loopback_transport(app)

    for invalid_transport in [
        {"NSAllowsArbitraryLoads": True},
        {
            "NSExceptionDomains": {
                "localhost": {"NSExceptionAllowsInsecureHTTPLoads": True}
            },
        },
        {
            "NSExceptionDomains": {
                "127.0.0.1": {"NSExceptionAllowsInsecureHTTPLoads": False}
            },
        },
    ]:
        with info_plist.open("wb") as stream:
            plistlib.dump(
                {**valid, "NSAppTransportSecurity": invalid_transport},
                stream,
            )
        with pytest.raises(smoke.SmokeFailure, match="loopback ATS policy"):
            smoke._validate_macos_loopback_transport(app)


def test_bundle_smoke_requires_the_generated_macos_app_icon(tmp_path: Path) -> None:
    smoke = _load_bundle_smoke_module()
    app = tmp_path / "OpenEvo Desktop.app"
    info_plist = app / "Contents" / "Info.plist"
    packaged_icon = app / "Contents" / "Resources" / "icon.icns"
    expected_icon = tmp_path / "generated-icon.icns"
    packaged_icon.parent.mkdir(parents=True)
    expected_icon.write_bytes(b"generated OpenEvo icon")
    packaged_icon.write_bytes(expected_icon.read_bytes())

    with info_plist.open("wb") as stream:
        plistlib.dump({"CFBundleIconFile": "icon.icns"}, stream)
    smoke._validate_macos_app_icon(app, expected_icon)

    with info_plist.open("wb") as stream:
        plistlib.dump({"CFBundleIconFile": "old-icon.icns"}, stream)
    with pytest.raises(smoke.SmokeFailure, match="does not select"):
        smoke._validate_macos_app_icon(app, expected_icon)

    with info_plist.open("wb") as stream:
        plistlib.dump({"CFBundleIconFile": "icon"}, stream)
    packaged_icon.write_bytes(b"stale icon")
    with pytest.raises(smoke.SmokeFailure, match="does not match"):
        smoke._validate_macos_app_icon(app, expected_icon)


def test_bundle_smoke_rejects_symbolic_app_bundle(tmp_path: Path) -> None:
    smoke = _load_bundle_smoke_module()
    external_app = tmp_path / "external" / "OpenEvo Desktop.app"
    external_app.mkdir(parents=True)
    (tmp_path / "OpenEvo Desktop.app").symlink_to(
        external_app,
        target_is_directory=True,
    )

    with pytest.raises(smoke.SmokeFailure, match="symbolic link"):
        smoke.find_app_executable(tmp_path)


def test_bundle_smoke_rejects_symbolic_tauri_executable(tmp_path: Path) -> None:
    smoke = _load_bundle_smoke_module()
    app = tmp_path / "OpenEvo Desktop.app"
    executable = app / "Contents" / "MacOS" / "OpenEvo Desktop"
    external_executable = tmp_path / "external-openevo-desktop"
    _write_fake_sidecar(external_executable)
    executable.parent.mkdir(parents=True)
    executable.symlink_to(external_executable)
    with (app / "Contents" / "Info.plist").open("wb") as stream:
        plistlib.dump({"CFBundleExecutable": executable.name}, stream)

    with pytest.raises(smoke.SmokeFailure, match="symbolic link"):
        smoke.find_app_executable(tmp_path)


def test_bundle_smoke_rejects_symbolic_bundled_sidecar(tmp_path: Path) -> None:
    smoke = _load_bundle_smoke_module()
    app = tmp_path / "OpenEvo Desktop.app"
    executable = app / "Contents" / "MacOS" / "OpenEvo Desktop"
    sidecar = executable.with_name("openevo-desktop-sidecar")
    external_sidecar = tmp_path / "external-openevo-desktop-sidecar"
    _write_fake_sidecar(executable)
    _write_fake_sidecar(external_sidecar)
    sidecar.symlink_to(external_sidecar)
    with (app / "Contents" / "Info.plist").open("wb") as stream:
        plistlib.dump({"CFBundleExecutable": executable.name}, stream)

    with pytest.raises(smoke.SmokeFailure, match="symbolic link"):
        smoke.find_bundled_sidecar(tmp_path)


def test_bundle_smoke_rejects_sidecar_only_bundle(tmp_path: Path) -> None:
    smoke = _load_bundle_smoke_module()
    sidecar = tmp_path / "OpenEvo Desktop.app" / "Contents" / "MacOS" / "openevo-desktop-sidecar"
    _write_fake_sidecar(sidecar)

    with pytest.raises(smoke.SmokeFailure, match="Info.plist"):
        smoke.smoke_bundle(
            tmp_path,
            launch_origin="mounted_dmg",
            source_dmg=tmp_path / "candidate.dmg",
            timeout_seconds=1,
        )


def test_bundle_smoke_rejects_binary_replacement_during_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _load_bundle_smoke_module()
    monkeypatch.setattr(smoke.sys, "platform", "linux")
    app = tmp_path / "OpenEvo Desktop.app"
    executable = app / "Contents" / "MacOS" / "OpenEvo Desktop"
    sidecar = app / "Contents" / "MacOS" / "openevo-desktop-sidecar"
    _write_fake_tauri_release_smoke(executable)
    executable.write_text(
        executable.read_text(encoding="utf-8").replace(
            "evidence = {\n",
            "Path(__file__).with_name('openevo-desktop-sidecar').write_bytes(b'replaced')\n"
            "evidence = {\n",
            1,
        ),
        encoding="utf-8",
    )
    sidecar.write_bytes(b"packaged externalBin")
    sidecar.chmod(0o755)
    with (app / "Contents" / "Info.plist").open("wb") as stream:
        plistlib.dump({"CFBundleExecutable": executable.name}, stream)
    source_dmg = tmp_path / "candidate.dmg"
    source_dmg.write_bytes(b"dmg")

    with pytest.raises(smoke.SmokeFailure, match="changed during native smoke"):
        smoke.smoke_bundle(
            tmp_path,
            launch_origin="detached_copy",
            source_dmg=source_dmg,
            timeout_seconds=5,
        )


def test_bundle_smoke_parses_latest_native_lifecycle_without_exposing_credentials(
    tmp_path: Path,
) -> None:
    smoke = _load_bundle_smoke_module()
    native_log = tmp_path / "native-host.stderr"
    native_log.write_text(
        "\n".join(
            [
                "unrelated bounded native diagnostic",
                "OPENEVO_DESKTOP_RENDERER_STAGE_V1 sidecar_start_requested",
                (
                    "OPENEVO_DESKTOP_SIDECAR_PROCESS_V2 "
                    "pid=41 pgid=41 sid=41 birth=darwin:1700000000:123 "
                    "executable_device=42 executable_inode=98 "
                    f"executable_sha256={'a' * 64} executable_size=17"
                ),
                (
                    "OPENEVO_DESKTOP_SIDECAR_PROCESS_V2 "
                    "pid=42 pgid=42 sid=42 birth=darwin:1700000001:456 "
                    "executable_device=42 executable_inode=99 "
                    f"executable_sha256={'b' * 64} executable_size=19"
                ),
                "OPENEVO_DESKTOP_RENDERER_STAGE_V1 sidecar_start_returned",
                "OPENEVO_DESKTOP_RENDERER_STAGE_V1 bootstrap_context_validated",
                "OPENEVO_DESKTOP_RENDERER_STAGE_V1 local_api_version_verified",
                "OPENEVO_DESKTOP_RENDERER_STAGE_V1 retry_recovery_ready",
                "OPENEVO_DESKTOP_RENDERER_STAGE_V1 provider_adapter_ready",
                "OPENEVO_DESKTOP_RENDERER_STAGE_V1 provider_created",
                "OPENEVO_DESKTOP_RENDERER_STAGE_V1 product_committed",
                "OPENEVO_DESKTOP_RENDERER_STAGE_V1 ready_requested",
                "OPENEVO_DESKTOP_RENDERER_STAGE_V1 window_not_visible",
                f"OPENEVO_DESKTOP_RENDERER_READY_V2 {RELEASE_OPENAPI_SHA256}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    observation = smoke._parse_native_host_observation(native_log.read_bytes())

    assert observation.active_process.pid == 42
    assert observation.active_process.executable_sha256 == "b" * 64
    assert observation.active_process.executable_size == 19
    assert observation.active_process.executable_device == 42
    assert observation.active_process.executable_inode == 99
    assert observation.renderer_ready is True
    assert observation.process_groups == frozenset({41, 42})
    assert observation.renderer_stages == frozenset(
        {
            "sidecar_start_requested",
            "sidecar_start_returned",
            "bootstrap_context_validated",
            "local_api_version_verified",
            "retry_recovery_ready",
            "provider_adapter_ready",
            "provider_created",
            "product_committed",
            "ready_requested",
            "window_not_visible",
        }
    )
    assert "readiness" not in repr(observation)
    assert "session_token" not in repr(observation)


def test_bundle_smoke_rejects_malformed_native_process_marker(tmp_path: Path) -> None:
    smoke = _load_bundle_smoke_module()
    native_log = tmp_path / "native-host.stderr"
    native_log.write_text(
        "OPENEVO_DESKTOP_SIDECAR_PROCESS_V2 pid=41 secret=do-not-accept\n",
        encoding="utf-8",
    )

    with pytest.raises(smoke.SmokeFailure, match="process marker is malformed"):
        smoke._parse_native_host_observation(native_log.read_bytes())


def test_bundle_smoke_rejects_unknown_renderer_stage() -> None:
    smoke = _load_bundle_smoke_module()

    with pytest.raises(smoke.SmokeFailure, match="renderer stage is malformed"):
        smoke._parse_native_host_observation(
            b"OPENEVO_DESKTOP_RENDERER_STAGE_V1 credential_dumped\n"
        )


def test_bundle_smoke_retains_valid_group_before_later_malformed_marker() -> None:
    smoke = _load_bundle_smoke_module()
    observed_groups = {40}
    payload = (
        "OPENEVO_DESKTOP_SIDECAR_PROCESS_V2 "
        "pid=41 pgid=41 sid=41 birth=darwin:1700000000:123 "
        "executable_device=42 executable_inode=98 "
        f"executable_sha256={'a' * 64} executable_size=17\n"
        "OPENEVO_DESKTOP_SIDECAR_PROCESS_V2 pid=42 malformed=true\n"
    ).encode("ascii")

    with pytest.raises(smoke.SmokeFailure, match="process marker is malformed"):
        smoke._parse_native_host_observation(payload, observed_groups)

    assert observed_groups == {40, 41}


def test_bundle_smoke_retains_valid_group_before_line_limit_failure() -> None:
    smoke = _load_bundle_smoke_module()
    observed_groups = {40}
    marker = (
        "OPENEVO_DESKTOP_SIDECAR_PROCESS_V2 "
        "pid=41 pgid=41 sid=41 birth=darwin:1700000000:123 "
        "executable_device=42 executable_inode=98 "
        f"executable_sha256={'a' * 64} executable_size=17\n"
    ).encode("ascii")
    payload = marker + b"noise\n" * (smoke.NATIVE_HOST_LOG_MAX_LINES + 1)

    with pytest.raises(smoke.SmokeFailure, match="exceeded the line limit"):
        smoke._parse_native_host_observation(payload, observed_groups)

    assert observed_groups == {40, 41}


def test_bundle_smoke_drain_retains_valid_group_before_byte_limit_failure(
    tmp_path: Path,
) -> None:
    smoke = _load_bundle_smoke_module()
    observed_groups = {40}
    marker = (
        "OPENEVO_DESKTOP_SIDECAR_PROCESS_V2 "
        "pid=41 pgid=41 sid=41 birth=darwin:1700000000:123 "
        "executable_device=42 executable_inode=98 "
        f"executable_sha256={'a' * 64} executable_size=17\n"
    ).encode("ascii")
    native_log = tmp_path / "native-host.stderr"
    native_log.write_bytes(marker + b"x" * smoke.NATIVE_HOST_LOG_MAX_BYTES)

    with native_log.open("rb") as stream:
        with pytest.raises(smoke.SmokeFailure, match="exceeded the byte limit"):
            smoke._drain_native_host_stderr(stream, bytearray(), observed_groups)

    assert observed_groups == {40, 41}


def test_bundle_smoke_bounds_native_log_and_resets_stale_renderer_ack() -> None:
    smoke = _load_bundle_smoke_module()
    process_marker = (
        "OPENEVO_DESKTOP_SIDECAR_PROCESS_V2 "
        "pid={pid} pgid={pid} sid={pid} birth=darwin:{seconds}:123 "
        "executable_device=42 executable_inode={inode} "
        f"executable_sha256={'a' * 64} executable_size=17"
    )
    payload = "\n".join(
        [
            process_marker.format(pid=41, seconds=1700000000, inode=98),
            f"OPENEVO_DESKTOP_RENDERER_READY_V2 {RELEASE_OPENAPI_SHA256}",
            process_marker.format(pid=42, seconds=1700000001, inode=99),
        ]
    ).encode("ascii") + b"\n"

    observation = smoke._parse_native_host_observation(payload)

    assert observation.active_process.pid == 42
    assert observation.renderer_ready is False
    with pytest.raises(smoke.SmokeFailure, match="exceeded the byte limit"):
        smoke._parse_native_host_observation(b"x" * (smoke.NATIVE_HOST_LOG_MAX_BYTES + 1))
    with pytest.raises(smoke.SmokeFailure, match="renderer marker is malformed"):
        smoke._parse_native_host_observation(
            payload + f"OPENEVO_DESKTOP_RENDERER_READY_V2 {'b' * 64}\n".encode("ascii")
        )


def test_bundle_smoke_observes_verified_fd_from_native_digest_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _load_bundle_smoke_module()
    sidecar = tmp_path / "openevo-desktop-sidecar"
    sidecar.write_bytes(b"observed sidecar")
    executable = tmp_path / "OpenEvo Desktop"
    executable.write_bytes(b"native executable")
    monkeypatch.setattr(
        smoke,
        "_descendants",
        lambda _pid, _deadline: [(41, 40, 41, "openevo-desktop-sidecar")],
    )
    monkeypatch.setattr(smoke.os, "getpgid", lambda _pid: 41)
    monkeypatch.setattr(smoke.os, "getsid", lambda _pid: 41)
    monkeypatch.setattr(
        smoke,
        "_darwin_process_birth_identity",
        lambda _pid: "darwin:1700000000:123",
    )
    monkeypatch.setattr(
        smoke,
        "_lsof_fd",
        lambda _pid, descriptor, _deadline: smoke.NativeFileDescriptorObservation(
            file_type="IPv4" if descriptor == 3 else "REG",
            name=(
                "127.0.0.1:1234"
                if descriptor == 3
                else "/private/tmp/openevo-sidecar"
            ),
            size=None if descriptor == 3 else len(sidecar.read_bytes()),
            tcp_state="LISTEN" if descriptor == 3 else None,
            device=None if descriptor == 3 else 42,
            inode=None if descriptor == 3 else 99,
        ),
    )
    digest = hashlib.sha256(sidecar.read_bytes()).hexdigest()
    observation = smoke.NativeHostObservation(
        active_process=smoke.NativeSidecarProcessMarker(
            pid=41,
            process_group=41,
            session_id=41,
            birth_identity="darwin:1700000000:123",
            executable_device=42,
            executable_inode=99,
            executable_sha256=digest,
            executable_size=len(sidecar.read_bytes()),
        ),
        renderer_ready=True,
        process_groups=frozenset({41}),
    )

    evidence, process_groups, stage = smoke._macos_native_evidence(
        40,
        executable,
        sidecar,
        {},
        digest,
        observation,
        smoke.time.monotonic() + 5,
    )

    assert evidence is not None
    assert evidence["native_listener_fd_handoff"] is True
    assert evidence["native_executable_fd_handoff"] is True
    assert process_groups == {41}
    assert stage == "ready"

    monkeypatch.setattr(
        smoke,
        "_lsof_fd",
        lambda _pid, descriptor, _deadline: smoke.NativeFileDescriptorObservation(
            file_type="IPv4" if descriptor == 3 else "REG",
            name=(
                "127.0.0.1:1234"
                if descriptor == 3
                else "/private/tmp/openevo-sidecar"
            ),
            size=None if descriptor == 3 else len(sidecar.read_bytes()),
            tcp_state="LISTEN" if descriptor == 3 else None,
            device=None if descriptor == 3 else 42,
            inode=None if descriptor == 3 else 100,
        ),
    )
    rejected, _groups, rejected_stage = smoke._macos_native_evidence(
        40,
        executable,
        sidecar,
        {},
        digest,
        observation,
        smoke.time.monotonic() + 5,
    )
    assert rejected is None
    assert rejected_stage == "executable_fd_unavailable"


def test_bundle_smoke_parses_lsof_machine_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _load_bundle_smoke_module()
    monkeypatch.setattr(
        smoke,
        "_run_probe",
        lambda arguments, **_kwargs: subprocess.CompletedProcess(
            arguments,
            0,
            "p41\nf3\ntIPv4\nD0x2a\ns17\ni99\nn127.0.0.1:1234\nTST=LISTEN\n",
            "",
        ),
    )

    observation = smoke._lsof_fd(41, 3, smoke.time.monotonic() + 5)

    assert observation == smoke.NativeFileDescriptorObservation(
        file_type="IPv4",
        name="127.0.0.1:1234",
        size=17,
        tcp_state="LISTEN",
        device=42,
        inode=99,
    )
    assert smoke._is_loopback_listener(observation) is True


def test_bundle_smoke_bounds_native_lsof_probe_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _load_bundle_smoke_module()
    probe_calls: list[str] = []

    def unavailable_probe(arguments, **_kwargs):
        probe_calls.append(arguments[0])
        return None

    monkeypatch.setattr(
        smoke,
        "_run_probe",
        unavailable_probe,
    )

    deadline = smoke.time.monotonic() + 5
    assert smoke._lsof_fd(41, 4, deadline) == smoke.NativeFileDescriptorObservation(
        None,
        None,
        None,
        None,
        None,
        None,
    )
    assert probe_calls == ["lsof"]


def test_bundle_smoke_lsof_deadline_does_not_replace_product_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _load_bundle_smoke_module()
    sidecar = tmp_path / "openevo-desktop-sidecar"
    sidecar.write_bytes(b"observed sidecar")
    executable = tmp_path / "OpenEvo Desktop"
    executable.write_bytes(b"native executable")
    digest = hashlib.sha256(sidecar.read_bytes()).hexdigest()
    marker = smoke.NativeSidecarProcessMarker(
        pid=41,
        process_group=41,
        session_id=41,
        birth_identity="darwin:1700000000:123",
        executable_device=42,
        executable_inode=99,
        executable_sha256=digest,
        executable_size=sidecar.stat().st_size,
    )
    monkeypatch.setattr(
        smoke,
        "_descendants",
        lambda _pid, _deadline: [(41, 40, 41, "openevo-desktop-sidecar")],
    )
    monkeypatch.setattr(smoke.os, "getpgid", lambda _pid: 41)
    monkeypatch.setattr(smoke.os, "getsid", lambda _pid: 41)
    monkeypatch.setattr(
        smoke,
        "_darwin_process_birth_identity",
        lambda _pid: marker.birth_identity,
    )
    monkeypatch.setattr(
        smoke,
        "_lsof_fd",
        lambda _pid, _descriptor, _deadline: smoke.NativeFileDescriptorObservation(
            None,
            None,
            None,
            None,
            None,
            None,
        ),
    )
    clock = iter((0.0, 0.0, 0.0, 0.0, 2.0))
    monkeypatch.setattr(smoke.time, "monotonic", lambda: next(clock))

    evidence, _groups, stage = smoke._macos_native_evidence(
        40,
        executable,
        sidecar,
        {},
        digest,
        smoke.NativeHostObservation(marker, False, frozenset({41})),
        1.0,
    )

    assert evidence is None
    assert stage == smoke.PROBE_DEADLINE_STAGE


def test_bundle_smoke_probe_timeout_kills_and_reaps_descendants(tmp_path: Path) -> None:
    smoke = _load_bundle_smoke_module()
    pid_file = tmp_path / "probe-pids"
    probe_script = "\n".join(
        [
            "import os",
            "from pathlib import Path",
            "import subprocess",
            "import sys",
            "import time",
            (
                "inherited = subprocess.Popen("
                "[sys.executable, '-c', 'import time; time.sleep(60)'])"
            ),
            (
                "escaped = subprocess.Popen("
                "[sys.executable, '-c', 'import time; time.sleep(60)'], "
                "start_new_session=True)"
            ),
            (
                f"Path({str(pid_file)!r}).write_text("
                "f'{os.getpid()} {inherited.pid} {escaped.pid}')"
            ),
            "time.sleep(60)",
        ]
    )
    started = smoke.time.monotonic()

    probe_pid: int | None = None
    escaped_pid: int | None = None
    try:
        result = smoke._run_probe(
            [smoke.sys.executable, "-c", probe_script],
            deadline=started + 1.0,
            timeout_cap=0.75,
        )

        assert result is None
        assert smoke.time.monotonic() - started < 4.0
        probe_pid, inherited_pid, escaped_pid = (
            int(value) for value in pid_file.read_text(encoding="utf-8").split()
        )

        def is_running(pid: int) -> bool:
            observed = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout.strip()
            return bool(observed) and not observed.startswith("Z")

        disappearance_deadline = smoke.time.monotonic() + 2
        while smoke.time.monotonic() < disappearance_deadline and any(
            is_running(pid) for pid in (probe_pid, inherited_pid, escaped_pid)
        ):
            smoke.time.sleep(0.05)
        assert all(
            not is_running(pid) for pid in (probe_pid, inherited_pid, escaped_pid)
        )
    finally:
        if probe_pid is None and pid_file.is_file():
            probe_pid = int(pid_file.read_text(encoding="utf-8").split()[0])
        if probe_pid is not None:
            smoke._kill_groups({probe_pid}, smoke.signal.SIGKILL)
        if escaped_pid is None and pid_file.is_file():
            escaped_pid = int(pid_file.read_text(encoding="utf-8").split()[2])
        if escaped_pid is not None:
            smoke._kill_groups({escaped_pid}, smoke.signal.SIGKILL)


def test_bundle_smoke_does_not_launch_probe_after_shared_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _load_bundle_smoke_module()
    monkeypatch.setattr(
        smoke.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("expired probe was launched"),
    )

    assert (
        smoke._run_probe(
            ["expired-probe"],
            deadline=smoke.time.monotonic() - 1,
            timeout_cap=5,
        )
        is None
    )


def test_bundle_smoke_caps_observed_sidecar_group_members(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _load_bundle_smoke_module()
    sidecar = tmp_path / "openevo-desktop-sidecar"
    sidecar.write_bytes(b"observed sidecar")
    executable = tmp_path / "OpenEvo Desktop"
    executable.write_bytes(b"native executable")
    digest = hashlib.sha256(sidecar.read_bytes()).hexdigest()
    marker = smoke.NativeSidecarProcessMarker(
        pid=41,
        process_group=41,
        session_id=41,
        birth_identity="darwin:1700000000:123",
        executable_device=42,
        executable_inode=99,
        executable_sha256=digest,
        executable_size=sidecar.stat().st_size,
    )
    monkeypatch.setattr(
        smoke,
        "_descendants",
        lambda _pid, _deadline: [
            (41 + index, 40, 41, "openevo-desktop-sidecar")
            for index in range(smoke.NATIVE_GROUP_MAX_PROCESSES + 1)
        ],
    )
    monkeypatch.setattr(smoke.os, "getpgid", lambda _pid: 41)
    monkeypatch.setattr(smoke.os, "getsid", lambda _pid: 41)
    monkeypatch.setattr(
        smoke,
        "_darwin_process_birth_identity",
        lambda _pid: "darwin:1700000000:123",
    )

    with pytest.raises(smoke.SmokeFailure, match="exceeded the observation limit"):
        smoke._macos_native_evidence(
            40,
            executable,
            sidecar,
            {},
            digest,
            smoke.NativeHostObservation(marker, True, frozenset({41})),
            smoke.time.monotonic() + 5,
        )


def test_bundle_smoke_cleanup_waits_for_parent_owned_sidecar(tmp_path: Path) -> None:
    smoke = _load_bundle_smoke_module()
    sidecar_pid_path = tmp_path / "sidecar-pid"
    sidecar_script = "\n".join(
        [
            "import os",
            "import sys",
            "import time",
            "expected_parent = int(sys.argv[1])",
            "while os.getppid() == expected_parent:",
            "    time.sleep(0.02)",
        ]
    )
    app_script = "\n".join(
        [
            "from pathlib import Path",
            "import os",
            "import signal",
            "import subprocess",
            "import sys",
            "import time",
            (
                "sidecar = subprocess.Popen("
                f"[sys.executable, '-c', {sidecar_script!r}, str(os.getpid())], "
                "start_new_session=True)"
            ),
            "def shutdown(_signal, _frame):",
            "    if sidecar.poll() is None:",
            "        sidecar.terminate()",
            "    sidecar.wait(timeout=5)",
            "    raise SystemExit(0)",
            "signal.signal(signal.SIGTERM, shutdown)",
            f"Path({str(sidecar_pid_path)!r}).write_text(str(sidecar.pid))",
            "time.sleep(60)",
        ]
    )
    app = subprocess.Popen(
        [smoke.sys.executable, "-c", app_script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = smoke.time.monotonic() + 3
        while smoke.time.monotonic() < deadline and not sidecar_pid_path.is_file():
            smoke.time.sleep(0.02)
        sidecar_pid = int(sidecar_pid_path.read_text(encoding="utf-8"))

        assert smoke._cleanup_launched_app(
            app,
            {app.pid, sidecar_pid},
            timeout_seconds=2,
        )
    finally:
        smoke._kill_groups({app.pid}, smoke.signal.SIGKILL)
        if sidecar_pid_path.is_file():
            smoke._kill_groups(
                {int(sidecar_pid_path.read_text(encoding="utf-8"))},
                smoke.signal.SIGKILL,
            )
        try:
            app.wait(timeout=2)
        except subprocess.TimeoutExpired:
            app.kill()
            app.wait(timeout=2)


def test_bundle_smoke_cleanup_never_signals_a_reaped_app_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _load_bundle_smoke_module()
    signalled: list[tuple[set[int], object]] = []
    monkeypatch.setattr(
        smoke,
        "_kill_groups",
        lambda groups, sig: signalled.append((set(groups), sig)),
    )
    monkeypatch.setattr(smoke, "_wait_for_groups_to_exit", lambda _groups, _deadline: True)
    reaped_process = SimpleNamespace(pid=4242, returncode=0)

    assert smoke._cleanup_launched_app(
        reaped_process,
        {4242, 4343},
        timeout_seconds=1,
    )
    assert signalled == []


def test_bundle_smoke_does_not_report_an_existing_zombie_group_as_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _load_bundle_smoke_module()
    monkeypatch.setattr(smoke, "_process_group_exists", lambda _group: True)

    assert (
        smoke._wait_for_groups_to_exit(
            {4242},
            smoke.time.monotonic(),
        )
        is False
    )


def test_bundle_smoke_uses_the_darwin_proc_bsdinfo_layout() -> None:
    smoke = _load_bundle_smoke_module()

    assert smoke.ctypes.sizeof(smoke._DarwinProcBsdInfo) == 136


def test_bundle_smoke_darwin_native_probes_match_live_kernel_state(
    tmp_path: Path,
) -> None:
    import socket

    smoke = _load_bundle_smoke_module()
    if smoke.sys.platform != "darwin":
        pytest.skip("requires macOS libproc and lsof")

    birth_identity = smoke._darwin_process_birth_identity(smoke.os.getpid())
    assert birth_identity is not None
    assert smoke.re.fullmatch(r"darwin:[1-9][0-9]*:[0-9]{1,6}", birth_identity)

    payload = tmp_path / "fd-payload"
    payload.write_bytes(b"verified fd payload")
    with payload.open("rb") as stream:
        metadata = smoke.os.fstat(stream.fileno())
        observed = smoke._lsof_fd(
            smoke.os.getpid(),
            stream.fileno(),
            smoke.time.monotonic() + 5,
        )
        assert observed.file_type == "REG"
        assert observed.device == metadata.st_dev
        assert observed.inode == metadata.st_ino
        assert observed.size == metadata.st_size

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        observed = smoke._lsof_fd(
            smoke.os.getpid(),
            listener.fileno(),
            smoke.time.monotonic() + 5,
        )
        assert smoke._is_loopback_listener(observed) is True


def test_bundle_smoke_rejects_native_marker_for_different_sidecar(
    tmp_path: Path,
) -> None:
    smoke = _load_bundle_smoke_module()
    sidecar = tmp_path / "openevo-desktop-sidecar"
    sidecar.write_bytes(b"observed sidecar")
    executable = tmp_path / "OpenEvo Desktop"
    executable.write_bytes(b"native executable")
    observation = smoke.NativeHostObservation(
        active_process=smoke.NativeSidecarProcessMarker(
            pid=41,
            process_group=41,
            session_id=41,
            birth_identity="darwin:1700000000:123",
            executable_device=42,
            executable_inode=99,
            executable_sha256="f" * 64,
            executable_size=sidecar.stat().st_size,
        ),
        renderer_ready=True,
        process_groups=frozenset({41}),
    )

    cleanup_groups = {40}
    with pytest.raises(smoke.SmokeFailure, match="different packaged sidecar"):
        smoke._macos_native_evidence(
            40,
            executable,
            sidecar,
            {},
            hashlib.sha256(sidecar.read_bytes()).hexdigest(),
            observation,
            smoke.time.monotonic() + 5,
            cleanup_groups,
        )
    assert 41 in cleanup_groups


def test_bundle_smoke_rejects_reused_darwin_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _load_bundle_smoke_module()
    sidecar = tmp_path / "openevo-desktop-sidecar"
    sidecar.write_bytes(b"observed sidecar")
    executable = tmp_path / "OpenEvo Desktop"
    executable.write_bytes(b"native executable")
    digest = hashlib.sha256(sidecar.read_bytes()).hexdigest()
    marker = smoke.NativeSidecarProcessMarker(
        pid=41,
        process_group=41,
        session_id=41,
        birth_identity="darwin:1700000000:123",
        executable_device=42,
        executable_inode=99,
        executable_sha256=digest,
        executable_size=sidecar.stat().st_size,
    )
    monkeypatch.setattr(
        smoke,
        "_descendants",
        lambda _pid, _deadline: [(41, 40, 41, "openevo-desktop-sidecar")],
    )
    monkeypatch.setattr(smoke.os, "getpgid", lambda _pid: 41)
    monkeypatch.setattr(smoke.os, "getsid", lambda _pid: 41)
    monkeypatch.setattr(
        smoke,
        "_darwin_process_birth_identity",
        lambda _pid: "darwin:1700000001:456",
    )

    with pytest.raises(smoke.SmokeFailure, match="birth identity changed"):
        smoke._macos_native_evidence(
            40,
            executable,
            sidecar,
            {},
            digest,
            smoke.NativeHostObservation(marker, True, frozenset({41})),
            smoke.time.monotonic() + 5,
        )


def test_bundle_smoke_reports_closed_native_readiness_stage() -> None:
    smoke = _load_bundle_smoke_module()

    evidence, process_groups, stage = smoke._macos_native_evidence(
        40,
        Path("OpenEvo Desktop"),
        Path("openevo-desktop-sidecar"),
        {},
        "a" * 64,
        smoke.NativeHostObservation(
            active_process=None,
            renderer_ready=False,
            process_groups=frozenset(),
        ),
        smoke.time.monotonic() + 5,
    )

    assert evidence is None
    assert process_groups == set()
    assert stage == "native_marker_absent"
    assert smoke.NATIVE_FAILURE_STAGES == {
        "native_marker_absent",
        "native_process_unavailable",
        "listener_fd_unavailable",
        "executable_fd_unavailable",
        "renderer_ack_absent",
    }
    assert smoke.PROBE_DEADLINE_STAGE == "probe_deadline_exhausted"
    assert smoke.NATIVE_RENDERER_STAGES == {
        "sidecar_start_requested",
        "sidecar_start_returned",
        "sidecar_start_failed",
        "bootstrap_context_validated",
        "bootstrap_context_failed",
        "local_api_version_verified",
        "local_api_version_failed",
        "retry_recovery_ready",
        "retry_recovery_failed",
        "provider_adapter_ready",
        "provider_adapter_failed",
        "provider_created",
        "provider_create_failed",
        "initial_snapshot_failed",
        "product_committed",
        "ready_requested",
        "window_identity_valid",
        "window_identity_invalid",
        "window_visible",
        "window_not_visible",
        "window_visibility_unknown",
        "ready_validation_failed",
    }


def test_bundle_smoke_probe_deadline_preserves_the_deepest_product_stage() -> None:
    smoke = _load_bundle_smoke_module()
    observed = {"native_marker_absent"}

    current = smoke._advance_readiness_stage(
        "native_marker_absent",
        observed,
        "renderer_ack_absent",
    )
    current = smoke._advance_readiness_stage(
        current,
        observed,
        smoke.PROBE_DEADLINE_STAGE,
    )

    assert current == "renderer_ack_absent"
    assert observed == {"native_marker_absent", "renderer_ack_absent"}


def test_bundle_smoke_transient_probe_failure_preserves_the_deepest_product_stage() -> None:
    smoke = _load_bundle_smoke_module()
    observed = {"native_marker_absent"}

    current = smoke._advance_readiness_stage(
        "native_marker_absent",
        observed,
        "renderer_ack_absent",
    )
    current = smoke._advance_readiness_stage(
        current,
        observed,
        "listener_fd_unavailable",
    )

    assert current == "renderer_ack_absent"
    assert observed == {
        "native_marker_absent",
        "listener_fd_unavailable",
        "renderer_ack_absent",
    }


def test_accepts_valid_openevo_release_wheel(tmp_path: Path) -> None:
    checker = _load_module()
    wheel = _write_wheel(tmp_path / "openevo-0.1.0-py3-none-any.whl")

    errors = checker.validate_wheel(wheel, expected_version="0.1.0")

    assert errors == []


def test_rejects_wheel_metadata_version_mismatch(tmp_path: Path) -> None:
    checker = _load_module()
    wheel = _write_wheel(
        tmp_path / "openevo-0.1.0-py3-none-any.whl",
        metadata=GOOD_METADATA.replace("Version: 0.1.0", "Version: 0.2.0"),
    )

    errors = checker.validate_wheel(wheel, expected_version="0.1.0")

    assert any("METADATA Version should be `0.1.0`" in error for error in errors)


def test_requires_packaged_remote_install_wheel(tmp_path: Path) -> None:
    checker = _load_module()
    wheel = _write_wheel(
        tmp_path / "openevo-0.1.0-py3-none-any.whl",
        include_nested_remote_wheel=False,
    )

    errors = checker.validate_wheel(wheel, expected_version="0.1.0")

    assert any("openevo/wheels/openevo-0.1.0-" in error for error in errors)


def test_validates_packaged_remote_install_wheel_metadata(tmp_path: Path) -> None:
    checker = _load_module()
    wheel = _write_wheel(
        tmp_path / "openevo-0.1.0-py3-none-any.whl",
        nested_remote_wheel_metadata=GOOD_METADATA.replace(
            "Version: 0.1.0",
            "Version: 0.2.0",
        ),
    )

    errors = checker.validate_wheel(wheel, expected_version="0.1.0")

    assert any(
        "Nested remote-install wheel METADATA Version should be `0.1.0`" in error
        for error in errors
    )


def test_requires_exact_openevo_wheel_artifact(tmp_path: Path) -> None:
    checker = _load_module()
    openevo_wheel = _write_wheel(tmp_path / "openevo-0.1.0-py3-none-any.whl")
    dmg = tmp_path / "OpenEvo-Desktop-0.1.0-aarch64.dmg"
    dmg.write_bytes(b"not a real dmg; release list validation only checks presence")
    artifacts = [_write_release_notes(tmp_path)]

    assert checker.validate_release_artifacts(
        artifacts,
        expected_version="0.1.0",
    ) == [
        "Release artifacts must include an exact OpenEvo wheel for remote install: "
        "openevo-0.1.0-*.whl.",
        "Release artifacts must include an OpenEvo Desktop macOS .dmg.",
    ]
    assert checker.validate_release_artifacts(
        artifacts + [openevo_wheel, _write_checksum(openevo_wheel)],
        expected_version="0.1.0",
    ) == ["Release artifacts must include an OpenEvo Desktop macOS .dmg."]
    assert (
        checker.validate_release_artifacts(
            artifacts + [openevo_wheel, _write_checksum(openevo_wheel), dmg, _write_checksum(dmg)],
            expected_version="0.1.0",
        )
        == []
    )


def test_release_dmg_name_uses_canonical_hyphenated_format() -> None:
    checker = _load_module()

    assert checker._allowed_dmg_name(
        "OpenEvo-Desktop-0.1.0-aarch64.dmg",
        expected_version="0.1.0",
    )
    assert not checker._allowed_dmg_name(
        "OpenEvo Desktop_0.1.0_aarch64.dmg",
        expected_version="0.1.0",
    )


def test_release_artifact_list_rejects_unknown_files_and_non_openevo_wheels(
    tmp_path: Path,
) -> None:
    checker = _load_module()
    openevo_wheel = _write_wheel(tmp_path / "openevo-0.1.0-py3-none-any.whl")
    polar_wheel = _write_wheel(tmp_path / "polar-0.1.0-py3-none-any.whl")
    dmg = tmp_path / "OpenEvo-Desktop-0.1.0-aarch64.dmg"
    dmg.write_bytes(b"dmg bytes")
    debug_dmg = tmp_path / "debug.dmg"
    debug_dmg.write_bytes(b"debug dmg bytes")
    mislabeled_dmg = tmp_path / "OpenEvo-Desktop-0.1.0-debug.dmg"
    mislabeled_dmg.write_bytes(b"mislabeled dmg bytes")
    unexpected = tmp_path / "debug.log"
    unexpected.write_text("not a release artifact\n", encoding="utf-8")

    errors = checker.validate_release_artifacts(
        [
            openevo_wheel,
            _write_checksum(openevo_wheel),
            polar_wheel,
            _write_checksum(polar_wheel),
            dmg,
            _write_checksum(dmg),
            debug_dmg,
            _write_checksum(debug_dmg),
            mislabeled_dmg,
            _write_checksum(mislabeled_dmg),
            _write_release_notes(tmp_path),
            unexpected,
        ],
        expected_version="0.1.0",
    )

    assert "Unexpected release artifact: polar-0.1.0-py3-none-any.whl" in errors
    assert "Unexpected release artifact: polar-0.1.0-py3-none-any.whl.sha256" in errors
    assert "Unexpected release artifact: debug.dmg" in errors
    assert "Unexpected release artifact: debug.dmg.sha256" in errors
    assert "Unexpected release artifact: OpenEvo-Desktop-0.1.0-debug.dmg" in errors
    assert "Unexpected release artifact: OpenEvo-Desktop-0.1.0-debug.dmg.sha256" in errors
    assert "Unexpected release artifact: debug.log" in errors


def test_release_artifact_list_rejects_multiple_openevo_wheels(tmp_path: Path) -> None:
    checker = _load_module()
    py3_wheel = _write_wheel(tmp_path / "openevo-0.1.0-py3-none-any.whl")
    cp311_wheel = _write_wheel(tmp_path / "openevo-0.1.0-cp311-cp311-macosx_14_0_arm64.whl")
    dmg = tmp_path / "OpenEvo-Desktop-0.1.0-aarch64.dmg"
    dmg.write_bytes(b"dmg bytes")

    errors = checker.validate_release_artifacts(
        [
            py3_wheel,
            _write_checksum(py3_wheel),
            cp311_wheel,
            _write_checksum(cp311_wheel),
            dmg,
            _write_checksum(dmg),
            _write_release_notes(tmp_path),
        ],
        expected_version="0.1.0",
    )

    assert (
        "Release artifacts must include exactly one exact OpenEvo wheel for remote "
        "install, found: openevo-0.1.0-py3-none-any.whl, "
        "openevo-0.1.0-cp311-cp311-macosx_14_0_arm64.whl."
    ) in errors


def test_release_artifact_list_rejects_multiple_desktop_dmgs(tmp_path: Path) -> None:
    checker = _load_module()
    wheel = _write_wheel(tmp_path / "openevo-0.1.0-py3-none-any.whl")
    arm_dmg = tmp_path / "OpenEvo-Desktop-0.1.0-aarch64.dmg"
    x64_dmg = tmp_path / "OpenEvo-Desktop-0.1.0-x64.dmg"
    arm_dmg.write_bytes(b"arm dmg bytes")
    x64_dmg.write_bytes(b"x64 dmg bytes")

    errors = checker.validate_release_artifacts(
        [
            wheel,
            _write_checksum(wheel),
            arm_dmg,
            _write_checksum(arm_dmg),
            x64_dmg,
            _write_checksum(x64_dmg),
            _write_release_notes(tmp_path),
        ],
        expected_version="0.1.0",
    )

    assert (
        "Release artifacts must include exactly one OpenEvo Desktop macOS .dmg, "
        "found: OpenEvo-Desktop-0.1.0-aarch64.dmg, OpenEvo-Desktop-0.1.0-x64.dmg."
    ) in errors


def test_cli_wheel_only_requires_exact_openevo_wheel_artifact_name(
    tmp_path: Path,
    capsys,
) -> None:
    checker = _load_module()
    wheel = _write_wheel(tmp_path / "polar-0.1.0-py3-none-any.whl")
    project_version = checker._project_version()

    result = checker.main(["--wheel", str(wheel)])

    assert result == 1
    assert (
        "Release artifacts must include an exact OpenEvo wheel for remote install: "
        f"openevo-{project_version}-*.whl."
    ) in capsys.readouterr().err


def test_release_artifact_list_rejects_nonexistent_paths(tmp_path: Path) -> None:
    checker = _load_module()
    openevo_wheel = _write_wheel(tmp_path / "openevo-0.1.0-py3-none-any.whl")
    missing_dmg = tmp_path / "release-artifacts" / "openevo-desktop-dmg" / "*.dmg"

    assert checker.validate_release_artifacts(
        [
            openevo_wheel,
            _write_checksum(openevo_wheel),
            _write_release_notes(tmp_path),
            missing_dmg,
        ],
        expected_version="0.1.0",
    ) == [
        f"Release artifact does not exist: {missing_dmg}",
        "Release artifacts must include an OpenEvo Desktop macOS .dmg.",
    ]


def test_release_artifact_list_requires_checksums_and_release_notes(tmp_path: Path) -> None:
    checker = _load_module()
    openevo_wheel = _write_wheel(tmp_path / "openevo-0.1.0-py3-none-any.whl")
    dmg = tmp_path / "OpenEvo-Desktop-0.1.0-aarch64.dmg"
    dmg.write_bytes(b"dmg bytes")

    assert checker.validate_release_artifacts(
        [openevo_wheel, dmg],
        expected_version="0.1.0",
    ) == [
        "Release artifacts must include release-notes.md.",
        "Release artifact openevo-0.1.0-py3-none-any.whl must have a sibling "
        "openevo-0.1.0-py3-none-any.whl.sha256 checksum.",
        "Release artifact OpenEvo-Desktop-0.1.0-aarch64.dmg must have a sibling "
        "OpenEvo-Desktop-0.1.0-aarch64.dmg.sha256 checksum.",
    ]

    bad_checksum = tmp_path / f"{dmg.name}.sha256"
    bad_checksum.write_text("0" * 64 + "  wrong.dmg\n", encoding="utf-8")
    empty_notes = tmp_path / "release-notes.md"
    empty_notes.write_text("", encoding="utf-8")

    errors = checker.validate_release_artifacts(
        [
            openevo_wheel,
            _write_checksum(openevo_wheel),
            dmg,
            bad_checksum,
            empty_notes,
        ],
        expected_version="0.1.0",
    )

    assert f"{empty_notes} must contain non-empty OpenEvo release notes." in errors
    assert (f"{bad_checksum} should reference `{dmg.name}`, got `wrong.dmg`.") in errors


def test_release_artifact_checksums_must_be_siblings(tmp_path: Path) -> None:
    checker = _load_module()
    wheel_dir = tmp_path / "openevo-wheel"
    checksum_dir = tmp_path / "checksums"
    dmg_dir = tmp_path / "openevo-desktop-dmg"
    wheel_dir.mkdir()
    checksum_dir.mkdir()
    dmg_dir.mkdir()
    openevo_wheel = _write_wheel(wheel_dir / "openevo-0.1.0-py3-none-any.whl")
    misplaced_checksum = checksum_dir / f"{openevo_wheel.name}.sha256"
    misplaced_checksum.write_text(
        _write_checksum(openevo_wheel).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    openevo_wheel.with_name(f"{openevo_wheel.name}.sha256").unlink()
    dmg = dmg_dir / "OpenEvo-Desktop-0.1.0-aarch64.dmg"
    dmg.write_bytes(b"dmg bytes")

    errors = checker.validate_release_artifacts(
        [
            openevo_wheel,
            misplaced_checksum,
            dmg,
            _write_checksum(dmg),
            _write_release_notes(tmp_path),
        ],
        expected_version="0.1.0",
    )

    assert (
        "Release artifact openevo-0.1.0-py3-none-any.whl must have a sibling "
        "openevo-0.1.0-py3-none-any.whl.sha256 checksum."
    ) in errors
    assert (
        "Checksum artifact openevo-0.1.0-py3-none-any.whl.sha256 must have a sibling "
        "openevo-0.1.0-py3-none-any.whl artifact."
    ) in errors


def test_rejects_non_openevo_project_metadata(tmp_path: Path) -> None:
    checker = _load_module()
    wheel = _write_wheel(
        tmp_path / "polar-0.1.0-py3-none-any.whl",
        metadata=GOOD_METADATA.replace("Name: openevo", "Name: polar"),
    )

    errors = checker.validate_wheel(wheel, expected_version="0.1.0")

    assert any("METADATA Name should be `openevo`" in error for error in errors)


def test_requires_expected_console_scripts(tmp_path: Path) -> None:
    checker = _load_module()
    wheel = _write_wheel(
        tmp_path / "openevo-0.1.0-py3-none-any.whl",
        entry_points="\n".join(
            [
                "[console_scripts]",
                "openevo = openevo.cli:main",
                "",
            ]
        ),
    )

    errors = checker.validate_wheel(wheel, expected_version="0.1.0")

    assert any("openevo-backend = openevo.backend.launcher:main" in error for error in errors)


def test_rejects_unexpected_console_scripts(tmp_path: Path) -> None:
    checker = _load_module()
    wheel = _write_wheel(
        tmp_path / "openevo-0.1.0-py3-none-any.whl",
        entry_points="\n".join(
            [
                "[console_scripts]",
                "openevo-backend = openevo.backend.launcher:main",
                "openevo = openevo.evolution.cli:main",
                "",
            ]
        ),
    )

    errors = checker.validate_wheel(wheel, expected_version="0.1.0")

    assert any("unexpected script(s): openevo" in error for error in errors)


def test_rejects_core_wheel_packaging_desktop_control_plane(tmp_path: Path) -> None:
    checker = _load_module()
    wheel = _write_wheel(
        tmp_path / "openevo-0.1.0-py3-none-any.whl",
        extra_files={
            "openevo_terminal_bench/cli.py": "",
            "benchmarks/terminal_bench/README.md": "",
            "openevo/desktop/web/index.html": "<title>OpenEvo Desktop</title>",
            "openevo/sidecar/api.py": "",
            "openevo/cli.py": "",
            "desktop/server/app.py": "",
            "desktop/sidecar/api.py": "",
            "desktop/src/App.tsx": "",
            "desktop/src-tauri/tauri.conf.json": "",
            "desktop/packaging/web/index.html": "<title>OpenEvo Desktop</title>",
        },
    )

    errors = checker.validate_wheel(wheel, expected_version="0.1.0")

    assert any("openevo_terminal_bench/" in error for error in errors)
    assert any("benchmarks/terminal_bench/" in error for error in errors)
    assert any("openevo/desktop/" in error for error in errors)
    assert any("openevo/sidecar/" in error for error in errors)
    assert any("openevo/cli.py" in error for error in errors)
    assert any("desktop/server/" in error for error in errors)
    assert any("desktop/sidecar/" in error for error in errors)
    assert any("desktop/src/" in error for error in errors)
    assert any("desktop/src-tauri/" in error for error in errors)
    assert any("desktop/packaging/web/" in error for error in errors)


def test_rejects_removed_terminal_bench_modules_in_core_wheel(tmp_path: Path) -> None:
    checker = _load_module()
    legacy_modules = {
        "openevo/evolution/terminal_bench_bridge.py": "",
        "openevo/evolution/terminal_bench_local_parametric.py": "",
        "openevo/evolution/terminal_bench_per_task.py": "",
        "openevo/evolution/terminal_bench_task_local_parametric.py": "",
    }
    wheel = _write_wheel(
        tmp_path / "openevo-0.1.0-py3-none-any.whl",
        extra_files=legacy_modules,
    )

    errors = checker.validate_wheel(wheel, expected_version="0.1.0")

    boundary_error = next(error for error in errors if "removed Terminal Bench modules" in error)
    assert all(path in boundary_error for path in legacy_modules)


def test_rejects_removed_terminal_bench_modules_in_nested_core_wheel(
    tmp_path: Path,
) -> None:
    checker = _load_module()
    legacy_path = "openevo/evolution/terminal_bench_per_task.py"
    wheel = _write_wheel(
        tmp_path / "openevo-0.1.0-py3-none-any.whl",
        nested_remote_wheel_extra_files={legacy_path: ""},
    )

    errors = checker.validate_wheel(wheel, expected_version="0.1.0")

    assert any(
        "openevo/wheels/openevo-0.1.0-py3-none-any.whl" in error and legacy_path in error
        for error in errors
    )


def test_core_wheel_boundary_allows_unrelated_similar_module_name(
    tmp_path: Path,
) -> None:
    checker = _load_module()
    wheel = _write_wheel(
        tmp_path / "openevo-0.1.0-py3-none-any.whl",
        extra_files={"openevo/evolution/terminal_bench_bridge_v2.py": ""},
    )

    assert checker.validate_wheel(wheel, expected_version="0.1.0") == []


def test_rejects_shared_dashboard_static_assets(tmp_path: Path) -> None:
    checker = _load_module()
    wheel = _write_wheel(
        tmp_path / "openevo-0.1.0-py3-none-any.whl",
        extra_files={
            "openevo/platform/desktop/dist/index.html": ("<title>OpenEvo Observability</title>")
        },
    )

    errors = checker.validate_wheel(wheel, expected_version="0.1.0")

    assert any("openevo/platform/desktop/dist" in error for error in errors)


def test_local_version_validation_reads_top_level_desktop_metadata() -> None:
    checker = _load_module()

    root = Path(__file__).resolve().parents[2]
    paths = {
        path.relative_to(root).as_posix() for path in checker._desktop_package_metadata_paths()
    }

    assert "desktop/package.json" in paths
    assert "desktop/src-tauri/tauri.conf.json" in paths
    assert not any(path.startswith("web/") for path in paths)


def test_release_smoke_workflow_splits_macos_packaging_from_linux_core() -> None:
    workflow = Path(".github/workflows/openevo-release-smoke.yml")
    framework_smoke = Path("scripts/ci/smoke_evolution_framework_wheel.py")
    capability_smoke = Path("scripts/ci/smoke_openevo_remote_capabilities.py")
    sidecar_process_smoke = Path("scripts/ci/smoke_openevo_desktop_sidecar.py")
    desktop_smoke = Path("scripts/ci/smoke_openevo_desktop_wheel.py")

    text = workflow.read_text(encoding="utf-8")
    framework_smoke_text = framework_smoke.read_text(encoding="utf-8")
    capability_smoke_text = capability_smoke.read_text(encoding="utf-8")
    sidecar_process_smoke_text = sidecar_process_smoke.read_text(encoding="utf-8")
    desktop_smoke_text = desktop_smoke.read_text(encoding="utf-8")

    assert text.startswith("name: OpenEvo packaged sidecar + installed Core release smoke")
    assert '"src/slime_bridge/**"' in text
    assert '"desktop/**"' in text
    assert '- "scripts/ci/**"' in text
    assert '"tests/**"' in text

    jobs = text.split("jobs:\n", maxsplit=1)[1]
    macos_job, linux_job = jobs.split("  linux-core-smoke:\n", maxsplit=1)
    assert macos_job.startswith("  macos-packaging-smoke:\n")
    assert "runs-on: macos-14" in macos_job
    assert "runs-on: ubuntu" not in macos_job
    assert "runs-on: ubuntu-latest" in linux_job
    assert "runs-on: macos" not in linux_job
    assert "needs: macos-packaging-smoke" in linux_job

    assert 'node-version: "22"' in macos_job
    assert "npm test -- --run" in macos_job
    assert "npm run typecheck" in macos_job
    assert "npm audit --audit-level=high" in macos_job
    assert "npm run build:openevo" in macos_job
    assert "diff -qr desktop/dist desktop/packaging/web" in macos_job
    assert "uv sync --frozen --group dev" in macos_job
    assert "tests/ci/test_build_sidecar.py" in macos_job
    assert "tests/ci/test_check_openevo_release.py" in macos_job
    assert "test_store_accepts_inode_bound_macos_var_alias" in macos_job
    assert "test_workspace_store_accepts_inode_bound_macos_var_alias" in macos_job
    assert "uv run python desktop/packaging/build_sidecar.py" in macos_job
    assert 'RUNNER_ENVIRONMENT: ${{ runner.environment }}' in macos_job
    assert 'test "$RUNNER_ENVIRONMENT" = "github-hosted"' in macos_job
    assert "macOS packaging + exact Core pair publication" in macos_job
    assert "APFS held-FD cleanup" not in macos_job
    assert '--core-wheel-output-dir "$RUNNER_TEMP/openevo-release-inputs"' in macos_job
    assert "scripts/ci/smoke_openevo_desktop_sidecar.py" in macos_job
    assert text.count("desktop/packaging/build_sidecar.py") == 1
    assert "uv run python packaging/build_sidecar.py" in linux_job

    assert "FSPathMakeRef" not in macos_job
    assert "FSUnlinkObject" not in macos_job
    assert "openevo-core-service ensure" not in macos_job

    artifact_name = (
        "openevo-core-release-inputs-${{ github.sha }}-"
        "${{ github.run_id }}-${{ github.run_attempt }}"
    )
    assert "outputs:\n      manifest_sha256: " in macos_job
    assert "steps.release_inputs.outputs.manifest_sha256" in macos_job
    assert "id: release_inputs" in macos_job
    assert "shasum -a 256 openevo-*.whl framework-lock.json > SHA256SUMS" in macos_job
    assert "shasum -a 256 --check SHA256SUMS" in macos_job
    assert macos_job.count("-mindepth 1 -maxdepth 1") == 1
    assert "actions/upload-artifact@v4" in macos_job
    assert f"name: {artifact_name}" in macos_job
    assert "${{ runner.temp }}/openevo-release-inputs/openevo-*.whl" in macos_job
    assert "${{ runner.temp }}/openevo-release-inputs/framework-lock.json" in macos_job
    assert "${{ runner.temp }}/openevo-release-inputs/SHA256SUMS" in macos_job
    assert "include-hidden-files: true" in macos_job

    assert "actions/download-artifact@v4" in linux_job
    assert f"name: {artifact_name}" in linux_job
    assert "path: .openevo-release-inputs" in linux_job
    assert "name: Restore private Core release input modes" in linux_job
    assert "chmod 0700 .openevo-release-inputs" in linux_job
    release_mode_step = linux_job.split(
        "      - name: Restore private Core release input modes\n", maxsplit=1
    )[1].split(
        "      - name: Verify transferred Core release input manifest\n", maxsplit=1
    )[0]
    assert "chmod 0600 \\" in release_mode_step
    assert ".openevo-release-inputs/openevo-*.whl" in release_mode_step
    assert ".openevo-release-inputs/framework-lock.json" in release_mode_step
    assert ".openevo-release-inputs/SHA256SUMS" in release_mode_step
    assert "stat -c '%a' .openevo-release-inputs/framework-lock.json" in release_mode_step
    assert (
        "EXPECTED_MANIFEST_SHA256: "
        "${{ needs.macos-packaging-smoke.outputs.manifest_sha256 }}"
    ) in linux_job
    assert "sha256sum --check -" in linux_job
    assert "sha256sum --check SHA256SUMS" in linux_job
    assert linux_job.count("-mindepth 1 -maxdepth 1") == 2
    assert "uv sync --frozen --group dev" in linux_job
    assert linux_job.index("actions/download-artifact@v4") < linux_job.index(
        "Restore private Core release input modes"
    )
    assert linux_job.index("Restore private Core release input modes") < linux_job.index(
        "Verify transferred Core release input manifest"
    )
    assert linux_job.index("Verify transferred Core release input manifest") < linux_job.index(
        "pip install .openevo-release-inputs/openevo-*.whl"
    )

    assert 'node-version: "22"' in linux_job
    assert "dtolnay/rust-toolchain@stable" in linux_job
    assert "npm ci" in linux_job
    assert "openevo-core-service ensure" not in linux_job
    assert "openevo-core-service consume-attachment" not in linux_job
    assert "openevo-core-service stop" not in linux_job
    assert '--source-commit "$GITHUB_SHA"' in linux_job
    assert "scripts/ci/smoke_evolution_framework_wheel.py" in linux_job
    assert "scripts/ci/smoke_openevo_remote_capabilities.py" in linux_job
    assert "--wheel .openevo-release-inputs/openevo-*.whl" in linux_job
    assert "--framework-lock .openevo-release-inputs/framework-lock.json" in linux_job
    assert "--mode linux-context-projection" in linux_job
    assert "--mode installed-registry" not in linux_job
    assert '--sidecar "$OPENEVO_LINUX_PACKAGED_SIDECAR"' in linux_job
    assert "openevo-remote-smoke-home" not in linux_job

    assert "name: Reverify final Core candidate bytes after service smoke" in linux_job
    assert "python -m build --wheel --outdir .openevo-release-inputs" not in text
    assert "outer_source" not in linux_job
    assert "python -m build" not in linux_job
    assert "dist/*.whl" not in linux_job
    assert "ensure_core_service" in capability_smoke_text
    assert "stop_core_service_if_generation" in capability_smoke_text
    assert 'parser.add_argument("--framework-lock"' in capability_smoke_text
    assert "load_framework_distribution_lock" in capability_smoke_text
    assert "TemporaryDirectory" not in capability_smoke_text
    assert "shutil.copy2" not in capability_smoke_text
    smoke_body = capability_smoke_text.split("def smoke(", 1)[1].split("def main(", 1)[0]
    assert "attachment = ensure_core_service(" in smoke_body
    assert "if not attachment.attached:" in smoke_body
    assert "expected_generation=attachment.generation" in smoke_body
    assert "expected_release_identity=attachment.release_identity" in smoke_body
    assert "sidecar_smoke.smoke_sidecar" in capability_smoke_text
    assert "TestClient" not in capability_smoke_text
    assert "create_sidecar_app" not in capability_smoke_text
    assert "BackendConnection" not in capability_smoke_text
    assert "backend_client_factory" not in capability_smoke_text
    assert "start_new_session=True" in sidecar_process_smoke_text
    assert '"--listener-fd"' in sidecar_process_smoke_text
    assert '"--native-instance-stdin"' in sidecar_process_smoke_text
    assert "pass_fds=" in sidecar_process_smoke_text
    assert "--backend-base-url" not in sidecar_process_smoke_text
    assert "/desktop/v1/state" in sidecar_process_smoke_text
    assert "/openevo-native/session" in sidecar_process_smoke_text
    assert "source Desktop harness, not a packaged app" in desktop_smoke_text
    assert "EXPECTED_METHOD_IDS" in framework_smoke_text
    assert "EXPECTED_TARGET_IDS" in framework_smoke_text
    assert "EXPECTED_HANDLER_IDS" in framework_smoke_text
    assert "load_framework_distribution_lock" in framework_smoke_text
    assert "FrameworkDistributionLock(" not in framework_smoke_text
    assert "load_framework_distribution_lock" in capability_smoke_text
    assert framework_smoke_text.index("verified = verify_distribution_install(") < (
        framework_smoke_text.index("from openevo.evolution.framework import (")
    )
    assert "load_verified_framework_registry" in framework_smoke_text
    assert framework_smoke_text.index("load_framework_distribution_lock") < (
        framework_smoke_text.index("loaded = load_verified_framework_registry(framework_lock)")
    )

    assert macos_job.index("npm ci") < macos_job.index("npm test -- --run")
    assert macos_job.index("npm ci") < macos_job.index("npm audit --audit-level=high")
    assert macos_job.index("npm audit --audit-level=high") < macos_job.index(
        "npm run build:openevo"
    )
    assert macos_job.index("npm test -- --run") < macos_job.index("npm run build:openevo")
    assert macos_job.index("npm run typecheck") < macos_job.index("npm run build:openevo")
    assert linux_job.index(
        "name: Smoke exact transferred Core release through Linux packaged sidecar gate"
    ) < linux_job.index(
        "name: Reverify final Core candidate bytes after service smoke"
    )


def test_remote_capability_smoke_does_not_stop_without_attachment_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _load_remote_capability_smoke_module()
    wheel = tmp_path / "openevo-0.1.0-py3-none-any.whl"
    framework_lock = tmp_path / "framework-lock.json"
    sidecar = tmp_path / "openevo-desktop-sidecar"
    for path in (wheel, framework_lock, sidecar):
        path.write_bytes(path.name.encode("ascii"))
    imported = tmp_path / "installed/openevo/__init__.py"
    imported.parent.mkdir(parents=True)
    imported.write_text("", encoding="utf-8")
    digest = "a" * 64

    class LockedIdentity:
        distribution = "openevo"
        distribution_version = "0.1.0"
        distribution_digest = digest
        wheel_filename = wheel.name

    class SidecarSmoke:
        @staticmethod
        def smoke_sidecar(path: Path, *, timeout_seconds: float) -> None:
            assert path == sidecar
            assert timeout_seconds == 1.0

    stop_calls: list[Path] = []

    def ensure_core_service(**_kwargs):
        raise RuntimeError("injected ensure failure")

    def stop_core_service_if_generation(*, service_root, **_kwargs):
        stop_calls.append(Path(service_root))

    monkeypatch.setattr(smoke.openevo, "__file__", str(imported))
    monkeypatch.setattr(smoke.metadata, "version", lambda _: "0.1.0")
    monkeypatch.setattr(smoke, "_sha256", lambda _: digest)
    monkeypatch.setattr(
        smoke,
        "load_framework_distribution_lock",
        lambda _: (LockedIdentity(), wheel.resolve()),
    )
    monkeypatch.setattr(smoke, "_load_sidecar_smoke", lambda: SidecarSmoke())
    monkeypatch.setattr(smoke, "default_core_service_root", lambda: tmp_path / "core")
    monkeypatch.setattr(smoke, "ensure_core_service", ensure_core_service)
    monkeypatch.setattr(
        smoke,
        "stop_core_service_if_generation",
        stop_core_service_if_generation,
    )

    with pytest.raises(RuntimeError, match="injected ensure failure"):
        smoke.smoke(
            wheel,
            framework_lock,
            sidecar,
            source_commit="b" * 40,
            timeout_seconds=1.0,
        )

    assert stop_calls == []


def test_remote_capability_smoke_cleanup_is_bound_to_attachment_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _load_remote_capability_smoke_module()
    wheel = tmp_path / "openevo-0.1.0-py3-none-any.whl"
    framework_lock = tmp_path / "framework-lock.json"
    sidecar = tmp_path / "openevo-desktop-sidecar"
    for path in (wheel, framework_lock, sidecar):
        path.write_bytes(path.name.encode("ascii"))
    imported = tmp_path / "installed/openevo/__init__.py"
    imported.parent.mkdir(parents=True)
    imported.write_text("", encoding="utf-8")
    digest = "a" * 64
    attachment = SimpleNamespace(
        attached=False,
        bearer_token="secret",
        generation="b" * 32,
        port=43117,
        release_identity="c" * 64,
    )
    stop_calls: list[dict[str, object]] = []

    class SidecarSmoke:
        @staticmethod
        def smoke_sidecar(_path: Path, *, timeout_seconds: float) -> None:
            assert timeout_seconds == 1.0

        @staticmethod
        def _read_json(_url: str, *, headers: dict[str, str]) -> dict[str, str]:
            assert headers == {"Authorization": "Bearer secret"}
            return {"registry_digest": "d" * 64}

        @staticmethod
        def _assert_capabilities(
            _payload: dict[str, str],
            *,
            execution_mode: str,
            expected_core_version: str,
        ) -> None:
            assert execution_mode == "codex_subscription_transcript"
            assert expected_core_version == "0.1.0"

    monkeypatch.setattr(smoke.openevo, "__file__", str(imported))
    monkeypatch.setattr(smoke.metadata, "version", lambda _: "0.1.0")
    monkeypatch.setattr(smoke, "_sha256", lambda _: digest)
    monkeypatch.setattr(smoke, "_verify_framework_lock_binding", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(smoke, "_load_sidecar_smoke", lambda: SidecarSmoke())
    monkeypatch.setattr(smoke, "default_core_service_root", lambda: tmp_path / "core")
    monkeypatch.setattr(smoke, "ensure_core_service", lambda **_kwargs: attachment)
    monkeypatch.setattr(
        smoke,
        "stop_core_service_if_generation",
        lambda **kwargs: stop_calls.append(kwargs) or False,
    )

    smoke.smoke(
        wheel,
        framework_lock,
        sidecar,
        source_commit="e" * 40,
        timeout_seconds=1.0,
    )

    assert stop_calls == [
        {
            "service_root": tmp_path / "core",
            "expected_generation": attachment.generation,
            "expected_release_identity": attachment.release_identity,
            "deadline_seconds": 1.0,
        }
    ]


def _write_framework_lock(
    path: Path,
    *,
    wheel_filename: str,
    version: str,
    digest: str,
) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "distribution": "openevo",
                "distribution_version": version,
                "distribution_digest": digest,
                "wheel_filename": wheel_filename,
            }
        ),
        encoding="utf-8",
    )


def test_remote_capability_smoke_rejects_lock_for_wrong_wheel_path(tmp_path: Path) -> None:
    smoke = _load_remote_capability_smoke_module()
    digest = hashlib.sha256(b"wheel").hexdigest()
    candidate = tmp_path / "candidate/openevo-0.1.0-py3-none-any.whl"
    locked = tmp_path / "locked/openevo-0.1.0-py3-none-any.whl"
    candidate.parent.mkdir()
    locked.parent.mkdir()
    candidate.write_bytes(b"wheel")
    locked.write_bytes(b"wheel")
    lock = locked.parent / "framework-lock.json"
    _write_framework_lock(lock, wheel_filename=locked.name, version="0.1.0", digest=digest)

    with pytest.raises(RuntimeError, match="does not bind the exact Core wheel"):
        smoke._verify_framework_lock_binding(
            candidate.resolve(),
            lock.resolve(),
            version="0.1.0",
            digest=digest,
        )


@pytest.mark.parametrize(
    ("locked_version", "locked_digest", "locked_filename"),
    [
        ("0.2.0", "a" * 64, "openevo-0.2.0-py3-none-any.whl"),
        ("0.1.0", "b" * 64, "openevo-0.1.0-py3-none-any.whl"),
        ("0.1.0", "a" * 64, "openevo-0.1.0-1-py3-none-any.whl"),
    ],
    ids=("wrong-version", "wrong-digest", "wrong-wheel"),
)
def test_remote_capability_smoke_rejects_mismatched_lock_identity(
    tmp_path: Path,
    locked_version: str,
    locked_digest: str,
    locked_filename: str,
) -> None:
    smoke = _load_remote_capability_smoke_module()
    wheel = tmp_path / "openevo-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    locked_wheel = tmp_path / locked_filename
    if locked_wheel != wheel:
        locked_wheel.write_bytes(b"wheel")
    lock = tmp_path / "framework-lock.json"
    _write_framework_lock(
        lock,
        wheel_filename=locked_filename,
        version=locked_version,
        digest=locked_digest,
    )

    with pytest.raises(RuntimeError, match="does not bind the exact Core wheel"):
        smoke._verify_framework_lock_binding(
            wheel.resolve(),
            lock.resolve(),
            version="0.1.0",
            digest="a" * 64,
        )


def test_pre_external_beta_release_artifact_workflow_is_disabled() -> None:
    workflow = Path(".github/workflows/openevo-release-artifact.yml")

    text = workflow.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in text
    assert "pre-external-beta release artifact path disabled" in text
    assert "build," in text
    assert "redownload, and verify the exact Core and DMG" in text
    assert "docs/maintainer/productization/spec.md" in text
    assert "tags:" not in text
    assert '"v*"' not in text
    assert "actions/upload-artifact@v4" not in text
    assert "python -m build --wheel" not in text
    assert "npm run build:desktop" not in text
    assert "desktop-dmg-artifact:" not in text


def test_desktop_candidate_workflow_roundtrips_exact_unsigned_draft_prerelease() -> None:
    workflow = Path(".github/workflows/openevo-desktop-candidate.yml")
    candidate_tool = Path("scripts/ci/openevo_release_candidate.py")

    text = workflow.read_text(encoding="utf-8")
    candidate_tool_text = candidate_tool.read_text(encoding="utf-8")
    linux_bundle = text[
        text.index("  linux-daemon-bundle:") : text.index("  macos-candidate:")
    ]
    macos_candidate = text[
        text.index("  macos-candidate:") : text.index("  linux-core-candidate:")
    ]
    linux_candidate = text[
        text.index("  linux-core-candidate:") : text.index(
            "  draft-prerelease-roundtrip:"
        )
    ]
    release_test_command = "cargo test --locked --release -- --test-threads=1"

    for marker in (
        "workflow_dispatch:",
        "runs-on: macos-14",
        "runs-on: ubuntu-24.04",
        "timeout-minutes:",
        'test "$GITHUB_REF" = "refs/heads/stable"',
        'RUNNER_ENVIRONMENT: ${{ runner.environment }}',
        'test "$RUNNER_ENVIRONMENT" = "github-hosted"',
        "uv sync --frozen --group dev",
        "tests/ci/test_build_sidecar.py",
        "tests/ci/test_openevo_release_candidate.py",
        "tests/ci/test_openevo_release_evidence.py",
        "tests/openevo/remote/test_system_executables.py",
        "tests/openevo/remote/test_host_keys.py",
        "tests/openevo/remote/test_ssh_transport.py",
        "scripts/ci/audit_openevo_identity.py",
        "npm ci",
        "npx playwright install --with-deps chromium",
        "npm run test:product-browser",
        "npm audit --audit-level=high",
        "uv run pip-audit",
        "--no-emit-project",
        "--pip-requirements",
        "cargo-audit --locked --version 0.22.2",
        "file --version",
        'test -x "$(xcrun --find lipo)"',
        "collect_openevo_release_evidence.py",
        "Retain failed supply-chain summaries",
        "npm test -- --run",
        "npm run typecheck",
        "packaging/build_sidecar.py",
        "--core-wheel",
        "framework-lock.json",
        "--framework-lock",
        "openevo-core-service",
        "components: rustfmt, clippy",
        'cargo metadata --locked --format-version 1 > "$metadata"',
        "cargo fmt --check",
        "cargo clippy --locked --release --all-targets -- -D warnings",
        release_test_command,
        "npm run tauri:build -- --ci",
        "hdiutil attach",
        "smoke_openevo_desktop_bundle.py",
        "--evidence-out candidate-artifacts/app-bundle-smoke.json",
        "--evidence-out candidate-artifacts/dmg-copy-smoke.json",
        "scripts/ci/openevo_release_candidate.py create",
        "core-install-artifact.json",
        "release-candidate.json",
        "SHA256SUMS",
        "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4",
        "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093 # v4",
        "Restore private Core candidate input modes",
        "stat -c '%a' candidate-artifacts/framework-lock.json",
        "retention-days: 14",
        "openevo_release_candidate.py write-notes",
        "openevo_release_candidate.py write-draft-body",
        "openevo_release_candidate.py assert-release-absent",
        "permissions:\n      contents: write",
        "gh release create",
        "--draft",
        "--prerelease",
        "gh release upload",
        "gh release download",
        "diff -qr candidate-artifacts downloaded-draft",
        "validate-draft",
        "--expected-owner",
        "--expected-repository",
        "--release-id-output",
        "apiUrl",
        "gh release view",
        "gh api --paginate",
        ".tag_name | @json",
        "secrets.token_hex(16)",
        "git check-ref-format",
        "steps.verified.outputs.complete != 'true'",
        "if: ${{ always()",
        "git ls-remote --exit-code --tags origin",
        "Refusing to delete a draft not owned by this workflow attempt",
        "gh api --method DELETE",
    ):
        assert marker in text
    assert linux_bundle.index(
        "- name: Require the reviewed stable source on Linux x86_64"
    ) < linux_bundle.index("npm ci") < linux_bundle.index(
        "npx playwright install --with-deps chromium"
    ) < linux_bundle.index("npm run test:product-browser")
    assert release_test_command in macos_candidate
    rust_setup = macos_candidate[
        macos_candidate.index(
            "      - uses: dtolnay/rust-toolchain@"
        ) : macos_candidate.index(
            "      - name: Install locked build dependencies"
        )
    ]
    assert 'toolchain: "1.95.0"' in rust_setup
    assert "components: rustfmt, clippy" in rust_setup
    assert "lipo -version" not in macos_candidate
    assert "bundle/macos" not in macos_candidate
    assert 'RUNNER_ENVIRONMENT: ${{ runner.environment }}' in linux_candidate
    assert 'test "$RUNNER_ENVIRONMENT" = "github-hosted"' in linux_candidate
    assert linux_candidate.index(
        "- name: Require an ephemeral GitHub-hosted Core runner"
    ) < linux_candidate.index("- name: Download exact final candidate bytes")
    assert macos_candidate.count(
        "scripts/ci/smoke_openevo_desktop_bundle.py"
    ) == 2
    assert macos_candidate.index(
        "- name: Validate locked release-mode Tauri dependency graph"
    ) < (
        macos_candidate.index(release_test_command)
    ) < macos_candidate.index("- name: Build unsigned Desktop DMG")
    expected_tauri_steps = (
        (
            "Validate locked release-mode Tauri dependency graph",
            5,
            'cargo metadata --locked --format-version 1 > "$metadata"',
        ),
        ("Check release-mode Tauri formatting", 5, "cargo fmt --check"),
        (
            "Compile release-mode Tauri host",
            30,
            "cargo check --locked --release --all-targets",
        ),
        (
            "Lint release-mode Tauri host",
            30,
            "cargo clippy --locked --release --all-targets -- -D warnings",
        ),
        ("Test release-mode Tauri host", 45, release_test_command),
        (
            "Exercise packaged native sidecar launch",
            10,
            "tests::packaged_external_bin_native_launch_smoke",
        ),
    )
    for index, (name, timeout, command) in enumerate(expected_tauri_steps):
        start = macos_candidate.index(f"      - name: {name}\n")
        end = (
            macos_candidate.index("      - name:", start + 1)
            if index + 1 < len(expected_tauri_steps)
            else macos_candidate.index(
                "      - name: Build unsigned Desktop DMG for the runner architecture\n",
                start,
            )
        )
        step = macos_candidate[start:end]
        assert "working-directory: desktop/src-tauri" in step
        assert f"timeout-minutes: {timeout}" in step
        assert command in step
    metadata_step = macos_candidate[
        macos_candidate.index(
            "      - name: Validate locked release-mode Tauri dependency graph\n"
        ) : macos_candidate.index(
            "      - name: Check release-mode Tauri formatting\n"
        )
    ]
    assert 'metadata="$RUNNER_TEMP/openevo-cargo-metadata.json"' in metadata_step
    assert "json.load(open(sys.argv[1], encoding=\"utf-8\"))" in metadata_step
    assert 'payload.get("packages")' in metadata_step
    assert 'payload.get("workspace_members")' in metadata_step
    build_step = macos_candidate[
        macos_candidate.index(
            "      - name: Build unsigned Desktop DMG for the runner architecture\n"
        ) :
        macos_candidate.index("      - name: Require native binary inspection tools\n")
    ]
    assert "timeout-minutes: 30" in build_step
    assert "npm run tauri:build -- --ci" in build_step

    shipped_app_smoke = macos_candidate.split(
        "      - name: Mount, launch, copy, detach, and relaunch the exact DMG app\n",
        maxsplit=1,
    )[1].split(
        "      - name: Write release notes and authoritative candidate manifest\n",
        maxsplit=1,
    )[0]
    mounted_smoke = (
        "uv run python scripts/ci/smoke_openevo_desktop_bundle.py \\\n"
        '            "$mounted_app" \\\n'
        "            --launch-origin mounted_dmg \\\n"
        '            --source-dmg "$dmg" \\\n'
        "            --timeout-seconds 120 \\\n"
        "            --evidence-out candidate-artifacts/app-bundle-smoke.json"
    )
    copied_smoke = (
        "uv run python scripts/ci/smoke_openevo_desktop_bundle.py \\\n"
        '            "$copied_app" \\\n'
        "            --launch-origin detached_copy \\\n"
        '            --source-dmg "$dmg" \\\n'
        "            --timeout-seconds 120 \\\n"
        "            --evidence-out candidate-artifacts/dmg-copy-smoke.json"
    )
    assert shipped_app_smoke.count(
        "uv run python scripts/ci/smoke_openevo_desktop_bundle.py"
    ) == 2
    assert shipped_app_smoke.count('dmg="candidate-artifacts/OpenEvo-Desktop-') == 1
    assert (
        'hdiutil attach "$dmg" -mountpoint "$mount_dir" -nobrowse -readonly'
        in shipped_app_smoke
    )
    assert 'mounted_app="$mount_dir/OpenEvo Desktop.app"' in shipped_app_smoke
    assert 'copied_app="$copy_dir/OpenEvo Desktop.app"' in shipped_app_smoke
    assert macos_candidate.count("mounted_app=") == 1
    assert macos_candidate.count("copied_app=") == 1
    assert mounted_smoke in shipped_app_smoke
    assert copied_smoke in shipped_app_smoke
    assert 'codesign --verify --deep --strict --verbose=2 "$mounted_app"' in shipped_app_smoke
    assert 'grep -F "Signature=adhoc"' in shipped_app_smoke
    assert 'xattr -w com.apple.quarantine "$quarantine_value" "$copied_app"' in shipped_app_smoke
    assert 'xattr -dr com.apple.quarantine "$copied_app"' in shipped_app_smoke
    assert 'codesign --verify --deep --strict --verbose=2 "$copied_app"' in shipped_app_smoke
    mounted_smoke_position = shipped_app_smoke.index(mounted_smoke)
    copy_position = shipped_app_smoke.index('ditto "$mounted_app" "$copied_app"')
    release_detach = shipped_app_smoke.index('hdiutil detach "$mount_dir" -quiet\n')
    quarantine_position = shipped_app_smoke.index(
        'xattr -w com.apple.quarantine "$quarantine_value" "$copied_app"'
    )
    quarantine_removal_position = shipped_app_smoke.index(
        'xattr -dr com.apple.quarantine "$copied_app"'
    )
    copied_smoke_position = shipped_app_smoke.index(copied_smoke)
    assert mounted_smoke_position < copy_position < release_detach
    assert release_detach < quarantine_position < quarantine_removal_position
    assert quarantine_removal_position < copied_smoke_position
    assert "trap cleanup EXIT" in shipped_app_smoke
    assert (
        'hdiutil detach "$mount_dir" -quiet >/dev/null 2>&1 || true'
        in shipped_app_smoke
    )
    assert 'rm -rf "$mount_dir" "$copy_dir"' in shipped_app_smoke

    candidate_ssh_step = text.split(
        "      - name: Exercise macOS SSH agent relay and fixed executable authority\n",
        maxsplit=1,
    )[1].split(
        "      - name: Resolve exact product version and runner architecture\n",
        maxsplit=1,
    )[0]
    assert "unset SSH_AUTH_SOCK" in candidate_ssh_step
    assert "umask 077" in candidate_ssh_step
    assert 'case "${HOME:-}" in' in candidate_ssh_step
    assert 'ssh_test_prefix="$HOME/.oe-ssh."' in candidate_ssh_step
    assert 'mktemp -d "${ssh_test_prefix}XXXXXX"' in candidate_ssh_step
    assert 'chmod 700 "$ssh_test_root"' in candidate_ssh_step
    assert 'trap \'rm -rf -- "$ssh_test_root"\' EXIT' in candidate_ssh_step
    assert '--basetemp="$ssh_test_root/pytest"' in candidate_ssh_step
    assert "$RUNNER_TEMP" not in candidate_ssh_step

    for marker in (
        "unsigned and not notarized",
        "## Supported Workflows",
        "Codex subscription transcript mode: packaged and declared in this Preview.",
            "Candidate-bound real Codex Subscription science E2E: required before",
            "A public Preview carrying these notes has passed the separate signed publication gate",
        "Self-Deployed Reference mode: unavailable in this Preview.",
        "## Known Limitations",
        "Parameter evolution is not included in this Preview.",
        "PyPI is not used for this release.",
        "Only the declared architecture was built.",
        "command-line quarantine removal is validated.",
        "## Validation Results",
        "Benchmark gates completed by this Preview: 0 of 3.",
        "Textual-memory pass@1 rescue count: pending.",
        "Trajectory-to-skill pass@1 rescue count: pending.",
        "Agent-system pass@1 rescue count: pending.",
        "## Security And Privacy",
        "No analytics, crash reporting, telemetry, or diagnostics upload is enabled by default.",
        "Credential-canary verification for release assets: pending.",
        "Local Desktop data under ~/.openevo/desktop is retained",
        "org.openevo.desktop",
        "run-retry recovery",
        "## Install, Upgrade, And Uninstall",
        "Install:",
        "Upgrade:",
        "Uninstall:",
    ):
        assert marker in candidate_tool_text

    assert "smoke_openevo_remote_capabilities.py" not in text

    assert text.index("npm ci") < text.index("npm run tauri:build -- --ci")
    assert text.index("hdiutil attach") < text.index(
        "scripts/ci/openevo_release_candidate.py create"
    )
    assert text.index("linux-core-candidate:") < text.index("draft-prerelease-roundtrip:")
    candidate_mode_step = text.split(
        "      - name: Restore private Core candidate input modes\n", maxsplit=1
    )[1].split(
        "      - name: Verify candidate manifest and transferred manifest bytes\n", maxsplit=1
    )[0]
    assert "chmod 0600 \\" in candidate_mode_step
    assert "candidate-artifacts/openevo-*.whl" in candidate_mode_step
    assert "candidate-artifacts/framework-lock.json" in candidate_mode_step
    assert text.index("Download exact final candidate bytes") < text.index(
        "Restore private Core candidate input modes"
    )
    assert text.index("Restore private Core candidate input modes") < text.index(
        "Verify candidate manifest and transferred manifest bytes"
    )
    core_lifecycle_step = text.split(
        "      - name: Smoke exact candidate Core service lifecycle\n", maxsplit=1
    )[1].split(
        "  draft-prerelease-roundtrip:\n", maxsplit=1
    )[0]
    assert core_lifecycle_step.count('openevo-core-service"') == 4
    assert '" ensure \\' in core_lifecycle_step
    assert core_lifecycle_step.count("consume-attachment \\") == 2
    assert '" stop \\' in core_lifecycle_step
    assert "--service-root" not in core_lifecycle_step
    assert "openevo-candidate-core-service" not in core_lifecycle_step
    assert (
        'attachment_suffix="$(PYTHONPATH= '
        '"$RUNNER_TEMP/openevo-candidate-core/bin/python" '
        "-c 'import secrets; print(secrets.token_hex(16))')\""
    ) in core_lifecycle_step
    assert '[[ "$attachment_suffix" =~ ^[0-9a-f]{32}$ ]]' in core_lifecycle_step
    assert core_lifecycle_step.count("attachment_name=") == 1
    assert 'attachment_name="bootstrap-${attachment_suffix}.json"' in core_lifecycle_step
    assert core_lifecycle_step.count('--attachment-name "$attachment_name"') == 3
    assert "GITHUB_RUN_ID" not in core_lifecycle_step
    assert "GITHUB_RUN_ATTEMPT" not in core_lifecycle_step
    assert "${{ github.run_id }}" not in core_lifecycle_step
    assert "${{ github.run_attempt }}" not in core_lifecycle_step
    assert core_lifecycle_step.count("trap cleanup EXIT") == 1
    lifecycle_cleanup = core_lifecycle_step.split("          cleanup() {\n", maxsplit=1)[
        1
    ].split("          }\n", maxsplit=1)[0]
    assert (
        'PYTHONPATH= "$RUNNER_TEMP/openevo-candidate-core/bin/openevo-core-service" \\\n'
        "              consume-attachment \\\n"
        '              --attachment-name "$attachment_name" '
        ">/dev/null 2>&1 || true"
    ) in lifecycle_cleanup
    assert lifecycle_cleanup.index("consume-attachment") < lifecycle_cleanup.index(
        '" stop \\'
    )
    assert "needs: [macos-candidate, linux-core-candidate]" in text
    draft_job = text.split("  draft-prerelease-roundtrip:\n", maxsplit=1)[1]
    assert "environment: openevo-preview-publication" in draft_job
    assert text.index("gh release create") < text.index("gh release upload")
    assert text.index("gh release upload") < text.index("gh release download")
    cleanup = text.split(
        "      - name: Delete an owned unverified draft", maxsplit=1
    )[1]
    assert cleanup.index("validate-draft") < cleanup.index("gh api --method DELETE")
    assert "--cleanup-tag" not in cleanup
    assert "gh release delete" not in cleanup
    assert cleanup.index("--release-id-output") < cleanup.index(
        '"repos/${GITHUB_REPOSITORY}/releases/${release_id}"'
    )
    assert "/releases/tags/" not in text
    assert text.count("assert-release-absent") >= 2
    assert text.count("git ls-remote --exit-code --tags origin") >= 3
    assert text.index("Verify every draft asset and review-facing field") < text.index(
        "Mark draft roundtrip complete"
    )
    candidate_artifact_name = (
        "openevo-desktop-candidate-${{ github.sha }}-"
        "${{ github.run_id }}-${{ github.run_attempt }}"
    )
    assert text.count(candidate_artifact_name) == 3
    publication_inputs_name = (
        "openevo-desktop-publication-inputs-${{ github.sha }}-"
        "${{ github.run_id }}-${{ github.run_attempt }}"
    )
    assert text.count(publication_inputs_name) == 1
    parsed_candidate = yaml.safe_load(text)
    publication_upload = next(
        step
        for step in parsed_candidate["jobs"]["macos-candidate"]["steps"]
        if step.get("name") == "Upload immutable publication verification inputs"
    )
    assert publication_upload == {
        "name": "Upload immutable publication verification inputs",
        "uses": "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
        "with": {
            "name": publication_inputs_name,
            "path": (
                "candidate-artifacts/release-candidate.json\n"
                "candidate-artifacts/app-bundle-smoke.json\n"
            ),
            "if-no-files-found": "error",
            "retention-days": 14,
        },
    }
    assert "openevo-desktop-candidate-${{ github.sha }}\n" not in text
    assert "if: failure() && steps.release.outputs.tag != ''" not in text
    assert text.count("contents: write") == 1
    assert "softprops/action-gh-release" not in text
    assert "cargo audit --ignore" not in text
    assert "--ignore-vuln" not in text
    assert "--suppress" not in text
    assert "tags:" not in text
    assert "universal" not in text
    assert "matrix:" not in text
    assert "Build distributable Core wheel" not in text
    assert "python -m build --wheel" not in text
    assert 'cp "$OPENEVO_CANDIDATE_CORE"/openevo-*.whl candidate-artifacts/' in text
    assert "--no-deps candidate-artifacts/openevo-*.whl" in text
    assert text.index("openevo_release_candidate.py validate") < text.index(
        "--no-deps candidate-artifacts/openevo-*.whl"
    )
    desktop_checks = Path(".github/workflows/openevo-desktop.yml").read_text(encoding="utf-8")
    assert '".github/workflows/openevo-desktop-candidate.yml"' in desktop_checks
    assert "Exercise macOS Core release publication contract" in desktop_checks
    assert "Core release ACL and cleanup policy" not in desktop_checks


def test_desktop_candidate_assigns_platform_safe_framework_smoke_modes() -> None:
    workflow = Path(".github/workflows/openevo-desktop-candidate.yml").read_text(encoding="utf-8")
    framework_smoke = Path("scripts/ci/smoke_evolution_framework_wheel.py").read_text(
        encoding="utf-8"
    )
    macos_job, linux_and_release_jobs = workflow.split("  linux-core-candidate:\n", maxsplit=1)
    linux_job = linux_and_release_jobs.split("  draft-prerelease-roundtrip:\n", maxsplit=1)[0]

    smoke_command = "scripts/ci/smoke_evolution_framework_wheel.py"
    assert "Clean-install and verify final Core wheel, lock, and registry" in macos_job
    assert macos_job.count(smoke_command) == 1
    assert "--mode installed-registry" in macos_job
    assert "--mode linux-context-projection" not in macos_job
    assert "Smoke Linux migration and context projection from exact Core wheel" in linux_job
    assert linux_job.count(smoke_command) == 1
    assert "--mode linux-context-projection" in linux_job
    assert "--mode installed-registry" not in linux_job
    assert linux_job.index(
        "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093"
    ) < linux_job.index(smoke_command)
    assert linux_job.index("Restore private Core candidate input modes") < linux_job.index(
        smoke_command
    )
    assert linux_job.index("openevo_release_candidate.py validate") < linux_job.index(
        smoke_command
    )
    assert linux_job.index("--no-deps candidate-artifacts/openevo-*.whl") < linux_job.index(
        smoke_command
    )
    assert '"--mode",' in framework_smoke
    assert 'choices=("installed-registry", "linux-context-projection")' in framework_smoke
    assert 'if mode == "linux-context-projection" and sys.platform != "linux":' in (
        framework_smoke
    )
    assert 'evidence["verification_mode"] = mode' in framework_smoke
    assert '"passed" if mode == "linux-context-projection" else "not-run"' in (framework_smoke)
    assert 'smoke.get("verification_mode") != "linux-context-projection"' in linux_job
    assert 'smoke.get("linux_context_projection") != "passed"' in linux_job
    assert "needs: macos-candidate" in linux_job
    assert "needs: [macos-candidate, linux-core-candidate]" in workflow


def test_linux_context_projection_mode_fails_closed_off_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    framework_smoke = _load_framework_wheel_smoke_module()
    monkeypatch.setattr(framework_smoke.sys, "platform", "darwin")

    with pytest.raises(
        RuntimeError,
        match="linux-context-projection framework smoke requires Linux",
    ):
        framework_smoke.smoke(
            Path("missing.whl"),
            Path("missing-framework-lock.json"),
            mode="linux-context-projection",
        )


def test_framework_wheel_smoke_dispatches_only_the_selected_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    framework_smoke = _load_framework_wheel_smoke_module()
    loaded_registry = object()
    projection_calls: list[object] = []

    def verify_registry(_wheel: Path, _lock: Path):
        return {"registry_digest": "a" * 64}, loaded_registry

    monkeypatch.setattr(framework_smoke, "_verify_installed_registry", verify_registry)
    monkeypatch.setattr(
        framework_smoke,
        "_smoke_linux_context_projection",
        projection_calls.append,
    )
    monkeypatch.setattr(framework_smoke.sys, "platform", "linux")

    installed = framework_smoke.smoke(
        Path("wheel.whl"),
        Path("framework-lock.json"),
        mode="installed-registry",
    )
    assert installed["verification_mode"] == "installed-registry"
    assert installed["linux_context_projection"] == "not-run"
    assert projection_calls == []

    projected = framework_smoke.smoke(
        Path("wheel.whl"),
        Path("framework-lock.json"),
        mode="linux-context-projection",
    )
    assert projected["verification_mode"] == "linux-context-projection"
    assert projected["linux_context_projection"] == "passed"
    assert projection_calls == [loaded_registry]


def test_framework_wheel_smoke_cli_requires_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    framework_smoke = _load_framework_wheel_smoke_module()
    monkeypatch.setattr(
        framework_smoke.sys,
        "argv",
        [
            "smoke_evolution_framework_wheel.py",
            "--wheel",
            "wheel.whl",
            "--framework-lock",
            "framework-lock.json",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        framework_smoke.main()

    assert exc_info.value.code == 2


def test_candidate_cleanup_deletes_only_the_validated_immutable_release_id() -> None:
    workflow = Path(".github/workflows/openevo-desktop-candidate.yml").read_text(
        encoding="utf-8"
    )
    cleanup = workflow.split(
        "      - name: Delete an owned unverified draft", maxsplit=1
    )[1]

    assert "--json apiUrl,body," in cleanup
    assert cleanup.index("validate-draft") < cleanup.index("--release-id-output")
    assert cleanup.index("--release-id-output") < cleanup.index(
        "IFS= read -r release_id"
    )
    assert cleanup.index("IFS= read -r release_id") < cleanup.index(
        '"repos/${GITHUB_REPOSITORY}/releases/${release_id}"'
    )
    assert 'gh release delete "$RELEASE_TAG"' not in cleanup


def test_candidate_and_preview_publisher_pin_supply_chain_actions_and_rust() -> None:
    workflow_paths = (
        Path(".github/workflows/openevo-desktop-candidate.yml"),
        Path(".github/workflows/openevo-desktop-publish-preview.yml"),
    )
    action_pattern = re.compile(
        r"^\s*-?\s*uses:\s+[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@([0-9a-f]{40})\s+#\s+.+$",
        flags=re.MULTILINE,
    )
    for path in workflow_paths:
        text = path.read_text(encoding="utf-8")
        uses_lines = [line for line in text.splitlines() if "uses:" in line]
        assert uses_lines
        assert len(action_pattern.findall(text)) == len(uses_lines)
        assert "@v4" not in text
        assert "@v5" not in text
        assert "@v6" not in text
        assert "@stable" not in text

    candidate = workflow_paths[0].read_text(encoding="utf-8")
    tool = Path("scripts/ci/openevo_release_candidate.py").read_text(encoding="utf-8")
    with Path("pyproject.toml").open("rb") as stream:
        dev_dependencies = tomllib.load(stream)["dependency-groups"]["dev"]
    assert "toolchain: \"1.95.0\"" in candidate
    assert 'RUST_TOOLCHAIN_VERSION = "1.95.0"' in tool
    assert '"rust_toolchain": RUST_TOOLCHAIN_VERSION' in tool
    assert "uv run pip-audit" in candidate
    assert "uvx --from pip-audit" not in candidate
    assert "pip-audit==2.9.0" in dev_dependencies


def test_preview_publisher_is_numeric_id_visibility_only_and_fail_closed() -> None:
    workflow = Path(
        ".github/workflows/openevo-desktop-publish-preview.yml"
    ).read_text(encoding="utf-8")
    required_inputs = (
        "candidate_tag:",
        "expected_release_id:",
        "expected_source_sha:",
        "expected_release_candidate_manifest_sha256:",
        "expected_real_science_e2e_sha256:",
        "expected_real_science_e2e_signature_sha256:",
        "candidate_workflow_run_id:",
        "candidate_workflow_run_attempt:",
        "confirmation:",
    )
    for marker in required_inputs:
        assert marker in workflow

    assert "workflow_dispatch:" in workflow
    assert workflow.count("environment: openevo-preview-publication") == 2
    assert workflow.count("OPENEVO_REAL_SCIENCE_E2E_PUBLIC_KEY_SHA256") == 3
    assert "actions: read" in workflow
    assert workflow.count("contents: write") == 1
    assert workflow.count("ref: ${{ github.workflow_sha }}") == 2
    assert "run-id: ${{ inputs.candidate_workflow_run_id }}" in workflow
    assert "validate-candidate-run" in workflow
    assert '".github/workflows/openevo-desktop-candidate.yml"' in Path(
        "scripts/ci/openevo_release_candidate.py"
    ).read_text(encoding="utf-8")
    assert "preview-release-snapshot.json" in workflow
    assert "validate-preview-snapshot" in workflow
    assert workflow.count("write-preview-asset-plan") == 1
    assert workflow.count("snapshot-preview") == 1
    assert "postpublication-release-rest.json" in workflow
    assert workflow.count("releases/assets/${asset_id}") == 1
    assert workflow.count('test ! -e "$public_dir"') == 1
    assert workflow.count('method="PATCH"') == 1
    assert workflow.count("b'{\"draft\":false}'") == 1
    assert 'publication_mode = "already_public"' not in workflow
    assert '"publication_mode": "published_now"' in workflow
    assert 'if release.get("draft") is not True:' in workflow
    assert "Candidate was published outside the validated mutation" in workflow
    assert "assert_tag_target(must_exist=True)" in workflow
    assert "assert_tag_target(must_exist=False)" in workflow
    assert 'json.load(open(sys.argv[1]))["immutable"]' in workflow
    assert '\\\"immutable\\\"' not in workflow
    assert (
        '"repos/${GITHUB_REPOSITORY}/releases/${EXPECTED_RELEASE_ID}"'
        in workflow
    )
    assert "gh release edit" not in workflow
    assert "gh release upload" not in workflow
    assert "gh release delete" not in workflow
    assert "--method DELETE" not in workflow
    assert "/releases/tags/" not in workflow
    assert "immutable-releases" not in workflow
    assert workflow.count('"immutable"') >= 2

    read_only_job = workflow.split("  verify-preview:\n", maxsplit=1)[1].split(
        "\n  publish-preview:\n", maxsplit=1
    )[0]
    write_job = workflow.split("  publish-preview:\n", maxsplit=1)[1].split(
        "\n  verify-public-preview:\n", maxsplit=1
    )[0]
    post_job = workflow.split("  verify-public-preview:\n", maxsplit=1)[1]
    assert "contents: read" in read_only_job
    assert "contents: write" not in read_only_job
    assert "validate-preview-snapshot" in read_only_job
    assert "validate_desktop_real_science_e2e.py" in read_only_job
    assert "expected_real_science_e2e_sha256" in read_only_job
    assert "expected_real_science_e2e_signature_sha256" in read_only_job
    assert "desktop_real_science_e2e_attestation.py verify" in read_only_job
    assert "release-trust/desktop-real-science-e2e-v1.pub" in read_only_job
    assert 'sha256sum "$public_key"' in read_only_job
    assert '"$EXPECTED_SIGNER_PUBLIC_KEY_SHA256"' in read_only_job
    assert "merge-base --is-ancestor" in read_only_job
    assert "After candidate creation, stable may add only" in read_only_job
    assert "/releases?" not in read_only_job
    assert "/releases/${EXPECTED_RELEASE_ID}" not in read_only_job
    assert "releases/assets/${asset_id}" not in read_only_job
    assert '--candidate-manifest "$RUNNER_TEMP/candidate-verification/release-candidate.json"' in read_only_job
    assert '--candidate-app-bundle-smoke "$RUNNER_TEMP/candidate-verification/app-bundle-smoke.json"' in read_only_job
    assert "contents: write" in write_job
    assert 'with open_asset(asset["id"]) as response:' in write_job
    assert "actions/checkout@" not in write_job
    assert "scripts/ci/" not in write_job
    assert "data only" in write_job
    assert "verified-real-science-e2e" in write_job
    assert "sha256sum" in write_job
    assert "EXPECTED_REAL_SCIENCE_E2E_SIGNATURE_SHA256" in write_job
    assert '"schema_version": 2' in write_job
    assert '"policy_commit": policy_sha' in write_job
    assert '"candidate_manifest_sha256": manifest_sha256' in write_job
    assert '"signer_public_key_sha256": signer_public_key_sha256' in write_job
    assert '"signature_sha256": evidence_signature_sha256' in write_job
    assert "retention-days: 90" in write_job
    assert 'request_headers["Content-Type"] = "application/json"' in write_job
    assert "class RejectRedirects(HTTPRedirectHandler)" in write_job
    assert '"Authorization"' not in write_job.split(
        'headers={"User-Agent": "openevo-preview-fixed-publisher"}',
        maxsplit=1,
    )[1]
    assert "needs: verify-preview" in write_job
    assert "contents: read" in post_job
    assert "contents: write" not in post_job
    assert "needs: publish-preview" in post_job

    parsed = yaml.safe_load(workflow)
    verify_steps = parsed["jobs"]["verify-preview"]["steps"]
    publication_download = next(
        step
        for step in verify_steps
        if step.get("name") == "Download candidate-owned publication verification inputs"
    )
    assert publication_download == {
        "name": "Download candidate-owned publication verification inputs",
        "uses": "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093",
        "with": {
            "name": (
                "openevo-desktop-publication-inputs-${{ inputs.expected_source_sha }}-"
                "${{ inputs.candidate_workflow_run_id }}-"
                "${{ inputs.candidate_workflow_run_attempt }}"
            ),
            "path": "${{ runner.temp }}/candidate-publication-inputs",
            "github-token": "${{ github.token }}",
            "run-id": "${{ inputs.candidate_workflow_run_id }}",
        },
    }
    manifest_binding = next(
        step
        for step in verify_steps
        if step.get("name")
        == "Bind the exact candidate manifest from the validated workflow run"
    )["run"]
    assert 'find "$inputs" -mindepth 1 -maxdepth 1' in manifest_binding
    assert 'find "$inputs" -maxdepth 1 -type f' in manifest_binding
    assert 'find "$inputs" -type l -print -quit' in manifest_binding
    assert 'asset.get("size") == source.stat().st_size' in manifest_binding
    assert 'sha256sum "$source"' in manifest_binding
    assert 'install -m 0600 "$source" "$target"' in manifest_binding
    smoke_binding = next(
        step
        for step in verify_steps
        if step.get("name")
        == "Bind the exact candidate native-sidecar smoke from the validated workflow run"
    )["run"]
    assert 'asset.get("sha256") == role.get("sha256")' in smoke_binding
    assert 'asset.get("size") == role.get("byte_size")' in smoke_binding
    assert 'sha256sum "$source"' in smoke_binding
    assert 'stat -c \'%s\' "$source"' in smoke_binding
    assert 'install -m 0600 "$source" "$target"' in smoke_binding
    publish_steps = parsed["jobs"]["publish-preview"]["steps"]
    fixed_step = next(
        step
        for step in publish_steps
        if step.get("name")
        == "Revalidate immutable candidate data and publish by numeric ID"
    )
    prefix = "python - <<'PY'\n"
    assert fixed_step["run"].startswith(prefix)
    fixed_source, suffix = fixed_step["run"][len(prefix) :].rsplit("\nPY", maxsplit=1)
    assert suffix.strip() == ""
    compile(fixed_source, "<fixed-preview-publisher>", "exec")

    read_only_validate = workflow.index(
        "Validate the candidate-owned draft verification snapshot"
    )
    publish = workflow.index("Revalidate immutable candidate data and publish by numeric ID")
    write_tag_validator = write_job.index("def assert_tag_target")
    write_release_read = write_job.index(
        'release = api_json(f"/releases/{release_id}")'
    )
    write_asset_hash = write_job.index("with open_asset(asset[\"id\"]) as response:")
    write_patch = write_job.index('body=b\'{"draft":false}\'')
    draft_gate = write_job.index('if release.get("draft") is not True:')
    postdownload = workflow.index(
        "Re-read and redownload the immutable public release"
    )
    postvalidate = workflow.index(
        "Verify public metadata, assets, body, and exact tag target"
    )
    assert read_only_validate < publish < postdownload < postvalidate
    assert (
        write_tag_validator
        < write_release_read
        < draft_gate
        < write_asset_hash
        < write_patch
    )
    assert "validate-published-tag" in workflow[postvalidate:]


def test_candidate_public_playwright_evidence_excludes_preview_simulator() -> None:
    workflow = Path(".github/workflows/openevo-desktop-candidate.yml").read_text(
        encoding="utf-8"
    )
    tool = Path("scripts/ci/openevo_release_candidate.py").read_text(encoding="utf-8")
    preview = workflow.split(
        "      - name: Gate non-release simulator preview\n", maxsplit=1
    )[1].split(
        "      - name: Produce packaged release-composition Playwright report\n",
        maxsplit=1,
    )[0]
    packaged = workflow.split(
        "      - name: Produce packaged release-composition Playwright report\n",
        maxsplit=1,
    )[1].split("      - uses: actions/setup-python", maxsplit=1)[0]

    assert "test:product-browser:preview" in preview
    assert "PLAYWRIGHT_BLOB_OUTPUT_FILE" not in preview
    assert "merge-reports" not in preview
    assert "test:product-browser:preview" not in packaged
    assert "release-packaged.zip" in packaged
    assert "merge-reports" in packaged
    for marker in (
        '"release-packaged-1440"',
        '"release-packaged-1024"',
        '"release-packaged-760"',
        '"simulator": False',
        '"provider_kind": "desktop_sidecar"',
        '"composition": "packaged_web"',
    ):
        assert marker in tool
    for forbidden in (
        '"desktop-1440"',
        '"desktop-1024"',
        '"minimum-760"',
        '"release-readonly-source"',
        '"release-readonly-packaged"',
    ):
        assert forbidden not in tool


def test_desktop_visual_gates_use_one_bounded_cross_runner_tolerance() -> None:
    preview = Path("desktop/playwright.config.ts").read_text(encoding="utf-8")
    packaged = Path("desktop/playwright.release-readonly.config.ts").read_text(
        encoding="utf-8"
    )

    for config in (preview, packaged):
        assert config.count("maxDiffPixelRatio: 0.035") == 1
        assert config.count('animations: "disabled"') == 1
        assert "maxDiffPixelRatio: 0.04" not in config


def test_packaged_release_visuals_use_fixed_viewports_not_full_page_height() -> None:
    release_test = Path(
        "desktop/tests/product-browser/release-readonly.pw.ts"
    ).read_text(encoding="utf-8")

    assert "fullPage: true" not in release_test
    assert "scrollIntoViewIfNeeded" in release_test
    assert 'toHaveScreenshot("release-packaged-research.png")' in release_test
    assert 'toHaveScreenshot("release-packaged-evolution.png")' in release_test


def test_release_remediation_keeps_daemon_lifecycle_inside_desktop() -> None:
    sources = [
        Path("src/openevo/backend/contracts/v1/provider.py"),
        Path("desktop/sidecar/core_bridge_adapters_v1.py"),
        Path("desktop/sidecar/release_provider.py"),
        Path("desktop/src/product/DesktopProductApp.tsx"),
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in sources)

    for forbidden in (
        "Install the supported Codex CLI as the current remote SSH user",
        "Sign in with Codex CLI as the current remote SSH user",
        "Restart OpenEvo Daemon",
        "Restart or update OpenEvo Daemon",
        "Reconnect OpenEvo Daemon",
        "Stop the active Daemon",
    ):
        assert forbidden not in text

    assert "server administrator" in text
    assert "retry activation in OpenEvo Desktop" in text
    assert "Desktop will manage the compatible " in text
    assert '"Daemon transition."' in text


def test_python_runtime_dependencies_pin_security_fixed_minimums() -> None:
    with Path("pyproject.toml").open("rb") as stream:
        dependencies = tomllib.load(stream)["project"]["dependencies"]

    assert "click>=8.3.3" in dependencies
    assert "idna>=3.15" in dependencies
    assert "starlette>=1.3.1" in dependencies


def test_desktop_package_defines_tauri_desktop_scripts_and_cli_dependency() -> None:
    package = json.loads(Path("desktop/package.json").read_text(encoding="utf-8"))

    assert package["name"] == "openevo-desktop"
    assert package["scripts"]["tauri:dev"] == "tauri dev"
    assert package["scripts"]["tauri:build"] == "tauri build"
    assert package["scripts"]["build:sidecar"] == "python packaging/build_sidecar.py"
    assert package["scripts"]["build:desktop"] == ("npm run build:sidecar && npm run tauri:build")
    assert "@tauri-apps/cli" in package["devDependencies"]


def test_desktop_tailwind_sources_are_explicit_and_exclude_packaged_web() -> None:
    styles = Path("desktop/src/styles.css").read_text(encoding="utf-8")

    assert '@import "tailwindcss" source(none);' in styles
    assert '@source "../index.html";' in styles
    assert '@source "./**/*.{ts,tsx}";' in styles
    assert "packaging/web" not in styles


def test_release_execution_mode_authority_has_no_renderer_fixture_fallback() -> None:
    release_provider = Path("desktop/src/product/releaseProvider.ts").read_text(encoding="utf-8")
    local_provider = Path("desktop/src/product/localApiProvider.ts").read_text(encoding="utf-8")
    product_app = Path("desktop/src/product/DesktopProductApp.tsx").read_text(encoding="utf-8")
    release_capabilities = Path("desktop/sidecar/release_capabilities.py").read_text(
        encoding="utf-8"
    )

    assert "fixtureProvider" not in release_provider
    assert "fixtureProvider" not in local_provider
    assert "self_deployed_release_unavailable" not in product_app
    assert "RELEASE_EXECUTION_MODE_CAPABILITIES" not in product_app
    assert "self_deployed_release_unavailable" in release_capabilities


def test_release_docs_and_notes_match_execution_mode_and_native_storage_authority() -> None:
    by_mode = {
        capability.mode: capability
        for capability in RELEASE_EXECUTION_MODE_CAPABILITIES_V1.modes
    }
    readme = Path("README.md").read_text(encoding="utf-8")
    normalized_readme = " ".join(readme.split())
    release_process = Path("docs/maintainer/release-process.md").read_text(
        encoding="utf-8"
    )
    normalized_release_process = " ".join(release_process.split())
    notes = _load_release_candidate_module().render_candidate_release_notes(
        source_commit="a" * 40,
        version="0.1.0",
        architecture="aarch64",
    )

    assert by_mode["codex_subscription_transcript"].support_state == "supported"
    assert by_mode["self-deployed"].support_state == "unavailable"
    assert "Codex subscription transcript mode: packaged and declared in this Preview." in notes
    assert "Candidate-bound real Codex Subscription science E2E: required before" in notes
    assert "A public Preview carrying these notes has passed the separate signed publication gate" in notes
    assert "Remote Core" not in notes
    assert "Self-Deployed Reference mode: unavailable in this Preview." in notes
    assert (
        "Codex subscription with transcript capture"
    ) in normalized_readme
    assert "Self-deployed inference" in normalized_readme
    assert "Do not install a Python package" in normalized_readme
    assert "persistent WebView storage" not in readme
    assert (
        "Tauri native host app-data directory for `org.openevo.desktop`"
        in normalized_release_process
    )


def test_tauri_macos_config_declares_unreleased_dmg_target() -> None:
    from desktop.sidecar.contracts.v1 import DESKTOP_OPENAPI_SHA256

    config = json.loads(Path("desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    cargo = Path("desktop/src-tauri/Cargo.toml").read_text(encoding="utf-8")
    cargo_config = tomllib.loads(cargo)
    project_config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    cargo_metadata = json.loads(
        subprocess.run(
            [
                "cargo",
                "metadata",
                "--locked",
                "--no-deps",
                "--format-version",
                "1",
                "--manifest-path",
                "desktop/src-tauri/Cargo.toml",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    candidate_tool = _load_release_candidate_module()
    main = Path("desktop/src-tauri/src/main.rs").read_text(encoding="utf-8")
    poisoned_environment_test = main[
        main.index(
            "fn verified_packaged_launch_rejects_poisoned_pyinstaller_environment"
        ) : main.index("fn packaged_launch_owns_the_native_executable_environment")
    ]
    process_group_termination = main[
        main.index("fn terminate_process_group_with") : main.index(
            "fn signal_verified_process_group"
        )
    ]
    verified_group_signal = main[
        main.index("fn signal_verified_process_group") : main.index(
            "fn wait_for_process_group_exit_with"
        )
    ]
    workflow = Path(".github/workflows/openevo-desktop.yml").read_text(encoding="utf-8")
    macos_workflow = workflow[workflow.index("  macos-native-launch-smoke:") :]
    release_test_command = "cargo test --locked --release -- --test-threads=1"
    sidecar_builder = Path("desktop/packaging/build_sidecar.py")
    sidecar_entry = Path("desktop/packaging/sidecar_entry.py")
    release_contract = json.loads(Path("desktop/release-contract.json").read_text(encoding="utf-8"))
    gitignore = Path(".gitignore").read_text(encoding="utf-8")

    assert config["productName"] == "OpenEvo Desktop"
    assert len(cargo_metadata["packages"]) == 1
    cargo_package = cargo_metadata["packages"][0]
    cargo_bin_targets = [
        target for target in cargo_package["targets"] if target["kind"] == ["bin"]
    ]
    assert len(cargo_bin_targets) == 1
    cargo_main_binary = cargo_bin_targets[0]["name"]
    assert cargo_package["default_run"] in (None, cargo_main_binary)
    effective_tauri_binary = config.get("mainBinaryName") or cargo_main_binary
    assert effective_tauri_binary == candidate_tool.TAURI_EXECUTABLE_NAME == "openevo-desktop"
    assert cargo_config["package"]["name"] == "openevo-desktop"
    assert (
        config["version"]
        == cargo_config["package"]["version"]
        == project_config["project"]["version"]
    )
    assert config["identifier"] == "org.openevo.desktop"
    assert config["app"]["windows"] == [
        {
            "title": "OpenEvo Desktop",
            "width": 1280,
            "height": 860,
            "minWidth": 760,
            "minHeight": 600,
        }
    ]
    assert config["build"]["beforeBuildCommand"] == "npm run build:openevo"
    assert config["build"]["frontendDist"] == "../dist"
    assert config["bundle"]["active"] is True
    assert config["bundle"]["targets"] == ["dmg"]
    assert config["bundle"]["externalBin"] == ["binaries/openevo-desktop-sidecar"]
    assert config["bundle"]["macOS"]["minimumSystemVersion"] == "12.0"
    assert config["bundle"]["macOS"]["infoPlist"] == "Info.plist"
    assert config["bundle"]["macOS"]["signingIdentity"] == "-"
    with Path("desktop/src-tauri/Info.plist").open("rb") as stream:
        info_plist = plistlib.load(stream)
    assert info_plist == {
        "NSAppTransportSecurity": {
            "NSExceptionDomains": {
                "127.0.0.1": {
                    "NSExceptionAllowsInsecureHTTPLoads": True,
                }
            },
        }
    }
    assert sidecar_builder.is_file()
    assert sidecar_entry.is_file()
    assert "desktop/src-tauri/binaries/openevo-desktop-sidecar-*" in gitignore
    sidecar_builder_text = sidecar_builder.read_text(encoding="utf-8")
    sidecar_entry_text = sidecar_entry.read_text(encoding="utf-8")
    assert "PyInstaller" in sidecar_builder_text
    assert "_build_core_wheel" in sidecar_builder_text
    assert "_validate_embedded_core_wheel" in sidecar_builder_text
    assert "--add-data" in sidecar_builder_text
    assert "desktop/packaging/web" in sidecar_builder_text
    assert "sidecar-build-metadata.json" in sidecar_builder_text
    assert '"rev-parse", "--verify", "HEAD^{commit}"' in sidecar_builder_text
    assert "_write_sidecar_build_metadata" in sidecar_builder_text
    assert "desktop.server.launcher" in sidecar_entry_text
    assert "_load_packaged_build_metadata" in sidecar_entry_text
    assert 'serde = { version = "1", features = ["derive"] }' in cargo
    assert "tauri = " in cargo
    assert "struct ManagedSidecar" in main
    assert "struct DesktopHostState" in main
    assert "fn allocate_sidecar_listener()" in main
    assert "fn prepare_packaged_sidecar(" in main
    assert "libc::O_NOFOLLOW" in main
    assert "acl_get_fd_np" in main
    assert '#[cfg(target_os = "linux")]\nfn fd_execution_path()' in main
    assert '#[cfg(all(target_os = "macos", test))]\nfn fd_execution_path()' in main
    assert "fn test_temp_root(temp: &TempDir) -> PathBuf" in main
    assert "fs::canonicalize(temp.path()).unwrap()" in main
    assert 'test_temp_root(temp).join("app-data")' in main
    assert "let temp_root = test_temp_root(&temp);" in main
    assert "for _attempt in 0..20" in main
    assert "exercise_blocked_exec_handoff_is_owned_and_stop_remains_bounded" in main
    assert 'SidecarFixture::from_existing(Path::new("/usr/bin/true"))' in main
    assert 'Path::new("/bin/true")' not in main
    assert "struct SpawnHandoff" in main
    assert "run_parent_liveness_watchdog" in main
    assert "libc::WNOWAIT" in main
    assert "GroupSignalAuthority::Finalizing" in main
    assert main.count("const DESKTOP_LOCAL_API_OPENAPI_SHA256") == 1
    assert release_contract == RELEASE_CONTRACT
    assert release_contract["allowed_provider_kinds"] == ["desktop_sidecar"]
    assert RELEASE_OPENAPI_SHA256 == DESKTOP_OPENAPI_SHA256
    assert f'    "{RELEASE_OPENAPI_SHA256}";' in main
    assert "fn macos_proc_listpgrppids_call(" in main
    assert process_group_termination.count(
        "Ok(VerifiedGroupSignalOutcome::PermissionDenied)"
    ) == 2
    assert "terminated && !control_failed" in process_group_termination
    assert "killed && !control_failed" in process_group_termination
    assert verified_group_signal.index("control.leader_exited(child)?") < (
        verified_group_signal.index("control.signal_group(process_group, signal)")
    )
    assert 'error.raw_os_error() == Some(libc::EPERM)' in verified_group_signal
    assert "permission_denied_group_signal_requires_and_accepts_empty_group_proof" in main
    assert "permission_denied_group_signal_cannot_finalize_a_live_leader" in main
    assert "permission_denied_leader_inspection_is_not_a_signal_outcome" in main
    assert "permission_denied_group_signal_retains_ownership_with_a_reported_descendant" in main
    assert "permission_denied_group_signal_does_not_override_inspection_failure" in main
    assert "program: release_execution_path(&private_launch_dir)" in poisoned_environment_test
    assert "program: fd_execution_path()" not in poisoned_environment_test
    assert "fn sanitize_pyinstaller_launch_environment(" in main
    assert 'command.env(PYINSTALLER_RESET_ENVIRONMENT, "1")' in main
    assert "fn monitor_running_sidecar(" in main
    assert "launch_gate" not in main
    assert "emergency_process_group" not in main
    assert "fn terminate_process_group(" in main
    assert "openevo-desktop-sidecar" in main
    assert "check_sidecar_health" in main
    assert "wait_for_sidecar_ready" in main
    assert "fn host_status(" in main
    assert "fn start_sidecar(" in main
    assert "fn stop_sidecar(" in main
    assert "fn create_ssh_tunnel(" not in main
    assert "fn keychain_reference(" not in main
    assert "fn app_logs(" not in main
    assert "desktop.server.launcher" in main
    assert "Command::new" in main
    assert "Stdio::null()" in main
    assert "tauri::generate_handler!" in main
    assert "tauri::RunEvent::ExitRequested" in main
    assert "cargo check --locked --release --all-targets" in workflow
    assert "cargo clippy --locked --release --all-targets -- -D warnings" in workflow
    assert release_test_command in macos_workflow
    assert macos_workflow.index("npm run build:sidecar") < macos_workflow.index(
        release_test_command
    ) < macos_workflow.index("tests::macos_release_spawns_from_the_populated_private_path")
    assert workflow.index("npm run build:sidecar") < workflow.index(
        "tests::packaged_external_bin_native_launch_smoke"
    )
    assert "macOS FD-bound packaged sidecar launch smoke" in workflow
    assert "tests::macos_release_spawns_from_the_populated_private_path" in workflow
    assert "if: always()" in workflow
    assert 'rm -f "$OPENEVO_PACKAGED_SIDECAR_PATH"' in workflow
    assert "cargo build --locked --release" in workflow
    assert "release binary contains the debug source launcher fallback" in workflow
    assert "release binary contains debug sidecar override code" in workflow


def test_sidecar_bootloader_separates_verified_archive_fd_from_macos_exec_path(
    tmp_path: Path,
) -> None:
    builder = Path("desktop/packaging/build_sidecar.py").read_text(encoding="utf-8")

    assert 'NATIVE_EXECUTABLE_FD_ENV = "OPENEVO_NATIVE_EXECUTABLE_FD"' in builder
    assert 'NATIVE_EXECUTABLE_PATH_ENV = "OPENEVO_NATIVE_EXECUTABLE_PATH"' in builder
    assert "/dev/fd/{NATIVE_EXECUTABLE_FD}" in builder
    assert "pyi_ctx->archive = pyi_archive_open(openevo_archive_path);" in builder
    assert "snprintf(pyi_ctx->executable_filename, PYI_PATH_MAX" in builder
    assert "realpath(openevo_native_path, openevo_resolved_path)" in builder
    assert "fstat({NATIVE_EXECUTABLE_FD}, &openevo_fd_stat)" in builder
    assert "lstat(openevo_native_path, &openevo_path_stat)" in builder
    assert "openevo_fd_stat.st_ino != openevo_path_stat.st_ino" in builder
    assert "openevo_path_stat.st_nlink != 1" in builder
    assert "openevo_path_stat.st_uid != geteuid()" in builder
    assert "NATIVE_EXECUTABLE_PATH_ENV.encode" in builder

    path = Path("desktop/packaging/build_sidecar.py").resolve()
    spec = importlib.util.spec_from_file_location("openevo_sidecar_builder", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "bootloader/src/pyi_main.c"
    source.parent.mkdir(parents=True)
    source.write_text(
        module._BOOTLOADER_MACOS_INCLUDE_NEEDLE
        + module._BOOTLOADER_RESOLVER_NEEDLE
        + module._BOOTLOADER_ARCHIVE_NEEDLE
        + module._BOOTLOADER_RESTART_NEEDLE
        + module._BOOTLOADER_CHILD_MAIN_NEEDLE,
        encoding="utf-8",
    )
    utils_source = tmp_path / "bootloader/src/pyi_utils_posix.c"
    utils_source.write_text(
        module._BOOTLOADER_POSIX_INCLUDE_NEEDLE
        + module._BOOTLOADER_NATIVE_HANDOFF_NEEDLE
        + module._BOOTLOADER_CHILD_EXEC_NEEDLE,
        encoding="utf-8",
    )
    utils_header = tmp_path / "bootloader/src/pyi_utils.h"
    utils_header.write_text(module._BOOTLOADER_UTILS_HEADER_NEEDLE, encoding="utf-8")
    wscript = tmp_path / "bootloader/wscript"
    wscript.write_text(
        module._BOOTLOADER_DARWIN_LIB_NEEDLE
        + module._BOOTLOADER_PROGRAM_LIBS_NEEDLE,
        encoding="utf-8",
    )

    module._patch_fd_bound_bootloader(tmp_path)

    patched = source.read_text(encoding="utf-8")
    patched_utils = utils_source.read_text(encoding="utf-8")
    patched_wscript = wscript.read_text(encoding="utf-8")
    assert patched.count('getenv("OPENEVO_NATIVE_EXECUTABLE_PATH")') == 1
    assert patched.count('getenv("OPENEVO_NATIVE_LISTENER_FD")') == 1
    assert patched.count("pyi_archive_open(openevo_archive_path)") == 1
    assert patched.count("fstat(4, &openevo_fd_stat)") == 1
    assert patched.count("lstat(openevo_native_path, &openevo_path_stat)") == 1
    assert patched.count("lstat(openevo_resolved_path, &openevo_resolved_stat)") == 1
    assert "SO_ACCEPTCONN" in patched_utils
    assert "proc_pidfdinfo" in patched_utils
    assert "uselib_store='PROC'" in patched_wscript
    assert "pyi_utils_openevo_native_handoff_restore()" in patched_utils


def test_pre_external_beta_pypi_publish_workflow_is_disabled() -> None:
    workflow = Path(".github/workflows/openevo-publish-pypi.yml")

    text = workflow.read_text(encoding="utf-8")

    assert "contents: read" in text
    assert "name: PyPI publishing disabled" in text
    assert "PyPI is not an External Beta release surface" in text
    assert "Any future PyPI release requires a separate product" in text
    assert "completing the External" in text
    assert "Beta gates must not enable publication here" in text
    assert "release:" not in text
    assert "types: [published]" not in text
    assert "id-token: write" not in text
    assert "name: pypi" not in text
    assert "python -m build --wheel" not in text
    assert "twine check --strict dist/*.whl" not in text
    assert "pypa/gh-action-pypi-publish@release/v1" not in text
    assert "password:" not in text.casefold()
    assert "api-token" not in text.casefold()


def test_disabled_release_artifact_workflow_does_not_upload_checksums_or_notes() -> None:
    workflow = Path(".github/workflows/openevo-release-artifact.yml")

    text = workflow.read_text(encoding="utf-8")

    assert "pre-External-Beta release artifact workflow is disabled" in text
    assert "name: Write release notes" not in text
    assert "actions/upload-artifact@v4" not in text
    assert "openevo-release-notes" not in text
    assert "release-artifacts/openevo-wheel/*" not in text
    assert "release-artifacts/openevo-desktop-dmg/*" not in text


def test_desktop_science_release_doc_matches_remote_lifecycle_state() -> None:
    doc = Path("docs/architecture/openevo-desktop-science-foundation.md")

    text = doc.read_text(encoding="utf-8")

    assert "a remote backend implementation" not in text
    assert "sidecar process supervision" not in text
    assert "remote workspace preparation" in text
    assert "`POST /openevo-api/desktop/bootstrap`" in text
    assert "`POST /openevo-api/desktop/services`" in text
    assert "`POST /openevo-api/desktop/run`" in text
    assert "GET /openevo-api/backend/runs/{run_id}/timeline" in text
    assert "GET /openevo-api/backend/runs/{run_id}/artifacts" in text
    assert "GET /openevo-api/backend/artifacts/{artifact_id}/content" in text


def test_maintainer_docs_own_release_checklist_and_frontend_audit_gate() -> None:
    contributing = Path("CONTRIBUTING.md").read_text(encoding="utf-8")
    release_process = Path("docs/maintainer/release-process.md").read_text(
        encoding="utf-8"
    )

    assert "npm ci" in contributing
    assert "npm audit --audit-level=high" in contributing
    assert contributing.index("npm ci") < contributing.index(
        "npm audit --audit-level=high"
    )
    assert contributing.index("npm audit --audit-level=high") < contributing.index(
        "npm test -- --run"
    )
    assert "npm run typecheck" in contributing
    assert "OpenEvo Desktop unsigned draft prerelease" in release_process
    assert "PyPI" in release_process
    assert "unsigned, non-gating" in release_process
    assert "docs/maintainer/productization/spec.md" in release_process
    assert "scripts/ci/smoke_openevo_desktop_wheel.py" not in release_process
    assert ".openevo-wheel-smoke/bin/openevo-backend run --help" not in release_process
    assert "PyPI trusted publishing" not in contributing
    assert "pypa/gh-action-pypi-publish@release/v1" not in contributing


def _write_wheel(
    path: Path,
    *,
    metadata: str = GOOD_METADATA,
    entry_points: str = GOOD_ENTRY_POINTS,
    include_nested_remote_wheel: bool = True,
    nested_remote_wheel_metadata: str = GOOD_METADATA,
    nested_remote_wheel_extra_files: dict[str, str] | None = None,
    extra_files: dict[str, str] | None = None,
) -> Path:
    dist_info = "openevo-0.1.0.dist-info"
    with ZipFile(path, "w") as wheel:
        wheel.writestr(f"{dist_info}/METADATA", metadata)
        wheel.writestr(f"{dist_info}/entry_points.txt", entry_points)
        if include_nested_remote_wheel:
            wheel.writestr(
                "openevo/wheels/openevo-0.1.0-py3-none-any.whl",
                _nested_wheel_bytes(
                    metadata=nested_remote_wheel_metadata,
                    extra_files=nested_remote_wheel_extra_files,
                ),
            )
        for name, content in (extra_files or {}).items():
            wheel.writestr(name, content)
    return path


def _nested_wheel_bytes(
    *,
    metadata: str,
    extra_files: dict[str, str] | None = None,
) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w") as wheel:
        wheel.writestr("openevo-0.1.0.dist-info/METADATA", metadata)
        for name, content in (extra_files or {}).items():
            wheel.writestr(name, content)
    return buffer.getvalue()


def _write_checksum(path: Path) -> Path:
    checksum_path = path.with_name(f"{path.name}.sha256")
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    checksum_path.write_text(f"{checksum}  {path.name}\n", encoding="utf-8")
    return checksum_path


def _write_release_notes(directory: Path) -> Path:
    notes = directory / "release-notes.md"
    notes.write_text("# OpenEvo 0.1.0\n\nRelease smoke notes.\n", encoding="utf-8")
    return notes


def _write_fake_tauri_release_smoke(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import json",
                "import os",
                "from pathlib import Path",
                "import signal",
                "import subprocess",
                "import sys",
                "import time",
                "",
                (
                    "child = subprocess.Popen("
                    "[sys.executable, '-c', 'import time; time.sleep(60)'])"
                ),
                "def shutdown(_signal, _frame):",
                "    if child.poll() is None:",
                "        child.terminate()",
                "    child.wait(timeout=5)",
                "    raise SystemExit(0)",
                "signal.signal(signal.SIGTERM, shutdown)",
                "evidence = {",
                "    'schema_version': 3,",
                "    'nonce': os.environ['OPENEVO_RELEASE_SMOKE_NONCE'],",
                "    'native_executable': 'OpenEvo Desktop',",
                "    'bundled_external_bin': 'openevo-desktop-sidecar',",
                "    'renderer_ready': True,",
                "    'sidecar_ready': True,",
                "    'bundled_external_bin_resolved': True,",
                "    'native_listener_fd_handoff': True,",
                "    'native_executable_fd_handoff': True,",
                "    'process_group_cleanup': True,",
                "    'mach_o': {",
                "        'native_executable': {",
                "            'file_output': 'Mach-O 64-bit executable arm64',",
                "            'slices': ['arm64'],",
                "        },",
                "        'bundled_external_bin': {",
                "            'file_output': 'Mach-O 64-bit executable arm64',",
                "            'slices': ['arm64'],",
                "        },",
                "    },",
                "}",
                "Path(os.environ['OPENEVO_RELEASE_SMOKE_EVIDENCE_PATH']).write_text(",
                "    json.dumps(evidence, sort_keys=True) + '\\n', encoding='utf-8'",
                ")",
                "time.sleep(60)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _release_version_payload() -> dict[str, object]:
    return {
        "schema_version": "1",
        "api_name": "openevo-desktop-local-api",
        "preferred_major": 1,
        "supported_majors": [1],
        "provider_kind": "desktop_sidecar",
        "build_channel": "release",
        "openapi_sha256": RELEASE_OPENAPI_SHA256,
        "build_version": "0.1.0",
        "source_commit": "89baeb26",
        "feature_flags": RELEASE_FEATURE_FLAGS,
    }


def _desktop_state_payload() -> dict[str, object]:
    return {
        "schema_version": "1",
        "observed_at": "2026-07-15T00:00:00Z",
        "contract": {
            "selected_major": 1,
            "desktop_openapi_sha256": RELEASE_OPENAPI_SHA256,
            "core_openapi_sha256": None,
            "compatible": True,
        },
        "execution_mode_capabilities": (
            RELEASE_EXECUTION_MODE_CAPABILITIES_V1.model_dump(mode="json")
        ),
        "core": {
            "state": "disconnected",
            "profile_id": None,
            "active_tunnel": False,
            "operation_id": None,
            "host_key_review": None,
            "core": None,
            "failure": None,
        },
        "active_project": None,
        "pending_operation_ids": [],
    }


def _write_fake_sidecar(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "from http.server import BaseHTTPRequestHandler, HTTPServer",
                "import argparse",
                "import hashlib",
                "import hmac",
                "import json",
                "import socket",
                "import sys",
                "",
                "frame = json.loads(sys.stdin.readline())",
                "assert sys.stdin.read(1) == ''",
                "session_token = frame['session_token']",
                "readiness_key = bytes.fromhex(frame['readiness_key'])",
                "instance_id = frame['instance_id']",
                "protocol = frame['protocol']",
                "",
                "class Handler(BaseHTTPRequestHandler):",
                "    def do_GET(self):",
                "        if self.path == '/health':",
                "            challenge = self.headers.get('X-OpenEvo-Native-Challenge')",
                "            if challenge is None:",
                "                self.send_response(403)",
                "                self.end_headers()",
                "                return",
                "            domain = f'{protocol}\\0{instance_id}\\0{challenge}'.encode('ascii')",
                "            proof = hmac.new(readiness_key, domain, hashlib.sha256).hexdigest()",
                "            body = json.dumps({",
                "                'service': 'openevo-sidecar',",
                "                'status': 'ok',",
                "                'protocol': protocol,",
                "                'instance_id': instance_id,",
                "                'instance_proof': proof,",
                "            }).encode()",
                "            self.send_response(200)",
                "            self.send_header('Content-Type', 'application/json')",
                "            self.send_header('Content-Length', str(len(body)))",
                "            self.end_headers()",
                "            self.wfile.write(body)",
                "            return",
                "        if self.path == '/version':",
                f"            body = json.dumps({_release_version_payload()!r}).encode()",
                "            self.send_response(200)",
                "            self.send_header('Content-Type', 'application/json')",
                "            self.send_header('Content-Length', str(len(body)))",
                "            self.end_headers()",
                "            self.wfile.write(body)",
                "            return",
                "        if self.path == '/desktop/v1/state':",
                "            if self.headers.get('X-OpenEvo-Desktop-Session') != session_token:",
                "                self.send_response(401)",
                "                self.end_headers()",
                "                return",
                f"            body = json.dumps({_desktop_state_payload()!r}).encode()",
                "            self.send_response(200)",
                "            self.send_header('Content-Type', 'application/json')",
                "            self.send_header('Content-Length', str(len(body)))",
                "            self.end_headers()",
                "            self.wfile.write(body)",
                "            return",
                "        if self.path == '/openevo-native/session':",
                "            status = 204 if self.headers.get('X-OpenEvo-Desktop-Session') == session_token else 403",
                "            self.send_response(status)",
                "            self.end_headers()",
                "            return",
                "        if self.path == '/openevo':",
                "            body = b'<script src=\"/assets/index.js\"></script>'",
                "            self.send_response(200)",
                "            self.send_header('Content-Type', 'text/html')",
                "            self.send_header('Content-Length', str(len(body)))",
                "            self.end_headers()",
                "            self.wfile.write(body)",
                "            return",
                "        if self.path == '/assets/index.js':",
                "            body = b'console.log(\"openevo\")'",
                "            self.send_response(200)",
                "            self.send_header('Content-Type', 'text/javascript')",
                "            self.send_header('Content-Length', str(len(body)))",
                "            self.end_headers()",
                "            self.wfile.write(body)",
                "            return",
                "        self.send_response(404)",
                "        self.end_headers()",
                "",
                "    def log_message(self, format, *args):",
                "        return",
                "",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('--host', default='127.0.0.1')",
                "parser.add_argument('--desktop-config-root')",
                "parser.add_argument('--release-assets-root', required=True)",
                "parser.add_argument('--listener-fd', type=int, required=True)",
                "parser.add_argument('--native-instance-stdin', action='store_true', required=True)",
                "args = parser.parse_args()",
                "server = HTTPServer((args.host, 0), Handler, bind_and_activate=False)",
                "server.socket = socket.socket(fileno=args.listener_fd)",
                "server.server_address = server.socket.getsockname()",
                "server.server_name = args.host",
                "server.server_port = server.server_address[1]",
                "server.serve_forever()",
                "",
            ]
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)
