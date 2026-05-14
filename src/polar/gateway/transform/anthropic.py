"""Anthropic Messages API transformer.

Transforms between Anthropic Messages API and OpenAI Chat Completions API.
Aligned with agent-harness-proxy/src/harness_proxy/transform/anthropic.py.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from polar.gateway.transform.base import BaseTransformer

# Claude Code SDK leaks `x-anthropic-billing-header: ...cch=<hash>;` as the
# first line of the system prompt. The cch= hash changes per request, so
# rendered prompt tokens drift every turn and prefix_merging can't chain
# multi-turn traces. Strip the line before forwarding to SGLang.
_CLAUDE_CODE_BILLING_HEADER_RE = re.compile(
    r"^\s*x-anthropic-billing-header:[^\n]*\n?", re.IGNORECASE
)


@dataclass
class _AnthropicToolCallState:
    id: str
    name: str = ""
    anthropic_index: int | None = None
    buffered_arguments: str = ""
    started: bool = False


class AnthropicStreamState:
    """Per-request Anthropic streaming state.

    Anthropic SSE blocks are stateful across chunks: content blocks must be
    explicitly started, optionally receive multiple deltas, and then be closed
    before the final message delta. This helper tracks those open blocks for a
    single upstream OpenAI/SGLang stream.
    """

    def __init__(self, model: str, finish_to_stop_reason: dict[str, str]):
        self.model = model
        self.finish_to_stop_reason = finish_to_stop_reason
        self.message_id = f"msg_{uuid.uuid4().hex}"
        self.next_block_index = 0
        self.text_block_index: int | None = None
        self.text_block_started = False
        self.tool_calls: dict[int, _AnthropicToolCallState] = {}
        self.stop_reason = "end_turn"
        self.output_tokens = 0
        self.any_block_started = False
        self.completed = False

    def process_chunk(self, chunk: dict[str, Any], is_first: bool = False) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []

        if is_first:
            events.append({
                "type": "message_start",
                "message": {
                    "id": self.message_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": self.model,
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            })

        usage = chunk.get("usage", {})
        if usage:
            self.output_tokens = usage.get("completion_tokens", self.output_tokens)

        choices = chunk.get("choices", [])
        if not choices:
            return events

        choice = choices[0]
        delta = choice.get("delta", {}) or {}
        finish_reason = choice.get("finish_reason")
        if finish_reason:
            self.stop_reason = self.finish_to_stop_reason.get(finish_reason, "end_turn")

        content = delta.get("content")
        if content:
            if not self.text_block_started:
                events.append(self._open_text_block())
            events.append({
                "type": "content_block_delta",
                "index": self.text_block_index,
                "delta": {"type": "text_delta", "text": content},
            })

        tool_call_deltas = delta.get("tool_calls") or []
        if not isinstance(tool_call_deltas, list):
            tool_call_deltas = [tool_call_deltas]
        for tool_call_delta in tool_call_deltas:
            if isinstance(tool_call_delta, dict):
                events.extend(self._process_tool_call(tool_call_delta))

        return events

    def finalize(self) -> list[dict[str, Any]]:
        if self.completed:
            return []

        events: list[dict[str, Any]] = []

        text_stop = self._close_text_block()
        if text_stop:
            events.append(text_stop)

        for tool_index in sorted(self.tool_calls):
            tool_state = self.tool_calls[tool_index]
            if tool_state.started and tool_state.anthropic_index is not None:
                events.append({
                    "type": "content_block_stop",
                    "index": tool_state.anthropic_index,
                })

        if not self.any_block_started:
            empty_index = self.next_block_index
            events.append({
                "type": "content_block_start",
                "index": empty_index,
                "content_block": {"type": "text", "text": ""},
            })
            events.append({"type": "content_block_stop", "index": empty_index})

        events.append({
            "type": "message_delta",
            "delta": {"stop_reason": self.stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": self.output_tokens},
        })
        events.append({"type": "message_stop"})

        self.completed = True
        return events

    def _open_text_block(self) -> dict[str, Any]:
        self.text_block_started = True
        self.text_block_index = self.next_block_index
        self.next_block_index += 1
        self.any_block_started = True
        return {
            "type": "content_block_start",
            "index": self.text_block_index,
            "content_block": {"type": "text", "text": ""},
        }

    def _close_text_block(self) -> dict[str, Any] | None:
        if not self.text_block_started or self.text_block_index is None:
            return None

        event = {"type": "content_block_stop", "index": self.text_block_index}
        self.text_block_started = False
        self.text_block_index = None
        return event

    def _process_tool_call(self, tool_call_delta: dict[str, Any]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []

        tool_index = tool_call_delta.get("index", 0)
        if not isinstance(tool_index, int):
            tool_index = 0

        tool_state = self.tool_calls.get(tool_index)
        if tool_state is None:
            tool_state = _AnthropicToolCallState(
                id=tool_call_delta.get("id", f"toolu_{uuid.uuid4().hex[:24]}"),
            )
            self.tool_calls[tool_index] = tool_state
        elif tool_call_delta.get("id"):
            tool_state.id = tool_call_delta["id"]

        function = tool_call_delta.get("function", {})
        name = function.get("name")
        if isinstance(name, str) and name:
            tool_state.name += name

        args = function.get("arguments")
        args_str = ""
        if isinstance(args, str) and args:
            args_str = args
        elif args not in (None, ""):
            args_str = json.dumps(args)

        if args_str:
            tool_state.buffered_arguments += args_str

        if tool_state.name and not tool_state.started:
            text_stop = self._close_text_block()
            if text_stop:
                events.append(text_stop)

            tool_state.started = True
            tool_state.anthropic_index = self.next_block_index
            self.next_block_index += 1
            self.any_block_started = True

            events.append({
                "type": "content_block_start",
                "index": tool_state.anthropic_index,
                "content_block": {
                    "type": "tool_use",
                    "id": tool_state.id,
                    "name": tool_state.name,
                    "input": {},
                },
            })

            if tool_state.buffered_arguments:
                events.append({
                    "type": "content_block_delta",
                    "index": tool_state.anthropic_index,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": tool_state.buffered_arguments,
                    },
                })
                tool_state.buffered_arguments = ""
        elif tool_state.started and args_str and tool_state.anthropic_index is not None:
            events.append({
                "type": "content_block_delta",
                "index": tool_state.anthropic_index,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": args_str,
                },
            })

        return events


class AnthropicTransformer(BaseTransformer):
    """Transform between Anthropic and OpenAI API formats."""

    FINISH_TO_STOP_REASON: dict[str, str] = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "content_filter": "end_turn",
    }

    def transform_request(self, body: dict[str, Any]) -> dict[str, Any]:
        messages = []

        # Handle system message
        system = body.get("system")
        if system:
            system_content = self._flatten_content(system)
            # Drop Claude Code's per-request billing header line (breaks
            # prefix_merging because cch= changes every turn).
            system_content = _CLAUDE_CODE_BILLING_HEADER_RE.sub("", system_content)
            if system_content:
                messages.append({"role": "system", "content": system_content})

        # Transform messages
        for msg in body.get("messages", []):
            transformed = self._transform_message(msg)
            if transformed:
                if isinstance(transformed, list):
                    messages.extend(transformed)
                else:
                    messages.append(transformed)

        result: dict[str, Any] = {
            "messages": messages,
            "max_tokens": body.get("max_tokens", 4096),
        }

        if "temperature" in body:
            result["temperature"] = body["temperature"]
        if "top_p" in body:
            result["top_p"] = body["top_p"]
        if "stop_sequences" in body:
            result["stop"] = body["stop_sequences"]
        if body.get("stream", False):
            result["stream"] = True

        # Tools. Claude Code sometimes sends tools=[] on compaction/summary
        # turns; forwarding tool_choice without a non-empty tools list makes
        # SGLang reject with "tool_choice only allowed when tools specified".
        if "tools" in body:
            tools = self._transform_tools_to_openai(body["tools"])
            if tools:
                result["tools"] = tools
                result["tool_choice"] = self._transform_tool_choice_to_openai(
                    body.get("tool_choice", {"type": "auto"})
                )

        return self._enhance_for_training(
            result,
            body.get("_polar_model_served"),
        )

    def transform_response(
        self,
        response: dict[str, Any],
        original_request: dict[str, Any],
    ) -> dict[str, Any]:
        choices = response.get("choices", [])
        if not choices:
            return self._error_response("No choices in response")

        choice = choices[0]
        message = choice.get("message", {})

        content = []
        text = message.get("content")
        if text:
            content.append({"type": "text", "text": text})

        for tool_call in message.get("tool_calls") or []:
            content.append({
                "type": "tool_use",
                "id": tool_call.get("id", f"toolu_{uuid.uuid4().hex[:24]}"),
                "name": tool_call.get("function", {}).get("name", ""),
                "input": self._parse_json_safe(
                    tool_call.get("function", {}).get("arguments", "{}")
                ),
            })

        finish_reason = choice.get("finish_reason", "stop")
        stop_reason = self.FINISH_TO_STOP_REASON.get(finish_reason, "end_turn")
        usage = response.get("usage", {})

        if not content:
            content.append({"type": "text", "text": ""})

        return {
            "id": f"msg_{response.get('id', uuid.uuid4().hex)}",
            "type": "message",
            "role": "assistant",
            "content": content,
            "model": original_request.get("model", "claude-3"),
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            },
        }

    def create_stream_state(self, original_request: dict[str, Any]) -> AnthropicStreamState:
        return AnthropicStreamState(
            model=original_request.get("model", "claude-3"),
            finish_to_stop_reason=self.FINISH_TO_STOP_REASON,
        )

    def transform_stream_chunk(
        self,
        chunk: dict[str, Any],
        original_request: dict[str, Any],
        is_first: bool = False,
    ) -> list[dict[str, Any]]:
        """Best-effort single-chunk Anthropic transform.

        The server uses `create_stream_state()` for request-scoped streaming.
        This fallback keeps direct callers working for simple single-chunk cases.
        """
        state = self.create_stream_state(original_request)
        events = state.process_chunk(chunk, is_first=is_first)
        choices = chunk.get("choices", [])
        if choices and choices[0].get("finish_reason"):
            events.extend(state.finalize())
        return events

    def _transform_message(self, msg: dict[str, Any]) -> Optional[dict | list]:
        """Transform a single Anthropic message to OpenAI format."""
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if isinstance(content, str):
            return {"role": role, "content": content}

        if not isinstance(content, list):
            return {"role": role, "content": str(content)}

        # Check for mixed content: tool_result blocks + other content
        tool_results = [c for c in content if isinstance(c, dict) and c.get("type") == "tool_result"]
        tool_uses = [c for c in content if isinstance(c, dict) and c.get("type") == "tool_use"]
        text_blocks = [c for c in content if isinstance(c, dict) and c.get("type") == "text"]

        messages = []

        # Handle assistant messages with tool_use blocks
        if role == "assistant" and tool_uses:
            tool_calls = []
            text_parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        tool_calls.append({
                            "id": block.get("id", f"call_{uuid.uuid4().hex[:24]}"),
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": json.dumps(block.get("input", {})),
                            },
                        })
            msg_dict: dict[str, Any] = {
                "role": "assistant",
                "content": "\n".join(text_parts) if text_parts else None,
            }
            if tool_calls:
                msg_dict["tool_calls"] = tool_calls
            return msg_dict

        # Handle user messages with tool_result blocks
        if role == "user" and tool_results:
            # Each tool_result becomes a tool message
            for tr in tool_results:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tr.get("tool_use_id", ""),
                    "content": self._flatten_content(tr.get("content", "")),
                })

            # Any extra user text should come after the tool results.
            text_parts = [b.get("text", "") for b in text_blocks if b.get("text")]
            if text_parts:
                messages.append({"role": "user", "content": "\n".join(text_parts)})
            return messages if messages else None

        # Regular content blocks — flatten to string
        return {"role": role, "content": self._flatten_content(content)}

    def _flatten_content(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_result":
                        parts.append(self._flatten_content(block.get("content", "")))
            return "\n".join(parts)
        return str(content)

    def _transform_tools_to_openai(self, tools: list[dict]) -> list[dict]:
        result = []
        for tool in tools:
            result.append({
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {}),
                },
            })
        return result

    def _transform_tool_choice_to_openai(self, tool_choice: Any) -> Any:
        if isinstance(tool_choice, dict):
            tc_type = tool_choice.get("type")
            if tc_type == "auto":
                return "auto"
            elif tc_type == "any":
                return "required"
            elif tc_type == "tool":
                return {
                    "type": "function",
                    "function": {"name": tool_choice.get("name", "")},
                }
        return "auto"

    def _parse_json_safe(self, s: str) -> dict:
        try:
            return json.loads(s)
        except (json.JSONDecodeError, TypeError):
            return {}

    def _error_response(self, message: str) -> dict[str, Any]:
        return {
            "type": "error",
            "error": {"type": "api_error", "message": message},
        }
