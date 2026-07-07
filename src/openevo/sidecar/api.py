from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from _thread import LockType
import posixpath
import re
import secrets
from typing import Any, Literal
from threading import Lock

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openevo.remote.executor import RemoteExecutorTransport
from openevo.science import ScienceProjectConfig
from openevo.sidecar.models import RemoteProfileConfig
from openevo.sidecar.planner import build_sidecar_science_plan

SIDECAR_MUTATION_TOKEN_HEADER = "X-OpenEvo-Sidecar-Token"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class SidecarHealth(_StrictFrozenModel):
    service: Literal["openevo-sidecar"] = "openevo-sidecar"
    status: Literal["ok"] = "ok"


class DesktopRemoteProxy(_StrictFrozenModel):
    https_proxy: str | None = None
    huggingface_endpoint: str | None = None


class DesktopRemoteProfile(_StrictFrozenModel):
    id: str
    host: str
    user: str
    proxy: DesktopRemoteProxy = Field(default_factory=DesktopRemoteProxy)


class DesktopScienceProject(_StrictFrozenModel):
    name: str
    task_id: str
    source: str
    objective: str


class DesktopExecutionStatus(_StrictFrozenModel):
    mode: Literal["codex_subscription_transcript", "codex_managed_local_inference"]
    model: str
    token_metrics_available: bool

    @model_validator(mode="after")
    def _validate_subscription_metrics(self) -> DesktopExecutionStatus:
        if (
            self.mode == "codex_subscription_transcript"
            and self.token_metrics_available
        ):
            raise ValueError(
                "token_metrics_available must be false for "
                "codex_subscription_transcript"
            )
        return self


class DesktopBootstrapStatus(_StrictFrozenModel):
    ready: bool
    state_root: str
    workspace_root: str
    readiness_notes: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("readiness_notes", mode="before")
    @classmethod
    def _coerce_notes(cls, value):
        if isinstance(value, list):
            return tuple(value)
        return value


class DesktopServiceStatus(_StrictFrozenModel):
    id: str
    label: str
    state: Literal["ready", "running", "planned", "blocked"]
    detail: str


class DesktopEvolutionStep(_StrictFrozenModel):
    id: str
    label: str
    state: Literal["complete", "running", "planned", "blocked"]
    detail: str


class DesktopDeveloperMode(_StrictFrozenModel):
    enabled: bool = False
    benchmark_controls_visible: bool = False


class DesktopSidecarSecurity(_StrictFrozenModel):
    mutation_token: str | None = None

    @field_validator("mutation_token")
    @classmethod
    def _strip_token(cls, value: str | None) -> str | None:
        if value is None:
            return None
        token = value.strip()
        if not token:
            raise ValueError("mutation_token must be a non-empty string")
        return token


class OpenEvoDesktopShellStatus(_StrictFrozenModel):
    remote: DesktopRemoteProfile
    project: DesktopScienceProject
    execution: DesktopExecutionStatus
    bootstrap: DesktopBootstrapStatus
    services: tuple[DesktopServiceStatus, ...] = Field(default_factory=tuple)
    evolution: tuple[DesktopEvolutionStep, ...] = Field(default_factory=tuple)
    developer_mode: DesktopDeveloperMode = Field(default_factory=DesktopDeveloperMode)
    sidecar: DesktopSidecarSecurity = Field(default_factory=DesktopSidecarSecurity)

    @field_validator("services", "evolution", mode="before")
    @classmethod
    def _coerce_tuples(cls, value):
        if isinstance(value, list):
            return tuple(value)
        return value


class OpenEvoDesktopBootstrapResponse(_StrictFrozenModel):
    bootstrap: DesktopBootstrapStatus
    report: dict[str, Any]
    status: OpenEvoDesktopShellStatus


@dataclass
class OpenEvoSidecarSession:
    project: ScienceProjectConfig
    profile: RemoteProfileConfig
    transport_factory: Callable[[RemoteProfileConfig], RemoteExecutorTransport]
    status: OpenEvoDesktopShellStatus
    last_bootstrap_report: object | None = None
    bootstrap_lock: LockType = field(default_factory=Lock)


