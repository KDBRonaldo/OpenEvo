from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx


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

    def _get(self, path: str) -> Any:
        try:
            response = self._http_client.get(f"{self._connection.base_url}{path}")
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

    def _raise_for_typed_error(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        try:
            error = response.json()
        except ValueError:
            error = {
                "code": "backend_http_error",
                "message": response.text,
                "severity": "blocking",
                "category": "internal",
                "retryable": False,
                "repair_action": "user_action_required",
                "details": {"status_code": response.status_code},
                "logs_ref": None,
            }
        raise DesktopBackendError(response.status_code, error)


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
