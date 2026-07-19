from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import hmac
from pathlib import Path
import re
import sqlite3
from typing import Literal, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp

from desktop.sidecar.contracts.v1.app import DESKTOP_SESSION_HEADER, create_contract_app
from desktop.sidecar.contracts.v1.models import ApiErrorV1
from desktop.sidecar.core_bridge_v1 import DesktopCoreBridgeErrorV1, DesktopCoreBridgeV1
from desktop.sidecar.core_client_v1 import (
    CoreClientErrorV1,
    CoreClientLocalErrorV1,
    CoreMutationOutcomeUnknownV1,
)
from desktop.sidecar.event_broker_v1 import (
    DesktopEventBrokerClosedError,
    DesktopEventBrokerError,
    DesktopEventBrokerV1,
    DesktopEventCapacityError,
    DesktopEventCursorExpiredError,
    DesktopEventSubscriberLimitError,
)
from desktop.sidecar.provider_store import (
    ContractValidationError,
    CursorExpiredError,
    CursorInvalidError,
    DesktopProviderStore,
    ETagConflictError,
    IdempotencyCapacityError,
    IdempotencyConflictError,
    ProviderDataCorruptionError,
    ProviderSchemaError,
    ProviderStateRootError,
    ProviderStoreError,
    ResourceInUseError,
    ResourceNotFoundError,
)
from desktop.sidecar.release_provider import (
    ActiveProjectMismatchError,
    DesktopReleaseProvider,
    EvolutionConfigurationPendingError,
    ExecutionModeReleaseUnavailableError,
    InvalidNativeChallengeError,
    LocalOperationCancellationUnavailableError,
    ProviderCapabilityUnavailableError,
)
from desktop.sidecar.release_capabilities import RELEASE_EXECUTION_MODE_CAPABILITIES_V1
from desktop.sidecar.release_runtime import (
    DesktopReleaseCoreRuntimeV1,
    create_release_core_runtime,
)
from desktop.sidecar.remote_lifecycle import (
    DesktopRemoteLifecycle,
    RemoteCredentialUnavailableError,
    RemoteLifecycleError,
    RemoteLifecycleSupersededError,
)
from desktop.sidecar.workspace_imports import (
    WorkspaceImportError,
    WorkspaceImportIntegrityError,
    WorkspaceImportNotFoundError,
    WorkspaceImportStore,
)
from openevo import __version__ as OPENEVO_VERSION
from openevo.backend.contracts.v1.models import (
    ErrorCategory as CoreErrorCategory,
    ErrorSeverity,
    RepairAction,
)
from openevo.deployment.host_keys import ProviderKnownHostStore


ErrorCategory = Literal[
    "contract",
    "authentication",
    "profile",
    "connection",
    "project",
    "capability",
    "operation",
    "run",
    "artifact",
    "service",
    "diagnostic",
    "maintenance",
]

_TAURI_RELEASE_ORIGINS = ("tauri://localhost", "http://tauri.localhost")
_TAURI_RELEASE_METHODS = ("GET", "POST", "PATCH", "DELETE")
_TAURI_RELEASE_HEADERS = (
    "Accept",
    "Accept-Language",
    "Cache-Control",
    "Content-Language",
    "Content-Type",
    DESKTOP_SESSION_HEADER,
    "Idempotency-Key",
    "If-Match",
    "Last-Event-ID",
    "Pragma",
)


class _ReleaseDesktopLocalApiApp(FastAPI):
    def build_middleware_stack(self) -> ASGIApp:
        # Global wrapping keeps CORS outside Starlette's ServerErrorMiddleware,
        # so the renderer can consume bounded 500 responses as well.
        return CORSMiddleware(
            app=super().build_middleware_stack(),
            allow_origins=list(_TAURI_RELEASE_ORIGINS),
            allow_methods=list(_TAURI_RELEASE_METHODS),
            allow_headers=list(_TAURI_RELEASE_HEADERS),
            allow_credentials=False,
        )


