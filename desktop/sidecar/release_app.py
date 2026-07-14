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
from fastapi.responses import JSONResponse

from desktop.sidecar.contracts.v1.app import DESKTOP_SESSION_HEADER, create_contract_app
from desktop.sidecar.contracts.v1.models import ApiErrorV1
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
    DesktopReleaseProvider,
    InvalidNativeChallengeError,
    ProviderCapabilityUnavailableError,
)
from openevo import __version__ as OPENEVO_VERSION


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
    error = ApiErrorV1(
        request_id=request_id,
        code=code,
        http_status=status_code,
        message=message,
        severity="blocking",
        category=category,
        retryable=retryable,
        repair_action=repair_action,
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
) -> FastAPI:
    """Create the release Desktop Local API v1 app and own its durable store."""

    if (
        type(session_token) is not str
        or not 32 <= len(session_token) <= 4096
        or re.search(r"[\x00-\x1f\x7f]", session_token) is not None
    ):
        raise ValueError("Desktop session token must be 32-4096 characters without controls")
    encoded_session_token = session_token.encode("utf-8")
    store = DesktopProviderStore(state_root, clock=clock)
    try:
        provider = DesktopReleaseProvider(
            store,
            build_version=build_version,
            source_commit=source_commit,
            build_channel=build_channel,
            instance_id=instance_id,
            readiness_key=readiness_key,
            clock=clock,
        )
        app = create_contract_app(provider)
    except BaseException:
        store.close()
        raise
    app.state.desktop_release_provider = provider

    @app.middleware("http")
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

    @app.exception_handler(ProviderCapabilityUnavailableError)
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

    @app.exception_handler(InvalidNativeChallengeError)
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

    @app.exception_handler(RequestValidationError)
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

    @app.exception_handler(ProviderStoreError)
    async def handle_store_error(request: Request, exc: ProviderStoreError) -> JSONResponse:
        return _store_error_response(request, exc)

    @app.exception_handler(sqlite3.Error)
    async def handle_sqlite_error(request: Request, exc: sqlite3.Error) -> JSONResponse:
        del exc
        return _error_response(
            request,
            status_code=503,
            code="local_provider_unavailable",
            message="The local Desktop provider is unavailable.",
            category="service",
        )

    app.router.add_event_handler("shutdown", provider.close)
    return app


__all__ = ("create_release_desktop_local_api_app",)
