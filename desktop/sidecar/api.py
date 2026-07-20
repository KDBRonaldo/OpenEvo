from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from _thread import LockType
import hashlib
import hmac
import json
from pathlib import Path
import posixpath
import re
import secrets
import shlex
import shutil
import tempfile
from typing import Any, Literal
from threading import Lock, Thread
from zipfile import BadZipFile, ZipFile
from email.parser import Parser

from fastapi import FastAPI, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic import ValidationError

import openevo
from openevo import __version__ as OPENEVO_VERSION
from openevo.backend.models import EvolutionProjectValidationResponse
from openevo.deployment.executor import RemoteExecutorTransport
from openevo.deployment.lifecycle import (
    RemoteServiceLog,
    RemoteServiceOperationResult,
    RemoteServicesStatus,
)
from openevo.deployment.preflight import PreflightCheck, PreflightReport, run_preflight
from openevo.deployment.redaction import sanitize_remote_text
from openevo.evolution.framework import (
    EvolutionCapabilitiesV1,
    FrameworkDistributionLock,
    ProjectEvolutionTargetSelection,
    execution_profile_for_release_mode,
    normalize_config_override,
)
from openevo.projects.science import ScienceProjectConfig
from openevo.projects.science.models import EvolutionTargetsConfig
from desktop.sidecar.config import (
    DesktopProjectConfigDraft,
    DesktopProjectConfigPaths,
    DesktopProjectConfigSummary,
    list_desktop_project_configs,
    load_desktop_project_config,
    save_desktop_project_config,
)
from desktop.sidecar.backend_client import (
    BackendClient,
    BackendConnection,
    DesktopBackendError,
)
from openevo.deployment.profile import (
    DesktopExecutionMode,
    RemoteProfileConfig,
    normalize_desktop_execution_mode,
)
from openevo.deployment.planner import build_sidecar_science_plan

SIDECAR_MUTATION_TOKEN_HEADER = "X-OpenEvo-Sidecar-Token"
SidecarTransportKind = Literal["dry-run", "ssh"]
NATIVE_SIDECAR_PROTOCOL = "openevo-native-sidecar-v1"


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
    protocol: Literal["openevo-native-sidecar-v1"] | None = None
    instance_id: str | None = None
    instance_proof: str | None = None


@dataclass(frozen=True)
class NativeSidecarInstance:
    instance_id: str
    readiness_key: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.instance_id) is not str
            or re.fullmatch(r"[0-9a-f]{32}", self.instance_id) is None
        ):
            raise ValueError("native instance id must be 32 lowercase hex characters")
        if type(self.readiness_key) is not bytes or len(self.readiness_key) != 32:
            raise ValueError("native readiness key must contain exactly 32 bytes")


