from __future__ import annotations

import pytest

from openevo.deployment.bootstrap import (
    RemoteBootstrapPlan,
    RemoteBootstrapReport,
    RemoteBootstrapStep,
    RemoteBootstrapStepExecution,
    RemoteBootstrapStepKind,
    RemoteBootstrapStepStatus,
    build_remote_bootstrap_plan,
    ensure_remote_openevo_cli_step,
    execute_remote_bootstrap_plan,
)
from openevo.deployment.preflight import (
    PreflightCheck,
    PreflightReport,
    RemoteCommandResult,
)
from openevo.projects.science import ScienceProjectConfig
from openevo.deployment import RemoteProfileConfig, build_sidecar_science_plan


class RecordingTransport:
    def __init__(
        self,
        *,
        fail_commands: set[str] | None = None,
        openevo_version: str = "0.1.0",
    ) -> None:
        self.fail_commands = fail_commands or set()
        self.openevo_version = openevo_version
        self.commands: list[tuple[str, str | None, dict[str, str] | None, float]] = []

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        self.commands.append((command, cwd, env, timeout_seconds))
        if command in self.fail_commands:
            return RemoteCommandResult(
                command=command,
                return_code=17,
                stderr="failed",
            )
        if "importlib.metadata" in command and "version('openevo')" in command:
            if (
                "no bundled wheel was available" in command
                and f"expected = {self.openevo_version!r}" not in command
            ):
                return RemoteCommandResult(
                    command=command,
                    return_code=1,
                    stderr=(
                        "Remote OpenEvo Core must be 0.1.0; "
                        "no bundled wheel was available"
                    ),
                )
            return RemoteCommandResult(
                command=command,
                return_code=0,
                stdout=f"{self.openevo_version}\n",
            )
        if "openevo-backend --version" in command:
            return RemoteCommandResult(
                command=command,
                return_code=0,
                stdout=f"openevo {self.openevo_version}\n",
            )
        if "openevo-backend --help" in command:
            return RemoteCommandResult(command=command, return_code=0, stdout="help")
        return RemoteCommandResult(command=command, return_code=0, stdout="ok")

    def upload_dir(self, local_path: str, remote_path: str) -> None:
        raise AssertionError("bootstrap executor must not upload workspaces")


class FailingPreflightTransport(RecordingTransport):
    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        self.commands.append((command, cwd, env, timeout_seconds))
        return RemoteCommandResult(command=command, return_code=1, stderr="no ssh")


class DiskLimitedPreflightTransport(RecordingTransport):
    def __init__(self, *, available_kb: int) -> None:
        super().__init__()
        self.available_kb = available_kb

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        self.commands.append((command, cwd, env, timeout_seconds))
        if command == 'df -Pk "$HOME"':
            return RemoteCommandResult(
                command=command,
                return_code=0,
                stdout=(
                    "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                    f"/dev/root 100000000 1 {self.available_kb} 1% /home\n"
                ),
            )
        return RemoteCommandResult(command=command, return_code=0, stdout="ok")


class RaisingTransport(RecordingTransport):
    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        self.commands.append((command, cwd, env, timeout_seconds))
        raise TimeoutError("timed out")


class LeakyCliInstallFailureTransport(RecordingTransport):
    def __init__(self, *, failing_command: str) -> None:
        super().__init__()
        self.failing_command = failing_command

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        self.commands.append((command, cwd, env, timeout_seconds))
        if command == self.failing_command:
            return RemoteCommandResult(
                command=command,
                return_code=13,
                stdout=(
                    "Looking in indexes: "
                    "https://pip-user:pip-secret@pypi.example/simple"
                ),
                stderr=(
                    "Proxy http://proxy-user:proxy-secret@127.0.0.1:7890 "
                    "failed for https://other-user:other-secret@example.test/pkg"
                ),
            )
        return RemoteCommandResult(command=command, return_code=0, stdout="ok")


