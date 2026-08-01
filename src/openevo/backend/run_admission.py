"""Private generation-bound admission bridge for Core-owned services."""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import TYPE_CHECKING, Literal, Protocol

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from openevo.backend.runtime_identity import (
    RuntimeIdentityError,
    require_managed_self_deployed_runtime_identity,
    require_managed_subscription_runtime_identity,
)
from openevo.backend.self_deployed_snapshot_issuer import (
    SelfDeployedSnapshotIssueError,
    _issue_self_deployed_snapshot,
)
from openevo.backend.subscription_snapshot_issuer import (
    SubscriptionSnapshotIssueError,
    _issue_subscription_snapshot,
)
from openevo.evolution.framework import MAX_JAVASCRIPT_SAFE_INTEGER
from openevo.evolution.revisions import VerifiedExecutionSnapshot

from openevo.internal_auth import (
    GenerationBoundRunAdmissionCheck,
    GenerationBoundRunAdmissionVerifier,
    RunAdmissionError,
    RunAdmissionOperation,
)

if TYPE_CHECKING:
    from openevo.backend.service_supervisor import ServiceRunBinding


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


class EffectiveExecutionSettings(BaseModel):
    """Closed desired settings resolved into one verified effective snapshot."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )

    execution_mode: Literal[
        "codex_subscription_transcript",
        "self-deployed",
    ]
    capture_mode: Literal["transcript", "proxy", "token_level"]
    harness_id: str
    model_ref: str
    token_limit: int = Field(ge=1, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    task_network_allow_internet: bool

    @field_validator("harness_id", "model_ref")
    @classmethod
    def _bounded_identity_text(cls, value: str) -> str:
        if (
            not value
            or len(value) > 4096
            or value != value.strip()
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
            or any(0xD800 <= ord(character) <= 0xDFFF for character in value)
        ):
            raise ValueError("execution setting identity text is invalid")
        return value


class EffectiveExecutionSnapshotUnavailable(RuntimeError):
    """Typed fail-closed result for an execution profile that cannot be sealed."""

    def __init__(self, code: str, *, retryable: bool) -> None:
        messages = {
            "managed_runtime_identity_unavailable": (
                "The verified managed Subscription runtime identity is unavailable."
            ),
            "managed_runtime_model_mismatch": (
                "The verified managed runtime does not match the desired Subscription model."
            ),
            "self_deployed_runtime_identity_unavailable": (
                "The verified managed Self-Deployed runtime identity is unavailable."
            ),
            "self_deployed_capture_invalid": (
                "Self-Deployed execution requires transcript capture."
            ),
            "self_deployed_harness_invalid": (
                "The effective Self-Deployed harness must be Codex."
            ),
            "self_deployed_model_mismatch": (
                "The verified Self-Deployed serving profile does not match the project."
            ),
            "self_deployed_snapshot_invalid": (
                "The effective Self-Deployed snapshot could not be sealed."
            ),
            "subscription_capture_invalid": (
                "Codex Subscription execution requires transcript capture."
            ),
            "subscription_harness_invalid": ("The effective Subscription harness must be Codex."),
            "subscription_model_invalid": ("The desired Subscription model identity is invalid."),
            "task_network_policy_invalid": ("The effective task-network policy is invalid."),
            "subscription_snapshot_invalid": (
                "The effective Subscription snapshot could not be sealed."
            ),
        }
        if code not in messages:
            raise ValueError("effective execution unavailable code is invalid")
        super().__init__(messages[code])
        self.code = code
        self.retryable = retryable


def resolve_genesis_execution_snapshot(
    *,
    settings: EffectiveExecutionSettings,
    service_binding: ServiceRunBinding | None,
) -> VerifiedExecutionSnapshot:
    """Resolve the verified effective execution used by a Subscription genesis."""

    return _resolve_effective_execution_snapshot(
        settings=settings,
        service_binding=service_binding,
    )


def resolve_settings_transition_execution_snapshot(
    *,
    settings: EffectiveExecutionSettings,
    service_binding: ServiceRunBinding | None,
) -> VerifiedExecutionSnapshot:
    """Resolve the verified effective execution for a settings-only successor."""

    return _resolve_effective_execution_snapshot(
        settings=settings,
        service_binding=service_binding,
    )


def _resolve_effective_execution_snapshot(
    *,
    settings: EffectiveExecutionSettings,
    service_binding: ServiceRunBinding | None,
) -> VerifiedExecutionSnapshot:
    if type(settings) is not EffectiveExecutionSettings:
        raise TypeError("effective execution settings must be a closed settings object")
    settings = EffectiveExecutionSettings.model_validate(settings.model_dump(mode="python"))
    if settings.execution_mode == "self-deployed":
        if settings.capture_mode != "transcript":
            raise EffectiveExecutionSnapshotUnavailable(
                "self_deployed_capture_invalid",
                retryable=False,
            )
        if settings.harness_id != "codex":
            raise EffectiveExecutionSnapshotUnavailable(
                "self_deployed_harness_invalid",
                retryable=False,
            )
        try:
            runtime = require_managed_self_deployed_runtime_identity(service_binding)
        except RuntimeIdentityError as exc:
            raise EffectiveExecutionSnapshotUnavailable(
                "self_deployed_runtime_identity_unavailable",
                retryable=True,
            ) from exc
        try:
            return _issue_self_deployed_snapshot(
                runtime=runtime,
                capture_mode=settings.capture_mode,
                harness_id=settings.harness_id,
                model_ref=settings.model_ref,
                token_limit=settings.token_limit,
                task_network_allow_internet=settings.task_network_allow_internet,
            )
        except SelfDeployedSnapshotIssueError as exc:
            raise EffectiveExecutionSnapshotUnavailable(
                exc.code,
                retryable=exc.code
                in {
                    "self_deployed_runtime_identity_unavailable",
                    "self_deployed_snapshot_invalid",
                },
            ) from exc
    if settings.capture_mode != "transcript":
        raise EffectiveExecutionSnapshotUnavailable(
            "subscription_capture_invalid",
            retryable=False,
        )
    if settings.harness_id != "codex":
        raise EffectiveExecutionSnapshotUnavailable(
            "subscription_harness_invalid",
            retryable=False,
        )
    try:
        runtime = require_managed_subscription_runtime_identity(service_binding)
    except RuntimeIdentityError as exc:
        raise EffectiveExecutionSnapshotUnavailable(
            "managed_runtime_identity_unavailable",
            retryable=True,
        ) from exc
    try:
        return _issue_subscription_snapshot(
            runtime=runtime,
            capture_mode=settings.capture_mode,
            harness_id=settings.harness_id,
            model_ref=settings.model_ref,
            token_limit=settings.token_limit,
            task_network_allow_internet=settings.task_network_allow_internet,
        )
    except SubscriptionSnapshotIssueError as exc:
        retryable = exc.code in {
            "managed_runtime_identity_unavailable",
            "managed_runtime_model_mismatch",
            "subscription_snapshot_invalid",
        }
        raise EffectiveExecutionSnapshotUnavailable(
            exc.code,
            retryable=retryable,
        ) from exc


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


__all__ = [
    "EffectiveExecutionSettings",
    "EffectiveExecutionSnapshotUnavailable",
    "RunServiceAuthenticator",
    "install_core_run_admission_endpoint",
    "resolve_genesis_execution_snapshot",
    "resolve_settings_transition_execution_snapshot",
]
