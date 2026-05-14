"""Base transformer interface with SGLang request enhancement."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseTransformer(ABC):
    """Abstract base class for API transformers.

    Transforms requests from source API format to OpenAI format (for SGLang),
    and transforms responses back to source API format.
    """

    @abstractmethod
    def transform_request(self, body: dict[str, Any]) -> dict[str, Any]:
        """Transform request body to OpenAI/SGLang format."""
        pass

    @abstractmethod
    def transform_response(
        self,
        response: dict[str, Any],
        original_request: dict[str, Any],
    ) -> dict[str, Any]:
        """Transform response back to source API format."""
        pass

    @abstractmethod
    def transform_stream_chunk(
        self,
        chunk: dict[str, Any],
        original_request: dict[str, Any],
        is_first: bool = False,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Transform a streaming chunk to source API format."""
        pass

    def is_streaming_request(self, body: dict[str, Any]) -> bool:
        """Check if request is for streaming response."""
        return body.get("stream", False)

    def create_stream_state(self, original_request: dict[str, Any]) -> Any | None:
        """Create per-request stream state when chunk transforms need memory."""
        return None

    @staticmethod
    def _is_qwen_model(model_name: str | None) -> bool:
        if not model_name:
            return False
        return "qwen" in model_name.lower()

    @staticmethod
    def _merge_developer_role(request: dict[str, Any]) -> dict[str, Any]:
        """Rename 'developer' role to 'system' and merge all system messages into one."""
        messages = request.get("messages")
        if not isinstance(messages, list):
            return request

        # Rename developer -> system
        normalized = [
            {**msg, "role": "system"} if isinstance(msg, dict) and msg.get("role") == "developer" else msg
            for msg in messages
        ]

        # Merge multiple system messages into one at the top
        system_parts: list[str] = []
        non_system: list[Any] = []
        for msg in normalized:
            if isinstance(msg, dict) and msg.get("role") == "system":
                content = msg.get("content", "")
                text = content if isinstance(content, str) else str(content) if content else ""
                if text:
                    system_parts.append(text)
            else:
                non_system.append(msg)

        if len(system_parts) > 1:
            request["messages"] = [{"role": "system", "content": "\n\n".join(system_parts)}, *non_system]
        else:
            request["messages"] = normalized
        return request

    def _enhance_for_training(
        self,
        request: dict[str, Any],
        model_name: str | None = None,
    ) -> dict[str, Any]:
        """Apply model compatibility fixes and request fields needed for training."""
        request.pop("_polar_model_served", None)

        if self._is_qwen_model(model_name):
            # Qwen chat templates do not support the developer role and need
            # to be thinking disabled.
            request = self._merge_developer_role(request)
            chat_template_kwargs = dict(request.get("chat_template_kwargs") or {})
            chat_template_kwargs.setdefault("enable_thinking", False)
            request["chat_template_kwargs"] = chat_template_kwargs

        request["logprobs"] = True
        return request
