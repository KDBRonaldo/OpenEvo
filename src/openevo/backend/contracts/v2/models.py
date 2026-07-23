"""Strict data models for the Core Control API v2 authority boundary."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)


MAX_JAVASCRIPT_SAFE_INTEGER = (1 << 53) - 1
MAX_SNAPSHOT_ENTRIES = 100_000
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024 * 1024


class ContractModel(BaseModel):
    """Base for immutable, coercion-free, closed v2 contract objects."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


OpaqueId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
    ),
]
Sha256Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
SourceCommit = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{7,64}$")]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=256)]
Description = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
LogText = Annotated[str, StringConstraints(max_length=16_384)]
Cursor = Annotated[str, StringConstraints(min_length=1, max_length=512)]
StrongETag = Annotated[
    str,
    StringConstraints(min_length=66, max_length=66, pattern=r'^"[0-9a-f]{64}"$'),
]
UtcTimestamp = Annotated[
    str,
    StringConstraints(
        pattern=(
            r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
            r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z$"
        )
    ),
]
MimeType = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=127,
        pattern=r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$",
    ),
]

ExecutionModeV2: TypeAlias = Literal[
    "codex_subscription_transcript",
    "self_deployed",
]
CaptureModeV2: TypeAlias = Literal["transcript", "proxy"]
TransitionKindV2: TypeAlias = Literal[
    "run_result",
    "settings",
    "context_rebind",
    "historical_restore",
    "evolution_abandon",
]


class CursorPageV2(ContractModel):
    next_cursor: Cursor | None = None
    has_more: bool

    @model_validator(mode="after")
    def _cursor_matches_has_more(self) -> CursorPageV2:
        if self.has_more != (self.next_cursor is not None):
            raise ValueError("has_more must be true if and only if next_cursor is present")
        return self


class ContractOnlyResponseV2(ContractModel):
    schema_version: Literal["2"] = "2"
    code: Literal["contract_only_not_implemented"]
    message: Description


