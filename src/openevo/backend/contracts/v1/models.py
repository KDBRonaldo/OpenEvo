"""Strict data models for the Core Control API v1 product boundary."""

from __future__ import annotations

import base64
import binascii
import hashlib
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

from openevo.evolution.framework.capabilities import EvolutionCapabilitiesV1
from openevo.evolution.framework.plan import ProjectEvolutionTargetMap
from openevo.evolution.framework.profiles import ReleaseExecutionMode


MAX_CANONICAL_JSON_BYTES = 256 * 1024
MAX_CANONICAL_JSON_DEPTH = 16
MAX_CANONICAL_JSON_NODES = 8192
MAX_CANONICAL_JSON_COLLECTION_ITEMS = 4096
MAX_JAVASCRIPT_SAFE_INTEGER = (1 << 53) - 1
MAX_WORKSPACE_UPLOAD_BYTES = 16 * 1024 * 1024 * 1024
MAX_WORKSPACE_CHUNK_BYTES = 8 * 1024 * 1024
MAX_WORKSPACE_ENTRIES = 100_000
MAX_WORKSPACE_PATH_DEPTH = 32
MAX_WORKSPACE_PATH_BYTES = 1024
MAX_WORKSPACE_FILE_BYTES = 8 * 1024 * 1024 * 1024
MAX_CONTENT_REF_BYTES = 1_000_000_000_000
MAX_ARTIFACT_PREVIEW_DOCUMENTS = 128
MAX_ARTIFACT_PREVIEW_UTF8_BYTES = 2 * 1024 * 1024
MAX_ARTIFACT_DIFF_HUNKS = 128
MAX_ARTIFACT_DIFF_LINES = 8192


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
StrongETag = Annotated[
    str,
    StringConstraints(min_length=66, max_length=66, pattern=r'^"[0-9a-f]{64}"$'),
]
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
AgentModelRef = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=256,
        pattern=r"^[^\x00-\x20\x7f](?:[^\x00-\x1f\x7f]*[^\x00-\x20\x7f])?$",
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


ExecutionMode = ReleaseExecutionMode


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


class ModelPreparationStatus(StrEnum):
    UNRESOLVED = "unresolved"
    DOWNLOADING = "downloading"
    READY = "ready"
    FAILED = "failed"


