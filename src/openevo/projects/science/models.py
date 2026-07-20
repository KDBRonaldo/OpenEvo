from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from openevo.codex_models import validate_codex_model_ref
from openevo.evolution.framework import ProjectEvolutionTargetMap
from openevo.projects.evolution_defaults import default_project_evolution_targets
from openevo.runtime.managed import reject_managed_subscription_env


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class ProjectInfo(_StrictModel):
    name: str = Field(min_length=1)
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        return _strip_non_empty(value, "project.name")

    @field_validator("description")
    @classmethod
    def _strip_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _strip_non_empty(value, "project.description")


class TaskSourceConfig(_StrictModel):
    type: Literal["local_folder", "git_repository", "remote_path", "scratch"] = "scratch"
    path: str | None = None
    url: str | None = None
    branch: str | None = None

    @field_validator("path", "url", "branch")
    @classmethod
    def _strip_optional_text(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _strip_non_empty(value, f"task.source.{info.field_name}")

    @model_validator(mode="after")
    def _validate_source_fields(self) -> TaskSourceConfig:
        if self.type in {"local_folder", "remote_path"}:
            if self.path is None:
                raise ValueError(f"task.source.{self.type} requires path")
            if self.url is not None:
                raise ValueError(f"task.source.{self.type} must not set url")
        elif self.type == "git_repository":
            if self.url is None:
                raise ValueError("task.source.git_repository requires url")
        elif self.type == "scratch":
            if self.path is not None or self.url is not None or self.branch is not None:
                raise ValueError("task.source.scratch must not set path, url, or branch")

        if self.branch is not None and self.type != "git_repository":
            raise ValueError("task.source.branch is only valid for git_repository")
        return self


class ScienceTaskConfig(_StrictModel):
    id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    source: TaskSourceConfig = Field(default_factory=TaskSourceConfig)
    setup_commands: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id", "objective")
    @classmethod
    def _strip_required_text(cls, value: str, info) -> str:
        text = _strip_non_empty(value, f"task.{info.field_name}")
        if info.field_name == "id" and "/" in text:
            raise ValueError("task.id must not contain '/'")
        return text

    @field_validator("setup_commands")
    @classmethod
    def _strip_setup_commands(cls, value: list[str]) -> list[str]:
        commands = []
        for index, command in enumerate(value):
            text = command.strip()
            if not text:
                raise ValueError(f"task.setup_commands[{index}] must be a non-empty string")
            commands.append(text)
        return commands


class EnvironmentConfig(_StrictModel):
    profile: Literal["managed_science", "python_research", "custom_image"] = (
        "managed_science"
    )
    custom_image: str | None = None
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("custom_image")
    @classmethod
    def _strip_custom_image(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _strip_non_empty(value, "environment.custom_image")

    @model_validator(mode="after")
    def _validate_custom_image(self) -> EnvironmentConfig:
        if self.profile == "custom_image":
            if self.custom_image is None:
                raise ValueError("environment.custom_image is required for custom_image")
        elif self.custom_image is not None:
            raise ValueError("environment.custom_image is only valid for custom_image")
        if self.profile != "custom_image":
            reject_managed_subscription_env(self.env, owner="environment")
        return self


_LEGACY_SELF_DEPLOYED_MODE = "codex_managed_local_inference"
_SELF_DEPLOYED_MODE = "self-deployed"


class ExecutionConfig(_StrictModel):
    mode: Literal["codex_subscription_transcript", "self-deployed"] = (
        "codex_subscription_transcript"
    )
    codex_model: str | None = None
    reasoning_effort: Literal["low", "medium", "high", "xhigh"] | None = None
    hf_model: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _default_subscription_codex_model(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        mode = _normalize_execution_mode(
            data.get("mode", "codex_subscription_transcript")
        )
        if mode == _SELF_DEPLOYED_MODE and "codex_model" in data:
            raise ValueError(
                "execution.codex_model is only valid for subscription transcript mode"
            )
        data = data | {"mode": mode}
        if "codex_model" in data:
            return data
        if mode != "codex_subscription_transcript":
            return data
        return data | {"codex_model": "gpt-5.5"}

    @model_serializer(mode="wrap")
    def _serialize_without_null_codex_model(self, serializer) -> dict[str, Any]:
        data = serializer(self)
        if self.codex_model is None:
            data.pop("codex_model", None)
        if self.reasoning_effort is None:
            data.pop("reasoning_effort", None)
        return data

    @field_validator("codex_model")
    @classmethod
    def _strip_codex_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_codex_model_ref(
            value,
            field_name="execution.codex_model",
        )

    @field_validator("hf_model")
    @classmethod
    def _strip_hf_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _strip_non_empty(value, "execution.hf_model")

    @model_validator(mode="after")
    def _validate_mode_specific_fields(self) -> ExecutionConfig:
        if self.mode == "codex_subscription_transcript":
            if self.codex_model is None:
                raise ValueError(
                    "execution.codex_model is required for subscription transcript mode"
                )
            if self.hf_model is not None:
                raise ValueError(
                    "execution.hf_model is only valid for self-deployed mode"
                )
        elif self.mode == _SELF_DEPLOYED_MODE:
            if self.codex_model is not None:
                raise ValueError(
                    "execution.codex_model is only valid for subscription transcript mode"
                )
            if self.hf_model is None:
                raise ValueError("execution.hf_model is required for self-deployed mode")
            if self.reasoning_effort is not None:
                raise ValueError(
                    "execution.reasoning_effort is only valid for subscription transcript mode"
                )
        return self


class EvolutionTargetsConfig(_StrictModel):
    targets: ProjectEvolutionTargetMap = Field(
        default_factory=default_project_evolution_targets
    )

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        """Return a fully validated target-map copy."""

        del deep
        payload = self.model_dump(mode="python")
        if update:
            payload.update(update)
        return type(self).model_validate(payload)


class ScienceProjectConfig(_StrictModel):
    version: Literal[1] = 1
    project: ProjectInfo
    remote_profile: str = Field(min_length=1)
    task: ScienceTaskConfig
    environment: EnvironmentConfig = Field(default_factory=EnvironmentConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    evolution: EvolutionTargetsConfig = Field(default_factory=EvolutionTargetsConfig)
    path: Path | None = None

    @field_validator("remote_profile")
    @classmethod
    def _strip_remote_profile(cls, value: str) -> str:
        return _strip_non_empty(value, "remote_profile")

    @model_validator(mode="after")
    def _validate_execution_evolution_compatibility(self) -> ScienceProjectConfig:
        if (
            self.environment.profile == "custom_image"
            and self.execution.mode == "codex_subscription_transcript"
        ):
            raise ValueError(
                "Codex subscription is not supported with environment.profile="
                "'custom_image' because OpenEvo cannot safely stage credentials for "
                "an image-defined user; choose a managed environment or self-deployed execution"
            )
        parametric_memory = self.evolution.targets.get("parametric_memory")
        if parametric_memory is not None and parametric_memory.enabled:
            raise ValueError(
                "Science Projects do not support parametric_memory yet"
            )
        return self


def load_science_project_config(path: Path) -> ScienceProjectConfig:
    if not path.exists():
        raise FileNotFoundError(f"Science project config not found: {path}")
    if not path.is_file():
        raise ValueError(f"Science project config path is not a file: {path}")

    try:
        with path.open(encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {path}: {exc}") from exc

    if not isinstance(loaded, dict):
        raise ValueError(f"Science project config {path} must contain a top-level mapping")

    return ScienceProjectConfig.model_validate({**loaded, "path": path})


def _strip_non_empty(value: str, field_name: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


def _normalize_execution_mode(value: Any) -> Any:
    if value == _LEGACY_SELF_DEPLOYED_MODE:
        return _SELF_DEPLOYED_MODE
    return value
