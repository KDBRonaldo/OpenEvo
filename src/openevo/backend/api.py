from __future__ import annotations

import json
from itertools import count
import os
from pathlib import Path
import posixpath
from typing import Any
from urllib.parse import unquote, urlparse

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from openevo import __version__
from openevo.backend.models import (
    ArtifactContent,
    ArtifactDiff,
    ArtifactSummary,
    BackendError,
    BackendStatus,
    EnvironmentCheck,
    EnvironmentDoctorRequest,
    EnvironmentDoctorResponse,
    EnvironmentRepairRequest,
    EnvironmentRepairResponse,
    EnvironmentSettings,
    ErrorCategory,
    HealthResponse,
    LogResponse,
    ProjectCreateRequest,
    ProjectPatchRequest,
    ProjectSummary,
    RunCreateRequest,
    RunSummary,
    ServiceActionResponse,
    ServiceSummary,
    TimelineEvent,
)
from openevo.evolution.framework import (
    CapabilityAudience,
    EvolutionCapabilitiesV1,
    ReleaseExecutionMode,
    build_evolution_capabilities,
    execution_profile_for_release_mode,
)
from openevo.evolution.framework.builtins import (
    VerifiedExecutableRegistry,
    require_verified_executable_registry,
)
from openevo.evolution.models import ArtifactResponse
from openevo.evolution.store import EvolutionStore

DISPLAY_ARTIFACT_TYPES = {
    "text_memory",
    "skill_bundle",
    "agent_system",
    "parametric_memory",
}


class BackendHTTPError(Exception):
    def __init__(self, status_code: int, error: BackendError) -> None:
        self.status_code = status_code
        self.error = error


def _error_response(status_code: int, error: BackendError) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=jsonable_encoder(error))


def _not_found(code: str, category: ErrorCategory, message: str) -> BackendHTTPError:
    return BackendHTTPError(
        404,
        BackendError(
            code=code,
            message=message,
            severity="blocking",
            category=category,
            retryable=False,
            repair_action="user_action_required",
        ),
    )


def _bad_request(code: str, category: ErrorCategory, message: str) -> BackendHTTPError:
    return BackendHTTPError(
        400,
        BackendError(
            code=code,
            message=message,
            severity="blocking",
            category=category,
            retryable=False,
            repair_action="user_action_required",
        ),
    )


