from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from _thread import LockType
import json
from pathlib import Path
import posixpath
import re
import secrets
import shlex
from typing import Any, Literal
from threading import Lock, Thread
from urllib.parse import quote, unquote, urlparse

from fastapi import FastAPI, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic import ValidationError

from openevo.core.capabilities import (
    CoreCapabilities,
    EvolutionMethodCapability,
    build_core_capabilities,
)
from openevo.remote.executor import RemoteExecutorTransport
from openevo.remote.redaction import sanitize_remote_text
from openevo.science import ScienceProjectConfig
from openevo.sidecar.config import (
    DesktopProjectConfigDraft,
    DesktopProjectConfigPaths,
    DesktopProjectConfigSummary,
    list_desktop_project_configs,
    load_desktop_project_config,
    save_desktop_project_config,
)
from openevo.sidecar.models import (
    DesktopExecutionMode,
    RemoteProfileConfig,
    normalize_desktop_execution_mode,
)
from openevo.sidecar.planner import build_sidecar_science_plan

SIDECAR_MUTATION_TOKEN_HEADER = "X-OpenEvo-Sidecar-Token"
SidecarTransportKind = Literal["dry-run", "ssh"]


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
    mode: DesktopExecutionMode
    model: str
    token_metrics_available: bool

    @field_validator("mode", mode="before")
    @classmethod
    def _normalize_mode(cls, value):
        return normalize_desktop_execution_mode(value)

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


class DesktopSidecarTransport(_StrictFrozenModel):
    id: SidecarTransportKind = "dry-run"
    label: str = "Dry-run transport"
    supports_password_ref: bool = True
    supports_passphrase_ref: bool = True


class DesktopSidecarSecurity(_StrictFrozenModel):
    mutation_token: str | None = None
    transport: DesktopSidecarTransport = Field(default_factory=DesktopSidecarTransport)

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


class OpenEvoDesktopServicesResponse(_StrictFrozenModel):
    services: dict[str, Any]
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


class OpenEvoDesktopRunArtifactJob(_StrictFrozenModel):
    artifact_type: str
    method: str
    worker_status: str
    artifact_ids: list[str] = Field(default_factory=list)
    approved_artifact_ids: list[str] = Field(default_factory=list)
    promotion_status: str


class OpenEvoDesktopRunArtifactRound(_StrictFrozenModel):
    round_index: int
    policy_version: str | None = None
    rollout_status: str | None = None
    dataset_status: str | None = None
    artifact_ids: dict[str, list[str]] = Field(default_factory=dict)
    jobs: list[OpenEvoDesktopRunArtifactJob] = Field(default_factory=list)


class OpenEvoDesktopRunArtifactTask(_StrictFrozenModel):
    task_id: str
    rounds: list[OpenEvoDesktopRunArtifactRound] = Field(default_factory=list)


class OpenEvoDesktopRunArtifactsResponse(_StrictFrozenModel):
    run_id: str
    output_dir: str
    summary_status: str | None = None
    experiment_id: str | None = None
    experiment_name: str | None = None
    round_count: int | None = None
    tasks: list[OpenEvoDesktopRunArtifactTask] = Field(default_factory=list)


class OpenEvoDesktopMethodsResponse(_StrictFrozenModel):
    methods: tuple[EvolutionMethodCapability, ...] = Field(default_factory=tuple)

    @field_validator("methods", mode="before")
    @classmethod
    def _coerce_methods(cls, value):
        if isinstance(value, list):
            return tuple(value)
        return value


class OpenEvoDesktopArtifactContent(_StrictFrozenModel):
    artifact_id: str
    artifact_type: str
    filename: str
    content: str
    mime_type: str = "text/markdown"


