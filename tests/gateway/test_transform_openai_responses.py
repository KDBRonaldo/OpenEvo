from __future__ import annotations

from polar.gateway.transform.openai_responses import OpenAIResponsesTransformer


def test_responses_request_maps_input_to_chat_messages_and_training_fields() -> None:
    transformer = OpenAIResponsesTransformer()

    transformed = transformer.transform_request(
        {
            "_polar_model_served": "Qwen/Qwen3.5-4B",
            "instructions": "You are a coding agent.",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Fix the bug."}],
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
            ],
            "max_output_tokens": 128,
            "tool_choice": "auto",
            "tools": [
                {
                    "name": "shell",
                    "description": "Run a shell command",
                    "parameters": {"type": "object"},
                }
            ],
        }
    )

    assert transformed["messages"] == [
        {"role": "system", "content": "You are a coding agent."},
        {"role": "user", "content": "Fix the bug."},
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
    ]
    assert transformed["max_tokens"] == 128
    assert transformed["tool_choice"] == "auto"
    assert transformed["logprobs"] is True
    assert transformed["chat_template_kwargs"]["enable_thinking"] is False


def test_responses_request_drops_tool_choice_when_tools_are_empty() -> None:
    transformer = OpenAIResponsesTransformer()

    transformed = transformer.transform_request(
        {
            "input": "hello",
            "tool_choice": "auto",
            "tools": [],
        }
    )

    assert "tool_choice" not in transformed
    assert "tools" not in transformed


def test_responses_transform_response_maps_chat_result_back_to_response_shape() -> None:
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

    assert response["model"] == "requested-model"
    assert response["usage"] == {
        "input_tokens": 10,
        "output_tokens": 3,
        "total_tokens": 13,
    }
    assert response["output"][0]["content"][0]["text"] == "Done"
    assert response["output"][1]["type"] == "function_call"
    assert response["output"][1]["name"] == "lookup"


def test_responses_stream_state_emits_created_delta_and_completed_events() -> None:
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
                        "delta": {"content": "lo"},
                        "finish_reason": "stop",
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
    assert event_types[-1] == "response.completed"
    assert events[-1]["response"]["output"][0]["content"][0]["text"] == "Hello"
