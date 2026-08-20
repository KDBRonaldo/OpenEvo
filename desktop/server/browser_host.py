from __future__ import annotations

import json
import re
import secrets
from typing import Annotated, Literal, Mapping

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError


BROWSER_BOOTSTRAP_ROUTE = "/openevo-native/browser/bootstrap"
_MAX_REQUEST_BYTES = 8_192


class BrowserBootstrapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    schema_version: Literal["2"]
    bootstrap_token: Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]


class BrowserBootstrapAuthority:
    def __init__(self, *, bootstrap_token: str, context: Mapping[str, object]) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", bootstrap_token) is None:
            raise ValueError("browser bootstrap token is invalid")
        self._bootstrap_token = bootstrap_token.encode("ascii")
        self._context = dict(context)

    def resolve(self, candidate: str) -> dict[str, object] | None:
        encoded = candidate.encode("ascii")
        if not secrets.compare_digest(encoded, self._bootstrap_token):
            return None
        return dict(self._context)


def install_browser_host_routes(
    app: FastAPI,
    *,
    endpoint: str,
    bootstrap_token: str,
    session_token: str,
    negotiated_contract: Mapping[str, object],
) -> None:
    authority = BrowserBootstrapAuthority(
        bootstrap_token=bootstrap_token,
        context={
            "schema_version": "2",
            "endpoint": endpoint,
            "session_token": session_token,
            "negotiated_contract": dict(negotiated_contract),
        },
    )

    @app.post(BROWSER_BOOTSTRAP_ROUTE, include_in_schema=False)
    async def browser_bootstrap(request: Request) -> Response:
        if not _same_loopback_origin(request, endpoint):
            return Response(status_code=403)
        try:
            parsed = BrowserBootstrapRequest.model_validate(await _read_json(request))
        except (ValueError, ValidationError, UnicodeDecodeError, json.JSONDecodeError):
            return Response(status_code=422)
        context = authority.resolve(parsed.bootstrap_token)
        if context is None:
            return Response(status_code=403)
        return JSONResponse(context, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


async def _read_json(request: Request) -> object:
    if request.headers.get("content-type", "").partition(";")[0].strip().lower() != "application/json":
        raise ValueError("JSON content type required")
    payload = bytearray()
    async for chunk in request.stream():
        if len(chunk) > _MAX_REQUEST_BYTES - len(payload):
            raise ValueError("request too large")
        payload.extend(chunk)
    return json.loads(payload.decode("utf-8", errors="strict"))


def _same_loopback_origin(request: Request, endpoint: str) -> bool:
    if request.url.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return False
    origin = request.headers.get("origin")
    return origin is None or origin == endpoint
