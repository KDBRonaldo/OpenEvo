from __future__ import annotations

from types import TracebackType
from typing import Any, Protocol
from urllib.parse import quote

import httpx


class RolloutClientProtocol(Protocol):
    def submit_task(self, payload: dict[str, Any]) -> str: ...

    def get_task(self, task_id: str) -> dict[str, Any]: ...


class EvolutionClientProtocol(Protocol):
    def create_dataset(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def create_job(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class RolloutHttpClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        timeout = (
            httpx.Timeout(timeout_seconds, connect=30.0)
            if timeout_seconds
            else httpx.Timeout(None, connect=30.0)
        )
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout, trust_env=False, transport=transport)

    def __enter__(self) -> RolloutHttpClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def submit_task(self, payload: dict[str, Any]) -> str:
        response = self._client.post(f"{self.base_url}/rollout/task/submit", json=payload)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("rollout submit response was not a JSON object")
        task_id = result.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("rollout submit response did not include task_id")
        return task_id

    def get_task(self, task_id: str) -> dict[str, Any]:
        encoded_task_id = quote(task_id, safe="")
        response = self._client.get(f"{self.base_url}/rollout/task/{encoded_task_id}")
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("rollout task response was not a JSON object")
        return result


class EvolutionHttpClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout_seconds, connect=10.0),
            trust_env=False,
            transport=transport,
        )

    def __enter__(self) -> EvolutionHttpClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def create_dataset(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(f"{self.base_url}/v1/datasets", json=payload)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("evolution dataset response was not a JSON object")
        return result

    def create_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(f"{self.base_url}/v1/jobs", json=payload)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("evolution job response was not a JSON object")
        return result