class ModelPreparationV1(ContractModel):
    model_ref: AgentModelRef
    status: ModelPreparationStatus
    downloaded_bytes: int | None = Field(default=None, ge=0, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    total_bytes: int | None = Field(default=None, ge=0, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    error: ApiErrorV1 | None = None
    updated_at: UtcTimestamp

    @model_validator(mode="after")
    def _valid_model_state(self) -> ModelPreparationV1:
        if self.downloaded_bytes is not None and self.total_bytes is None:
            raise ValueError("downloaded_bytes requires total_bytes")
        if (
            self.downloaded_bytes is not None
            and self.total_bytes is not None
            and self.downloaded_bytes > self.total_bytes
        ):
            raise ValueError("downloaded_bytes exceeds total_bytes")
        if (self.status is ModelPreparationStatus.FAILED) != (self.error is not None):
            raise ValueError("error is required only for failed model preparation")
        return self


class ServiceSummaryV1(ContractModel):
    id: OpaqueId
    display_name: DisplayName
    kind: ServiceKind
    status: ServiceStatus
    restartable: bool
    status_message: ShortText | None = None
    error: ApiErrorV1 | None = None
    model_preparation: ModelPreparationV1 | None = None
    updated_at: UtcTimestamp
    observed_at: UtcTimestamp
    etag: StrongETag

    @model_validator(mode="after")
    def _failed_service_has_error(self) -> ServiceSummaryV1:
        if (self.status is ServiceStatus.FAILED) != (self.error is not None):
            raise ValueError("error is required only for failed services")
        if self.kind is not ServiceKind.INFERENCE and self.model_preparation is not None:
            raise ValueError("model preparation belongs only to inference services")
        return self


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
    model_preparation: ModelPreparationV1 | None = None

    @model_validator(mode="after")
    def _model_check_shape(self) -> EnvironmentCheckV1:
        if (
            self.kind is not EnvironmentCheckKind.MODEL_SERVICE
            and self.model_preparation is not None
        ):
            raise ValueError("model preparation belongs only to model-service checks")
        return self


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


class ArtifactType(StrEnum):
    TEXT_MEMORY = "text_memory"
    SKILL_BUNDLE = "skill_bundle"
    AGENT_SYSTEM = "agent_system"
    PARAMETRIC_MEMORY = "parametric_memory"


CapabilitiesResponseV1 = EvolutionCapabilitiesV1


class EvolutionConfigV1(ContractModel):
    targets: ProjectEvolutionTargetMap


class SnapshotKind(StrEnum):
    PROJECT = "project"
    TASK = "task"
    WORKSPACE = "workspace"


class ImmutableSnapshotRefV1(ContractModel):
    id: OpaqueId
    kind: SnapshotKind
    content_sha256: Sha256Digest
    created_at: UtcTimestamp


class RevisionRefV1(ContractModel):
    id: OpaqueId
    project_id: OpaqueId
    generation: int = Field(ge=0, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    manifest_sha256: Sha256Digest


class RequiredRevisionRelation(StrEnum):
    ACTIVE = "active"
    SUCCESSOR = "successor"


class ReachableRequiredRevisionRefV1(ContractModel):
    revision: RevisionRefV1
    reachable_from_revision_id: OpaqueId
    relation: RequiredRevisionRelation

    @model_validator(mode="after")
    def _valid_reachability(self) -> ReachableRequiredRevisionRefV1:
        if (
            self.relation is RequiredRevisionRelation.ACTIVE
            and self.revision.id != self.reachable_from_revision_id
        ):
            raise ValueError("an active required revision must be the reachable head")
        if (
            self.relation is RequiredRevisionRelation.SUCCESSOR
            and self.revision.id == self.reachable_from_revision_id
        ):
            raise ValueError("a successor must differ from its reachable predecessor")
        return self


class ContentRefV1(ContractModel):
    content_id: OpaqueId
    sha256: Sha256Digest
    byte_size: int = Field(ge=0, le=MAX_CONTENT_REF_BYTES)


class TaskSpecV1(ContractModel):
    title: ShortText
    objective: Annotated[str, StringConstraints(min_length=1, max_length=65_536)]
    content_ref: ContentRefV1 | None = None


class WorkspaceArchiveFormat(StrEnum):
    OPENEVO_DETERMINISTIC_TAR_V1 = "openevo_deterministic_tar_v1"


class WorkspaceArchivePolicyV1(ContractModel):
    media_type: Literal["application/vnd.openevo.workspace-tar"]
    tar_format: Literal["posix_ustar"] = Field(
        description=(
            "Uncompressed POSIX ustar with canonical checksums, zero-padded file bodies, "
            "exactly two 512-byte zero end blocks, and no trailing bytes."
        )
    )
    entry_types: Literal["regular_files_and_directories"] = Field(
        description="Only regular-file and directory ustar entries are accepted."
    )
    path_policy: Literal["utf8_nfc_posix_relative"] = Field(
        description=(
            "Unique NFC UTF-8 POSIX relative paths; absolute paths, empty/dot/dot-dot "
            "segments, backslashes, NUL, and control characters are forbidden."
        )
    )
    entry_order: Literal["lexicographic"] = Field(
        description="Entries are unique and sorted by their encoded UTF-8 path bytes."
    )
    metadata_policy: Literal["uid_gid_zero_names_empty_mtime_zero"] = Field(
        description="uid/gid/mtime are zero and uname/gname are empty in every header."
    )
    file_mode_policy: Literal["0644_or_0755"] = Field(
        description="Regular files use only 0644 or 0755; setuid/setgid/sticky bits are forbidden."
    )
    directory_mode: Literal["0755"]
    allow_symlinks: Literal[False]
    allow_hardlinks: Literal[False]
    allow_devices: Literal[False]
    allow_fifos: Literal[False]
    allow_sparse_files: Literal[False]
    allow_tar_extensions: Literal[False]
    max_entries: Literal[100_000]
    max_path_depth: Literal[32]
    max_path_bytes: Literal[1024]
    max_file_bytes: Literal[8_589_934_592]
    max_extracted_bytes: Literal[17_179_869_184]


class WorkspaceArchiveV1(ContractModel):
    content_ref: ContentRefV1
    format: WorkspaceArchiveFormat = Field(
        description="The only release workspace transfer format; ZIP and compressed tar are invalid."
    )
    entry_count: int = Field(ge=0, le=MAX_WORKSPACE_ENTRIES)
    extracted_byte_size: int = Field(ge=0, le=MAX_WORKSPACE_UPLOAD_BYTES)
    policy: WorkspaceArchivePolicyV1

    @model_validator(mode="after")
    def _valid_archive_bounds(self) -> WorkspaceArchiveV1:
        if self.content_ref.byte_size > MAX_WORKSPACE_UPLOAD_BYTES:
            raise ValueError("workspace archive exceeds the upload byte limit")
        if self.entry_count == 0 and self.extracted_byte_size != 0:
            raise ValueError("an empty archive cannot declare extracted bytes")
        return self


class WorkspaceSourceKind(StrEnum):
    SCRATCH = "scratch"
    NATIVE_FOLDER_SNAPSHOT = "native_folder_snapshot"
    GIT_SNAPSHOT = "git_snapshot"
    REMOTE_SNAPSHOT = "remote_snapshot"


class ScratchWorkspaceSpecV1(ContractModel):
    kind: Literal[WorkspaceSourceKind.SCRATCH]
    display_name: ShortText


class ImportedWorkspaceSpecV1(ContractModel):
    kind: Literal[
        WorkspaceSourceKind.NATIVE_FOLDER_SNAPSHOT,
        WorkspaceSourceKind.GIT_SNAPSHOT,
        WorkspaceSourceKind.REMOTE_SNAPSHOT,
    ]
    display_name: ShortText
    archive: WorkspaceArchiveV1


ProjectWorkspaceSpecV1: TypeAlias = Annotated[
    ScratchWorkspaceSpecV1 | ImportedWorkspaceSpecV1,
    Field(discriminator="kind"),
]


class ProjectSpecV1(ContractModel):
    execution_mode: ExecutionMode
    capture_mode: CaptureMode
    harness_id: OpaqueId
    agent_model_ref: AgentModelRef = Field(
        description=(
            "For self-deployed projects, the exact bounded Hugging Face model string "
            "received from Desktop hf_model; it is not a managed-model resource ID."
        )
    )
    evolution: EvolutionConfigV1

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
    task: TaskSpecV1
    workspace: ProjectWorkspaceSpecV1


class ProjectPatchV1(ContractModel):
    schema_version: Literal["1"] = "1"
    name: DisplayName | None = None
    description: Description | None = None
    spec: ProjectSpecV1 | None = None
    task: TaskSpecV1 | None = None
    workspace: ProjectWorkspaceSpecV1 | None = None

    @model_validator(mode="after")
    def _has_change(self) -> ProjectPatchV1:
        if (
            self.name is None
            and self.description is None
            and self.spec is None
            and self.task is None
            and self.workspace is None
        ):
            raise ValueError("project patch must contain a change")
        return self


class ProjectSummaryV1(ContractModel):
    id: OpaqueId
    name: DisplayName
    description: Description | None = None
    status: ProjectStatus
    execution_mode: ExecutionMode
    workspace_kind: WorkspaceSourceKind
    current_project_snapshot: ImmutableSnapshotRefV1
    current_task_snapshot: ImmutableSnapshotRefV1
    current_workspace_snapshot: ImmutableSnapshotRefV1 | None = None
    active_revision: RevisionRefV1 | None = None
    registry_digest: Sha256Digest | None = None
    model_preparation: ModelPreparationV1
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    etag: StrongETag

    @model_validator(mode="after")
    def _snapshot_kinds_match(self) -> ProjectSummaryV1:
        expected = (
            (self.current_project_snapshot, SnapshotKind.PROJECT),
            (self.current_task_snapshot, SnapshotKind.TASK),
            (self.current_workspace_snapshot, SnapshotKind.WORKSPACE),
        )
        for snapshot, kind in expected:
            if snapshot is not None and snapshot.kind is not kind:
                raise ValueError(f"current {kind.value} snapshot has the wrong kind")
        if self.active_revision is not None and self.active_revision.project_id != self.id:
            raise ValueError("active revision belongs to another project")
        if (
            self.workspace_kind is WorkspaceSourceKind.SCRATCH
            and self.current_workspace_snapshot is None
        ):
            raise ValueError("scratch project requires a Core-signed empty workspace snapshot")
        if self.status is ProjectStatus.READY and (
            self.current_workspace_snapshot is None
            or self.active_revision is None
            or self.registry_digest is None
            or self.model_preparation.status is not ModelPreparationStatus.READY
        ):
            raise ValueError("a ready project requires workspace, revision, registry, and model")
        return self


class ProjectV1(ProjectSummaryV1):
    spec: ProjectSpecV1
    task: TaskSpecV1
    workspace: ProjectWorkspaceSpecV1

    @model_validator(mode="after")
    def _model_matches_spec(self) -> ProjectV1:
        if self.execution_mode is not self.spec.execution_mode:
            raise ValueError("project execution mode must match its spec")
        if self.model_preparation.model_ref != self.spec.agent_model_ref:
            raise ValueError("model preparation must describe the project model")
        if self.workspace_kind.value != self.workspace.kind:
            raise ValueError("workspace kind must match the project workspace spec")
        return self


class ProjectPageV1(ContractModel):
    schema_version: Literal["1"] = "1"
    items: list[ProjectSummaryV1] = Field(max_length=100)
    next_cursor: Cursor | None = None
    has_more: bool


class WorkspaceUploadStatus(StrEnum):
    OPEN = "open"
    FINALIZED = "finalized"
    ABORTED = "aborted"


class WorkspaceUploadCreateV1(ContractModel):
    schema_version: Literal["1"] = "1"
    archive: WorkspaceArchiveV1
    base_workspace_snapshot: ImmutableSnapshotRefV1 | None = None

    @model_validator(mode="after")
    def _base_is_workspace(self) -> WorkspaceUploadCreateV1:
        if (
            self.base_workspace_snapshot is not None
            and self.base_workspace_snapshot.kind is not SnapshotKind.WORKSPACE
        ):
            raise ValueError("base snapshot must be a workspace snapshot")
        return self


class WorkspaceUploadSessionV1(ContractModel):
    schema_version: Literal["1"] = "1"
    id: OpaqueId
    project_id: OpaqueId
    status: WorkspaceUploadStatus
    accepted_offset: int = Field(ge=0, le=MAX_WORKSPACE_UPLOAD_BYTES)
    archive: WorkspaceArchiveV1
    base_workspace_snapshot: ImmutableSnapshotRefV1 | None = None
    workspace_snapshot: ImmutableSnapshotRefV1 | None = None
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    etag: StrongETag

    @model_validator(mode="after")
    def _valid_upload_state(self) -> WorkspaceUploadSessionV1:
        archive_byte_size = self.archive.content_ref.byte_size
        if self.accepted_offset > archive_byte_size:
            raise ValueError("accepted offset exceeds declared upload size")
        if (self.status is WorkspaceUploadStatus.FINALIZED) != (
            self.workspace_snapshot is not None
        ):
            raise ValueError("only a finalized upload has a workspace snapshot")
        if (
            self.status is WorkspaceUploadStatus.FINALIZED
            and self.accepted_offset != archive_byte_size
        ):
            raise ValueError("a finalized upload must contain every declared byte")
        for snapshot in (self.base_workspace_snapshot, self.workspace_snapshot):
            if snapshot is not None and snapshot.kind is not SnapshotKind.WORKSPACE:
                raise ValueError("upload snapshots must be workspace snapshots")
        return self


class WorkspaceUploadChunkV1(ContractModel):
    schema_version: Literal["1"] = "1"
    offset: int = Field(ge=0, le=MAX_WORKSPACE_UPLOAD_BYTES)
    byte_length: int = Field(ge=1, le=MAX_WORKSPACE_CHUNK_BYTES)
    content_base64: Annotated[
        str,
        StringConstraints(min_length=4, max_length=((MAX_WORKSPACE_CHUNK_BYTES + 2) // 3) * 4),
    ]
    content_sha256: Sha256Digest

    @model_validator(mode="after")
    def _content_matches_identity(self) -> WorkspaceUploadChunkV1:
        try:
            decoded = base64.b64decode(self.content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("content_base64 must be canonical base64") from exc
        if base64.b64encode(decoded).decode("ascii") != self.content_base64:
            raise ValueError("content_base64 must be canonical base64")
        if len(decoded) != self.byte_length:
            raise ValueError("byte_length does not match decoded content")
        if hashlib.sha256(decoded).hexdigest() != self.content_sha256:
            raise ValueError("chunk digest does not match decoded content")
        return self


class WorkspaceUploadFinalizeV1(ContractModel):
    schema_version: Literal["1"] = "1"
    content_sha256: Sha256Digest


class WorkspaceUploadAbortV1(ContractModel):
    schema_version: Literal["1"] = "1"
    reason: Annotated[str, StringConstraints(min_length=1, max_length=512)]


class WorkspaceSnapshotV1(ContractModel):
    schema_version: Literal["1"] = "1"
    project_id: OpaqueId
    upload: WorkspaceUploadSessionV1
    workspace_snapshot: ImmutableSnapshotRefV1
    etag: StrongETag

    @model_validator(mode="after")
    def _finalized_upload_matches_snapshot(self) -> WorkspaceSnapshotV1:
        if self.upload.project_id != self.project_id:
            raise ValueError("upload belongs to another project")
        if self.upload.status is not WorkspaceUploadStatus.FINALIZED:
            raise ValueError("workspace snapshot requires a finalized upload")
        if self.upload.workspace_snapshot != self.workspace_snapshot:
            raise ValueError("upload and response workspace snapshots differ")
        if self.etag != self.upload.etag:
            raise ValueError("response ETag must match the finalized upload ETag")
        return self


class ValidationCheckV1(ContractModel):
    id: OpaqueId
    status: CheckStatus
    message: Description
    target_id: OpaqueId | None = None
    method_id: OpaqueId | None = None


class ProjectValidationRequestV1(ContractModel):
    schema_version: Literal["1"] = "1"
    project_snapshot: ImmutableSnapshotRefV1
    workspace_snapshot: ImmutableSnapshotRefV1
    expected_registry_digest: Sha256Digest

    @model_validator(mode="after")
    def _snapshot_kinds_match(self) -> ProjectValidationRequestV1:
        if self.project_snapshot.kind is not SnapshotKind.PROJECT:
            raise ValueError("project_snapshot has the wrong kind")
        if self.workspace_snapshot.kind is not SnapshotKind.WORKSPACE:
            raise ValueError("workspace_snapshot has the wrong kind")
        return self


class ProjectValidationResponseV1(ContractModel):
    schema_version: Literal["1"] = "1"
    valid: bool
    registry_digest: Sha256Digest
    checks: list[ValidationCheckV1] = Field(max_length=256)
    validated_at: UtcTimestamp


class RunCreateV1(ContractModel):
    schema_version: Literal["1"] = "1"
    project_id: OpaqueId
    project_snapshot: ImmutableSnapshotRefV1
    task_snapshot: ImmutableSnapshotRefV1
    workspace_snapshot: ImmutableSnapshotRefV1
    expected_registry_digest: Sha256Digest
    required_revision: ReachableRequiredRevisionRefV1

    @model_validator(mode="after")
    def _refs_match_project(self) -> RunCreateV1:
        if self.project_snapshot.kind is not SnapshotKind.PROJECT:
            raise ValueError("project_snapshot has the wrong kind")
        if self.task_snapshot.kind is not SnapshotKind.TASK:
            raise ValueError("task_snapshot has the wrong kind")
        if self.workspace_snapshot.kind is not SnapshotKind.WORKSPACE:
            raise ValueError("workspace_snapshot has the wrong kind")
        if self.required_revision.revision.project_id != self.project_id:
            raise ValueError("required revision belongs to another project")
        return self


class RunStatus(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class QueuedReasonCode(StrEnum):
    ADMISSION_PENDING = "admission_pending"
    CAPACITY = "capacity"
    SERVICE_STARTING = "service_starting"
    REQUIRED_REVISION_UNCOMMITTED = "required_revision_uncommitted"


class QueuedReasonV1(ContractModel):
    code: QueuedReasonCode
    summary: ShortText
    retry_after_seconds: int | None = Field(default=None, ge=0, le=86_400)


_TERMINAL_RUN_STATES = frozenset({RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED})


class AttemptV1(ContractModel):
    id: OpaqueId
    run_id: OpaqueId
    number: int = Field(ge=1, le=10_000)
    status: RunStatus
    queued_reason: QueuedReasonV1 | None = None
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
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
    PREPARING_SERVING = "preparing_serving"
    COMMITTING = "committing"
    ACTIVE = "active"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class RevisionTransitionV1(ContractModel):
    state: RevisionTransitionState
    predecessor_revision: RevisionRefV1
    successor_revision: RevisionRefV1
    progress_completed: int = Field(ge=0, le=10_000)
    progress_total: int = Field(ge=0, le=10_000)
    message: Description
    error: ApiErrorV1 | None = None
    updated_at: UtcTimestamp

    @model_validator(mode="after")
    def _valid_transition_shape(self) -> RevisionTransitionV1:
        if self.progress_completed > self.progress_total:
            raise ValueError("transition progress exceeds total")
        if (self.state is RevisionTransitionState.FAILED) != (self.error is not None):
            raise ValueError("error is required only for failed transitions")
        if self.successor_revision.project_id != self.predecessor_revision.project_id:
            raise ValueError("revision transition crosses projects")
        if self.successor_revision.generation != self.predecessor_revision.generation + 1:
            raise ValueError("successor generation must immediately follow predecessor")
        return self


class RevisionStatus(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    ACTIVE = "active"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RevisionV1(ContractModel):
    schema_version: Literal["1"] = "1"
    revision: RevisionRefV1
    status: RevisionStatus
    predecessor_revision: RevisionRefV1 | None = None
    project_snapshot: ImmutableSnapshotRefV1
    task_snapshot: ImmutableSnapshotRefV1 | None = None
    workspace_snapshot: ImmutableSnapshotRefV1
    registry_digest: Sha256Digest
    transition: RevisionTransitionV1 | None = None
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    activated_at: UtcTimestamp | None = None
    error: ApiErrorV1 | None = None
    etag: StrongETag

    @model_validator(mode="after")
    def _valid_revision_state(self) -> RevisionV1:
        if self.project_snapshot.kind is not SnapshotKind.PROJECT:
            raise ValueError("project_snapshot has the wrong kind")
        if self.task_snapshot is not None and self.task_snapshot.kind is not SnapshotKind.TASK:
            raise ValueError("task_snapshot has the wrong kind")
        if self.workspace_snapshot.kind is not SnapshotKind.WORKSPACE:
            raise ValueError("workspace_snapshot has the wrong kind")
        if (self.status is RevisionStatus.ACTIVE) != (self.activated_at is not None):
            raise ValueError("activated_at is required only for active revisions")
        if (self.status is RevisionStatus.FAILED) != (self.error is not None):
            raise ValueError("error is required only for failed revisions")
        if self.predecessor_revision is not None:
            if self.predecessor_revision.project_id != self.revision.project_id:
                raise ValueError("revision predecessor belongs to another project")
            if self.predecessor_revision.generation + 1 != self.revision.generation:
                raise ValueError("revision generation does not follow predecessor")
        return self


class RevisionPageV1(ContractModel):
    schema_version: Literal["1"] = "1"
    items: list[RevisionV1] = Field(max_length=100)
    next_cursor: Cursor | None = None
    has_more: bool


class RevisionHeadV1(ContractModel):
    schema_version: Literal["1"] = "1"
    project_id: OpaqueId
    active_revision: RevisionRefV1
    successor_revision: RevisionRefV1 | None = None
    transition: RevisionTransitionV1 | None = None
    updated_at: UtcTimestamp
    etag: StrongETag

    @model_validator(mode="after")
    def _valid_head(self) -> RevisionHeadV1:
        if self.active_revision.project_id != self.project_id:
            raise ValueError("active revision belongs to another project")
        if (self.successor_revision is None) != (self.transition is None):
            raise ValueError("successor revision and transition must appear together")
        if self.successor_revision is not None:
            if self.successor_revision.project_id != self.project_id:
                raise ValueError("successor revision belongs to another project")
            if self.successor_revision.generation != self.active_revision.generation + 1:
                raise ValueError("successor generation must immediately follow active head")
            if self.transition is None or (
                self.transition.predecessor_revision != self.active_revision
                or self.transition.successor_revision != self.successor_revision
            ):
                raise ValueError("head transition does not bind active and successor revisions")
        return self


class RunSummaryV1(ContractModel):
    id: OpaqueId
    project_id: OpaqueId
    project_snapshot: ImmutableSnapshotRefV1
    task_snapshot: ImmutableSnapshotRefV1
    workspace_snapshot: ImmutableSnapshotRefV1
    registry_digest: Sha256Digest
    execution_mode: ExecutionMode
    capture_mode: CaptureMode
    status: RunStatus
    queued_reason: QueuedReasonV1 | None = None
    current_attempt_id: OpaqueId | None = None
    current_attempt: AttemptV1 | None = None
    attempt_count: int = Field(ge=0, le=10_000)
    current_error: ApiErrorV1 | None = None
    pinned_revision: RevisionRefV1 | None = None
    required_revision: ReachableRequiredRevisionRefV1
    revision_transition: RevisionTransitionV1
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    started_at: UtcTimestamp | None = None
    finished_at: UtcTimestamp | None = None
    etag: StrongETag

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
        if (self.current_attempt_id is None) != (self.current_attempt is None):
            raise ValueError("current_attempt must match current_attempt_id")
        if self.current_attempt is not None:
            if self.current_attempt.id != self.current_attempt_id:
                raise ValueError("current_attempt has the wrong ID")
            if self.current_attempt.run_id != self.id:
                raise ValueError("current_attempt belongs to another run")
            if self.current_attempt.error != self.current_error:
                raise ValueError("run and current attempt errors differ")
        if self.pinned_revision is not None and (
            self.pinned_revision != self.required_revision.revision
        ):
            raise ValueError("a run may pin only its required revision")
        if self.status is RunStatus.QUEUED and self.pinned_revision is not None:
            raise ValueError("a queued run cannot claim an admission revision pin")
        if self.project_snapshot.kind is not SnapshotKind.PROJECT:
            raise ValueError("project_snapshot has the wrong kind")
        if self.task_snapshot.kind is not SnapshotKind.TASK:
            raise ValueError("task_snapshot has the wrong kind")
        if self.workspace_snapshot.kind is not SnapshotKind.WORKSPACE:
            raise ValueError("workspace_snapshot has the wrong kind")
        if self.required_revision.revision.project_id != self.project_id:
            raise ValueError("required revision belongs to another project")
        if (self.status is RunStatus.FAILED) != (self.current_error is not None):
            raise ValueError("current_error is required only for failed runs")
        if (
            self.execution_mode is ExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT
            and self.capture_mode is not CaptureMode.TRANSCRIPT
        ):
            raise ValueError("subscription execution requires transcript capture")
        return self


class RunV1(RunSummaryV1):
    attempts: list[AttemptV1] = Field(max_length=100)

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
    sequence: int = Field(ge=0, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    service_id: OpaqueId
    phase: TimelinePhase
    status: TimelineEventStatus
    title: DisplayName
    message: Description
    occurred_at: UtcTimestamp
    artifact_ids: list[OpaqueId] = Field(default_factory=list, max_length=128)
    error: ApiErrorV1 | None = None

    @model_validator(mode="after")
    def _failed_entry_has_error(self) -> TimelineEntryV1:
        if (self.status is TimelineEventStatus.FAILED) != (self.error is not None):
            raise ValueError("error is required only for failed timeline entries")
        return self


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
    sequence: int = Field(ge=0, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    occurred_at: UtcTimestamp
    stream: LogStream
    level: LogLevel
    message: LogText
    run_id: OpaqueId | None = None
    attempt_id: OpaqueId | None = None
    service_id: OpaqueId


class LogPageV1(ContractModel):
    schema_version: Literal["1"] = "1"
    items: list[LogEntryV1] = Field(max_length=100)
    next_cursor: Cursor | None = None
    has_more: bool


class ContextArtifactRefV1(ContractModel):
    artifact_id: OpaqueId
    artifact_type: ArtifactType
    target_id: OpaqueId
    revision: RevisionRefV1


class AdapterRefV1(ContractModel):
    artifact_id: OpaqueId
    adapter_id: OpaqueId
    base_model_ref: AgentModelRef
    revision: RevisionRefV1


class RunContextV1(ContractModel):
    schema_version: Literal["1"] = "1"
    run_id: OpaqueId
    project_id: OpaqueId
    project_snapshot: ImmutableSnapshotRefV1
    task_snapshot: ImmutableSnapshotRefV1
    workspace_snapshot: ImmutableSnapshotRefV1
    status: RunStatus
    queued_reason: QueuedReasonV1 | None = None
    current_attempt_id: OpaqueId | None = None
    current_attempt: AttemptV1 | None = None
    attempt_count: int = Field(ge=0, le=10_000)
    current_error: ApiErrorV1 | None = None
    pinned_revision: RevisionRefV1 | None = None
    required_revision: ReachableRequiredRevisionRefV1
    revision_transition: RevisionTransitionV1
    registry_digest: Sha256Digest
    execution_mode: ExecutionMode
    capture_mode: CaptureMode
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    started_at: UtcTimestamp | None = None
    finished_at: UtcTimestamp | None = None
    etag: StrongETag
    token_level_metrics_available: bool
    artifacts: list[ContextArtifactRefV1] = Field(max_length=256)
    adapters: list[AdapterRefV1] = Field(max_length=64)

    @model_validator(mode="after")
    def _capture_metrics_are_consistent(self) -> RunContextV1:
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
        if (self.current_attempt_id is None) != (self.current_attempt is None):
            raise ValueError("current_attempt must match current_attempt_id")
        if self.current_attempt is not None:
            if self.current_attempt.id != self.current_attempt_id:
                raise ValueError("current_attempt has the wrong ID")
            if self.current_attempt.run_id != self.run_id:
                raise ValueError("current_attempt belongs to another run")
            if self.current_attempt.error != self.current_error:
                raise ValueError("run and current attempt errors differ")
        if self.pinned_revision is not None and (
            self.pinned_revision != self.required_revision.revision
        ):
            raise ValueError("a run may pin only its required revision")
        if self.status is RunStatus.QUEUED and self.pinned_revision is not None:
            raise ValueError("a queued run cannot claim an admission revision pin")
        if (self.status is RunStatus.FAILED) != (self.current_error is not None):
            raise ValueError("current_error is required only for failed runs")
        if self.project_snapshot.kind is not SnapshotKind.PROJECT:
            raise ValueError("project_snapshot has the wrong kind")
        if self.task_snapshot.kind is not SnapshotKind.TASK:
            raise ValueError("task_snapshot has the wrong kind")
        if self.workspace_snapshot.kind is not SnapshotKind.WORKSPACE:
            raise ValueError("workspace_snapshot has the wrong kind")
        if self.required_revision.revision.project_id != self.project_id:
            raise ValueError("required revision belongs to another project")
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
    base_model_refs: list[AgentModelRef] = Field(max_length=64)


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


class TextMemoryArtifactMetadataV1(ContractModel):
    record_count: int = Field(ge=0, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    source_dataset_ids: list[OpaqueId] = Field(max_length=128)


class SkillBundleArtifactMetadataV1(ContractModel):
    document_count: int = Field(ge=1, le=MAX_ARTIFACT_PREVIEW_DOCUMENTS)
    root_document: Literal["SKILL.md"] = "SKILL.md"


class AgentSystemArtifactMetadataV1(ContractModel):
    target_path: Annotated[str, StringConstraints(min_length=1, max_length=256)]

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


class ParametricMemoryArtifactMetadataV1(ContractModel):
    adapter_id: OpaqueId
    base_model_ref: AgentModelRef
    adapter_format: Literal["lora"]


class ArtifactSummaryBaseV1(ContractModel):
    id: OpaqueId
    project_id: OpaqueId
    run_id: OpaqueId | None = None
    target_id: OpaqueId
    display_name: DisplayName
    summary: Description
    byte_size: int = Field(ge=0, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    produced_revision: RevisionRefV1
    membership_revisions: list[RevisionRefV1] = Field(max_length=128)
    content_sha256: Sha256Digest
    selected: bool
    promoted: bool
    release_enabled: bool
    compatibility: ArtifactCompatibilityV1
    lineage: ArtifactLineageV1
    scores: list[ArtifactScoreV1] = Field(max_length=64)
    created_at: UtcTimestamp

    @model_validator(mode="after")
    def _revision_membership_matches_project(self) -> ArtifactSummaryBaseV1:
        revisions = [self.produced_revision, *self.membership_revisions]
        if any(revision.project_id != self.project_id for revision in revisions):
            raise ValueError("artifact revision belongs to another project")
        membership_ids = [revision.id for revision in self.membership_revisions]
        if len(membership_ids) != len(set(membership_ids)):
            raise ValueError("artifact membership revisions must be unique")
        return self


class TextMemoryArtifactSummaryV1(ArtifactSummaryBaseV1):
    artifact_type: Literal[ArtifactType.TEXT_MEMORY]
    metadata: TextMemoryArtifactMetadataV1


class SkillBundleArtifactSummaryV1(ArtifactSummaryBaseV1):
    artifact_type: Literal[ArtifactType.SKILL_BUNDLE]
    metadata: SkillBundleArtifactMetadataV1


class AgentSystemArtifactSummaryV1(ArtifactSummaryBaseV1):
    artifact_type: Literal[ArtifactType.AGENT_SYSTEM]
    metadata: AgentSystemArtifactMetadataV1


class ParametricMemoryArtifactSummaryV1(ArtifactSummaryBaseV1):
    artifact_type: Literal[ArtifactType.PARAMETRIC_MEMORY]
    release_enabled: Literal[False]
    metadata: ParametricMemoryArtifactMetadataV1


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


class ArtifactDocumentPreviewV1(ContractModel):
    document_id: OpaqueId
    display_name: DisplayName
    relative_path: Annotated[
        str | None,
        StringConstraints(
            max_length=256,
            pattern=r"^[^/\x00-\x1f\x7f][^\x00-\x1f\x7f]*$",
        ),
    ] = None
    mime_type: MimeType
    content: ContentText
    content_sha256: Sha256Digest
    byte_size: int = Field(ge=0, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    truncated: bool

    @field_validator("relative_path")
    @classmethod
    def _safe_relative_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if any(segment in {"", ".", ".."} for segment in value.split("/")):
            raise ValueError("relative_path contains an unsafe path segment")
        return value

    @model_validator(mode="after")
    def _content_matches_metadata(self) -> ArtifactDocumentPreviewV1:
        returned_bytes = len(self.content.encode("utf-8"))
        if returned_bytes > self.byte_size:
            raise ValueError("document preview exceeds authoritative byte size")
        if not self.truncated:
            if returned_bytes != self.byte_size:
                raise ValueError("complete document byte size does not match content")
            if hashlib.sha256(self.content.encode("utf-8")).hexdigest() != self.content_sha256:
                raise ValueError("complete document digest does not match content")
        return self


class ArtifactContentV1(ContractModel):
    schema_version: Literal["1"] = "1"
    artifact_id: OpaqueId
    artifact_type: ArtifactType
    documents: list[ArtifactDocumentPreviewV1] = Field(
        max_length=MAX_ARTIFACT_PREVIEW_DOCUMENTS
    )
    total_documents: int = Field(ge=0, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    total_utf8_bytes: int = Field(ge=0, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    returned_utf8_bytes: int = Field(ge=0, le=MAX_ARTIFACT_PREVIEW_UTF8_BYTES)
    truncated: bool

    @model_validator(mode="after")
    def _valid_preview_budget(self) -> ArtifactContentV1:
        returned = sum(len(document.content.encode("utf-8")) for document in self.documents)
        if returned > MAX_ARTIFACT_PREVIEW_UTF8_BYTES:
            raise ValueError("artifact preview exceeds the aggregate UTF-8 byte budget")
        if returned != self.returned_utf8_bytes:
            raise ValueError("returned_utf8_bytes does not match document previews")
        if len(self.documents) > self.total_documents:
            raise ValueError("returned documents exceed total_documents")
        if self.returned_utf8_bytes > self.total_utf8_bytes:
            raise ValueError("returned bytes exceed total_utf8_bytes")
        actually_truncated = (
            len(self.documents) < self.total_documents
            or self.returned_utf8_bytes < self.total_utf8_bytes
            or any(document.truncated for document in self.documents)
        )
        if self.truncated != actually_truncated:
            raise ValueError("truncated does not match the preview totals")
        return self


class DiffLineKind(StrEnum):
    CONTEXT = "context"
    ADDED = "added"
    REMOVED = "removed"


class ArtifactDiffLineV1(ContractModel):
    kind: DiffLineKind
    old_line_number: int | None = Field(default=None, ge=1, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    new_line_number: int | None = Field(default=None, ge=1, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    text: LogText

    @model_validator(mode="after")
    def _line_numbers_match_kind(self) -> ArtifactDiffLineV1:
        if self.kind is DiffLineKind.CONTEXT and (
            self.old_line_number is None or self.new_line_number is None
        ):
            raise ValueError("context lines require old and new line numbers")
        if self.kind is DiffLineKind.ADDED and (
            self.old_line_number is not None or self.new_line_number is None
        ):
            raise ValueError("added lines require only a new line number")
        if self.kind is DiffLineKind.REMOVED and (
            self.old_line_number is None or self.new_line_number is not None
        ):
            raise ValueError("removed lines require only an old line number")
        return self


class ArtifactDiffHunkV1(ContractModel):
    old_start: int = Field(ge=0, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    old_count: int = Field(ge=0, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    new_start: int = Field(ge=0, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    new_count: int = Field(ge=0, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    lines: list[ArtifactDiffLineV1] = Field(max_length=512)


class ArtifactDiffV1(ContractModel):
    schema_version: Literal["1"] = "1"
    artifact_id: OpaqueId
    previous_artifact_id: OpaqueId | None = None
    hunks: list[ArtifactDiffHunkV1] = Field(max_length=MAX_ARTIFACT_DIFF_HUNKS)
    total_hunks: int = Field(ge=0, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    total_lines: int = Field(ge=0, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    truncated: bool

    @model_validator(mode="after")
    def _valid_diff_budget(self) -> ArtifactDiffV1:
        line_count = sum(len(hunk.lines) for hunk in self.hunks)
        if line_count > MAX_ARTIFACT_DIFF_LINES:
            raise ValueError("artifact diff exceeds the line budget")
        text_bytes = sum(
            len(line.text.encode("utf-8")) for hunk in self.hunks for line in hunk.lines
        )
        if text_bytes > MAX_ARTIFACT_PREVIEW_UTF8_BYTES:
            raise ValueError("artifact diff exceeds the aggregate UTF-8 byte budget")
        if len(self.hunks) > self.total_hunks or line_count > self.total_lines:
            raise ValueError("returned diff exceeds authoritative totals")
        actually_truncated = len(self.hunks) < self.total_hunks or line_count < self.total_lines
        if self.truncated != actually_truncated:
            raise ValueError("truncated does not match diff totals")
        return self


class ServicePageV1(ContractModel):
    schema_version: Literal["1"] = "1"
    items: list[ServiceSummaryV1] = Field(max_length=64)
    next_cursor: Cursor | None = None
    has_more: bool


class ServiceRestartRequestV1(ContractModel):
    schema_version: Literal["1"] = "1"
    reason: Annotated[str, StringConstraints(min_length=1, max_length=512)]


class ServiceActionV1(ContractModel):
    schema_version: Literal["1"] = "1"
    action_id: OpaqueId
    action: Literal["restart"]
    service: ServiceSummaryV1
    accepted_at: UtcTimestamp
    etag: StrongETag

    @model_validator(mode="after")
    def _etag_matches_service(self) -> ServiceActionV1:
        if self.etag != self.service.etag:
            raise ValueError("action ETag must match the returned service ETag")
        return self


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
    updated_at: UtcTimestamp
    observed_at: UtcTimestamp
    finished_at: UtcTimestamp | None = None
    error: ApiErrorV1 | None = None
    etag: StrongETag

    @model_validator(mode="after")
    def _valid_status_shape(self) -> DiagnosticV1:
        terminal = self.status in {DiagnosticStatus.SUCCEEDED, DiagnosticStatus.FAILED}
        if terminal != (self.finished_at is not None):
            raise ValueError("finished_at is required only for terminal diagnostics")
        if (self.status is DiagnosticStatus.FAILED) != (self.error is not None):
            raise ValueError("error is required only for failed diagnostics")
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
    reclaimed_bytes: int = Field(ge=0, le=MAX_JAVASCRIPT_SAFE_INTEGER)
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
    sequence: int = Field(ge=0, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    occurred_at: UtcTimestamp


class ResourceChangeType(StrEnum):
    PROJECT = "project"
    RUN = "run"
    TIMELINE_ENTRY = "timeline_entry"
    LOG_ENTRY = "log_entry"
    ARTIFACT = "artifact"
    REVISION = "revision"
    SERVICE = "service"
    DIAGNOSTIC = "diagnostic"


class ResourceChangeIdentityV1(ContractModel):
    change_id: OpaqueId
    resource_type: ResourceChangeType
    resource_id: OpaqueId
    resource_etag: StrongETag | None = None
    content_sha256: Sha256Digest | None = None

    @model_validator(mode="after")
    def _has_authoritative_identity(self) -> ResourceChangeIdentityV1:
        if self.resource_etag is None and self.content_sha256 is None:
            raise ValueError("resource change requires an ETag or content digest")
        return self


class RunUpdatedEventV1(EventBaseV1):
    event: Literal["run.updated.v1"]
    change: ResourceChangeIdentityV1
    payload: RunSummaryV1


class TimelineAppendedPayloadV1(ContractModel):
    run_id: OpaqueId
    entry: TimelineEntryV1


class RunTimelineAppendedEventV1(EventBaseV1):
    event: Literal["run.timeline_appended.v1"]
    change: ResourceChangeIdentityV1
    payload: TimelineAppendedPayloadV1


class ProjectUpdatedEventV1(EventBaseV1):
    event: Literal["project.updated.v1"]
    change: ResourceChangeIdentityV1
    payload: ProjectSummaryV1


class ServiceUpdatedEventV1(EventBaseV1):
    event: Literal["service.updated.v1"]
    change: ResourceChangeIdentityV1
    payload: ServiceSummaryV1


class DiagnosticUpdatedEventV1(EventBaseV1):
    event: Literal["diagnostic.updated.v1"]
    change: ResourceChangeIdentityV1
    payload: DiagnosticV1


class ArtifactUpdatedEventV1(EventBaseV1):
    event: Literal["artifact.updated.v1"]
    change: ResourceChangeIdentityV1
    payload: ArtifactSummaryV1


class LogAppendedEventV1(EventBaseV1):
    event: Literal["log.appended.v1"]
    change: ResourceChangeIdentityV1
    payload: LogEntryV1


class RevisionSuccessorTransitionUpdatedEventV1(EventBaseV1):
    event: Literal["revision.successor_transition_updated.v1"]
    change: ResourceChangeIdentityV1
    payload: RevisionHeadV1


class RevisionActivatedEventV1(EventBaseV1):
    event: Literal["revision.activated.v1"]
    change: ResourceChangeIdentityV1
    payload: RevisionV1


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
    | ArtifactUpdatedEventV1
    | LogAppendedEventV1
    | RevisionSuccessorTransitionUpdatedEventV1
    | RevisionActivatedEventV1
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
