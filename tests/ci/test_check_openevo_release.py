from __future__ import annotations

import importlib.util
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


def test_accepts_valid_openevo_release_wheel(tmp_path: Path) -> None:
    checker = _load_module()
    wheel = _write_wheel(tmp_path / "openevo-0.1.0-py3-none-any.whl")

    errors = checker.validate_wheel(wheel)

    assert errors == []


def test_rejects_non_openevo_project_metadata(tmp_path: Path) -> None:
    checker = _load_module()
    wheel = _write_wheel(
        tmp_path / "polar-0.1.0-py3-none-any.whl",
        metadata=GOOD_METADATA.replace("Name: openevo", "Name: polar"),
    )

    errors = checker.validate_wheel(wheel)

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

    errors = checker.validate_wheel(wheel)

    assert any("openevo = openevo.cli:main" in error for error in errors)


def test_requires_packaged_openevo_desktop_assets(tmp_path: Path) -> None:
    checker = _load_module()
    wheel = _write_wheel(
        tmp_path / "openevo-0.1.0-py3-none-any.whl",
        include_desktop_assets=False,
    )

    errors = checker.validate_wheel(wheel)

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

    errors = checker.validate_wheel(wheel)

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

    errors = checker.validate_wheel(wheel)

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

    errors = checker.validate_wheel(wheel)

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
    assert "tests/ci/test_check_openevo_release.py" in text
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
    assert text.index("name: Build wheel") < text.index("name: Validate OpenEvo wheel")
    assert text.index("name: Validate OpenEvo wheel") < text.index(
        "name: Install wheel and smoke OpenEvo CLI"
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
        for name, content in (extra_files or {}).items():
            wheel.writestr(name, content)
    return path