@dataclass
class OpenEvoSidecarSession:
    project: ScienceProjectConfig
    profile: RemoteProfileConfig
    transport_factory: Callable[[RemoteProfileConfig], RemoteExecutorTransport]
    status: OpenEvoDesktopShellStatus
    last_workspace_report: object | None = None
    last_bootstrap_report: object | None = None
    last_services_report: object | None = None
    latest_run: OpenEvoDesktopRunStatus | None = None
    status_lock: LockType = field(default_factory=Lock)
    workspace_lock: LockType = field(default_factory=Lock)
    bootstrap_lock: LockType = field(default_factory=Lock)
    services_lock: LockType = field(default_factory=Lock)
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
                detail="Remote runtime services have not started",
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
    transport_kind: SidecarTransportKind = "dry-run",
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
                    transport_kind=transport_kind,
                )
        return _status_with_mutation_token(
            desktop_status,
            sidecar_token,
            transport_kind=transport_kind,
        )

    @app.get(
        "/openevo-api/desktop/capabilities",
        response_model=CoreCapabilities,
    )
    def desktop_capabilities() -> CoreCapabilities:
        return build_core_capabilities()

    @app.get(
        "/openevo-api/desktop/methods",
        response_model=OpenEvoDesktopMethodsResponse,
    )
    def desktop_methods() -> OpenEvoDesktopMethodsResponse:
        return OpenEvoDesktopMethodsResponse(
            methods=build_core_capabilities().evolution_methods,
        )

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
                transport_kind=transport_kind,
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
                transport_kind=transport_kind,
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
            _raise_for_unsupported_lifecycle_auth(
                active_session.profile,
                transport_kind,
            )
            if not active_session.workspace_lock.acquire(blocking=False):
                raise HTTPException(
                    status_code=409,
                    detail="Desktop workspace sync is already running.",
                )
            if _other_lifecycle_busy(active_session, excluding="workspace"):
                active_session.workspace_lock.release()
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Desktop workspace sync cannot start while another "
                        "lifecycle action is running."
                    ),
                )
        try:
            report = _run_workspace_sync(active_session)
            with active_session.status_lock:
                active_session.last_workspace_report = report
                active_session.last_services_report = None
                active_session.latest_run = None
                active_session.status = _status_after_workspace(
                    _status_reset_runtime_services(active_session.status),
                    report,
                )
                response_status = _status_with_mutation_token(
                    active_session.status,
                    sidecar_token,
                    transport_kind=transport_kind,
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
            _raise_for_unsupported_lifecycle_auth(
                active_session.profile,
                transport_kind,
            )
            if not active_session.bootstrap_lock.acquire(blocking=False):
                raise HTTPException(
                    status_code=409,
                    detail="Desktop bootstrap is already running.",
                )
            if _other_lifecycle_busy(active_session, excluding="bootstrap"):
                active_session.bootstrap_lock.release()
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Desktop bootstrap cannot start while another lifecycle "
                        "action is running."
                    ),
                )
        try:
            report = _run_bootstrap(active_session)
            with active_session.status_lock:
                active_session.last_bootstrap_report = report
                active_session.last_services_report = None
                active_session.latest_run = None
                active_session.status = _status_after_bootstrap(
                    _status_reset_runtime_services(active_session.status),
                    report,
                )
                response_status = _status_with_mutation_token(
                    active_session.status,
                    sidecar_token,
                    transport_kind=transport_kind,
                )
        finally:
            active_session.bootstrap_lock.release()
        return OpenEvoDesktopBootstrapResponse(
            bootstrap=response_status.bootstrap,
            report=report.model_dump(mode="json"),
            status=response_status,
        )

    @app.post(
        "/openevo-api/desktop/services",
        response_model=OpenEvoDesktopServicesResponse,
    )
    def services(
        token: str | None = Header(
            default=None,
            alias=SIDECAR_MUTATION_TOKEN_HEADER,
        ),
    ) -> OpenEvoDesktopServicesResponse:
        _validate_mutation_token(token, sidecar_token)
        with session_pointer_lock:
            active_session = session
            if active_session is None:
                raise HTTPException(
                    status_code=409,
                    detail="Desktop services require a config-backed sidecar session.",
                )
            _raise_for_unsupported_lifecycle_auth(
                active_session.profile,
                transport_kind,
            )
            if not active_session.services_lock.acquire(blocking=False):
                raise HTTPException(
                    status_code=409,
                    detail="Desktop services are already running.",
                )
            if _other_lifecycle_busy(active_session, excluding="services"):
                active_session.services_lock.release()
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Desktop services cannot start while another lifecycle "
                        "action is running."
                    ),
                )
        try:
            _require_workspace_and_bootstrap_ready(active_session)
            with active_session.status_lock:
                active_session.last_services_report = None
                active_session.latest_run = None
                active_session.status = _status_services_running(
                    active_session.status
                )
            report = _run_services(active_session)
            with active_session.status_lock:
                active_session.last_services_report = report
                active_session.status = _status_after_services(
                    active_session.status,
                    report,
                )
                response_status = _status_with_mutation_token(
                    active_session.status,
                    sidecar_token,
                    transport_kind=transport_kind,
                )
        finally:
            active_session.services_lock.release()
        return OpenEvoDesktopServicesResponse(
            services=_services_response_payload(report),
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
            _raise_for_unsupported_lifecycle_auth(
                active_session.profile,
                transport_kind,
            )
            if not active_session.run_lock.acquire(blocking=False):
                raise HTTPException(
                    status_code=409,
                    detail="Desktop run launch is already running.",
                )
            if _other_lifecycle_busy(active_session, excluding="run"):
                active_session.run_lock.release()
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Desktop run launch requires ready workspace, bootstrap, "
                        "and services."
                    ),
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
                    transport_kind=transport_kind,
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
                transport_kind=transport_kind,
            )
            return OpenEvoDesktopRunResponse(
                run=active_session.latest_run,
                status=response_status,
            )

    @app.get(
        "/openevo-api/desktop/run/artifacts",
        response_model=OpenEvoDesktopRunArtifactsResponse,
    )
    def latest_run_artifacts(
        token: str | None = Header(
            default=None,
            alias=SIDECAR_MUTATION_TOKEN_HEADER,
        ),
    ) -> OpenEvoDesktopRunArtifactsResponse:
        _validate_mutation_token(token, sidecar_token)
        active_session = current_session()
        if active_session is None:
            raise HTTPException(
                status_code=409,
                detail="Desktop run artifacts require a config-backed sidecar session.",
            )
        with active_session.status_lock:
            if active_session.latest_run is None:
                raise HTTPException(
                    status_code=404,
                    detail="No Desktop run has been launched.",
                )
            latest = active_session.latest_run
        if latest.state == "running":
            raise HTTPException(
                status_code=409,
                detail="Desktop run artifacts require a terminal run.",
            )
        summary = _read_remote_run_summary(active_session, latest.output_dir)
        return _run_artifacts_response(latest, summary)

    @app.get(
        "/openevo-api/desktop/artifacts/{artifact_id}/content",
        response_model=OpenEvoDesktopArtifactContent,
    )
    def artifact_content(
        artifact_id: str,
        token: str | None = Header(
            default=None,
            alias=SIDECAR_MUTATION_TOKEN_HEADER,
        ),
    ) -> OpenEvoDesktopArtifactContent:
        _validate_mutation_token(token, sidecar_token)
        active_session = current_session()
        if active_session is None:
            raise HTTPException(
                status_code=409,
                detail="Desktop artifact content requires a config-backed sidecar session.",
            )
        with active_session.status_lock:
            if active_session.latest_run is None:
                raise HTTPException(
                    status_code=404,
                    detail="No Desktop run has been launched.",
                )
            latest = active_session.latest_run
        if latest.state == "running":
            raise HTTPException(
                status_code=409,
                detail="Desktop artifact content requires a terminal run.",
            )
        summary = _read_remote_run_summary(active_session, latest.output_dir)
        if not _summary_contains_artifact_id(summary, artifact_id):
            raise HTTPException(
                status_code=404,
                detail="Artifact not found in latest run summary.",
            )
        artifact = _read_remote_artifact_metadata(active_session, artifact_id)
        request = _artifact_content_request(artifact)
        content = _read_remote_artifact_content(active_session, request)
        return OpenEvoDesktopArtifactContent(
            artifact_id=artifact_id,
            artifact_type=request.artifact_type,
            filename=request.filename,
            content=content,
        )

    return app


