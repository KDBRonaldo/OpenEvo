from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from _thread import LockType
from pathlib import Path
import posixpath
import re
import secrets
import shlex
from typing import Any, Literal
from threading import Lock, Thread

from fastapi import FastAPI, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic import ValidationError

from openevo.remote.executor import RemoteExecutorTransport
from openevo.science import ScienceProjectConfig
from openevo.sidecar.config import (
    DesktopProjectConfigDraft,
    DesktopProjectConfigPaths,
    DesktopProjectConfigSummary,
    list_desktop_project_configs,
    load_desktop_project_config,
    save_desktop_project_config,
)
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
    http_proxy: str | None = None
    https_proxy: str | None = None
    no_proxy: str | None = None
    pip_index_url: str | None = None
    huggingface_endpoint: str | None = None
    hf_home: str | None = None


class DesktopRemoteAuth(_StrictFrozenModel):
    method: Literal["ssh_agent", "private_key", "password_ref"] = "ssh_agent"
    private_key_path: str | None = None
    password_ref: str | None = None
    passphrase_ref: str | None = None


class DesktopRemoteProfile(_StrictFrozenModel):
    id: str
    host: str
    port: int = Field(default=22, ge=1, le=65535)
    user: str
    auth: DesktopRemoteAuth = Field(default_factory=DesktopRemoteAuth)
    workspace_root: str
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


class OpenEvoDesktopWorkspaceResponse(_StrictFrozenModel):
    workspace: dict[str, Any]
    report: dict[str, Any]
    status: OpenEvoDesktopShellStatus


class OpenEvoDesktopProjectConfigResponse(_StrictFrozenModel):
    config: DesktopProjectConfigPaths
    status: OpenEvoDesktopShellStatus


class OpenEvoDesktopProjectConfigsResponse(_StrictFrozenModel):
    configs: tuple[DesktopProjectConfigSummary, ...] = Field(default_factory=tuple)

    @field_validator("configs", mode="before")
    @classmethod
    def _coerce_configs(cls, value):
        if isinstance(value, list):
            return tuple(value)
        return value


class OpenEvoDesktopRunStatus(_StrictFrozenModel):
    id: str
    state: Literal["running", "succeeded", "failed"]
    ready: bool
    command: str
    return_code: int | None
    stdout: str
    stderr: str
    output_dir: str
    experiment_snapshot: str
    started_at: str
    finished_at: str | None

    @model_validator(mode="after")
    def _validate_ready_state(self) -> OpenEvoDesktopRunStatus:
        if self.ready != (self.state == "succeeded"):
            raise ValueError("ready must be true only for succeeded runs")
        if self.state == "running" and self.finished_at is not None:
            raise ValueError("running runs must not have finished_at")
        if self.state != "running" and self.finished_at is None:
            raise ValueError("terminal runs must have finished_at")
        return self


class OpenEvoDesktopRunResponse(_StrictFrozenModel):
    run: OpenEvoDesktopRunStatus
    status: OpenEvoDesktopShellStatus


@dataclass
class OpenEvoSidecarSession:
    project: ScienceProjectConfig
    profile: RemoteProfileConfig
    transport_factory: Callable[[RemoteProfileConfig], RemoteExecutorTransport]
    status: OpenEvoDesktopShellStatus
    last_workspace_report: object | None = None
    last_bootstrap_report: object | None = None
    latest_run: OpenEvoDesktopRunStatus | None = None
    status_lock: LockType = field(default_factory=Lock)
    workspace_lock: LockType = field(default_factory=Lock)
    bootstrap_lock: LockType = field(default_factory=Lock)
    run_lock: LockType = field(default_factory=Lock)


