"""OpenAI Chat Completions transformer with SGLang training enhancements."""

from __future__ import annotations

from typing import Any

from polar.gateway.transform.base import BaseTransformer


class OpenAIChatTransformer(BaseTransformer):
    """Transform OpenAI Chat requests (passthrough + training params)."""

    def transform_request(self, body: dict[str, Any]) -> dict[str, Any]:
        result = body.copy()
        return self._enhance_for_training(
            result,
            body.get("_polar_model_served"),
        )

    def transform_response(
        self,
        response: dict[str, Any],
        original_request: dict[str, Any],
    ) -> dict[str, Any]:
        result = response.copy()
        if "model" in original_request:
            result["model"] = original_request["model"]
        return result

    def transform_stream_chunk(
        self,
        chunk: dict[str, Any],
        original_request: dict[str, Any],
        is_first: bool = False,
    ) -> dict[str, Any]:
        result = chunk.copy()
        if "model" in original_request:
            result["model"] = original_request["model"]
        return result
