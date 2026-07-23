"""Canonical schema-only FastAPI application for Desktop Local API v2."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from functools import wraps
from typing import Annotated, Protocol

from fastapi import APIRouter, FastAPI, Header, HTTPException, Query, Request, Security
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse, Response
from fastapi.security import APIKeyHeader
from pydantic import StringConstraints

from desktop.sidecar.ssh_catalog_errors import (
    SshCatalogActionCapacityError,
    SshCatalogGenerationChangedError,
    SshCatalogIdempotencyConflictError,
)

from . import models as m


DESKTOP_SESSION_HEADER = "X-OpenEvo-Desktop-Session"
IDEMPOTENCY_HEADER = "Idempotency-Key"
RESOURCE_GENERATION_HEADER = "X-OpenEvo-Resource-Generation"
IF_MATCH_HEADER = "If-Match"
_CONTRACT_ONLY_MESSAGE = (
    "This app defines the Desktop Local API v2 contract and has no provider."
)

_desktop_session = APIKeyHeader(
    name=DESKTOP_SESSION_HEADER,
    scheme_name="DesktopSessionV2",
    description="Ephemeral Desktop session credential issued by the native host.",
    auto_error=False,
)


async def _require_desktop_session(
    session: Annotated[str | None, Security(_desktop_session)],
) -> None:
    if session is None:
        raise HTTPException(status_code=401, detail="Desktop session is required.")

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
        alias=IDEMPOTENCY_HEADER,
        min_length=16,
        max_length=256,
        description="Stable key binding the session, route, generation, and request.",
    ),
]
ResourceGeneration = Annotated[
    int,
    Header(
        alias=RESOURCE_GENERATION_HEADER,
        ge=0,
        le=m.MAX_JAVASCRIPT_SAFE_INTEGER,
        description="Expected local or remote resource generation for mutation.",
    ),
]
IfMatch = Annotated[
    str,
    Header(
        alias=IF_MATCH_HEADER,
        pattern=r'^"[0-9a-f]{64}"$',
        description="Current strong ETag for the mutable resource.",
    ),
]
Limit = Annotated[int, Query(ge=1, le=100)]
Cursor = Annotated[str | None, Query(min_length=1, max_length=512)]
LastEventId = Annotated[
    str | None,
    Header(alias="Last-Event-ID", min_length=1, max_length=128),
]


_ERROR_RESPONSES = {
    400: {"model": m.DesktopErrorV2, "description": "Invalid request or cursor."},
    401: {"model": m.DesktopErrorV2, "description": "Desktop session is invalid."},
    403: {"model": m.DesktopErrorV2, "description": "Desktop session is missing."},
    404: {"model": m.DesktopErrorV2, "description": "Resource not found."},
    409: {"model": m.DesktopErrorV2, "description": "Resource or idempotency conflict."},
    410: {"model": m.DesktopErrorV2, "description": "Cursor or replay window expired."},
    412: {"model": m.DesktopErrorV2, "description": "Authority generation changed."},
    422: {"model": m.DesktopErrorV2, "description": "Closed contract validation failed."},
    426: {"model": m.DesktopErrorV2, "description": "Contract version is incompatible."},
    501: {
        "model": m.ContractOnlyResponseV2,
        "description": "The schema-only contract app has no provider.",
    },
    503: {"model": m.DesktopErrorV2, "description": "Required authority is unavailable."},
}


def _contract_only() -> JSONResponse:
    payload = m.ContractOnlyResponseV2(
        code="contract_only_not_implemented",
        message=_CONTRACT_ONLY_MESSAGE,
    )
    return JSONResponse(status_code=501, content=payload.model_dump(mode="json"))


class DesktopLocalApiProviderV2(Protocol):
    """Execution provider bound to canonical Desktop Local API v2 routes."""

    def invoke(self, operation_id: str, arguments: Mapping[str, object]) -> object: ...


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


def _bind_provider(app: FastAPI, provider: DesktopLocalApiProviderV2) -> None:
    for route in _iter_api_routes(app.routes):
        if route.operation_id is None:
            continue
        operation_id = route.operation_id
        original_endpoint = route.endpoint

        @wraps(original_endpoint)
        async def invoke_provider(
            _operation_id: str = operation_id,
            **arguments: object,
        ) -> object:
            return provider.invoke(_operation_id, arguments)

        route.endpoint = invoke_provider
        route.dependant.call = invoke_provider


def _catalog_error(
    *,
    status_code: int,
    code: str,
    summary: str,
    retryable: bool,
    action: m.DesktopActionV2,
) -> JSONResponse:
    error = m.DesktopErrorV2(
        code=code,
        summary=summary,
        retryable=retryable,
        action=action,
        affected_resource_id=None,
    )
    return JSONResponse(status_code=status_code, content=error.model_dump(mode="json"))


def create_desktop_local_v2_contract_app(
    provider: DesktopLocalApiProviderV2 | None = None,
    *,
    _app_factory: Callable[..., FastAPI] = FastAPI,
) -> FastAPI:
    """Build the Local API v2 schema source with no business implementation."""

    app = _app_factory(
        title="OpenEvo Desktop Local API v2 Contract (Schema Only)",
        summary="Strict renderer-to-sidecar system-OpenSSH and Core projection contract.",
        description=(
            "The native host supplies the endpoint and Desktop session credential. "
            "The contract app intentionally implements no product behavior."
        ),
        version="2.0.0",
        openapi_version="3.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        separate_input_output_schemas=False,
    )

    @app.get(
        "/version",
        operation_id="getDesktopContractVersionV2",
        response_model=m.DesktopVersionV2,
        responses={426: _ERROR_RESPONSES[426], 501: _ERROR_RESPONSES[501]},
        tags=["discovery"],
    )
    async def version() -> Response:
        return _contract_only()

    @app.get(
        "/health",
        operation_id="getDesktopHealthV2",
        response_model=m.DesktopHealthV2,
        responses={422: _ERROR_RESPONSES[422], 501: _ERROR_RESPONSES[501]},
        tags=["discovery"],
    )
    async def health() -> Response:
        return _contract_only()

    router = APIRouter(
        prefix="/desktop/v2",
        dependencies=[Security(_require_desktop_session)],
        responses=_ERROR_RESPONSES,
    )

    @router.get("/state", operation_id="getDesktopStateV2", response_model=m.DesktopStateV2)
    async def state() -> Response:
        return _contract_only()

    @router.get(
        "/ssh-hosts",
        operation_id="listConfiguredSshHostsV2",
        response_model=m.SshHostCatalogV2,
    )
    async def ssh_hosts() -> Response:
        return _contract_only()

    @router.post(
        "/ssh-hosts/rescan",
        operation_id="rescanConfiguredSshHostsV2",
        response_model=m.SshHostCatalogV2,
        status_code=202,
    )
    async def rescan_ssh_hosts(
        request: m.SshHostCatalogRescanV2,
        resource_generation: ResourceGeneration,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _contract_only()

    @router.get(
        "/profiles",
        operation_id="listRemoteWorkspaceProfilesV2",
        response_model=m.RemoteProfilePageV2,
    )
    async def profiles(
        limit: Limit = 50,
        after: Cursor = None,
    ) -> Response:
        return _contract_only()

    @router.post(
        "/profiles",
        operation_id="createSystemOpenSshProfileV2",
        response_model=m.RemoteWorkspaceProfileV2,
        status_code=201,
    )
    async def create_profile(
        request: m.SystemOpenSshProfileCreateV2,
        resource_generation: ResourceGeneration,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _contract_only()

    @router.get(
        "/profiles/{profile_id}",
        operation_id="getRemoteWorkspaceProfileV2",
        response_model=m.RemoteProfileV2,
    )
    async def profile(profile_id: ResourceId) -> Response:
        return _contract_only()

    @router.patch(
        "/profiles/{profile_id}",
        operation_id="renameRemoteWorkspaceProfileV2",
        response_model=m.RemoteProfileV2,
    )
    async def patch_profile(
        profile_id: ResourceId,
        request: m.ProfileDisplayNamePatchV2,
        resource_generation: ResourceGeneration,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _contract_only()

    @router.delete(
        "/profiles/{profile_id}",
        operation_id="deleteRemoteWorkspaceProfileV2",
        status_code=204,
    )
    async def delete_profile(
        profile_id: ResourceId,
        resource_generation: ResourceGeneration,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _contract_only()

    @router.post(
        "/profiles/{profile_id}/rebind",
        operation_id="rebindLegacyProfileToSystemOpenSshV2",
        response_model=m.RemoteWorkspaceProfileV2,
        status_code=201,
    )
    async def rebind_profile(
        profile_id: ResourceId,
        request: m.ProfileRebindV2,
        resource_generation: ResourceGeneration,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _contract_only()

    @router.post(
        "/profiles/{profile_id}/connect",
        operation_id="connectRemoteWorkspaceProfileV2",
        response_model=m.LocalOperationV2,
        status_code=202,
    )
    async def connect_profile(
        profile_id: ResourceId,
        request: m.ProfileConnectionActionV2,
        resource_generation: ResourceGeneration,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _contract_only()

    @router.post(
        "/profiles/{profile_id}/disconnect",
        operation_id="disconnectRemoteWorkspaceProfileV2",
        response_model=m.LocalOperationV2,
        status_code=202,
    )
    async def disconnect_profile(
        profile_id: ResourceId,
        request: m.ProfileConnectionActionV2,
        resource_generation: ResourceGeneration,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _contract_only()

    @router.post(
        "/profiles/{profile_id}/host-key/review",
        operation_id="reviewRemoteWorkspaceHostKeyV2",
        response_model=m.LocalOperationV2,
        status_code=202,
    )
    async def review_host_key(
        profile_id: ResourceId,
        request: m.HostKeyReviewRequestV2,
        resource_generation: ResourceGeneration,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _contract_only()

    @router.get(
        "/projects",
        operation_id="listDesktopProjectsV2",
        response_model=m.DesktopProjectPageV2,
    )
    async def projects(
        limit: Limit = 50,
        after: Cursor = None,
    ) -> Response:
        return _contract_only()

    @router.post(
        "/projects",
        operation_id="createDesktopProjectV2",
        response_model=m.DesktopProjectV2,
        status_code=201,
    )
    async def create_project(
        request: m.ProjectCreateV2,
        resource_generation: ResourceGeneration,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _contract_only()

    @router.get(
        "/projects/{project_id}",
        operation_id="getDesktopProjectV2",
        response_model=m.DesktopProjectV2,
    )
    async def project(project_id: ResourceId) -> Response:
        return _contract_only()

    @router.patch(
        "/projects/{project_id}",
        operation_id="updateDesktopProjectV2",
        response_model=m.DesktopProjectV2,
    )
    async def patch_project(
        project_id: ResourceId,
        request: m.ProjectPatchV2,
        resource_generation: ResourceGeneration,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _contract_only()

    @router.post(
        "/projects/{project_id}/activate",
        operation_id="activateDesktopProjectV2",
        response_model=m.LocalOperationV2,
        status_code=202,
    )
    async def activate_project(
        project_id: ResourceId,
        request: m.ProjectActionV2,
        resource_generation: ResourceGeneration,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _contract_only()

    @router.get(
        "/projects/{project_id}/capabilities",
        operation_id="getDesktopProjectCapabilitiesV2",
        response_model=m.ProjectCapabilityProjectionV2,
    )
    async def project_capabilities(project_id: ResourceId) -> Response:
        return _contract_only()

    @router.post(
        "/projects/{project_id}/validate",
        operation_id="validateDesktopProjectV2",
        response_model=m.ProjectValidationV2,
    )
    async def validate_project(
        project_id: ResourceId,
        request: m.ProjectValidationRequestV2,
        resource_generation: ResourceGeneration,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _contract_only()

    @router.get(
        "/tasks",
        operation_id="listDesktopTasksV2",
        response_model=m.DesktopTaskPageV2,
    )
    async def tasks(
        limit: Limit = 50,
        after: Cursor = None,
        project_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
    ) -> Response:
        return _contract_only()

    @router.post(
        "/tasks",
        operation_id="submitDesktopTaskV2",
        response_model=m.DesktopTaskV2,
        status_code=202,
    )
    async def submit_task(
        request: m.CoreTaskSubmitRequestV2,
        resource_generation: ResourceGeneration,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _contract_only()

    @router.get(
        "/tasks/{task_id}",
        operation_id="getDesktopTaskV2",
        response_model=m.DesktopTaskV2,
    )
    async def task(task_id: ResourceId) -> Response:
        return _contract_only()

    @router.post(
        "/tasks/{task_id}/cancel",
        operation_id="cancelDesktopTaskV2",
        response_model=m.LocalOperationV2,
        status_code=202,
    )
    async def cancel_task(
        task_id: ResourceId,
        request: m.TaskActionV2,
        resource_generation: ResourceGeneration,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _contract_only()

    @router.post(
        "/tasks/{task_id}/retry",
        operation_id="retryDesktopTaskV2",
        response_model=m.LocalOperationV2,
        status_code=202,
    )
    async def retry_task(
        task_id: ResourceId,
        request: m.TaskActionV2,
        resource_generation: ResourceGeneration,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _contract_only()

    @router.get(
        "/tasks/{task_id}/timeline",
        operation_id="getDesktopTaskTimelineV2",
        response_model=m.DesktopTimelinePageV2,
    )
    async def task_timeline(
        task_id: ResourceId,
        limit: Limit = 50,
        after: Cursor = None,
    ) -> Response:
        return _contract_only()

    @router.get(
        "/tasks/{task_id}/logs",
        operation_id="getDesktopTaskLogsV2",
        response_model=m.DesktopLogPageV2,
    )
    async def task_logs(
        task_id: ResourceId,
        limit: Limit = 100,
        after: Cursor = None,
    ) -> Response:
        return _contract_only()

    @router.get(
        "/tasks/{task_id}/context",
        operation_id="getDesktopTaskContextV2",
        response_model=m.DesktopTaskContextV2,
    )
    async def task_context(task_id: ResourceId) -> Response:
        return _contract_only()

    @router.get(
        "/tasks/{task_id}/artifacts",
        operation_id="listDesktopTaskArtifactsV2",
        response_model=m.DesktopArtifactPageV2,
    )
    async def task_artifacts(
        task_id: ResourceId,
        limit: Limit = 50,
        after: Cursor = None,
    ) -> Response:
        return _contract_only()

    @router.get(
        "/project-heads/{project_head_id}",
        operation_id="getDesktopProjectHeadV2",
        response_model=m.ProjectHeadRefV2,
    )
    async def project_head(project_head_id: ResourceId) -> Response:
        return _contract_only()

    @router.get(
        "/evolution-revisions/{evolution_revision_id}",
        operation_id="getDesktopEvolutionRevisionV2",
        response_model=m.EvolutionRevisionRefV2,
    )
    async def evolution_revision(evolution_revision_id: ResourceId) -> Response:
        return _contract_only()

    @router.get(
        "/runtime-contexts/{runtime_context_snapshot_id}",
        operation_id="getDesktopRuntimeContextV2",
        response_model=m.RuntimeContextSnapshotRefV2,
    )
    async def runtime_context(runtime_context_snapshot_id: ResourceId) -> Response:
        return _contract_only()

    @router.get(
        "/transitions/{transition_id}",
        operation_id="getDesktopTransitionV2",
        response_model=m.DesktopTransitionV2,
    )
    async def transition(transition_id: ResourceId) -> Response:
        return _contract_only()

    @router.post(
        "/transitions/{transition_id}/retry",
        operation_id="retryDesktopTransitionV2",
        response_model=m.LocalOperationV2,
        status_code=202,
    )
    async def retry_transition(
        transition_id: ResourceId,
        request: m.TransitionActionV2,
        resource_generation: ResourceGeneration,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _contract_only()

    @router.post(
        "/transitions/{transition_id}/replace",
        operation_id="replaceDesktopTransitionV2",
        response_model=m.LocalOperationV2,
        status_code=202,
    )
    async def replace_transition(
        transition_id: ResourceId,
        request: m.TransitionReplaceV2,
        resource_generation: ResourceGeneration,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _contract_only()

    @router.post(
        "/transitions/{transition_id}/abandon",
        operation_id="abandonDesktopTransitionV2",
        response_model=m.LocalOperationV2,
        status_code=202,
    )
    async def abandon_transition(
        transition_id: ResourceId,
        request: m.TransitionActionV2,
        resource_generation: ResourceGeneration,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _contract_only()

    @router.get(
        "/artifacts/{artifact_id}",
        operation_id="getDesktopArtifactV2",
        response_model=m.DesktopArtifactV2,
    )
    async def artifact(artifact_id: ResourceId) -> Response:
        return _contract_only()

    @router.get(
        "/artifacts/{artifact_id}/content",
        operation_id="getDesktopArtifactContentV2",
        response_model=m.DesktopArtifactContentV2,
    )
    async def artifact_content(artifact_id: ResourceId) -> Response:
        return _contract_only()

    @router.get(
        "/artifacts/{artifact_id}/diff",
        operation_id="getDesktopArtifactDiffV2",
        response_model=m.ArtifactDiffV2,
    )
    async def artifact_diff(
        artifact_id: ResourceId,
        previous_artifact_id: Annotated[
            str | None, Query(min_length=1, max_length=128)
        ] = None,
    ) -> Response:
        return _contract_only()

    @router.get(
        "/services",
        operation_id="listDesktopServicesV2",
        response_model=m.DesktopServicePageV2,
    )
    async def services(
        limit: Limit = 50,
        after: Cursor = None,
    ) -> Response:
        return _contract_only()

    @router.post(
        "/services/{service_id}/restart",
        operation_id="restartDesktopServiceV2",
        response_model=m.LocalOperationV2,
        status_code=202,
    )
    async def restart_service(
        service_id: ResourceId,
        request: m.ServiceRestartV2,
        resource_generation: ResourceGeneration,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _contract_only()

    @router.post(
        "/diagnostics",
        operation_id="createDesktopDiagnosticV2",
        response_model=m.DesktopDiagnosticV2,
        status_code=202,
    )
    async def create_diagnostic(
        request: m.DiagnosticRequestV2,
        resource_generation: ResourceGeneration,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _contract_only()

    @router.get(
        "/diagnostics/{diagnostic_id}",
        operation_id="getDesktopDiagnosticV2",
        response_model=m.DesktopDiagnosticV2,
    )
    async def diagnostic(diagnostic_id: ResourceId) -> Response:
        return _contract_only()

    @router.get(
        "/events",
        operation_id="streamDesktopEventsV2",
        response_model=m.DesktopSseFrameV2,
    )
    async def events(last_event_id: LastEventId = None) -> Response:
        return _contract_only()

    app.include_router(router)

    @app.exception_handler(SshCatalogGenerationChangedError)
    async def catalog_generation_changed(
        request: Request,
        exc: SshCatalogGenerationChangedError,
    ) -> JSONResponse:
        del request, exc
        return _catalog_error(
            status_code=412,
            code="ssh_catalog_generation_changed",
            summary="The configured SSH host catalog changed; reload it before rescanning.",
            retryable=True,
            action="rescan",
        )

    @app.exception_handler(SshCatalogIdempotencyConflictError)
    async def catalog_idempotency_conflict(
        request: Request,
        exc: SshCatalogIdempotencyConflictError,
    ) -> JSONResponse:
        del request, exc
        return _catalog_error(
            status_code=409,
            code="ssh_catalog_idempotency_conflict",
            summary="The SSH catalog action key was already used for another request.",
            retryable=False,
            action="none",
        )

    @app.exception_handler(SshCatalogActionCapacityError)
    async def catalog_action_capacity(
        request: Request,
        exc: SshCatalogActionCapacityError,
    ) -> JSONResponse:
        del request, exc
        return _catalog_error(
            status_code=503,
            code="ssh_catalog_action_capacity_exhausted",
            summary="The bounded SSH catalog action ledger is full.",
            retryable=False,
            action="reconnect",
        )

    if provider is not None:
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
        for discovery_path in ("/version", "/health"):
            operation = schema["paths"][discovery_path]["get"]
            operation["x-openevo-discovery-only"] = True
            operation["x-openevo-mutation-compatible"] = False
        events_operation = schema["paths"]["/desktop/v2/events"]["get"]
        events_operation["x-sse-delivery"] = "at-least-once"
        events_operation["x-sse-replay"] = "bounded"
        events_operation["x-sse-replay-max-events"] = 10_000
        events_operation["responses"]["200"]["content"] = {
            "text/event-stream": {
                "schema": {"$ref": "#/components/schemas/DesktopSseFrameV2"}
            }
        }
        app.openapi_schema = schema
        return schema

    app.openapi = contract_openapi
    return app


desktop_local_v2_contract_app = create_desktop_local_v2_contract_app()


__all__ = [
    "DesktopLocalApiProviderV2",
    "desktop_local_v2_contract_app",
    "create_desktop_local_v2_contract_app",
]
