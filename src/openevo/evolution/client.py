from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx


class EvolutionClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            trust_env=False,
            transport=transport,
            headers=headers,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def resolve_context(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post(
            f"{self.base_url}/v1/contexts/resolve",
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    async def export_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post(f"{self.base_url}/v1/events", json=payload)
        response.raise_for_status()
        return response.json()

    async def get_materialized_context(self, context_id: str) -> dict[str, Any]:
        encoded_context_id = quote(context_id, safe="")
        response = await self._client.get(
            f"{self.base_url}/v1/internal/materialized-contexts/{encoded_context_id}"
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("materialized context response was not a JSON object")
        return result

    async def get_materialized_blob(self, context_id: str, blob_id: str) -> bytes:
        encoded_context_id = quote(context_id, safe="")
        encoded_blob_id = quote(blob_id, safe="")
        response = await self._client.get(
            f"{self.base_url}/v1/internal/materialized-contexts/"
            f"{encoded_context_id}/blobs/{encoded_blob_id}"
        )
        response.raise_for_status()
        return response.content

    async def create_review_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post(f"{self.base_url}/v1/reviews", json=payload)
        response.raise_for_status()
        return response.json()

    async def get_review_request(self, review_id: str) -> dict[str, Any]:
        response = await self._client.get(f"{self.base_url}/v1/reviews/{review_id}")
        response.raise_for_status()
        return response.json()

    async def list_review_requests(
        self,
        *,
        status: str | None = None,
        task_id: str | None = None,
        assigned_to: str | None = None,
    ) -> list[dict[str, Any]]:
        params = {
            key: value
            for key, value in {
                "status": status,
                "task_id": task_id,
                "assigned_to": assigned_to,
            }.items()
            if value is not None
        }
        response = await self._client.get(f"{self.base_url}/v1/reviews", params=params)
        response.raise_for_status()
        return response.json()

    async def get_review_packet(self, packet_id: str) -> dict[str, Any]:
        response = await self._client.get(f"{self.base_url}/v1/review-packets/{packet_id}")
        response.raise_for_status()
        return response.json()

    async def list_review_packets(self) -> list[dict[str, Any]]:
        response = await self._client.get(f"{self.base_url}/v1/review-packets")
        response.raise_for_status()
        return response.json()

    async def claim_review_request(
        self,
        review_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self._client.post(
            f"{self.base_url}/v1/reviews/{review_id}/claim",
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    async def submit_human_feedback(
        self,
        review_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self._client.post(
            f"{self.base_url}/v1/reviews/{review_id}/feedback",
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    async def list_human_feedback(self, review_id: str) -> list[dict[str, Any]]:
        response = await self._client.get(f"{self.base_url}/v1/reviews/{review_id}/feedback")
        response.raise_for_status()
        return response.json()

    async def adjudicate_review_request(
        self,
        review_id: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = await self._client.post(
            f"{self.base_url}/v1/reviews/{review_id}/adjudicate",
            json=payload or {},
        )
        response.raise_for_status()
        return response.json()

    async def resolve_review_request(self, review_id: str) -> dict[str, Any]:
        response = await self._client.post(f"{self.base_url}/v1/reviews/{review_id}/resolve")
        response.raise_for_status()
        return response.json()

    async def mark_review_stale(self, review_id: str) -> dict[str, Any]:
        response = await self._client.post(
            f"{self.base_url}/v1/reviews/{review_id}/mark-stale"
        )
        response.raise_for_status()
        return response.json()

    async def create_human_query_decision(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post(
            f"{self.base_url}/v1/query-decisions",
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    async def get_human_query_decision(self, query_decision_id: str) -> dict[str, Any]:
        response = await self._client.get(
            f"{self.base_url}/v1/query-decisions/{query_decision_id}"
        )
        response.raise_for_status()
        return response.json()

    async def create_feedback_application(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post(
            f"{self.base_url}/v1/feedback-applications",
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    async def list_feedback_applications(
        self,
        *,
        feedback_id: str | None = None,
    ) -> list[dict[str, Any]]:
        params = {}
        if feedback_id is not None:
            params["feedback_id"] = feedback_id
        response = await self._client.get(
            f"{self.base_url}/v1/feedback-applications",
            params=params,
        )
        response.raise_for_status()
        return response.json()
