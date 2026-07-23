"""Canonical FastAPI routes and optional Core Control API v2 provider binding."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from functools import wraps
import re
import secrets
from typing import Annotated, Literal, Protocol

from fastapi import APIRouter, Body, Depends, FastAPI, Header, Path, Query, Request, Security
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import StringConstraints
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import models as m


_CONTRACT_ONLY_MESSAGE = "This app defines the Core Control API v2 contract and has no provider."
_VERSIONED_PATH = re.compile(r"^/v([0-9]+)(?:/|$)")
_PROJECT_VALIDATION_PATH = re.compile(
    r"^/v2/projects/[A-Za-z0-9][A-Za-z0-9._-]{0,127}/validate$",
    re.ASCII,
)
_PROJECT_UPDATE_PATH = re.compile(
    r"^/v2/projects/[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
    re.ASCII,
)
_MAX_PROJECT_VALIDATION_BYTES = 1024 * 1024
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


class CoreControlApiProviderV2(Protocol):
    """Business provider dispatched through the frozen v2 operation IDs."""

    def authenticate(self, authorization_values: tuple[bytes, ...]) -> bool: ...

    def invoke(self, operation_id: str, arguments: Mapping[str, object]) -> object: ...

    async def invoke_async(
        self, operation_id: str, arguments: Mapping[str, object]
    ) -> object: ...


class CoreControlHTTPErrorV2(Exception):
    """Typed provider error rendered as the frozen :class:`ApiErrorV2` shape."""

    def __init__(
        self,
        status_code: int,
        *,
        code: str,
        message: str,
        category: Literal[
            "system",
            "project",
            "task",
            "transition",
            "artifact",
            "service",
            "authentication",
            "contract",
            "internal",
        ],
        retryable: bool,
        repair_action: Literal[
            "retry", "repair", "reconfigure", "user_action_required", "unsupported"
        ],
        next_action: str,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error = m.ApiErrorV2(
            request_id=f"request-{secrets.token_hex(16)}",
            code=code,
            http_status=status_code,
            message=message,
            category=category,
            retryable=retryable,
            repair_action=repair_action,
            next_action=next_action,
        )
        self.headers = dict(headers or {})


class _ProjectDocumentBodyGuardV2:
    """Bound project JSON before Starlette decodes it or Pydantic runs."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not _is_bounded_project_json_request(scope):
            await self.app(scope, receive, send)
            return

        declared_length = _content_length(scope)
        if (
            declared_length is not None
            and declared_length > _MAX_PROJECT_VALIDATION_BYTES
        ):
            await _body_guard_error(
                413,
                code="request_body_too_large",
                message="The project request exceeds the byte limit.",
            )(scope, receive, send)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > _MAX_PROJECT_VALIDATION_BYTES:
                await _body_guard_error(
                    413,
                    code="request_body_too_large",
                    message="The project request exceeds the byte limit.",
                )(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        if _json_nesting_exceeds(body, m.MAX_PROJECT_CONFIG_JSON_DEPTH):
            await _body_guard_error(
                422,
                code="request_json_too_deep",
                message="The project request exceeds the nesting limit.",
            )(scope, receive, send)
            return

        replayed = False

        async def replay_receive() -> Message:
            nonlocal replayed
            if replayed:
                return {"type": "http.request", "body": b"", "more_body": False}
            replayed = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay_receive, send)


def _provider_error_response(exc: CoreControlHTTPErrorV2) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.error.model_dump(mode="json"),
        headers=exc.headers,
    )


def _body_guard_error(
    status_code: int,
    *,
    code: str,
    message: str,
) -> JSONResponse:
    return _provider_error_response(
        CoreControlHTTPErrorV2(
            status_code,
            code=code,
            message=message,
            category="contract",
            retryable=False,
            repair_action="reconfigure",
            next_action="Reduce or correct the request body before retrying.",
        )
    )


def _is_bounded_project_json_request(scope: Scope) -> bool:
    if scope["type"] != "http":
        return False
    method = scope.get("method")
    path = str(scope.get("path", ""))
    return (
        (method == "POST" and path == "/v2/projects")
        or (method == "PATCH" and _PROJECT_UPDATE_PATH.fullmatch(path) is not None)
        or (
            method == "POST"
            and _PROJECT_VALIDATION_PATH.fullmatch(path) is not None
        )
    )


