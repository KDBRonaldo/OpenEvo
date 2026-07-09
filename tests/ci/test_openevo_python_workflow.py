from __future__ import annotations

from pathlib import Path


def test_openevo_python_workflow_runs_focused_regressions() -> None:
    workflow = Path(".github/workflows/openevo-python.yml")

    text = workflow.read_text(encoding="utf-8")

    assert "name: OpenEvo Core Backend checks" in text
    assert '".github/workflows/openevo-desktop.yml"' in text
    assert '".github/workflows/openevo-release-artifact.yml"' in text
    assert '".github/workflows/openevo-publish-pypi.yml"' in text
    assert '"src/openevo/**"' in text
    assert '"src/slime_bridge/**"' in text
    assert '"scripts/**"' in text
    assert '"tests/**"' in text
    assert "python -m pip install -e ." in text
    assert "python -m pip install pytest pytest-asyncio ruff build twine" in text
    assert "ruff check src tests scripts" in text
    assert "tests/ci" in text
    assert "tests/config" in text
    assert "tests/platform" in text
    assert "tests/test_evolution_agent_harnesses.py" in text
    assert "tests/backend" in text
    assert "tests/evolution" in text
    assert "tests/gateway" in text
    assert "tests/trajectory" in text
    assert "tests/rollout" in text
    assert "tests/openevo/remote" in text
    assert "tests/openevo/science" in text
    assert "tests/openevo/sidecar" in text
    assert "tests/openevo/desktop" in text
    assert "tests/openevo/test_experiment_compiler.py" in text
    assert "tests/openevo/test_experiment_models.py" in text
    assert "tests/openevo/test_experiment_runner.py" in text
    assert "tests/openevo/test_core_capabilities.py" in text
    assert "-q" in text


def test_openevo_desktop_workflow_runs_frontend_and_tauri_checks() -> None:
    workflow = Path(".github/workflows/openevo-desktop.yml")

    text = workflow.read_text(encoding="utf-8")

    assert "name: OpenEvo Desktop checks" in text
    assert '"desktop/**"' in text
    assert '"scripts/ci/smoke_openevo_desktop_bundle.py"' in text
    assert '"scripts/ci/smoke_openevo_desktop_sidecar.py"' in text
    assert '"scripts/ci/write_sha256.py"' in text
    assert 'node-version: "22"' in text
    assert "dtolnay/rust-toolchain@stable" in text
    assert "name: Install Linux Tauri dependencies" in text
    assert "libwebkit2gtk-4.1-dev" in text
    assert "libayatana-appindicator3-dev" in text
    assert "libgtk-3-dev" in text
    assert "librsvg2-dev" in text
    assert "libxdo-dev" in text
    assert "patchelf" in text
    assert "npm ci" in text
    assert "npm audit --audit-level=high" in text
    assert "npm test -- --run" in text
    assert "npm run build:openevo" in text
    assert "working-directory: desktop/src-tauri" in text
    assert "cargo metadata --locked --format-version 1" in text
    assert "cargo test --locked" in text
