from __future__ import annotations

from types import SimpleNamespace

import pytest

import openevo.evolution.parametric.sd_lora_trainer as trainer_module
from openevo.evolution.parametric.contracts import (
    SdLoraMethodConfig,
    SdLoraTrainingRequest,
)
from openevo.evolution.parametric.sd_lora_trainer import (
    _bounded_replay_buffer,
    _component_direction_frobenius_norm,
    _compose_cumulative_weights,
    _encode_example,
    _encode_fallback,
    _initial_coefficient_values,
    _normalize_component_direction,
    _planned_optimizer_steps,
    _select_replay_examples,
)
from openevo.evolution.parametric.training_data import (
    normalize_chat_messages,
    normalize_tool_definitions,
    normalize_training_example,
)


class _CharacterTokenizer:
    bos_token_id = None
    eos_token_id = None

    def encode(self, value: str, *, add_special_tokens: bool) -> list[int]:
        assert add_special_tokens is False
        return [ord(character) for character in value]


class _OffsetTemplateTokenizer(_CharacterTokenizer):
    chat_template = "template-without-generation-block"
    truncation_side = "left"

    def apply_chat_template(
        self,
        messages,
        *,
        tokenize: bool,
        add_generation_prompt: bool,
        **kwargs,
    ):
        del kwargs
        assert tokenize is False
        rendered = "".join(
            f"<{message['role']}>{message['content']}</{message['role']}>" for message in messages
        )
        if add_generation_prompt:
            rendered += "<assistant>"
        return rendered

    def __call__(
        self,
        value: str,
        *,
        add_special_tokens: bool,
        truncation: bool,
        max_length: int,
        return_offsets_mapping: bool,
    ):
        assert add_special_tokens is False
        assert truncation is True
        assert return_offsets_mapping is True
        start = max(0, len(value) - max_length)
        return {
            "input_ids": [ord(character) for character in value[start:]],
            "offset_mapping": [(index, index + 1) for index in range(start, len(value))],
        }


def test_fallback_encoding_left_truncates_long_tool_context_but_keeps_target() -> None:
    encoded = _encode_fallback(
        _CharacterTokenizer(),
        [
            {"role": "tool", "content": "x" * 1000, "tool_call_id": "call-read"},
            {"role": "assistant", "content": "TARGET"},
        ],
        24,
    )

    supervised = [token for token in encoded["labels"] if token != -100]
    assert supervised
    assert "TARGET" in "".join(chr(token) for token in supervised)


def test_chat_template_offset_mask_uses_exact_rendering_and_keeps_target() -> None:
    encoded = _encode_example(
        _OffsetTemplateTokenizer(),
        {
            "messages": [
                {"role": "user", "content": "x" * 1000},
                {"role": "assistant", "content": "TARGET"},
            ]
        },
        32,
    )

    supervised = [token for token in encoded["labels"] if token != -100]
    assert "TARGET" in "".join(chr(token) for token in supervised)
    assert "<assistant>" not in "".join(chr(token) for token in supervised)


def test_chat_template_offset_mask_excludes_historical_assistant_messages() -> None:
    encoded = _encode_example(
        _OffsetTemplateTokenizer(),
        {
            "messages": [
                {"role": "user", "content": "Start the task."},
                {"role": "assistant", "content": "HISTORY"},
                {"role": "tool", "content": "Observed output.", "tool_call_id": "call-1"},
                {"role": "assistant", "content": "TARGET"},
            ],
            "target_message_start": 3,
        },
        256,
    )

    supervised_text = "".join(chr(token) for token in encoded["labels"] if token != -100)
    assert "TARGET" in supervised_text
    assert "HISTORY" not in supervised_text


def test_training_data_normalizes_generic_tool_messages_and_rejects_open_shapes() -> None:
    messages = normalize_chat_messages(
        [
            {"role": "user", "content": "Run the task.", "ignored": "not projected"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "run_command",
                            "arguments": {"command": "pytest -q"},
                        },
                    }
                ],
            },
        ]
    )
    tools = normalize_tool_definitions(
        [
            {
                "name": "run_command",
                "description": "Run one command.",
                "parameters_schema": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            }
        ]
    )

    normalized = normalize_training_example(
        {"messages": messages, "target_message_start": 1, "tools": tools}
    )
    assert normalized["target_message_start"] == 1
    assert normalized["messages"][-1]["tool_calls"][0]["function"]["arguments"] == {
        "command": "pytest -q"
    }
    assert normalized["tools"][0]["function"]["name"] == "run_command"

    messages[-1]["untrusted"] = "value"
    with pytest.raises(ValueError, match="open shape"):
        normalize_training_example(
            {"messages": messages, "target_message_start": 1, "tools": tools}
        )


