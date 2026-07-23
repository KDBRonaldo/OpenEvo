"""Canonical FastAPI routes and optional Core Control API v1 provider binding."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from functools import wraps
import re
import secrets
from typing import Annotated, Literal, Protocol

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request, Security
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import models as m


_CONTRACT_ONLY_MESSAGE = "This app defines the Core Control API v1 contract and has no provider."
_VERSIONED_PATH = re.compile(r"^/v([0-9]+)(?:/|$)")
_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="CoreBearerAuth",
    description="Bearer credential owned by the Desktop sidecar.",
)


async def _declare_bearer_security(
    _credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Security(_bearer),
    ],
) -> None:
    """Declare the security scheme without implementing an auth provider."""


class CoreControlApiProviderV1(Protocol):
    """Business provider dispatched through the frozen operation IDs."""

    def authenticate(self, authorization_values: tuple[bytes, ...]) -> bool: ...

    def invoke(self, operation_id: str, arguments: Mapping[str, object]) -> object: ...

    async def invoke_async(self, operation_id: str, arguments: Mapping[str, object]) -> object: ...


class CoreControlHTTPError(Exception):
    """Typed provider error rendered as the frozen ``ApiErrorV1`` shape."""

    def __init__(
        self,
        status_code: int,
        *,
        code: str,
        message: str,
        category: m.ErrorCategory,
        retryable: bool,
        repair_action: m.RepairAction,
        next_action: str,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error = m.ApiErrorV1(
            request_id=f"request-{secrets.token_hex(16)}",
            code=code,
            http_status=status_code,
            message=message,
            severity=m.ErrorSeverity.BLOCKING,
            category=category,
            retryable=retryable,
            repair_action=repair_action,
            next_action=next_action,
        )
        self.headers = dict(headers or {})

    @classmethod
    def from_error(cls, error: m.ApiErrorV1) -> CoreControlHTTPError:
        instance = cls.__new__(cls)
        Exception.__init__(instance, error.message)
        instance.status_code = error.http_status
        instance.error = error
        instance.headers = {}
        return instance


def _provider_error_response(exc: CoreControlHTTPError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.error.model_dump(mode="json"),
        headers=exc.headers,
    )


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


def _bind_provider(app: FastAPI, provider: CoreControlApiProviderV1) -> None:
    for route in _iter_api_routes(app.routes):
        if route.operation_id is None:
            continue
        operation_id = route.operation_id
        original_endpoint = route.endpoint

        @wraps(original_endpoint)
        async def invoke_provider(
            _operation_id: str = operation_id, **arguments: object
        ) -> object:
            return await provider.invoke_async(_operation_id, arguments)

        route.endpoint = invoke_provider
        route.dependant.call = invoke_provider


def _not_implemented() -> JSONResponse:
    payload = m.ContractOnlyResponseV1(
        code="contract_only_not_implemented",
        message=_CONTRACT_ONLY_MESSAGE,
    )
    return JSONResponse(status_code=501, content=payload.model_dump(mode="json"))


_ERROR_RESPONSES = {
    400: {"model": m.ApiErrorV1, "description": "Invalid request or cursor."},
    401: {"model": m.ApiErrorV1, "description": "Missing or invalid Core bearer."},
    404: {"model": m.ApiErrorV1, "description": "Resource not found."},
    409: {"model": m.ApiErrorV1, "description": "Resource or idempotency conflict."},
    410: {"model": m.ApiErrorV1, "description": "Cursor or event replay expired."},
    412: {"model": m.ApiErrorV1, "description": "ETag precondition failed."},
    422: {"model": m.ApiErrorV1, "description": "Closed contract validation failed."},
    426: {"model": m.ApiErrorV1, "description": "Contract version unsupported."},
    500: {"model": m.ApiErrorV1, "description": "Core internal error."},
    501: {
        "model": m.ContractOnlyResponseV1,
        "description": "The schema-only contract app has no business provider.",
    },
    503: {"model": m.ApiErrorV1, "description": "Required Core authority is unavailable."},
}

IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=256,
        description="Opaque idempotency key scoped to principal, route, and resource.",
    ),
]
IfMatch = Annotated[
    str,
    m.StringConstraints(pattern=r'^"[0-9a-f]{64}"$'),
    Header(
        alias="If-Match",
        min_length=66,
        max_length=66,
        description="ETag of the mutable resource.",
    ),
]
IfProjectMatch = Annotated[
    str,
    m.StringConstraints(pattern=r'^"[0-9a-f]{64}"$'),
    Header(
        alias="If-Project-Match",
        min_length=66,
        max_length=66,
        description=(
            "For finalization, this must equal both upload.project_etag and the current "
            "project ETag; the provider also verifies upload.project_snapshot is still current."
        ),
    ),
]
ResourceId = Annotated[str, m.StringConstraints(min_length=1, max_length=128)]
PageLimit = Annotated[int, Query(ge=1, le=100)]
PageCursor = Annotated[str | None, Query(min_length=1, max_length=512)]
LastEventId = Annotated[
    str | None,
    Header(alias="Last-Event-ID", min_length=1, max_length=512),
]


def create_core_control_contract_app(
    provider: CoreControlApiProviderV1 | None = None,
    *,
    mutation_enabled: bool = True,
) -> FastAPI:
    """Create the canonical app, optionally bound to a real business provider."""

    app = FastAPI(
        title="OpenEvo Core Control API v1 Contract (Schema Only)",
        summary="Closed product contract between Desktop sidecar and remote Core.",
        description=(
            "This FastAPI application is a schema source only. It does not implement "
            "Core business behavior and every operation returns HTTP 501 when called."
        ),
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get(
        "/version",
        operation_id="discoverCoreContractVersionV1",
        response_model=m.VersionResponseV1,
        responses=_ERROR_RESPONSES,
        tags=["discovery"],
    )
    async def version() -> Response:
        return _not_implemented()

    @app.get(
        "/health",
        operation_id="discoverCoreHealthV1",
        response_model=m.HealthResponseV1,
        responses=_ERROR_RESPONSES,
        tags=["discovery"],
    )
    async def health() -> Response:
        return _not_implemented()

    router = APIRouter(
        prefix="/v1",
        dependencies=[Depends(_declare_bearer_security)],
    )

    @router.get(
        "/status",
        operation_id="getCoreStatusV1",
        response_model=m.CoreStatusV1,
        responses=_ERROR_RESPONSES,
        tags=["status"],
    )
    async def status() -> Response:
        return _not_implemented()

    @router.post(
        "/environment/doctor",
        operation_id="doctorCoreEnvironmentV1",
        response_model=m.EnvironmentDoctorResponseV1,
        responses=_ERROR_RESPONSES,
        tags=["environment"],
    )
    async def doctor(
        request: m.EnvironmentDoctorRequestV1,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _not_implemented()

    @router.post(
        "/environment/repair",
        operation_id="repairCoreEnvironmentV1",
        response_model=m.OperationV1,
        status_code=202,
        responses=_ERROR_RESPONSES,
        tags=["environment"],
    )
    async def repair(
        request: m.EnvironmentRepairRequestV1,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _not_implemented()

    @router.get(
        "/capabilities",
        operation_id="getCoreCapabilitiesV1",
        response_model=m.CapabilitiesResponseV1,
        responses=_ERROR_RESPONSES,
        tags=["capabilities"],
    )
    async def capabilities(
        execution_mode: Annotated[m.ExecutionMode, Query()],
    ) -> Response:
        return _not_implemented()

    @router.get(
        "/projects",
        operation_id="listCoreProjectsV1",
        response_model=m.ProjectPageV1,
        responses=_ERROR_RESPONSES,
        tags=["projects"],
    )
    async def list_projects(
        limit: PageLimit = 50,
        after: PageCursor = None,
        sort: Annotated[Literal["created_at", "updated_at", "name"], Query()] = "updated_at",
        direction: Annotated[Literal["asc", "desc"], Query()] = "desc",
    ) -> Response:
        return _not_implemented()

    @router.post(
        "/projects",
        operation_id="createCoreProjectV1",
        response_model=m.ProjectV1,
        status_code=201,
        responses=_ERROR_RESPONSES,
        tags=["projects"],
    )
    async def create_project(
        request: m.ProjectCreateV1,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _not_implemented()

    @router.get(
        "/projects/{project_id}",
        operation_id="getCoreProjectV1",
        response_model=m.ProjectV1,
        responses=_ERROR_RESPONSES,
        tags=["projects"],
    )
    async def get_project(project_id: ResourceId) -> Response:
        return _not_implemented()

    @router.patch(
        "/projects/{project_id}",
        operation_id="patchCoreProjectV1",
        response_model=m.ProjectV1,
        responses=_ERROR_RESPONSES,
        tags=["projects"],
    )
    async def patch_project(
        project_id: ResourceId,
        request: m.ProjectPatchV1,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _not_implemented()

    @router.delete(
        "/projects/{project_id}",
        operation_id="deleteCoreProjectV1",
        status_code=204,
        responses=_ERROR_RESPONSES,
        tags=["projects"],
    )
    async def delete_project(
        project_id: ResourceId,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _not_implemented()

    @router.get(
        "/projects/{project_id}/revisions",
        operation_id="listCoreProjectRevisionsV1",
        response_model=m.RevisionPageV1,
        responses=_ERROR_RESPONSES,
        tags=["revisions"],
    )
    async def list_revisions(
        project_id: ResourceId,
        limit: PageLimit = 50,
        after: PageCursor = None,
        sort: Annotated[Literal["generation", "created_at", "updated_at"], Query()] = (
            "generation"
        ),
        direction: Annotated[Literal["asc", "desc"], Query()] = "desc",
    ) -> Response:
        return _not_implemented()

    @router.get(
        "/projects/{project_id}/revisions/head",
        operation_id="getCoreProjectRevisionHeadV1",
        response_model=m.RevisionHeadV1,
        responses=_ERROR_RESPONSES,
        tags=["revisions"],
    )
    async def revision_head(project_id: ResourceId) -> Response:
        return _not_implemented()

    @router.get(
        "/revisions/{revision_id}",
        operation_id="getCoreRevisionV1",
        response_model=m.RevisionV1,
        responses=_ERROR_RESPONSES,
        tags=["revisions"],
    )
    async def get_revision(revision_id: ResourceId) -> Response:
        return _not_implemented()

    @router.post(
        "/projects/{project_id}/workspace-uploads",
        operation_id="createCoreWorkspaceUploadV1",
        response_model=m.WorkspaceUploadSessionV1,
        status_code=201,
        responses=_ERROR_RESPONSES,
        tags=["workspace"],
    )
    async def create_workspace_upload(
        project_id: ResourceId,
        request: m.WorkspaceUploadCreateV1,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _not_implemented()

    @router.get(
        "/projects/{project_id}/workspace-uploads/{upload_id}",
        operation_id="getCoreWorkspaceUploadV1",
        response_model=m.WorkspaceUploadSessionV1,
        responses=_ERROR_RESPONSES,
        tags=["workspace"],
    )
    async def get_workspace_upload(project_id: ResourceId, upload_id: ResourceId) -> Response:
        return _not_implemented()

    @router.put(
        "/projects/{project_id}/workspace-uploads/{upload_id}/chunk",
        operation_id="putCoreWorkspaceUploadChunkV1",
        response_model=m.WorkspaceUploadSessionV1,
        responses=_ERROR_RESPONSES,
        tags=["workspace"],
    )
    async def put_workspace_upload_chunk(
        project_id: ResourceId,
        upload_id: ResourceId,
        request: m.WorkspaceUploadChunkV1,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _not_implemented()

    @router.post(
        "/projects/{project_id}/workspace-uploads/{upload_id}/finalize",
        operation_id="finalizeCoreWorkspaceUploadV1",
        response_model=m.WorkspaceUploadFinalizeResponseV1,
        status_code=201,
        responses=_ERROR_RESPONSES,
        tags=["workspace"],
    )
    async def finalize_workspace_upload(
        project_id: ResourceId,
        upload_id: ResourceId,
        request: m.WorkspaceUploadFinalizeV1,
        if_match: IfMatch,
        if_project_match: IfProjectMatch,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _not_implemented()

    @router.post(
        "/projects/{project_id}/workspace-uploads/{upload_id}/abort",
        operation_id="abortCoreWorkspaceUploadV1",
        response_model=m.WorkspaceUploadSessionV1,
        responses=_ERROR_RESPONSES,
        tags=["workspace"],
    )
    async def abort_workspace_upload(
        project_id: ResourceId,
        upload_id: ResourceId,
        request: m.WorkspaceUploadAbortV1,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _not_implemented()

    @router.post(
        "/projects/{project_id}/validate",
        operation_id="validateCoreProjectV1",
        response_model=m.ProjectValidationResponseV1,
        responses=_ERROR_RESPONSES,
        tags=["projects"],
    )
    async def validate_project(
        project_id: ResourceId,
        request: m.ProjectValidationRequestV1,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _not_implemented()

    @router.get(
        "/runs",
        operation_id="listCoreRunsV1",
        response_model=m.RunPageV1,
        responses=_ERROR_RESPONSES,
        tags=["runs"],
    )
    async def list_runs(
        limit: PageLimit = 50,
        after: PageCursor = None,
        sort: Annotated[
            Literal["created_at", "started_at", "finished_at"], Query()
        ] = "created_at",
        direction: Annotated[Literal["asc", "desc"], Query()] = "desc",
        project_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
        status: Annotated[m.RunStatus | None, Query()] = None,
    ) -> Response:
        return _not_implemented()

    @router.post(
        "/runs",
        operation_id="createCoreRunV1",
        response_model=m.RunV1,
        status_code=202,
        responses=_ERROR_RESPONSES,
        tags=["runs"],
    )
    async def create_run(
        request: m.RunCreateV1,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _not_implemented()

    @router.get(
        "/runs/{run_id}",
        operation_id="getCoreRunV1",
        response_model=m.RunV1,
        responses=_ERROR_RESPONSES,
        tags=["runs"],
    )
    async def get_run(run_id: ResourceId) -> Response:
        return _not_implemented()

    @router.delete(
        "/runs/{run_id}",
        operation_id="deleteCoreRunV1",
        status_code=204,
        responses=_ERROR_RESPONSES,
        tags=["runs"],
    )
    async def delete_run(
        run_id: ResourceId,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _not_implemented()

    @router.post(
        "/runs/{run_id}/cancel",
        operation_id="cancelCoreRunV1",
        response_model=m.RunV1,
        status_code=202,
        responses=_ERROR_RESPONSES,
        tags=["runs"],
    )
    async def cancel_run(
        run_id: ResourceId,
        request: m.RunCancelRequestV1,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _not_implemented()

    @router.post(
        "/runs/{run_id}/retry",
        operation_id="retryCoreRunV1",
        response_model=m.RunV1,
        status_code=202,
        responses=_ERROR_RESPONSES,
        tags=["runs"],
    )
    async def retry_run(
        run_id: ResourceId,
        request: m.RunRetryRequestV1,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _not_implemented()

    @router.get(
        "/runs/{run_id}/timeline",
        operation_id="getCoreRunTimelineV1",
        response_model=m.RunTimelinePageV1,
        responses=_ERROR_RESPONSES,
        tags=["runs"],
    )
    async def run_timeline(
        run_id: ResourceId,
        limit: PageLimit = 50,
        after: PageCursor = None,
        sort: Annotated[Literal["sequence", "occurred_at"], Query()] = "sequence",
        direction: Annotated[Literal["asc", "desc"], Query()] = "asc",
    ) -> Response:
        return _not_implemented()

    @router.get(
        "/runs/{run_id}/logs",
        operation_id="getCoreRunLogsV1",
        response_model=m.LogPageV1,
        responses=_ERROR_RESPONSES,
        tags=["runs"],
    )
    async def run_logs(
        run_id: ResourceId,
        limit: PageLimit = 100,
        after: PageCursor = None,
        sort: Annotated[Literal["sequence", "occurred_at"], Query()] = "sequence",
        direction: Annotated[Literal["asc", "desc"], Query()] = "asc",
        stream: Annotated[m.LogStream | None, Query()] = None,
    ) -> Response:
        return _not_implemented()

    @router.get(
        "/runs/{run_id}/context",
        operation_id="getCoreRunContextV1",
        response_model=m.RunContextV1,
        responses=_ERROR_RESPONSES,
        tags=["runs"],
    )
    async def run_context(run_id: ResourceId) -> Response:
        return _not_implemented()

    @router.get(
        "/runs/{run_id}/artifacts",
        operation_id="listCoreRunArtifactsV1",
        response_model=m.ArtifactPageV1,
        responses=_ERROR_RESPONSES,
        tags=["artifacts"],
    )
    async def run_artifacts(
        run_id: ResourceId,
        limit: PageLimit = 50,
        after: PageCursor = None,
        sort: Annotated[Literal["created_at", "title", "artifact_type"], Query()] = "created_at",
        direction: Annotated[Literal["asc", "desc"], Query()] = "asc",
        artifact_type: Annotated[m.ArtifactType | None, Query()] = None,
    ) -> Response:
        return _not_implemented()

    @router.get(
        "/projects/{project_id}/artifacts/{artifact_id}",
        operation_id="getCoreArtifactV1",
        response_model=m.ArtifactSummaryV1,
        responses=_ERROR_RESPONSES,
        tags=["artifacts"],
    )
    async def get_artifact(project_id: ResourceId, artifact_id: ResourceId) -> Response:
        return _not_implemented()

    @router.get(
        "/projects/{project_id}/artifacts/{artifact_id}/content",
        operation_id="getCoreArtifactContentV1",
        response_model=m.ArtifactContentV1,
        responses=_ERROR_RESPONSES,
        tags=["artifacts"],
    )
    async def artifact_content(project_id: ResourceId, artifact_id: ResourceId) -> Response:
        return _not_implemented()

    @router.get(
        "/projects/{project_id}/artifacts/{artifact_id}/diff",
        operation_id="getCoreArtifactDiffV1",
        response_model=m.ArtifactDiffV1,
        responses=_ERROR_RESPONSES,
        tags=["artifacts"],
    )
    async def artifact_diff(
        project_id: ResourceId,
        artifact_id: ResourceId,
        previous_artifact_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
    ) -> Response:
        return _not_implemented()

    @router.get(
        "/services",
        operation_id="listCoreServicesV1",
        response_model=m.ServicePageV1,
        responses=_ERROR_RESPONSES,
        tags=["services"],
    )
    async def list_services(
        limit: PageLimit = 50,
        after: PageCursor = None,
        sort: Annotated[Literal["kind", "status", "updated_at"], Query()] = "kind",
        direction: Annotated[Literal["asc", "desc"], Query()] = "asc",
    ) -> Response:
        return _not_implemented()

    @router.get(
        "/services/{service_id}",
        operation_id="getCoreServiceV1",
        response_model=m.ServiceSummaryV1,
        responses=_ERROR_RESPONSES,
        tags=["services"],
    )
    async def get_service(service_id: ResourceId) -> Response:
        return _not_implemented()

    @router.post(
        "/services/{service_id}/restart",
        operation_id="restartCoreServiceV1",
        response_model=m.OperationV1,
        status_code=202,
        responses=_ERROR_RESPONSES,
        tags=["services"],
    )
    async def restart_service(
        service_id: ResourceId,
        request: m.ServiceRestartRequestV1,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _not_implemented()

    @router.get(
        "/services/{service_id}/logs",
        operation_id="getCoreServiceLogsV1",
        response_model=m.LogPageV1,
        responses=_ERROR_RESPONSES,
        tags=["services"],
    )
    async def service_logs(
        service_id: ResourceId,
        limit: PageLimit = 100,
        after: PageCursor = None,
        sort: Annotated[Literal["sequence", "occurred_at"], Query()] = "sequence",
        direction: Annotated[Literal["asc", "desc"], Query()] = "asc",
    ) -> Response:
        return _not_implemented()

    @router.get(
        "/operations/{operation_id}",
        operation_id="getCoreOperationV1",
        response_model=m.OperationV1,
        responses=_ERROR_RESPONSES,
        tags=["operations"],
    )
    async def get_operation(operation_id: ResourceId) -> Response:
        return _not_implemented()

    @router.post(
        "/operations/{operation_id}/cancel",
        operation_id="cancelCoreOperationV1",
        response_model=m.OperationV1,
        status_code=202,
        responses={
            **_ERROR_RESPONSES,
            409: {
                "model": m.ApiErrorV1,
                "description": (
                    "Conflict: operation_kind_not_cancellable when the descriptor forbids "
                    "cancellation, or idempotency_key_reused for a conflicting replay."
                ),
            },
        },
        tags=["operations"],
    )
    async def cancel_operation(
        operation_id: ResourceId,
        request: m.OperationCancelRequestV1,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _not_implemented()

    @router.get(
        "/logs/{logs_ref}",
        operation_id="getCoreLogsByRefV1",
        response_model=m.ReferencedLogPageV1,
        responses=_ERROR_RESPONSES,
        tags=["logs"],
    )
    async def logs_by_ref(
        logs_ref: ResourceId,
        limit: PageLimit = 100,
        after: PageCursor = None,
        sort: Annotated[Literal["sequence", "occurred_at"], Query()] = "sequence",
        direction: Annotated[Literal["asc", "desc"], Query()] = "asc",
    ) -> Response:
        return _not_implemented()

    @router.post(
        "/diagnostics",
        operation_id="createCoreDiagnosticV1",
        response_model=m.DiagnosticV1,
        status_code=202,
        responses=_ERROR_RESPONSES,
        tags=["diagnostics"],
    )
    async def create_diagnostic(
        request: m.DiagnosticsRequestV1,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _not_implemented()

    @router.get(
        "/diagnostics/{diagnostic_id}",
        operation_id="getCoreDiagnosticV1",
        response_model=m.DiagnosticV1,
        responses=_ERROR_RESPONSES,
        tags=["diagnostics"],
    )
    async def get_diagnostic(diagnostic_id: ResourceId) -> Response:
        return _not_implemented()

    @router.delete(
        "/diagnostics/{diagnostic_id}",
        operation_id="deleteCoreDiagnosticV1",
        status_code=204,
        responses=_ERROR_RESPONSES,
        tags=["diagnostics"],
    )
    async def delete_diagnostic(
        diagnostic_id: ResourceId,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _not_implemented()

    @router.post(
        "/maintenance/cache-cleanup",
        operation_id="cleanupCoreCachesV1",
        response_model=m.OperationV1,
        status_code=202,
        responses=_ERROR_RESPONSES,
        tags=["maintenance"],
    )
    async def cache_cleanup(
        request: m.CacheCleanupRequestV1,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _not_implemented()

    @router.get(
        "/events",
        operation_id="streamCoreEventsV1",
        response_model=m.SseFrameV1,
        responses=_ERROR_RESPONSES,
        tags=["events"],
    )
    async def events(last_event_id: LastEventId = None) -> Response:
        return _not_implemented()

    app.include_router(router)

    if provider is not None:
        app.state.core_control_provider = provider
        app.state.core_control_v1_mutation_enabled = mutation_enabled

        @app.middleware("http")
        async def enforce_provider_boundary(request: Request, call_next):
            version_match = _VERSIONED_PATH.match(request.url.path)
            if version_match is not None:
                authorization_values = tuple(
                    value
                    for name, value in request.scope.get("headers", ())
                    if name.lower() == b"authorization"
                )
                if not provider.authenticate(authorization_values):
                    return _provider_error_response(
                        CoreControlHTTPError(
                            401,
                            code="core_bearer_invalid",
                            message="The Core bearer credential is missing or invalid.",
                            category=m.ErrorCategory.AUTHENTICATION,
                            retryable=False,
                            repair_action=m.RepairAction.USER_ACTION_REQUIRED,
                            next_action="Reconnect with the bearer issued for this Core instance.",
                            headers={"WWW-Authenticate": "Bearer"},
                        )
                    )
                if version_match.group(1) != "1":
                    return _provider_error_response(
                        CoreControlHTTPError(
                            426,
                            code="contract_version_unsupported",
                            message="The requested Core Control API major version is unsupported.",
                            category=m.ErrorCategory.CONTRACT,
                            retryable=False,
                            repair_action=m.RepairAction.USER_ACTION_REQUIRED,
                            next_action="Negotiate a supported major through GET /version.",
                        )
                    )
                if not mutation_enabled and request.method not in {"GET", "HEAD", "OPTIONS"}:
                    return _provider_error_response(
                        CoreControlHTTPError(
                            426,
                            code="v1_mutation_retired",
                            message=(
                                "Core Control API v1 is read-only migration input in this release."
                            ),
                            category=m.ErrorCategory.CONTRACT,
                            retryable=False,
                            repair_action=m.RepairAction.UNSUPPORTED,
                            next_action=(
                                "Reconnect through a compatible Core Control API v2 session."
                            ),
                        )
                    )
            return await call_next(request)

        @app.exception_handler(CoreControlHTTPError)
        async def provider_http_error(
            _request: Request, exc: CoreControlHTTPError
        ) -> JSONResponse:
            return _provider_error_response(exc)

        @app.exception_handler(RequestValidationError)
        async def provider_validation_error(
            _request: Request, _exc: RequestValidationError
        ) -> JSONResponse:
            return _provider_error_response(
                CoreControlHTTPError(
                    422,
                    code="request_validation_error",
                    message="The request does not satisfy the closed Core Control API v1 contract.",
                    category=m.ErrorCategory.CONTRACT,
                    retryable=False,
                    repair_action=m.RepairAction.OPENEVO_CAN_RECONFIGURE,
                    next_action="Correct the request fields and retry.",
                )
            )

        @app.exception_handler(Exception)
        async def provider_internal_error(_request: Request, _exc: Exception) -> JSONResponse:
            return _provider_error_response(
                CoreControlHTTPError(
                    500,
                    code="core_control_internal_error",
                    message="Core Control could not complete the request.",
                    category=m.ErrorCategory.INTERNAL,
                    retryable=True,
                    repair_action=m.RepairAction.OPENEVO_CAN_RETRY,
                    next_action="Inspect Core diagnostics before retrying.",
                )
            )

        _bind_provider(app, provider)

    def contract_openapi() -> dict[str, object]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            summary=app.summary,
            description=app.description,
            routes=app.routes,
        )
        schema["x-openevo-contract-only"] = True
        schema["x-openevo-business-provider"] = False
        events_operation = schema["paths"]["/v1/events"]["get"]
        events_operation["x-sse-delivery"] = "at-least-once"
        events_operation["x-sse-heartbeat-seconds"] = 15
        events_operation["x-sse-replay"] = "bounded"
        events_operation["x-sse-replay-max-events"] = 10_000
        events_operation["x-sse-cursor-expired-status"] = 410
        events_operation["responses"]["200"]["content"] = {
            "text/event-stream": {"schema": {"$ref": "#/components/schemas/SseFrameV1"}}
        }
        app.openapi_schema = schema
        return schema

    app.openapi = contract_openapi
    return app


core_control_contract_app = create_core_control_contract_app()


__all__ = [
    "CoreControlApiProviderV1",
    "CoreControlHTTPError",
    "core_control_contract_app",
    "create_core_control_contract_app",
]
