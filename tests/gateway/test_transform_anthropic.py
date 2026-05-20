from __future__ import annotations

from polar.gateway.transform.anthropic import AnthropicTransformer

IMAGE_B64 = "abc123"
IMAGE_URL = f"data:image/png;base64,{IMAGE_B64}"


def test_anthropic_request_maps_all_fields_and_image_input_to_chat() -> None:
    transformer = AnthropicTransformer()

    transformed = transformer.transform_request(
        {
            "_polar_model_served": "Qwen/Qwen3.5-4B",
            "system": "x-anthropic-billing-header: cch=unstable;\nBe direct.",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Count stars."},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": IMAGE_B64,
                            },
                        },
                    ],
                },
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "I will call a tool."},
                        {
                            "type": "tool_use",
                            "id": "toolu-1",
                            "name": "write_answer",
                            "input": {"answer": 2},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu-1",
                            "content": [
                                {"type": "text", "text": "ok"},
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/png",
                                        "data": IMAGE_B64,
                                    },
                                },
                            ],
                        },
                        {"type": "text", "text": "Thanks."},
                    ],
                },
            ],
            "max_tokens": 128,
            "temperature": 0.2,
            "top_p": 0.9,
            "stop_sequences": ["END"],
            "stream": True,
            "tools": [
                {
                    "name": "write_answer",
                    "description": "Write the answer",
                    "input_schema": {"type": "object"},
                }
            ],
            "tool_choice": {"type": "tool", "name": "write_answer"},
        }
    )

    assert transformed["messages"] == [
        {"role": "system", "content": "Be direct."},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Count stars."},
                {"type": "image_url", "image_url": {"url": IMAGE_URL}},
            ],
        },
        {
            "role": "assistant",
            "content": "I will call a tool.",
            "tool_calls": [
                {
                    "id": "toolu-1",
                    "type": "function",
                    "function": {
                        "name": "write_answer",
                        "arguments": '{"answer": 2}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "toolu-1", "content": "ok"},
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": IMAGE_URL}}]},
        {"role": "user", "content": "Thanks."},
    ]
    assert transformed["max_tokens"] == 128
    assert transformed["temperature"] == 0.2
    assert transformed["top_p"] == 0.9
    assert transformed["stop"] == ["END"]
    assert transformed["stream"] is True
    assert transformed["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "write_answer",
                "description": "Write the answer",
                "parameters": {"type": "object"},
            },
        }
    ]
    assert transformed["tool_choice"] == {
        "type": "function",
        "function": {"name": "write_answer"},
    }
    assert transformed["logprobs"] is True
    assert transformed["chat_template_kwargs"]["enable_thinking"] is False


def test_anthropic_response_maps_openai_content_and_usage_back() -> None:
    transformer = AnthropicTransformer()

    response = transformer.transform_response(
        {
            "id": "chatcmpl-1",
            "choices": [
                {
                    "message": {
                        "content": [
                            {"type": "text", "text": "There are two."},
                            {"type": "image_url", "image_url": {"url": IMAGE_URL}},
                        ],
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "function": {"name": "write_answer", "arguments": '{"answer": 2}'},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4},
        },
        {"model": "claude-test"},
    )

    assert response["id"] == "msg_chatcmpl-1"
    assert response["type"] == "message"
    assert response["role"] == "assistant"
    assert response["model"] == "claude-test"
    assert response["stop_reason"] == "tool_use"
    assert response["usage"] == {"input_tokens": 10, "output_tokens": 4}
    assert response["content"] == [
        {"type": "text", "text": "There are two."},
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": IMAGE_B64},
        },
        {
            "type": "tool_use",
            "id": "call-1",
            "name": "write_answer",
            "input": {"answer": 2},
        },
    ]


def test_anthropic_response_skips_empty_openai_content_with_tool_call() -> None:
    transformer = AnthropicTransformer()

    response = transformer.transform_response(
        {
            "id": "chatcmpl-1",
            "choices": [
                {
                    "message": {
                        "content": [],
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "function": {"name": "view_image", "arguments": "{}"},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4},
        },
        {"model": "claude-test"},
    )

    assert response["content"] == [
        {
            "type": "tool_use",
            "id": "call-1",
            "name": "view_image",
            "input": {},
        }
    ]


def test_anthropic_stream_state_emits_ordered_text_tool_and_usage_events() -> None:
    transformer = AnthropicTransformer()
    state = transformer.create_stream_state({"model": "claude-test"})

    events = state.process_chunk(
        {
            "choices": [{"delta": {"content": "Hi"}}],
            "usage": {"completion_tokens": 1},
        },
        is_first=True,
    )
    events.extend(
        state.process_chunk(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-1",
                                    "function": {"name": "write_answer", "arguments": '{"answer"'},
                                }
                            ]
                        }
                    }
                ]
            }
        )
    )
    events.extend(
        state.process_chunk(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": ": 2}"},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"completion_tokens": 3},
            }
        )
    )
    events.extend(state.finalize())

    event_types = [event["type"] for event in events]
    assert event_types[0] == "message_start"
    assert "content_block_start" in event_types
    assert "content_block_delta" in event_types
    assert events[-2] == {
        "type": "message_delta",
        "delta": {"stop_reason": "tool_use", "stop_sequence": None},
        "usage": {"output_tokens": 3},
    }
    assert events[-1] == {"type": "message_stop"}
