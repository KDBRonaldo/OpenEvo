"""Official-style OPSD helpers for privileged-context distillation.

The core OPSD contract is intentionally small:

* the student generates from the student-visible prompt only;
* the teacher scores that same completion with extra privileged context;
* losses are applied only on completion tokens.

This module keeps that contract independent of a concrete trainer or serving
backend. A vLLM runner can use these helpers to build the two prompts and align
the completion-token logits returned by full-logit scoring.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence


IGNORE_INDEX = -100

DEFAULT_RESPONSE_INSTRUCTION = (
    "Please reason step by step, and put your final answer within the required schema."
)

DEFAULT_TEACHER_TRANSITION = (
    "After reading the privileged information above, make sure you understand the "
    "reasoning behind the answer. Now, using your own words and independent reasoning, "
    "produce the answer to the problem above."
)


@dataclass(frozen=True)
class OpsdPromptPair:
    """Student and teacher prompts for one OPSD example."""

    student_prompt: str
    teacher_prompt: str


@dataclass(frozen=True)
class OpsdTokenSequence:
    """Tokenized [prompt][completion] sequence plus official OPSD mask/slices."""

    prompt_ids: tuple[int, ...]
    completion_ids: tuple[int, ...]
    input_ids: tuple[int, ...]
    labels: tuple[int, ...]
    loss_mask: tuple[int, ...]
    completion_token_slice: slice
    completion_logit_slice: slice

    @property
    def target_ids(self) -> tuple[int, ...]:
        return self.completion_ids


@dataclass(frozen=True)
class OpsdTokenSequences:
    """Student and teacher sequences sharing the same student completion."""

    student: OpsdTokenSequence
    teacher: OpsdTokenSequence


def build_opsd_prompt_pair(
    *,
    problem: str,
    privileged_info: str,
    response_instruction: str = DEFAULT_RESPONSE_INSTRUCTION,
    privileged_label: str = "Privileged Information",
    teacher_transition: str = DEFAULT_TEACHER_TRANSITION,
) -> OpsdPromptPair:
    """Build official-style student/teacher prompts.

    The student receives only the problem and response instruction. The teacher
    receives the same problem plus a delimited privileged block and transition
    prompt before scoring the student's completion.
    """

    problem = _required_text(problem, "problem")
    privileged_info = _required_text(privileged_info, "privileged_info")
    response_instruction = _required_text(response_instruction, "response_instruction")
    privileged_label = _required_text(privileged_label, "privileged_label")
    teacher_transition = _required_text(teacher_transition, "teacher_transition")

    student_prompt = f"Problem: {problem}\n\n{response_instruction}"
    teacher_prompt = (
        f"Problem: {problem}\n\n"
        "Here is privileged information for this problem:\n"
        f"=== {privileged_label} Begin ===\n"
        f"{privileged_info}\n"
        f"=== {privileged_label} End ===\n\n"
        f"{teacher_transition}\n\n"
        f"{response_instruction}"
    )
    return OpsdPromptPair(student_prompt=student_prompt, teacher_prompt=teacher_prompt)


def build_opsd_token_sequences(
    *,
    student_prompt_ids: Sequence[int],
    teacher_prompt_ids: Sequence[int],
    completion_ids: Sequence[int],
    ignore_index: int = IGNORE_INDEX,
) -> OpsdTokenSequences:
    """Create student/teacher [prompt][completion] sequences for OPSD scoring.

    The completion ids must be the student's on-policy generation. They are
    appended unchanged to both prompts. The completion logit slice follows the
    official implementation: logits[prompt_len - 1 : -1] predicts completion ids.
    """

    student_prompt = _token_tuple(student_prompt_ids, "student_prompt_ids")
    teacher_prompt = _token_tuple(teacher_prompt_ids, "teacher_prompt_ids")
    completion = _token_tuple(completion_ids, "completion_ids")
    if not student_prompt:
        raise ValueError("student_prompt_ids must not be empty")
    if not teacher_prompt:
        raise ValueError("teacher_prompt_ids must not be empty")
    if not completion:
        raise ValueError("completion_ids must not be empty")

    return OpsdTokenSequences(
        student=_build_one_sequence(
            prompt_ids=student_prompt,
            completion_ids=completion,
            ignore_index=ignore_index,
        ),
        teacher=_build_one_sequence(
            prompt_ids=teacher_prompt,
            completion_ids=completion,
            ignore_index=ignore_index,
        ),
    )


def generalized_jsd_loss(
    student_logits: Sequence[Sequence[float]],
    teacher_logits: Sequence[Sequence[float]],
    *,
    mask: Sequence[int] | None = None,
    beta: float = 0.5,
    temperature: float = 1.0,
    top_k: int | None = None,
) -> float:
    """Compute mean generalized JSD over selected token positions.

    Inputs are shaped [tokens][vocab]. ``top_k`` restricts each token's loss to
    the teacher's top-k vocabulary entries and renormalizes both distributions
    over that candidate set.
    """

    if not 0.0 <= beta <= 1.0:
        raise ValueError("beta must be between 0 and 1")
    if temperature <= 0:
        raise ValueError("temperature must be positive")
    if top_k is not None and top_k <= 0:
        raise ValueError("top_k must be positive when set")

    student_rows = [_float_tuple(row, "student_logits") for row in student_logits]
    teacher_rows = [_float_tuple(row, "teacher_logits") for row in teacher_logits]
    if len(student_rows) != len(teacher_rows):
        raise ValueError("student_logits and teacher_logits must have the same token count")
    if not student_rows:
        raise ValueError("logits must contain at least one token")

    if mask is None:
        mask_values = (1,) * len(student_rows)
    else:
        mask_values = tuple(int(value) for value in mask)
        if len(mask_values) != len(student_rows):
            raise ValueError("mask length must match token count")
        if any(value not in (0, 1) for value in mask_values):
            raise ValueError("mask values must be 0 or 1")

    losses: list[float] = []
    for selected, student_row, teacher_row in zip(mask_values, student_rows, teacher_rows):
        if not selected:
            continue
        if len(student_row) != len(teacher_row):
            raise ValueError("student and teacher vocab sizes must match")
        if not student_row:
            raise ValueError("logit rows must not be empty")

        if top_k is not None:
            if top_k > len(teacher_row):
                raise ValueError("top_k must not exceed vocab size")
            indices = sorted(
                range(len(teacher_row)),
                key=lambda idx: teacher_row[idx],
                reverse=True,
            )[:top_k]
            student_row = tuple(student_row[index] for index in indices)
            teacher_row = tuple(teacher_row[index] for index in indices)

        student_probs = _softmax(student_row, temperature=temperature)
        teacher_probs = _softmax(teacher_row, temperature=temperature)
        if beta == 0.0:
            losses.append(_kl_divergence(teacher_probs, student_probs))
        elif beta == 1.0:
            losses.append(_kl_divergence(student_probs, teacher_probs))
        else:
            mixture_probs = tuple(
                (1.0 - beta) * student_prob + beta * teacher_prob
                for student_prob, teacher_prob in zip(student_probs, teacher_probs)
            )
            losses.append(
                beta * _kl_divergence(teacher_probs, mixture_probs)
                + (1.0 - beta) * _kl_divergence(student_probs, mixture_probs)
            )

    if not losses:
        raise ValueError("mask selects no tokens")
    return sum(losses) / len(losses)


def _build_one_sequence(
    *,
    prompt_ids: tuple[int, ...],
    completion_ids: tuple[int, ...],
    ignore_index: int,
) -> OpsdTokenSequence:
    input_ids = (*prompt_ids, *completion_ids)
    labels = (*((ignore_index,) * len(prompt_ids)), *completion_ids)
    loss_mask = (*((0,) * len(prompt_ids)), *((1,) * len(completion_ids)))
    token_start = len(prompt_ids)
    token_stop = token_start + len(completion_ids)
    logit_start = len(prompt_ids) - 1
    logit_stop = logit_start + len(completion_ids)
    return OpsdTokenSequence(
        prompt_ids=prompt_ids,
        completion_ids=completion_ids,
        input_ids=input_ids,
        labels=labels,
        loss_mask=loss_mask,
        completion_token_slice=slice(token_start, token_stop),
        completion_logit_slice=slice(logit_start, logit_stop),
    )


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _token_tuple(values: Sequence[int], field_name: str) -> tuple[int, ...]:
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise ValueError(f"{field_name} must be a sequence of token ids")
    try:
        return tuple(int(value) for value in values)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must contain integer token ids") from exc


def _float_tuple(values: Sequence[float], field_name: str) -> tuple[float, ...]:
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise ValueError(f"{field_name} must be a sequence of logits")
    try:
        result = tuple(float(value) for value in values)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must contain numeric logits") from exc
    if any(not math.isfinite(value) for value in result):
        raise ValueError(f"{field_name} must contain only finite logits")
    return result


def _softmax(logits: Sequence[float], *, temperature: float) -> tuple[float, ...]:
    scaled = tuple(value / temperature for value in logits)
    max_value = max(scaled)
    exp_values = tuple(math.exp(value - max_value) for value in scaled)
    total = sum(exp_values)
    return tuple(value / total for value in exp_values)


def _kl_divergence(left_probs: Sequence[float], right_probs: Sequence[float]) -> float:
    total = 0.0
    for left, right in zip(left_probs, right_probs):
        if left > 0.0:
            total += left * math.log(left / right)
    return total
