"""vLLM/OpenAI-compatible runner helpers for official-style OPSD.

This module intentionally stays outside the evolution artifact registry. It is
for external trainers or smoke tests that already have local vLLM endpoints
serving student and teacher models with full prompt logits enabled.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import httpx

from openevo.evolution.opsd import (
    OpsdPromptPair,
    OpsdTokenSequences,
    build_opsd_prompt_pair,
    build_opsd_token_sequences,
    generalized_jsd_loss,
)


@dataclass(frozen=True)
class VllmGeneration:
    """Student on-policy completion returned by vLLM."""

    text: str
    prompt_token_ids: tuple[int, ...]
    completion_token_ids: tuple[int, ...]


@dataclass(frozen=True)
class VllmPromptScore:
    """Prompt token ids and per-token vocab logits from vLLM prompt_logprobs."""

    input_ids: tuple[int, ...]
    prompt_logits: tuple[dict[int, float] | None, ...]

    def completion_logits(
        self,
        token_slice: slice,
        *,
        vocab_size: int | None = None,
    ) -> tuple[tuple[float, ...], ...]:
        """Return dense logits rows for target-token positions in ``token_slice``."""

        rows = self.prompt_logits[token_slice]
        if not rows:
            raise ValueError("token_slice selects no prompt logits")

        if vocab_size is None:
            max_token_id = max(
                (token_id for row in rows if row is not None for token_id in row),
                default=-1,
            )
            vocab_size = max_token_id + 1
        if vocab_size <= 0:
            raise ValueError("vocab_size must be positive")

        dense_rows: list[tuple[float, ...]] = []
        for row in rows:
            if row is None:
                raise ValueError("selected prompt logits include an unscored token")
            missing = [token_id for token_id in range(vocab_size) if token_id not in row]
            if missing:
                raise ValueError(
                    "selected prompt logits are not full-vocab; "
                    f"missing token id {missing[0]}"
                )
            dense_rows.append(tuple(row[token_id] for token_id in range(vocab_size)))
        return tuple(dense_rows)


@dataclass(frozen=True)
class VllmOpsdStepResult:
    """One official-style OPSD scoring step."""

    prompt_pair: OpsdPromptPair
    sequences: OpsdTokenSequences
    completion_text: str
    student_score: VllmPromptScore
    teacher_score: VllmPromptScore
    student_completion_logits: tuple[tuple[float, ...], ...]
    teacher_completion_logits: tuple[tuple[float, ...], ...]
    loss: float


class VllmOpsdClient:
    """Small sync client for vLLM completion generation and full-logit scoring."""

    def __init__(
        self,
        *,
        base_url: str,
        student_model: str,
        teacher_model: str,
        api_key: str | None = None,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.student_model = _required_text(student_model, "student_model")
        self.teacher_model = _required_text(teacher_model, "teacher_model")
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.Client(
            headers=headers,
            timeout=timeout,
            transport=transport,
            trust_env=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "VllmOpsdClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def generate_student_completion(
        self,
        prompt: str,
        *,
        max_tokens: int,
        temperature: float = 0.7,
        top_p: float | None = None,
        seed: int | None = None,
    ) -> VllmGeneration:
        """Generate the on-policy student completion from the student-visible prompt."""

        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        payload: dict[str, Any] = {
            "model": self.student_model,
            "prompt": _required_text(prompt, "prompt"),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "return_token_ids": True,
        }
        if top_p is not None:
            payload["top_p"] = top_p
        if seed is not None:
            payload["seed"] = seed

        choice = self._completion_choice(payload)
        return VllmGeneration(
            text=str(choice.get("text") or ""),
            prompt_token_ids=_int_tuple(choice.get("prompt_token_ids"), "prompt_token_ids"),
            completion_token_ids=_int_tuple(choice.get("token_ids"), "token_ids"),
        )

    def tokenize_prompt(self, *, model: str, prompt: str) -> tuple[int, ...]:
        """Ask vLLM to tokenize a prompt using a completion request with no generation."""

        payload = {
            "model": _required_text(model, "model"),
            "prompt": _required_text(prompt, "prompt"),
            "max_tokens": 0,
            "temperature": 0.0,
            "return_token_ids": True,
        }
        choice = self._completion_choice(payload)
        return _int_tuple(choice.get("prompt_token_ids"), "prompt_token_ids")

    def score_token_ids(
        self,
        *,
        model: str,
        input_ids: Sequence[int],
        prompt_logprobs: int = -1,
    ) -> VllmPromptScore:
        """Score a pre-tokenized [prompt][completion] sequence with full prompt logits."""

        ids = _int_tuple(input_ids, "input_ids")
        if not ids:
            raise ValueError("input_ids must not be empty")
        payload = {
            "model": _required_text(model, "model"),
            "prompt": list(ids),
            "max_tokens": 0,
            "temperature": 0.0,
            "prompt_logprobs": prompt_logprobs,
            "return_token_ids": True,
            "add_special_tokens": False,
        }
        choice = self._completion_choice(payload)
        returned_ids = choice.get("prompt_token_ids")
        if returned_ids is not None and _int_tuple(returned_ids, "prompt_token_ids") != ids:
            raise ValueError("vLLM returned prompt_token_ids that do not match input_ids")
        return VllmPromptScore(
            input_ids=ids,
            prompt_logits=_parse_prompt_logits(choice.get("prompt_logprobs")),
        )

    def run_step(
        self,
        *,
        problem: str,
        privileged_info: str,
        response_instruction: str,
        max_tokens: int,
        vocab_size: int | None = None,
        beta: float = 0.5,
        temperature: float = 1.0,
        top_k: int | None = None,
        generation_temperature: float = 0.7,
        generation_top_p: float | None = None,
        seed: int | None = None,
    ) -> VllmOpsdStepResult:
        """Run one official OPSD step and return logits/loss for trainer consumption."""

        pair = build_opsd_prompt_pair(
            problem=problem,
            privileged_info=privileged_info,
            response_instruction=response_instruction,
        )
        generation = self.generate_student_completion(
            pair.student_prompt,
            max_tokens=max_tokens,
            temperature=generation_temperature,
            top_p=generation_top_p,
            seed=seed,
        )
        teacher_prompt_ids = self.tokenize_prompt(
            model=self.teacher_model,
            prompt=pair.teacher_prompt,
        )
        sequences = build_opsd_token_sequences(
            student_prompt_ids=generation.prompt_token_ids,
            teacher_prompt_ids=teacher_prompt_ids,
            completion_ids=generation.completion_token_ids,
        )
        student_score = self.score_token_ids(
            model=self.student_model,
            input_ids=sequences.student.input_ids,
        )
        teacher_score = self.score_token_ids(
            model=self.teacher_model,
            input_ids=sequences.teacher.input_ids,
        )
        student_logits = student_score.completion_logits(
            sequences.student.completion_token_slice,
            vocab_size=vocab_size,
        )
        teacher_logits = teacher_score.completion_logits(
            sequences.teacher.completion_token_slice,
            vocab_size=vocab_size,
        )
        loss = generalized_jsd_loss(
            student_logits,
            teacher_logits,
            beta=beta,
            temperature=temperature,
            top_k=top_k,
        )
        return VllmOpsdStepResult(
            prompt_pair=pair,
            sequences=sequences,
            completion_text=generation.text,
            student_score=student_score,
            teacher_score=teacher_score,
            student_completion_logits=student_logits,
            teacher_completion_logits=teacher_logits,
            loss=loss,
        )

    def _completion_choice(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        response = self._client.post(f"{self.base_url}/v1/completions", json=dict(payload))
        response.raise_for_status()
        body = response.json()
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ValueError("vLLM completion response missing choices[0]")
        return choices[0]


def _parse_prompt_logits(value: Any) -> tuple[dict[int, float] | None, ...]:
    if not isinstance(value, list):
        raise ValueError("vLLM response missing prompt_logprobs")
    rows: list[dict[int, float] | None] = []
    for row in value:
        if row is None:
            rows.append(None)
            continue
        if not isinstance(row, dict):
            raise ValueError("prompt_logprobs rows must be objects or null")
        parsed: dict[int, float] = {}
        for token_id_value, logprob_value in row.items():
            token_id = _parse_token_id(token_id_value)
            parsed[token_id] = _parse_logit_value(logprob_value)
        rows.append(parsed)
    return tuple(rows)


def _parse_token_id(value: Any) -> int:
    if isinstance(value, int):
        return value
    text = str(value)
    if text.startswith("token_id:"):
        text = text.removeprefix("token_id:")
    try:
        return int(text)
    except ValueError as exc:
        raise ValueError(f"invalid token id in vLLM prompt_logprobs: {value!r}") from exc


def _parse_logit_value(value: Any) -> float:
    if isinstance(value, dict):
        if "logprob" not in value:
            raise ValueError("vLLM logprob object missing logprob")
        value = value["logprob"]
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid vLLM logit/logprob value: {value!r}") from exc
    return result


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _int_tuple(value: Any, field_name: str) -> tuple[int, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValueError(f"{field_name} must be a sequence of token ids")
    try:
        result = tuple(int(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must contain integer token ids") from exc
    return result
