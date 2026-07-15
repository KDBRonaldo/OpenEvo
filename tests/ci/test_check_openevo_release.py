from __future__ import annotations

import importlib.util
import json
import hashlib
from io import BytesIO
from pathlib import Path
import plistlib
import subprocess
from types import SimpleNamespace
import tomllib
from zipfile import ZipFile

import pytest

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
    wheel = tmp_path / "openevo-0.1.0-py3-none-any.whl"
    wheel.write_bytes(_nested_wheel_bytes(metadata=GOOD_METADATA))
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
    evidence = smoke.smoke_bundle(
        tmp_path,
        timeout_seconds=5,
        evidence_out=evidence_path,
    )

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


def test_bundle_smoke_rejects_sidecar_only_bundle(tmp_path: Path) -> None:
    smoke = _load_bundle_smoke_module()
    sidecar = tmp_path / "OpenEvo Desktop.app" / "Contents" / "MacOS" / "openevo-desktop-sidecar"
    _write_fake_sidecar(sidecar)

    with pytest.raises(smoke.SmokeFailure, match="Info.plist"):
        smoke.smoke_bundle(tmp_path, timeout_seconds=1)


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

    result = checker.main(["--wheel", str(wheel)])

    assert result == 1
    assert (
        "Release artifacts must include an exact OpenEvo wheel for remote install: "
        "openevo-0.1.0-*.whl."
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
    assert "uv run python desktop/packaging/build_sidecar.py" in macos_job
    assert "--core-wheel-output-dir .openevo-release-inputs" in macos_job
    assert "scripts/ci/smoke_openevo_desktop_sidecar.py" in macos_job
    assert text.count("desktop/packaging/build_sidecar.py") == 2
    assert "uv run python packaging/build_sidecar.py" in linux_job

    assert "name: Probe APFS held-FD to FSRef to FSUnlinkObject cleanup" in macos_job
    assert 'diskutil info "$RUNNER_TEMP"' in macos_job
    assert "File System Personality|Type \\(Bundle\\)" in macos_job
    assert "_core_release_fd_removal_supported" in macos_job
    assert "_remove_core_release_fd_bound_entry" in macos_job
    assert "prepare_fsref = builder._prepare_core_release_fd_removal" in macos_job
    assert "execute_fsunlink = builder._execute_core_release_fd_removal" in macos_job
    assert 'native_calls.append("FSPathMakeRef")' in macos_job
    assert 'native_calls.append("FSUnlinkObject")' in macos_job
    assert "object_fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)" in macos_job
    assert 'subject="APFS held-FD FSRef probe"' in macos_job
    assert "FSUnlinkObject did not unlink the held object" in macos_job
    assert "openevo-core-service ensure" not in macos_job

    artifact_name = "openevo-core-release-inputs-${{ github.sha }}"
    assert "outputs:\n      manifest_sha256: " in macos_job
    assert "steps.release_inputs.outputs.manifest_sha256" in macos_job
    assert "id: release_inputs" in macos_job
    assert "shasum -a 256 openevo-*.whl framework-lock.json > SHA256SUMS" in macos_job
    assert "shasum -a 256 --check SHA256SUMS" in macos_job
    assert macos_job.count("-mindepth 1 -maxdepth 1") == 1
    assert "actions/upload-artifact@v4" in macos_job
    assert f"name: {artifact_name}" in macos_job
    assert ".openevo-release-inputs/openevo-*.whl" in macos_job
    assert ".openevo-release-inputs/framework-lock.json" in macos_job
    assert ".openevo-release-inputs/SHA256SUMS" in macos_job
    assert "include-hidden-files: true" in macos_job

    assert "actions/download-artifact@v4" in linux_job
    assert f"name: {artifact_name}" in linux_job
    assert "path: .openevo-release-inputs" in linux_job
    assert (
        "EXPECTED_MANIFEST_SHA256: "
        "${{ needs.macos-packaging-smoke.outputs.manifest_sha256 }}"
    ) in linux_job
    assert "sha256sum --check -" in linux_job
    assert "sha256sum --check SHA256SUMS" in linux_job
    assert linux_job.count("-mindepth 1 -maxdepth 1") == 2
    assert "uv sync --frozen --group dev" in linux_job
    assert linux_job.index("actions/download-artifact@v4") < linux_job.index(
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
            assert execution_mode in {"codex_subscription_transcript", "self-deployed"}
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

    text = workflow.read_text(encoding="utf-8")

    for marker in (
        "workflow_dispatch:",
        "runs-on: macos-14",
        "runs-on: ubuntu-latest",
        "timeout-minutes:",
        'test "$GITHUB_REF" = "refs/heads/stable"',
        "uv sync --frozen --group dev",
        "tests/ci/test_build_sidecar.py",
        "tests/ci/test_openevo_release_candidate.py",
        "tests/ci/test_openevo_release_evidence.py",
        "tests/openevo/remote/test_system_executables.py",
        "tests/openevo/remote/test_host_keys.py",
        "tests/openevo/remote/test_ssh_transport.py",
        "scripts/ci/audit_openevo_identity.py",
        "npm ci",
        "npm audit --audit-level=high",
        "pip-audit==2.9.0",
        "--no-emit-project",
        "--pip-requirements",
        "cargo-audit --locked --version 0.22.2",
        "file --version",
        "lipo -version",
        "collect_openevo_release_evidence.py",
        "Retain failed supply-chain summaries",
        "npm test -- --run",
        "npm run typecheck",
        "packaging/build_sidecar.py",
        "--core-wheel-output-dir",
        "framework-lock.json",
        "--framework-lock",
        "openevo-core-service",
        "cargo fmt --check",
        "cargo clippy --locked --release --all-targets -- -D warnings",
        "cargo test --locked --release",
        "npm run tauri:build -- --ci",
        "hdiutil attach",
        "smoke_openevo_desktop_bundle.py",
        "--evidence-out candidate-artifacts/app-bundle-smoke.json",
        "--evidence-out candidate-artifacts/dmg-copy-smoke.json",
        "scripts/ci/openevo_release_candidate.py create",
        "core-install-artifact.json",
        "release-candidate.json",
        "SHA256SUMS",
        "actions/upload-artifact@v4",
        "actions/download-artifact@v4",
        "retention-days: 14",
        "unsigned and not notarized",
        "permissions:\n      contents: write",
        "gh release create",
        "--draft",
        "--prerelease",
        "gh release upload",
        "gh release download",
        "diff -qr candidate-artifacts downloaded-draft",
        "gh release delete",
    ):
        assert marker in text

    assert "smoke_openevo_remote_capabilities.py" not in text

    assert text.index("npm ci") < text.index("npm run tauri:build -- --ci")
    assert text.index("hdiutil attach") < text.index(
        "scripts/ci/openevo_release_candidate.py create"
    )
    assert text.index("linux-core-candidate:") < text.index("draft-prerelease-roundtrip:")
    assert "needs: [macos-candidate, linux-core-candidate]" in text
    assert text.index("gh release create") < text.index("gh release upload")
    assert text.index("gh release upload") < text.index("gh release download")
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
    assert "cp .candidate-core/openevo-*.whl candidate-artifacts/" in text
    assert 'pip install candidate-artifacts/openevo-*.whl' in text
    assert text.index("openevo_release_candidate.py validate") < text.index(
        'pip install candidate-artifacts/openevo-*.whl'
    )

    desktop_checks = Path(".github/workflows/openevo-desktop.yml").read_text(encoding="utf-8")
    assert '".github/workflows/openevo-desktop-candidate.yml"' in desktop_checks


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


def test_tauri_macos_config_declares_unreleased_dmg_target() -> None:
    from desktop.sidecar.contracts.v1 import DESKTOP_OPENAPI_SHA256

    config = json.loads(Path("desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    cargo = Path("desktop/src-tauri/Cargo.toml").read_text(encoding="utf-8")
    main = Path("desktop/src-tauri/src/main.rs").read_text(encoding="utf-8")
    workflow = Path(".github/workflows/openevo-desktop.yml").read_text(encoding="utf-8")
    sidecar_builder = Path("desktop/packaging/build_sidecar.py")
    sidecar_entry = Path("desktop/packaging/sidecar_entry.py")
    release_contract = json.loads(Path("desktop/release-contract.json").read_text(encoding="utf-8"))
    gitignore = Path(".gitignore").read_text(encoding="utf-8")

    assert config["productName"] == "OpenEvo Desktop"
    assert config["version"] == "0.1.0"
    assert config["identifier"] == "org.openevo.desktop"
    assert config["build"]["beforeBuildCommand"] == "npm run build:openevo"
    assert config["build"]["frontendDist"] == "../dist"
    assert config["bundle"]["active"] is True
    assert config["bundle"]["targets"] == ["dmg"]
    assert config["bundle"]["externalBin"] == ["binaries/openevo-desktop-sidecar"]
    assert config["bundle"]["macOS"]["minimumSystemVersion"] == "12.0"
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
    assert 'name = "openevo-desktop"' in cargo
    assert 'serde = { version = "1", features = ["derive"] }' in cargo
    assert "tauri = " in cargo
    assert "struct ManagedSidecar" in main
    assert "struct DesktopHostState" in main
    assert "fn allocate_sidecar_listener()" in main
    assert "fn prepare_packaged_sidecar(" in main
    assert "libc::O_NOFOLLOW" in main
    assert "acl_get_fd_np" in main
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
    assert "cargo test --locked --release" in workflow
    assert workflow.index("npm run build:sidecar") < workflow.index(
        "tests::packaged_external_bin_native_launch_smoke"
    )
    assert "macOS FD-bound packaged sidecar launch smoke" in workflow
    assert "tests::macos_release_uses_private_path_and_keeps_the_verified_fd" in workflow
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

    module._patch_fd_bound_bootloader(tmp_path)

    patched = source.read_text(encoding="utf-8")
    patched_utils = utils_source.read_text(encoding="utf-8")
    assert patched.count('getenv("OPENEVO_NATIVE_EXECUTABLE_PATH")') == 1
    assert patched.count('getenv("OPENEVO_NATIVE_LISTENER_FD")') == 1
    assert patched.count("pyi_archive_open(openevo_archive_path)") == 1
    assert patched.count("fstat(4, &openevo_fd_stat)") == 1
    assert patched.count("lstat(openevo_native_path, &openevo_path_stat)") == 1
    assert patched.count("lstat(openevo_resolved_path, &openevo_resolved_stat)") == 1
    assert "SO_ACCEPTCONN" in patched_utils
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


def test_readme_release_checklist_matches_frontend_audit_gate() -> None:
    readme = Path("README.md")

    text = readme.read_text(encoding="utf-8")
    smoke_section = text[text.index("## Pre-External-Beta Release Smoke") :]

    assert "npm ci" in text
    assert "npm audit --audit-level=high" in text
    assert text.index("npm ci") < text.index("npm audit --audit-level=high")
    assert text.index("npm audit --audit-level=high") < text.index("npm test -- --run")
    assert "npm run typecheck" in text
    assert smoke_section.startswith("## Pre-External-Beta Release Smoke")
    assert "maintainer-only" in smoke_section
    assert "GitHub Release" in smoke_section
    assert "PyPI" in smoke_section
    assert "docs/maintainer/productization/spec.md" in smoke_section
    assert "scripts/ci/smoke_openevo_desktop_wheel.py" not in smoke_section
    assert ".openevo-wheel-smoke/bin/openevo-backend run --help" not in smoke_section
    assert "PyPI trusted publishing" not in text
    assert "pypa/gh-action-pypi-publish@release/v1" not in text


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
                "import subprocess",
                "",
                "subprocess.Popen(['/bin/sh', '-c', 'sleep 60'])",
                "evidence = {",
                "    'schema_version': 2,",
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
        "execution_mode_capabilities": {
            "schema_version": "1",
            "modes": [
                {
                    "mode": "codex_subscription_transcript",
                    "display_name": "Subscription",
                    "support_state": "supported",
                    "reason_code": None,
                    "message": "Available in this OpenEvo Desktop release.",
                },
                {
                    "mode": "self-deployed",
                    "display_name": "Self-deployed",
                    "support_state": "unavailable",
                    "reason_code": "self_deployed_release_unavailable",
                    "message": (
                        "Self-deployed execution is not available in this OpenEvo "
                        "Desktop release. Choose Subscription to save or run this project."
                    ),
                },
            ],
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
