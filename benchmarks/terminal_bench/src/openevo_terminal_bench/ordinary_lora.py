"""Fixed ordinary sequential-LoRA control for continual-memory benchmarks."""

from __future__ import annotations

from dataclasses import dataclass
import gc
import math
import os
from pathlib import Path
import random
import time
from typing import Any

from openevo.evolution.parametric.contracts import SdLoraDType, SdLoraMethodConfig
from openevo.evolution.parametric.sd_lora_trainer import (
    _batches,
    _collate,
    _encode_example,
    _initialize_cuda_runtime,
)
from openevo.evolution.parametric.training_data import normalize_training_example


@dataclass(frozen=True)
class OrdinaryLoraTrainingResult:
    adapter_path: Path
    steps_completed: int
    training_loss: float
    training_time_seconds: float
    gpu_peak_memory_bytes: int
    training_record_count: int


def train_ordinary_sequential_lora(
    *,
    config: SdLoraMethodConfig,
    examples: list[dict[str, Any]],
    output_dir: Path,
    prior_adapter: Path | None,
) -> OrdinaryLoraTrainingResult:
    """Continue one ordinary PEFT LoRA in place across the task stream."""

    try:
        import torch
        from peft import LoraConfig, PeftModel, get_peft_model
        from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
    except ImportError as exc:
        raise RuntimeError(
            "ordinary-LoRA control requires torch, transformers, peft, and accelerate"
        ) from exc
    if not examples:
        raise ValueError("ordinary-LoRA control requires at least one training example")
    if output_dir.exists() or output_dir.is_symlink():
        raise ValueError("ordinary-LoRA output directory already exists")

    started = time.monotonic()
    _initialize_cuda_runtime(torch, component_name="ordinary-LoRA control")
    torch.manual_seed(config.seed)
    torch.cuda.manual_seed_all(config.seed)
    random.seed(config.seed)
    dtype_by_name = {
        SdLoraDType.BFLOAT16: torch.bfloat16,
        SdLoraDType.FLOAT16: torch.float16,
        SdLoraDType.FLOAT32: torch.float32,
    }
    model_dtype = dtype_by_name[config.dtype]
    model_kwargs: dict[str, Any] = {
        "revision": config.model_revision,
        "torch_dtype": model_dtype,
        "trust_remote_code": False,
    }
    if config.load_in_4bit:
        model_kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_compute_dtype=model_dtype,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
        )
        model_kwargs["device_map"] = {"": 0}
    model = AutoModelForCausalLM.from_pretrained(config.base_model, **model_kwargs)
    try:
        if not config.load_in_4bit:
            model.to(torch.device("cuda", 0))
        tokenizer = AutoTokenizer.from_pretrained(
            config.base_model,
            revision=config.model_revision,
            trust_remote_code=False,
        )
        if tokenizer.pad_token_id is None:
            if tokenizer.eos_token_id is None:
                raise ValueError("ordinary-LoRA tokenizer requires an EOS or padding token")
            tokenizer.pad_token = tokenizer.eos_token
        normalized = [normalize_training_example(example) for example in examples]
        original_truncation_side = tokenizer.truncation_side
        tokenizer.truncation_side = "left"
        try:
            encoded = [
                _encode_example(tokenizer, example, config.max_length)
                for example in normalized
            ]
        finally:
            tokenizer.truncation_side = original_truncation_side

        if prior_adapter is None:
            model = get_peft_model(
                model,
                LoraConfig(
                    r=config.rank,
                    lora_alpha=config.rank,
                    lora_dropout=0.0,
                    bias="none",
                    task_type="CAUSAL_LM",
                    target_modules=list(config.target_modules),
                ),
            )
        else:
            model = PeftModel.from_pretrained(
                model,
                prior_adapter,
                is_trainable=True,
            )
        if config.gradient_checkpointing:
            model.gradient_checkpointing_enable()
            if hasattr(model, "enable_input_require_grads"):
                model.enable_input_require_grads()
        if hasattr(model.config, "use_cache"):
            model.config.use_cache = False
        trainable = [parameter for parameter in model.parameters() if parameter.requires_grad]
        if not trainable:
            raise RuntimeError("ordinary-LoRA control found no trainable adapter parameters")
        optimizer = torch.optim.AdamW(
            trainable,
            lr=config.learning_rate,
            weight_decay=config.weight_decay,
        )
        scaler = torch.amp.GradScaler(
            "cuda",
            enabled=config.dtype is SdLoraDType.FLOAT16,
        )
        optimizer.zero_grad(set_to_none=True)
        model.train()
        device = next(model.parameters()).device
        losses: list[float] = []
        steps_completed = 0
        micro_steps = 0
        pending_gradient = False
        for batch in _batches(
            encoded,
            batch_size=config.per_device_train_batch_size,
            seed=config.seed,
            epochs=config.epochs,
        ):
            tensors = {
                key: value.to(device, non_blocking=True)
                for key, value in _collate(
                    torch,
                    batch,
                    int(tokenizer.pad_token_id),
                ).items()
            }
            with torch.autocast(
                device_type="cuda",
                dtype=model_dtype,
                enabled=config.dtype is not SdLoraDType.FLOAT32,
            ):
                loss = model(**tensors).loss
                scaled_loss = loss / config.gradient_accumulation_steps
            if not bool(torch.isfinite(loss)):
                raise ValueError("ordinary-LoRA control produced a non-finite loss")
            scaler.scale(scaled_loss).backward()
            losses.append(float(loss.detach().float().cpu()))
            micro_steps += 1
            pending_gradient = True
            if micro_steps % config.gradient_accumulation_steps != 0:
                continue
            scaler.unscale_(optimizer)
            torch.nn.utils.clip_grad_norm_(trainable, config.max_grad_norm)
            scaler.step(optimizer)
            scaler.update()
            optimizer.zero_grad(set_to_none=True)
            pending_gradient = False
            steps_completed += 1
            if config.max_steps is not None and steps_completed >= config.max_steps:
                break
        if pending_gradient and (
            config.max_steps is None or steps_completed < config.max_steps
        ):
            scaler.unscale_(optimizer)
            torch.nn.utils.clip_grad_norm_(trainable, config.max_grad_norm)
            scaler.step(optimizer)
            scaler.update()
            optimizer.zero_grad(set_to_none=True)
            steps_completed += 1
        if steps_completed < 1 or not losses:
            raise ValueError("ordinary-LoRA control completed no optimizer steps")

        output_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        model.save_pretrained(output_dir, safe_serialization=True)
        for directory, _, filenames in os.walk(output_dir):
            os.chmod(directory, 0o700, follow_symlinks=False)
            for filename in filenames:
                os.chmod(Path(directory) / filename, 0o600, follow_symlinks=False)
        elapsed = time.monotonic() - started
        peak = int(torch.cuda.max_memory_allocated())
        loss_value = sum(losses) / len(losses)
        if not math.isfinite(elapsed) or elapsed <= 0 or peak < 1:
            raise RuntimeError("ordinary-LoRA control produced invalid resource accounting")
        return OrdinaryLoraTrainingResult(
            adapter_path=output_dir,
            steps_completed=steps_completed,
            training_loss=loss_value,
            training_time_seconds=elapsed,
            gpu_peak_memory_bytes=peak,
            training_record_count=len(normalized),
        )
    finally:
        del model
        gc.collect()
        torch.cuda.empty_cache()


__all__ = ["OrdinaryLoraTrainingResult", "train_ordinary_sequential_lora"]
