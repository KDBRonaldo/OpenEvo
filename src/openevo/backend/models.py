from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openevo.codex_models import validate_codex_model_ref
from openevo.evolution.framework import ProjectEvolutionTargetMap
from openevo.projects.science.models import ScienceTaskConfig


ErrorSeverity = Literal["info", "warning", "blocking"]
ErrorCategory = Literal["environment", "project", "run", "artifact", "service", "internal"]
RepairAction = Literal[
    "openevo_can_retry",
    "openevo_can_install",
    "openevo_can_reconfigure",
    "user_action_required",
    "unsupported",
]
ExecutionMode = Literal["codex_subscription_transcript", "self-deployed"]
CaptureMode = Literal["transcript", "proxy"]
ArtifactFamily = Literal["text_memory", "skill_bundle", "agent_system", "parametric_memory"]
_BENCHMARK_ONLY_TASK_KEYS = frozenset(
    {
        "benchmark_task_id",
        "benchmark_id",
        "benchmark_name",
        "terminal_bench_task_id",
    }
)
MAX_EVOLUTION_PROJECT_VALIDATION_REQUEST_BYTES = 1024 * 1024
MAX_EVOLUTION_PROJECT_VALIDATION_JSON_DEPTH = 24


class BackendError(BaseModel):
    code: str
    message: str
    severity: ErrorSeverity
    category: ErrorCategory
    retryable: bool
    repair_action: RepairAction
    details: dict[str, Any] = Field(default_factory=dict)
    logs_ref: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok"]


class ServiceSummary(BaseModel):
    id: str
    name: str
    status: Literal["stopped", "starting", "running", "failed"]
    restartable: bool = True


class BackendStatus(BaseModel):
    status: Literal["starting", "ready", "degraded", "blocked"]
    services: list[ServiceSummary]
    active_runs: int = 0
    supervision_mode: Literal["scaffold", "managed"] = "scaffold"


class EvolutionProjectValidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    execution_mode: ExecutionMode
    expected_registry_digest: str = Field(pattern=r"[0-9a-f]{64}")
    agent_model: str = Field(min_length=1, max_length=4096)
    reasoning_effort: Literal["low", "medium", "high", "xhigh"] | None = None
    targets: ProjectEvolutionTargetMap

    @field_validator("agent_model")
    @classmethod
    def _validate_subscription_codex_model(cls, value: str, info) -> str:
        execution_mode = info.data.get("execution_mode")
        if execution_mode == "codex_subscription_transcript":
            return validate_codex_model_ref(
                value,
                field_name="agent_model",
                max_length=4096,
            )
        return value

    @model_validator(mode="after")
    def _scope_reasoning_effort(self) -> EvolutionProjectValidationRequest:
        if (
            self.execution_mode != "codex_subscription_transcript"
            and self.reasoning_effort is not None
        ):
            raise ValueError(
                "reasoning_effort is only valid for Codex subscription execution"
            )
        return self


class EvolutionProjectValidationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    valid: Literal[True] = True
    registry_digest: str = Field(pattern=r"[0-9a-f]{64}")


class EnvironmentSettings(BaseModel):
    workspace_root: str = "~/.openevo/workspaces"
    proxy_url: str | None = None
    no_proxy: list[str] = Field(default_factory=list)
    pip_index_url: str | None = None
    huggingface_endpoint: str | None = None
    huggingface_cache: str | None = None


class EnvironmentDoctorRequest(BaseModel):
    repair: bool = False


class EnvironmentCheck(BaseModel):
    id: str
    category: Literal["python", "docker", "codex", "network"]
    status: Literal["ok", "warning", "blocking"]
    message: str
    repair_action: RepairAction


class EnvironmentDoctorResponse(BaseModel):
    status: Literal["ok", "needs_user_action"]
    checks: list[EnvironmentCheck]


class EnvironmentRepairRequest(BaseModel):
    actions: list[str] = Field(default_factory=list)


class EnvironmentRepairResponse(BaseModel):
    status: Literal["ok", "needs_user_action"]
    performed_actions: list[str] = Field(default_factory=list)
    errors: list[BackendError] = Field(default_factory=list)


class ProjectCreateRequest(BaseModel):
    name: str
    workspace_root: str


class ProjectPatchRequest(BaseModel):
    name: str | None = None
    workspace_root: str | None = None


class ProjectSummary(BaseModel):
    id: str
    name: str
    workspace_root: str
    status: Literal["draft", "ready", "blocked"] = "draft"


class RunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)

    schema_version: Literal["1"]
    idempotency_key: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    project_snapshot_id: str = Field(min_length=1)
    workspace_snapshot_ref: str = Field(min_length=1)
    task: ScienceTaskConfig
    execution_mode: ExecutionMode
    capture_mode: CaptureMode
    artifact_families: list[ArtifactFamily] = Field(min_length=1)
    method_ids: list[str] = Field(min_length=1)
    runtime: dict[str, Any] = Field(default_factory=dict)
    model: dict[str, Any] = Field(default_factory=dict)
    context_artifact_ids: list[str] | None = None

    @model_validator(mode="after")
    def _validate_science_run_contract(self) -> RunCreateRequest:
        if (
            self.execution_mode == "codex_subscription_transcript"
            and self.capture_mode != "transcript"
        ):
            raise ValueError(
                "subscription execution mode requires transcript capture"
            )
        _reject_benchmark_only_task_fields(self.task.model_dump(mode="json"))
        return self


class RunSummary(BaseModel):
    id: str
    project_id: str
    execution_mode: ExecutionMode
    status: Literal["created", "running", "completed", "failed", "cancelled"]


class TimelineEvent(BaseModel):
    id: str
    phase: str
    title: str
    message: str
    artifact_ids: list[str] = Field(default_factory=list)


class LogResponse(BaseModel):
    id: str
    lines: list[str]


class ArtifactSummary(BaseModel):
    id: str
    run_id: str
    artifact_type: Literal["text_memory", "skill_bundle", "agent_system", "parametric_memory"]
    title: str
    promoted: bool = False
    lineage: dict[str, Any] = Field(default_factory=dict)


class ArtifactContent(BaseModel):
    id: str
    artifact_type: str
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArtifactDiff(BaseModel):
    id: str
    before: str
    after: str
    format: Literal["unified_text"] = "unified_text"


class ServiceActionResponse(BaseModel):
    service_id: str
    status: Literal["running", "stopped", "failed"]


def _reject_benchmark_only_task_fields(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in _BENCHMARK_ONLY_TASK_KEYS:
                raise ValueError(
                    f"RunCreateRequest.task contains benchmark-only field: {key}"
                )
            _reject_benchmark_only_task_fields(nested)
    elif isinstance(value, list):
        for item in value:
            _reject_benchmark_only_task_fields(item)
