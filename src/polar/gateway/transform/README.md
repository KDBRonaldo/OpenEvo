# API Transforms

`polar.gateway.transform` keeps agent-facing APIs stable while adding the fields
needed for trainable SGLang completions.

## Main Files

- `base.py`: common transformer interface and training request enhancement.
- `openai_chat.py`: OpenAI Chat Completions passthrough and response repair.
- `openai_responses.py`: OpenAI Responses conversion and streaming events.
- `anthropic.py`: Anthropic-style request and response conversion.
- `google.py`: Google-style request and response conversion.
- `__init__.py`: transformer registry by detected API type.

## Responsibilities

Request transforms:

- Preserve the user-requested model for agent compatibility.
- Forward to the served model expected by the gateway when needed.
- Add training fields such as logprobs.
- Normalize API-specific message shapes before proxying.

Response transforms:

- Return a shape the agent harness expects.
- Preserve tool calls, text chunks, finish reasons, and streaming events.
- Keep original requested model names where clients depend on them.

## Streaming

Streaming transforms operate chunk by chunk. They must preserve event ordering
and emit terminal events that client SDKs expect.
