"""Helpers for converting completion records into trajectory traces."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from polar.trajectory.models import CompletionRecord, Trace


def _extract_response_ids(response: dict[str, Any], choice: dict[str, Any]) -> list[int]:
    token_ids = choice.get("token_ids", response.get("token_ids"))
    if isinstance(token_ids, list):
        return list(token_ids)

    logprobs = choice.get("logprobs")
    if isinstance(logprobs, dict):
        content = logprobs.get("content")
        if isinstance(content, list):
            extracted = [
                int(item["token_id"])
                for item in content
                if isinstance(item, dict) and item.get("token_id") is not None
            ]
            if extracted:
                return extracted
    return []


def _extract_response_logprobs(choice: dict[str, Any]) -> list[float] | None:
    """Sampled-token logprob per position, aligned 1:1 with response_ids.

    The token id is intentionally dropped -- it is already in ``response_ids``
    at the same index; only the float is needed for training.
    """
    logprobs = choice.get("logprobs")
    if isinstance(logprobs, dict):
        content = logprobs.get("content")
        if isinstance(content, list):
            return [
                float(item.get("logprob", 0.0)) if isinstance(item, dict) else 0.0
                for item in content
            ]
    return None


def _extract_prompt_messages(request: dict[str, Any]) -> list[dict[str, Any]]:
    messages = request.get("messages")
    if not isinstance(messages, list):
        return []
    return [deepcopy(message) for message in messages if isinstance(message, dict)]


def _extract_tools(request: dict[str, Any]) -> list[dict[str, Any]] | None:
    tools = request.get("tools")
    if not isinstance(tools, list) or not tools:
        return None
    extracted = [deepcopy(tool) for tool in tools if isinstance(tool, dict)]
    return extracted or None


def build_trace_from_completion(completion: CompletionRecord) -> Trace:
    """Normalize one stored completion record into a trajectory trace."""

    request = completion.request if isinstance(completion.request, dict) else {}
    response = completion.response if isinstance(completion.response, dict) else {}
    choices = response.get("choices")
    first_choice = (
        choices[0]
        if isinstance(choices, list) and choices and isinstance(choices[0], dict)
        else {}
    )
    prompt_ids = first_choice.get("input_token_ids") or response.get("prompt_token_ids")
    response_message = first_choice.get("message")
    finish_reason = first_choice.get("finish_reason")

    response_ids = _extract_response_ids(response, first_choice)

    return Trace(
        prompt_ids=list(prompt_ids) if isinstance(prompt_ids, list) else [],
        response_ids=response_ids,
        loss_mask=[1] * len(response_ids),
        prompt_messages=_extract_prompt_messages(request),
        response_messages=[deepcopy(response_message)] if isinstance(response_message, dict) else [],
        tools=_extract_tools(request),
        finish_reason=str(finish_reason) if finish_reason is not None else None,
        response_logprobs=_extract_response_logprobs(first_choice),
        metadata=deepcopy(completion.metadata),
    )