def build_desktop_shell_status(
    project: ScienceProjectConfig,
    profile: RemoteProfileConfig,
) -> OpenEvoDesktopShellStatus:
    sidecar_plan = build_sidecar_science_plan(project, profile)
    return OpenEvoDesktopShellStatus(
        remote=DesktopRemoteProfile(
            id=profile.id,
            host=profile.host,
            user=profile.user,
            proxy=DesktopRemoteProxy(
                https_proxy=profile.proxy.https_proxy,
                huggingface_endpoint=profile.proxy.huggingface_endpoint,
            ),
        ),
        project=DesktopScienceProject(
            name=project.project.name,
            task_id=project.task.id,
            source=_source_label(project),
            objective=project.task.objective,
        ),
        execution=_execution_status(project),
        bootstrap=DesktopBootstrapStatus(
            ready=False,
            state_root=_state_root(
                sidecar_plan.workspace.workspace_root,
                project_name=sidecar_plan.project_name,
                task_id=sidecar_plan.task_id,
            ),
            workspace_root=sidecar_plan.workspace.workspace_root,
            readiness_notes=("Remote bootstrap has not run yet.",),
        ),
        services=_service_statuses(project),
        evolution=_evolution_steps(project),
    )


def default_desktop_shell_status() -> OpenEvoDesktopShellStatus:
    return OpenEvoDesktopShellStatus(
        remote=DesktopRemoteProfile(
            id="lab-gpu",
            host="gpu.example.edu",
            user="alice",
            proxy=DesktopRemoteProxy(
                https_proxy="http://127.0.0.1:7890",
                huggingface_endpoint="https://hf-mirror.com",
            ),
        ),
        project=DesktopScienceProject(
            name="Protein Folding Literature Sprint",
            task_id="folding-baseline",
            source="Git repository: github.com/example/protein-workflows",
            objective=(
                "Survey recent folding papers, extract benchmark tables, "
                "and run the baseline analysis notebook."
            ),
        ),
        execution=DesktopExecutionStatus(
            mode="codex_subscription_transcript",
            model="gpt-5.1-codex-mini",
            token_metrics_available=False,
        ),
        bootstrap=DesktopBootstrapStatus(
            ready=True,
            state_root=(
                "/home/alice/.openevo/runs/"
                "protein-folding-literature-sprint/folding-baseline"
            ),
            workspace_root="/home/alice/.openevo/workspaces",
            readiness_notes=("Codex subscription login available",),
        ),
        services=(
            DesktopServiceStatus(
                id="ssh",
                label="SSH transport",
                state="ready",
                detail="Remote command execution available",
            ),
            DesktopServiceStatus(
                id="workspace",
                label="Workspace",
                state="ready",
                detail="Repository materialized in managed workspace",
            ),
            DesktopServiceStatus(
                id="bootstrap",
                label="Bootstrap",
                state="ready",
                detail="Runtime image and manifests prepared",
            ),
            DesktopServiceStatus(
                id="openevo-backend",
                label="OpenEvo backend",
                state="planned",
                detail="Service supervisor integration is next",
            ),
        ),
        evolution=(
            DesktopEvolutionStep(
                id="transcript",
                label="Transcript capture",
                state="complete",
                detail="Codex subscription mode uses transcript trajectory data",
            ),
            DesktopEvolutionStep(
                id="memory",
                label="Text memory",
                state="complete",
                detail="Two durable research notes promoted",
            ),
            DesktopEvolutionStep(
                id="skills",
                label="Skill bundle",
                state="running",
                detail="Extracting reusable literature-review workflow",
            ),
            DesktopEvolutionStep(
                id="agent-system",
                label="Agent system",
                state="planned",
                detail="Instruction diff will be reviewed after this round",
            ),
        ),
    )


