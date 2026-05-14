from __future__ import annotations

from polar.gateway.transform.openai_chat import OpenAIChatTransformer


def test_qwen_chat_request_merges_developer_role_and_enables_training_fields() -> None:
    transformer = OpenAIChatTransformer()

    transformed = transformer.transform_request(
        {
            "_polar_model_served": "Qwen/Qwen3.5-4B",
            "model": "client-visible-model",
            "messages": [
                {"role": "developer", "content": "Use short answers."},
                {"role": "system", "content": "Be precise."},
                {"role": "user", "content": "2+2?"},
            ],
            "chat_template_kwargs": {"foo": "bar"},
        }
    )

    assert "_polar_model_served" not in transformed
    assert transformed["logprobs"] is True
    assert transformed["chat_template_kwargs"] == {"foo": "bar", "enable_thinking": False}
    assert transformed["messages"] == [
        {"role": "system", "content": "Use short answers.\n\nBe precise."},
        {"role": "user", "content": "2+2?"},
    ]


def test_non_qwen_chat_request_keeps_roles_but_adds_logprobs() -> None:
    transformer = OpenAIChatTransformer()

    transformed = transformer.transform_request(
        {
            "_polar_model_served": "meta/llama",
            "messages": [{"role": "developer", "content": "Style rule."}],
        }
    )

    assert transformed["messages"] == [{"role": "developer", "content": "Style rule."}]
    assert transformed["logprobs"] is True


def test_chat_response_preserves_original_requested_model() -> None:
    transformer = OpenAIChatTransformer()

    response = transformer.transform_response(
        {"id": "cmpl-1", "model": "served-model"},
        {"model": "requested-model"},
    )
    stream_chunk = transformer.transform_stream_chunk(
        {"id": "cmpl-1", "model": "served-model"},
        {"model": "requested-model"},
    )

    assert response["model"] == "requested-model"
    assert stream_chunk["model"] == "requested-model"