def _content_length(scope: Scope) -> int | None:
    values = [
        value
        for name, value in scope.get("headers", ())
        if name.lower() == b"content-length"
    ]
    if len(values) != 1:
        return None
    try:
        value = int(values[0])
    except ValueError:
        return None
    return value if value >= 0 else None


def _json_nesting_exceeds(body: bytes | bytearray, maximum: int) -> bool:
    depth = 0
    in_string = False
    escaped = False
    for value in body:
        if in_string:
            if escaped:
                escaped = False
            elif value == ord("\\"):
                escaped = True
            elif value == ord('"'):
                in_string = False
            continue
        if value == ord('"'):
            in_string = True
        elif value in (ord("{"), ord("[")):
            depth += 1
            if depth > maximum:
                return True
        elif value in (ord("}"), ord("]")):
            depth = max(depth - 1, 0)
    return False


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


def _bind_provider(app: FastAPI, provider: CoreControlApiProviderV2) -> None:
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
    413: {"model": m.ApiErrorV2, "description": "Request body exceeds its byte limit."},
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
ChunkIndex = Annotated[int, Path(ge=0, le=m.MAX_WORKSPACE_CHUNKS - 1)]
ChunkSha256 = Annotated[
    str,
    Header(alias="X-OpenEvo-Chunk-SHA256", pattern=r"^[0-9a-f]{64}$"),
]
ChunkByteSize = Annotated[
    int,
    Header(alias="X-OpenEvo-Chunk-Byte-Size", ge=1, le=m.MAX_WORKSPACE_CHUNK_BYTES),
]