def create_backend_app(
    state_root: str | Path | None = None,
    *,
    evolution_registry: VerifiedExecutableRegistry | None = None,
) -> FastAPI:
    if evolution_registry is not None:
        require_verified_executable_registry(evolution_registry)
    app = FastAPI(title="OpenEvo Core Backend", version="0.1.0")
    app.state.evolution_registry = evolution_registry
    canonical_state_root = _canonical_state_root(state_root)
    project_counter = count(1)
    run_counter = count(1)
    artifact_counter = count(1)
    projects: dict[str, ProjectSummary] = {}
    runs: dict[str, RunSummary] = {}
    run_artifacts: dict[str, list[ArtifactSummary]] = {}
    services = {
        "gateway": ServiceSummary(id="gateway", name="Gateway", status="running"),
        "rollout": ServiceSummary(id="rollout", name="Rollout", status="running"),
        "evolution-worker": ServiceSummary(
            id="evolution-worker",
            name="Evolution Worker",
            status="running",
        ),
    }

    @app.exception_handler(BackendHTTPError)
    def backend_error_handler(_request: Request, exc: BackendHTTPError) -> JSONResponse:
        return _error_response(exc.status_code, exc.error)

    @app.exception_handler(RequestValidationError)
    def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        error = BackendError(
            code="request_validation_error",
            message="The request payload does not match the OpenEvo backend contract.",
            severity="blocking",
            category="internal",
            retryable=False,
            repair_action="openevo_can_reconfigure",
            details={"errors": _sanitize_validation_errors(exc.errors())},
        )
        return _error_response(422, error)

    @app.exception_handler(StarletteHTTPException)
    def http_error_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        error = BackendError(
            code="http_error",
            message=str(exc.detail),
            severity="blocking",
            category="internal",
            retryable=False,
            repair_action="user_action_required",
            details={"status_code": exc.status_code},
        )
        return _error_response(exc.status_code, error)

    @app.exception_handler(Exception)
    def unhandled_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        error = BackendError(
            code="internal_server_error",
            message="OpenEvo backend hit an unexpected error.",
            severity="blocking",
            category="internal",
            retryable=True,
            repair_action="openevo_can_retry",
            details={"error_type": type(exc).__name__},
        )
        return _error_response(500, error)

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get("/status", response_model=BackendStatus)
    def status() -> BackendStatus:
        active_runs = len(runs)
        if canonical_state_root is not None:
            active_runs += len(_canonical_run_ids(canonical_state_root))
        return BackendStatus(
            status="ready",
            services=list(services.values()),
            active_runs=active_runs,
        )

    @app.get("/environment", response_model=EnvironmentSettings)
    def environment() -> EnvironmentSettings:
        return EnvironmentSettings()

    @app.post("/environment/doctor", response_model=EnvironmentDoctorResponse)
    def environment_doctor(_request: EnvironmentDoctorRequest) -> EnvironmentDoctorResponse:
        return EnvironmentDoctorResponse(
            status="ok",
            checks=[
                EnvironmentCheck(
                    id="python",
                    category="python",
                    status="ok",
                    message="Python environment is usable.",
                    repair_action="openevo_can_retry",
                )
            ],
        )

    @app.post("/environment/repair", response_model=EnvironmentRepairResponse)
    def environment_repair(request: EnvironmentRepairRequest) -> EnvironmentRepairResponse:
        return EnvironmentRepairResponse(status="ok", performed_actions=request.actions)

    @app.post("/projects", response_model=ProjectSummary)
    def create_project(request: ProjectCreateRequest) -> ProjectSummary:
        project_id = f"project-{next(project_counter)}"
        project = ProjectSummary(
            id=project_id,
            name=request.name,
            workspace_root=request.workspace_root,
            status="ready",
        )
        projects[project_id] = project
        return project

    @app.get("/projects", response_model=list[ProjectSummary])
    def list_projects() -> list[ProjectSummary]:
        return list(projects.values())

    @app.get("/projects/{project_id}", response_model=ProjectSummary)
    def get_project(project_id: str) -> ProjectSummary:
        if project_id not in projects:
            raise _not_found("project_not_found", "project", f"Project {project_id} was not found.")
        return projects[project_id]

    @app.patch("/projects/{project_id}", response_model=ProjectSummary)
    def patch_project(project_id: str, request: ProjectPatchRequest) -> ProjectSummary:
        project = get_project(project_id)
        updated = project.model_copy(update=request.model_dump(exclude_none=True))
        projects[project_id] = updated
        return updated

    @app.post("/runs", response_model=RunSummary)
    def create_run(request: RunCreateRequest) -> RunSummary:
        get_project(request.project_id)
        run_id = f"run-{next(run_counter)}"
        run = RunSummary(
            id=run_id,
            project_id=request.project_id,
            execution_mode=request.execution_mode,
            status="created",
        )
        runs[run_id] = run
        artifact_id = f"artifact-{next(artifact_counter)}"
        run_artifacts[run_id] = [
            ArtifactSummary(
                id=artifact_id,
                run_id=run_id,
                artifact_type="text_memory",
                title="Initial memory draft",
                lineage={"project_id": request.project_id, "run_id": run_id},
            )
        ]
        return run

    @app.get("/runs", response_model=list[RunSummary])
    def list_runs() -> list[RunSummary]:
        canonical_runs = []
        if canonical_state_root is not None:
            canonical_runs = [
                _run_summary_from_canonical(run_id, summary)
                for run_id, summary in _canonical_run_summaries(canonical_state_root)
            ]
        return [*runs.values(), *canonical_runs]

    @app.get("/runs/{run_id}", response_model=RunSummary)
    def get_run(run_id: str) -> RunSummary:
        if run_id not in runs:
            canonical = _read_canonical_run_summary(canonical_state_root, run_id)
            if canonical is None:
                raise _not_found("run_not_found", "run", f"Run {run_id} was not found.")
            return _run_summary_from_canonical(run_id, canonical)
        return runs[run_id]

    @app.post("/runs/{run_id}/cancel", response_model=RunSummary)
    def cancel_run(run_id: str) -> RunSummary:
        run = get_run(run_id).model_copy(update={"status": "cancelled"})
        runs[run_id] = run
        return run

    @app.post("/runs/{run_id}/retry", response_model=RunSummary)
    def retry_run(run_id: str) -> RunSummary:
        run = get_run(run_id).model_copy(update={"status": "created"})
        runs[run_id] = run
        return run

    @app.get("/runs/{run_id:path}/timeline", response_model=list[TimelineEvent])
    def run_timeline(run_id: str) -> list[TimelineEvent]:
        _validate_opaque_id(run_id, "run_id")
        canonical = _read_canonical_run_summary(canonical_state_root, run_id)
        if canonical is not None:
            return _timeline_from_canonical_run(run_id, canonical)
        run = get_run(run_id)
        artifact_ids = [artifact.id for artifact in run_artifacts.get(run_id, [])]
        return [
            TimelineEvent(
                id=f"{run_id}-created",
                phase="created",
                title="Run created",
                message=f"{run.execution_mode} run is queued.",
                artifact_ids=artifact_ids,
            )
        ]

    @app.get("/runs/{run_id:path}/logs", response_model=LogResponse)
    def run_logs(run_id: str) -> LogResponse:
        _validate_opaque_id(run_id, "run_id")
        canonical = _read_canonical_run_summary(canonical_state_root, run_id)
        if canonical is not None:
            return LogResponse(
                id=run_id,
                lines=[
                    f"run status: {_string_value(canonical.get('status'), 'unknown')}",
                    f"summary: {_canonical_summary_path(canonical_state_root, run_id)}",
                ],
            )
        get_run(run_id)
        return LogResponse(id=run_id, lines=["run created"])

    @app.get("/runs/{run_id:path}/artifacts", response_model=list[ArtifactSummary])
    def artifacts_for_run(run_id: str) -> list[ArtifactSummary]:
        _validate_opaque_id(run_id, "run_id")
        canonical = _read_canonical_run_summary(canonical_state_root, run_id)
        if canonical is not None:
            return _artifacts_from_canonical_run(
                run_id,
                canonical,
                state_root=canonical_state_root,
            )
        get_run(run_id)
        return run_artifacts.get(run_id, [])

    @app.get("/artifacts/{artifact_id}", response_model=ArtifactSummary)
    def get_artifact(artifact_id: str) -> ArtifactSummary:
        canonical = _canonical_artifact_summary(canonical_state_root, artifact_id)
        if canonical is not None:
            return canonical
        for artifacts in run_artifacts.values():
            for artifact in artifacts:
                if artifact.id == artifact_id:
                    return artifact
        raise _not_found("artifact_not_found", "artifact", f"Artifact {artifact_id} was not found.")

    @app.get("/artifacts/{artifact_id:path}/content", response_model=ArtifactContent)
    def artifact_content(artifact_id: str) -> ArtifactContent:
        _validate_opaque_id(artifact_id, "artifact_id")
        canonical = _read_canonical_artifact(canonical_state_root, artifact_id)
        if canonical is not None:
            content, target_path = _read_artifact_content_file(
                canonical,
                state_root=canonical_state_root,
            )
            return ArtifactContent(
                id=canonical.artifact_id,
                artifact_type=str(canonical.type),
                content=content,
                metadata={
                    "lineage": _artifact_lineage(canonical),
                    "target_path": target_path,
                },
            )
        artifact = get_artifact(artifact_id)
        return ArtifactContent(
            id=artifact.id,
            artifact_type=artifact.artifact_type,
            content="OpenEvo memory draft.",
            metadata={"lineage": artifact.lineage},
        )

    @app.get("/artifacts/{artifact_id:path}/diff", response_model=ArtifactDiff)
    def artifact_diff(artifact_id: str) -> ArtifactDiff:
        _validate_opaque_id(artifact_id, "artifact_id")
        canonical = _read_canonical_artifact(canonical_state_root, artifact_id)
        if canonical is not None:
            content, _target_path = _read_artifact_content_file(
                canonical,
                state_root=canonical_state_root,
            )
            return ArtifactDiff(id=artifact_id, before="", after=content)
        get_artifact(artifact_id)
        return ArtifactDiff(id=artifact_id, before="", after="OpenEvo memory draft.")

    @app.get("/services", response_model=list[ServiceSummary])
    def list_services() -> list[ServiceSummary]:
        return list(services.values())

    @app.get("/services/{service_id}/logs", response_model=LogResponse)
    def service_logs(service_id: str) -> LogResponse:
        if service_id not in services:
            raise _not_found("service_not_found", "service", f"Service {service_id} was not found.")
        return LogResponse(id=service_id, lines=[f"{service_id} is running"])

    @app.post("/services/{service_id}/restart", response_model=ServiceActionResponse)
    def restart_service(service_id: str) -> ServiceActionResponse:
        if service_id not in services:
            raise _not_found("service_not_found", "service", f"Service {service_id} was not found.")
        services[service_id] = services[service_id].model_copy(update={"status": "running"})
        return ServiceActionResponse(service_id=service_id, status="running")

    @app.post("/services/{service_id}/stop", response_model=ServiceActionResponse)
    def stop_service(service_id: str) -> ServiceActionResponse:
        if service_id not in services:
            raise _not_found("service_not_found", "service", f"Service {service_id} was not found.")
        services[service_id] = services[service_id].model_copy(update={"status": "stopped"})
        return ServiceActionResponse(service_id=service_id, status="stopped")

    @app.get(
        "/capabilities",
        response_model=EvolutionCapabilitiesV1,
        responses={422: {"model": BackendError}, 503: {"model": BackendError}},
    )
    def capabilities(execution_mode: ReleaseExecutionMode) -> EvolutionCapabilitiesV1:
        if evolution_registry is None:
            raise BackendHTTPError(
                503,
                BackendError(
                    code="evolution_registry_unavailable",
                    message="Verified evolution capabilities are unavailable.",
                    severity="blocking",
                    category="service",
                    retryable=True,
                    repair_action="openevo_can_retry",
                ),
            )
        profile = execution_profile_for_release_mode(execution_mode)
        return build_evolution_capabilities(
            evolution_registry.snapshot,
            profile=profile,
            audience=CapabilityAudience.DESKTOP,
            core_version=__version__,
        )

    return app


