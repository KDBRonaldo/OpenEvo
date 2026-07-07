from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any


def _load_trainer_module() -> Any:
    path = Path(__file__).resolve().parents[2] / "scripts" / "qwen_lora_sft.py"
    spec = importlib.util.spec_from_file_location("qwen_lora_sft", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _FakeTokenizer:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def apply_chat_template(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        self.calls.append({"messages": messages, **kwargs})
        rendered = "|".join(message["role"] for message in messages)
        if kwargs.get("add_generation_prompt"):
            return f"{rendered}|assistant:"
        return f"{rendered}:tool-call"

    def __call__(self, text: str, **_kwargs: Any) -> dict[str, list[int]]:
        return {"input_ids": [ord(char) for char in text]}


def test_qwen_lora_sft_passes_tools_to_full_and_prefix_templates() -> None:
    trainer = _load_trainer_module()
    tokenizer = _FakeTokenizer()
    tools = [{"type": "function", "function": {"name": "tb_exec"}}]
    record = {
        "messages": [
            {"role": "system", "content": "Use tools."},
            {"role": "user", "content": "Solve."},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call"}]},
        ],
        "tools": tools,
    }

    full_text, prefix_text = trainer.render_chat_texts(tokenizer, record)

    assert full_text == "system|user|assistant:tool-call"
    assert prefix_text == "system|user|assistant:"
    assert tokenizer.calls[0]["tools"] == tools
    assert tokenizer.calls[1]["tools"] == tools


def test_qwen_lora_sft_labels_first_suffix_token_after_prefix() -> None:
    trainer = _load_trainer_module()
    tokenizer = _FakeTokenizer()

    tokenized = trainer.build_token_id_lists(
        tokenizer,
        prefix_text="prompt\n",
        full_text="prompt\n\n<tool_call>",
        max_length=128,
    )

    assert tokenized["labels"][: len("prompt\n")] == [-100] * len("prompt\n")
    assert tokenized["labels"][len("prompt\n")] == ord("\n")
    assert tokenized["input_ids"][len("prompt\n")] == ord("\n")