@pytest.mark.parametrize("target_message_start", [True, -1, 2])
def test_training_data_rejects_invalid_target_message_boundaries(
    target_message_start: object,
) -> None:
    with pytest.raises(ValueError, match="outside the message sequence"):
        normalize_training_example(
            {
                "messages": [
                    {"role": "user", "content": "Question"},
                    {"role": "assistant", "content": "Answer"},
                ],
                "target_message_start": target_message_start,
            }
        )


def test_training_data_requires_assistant_target_after_boundary() -> None:
    with pytest.raises(ValueError, match="no assistant target"):
        normalize_training_example(
            {
                "messages": [
                    {"role": "assistant", "content": "Historical answer"},
                    {"role": "user", "content": "New question"},
                ],
                "target_message_start": 1,
            }
        )


def test_training_data_rejects_surrogates_as_a_contract_error() -> None:
    with pytest.raises(ValueError, match="UTF-8 text budget"):
        normalize_chat_messages([{"role": "assistant", "content": "bad\ud800"}])


def test_training_data_projects_empty_gateway_tool_calls_only_for_raw_text() -> None:
    assert normalize_chat_messages(
        [{"role": "assistant", "content": "Plain gateway response.", "tool_calls": []}]
    ) == [{"role": "assistant", "content": "Plain gateway response."}]

    with pytest.raises(ValueError, match="requires content"):
        normalize_chat_messages([{"role": "assistant", "content": "", "tool_calls": []}])

    with pytest.raises(ValueError, match="non-empty bounded list"):
        normalize_training_example(
            {
                "messages": [
                    {"role": "user", "content": "Question"},
                    {"role": "assistant", "content": "Answer", "tool_calls": []},
                ]
            }
        )


def test_training_data_preserves_empty_tool_observations_with_call_identity() -> None:
    messages = normalize_chat_messages(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-empty",
                        "type": "function",
                        "function": {
                            "name": "shell",
                            "arguments": {"commands": ["mkdir -p /app/output"]},
                        },
                    }
                ],
            },
            {"role": "tool", "content": "", "tool_call_id": "call-empty"},
            {"role": "assistant", "content": "The directory is ready."},
        ]
    )

    assert messages[1] == {
        "role": "tool",
        "content": "",
        "tool_call_id": "call-empty",
    }
    assert normalize_training_example({"messages": messages})["messages"] == messages


def test_sd_lora_schedule_requires_two_steps_for_zero_initialized_b() -> None:
    assert (
        _planned_optimizer_steps(
            record_count=1,
            batch_size=1,
            epochs=1,
            gradient_accumulation_steps=1,
            max_steps=None,
        )
        == 1
    )
    assert (
        _planned_optimizer_steps(
            record_count=5,
            batch_size=2,
            epochs=3,
            gradient_accumulation_steps=2,
            max_steps=4,
        )
        == 4
    )


