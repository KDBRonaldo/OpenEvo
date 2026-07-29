"""Closed contracts for Daemon-owned parametric-memory training."""

from __future__ import annotations

import math
import os
import re
import unicodedata
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import Field, field_validator, model_validator

from openevo.evolution.framework.contracts import (
    MAX_JAVASCRIPT_SAFE_INTEGER,
    _Contract,
    _stable_id,
    validate_relative_path,
)


SD_LORA_STATE_MANIFEST = "openevo_sd_lora_state.json"
SD_LORA_STATE_WEIGHTS = "openevo_sd_lora_state.safetensors"
SD_LORA_REQUEST_SCHEMA = "openevo.sd_lora_train_request.v1"
SD_LORA_RESULT_SCHEMA = "openevo.sd_lora_train_result.v1"
SD_LORA_STATE_SCHEMA = "openevo.sd_lora_state.v1"
MAX_SD_LORA_COMPONENTS = 64
MAX_SD_LORA_EFFECTIVE_RANK = 4096
MAX_SD_LORA_TARGET_MODULES = 128
_MODULE_SUFFIX_RE = re.compile(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*")
_MODEL_ID_RE = re.compile(r"(?:[A-Za-z0-9][A-Za-z0-9._-]{0,95}/)?[A-Za-z0-9][A-Za-z0-9._-]{0,95}")
_IMMUTABLE_MODEL_REVISION_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


class SdLoraDType(StrEnum):
    BFLOAT16 = "bfloat16"
    FLOAT16 = "float16"
    FLOAT32 = "float32"


def _identity_text(value: str, *, label: str, maximum: int = 4096) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or value != value.strip()
        or unicodedata.normalize("NFC", value) != value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or any(0xD800 <= ord(character) <= 0xDFFF for character in value)
    ):
        raise ValueError(f"{label} must be normalized bounded identity text")
    return value


def _module_suffixes(values: tuple[str, ...]) -> tuple[str, ...]:
    if not values or len(values) > MAX_SD_LORA_TARGET_MODULES:
        raise ValueError("target_modules must contain between 1 and 128 entries")
    normalized = tuple(
        _identity_text(value, label="target module", maximum=256) for value in values
    )
    if any(_MODULE_SUFFIX_RE.fullmatch(value) is None for value in normalized):
        raise ValueError("target_modules entries must be dotted Python module suffixes")
    if len(normalized) != len(set(normalized)):
        raise ValueError("target_modules entries must be unique")
    return normalized


def _module_names(values: tuple[str, ...]) -> tuple[str, ...]:
    if not values or len(values) > 4096:
        raise ValueError("target module names must contain between 1 and 4096 entries")
    normalized = tuple(
        _identity_text(value, label="target module name", maximum=1024) for value in values
    )
    if any(_MODULE_SUFFIX_RE.fullmatch(value) is None for value in normalized):
        raise ValueError("target module names must be dotted Python module paths")
    if len(normalized) != len(set(normalized)):
        raise ValueError("target module names must be unique")
    return normalized


def _model_id(value: str) -> str:
    normalized = _identity_text(value, label="base_model", maximum=193)
    if _MODEL_ID_RE.fullmatch(normalized) is None:
        raise ValueError("base_model must be a model ID, not a path or URI")
    return normalized


def _immutable_model_revision(value: str) -> str:
    normalized = _identity_text(value, label="model_revision", maximum=64)
    if _IMMUTABLE_MODEL_REVISION_RE.fullmatch(normalized) is None:
        raise ValueError("model_revision must be a full immutable hexadecimal revision")
    return normalized


