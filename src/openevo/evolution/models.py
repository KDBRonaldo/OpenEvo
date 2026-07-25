from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ArtifactType(StrEnum):
    TEXT_MEMORY = "text_memory"
    SKILL_BUNDLE = "skill_bundle"
    AGENT_SYSTEM = "agent_system"
    PARAMETRIC_MEMORY = "parametric_memory"
    DATASET = "dataset"
    REPORT = "report"
    CONTEXT_SNAPSHOT = "context_snapshot"


class ArtifactState(StrEnum):
    STAGED = "staged"
    SEALED = "sealed"
    ACTIVE = "active"
    EXPERIMENTAL = "experimental"
    DEPRECATED = "deprecated"
    BROKEN = "broken"


class ReviewType(StrEnum):
    PROMOTION = "promotion"
    COMPARISON = "comparison"
    CRITIQUE = "critique"
    ANNOTATION = "annotation"
    VALIDATION = "validation"
    QUERY_POLICY_AUDIT = "query_policy_audit"


class ReviewStatus(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    ASSIGNED = "assigned"
    IN_REVIEW = "in_review"
    SUBMITTED = "submitted"
    VALIDATED = "validated"
    ADJUDICATED = "adjudicated"
    RESOLVED = "resolved"
    STALE = "stale"
    NEEDS_REVISION = "needs_revision"
    REJECTED_INVALID = "rejected_invalid"
    CONFLICT = "conflict"
    ARCHIVED_ONLY = "archived_only"


class HumanFeedbackStatus(StrEnum):
    SUBMITTED = "submitted"
    VALIDATED = "validated"
    NORMALIZED = "normalized"
    REJECTED_INVALID = "rejected_invalid"
    REDACTED = "redacted"
    INDEXED = "indexed"
    AVAILABLE_FOR_EVOLUTION = "available_for_evolution"
    ARCHIVED_ONLY = "archived_only"
    CONSUMED = "consumed"


class HumanFeedbackDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REVISE = "revise"
    ABSTAIN = "abstain"
    PREFER_A = "prefer_a"
    PREFER_B = "prefer_b"
    TIE = "tie"
    COMMENT_ONLY = "comment_only"


class HumanQueryDecision(StrEnum):
    ASK_HUMAN = "ask_human"
    ASK_LLM = "ask_llm"
    AUTO_PROMOTE = "auto_promote"
    AUTO_REJECT = "auto_reject"
    RUN_MORE_EVAL = "run_more_eval"
    DEFER = "defer"


class FeedbackApplicationTargetType(StrEnum):
    PROMOTION_DECISION = "promotion_decision"
    PROMPT_SEED = "prompt_seed"
    MUTATION_CONSTRAINT = "mutation_constraint"
    NEGATIVE_CONSTRAINT = "negative_constraint"
    VALIDATION_CHECK = "validation_check"
    RANKING_SIGNAL = "ranking_signal"
    DATASET_RECORD = "dataset_record"
    AUDIT_NOTE = "audit_note"


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


class ArtifactPromotionUpdateRequest(BaseModel):
    promoted: bool


class DatasetQuery(BaseModel):
    source: str | None = Field(default=None, min_length=1, max_length=256)
    event_types: list[str] = Field(default_factory=list)
    status: list[str] = Field(default_factory=list)
    reward_min: float | None = None
    policy_version: str | None = None
    task_tags: list[str] = Field(default_factory=list)
    source_event_id: str | None = Field(default=None, min_length=1, max_length=256)
    task_id: str | None = Field(default=None, min_length=1, max_length=256)
    session_id: str | None = Field(default=None, min_length=1, max_length=256)


class DatasetLimits(BaseModel):
    max_events: int = Field(default=10000, ge=1)
    max_traces: int = Field(default=50000, ge=1)


class DatasetCreateRequest(BaseModel):
    idempotency_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
    )
    name: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    query: DatasetQuery = Field(default_factory=DatasetQuery)
    limits: DatasetLimits = Field(default_factory=DatasetLimits)