class LeakyVersionProbeFailureTransport(RecordingTransport):
    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        self.commands.append((command, cwd, env, timeout_seconds))
        if (
            command.startswith("python3 - <<'PY'")
            and "importlib.metadata" in command
            and "version('openevo')" in command
        ):
            return RemoteCommandResult(
                command=command,
                return_code=1,
                stdout=(
                    "Looking in indexes: "
                    "https://pip-user:pip-secret@pypi.example/simple"
                ),
                stderr=(
                    "Proxy http://proxy-user:proxy-secret@127.0.0.1:7890 "
                    "failed for https://other-user:other-secret@example.test/pkg"
                ),
            )
        return RemoteCommandResult(command=command, return_code=0, stdout="ok")


class LeakySuccessfulVersionProbeTransport(RecordingTransport):
    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        self.commands.append((command, cwd, env, timeout_seconds))
        if "importlib.metadata" in command and "print(version('openevo'))" in command:
            return RemoteCommandResult(
                command=command,
                return_code=0,
                stdout="https://user:secret@example.test/simple\n",
            )
        if "importlib.metadata" in command and "version('openevo')" in command:
            return RemoteCommandResult(command=command, return_code=0, stdout="0.1.0\n")
        if "openevo-backend --version" in command:
            return RemoteCommandResult(command=command, return_code=0, stdout="openevo 0.1.0\n")
        if "openevo-backend --help" in command:
            return RemoteCommandResult(command=command, return_code=0, stdout="help")
        return RemoteCommandResult(command=command, return_code=0, stdout="ok")


class MissingCliEntrypointTransport(RecordingTransport):
    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        self.commands.append((command, cwd, env, timeout_seconds))
        if "importlib.metadata" in command and "version('openevo')" in command:
            return RemoteCommandResult(command=command, return_code=0, stdout="0.1.0\n")
        if "openevo-backend --version" in command:
            return RemoteCommandResult(
                command=command,
                return_code=127,
                stderr="openevo: command not found",
            )
        return RemoteCommandResult(command=command, return_code=0, stdout="ok")


class StaleCliEntrypointTransport(RecordingTransport):
    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        self.commands.append((command, cwd, env, timeout_seconds))
        if "importlib.metadata" in command and "version('openevo')" in command:
            return RemoteCommandResult(command=command, return_code=0, stdout="0.1.0\n")
        if "openevo-backend --version" in command:
            return RemoteCommandResult(command=command, return_code=0, stdout="openevo 0.2.0\n")
        return RemoteCommandResult(command=command, return_code=0, stdout="ok")


def _step(**overrides) -> RemoteBootstrapStep:
    payload = {
        "id": "ensure_state_dir",
        "kind": "ensure_dir",
        "command": "mkdir -p /home/alice/.openevo/runs/protein-design/folding-baseline",
        "timeout_seconds": 30.0,
        "network": False,
        "required": True,
        "remediation_kind": "openevo_retry",
    }
    payload.update(overrides)
    return RemoteBootstrapStep.model_validate(payload)


def _profile(**overrides: object) -> RemoteProfileConfig:
    payload = {
        "version": 1,
        "id": "lab-gpu",
        "host": "gpu.example.edu",
        "user": "alice",
        "proxy": {
            "https_proxy": "http://127.0.0.1:7890",
            "huggingface_endpoint": "https://hf-mirror.com",
        },
    }
    payload.update(overrides)
    return RemoteProfileConfig.model_validate(payload)


def _project(**overrides: object) -> ScienceProjectConfig:
    payload = {
        "version": 1,
        "project": {"name": "protein-design"},
        "remote_profile": "lab-gpu",
        "task": {
            "id": "folding-baseline",
            "objective": "Improve the folding baseline.",
            "source": {"type": "scratch"},
        },
    }
    payload.update(overrides)
    return ScienceProjectConfig.model_validate(payload)


def test_bootstrap_step_strips_and_validates_fields() -> None:
    step = _step(
        id="  docker_pull  ",
        kind="docker_pull",
        command="  docker pull image  ",
    )

    assert step.id == "docker_pull"
    assert step.kind == RemoteBootstrapStepKind.DOCKER_PULL
    assert step.command == "docker pull image"

    with pytest.raises(ValueError, match="id"):
        _step(id="   ")

    with pytest.raises(ValueError, match="command"):
        _step(command="   ")


