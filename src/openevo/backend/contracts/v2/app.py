"""Canonical schema-only FastAPI application for Core Control API v2."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Security
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import StringConstraints

from . import models as m


_CONTRACT_ONLY_MESSAGE = "This app defines the Core Control API v2 contract and has no provider."
_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="CoreBearerAuthV2",
    description="Bearer credential owned by the authenticated Desktop sidecar session.",
)


async def _declare_bearer_security(
    _credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Security(_bearer),
    ],
) -> None:
    """Declare the v2 security scheme without implementing a provider."""


def _not_implemented() -> JSONResponse:
    payload = m.ContractOnlyResponseV2(
        code="contract_only_not_implemented",
        message=_CONTRACT_ONLY_MESSAGE,
    )
    return JSONResponse(status_code=501, content=payload.model_dump(mode="json"))


_ERROR_RESPONSES = {
    400: {"model": m.ApiErrorV2, "description": "Invalid request or cursor."},
    401: {"model": m.ApiErrorV2, "description": "Missing or invalid Core bearer."},
    404: {"model": m.ApiErrorV2, "description": "Resource not found."},
    409: {"model": m.ApiErrorV2, "description": "Resource or idempotency conflict."},
    410: {"model": m.ApiErrorV2, "description": "Cursor or event replay expired."},
    412: {"model": m.ApiErrorV2, "description": "Expected authority identity changed."},
    422: {"model": m.ApiErrorV2, "description": "Closed contract validation failed."},
    426: {"model": m.ApiErrorV2, "description": "Contract version unsupported."},
    500: {"model": m.ApiErrorV2, "description": "Core internal error."},
    501: {
        "model": m.ContractOnlyResponseV2,
        "description": "The schema-only contract app has no business provider.",
    },
    503: {"model": m.ApiErrorV2, "description": "Required Core authority is unavailable."},
}

ResourceId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
    ),
]
IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=256,
        description="Opaque idempotency key scoped to principal, route, and authority.",
    ),
]
IfMatch = Annotated[
    str,
    StringConstraints(pattern=r'^"[0-9a-f]{64}"$'),
    Header(
        alias="If-Match",
        min_length=66,
        max_length=66,
        description="Strong ETag for the mutable resource.",
    ),
]
PageLimit = Annotated[int, Query(ge=1, le=100)]
PageCursor = Annotated[str | None, Query(min_length=1, max_length=512)]
LastEventId = Annotated[
    str | None,
    Header(alias="Last-Event-ID", min_length=1, max_length=128),
]


def create_core_control_v2_contract_app() -> FastAPI:
    """Create the v2 schema source; every endpoint intentionally returns 501."""

    app = FastAPI(
        title="OpenEvo Core Control API v2 Contract (Schema Only)",
        summary="Closed authority contract between Desktop sidecar and remote Daemon.",
        description=(
            "This FastAPI application is a schema source only. It has no business "
            "provider and every operation returns HTTP 501 when invoked."
        ),
        version="2.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get(
        "/version",
        operation_id="discoverCoreContractVersionV2",
        response_model=m.VersionResponseV2,
        responses=_ERROR_RESPONSES,
        tags=["discovery"],
    )
    async def version() -> Response:
        return _not_implemented()

    @app.get(
        "/health",
        operation_id="discoverCoreHealthV2",
        response_model=m.HealthResponseV2,
        responses=_ERROR_RESPONSES,
        tags=["discovery"],
    )
    async def health() -> Response:
        return _not_implemented()

    router = APIRouter(
        prefix="/v2",
        dependencies=[Depends(_declare_bearer_security)],
    )

    @router.get(
        "/system/status",
        operation_id="getCoreSystemStatusV2",
        response_model=m.SystemStatusV2,
        responses=_ERROR_RESPONSES,
        tags=["system"],
    )
    async def system_status() -> Response:
        return _not_implemented()

    @router.get(
        "/projects",
        operation_id="listCoreProjectsV2",
        response_model=m.ProjectPageV2,
        responses=_ERROR_RESPONSES,
        tags=["projects"],
    )
    async def list_projects(
        limit: PageLimit = 50,
        after: PageCursor = None,
        direction: Annotated[Literal["asc", "desc"], Query()] = "desc",
    ) -> Response:
        return _not_implemented()

    @router.post(
        "/projects",
        operation_id="createCoreProjectV2",
        response_model=m.ProjectV2,
        status_code=201,
        responses=_ERROR_RESPONSES,
        tags=["projects"],
    )
    async def create_project(
        request: m.ProjectCreateV2,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _not_implemented()

    @router.get(
        "/projects/{project_id}",
        operation_id="getCoreProjectV2",
        response_model=m.ProjectV2,
        responses=_ERROR_RESPONSES,
        tags=["projects"],
    )
    async def get_project(project_id: ResourceId) -> Response:
        return _not_implemented()

    @router.get(
        "/projects/{project_id}/heads",
        operation_id="listCoreProjectHeadsV2",
        response_model=m.ProjectHeadPageV2,
        responses=_ERROR_RESPONSES,
        tags=["project-heads"],
    )
    async def list_project_heads(
        project_id: ResourceId,
        limit: PageLimit = 50,
        after: PageCursor = None,
        direction: Annotated[Literal["asc", "desc"], Query()] = "desc",
    ) -> Response:
        return _not_implemented()

    @router.get(
        "/projects/{project_id}/heads/active",
        operation_id="getCoreActiveProjectHeadV2",
        response_model=m.ProjectHeadRefV2,
        responses=_ERROR_RESPONSES,
        tags=["project-heads"],
    )
    async def get_active_project_head(project_id: ResourceId) -> Response:
        return _not_implemented()

    @router.get(
        "/project-heads/{project_head_id}",
        operation_id="getCoreProjectHeadV2",
        response_model=m.ProjectHeadRefV2,
        responses=_ERROR_RESPONSES,
        tags=["project-heads"],
    )
    async def get_project_head(project_head_id: ResourceId) -> Response:
        return _not_implemented()

    @router.get(
        "/projects/{project_id}/transitions",
        operation_id="listCoreSuccessorTransitionsV2",
        response_model=m.SuccessorTransitionPageV2,
        responses=_ERROR_RESPONSES,
        tags=["transitions"],
    )
    async def list_transitions(
        project_id: ResourceId,
        limit: PageLimit = 50,
        after: PageCursor = None,
        direction: Annotated[Literal["asc", "desc"], Query()] = "desc",
    ) -> Response:
        return _not_implemented()

    @router.get(
        "/transitions/{successor_transition_id}",
        operation_id="getCoreSuccessorTransitionV2",
        response_model=m.SuccessorTransitionV2,
        responses=_ERROR_RESPONSES,
        tags=["transitions"],
    )
    async def get_transition(successor_transition_id: ResourceId) -> Response:
        return _not_implemented()

    @router.post(
        "/transitions/{successor_transition_id}/retry",
        operation_id="retryCoreSuccessorTransitionV2",
        response_model=m.OperationV2,
        status_code=202,
        responses=_ERROR_RESPONSES,
        tags=["transitions"],
    )
    async def retry_transition(
        successor_transition_id: ResourceId,
        request: m.ActionRequestV2,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _not_implemented()

    @router.post(
        "/transitions/{successor_transition_id}/abandon",
        operation_id="abandonCoreSuccessorTransitionV2",
        response_model=m.OperationV2,
        status_code=202,
        responses=_ERROR_RESPONSES,
        tags=["transitions"],
    )
    async def abandon_transition(
        successor_transition_id: ResourceId,
        request: m.ActionRequestV2,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _not_implemented()

    @router.get(
        "/tasks",
        operation_id="listCoreTasksV2",
        response_model=m.TaskPageV2,
        responses=_ERROR_RESPONSES,
        tags=["tasks"],
    )
    async def list_tasks(
        limit: PageLimit = 50,
        after: PageCursor = None,
        project_id: Annotated[ResourceId | None, Query()] = None,
        direction: Annotated[Literal["asc", "desc"], Query()] = "desc",
    ) -> Response:
        return _not_implemented()

    @router.post(
        "/tasks",
        operation_id="submitCoreTaskV2",
        response_model=m.TaskV2,
        status_code=202,
        responses=_ERROR_RESPONSES,
        tags=["tasks"],
    )
    async def submit_task(
        request: m.TaskSubmitRequestV2,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _not_implemented()

    @router.get(
        "/tasks/{task_id}",
        operation_id="getCoreTaskV2",
        response_model=m.TaskV2,
        responses=_ERROR_RESPONSES,
        tags=["tasks"],
    )
    async def get_task(task_id: ResourceId) -> Response:
        return _not_implemented()

    @router.get(
        "/tasks/{task_id}/admission",
        operation_id="getCoreTaskAdmissionV2",
        response_model=m.TaskAdmissionRefV2,
        responses=_ERROR_RESPONSES,
        tags=["tasks"],
    )
    async def get_task_admission(task_id: ResourceId) -> Response:
        return _not_implemented()

    @router.get(
        "/tasks/{task_id}/attempts",
        operation_id="listCoreTaskAttemptsV2",
        response_model=m.AttemptPageV2,
        responses=_ERROR_RESPONSES,
        tags=["attempts"],
    )
    async def list_task_attempts(
        task_id: ResourceId,
        limit: PageLimit = 50,
        after: PageCursor = None,
    ) -> Response:
        return _not_implemented()

    @router.post(
        "/tasks/{task_id}/attempts",
        operation_id="appendCoreTaskAttemptV2",
        response_model=m.AttemptRefV2,
        status_code=202,
        responses=_ERROR_RESPONSES,
        tags=["attempts"],
    )
    async def append_task_attempt(
        task_id: ResourceId,
        request: m.AttemptAppendRequestV2,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _not_implemented()

    @router.get(
        "/tasks/{task_id}/attempts/{attempt_id}",
        operation_id="getCoreTaskAttemptV2",
        response_model=m.AttemptRefV2,
        responses=_ERROR_RESPONSES,
        tags=["attempts"],
    )
    async def get_task_attempt(task_id: ResourceId, attempt_id: ResourceId) -> Response:
        return _not_implemented()

    @router.post(
        "/tasks/{task_id}/attempts/{attempt_id}/cancel",
        operation_id="cancelCoreTaskAttemptV2",
        response_model=m.OperationV2,
        status_code=202,
        responses=_ERROR_RESPONSES,
        tags=["attempts"],
    )
    async def cancel_task_attempt(
        task_id: ResourceId,
        attempt_id: ResourceId,
        request: m.TaskActionRequestV2,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _not_implemented()

    @router.post(
        "/tasks/{task_id}/close",
        operation_id="closeCoreTaskV2",
        response_model=m.OperationV2,
        status_code=202,
        responses=_ERROR_RESPONSES,
        tags=["tasks"],
    )
    async def close_task(
        task_id: ResourceId,
        request: m.TaskActionRequestV2,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _not_implemented()

    @router.get(
        "/tasks/{task_id}/timeline",
        operation_id="getCoreTaskTimelineV2",
        response_model=m.TimelinePageV2,
        responses=_ERROR_RESPONSES,
        tags=["tasks"],
    )
    async def task_timeline(
        task_id: ResourceId,
        limit: PageLimit = 50,
        after: PageCursor = None,
    ) -> Response:
        return _not_implemented()

    @router.get(
        "/tasks/{task_id}/logs",
        operation_id="getCoreTaskLogsV2",
        response_model=m.LogPageV2,
        responses=_ERROR_RESPONSES,
        tags=["tasks"],
    )
    async def task_logs(
        task_id: ResourceId,
        limit: PageLimit = 100,
        after: PageCursor = None,
    ) -> Response:
        return _not_implemented()

    @router.get(
        "/tasks/{task_id}/context",
        operation_id="getCoreTaskContextV2",
        response_model=m.TaskContextV2,
        responses=_ERROR_RESPONSES,
        tags=["tasks"],
    )
    async def task_context(task_id: ResourceId) -> Response:
        return _not_implemented()

    @router.get(
        "/tasks/{task_id}/artifacts",
        operation_id="listCoreTaskArtifactsV2",
        response_model=m.ArtifactPageV2,
        responses=_ERROR_RESPONSES,
        tags=["artifacts"],
    )
    async def task_artifacts(
        task_id: ResourceId,
        limit: PageLimit = 50,
        after: PageCursor = None,
    ) -> Response:
        return _not_implemented()

    @router.get(
        "/projects/{project_id}/artifacts/{artifact_id}",
        operation_id="getCoreArtifactV2",
        response_model=m.ArtifactV2,
        responses=_ERROR_RESPONSES,
        tags=["artifacts"],
    )
    async def get_artifact(project_id: ResourceId, artifact_id: ResourceId) -> Response:
        return _not_implemented()

    @router.get(
        "/projects/{project_id}/artifacts/{artifact_id}/content",
        operation_id="getCoreArtifactContentV2",
        response_model=m.ArtifactContentV2,
        responses=_ERROR_RESPONSES,
        tags=["artifacts"],
    )
    async def get_artifact_content(
        project_id: ResourceId, artifact_id: ResourceId
    ) -> Response:
        return _not_implemented()

    @router.get(
        "/services",
        operation_id="listCoreServicesV2",
        response_model=m.ServicePageV2,
        responses=_ERROR_RESPONSES,
        tags=["services"],
    )
    async def list_services(
        limit: PageLimit = 50,
        after: PageCursor = None,
    ) -> Response:
        return _not_implemented()

    @router.get(
        "/services/{service_id}",
        operation_id="getCoreServiceV2",
        response_model=m.ServiceV2,
        responses=_ERROR_RESPONSES,
        tags=["services"],
    )
    async def get_service(service_id: ResourceId) -> Response:
        return _not_implemented()

    @router.post(
        "/services/{service_id}/restart",
        operation_id="restartCoreServiceV2",
        response_model=m.OperationV2,
        status_code=202,
        responses=_ERROR_RESPONSES,
        tags=["services"],
    )
    async def restart_service(
        service_id: ResourceId,
        request: m.ActionRequestV2,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _not_implemented()

    @router.get(
        "/services/{service_id}/logs",
        operation_id="getCoreServiceLogsV2",
        response_model=m.LogPageV2,
        responses=_ERROR_RESPONSES,
        tags=["services"],
    )
    async def service_logs(
        service_id: ResourceId,
        limit: PageLimit = 100,
        after: PageCursor = None,
    ) -> Response:
        return _not_implemented()

    @router.get(
        "/operations/{operation_id}",
        operation_id="getCoreOperationV2",
        response_model=m.OperationV2,
        responses=_ERROR_RESPONSES,
        tags=["operations"],
    )
    async def get_operation(operation_id: ResourceId) -> Response:
        return _not_implemented()

    @router.post(
        "/operations/{operation_id}/cancel",
        operation_id="cancelCoreOperationV2",
        response_model=m.OperationV2,
        status_code=202,
        responses=_ERROR_RESPONSES,
        tags=["operations"],
    )
    async def cancel_operation(
        operation_id: ResourceId,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _not_implemented()

    @router.post(
        "/diagnostics",
        operation_id="createCoreDiagnosticV2",
        response_model=m.DiagnosticV2,
        status_code=202,
        responses=_ERROR_RESPONSES,
        tags=["diagnostics"],
    )
    async def create_diagnostic(
        request: m.DiagnosticRequestV2,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _not_implemented()

    @router.get(
        "/diagnostics/{diagnostic_id}",
        operation_id="getCoreDiagnosticV2",
        response_model=m.DiagnosticV2,
        responses=_ERROR_RESPONSES,
        tags=["diagnostics"],
    )
    async def get_diagnostic(diagnostic_id: ResourceId) -> Response:
        return _not_implemented()

    @router.delete(
        "/diagnostics/{diagnostic_id}",
        operation_id="deleteCoreDiagnosticV2",
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
        operation_id="cleanupCoreCachesV2",
        response_model=m.OperationV2,
        status_code=202,
        responses=_ERROR_RESPONSES,
        tags=["maintenance"],
    )
    async def cache_cleanup(
        request: m.CacheCleanupRequestV2,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _not_implemented()

    @router.get(
        "/events",
        operation_id="streamCoreEventsV2",
        response_model=m.SseFrameV2,
        responses=_ERROR_RESPONSES,
        tags=["events"],
    )
    async def events(last_event_id: LastEventId = None) -> Response:
        return _not_implemented()

    app.include_router(router)

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
        events_operation = schema["paths"]["/v2/events"]["get"]
        events_operation["x-sse-delivery"] = "at-least-once"
        events_operation["x-sse-heartbeat-seconds"] = 15
        events_operation["x-sse-replay"] = "bounded"
        events_operation["x-sse-replay-max-events"] = 10_000
        events_operation["x-sse-cursor-expired-status"] = 410
        events_operation["responses"]["200"]["content"] = {
            "text/event-stream": {"schema": {"$ref": "#/components/schemas/SseFrameV2"}}
        }
        app.openapi_schema = schema
        return schema

    app.openapi = contract_openapi
    return app


core_control_v2_contract_app = create_core_control_v2_contract_app()


__all__ = [
    "core_control_v2_contract_app",
    "create_core_control_v2_contract_app",
]