def create_sidecar_app_for_project(
    project: ScienceProjectConfig,
    profile: RemoteProfileConfig,
    *,
    transport_factory: Callable[[RemoteProfileConfig], RemoteExecutorTransport],
    mutation_token: str | None = None,
    transport_kind: SidecarTransportKind = "dry-run",
) -> FastAPI:
    session = OpenEvoSidecarSession(
        project=project,
        profile=profile,
        transport_factory=transport_factory,
        status=build_desktop_shell_status(project, profile),
    )
    return create_sidecar_app(
        session=session,
        mutation_token=mutation_token,
        transport_kind=transport_kind,
    )


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
        # Project-only shell status has no run summary capture metadata yet.
        token_metrics_available=False,
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
            detail="Remote runtime services have not started",
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


def _run_services(session: OpenEvoSidecarSession):
    from openevo.remote.bootstrap import build_remote_bootstrap_plan
    from openevo.remote.services import (
        build_remote_services_plan,
        execute_remote_services_plan,
    )

    _require_workspace_and_bootstrap_ready(session)
    sidecar_plan = build_sidecar_science_plan(session.project, session.profile)
    services_plan = build_remote_services_plan(build_remote_bootstrap_plan(sidecar_plan))
    return execute_remote_services_plan(
        services_plan,
        session.transport_factory(session.profile),
    )


