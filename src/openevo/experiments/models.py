from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openevo.harness.capture import TRANSCRIPT_CAPTURE_MODES, transcript_capture_enabled

_SUBSCRIPTION_AUTH_MODES = {"subscription", "chatgpt_subscription"}
_NATIVE_MEMORY_POLICIES = {"preserve", "clear"}
PROMOTION_SUPPORT_FIELDS = [
    "trajectory_findings",
    "proposed_changes",
    "expected_benefits",
    "risks",
    "validation_checks",
]
PROMOTABLE_ARTIFACT_TYPES = [
    "text_memory",
    "skill_bundle",
    "agent_system",
    "parametric_memory",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class ExperimentInfo(_StrictModel):
    name: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        return _strip_non_empty(value, "experiment.name")


class AgentConfig(_StrictModel):
    preset: str = Field(min_length=1)
    model: str = Field(min_length=1)
    auth: Literal["proxy", "subscription", "chatgpt_subscription"] = "proxy"
    provider: str | None = None
    settings: dict[str, Any] = Field(default_factory=dict)
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("preset", "model")
    @classmethod
    def _strip_required_text(cls, value: str, info) -> str:
        return _strip_non_empty(value, f"agent.{info.field_name}")

    @field_validator("provider")
    @classmethod
    def _strip_provider(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _strip_non_empty(value, "agent.provider")

    @model_validator(mode="after")
    def _require_transcript_capture_for_subscription(self) -> AgentConfig:
        settings_auth_mode = self.settings.get("auth_mode")
        if (
            settings_auth_mode is not None
            and _auth_mode_family(settings_auth_mode) != _auth_mode_family(self.auth)
        ):
            raise ValueError("agent.settings.auth_mode must match agent.auth")
        capture_mode = self.settings.get("capture_mode")
        if (
            self.auth in _SUBSCRIPTION_AUTH_MODES
            and (
                capture_mode is None
                or not transcript_capture_enabled(capture_mode)
            )
        ):
            accepted = ", ".join(repr(value) for value in TRANSCRIPT_CAPTURE_MODES)
            raise ValueError(
                "subscription agents require transcript capture; "
                f"settings.capture_mode must be one of: {accepted}"
            )
        if "native_memory_policy" in self.settings:
            native_memory_policy = self.settings["native_memory_policy"]
            if (
                not isinstance(native_memory_policy, str)
                or native_memory_policy not in _NATIVE_MEMORY_POLICIES
            ):
                raise ValueError(
                    "agent.settings.native_memory_policy must be 'preserve' or 'clear'"
                )
        return self


class RuntimePrepareActionConfig(_StrictModel):
    type: Literal["upload_file", "upload_dir", "exec"]
    source: str | None = None
    target: str | None = None
    command: str | None = None
    cwd: str | None = None
    env: dict[str, str] | None = None

    @model_validator(mode="after")
    def _validate_fields(self) -> RuntimePrepareActionConfig:
        if self.type in {"upload_file", "upload_dir"}:
            if not self.source or not self.target:
                raise ValueError(f"{self.type} requires source and target")
            if self.command is not None or self.cwd is not None or self.env is not None:
                raise ValueError(f"{self.type} must not set command, cwd, or env")
        elif self.type == "exec":
            if not self.command:
                raise ValueError("exec requires command")
            if self.source is not None or self.target is not None:
                raise ValueError("exec must not set source or target")
        return self


class RuntimeConfig(_StrictModel):
    kind: Literal["docker", "apptainer"] = "docker"
    workdir: str = "/openevo/session/workspace"
    image: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    prepare: list[RuntimePrepareActionConfig] = Field(default_factory=list)

    @field_validator("workdir")
    @classmethod
    def _strip_workdir(cls, value: str) -> str:
        return _strip_non_empty(value, "runtime.workdir")

    @field_validator("image")
    @classmethod
    def _strip_image(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _strip_non_empty(value, "runtime.image")


class RolloutConfig(_StrictModel):
    url: str = Field(
        default_factory=lambda: os.environ.get(
            "OPENEVO_ROLLOUT_URL",
            "http://127.0.0.1:8080",
        )
    )

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        return _normalize_http_url(value, "rollout.url")


class EvolutionWorkerConfig(_StrictModel):
    mode: Literal["local_once"] = "local_once"


class PromotionGateConfig(_StrictModel):
    mode: Literal["none", "human", "llm"] = "none"
    human_input: Literal["auto", "file", "tui"] = "auto"
    artifact_types: list[
        Literal["text_memory", "skill_bundle", "agent_system", "parametric_memory"]
    ] = Field(default_factory=lambda: list(PROMOTABLE_ARTIFACT_TYPES))
    review_dir: str | None = None
    decision_dir: str | None = None
    min_score: float = Field(default=0.7, ge=0.0, le=1.0)
    require_support: bool = True
    max_artifact_content_chars: int = Field(default=12000, ge=0)
    decision_timeout_seconds: float = Field(default=300.0, ge=0.0)
    decision_poll_interval_seconds: float = Field(default=2.0, gt=0.0)
    llm: dict[str, Any] = Field(default_factory=dict)

    @field_validator("artifact_types")
    @classmethod
    def _dedupe_artifact_types(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))

    @field_validator("review_dir", "decision_dir")
    @classmethod
    def _strip_dir(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _strip_non_empty(value, f"evolution.promotion_gate.{info.field_name}")


class EvolutionConfig(_StrictModel):
    backend_url: str = Field(
        default_factory=lambda: os.environ.get(
            "OPENEVO_EVOLUTION_URL",
            "http://127.0.0.1:8200",
        )
    )
    rounds: int = Field(default=1, ge=1)
    worker: EvolutionWorkerConfig = Field(default_factory=EvolutionWorkerConfig)
    promotion_gate: PromotionGateConfig = Field(default_factory=PromotionGateConfig)

    @field_validator("backend_url")
    @classmethod
    def _validate_backend_url(cls, value: str) -> str:
        return _normalize_http_url(value, "evolution.backend_url")


class AgentSystemArtifactConfig(_StrictModel):
    enabled: bool = True
    method: str = "auto"
    target_path: str = "AGENTS.md"

    @field_validator("method", "target_path")
    @classmethod
    def _strip_required_text(cls, value: str, info) -> str:
        return _strip_non_empty(value, f"artifacts.agent_system.{info.field_name}")


class TextMemoryArtifactConfig(_StrictModel):
    enabled: bool = True
    method: str = "text_memory_reflector"

    @field_validator("method")
    @classmethod
    def _strip_method(cls, value: str) -> str:
        return _strip_non_empty(value, "artifacts.text_memory.method")


class ParametricMemoryArtifactConfig(_StrictModel):
    enabled: bool = False
    method: str = "parametric_memory_register"
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("method")
    @classmethod
    def _strip_method(cls, value: str) -> str:
        return _strip_non_empty(value, "artifacts.parametric_memory.method")


class SkillBundleArtifactConfig(_StrictModel):
    enabled: bool = True
    method: str = "skill_bundle_reflector"

    @field_validator("method")
    @classmethod
    def _strip_method(cls, value: str) -> str:
        return _strip_non_empty(value, "artifacts.skill_bundle.method")


class ArtifactControls(_StrictModel):
    agent_system: AgentSystemArtifactConfig = Field(default_factory=AgentSystemArtifactConfig)
    text_memory: TextMemoryArtifactConfig = Field(default_factory=TextMemoryArtifactConfig)
    parametric_memory: ParametricMemoryArtifactConfig = Field(
        default_factory=ParametricMemoryArtifactConfig
    )
    skill_bundle: SkillBundleArtifactConfig = Field(default_factory=SkillBundleArtifactConfig)


class TaskConfig(_StrictModel):
    id: str = Field(min_length=1)
    instruction: str = Field(min_length=1)
    workspace: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id", "instruction")
    @classmethod
    def _strip_required_text(cls, value: str, info) -> str:
        text = _strip_non_empty(value, f"tasks[].{info.field_name}")
        if info.field_name == "id" and "/" in text:
            raise ValueError("tasks[].id must not contain '/'")
        return text

    @field_validator("workspace")
    @classmethod
    def _strip_workspace(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _strip_non_empty(value, "tasks[].workspace")


class ExperimentConfig(_StrictModel):
    version: Literal[1] = 1
    experiment: ExperimentInfo
    agent: AgentConfig
    tasks: list[TaskConfig] = Field(min_length=1)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    rollout: RolloutConfig = Field(default_factory=RolloutConfig)
    evolution: EvolutionConfig = Field(default_factory=EvolutionConfig)
    artifacts: ArtifactControls = Field(default_factory=ArtifactControls)
    path: Path | None = None

    @model_validator(mode="after")
    def _require_runtime_image_for_workspace_uploads(self) -> ExperimentConfig:
        task_ids = [task.id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("tasks[].id values must be unique")
        if (
            self.artifacts.parametric_memory.enabled
            and self.agent.auth in _SUBSCRIPTION_AUTH_MODES
        ):
            raise ValueError(
                "artifacts.parametric_memory requires proxy/local inference auth; "
                "subscription runs can use text_memory but cannot apply parametric adapters"
            )
        if self.runtime.image is None and any(task.workspace for task in self.tasks):
            raise ValueError(
                "runtime.image is required when tasks[].workspace is set; "
                "OpenEvo cannot upload a workspace without an explicit runtime image"
            )
        if self.runtime.image is None and _runtime_has_non_default_overrides(self.runtime):
            raise ValueError(
                "runtime.image is required when runtime overrides are set; "
                "remove runtime overrides to use the rollout node default runtime"
            )
        return self


def load_experiment_config(path: Path) -> ExperimentConfig:
    if not path.exists():
        raise FileNotFoundError(f"Experiment config not found: {path}")
    if not path.is_file():
        raise ValueError(f"Experiment config path is not a file: {path}")

    try:
        with path.open(encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {path}: {exc}") from exc

    if not isinstance(loaded, dict):
        raise ValueError(f"Experiment config {path} must contain a top-level mapping")

    return ExperimentConfig.model_validate({**loaded, "path": path})


def _strip_non_empty(value: str, field_name: str) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


def _auth_mode_family(value: object) -> str:
    text = str(value or "")
    if text in _SUBSCRIPTION_AUTH_MODES:
        return "subscription"
    return text


def _normalize_http_url(value: str, field_name: str) -> str:
    text = _strip_non_empty(value, field_name)
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{field_name} must be an http:// or https:// URL")
    return text.rstrip("/")


def _runtime_has_non_default_overrides(runtime: RuntimeConfig) -> bool:
    return (
        runtime.kind != "docker"
        or runtime.workdir != "/openevo/session/workspace"
        or bool(runtime.env)
        or bool(runtime.prepare)
    )