def test_sd_lora_trainer_rejects_one_step_before_loading_the_model(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = SdLoraTrainingRequest(
        request_id="sd-lora-one-step",
        work_dir=str(tmp_path),
        training_data_path="training.jsonl",
        output_adapter_path="adapter",
        adapter_id="sd-lora-one-step",
        source_dataset_artifact_ids=("dataset-one",),
        training_record_count=1,
        config=SdLoraMethodConfig(
            base_model="Qwen/Qwen3-4B-Instruct-2507",
            model_revision="cdbee75f17c01a7cc42f958dc650907174af0554",
            epochs=1,
        ),
    )
    monkeypatch.setattr(
        trainer_module,
        "_dependencies",
        lambda: (_ for _ in ()).throw(AssertionError("model should not load")),
    )

    with pytest.raises(ValueError, match="at least two optimizer steps"):
        trainer_module._train(request)


def _replay_example(task_id: str, target: str) -> dict[str, object]:
    return {
        "messages": [
            {"role": "user", "content": f"Run {task_id}."},
            {"role": "assistant", "content": target},
        ],
        "target_message_start": 1,
        "metadata": {"task_id": task_id},
    }


def test_replay_buffer_deduplicates_and_balances_tasks() -> None:
    duplicate = normalize_training_example(_replay_example("task-a", "same"))
    selected = _bounded_replay_buffer(
        [duplicate, normalize_training_example(_replay_example("task-a", "old-a"))],
        [
            normalize_training_example(_replay_example("task-b", "new-b")),
            normalize_training_example({**duplicate, "metadata": {"task_id": "task-b"}}),
        ],
        capacity=2,
    )

    assert len(selected) == 2
    assert {record["metadata"]["task_id"] for record in selected} == {
        "task-a",
        "task-b",
    }


def test_training_data_preserves_closed_replay_group_metadata() -> None:
    example = _replay_example("task-a", "answer")
    example["metadata"] = {
        "dataset_artifact_id": "artifact-a",
        "dataset_name": "dataset-a",
        "event_id": "event-a",
        "reward": 1.0,
        "session_id": "session-a",
        "status": "completed",
        "task_id": "task-a",
        "trace_index": 3,
    }

    assert normalize_training_example(example)["metadata"] == example["metadata"]

    example["metadata"] = {"task_id": "task-a", "untrusted": "value"}
    with pytest.raises(ValueError, match="metadata has an open"):
        normalize_training_example(example)


def test_replay_sampling_is_deterministic_and_balanced_with_current_count() -> None:
    replay = [
        _replay_example("task-a", "a-1"),
        _replay_example("task-a", "a-2"),
    ]

    first = _select_replay_examples(replay, count=5, seed=17)
    second = _select_replay_examples(replay, count=5, seed=17)

    assert first == second
    assert len(first) == 5
    assert {record["messages"][-1]["content"] for record in first} == {"a-1", "a-2"}


def test_new_generation_restores_prior_coefficients_before_optimization() -> None:
    manifest = SimpleNamespace(
        components=(
            SimpleNamespace(coefficient=0.37),
            SimpleNamespace(coefficient=1.25),
        )
    )

    assert _initial_coefficient_values(manifest, 0.8) == (0.37, 1.25, 0.8)


def test_cumulative_adapter_exactly_matches_sd_lora_forward_rule() -> None:
    torch = pytest.importorskip("torch")
    prior_a = torch.tensor([[1.0, 2.0]])
    prior_b = torch.tensor([[3.0], [4.0]])
    current_a = torch.tensor([[2.0, -1.0]])
    current_b = torch.tensor([[1.5], [-2.0]])
    coefficients = (0.6, 1.25)

    merged = _compose_cumulative_weights(
        torch,
        [
            {"layer": (prior_a, prior_b)},
            {"layer": (current_a, current_b)},
        ],
        coefficients,
        ("layer",),
    )
    merged_a, merged_b = merged["layer"]
    expected = coefficients[0] * (prior_b @ prior_a) + coefficients[1] * (current_b @ current_a)

    torch.testing.assert_close(merged_b @ merged_a, expected)


def test_cumulative_adapter_is_exactly_continuous_across_generations() -> None:
    torch = pytest.importorskip("torch")
    prior = {
        "layer": (
            torch.tensor([[1.0, -2.0]]),
            torch.tensor([[0.5], [3.0]]),
        )
    }
    trained_coefficient = 0.73
    normalized_prior, direction_norm = _normalize_component_direction(
        torch,
        prior,
        ("layer",),
    )
    coefficient = trained_coefficient * direction_norm
    assert _component_direction_frobenius_norm(
        torch,
        normalized_prior,
        ("layer",),
    ) == pytest.approx(1.0)
    generation_zero = _compose_cumulative_weights(
        torch,
        [normalized_prior],
        (coefficient,),
        ("layer",),
    )
    generation_one = _compose_cumulative_weights(
        torch,
        [
            normalized_prior,
            {"layer": (torch.ones(1, 2), torch.zeros(2, 1))},
        ],
        (coefficient, 0.8),
        ("layer",),
    )

    zero_a, zero_b = generation_zero["layer"]
    one_a, one_b = generation_one["layer"]
    torch.testing.assert_close(
        zero_b @ zero_a,
        trained_coefficient * (prior["layer"][1] @ prior["layer"][0]),
    )
    torch.testing.assert_close(one_b @ one_a, zero_b @ zero_a)