def _read_remote_run_summary(
    session: OpenEvoSidecarSession,
    output_dir: str,
) -> dict[str, Any]:
    transport = session.transport_factory(session.profile)
    command = _read_run_summary_command(output_dir)
    result = transport.run(command, timeout_seconds=30.0)
    env = session.profile.proxy.to_env()
    if not result.ok:
        detail = sanitize_remote_text(result.stderr or result.stdout, env).strip()
        if "summary not found" in detail.lower():
            raise HTTPException(
                status_code=404,
                detail="OpenEvo run summary not found.",
            )
        raise HTTPException(
            status_code=502,
            detail=detail or "Failed to read OpenEvo run summary.",
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=502,
            detail="OpenEvo run summary was not valid JSON.",
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=502,
            detail="OpenEvo run summary was not a JSON object.",
        )
    return payload


def _read_run_summary_command(output_dir: str) -> str:
    return "\n".join(
        [
            "python3 - <<'PY'",
            "from pathlib import Path",
            f"path = Path({output_dir!r}) / 'summary.json'",
            "if not path.is_file():",
            "    raise SystemExit('OpenEvo run summary not found.')",
            "print(path.read_text(encoding='utf-8'), end='')",
            "PY",
        ]
    )


@dataclass(frozen=True)
class _ArtifactContentReadRequest:
    artifact_type: str
    artifact_root: str
    relative_path: str
    filename: str


def _summary_contains_artifact_id(value: object, artifact_id: str) -> bool:
    if not isinstance(value, dict):
        return False
    for task in _dict_list(value.get("tasks")):
        for round_payload in _dict_list(task.get("rounds")):
            if _artifact_id_container_contains(
                round_payload.get("artifact_ids"),
                artifact_id,
            ):
                return True
            for job in _dict_list(round_payload.get("jobs")):
                if _artifact_id_container_contains(job.get("artifact_ids"), artifact_id):
                    return True
                if _artifact_id_container_contains(
                    job.get("approved_artifact_ids"),
                    artifact_id,
                ):
                    return True
    return False


