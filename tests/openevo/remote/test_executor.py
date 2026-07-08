from __future__ import annotations

from pathlib import Path

from openevo.remote import RemoteCommandResult
from openevo.remote.executor import (
    RemoteExecutorTransport,
    SidecarExecutionReport,
    WorkspaceActionStatus,
    WorkspaceExecutionReport,
    execute_sidecar_plan,
    execute_workspace_plan,
)
from openevo.science import ScienceProjectConfig
from openevo.sidecar import RemoteProfileConfig, build_sidecar_science_plan


class FakeTransport:
    def __init__(self, *, fail_commands: set[str] | None = None) -> None:
        self.fail_commands = fail_commands or set()
        self.commands: list[tuple[str, str | None, dict[str, str] | None]] = []
        self.uploads: list[tuple[str, str]] = []

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        self.commands.append((command, cwd, env))
        if command in self.fail_commands:
            return RemoteCommandResult(
                command=command,
                return_code=23,
                stderr="boom",
            )
        return RemoteCommandResult(command=command, return_code=0, stdout="ok")

    def upload_dir(self, local_path: str, remote_path: str) -> None:
        self.uploads.append((local_path, remote_path))


class RaisingRunTransport(FakeTransport):
    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        self.commands.append((command, cwd, env))
        raise TimeoutError("timed out")


class ReadyPreflightTransport(FakeTransport):
    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        self.commands.append((command, cwd, env))
        if command == 'df -Pk "$HOME"':
            return RemoteCommandResult(
                command=command,
                return_code=0,
                stdout=(
                    "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                    "/dev/sda1 100000000 1000 50000000 1% /home/user\n"
                ),
            )
        return RemoteCommandResult(command=command, return_code=0, stdout="ok")


def _profile() -> RemoteProfileConfig:
    return RemoteProfileConfig.model_validate(
        {
            "version": 1,
            "id": "lab-gpu",
            "host": "gpu.example.edu",
            "user": "alice",
            "proxy": {
                "https_proxy": "http://127.0.0.1:7890",
                "huggingface_endpoint": "https://hf-mirror.com",
            },
        }
    )


def _project(
    source: dict[str, str], tmp_path: Path | None = None
) -> ScienceProjectConfig:
    path = None if tmp_path is None else tmp_path / "science.yaml"
    if path is not None:
        path.write_text("version: 1\n", encoding="utf-8")
    return ScienceProjectConfig.model_validate(
        {
            "version": 1,
            "project": {"name": "protein-design"},
            "remote_profile": "lab-gpu",
            "task": {
                "id": "folding-baseline",
                "objective": "Improve the folding baseline.",
                "source": source,
            },
            "path": path,
        }
    )


def test_transport_protocol_accepts_fake_transport() -> None:
    assert isinstance(FakeTransport(), RemoteExecutorTransport)


def test_execute_workspace_plan_uploads_local_folder(tmp_path: Path) -> None:
    source_dir = tmp_path / "workspace"
    source_dir.mkdir()
    plan = build_sidecar_science_plan(
        _project({"type": "local_folder", "path": "workspace"}, tmp_path),
        _profile(),
    )
    transport = FakeTransport()

    report = execute_workspace_plan(plan, transport)

    assert report.ready is True
    assert [item.status for item in report.actions] == [WorkspaceActionStatus.PASS]
    assert transport.uploads == [(str(source_dir), plan.workspace.actions[0].target)]
    assert transport.commands == []


def test_execute_workspace_plan_runs_git_clone_with_proxy_env() -> None:
    plan = build_sidecar_science_plan(
        _project(
            {
                "type": "git_repository",
                "url": "https://github.com/example/research.git",
                "branch": "main",
            }
        ),
        _profile(),
    )
    transport = FakeTransport()

    report = execute_workspace_plan(plan, transport)

    assert report.ready is True
    action = plan.workspace.actions[0]
    assert transport.commands == [
        (
            str(action.command),
            None,
            {
                "HTTPS_PROXY": "http://127.0.0.1:7890",
                "https_proxy": "http://127.0.0.1:7890",
                "HF_ENDPOINT": "https://hf-mirror.com",
            },
        )
    ]


def test_execute_workspace_plan_marks_remote_path_as_skipped() -> None:
    plan = build_sidecar_science_plan(
        _project({"type": "remote_path", "path": "/datasets/folding"}),
        _profile(),
    )

    report = execute_workspace_plan(plan, FakeTransport())

    assert report.ready is True
    assert report.actions[0].status == WorkspaceActionStatus.SKIP
    assert report.actions[0].message == "Remote path already exists by contract."


