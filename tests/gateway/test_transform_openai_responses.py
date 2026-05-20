from __future__ import annotations

from polar.gateway.transform.openai_responses import OpenAIResponsesTransformer

IMAGE_URL = "data:image/png;base64,abc123"


def test_responses_request_maps_all_fields_and_image_input_to_chat() -> None:
    transformer = OpenAIResponsesTransformer()

    transformed = transformer.transform_request(
        {
            "_polar_model_served": "Qwen/Qwen3.5-4B",
            "instructions": "You are a coding agent.",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Count stars."},
                        {"type": "input_image", "image_url": IMAGE_URL, "detail": "low"},
                    ],
                },
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "shell",
                    "arguments": '{"cmd": "pytest"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": "failed",
                },
                {"type": "input_text", "text": "Count again."},
                {"type": "input_image", "image_url": IMAGE_URL},
            ],
            "max_output_tokens": 128,
            "temperature": 0.2,
            "top_p": 0.9,
            "stream": True,
            "tool_choice": "auto",
            "tools": [
                {
                    "name": "shell",
                    "description": "Run a shell command",
                    "parameters": {"type": "object"},
                    "strict": True,
                }
            ],
        }
    )

    assert transformed["messages"] == [
        {"role": "system", "content": "You are a coding agent."},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Count stars."},
                {"type": "image_url", "image_url": {"url": IMAGE_URL, "detail": "low"}},
            ],
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "shell", "arguments": '{"cmd": "pytest"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "failed"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Count again."},
                {"type": "image_url", "image_url": {"url": IMAGE_URL}},
            ],
        },
    ]
    assert transformed["max_tokens"] == 128
    assert transformed["temperature"] == 0.2
    assert transformed["top_p"] == 0.9
    assert transformed["stream"] is True
    assert transformed["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "shell",
                "description": "Run a shell command",
                "parameters": {"type": "object"},
                "strict": True,
            },
        }
    ]
    assert transformed["tool_choice"] == "auto"
    assert transformed["logprobs"] is True
    assert transformed["chat_template_kwargs"]["enable_thinking"] is False


def test_responses_request_moves_image_function_output_to_user_message() -> None:
    transformer = OpenAIResponsesTransformer()

    transformed = transformer.transform_request(
        {
            "input": [
                {"type": "message", "role": "user", "content": "Look at the image."},
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "view_image",
                    "arguments": '{"path": "/polar/session/workspace/polar_stars.png"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": {
                        "body": [
                            {"type": "input_image", "image_url": IMAGE_URL, "detail": "high"}
                        ],
                        "success": True,
                    },
                },
            ],
        }
    )

    assert transformed["messages"] == [
        {"role": "user", "content": "Look at the image."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "view_image",
                        "arguments": '{"path": "/polar/session/workspace/polar_stars.png"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": ""},
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": IMAGE_URL, "detail": "high"}}
            ],
        },
    ]


def test_responses_request_drops_tool_choice_when_tools_are_empty() -> None:
    transformer = OpenAIResponsesTransformer()

    transformed = transformer.transform_request(
        {
            "input": "hello",
            "tool_choice": "auto",
            "tools": [],
        }
    )

    assert transformed["messages"] == [{"role": "user", "content": "hello"}]
    assert "tool_choice" not in transformed
    assert "tools" not in transformed
    assert transformed["logprobs"] is True


def test_responses_response_maps_chat_result_back_to_response_shape() -> None:
    transformer = OpenAIResponsesTransformer()

    response = transformer.transform_response(
        {
            "id": "chatcmpl-1",
            "model": "served-model",
            "created": 123,
            "choices": [
                {
                    "message": {
                        "content": "Done",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "function": {"name": "lookup", "arguments": '{"q": "x"}'},
                            }
                        ],
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 3,
                "total_tokens": 13,
            },
        },
        {"model": "requested-model"},
    )

    assert response == {
        "id": "chatcmpl-1",
        "object": "response",
        "created_at": 123,
        "status": "completed",
        "model": "requested-model",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "Done"}],
            },
            {
                "type": "function_call",
                "id": response["output"][1]["id"],
                "call_id": "call-1",
                "name": "lookup",
                "arguments": '{"q": "x"}',
                "status": "completed",
            },
        ],
        "usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13},
    }


def test_responses_stream_state_emits_response_events_for_text_and_tools() -> None:
    transformer = OpenAIResponsesTransformer()
    state = transformer.create_stream_state({"model": "requested-model"})

    events = state.process_chunk(
        {
            "choices": [{"delta": {"content": "Hel"}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
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
                                    "function": {"name": "lookup", "arguments": '{"q"'},
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
                            "content": "lo",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": ': "x"}'},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            }
        )
    )
    events.extend(state.finalize())

    event_types = [event["type"] for event in events]
    assert event_types[0] == "response.created"
    assert "response.output_text.delta" in event_types
    assert "response.function_call_arguments.delta" in event_types
    assert events[-1]["type"] == "response.completed"
    assert events[-1]["response"]["model"] == "requested-model"
    assert events[-1]["response"]["output"][0]["content"][0]["text"] == "Hello"
    assert events[-1]["response"]["output"][1]["arguments"] == '{"q": "x"}'
