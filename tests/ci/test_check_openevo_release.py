from __future__ import annotations

import importlib.util
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
        "openevo = openevo.cli:main",
        "polar = polar.cli:main",
        "polar-evolution = polar_evolution.cli:main",
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

    assert checker.validate_release_artifacts(
        [other_wheel],
        expected_version="0.1.0",
    ) == [
        "Release artifacts must include an exact OpenEvo wheel for remote install: "
        "openevo-0.1.0-*.whl."
    ]
    assert checker.validate_release_artifacts(
        [other_wheel, openevo_wheel],
        expected_version="0.1.0",
    ) == []


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
                "polar = polar.cli:main",
                "polar-evolution = polar_evolution.cli:main",
                "",
            ]
        ),
    )

    errors = checker.validate_wheel(wheel, expected_version="0.1.0")

    assert any("openevo = openevo.cli:main" in error for error in errors)


def test_requires_packaged_openevo_desktop_assets(tmp_path: Path) -> None:
    checker = _load_module()
    wheel = _write_wheel(
        tmp_path / "openevo-0.1.0-py3-none-any.whl",
        include_desktop_assets=False,
    )

    errors = checker.validate_wheel(wheel, expected_version="0.1.0")

    assert any("openevo/desktop/web/index.html" in error for error in errors)
    assert any("openevo/desktop/web/assets/" in error for error in errors)


def test_rejects_desktop_index_referencing_missing_assets(tmp_path: Path) -> None:
    checker = _load_module()
    wheel = _write_wheel(
        tmp_path / "openevo-0.1.0-py3-none-any.whl",
        desktop_index=(
            "<title>OpenEvo Desktop</title>"
            '<script type="module" src="/assets/missing.js"></script>'
        ),
    )

    errors = checker.validate_wheel(wheel, expected_version="0.1.0")

    assert any(
        "references missing Desktop asset" in error and "assets/missing.js" in error
        for error in errors
    )


def test_rejects_shared_dashboard_static_assets(tmp_path: Path) -> None:
    checker = _load_module()
    wheel = _write_wheel(
        tmp_path / "openevo-0.1.0-py3-none-any.whl",
        extra_files={"polar/platform/web/dist/index.html": "<title>Polar Dashboard</title>"},
    )

    errors = checker.validate_wheel(wheel, expected_version="0.1.0")

    assert any("polar/platform/web/dist" in error for error in errors)


def test_rejects_shared_dashboard_shell_copied_into_openevo_assets(tmp_path: Path) -> None:
    checker = _load_module()
    wheel = _write_wheel(
        tmp_path / "openevo-0.1.0-py3-none-any.whl",
        extra_files={
            "openevo/desktop/web/assets/shared-shell.js": (
                'const nav = "Polar Dashboard"; const tasks = "/tasks";'
            )
        },
    )

    errors = checker.validate_wheel(wheel, expected_version="0.1.0")

    assert any("shared dashboard marker" in error for error in errors)


def test_release_smoke_workflow_builds_packaged_assets_and_validates_wheel() -> None:
    workflow = Path(".github/workflows/openevo-release-smoke.yml")

    text = workflow.read_text(encoding="utf-8")

    assert 'node-version: "22"' in text
    assert "npm test -- --run" in text
    assert "npm audit --audit-level=high" in text
    assert "npm run build:openevo" in text
    assert "diff -qr web/dist src/openevo/desktop/web" in text
    assert '"src/polar/**"' in text
    assert '"src/polar_evolution/**"' in text
    assert '"src/slime_bridge/**"' in text
    assert '"tests/ci/**"' in text
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
    assert ".openevo-wheel-smoke/bin/openevo --help" in text
    assert ".openevo-wheel-smoke/bin/openevo desktop --help" in text
    assert ".openevo-wheel-smoke/bin/openevo desktop open --help" in text
    assert (
        ".openevo-wheel-smoke/bin/python "
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
        "name: Install wheel and smoke OpenEvo CLI"
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
    assert "diff -qr web/dist src/openevo/desktop/web" in text
    assert "python -m pip install --upgrade pip pytest -e ." in text
    assert "name: Build remote install wheel" in text
    assert "python -m build --wheel --outdir .openevo-remote-wheel" in text
    assert "mkdir -p src/openevo/wheels" in text
    assert "cp .openevo-remote-wheel/openevo-*.whl src/openevo/wheels/" in text
    assert "python -m build --wheel" in text
    assert "scripts/ci/check_openevo_release.py --wheel dist/*.whl" in text
    assert ".openevo-wheel-smoke/bin/openevo desktop open --help" in text
    assert (
        ".openevo-wheel-smoke/bin/python "
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
    assert "diff -qr web/dist src/openevo/desktop/web" in text
    assert "python -m pip install --upgrade pip pytest twine -e ." in text
    assert "name: Build remote install wheel" in text
    assert "python -m build --wheel --outdir .openevo-remote-wheel" in text
    assert "mkdir -p src/openevo/wheels" in text
    assert "cp .openevo-remote-wheel/openevo-*.whl src/openevo/wheels/" in text
    assert "python -m build --wheel" in text
    assert "scripts/ci/check_openevo_release.py --wheel dist/*.whl" in text
    assert "twine check --strict dist/*.whl" in text
    assert ".openevo-wheel-smoke/bin/openevo desktop open --help" in text
    assert (
        ".openevo-wheel-smoke/bin/python "
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
    assert "`GET /openevo-api/desktop/run/artifacts`" in text


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
        ".openevo-wheel-smoke/bin/python "
        "scripts/ci/smoke_openevo_desktop_wheel.py"
    ) in text
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
    include_desktop_assets: bool = True,
    desktop_index: str = (
        "<title>OpenEvo Desktop</title>"
        '<script src="/assets/app.js"></script>'
    ),
    include_nested_remote_wheel: bool = True,
    nested_remote_wheel_metadata: str = GOOD_METADATA,
    extra_files: dict[str, str] | None = None,
) -> Path:
    dist_info = "openevo-0.1.0.dist-info"
    with ZipFile(path, "w") as wheel:
        wheel.writestr(f"{dist_info}/METADATA", metadata)
        wheel.writestr(f"{dist_info}/entry_points.txt", entry_points)
        if include_desktop_assets:
            wheel.writestr(
                "openevo/desktop/web/index.html",
                desktop_index,
            )
            wheel.writestr("openevo/desktop/web/assets/app.js", "console.log('openevo')")
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
