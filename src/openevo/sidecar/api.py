from __future__ import annotations

import posixpath
import re
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openevo.science import ScienceProjectConfig
from openevo.sidecar.models import RemoteProfileConfig
from openevo.sidecar.planner import build_sidecar_science_plan


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


class OpenEvoDesktopShellStatus(_StrictFrozenModel):
    remote: DesktopRemoteProfile
    project: DesktopScienceProject
    execution: DesktopExecutionStatus
    bootstrap: DesktopBootstrapStatus
    services: tuple[DesktopServiceStatus, ...] = Field(default_factory=tuple)
    evolution: tuple[DesktopEvolutionStep, ...] = Field(default_factory=tuple)
    developer_mode: DesktopDeveloperMode = Field(default_factory=DesktopDeveloperMode)

    @field_validator("services", "evolution", mode="before")
    @classmethod
    def _coerce_tuples(cls, value):
        if isinstance(value, list):
            return tuple(value)
        return value


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
) -> FastAPI:
    desktop_status = status or default_desktop_shell_status()
    app = FastAPI(title="OpenEvo Desktop Sidecar", version="0.1.0")

    @app.get("/health", response_model=SidecarHealth)
    def health() -> SidecarHealth:
        return SidecarHealth()

    @app.get(
        "/openevo-api/desktop/shell",
        response_model=OpenEvoDesktopShellStatus,
    )
    def desktop_shell() -> OpenEvoDesktopShellStatus:
        return desktop_status

    return app


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