def _cleanup_after_primary_failure(cleanup: Callable[[], None]) -> None:
    try:
        cleanup()
    except BaseException:
        pass


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    category: ErrorCategory,
    retryable: bool = False,
    repair_action: Literal[
        "none",
        "openevo_can_retry",
        "user_input_required",
        "reconnect_required",
        "upgrade_required",
    ] = "none",
    next_action: str | None = None,
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    if request_id is None:
        import secrets

        request_id = secrets.token_hex(16)
        request.state.request_id = request_id
    category_value = {
        "contract": CoreErrorCategory.CONTRACT,
        "authentication": CoreErrorCategory.AUTHENTICATION,
        "profile": CoreErrorCategory.PROJECT,
        "connection": CoreErrorCategory.AUTHENTICATION,
        "project": CoreErrorCategory.PROJECT,
        "capability": CoreErrorCategory.PROJECT,
        "operation": CoreErrorCategory.PROJECT,
        "run": CoreErrorCategory.RUN,
        "artifact": CoreErrorCategory.ARTIFACT,
        "service": CoreErrorCategory.SERVICE,
        "diagnostic": CoreErrorCategory.SERVICE,
        "maintenance": CoreErrorCategory.SERVICE,
    }[category]
    repair_action_value = {
        "none": RepairAction.UNSUPPORTED,
        "openevo_can_retry": RepairAction.OPENEVO_CAN_RETRY,
        "user_input_required": RepairAction.USER_ACTION_REQUIRED,
        "reconnect_required": RepairAction.OPENEVO_CAN_RECONFIGURE,
        "upgrade_required": RepairAction.USER_ACTION_REQUIRED,
    }[repair_action]
    if next_action is None:
        next_action = {
            "none": "Review the error before retrying this operation.",
            "openevo_can_retry": "Retry this operation from OpenEvo Desktop.",
            "user_input_required": "Review and correct the request before retrying.",
            "reconnect_required": "Reconnect the Desktop session before retrying.",
            "upgrade_required": "Install a compatible OpenEvo Desktop release.",
        }[repair_action]
    error = ApiErrorV1(
        request_id=request_id,
        code=code,
        http_status=status_code,
        message=message,
        severity=ErrorSeverity.BLOCKING,
        category=category_value,
        retryable=retryable,
        repair_action=repair_action_value,
        next_action=next_action,
    )
    return JSONResponse(status_code=status_code, content=error.model_dump(mode="json"))


def _operation_category(operation_id: str) -> ErrorCategory:
    lowered = operation_id.lower()
    for marker, category in (
        ("profile", "profile"),
        ("project", "project"),
        ("capabil", "capability"),
        ("validat", "capability"),
        ("operation", "operation"),
        ("run", "run"),
        ("artifact", "artifact"),
        ("service", "service"),
        ("diagnostic", "diagnostic"),
        ("maintenance", "maintenance"),
        ("cache", "maintenance"),
    ):
        if marker in lowered:
            return cast(ErrorCategory, category)
    return "contract"


