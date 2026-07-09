from __future__ import annotations

import importlib.util
import json
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile


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
    path = (
        Path(__file__).resolve().parents[2]
        / "scripts/ci/smoke_openevo_desktop_wheel.py"
    )
    spec = importlib.util.spec_from_file_location("smoke_openevo_desktop_wheel", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_desktop_wheel_smoke_exercises_config_backed_lifecycle(capsys) -> None:
    smoke = _load_desktop_wheel_smoke_module()

    assert smoke.main() == 0

    output = capsys.readouterr().out
    assert "OpenEvo Desktop config-backed lifecycle smoke passed" in output


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
    other_wheel = _write_wheel(tmp_path / "polar-0.1.0-py3-none-any.whl")
    openevo_wheel = _write_wheel(tmp_path / "openevo-0.1.0-py3-none-any.whl")
    dmg = tmp_path / "OpenEvo Desktop_0.1.0_aarch64.dmg"
    dmg.write_bytes(b"not a real dmg; release list validation only checks presence")

    assert checker.validate_release_artifacts(
        [other_wheel],
        expected_version="0.1.0",
    ) == [
        "Release artifacts must include an exact OpenEvo wheel for remote install: "
        "openevo-0.1.0-*.whl.",
        "Release artifacts must include an OpenEvo Desktop macOS .dmg.",
    ]
    assert checker.validate_release_artifacts(
        [other_wheel, openevo_wheel],
        expected_version="0.1.0",
    ) == ["Release artifacts must include an OpenEvo Desktop macOS .dmg."]
    assert checker.validate_release_artifacts(
        [other_wheel, openevo_wheel, dmg],
        expected_version="0.1.0",
    ) == []


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
        [openevo_wheel, missing_dmg],
        expected_version="0.1.0",
    ) == [
        f"Release artifact does not exist: {missing_dmg}",
        "Release artifacts must include an OpenEvo Desktop macOS .dmg.",
    ]


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

    assert any(
        "openevo-backend = openevo.backend.launcher:main" in error for error in errors
    )


