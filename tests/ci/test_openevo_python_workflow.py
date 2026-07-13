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
    assert '"benchmarks/terminal_bench/**"' in text
    core_job, benchmark_job = text.split("  terminal-bench-tests:", maxsplit=1)
    assert "python -m pip install -e ." in core_job
    assert "python -m pip install -e benchmarks/terminal_bench" not in core_job
    assert "python -m pip install pytest pytest-asyncio ruff build twine" in core_job
    assert "ruff check src tests scripts" in core_job
    assert "benchmarks/terminal_bench/tests" not in core_job
    assert "smoke_terminal_bench_package.py" not in core_job
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
    assert "needs:" not in benchmark_job
    assert "python -m pip install -e ." in benchmark_job
    assert "python -m pip install -e benchmarks/terminal_bench" in benchmark_job
    assert "ruff check benchmarks/terminal_bench" in benchmark_job
    assert "benchmarks/terminal_bench/tests" in benchmark_job
    assert "smoke_terminal_bench_package.py" in benchmark_job
    assert "-q" in text


def test_openevo_desktop_workflow_runs_frontend_and_tauri_checks() -> None:
    workflow = Path(".github/workflows/openevo-desktop.yml")

    text = workflow.read_text(encoding="utf-8")

    assert "name: OpenEvo packaged sidecar and Desktop source checks" in text
    assert '"desktop/**"' in text
    assert '"scripts/ci/smoke_openevo_desktop_bundle.py"' in text
    assert '"scripts/ci/smoke_openevo_desktop_sidecar.py"' in text
    assert '"scripts/ci/write_sha256.py"' in text
    assert 'node-version: "22"' in text
    assert "dtolnay/rust-toolchain@stable" in text
    assert 'python-version: "3.11"' in text
    assert "astral-sh/setup-uv@v6" in text
    assert "name: Install packaged sidecar build dependencies" in text
    assert "uv sync --frozen --group dev" in text
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
    assert "npm run typecheck" in text
    assert "npm run build:openevo" in text
    assert "npm run build:sidecar" in text
    assert text.index("npm run build:sidecar") < text.index("cargo test --locked")
    assert "working-directory: desktop/src-tauri" in text
    assert "cargo metadata --locked --format-version 1" in text
    assert "cargo test --locked" in text


def test_release_smoke_path_filter_and_packaged_capability_guard() -> None:
    workflow = Path(".github/workflows/openevo-release-smoke.yml")
    remote_smoke = Path("scripts/ci/smoke_openevo_remote_capabilities.py")

    workflow_text = workflow.read_text(encoding="utf-8")
    remote_smoke_text = remote_smoke.read_text(encoding="utf-8")

    assert '- "scripts/ci/**"' in workflow_text
    assert "--sidecar \"$sidecar\"" in workflow_text
    assert "PYTHONPATH= .openevo-remote-wheel-smoke/bin/python" in workflow_text
    assert "sidecar_smoke.smoke_sidecar" in remote_smoke_text
    assert "TestClient" not in remote_smoke_text
