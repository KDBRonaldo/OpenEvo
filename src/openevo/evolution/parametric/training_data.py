"""Bounded, model-agnostic chat SFT records for parametric memory."""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Any

from openevo.evolution.framework.contracts import (
    MAX_JAVASCRIPT_SAFE_INTEGER,
    canonical_json,
)


MAX_TRAINING_FILE_BYTES = 256 * 1024 * 1024
MAX_TRAINING_LINE_BYTES = 16 * 1024 * 1024
MAX_TRAINING_MESSAGES = 1024
MAX_TOOL_CALLS = 128
MAX_TOOLS = 128
_MAX_JSON_DEPTH = 16
_MAX_JSON_NODES = 8192
_MAX_COLLECTION_ITEMS = 4096
_MAX_TEXT_BYTES = 4 * 1024 * 1024
_MAX_TOOL_JSON_BYTES = 4 * 1024 * 1024
_TOOL_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,255}")
_MESSAGE_KEYS = frozenset({"content", "name", "role", "tool_call_id", "tool_calls"})
_TRAINING_METADATA_TEXT_FIELDS = (
    "dataset_artifact_id",
    "dataset_name",
    "event_id",
    "session_id",
    "status",
    "task_id",
)
_TRAINING_METADATA_KEYS = frozenset({*_TRAINING_METADATA_TEXT_FIELDS, "reward", "trace_index"})