def test_bootstrap_plan_is_tuple_backed_and_json_round_trips() -> None:
    plan = RemoteBootstrapPlan(
        remote_profile_id="lab-gpu",
        project_name="protein-design",
        task_id="folding-baseline",
        proxy_env={"HTTPS_PROXY": "http://127.0.0.1:7890"},
        state_root="/home/alice/.openevo/runs/protein-design/folding-baseline",
        workspace_root="/home/alice/.openevo/workspaces",
        experiment_snapshot={"experiment": {"name": "protein-design"}},
        steps=[_step()],
    )

    dumped = plan.model_dump(mode="json")
    restored = RemoteBootstrapPlan.model_validate(dumped)

    assert isinstance(plan.steps, tuple)
    assert restored == plan
    with pytest.raises(AttributeError):
        plan.steps.append(_step())


def test_bootstrap_report_ready_and_status_are_computed_and_round_trip() -> None:
    preflight = PreflightReport(
        checks=[
            PreflightCheck(
                name="ssh",
                status="pass",
                message="Remote command execution is available.",
            )
        ]
    )
    report = RemoteBootstrapReport(
        remote_profile_id="lab-gpu",
        project_name="protein-design",
        task_id="folding-baseline",
        preflight=preflight,
        steps=[
            RemoteBootstrapStepExecution(
                id="ensure_state_dir",
                kind=RemoteBootstrapStepKind.ENSURE_DIR,
                status=RemoteBootstrapStepStatus.PASS,
                message="ok",
                command="mkdir -p /tmp/run",
                return_code=0,
            )
        ],
        prepared_paths={"state_root": "/tmp/run"},
        next_actions=["Start remote OpenEvo services."],
    )

    dumped = report.model_dump(mode="json")
    restored = RemoteBootstrapReport.model_validate(dumped)

    assert dumped["ready"] is True
    assert dumped["status"] == "pass"
    assert isinstance(report.steps, tuple)
    assert restored == report


def test_bootstrap_report_fails_when_preflight_or_required_step_fails() -> None:
    report = RemoteBootstrapReport(
        remote_profile_id="lab-gpu",
        project_name="protein-design",
        task_id="folding-baseline",
        preflight=PreflightReport(
            checks=[
                PreflightCheck(
                    name="docker",
                    status="fail",
                    message="Docker unavailable.",
                )
            ]
        ),
        steps=[],
    )

    assert report.ready is False
    assert report.status == "fail"


def test_exact_openevo_cli_step_installs_bundled_wheel_and_records_contract() -> None:
    step = ensure_remote_openevo_cli_step(
        expected_version="0.1.0",
        bundled_wheel_remote_path=(
            "/home/alice/.openevo/runs/protein-design/folding-baseline/"
            "wheels/openevo-0.1.0-py3-none-any.whl"
        ),
        proxy_env={"HTTPS_PROXY": "http://127.0.0.1:7890"},
    )

    assert step.id == "ensure_openevo_cli"
    assert step.remediation_kind == "upload_exact_openevo_wheel"
    assert step.manifest == {
        "expected_version": "0.1.0",
        "bundled_wheel": True,
        "bundled_wheel_remote_path": (
            "/home/alice/.openevo/runs/protein-design/folding-baseline/"
            "wheels/openevo-0.1.0-py3-none-any.whl"
        ),
    }
    assert "python3 -m pip install --user" in step.command
    assert "--force-reinstall" in step.command
    assert step.command.startswith("set -e\n")
    assert "openevo-0.1.0-py3-none-any.whl" in step.command
    assert "importlib.metadata" in step.command
    assert "expected = '0.1.0'" in step.command
    assert "pip install --user --upgrade openevo" not in step.command
    assert "'--upgrade'" not in step.command
    assert " openevo\n" not in step.command


