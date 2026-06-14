from __future__ import annotations

from typing import Any

import httpx


class EvolutionClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            trust_env=False,
            transport=transport,
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