class ApiErrorV2(ContractModel):
    schema_version: Literal["2"] = "2"
    request_id: OpaqueId
    code: Annotated[
        str,
        StringConstraints(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$"),
    ]
    http_status: int = Field(ge=400, le=599)
    message: Description
    category: Literal[
        "system",
        "project",
        "task",
        "transition",
        "artifact",
        "service",
        "authentication",
        "contract",
        "internal",
    ]
    retryable: bool
    repair_action: Literal[
        "retry",
        "repair",
        "reconfigure",
        "user_action_required",
        "unsupported",
    ]
    next_action: Description


class WorkspaceSnapshotRefV2(ContractModel):
    schema_version: Literal["2"] = "2"
    workspace_snapshot_id: OpaqueId
    project_id: OpaqueId
    manifest_sha256: Sha256Digest
    entry_count: int = Field(ge=0, le=MAX_SNAPSHOT_ENTRIES)
    byte_size: int = Field(ge=0, le=MAX_SNAPSHOT_BYTES)


class EvolutionRevisionRefV2(ContractModel):
    schema_version: Literal["2"] = "2"
    evolution_revision_id: OpaqueId
    project_id: OpaqueId
    manifest_sha256: Sha256Digest
    artifact_count: int = Field(ge=0, le=128)


class RuntimeContextSnapshotRefV2(ContractModel):
    schema_version: Literal["2"] = "2"
    runtime_context_snapshot_id: OpaqueId
    project_id: OpaqueId
    evolution_revision_id: OpaqueId
    evolution_revision_manifest_sha256: Sha256Digest
    registry_sha256: Sha256Digest
    runtime_contract_sha256: Sha256Digest
    manifest_sha256: Sha256Digest


class EffectiveExecutionSnapshotRefV2(ContractModel):
    schema_version: Literal["2"] = "2"
    effective_execution_snapshot_id: OpaqueId
    project_id: OpaqueId
    execution_mode: ExecutionModeV2
    capture_mode: CaptureModeV2
    token_level_metrics_available: bool
    producer_id: OpaqueId
    snapshot_sha256: Sha256Digest

    @model_validator(mode="after")
    def _valid_capture_mode(self) -> EffectiveExecutionSnapshotRefV2:
        if self.execution_mode == "codex_subscription_transcript":
            if self.capture_mode != "transcript":
                raise ValueError("subscription execution requires transcript capture")
            if self.token_level_metrics_available:
                raise ValueError("subscription execution cannot expose token-level metrics")
        if self.capture_mode == "transcript" and self.token_level_metrics_available:
            raise ValueError("transcript capture cannot expose token-level metrics")
        return self


class ProjectHeadRefV2(ContractModel):
    schema_version: Literal["2"] = "2"
    project_head_id: OpaqueId
    project_id: OpaqueId
    generation: int = Field(ge=0, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    predecessor_project_head_id: OpaqueId | None
    workspace_snapshot: WorkspaceSnapshotRefV2
    evolution_revision: EvolutionRevisionRefV2
    runtime_context_snapshot: RuntimeContextSnapshotRefV2
    effective_execution_snapshot: EffectiveExecutionSnapshotRefV2
    registry_sha256: Sha256Digest
    manifest_sha256: Sha256Digest

    @model_validator(mode="after")
    def _bind_exact_composition(self) -> ProjectHeadRefV2:
        if self.generation == 0 and self.predecessor_project_head_id is not None:
            raise ValueError("generation zero must not have a predecessor project head")
        if self.generation != 0 and self.predecessor_project_head_id is None:
            raise ValueError("a nonzero generation requires a predecessor project head")
        if self.predecessor_project_head_id == self.project_head_id:
            raise ValueError("a project head cannot be its own predecessor")
        if self.workspace_snapshot.project_id != self.project_id:
            raise ValueError("workspace snapshot belongs to another project")
        if self.evolution_revision.project_id != self.project_id:
            raise ValueError("evolution revision belongs to another project")
        if self.runtime_context_snapshot.project_id != self.project_id:
            raise ValueError("runtime context belongs to another project")
        if self.effective_execution_snapshot.project_id != self.project_id:
            raise ValueError("effective execution snapshot belongs to another project")
        if (
            self.runtime_context_snapshot.evolution_revision_id
            != self.evolution_revision.evolution_revision_id
        ):
            raise ValueError("runtime context binds another evolution revision")
        if (
            self.runtime_context_snapshot.evolution_revision_manifest_sha256
            != self.evolution_revision.manifest_sha256
        ):
            raise ValueError("runtime context binds another evolution revision manifest")
        if self.runtime_context_snapshot.registry_sha256 != self.registry_sha256:
            raise ValueError("project head and runtime context registry digests differ")
        return self


class TaskAdmissionRefV2(ContractModel):
    schema_version: Literal["2"] = "2"
    task_admission_id: OpaqueId
    task_id: OpaqueId
    project_id: OpaqueId
    predecessor_project_head: ProjectHeadRefV2
    workspace_snapshot: WorkspaceSnapshotRefV2
    project_config_sha256: Sha256Digest
    task_envelope_sha256: Sha256Digest
    normalized_evolution_intent_sha256: Sha256Digest
    registry_sha256: Sha256Digest
    admission_sha256: Sha256Digest
    admitted_at: UtcTimestamp

    @model_validator(mode="after")
    def _bind_exact_admission(self) -> TaskAdmissionRefV2:
        if self.predecessor_project_head.project_id != self.project_id:
            raise ValueError("predecessor project head belongs to another project")
        if self.workspace_snapshot.project_id != self.project_id:
            raise ValueError("workspace snapshot belongs to another project")
        if self.registry_sha256 != self.predecessor_project_head.registry_sha256:
            raise ValueError("admission registry digest differs from the predecessor head")
        return self


class AttemptRefV2(ContractModel):
    schema_version: Literal["2"] = "2"
    attempt_id: OpaqueId
    ordinal: int = Field(ge=1, le=100)
    task_id: OpaqueId
    task_admission_id: OpaqueId
    admission_sha256: Sha256Digest
    project_id: OpaqueId
    predecessor_project_head_id: OpaqueId
    created_at: UtcTimestamp


class SuccessorTransitionRefV2(ContractModel):
    schema_version: Literal["2"] = "2"
    successor_transition_id: OpaqueId
    project_id: OpaqueId
    kind: TransitionKindV2
    predecessor_project_head: ProjectHeadRefV2
    expected_successor_generation: int = Field(
        ge=1, le=MAX_JAVASCRIPT_SAFE_INTEGER
    )
    plan_sha256: Sha256Digest
    task_admission: TaskAdmissionRefV2 | None
    accepted_attempt: AttemptRefV2 | None
    successor_project_head: ProjectHeadRefV2 | None

    @model_validator(mode="after")
    def _bind_transition_ownership(self) -> SuccessorTransitionRefV2:
        predecessor = self.predecessor_project_head
        if predecessor.project_id != self.project_id:
            raise ValueError("predecessor project head belongs to another project")
        if self.expected_successor_generation != predecessor.generation + 1:
            raise ValueError("expected successor generation must be adjacent")

        task_bound = self.kind in {"run_result", "evolution_abandon"}
        if task_bound and (self.task_admission is None or self.accepted_attempt is None):
            raise ValueError("a task-result transition requires admission and attempt")
        if not task_bound and (
            self.task_admission is not None or self.accepted_attempt is not None
        ):
            raise ValueError("this transition kind must not bind a task")

        if self.task_admission is not None and self.accepted_attempt is not None:
            admission = self.task_admission
            attempt = self.accepted_attempt
            if admission.project_id != self.project_id:
                raise ValueError("task admission belongs to another project")
            if admission.predecessor_project_head != predecessor:
                raise ValueError("task admission does not pin the transition predecessor")
            if (
                attempt.project_id != self.project_id
                or attempt.task_id != admission.task_id
                or attempt.task_admission_id != admission.task_admission_id
                or attempt.admission_sha256 != admission.admission_sha256
                or attempt.predecessor_project_head_id != predecessor.project_head_id
            ):
                raise ValueError("accepted attempt does not belong to the exact admission")

        if self.successor_project_head is not None:
            successor = self.successor_project_head
            if successor.project_id != self.project_id:
                raise ValueError("successor project head belongs to another project")
            if successor.generation != self.expected_successor_generation:
                raise ValueError("successor generation differs from the transition")
            if successor.predecessor_project_head_id != predecessor.project_head_id:
                raise ValueError("successor does not bind the transition predecessor")
        return self


class VersionResponseV2(ContractModel):
    schema_version: Literal["2"] = "2"
    api_major: Literal["2"] = "2"
    release_version: ShortText
    source_commit: SourceCommit


class HealthResponseV2(ContractModel):
    schema_version: Literal["2"] = "2"
    status: Literal["healthy", "degraded", "unavailable"]
    checked_at: UtcTimestamp


class SystemStatusV2(ContractModel):
    schema_version: Literal["2"] = "2"
    status: Literal["ready", "needs_attention", "unavailable"]
    release_version: ShortText
    source_commit: SourceCommit
    registry_sha256: Sha256Digest
    checked_at: UtcTimestamp


class ProjectCreateV2(ContractModel):
    schema_version: Literal["2"] = "2"
    display_name: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    project_config_sha256: Sha256Digest


class ProjectV2(ContractModel):
    schema_version: Literal["2"] = "2"
    project_id: OpaqueId
    display_name: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    project_config_sha256: Sha256Digest
    active_project_head: ProjectHeadRefV2 | None
    state: Literal["ready", "transitioning", "not_ready", "needs_attention"]
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    etag: StrongETag

    @model_validator(mode="after")
    def _head_belongs_to_project(self) -> ProjectV2:
        if (
            self.active_project_head is not None
            and self.active_project_head.project_id != self.project_id
        ):
            raise ValueError("active project head belongs to another project")
        return self


class ProjectPageV2(CursorPageV2):
    schema_version: Literal["2"] = "2"
    items: list[ProjectV2] = Field(max_length=100)


class ProjectHeadPageV2(CursorPageV2):
    schema_version: Literal["2"] = "2"
    items: list[ProjectHeadRefV2] = Field(max_length=100)


TransitionStateV2: TypeAlias = Literal[
    "pending",
    "sealing_dataset",
    "running_methods",
    "validating",
    "materializing",
    "committing",
    "committed",
    "failed",
    "cancelled",
    "superseded",
]


class SuccessorTransitionV2(ContractModel):
    schema_version: Literal["2"] = "2"
    transition: SuccessorTransitionRefV2
    state: TransitionStateV2
    progress_completed: int = Field(ge=0, le=10_000)
    progress_total: int = Field(ge=0, le=10_000)
    error: ApiErrorV2 | None
    created_at: UtcTimestamp
    updated_at: UtcTimestamp

    @model_validator(mode="after")
    def _valid_state(self) -> SuccessorTransitionV2:
        if self.progress_completed > self.progress_total:
            raise ValueError("transition progress exceeds total")
        if (self.state == "failed") != (self.error is not None):
            raise ValueError("error is required only for a failed transition")
        return self


class SuccessorTransitionPageV2(CursorPageV2):
    schema_version: Literal["2"] = "2"
    items: list[SuccessorTransitionV2] = Field(max_length=100)


class TaskSubmitRequestV2(ContractModel):
    schema_version: Literal["2"] = "2"
    project_id: OpaqueId
    expected_project_head_id: OpaqueId
    expected_project_head_manifest_sha256: Sha256Digest
    project_config_sha256: Sha256Digest
    task_envelope_sha256: Sha256Digest
    workspace_snapshot: WorkspaceSnapshotRefV2
    normalized_evolution_intent_sha256: Sha256Digest
    expected_registry_sha256: Sha256Digest

    @model_validator(mode="after")
    def _workspace_belongs_to_project(self) -> TaskSubmitRequestV2:
        if self.workspace_snapshot.project_id != self.project_id:
            raise ValueError("workspace snapshot belongs to another project")
        return self


TaskStateV2: TypeAlias = Literal[
    "admitted",
    "preparing",
    "running",
    "cancelling",
    "completed",
    "failed",
    "cancelled",
    "closed",
    "waiting_for_successor",
]


class TaskV2(ContractModel):
    schema_version: Literal["2"] = "2"
    task_id: OpaqueId
    project_id: OpaqueId
    admission: TaskAdmissionRefV2
    attempts: list[AttemptRefV2] = Field(max_length=100)
    authoritative_attempt_id: OpaqueId | None
    successor_transition: SuccessorTransitionRefV2 | None
    state: TaskStateV2
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    etag: StrongETag

    @model_validator(mode="after")
    def _bind_task_ownership(self) -> TaskV2:
        if (
            self.admission.task_id != self.task_id
            or self.admission.project_id != self.project_id
        ):
            raise ValueError("admission does not belong to this task")
        expected_ordinal = 1
        attempt_ids: set[str] = set()
        for attempt in self.attempts:
            if (
                attempt.task_id != self.task_id
                or attempt.project_id != self.project_id
                or attempt.task_admission_id != self.admission.task_admission_id
                or attempt.admission_sha256 != self.admission.admission_sha256
            ):
                raise ValueError("attempt does not belong to this task admission")
            if attempt.ordinal != expected_ordinal:
                raise ValueError("attempt ordinals must be contiguous")
            expected_ordinal += 1
            attempt_ids.add(attempt.attempt_id)
        if (
            self.authoritative_attempt_id is not None
            and self.authoritative_attempt_id not in attempt_ids
        ):
            raise ValueError("authoritative attempt is not part of this task")
        if self.successor_transition is not None:
            if self.authoritative_attempt_id is None:
                raise ValueError("a task transition requires an authoritative attempt")
            if self.successor_transition.project_id != self.project_id:
                raise ValueError("task transition belongs to another project")
        return self


class TaskPageV2(CursorPageV2):
    schema_version: Literal["2"] = "2"
    items: list[TaskV2] = Field(max_length=100)


class AttemptPageV2(CursorPageV2):
    schema_version: Literal["2"] = "2"
    items: list[AttemptRefV2] = Field(max_length=100)


class ActionRequestV2(ContractModel):
    schema_version: Literal["2"] = "2"
    expected_project_head_id: OpaqueId


class TaskActionRequestV2(ContractModel):
    schema_version: Literal["2"] = "2"
    task_admission_id: OpaqueId
    admission_sha256: Sha256Digest


class AttemptAppendRequestV2(TaskActionRequestV2):
    expected_previous_attempt_id: OpaqueId | None
    expected_next_ordinal: int = Field(ge=1, le=100)


class EventBaseV2(ContractModel):
    schema_version: Literal["2"] = "2"
    event_id: OpaqueId
    sequence: int = Field(ge=1, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    occurred_at: UtcTimestamp
    project_id: OpaqueId


class TaskAdmittedEventV2(EventBaseV2):
    event_type: Literal["task_admitted"]
    admission: TaskAdmissionRefV2


class AttemptAppendedEventV2(EventBaseV2):
    event_type: Literal["attempt_appended"]
    attempt: AttemptRefV2


class DatasetSealedEventV2(EventBaseV2):
    event_type: Literal["dataset_sealed"]
    task_id: OpaqueId
    task_admission_id: OpaqueId
    attempt_id: OpaqueId
    dataset_id: OpaqueId
    dataset_sha256: Sha256Digest


class TransitionChangedEventV2(EventBaseV2):
    event_type: Literal["transition_changed"]
    transition: SuccessorTransitionRefV2
    state: TransitionStateV2
    progress_completed: int = Field(ge=0, le=10_000)
    progress_total: int = Field(ge=0, le=10_000)


class EvolutionRevisionCommittedEventV2(EventBaseV2):
    event_type: Literal["evolution_revision_committed"]
    successor_transition_id: OpaqueId
    evolution_revision: EvolutionRevisionRefV2


class RuntimeContextCommittedEventV2(EventBaseV2):
    event_type: Literal["runtime_context_committed"]
    successor_transition_id: OpaqueId
    runtime_context_snapshot: RuntimeContextSnapshotRefV2


class ProjectHeadActivatedEventV2(EventBaseV2):
    event_type: Literal["project_head_activated"]
    successor_transition_id: OpaqueId
    project_head: ProjectHeadRefV2


EventEnvelopeV2: TypeAlias = Annotated[
    TaskAdmittedEventV2
    | AttemptAppendedEventV2
    | DatasetSealedEventV2
    | TransitionChangedEventV2
    | EvolutionRevisionCommittedEventV2
    | RuntimeContextCommittedEventV2
    | ProjectHeadActivatedEventV2,
    Field(discriminator="event_type"),
]


class SseFrameV2(ContractModel):
    id: OpaqueId
    event: Literal[
        "task_admitted",
        "attempt_appended",
        "dataset_sealed",
        "transition_changed",
        "evolution_revision_committed",
        "runtime_context_committed",
        "project_head_activated",
    ]
    data: EventEnvelopeV2
    retry: int | None = Field(default=None, ge=1000, le=60_000)

    @model_validator(mode="after")
    def _frame_matches_event(self) -> SseFrameV2:
        if self.id != self.data.event_id:
            raise ValueError("SSE frame ID differs from its event envelope")
        if self.event != self.data.event_type:
            raise ValueError("SSE event name differs from its event envelope")
        return self


class TimelinePageV2(CursorPageV2):
    schema_version: Literal["2"] = "2"
    items: list[EventEnvelopeV2] = Field(max_length=100)


class LogEntryV2(ContractModel):
    sequence: int = Field(ge=1, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    occurred_at: UtcTimestamp
    stream: Literal["system", "stdout", "stderr", "transcript"]
    message: LogText


class LogPageV2(CursorPageV2):
    schema_version: Literal["2"] = "2"
    items: list[LogEntryV2] = Field(max_length=100)


class TaskContextV2(ContractModel):
    schema_version: Literal["2"] = "2"
    task_id: OpaqueId
    task_admission_id: OpaqueId
    project_head: ProjectHeadRefV2
    workspace_snapshot: WorkspaceSnapshotRefV2


class ArtifactV2(ContractModel):
    schema_version: Literal["2"] = "2"
    artifact_id: OpaqueId
    project_id: OpaqueId
    artifact_type: Literal[
        "dataset",
        "workspace_result",
        "text_memory",
        "skill_bundle",
        "agent_system",
        "parametric_memory",
        "diagnostic",
    ]
    manifest_sha256: Sha256Digest
    byte_size: int = Field(ge=0, le=MAX_SNAPSHOT_BYTES)
    created_at: UtcTimestamp


class ArtifactPageV2(CursorPageV2):
    schema_version: Literal["2"] = "2"
    items: list[ArtifactV2] = Field(max_length=100)


class ArtifactContentV2(ContractModel):
    schema_version: Literal["2"] = "2"
    artifact: ArtifactV2
    media_type: MimeType
    content_sha256: Sha256Digest
    byte_size: int = Field(ge=0, le=MAX_SNAPSHOT_BYTES)


class ServiceV2(ContractModel):
    schema_version: Literal["2"] = "2"
    service_id: OpaqueId
    kind: Literal["daemon", "codex", "gateway", "worker", "runtime", "model"]
    status: Literal["ready", "starting", "stopping", "degraded", "unavailable"]
    updated_at: UtcTimestamp
    etag: StrongETag


class ServicePageV2(CursorPageV2):
    schema_version: Literal["2"] = "2"
    items: list[ServiceV2] = Field(max_length=100)


class OperationV2(ContractModel):
    schema_version: Literal["2"] = "2"
    operation_id: OpaqueId
    kind: Literal[
        "transition_retry",
        "transition_abandon",
        "attempt_cancel",
        "task_close",
        "service_restart",
        "diagnostic",
        "cache_cleanup",
    ]
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    progress_completed: int = Field(ge=0, le=10_000)
    progress_total: int = Field(ge=0, le=10_000)
    error: ApiErrorV2 | None
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    etag: StrongETag


class DiagnosticRequestV2(ContractModel):
    schema_version: Literal["2"] = "2"
    scope: Literal["system", "project", "task", "transition", "service"]
    resource_id: OpaqueId | None


class DiagnosticV2(ContractModel):
    schema_version: Literal["2"] = "2"
    diagnostic_id: OpaqueId
    scope: Literal["system", "project", "task", "transition", "service"]
    resource_id: OpaqueId | None
    status: Literal["queued", "running", "ready", "failed"]
    artifact_id: OpaqueId | None
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    etag: StrongETag


class CacheCleanupRequestV2(ContractModel):
    schema_version: Literal["2"] = "2"
    scope: Literal["safe_unreferenced"] = "safe_unreferenced"


__all__ = [
    "ActionRequestV2",
    "ApiErrorV2",
    "ArtifactContentV2",
    "ArtifactPageV2",
    "ArtifactV2",
    "AttemptAppendRequestV2",
    "AttemptPageV2",
    "AttemptRefV2",
    "CacheCleanupRequestV2",
    "ContractModel",
    "ContractOnlyResponseV2",
    "DiagnosticRequestV2",
    "DiagnosticV2",
    "EffectiveExecutionSnapshotRefV2",
    "EventEnvelopeV2",
    "EvolutionRevisionRefV2",
    "HealthResponseV2",
    "LogPageV2",
    "OperationV2",
    "ProjectCreateV2",
    "ProjectHeadPageV2",
    "ProjectHeadRefV2",
    "ProjectPageV2",
    "ProjectV2",
    "RuntimeContextSnapshotRefV2",
    "ServicePageV2",
    "ServiceV2",
    "SseFrameV2",
    "SuccessorTransitionPageV2",
    "SuccessorTransitionRefV2",
    "SuccessorTransitionV2",
    "SystemStatusV2",
    "TaskActionRequestV2",
    "TaskAdmissionRefV2",
    "TaskContextV2",
    "TaskPageV2",
    "TaskSubmitRequestV2",
    "TaskV2",
    "TimelinePageV2",
    "VersionResponseV2",
    "WorkspaceSnapshotRefV2",
]