def test_rejects_core_wheel_packaging_desktop_control_plane(tmp_path: Path) -> None:
    checker = _load_module()
    wheel = _write_wheel(
        tmp_path / "openevo-0.1.0-py3-none-any.whl",
        extra_files={
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

    assert any("openevo/desktop/" in error for error in errors)
    assert any("openevo/sidecar/" in error for error in errors)
    assert any("openevo/cli.py" in error for error in errors)
    assert any("desktop/server/" in error for error in errors)
    assert any("desktop/sidecar/" in error for error in errors)
    assert any("desktop/src/" in error for error in errors)
    assert any("desktop/src-tauri/" in error for error in errors)
    assert any("desktop/packaging/web/" in error for error in errors)


def test_rejects_shared_dashboard_static_assets(tmp_path: Path) -> None:
    checker = _load_module()
    wheel = _write_wheel(
        tmp_path / "openevo-0.1.0-py3-none-any.whl",
        extra_files={
            "openevo/platform/desktop/dist/index.html": (
                "<title>OpenEvo Observability</title>"
            )
        },
    )

    errors = checker.validate_wheel(wheel, expected_version="0.1.0")

    assert any("openevo/platform/desktop/dist" in error for error in errors)


def test_local_version_validation_reads_top_level_desktop_metadata() -> None:
    checker = _load_module()

    root = Path(__file__).resolve().parents[2]
    paths = {
        path.relative_to(root).as_posix()
        for path in checker._desktop_package_metadata_paths()
    }

    assert "desktop/package.json" in paths
    assert "desktop/src-tauri/tauri.conf.json" in paths
    assert not any(path.startswith("web/") for path in paths)


def test_release_smoke_workflow_builds_packaged_assets_and_validates_wheel() -> None:
    workflow = Path(".github/workflows/openevo-release-smoke.yml")

    text = workflow.read_text(encoding="utf-8")

    assert 'node-version: "22"' in text
    assert "npm test -- --run" in text
    assert "npm audit --audit-level=high" in text
    assert "npm run build:openevo" in text
    assert "diff -qr desktop/dist desktop/packaging/web" in text
    assert '"src/slime_bridge/**"' in text
    assert '"desktop/**"' in text
    assert '"tests/**"' in text
    assert "python -m pip install --upgrade pip pytest -e ." in text
    assert "tests/ci/test_check_openevo_release.py" in text
    assert "name: Build remote install wheel" in text
    assert "python -m build --wheel --outdir .openevo-remote-wheel" in text
    assert "mkdir -p src/openevo/wheels" in text
    assert "cp .openevo-remote-wheel/openevo-*.whl src/openevo/wheels/" in text
    assert "python -m build --wheel" in text
    assert "scripts/ci/check_openevo_release.py --wheel dist/*.whl" in text
    assert "python -m venv .openevo-wheel-smoke" in text
    assert ".openevo-wheel-smoke/bin/python -m pip install dist/*.whl" in text
    assert ".openevo-wheel-smoke/bin/openevo-backend --help" in text
    assert ".openevo-wheel-smoke/bin/openevo-backend serve --help" in text
    assert ".openevo-wheel-smoke/bin/openevo-backend run --help" in text
    assert (
        "PYTHONPATH=. .openevo-wheel-smoke/bin/python "
        "scripts/ci/smoke_openevo_desktop_wheel.py"
    ) in text

    assert text.index("npm ci") < text.index("npm test -- --run")
    assert text.index("npm ci") < text.index("npm audit --audit-level=high")
    assert text.index("npm audit --audit-level=high") < text.index(
        "npm run build:openevo"
    )
    assert text.index("npm test -- --run") < text.index("npm run build:openevo")
    assert text.index("name: Build remote install wheel") < text.index(
        "name: Build wheel"
    )
    assert text.index("name: Build wheel") < text.index("name: Validate OpenEvo wheel")
    assert text.index("name: Validate OpenEvo wheel") < text.index(
        "name: Install wheel and smoke OpenEvo Backend"
    )


def test_release_artifact_workflow_builds_validated_wheel_artifact() -> None:
    workflow = Path(".github/workflows/openevo-release-artifact.yml")

    text = workflow.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in text
    assert "tags:" in text
    assert '"v*"' in text
    assert 'node-version: "22"' in text
    assert "npm ci" in text
    assert "npm audit --audit-level=high" in text
    assert "npm test -- --run" in text
    assert "npm run build:openevo" in text
    assert "diff -qr desktop/dist desktop/packaging/web" in text
    assert "python -m pip install --upgrade pip pytest -e ." in text
    assert "name: Build remote install wheel" in text
    assert "python -m build --wheel --outdir .openevo-remote-wheel" in text
    assert "mkdir -p src/openevo/wheels" in text
    assert "cp .openevo-remote-wheel/openevo-*.whl src/openevo/wheels/" in text
    assert "python -m build --wheel" in text
    assert "scripts/ci/check_openevo_release.py --wheel dist/*.whl" in text
    assert ".openevo-wheel-smoke/bin/openevo-backend --help" in text
    assert ".openevo-wheel-smoke/bin/openevo-backend serve --help" in text
    assert ".openevo-wheel-smoke/bin/openevo-backend run --help" in text
    assert (
        "PYTHONPATH=. .openevo-wheel-smoke/bin/python "
        "scripts/ci/smoke_openevo_desktop_wheel.py"
    ) in text
    assert "actions/upload-artifact@v4" in text
    assert "path: dist/*.whl" in text

    assert text.index("npm audit --audit-level=high") < text.index(
        "npm run build:openevo"
    )
    assert text.index("name: Build remote install wheel") < text.index(
        "name: Build wheel"
    )
    assert text.index("python -m build --wheel") < text.index(
        "scripts/ci/check_openevo_release.py --wheel dist/*.whl"
    )
    assert text.index("scripts/ci/check_openevo_release.py --wheel dist/*.whl") < (
        text.index("actions/upload-artifact@v4")
    )


def test_release_artifact_workflow_builds_desktop_dmg_artifact() -> None:
    workflow = Path(".github/workflows/openevo-release-artifact.yml")

    text = workflow.read_text(encoding="utf-8")

    assert "desktop-dmg-artifact:" in text
    assert "runs-on: macos-latest" in text
    assert 'node-version: "20"' in text
    assert "actions/setup-python@v5" in text
    assert "python-version: \"3.11\"" in text
    assert "dtolnay/rust-toolchain@stable" in text
    assert "name: Build bundled OpenEvo Desktop sidecar" in text
    assert "python -m pip install -e . pyinstaller" in text
    assert "python desktop/packaging/build_sidecar.py" in text
    assert 'SIDECAR="desktop/src-tauri/binaries/openevo-desktop-sidecar-$(rustc --print host-tuple)"' in text
    assert 'test -x "$SIDECAR"' in text
    assert '"$SIDECAR" --help' in text
    assert "working-directory: desktop" in text
    assert "working-directory: desktop/src-tauri" in text
    assert "cargo metadata --locked --format-version 1" in text
    assert "cargo test --locked" in text
    assert "npm ci" in text
    assert "npm run build:desktop" in text
    assert "name: openevo-desktop-dmg" in text
    assert "desktop/src-tauri/target/release/bundle/dmg/*.dmg" in text

    assert text.index("runs-on: macos-latest") < text.index('node-version: "20"')
    assert text.index('node-version: "20"') < text.index("dtolnay/rust-toolchain@stable")
    assert text.index("Build bundled OpenEvo Desktop sidecar") < text.index(
        "cargo metadata --locked --format-version 1"
    )
    assert text.index("cargo metadata --locked --format-version 1") < text.index(
        "npm run build:desktop"
    )
    assert text.index("npm ci") < text.index("npm run build:desktop")
    assert text.index("npm run build:desktop") < text.index("openevo-desktop-dmg")


def test_desktop_package_defines_tauri_desktop_scripts_and_cli_dependency() -> None:
    package = json.loads(Path("desktop/package.json").read_text(encoding="utf-8"))

    assert package["name"] == "openevo-desktop"
    assert package["scripts"]["tauri:dev"] == "tauri dev"
    assert package["scripts"]["tauri:build"] == "tauri build"
    assert package["scripts"]["build:desktop"] == "npm run tauri:build"
    assert "@tauri-apps/cli" in package["devDependencies"]


def test_tauri_macos_config_builds_dmg_release_shell() -> None:
    config = json.loads(Path("desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    cargo = Path("desktop/src-tauri/Cargo.toml").read_text(encoding="utf-8")
    main = Path("desktop/src-tauri/src/main.rs").read_text(encoding="utf-8")
    sidecar_builder = Path("desktop/packaging/build_sidecar.py")
    sidecar_entry = Path("desktop/packaging/sidecar_entry.py")
    linux_sidecar_stub = Path(
        "desktop/src-tauri/binaries/"
        "openevo-desktop-sidecar-x86_64-unknown-linux-gnu"
    )

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
    assert linux_sidecar_stub.is_file()
    assert "PyInstaller" in sidecar_builder.read_text(encoding="utf-8")
    assert "desktop.server.launcher" in sidecar_entry.read_text(encoding="utf-8")
    assert 'name = "openevo-desktop"' in cargo
    assert 'serde = { version = "1", features = ["derive"] }' in cargo
    assert 'tauri = ' in cargo
    assert "struct ManagedSidecar" in main
    assert "struct DesktopHostState" in main
    assert "fn allocate_port()" in main
    assert "fn sidecar_command(" in main
    assert "openevo-desktop-sidecar" in main
    assert "check_sidecar_health" in main
    assert "wait_for_sidecar_ready" in main
    assert "fn host_status(" in main
    assert "fn start_sidecar(" in main
    assert "fn stop_sidecar(" in main
    assert "fn create_ssh_tunnel(" not in main
    assert "fn keychain_reference(" in main
    assert "fn app_logs(" in main
    assert "desktop.server.launcher" in main
    assert "Command::new" in main
    assert "Stdio::null()" in main
    assert "tauri::generate_handler!" in main


def test_pypi_publish_workflow_uses_trusted_publishing() -> None:
    workflow = Path(".github/workflows/openevo-publish-pypi.yml")

    text = workflow.read_text(encoding="utf-8")

    assert "release:" in text
    assert "types: [published]" in text
    assert "id-token: write" in text
    assert "contents: read" in text
    assert "environment:" in text
    assert "name: pypi" in text
    assert 'node-version: "22"' in text
    assert "npm audit --audit-level=high" in text
    assert "npm test -- --run" in text
    assert "npm run build:openevo" in text
    assert "diff -qr desktop/dist desktop/packaging/web" in text
    assert "python -m pip install --upgrade pip pytest twine -e ." in text
    assert "name: Build remote install wheel" in text
    assert "python -m build --wheel --outdir .openevo-remote-wheel" in text
    assert "mkdir -p src/openevo/wheels" in text
    assert "cp .openevo-remote-wheel/openevo-*.whl src/openevo/wheels/" in text
    assert "python -m build --wheel" in text
    assert "scripts/ci/check_openevo_release.py --wheel dist/*.whl" in text
    assert "twine check --strict dist/*.whl" in text
    assert ".openevo-wheel-smoke/bin/openevo-backend --help" in text
    assert ".openevo-wheel-smoke/bin/openevo-backend serve --help" in text
    assert ".openevo-wheel-smoke/bin/openevo-backend run --help" in text
    assert (
        "PYTHONPATH=. .openevo-wheel-smoke/bin/python "
        "scripts/ci/smoke_openevo_desktop_wheel.py"
    ) in text
    assert "pypa/gh-action-pypi-publish@release/v1" in text
    assert "password:" not in text.casefold()
    assert "api-token" not in text.casefold()

    assert text.index("twine check --strict dist/*.whl") < text.index(
        "pypa/gh-action-pypi-publish@release/v1"
    )


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

    assert "Node 22" in text
    assert "npm ci" in text
    assert "npm audit --audit-level=high" in text
    assert text.index("npm ci") < text.index("npm audit --audit-level=high")
    assert text.index("npm audit --audit-level=high") < text.index(
        "npm test -- --run"
    )
    assert (
        "PYTHONPATH=. .openevo-wheel-smoke/bin/python "
        "scripts/ci/smoke_openevo_desktop_wheel.py"
    ) in text
    assert ".openevo-wheel-smoke/bin/openevo-backend run --help" in text
    assert "config-backed Desktop lifecycle" in text
    assert "PyPI trusted publishing" in text
    assert "pypa/gh-action-pypi-publish@release/v1" in text
    assert "GitHub release" in text
    assert "does not publish to PyPI yet" not in text


def _write_wheel(
    path: Path,
    *,
    metadata: str = GOOD_METADATA,
    entry_points: str = GOOD_ENTRY_POINTS,
    include_nested_remote_wheel: bool = True,
    nested_remote_wheel_metadata: str = GOOD_METADATA,
    extra_files: dict[str, str] | None = None,
) -> Path:
    dist_info = "openevo-0.1.0.dist-info"
    with ZipFile(path, "w") as wheel:
        wheel.writestr(f"{dist_info}/METADATA", metadata)
        wheel.writestr(f"{dist_info}/entry_points.txt", entry_points)
        if include_nested_remote_wheel:
            wheel.writestr(
                "openevo/wheels/openevo-0.1.0-py3-none-any.whl",
                _nested_wheel_bytes(metadata=nested_remote_wheel_metadata),
            )
        for name, content in (extra_files or {}).items():
            wheel.writestr(name, content)
    return path


def _nested_wheel_bytes(*, metadata: str) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w") as wheel:
        wheel.writestr("openevo-0.1.0.dist-info/METADATA", metadata)
    return buffer.getvalue()
