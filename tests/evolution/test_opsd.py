from __future__ import annotations

import math

import pytest

from polar_evolution.opsd import (
    build_opsd_prompt_pair,
    build_opsd_token_sequences,
    generalized_jsd_loss,
)


def test_opsd_prompt_pair_gives_privileged_context_only_to_teacher() -> None:
    pair = build_opsd_prompt_pair(
        problem="Extract biological components from the article.",
        privileged_info="Reference answer: kinase A is supported by paragraph 3.",
        response_instruction="Return JSON only.",
    )

    assert "Extract biological components" in pair.student_prompt
    assert "Return JSON only." in pair.student_prompt
    assert "Reference answer" not in pair.student_prompt
    assert "kinase A" not in pair.student_prompt

    assert "Extract biological components" in pair.teacher_prompt
    assert "Reference answer: kinase A" in pair.teacher_prompt
    assert "=== Privileged Information Begin ===" in pair.teacher_prompt
    assert "=== Privileged Information End ===" in pair.teacher_prompt
    assert "using your own words and independent reasoning" in pair.teacher_prompt


def test_opsd_token_sequences_append_same_student_completion_and_mask_prompt() -> None:
    sequences = build_opsd_token_sequences(
        student_prompt_ids=[11, 12, 13],
        teacher_prompt_ids=[21, 22, 23, 24, 25],
        completion_ids=[31, 32],
    )

    assert sequences.student.input_ids == (11, 12, 13, 31, 32)
    assert sequences.teacher.input_ids == (21, 22, 23, 24, 25, 31, 32)
    assert sequences.student.target_ids == (31, 32)
    assert sequences.teacher.target_ids == (31, 32)
    assert sequences.student.loss_mask == (0, 0, 0, 1, 1)
    assert sequences.teacher.loss_mask == (0, 0, 0, 0, 0, 1, 1)

    assert sequences.student.completion_token_slice == slice(3, 5)
    assert sequences.teacher.completion_token_slice == slice(5, 7)

    # Official OPSD forwards [prompt][completion] and scores completion tokens
    # from logits[prompt_len - 1 : -1].
    assert sequences.student.completion_logit_slice == slice(2, 4)
    assert sequences.teacher.completion_logit_slice == slice(4, 6)


def test_opsd_token_sequences_reject_empty_completion() -> None:
    with pytest.raises(ValueError, match="completion_ids must not be empty"):
        build_opsd_token_sequences(
            student_prompt_ids=[11],
            teacher_prompt_ids=[21],
            completion_ids=[],
        )


def test_generalized_jsd_loss_is_zero_for_identical_logits() -> None:
    logits = [[0.0, 1.0, 2.0], [3.0, 0.0, -1.0]]

    assert generalized_jsd_loss(logits, logits) == pytest.approx(0.0)


def test_generalized_jsd_loss_masks_tokens_before_averaging() -> None:
    student_logits = [[0.0, 2.0], [20.0, -20.0]]
    teacher_logits = [[0.0, 2.0], [-20.0, 20.0]]

    loss = generalized_jsd_loss(student_logits, teacher_logits, mask=[1, 0])

    assert math.isclose(loss, 0.0, abs_tol=1e-12)


def test_generalized_jsd_loss_beta_endpoints_match_official_kl_directions() -> None:
    student_logits = [[math.log(0.8), math.log(0.2)]]
    teacher_logits = [[math.log(0.5), math.log(0.5)]]

    teacher_to_student_kl = 0.5 * math.log(0.5 / 0.8) + 0.5 * math.log(0.5 / 0.2)
    student_to_teacher_kl = 0.8 * math.log(0.8 / 0.5) + 0.2 * math.log(0.2 / 0.5)

    assert generalized_jsd_loss(
        student_logits,
        teacher_logits,
        beta=0.0,
    ) == pytest.approx(teacher_to_student_kl)
    assert generalized_jsd_loss(
        student_logits,
        teacher_logits,
        beta=1.0,
    ) == pytest.approx(student_to_teacher_kl)
