from __future__ import annotations

from io import BytesIO
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from openevo.daemon.model_proxy import ManagedModelProxy


class _Response(BytesIO):
    status = 200


def test_managed_model_proxy_closes_vllm_stream_when_codex_disconnects() -> None:
    upstream_disconnected = threading.Event()

    class UpstreamHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for index in range(500):
                    chunk = {
                        "id": "chatcmpl-cancel",
                        "object": "chat.completion.chunk",
                        "model": "fixture/model",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": str(index)},
                                "finish_reason": None,
                            }
                        ],
                    }
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(0.01)
            except (BrokenPipeError, ConnectionResetError):
                upstream_disconnected.set()

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    proxy = ManagedModelProxy(
        upstream_base_url=f"http://127.0.0.1:{upstream.server_address[1]}/v1",
        model="fixture/model",
    )
    parsed = urlsplit(proxy.start())
    connection = HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    try:
        connection.request(
            "POST",
            "/v1/responses",
            body=json.dumps({"model": "fixture/model", "input": "hello", "stream": True}),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        assert response.status == 200
        assert response.read(1)
        response.close()
        connection.close()
        assert upstream_disconnected.wait(3)
    finally:
        connection.close()
        proxy.close()
        upstream.shutdown()
        upstream.server_close()
        upstream_thread.join(timeout=5)


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
        response_body = {
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
        if captured[-1]["body"].get("stream"):
            chunk = {
                **response_body,
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "OPENEVO_OK"},
                        "finish_reason": "stop",
                    }
                ],
            }
            return _Response(f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode())
        return _Response(json.dumps(response_body).encode())

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
        "parallel_tool_calls": False,
    }
    assert captured[1]["body"]["stream"] is True
    assert captured[1]["body"]["stream_options"] == {"include_usage": True}
    assert captured[1]["body"]["max_tokens"] == 1024
