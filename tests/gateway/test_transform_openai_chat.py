from __future__ import annotations

from polar.gateway.transform.openai_chat import OpenAIChatTransformer

IMAGE_URL = "data:image/png;base64,abc123"


def test_openai_chat_request_preserves_fields_and_image_content() -> None:
    transformer = OpenAIChatTransformer()

    body = {
        "_polar_model_served": "Qwen/Qwen3.5-4B",
        "model": "client-visible-model",
        "messages": [
            {"role": "developer", "content": "Use short answers."},
            {"role": "system", "content": "Be precise."},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Count stars."},
                    {"type": "image_url", "image_url": {"url": IMAGE_URL, "detail": "low"}},
                ],
            },
        ],
        "max_tokens": 64,
        "temperature": 0.2,
        "top_p": 0.9,
        "stream": True,
        "stop": ["END"],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "write_answer",
                    "description": "Write the answer",
                    "parameters": {"type": "object"},
                },
            }
        ],
        "tool_choice": "auto",
        "chat_template_kwargs": {"foo": "bar"},
    }

    transformed = transformer.transform_request(body)

    assert "_polar_model_served" not in transformed
    assert transformed["model"] == "client-visible-model"
    assert transformed["messages"] == [
        {"role": "system", "content": "Use short answers.\n\nBe precise."},
        body["messages"][2],
    ]
    assert transformed["max_tokens"] == 64
    assert transformed["temperature"] == 0.2
    assert transformed["top_p"] == 0.9
    assert transformed["stream"] is True
    assert transformed["stop"] == ["END"]
    assert transformed["tools"] == body["tools"]
    assert transformed["tool_choice"] == "auto"
    assert transformed["logprobs"] is True
    assert transformed["chat_template_kwargs"] == {"foo": "bar", "enable_thinking": False}


def test_openai_chat_response_and_stream_preserve_requested_model() -> None:
    transformer = OpenAIChatTransformer()
    upstream = {
        "id": "chatcmpl-1",
        "model": "served-model",
        "choices": [{"message": {"content": "Done"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
    }

    response = transformer.transform_response(upstream, {"model": "requested-model"})
    stream_chunk = transformer.transform_stream_chunk(
        {
            "id": "chatcmpl-1",
            "model": "served-model",
            "choices": [{"delta": {"content": "D"}}],
        },
        {"model": "requested-model"},
    )

    assert response == {**upstream, "model": "requested-model"}
    assert stream_chunk["model"] == "requested-model"
    assert stream_chunk["choices"][0]["delta"]["content"] == "D"