def _canonical_state_root(state_root: str | Path | None) -> Path | None:
    raw = state_root or os.environ.get("OPENEVO_BACKEND_STATE_ROOT")
    if raw is None:
        return None
    path = Path(raw).expanduser().resolve()
    return path if path.exists() else path


def _canonical_summary_path(state_root: Path | None, run_id: str) -> Path | None:
    if state_root is None:
        return None
    _validate_opaque_id(run_id, "run_id")
    path = (state_root / "runs" / run_id / "summary.json").resolve()
    runs_root = (state_root / "runs").resolve()
    if runs_root != path.parent.parent:
        raise ValueError("run_id resolved outside state root")
    return path


def _read_canonical_run_summary(
    state_root: Path | None,
    run_id: str,
) -> dict[str, Any] | None:
    path = _canonical_summary_path(state_root, run_id)
    if path is None or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _canonical_run_ids(state_root: Path) -> list[str]:
    runs_root = state_root / "runs"
    if not runs_root.is_dir():
        return []
    return sorted(
        path.parent.name
        for path in runs_root.glob("*/summary.json")
        if path.is_file()
    )


def _canonical_run_summaries(state_root: Path) -> list[tuple[str, dict[str, Any]]]:
    summaries: list[tuple[str, dict[str, Any]]] = []
    for run_id in _canonical_run_ids(state_root):
        summary = _read_canonical_run_summary(state_root, run_id)
        if summary is not None:
            summaries.append((run_id, summary))
    return summaries


