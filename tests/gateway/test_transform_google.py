from __future__ import annotations

from polar.gateway.transform.google import GoogleTransformer

IMAGE_B64 = "abc123"
IMAGE_URL = f"data:image/png;base64,{IMAGE_B64}"


def test_google_request_maps_all_fields_and_image_input_to_chat() -> None:
    transformer = GoogleTransformer()

    transformed = transformer.transform_request(
        {
            "_polar_model_served": "Qwen/Qwen3.5-4B",
            "_streaming": True,
            "config": {
                "systemInstruction": {"parts": [{"text": "Be direct."}]},
                "generationConfig": {
                    "maxOutputTokens": 128,
                    "temperature": 0.2,
                    "topP": 0.9,
                    "stopSequences": ["END"],
                },
                "tools": [
                    {
                        "functionDeclarations": [
                            {
                                "name": "write_answer",
                                "description": "Write the answer",
                                "parameters": {"type": "object"},
                            }
                        ]
                    }
                ],
                "toolConfig": {
                    "functionCallingConfig": {
                        "mode": "ANY",
                        "allowedFunctionNames": ["write_answer"],
                    }
                },
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": "Count stars."},
                        {
                            "inlineData": {
                                "mimeType": "image/png",
                                "data": IMAGE_B64,
                            }
                        },
                        {
                            "fileData": {
                                "mimeType": "image/jpeg",
                                "fileUri": "https://example.test/star.jpg",
                            }
                        },
                    ],
                },
                {
                    "role": "model",
                    "parts": [
                        {"text": "I will call a tool."},
                        {
                            "functionCall": {
                                "id": "call-1",
                                "name": "write_answer",
                                "args": {"answer": 2},
                            }
                        },
                    ],
                },
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "id": "call-1",
                                "name": "write_answer",
                                "response": {"ok": True},
                            }
                        }
                    ],
                },
            ],
        }
    )

    assert transformed["messages"] == [
        {"role": "system", "content": "Be direct."},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Count stars."},
                {"type": "image_url", "image_url": {"url": IMAGE_URL}},
                {"type": "image_url", "image_url": {"url": "https://example.test/star.jpg"}},
            ],
        },
        {
            "role": "assistant",
            "content": "I will call a tool.",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "write_answer",
                        "arguments": '{"answer": 2}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": '{"ok": true}'},
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


def test_google_response_maps_openai_content_tools_finish_and_usage_back() -> None:
    transformer = GoogleTransformer()

    response = transformer.transform_response(
        {
            "choices": [
                {
                    "index": 0,
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
                    "finish_reason": "length",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        },
        {},
    )

    assert response == {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "There are two."},
                        {"inline_data": {"mime_type": "image/png", "data": IMAGE_B64}},
                        {
                            "functionCall": {
                                "name": "write_answer",
                                "args": {"answer": 2},
                                "id": "call-1",
                            }
                        },
                    ],
                    "role": "model",
                },
                "finishReason": "MAX_TOKENS",
                "index": 0,
                "safetyRatings": [],
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 10,
            "candidatesTokenCount": 4,
            "totalTokenCount": 14,
        },
        "functionCalls": [{"name": "write_answer", "args": {"answer": 2}, "id": "call-1"}],
    }


def test_google_stream_state_accumulates_tool_deltas_and_usage() -> None:
    transformer = GoogleTransformer()
    state = transformer.create_stream_state({})

    events = state.process_chunk(
        {
            "choices": [{"delta": {"content": "Hi"}}],
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
                "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            }
        )
    )
    events.extend(state.finalize())

    assert events[0]["candidates"][0]["content"]["parts"] == [{"text": "Hi"}]
    assert events[0]["usageMetadata"] == {
        "promptTokenCount": 4,
        "candidatesTokenCount": 1,
        "totalTokenCount": 5,
    }
    assert events[-1]["candidates"][0]["finishReason"] == "STOP"
    assert events[-1]["functionCalls"] == [
        {"name": "write_answer", "args": {"answer": 2}, "id": "call-1"}
    ]


def test_google_stream_state_preserves_text_finish_reason() -> None:
    transformer = GoogleTransformer()
    state = transformer.create_stream_state({})

    events = state.process_chunk(
        {
            "choices": [
                {
                    "delta": {"content": "All done."},
                    "finish_reason": "stop",
                }
            ]
        }
    )

    assert events[0]["candidates"][0]["content"]["parts"] == [{"text": "All done."}]
    assert events[0]["candidates"][0]["finishReason"] == "STOP"
    assert state.finalize() == []


def test_google_stream_state_emits_finish_only_text_event() -> None:
    transformer = GoogleTransformer()
    state = transformer.create_stream_state({})

    events = state.process_chunk(
        {
            "choices": [{"delta": {}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
        }
    )

    assert events[0]["candidates"][0]["content"]["parts"] == []
    assert events[0]["candidates"][0]["finishReason"] == "MAX_TOKENS"
    assert events[0]["usageMetadata"] == {
        "promptTokenCount": 4,
        "candidatesTokenCount": 2,
        "totalTokenCount": 6,
    }
