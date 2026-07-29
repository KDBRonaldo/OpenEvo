from __future__ import annotations

import pytest

from openevo.evolution.parametric.sd_lora_trainer import (
    _compose_cumulative_weights,
    _encode_example,
    _encode_fallback,
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

    normalized = normalize_training_example({"messages": messages, "tools": tools})
    assert normalized["messages"][-1]["tool_calls"][0]["function"]["arguments"] == {
        "command": "pytest -q"
    }
    assert normalized["tools"][0]["function"]["name"] == "run_command"

    messages[-1]["untrusted"] = "value"
    with pytest.raises(ValueError, match="open shape"):
        normalize_training_example({"messages": messages, "tools": tools})


def test_training_data_rejects_surrogates_as_a_contract_error() -> None:
    with pytest.raises(ValueError, match="UTF-8 text budget"):
        normalize_chat_messages([{"role": "assistant", "content": "bad\ud800"}])


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
    expected = coefficients[0] * (prior_b @ prior_a) / (
        prior_a.norm() * prior_b.norm()
    ) + coefficients[1] * (current_b @ current_a)

    torch.testing.assert_close(merged_b @ merged_a, expected)


def test_cumulative_adapter_rejects_zero_norm_prior_component() -> None:
    torch = pytest.importorskip("torch")
    with pytest.raises(ValueError, match="zero or invalid norm"):
        _compose_cumulative_weights(
            torch,
            [
                {"layer": (torch.zeros(1, 2), torch.ones(2, 1))},
                {"layer": (torch.ones(1, 2), torch.ones(2, 1))},
            ],
            (0.8, 0.8),
            ("layer",),
        )
