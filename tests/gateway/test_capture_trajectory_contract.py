from __future__ import annotations

import pytest

from openevo.gateway.storage import SessionStore
from openevo.trajectory.models import StrategySpec
from openevo.trajectory.registry import default_builder_registry


@pytest.mark.asyncio
async def test_stored_completion_builds_aligned_token_trajectory() -> None:
    store = SessionStore()
    store.ensure_session(
        "capture-probe",
        model_requested="requested-model",
        model_used="served-model",
        api_type="openai_chat",
        task_id="task-probe",
        metadata={"rollout_step": 7},
    )
    store.save_message(
        "capture-probe",
        request={
            "model": "served-model",
            "messages": [{"role": "user", "content": "Return two tokens."}],
        },
        response={
            "choices": [
                {
                    "input_token_ids": [101, 102, 103],
                    "token_ids": [201, 202],
                    "message": {"role": "assistant", "content": "two tokens"},
                    "finish_reason": "stop",
                    "logprobs": {
                        "content": [
                            {"token_id": 201, "logprob": -0.125},
                            {"token_id": 202, "logprob": -0.5},
                        ]
                    },
                }
            ]
        },
        original_request={
            "model": "requested-model",
            "messages": [{"role": "user", "content": "Return two tokens."}],
        },
        model_requested="requested-model",
        model_used="served-model",
        api_type="openai_chat",
        task_id="task-probe",
    )
    completion_session = store.load_completion_session("capture-probe")
    builder = default_builder_registry().create(StrategySpec(strategy="per_request"))

    trajectory = await builder.build(completion_session)

    assert trajectory.status == "COMPLETED"
    assert trajectory.metadata["builder"] == "per_request"
    assert trajectory.metadata["record_count"] == 1
    assert trajectory.metadata["rollout_step"] == 7
    trace = trajectory.traces[0]
    assert trace.prompt_ids == [101, 102, 103]
    assert trace.response_ids == [201, 202]
    assert trace.loss_mask == [1, 1]
    assert trace.response_logprobs == [-0.125, -0.5]
    assert len(trace.response_ids) == len(trace.loss_mask)
    assert len(trace.response_ids) == len(trace.response_logprobs or [])