def create_core_control_v2_contract_app(
    provider: CoreControlApiProviderV2 | None = None,
) -> FastAPI:
    """Create the canonical app, optionally bound to a real business provider."""

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
    app.add_middleware(_ProjectDocumentBodyGuardV2)

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
        "/capabilities",
        operation_id="getCoreCapabilitiesV2",
        response_model=m.CapabilitiesResponseV2,
        responses=_ERROR_RESPONSES,
        tags=["capabilities"],
    )
    async def capabilities(
        execution_mode: Annotated[
            Literal["codex_subscription_transcript", "self_deployed"], Query()
        ],
    ) -> Response:
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

    @router.patch(
        "/projects/{project_id}",
        operation_id="updateCoreProjectV2",
        response_model=m.ProjectV2,
        responses=_ERROR_RESPONSES,
        tags=["projects"],
    )
    async def update_project(
        project_id: ResourceId,
        request: m.ProjectUpdateV2,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _not_implemented()

    @router.post(
        "/projects/{project_id}/workspace-uploads",
        operation_id="createCoreWorkspaceUploadV2",
        response_model=m.WorkspaceUploadSessionV2,
        status_code=201,
        responses=_ERROR_RESPONSES,
        tags=["workspace"],
    )
    async def create_workspace_upload(
        project_id: ResourceId,
        request: m.WorkspaceUploadCreateV2,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _not_implemented()

    @router.get(
        "/projects/{project_id}/workspace-uploads/{upload_id}",
        operation_id="getCoreWorkspaceUploadV2",
        response_model=m.WorkspaceUploadSessionV2,
        responses=_ERROR_RESPONSES,
        tags=["workspace"],
    )
    async def get_workspace_upload(
        project_id: ResourceId,
        upload_id: ResourceId,
    ) -> Response:
        return _not_implemented()

    @router.put(
        "/projects/{project_id}/workspace-uploads/{upload_id}/chunks/{chunk_index}",
        operation_id="putCoreWorkspaceUploadChunkV2",
        response_model=m.WorkspaceUploadSessionV2,
        responses=_ERROR_RESPONSES,
        tags=["workspace"],
    )
    async def put_workspace_upload_chunk(
        project_id: ResourceId,
        upload_id: ResourceId,
        chunk_index: ChunkIndex,
        chunk: Annotated[
            bytes,
            Body(
                min_length=1,
                max_length=m.MAX_WORKSPACE_CHUNK_BYTES,
                media_type="application/octet-stream",
            ),
        ],
        chunk_sha256: ChunkSha256,
        chunk_byte_size: ChunkByteSize,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _not_implemented()

    @router.post(
        "/projects/{project_id}/workspace-uploads/{upload_id}/finalize",
        operation_id="finalizeCoreWorkspaceUploadV2",
        response_model=m.WorkspaceUploadSessionV2,
        status_code=201,
        responses=_ERROR_RESPONSES,
        tags=["workspace"],
    )
    async def finalize_workspace_upload(
        project_id: ResourceId,
        upload_id: ResourceId,
        request: m.WorkspaceUploadFinalizeV2,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _not_implemented()

    @router.post(
        "/projects/{project_id}/workspace-uploads/{upload_id}/abort",
        operation_id="abortCoreWorkspaceUploadV2",
        response_model=m.WorkspaceUploadSessionV2,
        responses=_ERROR_RESPONSES,
        tags=["workspace"],
    )
    async def abort_workspace_upload(
        project_id: ResourceId,
        upload_id: ResourceId,
        request: m.WorkspaceUploadAbortV2,
        if_match: IfMatch,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        return _not_implemented()

    @router.post(
        "/projects/{project_id}/validate",
        operation_id="validateCoreProjectV2",
        response_model=m.ProjectValidationResponseV2,
        responses=_ERROR_RESPONSES,
        tags=["projects"],
    )
    async def validate_project(
        project_id: ResourceId,
        request: m.ProjectValidationRequestV2,
        idempotency_key: IdempotencyKey,
    ) -> Response:
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

    if provider is not None:
        app.state.core_control_v2_provider = provider

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
                        CoreControlHTTPErrorV2(
                            401,
                            code="core_bearer_invalid",
                            message="The Core bearer credential is missing or invalid.",
                            category="authentication",
                            retryable=False,
                            repair_action="user_action_required",
                            next_action=(
                                "Reconnect with the bearer issued for this Core instance."
                            ),
                            headers={"WWW-Authenticate": "Bearer"},
                        )
                    )
                if version_match.group(1) != "2":
                    return _provider_error_response(
                        CoreControlHTTPErrorV2(
                            426,
                            code="contract_version_unsupported",
                            message=(
                                "The requested Core Control API major version is unsupported."
                            ),
                            category="contract",
                            retryable=False,
                            repair_action="user_action_required",
                            next_action="Negotiate a supported major through GET /version.",
                        )
                    )
            return await call_next(request)

        @app.exception_handler(CoreControlHTTPErrorV2)
        async def provider_http_error(
            _request: Request, exc: CoreControlHTTPErrorV2
        ) -> JSONResponse:
            return _provider_error_response(exc)

        @app.exception_handler(RequestValidationError)
        async def provider_validation_error(
            _request: Request, _exc: RequestValidationError
        ) -> JSONResponse:
            return _provider_error_response(
                CoreControlHTTPErrorV2(
                    422,
                    code="request_validation_error",
                    message=(
                        "The request does not satisfy the closed Core Control API v2 contract."
                    ),
                    category="contract",
                    retryable=False,
                    repair_action="reconfigure",
                    next_action="Correct the request fields and retry.",
                )
            )

        @app.exception_handler(Exception)
        async def provider_internal_error(
            _request: Request, _exc: Exception
        ) -> JSONResponse:
            return _provider_error_response(
                CoreControlHTTPErrorV2(
                    500,
                    code="core_control_internal_error",
                    message="Core Control could not complete the request.",
                    category="internal",
                    retryable=True,
                    repair_action="retry",
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
        for discovery_path in ("/version", "/health"):
            operation = schema["paths"][discovery_path]["get"]
            operation["x-openevo-discovery-only"] = True
            operation["x-openevo-mutation-compatible"] = False
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
    "CoreControlApiProviderV2",
    "CoreControlHTTPErrorV2",
    "core_control_v2_contract_app",
    "create_core_control_v2_contract_app",
]
