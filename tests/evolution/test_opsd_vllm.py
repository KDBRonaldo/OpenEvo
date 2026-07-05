from __future__ import annotations

import json

import httpx
import pytest

from polar_evolution.opsd_vllm import VllmOpsdClient


def test_vllm_score_token_ids_requests_full_prompt_logits() -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "prompt_token_ids": [10, 11, 12],
                        "prompt_logprobs": [
                            None,
                            {
                                "0": {"logprob": -1.0},
                                "1": {"logprob": -2.0},
                            },
                            {
                                "0": {"logprob": -3.0},
                                "1": {"logprob": -4.0},
                            },
                        ],
                    }
                ]
            },
        )

    client = VllmOpsdClient(
        base_url="http://vllm.test",
        student_model="student-model",
        teacher_model="teacher-model",
        transport=httpx.MockTransport(handler),
    )

    score = client.score_token_ids(model="student-model", input_ids=[10, 11, 12])

    assert requests == [
        {
            "model": "student-model",
            "prompt": [10, 11, 12],
            "max_tokens": 0,
            "temperature": 0.0,
            "prompt_logprobs": -1,
            "return_token_ids": True,
            "add_special_tokens": False,
        }
    ]
    assert score.input_ids == (10, 11, 12)
    assert score.prompt_logits[0] is None
    assert score.prompt_logits[1] == {0: -1.0, 1: -2.0}
    assert score.completion_logits(slice(1, 3), vocab_size=2) == (
        (-1.0, -2.0),
        (-3.0, -4.0),
    )


def test_vllm_opsd_step_scores_same_student_completion_with_teacher_privilege() -> None:
    requests: list[dict] = []

    def score_response(prompt_ids: list[int], rows: list[dict[int, float] | None]) -> dict:
        return {
            "choices": [
                {
                    "prompt_token_ids": prompt_ids,
                    "prompt_logprobs": [
                        None
                        if row is None
                        else {
                            str(token_id): {"logprob": value}
                            for token_id, value in row.items()
                        }
                        for row in rows
                    ],
                }
            ]
        }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        prompt = body["prompt"]
        if isinstance(prompt, str) and body["max_tokens"] == 2:
            assert "Reference answer" not in prompt
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "text": '{"components":["kinase A"]}',
                            "prompt_token_ids": [10, 11],
                            "token_ids": [20, 21],
                        }
                    ]
                },
            )
        if isinstance(prompt, str) and body["max_tokens"] == 0:
            assert "Reference answer: kinase A" in prompt
            return httpx.Response(
                200,
                json={"choices": [{"prompt_token_ids": [30, 31, 32]}]},
            )
        if body["model"] == "student-model":
            return httpx.Response(
                200,
                json=score_response(
                    [10, 11, 20, 21],
                    [
                        None,
                        {0: 0.0, 1: 0.0},
                        {0: 0.0, 1: 1.0},
                        {0: 1.0, 1: 0.0},
                    ],
                ),
            )
        if body["model"] == "teacher-model":
            return httpx.Response(
                200,
                json=score_response(
                    [30, 31, 32, 20, 21],
                    [
                        None,
                        {0: 0.0, 1: 0.0},
                        {0: 0.0, 1: 0.0},
                        {0: 0.0, 1: 1.0},
                        {0: 1.0, 1: 0.0},
                    ],
                ),
            )
        raise AssertionError(f"unexpected request body: {body}")

    client = VllmOpsdClient(
        base_url="http://vllm.test",
        student_model="student-model",
        teacher_model="teacher-model",
        transport=httpx.MockTransport(handler),
    )

    result = client.run_step(
        problem="Extract biological components from the article.",
        privileged_info="Reference answer: kinase A is supported by paragraph 3.",
        response_instruction="Return JSON only.",
        max_tokens=2,
        vocab_size=2,
    )

    assert result.completion_text == '{"components":["kinase A"]}'
    assert result.sequences.student.input_ids == (10, 11, 20, 21)
    assert result.sequences.teacher.input_ids == (30, 31, 32, 20, 21)
    assert result.student_completion_logits == ((0.0, 1.0), (1.0, 0.0))
    assert result.teacher_completion_logits == ((0.0, 1.0), (1.0, 0.0))
    assert result.loss == pytest.approx(0.0)

    generation_request, teacher_tokenize_request, student_score_request, teacher_score_request = (
        requests
    )
    assert generation_request["model"] == "student-model"
    assert generation_request["return_token_ids"] is True
    assert teacher_tokenize_request["model"] == "teacher-model"
    assert teacher_tokenize_request["return_token_ids"] is True
    assert student_score_request["prompt"] == [10, 11, 20, 21]
    assert teacher_score_request["prompt"] == [30, 31, 32, 20, 21]