def create_sidecar_app(
    status: OpenEvoDesktopShellStatus | None = None,
    *,
    session: OpenEvoSidecarSession | None = None,
    mutation_token: str | None = None,
) -> FastAPI:
    desktop_status = status or default_desktop_shell_status()
    sidecar_token = _mutation_token(mutation_token)
    app = FastAPI(title="OpenEvo Desktop Sidecar", version="0.1.0")

    @app.get("/health", response_model=SidecarHealth)
    def health() -> SidecarHealth:
        return SidecarHealth()

    @app.get(
        "/openevo-api/desktop/shell",
        response_model=OpenEvoDesktopShellStatus,
    )
    def desktop_shell() -> OpenEvoDesktopShellStatus:
        if session is not None:
            return _status_with_mutation_token(session.status, sidecar_token)
        return _status_with_mutation_token(desktop_status, sidecar_token)

    @app.post(
        "/openevo-api/desktop/bootstrap",
        response_model=OpenEvoDesktopBootstrapResponse,
    )
    def bootstrap(
        token: str | None = Header(
            default=None,
            alias=SIDECAR_MUTATION_TOKEN_HEADER,
        ),
    ) -> OpenEvoDesktopBootstrapResponse:
        _validate_mutation_token(token, sidecar_token)
        if session is None:
            raise HTTPException(
                status_code=409,
                detail="Desktop bootstrap requires a config-backed sidecar session.",
            )
        if not session.bootstrap_lock.acquire(blocking=False):
            raise HTTPException(
                status_code=409,
                detail="Desktop bootstrap is already running.",
            )
        try:
            report = _run_bootstrap(session)
            session.last_bootstrap_report = report
            session.status = _status_after_bootstrap(session.status, report)
        finally:
            session.bootstrap_lock.release()
        response_status = _status_with_mutation_token(session.status, sidecar_token)
        return OpenEvoDesktopBootstrapResponse(
            bootstrap=response_status.bootstrap,
            report=report.model_dump(mode="json"),
            status=response_status,
        )

    return app


def create_sidecar_app_for_project(
    project: ScienceProjectConfig,
    profile: RemoteProfileConfig,
    *,
    transport_factory: Callable[[RemoteProfileConfig], RemoteExecutorTransport],
    mutation_token: str | None = None,
) -> FastAPI:
    session = OpenEvoSidecarSession(
        project=project,
        profile=profile,
        transport_factory=transport_factory,
        status=build_desktop_shell_status(project, profile),
    )
    return create_sidecar_app(session=session, mutation_token=mutation_token)


def _execution_status(project: ScienceProjectConfig) -> DesktopExecutionStatus:
    if project.execution.mode == "codex_subscription_transcript":
        return DesktopExecutionStatus(
            mode=project.execution.mode,
            model=project.execution.codex_model or "gpt-5.1-codex-mini",
            token_metrics_available=False,
        )
    return DesktopExecutionStatus(
        mode=project.execution.mode,
        model=project.execution.hf_model or "",
        token_metrics_available=True,
    )


def _source_label(project: ScienceProjectConfig) -> str:
    source = project.task.source
    if source.type == "remote_path":
        return f"Remote path: {source.path}"
    if source.type == "local_folder":
        return f"Local folder: {source.path}"
    if source.type == "git_repository":
        branch = f" ({source.branch})" if source.branch else ""
        return f"Git repository: {source.url}{branch}"
    return "Scratch workspace"


def _service_statuses(
    project: ScienceProjectConfig,
) -> tuple[DesktopServiceStatus, ...]:
    source_type = project.task.source.type
    workspace_ready = source_type in {"remote_path", "scratch"}
    if source_type == "remote_path":
        workspace_detail = "Workspace source is already remote"
    elif source_type == "scratch":
        workspace_detail = "Scratch workspace does not need source preparation"
    else:
        workspace_detail = "Workspace preparation has not run yet"
    return (
        DesktopServiceStatus(
            id="ssh",
            label="SSH transport",
            state="planned",
            detail="Remote preflight has not run yet",
        ),
        DesktopServiceStatus(
            id="workspace",
            label="Workspace",
            state="ready" if workspace_ready else "planned",
            detail=workspace_detail,
        ),
        DesktopServiceStatus(
            id="bootstrap",
            label="Bootstrap",
            state="planned",
            detail="Remote bootstrap has not run yet",
        ),
        DesktopServiceStatus(
            id="openevo-backend",
            label="OpenEvo backend",
            state="planned",
            detail="Service supervisor integration is next",
        ),
    )


