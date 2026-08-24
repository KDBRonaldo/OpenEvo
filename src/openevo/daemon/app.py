"""Minimal authenticated FastAPI surface owned by the new OpenEvo daemon."""

from __future__ import annotations

import hmac
import os
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from openevo import __version__
from openevo.daemon.models import DaemonApiErrorV1, DaemonHealthV1, DaemonStatusV1


def create_daemon_app(
    *,
    token: str,
    started_at: str | None = None,
) -> FastAPI:
    """Create the loopback daemon app with one public health route."""
    normalized_token = token.strip()
    if not normalized_token:
        raise ValueError("daemon token must not be empty")
    daemon_started_at = started_at or _utc_now()

    app = FastAPI(
        title="OpenEvo Daemon",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health", response_model=DaemonHealthV1)
    async def health() -> DaemonHealthV1:
        return DaemonHealthV1()

    @app.get(
        "/v1/daemon/status",
        response_model=DaemonStatusV1,
        responses={401: {"model": DaemonApiErrorV1}},
    )
    async def daemon_status(request: Request) -> DaemonStatusV1 | JSONResponse:
        supplied = _bearer_token(request)
        if supplied is None or not hmac.compare_digest(supplied, normalized_token):
            error = DaemonApiErrorV1()
            return JSONResponse(status_code=401, content=error.model_dump(mode="json"))
        return DaemonStatusV1(pid=os.getpid(), started_at=daemon_started_at)

    return app


def _bearer_token(request: Request) -> str | None:
    authorization = request.headers.get("Authorization", "")
    scheme, separator, value = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return None
    token = value.strip()
    return token or None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