def _native_readiness_domain(instance_id: str, challenge: str) -> bytes:
    return (f"{NATIVE_SIDECAR_PROTOCOL}\0{instance_id}\0{challenge}").encode("ascii")


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
    evolution_targets: dict[str, ProjectEvolutionTargetSelection]


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
        if self.mode == "codex_subscription_transcript" and self.token_metrics_available:
            raise ValueError(
                "token_metrics_available must be false for codex_subscription_transcript"
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


class PackagedCoreArtifactIdentity(_StrictFrozenModel):
    available: bool
    distribution: Literal["openevo"] = "openevo"
    distribution_version: str
    wheel_filename: str | None = None
    distribution_digest: str | None = None
    framework_lock: FrameworkDistributionLock | None = None


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


class OpenEvoDesktopServiceOperationRequest(_StrictFrozenModel):
    service_id: str

    @field_validator("service_id")
    @classmethod
    def _strip_service_id(cls, value: str) -> str:
        service_id = value.strip()
        if not service_id:
            raise ValueError("service_id must be a non-empty string")
        return service_id


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
    last_services_report: object | None = None
    latest_run: OpenEvoDesktopRunStatus | None = None
    backend_client: BackendClient | None = None
    backend_tunnel: Any | None = None
    backend_lock: LockType = field(default_factory=Lock)
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
            evolution_targets=dict(project.evolution.targets),
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
            id="not-configured",
            host="",
            port=22,
            user="",
            workspace_root="~/.openevo/workspaces",
        ),
        project=DesktopScienceProject(
            name="Untitled Science Project",
            task_id="new-task",
            source="Scratch workspace",
            objective="",
            evolution_targets=dict(EvolutionTargetsConfig().targets),
        ),
        execution=DesktopExecutionStatus(
            mode="codex_subscription_transcript",
            model="codex subscription on remote server",
            token_metrics_available=False,
        ),
        bootstrap=DesktopBootstrapStatus(
            ready=False,
            state_root="~/.openevo/runs/untitled-science-project/new-task",
            workspace_root="~/.openevo/workspaces",
            readiness_notes=("Configure a project and remote backend to begin.",),
        ),
        services=(
            DesktopServiceStatus(
                id="ssh",
                label="SSH transport",
                state="planned",
                detail="Configure a remote GPU server profile",
            ),
            DesktopServiceStatus(
                id="workspace",
                label="Workspace",
                state="planned",
                detail="Save project config before workspace sync",
            ),
            DesktopServiceStatus(
                id="bootstrap",
                label="Bootstrap",
                state="planned",
                detail="Run remote bootstrap after project config is saved",
            ),
            DesktopServiceStatus(
                id="openevo-backend",
                label="OpenEvo backend",
                state="planned",
                detail="Start backend after bootstrap is ready",
            ),
        ),
        evolution=(
            DesktopEvolutionStep(
                id="text-memory",
                label="Text memory",
                state="planned",
                detail="Memory updates appear after a run produces trajectories",
            ),
            DesktopEvolutionStep(
                id="skill-bundle",
                label="Skill bundle",
                state="planned",
                detail="Learned skills appear after evolution jobs complete",
            ),
            DesktopEvolutionStep(
                id="agent-system",
                label="Agent system",
                state="planned",
                detail="Instruction diffs appear after promoted artifacts exist",
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
    transport_factory: Callable[[RemoteProfileConfig], RemoteExecutorTransport] | None = None,
    backend_connection: BackendConnection | None = None,
    backend_client_factory: Callable[[], BackendClient] | None = None,
    native_instance: NativeSidecarInstance | None = None,
) -> FastAPI:
    desktop_status = status or default_desktop_shell_status()
    sidecar_token = _mutation_token(mutation_token)
    session_pointer_lock = Lock()
    shared_backend_client = (
        BackendClient(backend_connection)
        if backend_client_factory is None and backend_connection is not None
        else None
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            active_session = current_session()
            if active_session is not None:
                _close_session_backend(active_session)
            if shared_backend_client is not None:
                shared_backend_client.close()

    app = FastAPI(
        title="OpenEvo Desktop Sidecar",
        version=OPENEVO_VERSION,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=(
            r"^(https?://(localhost|127\.0\.0\.1):5173|"
            r"https?://tauri\.localhost(:\d+)?|"
            r"tauri://localhost)$"
        ),
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=False,
    )

    @app.exception_handler(DesktopBackendError)
    def desktop_backend_error_handler(
        _request,
        exc: DesktopBackendError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=jsonable_encoder(exc.error),
        )

    def current_session() -> OpenEvoSidecarSession | None:
        with session_pointer_lock:
            return session

    @contextmanager
    def backend_client_context(
        required_session: OpenEvoSidecarSession | None = None,
    ):
        if backend_client_factory is not None:
            client = backend_client_factory()
            try:
                yield client
            finally:
                if isinstance(client, BackendClient):
                    client.close()
            return
        active_session = required_session or current_session()
        if active_session is not None:
            with active_session.backend_lock:
                if active_session.backend_client is not None:
                    yield active_session.backend_client
                    return
            raise DesktopBackendError(
                409,
                {
                    "code": "backend_tunnel_not_configured",
                    "message": ("Desktop has no active tunnel to the remote OpenEvo backend."),
                    "severity": "blocking",
                    "category": "service",
                    "retryable": True,
                    "repair_action": "openevo_can_reconfigure",
                    "details": {},
                    "logs_ref": None,
                },
            )
        if shared_backend_client is not None:
            yield shared_backend_client
            return
        raise DesktopBackendError(
            409,
            {
                "code": "backend_tunnel_not_configured",
                "message": "Desktop has no active tunnel to the remote OpenEvo backend.",
                "severity": "blocking",
                "category": "service",
                "retryable": True,
                "repair_action": "openevo_can_reconfigure",
                "details": {},
                "logs_ref": None,
            },
        )

    def remote_capabilities(
        execution_mode: DesktopExecutionMode,
        *,
        required_session: OpenEvoSidecarSession | None = None,
    ) -> EvolutionCapabilitiesV1:
        with backend_client_context(required_session) as backend_client:
            payload = backend_client.capabilities(execution_mode)
        try:
            capabilities = EvolutionCapabilitiesV1.model_validate(payload)
        except ValidationError as exc:
            raise _invalid_backend_capabilities_error(execution_mode) from exc
        expected_profile = execution_profile_for_release_mode(execution_mode)
        if capabilities.evaluated_profile != expected_profile:
            raise _invalid_backend_capabilities_error(execution_mode)
        if any(
            target.exposure != "desktop"
            or any(method.exposure != "desktop" for method in target.methods)
            for target in capabilities.targets
        ):
            raise _invalid_backend_capabilities_error(execution_mode)
        return capabilities

    def validate_remote_project_evolution(
        project: ScienceProjectConfig,
        capabilities: EvolutionCapabilitiesV1,
        *,
        required_session: OpenEvoSidecarSession,
    ) -> None:
        agent_model = project.execution.codex_model or project.execution.hf_model
        if agent_model is None:  # ScienceProjectConfig makes this unreachable.
            raise _invalid_remote_project_validation_error()
        request = {
            "execution_mode": project.execution.mode,
            "expected_registry_digest": capabilities.registry_digest,
            "agent_model": agent_model,
            "reasoning_effort": project.execution.reasoning_effort,
            "targets": project.evolution.model_dump(mode="json")["targets"],
        }
        with backend_client_context(required_session) as backend_client:
            payload = backend_client.validate_evolution_project(request)
        try:
            response = EvolutionProjectValidationResponse.model_validate(
                payload,
                strict=True,
            )
        except ValidationError as exc:
            raise _invalid_remote_project_validation_error() from exc
        if response.registry_digest != capabilities.registry_digest:
            raise _invalid_remote_project_validation_error()

    @app.get(
        "/health",
        response_model=SidecarHealth,
        response_model_exclude_none=True,
    )
    def health(
        x_openevo_native_challenge: str | None = Header(default=None),
    ) -> SidecarHealth:
        if native_instance is None:
            return SidecarHealth()
        challenge = x_openevo_native_challenge or ""
        if re.fullmatch(r"[0-9a-f]{64}", challenge) is None:
            raise HTTPException(status_code=403, detail="Invalid native health challenge.")
        proof = hmac.new(
            native_instance.readiness_key,
            _native_readiness_domain(native_instance.instance_id, challenge),
            hashlib.sha256,
        ).hexdigest()
        return SidecarHealth(
            protocol=NATIVE_SIDECAR_PROTOCOL,
            instance_id=native_instance.instance_id,
            instance_proof=proof,
        )

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
        response_model=EvolutionCapabilitiesV1,
    )
    def desktop_capabilities(
        execution_mode: DesktopExecutionMode,
        token: str | None = Header(
            default=None,
            alias=SIDECAR_MUTATION_TOKEN_HEADER,
        ),
    ) -> EvolutionCapabilitiesV1:
        _validate_mutation_token(token, sidecar_token)
        return remote_capabilities(execution_mode)

    @app.get(
        "/openevo-api/desktop/core-artifact",
        response_model=PackagedCoreArtifactIdentity,
    )
    def desktop_core_artifact() -> PackagedCoreArtifactIdentity:
        return packaged_core_artifact_identity()

    @app.get("/openevo-api/backend/health")
    def backend_health(
        token: str | None = Header(
            default=None,
            alias=SIDECAR_MUTATION_TOKEN_HEADER,
        ),
    ) -> dict[str, Any]:
        _validate_mutation_token(token, sidecar_token)
        with backend_client_context() as backend_client:
            return backend_client.health()

    @app.get("/openevo-api/backend/status")
    def backend_status(
        token: str | None = Header(
            default=None,
            alias=SIDECAR_MUTATION_TOKEN_HEADER,
        ),
    ) -> dict[str, Any]:
        _validate_mutation_token(token, sidecar_token)
        with backend_client_context() as backend_client:
            return backend_client.status()

    @app.post("/openevo-api/backend/environment/doctor")
    def backend_environment_doctor(
        token: str | None = Header(
            default=None,
            alias=SIDECAR_MUTATION_TOKEN_HEADER,
        ),
    ) -> dict[str, Any]:
        _validate_mutation_token(token, sidecar_token)
        with backend_client_context() as backend_client:
            return backend_client.environment_doctor()

    @app.post("/openevo-api/backend/environment/repair")
    def backend_environment_repair(
        payload: dict[str, Any],
        token: str | None = Header(
            default=None,
            alias=SIDECAR_MUTATION_TOKEN_HEADER,
        ),
    ) -> dict[str, Any]:
        _validate_mutation_token(token, sidecar_token)
        actions = payload.get("actions", [])
        if not isinstance(actions, list) or not all(isinstance(action, str) for action in actions):
            raise HTTPException(
                status_code=422,
                detail="actions must be a list of strings.",
            )
        with backend_client_context() as backend_client:
            return backend_client.environment_repair(actions)

    @app.get("/openevo-api/backend/runs/{run_id:path}/timeline")
    def backend_run_timeline(
        run_id: str,
        token: str | None = Header(
            default=None,
            alias=SIDECAR_MUTATION_TOKEN_HEADER,
        ),
    ) -> list[dict[str, Any]]:
        _validate_mutation_token(token, sidecar_token)
        with backend_client_context() as backend_client:
            return backend_client.run_timeline(run_id)

    @app.get("/openevo-api/backend/runs/{run_id:path}/logs")
    def backend_run_logs(
        run_id: str,
        token: str | None = Header(
            default=None,
            alias=SIDECAR_MUTATION_TOKEN_HEADER,
        ),
    ) -> dict[str, Any]:
        _validate_mutation_token(token, sidecar_token)
        with backend_client_context() as backend_client:
            return backend_client.run_logs(run_id)

    @app.get("/openevo-api/backend/runs/{run_id:path}/artifacts")
    def backend_run_artifacts(
        run_id: str,
        token: str | None = Header(
            default=None,
            alias=SIDECAR_MUTATION_TOKEN_HEADER,
        ),
    ) -> list[dict[str, Any]]:
        _validate_mutation_token(token, sidecar_token)
        with backend_client_context() as backend_client:
            return backend_client.run_artifacts(run_id)

    @app.get("/openevo-api/backend/artifacts/{artifact_id:path}/content")
    def backend_artifact_content(
        artifact_id: str,
        token: str | None = Header(
            default=None,
            alias=SIDECAR_MUTATION_TOKEN_HEADER,
        ),
    ) -> dict[str, Any]:
        _validate_mutation_token(token, sidecar_token)
        with backend_client_context() as backend_client:
            return backend_client.artifact_content(artifact_id)

    @app.get("/openevo-api/backend/artifacts/{artifact_id:path}/diff")
    def backend_artifact_diff(
        artifact_id: str,
        token: str | None = Header(
            default=None,
            alias=SIDECAR_MUTATION_TOKEN_HEADER,
        ),
    ) -> dict[str, Any]:
        _validate_mutation_token(token, sidecar_token)
        with backend_client_context() as backend_client:
            return backend_client.artifact_diff(artifact_id)

    @app.get("/openevo-api/backend/services/{service_id:path}/logs")
    def backend_service_logs(
        service_id: str,
        token: str | None = Header(
            default=None,
            alias=SIDECAR_MUTATION_TOKEN_HEADER,
        ),
    ) -> dict[str, Any]:
        _validate_mutation_token(token, sidecar_token)
        with backend_client_context() as backend_client:
            return backend_client.service_logs(service_id)

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
                    "Desktop project config requires a writable config root and transport factory."
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
            old_session = session
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
        if old_session is not None:
            _close_session_backend(old_session)
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
            old_session = session
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
        if old_session is not None:
            _close_session_backend(old_session)
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
                    detail=("Desktop workspace sync requires a config-backed sidecar session."),
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
            _close_session_backend(active_session)
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
                        "Desktop bootstrap cannot start while another lifecycle action is running."
                    ),
                )
        try:
            report = _run_bootstrap(active_session)
            _close_session_backend(active_session)
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
                        "Desktop services cannot start while another lifecycle action is running."
                    ),
                )
        try:
            _require_workspace_and_bootstrap_ready(active_session)
            _close_session_backend(active_session)
            with active_session.status_lock:
                active_session.last_services_report = None
                active_session.latest_run = None
                active_session.status = _status_services_running(active_session.status)
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

    @app.get(
        "/openevo-api/desktop/services/status",
        response_model=RemoteServicesStatus,
    )
    def services_status(
        token: str | None = Header(
            default=None,
            alias=SIDECAR_MUTATION_TOKEN_HEADER,
        ),
    ) -> RemoteServicesStatus:
        _validate_mutation_token(token, sidecar_token)
        active_session = _active_ready_services_session(
            session,
            session_pointer_lock=session_pointer_lock,
            transport_kind=transport_kind,
        )
        return _inspect_remote_services(active_session)

    @app.get(
        "/openevo-api/desktop/services/health",
        response_model=RemoteServicesStatus,
    )
    def services_health(
        token: str | None = Header(
            default=None,
            alias=SIDECAR_MUTATION_TOKEN_HEADER,
        ),
    ) -> RemoteServicesStatus:
        _validate_mutation_token(token, sidecar_token)
        active_session = _active_ready_services_session(
            session,
            session_pointer_lock=session_pointer_lock,
            transport_kind=transport_kind,
        )
        return _inspect_remote_services(active_session)

    @app.get(
        "/openevo-api/desktop/services/logs",
        response_model=RemoteServiceLog,
    )
    def service_logs(
        service_id: str,
        lines: int = 200,
        token: str | None = Header(
            default=None,
            alias=SIDECAR_MUTATION_TOKEN_HEADER,
        ),
    ) -> RemoteServiceLog:
        _validate_mutation_token(token, sidecar_token)
        active_session = _active_ready_services_session(
            session,
            session_pointer_lock=session_pointer_lock,
            transport_kind=transport_kind,
        )
        try:
            return _read_remote_service_logs(active_session, service_id, lines=lines)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post(
        "/openevo-api/desktop/services/stop",
        response_model=RemoteServiceOperationResult,
    )
    def service_stop(
        payload: OpenEvoDesktopServiceOperationRequest,
        token: str | None = Header(
            default=None,
            alias=SIDECAR_MUTATION_TOKEN_HEADER,
        ),
    ) -> RemoteServiceOperationResult:
        _validate_mutation_token(token, sidecar_token)
        active_session = _active_ready_services_session(
            session,
            session_pointer_lock=session_pointer_lock,
            transport_kind=transport_kind,
        )
        if not active_session.services_lock.acquire(blocking=False):
            raise HTTPException(
                status_code=409,
                detail="Desktop services control is already running.",
            )
        if _other_lifecycle_busy(active_session, excluding="services"):
            active_session.services_lock.release()
            raise HTTPException(
                status_code=409,
                detail=(
                    "Desktop services control cannot run while another "
                    "lifecycle action is running."
                ),
            )
        try:
            return _stop_remote_service(active_session, payload.service_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        finally:
            active_session.services_lock.release()

    @app.post(
        "/openevo-api/desktop/services/restart",
        response_model=RemoteServiceOperationResult,
    )
    def service_restart(
        payload: OpenEvoDesktopServiceOperationRequest,
        token: str | None = Header(
            default=None,
            alias=SIDECAR_MUTATION_TOKEN_HEADER,
        ),
    ) -> RemoteServiceOperationResult:
        _validate_mutation_token(token, sidecar_token)
        active_session = _active_ready_services_session(
            session,
            session_pointer_lock=session_pointer_lock,
            transport_kind=transport_kind,
        )
        if not active_session.services_lock.acquire(blocking=False):
            raise HTTPException(
                status_code=409,
                detail="Desktop services control is already running.",
            )
        if _other_lifecycle_busy(active_session, excluding="services"):
            active_session.services_lock.release()
            raise HTTPException(
                status_code=409,
                detail=(
                    "Desktop services control cannot run while another "
                    "lifecycle action is running."
                ),
            )
        try:
            return _restart_remote_service(active_session, payload.service_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        finally:
            active_session.services_lock.release()

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
                        "Desktop run launch requires ready workspace, bootstrap, and services."
                    ),
                )
        try:
            experiment_snapshot, state_root = _run_ready_context(active_session)
            capabilities = remote_capabilities(
                active_session.project.execution.mode,
                required_session=active_session,
            )
            _validate_project_evolution_capabilities(
                active_session.project,
                capabilities,
            )
            validate_remote_project_evolution(
                active_session.project,
                capabilities,
                required_session=active_session,
            )
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

    return app


