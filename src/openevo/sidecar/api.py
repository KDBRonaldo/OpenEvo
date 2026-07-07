from __future__ import annotations

from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