def _store_error_response(request: Request, exc: ProviderStoreError) -> JSONResponse:
    if isinstance(exc, CursorInvalidError):
        return _error_response(
            request,
            status_code=400,
            code="cursor_invalid",
            message="The pagination cursor is invalid for this request.",
            category="contract",
        )
    if isinstance(exc, ResourceNotFoundError):
        return _error_response(
            request,
            status_code=404,
            code="resource_not_found",
            message="The requested resource was not found.",
            category=cast(ErrorCategory, exc.resource_type),
        )
    if isinstance(exc, ResourceInUseError):
        return _error_response(
            request,
            status_code=409,
            code="resource_in_use",
            message="The resource cannot be changed while it is active or in use.",
            category=cast(ErrorCategory, exc.resource_type),
        )
    if isinstance(exc, IdempotencyConflictError):
        return _error_response(
            request,
            status_code=409,
            code="idempotency_key_reused",
            message="The idempotency key is already bound to a different request.",
            category="contract",
        )
    if isinstance(exc, CursorExpiredError):
        return _error_response(
            request,
            status_code=410,
            code="cursor_expired",
            message="The pagination cursor expired; reload the first page.",
            category="contract",
            next_action="Reload the resource list.",
        )
    if isinstance(exc, ETagConflictError):
        return _error_response(
            request,
            status_code=412,
            code="etag_precondition_failed",
            message="The resource changed since it was loaded.",
            category=cast(ErrorCategory, exc.resource_type),
            next_action="Reload the resource before retrying the change.",
        )
    if isinstance(exc, ContractValidationError):
        return _error_response(
            request,
            status_code=422,
            code="contract_validation_failed",
            message="The request does not satisfy the Desktop Local API contract.",
            category="contract",
            repair_action="user_input_required",
        )
    if isinstance(
        exc,
        (
            IdempotencyCapacityError,
            ProviderDataCorruptionError,
            ProviderSchemaError,
            ProviderStateRootError,
        ),
    ):
        return _error_response(
            request,
            status_code=503,
            code="local_provider_unavailable",
            message="The local Desktop provider is unavailable.",
            category="service",
            retryable=isinstance(exc, IdempotencyCapacityError),
            repair_action="openevo_can_retry"
            if isinstance(exc, IdempotencyCapacityError)
            else "none",
        )
    return _error_response(
        request,
        status_code=503,
        code="local_provider_unavailable",
        message="The local Desktop provider is unavailable.",
        category="service",
    )


