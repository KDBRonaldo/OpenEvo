from __future__ import annotations

import importlib.util
import json
import hashlib
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest


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


def test_bundle_smoke_finds_and_launches_app_sidecar(tmp_path: Path) -> None:
    smoke = _load_bundle_smoke_module()
    sidecar = tmp_path / "OpenEvo Desktop.app" / "Contents" / "MacOS" / "openevo-desktop-sidecar"
    _write_fake_sidecar(sidecar)

    smoked_sidecar = smoke.smoke_bundle(tmp_path, timeout_seconds=5)

    assert smoked_sidecar == sidecar


def test_bundle_smoke_requires_openevo_desktop_app_bundle(tmp_path: Path) -> None:
    smoke = _load_bundle_smoke_module()
    other_sidecar = tmp_path / "Other.app" / "Contents" / "MacOS" / "openevo-desktop-sidecar"
    _write_fake_sidecar(other_sidecar)

    try:
        smoke.find_bundled_sidecar(tmp_path)
    except smoke.SmokeFailure as exc:
        assert "No OpenEvo Desktop.app bundle found" in str(exc)
    else:
        raise AssertionError("Expected missing OpenEvo Desktop.app bundle to fail")


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


def test_release_smoke_workflow_builds_packaged_assets_and_validates_wheel() -> None:
    workflow = Path(".github/workflows/openevo-release-smoke.yml")
    framework_smoke = Path("scripts/ci/smoke_evolution_framework_wheel.py")
    capability_smoke = Path("scripts/ci/smoke_openevo_remote_capabilities.py")
    desktop_smoke = Path("scripts/ci/smoke_openevo_desktop_wheel.py")

    text = workflow.read_text(encoding="utf-8")
    framework_smoke_text = framework_smoke.read_text(encoding="utf-8")
    capability_smoke_text = capability_smoke.read_text(encoding="utf-8")
    desktop_smoke_text = desktop_smoke.read_text(encoding="utf-8")

    assert text.startswith("name: OpenEvo packaged sidecar + installed Core release smoke")
    assert 'node-version: "22"' in text
    assert "npm test -- --run" in text
    assert "npm run typecheck" in text
    assert "npm audit --audit-level=high" in text
    assert "npm run build:openevo" in text
    assert "diff -qr desktop/dist desktop/packaging/web" in text
    assert '"src/slime_bridge/**"' in text
    assert '"desktop/**"' in text
    assert '- "scripts/ci/**"' in text
    assert '"tests/**"' in text
    assert "astral-sh/setup-uv@v6" in text
    assert "uv sync --frozen --group dev" in text
    assert "tests/ci/test_build_sidecar.py" in text
    assert "tests/ci/test_check_openevo_release.py" in text
    assert "name: Build and smoke packaged Desktop sidecar" in text
    assert "uv run python desktop/packaging/build_sidecar.py" in text
    assert "--core-wheel-output-dir .openevo-remote-wheel" in text
    assert "scripts/ci/smoke_openevo_desktop_sidecar.py" in text
    assert "name: Build outer smoke wheel from isolated source" in text
    assert "python -m build --wheel --outdir .openevo-remote-wheel" not in text
    assert "rm -rf src/openevo/wheels" not in text
    assert "mkdir -p src/openevo/wheels" not in text
    assert 'mkdir -p "$outer_source/src/openevo/wheels"' in text
    assert 'src/ "$outer_source/src/"' in text
    assert "uv run python -m build --wheel --no-isolation" in text
    assert "scripts/ci/check_openevo_release.py --wheel dist/*.whl" in text
    assert "name: Smoke exact remote Core wheel" in text
    assert "python -m venv .openevo-remote-wheel-smoke" in text
    assert (
        ".openevo-remote-wheel-smoke/bin/python -m pip install .openevo-remote-wheel/*.whl"
    ) in text
    assert ".openevo-remote-wheel-smoke/bin/openevo-backend --help" in text
    assert ".openevo-remote-wheel-smoke/bin/openevo-backend serve --help" in text
    assert ".openevo-remote-wheel-smoke/bin/openevo-backend run --help" in text
    assert (
        "PYTHONPATH= .openevo-remote-wheel-smoke/bin/python "
        "scripts/ci/smoke_evolution_framework_wheel.py "
        "--wheel .openevo-remote-wheel/*.whl"
    ) in text
    assert (
        "PYTHONPATH= .openevo-remote-wheel-smoke/bin/python "
        "scripts/ci/smoke_openevo_remote_capabilities.py"
    ) in text
    assert "--wheel .openevo-remote-wheel/*.whl" in text
    assert '--sidecar "$sidecar"' in text
    assert (
        'sidecar="desktop/src-tauri/binaries/openevo-desktop-sidecar-$(rustc --print host-tuple)"'
    ) in text
    assert "openevo-backend" in capability_smoke_text
    assert "sidecar_smoke.smoke_sidecar" in capability_smoke_text
    assert "TestClient" not in capability_smoke_text
    assert "create_sidecar_app" not in capability_smoke_text
    assert "BackendConnection" not in capability_smoke_text
    assert "backend_client_factory" not in capability_smoke_text
    assert "start_new_session=True" in capability_smoke_text
    assert "name: Smoke installed Core with source Desktop harness" in text
    assert "python -m venv .openevo-wheel-smoke" in text
    assert ".openevo-wheel-smoke/bin/python -m pip install dist/*.whl" in text
    assert (
        "PYTHONPATH=. .openevo-wheel-smoke/bin/python scripts/ci/smoke_openevo_desktop_wheel.py"
    ) in text
    assert "source Desktop harness, not a packaged app" in desktop_smoke_text
    assert "EXPECTED_METHOD_IDS" in framework_smoke_text
    assert "EXPECTED_TARGET_IDS" in framework_smoke_text
    assert "EXPECTED_HANDLER_IDS" in framework_smoke_text
    assert framework_smoke_text.index("verified = verify_distribution_install(") < (
        framework_smoke_text.index("from openevo.evolution.framework import (")
    )
    assert "FrameworkDistributionLock" in framework_smoke_text
    assert "load_verified_framework_registry" in framework_smoke_text
    assert framework_smoke_text.index("FrameworkDistributionLock(") < (
        framework_smoke_text.index("loaded = load_verified_framework_registry(lock_path)")
    )

    assert text.index("npm ci") < text.index("npm test -- --run")
    assert text.index("npm ci") < text.index("npm audit --audit-level=high")
    assert text.index("npm audit --audit-level=high") < text.index("npm run build:openevo")
    assert text.index("npm test -- --run") < text.index("npm run build:openevo")
    assert text.index("npm run typecheck") < text.index("npm run build:openevo")
    assert text.index("name: Build and smoke packaged Desktop sidecar") < text.index(
        "name: Build outer smoke wheel from isolated source"
    )
    assert text.index("name: Build outer smoke wheel from isolated source") < text.index(
        "name: Validate OpenEvo wheel"
    )
    assert text.index("name: Validate OpenEvo wheel") < text.index(
        "name: Smoke exact remote Core wheel"
    )
    assert text.index("name: Smoke exact remote Core wheel") < text.index(
        "name: Smoke installed Core with source Desktop harness"
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


def test_tauri_macos_config_declares_unreleased_dmg_target() -> None:
    config = json.loads(Path("desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    cargo = Path("desktop/src-tauri/Cargo.toml").read_text(encoding="utf-8")
    main = Path("desktop/src-tauri/src/main.rs").read_text(encoding="utf-8")
    workflow = Path(".github/workflows/openevo-desktop.yml").read_text(encoding="utf-8")
    sidecar_builder = Path("desktop/packaging/build_sidecar.py")
    sidecar_entry = Path("desktop/packaging/sidecar_entry.py")
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
    assert "PyInstaller" in sidecar_builder.read_text(encoding="utf-8")
    assert "_build_core_wheel" in sidecar_builder.read_text(encoding="utf-8")
    assert "_validate_embedded_core_wheel" in sidecar_builder.read_text(encoding="utf-8")
    assert "--add-data" in sidecar_builder.read_text(encoding="utf-8")
    assert "desktop/packaging/web" in sidecar_builder.read_text(encoding="utf-8")
    assert "desktop.server.launcher" in sidecar_entry.read_text(encoding="utf-8")
    assert 'name = "openevo-desktop"' in cargo
    assert 'serde = { version = "1", features = ["derive"] }' in cargo
    assert "tauri = " in cargo
    assert "struct ManagedSidecar" in main
    assert "struct DesktopHostState" in main
    assert "fn allocate_sidecar_listener()" in main
    assert "fn prepare_packaged_sidecar(" in main
    assert "libc::O_NOFOLLOW" in main
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
    assert "tests::macos_release_executes_the_inherited_fd_through_devfs" in workflow
    assert "if: always()" in workflow
    assert 'rm -f "$OPENEVO_PACKAGED_SIDECAR_PATH"' in workflow
    assert "cargo build --locked --release" in workflow
    assert "release binary contains the debug source launcher fallback" in workflow
    assert "release binary contains debug sidecar override code" in workflow


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


def _write_fake_sidecar(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "from http.server import BaseHTTPRequestHandler, HTTPServer",
                "import argparse",
                "import json",
                "",
                "class Handler(BaseHTTPRequestHandler):",
                "    def do_GET(self):",
                "        if self.path == '/health':",
                "            body = json.dumps({'status': 'ok'}).encode()",
                "            self.send_response(200)",
                "            self.send_header('Content-Type', 'application/json')",
                "            self.send_header('Content-Length', str(len(body)))",
                "            self.end_headers()",
                "            self.wfile.write(body)",
                "            return",
                "        if self.path == '/openevo-api/desktop/core-artifact':",
                "            digest = 'a' * 64",
                "            body = json.dumps({",
                "                'available': True,",
                "                'distribution': 'openevo',",
                "                'distribution_version': '0.1.0',",
                "                'wheel_filename': 'openevo-0.1.0-py3-none-any.whl',",
                "                'distribution_digest': digest,",
                "                'framework_lock': {",
                "                    'distribution_digest': digest,",
                "                    'wheel_filename': 'openevo-0.1.0-py3-none-any.whl',",
                "                },",
                "            }).encode()",
                "            self.send_response(200)",
                "            self.send_header('Content-Type', 'application/json')",
                "            self.send_header('Content-Length', str(len(body)))",
                "            self.end_headers()",
                "            self.wfile.write(body)",
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
                "parser.add_argument('--port', type=int, required=True)",
                "parser.add_argument('--desktop-config-root')",
                "args = parser.parse_args()",
                "HTTPServer((args.host, args.port), Handler).serve_forever()",
                "",
            ]
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)