def test_exact_openevo_cli_step_without_wheel_only_accepts_existing_exact_version() -> None:
    step = ensure_remote_openevo_cli_step(
        expected_version="0.1.0",
        bundled_wheel_remote_path=None,
        proxy_env={},
    )

    assert step.remediation_kind == "upload_exact_openevo_wheel"
    assert step.manifest == {
        "expected_version": "0.1.0",
        "bundled_wheel": False,
        "bundled_wheel_remote_path": None,
    }
    assert "Remote OpenEvo Core must be 0.1.0; no bundled wheel was available" in (
        step.command
    )
    assert "pip install" not in step.command
    assert "importlib.metadata" in step.command
    assert "expected = '0.1.0'" in step.command


def test_build_remote_bootstrap_plan_derives_subscription_steps() -> None:
    sidecar_plan = build_sidecar_science_plan(_project(), _profile())

    plan = build_remote_bootstrap_plan(
        sidecar_plan,
        expected_openevo_version="0.1.0",
        bundled_wheel_remote_path=(
            "/home/alice/.openevo/runs/protein-design/folding-baseline/"
            "wheels/openevo-0.1.0-py3-none-any.whl"
        ),
    )
    steps_by_id = {step.id: step for step in plan.steps}

    assert plan.remote_profile_id == "lab-gpu"
    assert plan.project_name == "protein-design"
    assert plan.task_id == "folding-baseline"
    assert plan.workspace_root == "/home/alice/.openevo/workspaces"
    assert plan.state_root == "/home/alice/.openevo/runs/protein-design/folding-baseline"
    assert plan.proxy_env["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert plan.proxy_env["HF_ENDPOINT"] == "https://hf-mirror.com"
    assert plan.preflight.require_codex_subscription is True
    assert plan.preflight.min_home_available_kb == 20_000_000
    assert plan.experiment_snapshot["agent"]["auth"] == "subscription"
    assert isinstance(plan.experiment_snapshot["tasks"], list)

    assert steps_by_id["ensure_workspace_root"].kind == RemoteBootstrapStepKind.ENSURE_DIR
    assert steps_by_id["ensure_state_root"].command == (
        "mkdir -p /home/alice/.openevo/runs/protein-design/folding-baseline"
    )
    assert steps_by_id["write_experiment_snapshot"].kind == (
        RemoteBootstrapStepKind.WRITE_FILE
    )
    assert (
        "/home/alice/.openevo/runs/protein-design/folding-baseline/experiment.json"
        in steps_by_id["write_experiment_snapshot"].command
    )
    assert steps_by_id["ensure_openevo_cli"].kind == (
        RemoteBootstrapStepKind.CHECK_COMMAND
    )
    assert steps_by_id["ensure_openevo_cli"].required is True
    assert steps_by_id["ensure_openevo_cli"].network is True
    assert (
        steps_by_id["ensure_openevo_cli"].remediation_kind
        == "upload_exact_openevo_wheel"
    )
    assert steps_by_id["ensure_openevo_cli"].env["HTTPS_PROXY"] == (
        "http://127.0.0.1:7890"
    )
    assert steps_by_id["ensure_openevo_cli"].manifest == {
        "expected_version": "0.1.0",
        "bundled_wheel": True,
        "bundled_wheel_remote_path": (
            "/home/alice/.openevo/runs/protein-design/folding-baseline/"
            "wheels/openevo-0.1.0-py3-none-any.whl"
        ),
    }
    assert "python3 -m pip install --user" in steps_by_id["ensure_openevo_cli"].command
    assert "--force-reinstall" in steps_by_id["ensure_openevo_cli"].command
    assert "--no-input" in steps_by_id["ensure_openevo_cli"].command
    assert "--disable-pip-version-check" in (
        steps_by_id["ensure_openevo_cli"].command
    )
    assert "openevo-0.1.0-py3-none-any.whl" in (
        steps_by_id["ensure_openevo_cli"].command
    )
    assert "importlib.metadata" in steps_by_id["ensure_openevo_cli"].command
    assert "pip install --user --upgrade openevo" not in (
        steps_by_id["ensure_openevo_cli"].command
    )
    assert steps_by_id["write_bootstrap_manifest"].kind == (
        RemoteBootstrapStepKind.WRITE_FILE
    )
    assert steps_by_id["write_bootstrap_manifest"].manifest["path"].endswith(
        "/bootstrap.json"
    )
    assert '"experiment_snapshot":' in steps_by_id["write_bootstrap_manifest"].command
    assert "docker pull openevo/science-runtime:0.1.0 || docker build" in (
        steps_by_id["docker_pull_runtime"].command
    )
    assert steps_by_id["docker_pull_runtime"].manifest["managed_runtime"] is True
    assert steps_by_id["docker_pull_runtime"].env["HTTPS_PROXY"] == (
        "http://127.0.0.1:7890"
    )
    assert steps_by_id["check_codex_cli"].command == "codex --version"
    assert steps_by_id["check_codex_subscription"].command == (
        "test -f ~/.codex/auth.json"
    )
    assert "hf_snapshot_download" not in steps_by_id


def test_managed_runtime_bootstrap_builds_image_when_pull_fails() -> None:
    sidecar_plan = build_sidecar_science_plan(_project(), _profile())

    plan = build_remote_bootstrap_plan(sidecar_plan)
    docker_step = {step.id: step for step in plan.steps}["docker_pull_runtime"]

    assert "docker pull openevo/science-runtime:0.1.0 || docker build" in (
        docker_step.command
    )
    assert "node:22-bookworm-slim" in docker_step.command
    assert "@openai/codex@0.121.0" in docker_step.command
    assert "https://deb.debian.org" in docker_step.command
    assert "allow-unauthenticated" not in docker_step.command.lower()
    assert "trusted=yes" not in docker_step.command.lower()
    assert "--build-arg HTTP_PROXY" in docker_step.command
    assert docker_step.manifest["managed_runtime"] is True


def test_python_research_runtime_bootstrap_builds_image_when_pull_fails() -> None:
    sidecar_plan = build_sidecar_science_plan(
        _project(environment={"profile": "python_research"}),
        _profile(),
    )

    plan = build_remote_bootstrap_plan(sidecar_plan)
    docker_step = {step.id: step for step in plan.steps}["docker_pull_runtime"]

    assert "docker pull openevo/python-research-runtime:0.1.0 || docker build" in (
        docker_step.command
    )
    assert docker_step.manifest["managed_runtime"] is True


def test_custom_runtime_bootstrap_remains_pull_only() -> None:
    sidecar_plan = build_sidecar_science_plan(
        _project(
            environment={
                "profile": "custom_image",
                "custom_image": "ghcr.io/example/science:latest",
            }
        ),
        _profile(),
    )

    plan = build_remote_bootstrap_plan(sidecar_plan)
    docker_step = {step.id: step for step in plan.steps}["docker_pull_runtime"]

    assert docker_step.command == "docker pull ghcr.io/example/science:latest"
    assert "docker build" not in docker_step.command
    assert docker_step.manifest["managed_runtime"] is False


def test_build_remote_bootstrap_plan_adds_managed_inference_hf_prefetch() -> None:
    sidecar_plan = build_sidecar_science_plan(
        _project(
            execution={
                "mode": "codex_managed_local_inference",
                "hf_model": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
            }
        ),
        _profile(workspace_root="/srv/openevo/workspaces"),
    )

    plan = build_remote_bootstrap_plan(sidecar_plan)
    steps_by_id = {step.id: step for step in plan.steps}

    assert plan.state_root == "/srv/openevo/runs/protein-design/folding-baseline"
    assert "check_codex_cli" not in steps_by_id
    assert "check_codex_subscription" not in steps_by_id
    assert "docker pull openevo/science-runtime:0.1.0 || docker build" in (
        steps_by_id["docker_pull_runtime"].command
    )
    assert steps_by_id["docker_pull_runtime"].manifest["managed_runtime"] is True
    assert steps_by_id["hf_snapshot_download"].kind == (
        RemoteBootstrapStepKind.HF_SNAPSHOT_DOWNLOAD
    )
    assert steps_by_id["hf_snapshot_download"].network is True
    assert steps_by_id["hf_snapshot_download"].env["HF_ENDPOINT"] == (
        "https://hf-mirror.com"
    )
    assert "snapshot_download('Qwen/Qwen3-Coder-30B-A3B-Instruct')" in (
        steps_by_id["hf_snapshot_download"].command
    )


def test_execute_remote_bootstrap_plan_runs_steps_and_reports_ready() -> None:
    plan = build_remote_bootstrap_plan(
        build_sidecar_science_plan(_project(), _profile())
    )
    transport = RecordingTransport()

    report = execute_remote_bootstrap_plan(
        plan,
        transport,
        run_remote_preflight=False,
    )

    assert report.ready is True
    assert report.status == RemoteBootstrapStepStatus.PASS
    assert [step.status for step in report.steps] == [
        RemoteBootstrapStepStatus.PASS
    ] * len(plan.steps)
    commands = [command for command, _cwd, _env, _timeout in transport.commands]
    version_probe = next(
        command
        for command in commands
        if "importlib.metadata" in command and "print(version('openevo'))" in command
    )
    cli_version_probe = 'PATH="$HOME/.local/bin:$PATH" openevo-backend --version'
    cli_help_probe = 'PATH="$HOME/.local/bin:$PATH" openevo-backend --help'
    assert commands == [
        plan.steps[0].command,
        plan.steps[1].command,
        plan.steps[2].command,
        plan.steps[3].command,
        plan.steps[4].command,
        version_probe,
        cli_version_probe,
        cli_help_probe,
        *[step.command for step in plan.steps[5:]],
    ]
    assert report.prepared_paths == {
        "state_root": plan.state_root,
        "workspace_root": plan.workspace_root,
        "experiment_snapshot": f"{plan.state_root}/experiment.json",
        "bootstrap_manifest": f"{plan.state_root}/bootstrap.json",
    }
    assert report.next_actions == ("Remote bootstrap is ready.",)


def test_execute_remote_bootstrap_plan_blocks_steps_when_preflight_fails() -> None:
    plan = build_remote_bootstrap_plan(
        build_sidecar_science_plan(_project(), _profile())
    )
    transport = FailingPreflightTransport()

    report = execute_remote_bootstrap_plan(plan, transport)

    assert report.ready is False
    assert report.status == RemoteBootstrapStepStatus.FAIL
    assert report.steps == ()
    assert [command for command, _cwd, _env, _timeout in transport.commands] == ["true"]
    assert report.next_actions == (
        "Fix remote preflight failures and rerun bootstrap.",
    )


def test_execute_remote_bootstrap_plan_uses_plan_preflight_settings() -> None:
    sidecar_plan = build_sidecar_science_plan(
        _project(),
        _profile(min_home_available_kb=100_000_000),
    )
    plan = build_remote_bootstrap_plan(sidecar_plan)
    transport = DiskLimitedPreflightTransport(available_kb=99_999_999)

    report = execute_remote_bootstrap_plan(plan, transport)

    assert report.ready is False
    assert report.steps == ()
    assert report.preflight is not None
    assert [check.name for check in report.preflight.checks] == [
        "ssh",
        "docker",
        "docker_compose",
        "gpu",
        "disk",
        "codex_cli",
        "codex_subscription",
    ]
    assert report.preflight.by_name("disk").status == "fail"
    assert [command for command, _cwd, _env, _timeout in transport.commands] == [
        "true",
        "docker info",
        "docker compose version",
        "nvidia-smi -L",
        'df -Pk "$HOME"',
        "codex --version",
        "test -f ~/.codex/auth.json",
    ]


def test_execute_remote_bootstrap_plan_stops_after_required_step_failure() -> None:
    plan = build_remote_bootstrap_plan(
        build_sidecar_science_plan(_project(), _profile())
    )
    failing_command = {step.id: step.command for step in plan.steps}[
        "docker_pull_runtime"
    ]
    transport = RecordingTransport(fail_commands={failing_command})

    report = execute_remote_bootstrap_plan(
        plan,
        transport,
        run_remote_preflight=False,
    )

    assert report.ready is False
    assert report.steps[-1].id == "docker_pull_runtime"
    assert report.steps[-1].status == RemoteBootstrapStepStatus.FAIL
    assert report.steps[-1].return_code == 17
    assert report.steps[-1].stderr == "failed"
    assert report.next_actions == ("Resolve failed bootstrap steps and rerun.",)


def test_bootstrap_fails_instead_of_installing_latest_when_exact_version_missing() -> None:
    plan = build_remote_bootstrap_plan(
        build_sidecar_science_plan(_project(), _profile()),
        expected_openevo_version="0.1.0",
        bundled_wheel_remote_path=None,
    )
    ensure_step = {step.id: step for step in plan.steps}["ensure_openevo_cli"]

    report = execute_remote_bootstrap_plan(
        plan,
        RecordingTransport(openevo_version="0.2.0"),
        run_remote_preflight=False,
    )

    assert report.ready is False
    assert report.steps[0].id == "ensure_workspace_root"
    ensure_execution = next(step for step in report.steps if step.id == "ensure_openevo_cli")
    assert ensure_execution.status == RemoteBootstrapStepStatus.FAIL
    assert ensure_execution.remediation_kind == "upload_exact_openevo_wheel"
    assert (
        "Remote OpenEvo Core must be 0.1.0; no bundled wheel was available"
        in ensure_execution.stderr
    )
    assert "pip install" not in ensure_step.command
    assert "pip install --user --upgrade openevo" not in ensure_step.command


def test_execute_remote_bootstrap_plan_verifies_exact_version_after_install() -> None:
    plan = build_remote_bootstrap_plan(
        build_sidecar_science_plan(_project(), _profile()),
        expected_openevo_version="0.1.0",
        bundled_wheel_remote_path="/tmp/openevo-0.1.0-py3-none-any.whl",
    )

    report = execute_remote_bootstrap_plan(
        plan,
        RecordingTransport(openevo_version="0.2.0"),
        run_remote_preflight=False,
    )

    assert report.ready is False
    ensure_execution = next(step for step in report.steps if step.id == "ensure_openevo_cli")
    assert ensure_execution.status == RemoteBootstrapStepStatus.FAIL
    assert ensure_execution.message == (
        "Remote OpenEvo Core must be 0.1.0; found 0.2.0."
    )
    assert ensure_execution.remediation_kind == "upload_exact_openevo_wheel"
    assert report.next_actions == ("Resolve failed bootstrap steps and rerun.",)


def test_execute_remote_bootstrap_plan_reports_sanitized_openevo_install_failure() -> (
    None
):
    plan = build_remote_bootstrap_plan(
        build_sidecar_science_plan(
            _project(),
            _profile(
                proxy={
                    "https_proxy": "http://proxy-user:proxy-secret@127.0.0.1:7890",
                    "pip_index_url": (
                        "https://pip-user:pip-secret@pypi.example/simple"
                    ),
                }
            ),
        )
    )
    failing_command = {step.id: step.command for step in plan.steps}[
        "ensure_openevo_cli"
    ]

    report = execute_remote_bootstrap_plan(
        plan,
        LeakyCliInstallFailureTransport(failing_command=failing_command),
        run_remote_preflight=False,
    )

    assert report.ready is False
    assert report.steps[-1].id == "ensure_openevo_cli"
    assert report.steps[-1].status == RemoteBootstrapStepStatus.FAIL
    assert report.steps[-1].return_code == 13
    assert report.steps[-1].remediation_kind == "upload_exact_openevo_wheel"
    assert "[REDACTED]" in report.steps[-1].stdout
    assert "[REDACTED]" in report.steps[-1].stderr
    assert "example.test/pkg" in report.steps[-1].stderr
    for leaked in (
        "pip-secret",
        "proxy-secret",
        "other-secret",
        "pip-user:pip-secret",
        "proxy-user:proxy-secret",
        "other-user:other-secret",
    ):
        assert leaked not in report.steps[-1].stdout
        assert leaked not in report.steps[-1].stderr


def test_execute_remote_bootstrap_plan_reports_sanitized_version_probe_failure() -> None:
    plan = build_remote_bootstrap_plan(
        build_sidecar_science_plan(
            _project(),
            _profile(
                proxy={
                    "https_proxy": "http://proxy-user:proxy-secret@127.0.0.1:7890",
                    "pip_index_url": (
                        "https://pip-user:pip-secret@pypi.example/simple"
                    ),
                }
            ),
        ),
        expected_openevo_version="0.1.0",
        bundled_wheel_remote_path="/tmp/openevo-0.1.0-py3-none-any.whl",
    )

    report = execute_remote_bootstrap_plan(
        plan,
        LeakyVersionProbeFailureTransport(),
        run_remote_preflight=False,
    )

    ensure_execution = next(step for step in report.steps if step.id == "ensure_openevo_cli")
    assert ensure_execution.status == RemoteBootstrapStepStatus.FAIL
    assert "found unknown" in ensure_execution.message
    assert "[REDACTED]" in ensure_execution.stdout
    assert "[REDACTED]" in ensure_execution.stderr
    for leaked in (
        "pip-secret",
        "proxy-secret",
        "other-secret",
        "pip-user:pip-secret",
        "proxy-user:proxy-secret",
        "other-user:other-secret",
    ):
        assert leaked not in ensure_execution.stdout
        assert leaked not in ensure_execution.stderr


def test_execute_remote_bootstrap_plan_sanitizes_invalid_successful_probe_stdout() -> None:
    plan = build_remote_bootstrap_plan(
        build_sidecar_science_plan(_project(), _profile()),
        expected_openevo_version="0.1.0",
        bundled_wheel_remote_path="/tmp/openevo-0.1.0-py3-none-any.whl",
    )

    report = execute_remote_bootstrap_plan(
        plan,
        LeakySuccessfulVersionProbeTransport(),
        run_remote_preflight=False,
    )

    ensure_execution = next(step for step in report.steps if step.id == "ensure_openevo_cli")
    assert ensure_execution.status == RemoteBootstrapStepStatus.FAIL
    assert ensure_execution.message == (
        "Remote OpenEvo Core must be 0.1.0; found unknown."
    )
    assert "secret" not in ensure_execution.message
    assert "user:secret" not in ensure_execution.stdout
    assert "[REDACTED]" in ensure_execution.stdout


def test_execute_remote_bootstrap_plan_fails_when_cli_entrypoint_missing() -> None:
    plan = build_remote_bootstrap_plan(
        build_sidecar_science_plan(_project(), _profile()),
        expected_openevo_version="0.1.0",
        bundled_wheel_remote_path="/tmp/openevo-0.1.0-py3-none-any.whl",
    )

    report = execute_remote_bootstrap_plan(
        plan,
        MissingCliEntrypointTransport(),
        run_remote_preflight=False,
    )

    ensure_execution = next(step for step in report.steps if step.id == "ensure_openevo_cli")
    assert ensure_execution.status == RemoteBootstrapStepStatus.FAIL
    assert ensure_execution.message == (
        "Remote OpenEvo Backend launcher must be executable and report 0.1.0; found unknown."
    )
    assert "command not found" in ensure_execution.stderr


def test_execute_remote_bootstrap_plan_fails_when_cli_entrypoint_is_stale() -> None:
    plan = build_remote_bootstrap_plan(
        build_sidecar_science_plan(_project(), _profile()),
        expected_openevo_version="0.1.0",
        bundled_wheel_remote_path="/tmp/openevo-0.1.0-py3-none-any.whl",
    )

    report = execute_remote_bootstrap_plan(
        plan,
        StaleCliEntrypointTransport(),
        run_remote_preflight=False,
    )

    ensure_execution = next(step for step in report.steps if step.id == "ensure_openevo_cli")
    assert ensure_execution.status == RemoteBootstrapStepStatus.FAIL
    assert ensure_execution.message == (
        "Remote OpenEvo Backend launcher must be executable and report 0.1.0; found 0.2.0."
    )


def test_execute_remote_bootstrap_plan_reports_step_exception() -> None:
    plan = build_remote_bootstrap_plan(
        build_sidecar_science_plan(_project(), _profile())
    )

    report = execute_remote_bootstrap_plan(
        plan,
        RaisingTransport(),
        run_remote_preflight=False,
    )

    assert report.ready is False
    assert len(report.steps) == 1
    assert report.steps[0].status == RemoteBootstrapStepStatus.FAIL
    assert "timed out" in report.steps[0].message
    assert "timed out" in report.steps[0].stderr
