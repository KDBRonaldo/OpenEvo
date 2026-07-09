#!/usr/bin/env python3
"""Minimal Qwen chat-template LoRA SFT trainer for parametric-memory experiments.

The script intentionally lives outside the package runtime. It is an optional
experiment helper used by `parametric_memory_lora_sft` jobs whose trainer args
point at this file.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train-file", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--model-name", default="Qwen/Qwen3.6-35B-A3B")
    parser.add_argument("--max-length", type=int, default=1024)
    parser.add_argument("--max-records", type=int, default=0)
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--max-steps", type=int, default=0)
    parser.add_argument("--lr", type=float, default=2e-4)
    parser.add_argument("--lora-r", type=int, default=4)
    parser.add_argument("--lora-alpha", type=int, default=16)
    parser.add_argument("--lora-dropout", type=float, default=0.0)
    parser.add_argument(
        "--target-modules",
        default="q_proj,k_proj,v_proj,o_proj",
        help="Comma-separated LoRA target module names.",
    )
    return parser.parse_args(argv)


def read_records(path: Path, max_records: int = 0) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if isinstance(record, dict) and isinstance(record.get("messages"), list):
            records.append(record)
        if max_records > 0 and len(records) >= max_records:
            break
    if not records:
        raise ValueError(f"no training records found in {path}")
    return records


def message_content(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return ""


def record_tools(record: dict[str, Any]) -> list[dict[str, Any]] | None:
    tools = record.get("tools")
    if isinstance(tools, list):
        return [tool for tool in tools if isinstance(tool, dict)]
    return None


def prompt_prefix_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    last_assistant = -1
    for index, message in enumerate(messages):
        if message.get("role") == "assistant":
            last_assistant = index
    if last_assistant <= 0:
        return []
    return messages[:last_assistant]


def fallback_chat_text(
    messages: list[dict[str, Any]],
    *,
    add_generation_prompt: bool = False,
) -> str:
    rendered: list[str] = []
    for message in messages:
        role = str(message.get("role") or "user")
        content = message_content(message)
        tool_calls = message.get("tool_calls")
        if content:
            rendered.append(f"<|im_start|>{role}\n{content}<|im_end|>")
        elif isinstance(tool_calls, list) and tool_calls:
            rendered.append(
                "<|im_start|>assistant\n"
                + json.dumps(tool_calls, ensure_ascii=False, sort_keys=True)
                + "<|im_end|>"
            )
    if add_generation_prompt:
        rendered.append("<|im_start|>assistant\n")
    return "\n".join(rendered) + "\n"


def render_chat_texts(tokenizer: Any, record: dict[str, Any]) -> tuple[str, str]:
    messages = record["messages"]
    tools = record_tools(record)
    full_text = _apply_chat_template(
        tokenizer,
        messages,
        tools=tools,
        add_generation_prompt=False,
    )
    prefix_messages = prompt_prefix_messages(messages)
    if not prefix_messages:
        return full_text, ""
    prefix_text = _apply_chat_template(
        tokenizer,
        prefix_messages,
        tools=tools,
        add_generation_prompt=True,
    )
    return full_text, prefix_text


def build_token_id_lists(
    tokenizer: Any,
    *,
    prefix_text: str,
    full_text: str,
    max_length: int,
) -> dict[str, list[int]]:
    if prefix_text and full_text.startswith(prefix_text):
        prefix_ids = _tokenize_ids(tokenizer, prefix_text, truncation=False)
        suffix_ids = _tokenize_ids(
            tokenizer,
            full_text[len(prefix_text) :],
            truncation=False,
        )
        input_ids = [*prefix_ids, *suffix_ids]
        labels = [-100] * len(prefix_ids) + list(suffix_ids)
    else:
        input_ids = _tokenize_ids(
            tokenizer,
            full_text,
            truncation=True,
            max_length=max_length,
        )
        labels = list(input_ids)
        if prefix_text:
            prefix_ids = _tokenize_ids(
                tokenizer,
                prefix_text,
                truncation=True,
                max_length=max_length,
            )
            prompt_len = min(len(prefix_ids), len(labels))
            labels[:prompt_len] = [-100] * prompt_len

    if len(input_ids) > max_length:
        overflow = len(input_ids) - max_length
        input_ids = input_ids[overflow:]
        labels = labels[overflow:]
    if labels and all(label == -100 for label in labels):
        labels[-1] = input_ids[-1]
    return {
        "input_ids": input_ids,
        "attention_mask": [1] * len(input_ids),
        "labels": labels,
    }


def encode_record(
    tokenizer: Any,
    record: dict[str, Any],
    *,
    max_length: int,
    torch_module: Any,
) -> dict[str, Any]:
    full_text, prefix_text = render_chat_texts(tokenizer, record)
    tokenized = build_token_id_lists(
        tokenizer,
        prefix_text=prefix_text,
        full_text=full_text,
        max_length=max_length,
    )
    return {
        key: torch_module.tensor(value, dtype=torch_module.long)
        for key, value in tokenized.items()
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.max_length <= 0:
        raise SystemExit("--max-length must be positive")
    if args.max_records < 0:
        raise SystemExit("--max-records cannot be negative")
    if args.epochs <= 0:
        raise SystemExit("--epochs must be positive")
    if args.max_steps < 0:
        raise SystemExit("--max-steps cannot be negative")

    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import AutoModelForCausalLM, AutoTokenizer

    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    train_file = Path(args.train_file)
    raw_records = read_records(train_file, args.max_records)
    tokenizer = AutoTokenizer.from_pretrained(
        args.model_name,
        trust_remote_code=True,
        use_fast=True,
    )
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    encoded_records = [
        encode_record(
            tokenizer,
            record,
            max_length=args.max_length,
            torch_module=torch,
        )
        for record in raw_records
    ]

    model = AutoModelForCausalLM.from_pretrained(
        args.model_name,
        torch_dtype=torch.bfloat16,
        device_map="auto",
        trust_remote_code=True,
        low_cpu_mem_usage=True,
    )
    model.config.use_cache = False
    if hasattr(model, "gradient_checkpointing_enable"):
        model.gradient_checkpointing_enable()
    model = get_peft_model(
        model,
        LoraConfig(
            r=args.lora_r,
            lora_alpha=args.lora_alpha,
            lora_dropout=args.lora_dropout,
            bias="none",
            task_type="CAUSAL_LM",
            target_modules=_target_modules(args.target_modules),
        ),
    )
    model.train()

    optimizer = torch.optim.AdamW(
        (parameter for parameter in model.parameters() if parameter.requires_grad),
        lr=args.lr,
    )
    first_device = next(model.parameters()).device
    losses: list[float] = []
    step = 0
    epoch = 0
    while True:
        epoch += 1
        for batch in encoded_records:
            if args.max_steps > 0 and step >= args.max_steps:
                break
            tensor_batch = {
                key: value.unsqueeze(0).to(first_device)
                for key, value in batch.items()
            }
            optimizer.zero_grad(set_to_none=True)
            output = model(**tensor_batch)
            loss = output.loss
            loss.backward()
            optimizer.step()
            losses.append(float(loss.detach().cpu()))
            step += 1
        if args.max_steps > 0:
            if step >= args.max_steps:
                break
            continue
        if epoch >= args.epochs:
            break

    model.save_pretrained(output_dir)
    tokenizer.save_pretrained(output_dir)
    diagnostics = {
        "model_name": args.model_name,
        "train_file": str(train_file),
        "record_count": len(raw_records),
        "trained_steps": step,
        "max_length": args.max_length,
        "lora_r": args.lora_r,
        "lora_alpha": args.lora_alpha,
        "lora_dropout": args.lora_dropout,
        "target_modules": _target_modules(args.target_modules),
        "losses": losses,
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
    }
    (output_dir / "trainer_diagnostics.json").write_text(
        json.dumps(diagnostics, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


def _apply_chat_template(
    tokenizer: Any,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None,
    add_generation_prompt: bool,
) -> str:
    try:
        kwargs: dict[str, Any] = {
            "tokenize": False,
            "add_generation_prompt": add_generation_prompt,
        }
        if tools:
            kwargs["tools"] = tools
        return tokenizer.apply_chat_template(messages, **kwargs)
    except Exception:
        return fallback_chat_text(
            messages,
            add_generation_prompt=add_generation_prompt,
        )


def _tokenize_ids(
    tokenizer: Any,
    text: str,
    *,
    truncation: bool,
    max_length: int | None = None,
) -> list[int]:
    kwargs: dict[str, Any] = {
        "add_special_tokens": False,
        "truncation": truncation,
    }
    if max_length is not None:
        kwargs["max_length"] = max_length
    encoded = tokenizer(text, **kwargs)
    input_ids = encoded["input_ids"]
    if input_ids and isinstance(input_ids[0], list):
        input_ids = input_ids[0]
    return [int(token_id) for token_id in input_ids]


def _target_modules(value: str) -> list[str]:
    modules = [item.strip() for item in value.split(",") if item.strip()]
    if not modules:
        raise SystemExit("--target-modules must include at least one module")
    return modules


if __name__ == "__main__":
    raise SystemExit(main())
