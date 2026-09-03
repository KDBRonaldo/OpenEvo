"""Loopback Responses-to-Chat adapter for daemon-managed vLLM models."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from openevo.gateway.transform.openai_responses import OpenAIResponsesTransformer


_MAX_REQUEST_BYTES = 16 * 1024 * 1024
_MAX_RESPONSE_BYTES = 32 * 1024 * 1024


def _response_to_stream_chunk(response: dict[str, Any]) -> dict[str, Any]:
    choices = response.get("choices") or [{}]
    choice = choices[0]
    message = choice.get("message", {}) or {}
    tool_calls = []
    for index, tool_call in enumerate(message.get("tool_calls") or []):
        function = tool_call.get("function", {}) or {}
        tool_calls.append(
            {
                "index": index,
                "id": tool_call.get("id"),
                "type": tool_call.get("type", "function"),
                "function": {
                    "name": function.get("name", ""),
                    "arguments": function.get("arguments", ""),
                },
            }
        )
    delta: dict[str, Any] = {"role": "assistant"}
    if message.get("content") is not None:
        delta["content"] = message["content"]
    if message.get("reasoning_content") is not None:
        delta["reasoning_content"] = message["reasoning_content"]
    if tool_calls:
        delta["tool_calls"] = tool_calls
    return {
        "id": response.get("id"),
        "object": "chat.completion.chunk",
        "created": response.get("created"),
        "model": response.get("model"),
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": choice.get("finish_reason"),
            }
        ],
        "usage": response.get("usage"),
    }


class ManagedModelProxy:
    """Expose a bounded loopback Responses API backed by chat completions."""

    def __init__(
        self,
        *,
        upstream_base_url: str,
        model: str,
        context_window: int = 8192,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self._upstream_base_url = upstream_base_url.rstrip("/")
        self._model = model
        self._context_window = context_window
        self._opener = opener
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.base_url: str | None = None

    def start(self) -> str:
        if self.base_url is not None:
            return self.base_url
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "OpenEvoModelProxy/1"

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                path = urlsplit(self.path).path
                if path not in {"/health", "/v1/models"}:
                    self._json(404, {"error": {"message": "Not found"}})
                    return
                if path == "/health":
                    self._json(200, {"status": "ok"})
                    return
                if "client_version" in self.path:
                    self._json(200, proxy._codex_models())
                    return
                self._forward_models()

            def do_POST(self) -> None:  # noqa: N802
                if urlsplit(self.path).path != "/v1/responses":
                    self._json(404, {"error": {"message": "Not found"}})
                    return
                body = self._request_json()
                if body is None:
                    return
                self._responses(body)

            def _request_json(self) -> dict[str, Any] | None:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self._json(400, {"error": {"message": "Invalid content length"}})
                    return None
                if length <= 0 or length > _MAX_REQUEST_BYTES:
                    self._json(413, {"error": {"message": "Request is too large"}})
                    return None
                try:
                    value = json.loads(self.rfile.read(length))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self._json(400, {"error": {"message": "Invalid JSON body"}})
                    return None
                if not isinstance(value, dict):
                    self._json(400, {"error": {"message": "JSON body must be an object"}})
                    return None
                return value

            def _forward_models(self) -> None:
                try:
                    status, payload = proxy._upstream("GET", "/models", None)
                except RuntimeError as exc:
                    self._json(502, {"error": {"message": str(exc)}})
                    return
                self._raw(status, payload, "application/json")

            def _responses(self, original: dict[str, Any]) -> None:
                transformer = OpenAIResponsesTransformer()
                transformed = dict(original)
                transformed["_openevo_model_served"] = proxy._model
                chat_request = transformer.transform_request(transformed)
                chat_request["model"] = proxy._model
                streaming = bool(chat_request.pop("stream", False))
                chat_request.pop("stream_options", None)
                chat_request["stream"] = False
                chat_request.setdefault("temperature", 0.0)
                try:
                    status, payload = proxy._upstream(
                        "POST", "/chat/completions", chat_request
                    )
                except RuntimeError as exc:
                    self._json(502, {"error": {"message": str(exc)}})
                    return
                if status < 200 or status >= 300:
                    self._raw(status, payload, "application/json")
                    return
                try:
                    response = json.loads(payload)
                    if not isinstance(response, dict):
                        raise ValueError
                except (json.JSONDecodeError, ValueError):
                    self._json(502, {"error": {"message": "Invalid vLLM response"}})
                    return
                if not streaming:
                    self._json(200, transformer.transform_response(response, original))
                    return
                state = transformer.create_stream_state(original)
                events = state.process_chunk(
                    _response_to_stream_chunk(response), is_first=True
                )
                events.extend(state.finalize())
                output = "".join(
                    f"event: {event.get('type', 'unknown')}\n"
                    f"data: {json.dumps(event, default=str)}\n\n"
                    for event in events
                ).encode("utf-8")
                self._raw(200, output, "text/event-stream")

            def _json(self, status: int, body: dict[str, Any]) -> None:
                self._raw(
                    status,
                    json.dumps(body, default=str).encode("utf-8"),
                    "application/json",
                )

            def _raw(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        port = int(server.server_address[1])
        thread = threading.Thread(
            target=server.serve_forever,
            name="openevo-managed-model-proxy",
            daemon=True,
        )
        thread.start()
        self._server = server
        self._thread = thread
        self.base_url = f"http://127.0.0.1:{port}/v1"
        return self.base_url

    def _codex_models(self) -> dict[str, Any]:
        return {
            "models": [
                {
                    "slug": self._model,
                    "display_name": self._model,
                    "description": "OpenEvo daemon-managed Hugging Face model",
                    "base_instructions": (
                        "You are an OpenEvo coding agent. Work carefully in the user's "
                        "workspace, use the provided tools when needed, preserve unrelated "
                        "changes, and give a concise final answer when the task is complete."
                    ),
                    "prefer_websockets": False,
                    "support_verbosity": False,
                    "default_verbosity": None,
                    "apply_patch_tool_type": "freeform",
                    "web_search_tool_type": "text_and_image",
                    "input_modalities": ["text"],
                    "supports_image_detail_original": False,
                    "truncation_policy": {"mode": "tokens", "limit": 10_000},
                    "supports_parallel_tool_calls": False,
                    "tool_mode": "direct",
                    "multi_agent_version": "v1",
                    "use_responses_lite": False,
                    "include_skills_usage_instructions": False,
                    "include_apps_usage_instructions": False,
                    "include_plugin_usage_instructions": False,
                    "node_repl_auto_review_required": False,
                    "node_repl_disabled": True,
                    "auto_review_model_override": None,
                    "model_specialty": None,
                    "context_window": self._context_window,
                    "max_context_window": self._context_window,
                    "auto_compact_token_limit": None,
                    "comp_hash": "openevo-managed-v1",
                    "default_reasoning_summary": "none",
                    "default_reasoning_level": "low",
                    "supported_reasoning_levels": [
                        {"effort": "low", "description": "Default model effort"}
                    ],
                    "shell_type": "unified_exec",
                    "visibility": "list",
                    "minimal_client_version": "0.0.0",
                    "supported_in_api": True,
                    "availability_nux": None,
                    "upgrade": None,
                    "priority": 1,
                    "supports_search_tool": False,
                    "default_service_tier": None,
                    "service_tiers": [],
                    "additional_speed_tiers": [],
                    "supports_reasoning_summary_parameter": False,
                    "supports_reasoning_summaries": False,
                    "experimental_supported_tools": [],
                }
            ]
        }

    def _upstream(
        self, method: str, path: str, body: dict[str, Any] | None
    ) -> tuple[int, bytes]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(
            f"{self._upstream_base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            response = self._opener(request, timeout=900)
            with response:
                payload = response.read(_MAX_RESPONSE_BYTES + 1)
                if len(payload) > _MAX_RESPONSE_BYTES:
                    raise RuntimeError("vLLM response is too large")
                return int(response.status), payload
        except HTTPError as exc:
            payload = exc.read(_MAX_RESPONSE_BYTES + 1)
            return int(exc.code), payload[:_MAX_RESPONSE_BYTES]
        except (OSError, URLError) as exc:
            raise RuntimeError("vLLM is unavailable") from exc

    def close(self) -> None:
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        self.base_url = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)


__all__ = ["ManagedModelProxy"]