def _artifact_id_container_contains(value: object, artifact_id: str) -> bool:
    if isinstance(value, str):
        return value == artifact_id
    if isinstance(value, list):
        return any(_artifact_id_container_contains(item, artifact_id) for item in value)
    if isinstance(value, dict):
        return any(
            _artifact_id_container_contains(item, artifact_id)
            for item in value.values()
        )
    return False


def _read_remote_artifact_metadata(
    session: OpenEvoSidecarSession,
    artifact_id: str,
) -> dict[str, Any]:
    transport = session.transport_factory(session.profile)
    command = _read_artifact_metadata_command(artifact_id)
    result = transport.run(command, timeout_seconds=30.0)
    env = session.profile.proxy.to_env()
    if not result.ok:
        detail = sanitize_remote_text(result.stderr or result.stdout, env).strip()
        if "not found" in detail.lower() or "404" in detail:
            raise HTTPException(
                status_code=404,
                detail="Artifact metadata not found.",
            )
        raise HTTPException(
            status_code=502,
            detail=detail or "Failed to read artifact metadata.",
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=502,
            detail="Artifact metadata was not valid JSON.",
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=502,
            detail="Artifact metadata was not a JSON object.",
        )
    return payload


def _read_artifact_metadata_command(artifact_id: str) -> str:
    encoded_artifact_id = quote(artifact_id, safe="")
    url = f"http://127.0.0.1:8200/v1/artifacts/{encoded_artifact_id}"
    return "\n".join(
        [
            "python3 - <<'PY'",
            "import sys",
            "import urllib.error",
            "import urllib.request",
            f"url = {url!r}",
            "try:",
            "    with urllib.request.urlopen(url, timeout=10) as response:",
            "        body = response.read().decode('utf-8')",
            "except urllib.error.HTTPError as exc:",
            "    if exc.code == 404:",
            "        raise SystemExit('Artifact metadata not found.')",
            (
                "    raise SystemExit("
                "f'Artifact metadata fetch failed: HTTP {exc.code}')"
            ),
            "except urllib.error.URLError as exc:",
            (
                "    raise SystemExit("
                "f'Artifact metadata fetch failed: {exc.reason}')"
            ),
            "sys.stdout.write(body)",
            "PY",
        ]
    )


def _artifact_content_request(
    artifact: dict[str, Any],
) -> _ArtifactContentReadRequest:
    artifact_type = _optional_string(artifact.get("type")) or _optional_string(
        artifact.get("artifact_type")
    )
    if artifact_type not in {"text_memory", "skill_bundle", "agent_system"}:
        raise HTTPException(
            status_code=422,
            detail=(
                "Artifact content is only supported for text_memory, "
                "skill_bundle, and agent_system artifacts."
            ),
        )
    uri = _optional_string(artifact.get("uri"))
    if uri is None:
        raise HTTPException(
            status_code=422,
            detail="Artifact summary does not include a file URI.",
        )
    uri_path = _file_uri_path(uri)
    relative_path = _artifact_relative_content_path(
        artifact,
        artifact_type=artifact_type,
        uri_path=uri_path,
    )
    _validate_relative_artifact_path(relative_path)
    artifact_root = _artifact_root_path(
        uri_path=uri_path,
        relative_path=relative_path,
    )
    return _ArtifactContentReadRequest(
        artifact_type=artifact_type,
        artifact_root=artifact_root,
        relative_path=relative_path,
        filename=posixpath.basename(relative_path),
    )


