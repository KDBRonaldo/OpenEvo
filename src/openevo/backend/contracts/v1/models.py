"""Strict data models for the Core Control API v1 product boundary."""

from __future__ import annotations

import json
import math
from enum import StrEnum
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StringConstraints,
    field_validator,
    model_validator,
)


MAX_CANONICAL_JSON_BYTES = 256 * 1024
MAX_CANONICAL_JSON_DEPTH = 16
MAX_CANONICAL_JSON_NODES = 8192
MAX_CANONICAL_JSON_COLLECTION_ITEMS = 4096
MAX_JAVASCRIPT_SAFE_INTEGER = (1 << 53) - 1


class ContractModel(BaseModel):
    """Base for immutable, coercion-free, closed contract objects."""

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
        pattern=r"^[^\x00-\x20\x7f](?:[^\x00-\x1f\x7f]*[^\x00-\x20\x7f])?$",
    ),
]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=256)]
DisplayName = Annotated[str, StringConstraints(min_length=1, max_length=128)]
Description = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
LogText = Annotated[str, StringConstraints(max_length=16_384)]
ContentText = Annotated[str, StringConstraints(max_length=2 * 1024 * 1024)]
Sha256Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
SourceCommit = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{7,64}$")]
UtcTimestamp = Annotated[
    str,
    StringConstraints(
        pattern=(
            r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
            r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z$"
        )
    ),
]
Cursor = Annotated[str, StringConstraints(min_length=1, max_length=512)]
MimeType = Annotated[
    str,
    StringConstraints(
        max_length=127,
        pattern=r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$",
    ),
]


