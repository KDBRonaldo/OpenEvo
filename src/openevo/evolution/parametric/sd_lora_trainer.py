"""Trusted local trainer for the OpenEvo SD-LoRA continual-memory method."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import random
import resource
import signal
import stat
import sys
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from openevo.evolution.framework.contracts import canonical_json, validate_relative_path

from .contracts import (
    MAX_SD_LORA_COMPONENTS,
    MAX_SD_LORA_EFFECTIVE_RANK,
    SD_LORA_STATE_MANIFEST,
    SD_LORA_STATE_WEIGHTS,
    SdLoraDType,
    SdLoraStateComponent,
    SdLoraStateManifest,
    SdLoraStateModule,
    SdLoraTrainingRequest,
    SdLoraTrainingResult,
)
from .training_data import (
    MAX_TRAINING_FILE_BYTES,
    MAX_TRAINING_LINE_BYTES,
    normalize_training_example,
)


_MAX_REQUEST_BYTES = 1024 * 1024
_STATE_KEY_PREFIX = "components"
_PARENT_PID_ENV = "OPENEVO_SD_LORA_PARENT_PID"
_PR_SET_PDEATHSIG = 1
_MAX_OUTPUT_FILE_BYTES = 16 * 1024 * 1024 * 1024
_MAX_OPEN_FILES = 1024


def _install_parent_death_signal() -> None:
    raw_parent = os.environ.pop(_PARENT_PID_ENV, None)
    if raw_parent is None or not raw_parent.isdecimal() or int(raw_parent) <= 1:
        raise RuntimeError("SD-LoRA trainer parent identity is unavailable")
    if not sys.platform.startswith("linux"):
        raise RuntimeError("SD-LoRA trainer requires Linux parent-death signaling")
    expected_parent = int(raw_parent)
    if os.getppid() != expected_parent:
        raise RuntimeError("SD-LoRA trainer parent changed before startup")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    if os.getppid() != expected_parent:
        os.kill(os.getpid(), signal.SIGKILL)
        raise RuntimeError("SD-LoRA trainer parent changed during startup")


def _apply_resource_limits(timeout_seconds: float) -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    file_hard = resource.getrlimit(resource.RLIMIT_FSIZE)[1]
    file_limit = (
        _MAX_OUTPUT_FILE_BYTES
        if file_hard == resource.RLIM_INFINITY
        else min(_MAX_OUTPUT_FILE_BYTES, file_hard)
    )
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_limit, file_limit))
    nofile_hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    nofile_limit = (
        _MAX_OPEN_FILES
        if nofile_hard == resource.RLIM_INFINITY
        else min(_MAX_OPEN_FILES, nofile_hard)
    )
    resource.setrlimit(resource.RLIMIT_NOFILE, (nofile_limit, nofile_limit))
    cpu_hard = resource.getrlimit(resource.RLIMIT_CPU)[1]
    requested_cpu = max(1, math.ceil(timeout_seconds) + 60)
    cpu_limit = (
        requested_cpu
        if cpu_hard == resource.RLIM_INFINITY
        else min(requested_cpu, cpu_hard)
    )
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit))


def _stable_file_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_uid,
        value.st_gid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _read_owned_file(path: Path, *, maximum_bytes: int) -> bytes:
    directory_fd = os.open(
        path.parent,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
    )
    try:
        fd = os.open(
            path.name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or before.st_nlink != 1
                or before.st_size < 1
                or before.st_size > maximum_bytes
            ):
                raise ValueError("trainer input must be a bounded owned regular file")
            remaining = before.st_size
            chunks: list[bytes] = []
            while remaining:
                chunk = os.read(fd, min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("trainer input ended before its observed size")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(fd, 1):
                raise ValueError("trainer input grew while being read")
            after = os.fstat(fd)
            if _stable_file_identity(after) != _stable_file_identity(before):
                raise ValueError("trainer input identity changed while being read")
            return b"".join(chunks)
        finally:
            os.close(fd)
    finally:
        os.close(directory_fd)


def _write_private_json(path: Path, value: Any) -> None:
    payload = (canonical_json(value) + "\n").encode("utf-8")
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        pending = memoryview(payload)
        while pending:
            written = os.write(fd, pending)
            if written <= 0:
                raise OSError("trainer output write made no progress")
            pending = pending[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _sha256_file(path: Path) -> tuple[int, str]:
    directory_fd = os.open(
        path.parent,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
    )
    try:
        fd = os.open(
            path.name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or before.st_nlink != 1
                or before.st_size < 1
                or before.st_size > 16 * 1024 * 1024 * 1024
            ):
                raise ValueError("state weights must be a bounded owned regular file")
            digest = hashlib.sha256()
            observed_size = 0
            while chunk := os.read(fd, 1024 * 1024):
                observed_size += len(chunk)
                if observed_size > before.st_size:
                    raise ValueError("state weights grew while being hashed")
                digest.update(chunk)
            after = os.fstat(fd)
            if observed_size != before.st_size or _stable_file_identity(
                after
            ) != _stable_file_identity(before):
                raise ValueError("state weights changed while being hashed")
            return observed_size, digest.hexdigest()
        finally:
            os.close(fd)
    finally:
        os.close(directory_fd)


def _dependencies() -> tuple[Any, Any, Any, Any, Any, Any, Any]:
    try:
        import torch
        from peft import LoraConfig, get_peft_model
        from safetensors.torch import load_file, save_file
        from transformers import (
            AutoModelForCausalLM,
            AutoTokenizer,
            BitsAndBytesConfig,
        )
    except ImportError as exc:
        raise RuntimeError(
            "SD-LoRA training requires the OpenEvo parametric-memory Daemon profile "
            "(torch, transformers, peft, safetensors, and accelerate)"
        ) from exc
    return (
        torch,
        LoraConfig,
        get_peft_model,
        load_file,
        save_file,
        AutoModelForCausalLM,
        (AutoTokenizer, BitsAndBytesConfig),
    )


def _state_key(task_index: int, module_name: str, matrix: str) -> str:
    return f"{_STATE_KEY_PREFIX}.{task_index}.{module_name}.{matrix}"


def _read_examples(path: Path, *, expected_count: int) -> list[dict[str, Any]]:
    payload = _read_owned_file(path, maximum_bytes=MAX_TRAINING_FILE_BYTES)
    examples: list[dict[str, Any]] = []
    for raw_line in payload.splitlines():
        if not raw_line.strip():
            continue
        if len(raw_line) > MAX_TRAINING_LINE_BYTES:
            raise ValueError("SD-LoRA training example exceeds the line budget")
        try:
            value = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("SD-LoRA training data is not valid UTF-8 JSONL") from exc
        examples.append(normalize_training_example(value))
    if len(examples) != expected_count:
        raise ValueError("SD-LoRA training record count differs from the request")
    return examples


def _target_module_names(model: Any, suffixes: Sequence[str]) -> tuple[str, ...]:
    names: list[str] = []
    for name, module in model.named_modules():
        if not name or not any(
            name == suffix or name.endswith("." + suffix) for suffix in suffixes
        ):
            continue
        in_features = getattr(module, "in_features", None)
        out_features = getattr(module, "out_features", None)
        if (
            isinstance(in_features, int)
            and not isinstance(in_features, bool)
            and isinstance(out_features, int)
            and not isinstance(out_features, bool)
            and in_features > 0
            and out_features > 0
        ):
            names.append(name)
    if not names:
        raise ValueError("SD-LoRA target_modules matched no supported linear modules")
    if len(names) > 4096:
        raise ValueError("SD-LoRA target_modules matched too many modules")
    return tuple(sorted(names))


def _load_prior_state(
    request: SdLoraTrainingRequest,
    prior_dir: Path,
    *,
    torch: Any,
    load_file: Any,
) -> tuple[SdLoraStateManifest, list[dict[str, tuple[Any, Any]]]]:
    manifest_path = prior_dir / SD_LORA_STATE_MANIFEST
    weights_path = prior_dir / SD_LORA_STATE_WEIGHTS
    manifest_bytes = _read_owned_file(manifest_path, maximum_bytes=_MAX_REQUEST_BYTES)
    try:
        manifest = SdLoraStateManifest.model_validate_json(manifest_bytes)
    except ValueError as exc:
        raise ValueError("prior SD-LoRA state manifest is invalid") from exc
    if manifest_bytes != (canonical_json(manifest.model_dump(mode="json")) + "\n").encode("utf-8"):
        raise ValueError("prior SD-LoRA state manifest bytes are not canonical")
    if manifest.base_model != request.config.base_model:
        raise ValueError("prior SD-LoRA state targets a different base model")
    if manifest.model_revision != request.config.model_revision:
        raise ValueError("prior SD-LoRA state targets a different model revision")
    if manifest.target_module_suffixes != request.config.target_modules:
        raise ValueError("prior SD-LoRA state uses different target modules")
    if manifest.component_count >= MAX_SD_LORA_COMPONENTS:
        raise ValueError("prior SD-LoRA state reached the component limit")
    if any(component.rank != request.config.rank for component in manifest.components):
        raise ValueError("SD-LoRA component rank cannot change within one continual stream")
    observed_size, observed_digest = _sha256_file(weights_path)
    if (
        observed_size != manifest.state_weights_size_bytes
        or observed_digest != manifest.state_weights_sha256
    ):
        raise ValueError("prior SD-LoRA state weights do not match their manifest")
    tensors = load_file(str(weights_path), device="cpu")
    expected_keys = {
        _state_key(component.task_index, module.name, matrix)
        for component in manifest.components
        for module in manifest.modules
        for matrix in ("A", "B")
    }
    if set(tensors) != expected_keys:
        raise ValueError("prior SD-LoRA state tensor inventory is not exact")
    components: list[dict[str, tuple[Any, Any]]] = []
    for component in manifest.components:
        values: dict[str, tuple[Any, Any]] = {}
        for module in manifest.modules:
            matrix_a = tensors[_state_key(component.task_index, module.name, "A")]
            matrix_b = tensors[_state_key(component.task_index, module.name, "B")]
            if tuple(matrix_a.shape) != (component.rank, module.in_features) or tuple(
                matrix_b.shape
            ) != (module.out_features, component.rank):
                raise ValueError("prior SD-LoRA state tensor shape is invalid")
            if not bool(torch.isfinite(matrix_a).all()) or not bool(
                torch.isfinite(matrix_b).all()
            ):
                raise ValueError("prior SD-LoRA state contains non-finite tensors")
            values[module.name] = (matrix_a.float(), matrix_b.float())
        components.append(values)
    return manifest, components


def _sd_lora_layer_class(torch: Any) -> type[Any]:
    class SdLoraLinear(torch.nn.Module):
        def __init__(
            self,
            base_layer: Any,
            prior: Sequence[tuple[Any, Any]],
            coefficients: Any,
            *,
            rank: int,
            dtype: Any,
        ) -> None:
            super().__init__()
            self.base_layer = base_layer
            self.prior_a = torch.nn.ParameterList(
                [torch.nn.Parameter(a, requires_grad=False) for a, _ in prior]
            )
            self.prior_b = torch.nn.ParameterList(
                [torch.nn.Parameter(b, requires_grad=False) for _, b in prior]
            )
            object.__setattr__(self, "_shared_coefficients", coefficients)
            self.current_a = torch.nn.Parameter(
                torch.empty(
                    rank,
                    int(base_layer.in_features),
                    device=coefficients.device,
                    dtype=dtype,
                )
            )
            self.current_b = torch.nn.Parameter(
                torch.empty(
                    int(base_layer.out_features),
                    rank,
                    device=coefficients.device,
                    dtype=dtype,
                )
            )
            torch.nn.init.kaiming_uniform_(self.current_a, a=math.sqrt(5))
            torch.nn.init.zeros_(self.current_b)

        def forward(self, inputs: Any) -> Any:
            output = self.base_layer(inputs)
            delta = torch.zeros_like(output)
            coefficients = object.__getattribute__(self, "_shared_coefficients")
            for index, (matrix_a, matrix_b) in enumerate(
                zip(self.prior_a, self.prior_b, strict=True)
            ):
                denominator = (matrix_a.float().norm() * matrix_b.float().norm()).clamp_min(
                    torch.finfo(torch.float32).eps
                )
                projected = torch.nn.functional.linear(
                    torch.nn.functional.linear(inputs, matrix_a),
                    matrix_b,
                )
                delta = delta + projected * (
                    coefficients[index].to(projected.dtype) / denominator.to(projected.dtype)
                )
            current = torch.nn.functional.linear(
                torch.nn.functional.linear(inputs, self.current_a),
                self.current_b,
            )
            delta = delta + current * coefficients[-1].to(current.dtype)
            return output + delta.to(output.dtype)

    return SdLoraLinear


def _set_submodule(model: Any, name: str, replacement: Any) -> None:
    if "." in name:
        parent_name, child_name = name.rsplit(".", 1)
        parent = model.get_submodule(parent_name)
    else:
        parent = model
        child_name = name
    setattr(parent, child_name, replacement)


def _encode_fallback(
    tokenizer: Any,
    messages: Sequence[dict[str, Any]],
    maximum: int,
) -> dict[str, list[int]]:
    input_ids: list[int] = []
    labels: list[int] = []
    bos_token_id = getattr(tokenizer, "bos_token_id", None)
    if isinstance(bos_token_id, int):
        input_ids.append(bos_token_id)
        labels.append(-100)
    for message in messages:
        role = message["role"]
        if message.get("name"):
            role += f":{message['name']}"
        if message.get("tool_call_id"):
            role += f":{message['tool_call_id']}"
        content = message["content"]
        if message.get("tool_calls"):
            tool_calls = canonical_json({"tool_calls": message["tool_calls"]})
            content = f"{content}\n{tool_calls}" if content else tool_calls
        rendered = f"<|{role}|>\n{content}\n"
        segment = tokenizer.encode(rendered, add_special_tokens=False)
        input_ids.extend(segment)
        labels.extend(segment if message["role"] == "assistant" else [-100] * len(segment))
    eos_token_id = getattr(tokenizer, "eos_token_id", None)
    if isinstance(eos_token_id, int) and len(input_ids) < maximum:
        input_ids.append(eos_token_id)
        labels.append(eos_token_id if messages[-1]["role"] == "assistant" else -100)
    input_ids = input_ids[-maximum:]
    labels = labels[-maximum:]
    if not any(label != -100 for label in labels):
        raise ValueError("SD-LoRA encoded example has no assistant loss tokens")
    return {"input_ids": input_ids, "labels": labels}


def _chat_template_has_generation_block(template: Any) -> bool:
    if isinstance(template, str):
        return "{% generation" in template
    if isinstance(template, dict):
        return any(
            isinstance(value, str) and "{% generation" in value for value in template.values()
        )
    return False


def _encode_chat_template_with_offsets(
    tokenizer: Any,
    example: dict[str, Any],
    maximum: int,
) -> dict[str, list[int]]:
    messages = example["messages"]
    template_kwargs: dict[str, Any] = {}
    if "tools" in example:
        template_kwargs["tools"] = example["tools"]
    rendered = tokenizer.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=False,
        **template_kwargs,
    )
    if not isinstance(rendered, str) or not rendered:
        raise ValueError("chat template did not render bounded training text")

    assistant_spans: list[tuple[int, int]] = []
    for index, message in enumerate(messages):
        if message["role"] != "assistant":
            continue
        target_prefix = tokenizer.apply_chat_template(
            messages[:index],
            tokenize=False,
            add_generation_prompt=True,
            **template_kwargs,
        )
        completed_prefix = tokenizer.apply_chat_template(
            messages[: index + 1],
            tokenize=False,
            add_generation_prompt=False,
            **template_kwargs,
        )
        if (
            not isinstance(target_prefix, str)
            or not isinstance(completed_prefix, str)
            or not completed_prefix.startswith(target_prefix)
            or not rendered.startswith(completed_prefix)
            or len(completed_prefix) <= len(target_prefix)
        ):
            raise ValueError("chat template assistant boundaries are not prefix-stable")
        assistant_spans.append((len(target_prefix), len(completed_prefix)))
    if not assistant_spans:
        raise ValueError("chat template example has no assistant span")

    encoded = tokenizer(
        rendered,
        add_special_tokens=False,
        truncation=True,
        max_length=maximum,
        return_offsets_mapping=True,
    )
    input_ids = list(encoded["input_ids"])
    offsets = list(encoded["offset_mapping"])
    if len(input_ids) != len(offsets):
        raise ValueError("chat template tokenizer returned an invalid offset mapping")
    labels = [
        token_id
        if any(end > span_start and start < span_end for span_start, span_end in assistant_spans)
        else -100
        for token_id, (start, end) in zip(input_ids, offsets, strict=True)
    ]
    if not any(label != -100 for label in labels):
        raise ValueError("SD-LoRA truncation removed every assistant loss token")
    return {"input_ids": input_ids, "labels": labels}


def _encode_example(tokenizer: Any, example: dict[str, Any], maximum: int) -> dict[str, list[int]]:
    messages = example["messages"]
    chat_template = getattr(tokenizer, "chat_template", None)
    if chat_template and _chat_template_has_generation_block(chat_template):
        kwargs: dict[str, Any] = {
            "tokenize": True,
            "add_generation_prompt": False,
            "return_assistant_tokens_mask": True,
            "return_dict": True,
            "truncation": True,
            "max_length": maximum,
        }
        if "tools" in example:
            kwargs["tools"] = example["tools"]
        try:
            encoded = tokenizer.apply_chat_template(messages, **kwargs)
            input_ids = list(encoded["input_ids"])
            mask = encoded.get("assistant_masks") or encoded.get("assistant_mask")
            if (
                isinstance(mask, list)
                and len(mask) == len(input_ids)
                and any(bool(value) for value in mask)
            ):
                return {
                    "input_ids": input_ids,
                    "labels": [
                        token_id if bool(include) else -100
                        for token_id, include in zip(input_ids, mask, strict=True)
                    ],
                }
        except (KeyError, TypeError, ValueError):
            pass
    if chat_template:
        try:
            return _encode_chat_template_with_offsets(tokenizer, example, maximum)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "SD-LoRA could not derive an exact assistant mask from the model chat template"
            ) from exc
    return _encode_fallback(tokenizer, messages, maximum)


def _collate(
    torch: Any, batch: Sequence[dict[str, list[int]]], pad_token_id: int
) -> dict[str, Any]:
    maximum = max(len(item["input_ids"]) for item in batch)
    input_ids: list[list[int]] = []
    labels: list[list[int]] = []
    attention_mask: list[list[int]] = []
    for item in batch:
        padding = maximum - len(item["input_ids"])
        input_ids.append([*item["input_ids"], *([pad_token_id] * padding)])
        labels.append([*item["labels"], *([-100] * padding)])
        attention_mask.append([1] * len(item["input_ids"]) + [0] * padding)
    return {
        "input_ids": torch.tensor(input_ids, dtype=torch.long),
        "labels": torch.tensor(labels, dtype=torch.long),
        "attention_mask": torch.tensor(attention_mask, dtype=torch.long),
    }


def _batches(
    values: Sequence[dict[str, list[int]]],
    *,
    batch_size: int,
    seed: int,
    epochs: int,
) -> Iterable[list[dict[str, list[int]]]]:
    randomizer = random.Random(seed)
    for _ in range(epochs):
        indices = list(range(len(values)))
        randomizer.shuffle(indices)
        for start in range(0, len(indices), batch_size):
            yield [values[index] for index in indices[start : start + batch_size]]


def _compose_cumulative_weights(
    torch: Any,
    components: Sequence[dict[str, tuple[Any, Any]]],
    coefficients: Sequence[float],
    module_names: Sequence[str],
) -> dict[str, tuple[Any, Any]]:
    """Fold SD-LoRA components into one PEFT-compatible low-rank update."""

    if not components or len(components) != len(coefficients):
        raise ValueError("SD-LoRA composition requires one coefficient per component")
    newest = len(components) - 1
    merged: dict[str, tuple[Any, Any]] = {}
    for module_name in module_names:
        matrices_a: list[Any] = []
        matrices_b: list[Any] = []
        for index, component in enumerate(components):
            matrix_a, matrix_b = component[module_name]
            scale = float(coefficients[index])
            if index < newest:
                denominator = float(matrix_a.float().norm() * matrix_b.float().norm())
                if not math.isfinite(denominator) or denominator <= 0.0:
                    raise ValueError("prior SD-LoRA component has a zero or invalid norm")
                scale /= denominator
            matrices_a.append(matrix_a.float().contiguous())
            matrices_b.append((matrix_b.float() * scale).contiguous())
        merged[module_name] = (
            torch.cat(matrices_a, dim=0).contiguous(),
            torch.cat(matrices_b, dim=1).contiguous(),
        )
    return merged


def _initialize_cuda_runtime(
    torch: Any,
    *,
    component_name: str = "SD-LoRA parametric-memory training",
) -> None:
    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise RuntimeError(f"{component_name} requires exactly one visible CUDA device")
    # CUDA 13 may report devices before creating the primary context required
    # by the allocator's peak-memory accounting API.
    torch.cuda.init()
    torch.cuda.reset_peak_memory_stats()


def _train(request: SdLoraTrainingRequest) -> SdLoraTrainingResult:
    training_started = time.monotonic()
    (
        torch,
        LoraConfig,
        get_peft_model,
        load_file,
        save_file,
        AutoModelForCausalLM,
        tokenizer_dependencies,
    ) = _dependencies()
    AutoTokenizer, BitsAndBytesConfig = tokenizer_dependencies
    _initialize_cuda_runtime(torch)
    torch.manual_seed(request.config.seed)
    torch.cuda.manual_seed_all(request.config.seed)
    random.seed(request.config.seed)

    dtype_by_name = {
        SdLoraDType.BFLOAT16: torch.bfloat16,
        SdLoraDType.FLOAT16: torch.float16,
        SdLoraDType.FLOAT32: torch.float32,
    }
    model_dtype = dtype_by_name[request.config.dtype]
    model_kwargs: dict[str, Any] = {
        "revision": request.config.model_revision,
        "trust_remote_code": False,
        "torch_dtype": model_dtype,
    }
    if request.config.load_in_4bit:
        model_kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=model_dtype,
            bnb_4bit_use_double_quant=True,
        )
        model_kwargs["device_map"] = {"": 0}
    model = AutoModelForCausalLM.from_pretrained(request.config.base_model, **model_kwargs)
    if not request.config.load_in_4bit:
        model.to(torch.device("cuda", 0))
    device = next(model.parameters()).device
    tokenizer = AutoTokenizer.from_pretrained(
        request.config.base_model,
        revision=request.config.model_revision,
        trust_remote_code=False,
    )
    if tokenizer.pad_token_id is None:
        if tokenizer.eos_token_id is None:
            raise ValueError("SD-LoRA tokenizer requires an EOS or padding token")
        tokenizer.pad_token = tokenizer.eos_token

    work_dir = Path(request.work_dir)
    examples = _read_examples(
        work_dir / request.training_data_path,
        expected_count=request.training_record_count,
    )
    original_truncation_side = tokenizer.truncation_side
    tokenizer.truncation_side = "left"
    try:
        encoded_examples = [
            _encode_example(tokenizer, example, request.config.max_length) for example in examples
        ]
    finally:
        tokenizer.truncation_side = original_truncation_side
    target_names = _target_module_names(model, request.config.target_modules)
    module_specs = tuple(
        SdLoraStateModule(
            name=name,
            in_features=int(model.get_submodule(name).in_features),
            out_features=int(model.get_submodule(name).out_features),
        )
        for name in target_names
    )

    prior_manifest: SdLoraStateManifest | None = None
    prior_components: list[dict[str, tuple[Any, Any]]] = []
    if request.prior_adapter_path is not None:
        prior_manifest, prior_components = _load_prior_state(
            request,
            work_dir / request.prior_adapter_path,
            torch=torch,
            load_file=load_file,
        )
        if tuple(prior_manifest.modules) != module_specs:
            raise ValueError("prior SD-LoRA state does not match the loaded model modules")
    component_count = len(prior_components) + 1
    effective_rank = component_count * request.config.rank
    if effective_rank > MAX_SD_LORA_EFFECTIVE_RANK:
        raise ValueError("SD-LoRA cumulative adapter exceeds the effective-rank limit")

    for parameter in model.parameters():
        parameter.requires_grad = False
    coefficients = torch.nn.Parameter(
        torch.full(
            (component_count,),
            request.config.coefficient_init,
            dtype=torch.float32,
            device=device,
        )
    )
    layer_class = _sd_lora_layer_class(torch)
    wrappers: dict[str, Any] = {}
    for module_name in target_names:
        base_layer = model.get_submodule(module_name)
        prior = [
            (
                component[module_name][0].to(device=device, dtype=model_dtype),
                component[module_name][1].to(device=device, dtype=model_dtype),
            )
            for component in prior_components
        ]
        wrapper = layer_class(
            base_layer,
            prior,
            coefficients,
            rank=request.config.rank,
            dtype=model_dtype,
        )
        _set_submodule(model, module_name, wrapper)
        wrappers[module_name] = wrapper

    if request.config.gradient_checkpointing:
        model.gradient_checkpointing_enable()
        if hasattr(model, "enable_input_require_grads"):
            model.enable_input_require_grads()
    if hasattr(model.config, "use_cache"):
        model.config.use_cache = False
    trainable = [coefficients]
    for wrapper in wrappers.values():
        trainable.extend((wrapper.current_a, wrapper.current_b))
    optimizer = torch.optim.AdamW(
        trainable,
        lr=request.config.learning_rate,
        weight_decay=request.config.weight_decay,
    )
    scaler = torch.amp.GradScaler(
        "cuda",
        enabled=request.config.dtype is SdLoraDType.FLOAT16,
    )
    optimizer.zero_grad(set_to_none=True)
    model.train()
    steps_completed = 0
    micro_steps = 0
    losses: list[float] = []
    pending_gradient = False
    for batch in _batches(
        encoded_examples,
        batch_size=request.config.per_device_train_batch_size,
        seed=request.config.seed,
        epochs=request.config.epochs,
    ):
        tensors = {
            key: value.to(device, non_blocking=True)
            for key, value in _collate(torch, batch, int(tokenizer.pad_token_id)).items()
        }
        with torch.autocast(
            device_type="cuda",
            dtype=model_dtype,
            enabled=request.config.dtype is not SdLoraDType.FLOAT32,
        ):
            loss = model(**tensors).loss
            scaled_loss = loss / request.config.gradient_accumulation_steps
        if not bool(torch.isfinite(loss)):
            raise ValueError("SD-LoRA training produced a non-finite loss")
        scaler.scale(scaled_loss).backward()
        losses.append(float(loss.detach().float().cpu()))
        micro_steps += 1
        pending_gradient = True
        if micro_steps % request.config.gradient_accumulation_steps != 0:
            continue
        scaler.unscale_(optimizer)
        torch.nn.utils.clip_grad_norm_(trainable, request.config.max_grad_norm)
        scaler.step(optimizer)
        scaler.update()
        optimizer.zero_grad(set_to_none=True)
        pending_gradient = False
        steps_completed += 1
        if request.config.max_steps is not None and steps_completed >= request.config.max_steps:
            break
    if pending_gradient and (
        request.config.max_steps is None or steps_completed < request.config.max_steps
    ):
        scaler.unscale_(optimizer)
        torch.nn.utils.clip_grad_norm_(trainable, request.config.max_grad_norm)
        scaler.step(optimizer)
        scaler.update()
        optimizer.zero_grad(set_to_none=True)
        steps_completed += 1
    if steps_completed < 1 or not losses:
        raise ValueError("SD-LoRA training completed no optimizer steps")
    training_loss = sum(losses) / len(losses)
    coefficient_values = tuple(
        float(value) for value in coefficients.detach().float().cpu().tolist()
    )

    current_component = {
        module_name: (
            wrapper.current_a.detach().float().cpu().contiguous(),
            wrapper.current_b.detach().float().cpu().contiguous(),
        )
        for module_name, wrapper in wrappers.items()
    }
    all_components = [*prior_components, current_component]
    merged = _compose_cumulative_weights(
        torch,
        all_components,
        coefficient_values,
        target_names,
    )
    for module_name, wrapper in wrappers.items():
        _set_submodule(model, module_name, wrapper.base_layer)
    peft_config = LoraConfig(
        r=effective_rank,
        lora_alpha=effective_rank,
        lora_dropout=0.0,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=list(request.config.target_modules),
    )
    peft_model = get_peft_model(model, peft_config)
    for module_name in target_names:
        layer = peft_model.base_model.model.get_submodule(module_name)
        matrix_a, matrix_b = merged[module_name]
        layer.lora_A["default"].weight.data.copy_(
            matrix_a.to(
                device=layer.lora_A["default"].weight.device,
                dtype=layer.lora_A["default"].weight.dtype,
            )
        )
        layer.lora_B["default"].weight.data.copy_(
            matrix_b.to(
                device=layer.lora_B["default"].weight.device,
                dtype=layer.lora_B["default"].weight.dtype,
            )
        )

    output_dir = work_dir / request.output_adapter_path
    if output_dir.exists() or output_dir.is_symlink():
        raise ValueError("SD-LoRA output adapter path already exists")
    peft_model.save_pretrained(output_dir, safe_serialization=True)
    state_tensors = {
        _state_key(task_index, module_name, matrix): tensor
        for task_index, component in enumerate(all_components)
        for module_name, pair in component.items()
        for matrix, tensor in zip(("A", "B"), pair, strict=True)
    }
    state_weights_path = output_dir / SD_LORA_STATE_WEIGHTS
    save_file(state_tensors, str(state_weights_path))
    state_weights_size, state_weights_digest = _sha256_file(state_weights_path)
    training_time_seconds = time.monotonic() - training_started
    gpu_peak_memory_bytes = int(torch.cuda.max_memory_allocated())
    if (
        not math.isfinite(training_time_seconds)
        or training_time_seconds <= 0.0
        or gpu_peak_memory_bytes < 1
    ):
        raise RuntimeError("SD-LoRA trainer produced invalid resource accounting")
    state_manifest = SdLoraStateManifest(
        adapter_id=request.adapter_id,
        base_model=request.config.base_model,
        model_revision=request.config.model_revision,
        task_index=component_count - 1,
        component_count=component_count,
        effective_rank=effective_rank,
        target_module_suffixes=request.config.target_modules,
        modules=module_specs,
        components=tuple(
            SdLoraStateComponent(
                task_index=index,
                rank=request.config.rank,
                coefficient=coefficient_values[index],
            )
            for index in range(component_count)
        ),
        state_weights_size_bytes=state_weights_size,
        state_weights_sha256=state_weights_digest,
        training_record_count=request.training_record_count,
        steps_completed=steps_completed,
        training_loss=training_loss,
        training_time_seconds=training_time_seconds,
        gpu_peak_memory_bytes=gpu_peak_memory_bytes,
        source_dataset_artifact_ids=request.source_dataset_artifact_ids,
        prior_parametric_memory_artifact_id=(request.prior_parametric_memory_artifact_id),
    )
    _write_private_json(
        output_dir / SD_LORA_STATE_MANIFEST,
        state_manifest.model_dump(mode="json"),
    )
    for directory, _, filenames in os.walk(output_dir):
        os.chmod(directory, 0o700, follow_symlinks=False)
        for filename in filenames:
            os.chmod(Path(directory) / filename, 0o600, follow_symlinks=False)

    return SdLoraTrainingResult(
        request_id=request.request_id,
        adapter_path=request.output_adapter_path,
        state_manifest_path=f"{request.output_adapter_path}/{SD_LORA_STATE_MANIFEST}",
        state_weights_path=f"{request.output_adapter_path}/{SD_LORA_STATE_WEIGHTS}",
        training_record_count=request.training_record_count,
        steps_completed=steps_completed,
        training_loss=training_loss,
        training_time_seconds=training_time_seconds,
        gpu_peak_memory_bytes=gpu_peak_memory_bytes,
        task_index=component_count - 1,
        component_count=component_count,
        effective_rank=effective_rank,
        target_module_names=target_names,
        coefficients=coefficient_values,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpenEvo internal SD-LoRA trainer")
    parser.add_argument("--request", required=True)
    parser.add_argument("--response", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    _install_parent_death_signal()
    args = _parser().parse_args(argv)
    os.umask(0o077)
    request_name = validate_relative_path(args.request)
    response_name = validate_relative_path(args.response)
    if Path(request_name).name != request_name or Path(response_name).name != response_name:
        raise ValueError("trainer request and response must be direct work-directory files")
    request_path = Path.cwd() / request_name
    response_path = Path.cwd() / response_name
    request_bytes = _read_owned_file(request_path, maximum_bytes=_MAX_REQUEST_BYTES)
    try:
        request = SdLoraTrainingRequest.model_validate_json(request_bytes)
    except ValueError as exc:
        raise ValueError("SD-LoRA trainer request is invalid") from exc
    if request.work_dir != str(Path.cwd()):
        raise ValueError("SD-LoRA trainer request work directory does not match cwd")
    if request_bytes != (canonical_json(request.model_dump(mode="json")) + "\n").encode("utf-8"):
        raise ValueError("SD-LoRA trainer request bytes are not canonical")
    _apply_resource_limits(request.config.timeout_seconds)
    result = _train(request)
    _write_private_json(response_path, result.model_dump(mode="json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "_apply_resource_limits",
    "_compose_cumulative_weights",
    "_install_parent_death_signal",
    "main",
]