def _file_uri_path(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise HTTPException(
            status_code=422,
            detail="Artifact URI must use the file scheme.",
        )
    path = unquote(parsed.path)
    if not path.startswith("/"):
        raise HTTPException(
            status_code=422,
            detail="Artifact file URI must contain an absolute path.",
        )
    return posixpath.normpath(path)


def _artifact_relative_content_path(
    artifact: dict[str, Any],
    *,
    artifact_type: str,
    uri_path: str,
) -> str:
    manifest = artifact.get("manifest")
    if isinstance(manifest, dict):
        content_path = manifest.get("content_path")
        if isinstance(content_path, str) and content_path.strip():
            return content_path.strip()
    if artifact_type == "text_memory":
        filename = posixpath.basename(uri_path)
        return filename if "." in filename else "memory.md"
    if artifact_type == "agent_system":
        if isinstance(manifest, dict):
            target_path = manifest.get("target_path")
            if isinstance(target_path, str) and target_path.strip():
                return posixpath.basename(target_path.strip())
        return "agent_system.md"
    return "SKILL.md"


def _artifact_root_path(
    *,
    uri_path: str,
    relative_path: str,
) -> str:
    normalized_relative = posixpath.normpath(relative_path)
    suffix = f"/{normalized_relative}"
    if uri_path.endswith(suffix):
        return uri_path[: -len(suffix)] or "/"
    return uri_path


def _validate_relative_artifact_path(relative_path: str) -> None:
    normalized = posixpath.normpath(relative_path)
    if (
        normalized in {"", "."}
        or posixpath.isabs(relative_path)
        or normalized == ".."
        or normalized.startswith("../")
    ):
        raise HTTPException(
            status_code=422,
            detail="Artifact content_path must stay within the artifact root.",
        )


def _read_remote_artifact_content(
    session: OpenEvoSidecarSession,
    request: _ArtifactContentReadRequest,
) -> str:
    transport = session.transport_factory(session.profile)
    command = _read_artifact_content_command(
        request.artifact_root,
        request.relative_path,
    )
    result = transport.run(command, timeout_seconds=30.0)
    env = session.profile.proxy.to_env()
    if not result.ok:
        detail = sanitize_remote_text(result.stderr or result.stdout, env).strip()
        if "not found" in detail.lower():
            raise HTTPException(
                status_code=404,
                detail="Artifact content file not found.",
            )
        if "stay within the artifact root" in detail:
            raise HTTPException(status_code=422, detail=detail)
        raise HTTPException(
            status_code=502,
            detail=detail or "Failed to read artifact content.",
        )
    return result.stdout


def _read_artifact_content_command(artifact_root: str, relative_path: str) -> str:
    return "\n".join(
        [
            "python3 - <<'PY'",
            "from pathlib import Path",
            f"root = Path({artifact_root!r}).resolve()",
            f"relative = Path({relative_path!r})",
            "if relative.is_absolute() or '..' in relative.parts:",
            (
                "    raise SystemExit("
                "'Artifact content_path must stay within the artifact root.')"
            ),
            "path = (root / relative).resolve()",
            "try:",
            "    path.relative_to(root)",
            "except ValueError:",
            (
                "    raise SystemExit("
                "'Artifact content_path must stay within the artifact root.')"
            ),
            "if not path.is_file():",
            "    raise SystemExit('Artifact content file not found.')",
            "print(path.read_text(encoding='utf-8'), end='')",
            "PY",
        ]
    )


def _run_workspace_sync(session: OpenEvoSidecarSession):
    from openevo.remote.executor import execute_sidecar_plan

    sidecar_plan = build_sidecar_science_plan(session.project, session.profile)
    return execute_sidecar_plan(
        sidecar_plan,
        session.transport_factory(session.profile),
    )


def _run_artifacts_response(
    latest: OpenEvoDesktopRunStatus,
    summary: dict[str, Any],
) -> OpenEvoDesktopRunArtifactsResponse:
    return OpenEvoDesktopRunArtifactsResponse(
        run_id=latest.id,
        output_dir=latest.output_dir,
        summary_status=_optional_string(summary.get("status")),
        experiment_id=_optional_string(summary.get("experiment_id")),
        experiment_name=_optional_string(summary.get("experiment_name")),
        round_count=_optional_int(summary.get("round_count")),
        tasks=[
            _artifact_task_summary(task)
            for task in _dict_list(summary.get("tasks"))
        ],
    )


def _artifact_task_summary(task: dict[str, Any]) -> OpenEvoDesktopRunArtifactTask:
    return OpenEvoDesktopRunArtifactTask(
        task_id=_string_value(task.get("task_id"), "unknown-task"),
        rounds=[
            _artifact_round_summary(round_payload)
            for round_payload in _dict_list(task.get("rounds"))
        ],
    )


def _artifact_round_summary(
    round_payload: dict[str, Any],
) -> OpenEvoDesktopRunArtifactRound:
    return OpenEvoDesktopRunArtifactRound(
        round_index=_int_value(round_payload.get("round_index"), 0),
        policy_version=_optional_string(round_payload.get("policy_version")),
        rollout_status=_optional_string(round_payload.get("rollout_status")),
        dataset_status=_optional_string(round_payload.get("dataset_status")),
        artifact_ids=_artifact_id_map(round_payload.get("artifact_ids")),
        jobs=[
            _artifact_job_summary(job_payload)
            for job_payload in _dict_list(round_payload.get("jobs"))
        ],
    )


def _artifact_job_summary(job: dict[str, Any]) -> OpenEvoDesktopRunArtifactJob:
    return OpenEvoDesktopRunArtifactJob(
        artifact_type=_string_value(job.get("artifact_type"), "unknown"),
        method=_string_value(job.get("method"), "unknown"),
        worker_status=_string_value(job.get("worker_status"), "unknown"),
        artifact_ids=_string_list(job.get("artifact_ids")),
        approved_artifact_ids=_string_list(job.get("approved_artifact_ids")),
        promotion_status=_string_value(job.get("promotion_status"), "unknown"),
    )


def _artifact_id_map(value: object) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): _string_list(item)
        for key, item in value.items()
        if isinstance(key, str)
    }


