from __future__ import annotations

from pathlib import Path


def test_openevo_python_workflow_runs_focused_regressions() -> None:
    workflow = Path(".github/workflows/openevo-python.yml")

    text = workflow.read_text(encoding="utf-8")

    assert "name: OpenEvo Core Backend checks" in text
    assert '".github/workflows/openevo-desktop.yml"' in text
    assert '".github/workflows/openevo-desktop-candidate.yml"' in text
    assert '".github/workflows/openevo-release-artifact.yml"' in text
    assert '".github/workflows/openevo-publish-pypi.yml"' in text
    assert '"src/openevo/**"' in text
    assert '"src/slime_bridge/**"' in text
    assert '"scripts/**"' in text
    assert '"tests/**"' in text
    assert '"benchmarks/terminal_bench/**"' in text
    python_job = text.split("  python-tests:\n", maxsplit=1)[1].split(
        "\n  real-docker-runtime-probes:", maxsplit=1
    )[0]
    remaining_jobs = text.split("  macos-ssh-transport:\n", maxsplit=1)[1]
    macos_job, benchmark_job = remaining_jobs.split("  terminal-bench-tests:", maxsplit=1)
    assert "python -m pip install -e ." in python_job
    assert "python -m pip install -e benchmarks/terminal_bench" not in python_job
    assert "python -m pip install pytest pytest-asyncio ruff build twine wheel" in python_job
    assert "ruff check src tests scripts" in python_job
    assert "benchmarks/terminal_bench/tests" not in python_job
    assert "smoke_terminal_bench_package.py" not in python_job
    assert "tests/ci" in text
    assert "tests/config" in text
    assert "tests/platform" in text
    assert "tests/test_evolution_agent_harnesses.py" in text
    assert "tests/backend" in text
    assert "tests/evolution" in text
    assert "tests/gateway" in text
    assert "tests/trajectory" in text
    assert "tests/rollout" in text
    assert "tests/runtime" in python_job
    assert "tests/openevo/remote" in text
    assert "tests/openevo/science" in text
    assert "tests/openevo/sidecar" in text
    assert "tests/openevo/desktop" in text
    assert "tests/openevo/test_experiment_compiler.py" in text
    assert "tests/openevo/test_experiment_models.py" in text
    assert "tests/openevo/test_experiment_runner.py" in text
    assert "tests/openevo/test_core_capabilities.py" in text
    regression_block = python_job.split(
        "      - name: Run OpenEvo regression tests\n", maxsplit=1
    )[1]
    assert "          umask 077\n" in regression_block
    assert regression_block.index("umask 077") < regression_block.index("python -m pytest")
    assert "runs-on: macos-14" in macos_job
    assert "python -m pip install -e ." in macos_job
    assert "python -m pip install pytest" in macos_job
    assert "unset SSH_AUTH_SOCK" in macos_job
    assert "umask 077" in macos_job
    anonymous_transport_tests = (
        "test_core_tunnel_uses_parent_owned_socketpair_and_per_connection_ssh_child",
        "test_core_tunnel_connection_start_cancellation_closes_parent_stream_and_propagates",
        "test_core_tunnel_never_fchmods_anonymous_socketpair_on_darwin",
        "test_core_tunnel_rejects_non_stream_socketpair",
        "test_core_tunnel_rejects_socketpair_not_owned_by_effective_uid",
        "test_core_tunnel_rejects_peer_fd_replacement_after_child_spawn",
        "test_core_tunnel_rejects_child_that_exits_during_connection_start",
        "test_core_connection_authority_passes_birth_and_peer_fds_to_exact_ssh_child",
        "test_core_connection_subprocess_bridges_a_real_parent_owned_af_unix_stream",
    )
    transport_support_tests = (
        "test_source_birth_launcher_uses_platform_bound_execution_target",
        "test_rsync_nested_ssh_uses_verified_platform_target_and_inherits_fd",
        "test_darwin_rsync_nested_ssh_uses_revalidated_fixed_path",
        "test_group_signal_skips_kill_when_only_zombie_members_remain",
        "test_group_signal_permission_error_requires_state_recheck",
    )
    system_executable_tests = (
        "test_darwin_system_executable_uses_revalidated_root_owned_path",
        "test_owned_subprocess_birth_uses_platform_execution_authority",
        "test_malicious_path_ssh_is_never_executed",
        "test_keyscan_executes_only_the_held_fixed_binary",
    )
    selected_transport_tests = anonymous_transport_tests + transport_support_tests
    assert macos_job.count("tests/openevo/remote/test_ssh_transport.py::") == len(
        selected_transport_tests
    )
    assert macos_job.count("tests/openevo/remote/test_system_executables.py::") == len(
        system_executable_tests
    )
    for test_name in selected_transport_tests:
        assert f"tests/openevo/remote/test_ssh_transport.py::{test_name}" in macos_job
    for test_name in system_executable_tests:
        assert f"tests/openevo/remote/test_system_executables.py::{test_name}" in macos_job
    assert "tests/openevo/remote/test_ssh_transport.py -q" not in macos_job
    assert "--basetemp" not in macos_job
    assert "needs:" not in benchmark_job
    assert "python -m pip install -e ." in benchmark_job
    assert "python -m pip install -e benchmarks/terminal_bench" in benchmark_job
    assert "ruff check benchmarks/terminal_bench" in benchmark_job
    assert "benchmarks/terminal_bench/tests" in benchmark_job
    assert "smoke_terminal_bench_package.py" in benchmark_job
    assert "-q" in text


