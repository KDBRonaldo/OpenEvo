from __future__ import annotations

from typing import Any

import pytest

from openevo.gateway import proxy
from openevo.gateway.engine import get_engine


@pytest.mark.asyncio
async def test_inference_client_does_not_inherit_host_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}

    class _RecordingAsyncClient:
        is_closed = False

        def __init__(self, **kwargs: Any) -> None:
            observed.update(kwargs)

    monkeypatch.setattr(proxy.httpx, "AsyncClient", _RecordingAsyncClient)
    inference = proxy.InferenceClient("http://127.0.0.1:8313", get_engine("vllm"))

    client = await inference._get_client()

    assert isinstance(client, _RecordingAsyncClient)
    assert observed["base_url"] == "http://127.0.0.1:8313"
    assert observed["trust_env"] is False
