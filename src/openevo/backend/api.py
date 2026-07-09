from __future__ import annotations

from itertools import count

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

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
from openevo.capabilities import CoreCapabilities, build_core_capabilities


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


def create_backend_app() -> FastAPI:
    app = FastAPI(title="OpenEvo Core Backend", version="0.1.0")
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
            details={"errors": exc.errors()},
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
        return BackendStatus(status="ready", services=list(services.values()), active_runs=len(runs))

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
        return list(runs.values())

    @app.get("/runs/{run_id}", response_model=RunSummary)
    def get_run(run_id: str) -> RunSummary:
        if run_id not in runs:
            raise _not_found("run_not_found", "run", f"Run {run_id} was not found.")
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

    @app.get("/runs/{run_id}/timeline", response_model=list[TimelineEvent])
    def run_timeline(run_id: str) -> list[TimelineEvent]:
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

    @app.get("/runs/{run_id}/logs", response_model=LogResponse)
    def run_logs(run_id: str) -> LogResponse:
        get_run(run_id)
        return LogResponse(id=run_id, lines=["run created"])

    @app.get("/runs/{run_id}/artifacts", response_model=list[ArtifactSummary])
    def artifacts_for_run(run_id: str) -> list[ArtifactSummary]:
        get_run(run_id)
        return run_artifacts.get(run_id, [])

    @app.get("/artifacts/{artifact_id}", response_model=ArtifactSummary)
    def get_artifact(artifact_id: str) -> ArtifactSummary:
        for artifacts in run_artifacts.values():
            for artifact in artifacts:
                if artifact.id == artifact_id:
                    return artifact
        raise _not_found("artifact_not_found", "artifact", f"Artifact {artifact_id} was not found.")

    @app.get("/artifacts/{artifact_id}/content", response_model=ArtifactContent)
    def artifact_content(artifact_id: str) -> ArtifactContent:
        artifact = get_artifact(artifact_id)
        return ArtifactContent(
            id=artifact.id,
            artifact_type=artifact.artifact_type,
            content="OpenEvo memory draft.",
            metadata={"lineage": artifact.lineage},
        )

    @app.get("/artifacts/{artifact_id}/diff", response_model=ArtifactDiff)
    def artifact_diff(artifact_id: str) -> ArtifactDiff:
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

    @app.get("/capabilities", response_model=CoreCapabilities)
    def capabilities() -> CoreCapabilities:
        return build_core_capabilities()

    return app
