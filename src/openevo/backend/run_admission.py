"""Private generation-bound admission bridge for Core-owned services."""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Protocol

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from openevo.internal_auth import (
    GenerationBoundRunAdmissionCheck,
    GenerationBoundRunAdmissionVerifier,
    RunAdmissionError,
    RunAdmissionOperation,
)


_PATH = "/internal/v1/run-admissions/verify"
_MAX_BODY_BYTES = 4096
_FIELDS = frozenset(
    {
        "framework_lock_digest",
        "generation_digest",
        "operation",
        "payload_sha256",
        "registry_digest",
        "session_id",
        "task_id",
    }
)


class RunServiceAuthenticator(Protocol):
    def authenticates_run_service(self, headers: Mapping[str, str]) -> bool: ...


def install_core_run_admission_endpoint(
    app: FastAPI,
    service_control: RunServiceAuthenticator,
    authority: GenerationBoundRunAdmissionVerifier,
) -> None:
    """Install the private verifier without changing the frozen OpenAPI surface."""

    if getattr(app.state, "core_run_admission_installed", False):
        raise RuntimeError("Core run admission endpoint is already installed")
    app.state.core_run_admission_installed = True

    @app.post(_PATH, include_in_schema=False, status_code=204)
    async def verify_run_admission(request: Request) -> Response:
        if not service_control.authenticates_run_service(request.headers):
            return _error(401, "internal_authentication_required", False)
        payload = bytearray()
        async for chunk in request.stream():
            if len(payload) + len(chunk) > _MAX_BODY_BYTES:
                return _error(413, "run_admission_payload_too_large", False)
            payload.extend(chunk)
        try:
            raw = json.loads(bytes(payload))
            check = _parse_check(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return _error(400, "run_admission_payload_invalid", False)
        try:
            await authority.verify(check)
        except RunAdmissionError as exc:
            return _error(exc.status_code, exc.code, exc.retryable)
        return Response(status_code=204)


def _parse_check(raw: object) -> GenerationBoundRunAdmissionCheck:
    if not isinstance(raw, dict) or set(raw) != _FIELDS:
        raise ValueError("run admission payload is not a closed object")
    required_strings = (
        "framework_lock_digest",
        "generation_digest",
        "operation",
        "payload_sha256",
        "registry_digest",
    )
    if any(not isinstance(raw[field], str) for field in required_strings):
        raise TypeError("run admission identity fields must be strings")
    for field in ("session_id", "task_id"):
        if raw[field] is not None and not isinstance(raw[field], str):
            raise TypeError("run admission optional identities must be strings or null")
    return GenerationBoundRunAdmissionCheck(
        operation=RunAdmissionOperation(raw["operation"]),
        generation_digest=raw["generation_digest"],
        registry_digest=raw["registry_digest"],
        framework_lock_digest=raw["framework_lock_digest"],
        payload_sha256=raw["payload_sha256"],
        task_id=raw["task_id"],
        session_id=raw["session_id"],
    )


def _error(status_code: int, code: str, retryable: bool) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "retryable": retryable}},
        headers={"Cache-Control": "no-store"},
    )


__all__ = ["RunServiceAuthenticator", "install_core_run_admission_endpoint"]
