from __future__ import annotations

from types import TracebackType
from typing import Any

import httpx


class EvolutionWorkerClient:
    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=30.0, trust_env=False, transport=transport)

    def __enter__(self) -> EvolutionWorkerClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def claim(
        self,
        worker_id: str,
        capabilities: list[str],
        *,
        lease_seconds: int | None = None,
    ) -> dict[str, Any] | None:
        payload: dict[str, Any] = {
            "worker_id": worker_id,
            "capabilities": capabilities,
        }
        if lease_seconds is not None:
            payload["lease_seconds"] = lease_seconds
        response = self._client.post(
            f"{self.base_url}/v1/jobs/claim",
            json=payload,
        )
        response.raise_for_status()
        return response.json().get("job")

    def heartbeat(
        self,
        job_id: str,
        lease_id: str,
        *,
        progress: float | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"lease_id": lease_id}
        if progress is not None:
            payload["progress"] = progress
        if message is not None:
            payload["message"] = message
        response = self._client.post(
            f"{self.base_url}/v1/jobs/{job_id}/heartbeat",
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    def complete(
        self,
        job_id: str,
        lease_id: str,
        artifacts: list[dict[str, Any]],
        *,
        report: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "lease_id": lease_id,
            "artifacts": artifacts,
        }
        if report is not None:
            payload["report"] = report
        response = self._client.post(
            f"{self.base_url}/v1/jobs/{job_id}/complete",
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    def fail(
        self,
        job_id: str,
        lease_id: str,
        error: str,
        *,
        retryable: bool = True,
    ) -> dict[str, Any]:
        response = self._client.post(
            f"{self.base_url}/v1/jobs/{job_id}/fail",
            json={"lease_id": lease_id, "error": error, "retryable": retryable},
        )
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        self._client.close()