def _dict_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _string_value(value: object, default: str) -> str:
    return value if isinstance(value, str) and value else default


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _int_value(value: object, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _require_workspace_and_bootstrap_ready(session: OpenEvoSidecarSession) -> None:
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
            detail="Desktop services require ready workspace and bootstrap.",
        )


def _run_ready_context(session: OpenEvoSidecarSession) -> tuple[str, str]:
    with session.status_lock:
        workspace_ready = any(
            service.id == "workspace" and service.state == "ready"
            for service in session.status.services
        )
        bootstrap_ready = session.status.bootstrap.ready
        bootstrap_report = session.last_bootstrap_report
        services_report = session.last_services_report
    services_ready = bool(
        services_report is not None and getattr(services_report, "ready", False)
    )
    if (
        not workspace_ready
        or not bootstrap_ready
        or bootstrap_report is None
        or not services_ready
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "Desktop run launch requires ready workspace, bootstrap, "
                "and services."
            ),
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
        f'PATH="$HOME/.local/bin:$PATH" openevo run '
        f"{shlex.quote(experiment_snapshot)} "
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
        or session.services_lock.locked()
        or session.run_lock.locked()
    )


def _other_lifecycle_busy(
    session: OpenEvoSidecarSession,
    *,
    excluding: Literal["workspace", "bootstrap", "services", "run"],
) -> bool:
    locks = {
        "workspace": session.workspace_lock,
        "bootstrap": session.bootstrap_lock,
        "services": session.services_lock,
        "run": session.run_lock,
    }
    return any(name != excluding and lock.locked() for name, lock in locks.items())


def _status_with_mutation_token(
    status: OpenEvoDesktopShellStatus,
    token: str,
    *,
    transport_kind: SidecarTransportKind,
) -> OpenEvoDesktopShellStatus:
    return status.model_copy(
        update={
            "sidecar": DesktopSidecarSecurity(
                mutation_token=token,
                transport=_sidecar_transport_status(transport_kind),
            )
        }
    )


