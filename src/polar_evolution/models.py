from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ArtifactType(StrEnum):
    TEXT_MEMORY = "text_memory"
    SKILL_BUNDLE = "skill_bundle"
    PARAMETRIC_MEMORY = "parametric_memory"
    DATASET = "dataset"
    REPORT = "report"
    CONTEXT_SNAPSHOT = "context_snapshot"


class ArtifactState(StrEnum):
    ACTIVE = "active"
    EXPERIMENTAL = "experimental"
    DEPRECATED = "deprecated"
    BROKEN = "broken"


class JobState(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class EventIngestRequest(BaseModel):
    source: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    source_event_id: str = Field(min_length=1)
    created_at: datetime | None = None
    task_id: str | None = None
    session_id: str | None = None
    policy_version: str | None = None
    rollout_step: int | None = None
    agent: dict[str, Any] = Field(default_factory=dict)
    base_model: str | None = None
    reward: float | None = None
    status: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class EventIngestResponse(BaseModel):
    event_id: str
    ingested: bool
    duplicate: bool


class ArtifactRegisterRequest(BaseModel):
    type: ArtifactType
    name: str = Field(min_length=1)
    uri: str = Field(min_length=1)
    manifest: dict[str, Any] = Field(default_factory=dict)
    lineage: dict[str, Any] = Field(default_factory=dict)
    compatibility: dict[str, Any] = Field(default_factory=dict)
    scores: dict[str, float] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    promoted: bool = False


class ArtifactResponse(BaseModel):
    artifact_id: str
    type: ArtifactType
    name: str
    version: int = Field(ge=1)
    state: ArtifactState
    uri: str
    manifest: dict[str, Any] = Field(default_factory=dict)
    compatibility: dict[str, Any] = Field(default_factory=dict)
    scores: dict[str, float] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    promoted: bool = False


class DatasetQuery(BaseModel):
    event_types: list[str] = Field(default_factory=list)
    status: list[str] = Field(default_factory=list)
    reward_min: float | None = None
    policy_version: str | None = None
    task_tags: list[str] = Field(default_factory=list)


class DatasetLimits(BaseModel):
    max_events: int = Field(default=10000, ge=1)
    max_traces: int = Field(default=50000, ge=1)


class DatasetCreateRequest(BaseModel):
    name: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    query: DatasetQuery = Field(default_factory=DatasetQuery)
    limits: DatasetLimits = Field(default_factory=DatasetLimits)


class DatasetCreateResponse(BaseModel):
    dataset_id: str
    artifact_id: str
    event_count: int = Field(ge=0)
    trace_count: int = Field(ge=0)


class JobCreateRequest(BaseModel):
    method: str = Field(min_length=1)
    job_type: str = Field(min_length=1)
    input_artifact_ids: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    priority: int = 100


class JobCreateResponse(BaseModel):
    job_id: str
    state: JobState


class WorkerClaimRequest(BaseModel):
    worker_id: str = Field(min_length=1)
    capabilities: list[str] = Field(default_factory=list)
    lease_seconds: int = Field(default=600, ge=1)


class WorkerClaimInputArtifact(BaseModel):
    artifact_id: str
    type: ArtifactType | str
    uri: str
    name: str | None = None


class WorkerClaimedJob(BaseModel):
    job_id: str
    lease_id: str
    job_type: str
    method: str
    input_artifacts: list[WorkerClaimInputArtifact] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    priority: int | None = None
    state: JobState | None = None


class WorkerClaimResponse(BaseModel):
    job: WorkerClaimedJob | None = None


class WorkerHeartbeatRequest(BaseModel):
    lease_id: str = Field(min_length=1)
    progress: float | None = Field(default=None, ge=0.0, le=1.0)
    message: str | None = None


class WorkerCompleteRequest(BaseModel):
    lease_id: str = Field(min_length=1)
    artifacts: list[ArtifactRegisterRequest] = Field(default_factory=list)
    report: dict[str, Any] = Field(default_factory=dict)


class WorkerFailRequest(BaseModel):
    lease_id: str = Field(min_length=1)
    error: str = Field(min_length=1)
    retryable: bool = True


class ContextLimits(BaseModel):
    max_memory_chars: int = Field(default=12000, ge=0)
    max_skill_bundles: int = Field(default=4, ge=0)
    max_adapters: int = Field(default=2, ge=0)


class ContextResolveRequest(BaseModel):
    task_id: str = Field(min_length=1)
    instruction: str = Field(min_length=1)
    agent: dict[str, Any] = Field(default_factory=dict)
    base_model: str | None = None
    policy_version: str | None = None
    rollout_step: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    limits: ContextLimits = Field(default_factory=ContextLimits)


class AdapterMergeSpec(BaseModel):
    base_model: str | None = None
    merge_mode: str = "reference_only"
    adapters: list[dict[str, Any]] = Field(default_factory=list)


class ContextResolveResponse(BaseModel):
    context_id: str
    memory: dict[str, Any] = Field(default_factory=dict)
    skills: list[dict[str, Any]] = Field(default_factory=list)
    adapter_merge_spec: AdapterMergeSpec = Field(default_factory=AdapterMergeSpec)
    selection: dict[str, Any] = Field(default_factory=dict)
