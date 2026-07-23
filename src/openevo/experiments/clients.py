from __future__ import annotations

from collections.abc import Mapping
from types import TracebackType
from typing import Any, Protocol
from urllib.parse import quote

import httpx


class RolloutClientProtocol(Protocol):
    def submit_task(self, payload: dict[str, Any]) -> str: ...

    def get_task(self, task_id: str) -> dict[str, Any]: ...

    def cancel_task(self, task_id: str) -> dict[str, Any]: ...


class EvolutionClientProtocol(Protocol):
    def create_dataset(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def create_job(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def create_plan_bound_job(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def get_artifact(self, artifact_id: str) -> dict[str, Any]: ...

    def get_context_runtime_authority(self, context_id: str) -> dict[str, Any]: ...

    def create_materialized_context(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def get_materialized_context(self, context_id: str) -> dict[str, Any]: ...

    def get_internal_job_result(self, job_id: str) -> dict[str, Any]: ...

    def update_artifact_promotion(
        self,
        artifact_id: str,
        *,
        promoted: bool,
    ) -> dict[str, Any]: ...

    def create_review_request(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def list_review_requests(self, **filters: Any) -> list[dict[str, Any]]: ...

    def submit_human_feedback(
        self,
        review_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]: ...

    def list_human_feedback(self, *, review_id: str) -> list[dict[str, Any]]: ...

    def create_feedback_application(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def create_human_query_decision(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class RolloutHttpClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float | None = None,
        headers: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        timeout = (
            httpx.Timeout(timeout_seconds, connect=30.0)
            if timeout_seconds
            else httpx.Timeout(None, connect=30.0)
        )
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            timeout=timeout,
            trust_env=False,
            headers=dict(headers or {}),
            transport=transport,
        )

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

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        encoded_task_id = quote(task_id, safe="")
        response = self._client.delete(f"{self.base_url}/rollout/task/{encoded_task_id}")
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("rollout cancellation response was not a JSON object")
        if result.get("task_id") != task_id or result.get("status") != "cancelled":
            raise ValueError("rollout cancellation response did not prove termination")
        return result


class EvolutionHttpClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 30.0,
        headers: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout_seconds, connect=10.0),
            trust_env=False,
            headers=dict(headers or {}),
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

    def create_plan_bound_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(f"{self.base_url}/v1/planned-jobs", json=payload)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("planned evolution job response was not a JSON object")
        return result

    def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        encoded_artifact_id = quote(artifact_id, safe="")
        response = self._client.get(f"{self.base_url}/v1/artifacts/{encoded_artifact_id}")
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("evolution artifact response was not a JSON object")
        return result

    def get_context_runtime_authority(self, context_id: str) -> dict[str, Any]:
        encoded_context_id = quote(context_id, safe="")
        response = self._client.get(
            f"{self.base_url}/v1/internal/contexts/{encoded_context_id}/runtime-authority"
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("context runtime authority was not a JSON object")
        return result

    def create_materialized_context(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(
            f"{self.base_url}/v1/internal/materialized-contexts",
            json=payload,
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("materialized context response was not a JSON object")
        return result

    def get_materialized_context(self, context_id: str) -> dict[str, Any]:
        encoded_context_id = quote(context_id, safe="")
        response = self._client.get(
            f"{self.base_url}/v1/internal/materialized-contexts/{encoded_context_id}"
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("materialized context response was not a JSON object")
        return result

    def get_internal_job_result(self, job_id: str) -> dict[str, Any]:
        encoded_job_id = quote(job_id, safe="")
        response = self._client.get(f"{self.base_url}/v1/internal/jobs/{encoded_job_id}")
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("evolution job result was not a JSON object")
        return result

    def update_artifact_promotion(
        self,
        artifact_id: str,
        *,
        promoted: bool,
    ) -> dict[str, Any]:
        encoded_artifact_id = quote(artifact_id, safe="")
        response = self._client.patch(
            f"{self.base_url}/v1/artifacts/{encoded_artifact_id}/promotion",
            json={"promoted": promoted},
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("evolution artifact promotion response was not a JSON object")
        return result

    def create_review_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(f"{self.base_url}/v1/reviews", json=payload)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("evolution review response was not a JSON object")
        return result

    def list_review_requests(self, **filters: Any) -> list[dict[str, Any]]:
        params = {key: value for key, value in filters.items() if value is not None}
        response = self._client.get(f"{self.base_url}/v1/reviews", params=params)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, list):
            raise ValueError("evolution review list response was not a JSON array")
        return result

    def submit_human_feedback(
        self,
        review_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        encoded_review_id = quote(review_id, safe="")
        response = self._client.post(
            f"{self.base_url}/v1/reviews/{encoded_review_id}/feedback",
            json=payload,
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("evolution human feedback response was not a JSON object")
        return result

    def list_human_feedback(self, *, review_id: str) -> list[dict[str, Any]]:
        encoded_review_id = quote(review_id, safe="")
        response = self._client.get(f"{self.base_url}/v1/reviews/{encoded_review_id}/feedback")
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, list):
            raise ValueError("evolution human feedback list response was not a JSON array")
        return result

    def create_feedback_application(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(
            f"{self.base_url}/v1/feedback-applications",
            json=payload,
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("evolution feedback application response was not a JSON object")
        return result

    def create_human_query_decision(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(f"{self.base_url}/v1/query-decisions", json=payload)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("evolution query decision response was not a JSON object")
        return result