def build_desktop_shell_status(
    project: ScienceProjectConfig,
    profile: RemoteProfileConfig,
) -> OpenEvoDesktopShellStatus:
    sidecar_plan = build_sidecar_science_plan(project, profile)
    return OpenEvoDesktopShellStatus(
        remote=DesktopRemoteProfile(
            id=profile.id,
            host=profile.host,
            port=profile.port,
            user=profile.user,
            auth=DesktopRemoteAuth(
                method=profile.auth.method,
                private_key_path=profile.auth.private_key_path,
                password_ref=profile.auth.password_ref,
                passphrase_ref=profile.auth.passphrase_ref,
            ),
            workspace_root=profile.effective_workspace_root,
            proxy=DesktopRemoteProxy(
                http_proxy=profile.proxy.http_proxy,
                https_proxy=profile.proxy.https_proxy,
                no_proxy=profile.proxy.no_proxy,
                pip_index_url=profile.proxy.pip_index_url,
                huggingface_endpoint=profile.proxy.huggingface_endpoint,
                hf_home=profile.proxy.hf_home,
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
            port=22,
            user="alice",
            workspace_root="/home/alice/.openevo/workspaces",
            proxy=DesktopRemoteProxy(
                http_proxy=None,
                https_proxy="http://127.0.0.1:7890",
                no_proxy=None,
                pip_index_url=None,
                huggingface_endpoint="https://hf-mirror.com",
                hf_home=None,
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
    config_root: Path | None = None,
    transport_factory: Callable[[RemoteProfileConfig], RemoteExecutorTransport]
    | None = None,
) -> FastAPI:
    desktop_status = status or default_desktop_shell_status()
    sidecar_token = _mutation_token(mutation_token)
    session_pointer_lock = Lock()
    app = FastAPI(title="OpenEvo Desktop Sidecar", version="0.1.0")

    def current_session() -> OpenEvoSidecarSession | None:
        with session_pointer_lock:
            return session

    @app.get("/health", response_model=SidecarHealth)
    def health() -> SidecarHealth:
        return SidecarHealth()

    @app.get(
        "/openevo-api/desktop/shell",
        response_model=OpenEvoDesktopShellStatus,
    )
    def desktop_shell() -> OpenEvoDesktopShellStatus:
        active_session = current_session()
        if active_session is not None:
            with active_session.status_lock:
                return _status_with_mutation_token(
                    active_session.status,
                    sidecar_token,
                )
        return _status_with_mutation_token(desktop_status, sidecar_token)

    @app.post(
        "/openevo-api/desktop/project-config",
        response_model=OpenEvoDesktopProjectConfigResponse,
    )
    def project_config(
        payload: dict[str, Any],
        token: str | None = Header(
            default=None,
            alias=SIDECAR_MUTATION_TOKEN_HEADER,
        ),
    ) -> OpenEvoDesktopProjectConfigResponse:
        nonlocal session
        _validate_mutation_token(token, sidecar_token)
        if config_root is None or transport_factory is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Desktop project config requires a writable config root "
                    "and transport factory."
                ),
            )
        try:
            draft = DesktopProjectConfigDraft.model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail=_validation_error_detail(exc),
            ) from exc
        with session_pointer_lock:
            if session is not None and _session_lifecycle_busy(session):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Desktop project config cannot run while another "
                        "lifecycle action is running."
                    ),
                )
            try:
                project, profile, paths = save_desktop_project_config(
                    draft,
                    config_root,
                )
            except ValidationError as exc:
                raise HTTPException(
                    status_code=422,
                    detail=_validation_error_detail(exc),
                ) from exc
            new_session = OpenEvoSidecarSession(
                project=project,
                profile=profile,
                transport_factory=transport_factory,
                status=build_desktop_shell_status(project, profile),
            )
            session = new_session
        with new_session.status_lock:
            response_status = _status_with_mutation_token(
                new_session.status,
                sidecar_token,
            )
        return OpenEvoDesktopProjectConfigResponse(
            config=paths,
            status=response_status,
        )

    @app.get(
        "/openevo-api/desktop/project-configs",
        response_model=OpenEvoDesktopProjectConfigsResponse,
    )
    def project_config_catalog() -> OpenEvoDesktopProjectConfigsResponse:
        if config_root is None:
            raise HTTPException(
                status_code=409,
                detail="Desktop project config catalog requires a writable config root.",
            )
        return OpenEvoDesktopProjectConfigsResponse(
            configs=list_desktop_project_configs(config_root),
        )

    @app.post(
        "/openevo-api/desktop/project-configs/{project_slug}/activate",
        response_model=OpenEvoDesktopProjectConfigResponse,
    )
    def activate_project_config(
        project_slug: str,
        token: str | None = Header(
            default=None,
            alias=SIDECAR_MUTATION_TOKEN_HEADER,
        ),
    ) -> OpenEvoDesktopProjectConfigResponse:
        nonlocal session
        _validate_mutation_token(token, sidecar_token)
        if config_root is None or transport_factory is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Desktop project config activation requires a writable "
                    "config root and transport factory."
                ),
            )
        with session_pointer_lock:
            if session is not None and _session_lifecycle_busy(session):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Desktop project config cannot activate while another "
                        "lifecycle action is running."
                    ),
                )
            try:
                project, profile, paths = load_desktop_project_config(
                    config_root,
                    project_slug,
                )
            except FileNotFoundError as exc:
                if str(exc) == "Saved Desktop project config not found.":
                    raise HTTPException(status_code=404, detail=str(exc)) from exc
                raise HTTPException(
                    status_code=422,
                    detail=_saved_config_error(exc, config_root),
                ) from exc
            except ValueError as exc:
                if str(exc) == "Invalid Desktop project slug.":
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                raise HTTPException(
                    status_code=422,
                    detail=_saved_config_error(exc, config_root),
                ) from exc
            new_session = OpenEvoSidecarSession(
                project=project,
                profile=profile,
                transport_factory=transport_factory,
                status=build_desktop_shell_status(project, profile),
            )
            session = new_session
        with new_session.status_lock:
            response_status = _status_with_mutation_token(
                new_session.status,
                sidecar_token,
            )
        return OpenEvoDesktopProjectConfigResponse(
            config=paths,
            status=response_status,
        )

    @app.post(
        "/openevo-api/desktop/workspace",
        response_model=OpenEvoDesktopWorkspaceResponse,
    )
    def workspace(
        token: str | None = Header(
            default=None,
            alias=SIDECAR_MUTATION_TOKEN_HEADER,
        ),
    ) -> OpenEvoDesktopWorkspaceResponse:
        _validate_mutation_token(token, sidecar_token)
        with session_pointer_lock:
            active_session = session
            if active_session is None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Desktop workspace sync requires a config-backed sidecar "
                        "session."
                    ),
                )
            if not active_session.workspace_lock.acquire(blocking=False):
                raise HTTPException(
                    status_code=409,
                    detail="Desktop workspace sync is already running.",
                )
        try:
            report = _run_workspace_sync(active_session)
            with active_session.status_lock:
                active_session.last_workspace_report = report
                active_session.status = _status_after_workspace(
                    active_session.status,
                    report,
                )
                response_status = _status_with_mutation_token(
                    active_session.status,
                    sidecar_token,
                )
        finally:
            active_session.workspace_lock.release()
        return OpenEvoDesktopWorkspaceResponse(
            workspace=_workspace_response_payload(report),
            report=report.model_dump(mode="json"),
            status=response_status,
        )

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
        with session_pointer_lock:
            active_session = session
            if active_session is None:
                raise HTTPException(
                    status_code=409,
                    detail="Desktop bootstrap requires a config-backed sidecar session.",
                )
            if not active_session.bootstrap_lock.acquire(blocking=False):
                raise HTTPException(
                    status_code=409,
                    detail="Desktop bootstrap is already running.",
                )
        try:
            report = _run_bootstrap(active_session)
            with active_session.status_lock:
                active_session.last_bootstrap_report = report
                active_session.status = _status_after_bootstrap(
                    active_session.status,
                    report,
                )
                response_status = _status_with_mutation_token(
                    active_session.status,
                    sidecar_token,
                )
        finally:
            active_session.bootstrap_lock.release()
        return OpenEvoDesktopBootstrapResponse(
            bootstrap=response_status.bootstrap,
            report=report.model_dump(mode="json"),
            status=response_status,
        )

    @app.post(
        "/openevo-api/desktop/run",
        response_model=OpenEvoDesktopRunResponse,
    )
    def run(
        token: str | None = Header(
            default=None,
            alias=SIDECAR_MUTATION_TOKEN_HEADER,
        ),
    ) -> OpenEvoDesktopRunResponse:
        _validate_mutation_token(token, sidecar_token)
        with session_pointer_lock:
            active_session = session
            if active_session is None:
                raise HTTPException(
                    status_code=409,
                    detail="Desktop run launch requires a config-backed sidecar session.",
                )
            if not active_session.run_lock.acquire(blocking=False):
                raise HTTPException(
                    status_code=409,
                    detail="Desktop run launch is already running.",
                )
        try:
            experiment_snapshot, state_root = _run_ready_context(active_session)
            run_status = _initial_run_status(
                experiment_snapshot=experiment_snapshot,
                state_root=state_root,
            )
            with active_session.status_lock:
                active_session.latest_run = run_status
                active_session.status = _status_after_run(
                    active_session.status,
                    run_status,
                )
                response_status = _status_with_mutation_token(
                    active_session.status,
                    sidecar_token,
                )
            Thread(
                target=_finish_openevo_task_run,
                args=(active_session, run_status, state_root),
                daemon=True,
            ).start()
        except Exception:
            active_session.run_lock.release()
            raise
        return OpenEvoDesktopRunResponse(run=run_status, status=response_status)

    @app.get(
        "/openevo-api/desktop/run",
        response_model=OpenEvoDesktopRunResponse,
    )
    def latest_run(
        token: str | None = Header(
            default=None,
            alias=SIDECAR_MUTATION_TOKEN_HEADER,
        ),
    ) -> OpenEvoDesktopRunResponse:
        _validate_mutation_token(token, sidecar_token)
        active_session = current_session()
        if active_session is None:
            raise HTTPException(
                status_code=409,
                detail="Desktop run status requires a config-backed sidecar session.",
            )
        with active_session.status_lock:
            if active_session.latest_run is None:
                raise HTTPException(
                    status_code=404,
                    detail="No Desktop run has been launched.",
                )
            response_status = _status_with_mutation_token(
                active_session.status,
                sidecar_token,
            )
            return OpenEvoDesktopRunResponse(
                run=active_session.latest_run,
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


def _run_workspace_sync(session: OpenEvoSidecarSession):
    from openevo.remote.executor import execute_sidecar_plan

    sidecar_plan = build_sidecar_science_plan(session.project, session.profile)
    return execute_sidecar_plan(
        sidecar_plan,
        session.transport_factory(session.profile),
    )


def _run_ready_context(session: OpenEvoSidecarSession) -> tuple[str, str]:
    with session.status_lock:
        workspace_ready = any(
            service.id == "workspace" and service.state == "ready"
            for service in session.status.services
        )
        bootstrap_ready = session.status.bootstrap.ready
        bootstrap_report = session.last_bootstrap_report
    if not workspace_ready or not bootstrap_ready or bootstrap_report is None:
        raise HTTPException(
            status_code=409,
            detail="Desktop run launch requires ready workspace and bootstrap.",
        )
    prepared_paths = getattr(bootstrap_report, "prepared_paths", {})
    experiment_snapshot = prepared_paths.get("experiment_snapshot")
    state_root = prepared_paths.get("state_root")
    if not experiment_snapshot or not state_root:
        raise HTTPException(
            status_code=409,
            detail="Desktop run launch requires ready workspace and bootstrap.",
        )
    return experiment_snapshot, state_root


def _initial_run_status(
    *,
    experiment_snapshot: str,
    state_root: str,
) -> OpenEvoDesktopRunStatus:
    run_id = "run_" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    output_dir = posixpath.join(state_root, "runs", run_id)
    command = (
        f"openevo run {shlex.quote(experiment_snapshot)} "
        f"--output-dir {shlex.quote(output_dir)} --json"
    )
    return OpenEvoDesktopRunStatus(
        id=run_id,
        state="running",
        ready=False,
        command=command,
        return_code=None,
        stdout="",
        stderr="",
        output_dir=output_dir,
        experiment_snapshot=experiment_snapshot,
        started_at=datetime.now(timezone.utc).isoformat(),
        finished_at=None,
    )


def _finish_openevo_task_run(
    session: OpenEvoSidecarSession,
    started: OpenEvoDesktopRunStatus,
    state_root: str,
) -> None:
    try:
        finished = _run_openevo_task(
            session,
            started=started,
            state_root=state_root,
        )
        with session.status_lock:
            if session.latest_run is not None and session.latest_run.id == started.id:
                session.latest_run = finished
                session.status = _status_after_run(session.status, finished)
    finally:
        session.run_lock.release()


def _run_openevo_task(
    session: OpenEvoSidecarSession,
    *,
    started: OpenEvoDesktopRunStatus,
    state_root: str,
) -> OpenEvoDesktopRunStatus:
    try:
        result = session.transport_factory(session.profile).run(
            started.command,
            cwd=state_root,
            timeout_seconds=86400.0,
        )
    except Exception as exc:
        return started.model_copy(
            update={
                "state": "failed",
                "ready": False,
                "return_code": None,
                "stderr": str(exc),
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    ready = result.return_code == 0
    return started.model_copy(
        update={
            "state": "succeeded" if ready else "failed",
            "ready": ready,
            "return_code": result.return_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
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


def _validation_error_detail(exc: ValidationError):
    return jsonable_encoder(exc.errors(include_input=False))


def _saved_config_error(exc: Exception, config_root: Path) -> str:
    if isinstance(exc, ValidationError):
        return f"Saved Desktop project config is invalid: {_validation_error_summary(exc)}"
    text = str(exc)
    root = config_root.expanduser()
    for prefix in (f"{root.as_posix()}/", str(root) + "/", root.as_posix(), str(root)):
        text = text.replace(prefix, "")
    return f"Saved Desktop project config is invalid: {text}"


def _validation_error_summary(exc: ValidationError) -> str:
    messages: list[str] = []
    for error in exc.errors(include_input=False):
        loc = ".".join(str(part) for part in error.get("loc", ()))
        message = str(error.get("msg", "Invalid value"))
        if loc:
            messages.append(f"{loc}: {message}")
        else:
            messages.append(message)
    return "; ".join(messages) or "Invalid config"


def _session_lifecycle_busy(session: OpenEvoSidecarSession) -> bool:
    return (
        session.workspace_lock.locked()
        or session.bootstrap_lock.locked()
        or session.run_lock.locked()
    )


def _status_with_mutation_token(
    status: OpenEvoDesktopShellStatus,
    token: str,
) -> OpenEvoDesktopShellStatus:
    return status.model_copy(
        update={"sidecar": DesktopSidecarSecurity(mutation_token=token)}
    )


def _workspace_response_payload(report) -> dict[str, Any]:
    payload = report.workspace.model_dump(mode="json")
    payload["ready"] = bool(report.ready)
    return payload


def _status_after_workspace(
    status: OpenEvoDesktopShellStatus,
    report,
) -> OpenEvoDesktopShellStatus:
    return status.model_copy(
        update={
            "services": tuple(
                _service_after_workspace(service, report=report)
                for service in status.services
            ),
        }
    )


def _service_after_workspace(
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
    if service.id == "workspace":
        if report.ready:
            detail = (
                service.detail
                if service.state == "ready"
                else "Workspace prepared"
            )
            return service.model_copy(update={"state": "ready", "detail": detail})
        return service.model_copy(
            update={"state": "blocked", "detail": _workspace_blocked_detail(report)}
        )
    return service


def _workspace_blocked_detail(report) -> str:
    if report.preflight is not None and not report.preflight.ready:
        return "Remote preflight failed"
    for action in report.workspace.actions:
        if action.status == "fail":
            return action.message
    return "Workspace preparation did not complete."


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


def _status_after_run(
    status: OpenEvoDesktopShellStatus,
    report: OpenEvoDesktopRunStatus,
) -> OpenEvoDesktopShellStatus:
    return status.model_copy(
        update={
            "services": tuple(
                _service_after_run(service, report=report)
                for service in status.services
            ),
            "evolution": tuple(
                _evolution_after_run(step, report=report)
                for step in status.evolution
            ),
        }
    )


def _service_after_run(
    service: DesktopServiceStatus,
    *,
    report: OpenEvoDesktopRunStatus,
) -> DesktopServiceStatus:
    if service.id != "openevo-backend":
        return service
    if report.state == "running":
        return service.model_copy(
            update={"state": "running", "detail": "OpenEvo run is running"}
        )
    if report.ready:
        return service.model_copy(
            update={"state": "ready", "detail": "Last run completed"}
        )
    return service.model_copy(
        update={"state": "blocked", "detail": _run_blocked_detail(report)}
    )


def _evolution_after_run(
    step: DesktopEvolutionStep,
    *,
    report: OpenEvoDesktopRunStatus,
) -> DesktopEvolutionStep:
    if step.id != "transcript":
        return step
    if report.state == "running":
        return step.model_copy(
            update={"state": "running", "detail": "Capturing transcript trajectory"}
        )
    if report.ready:
        return step.model_copy(
            update={
                "state": "complete",
                "detail": "Run completed and transcript captured",
            }
        )
    return step.model_copy(
        update={"state": "blocked", "detail": _run_blocked_detail(report)}
    )


def _run_blocked_detail(report: OpenEvoDesktopRunStatus) -> str:
    stderr = report.stderr.strip()
    if stderr:
        return stderr
    stdout = report.stdout.strip()
    if stdout:
        return stdout
    return "Run launch failed"


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