def _run_summary_from_canonical(
    run_id: str,
    summary: dict[str, Any],
) -> RunSummary:
    status = _canonical_run_status(summary.get("status"))
    return RunSummary(
        id=run_id,
        project_id=_string_value(summary.get("experiment_id"), "canonical-state"),
        execution_mode="codex_subscription_transcript",
        status=status,
    )


def _canonical_run_status(value: object) -> str:
    status = _string_value(value, "completed").lower()
    if status in {"completed", "succeeded", "success"}:
        return "completed"
    if status in {"running", "created", "cancelled"}:
        return status
    return "failed"


def _timeline_from_canonical_run(
    run_id: str,
    summary: dict[str, Any],
) -> list[TimelineEvent]:
    events = [
        TimelineEvent(
            id=f"{run_id}-summary",
            phase=_string_value(summary.get("status"), "completed"),
            title="Run summary available",
            message=(
                f"{_string_value(summary.get('experiment_name'), 'OpenEvo')} "
                "run output is available."
            ),
            artifact_ids=[],
        )
    ]
    for task in _dict_items(summary.get("tasks")):
        task_id = _string_value(task.get("task_id"), "unknown-task")
        for round_payload in _dict_items(task.get("rounds")):
            round_index = _int_value(round_payload.get("round_index"), 0)
            artifact_ids = _artifact_ids_from_round(round_payload)
            events.append(
                TimelineEvent(
                    id=f"{run_id}-{task_id}-round-{round_index}",
                    phase="evolution",
                    title=f"{task_id} round {round_index}",
                    message=_round_timeline_message(round_payload),
                    artifact_ids=artifact_ids,
                )
            )
    return events