def test_execute_workspace_plan_reports_command_failure() -> None:
    plan = build_sidecar_science_plan(
        _project(
            {"type": "git_repository", "url": "https://github.com/example/research.git"}
        ),
        _profile(),
    )
    command = str(plan.workspace.actions[0].command)

    report = execute_workspace_plan(plan, FakeTransport(fail_commands={command}))

    assert report.ready is False
    assert report.actions[0].status == WorkspaceActionStatus.FAIL
    assert report.actions[0].return_code == 23
    assert report.actions[0].stderr == "boom"


def test_workspace_report_round_trips_from_json_mode_dict() -> None:
    plan = build_sidecar_science_plan(
        _project({"type": "remote_path", "path": "/datasets/folding"}),
        _profile(),
    )
    report = execute_workspace_plan(plan, FakeTransport())

    restored = WorkspaceExecutionReport.model_validate(report.model_dump(mode="json"))

    assert restored == report
    assert restored.actions[0].status == WorkspaceActionStatus.SKIP


def test_execute_workspace_plan_reports_git_clone_transport_exception() -> None:
    plan = build_sidecar_science_plan(
        _project(
            {"type": "git_repository", "url": "https://github.com/example/research.git"}
        ),
        _profile(),
    )
    command = str(plan.workspace.actions[0].command)

    report = execute_workspace_plan(plan, RaisingRunTransport())

    assert report.ready is False
    assert len(report.actions) == 1
    assert report.actions[0].status == WorkspaceActionStatus.FAIL
    assert report.actions[0].return_code is None
    assert "timed out" in report.actions[0].message
    assert "timed out" in report.actions[0].stderr
    assert report.actions[0].command == command


def test_execute_sidecar_plan_blocks_workspace_when_preflight_fails_disk_parse() -> None:
    plan = build_sidecar_science_plan(
        _project({"type": "remote_path", "path": "/datasets/folding"}),
        _profile(),
    )
    transport = FakeTransport()

    report = execute_sidecar_plan(plan, transport)

    assert report.remote_profile_id == "lab-gpu"
    assert report.project_name == "protein-design"
    assert report.task_id == "folding-baseline"
    assert report.ready is False
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
    assert transport.commands[0][0] == "true"
    assert report.workspace.actions == ()


def test_execute_sidecar_plan_reports_preflight_transport_exception() -> None:
    plan = build_sidecar_science_plan(
        _project({"type": "remote_path", "path": "/datasets/folding"}),
        _profile(),
    )

    report = execute_sidecar_plan(plan, RaisingRunTransport())

    assert report.ready is False
    assert report.preflight is not None
    assert len(report.preflight.checks) == 1
    check = report.preflight.checks[0]
    assert check.status == "fail"
    assert "timed out" in check.message
    assert "timed out" in check.stderr
    assert report.workspace.actions == ()


def test_execute_sidecar_plan_can_skip_preflight_and_execute_workspace() -> None:
    plan = build_sidecar_science_plan(
        _project({"type": "remote_path", "path": "/datasets/folding"}),
        _profile(),
    )
    transport = FakeTransport()

    report = execute_sidecar_plan(plan, transport, run_remote_preflight=False)

    assert report.ready is True
    assert report.preflight is None
    assert [action.status for action in report.workspace.actions] == [
        WorkspaceActionStatus.SKIP
    ]
    assert transport.commands == []


def test_execute_sidecar_plan_executes_workspace_after_ready_preflight() -> None:
    plan = build_sidecar_science_plan(
        _project({"type": "remote_path", "path": "/datasets/folding"}),
        _profile(),
    )
    transport = ReadyPreflightTransport()

    report = execute_sidecar_plan(plan, transport)

    assert report.ready is True
    assert report.preflight is not None
    assert report.preflight.ready is True
    assert [command for command, _cwd, _env in transport.commands] == [
        "true",
        "docker info",
        "docker compose version",
        "nvidia-smi -L",
        'df -Pk "$HOME"',
        "codex --version",
        "test -f ~/.codex/auth.json",
    ]
    assert [action.status for action in report.workspace.actions] == [
        WorkspaceActionStatus.SKIP
    ]


def test_sidecar_execution_report_round_trips_from_json_mode_dict() -> None:
    plan = build_sidecar_science_plan(
        _project({"type": "remote_path", "path": "/datasets/folding"}),
        _profile(),
    )
    report = execute_sidecar_plan(plan, ReadyPreflightTransport())

    restored = SidecarExecutionReport.model_validate(report.model_dump(mode="json"))

    assert restored == report
    assert restored.ready is True
    assert restored.workspace.actions[0].status == WorkspaceActionStatus.SKIP
