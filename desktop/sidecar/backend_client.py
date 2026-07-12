from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from openevo.backend.models import (
    BackendError,
    MAX_EVOLUTION_PROJECT_VALIDATION_REQUEST_BYTES,
)
from openevo.deployment.profile import DesktopExecutionMode


MAX_CAPABILITIES_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_VALIDATION_RESPONSE_BYTES = 1024 * 1024
_MAX_BACKEND_ERROR_RESPONSE_BYTES = 64 * 1024
_SAFE_ERROR_CODE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}\Z", re.ASCII)


@dataclass(frozen=True)
class BackendConnection:
    base_url: str


class DesktopBackendError(RuntimeError):
    def __init__(self, status_code: int, error: dict[str, Any]) -> None:
        super().__init__(str(error.get("message", "OpenEvo backend request failed.")))
        self.status_code = status_code
        self.error = error


class BackendClient:
    def __init__(
        self,
        connection: BackendConnection,
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._connection = connection
        self._http_client = http_client or httpx.Client(timeout=10, trust_env=False)
        self._owns_http_client = http_client is None

    def health(self) -> dict[str, Any]:
        return self._get("/health")

    def status(self) -> dict[str, Any]:
        return self._get("/status")

    def capabilities(self, execution_mode: DesktopExecutionMode) -> dict[str, Any]:
        try:
            return self._get_bounded_json(
                "/capabilities",
                params={"execution_mode": execution_mode},
                max_response_bytes=MAX_CAPABILITIES_RESPONSE_BYTES,
            )
        except DesktopBackendError as exc:
            raise _capabilities_backend_error(exc, execution_mode) from exc
        except ValueError as exc:
            raise DesktopBackendError(
                502,
                {
                    "code": "backend_capabilities_invalid",
                    "message": (
                        "Remote OpenEvo backend returned an invalid "
                        "capabilities payload."
                    ),
                    "severity": "blocking",
                    "category": "internal",
                    "retryable": False,
                    "repair_action": "user_action_required",
                    "details": {
                        "execution_mode": execution_mode,
                        "error_type": type(exc).__name__,
                    },
                    "logs_ref": "services/openevo-backend",
                },
            ) from exc

    def validate_evolution_project(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._post_bounded_json(
                "/evolution/project-validation",
                payload,
                max_request_bytes=MAX_EVOLUTION_PROJECT_VALIDATION_REQUEST_BYTES,
                max_response_bytes=_MAX_VALIDATION_RESPONSE_BYTES,
            )
        except DesktopBackendError as exc:
            raise _evolution_validation_backend_error(exc) from exc
        except ValueError as exc:
            raise DesktopBackendError(
                502,
                {
                    "code": "backend_evolution_validation_invalid",
                    "message": (
                        "Remote OpenEvo backend returned an invalid project "
                        "validation payload."
                    ),
                    "severity": "blocking",
                    "category": "internal",
                    "retryable": False,
                    "repair_action": "user_action_required",
                    "details": {"error_type": type(exc).__name__},
                    "logs_ref": "services/openevo-backend",
                },
            ) from exc

    def environment_doctor(self) -> dict[str, Any]:
        return self._post("/environment/doctor", {"repair": False})

    def environment_repair(self, actions: list[str]) -> dict[str, Any]:
        return self._post("/environment/repair", {"actions": actions})

    def run_timeline(self, run_id: str) -> list[dict[str, Any]]:
        return self._get(f"/runs/{_path_segment(run_id)}/timeline")

    def run_logs(self, run_id: str) -> dict[str, Any]:
        return self._get(f"/runs/{_path_segment(run_id)}/logs")

    def run_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        return self._get(f"/runs/{_path_segment(run_id)}/artifacts")

    def artifact_content(self, artifact_id: str) -> dict[str, Any]:
        return self._get(f"/artifacts/{_path_segment(artifact_id)}/content")

    def artifact_diff(self, artifact_id: str) -> dict[str, Any]:
        return self._get(f"/artifacts/{_path_segment(artifact_id)}/diff")

    def service_logs(self, service_id: str) -> dict[str, Any]:
        return self._get(f"/services/{_path_segment(service_id)}/logs")

    def close(self) -> None:
        if self._owns_http_client:
            self._http_client.close()

    def _get(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> Any:
        try:
            response = self._http_client.get(
                f"{self._connection.base_url}{path}",
                params=params,
            )
        except httpx.RequestError as exc:
            raise _connection_error(exc) from exc
        self._raise_for_typed_error(response)
        return response.json()

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        try:
            response = self._http_client.post(
                f"{self._connection.base_url}{path}",
                json=body,
            )
        except httpx.RequestError as exc:
            raise _connection_error(exc) from exc
        self._raise_for_typed_error(response)
        return response.json()

    def _get_bounded_json(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        max_response_bytes: int,
    ) -> Any:
        return self._request_bounded_json(
            "GET",
            path,
            params=params,
            max_response_bytes=max_response_bytes,
        )

    def _post_bounded_json(
        self,
        path: str,
        body: dict[str, Any],
        *,
        max_request_bytes: int,
        max_response_bytes: int,
    ) -> Any:
        encoded_body = json.dumps(
            body,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded_body) > max_request_bytes:
            raise ValueError("request exceeds maximum bytes")
        return self._request_bounded_json(
            "POST",
            path,
            body=encoded_body,
            max_response_bytes=max_response_bytes,
        )

    def _request_bounded_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: bytes | None = None,
        max_response_bytes: int,
    ) -> Any:
        request_kwargs: dict[str, Any] = {}
        if body is not None:
            request_kwargs = {
                "content": body,
                "headers": {"content-type": "application/json"},
            }
        try:
            with self._http_client.stream(
                method,
                f"{self._connection.base_url}{path}",
                params=params,
                **request_kwargs,
            ) as response:
                limit = (
                    _MAX_BACKEND_ERROR_RESPONSE_BYTES
                    if response.status_code >= 400
                    else max_response_bytes
                )
                response_body = _read_bounded_response(response, limit)
        except httpx.RequestError as exc:
            raise _connection_error(exc) from exc
        if response.status_code >= 400:
            self._raise_for_typed_error_body(response.status_code, response_body)
        return json.loads(response_body)

    def _raise_for_typed_error(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        try:
            error = BackendError.model_validate(
                response.json(),
                strict=True,
            ).model_dump(mode="json")
        except (ValueError, ValidationError):
            error = _http_error(response.status_code)
        raise DesktopBackendError(response.status_code, error)

    def _raise_for_typed_error_body(self, status_code: int, body: bytes) -> None:
        try:
            error = BackendError.model_validate_json(
                body,
                strict=True,
            ).model_dump(mode="json")
        except (ValueError, ValidationError):
            error = _http_error(status_code)
        raise DesktopBackendError(status_code, error)


def _read_bounded_response(response: httpx.Response, limit: int) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise ValueError("response Content-Length is invalid") from exc
        if declared_length < 0 or declared_length > limit:
            raise ValueError("response exceeds maximum bytes")
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > limit:
            raise ValueError("response exceeds maximum bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _capabilities_backend_error(
    error: DesktopBackendError,
    execution_mode: DesktopExecutionMode,
) -> DesktopBackendError:
    remote_code = error.error.get("code")
    code = (
        remote_code
        if isinstance(remote_code, str) and _SAFE_ERROR_CODE.fullmatch(remote_code)
        else "backend_capabilities_unavailable"
    )
    return DesktopBackendError(
        error.status_code,
        {
            "code": code,
            "message": "Remote OpenEvo backend could not provide capabilities.",
            "severity": error.error.get("severity", "blocking"),
            "category": error.error.get("category", "service"),
            "retryable": error.error.get("retryable", False),
            "repair_action": error.error.get(
                "repair_action", "user_action_required"
            ),
            "details": {"execution_mode": execution_mode},
            "logs_ref": "services/openevo-backend",
        },
    )


def _evolution_validation_backend_error(
    error: DesktopBackendError,
) -> DesktopBackendError:
    allowed_codes = {
        "backend_connection_failed",
        "backend_http_error",
        "evolution_project_invalid",
        "evolution_registry_changed",
        "evolution_registry_unavailable",
        "request_body_too_large",
        "request_validation_error",
    }
    remote_code = error.error.get("code")
    code = (
        remote_code
        if isinstance(remote_code, str) and remote_code in allowed_codes
        else "backend_evolution_validation_failed"
    )
    details = _evolution_validation_error_details(code, error.error.get("details"))
    return DesktopBackendError(
        error.status_code,
        {
            "code": code,
            "message": "Remote OpenEvo backend could not validate this project.",
            "severity": error.error.get("severity", "blocking"),
            "category": error.error.get("category", "service"),
            "retryable": error.error.get("retryable", False),
            "repair_action": error.error.get(
                "repair_action", "user_action_required"
            ),
            "details": details,
            "logs_ref": "services/openevo-backend",
        },
    )


def _evolution_validation_error_details(
    code: str,
    raw_details: object,
) -> dict[str, str | None]:
    if not isinstance(raw_details, dict):
        return {}
    allowed_fields = (
        ("registry_digest",)
        if code == "evolution_registry_changed"
        else ("target_id", "selection", "reason_code", "registry_digest")
        if code == "evolution_project_invalid"
        else ()
    )
    details: dict[str, str | None] = {}
    for field_name in allowed_fields:
        value = raw_details.get(field_name)
        if value is None and field_name == "selection":
            details[field_name] = None
        elif isinstance(value, str) and len(value) <= 128:
            details[field_name] = value
    return details


def _http_error(status_code: int) -> dict[str, Any]:
    return {
        "code": "backend_http_error",
        "message": "Remote OpenEvo backend returned an HTTP error.",
        "severity": "blocking",
        "category": "internal",
        "retryable": False,
        "repair_action": "user_action_required",
        "details": {"status_code": status_code},
        "logs_ref": None,
    }


def _connection_error(exc: httpx.RequestError) -> DesktopBackendError:
    return DesktopBackendError(
        503,
        {
            "code": "backend_connection_failed",
            "message": "Desktop could not reach the remote OpenEvo backend.",
            "severity": "blocking",
            "category": "service",
            "retryable": True,
            "repair_action": "openevo_can_retry",
            "details": {"error_type": type(exc).__name__},
            "logs_ref": None,
        },
    )


def _path_segment(value: str) -> str:
    return quote(value, safe="")