def _round_timeline_message(round_payload: dict[str, Any]) -> str:
    jobs = _dict_items(round_payload.get("jobs"))
    succeeded = sum(
        1 for job in jobs if _string_value(job.get("worker_status"), "") == "succeeded"
    )
    if succeeded:
        return f"{succeeded} evolution job(s) completed."
    rollout_status = _string_value(round_payload.get("rollout_status"), "")
    if rollout_status:
        return f"Rollout status: {rollout_status}."
    return "Round output is available."


def _artifacts_from_canonical_run(
    run_id: str,
    summary: dict[str, Any],
    *,
    state_root: Path | None,
) -> list[ArtifactSummary]:
    references = _artifact_references_from_summary(summary)
    artifacts: list[ArtifactSummary] = []
    for artifact_id, reference in references.items():
        metadata = _read_canonical_artifact(state_root, artifact_id)
        artifact_type = reference["artifact_type"]
        if metadata is not None:
            artifact_type = str(metadata.type)
        if artifact_type not in DISPLAY_ARTIFACT_TYPES:
            continue
        artifacts.append(
            ArtifactSummary(
                id=artifact_id,
                run_id=run_id,
                artifact_type=artifact_type,
                title=metadata.name if metadata is not None else _title_from_type(artifact_type),
                promoted=(
                    bool(metadata.promoted)
                    if metadata is not None
                    else bool(reference.get("promoted"))
                ),
                lineage=_merged_artifact_lineage(metadata, reference),
            )
        )
    return artifacts


def _canonical_artifact_summary(
    state_root: Path | None,
    artifact_id: str,
) -> ArtifactSummary | None:
    metadata = _read_canonical_artifact(state_root, artifact_id)
    if metadata is None or str(metadata.type) not in DISPLAY_ARTIFACT_TYPES:
        return None
    run_id = _run_id_for_artifact(state_root, artifact_id) or "canonical-state"
    return ArtifactSummary(
        id=metadata.artifact_id,
        run_id=run_id,
        artifact_type=str(metadata.type),
        title=metadata.name,
        promoted=metadata.promoted,
        lineage=_artifact_lineage(metadata),
    )


