from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from functools import wraps
from typing import Annotated, Literal, NoReturn, Protocol

from fastapi import APIRouter, FastAPI, Header, HTTPException, Path, Query, Security
from fastapi.routing import APIRoute
from fastapi.responses import StreamingResponse
from fastapi.security import APIKeyHeader

from .models import (
    ApiErrorV1,
    ArtifactContentV1,
    ArtifactDiffV1,
    ArtifactPageV1,
    ArtifactV1,
    CacheCleanupRequestV1,
    CapabilitiesEnvelopeV1,
    DesktopStateV1,
    DiagnosticReportV1,
    DiagnosticRequestV1,
    EventEnvelopeV1,
    HealthV1,
    HostKeyAcceptV1,
    LocalLogPageV1,
    LocalOperationV1,
    LogPageV1,
    OperationV1,
    ProjectCreateV1,
    ProjectPageV1,
    ProjectPatchV1,
    ProjectV1,
    ProjectValidationV1,
    ReferencedLogPageV1,
    RemoteProfileCreateV1,
    RemoteProfilePageV1,
    RemoteProfilePatchV1,
    RemoteProfileV1,
    RunCreateV1,
    RunContextV1,
    RunPageV1,
    RunRetryV1,
    RunV1,
    ServicePageV1,
    TimelinePageV1,
    VersionV1,
)


DESKTOP_SESSION_HEADER = "X-OpenEvo-Desktop-Session"
IDEMPOTENCY_HEADER = "Idempotency-Key"
IF_MATCH_HEADER = "If-Match"

_desktop_session = APIKeyHeader(
    name=DESKTOP_SESSION_HEADER,
    scheme_name="DesktopSession",
    description=(
        "Ephemeral Desktop session credential returned only by the native start_sidecar command."
    ),
    auto_error=True,
)

ResourceId = Annotated[str, Path(min_length=1, max_length=256)]
IdempotencyKey = Annotated[
    str,
    Header(
        alias=IDEMPOTENCY_HEADER,
        min_length=16,
        max_length=256,
        description="Stable key binding this principal, route, scope, and request body.",
    ),
]
IfMatch = Annotated[
    str,
    Header(
        alias=IF_MATCH_HEADER,
        pattern=r'^"[0-9a-f]{64}"$',
        description="Current resource ETag required for mutation.",
    ),
]
Limit = Annotated[int, Query(ge=1, le=100)]
Cursor = Annotated[str | None, Query(min_length=1, max_length=2_048)]
Sort = Annotated[
    str,
    Query(pattern=r"^[a-z][a-z0-9_]{0,63}$", description="Route-supported stable sort key."),
]
Direction = Annotated[Literal["asc", "desc"], Query()]

_ERROR_RESPONSES = {
    400: {"model": ApiErrorV1, "description": "Invalid request or cursor."},
    401: {"model": ApiErrorV1, "description": "Desktop session is missing or invalid."},
    404: {"model": ApiErrorV1, "description": "Resource not found."},
    409: {"model": ApiErrorV1, "description": "Resource or idempotency conflict."},
    410: {"model": ApiErrorV1, "description": "Cursor expired; reload the snapshot."},
    412: {"model": ApiErrorV1, "description": "ETag precondition failed."},
    422: {"model": ApiErrorV1, "description": "Closed contract validation failed."},
    426: {"model": ApiErrorV1, "description": "No compatible contract major version."},
    503: {"model": ApiErrorV1, "description": "Required remote capability is unavailable."},
}


class DesktopLocalApiProviderV1(Protocol):
    """Execution provider bound to the canonical Desktop Local API routes."""

    def invoke(self, operation_id: str, arguments: Mapping[str, object]) -> object: ...


def _contract_only() -> NoReturn:
    raise HTTPException(status_code=501, detail="Contract-only application")


def _iter_api_routes(routes: Iterable[object]) -> Iterator[APIRoute]:
    visited_routers: set[int] = set()

    def visit(items: Iterable[object]) -> Iterator[APIRoute]:
        for route in items:
            if isinstance(route, APIRoute):
                yield route
                continue
            original_router = getattr(route, "original_router", None)
            if not isinstance(original_router, APIRouter):
                continue
            identity = id(original_router)
            if identity in visited_routers:
                continue
            visited_routers.add(identity)
            yield from visit(original_router.routes)

    yield from visit(routes)