def _text(value: Any, *, label: str, maximum_bytes: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    if not allow_empty and not value:
        raise ValueError(f"{label} must not be empty")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError(f"{label} exceeds its UTF-8 text budget")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{label} exceeds its UTF-8 text budget")
    return value


def _identity(value: Any, *, label: str, maximum_bytes: int = 256) -> str:
    normalized = _text(value, label=label, maximum_bytes=maximum_bytes)
    if (
        normalized != normalized.strip()
        or unicodedata.normalize("NFC", normalized) != normalized
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise ValueError(f"{label} must be normalized identity text")
    return normalized


def _bounded_json_value(value: Any, *, label: str) -> Any:
    nodes = 0
    text_bytes = 0

    def visit(current: Any, depth: int) -> Any:
        nonlocal nodes, text_bytes
        nodes += 1
        if nodes > _MAX_JSON_NODES or depth > _MAX_JSON_DEPTH:
            raise ValueError(f"{label} exceeds its JSON structure budget")
        if current is None or isinstance(current, bool):
            return current
        if isinstance(current, int):
            if abs(current) > MAX_JAVASCRIPT_SAFE_INTEGER:
                raise ValueError(f"{label} contains an unsafe integer")
            return current
        if isinstance(current, float):
            if not math.isfinite(current):
                raise ValueError(f"{label} contains a non-finite number")
            return current
        if isinstance(current, str):
            normalized = _text(
                current,
                label=label,
                maximum_bytes=_MAX_TEXT_BYTES,
                allow_empty=True,
            )
            text_bytes += len(normalized.encode("utf-8"))
            if text_bytes > _MAX_TEXT_BYTES:
                raise ValueError(f"{label} exceeds its aggregate text budget")
            return normalized
        if isinstance(current, list):
            if len(current) > _MAX_COLLECTION_ITEMS:
                raise ValueError(f"{label} contains too many collection items")
            return [visit(item, depth + 1) for item in current]
        if isinstance(current, dict):
            if len(current) > _MAX_COLLECTION_ITEMS:
                raise ValueError(f"{label} contains too many collection items")
            normalized: dict[str, Any] = {}
            for key, item in current.items():
                normalized_key = _identity(key, label=f"{label} JSON key", maximum_bytes=256)
                if normalized_key in normalized:
                    raise ValueError(f"{label} contains duplicate normalized keys")
                normalized[normalized_key] = visit(item, depth + 1)
            return normalized
        raise ValueError(f"{label} contains a non-JSON value")

    normalized = visit(value, 0)
    if len(canonical_json(normalized).encode("utf-8")) > _MAX_TOOL_JSON_BYTES:
        raise ValueError(f"{label} exceeds its canonical JSON byte budget")
    return normalized


def _normalize_tool_calls(value: Any, *, strict: bool) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > MAX_TOOL_CALLS:
        raise ValueError("assistant tool_calls must be a non-empty bounded list")
    normalized: list[dict[str, Any]] = []
    for call in value:
        if not isinstance(call, dict):
            raise ValueError("assistant tool call must be an object")
        allowed = {"function", "id", "type"}
        if strict and set(call).difference(allowed):
            raise ValueError("assistant tool call has an open shape")
        if call.get("type", "function") != "function":
            raise ValueError("only function tool calls are supported")
        function = call.get("function")
        if not isinstance(function, dict):
            raise ValueError("assistant tool call requires a function object")
        if strict and set(function) != {"arguments", "name"}:
            raise ValueError("assistant tool-call function has an open shape")
        name = _identity(function.get("name"), label="tool-call function name")
        if _TOOL_NAME_RE.fullmatch(name) is None:
            raise ValueError("tool-call function name is invalid")
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            arguments = _text(
                arguments,
                label="tool-call arguments",
                maximum_bytes=_MAX_TEXT_BYTES,
            )
        elif isinstance(arguments, dict):
            arguments = _bounded_json_value(arguments, label="tool-call arguments")
        else:
            raise ValueError("tool-call arguments must be text or an object")
        item: dict[str, Any] = {
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        }
        call_id = call.get("id")
        if call_id is not None:
            item["id"] = _identity(call_id, label="tool call ID")
        normalized.append(item)
    return normalized


def normalize_chat_messages(value: Any, *, strict: bool = False) -> list[dict[str, Any]]:
    """Project common harness messages without benchmark-specific interpretation."""

    if not isinstance(value, list) or len(value) > MAX_TRAINING_MESSAGES:
        raise ValueError("chat messages must be a bounded list")
    normalized: list[dict[str, Any]] = []
    for message in value:
        if not isinstance(message, dict):
            raise ValueError("chat message must be an object")
        if strict and set(message).difference(_MESSAGE_KEYS):
            raise ValueError("chat message has an open shape")
        role = message.get("role")
        if role not in {"assistant", "developer", "system", "tool", "user"}:
            raise ValueError("chat message role is unsupported")
        content = message.get("content", "")
        if isinstance(content, (dict, list)):
            content = canonical_json(_bounded_json_value(content, label="message content"))
        else:
            content = _text(
                content if content is not None else "",
                label="message content",
                maximum_bytes=_MAX_TEXT_BYTES,
                allow_empty=True,
            )
        item: dict[str, Any] = {"role": role, "content": content}
        for key, label in (("name", "message name"), ("tool_call_id", "tool call ID")):
            if message.get(key) is not None:
                item[key] = _identity(message[key], label=label)
        raw_tool_calls = message.get("tool_calls")
        if raw_tool_calls is not None:
            if role != "assistant":
                raise ValueError("only assistant messages may contain tool_calls")
            # Gateway chat-completion responses may retain the provider's
            # explicit empty list on an otherwise ordinary text response. It
            # carries no training signal, so drop it while projecting raw
            # trajectories. Canonical trainer JSONL remains closed and rejects
            # the same shape through the strict validator below.
            if strict or raw_tool_calls != []:
                item["tool_calls"] = _normalize_tool_calls(
                    raw_tool_calls,
                    strict=strict,
                )
        empty_tool_observation = role == "tool" and "tool_call_id" in item
        if not content and "tool_calls" not in item and not empty_tool_observation:
            raise ValueError("chat message requires content or assistant tool_calls")
        if strict and item != message:
            raise ValueError("chat message is not in canonical training shape")
        normalized.append(item)
    return normalized


def normalize_tool_definitions(value: Any, *, strict: bool = False) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > MAX_TOOLS:
        raise ValueError("tools must be a non-empty bounded list")
    normalized: list[dict[str, Any]] = []
    for tool in value:
        if not isinstance(tool, dict):
            raise ValueError("tool definition must be an object")
        function = tool.get("function")
        if function is None and not strict:
            function = {
                "name": tool.get("name"),
                "description": tool.get("description"),
                "parameters": tool.get("parameters", tool.get("parameters_schema")),
            }
        elif strict and set(tool) != {"function", "type"}:
            raise ValueError("tool definition has an open shape")
        if tool.get("type", "function") != "function" or not isinstance(function, dict):
            raise ValueError("only function tool definitions are supported")
        allowed_function_keys = {"description", "name", "parameters", "strict"}
        if strict and set(function).difference(allowed_function_keys):
            raise ValueError("tool function definition has an open shape")
        name = _identity(function.get("name"), label="tool function name")
        if _TOOL_NAME_RE.fullmatch(name) is None:
            raise ValueError("tool function name is invalid")
        normalized_function: dict[str, Any] = {"name": name}
        description = function.get("description")
        if description is not None:
            normalized_function["description"] = _text(
                description,
                label="tool function description",
                maximum_bytes=64 * 1024,
                allow_empty=True,
            )
        parameters = function.get("parameters")
        if parameters is not None:
            if not isinstance(parameters, dict):
                raise ValueError("tool function parameters must be an object")
            normalized_function["parameters"] = _bounded_json_value(
                parameters,
                label="tool function parameters",
            )
        if function.get("strict") is not None:
            if not isinstance(function["strict"], bool):
                raise ValueError("tool function strict flag must be boolean")
            normalized_function["strict"] = function["strict"]
        item = {"type": "function", "function": normalized_function}
        if strict and item != tool:
            raise ValueError("tool definition is not in canonical training shape")
        normalized.append(item)
    return normalized


def normalize_training_example(value: Any) -> dict[str, Any]:
    """Validate the exact JSONL shape consumed by the trusted trainer."""

    if not isinstance(value, dict) or set(value).difference(
        {"messages", "metadata", "target_message_start", "tools"}
    ):
        raise ValueError("SD-LoRA training example has an open or invalid shape")
    messages = normalize_chat_messages(value.get("messages"), strict=True)
    target_message_start = value.get("target_message_start", 0)
    if (
        not isinstance(target_message_start, int)
        or isinstance(target_message_start, bool)
        or target_message_start < 0
        or target_message_start >= len(messages)
    ):
        raise ValueError("SD-LoRA target_message_start is outside the message sequence")
    if not any(
        message["role"] == "assistant"
        and (bool(message["content"]) or bool(message.get("tool_calls")))
        for message in messages[target_message_start:]
    ):
        raise ValueError("SD-LoRA training example has no assistant target")
    normalized: dict[str, Any] = {
        "messages": messages,
        "target_message_start": target_message_start,
    }
    if value.get("tools") is not None:
        normalized["tools"] = normalize_tool_definitions(value["tools"], strict=True)
    metadata = value.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict) or set(metadata).difference(_TRAINING_METADATA_KEYS):
            raise ValueError("SD-LoRA training metadata has an open or invalid shape")
        normalized_metadata: dict[str, Any] = {}
        for key in _TRAINING_METADATA_TEXT_FIELDS:
            item = metadata.get(key)
            if item is not None:
                normalized_metadata[key] = _identity(
                    item,
                    label=f"training metadata {key}",
                    maximum_bytes=4096,
                )
        reward = metadata.get("reward")
        if reward is not None:
            if (
                not isinstance(reward, (int, float))
                or isinstance(reward, bool)
                or not math.isfinite(float(reward))
            ):
                raise ValueError("SD-LoRA training metadata reward must be finite")
            normalized_metadata["reward"] = reward
        trace_index = metadata.get("trace_index")
        if trace_index is not None:
            if (
                not isinstance(trace_index, int)
                or isinstance(trace_index, bool)
                or trace_index < 0
                or trace_index > MAX_JAVASCRIPT_SAFE_INTEGER
            ):
                raise ValueError("SD-LoRA training metadata trace_index is invalid")
            normalized_metadata["trace_index"] = trace_index
        if normalized_metadata:
            normalized["metadata"] = normalized_metadata
    return normalized


__all__ = [
    "MAX_TRAINING_FILE_BYTES",
    "MAX_TRAINING_LINE_BYTES",
    "normalize_chat_messages",
    "normalize_tool_definitions",
    "normalize_training_example",
]
