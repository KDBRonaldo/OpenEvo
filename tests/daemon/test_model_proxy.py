from __future__ import annotations

from io import BytesIO
import json
from urllib.request import Request, urlopen

from openevo.daemon.model_proxy import ManagedModelProxy


class _Response(BytesIO):
    status = 200


def test_managed_model_proxy_translates_responses_and_streams() -> None:
    captured: list[dict[str, object]] = []

    def opener(request: Request, *, timeout: int) -> _Response:
        assert timeout == 900
        captured.append(
            {
                "url": request.full_url,
                "body": json.loads(request.data or b"{}"),
            }
        )
        if request.full_url.endswith("/models"):
            return _Response(
                json.dumps({"object": "list", "data": [{"id": "fixture/model"}]}).encode()
            )
        return _Response(
            json.dumps(
                {
                    "id": "chatcmpl-fixture",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "fixture/model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "OPENEVO_OK",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 2,
                        "total_tokens": 5,
                    },
                }
            ).encode()
        )

    proxy = ManagedModelProxy(
        upstream_base_url="http://127.0.0.1:18000/v1",
        model="fixture/model",
        opener=opener,
    )
    base_url = proxy.start()
    try:
        with urlopen(  # noqa: S310 - test-owned loopback server
            f"{base_url}/models?client_version=fixture", timeout=5
        ) as response:
            models = json.loads(response.read())
        assert models["models"][0]["slug"] == "fixture/model"

        body = json.dumps(
            {
                "model": "fixture/model",
                "input": "hello",
                "max_output_tokens": 20,
            }
        ).encode()
        with urlopen(  # noqa: S310 - test-owned loopback server
            Request(
                f"{base_url}/responses",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            ),
            timeout=5,
        ) as response:
            payload = json.loads(response.read())
        assert payload["object"] == "response"
        assert payload["output"][0]["content"][0]["text"] == "OPENEVO_OK"

        stream_body = json.dumps(
            {"model": "fixture/model", "input": "hello", "stream": True}
        ).encode()
        with urlopen(  # noqa: S310 - test-owned loopback server
            Request(
                f"{base_url}/responses",
                data=stream_body,
                headers={"Content-Type": "application/json"},
                method="POST",
            ),
            timeout=5,
        ) as response:
            stream = response.read().decode()
        assert "event: response.created" in stream
        assert "event: response.output_text.delta" in stream
        assert "event: response.completed" in stream
    finally:
        proxy.close()

    assert captured[0]["url"] == "http://127.0.0.1:18000/v1/chat/completions"
    assert captured[0]["body"] == {
        "messages": [{"role": "user", "content": "hello"}],
        "model": "fixture/model",
        "max_tokens": 20,
        "stream": False,
        "temperature": 0.0,
    }