def _read_canonical_artifact(
    state_root: Path | None,
    artifact_id: str,
) -> ArtifactResponse | None:
    if state_root is None:
        return None
    _validate_opaque_id(artifact_id, "artifact_id")
    db_path = state_root / "evolution" / "evolution.db"
    artifact_root = _canonical_artifact_root(state_root)
    if not db_path.is_file():
        return None
    store = EvolutionStore(db_path=db_path, artifact_root=artifact_root)
    try:
        return store.get_artifact(artifact_id)
    except Exception:
        return None


def _canonical_artifact_root(state_root: Path) -> Path:
    return (state_root / "evolution" / "artifacts").resolve()


def _run_id_for_artifact(state_root: Path | None, artifact_id: str) -> str | None:
    if state_root is None:
        return None
    for run_id, summary in _canonical_run_summaries(state_root):
        if artifact_id in _artifact_references_from_summary(summary):
            return run_id
    return None


def _artifact_references_from_summary(
    summary: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    references: dict[str, dict[str, Any]] = {}
    for task in _dict_items(summary.get("tasks")):
        task_id = _string_value(task.get("task_id"), "unknown-task")
        for round_payload in _dict_items(task.get("rounds")):
            round_index = _int_value(round_payload.get("round_index"), 0)
            for artifact_type, artifact_ids in _artifact_id_map(
                round_payload.get("artifact_ids")
            ).items():
                for artifact_id in artifact_ids:
                    if artifact_type in DISPLAY_ARTIFACT_TYPES:
                        references.setdefault(
                            artifact_id,
                            {
                                "artifact_type": artifact_type,
                                "task_id": task_id,
                                "round_index": round_index,
                                "promoted": True,
                            },
                        )
            for job in _dict_items(round_payload.get("jobs")):
                artifact_type = _string_value(job.get("artifact_type"), "")
                if artifact_type not in DISPLAY_ARTIFACT_TYPES:
                    continue
                approved = set(_string_items(job.get("approved_artifact_ids")))
                for artifact_id in _string_items(job.get("artifact_ids")):
                    reference = references.setdefault(
                        artifact_id,
                        {
                            "artifact_type": artifact_type,
                            "task_id": task_id,
                            "round_index": round_index,
                        },
                    )
                    reference.update(
                        {
                            "method": _string_value(job.get("method"), ""),
                            "worker_status": _string_value(
                                job.get("worker_status"),
                                "",
                            ),
                            "promotion_status": _string_value(
                                job.get("promotion_status"),
                                "",
                            ),
                            "promoted": bool(reference.get("promoted"))
                            or artifact_id in approved,
                        }
                    )
    return references


def _artifact_ids_from_round(round_payload: dict[str, Any]) -> list[str]:
    artifact_ids: list[str] = []
    for ids in _artifact_id_map(round_payload.get("artifact_ids")).values():
        artifact_ids.extend(ids)
    for job in _dict_items(round_payload.get("jobs")):
        artifact_ids.extend(_string_items(job.get("artifact_ids")))
        artifact_ids.extend(_string_items(job.get("approved_artifact_ids")))
    return list(dict.fromkeys(artifact_ids))


def _artifact_id_map(value: object) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, list[str]] = {}
    for key, item in value.items():
        if isinstance(key, str):
            result[key] = _string_items(item)
    return result


def _read_artifact_content_file(
    artifact: ArtifactResponse,
    *,
    state_root: Path | None,
) -> tuple[str, str]:
    if state_root is None:
        raise _not_found(
            "artifact_content_not_found",
            "artifact",
            f"Artifact {artifact.artifact_id} content was not found.",
        )
    if _run_id_for_artifact(state_root, artifact.artifact_id) is None:
        raise _not_found(
            "artifact_content_not_found",
            "artifact",
            f"Artifact {artifact.artifact_id} content was not found.",
        )
    allowed_root = _canonical_artifact_root(state_root)
    uri_path = _file_uri_path(artifact.uri)
    relative_path = _artifact_relative_content_path(artifact, uri_path=uri_path)
    root = _artifact_root_path(uri_path=uri_path, relative_path=relative_path)
    path = (Path(root) / relative_path).resolve()
    root_path = Path(root).resolve()
    if root_path != path and root_path not in path.parents:
        raise _not_found(
            "artifact_content_not_found",
            "artifact",
            f"Artifact {artifact.artifact_id} content was not found.",
        )
    if allowed_root != path and allowed_root not in path.parents:
        raise _not_found(
            "artifact_content_not_found",
            "artifact",
            f"Artifact {artifact.artifact_id} content was not found.",
        )
    if not path.is_file():
        raise _not_found(
            "artifact_content_not_found",
            "artifact",
            f"Artifact {artifact.artifact_id} content was not found.",
        )
    return path.read_text(encoding="utf-8"), relative_path