def _bind_provider(app: FastAPI, provider: DesktopLocalApiProviderV1) -> None:
    for route in _iter_api_routes(app.routes):
        if route.operation_id is None:
            continue
        operation_id = route.operation_id
        original_endpoint = route.endpoint

        @wraps(original_endpoint)
        def invoke_provider(_operation_id: str = operation_id, **arguments: object) -> object:
            return provider.invoke(_operation_id, arguments)

        route.endpoint = invoke_provider
        route.dependant.call = invoke_provider


def create_contract_app(
    provider: DesktopLocalApiProviderV1 | None = None,
    *,
    _app_factory: Callable[..., FastAPI] = FastAPI,
) -> FastAPI:
    app = _app_factory(
        title="OpenEvo Desktop Local API",
        summary="Strict renderer-to-sidecar product contract.",
        description=(
            "The native host supplies the endpoint and Desktop session credential directly "
            "to the renderer. Discovery responses never contain credentials."
        ),
        version="1.0.0",
        openapi_version="3.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        separate_input_output_schemas=False,
        contact={"name": "OpenEvo", "url": "https://github.com/CompLifeLab-ZJU/OpenEvo"},
        license_info={"name": "Apache-2.0"},
    )

    @app.get(
        "/version",
        operation_id="getDesktopContractVersion",
        response_model=VersionV1,
        responses={426: _ERROR_RESPONSES[426]},
        tags=["discovery"],
    )
    def get_version() -> VersionV1:
        _contract_only()

    @app.get(
        "/health",
        operation_id="getDesktopHealth",
        response_model=HealthV1,
        responses={
            403: {"model": ApiErrorV1, "description": "Invalid native health challenge."},
            422: _ERROR_RESPONSES[422],
        },
        tags=["discovery"],
    )
    def get_health(
        x_openevo_native_challenge: Annotated[
            str | None,
            Header(
                alias="X-OpenEvo-Native-Challenge",
                pattern=r"^[0-9a-f]{64}$",
                description="Fresh native-host challenge used to prove sidecar instance identity.",
            ),
        ] = None,
    ) -> HealthV1:
        del x_openevo_native_challenge
        _contract_only()

    router = APIRouter(
        prefix="/desktop/v1",
        dependencies=[Security(_desktop_session)],
        responses=_ERROR_RESPONSES,
    )

    @router.get("/state", operation_id="getDesktopState", response_model=DesktopStateV1)
    def get_state() -> DesktopStateV1:
        _contract_only()

    @router.get("/profiles", operation_id="listRemoteProfiles", response_model=RemoteProfilePageV1)
    def list_profiles(
        limit: Limit = 50,
        after: Cursor = None,
        sort: Sort = "updated_at",
        direction: Direction = "desc",
    ) -> RemoteProfilePageV1:
        _contract_only()

    @router.post(
        "/profiles",
        operation_id="createRemoteProfile",
        response_model=RemoteProfileV1,
        status_code=201,
    )
    def create_profile(
        request: RemoteProfileCreateV1, idempotency_key: IdempotencyKey
    ) -> RemoteProfileV1:
        _contract_only()

    @router.get(
        "/profiles/{profile_id}",
        operation_id="getRemoteProfile",
        response_model=RemoteProfileV1,
    )
    def get_profile(profile_id: ResourceId) -> RemoteProfileV1:
        _contract_only()

    @router.patch(
        "/profiles/{profile_id}",
        operation_id="updateRemoteProfile",
        response_model=RemoteProfileV1,
    )
    def update_profile(
        profile_id: ResourceId, request: RemoteProfilePatchV1, if_match: IfMatch
    ) -> RemoteProfileV1:
        _contract_only()

    @router.delete(
        "/profiles/{profile_id}",
        operation_id="deleteRemoteProfile",
        status_code=204,
        response_model=None,
    )
    def delete_profile(profile_id: ResourceId, if_match: IfMatch) -> None:
        _contract_only()

    @router.post(
        "/profiles/{profile_id}/connect",
        operation_id="connectRemoteProfile",
        response_model=LocalOperationV1,
        status_code=202,
    )
    def connect_profile(
        profile_id: ResourceId, idempotency_key: IdempotencyKey, if_match: IfMatch
    ) -> LocalOperationV1:
        _contract_only()

    @router.post(
        "/profiles/{profile_id}/disconnect",
        operation_id="disconnectRemoteProfile",
        response_model=LocalOperationV1,
        status_code=202,
    )
    def disconnect_profile(
        profile_id: ResourceId, idempotency_key: IdempotencyKey, if_match: IfMatch
    ) -> LocalOperationV1:
        _contract_only()

    @router.post(
        "/profiles/{profile_id}/host-key/accept",
        operation_id="acceptRemoteHostKey",
        response_model=LocalOperationV1,
        status_code=202,
    )
    def accept_host_key(
        profile_id: ResourceId,
        request: HostKeyAcceptV1,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
    ) -> LocalOperationV1:
        _contract_only()

    @router.get("/projects", operation_id="listProjects", response_model=ProjectPageV1)
    def list_projects(
        limit: Limit = 50,
        after: Cursor = None,
        sort: Sort = "updated_at",
        direction: Direction = "desc",
    ) -> ProjectPageV1:
        _contract_only()

    @router.post(
        "/projects",
        operation_id="createProject",
        response_model=ProjectV1,
        status_code=201,
    )
    def create_project(request: ProjectCreateV1, idempotency_key: IdempotencyKey) -> ProjectV1:
        _contract_only()

    @router.get("/projects/{project_id}", operation_id="getProject", response_model=ProjectV1)
    def get_project(project_id: ResourceId) -> ProjectV1:
        _contract_only()

    @router.patch("/projects/{project_id}", operation_id="updateProject", response_model=ProjectV1)
    def update_project(
        project_id: ResourceId, request: ProjectPatchV1, if_match: IfMatch
    ) -> ProjectV1:
        _contract_only()

    @router.delete(
        "/projects/{project_id}",
        operation_id="deleteProject",
        status_code=204,
        response_model=None,
    )
    def delete_project(project_id: ResourceId, if_match: IfMatch) -> None:
        _contract_only()

    @router.post(
        "/projects/{project_id}/activate",
        operation_id="activateProject",
        response_model=LocalOperationV1,
        status_code=202,
    )
    def activate_project(
        project_id: ResourceId, idempotency_key: IdempotencyKey, if_match: IfMatch
    ) -> LocalOperationV1:
        _contract_only()

    @router.post(
        "/projects/{project_id}/bootstrap",
        operation_id="bootstrapProject",
        response_model=LocalOperationV1,
        status_code=202,
    )
    def bootstrap_project(
        project_id: ResourceId, idempotency_key: IdempotencyKey, if_match: IfMatch
    ) -> LocalOperationV1:
        _contract_only()

    @router.post(
        "/projects/{project_id}/doctor",
        operation_id="doctorProject",
        response_model=LocalOperationV1,
        status_code=202,
    )
    def doctor_project(
        project_id: ResourceId, idempotency_key: IdempotencyKey, if_match: IfMatch
    ) -> LocalOperationV1:
        _contract_only()

    @router.post(
        "/projects/{project_id}/repair",
        operation_id="repairProject",
        response_model=LocalOperationV1,
        status_code=202,
    )
    def repair_project(
        project_id: ResourceId, idempotency_key: IdempotencyKey, if_match: IfMatch
    ) -> LocalOperationV1:
        _contract_only()

    @router.post(
        "/projects/{project_id}/workspace-sync",
        operation_id="syncProjectWorkspace",
        response_model=LocalOperationV1,
        status_code=202,
    )
    def sync_project_workspace(
        project_id: ResourceId, idempotency_key: IdempotencyKey, if_match: IfMatch
    ) -> LocalOperationV1:
        _contract_only()

    @router.get(
        "/projects/{project_id}/capabilities",
        operation_id="getProjectCapabilities",
        response_model=CapabilitiesEnvelopeV1,
    )
    def get_project_capabilities(project_id: ResourceId) -> CapabilitiesEnvelopeV1:
        _contract_only()

    @router.post(
        "/projects/{project_id}/validate",
        operation_id="validateProject",
        response_model=ProjectValidationV1,
    )
    def validate_project(
        project_id: ResourceId,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
    ) -> ProjectValidationV1:
        _contract_only()

    @router.get(
        "/operations/{operation_id}",
        operation_id="getLocalOperation",
        response_model=LocalOperationV1,
    )
    def get_operation(operation_id: ResourceId) -> LocalOperationV1:
        _contract_only()

    @router.get(
        "/operations/{operation_id}/logs",
        operation_id="listLocalOperationLogs",
        response_model=LocalLogPageV1,
    )
    def list_operation_logs(
        operation_id: ResourceId,
        limit: Limit = 50,
        after: Cursor = None,
        sort: Sort = "occurred_at",
        direction: Direction = "asc",
    ) -> LocalLogPageV1:
        _contract_only()

    @router.post(
        "/operations/{operation_id}/cancel",
        operation_id="cancelLocalOperation",
        response_model=LocalOperationV1,
        status_code=202,
    )
    def cancel_operation(
        operation_id: ResourceId, idempotency_key: IdempotencyKey, if_match: IfMatch
    ) -> LocalOperationV1:
        _contract_only()

    @router.get("/runs", operation_id="listRuns", response_model=RunPageV1)
    def list_runs(
        limit: Limit = 50,
        after: Cursor = None,
        sort: Sort = "created_at",
        direction: Direction = "desc",
    ) -> RunPageV1:
        _contract_only()

    @router.post("/runs", operation_id="createRun", response_model=RunV1, status_code=202)
    def create_run(
        request: RunCreateV1,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
    ) -> RunV1:
        _contract_only()

    @router.get("/runs/{run_id}", operation_id="getRun", response_model=RunV1)
    def get_run(run_id: ResourceId) -> RunV1:
        _contract_only()

    @router.delete(
        "/runs/{run_id}",
        operation_id="deleteRun",
        status_code=204,
        response_model=None,
    )
    def delete_run(run_id: ResourceId, if_match: IfMatch) -> None:
        _contract_only()

    @router.post(
        "/runs/{run_id}/cancel",
        operation_id="cancelRun",
        response_model=RunV1,
        status_code=202,
    )
    def cancel_run(
        run_id: ResourceId, idempotency_key: IdempotencyKey, if_match: IfMatch
    ) -> RunV1:
        _contract_only()

    @router.post(
        "/runs/{run_id}/retry",
        operation_id="retryRun",
        response_model=RunV1,
        status_code=202,
    )
    def retry_run(
        run_id: ResourceId,
        request: RunRetryV1,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
    ) -> RunV1:
        _contract_only()

    @router.get(
        "/runs/{run_id}/timeline",
        operation_id="listRunTimeline",
        response_model=TimelinePageV1,
    )
    def list_run_timeline(
        run_id: ResourceId,
        limit: Limit = 50,
        after: Cursor = None,
        sort: Sort = "occurred_at",
        direction: Direction = "asc",
    ) -> TimelinePageV1:
        _contract_only()

    @router.get("/runs/{run_id}/logs", operation_id="listRunLogs", response_model=LogPageV1)
    def list_run_logs(
        run_id: ResourceId,
        limit: Limit = 50,
        after: Cursor = None,
        sort: Sort = "occurred_at",
        direction: Direction = "asc",
    ) -> LogPageV1:
        _contract_only()

    @router.get(
        "/runs/{run_id}/context",
        operation_id="getRunContext",
        response_model=RunContextV1,
    )
    def get_run_context(run_id: ResourceId) -> RunContextV1:
        _contract_only()

    @router.get(
        "/runs/{run_id}/artifacts",
        operation_id="listRunArtifacts",
        response_model=ArtifactPageV1,
    )
    def list_run_artifacts(
        run_id: ResourceId,
        limit: Limit = 50,
        after: Cursor = None,
        sort: Sort = "created_at",
        direction: Direction = "asc",
    ) -> ArtifactPageV1:
        _contract_only()

    @router.get("/artifacts/{artifact_id}", operation_id="getArtifact", response_model=ArtifactV1)
    def get_artifact(artifact_id: ResourceId) -> ArtifactV1:
        _contract_only()

    @router.get(
        "/artifacts/{artifact_id}/content",
        operation_id="getArtifactContent",
        response_model=ArtifactContentV1,
    )
    def get_artifact_content(artifact_id: ResourceId) -> ArtifactContentV1:
        _contract_only()

    @router.get(
        "/artifacts/{artifact_id}/diff",
        operation_id="getArtifactDiff",
        response_model=ArtifactDiffV1,
    )
    def get_artifact_diff(artifact_id: ResourceId) -> ArtifactDiffV1:
        _contract_only()

    @router.get("/services", operation_id="listServices", response_model=ServicePageV1)
    def list_services(
        limit: Limit = 50,
        after: Cursor = None,
        sort: Sort = "display_name",
        direction: Direction = "asc",
    ) -> ServicePageV1:
        _contract_only()

    @router.post(
        "/services/{service_id}/restart",
        operation_id="restartService",
        response_model=OperationV1,
        status_code=202,
    )
    def restart_service(
        service_id: ResourceId, idempotency_key: IdempotencyKey, if_match: IfMatch
    ) -> OperationV1:
        _contract_only()

    @router.get(
        "/core/operations/{operation_id}",
        operation_id="getCoreOperation",
        response_model=OperationV1,
    )
    def get_core_operation(operation_id: ResourceId) -> OperationV1:
        _contract_only()

    @router.get(
        "/core/logs/{logs_ref}",
        operation_id="getCoreLogsByRef",
        response_model=ReferencedLogPageV1,
    )
    def get_core_logs_by_ref(
        logs_ref: ResourceId,
        limit: Limit = 50,
        after: Cursor = None,
        sort: Sort = "sequence",
        direction: Direction = "asc",
    ) -> ReferencedLogPageV1:
        _contract_only()

    @router.get(
        "/services/{service_id}/logs",
        operation_id="listServiceLogs",
        response_model=LogPageV1,
    )
    def list_service_logs(
        service_id: ResourceId,
        limit: Limit = 50,
        after: Cursor = None,
        sort: Sort = "occurred_at",
        direction: Direction = "asc",
    ) -> LogPageV1:
        _contract_only()

    @router.post(
        "/diagnostics",
        operation_id="createDiagnostic",
        response_model=DiagnosticReportV1,
        status_code=202,
    )
    def create_diagnostic(
        request: DiagnosticRequestV1, idempotency_key: IdempotencyKey
    ) -> DiagnosticReportV1:
        _contract_only()

    @router.get(
        "/diagnostics/{diagnostic_id}",
        operation_id="getDiagnostic",
        response_model=DiagnosticReportV1,
    )
    def get_diagnostic(diagnostic_id: ResourceId) -> DiagnosticReportV1:
        _contract_only()

    @router.delete(
        "/diagnostics/{diagnostic_id}",
        operation_id="deleteDiagnostic",
        status_code=204,
        response_model=None,
    )
    def delete_diagnostic(
        diagnostic_id: ResourceId,
        idempotency_key: IdempotencyKey,
        if_match: IfMatch,
    ) -> None:
        _contract_only()

    @router.post(
        "/maintenance/cache-cleanup",
        operation_id="cleanupCaches",
        response_model=OperationV1,
        status_code=202,
    )
    def cleanup_caches(
        request: CacheCleanupRequestV1, idempotency_key: IdempotencyKey
    ) -> OperationV1:
        _contract_only()

    @router.get(
        "/events",
        operation_id="subscribeDesktopEvents",
        response_model=EventEnvelopeV1,
        response_class=StreamingResponse,
        responses={
            200: {
                "description": (
                    "SSE stream with at-least-once delivery, 15-second heartbeats, "
                    "and bounded replay. Each data field is EventEnvelopeV1."
                ),
                "content": {
                    "text/event-stream": {
                        "schema": {"$ref": "#/components/schemas/EventEnvelopeV1"}
                    }
                },
            }
        },
    )
    def subscribe_events(
        last_event_id: Annotated[
            str | None,
            Header(alias="Last-Event-ID", min_length=1, max_length=256),
        ] = None,
    ) -> EventEnvelopeV1:
        _contract_only()

    app.include_router(router)
    if provider is not None:
        _bind_provider(app, provider)
    return app


contract_app = create_contract_app()