def create_sidecar_app_for_project(
    project: ScienceProjectConfig,
    profile: RemoteProfileConfig,
    *,
    transport_factory: Callable[[RemoteProfileConfig], RemoteExecutorTransport],
    mutation_token: str | None = None,
    transport_kind: SidecarTransportKind = "dry-run",
    backend_connection: BackendConnection | None = None,
    backend_client_factory: Callable[[], BackendClient] | None = None,
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
        backend_connection=backend_connection,
        backend_client_factory=backend_client_factory,
    )


def _execution_status(project: ScienceProjectConfig) -> DesktopExecutionStatus:
    if project.execution.mode == "codex_subscription_transcript":
        return DesktopExecutionStatus(
            mode=project.execution.mode,
            model=project.execution.codex_model or "gpt-5.5",
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
    targets = project.evolution.targets
    steps = [
        DesktopEvolutionStep(
            id="transcript",
            label="Transcript capture",
            state="planned",
            detail="Trajectory capture will start after the first run",
        )
    ]
    for target_id, selection in targets.items():
        if not selection.enabled:
            continue
        label = target_id.replace("_", " ").replace("-", " ").capitalize()
        steps.append(
            DesktopEvolutionStep(
                id=target_id.replace("_", "-"),
                label=label,
                state="planned",
                detail=f"No promoted {label.lower()} artifact yet",
            )
        )
    return tuple(steps)


def _run_bootstrap(session: OpenEvoSidecarSession):
    sidecar_plan = build_sidecar_science_plan(session.project, session.profile)
    return prepare_and_execute_remote_bootstrap(
        sidecar_plan,
        session.transport_factory(session.profile),
    )


def prepare_and_execute_remote_bootstrap(
    sidecar_plan,
    transport: RemoteExecutorTransport,
    *,
    expected_version: str = OPENEVO_VERSION,
    run_remote_preflight: bool = True,
):
    from openevo.deployment.bootstrap import (
        RemoteBootstrapReport,
        RemoteBootstrapStepExecution,
        RemoteBootstrapStepKind,
        RemoteBootstrapStepStatus,
        build_remote_bootstrap_plan,
        execute_remote_bootstrap_plan,
    )

    bootstrap_plan = build_remote_bootstrap_plan(
        sidecar_plan,
        expected_openevo_version=expected_version,
    )
    wheel = discover_local_openevo_wheel()
    preflight_report = None
    if wheel is not None and run_remote_preflight:
        preflight_report = _run_bootstrap_preflight(bootstrap_plan, transport)
        if not preflight_report.ready:
            return _bootstrap_report_for_preflight_failure(
                bootstrap_plan,
                preflight_report,
            )
    remote_wheel_path = None
    if wheel is not None:
        remote_wheel_dir = posixpath.join(bootstrap_plan.state_root, "wheels")
        try:
            with tempfile.TemporaryDirectory(prefix="openevo-wheel-upload-") as staging:
                staging_path = Path(staging)
                shutil.copy2(wheel, staging_path / wheel.name)
                framework_lock = _framework_distribution_lock(
                    wheel,
                    expected_version=expected_version,
                )
                (staging_path / "framework-lock.json").write_text(
                    framework_lock.model_dump_json(indent=2) + "\n",
                    encoding="utf-8",
                )
                transport.upload_dir(str(staging_path), remote_wheel_dir)
        except Exception as exc:
            message = sanitize_remote_text(str(exc), bootstrap_plan.proxy_env)
            return RemoteBootstrapReport(
                remote_profile_id=bootstrap_plan.remote_profile_id,
                project_name=bootstrap_plan.project_name,
                task_id=bootstrap_plan.task_id,
                preflight=preflight_report,
                steps=(
                    RemoteBootstrapStepExecution(
                        id="ensure_openevo_cli",
                        kind=RemoteBootstrapStepKind.CHECK_COMMAND,
                        status=RemoteBootstrapStepStatus.FAIL,
                        message="Bundled OpenEvo wheel upload failed.",
                        command=f"upload {wheel.name} to {remote_wheel_dir}",
                        required=True,
                        remediation_kind="upload_exact_openevo_wheel",
                        stderr=message,
                    ),
                ),
                prepared_paths=_bootstrap_prepared_paths(bootstrap_plan),
                next_actions=("Resolve failed bootstrap steps and rerun.",),
            )
        remote_wheel_path = posixpath.join(remote_wheel_dir, wheel.name)
        bootstrap_plan = build_remote_bootstrap_plan(
            sidecar_plan,
            expected_openevo_version=expected_version,
            bundled_wheel_remote_path=remote_wheel_path,
        )
    report = execute_remote_bootstrap_plan(
        bootstrap_plan,
        transport,
        run_remote_preflight=run_remote_preflight and preflight_report is None,
    )
    if preflight_report is not None:
        return report.model_copy(update={"preflight": preflight_report})
    return report


def _run_bootstrap_preflight(
    bootstrap_plan,
    transport: RemoteExecutorTransport,
) -> PreflightReport:
    try:
        return run_preflight(transport, bootstrap_plan.preflight)
    except Exception as exc:
        message = sanitize_remote_text(str(exc), bootstrap_plan.proxy_env)
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


def _bootstrap_report_for_preflight_failure(
    bootstrap_plan,
    preflight: PreflightReport,
):
    from openevo.deployment.bootstrap import RemoteBootstrapReport

    return RemoteBootstrapReport(
        remote_profile_id=bootstrap_plan.remote_profile_id,
        project_name=bootstrap_plan.project_name,
        task_id=bootstrap_plan.task_id,
        preflight=preflight,
        prepared_paths=_bootstrap_prepared_paths(bootstrap_plan),
        next_actions=("Fix remote preflight failures and rerun bootstrap.",),
    )


def _bootstrap_prepared_paths(bootstrap_plan) -> dict[str, str]:
    return {
        "state_root": bootstrap_plan.state_root,
        "workspace_root": bootstrap_plan.workspace_root,
        "experiment_snapshot": posixpath.join(
            bootstrap_plan.state_root,
            "experiment.json",
        ),
        "bootstrap_manifest": posixpath.join(
            bootstrap_plan.state_root,
            "bootstrap.json",
        ),
    }


def discover_local_openevo_wheel() -> Path | None:
    candidates: list[Path] = []
    for directory in _openevo_wheel_search_dirs():
        if directory.is_dir():
            candidates.extend(sorted(directory.glob(f"openevo-{OPENEVO_VERSION}-*.whl")))
    for candidate in candidates:
        if _is_valid_openevo_wheel(candidate, expected_version=OPENEVO_VERSION):
            return candidate
    return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _framework_distribution_lock(
    wheel: Path,
    *,
    expected_version: str,
) -> FrameworkDistributionLock:
    return FrameworkDistributionLock(
        distribution_version=expected_version,
        distribution_digest=_sha256_file(wheel),
        wheel_filename=wheel.name,
    )


def packaged_core_artifact_identity() -> PackagedCoreArtifactIdentity:
    wheel = discover_local_openevo_wheel()
    if wheel is None:
        return PackagedCoreArtifactIdentity(
            available=False,
            distribution_version=OPENEVO_VERSION,
        )
    framework_lock = _framework_distribution_lock(
        wheel,
        expected_version=OPENEVO_VERSION,
    )
    return PackagedCoreArtifactIdentity(
        available=True,
        distribution_version=OPENEVO_VERSION,
        wheel_filename=wheel.name,
        distribution_digest=framework_lock.distribution_digest,
        framework_lock=framework_lock,
    )


def _openevo_wheel_search_dirs() -> tuple[Path, ...]:
    package_root = Path(openevo.__file__).resolve().parent
    return (package_root / "wheels",)


def _is_valid_openevo_wheel(path: Path, *, expected_version: str) -> bool:
    try:
        with ZipFile(path) as wheel:
            metadata_name = _wheel_metadata_path(set(wheel.namelist()))
            if metadata_name is None:
                return False
            metadata = Parser().parsestr(wheel.read(metadata_name).decode("utf-8"))
    except (BadZipFile, KeyError, UnicodeDecodeError, OSError):
        return False
    return metadata.get("Name") == "openevo" and metadata.get("Version") == expected_version


def _close_session_backend(session: OpenEvoSidecarSession) -> None:
    with session.backend_lock:
        old_client = session.backend_client
        old_tunnel = session.backend_tunnel
        session.backend_client = None
        session.backend_tunnel = None
    _close_backend_resources(old_client, old_tunnel)


def _close_backend_resources(
    client: BackendClient | None,
    tunnel: Any | None,
) -> None:
    if client is not None:
        client.close()
    if tunnel is not None:
        _close_backend_tunnel(tunnel)


def _close_backend_tunnel(tunnel: Any) -> None:
    close = getattr(tunnel, "close", None)
    if callable(close):
        close()


def _wheel_metadata_path(names: set[str]) -> str | None:
    matches = sorted(
        name
        for name in names
        if name.endswith(".dist-info/METADATA") and name.count(".dist-info/") == 1
    )
    return matches[0] if matches else None


def _run_services(session: OpenEvoSidecarSession):
    from openevo.deployment.bootstrap import build_remote_bootstrap_plan
    from openevo.deployment.services import (
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


def _active_ready_services_session(
    session: OpenEvoSidecarSession | None,
    *,
    session_pointer_lock: LockType,
    transport_kind: SidecarTransportKind,
) -> OpenEvoSidecarSession:
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
    _require_workspace_and_bootstrap_ready(active_session)
    return active_session


def _current_remote_services_plan(session: OpenEvoSidecarSession):
    from openevo.deployment.bootstrap import build_remote_bootstrap_plan
    from openevo.deployment.services import build_remote_services_plan

    sidecar_plan = build_sidecar_science_plan(session.project, session.profile)
    return build_remote_services_plan(build_remote_bootstrap_plan(sidecar_plan))


def _inspect_remote_services(
    session: OpenEvoSidecarSession,
) -> RemoteServicesStatus:
    from openevo.deployment.services import inspect_remote_services

    return inspect_remote_services(
        session.transport_factory(session.profile),
        _current_remote_services_plan(session),
    )


def _read_remote_service_logs(
    session: OpenEvoSidecarSession,
    service_id: str,
    *,
    lines: int,
) -> RemoteServiceLog:
    from openevo.deployment.services import read_remote_service_logs

    return read_remote_service_logs(
        session.transport_factory(session.profile),
        _current_remote_services_plan(session),
        service_id,
        lines=lines,
    )


def _stop_remote_service(
    session: OpenEvoSidecarSession,
    service_id: str,
) -> RemoteServiceOperationResult:
    from openevo.deployment.services import stop_remote_service

    return stop_remote_service(
        session.transport_factory(session.profile),
        _current_remote_services_plan(session),
        service_id,
    )


def _restart_remote_service(
    session: OpenEvoSidecarSession,
    service_id: str,
) -> RemoteServiceOperationResult:
    from openevo.deployment.services import restart_remote_service

    return restart_remote_service(
        session.transport_factory(session.profile),
        _current_remote_services_plan(session),
        service_id,
    )


def _run_workspace_sync(session: OpenEvoSidecarSession):
    from openevo.deployment.executor import execute_sidecar_plan

    sidecar_plan = build_sidecar_science_plan(session.project, session.profile)
    return execute_sidecar_plan(
        sidecar_plan,
        session.transport_factory(session.profile),
    )


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
    services_ready = bool(services_report is not None and getattr(services_report, "ready", False))
    if (
        not workspace_ready
        or not bootstrap_ready
        or bootstrap_report is None
        or not services_ready
    ):
        raise HTTPException(
            status_code=409,
            detail=("Desktop run launch requires ready workspace, bootstrap, and services."),
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
    artifact_root = posixpath.join(state_root, "evolution", "artifacts")
    framework_lock = posixpath.join(state_root, "wheels", "framework-lock.json")
    command = (
        f'PATH="$HOME/.local/bin:$PATH" openevo-backend run '
        f"{shlex.quote(experiment_snapshot)} "
        f"--output-dir {shlex.quote(output_dir)} "
        f"--artifact-root {shlex.quote(artifact_root)} "
        f"--framework-lock {shlex.quote(framework_lock)} --json"
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
    run_env = session.profile.proxy.to_env()
    try:
        result = session.transport_factory(session.profile).run(
            started.command,
            cwd=state_root,
            env=run_env,
            timeout_seconds=86400.0,
        )
    except Exception as exc:
        return started.model_copy(
            update={
                "state": "failed",
                "ready": False,
                "return_code": None,
                "stderr": sanitize_remote_text(str(exc), run_env),
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    ready = result.return_code == 0
    return started.model_copy(
        update={
            "state": "succeeded" if ready else "failed",
            "ready": ready,
            "return_code": result.return_code,
            "stdout": sanitize_remote_text(result.stdout, run_env),
            "stderr": sanitize_remote_text(result.stderr, run_env),
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


def _invalid_backend_capabilities_error(
    execution_mode: DesktopExecutionMode,
) -> DesktopBackendError:
    return DesktopBackendError(
        502,
        {
            "code": "backend_capabilities_invalid",
            "message": ("Remote OpenEvo backend returned an invalid capabilities payload."),
            "severity": "blocking",
            "category": "internal",
            "retryable": False,
            "repair_action": "user_action_required",
            "details": {"execution_mode": execution_mode},
            "logs_ref": "services/openevo-backend",
        },
    )


def _invalid_remote_project_validation_error() -> DesktopBackendError:
    return DesktopBackendError(
        502,
        {
            "code": "backend_evolution_validation_invalid",
            "message": ("Remote OpenEvo backend returned an invalid project validation payload."),
            "severity": "blocking",
            "category": "internal",
            "retryable": False,
            "repair_action": "user_action_required",
            "details": {},
            "logs_ref": "services/openevo-backend",
        },
    )


def _validate_project_evolution_capabilities(
    project: ScienceProjectConfig,
    capabilities: EvolutionCapabilitiesV1,
) -> None:
    targets = {target.target_id: target for target in capabilities.targets}
    for target_id, selection in project.evolution.targets.items():
        if not selection.enabled:
            continue
        target = targets.get(target_id)
        if target is None:
            raise _unavailable_evolution_selection_error(
                target_id,
                selection.method,
                capabilities.registry_digest,
                "The target is absent from the remote registry capabilities.",
            )
        method = next(
            (
                candidate
                for candidate in target.accepted_methods
                if candidate.method_id == selection.method
            ),
            None,
        )
        if method is not None and method.support.overall == "supported":
            visible_method = next(
                (
                    candidate
                    for candidate in target.methods
                    if candidate.method_id == selection.method
                ),
                None,
            )
            if visible_method is not None:
                try:
                    normalize_config_override(
                        json.loads(visible_method.config_schema_json),
                        json.loads(visible_method.default_config_json),
                        selection.config.to_dict(),
                    )
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise _invalid_evolution_config_error(
                        target_id,
                        selection.method,
                        capabilities.registry_digest,
                    ) from exc
            continue
        resolver = next(
            (
                candidate
                for candidate in target.selection_resolvers
                if candidate.selection_value == selection.method
            ),
            None,
        )
        if resolver is not None and all(
            candidate.support.overall == "supported" for candidate in resolver.resolved_methods
        ):
            continue
        reason = (
            "The selected method is unsupported by the active remote profile."
            if method is not None
            else "The selected method or resolver is absent from the remote registry."
        )
        raise _unavailable_evolution_selection_error(
            target_id,
            selection.method,
            capabilities.registry_digest,
            reason,
        )


def _unavailable_evolution_selection_error(
    target_id: str,
    selection: str | None,
    registry_digest: str,
    reason: str,
) -> DesktopBackendError:
    return DesktopBackendError(
        409,
        {
            "code": "evolution_selection_unavailable",
            "message": "The active project evolution selection cannot run.",
            "severity": "blocking",
            "category": "project",
            "retryable": False,
            "repair_action": "openevo_can_reconfigure",
            "details": {
                "target_id": target_id,
                "selection": selection,
                "registry_digest": registry_digest,
                "reason": reason,
            },
            "logs_ref": None,
        },
    )


def _invalid_evolution_config_error(
    target_id: str,
    selection: str | None,
    registry_digest: str,
) -> DesktopBackendError:
    return DesktopBackendError(
        409,
        {
            "code": "evolution_config_invalid",
            "message": "The active project evolution configuration is invalid.",
            "severity": "blocking",
            "category": "project",
            "retryable": False,
            "repair_action": "openevo_can_reconfigure",
            "details": {
                "target_id": target_id,
                "selection": selection,
                "registry_digest": registry_digest,
            },
            "logs_ref": None,
        },
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
                _service_after_workspace(service, report=report) for service in status.services
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
        return service.model_copy(update={"state": "blocked", "detail": "Remote preflight failed"})
    if service.id == "workspace":
        if report.ready:
            detail = service.detail if service.state == "ready" else "Workspace prepared"
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
                _service_after_bootstrap(service, report=report) for service in status.services
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
        return service.model_copy(update={"state": "blocked", "detail": "Remote preflight failed"})
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
                _service_after_services(service, report=report) for service in status.services
            ),
        }
    )


def _status_services_running(
    status: OpenEvoDesktopShellStatus,
) -> OpenEvoDesktopShellStatus:
    return status.model_copy(
        update={
            "services": tuple(_service_services_running(service) for service in status.services),
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
            "services": tuple(_reset_runtime_service(service) for service in status.services),
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
                _service_after_run(service, report=report) for service in status.services
            ),
            "evolution": tuple(
                _evolution_after_run(step, report=report) for step in status.evolution
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
        return service.model_copy(update={"state": "running", "detail": "OpenEvo run is running"})
    if report.ready:
        return service.model_copy(update={"state": "ready", "detail": "Last run completed"})
    return service.model_copy(update={"state": "blocked", "detail": _run_blocked_detail(report)})


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
    return step.model_copy(update={"state": "blocked", "detail": _run_blocked_detail(report)})


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