def _file_uri_path(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme != "file" or parsed.netloc:
        raise _not_found(
            "artifact_content_not_found",
            "artifact",
            "Artifact content is only available for file artifacts.",
        )
    path = unquote(parsed.path)
    if not path.startswith("/"):
        raise _not_found(
            "artifact_content_not_found",
            "artifact",
            "Artifact content is only available for file artifacts.",
        )
    return path


def _artifact_relative_content_path(
    artifact: ArtifactResponse,
    *,
    uri_path: str,
) -> str:
    content_path = artifact.manifest.get("content_path")
    if isinstance(content_path, str) and content_path.strip():
        relative_path = content_path.strip()
    elif str(artifact.type) == "text_memory":
        filename = posixpath.basename(uri_path)
        relative_path = filename if "." in filename else "memory.md"
    elif str(artifact.type) == "agent_system":
        target_path = artifact.manifest.get("target_path")
        relative_path = (
            posixpath.basename(target_path.strip())
            if isinstance(target_path, str) and target_path.strip()
            else "agent_system.md"
        )
    else:
        relative_path = "SKILL.md"
    normalized = posixpath.normpath(relative_path)
    if (
        normalized in {"", "."}
        or posixpath.isabs(relative_path)
        or normalized == ".."
        or normalized.startswith("../")
    ):
        raise _not_found(
            "artifact_content_not_found",
            "artifact",
            "Artifact content path is not readable.",
        )
    return normalized


def _artifact_root_path(*, uri_path: str, relative_path: str) -> str:
    normalized_relative = posixpath.normpath(relative_path)
    suffix = f"/{normalized_relative}"
    if uri_path.endswith(suffix):
        return uri_path[: -len(suffix)] or "/"
    return uri_path


def _artifact_lineage(artifact: ArtifactResponse) -> dict[str, Any]:
    manifest_lineage = artifact.manifest.get("lineage")
    if isinstance(manifest_lineage, dict):
        return dict(manifest_lineage)
    return {}


def _merged_artifact_lineage(
    artifact: ArtifactResponse | None,
    reference: dict[str, Any],
) -> dict[str, Any]:
    lineage = _artifact_lineage(artifact) if artifact is not None else {}
    return _reference_lineage(reference) | lineage


def _reference_lineage(reference: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in reference.items()
        if key
        in {
            "task_id",
            "round_index",
            "method",
            "worker_status",
            "promotion_status",
        }
        and value not in {"", None}
    }


def _dict_items(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _string_items(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _string_value(value: object, default: str) -> str:
    return value if isinstance(value, str) and value else default


def _int_value(value: object, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _sanitize_validation_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sanitized: list[dict[str, Any]] = []
    for error in errors:
        item = dict(error)
        ctx = item.get("ctx")
        if isinstance(ctx, dict):
            item["ctx"] = {key: str(value) for key, value in ctx.items()}
        sanitized.append(item)
    return sanitized


def _title_from_type(artifact_type: str) -> str:
    return artifact_type.replace("_", " ").title()


def _validate_opaque_id(value: str, field_name: str) -> None:
    if not value.strip() or "/" in value or "\\" in value or value in {".", ".."}:
        if field_name == "run_id":
            raise _bad_request(
                "invalid_run_id",
                "run",
                "Run id must be an opaque id.",
            )
        if field_name == "artifact_id":
            raise _bad_request(
                "invalid_artifact_id",
                "artifact",
                "Artifact id must be an opaque id.",
            )
        raise _bad_request(
            "invalid_id",
            "internal",
            f"{field_name} must be an opaque id.",
        )