class SdLoraMethodConfig(_Contract):
    """User-visible method configuration after Core project injections."""

    base_model: str = Field(min_length=1, max_length=193)
    model_revision: str = Field(min_length=40, max_length=64)
    rank: int = Field(default=8, ge=1, le=128)
    target_modules: tuple[str, ...] = (
        "q_proj",
        "k_proj",
        "v_proj",
        "o_proj",
    )
    learning_rate: float = Field(default=2.0e-4, gt=0.0, le=1.0)
    weight_decay: float = Field(default=0.0, ge=0.0, le=1.0)
    epochs: int = Field(default=1, ge=1, le=100)
    max_steps: int | None = Field(default=None, ge=1, le=1_000_000)
    max_length: int = Field(default=2048, ge=32, le=131_072)
    max_records: int = Field(default=256, ge=1, le=100_000)
    per_device_train_batch_size: int = Field(default=1, ge=1, le=128)
    gradient_accumulation_steps: int = Field(default=1, ge=1, le=4096)
    max_grad_norm: float = Field(default=1.0, gt=0.0, le=1_000.0)
    dtype: SdLoraDType = SdLoraDType.BFLOAT16
    load_in_4bit: bool = False
    gradient_checkpointing: bool = True
    coefficient_init: float = Field(default=0.8, gt=0.0, le=100.0)
    minimum_reward: float = Field(default=0.5, ge=-1_000_000.0, le=1_000_000.0)
    seed: int = Field(default=1993, ge=0, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    timeout_seconds: float = Field(default=3600.0, gt=0.0, le=86_400.0)

    _model = field_validator("base_model")(_model_id)
    _revision = field_validator("model_revision")(_immutable_model_revision)
    _targets = field_validator("target_modules")(_module_suffixes)

    @model_validator(mode="after")
    def _finite_values(self) -> SdLoraMethodConfig:
        for label, value in (
            ("learning_rate", self.learning_rate),
            ("weight_decay", self.weight_decay),
            ("max_grad_norm", self.max_grad_norm),
            ("coefficient_init", self.coefficient_init),
            ("minimum_reward", self.minimum_reward),
            ("timeout_seconds", self.timeout_seconds),
        ):
            if not math.isfinite(value):
                raise ValueError(f"{label} must be finite")
        return self


class SdLoraTrainingRequest(_Contract):
    schema_version: str = SD_LORA_REQUEST_SCHEMA
    request_id: str
    work_dir: str = Field(min_length=1, max_length=4096)
    training_data_path: str
    prior_adapter_path: str | None = None
    output_adapter_path: str
    adapter_id: str
    source_dataset_artifact_ids: tuple[str, ...]
    prior_parametric_memory_artifact_id: str | None = None
    training_record_count: int = Field(ge=1, le=100_000)
    config: SdLoraMethodConfig

    _request_id = field_validator("request_id", "adapter_id")(_stable_id)
    _paths = field_validator(
        "training_data_path",
        "prior_adapter_path",
        "output_adapter_path",
    )(lambda value: None if value is None else validate_relative_path(value))
    _dataset_ids = field_validator("source_dataset_artifact_ids")(
        lambda values: tuple(
            _identity_text(value, label="dataset artifact ID", maximum=256) for value in values
        )
    )
    _prior_id = field_validator("prior_parametric_memory_artifact_id")(
        lambda value: (
            None
            if value is None
            else _identity_text(value, label="prior artifact ID", maximum=256)
        )
    )

    @field_validator("work_dir")
    @classmethod
    def _absolute_work_dir(cls, value: str) -> str:
        normalized = _identity_text(value, label="work_dir")
        if not os.path.isabs(normalized) or os.path.normpath(normalized) != normalized:
            raise ValueError("work_dir must be a normalized absolute path")
        return normalized

    @model_validator(mode="after")
    def _request_shape(self) -> SdLoraTrainingRequest:
        if self.schema_version != SD_LORA_REQUEST_SCHEMA:
            raise ValueError("unsupported SD-LoRA training request schema")
        if not self.source_dataset_artifact_ids:
            raise ValueError("SD-LoRA training requires at least one dataset artifact")
        if len(self.source_dataset_artifact_ids) != len(set(self.source_dataset_artifact_ids)):
            raise ValueError("source dataset artifact IDs must be unique")
        if (self.prior_adapter_path is None) != (self.prior_parametric_memory_artifact_id is None):
            raise ValueError("prior adapter path and artifact ID must be supplied together")
        return self


class SdLoraTrainingResult(_Contract):
    schema_version: str = SD_LORA_RESULT_SCHEMA
    request_id: str
    adapter_path: str
    state_manifest_path: str
    state_weights_path: str
    training_record_count: int = Field(ge=1, le=100_000)
    steps_completed: int = Field(ge=1, le=1_000_000)
    training_loss: float
    task_index: int = Field(ge=0, lt=MAX_SD_LORA_COMPONENTS)
    component_count: int = Field(ge=1, le=MAX_SD_LORA_COMPONENTS)
    effective_rank: int = Field(ge=1, le=MAX_SD_LORA_EFFECTIVE_RANK)
    target_module_names: tuple[str, ...]
    coefficients: tuple[float, ...]

    _request_id = field_validator("request_id")(_stable_id)
    _paths = field_validator(
        "adapter_path",
        "state_manifest_path",
        "state_weights_path",
    )(validate_relative_path)
    _modules = field_validator("target_module_names")(_module_names)

    @model_validator(mode="after")
    def _result_shape(self) -> SdLoraTrainingResult:
        if self.schema_version != SD_LORA_RESULT_SCHEMA:
            raise ValueError("unsupported SD-LoRA training result schema")
        if self.task_index + 1 != self.component_count:
            raise ValueError("SD-LoRA task index must identify the newest component")
        if len(self.coefficients) != self.component_count:
            raise ValueError("SD-LoRA result requires one coefficient per component")
        if not math.isfinite(self.training_loss):
            raise ValueError("training_loss must be finite")
        if any(not math.isfinite(value) for value in self.coefficients):
            raise ValueError("SD-LoRA coefficients must be finite")
        return self


class SdLoraStateModule(_Contract):
    name: str
    in_features: int = Field(ge=1, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    out_features: int = Field(ge=1, le=MAX_JAVASCRIPT_SAFE_INTEGER)

    _name = field_validator("name")(lambda value: _module_names((value,))[0])


class SdLoraStateComponent(_Contract):
    task_index: int = Field(ge=0, lt=MAX_SD_LORA_COMPONENTS)
    rank: int = Field(ge=1, le=128)
    coefficient: float

    @field_validator("coefficient")
    @classmethod
    def _finite_coefficient(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("SD-LoRA component coefficient must be finite")
        return value


class SdLoraStateManifest(_Contract):
    schema_version: str = SD_LORA_STATE_SCHEMA
    algorithm_family: str = "SD-LoRA"
    adaptation_scope: str = "causal_lm_continual_sft_v1"
    paper_equivalent: bool = False
    adapter_id: str
    base_model: str
    model_revision: str
    task_index: int = Field(ge=0, lt=MAX_SD_LORA_COMPONENTS)
    component_count: int = Field(ge=1, le=MAX_SD_LORA_COMPONENTS)
    effective_rank: int = Field(ge=1, le=MAX_SD_LORA_EFFECTIVE_RANK)
    target_module_suffixes: tuple[str, ...]
    modules: tuple[SdLoraStateModule, ...]
    components: tuple[SdLoraStateComponent, ...]
    state_weights_size_bytes: int = Field(ge=1, le=16 * 1024 * 1024 * 1024)
    state_weights_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    training_record_count: int = Field(ge=1, le=100_000)
    steps_completed: int = Field(ge=1, le=1_000_000)
    training_loss: float
    source_dataset_artifact_ids: tuple[str, ...]
    prior_parametric_memory_artifact_id: str | None = None
    upstream_repository: str = "https://github.com/WuYichen-97/SD-Lora-CL"
    upstream_revision: str = "8bacded6eb44786db071f66fb90a87dd660d94ea"
    routing_mode: str = "single_cumulative_adapter"

    _adapter = field_validator("adapter_id")(_stable_id)
    _model = field_validator("base_model")(_model_id)
    _revision = field_validator("model_revision")(_immutable_model_revision)
    _suffixes = field_validator("target_module_suffixes")(_module_suffixes)
    _dataset_ids = field_validator("source_dataset_artifact_ids")(
        lambda values: tuple(
            _identity_text(value, label="dataset artifact ID", maximum=256) for value in values
        )
    )
    _prior_id = field_validator("prior_parametric_memory_artifact_id")(
        lambda value: (
            None
            if value is None
            else _identity_text(value, label="prior artifact ID", maximum=256)
        )
    )

    @model_validator(mode="after")
    def _state_shape(self) -> SdLoraStateManifest:
        if (
            self.schema_version != SD_LORA_STATE_SCHEMA
            or self.algorithm_family != "SD-LoRA"
            or self.adaptation_scope != "causal_lm_continual_sft_v1"
            or self.paper_equivalent is not False
            or self.routing_mode != "single_cumulative_adapter"
        ):
            raise ValueError("unsupported SD-LoRA state identity")
        if self.task_index + 1 != self.component_count:
            raise ValueError("SD-LoRA state task index must identify the newest component")
        if len(self.components) != self.component_count:
            raise ValueError("SD-LoRA state component count does not match")
        if tuple(item.task_index for item in self.components) != tuple(
            range(self.component_count)
        ):
            raise ValueError("SD-LoRA state components must be contiguous and ordered")
        if sum(item.rank for item in self.components) != self.effective_rank:
            raise ValueError("SD-LoRA state effective rank does not match components")
        module_names = tuple(item.name for item in self.modules)
        if not module_names or len(module_names) != len(set(module_names)):
            raise ValueError("SD-LoRA state modules must be non-empty and unique")
        if not self.source_dataset_artifact_ids:
            raise ValueError("SD-LoRA state requires source dataset lineage")
        if not math.isfinite(self.training_loss):
            raise ValueError("SD-LoRA state training loss must be finite")
        return self


@runtime_checkable
class CoreParametricTrainer(Protocol):
    """Daemon-owned training capability exposed to verified context methods."""

    def train_sd_lora(self, request: SdLoraTrainingRequest) -> SdLoraTrainingResult: ...


__all__ = [
    "CoreParametricTrainer",
    "MAX_SD_LORA_COMPONENTS",
    "MAX_SD_LORA_EFFECTIVE_RANK",
    "SD_LORA_REQUEST_SCHEMA",
    "SD_LORA_RESULT_SCHEMA",
    "SD_LORA_STATE_SCHEMA",
    "SD_LORA_STATE_MANIFEST",
    "SD_LORA_STATE_WEIGHTS",
    "SdLoraDType",
    "SdLoraMethodConfig",
    "SdLoraStateComponent",
    "SdLoraStateManifest",
    "SdLoraStateModule",
    "SdLoraTrainingRequest",
    "SdLoraTrainingResult",
]