class DatasetCreateHttpRequest(DatasetCreateRequest):
    idempotency_key: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
    )


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
    method_capabilities: list[str] | None = None
    method_identity_capabilities: dict[str, str] | None = None
    lease_seconds: int = Field(default=600, ge=1)

    @field_validator("capabilities")
    @classmethod
    def _capabilities(cls, value: list[str]) -> list[str]:
        if len(value) > 256:
            raise ValueError("worker capabilities exceed the size limit")
        if any(
            not capability
            or len(capability) > 512
            or capability != capability.strip()
            for capability in value
        ):
            raise ValueError("worker capability is invalid")
        if len(value) != len(set(value)):
            raise ValueError("worker capabilities must be unique")
        return list(value)

    @field_validator("method_capabilities")
    @classmethod
    def _method_capabilities(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if len(value) > 256:
            raise ValueError("method capabilities exceed the size limit")
        if any(
            not method_id
            or len(method_id) > 128
            or method_id != method_id.strip()
            for method_id in value
        ):
            raise ValueError("method capability ID is invalid")
        if len(value) != len(set(value)):
            raise ValueError("method capabilities must be unique")
        return list(value)

    @field_validator("method_identity_capabilities")
    @classmethod
    def _method_identity_capabilities(
        cls,
        value: dict[str, str] | None,
    ) -> dict[str, str] | None:
        if value is None:
            return None
        if len(value) > 256:
            raise ValueError("method identity capabilities exceed the size limit")
        for method_id, digest in value.items():
            if not method_id or len(method_id) > 128 or method_id != method_id.strip():
                raise ValueError("method identity capability ID is invalid")
            if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise ValueError("method identity capability must be a lowercase SHA-256")
        return dict(value)

    @model_validator(mode="after")
    def _matching_method_capabilities(self) -> WorkerClaimRequest:
        if (
            self.method_capabilities is not None
            and self.method_identity_capabilities is not None
            and set(self.method_capabilities) != set(self.method_identity_capabilities)
        ):
            raise ValueError("method capabilities and identities must name the same methods")
        return self


class WorkerClaimInputArtifact(BaseModel):
    artifact_id: str
    type: ArtifactType | str
    uri: str
    name: str | None = None
    manifest_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    records_byte_size: int | None = Field(default=None, ge=0)
    records_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def _paired_dataset_records_identity(self) -> WorkerClaimInputArtifact:
        if (self.records_byte_size is None) != (self.records_sha256 is None):
            raise ValueError(
                "dataset records byte size and digest must be supplied together"
            )
        return self


class WorkerClaimedJob(BaseModel):
    job_id: str
    lease_id: str
    job_type: str
    method: str
    input_artifacts: list[WorkerClaimInputArtifact] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    priority: int | None = None
    state: JobState | None = None
    plan: dict[str, Any] | None = None
    target_id: str | None = None
    registry_snapshot_digest: str | None = None
    method_identity_digest: str | None = None
    execution_envelope: dict[str, Any] | None = None
    execution_envelope_digest: str | None = None


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
    max_agent_system_chars: int = Field(default=12000, ge=0)
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
    agent_system: dict[str, Any] = Field(default_factory=dict)
    skills: list[dict[str, Any]] = Field(default_factory=list)
    adapter_merge_spec: AdapterMergeSpec = Field(default_factory=AdapterMergeSpec)
    selection: dict[str, Any] = Field(default_factory=dict)


class ReviewPacket(BaseModel):
    model_config = ConfigDict(extra="allow")

    trusted_metadata: dict[str, Any] = Field(default_factory=dict)
    untrusted_artifact_excerpts: list[dict[str, Any]] = Field(default_factory=list)
    promotion_support: dict[str, Any] = Field(default_factory=dict)
    questions: list[str] = Field(default_factory=list)

    def __getitem__(self, key: str) -> Any:
        return self.model_dump(mode="python")[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.model_dump(mode="python").get(key, default)


class ReviewPacketResponse(BaseModel):
    packet_id: str
    packet_hash: str
    packet: ReviewPacket = Field(default_factory=ReviewPacket)
    created_at: str


class ReviewRequestCreateRequest(BaseModel):
    review_type: ReviewType
    artifact_ids: list[str] = Field(default_factory=list)
    candidate_ids: list[str] = Field(default_factory=list)
    job_id: str | None = None
    task_id: str | None = None
    round_index: int | None = None
    method: str | None = None
    artifact_type: str | None = None
    packet: ReviewPacket = Field(default_factory=ReviewPacket)
    artifact_hashes: dict[str, str] = Field(default_factory=dict)
    query_decision_id: str | None = None
    query_decision: dict[str, Any] | None = None
    priority: int = 100

    @model_validator(mode="after")
    def _require_review_target(self) -> "ReviewRequestCreateRequest":
        if not any(str(item).strip() for item in self.artifact_ids) and not any(
            str(item).strip() for item in self.candidate_ids
        ):
            raise ValueError("review request must include artifact_ids or candidate_ids")
        return self


class ReviewRequestResponse(BaseModel):
    review_id: str
    review_type: ReviewType
    status: ReviewStatus
    artifact_ids: list[str] = Field(default_factory=list)
    candidate_ids: list[str] = Field(default_factory=list)
    job_id: str | None = None
    task_id: str | None = None
    round_index: int | None = None
    method: str | None = None
    artifact_type: str | None = None
    packet_id: str
    packet_hash: str
    packet: ReviewPacket = Field(default_factory=ReviewPacket)
    artifact_hashes: dict[str, str] = Field(default_factory=dict)
    query_decision_id: str | None = None
    assigned_to: str | None = None
    reviewer_role: str | None = None
    adjudication_rationale: str | None = None
    priority: int = 100
    created_at: str
    updated_at: str


class ReviewClaimRequest(BaseModel):
    reviewer_id: str = Field(min_length=1)
    reviewer_role: str | None = None


class HumanFeedbackCreateRequest(BaseModel):
    reviewer_id: str = Field(min_length=1)
    reviewer_role: str | None = None
    decision: HumanFeedbackDecision
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    rationale: str | None = None
    observed_issues: list[str] = Field(default_factory=list)
    suggested_changes: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    validation_checks: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    raw_payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("score", "confidence", mode="before")
    @classmethod
    def _reject_boolean_contract_scores(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("boolean values are not valid numeric contract scores")
        return value


class HumanFeedbackResponse(BaseModel):
    feedback_id: str
    review_id: str
    reviewer_id: str
    reviewer_role: str | None = None
    status: HumanFeedbackStatus
    decision: HumanFeedbackDecision
    score: float | None = None
    confidence: float | None = None
    rationale: str = ""
    normalized_payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class ReviewAdjudicationRequest(BaseModel):
    status: ReviewStatus = ReviewStatus.ADJUDICATED
    rationale: str | None = None


class FeedbackApplicationCreateRequest(BaseModel):
    feedback_id: str = Field(min_length=1)
    target_type: FeedbackApplicationTargetType
    target_id: str = Field(min_length=1)
    consumed_by_method: str = Field(min_length=1)
    consumed_in_job_id: str | None = None
    effect_summary: str = Field(min_length=1)


class FeedbackApplicationResponse(BaseModel):
    application_id: str
    feedback_id: str
    target_type: FeedbackApplicationTargetType
    target_id: str
    consumed_by_method: str
    consumed_in_job_id: str | None = None
    effect_summary: str
    created_at: str


class HumanQueryDecisionCreateRequest(BaseModel):
    artifact_ids: list[str] = Field(default_factory=list)
    candidate_ids: list[str] = Field(default_factory=list)
    task_id: str | None = None
    round_index: int | None = None
    method: str | None = None
    decision: HumanQueryDecision
    reason_codes: list[str] = Field(default_factory=list)
    estimated_value_of_information: float | None = Field(default=None, ge=0.0)
    estimated_human_cost: float | None = Field(default=None, ge=0.0)
    budget_context: dict[str, Any] = Field(default_factory=dict)


class HumanQueryDecisionResponse(BaseModel):
    query_decision_id: str
    artifact_ids: list[str] = Field(default_factory=list)
    candidate_ids: list[str] = Field(default_factory=list)
    task_id: str | None = None
    round_index: int | None = None
    method: str | None = None
    decision: HumanQueryDecision
    reason_codes: list[str] = Field(default_factory=list)
    estimated_value_of_information: float | None = None
    estimated_human_cost: float | None = None
    budget_context: dict[str, Any] = Field(default_factory=dict)
    actual_latency_seconds: float | None = None
    feedback_changed_promotion: bool | None = None
    feedback_changed_next_candidate: bool | None = None
    downstream_delta: float | None = None
    review_id: str | None = None
    created_at: str
