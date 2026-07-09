"""Inference backend strategies for the Polar gateway.

The gateway speaks the OpenAI Chat Completions API to a local inference server.
Two backends are supported, and they differ only in:

  1. the request params that make them emit the token ids + per-token logprobs
     Polar needs for training, and
  2. the exact shape of those fields in the response.

The base implements the canonical contract -- request ``logprobs`` (the one
training param every backend needs) and a pass-through response. A backend
overrides only what it does differently, via two hooks: ``prepare_request``
(extra request params) and ``normalize_response`` (response canonicalization).
Everything downstream -- storage, trace builder, transforms, slime adapter --
then sees one shape. The canonical shape is SGLang's patched output:

  - prompt token ids:   ``choice.input_token_ids`` (or ``response.prompt_token_ids``)
  - response token ids: ``choice.token_ids``       (or ``logprobs.content[].token_id``)
  - per-token logprobs: ``choice.logprobs.content[]`` with ``{token, token_id, logprob, ...}``

SGLang reaches this shape via ``scripts/patch/patch_sglang.sh``; vLLM reaches it
natively via the ``return_token_ids`` request flag plus a light response rename.
"""

from __future__ import annotations

from abc import ABC
from typing import Any


class InferenceEngine(ABC):
    """Strategy for one OpenAI-compatible inference backend.

    The base encodes the canonical contract: request ``logprobs`` (the one
    training param every backend needs) and pass the response through
    unchanged. A backend overrides only what it does differently.
    """

    name: str

    def prepare_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """Inject the request params this backend needs to emit training signals.

        ``logprobs`` is universal; subclasses add backend-specific params (e.g.
        token-id flags) on top via ``super().prepare_request(...)``.
        """
        request["logprobs"] = True
        return request

    def normalize_response(self, response: dict[str, Any]) -> dict[str, Any]:
        """Canonicalize the backend's response (in place) and return it.

        The canonical shape is SGLang's patched output, so the default is a
        pass-through; a backend that differs overrides this.
        """
        return response

    def apply_adapter_merge_spec(
        self,
        request: dict[str, Any],
        merge_spec: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Apply a session-scoped adapter merge spec to this request if supported."""
        return request


class SGLangEngine(InferenceEngine):
    """Canonical backend: it emits Polar's training shape with no per-request or
    response adaptation here -- so it inherits the base hooks unchanged. The one
    thing SGLang lacks natively is token-id *output* (no request flag exists for
    it); ``scripts/patch/patch_sglang.sh`` adds that on the response side.
    """

    name = "sglang"

    def apply_adapter_merge_spec(
        self,
        request: dict[str, Any],
        merge_spec: dict[str, Any] | None,
    ) -> dict[str, Any]:
        adapter_id = _runtime_lora_adapter_id(merge_spec)
        if adapter_id is None:
            return request
        base_model = str(request.get("model") or merge_spec.get("base_model") or "").strip()
        if not base_model:
            return request
        request["model"] = f"{base_model}:{adapter_id}"
        return request


class VLLMEngine(InferenceEngine):
    """vLLM via its native OpenAI-compatible server.

    ``return_token_ids`` makes vLLM emit ``response.prompt_token_ids`` and
    ``choice.token_ids`` -- the same ids SGLang's patch produces, with no source
    patch needed. ``top_logprobs`` must be set (not None) for vLLM to populate
    ``logprobs.content[]`` given ``logprobs=True``; 0 returns just the sampled
    token's logprob, which is all training needs.
    """

    name = "vllm"

    def apply_adapter_merge_spec(
        self,
        request: dict[str, Any],
        merge_spec: dict[str, Any] | None,
    ) -> dict[str, Any]:
        adapter_id = _runtime_lora_adapter_id(merge_spec)
        if adapter_id is not None:
            request["model"] = adapter_id
        return request

    def prepare_request(self, request: dict[str, Any]) -> dict[str, Any]:
        request = super().prepare_request(request)  # logprobs=True
        request["return_token_ids"] = True
        request.setdefault("top_logprobs", 0)
        # vLLM reads input reasoning from `reasoning`, not Polar's canonical
        # `reasoning_content`; rename it so prior turns' interleaved thinking
        # survives templating (else they render an empty `<think></think>`).
        for message in request.get("messages") or []:
            if isinstance(message, dict) and message.get("reasoning_content") is not None:
                message["reasoning"] = message.pop("reasoning_content")
        return request

    def normalize_response(self, response: dict[str, Any]) -> dict[str, Any]:
        choices = response.get("choices")
        if not isinstance(choices, list):
            return response
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            self._canonicalize_reasoning(choice.get("message"))
            self._stamp_token_ids_onto_logprobs(choice)
        return response

    @staticmethod
    def _canonicalize_reasoning(message: Any) -> None:
        """vLLM names the field ``reasoning``; Polar's canonical field is ``reasoning_content``."""
        if not isinstance(message, dict):
            return
        if message.get("reasoning_content") is None and message.get("reasoning") is not None:
            message["reasoning_content"] = message.pop("reasoning")

    @staticmethod
    def _stamp_token_ids_onto_logprobs(choice: dict[str, Any]) -> None:
        """Parity with SGLang: copy token_id onto each logprob entry.

        vLLM builds ``logprobs.content`` and ``choice.token_ids`` from the same
        ``output.token_ids``, so they align; guard on equal length regardless.
        Not load-bearing for training (which reads ``choice.token_ids`` and the
        per-entry ``logprob``) -- it keeps stored traces one shape across engines.
        """
        token_ids = choice.get("token_ids")
        logprobs = choice.get("logprobs")
        if not isinstance(token_ids, list) or not isinstance(logprobs, dict):
            return
        content = logprobs.get("content")
        if not isinstance(content, list) or len(content) != len(token_ids):
            return
        for entry, token_id in zip(content, token_ids):
            if isinstance(entry, dict):
                entry.setdefault("token_id", token_id)


def _runtime_lora_adapter_id(merge_spec: dict[str, Any] | None) -> str | None:
    if not isinstance(merge_spec, dict):
        return None
    if merge_spec.get("merge_mode") != "runtime_lora":
        return None
    adapters = merge_spec.get("adapters")
    if not isinstance(adapters, list):
        return None
    for adapter in adapters:
        if not isinstance(adapter, dict):
            continue
        for key in ("adapter_id", "name", "artifact_id"):
            value = adapter.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


_ENGINES: dict[str, type[InferenceEngine]] = {
    SGLangEngine.name: SGLangEngine,
    VLLMEngine.name: VLLMEngine,
}


def get_engine(name: str) -> InferenceEngine:
    """Return the inference engine strategy for ``name`` (``sglang`` | ``vllm``)."""
    try:
        engine_cls = _ENGINES[name]
    except KeyError:
        supported = ", ".join(sorted(_ENGINES))
        raise ValueError(
            f"Unknown inference engine {name!r}; supported: {supported}"
        ) from None
    return engine_cls()