def create_release_desktop_local_api_app(
    *,
    state_root: Path | str,
    session_token: str,
    instance_id: str,
    readiness_key: bytes,
    source_commit: str,
    build_version: str = OPENEVO_VERSION,
    build_channel: Literal["release", "development", "test"] = "release",
    clock: Callable[[], datetime] | None = None,
    remote_lifecycle: DesktopRemoteLifecycle | None = None,
    core_assets_root: Path | str | None = None,
    core_bridge: DesktopCoreBridgeV1 | None = None,
    event_broker: DesktopEventBrokerV1 | None = None,
    startup_phase: Callable[[str], None] | None = None,
    close_on_shutdown: bool = True,
) -> FastAPI:
    """Create the release Desktop Local API v1 app and own its durable store."""

    if (
        type(session_token) is not str
        or not 32 <= len(session_token) <= 4096
        or re.search(r"[\x00-\x1f\x7f]", session_token) is not None
    ):
        raise ValueError("Desktop session token must be 32-4096 characters without controls")
    if core_assets_root is not None and (core_bridge is not None or event_broker is not None):
        raise ValueError("packaged Core assets cannot be combined with injected Core resources")
    encoded_session_token = session_token.encode("utf-8")
    if startup_phase is not None:
        startup_phase("provider_store")
    store = DesktopProviderStore(state_root, clock=clock)
    lifecycle = remote_lifecycle
    workspace_import_store: WorkspaceImportStore | None = None
    core_runtime: DesktopReleaseCoreRuntimeV1 | None = None
    try:
        if startup_phase is not None:
            startup_phase("credential_reset")
        store.reset_release_credential_slots()
        if lifecycle is None:
            if startup_phase is not None:
                startup_phase("remote_lifecycle")
            lifecycle = DesktopRemoteLifecycle(
                ProviderKnownHostStore(
                    store.state_root / "ssh-host-keys",
                    secure_ancestor=store.state_root,
                )
            )
        if startup_phase is not None:
            startup_phase("workspace_store")
        workspace_import_store = WorkspaceImportStore(
            store.state_root / "workspace-imports",
            reconcile_on_open=False,
        )
        if core_assets_root is not None:
            core_runtime = create_release_core_runtime(
                provider_store=store,
                workspace_store=workspace_import_store,
                remote_lifecycle=lifecycle,
                asset_root=core_assets_root,
                source_commit=source_commit,
                startup_phase=startup_phase,
            )
        if startup_phase is not None:
            startup_phase("release_provider")
        provider = DesktopReleaseProvider(
            store,
            workspace_import_store,
            build_version=build_version,
            source_commit=source_commit,
            build_channel=build_channel,
            instance_id=instance_id,
            readiness_key=readiness_key,
            execution_mode_capabilities=RELEASE_EXECUTION_MODE_CAPABILITIES_V1,
            remote_lifecycle=lifecycle,
            core_runtime=core_runtime,
            core_bridge=core_bridge,
            event_broker=event_broker,
            clock=clock,
        )
        if startup_phase is not None:
            startup_phase("contract_app")
        app = create_contract_app(provider, _app_factory=_ReleaseDesktopLocalApiApp)
    except BaseException:
        if core_runtime is not None:
            _cleanup_after_primary_failure(core_runtime.close)
        elif event_broker is not None:
            _cleanup_after_primary_failure(event_broker.close)
        if core_runtime is None and core_bridge is not None:
            _cleanup_after_primary_failure(core_bridge.close)
        if lifecycle is not None:
            _cleanup_after_primary_failure(lifecycle.close)
        _cleanup_after_primary_failure(store.close)
        if workspace_import_store is not None:
            _cleanup_after_primary_failure(workspace_import_store.close)
        raise

    def register_release_handler(registrar, key):
        try:
            decorator = registrar(key)
        except BaseException:
            _cleanup_after_primary_failure(provider.close)
            raise

        def register(handler):
            try:
                return decorator(handler)
            except BaseException:
                _cleanup_after_primary_failure(provider.close)
                raise

        return register

    try:
        if startup_phase is not None:
            startup_phase("release_routes")
        app.state.desktop_release_provider = provider
    except BaseException:
        _cleanup_after_primary_failure(provider.close)
        raise

    @register_release_handler(app.middleware, "http")
    async def authenticate_desktop_session(request: Request, call_next):
        import secrets

        request.state.request_id = secrets.token_hex(16)
        if request.url.path == "/desktop/v1" or request.url.path.startswith("/desktop/v1/"):
            header_name = DESKTOP_SESSION_HEADER.lower().encode("ascii")
            candidates = [value for name, value in request.scope["headers"] if name == header_name]
            candidate = candidates[0] if len(candidates) == 1 else b""
            token_matches = hmac.compare_digest(candidate, encoded_session_token)
            if len(candidates) != 1 or not token_matches:
                return _error_response(
                    request,
                    status_code=401,
                    code="desktop_session_invalid",
                    message="The Desktop session credential is missing or invalid.",
                    category="authentication",
                    repair_action="reconnect_required",
                )
        return await call_next(request)

    @register_release_handler(app.exception_handler, ProviderCapabilityUnavailableError)
    async def handle_unavailable(
        request: Request, exc: ProviderCapabilityUnavailableError
    ) -> JSONResponse:
        return _error_response(
            request,
            status_code=503,
            code="provider_capability_unavailable",
            message="This operation requires a provider capability that is not available yet.",
            category=_operation_category(exc.operation_id),
            repair_action="none",
        )

    @register_release_handler(app.exception_handler, ExecutionModeReleaseUnavailableError)
    async def handle_execution_mode_unavailable(
        request: Request, exc: ExecutionModeReleaseUnavailableError
    ) -> JSONResponse:
        capability = exc.capability
        return _error_response(
            request,
            status_code=409 if capability.support_state == "unavailable" else 422,
            code=capability.reason_code or "execution_mode_release_unsupported",
            message=capability.message,
            category=_operation_category(exc.operation_id),
            repair_action="user_input_required",
            next_action="Choose a supported execution mode and save the project before continuing.",
        )

    @register_release_handler(app.exception_handler, ActiveProjectMismatchError)
    async def handle_active_project_mismatch(
        request: Request, exc: ActiveProjectMismatchError
    ) -> JSONResponse:
        return _error_response(
            request,
            status_code=409,
            code="active_project_mismatch",
            message="The requested resource does not belong to the active local project.",
            category="service",
            repair_action="none",
            next_action="Reconnect and activate the saved project.",
        )

    @register_release_handler(
        app.exception_handler, LocalOperationCancellationUnavailableError
    )
    async def handle_local_operation_cancellation_unavailable(
        request: Request, exc: LocalOperationCancellationUnavailableError
    ) -> JSONResponse:
        del exc
        return _error_response(
            request,
            status_code=409,
            code="operation_cancellation_unavailable",
            message="This maintenance operation cannot be cancelled safely.",
            category="operation",
            retryable=False,
            repair_action="none",
            next_action="Wait for the operation to finish, then run Check again.",
        )

    @register_release_handler(app.exception_handler, EvolutionConfigurationPendingError)
    async def handle_evolution_configuration_pending(
        request: Request, exc: EvolutionConfigurationPendingError
    ) -> JSONResponse:
        del exc
        return _error_response(
            request,
            status_code=409,
            code="evolution_configuration_pending",
            message="Evolution setup must be explicitly completed before starting a run.",
            category="project",
            repair_action="user_input_required",
            next_action="Finish or disable the evolution targets, then save the project.",
        )

    @register_release_handler(app.exception_handler, InvalidNativeChallengeError)
    async def handle_invalid_challenge(
        request: Request, exc: InvalidNativeChallengeError
    ) -> JSONResponse:
        del exc
        return _error_response(
            request,
            status_code=403,
            code="native_challenge_invalid",
            message="The native readiness challenge is missing or invalid.",
            category="authentication",
        )

    @register_release_handler(app.exception_handler, RemoteLifecycleError)
    async def handle_remote_lifecycle(request: Request, exc: RemoteLifecycleError) -> JSONResponse:
        if isinstance(exc, RemoteCredentialUnavailableError):
            return _error_response(
                request,
                status_code=409,
                code="ssh_credential_unavailable",
                message="The selected SSH credential is not available to OpenEvo Desktop.",
                category="connection",
                repair_action="user_input_required",
                next_action="Switch this remote workspace to SSH agent authentication.",
            )
        if isinstance(exc, RemoteLifecycleSupersededError):
            return _error_response(
                request,
                status_code=409,
                code="connection_operation_superseded",
                message="A newer connection action replaced this SSH operation.",
                category="connection",
                retryable=True,
                repair_action="openevo_can_retry",
                next_action="Reload the connection state before retrying.",
            )
        return _error_response(
            request,
            status_code=503,
            code="ssh_connection_failed",
            message="OpenEvo Desktop could not establish the SSH connection.",
            category="connection",
            retryable=True,
            repair_action="openevo_can_retry",
            next_action="Check the server and SSH settings, then retry.",
        )

    @register_release_handler(app.exception_handler, DesktopCoreBridgeErrorV1)
    async def handle_core_bridge_error(
        request: Request, exc: DesktopCoreBridgeErrorV1
    ) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.error.http_status,
            content=exc.error.model_dump(mode="json"),
        )

    @register_release_handler(app.exception_handler, CoreClientErrorV1)
    async def handle_core_client_error(request: Request, exc: CoreClientErrorV1) -> JSONResponse:
        if isinstance(exc.error, ApiErrorV1):
            return JSONResponse(
                status_code=exc.status_code,
                content=exc.error.model_dump(mode="json"),
            )
        if not isinstance(exc.error, CoreClientLocalErrorV1):
            return _error_response(
                request,
                status_code=502,
                code="invalid_core_error",
                message="OpenEvo Core returned an invalid error response.",
                category="service",
            )
        category: ErrorCategory
        if exc.error.code.value in {"invalid_core_request", "invalid_core_response"}:
            category = "contract"
        elif exc.error.code.value in {
            "active_project_mismatch",
            "core_snapshot_refresh_required",
        }:
            category = "project"
        else:
            category = "connection"
        return _error_response(
            request,
            status_code=exc.status_code,
            code=exc.error.code.value,
            message=exc.error.message,
            category=category,
            retryable=exc.error.retryable,
            repair_action=("openevo_can_retry" if exc.error.retryable else "none"),
        )

    @register_release_handler(app.exception_handler, CoreMutationOutcomeUnknownV1)
    async def handle_core_mutation_outcome_unknown(
        request: Request, exc: CoreMutationOutcomeUnknownV1
    ) -> JSONResponse:
        del exc
        return _error_response(
            request,
            status_code=503,
            code="core_mutation_outcome_unknown",
            message="OpenEvo Core may have accepted this mutation, but its response was lost.",
            category="run",
            retryable=True,
            repair_action="openevo_can_retry",
            next_action="Retry only the exact saved mutation while Desktop reconciles Core state.",
        )

    @register_release_handler(app.exception_handler, DesktopEventCursorExpiredError)
    async def handle_event_cursor_expired(
        request: Request, exc: DesktopEventCursorExpiredError
    ) -> JSONResponse:
        del exc
        return _error_response(
            request,
            status_code=410,
            code="event_cursor_expired",
            message="The Desktop event cursor is outside the replay window.",
            category="operation",
            repair_action="openevo_can_retry",
            next_action="Refresh Desktop state and reconnect the event stream.",
        )

    @register_release_handler(app.exception_handler, DesktopEventBrokerError)
    async def handle_event_broker_error(
        request: Request, exc: DesktopEventBrokerError
    ) -> JSONResponse:
        retryable = isinstance(
            exc,
            (
                DesktopEventBrokerClosedError,
                DesktopEventCapacityError,
                DesktopEventSubscriberLimitError,
            ),
        )
        return _error_response(
            request,
            status_code=503,
            code="event_stream_unavailable",
            message="The Desktop event stream is temporarily unavailable.",
            category="operation",
            retryable=retryable,
            repair_action="openevo_can_retry" if retryable else "none",
        )

    @register_release_handler(app.exception_handler, RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        challenge_error = any(
            len(location := tuple(error.get("loc", ()))) == 2
            and location[0] == "header"
            and str(location[1]).lower() == "x-openevo-native-challenge"
            for error in exc.errors()
        )
        if challenge_error:
            return _error_response(
                request,
                status_code=403,
                code="native_challenge_invalid",
                message="The native readiness challenge is missing or invalid.",
                category="authentication",
            )
        return _error_response(
            request,
            status_code=422,
            code="contract_validation_failed",
            message="The request does not satisfy the Desktop Local API contract.",
            category="contract",
            repair_action="user_input_required",
        )

    @register_release_handler(app.exception_handler, ProviderStoreError)
    async def handle_store_error(request: Request, exc: ProviderStoreError) -> JSONResponse:
        return _store_error_response(request, exc)

    @register_release_handler(app.exception_handler, WorkspaceImportError)
    async def handle_workspace_import_error(
        request: Request, exc: WorkspaceImportError
    ) -> JSONResponse:
        invalid_reference = isinstance(
            exc,
            (WorkspaceImportIntegrityError, WorkspaceImportNotFoundError),
        )
        return _error_response(
            request,
            status_code=422 if invalid_reference else 503,
            code="workspace_import_invalid" if invalid_reference else "local_provider_unavailable",
            message="The selected workspace snapshot is unavailable."
            if invalid_reference
            else "The local Desktop provider is unavailable.",
            category="project" if invalid_reference else "service",
            retryable=False,
            repair_action="user_input_required" if invalid_reference else "none",
            next_action="Select the research folder again before saving the project."
            if invalid_reference
            else None,
        )

    @register_release_handler(app.exception_handler, sqlite3.Error)
    async def handle_sqlite_error(request: Request, exc: sqlite3.Error) -> JSONResponse:
        del exc
        return _error_response(
            request,
            status_code=503,
            code="local_provider_unavailable",
            message="The local Desktop provider is unavailable.",
            category="service",
        )

    if close_on_shutdown:
        try:
            app.router.add_event_handler("shutdown", provider.close)
        except BaseException:
            _cleanup_after_primary_failure(provider.close)
            raise
    return app


__all__ = ("create_release_desktop_local_api_app",)
