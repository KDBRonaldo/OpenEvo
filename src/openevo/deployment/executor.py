from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from openevo.deployment.preflight import (
    PreflightCheck,
    PreflightReport,
    RemoteCommandResult,
    run_preflight,
)

if TYPE_CHECKING:
    from openevo.deployment.planner import SidecarSciencePlan


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


@runtime_checkable
class RemoteExecutorTransport(Protocol):
    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult: ...

    def upload_dir(self, local_path: str, remote_path: str) -> None: ...


class WorkspaceActionStatus(StrEnum):
    PASS = "pass"
    SKIP = "skip"
    FAIL = "fail"


class WorkspaceActionExecution(_StrictFrozenModel):
    type: Literal["upload_dir", "git_clone", "use_remote_path"]
    task_id: str
    status: WorkspaceActionStatus
    message: str
    source: str | None = None
    target: str
    command: str | None = None
    return_code: int | None = None
    stdout: str = ""
    stderr: str = ""

    @field_validator("status", mode="before")
    @classmethod
    def _coerce_status(cls, value):
        if isinstance(value, str):
            return WorkspaceActionStatus(value)
        return value


class WorkspaceExecutionReport(_StrictFrozenModel):
    actions: tuple[WorkspaceActionExecution, ...] = Field(default_factory=tuple)

    @model_validator(mode="before")
    @classmethod
    def _ignore_dumped_ready(cls, value):
        if isinstance(value, dict) and "ready" in value:
            return {key: item for key, item in value.items() if key != "ready"}
        return value

    @field_validator("actions", mode="before")
    @classmethod
    def _coerce_actions_tuple(cls, value):
        if isinstance(value, list):
            return tuple(value)
        return value

    @computed_field
    @property
    def ready(self) -> bool:
        return all(action.status != WorkspaceActionStatus.FAIL for action in self.actions)


class SidecarExecutionReport(_StrictFrozenModel):
    remote_profile_id: str
    project_name: str
    task_id: str
    preflight: PreflightReport | None = None
    workspace: WorkspaceExecutionReport

    @model_validator(mode="before")
    @classmethod
    def _ignore_dumped_ready(cls, value):
        if isinstance(value, dict) and "ready" in value:
            return {key: item for key, item in value.items() if key != "ready"}
        return value

    @computed_field
    @property
    def ready(self) -> bool:
        preflight_ready = self.preflight is None or self.preflight.ready
        return preflight_ready and self.workspace.ready


def execute_sidecar_plan(
    plan: SidecarSciencePlan,
    transport: RemoteExecutorTransport,
    *,
    run_remote_preflight: bool = True,
) -> SidecarExecutionReport:
    if run_remote_preflight:
        try:
            preflight = run_preflight(transport, plan.preflight)
        except Exception as exc:
            preflight = _preflight_exception_report(exc)
    else:
        preflight = None
    if preflight is not None and not preflight.ready:
        workspace = WorkspaceExecutionReport(actions=())
    else:
        workspace = execute_workspace_plan(plan, transport)
    return SidecarExecutionReport(
        remote_profile_id=plan.remote_profile_id,
        project_name=plan.project_name,
        task_id=plan.task_id,
        preflight=preflight,
        workspace=workspace,
    )


def _preflight_exception_report(exc: Exception) -> PreflightReport:
    message = str(exc)
    return PreflightReport(
        checks=(
            PreflightCheck(
                name="preflight",
                status="fail",
                message=f"Remote preflight failed: {message}",
                remediation_kind="user_action",
                stderr=message,
            ),
        )
    )


def execute_workspace_plan(
    plan: SidecarSciencePlan,
    transport: RemoteExecutorTransport,
) -> WorkspaceExecutionReport:
    executions: list[WorkspaceActionExecution] = []
    for action in plan.workspace.actions:
        if action.type == "upload_dir":
            executions.append(_execute_upload_dir(action, transport))
        elif action.type == "git_clone":
            executions.append(_execute_git_clone(action, plan, transport))
        elif action.type == "use_remote_path":
            executions.append(
                _action_execution(
                    action,
                    status=WorkspaceActionStatus.SKIP,
                    message="Remote path already exists by contract.",
                )
            )
    return WorkspaceExecutionReport(actions=tuple(executions))


def _execute_upload_dir(action, transport: RemoteExecutorTransport) -> WorkspaceActionExecution:
    if action.source is None:
        return _action_execution(
            action,
            status=WorkspaceActionStatus.FAIL,
            message="upload_dir action requires source.",
        )
    try:
        transport.upload_dir(action.source, action.target)
    except Exception as exc:
        message = str(exc)
        return _action_execution(
            action,
            status=WorkspaceActionStatus.FAIL,
            message=message,
            stderr=message,
        )
    return _action_execution(
        action,
        status=WorkspaceActionStatus.PASS,
        message="Workspace directory uploaded.",
    )


def _execute_git_clone(
    action,
    plan: SidecarSciencePlan,
    transport: RemoteExecutorTransport,
) -> WorkspaceActionExecution:
    if action.command is None:
        return _action_execution(
            action,
            status=WorkspaceActionStatus.FAIL,
            message="git_clone action requires command.",
        )
    command = str(action.command)
    try:
        result = transport.run(command, env=dict(plan.proxy_env))
    except Exception as exc:
        message = str(exc)
        return _action_execution(
            action,
            status=WorkspaceActionStatus.FAIL,
            message=message,
            stderr=message,
        )
    status = WorkspaceActionStatus.PASS if result.ok else WorkspaceActionStatus.FAIL
    message = "Git clone completed." if result.ok else "Git clone failed."
    return _action_execution(
        action,
        status=status,
        message=message,
        return_code=result.return_code,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def _action_execution(
    action,
    *,
    status: WorkspaceActionStatus,
    message: str,
    return_code: int | None = None,
    stdout: str = "",
    stderr: str = "",
) -> WorkspaceActionExecution:
    return WorkspaceActionExecution(
        type=action.type,
        task_id=action.task_id,
        status=status,
        message=message,
        source=action.source,
        target=action.target,
        command=action.command,
        return_code=return_code,
        stdout=stdout,
        stderr=stderr,
    )