def _canonical_json_object(value: str) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) > MAX_CANONICAL_JSON_BYTES:
        raise ValueError("canonical JSON exceeds the byte limit")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("must contain canonical JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError("canonical JSON value must be an object")
    if (
        json.dumps(
            decoded,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        != value
    ):
        raise ValueError("must contain canonical JSON with sorted keys")
    stack: list[tuple[object, int]] = [(decoded, 1)]
    nodes = 0
    collection_items = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_CANONICAL_JSON_NODES:
            raise ValueError("canonical JSON exceeds the node limit")
        if depth > MAX_CANONICAL_JSON_DEPTH:
            raise ValueError("canonical JSON exceeds the depth limit")
        if current is None or isinstance(current, bool | str):
            continue
        if isinstance(current, int):
            if abs(current) > MAX_JAVASCRIPT_SAFE_INTEGER:
                raise ValueError("integer exceeds the JavaScript safe range")
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                raise ValueError("numbers must be finite")
            if current.is_integer() and abs(current) > MAX_JAVASCRIPT_SAFE_INTEGER:
                raise ValueError("integer exceeds the JavaScript safe range")
            continue
        if isinstance(current, dict):
            collection_items += len(current)
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            collection_items += len(current)
            stack.extend((item, depth + 1) for item in current)
        else:
            raise ValueError("canonical JSON contains an unsupported value")
        if collection_items > MAX_CANONICAL_JSON_COLLECTION_ITEMS:
            raise ValueError("canonical JSON exceeds the collection item limit")
    return value


CanonicalJsonObject = Annotated[
    str,
    StringConstraints(min_length=2, max_length=MAX_CANONICAL_JSON_BYTES),
    AfterValidator(_canonical_json_object),
]


class ErrorSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"


class ErrorCategory(StrEnum):
    ENVIRONMENT = "environment"
    PROJECT = "project"
    RUN = "run"
    ARTIFACT = "artifact"
    SERVICE = "service"
    AUTHENTICATION = "authentication"
    CONTRACT = "contract"
    INTERNAL = "internal"


class RepairAction(StrEnum):
    OPENEVO_CAN_RETRY = "openevo_can_retry"
    OPENEVO_CAN_INSTALL = "openevo_can_install"
    OPENEVO_CAN_RECONFIGURE = "openevo_can_reconfigure"
    USER_ACTION_REQUIRED = "user_action_required"
    UNSUPPORTED = "unsupported"


class ErrorFieldIssueV1(ContractModel):
    field: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    issue: ShortText


class ErrorConflictV1(ContractModel):
    resource_type: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    resource_id: OpaqueId


class ApiErrorDetailsV1(ContractModel):
    field_issues: list[ErrorFieldIssueV1] = Field(default_factory=list, max_length=64)
    conflicts: list[ErrorConflictV1] = Field(default_factory=list, max_length=32)


class ApiErrorV1(ContractModel):
    schema_version: Literal["1"] = "1"
    request_id: OpaqueId
    code: Annotated[
        str,
        StringConstraints(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$"),
    ]
    http_status: int = Field(ge=400, le=599)
    message: Description
    severity: ErrorSeverity
    category: ErrorCategory
    retryable: bool
    repair_action: RepairAction
    next_action: Description
    details: ApiErrorDetailsV1 = Field(default_factory=ApiErrorDetailsV1)
    logs_ref: OpaqueId | None = None


class ProviderKind(StrEnum):
    OPENEVO_CORE = "openevo_core"
    CONTRACT_SIMULATOR = "contract_simulator"
    SCAFFOLD = "scaffold"
    DRY_RUN = "dry_run"


class BuildChannel(StrEnum):
    RELEASE = "release"
    DEVELOPMENT = "development"
    TEST = "test"


class FeatureFlag(StrEnum):
    PROJECTS = "projects"
    WORKSPACE_SYNC = "workspace_sync"
    VERIFIED_CAPABILITIES = "verified_capabilities"
    TRANSCRIPT_CAPTURE = "transcript_capture"
    TOKEN_LEVEL_CAPTURE = "token_level_capture"
    CROSS_SESSION_REVISIONS = "cross_session_revisions"
    NON_PARAMETRIC_EVOLUTION = "non_parametric_evolution"
    PARAMETRIC_MEMORY_RESERVED = "parametric_memory_reserved"
    SSE_REPLAY = "sse_replay"
    DIAGNOSTICS = "diagnostics"


class VersionResponseV1(ContractModel):
    schema_version: Literal["1"] = "1"
    preferred_major: Literal[1]
    supported_majors: list[Literal[1]] = Field(min_length=1, max_length=8)
    openapi_sha256: Sha256Digest
    build_version: ShortText
    source_commit: SourceCommit
    build_channel: BuildChannel
    provider_kind: ProviderKind
    features: list[FeatureFlag] = Field(max_length=32)

    @field_validator("supported_majors", "features")
    @classmethod
    def _unique_list(cls, value: list[object]) -> list[object]:
        if len(value) != len(set(value)):
            raise ValueError("items must be unique")
        return value


class HealthStatus(StrEnum):
    OK = "ok"
    STARTING = "starting"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class HealthResponseV1(ContractModel):
    schema_version: Literal["1"] = "1"
    status: HealthStatus
    ready: bool
    checked_at: UtcTimestamp


class ExecutionMode(StrEnum):
    CODEX_SUBSCRIPTION_TRANSCRIPT = "codex_subscription_transcript"
    SELF_DEPLOYED = "self-deployed"


class CaptureMode(StrEnum):
    TRANSCRIPT = "transcript"
    TOKEN_LEVEL = "token_level"


class ServiceKind(StrEnum):
    CONTROL = "control"
    GATEWAY = "gateway"
    INFERENCE = "inference"
    EVOLUTION_WORKER = "evolution_worker"
    ARTIFACT_STORE = "artifact_store"


class ServiceStatus(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class ServiceSummaryV1(ContractModel):
    id: OpaqueId
    display_name: DisplayName
    kind: ServiceKind
    status: ServiceStatus
    restartable: bool
    status_message: ShortText | None = None
    updated_at: UtcTimestamp


class RegistryStatus(StrEnum):
    VERIFIED = "verified"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


class CoreStatusV1(ContractModel):
    schema_version: Literal["1"] = "1"
    status: HealthStatus
    registry_status: RegistryStatus
    registry_digest: Sha256Digest | None = None
    active_runs: int = Field(ge=0, le=100_000)
    queued_runs: int = Field(ge=0, le=100_000)
    services: list[ServiceSummaryV1] = Field(max_length=64)
    checked_at: UtcTimestamp

    @model_validator(mode="after")
    def _verified_registry_has_digest(self) -> CoreStatusV1:
        if (self.registry_status is RegistryStatus.VERIFIED) != (self.registry_digest is not None):
            raise ValueError("only a verified registry has a registry digest")
        return self


class EnvironmentCheckKind(StrEnum):
    PYTHON = "python"
    CONTAINER_RUNTIME = "container_runtime"
    CODEX_SUBSCRIPTION = "codex_subscription"
    MODEL_SERVICE = "model_service"
    NETWORK = "network"
    STORAGE = "storage"
    REGISTRY = "registry"


class CheckStatus(StrEnum):
    OK = "ok"
    WARNING = "warning"
    BLOCKING = "blocking"
    UNAVAILABLE = "unavailable"


class EnvironmentDoctorRequestV1(ContractModel):
    schema_version: Literal["1"] = "1"
    execution_mode: ExecutionMode
    checks: list[EnvironmentCheckKind] = Field(default_factory=list, max_length=16)

    @field_validator("checks")
    @classmethod
    def _unique_checks(cls, value: list[EnvironmentCheckKind]) -> list[EnvironmentCheckKind]:
        if len(value) != len(set(value)):
            raise ValueError("checks must be unique")
        return value


class EnvironmentCheckV1(ContractModel):
    id: OpaqueId
    kind: EnvironmentCheckKind
    status: CheckStatus
    message: Description
    repair_action: RepairAction
    next_action: Description | None = None
    logs_ref: OpaqueId | None = None


class DoctorStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    NEEDS_USER_ACTION = "needs_user_action"


class EnvironmentDoctorResponseV1(ContractModel):
    schema_version: Literal["1"] = "1"
    status: DoctorStatus
    checks: list[EnvironmentCheckV1] = Field(max_length=64)
    checked_at: UtcTimestamp


class EnvironmentRepairAction(StrEnum):
    RETRY_NETWORK = "retry_network"
    RESTART_CONTAINER_RUNTIME = "restart_container_runtime"
    RESTART_MODEL_SERVICE = "restart_model_service"
    REPAIR_REGISTRY_INSTALL = "repair_registry_install"
    RECONCILE_MANAGED_STATE = "reconcile_managed_state"


class EnvironmentRepairRequestV1(ContractModel):
    schema_version: Literal["1"] = "1"
    execution_mode: ExecutionMode
    actions: list[EnvironmentRepairAction] = Field(min_length=1, max_length=16)

    @field_validator("actions")
    @classmethod
    def _unique_actions(
        cls, value: list[EnvironmentRepairAction]
    ) -> list[EnvironmentRepairAction]:
        if len(value) != len(set(value)):
            raise ValueError("actions must be unique")
        return value


class RepairActionResultV1(ContractModel):
    action: EnvironmentRepairAction
    status: CheckStatus
    message: Description


class EnvironmentRepairResponseV1(ContractModel):
    schema_version: Literal["1"] = "1"
    status: DoctorStatus
    results: list[RepairActionResultV1] = Field(min_length=1, max_length=16)
    checked_at: UtcTimestamp


class SupportStatus(StrEnum):
    SUPPORTED = "supported"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"


class SupportReason(StrEnum):
    AVAILABLE = "available"
    EXECUTION_MODE_UNSUPPORTED = "execution_mode_unsupported"
    CAPTURE_MODE_UNSUPPORTED = "capture_mode_unsupported"
    HARNESS_UNSUPPORTED = "harness_unsupported"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    MODEL_SERVICE_UNHEALTHY = "model_service_unhealthy"
    RELEASE_DISABLED = "release_disabled"


class CapabilitySupportAxisV1(ContractModel):
    status: SupportStatus
    reason: SupportReason
    message: Description | None = None


class MethodCapabilityV1(ContractModel):
    method_id: OpaqueId
    display_name: DisplayName
    description: Description
    maturity: Literal["stable", "experimental"]
    config_schema_json: CanonicalJsonObject
    default_config_json: CanonicalJsonObject
    implementation_identity_digest: Sha256Digest
    execution: CapabilitySupportAxisV1
    capture: CapabilitySupportAxisV1
    harness: CapabilitySupportAxisV1
    runtime: CapabilitySupportAxisV1


class ResolvedMethodCapabilityV1(ContractModel):
    method_id: OpaqueId
    implementation_identity_digest: Sha256Digest
    execution: CapabilitySupportAxisV1
    capture: CapabilitySupportAxisV1
    harness: CapabilitySupportAxisV1
    runtime: CapabilitySupportAxisV1


class SelectionResolverCapabilityV1(ContractModel):
    selection_value: OpaqueId
    display_name: DisplayName
    resolved_methods: list[ResolvedMethodCapabilityV1] = Field(min_length=1, max_length=256)


class ArtifactType(StrEnum):
    TEXT_MEMORY = "text_memory"
    SKILL_BUNDLE = "skill_bundle"
    AGENT_SYSTEM = "agent_system"
    PARAMETRIC_MEMORY = "parametric_memory"


class TargetCapabilityV1(ContractModel):
    target_id: OpaqueId
    display_name: DisplayName
    description: Description
    artifact_type: ArtifactType
    configured_default_method_id: OpaqueId
    effective_default_method_id: OpaqueId | None
    methods: list[MethodCapabilityV1] = Field(max_length=256)
    accepted_methods: list[ResolvedMethodCapabilityV1] = Field(min_length=1, max_length=256)
    selection_resolvers: list[SelectionResolverCapabilityV1] = Field(max_length=64)


class CapabilitiesResponseV1(ContractModel):
    schema_version: Literal["1"] = "1"
    core_version: ShortText
    registry_digest: Sha256Digest
    execution_mode: ExecutionMode
    capture_mode: CaptureMode
    harness_id: OpaqueId
    targets: list[TargetCapabilityV1] = Field(max_length=128)


class EvolutionTargetSelectionV1(ContractModel):
    target_id: OpaqueId
    enabled: bool
    method_id: OpaqueId | None = None
    config_json: CanonicalJsonObject = "{}"

    @model_validator(mode="after")
    def _enabled_target_has_method(self) -> EvolutionTargetSelectionV1:
        if self.enabled and self.method_id is None:
            raise ValueError("an enabled target requires a method_id")
        return self


class ProjectSpecV1(ContractModel):
    execution_mode: ExecutionMode
    capture_mode: CaptureMode
    harness_id: OpaqueId
    agent_model_ref: OpaqueId
    evolution_targets: list[EvolutionTargetSelectionV1] = Field(max_length=128)

    @field_validator("evolution_targets")
    @classmethod
    def _unique_targets(
        cls, value: list[EvolutionTargetSelectionV1]
    ) -> list[EvolutionTargetSelectionV1]:
        target_ids = [target.target_id for target in value]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("target IDs must be unique")
        return value

    @model_validator(mode="after")
    def _subscription_requires_transcript(self) -> ProjectSpecV1:
        if (
            self.execution_mode is ExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT
            and self.capture_mode is not CaptureMode.TRANSCRIPT
        ):
            raise ValueError("subscription execution requires transcript capture")
        return self


class ProjectStatus(StrEnum):
    DRAFT = "draft"
    READY = "ready"
    BLOCKED = "blocked"
    ARCHIVED = "archived"


class ProjectCreateV1(ContractModel):
    schema_version: Literal["1"] = "1"
    name: DisplayName
    description: Description | None = None
    spec: ProjectSpecV1


class ProjectPatchV1(ContractModel):
    schema_version: Literal["1"] = "1"
    name: DisplayName | None = None
    description: Description | None = None
    spec: ProjectSpecV1 | None = None

    @model_validator(mode="after")
    def _has_change(self) -> ProjectPatchV1:
        if self.name is None and self.description is None and self.spec is None:
            raise ValueError("project patch must contain a change")
        return self


class ProjectSummaryV1(ContractModel):
    id: OpaqueId
    name: DisplayName
    description: Description | None = None
    status: ProjectStatus
    execution_mode: ExecutionMode
    current_project_snapshot_id: OpaqueId
    current_workspace_snapshot_id: OpaqueId | None = None
    registry_digest: Sha256Digest | None = None
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    etag: ShortText


class ProjectV1(ProjectSummaryV1):
    spec: ProjectSpecV1


class ProjectPageV1(ContractModel):
    schema_version: Literal["1"] = "1"
    items: list[ProjectSummaryV1] = Field(max_length=100)
    next_cursor: Cursor | None = None
    has_more: bool


class WorkspaceSyncRequestV1(ContractModel):
    schema_version: Literal["1"] = "1"
    upload_id: OpaqueId
    content_sha256: Sha256Digest
    expected_workspace_snapshot_id: OpaqueId | None = None


class WorkspaceSnapshotV1(ContractModel):
    id: OpaqueId
    project_id: OpaqueId
    content_sha256: Sha256Digest
    created_at: UtcTimestamp


class ValidationCheckV1(ContractModel):
    id: OpaqueId
    status: CheckStatus
    message: Description
    target_id: OpaqueId | None = None
    method_id: OpaqueId | None = None


class ProjectValidationRequestV1(ContractModel):
    schema_version: Literal["1"] = "1"
    project_snapshot_id: OpaqueId
    workspace_snapshot_id: OpaqueId
    expected_registry_digest: Sha256Digest


class ProjectValidationResponseV1(ContractModel):
    schema_version: Literal["1"] = "1"
    valid: bool
    registry_digest: Sha256Digest
    checks: list[ValidationCheckV1] = Field(max_length=256)
    validated_at: UtcTimestamp


class RunCreateV1(ContractModel):
    schema_version: Literal["1"] = "1"
    project_id: OpaqueId
    project_snapshot_id: OpaqueId
    task_snapshot_id: OpaqueId
    workspace_snapshot_id: OpaqueId
    expected_registry_digest: Sha256Digest
    required_revision_id: OpaqueId
    execution_mode: ExecutionMode
    capture_mode: CaptureMode

    @model_validator(mode="after")
    def _subscription_requires_transcript(self) -> RunCreateV1:
        if (
            self.execution_mode is ExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT
            and self.capture_mode is not CaptureMode.TRANSCRIPT
        ):
            raise ValueError("subscription execution requires transcript capture")
        return self


class RunStatus(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class QueuedReason(StrEnum):
    ADMISSION_PENDING = "admission_pending"
    CAPACITY = "capacity"
    SERVICE_STARTING = "service_starting"
    REQUIRED_REVISION_UNCOMMITTED = "required_revision_uncommitted"


_TERMINAL_RUN_STATES = frozenset({RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED})


class AttemptV1(ContractModel):
    id: OpaqueId
    run_id: OpaqueId
    number: int = Field(ge=1, le=10_000)
    status: RunStatus
    queued_reason: QueuedReason | None = None
    created_at: UtcTimestamp
    started_at: UtcTimestamp | None = None
    finished_at: UtcTimestamp | None = None
    error: ApiErrorV1 | None = None

    @model_validator(mode="after")
    def _valid_state_shape(self) -> AttemptV1:
        if (self.status is RunStatus.QUEUED) != (self.queued_reason is not None):
            raise ValueError("queued_reason is required only for queued attempts")
        if (self.status in _TERMINAL_RUN_STATES) != (self.finished_at is not None):
            raise ValueError("finished_at is required only for terminal attempts")
        if (
            self.status
            in {
                RunStatus.RUNNING,
                RunStatus.CANCELLING,
                RunStatus.SUCCEEDED,
                RunStatus.FAILED,
            }
            and self.started_at is None
        ):
            raise ValueError("started_at is required after an attempt starts")
        if (self.status is RunStatus.FAILED) != (self.error is not None):
            raise ValueError("error is required only for failed attempts")
        return self


class RevisionTransitionState(StrEnum):
    NOT_STARTED = "not_started"
    SEALING_DATASET = "sealing_dataset"
    RUNNING_METHODS = "running_methods"
    VALIDATING = "validating"
    MATERIALIZING = "materializing"
    COMMITTING = "committing"
    ACTIVE = "active"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class RevisionTransitionV1(ContractModel):
    state: RevisionTransitionState
    predecessor_revision_id: OpaqueId
    successor_revision_id: OpaqueId | None = None
    progress_completed: int = Field(ge=0, le=10_000)
    progress_total: int = Field(ge=0, le=10_000)
    message: Description
    error: ApiErrorV1 | None = None

    @model_validator(mode="after")
    def _valid_transition_shape(self) -> RevisionTransitionV1:
        if self.progress_completed > self.progress_total:
            raise ValueError("transition progress exceeds total")
        if (self.state is RevisionTransitionState.FAILED) != (self.error is not None):
            raise ValueError("error is required only for failed transitions")
        if self.state is RevisionTransitionState.ACTIVE and self.successor_revision_id is None:
            raise ValueError("an active transition requires a successor revision")
        return self


class RunSummaryV1(ContractModel):
    id: OpaqueId
    project_id: OpaqueId
    project_snapshot_id: OpaqueId
    task_snapshot_id: OpaqueId
    workspace_snapshot_id: OpaqueId
    status: RunStatus
    queued_reason: QueuedReason | None = None
    current_attempt_id: OpaqueId | None = None
    attempt_count: int = Field(ge=0, le=10_000)
    pinned_revision_id: OpaqueId | None = None
    required_revision_id: OpaqueId
    created_at: UtcTimestamp
    started_at: UtcTimestamp | None = None
    finished_at: UtcTimestamp | None = None

    @model_validator(mode="after")
    def _valid_state_shape(self) -> RunSummaryV1:
        if (self.status is RunStatus.QUEUED) != (self.queued_reason is not None):
            raise ValueError("queued_reason is required only for queued runs")
        if (self.status in _TERMINAL_RUN_STATES) != (self.finished_at is not None):
            raise ValueError("finished_at is required only for terminal runs")
        if (
            self.status
            in {
                RunStatus.RUNNING,
                RunStatus.CANCELLING,
                RunStatus.SUCCEEDED,
                RunStatus.FAILED,
            }
            and self.started_at is None
        ):
            raise ValueError("started_at is required after a run starts")
        if (self.attempt_count == 0) != (self.current_attempt_id is None):
            raise ValueError("current_attempt_id must match attempt_count")
        if self.pinned_revision_id is not None and (
            self.pinned_revision_id != self.required_revision_id
        ):
            raise ValueError("a run may pin only its required revision")
        return self


class RunV1(RunSummaryV1):
    execution_mode: ExecutionMode
    capture_mode: CaptureMode
    registry_digest: Sha256Digest
    attempts: list[AttemptV1] = Field(max_length=100)
    revision_transition: RevisionTransitionV1

    @model_validator(mode="after")
    def _attempts_match_summary(self) -> RunV1:
        if len(self.attempts) != self.attempt_count:
            raise ValueError("attempt_count does not match attempts")
        if len({attempt.id for attempt in self.attempts}) != len(self.attempts):
            raise ValueError("attempt IDs must be unique")
        if any(attempt.run_id != self.id for attempt in self.attempts):
            raise ValueError("attempt belongs to another run")
        if self.attempts and self.attempts[-1].id != self.current_attempt_id:
            raise ValueError("current_attempt_id must identify the last attempt")
        return self


class RunPageV1(ContractModel):
    schema_version: Literal["1"] = "1"
    items: list[RunSummaryV1] = Field(max_length=100)
    next_cursor: Cursor | None = None
    has_more: bool


class RunCancelReason(StrEnum):
    USER_REQUESTED = "user_requested"
    PROJECT_DEACTIVATED = "project_deactivated"


class RunCancelRequestV1(ContractModel):
    schema_version: Literal["1"] = "1"
    reason: RunCancelReason


class RunRetryRequestV1(ContractModel):
    schema_version: Literal["1"] = "1"
    terminal_attempt_id: OpaqueId


class TimelinePhase(StrEnum):
    ADMISSION = "admission"
    PREPARATION = "preparation"
    EXECUTION = "execution"
    CAPTURE = "capture"
    DATASET = "dataset"
    EVOLUTION = "evolution"
    MATERIALIZATION = "materialization"
    REVISION = "revision"
    TERMINAL = "terminal"


class TimelineEventStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNAVAILABLE = "unavailable"


class TimelineEntryV1(ContractModel):
    id: OpaqueId
    run_id: OpaqueId
    attempt_id: OpaqueId | None = None
    sequence: int = Field(ge=0, le=9_223_372_036_854_775_807)
    phase: TimelinePhase
    status: TimelineEventStatus
    title: DisplayName
    message: Description
    occurred_at: UtcTimestamp
    artifact_ids: list[OpaqueId] = Field(default_factory=list, max_length=128)


class RunTimelinePageV1(ContractModel):
    schema_version: Literal["1"] = "1"
    items: list[TimelineEntryV1] = Field(max_length=100)
    next_cursor: Cursor | None = None
    has_more: bool


class LogStream(StrEnum):
    CORE = "core"
    AGENT = "agent"
    EVOLUTION = "evolution"
    SERVICE = "service"


class LogLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class LogEntryV1(ContractModel):
    id: OpaqueId
    sequence: int = Field(ge=0, le=9_223_372_036_854_775_807)
    occurred_at: UtcTimestamp
    stream: LogStream
    level: LogLevel
    message: LogText
    run_id: OpaqueId | None = None
    attempt_id: OpaqueId | None = None
    service_id: OpaqueId | None = None


class LogPageV1(ContractModel):
    schema_version: Literal["1"] = "1"
    items: list[LogEntryV1] = Field(max_length=100)
    next_cursor: Cursor | None = None
    has_more: bool


class ContextArtifactRefV1(ContractModel):
    artifact_id: OpaqueId
    artifact_type: ArtifactType
    revision_id: OpaqueId


class AdapterRefV1(ContractModel):
    artifact_id: OpaqueId
    adapter_id: OpaqueId
    base_model_ref: OpaqueId
    revision_id: OpaqueId


class RunContextV1(ContractModel):
    schema_version: Literal["1"] = "1"
    run_id: OpaqueId
    project_snapshot_id: OpaqueId
    task_snapshot_id: OpaqueId
    workspace_snapshot_id: OpaqueId
    pinned_revision_id: OpaqueId
    registry_digest: Sha256Digest
    execution_mode: ExecutionMode
    capture_mode: CaptureMode
    token_level_metrics_available: bool
    artifacts: list[ContextArtifactRefV1] = Field(max_length=256)
    adapters: list[AdapterRefV1] = Field(max_length=64)

    @model_validator(mode="after")
    def _capture_metrics_are_consistent(self) -> RunContextV1:
        if self.capture_mode is CaptureMode.TRANSCRIPT and self.token_level_metrics_available:
            raise ValueError("transcript capture has no token-level metrics")
        if (
            self.execution_mode is ExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT
            and self.capture_mode is not CaptureMode.TRANSCRIPT
        ):
            raise ValueError("subscription execution requires transcript capture")
        return self


class ArtifactCompatibilityV1(ContractModel):
    execution_modes: list[ExecutionMode] = Field(max_length=2)
    harness_ids: list[OpaqueId] = Field(max_length=64)
    base_model_refs: list[OpaqueId] = Field(max_length=64)


class ArtifactLineageV1(ContractModel):
    method_id: OpaqueId
    job_id: OpaqueId
    source_dataset_ids: list[OpaqueId] = Field(max_length=128)
    source_artifact_ids: list[OpaqueId] = Field(max_length=128)


class ArtifactScoreV1(ContractModel):
    name: Annotated[
        str,
        StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$"),
    ]
    value: float = Field(ge=-1_000_000, le=1_000_000, allow_inf_nan=False)


class ArtifactSummaryBaseV1(ContractModel):
    id: OpaqueId
    run_id: OpaqueId
    title: DisplayName
    revision_id: OpaqueId
    content_sha256: Sha256Digest
    selected: bool
    promoted: bool
    release_enabled: bool
    compatibility: ArtifactCompatibilityV1
    lineage: ArtifactLineageV1
    scores: list[ArtifactScoreV1] = Field(max_length=64)
    created_at: UtcTimestamp


class TextMemoryArtifactSummaryV1(ArtifactSummaryBaseV1):
    artifact_type: Literal[ArtifactType.TEXT_MEMORY]


class SkillBundleArtifactSummaryV1(ArtifactSummaryBaseV1):
    artifact_type: Literal[ArtifactType.SKILL_BUNDLE]


class AgentSystemArtifactSummaryV1(ArtifactSummaryBaseV1):
    artifact_type: Literal[ArtifactType.AGENT_SYSTEM]


class ParametricMemoryArtifactSummaryV1(ArtifactSummaryBaseV1):
    artifact_type: Literal[ArtifactType.PARAMETRIC_MEMORY]
    release_enabled: Literal[False]


ArtifactSummaryV1: TypeAlias = Annotated[
    TextMemoryArtifactSummaryV1
    | SkillBundleArtifactSummaryV1
    | AgentSystemArtifactSummaryV1
    | ParametricMemoryArtifactSummaryV1,
    Field(discriminator="artifact_type"),
]


class ArtifactPageV1(ContractModel):
    schema_version: Literal["1"] = "1"
    items: list[ArtifactSummaryV1] = Field(max_length=100)
    next_cursor: Cursor | None = None
    has_more: bool


class TextMemoryContentV1(ContractModel):
    artifact_type: Literal[ArtifactType.TEXT_MEMORY]
    mime_type: Literal["text/markdown"]
    content: ContentText


class SkillFileV1(ContractModel):
    relative_path: Annotated[
        str,
        StringConstraints(
            min_length=1,
            max_length=256,
            pattern=r"^[^/\x00-\x1f\x7f][^\x00-\x1f\x7f]*$",
        ),
    ]
    mime_type: MimeType
    content: ContentText
    content_sha256: Sha256Digest

    @field_validator("relative_path")
    @classmethod
    def _safe_relative_path(cls, value: str) -> str:
        if any(segment in {"", ".", ".."} for segment in value.split("/")):
            raise ValueError("relative_path contains an unsafe path segment")
        return value


class SkillBundleContentV1(ContractModel):
    artifact_type: Literal[ArtifactType.SKILL_BUNDLE]
    files: list[SkillFileV1] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def _contains_root_skill(self) -> SkillBundleContentV1:
        if "SKILL.md" not in {item.relative_path for item in self.files}:
            raise ValueError("skill bundle requires root SKILL.md")
        return self


class AgentSystemContentV1(ContractModel):
    artifact_type: Literal[ArtifactType.AGENT_SYSTEM]
    target_path: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    mime_type: Literal["text/markdown"]
    content: ContentText

    @field_validator("target_path")
    @classmethod
    def _safe_target_path(cls, value: str) -> str:
        allowed_files = {"AGENTS.md", "agents.md", "CLAUDE.md", "GEMINI.md"}
        if value in allowed_files:
            return value
        prefix = ".openhands/microagents/"
        if (
            value.startswith(prefix)
            and value.endswith(".md")
            and "/" not in value[len(prefix) :]
            and value[len(prefix) :] != ".md"
            and ".." not in value
        ):
            return value
        raise ValueError("target_path is not an allowed harness instruction path")


class ParametricMemoryContentV1(ContractModel):
    artifact_type: Literal[ArtifactType.PARAMETRIC_MEMORY]
    adapter_id: OpaqueId
    base_model_ref: OpaqueId
    adapter_format: Literal["lora"]
    release_enabled: Literal[False]


ArtifactContentV1: TypeAlias = Annotated[
    TextMemoryContentV1 | SkillBundleContentV1 | AgentSystemContentV1 | ParametricMemoryContentV1,
    Field(discriminator="artifact_type"),
]


class ArtifactDiffV1(ContractModel):
    schema_version: Literal["1"] = "1"
    artifact_id: OpaqueId
    previous_artifact_id: OpaqueId | None = None
    format: Literal["unified_text"]
    before: ContentText
    after: ContentText
    diff: ContentText


class ServicePageV1(ContractModel):
    schema_version: Literal["1"] = "1"
    items: list[ServiceSummaryV1] = Field(max_length=64)
    next_cursor: Cursor | None = None
    has_more: bool


class ServiceRestartRequestV1(ContractModel):
    schema_version: Literal["1"] = "1"
    reason: Annotated[str, StringConstraints(min_length=1, max_length=512)]


class ServiceStopRequestV1(ContractModel):
    schema_version: Literal["1"] = "1"
    reason: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    drain_active_work: bool = True


class ServiceActionV1(ContractModel):
    schema_version: Literal["1"] = "1"
    service: ServiceSummaryV1
    accepted_at: UtcTimestamp


class DiagnosticScope(StrEnum):
    ENVIRONMENT = "environment"
    PROJECT = "project"
    RUN = "run"
    SERVICES = "services"
    REGISTRY = "registry"
    STORAGE = "storage"


class DiagnosticsRequestV1(ContractModel):
    schema_version: Literal["1"] = "1"
    scopes: list[DiagnosticScope] = Field(min_length=1, max_length=16)
    project_id: OpaqueId | None = None
    run_id: OpaqueId | None = None

    @field_validator("scopes")
    @classmethod
    def _unique_scopes(cls, value: list[DiagnosticScope]) -> list[DiagnosticScope]:
        if len(value) != len(set(value)):
            raise ValueError("diagnostic scopes must be unique")
        return value


class DiagnosticStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class DiagnosticCheckV1(ContractModel):
    id: OpaqueId
    scope: DiagnosticScope
    status: CheckStatus
    message: Description
    repair_action: RepairAction
    logs_ref: OpaqueId | None = None


class DiagnosticV1(ContractModel):
    schema_version: Literal["1"] = "1"
    id: OpaqueId
    status: DiagnosticStatus
    scopes: list[DiagnosticScope] = Field(min_length=1, max_length=16)
    checks: list[DiagnosticCheckV1] = Field(max_length=256)
    created_at: UtcTimestamp
    finished_at: UtcTimestamp | None = None

    @model_validator(mode="after")
    def _valid_status_shape(self) -> DiagnosticV1:
        terminal = self.status in {DiagnosticStatus.SUCCEEDED, DiagnosticStatus.FAILED}
        if terminal != (self.finished_at is not None):
            raise ValueError("finished_at is required only for terminal diagnostics")
        return self


class CacheScope(StrEnum):
    MODEL_DOWNLOADS = "model_downloads"
    BUILD_ARTIFACTS = "build_artifacts"
    COMPLETED_RUNS = "completed_runs"
    COMPLETED_DIAGNOSTICS = "completed_diagnostics"


class CacheCleanupRequestV1(ContractModel):
    schema_version: Literal["1"] = "1"
    scopes: list[CacheScope] = Field(min_length=1, max_length=8)
    older_than_days: int = Field(ge=1, le=3650)

    @field_validator("scopes")
    @classmethod
    def _unique_scopes(cls, value: list[CacheScope]) -> list[CacheScope]:
        if len(value) != len(set(value)):
            raise ValueError("cache scopes must be unique")
        return value


class CacheCleanupV1(ContractModel):
    schema_version: Literal["1"] = "1"
    id: OpaqueId
    status: DiagnosticStatus
    scopes: list[CacheScope] = Field(min_length=1, max_length=8)
    removed_entries: int = Field(ge=0, le=100_000_000)
    reclaimed_bytes: int = Field(ge=0, le=9_223_372_036_854_775_807)
    created_at: UtcTimestamp
    finished_at: UtcTimestamp | None = None
    error: ApiErrorV1 | None = None

    @model_validator(mode="after")
    def _valid_status_shape(self) -> CacheCleanupV1:
        terminal = self.status in {DiagnosticStatus.SUCCEEDED, DiagnosticStatus.FAILED}
        if terminal != (self.finished_at is not None):
            raise ValueError("finished_at is required only for terminal cleanup")
        if (self.status is DiagnosticStatus.FAILED) != (self.error is not None):
            raise ValueError("error is required only for failed cleanup")
        return self


class EventBaseV1(ContractModel):
    schema_version: Literal["1"] = "1"
    id: OpaqueId
    sequence: int = Field(ge=0, le=9_223_372_036_854_775_807)
    occurred_at: UtcTimestamp


class RunUpdatedEventV1(EventBaseV1):
    event: Literal["run.updated.v1"]
    payload: RunSummaryV1


class TimelineAppendedPayloadV1(ContractModel):
    run_id: OpaqueId
    entry: TimelineEntryV1


class RunTimelineAppendedEventV1(EventBaseV1):
    event: Literal["run.timeline_appended.v1"]
    payload: TimelineAppendedPayloadV1


class ProjectUpdatedEventV1(EventBaseV1):
    event: Literal["project.updated.v1"]
    payload: ProjectSummaryV1


class ServiceUpdatedEventV1(EventBaseV1):
    event: Literal["service.updated.v1"]
    payload: ServiceSummaryV1


class DiagnosticUpdatedEventV1(EventBaseV1):
    event: Literal["diagnostic.updated.v1"]
    payload: DiagnosticV1


class HeartbeatPayloadV1(ContractModel):
    active_run_count: int = Field(ge=0, le=100_000)


class HeartbeatEventV1(EventBaseV1):
    event: Literal["heartbeat.v1"]
    payload: HeartbeatPayloadV1


_EventEnvelopeUnion: TypeAlias = Annotated[
    RunUpdatedEventV1
    | RunTimelineAppendedEventV1
    | ProjectUpdatedEventV1
    | ServiceUpdatedEventV1
    | DiagnosticUpdatedEventV1
    | HeartbeatEventV1,
    Field(discriminator="event"),
]


class EventEnvelopeV1(RootModel[_EventEnvelopeUnion]):
    """Closed discriminated union serialized directly as one SSE data object."""

    model_config = ConfigDict(frozen=True, strict=True, validate_default=True)


class ContractOnlyResponseV1(ContractModel):
    """Runtime response emitted by the schema-only app for every operation."""

    schema_version: Literal["1"] = "1"
    code: Literal["contract_only_not_implemented"]
    message: Literal["This app defines the Core Control API v1 contract and has no provider."]