def _evolution_steps(
    project: ScienceProjectConfig,
) -> tuple[DesktopEvolutionStep, ...]:
    steps = [
        DesktopEvolutionStep(
            id="transcript",
            label="Transcript capture",
            state="planned",
            detail="Trajectory capture will start after the first run",
        )
    ]
    if project.evolution.text_memory:
        steps.append(
            DesktopEvolutionStep(
                id="text-memory",
                label="Text memory",
                state="planned",
                detail="No promoted memory artifact yet",
            )
        )
    if project.evolution.skill_bundle:
        steps.append(
            DesktopEvolutionStep(
                id="skill-bundle",
                label="Skill bundle",
                state="planned",
                detail="No promoted skill bundle yet",
            )
        )
    if project.evolution.agent_system:
        steps.append(
            DesktopEvolutionStep(
                id="agent-system",
                label="Agent system",
                state="planned",
                detail="No promoted agent-system artifact yet",
            )
        )
    return tuple(steps)


def _run_bootstrap(session: OpenEvoSidecarSession):
    from openevo.remote.bootstrap import (
        build_remote_bootstrap_plan,
        execute_remote_bootstrap_plan,
    )

    sidecar_plan = build_sidecar_science_plan(session.project, session.profile)
    bootstrap_plan = build_remote_bootstrap_plan(sidecar_plan)
    return execute_remote_bootstrap_plan(
        bootstrap_plan,
        session.transport_factory(session.profile),
    )


def _mutation_token(value: str | None) -> str:
    if value is None:
        return secrets.token_urlsafe(32)
    token = value.strip()
    if not token:
        raise ValueError("mutation_token must be a non-empty string")
    return token


def _validate_mutation_token(candidate: str | None, expected: str) -> None:
    if candidate is None or not secrets.compare_digest(candidate, expected):
        raise HTTPException(
            status_code=403,
            detail="Invalid OpenEvo sidecar token.",
        )


def _status_with_mutation_token(
    status: OpenEvoDesktopShellStatus,
    token: str,
) -> OpenEvoDesktopShellStatus:
    return status.model_copy(
        update={"sidecar": DesktopSidecarSecurity(mutation_token=token)}
    )


def _status_after_bootstrap(
    status: OpenEvoDesktopShellStatus,
    report,
) -> OpenEvoDesktopShellStatus:
    bootstrap_ready = bool(report.ready)
    return status.model_copy(
        update={
            "bootstrap": status.bootstrap.model_copy(
                update={
                    "ready": bootstrap_ready,
                    "readiness_notes": tuple(report.next_actions),
                }
            ),
            "services": tuple(
                _service_after_bootstrap(service, report=report)
                for service in status.services
            ),
        }
    )


def _service_after_bootstrap(
    service: DesktopServiceStatus,
    *,
    report,
) -> DesktopServiceStatus:
    if service.id == "ssh" and report.preflight is not None:
        if report.preflight.ready:
            return service.model_copy(
                update={"state": "ready", "detail": "Remote preflight passed"}
            )
        return service.model_copy(
            update={"state": "blocked", "detail": "Remote preflight failed"}
        )
    if service.id == "bootstrap":
        if report.ready:
            return service.model_copy(
                update={
                    "state": "ready",
                    "detail": "Runtime image and manifests prepared",
                }
            )
        return service.model_copy(
            update={"state": "blocked", "detail": _bootstrap_blocked_detail(report)}
        )
    return service


def _bootstrap_blocked_detail(report) -> str:
    if report.next_actions:
        return report.next_actions[0]
    return "Remote bootstrap did not complete."


def _state_root(workspace_root: str, *, project_name: str, task_id: str) -> str:
    root = workspace_root.rstrip("/") or "/"
    if posixpath.basename(root) == "workspaces":
        base = posixpath.join(posixpath.dirname(root), "runs")
    else:
        base = posixpath.join(root, ".openevo-runs")
    return posixpath.join(base, _slugify(project_name), _slugify(task_id))


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "item"