def _sidecar_transport_status(kind: SidecarTransportKind) -> DesktopSidecarTransport:
    if kind == "ssh":
        return DesktopSidecarTransport(
            id="ssh",
            label="SSH transport",
            supports_password_ref=False,
            supports_passphrase_ref=False,
        )
    return DesktopSidecarTransport(
        id="dry-run",
        label="Dry-run transport",
        supports_password_ref=True,
        supports_passphrase_ref=True,
    )


def _raise_for_unsupported_lifecycle_auth(
    profile: RemoteProfileConfig,
    transport_kind: SidecarTransportKind,
) -> None:
    detail = _unsupported_lifecycle_auth_detail(profile, transport_kind)
    if detail is not None:
        raise HTTPException(status_code=409, detail=detail)


def _unsupported_lifecycle_auth_detail(
    profile: RemoteProfileConfig,
    transport_kind: SidecarTransportKind,
) -> str | None:
    transport = _sidecar_transport_status(transport_kind)
    auth = profile.auth
    if auth.method == "password_ref" and not transport.supports_password_ref:
        return (
            f"{transport.label} cannot resolve password_ref yet. "
            "Use SSH agent or a private key without a secret reference."
        )
    if auth.passphrase_ref is not None and not transport.supports_passphrase_ref:
        return (
            f"{transport.label} cannot resolve passphrase_ref yet. "
            "Use SSH agent or a private key without a secret reference."
        )
    return None


def _workspace_response_payload(report) -> dict[str, Any]:
    payload = report.workspace.model_dump(mode="json")
    payload["ready"] = bool(report.ready)
    return payload


def _services_response_payload(report) -> dict[str, Any]:
    return {
        "ready": bool(report.ready),
        "state_root": report.state_root,
        "topology_path": report.topology_path,
    }


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


def _status_after_services(
    status: OpenEvoDesktopShellStatus,
    report,
) -> OpenEvoDesktopShellStatus:
    return status.model_copy(
        update={
            "services": tuple(
                _service_after_services(service, report=report)
                for service in status.services
            ),
        }
    )


def _status_services_running(
    status: OpenEvoDesktopShellStatus,
) -> OpenEvoDesktopShellStatus:
    return status.model_copy(
        update={
            "services": tuple(
                _service_services_running(service) for service in status.services
            ),
        }
    )


def _service_services_running(service: DesktopServiceStatus) -> DesktopServiceStatus:
    if service.id != "openevo-backend":
        return service
    return service.model_copy(
        update={
            "state": "running",
            "detail": "Starting remote runtime services",
        }
    )


def _service_after_services(
    service: DesktopServiceStatus,
    *,
    report,
) -> DesktopServiceStatus:
    if service.id != "openevo-backend":
        return service
    if report.ready:
        return service.model_copy(
            update={
                "state": "ready",
                "detail": "Remote runtime services are ready",
            }
        )
    return service.model_copy(
        update={"state": "blocked", "detail": _services_blocked_detail(report)}
    )


def _services_blocked_detail(report) -> str:
    for step in report.steps:
        if step.status == "fail":
            for text in (
                step.stderr,
                step.health_stderr,
                step.message,
                step.health_stdout,
                step.stdout,
            ):
                stripped = text.strip()
                if stripped:
                    return stripped
    if report.next_actions:
        return report.next_actions[0]
    return "Remote services did not become ready."


def _status_reset_runtime_services(
    status: OpenEvoDesktopShellStatus,
) -> OpenEvoDesktopShellStatus:
    return status.model_copy(
        update={
            "services": tuple(
                _reset_runtime_service(service) for service in status.services
            ),
        }
    )


def _reset_runtime_service(service: DesktopServiceStatus) -> DesktopServiceStatus:
    if service.id != "openevo-backend":
        return service
    return service.model_copy(
        update={
            "state": "planned",
            "detail": "Remote runtime services have not started",
        }
    )


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