def test_runtime_docker_candidate_gate_requires_real_docker_and_probe_image() -> None:
    workflow = Path(".github/workflows/openevo-python.yml")

    text = workflow.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in text
    assert "require_real_docker" in text
    assert "default: true" in text
    assert "real-docker-runtime-probes:" in text
    assert "docker info" in text
    assert "docker pull python:3.12-slim-bookworm" in text
    assert 'OPENEVO_REQUIRE_REAL_DOCKER: "1"' in text
    assert "test_real_docker_name_collision_preserves_running_external_container" in text
    assert "test_real_docker_credential_auth_inode_remains_pinned_after_host_replacement" in text
    assert "test_real_docker_cancel_after_cidfile_is_recoverably_owned" in text


def test_openevo_desktop_workflow_runs_frontend_and_tauri_checks() -> None:
    workflow = Path(".github/workflows/openevo-desktop.yml")

    text = workflow.read_text(encoding="utf-8")

    assert "name: OpenEvo packaged sidecar and Desktop source checks" in text
    assert '".github/workflows/openevo-python.yml"' in text
    assert '"desktop/**"' in text
    assert '"src/openevo/deployment/**"' in text
    assert '"tests/ci/test_openevo_python_workflow.py"' in text
    assert '"tests/ci/test_sidecar_startup_diagnostics.py"' in text
    assert '"tests/openevo/remote/**"' in text
    assert '"tests/openevo/sidecar/test_fd_xattrs.py"' in text
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
    macos_job = text.split("  macos-native-launch-smoke:\n", maxsplit=1)[1]
    workspace_step, remaining_macos = macos_job.split(
        "      - name: Exercise macOS fixed SSH executable and process authority\n",
        maxsplit=1,
    )
    ssh_step, _remaining_macos = remaining_macos.split(
        "      - name: Exercise macOS Core release publication contract\n",
        maxsplit=1,
    )
    assert "umask 077" in workspace_step
    assert '--basetemp="$RUNNER_TEMP/oe-workspace"' in workspace_step
    assert "tests/openevo/sidecar/test_fd_xattrs.py" in workspace_step
    assert "unset SSH_AUTH_SOCK" in ssh_step
    assert "umask 077" in ssh_step
    assert 'case "${HOME:-}" in' in ssh_step
    assert 'ssh_test_prefix="$HOME/.oe-ssh."' in ssh_step
    assert 'mktemp -d "${ssh_test_prefix}XXXXXX"' in ssh_step
    assert 'chmod 700 "$ssh_test_root"' in ssh_step
    assert 'trap \'rm -rf -- "$ssh_test_root"\' EXIT' in ssh_step
    assert '--basetemp="$ssh_test_root/pytest"' in ssh_step
    assert "$RUNNER_TEMP" not in ssh_step


def test_release_smoke_path_filter_and_platform_separation_guard() -> None:
    workflow = Path(".github/workflows/openevo-release-smoke.yml")
    remote_smoke = Path("scripts/ci/smoke_openevo_remote_capabilities.py")

    workflow_text = workflow.read_text(encoding="utf-8")
    remote_smoke_text = remote_smoke.read_text(encoding="utf-8")

    assert '- "scripts/ci/**"' in workflow_text
    macos_job, linux_job = workflow_text.split("  linux-core-smoke:\n", maxsplit=1)
    assert "runs-on: macos-14" in macos_job
    assert "scripts/ci/smoke_openevo_desktop_sidecar.py" in macos_job
    assert "openevo-core-service ensure" not in macos_job
    assert "runs-on: ubuntu-latest" in linux_job
    assert "needs: macos-packaging-smoke" in linux_job
    assert "scripts/ci/smoke_openevo_remote_capabilities.py" in linux_job
    assert "uv run python packaging/build_sidecar.py" in linux_job
    assert "openevo-core-service ensure" not in linux_job
    assert "sidecar_smoke.smoke_sidecar" in remote_smoke_text
    assert "TestClient" not in remote_smoke_text
