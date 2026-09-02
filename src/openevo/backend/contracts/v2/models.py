"""Strict data models for the Core Control API v2 authority boundary."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from openevo.codex_models import validate_codex_model_ref
from openevo.evolution.framework.capabilities import EvolutionCapabilitiesV1
from openevo.evolution.framework.plan import ProjectEvolutionTargetMap


MAX_JAVASCRIPT_SAFE_INTEGER = (1 << 53) - 1
MAX_SNAPSHOT_ENTRIES = 100_000
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024 * 1024
MAX_PROJECT_CONFIG_BYTES = 1024 * 1024
MAX_PROJECT_CONFIG_JSON_DEPTH = 24
MAX_WORKSPACE_CHUNK_BYTES = 8 * 1024 * 1024
MAX_WORKSPACE_CHUNKS = 65_536


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
    "self-deployed",
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
        if self.workspace_snapshot != self.predecessor_project_head.workspace_snapshot:
            raise ValueError("workspace snapshot differs from the predecessor project head")
        if self.registry_sha256 != self.predecessor_project_head.registry_sha256:
            raise ValueError("admission registry digest differs from the predecessor head")
        if self.admission_sha256 != task_admission_sha256_for(self):
            raise ValueError("task admission digest does not match its immutable pins")
        return self


def task_admission_sha256_for(admission: TaskAdmissionRefV2) -> str:
    """Hash all immutable admission pins except the self-referential digest field."""

    if type(admission) is not TaskAdmissionRefV2:
        raise TypeError("task admission digest requires TaskAdmissionRefV2")
    payload = admission.model_dump(mode="json", exclude={"admission_sha256"})
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
    expected_successor_generation: int = Field(ge=1, le=MAX_JAVASCRIPT_SAFE_INTEGER)
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


class ContractOfferV2(ContractModel):
    schema_version: Literal["2"] = "2"
    api_major: Literal[1, 2]
    openapi_sha256: Sha256Digest
    event_schema_sha256: Sha256Digest
    access: Literal["read_only_migration", "mutation"]
    mutation_compatible: bool

    @model_validator(mode="after")
    def _valid_access(self) -> ContractOfferV2:
        if self.api_major == 1 and (
            self.access != "read_only_migration" or self.mutation_compatible
        ):
            raise ValueError("Core Control API v1 is read-only migration input")
        if self.api_major == 2 and ((self.access == "mutation") != self.mutation_compatible):
            raise ValueError("v2 access and mutation compatibility differ")
        return self


class VersionResponseV2(ContractModel):
    schema_version: Literal["2"] = "2"
    api_name: Literal["openevo-core-control-api"]
    preferred_major: Literal[2]
    supported_majors: list[Literal[1, 2]] = Field(min_length=1, max_length=2)
    mutation_major: Literal[2]
    contracts: list[ContractOfferV2] = Field(min_length=1, max_length=2)
    release_version: ShortText
    build_id: Sha256Digest
    source_commit: SourceCommit
    build_channel: Literal["release", "development", "test"]
    provider_kind: Literal["openevo_daemon"]
    feature_flags: list[OpaqueId] = Field(min_length=1, max_length=128)
    feature_set_sha256: Sha256Digest
    registry_sha256: Sha256Digest
    runtime_contract_sha256: Sha256Digest
    mutation_compatible: bool

    @model_validator(mode="after")
    def _bind_negotiated_authority(self) -> VersionResponseV2:
        if self.supported_majors != sorted(set(self.supported_majors)):
            raise ValueError("supported API majors must be sorted and unique")
        contract_majors = [offer.api_major for offer in self.contracts]
        if contract_majors != self.supported_majors:
            raise ValueError("contract offers must exactly match supported API majors")
        v2_offer = next(
            (offer for offer in self.contracts if offer.api_major == self.mutation_major),
            None,
        )
        if v2_offer is None or v2_offer.mutation_compatible != self.mutation_compatible:
            raise ValueError("mutation major does not bind a matching contract offer")
        if self.mutation_compatible and v2_offer.access != "mutation":
            raise ValueError("mutation major is not available for mutation")
        if self.feature_flags != sorted(set(self.feature_flags)):
            raise ValueError("feature flags must be sorted and unique")
        encoded_features = json.dumps(
            self.feature_flags,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if hashlib.sha256(encoded_features).hexdigest() != self.feature_set_sha256:
            raise ValueError("feature-set digest does not match feature flags")
        return self


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


class ScienceTaskConfigV2(ContractModel):
    """Ordinary-user task text saved as Daemon-owned project authority."""

    title: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    objective: Annotated[str, StringConstraints(min_length=1, max_length=65_536)]


class ScienceWorkspaceSourceV2(ContractModel):
    """Workspace intent without a local path, URI, or caller-created snapshot."""

    kind: Literal["scratch", "native_folder_snapshot"]
    display_name: Annotated[str, StringConstraints(min_length=1, max_length=256)]


class CodexSubscriptionExecutionSettingsV2(ContractModel):
    """Closed desired settings for the release Subscription vertical."""

    mode: Literal["codex_subscription_transcript"]
    capture_mode: Literal["transcript"]
    token_level_metrics_available: Literal[False]
    harness_id: Literal["codex"]
    codex_model: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    reasoning_effort: Literal["low", "medium", "high", "xhigh"] | None
    token_limit: int = Field(ge=1, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    task_network_allow_internet: bool

    @field_validator("codex_model")
    @classmethod
    def _valid_codex_model(cls, value: str) -> str:
        model = validate_codex_model_ref(value, field_name="execution.codex_model")
        if (
            "://" in model
            or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", model) is not None
            or model.startswith(("/", "\\", ".", "~"))
            or "\\" in model
            or any(part in {".", ".."} for part in model.split("/"))
        ):
            raise ValueError("execution.codex_model must not be a path or URI")
        return model


class SelfDeployedExecutionSettingsV2(ContractModel):
    """A release profile or an immutable daemon-managed Hugging Face model."""

    mode: Literal["self-deployed"]
    capture_mode: Literal["transcript"]
    token_level_metrics_available: Literal[False]
    harness_id: Literal["codex"]
    model_profile_id: Literal["qwen3-0.6b-v1"] | None = None
    model_resource_id: OpaqueId | None = None
    repository_id: Annotated[str, StringConstraints(min_length=3, max_length=193)] | None = None
    model_revision: Annotated[
        str, StringConstraints(pattern=r"^[0-9a-f]{40}$")
    ] | None = None
    token_limit: int = Field(ge=1, le=8_192)
    task_network_allow_internet: bool

    @model_validator(mode="after")
    def _one_model_authority(self) -> "SelfDeployedExecutionSettingsV2":
        legacy = self.model_profile_id is not None
        managed = all(
            value is not None
            for value in (self.model_resource_id, self.repository_id, self.model_revision)
        )
        if legacy == managed:
            raise ValueError("self-deployed execution must select exactly one model authority")
        if not legacy and any(
            value is None
            for value in (self.model_resource_id, self.repository_id, self.model_revision)
        ):
            raise ValueError("daemon-managed model identity is incomplete")
        if self.repository_id is not None and re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}",
            self.repository_id,
        ) is None:
            raise ValueError("execution.repository_id must use owner/repository")
        return self

    @property
    def codex_model(self) -> str:
        """Concrete served-model identity without exposing its host path."""

        if self.repository_id is not None:
            return self.repository_id

        from openevo.runtime.self_deployed import (
            require_release_self_deployed_model_profile,
        )

        assert self.model_profile_id is not None
        return require_release_self_deployed_model_profile(self.model_profile_id).model_id

    @property
    def reasoning_effort(self) -> None:
        return None


ScienceExecutionSettingsV2: TypeAlias = Annotated[
    CodexSubscriptionExecutionSettingsV2 | SelfDeployedExecutionSettingsV2,
    Field(discriminator="mode"),
]


class ScienceEvolutionConfigV2(ContractModel):
    targets: ProjectEvolutionTargetMap


class ScienceProjectConfigV2(ContractModel):
    """Complete canonical Science configuration persisted behind its digest."""

    schema_version: Literal["2"] = "2"
    task: ScienceTaskConfigV2
    workspace: ScienceWorkspaceSourceV2
    execution: ScienceExecutionSettingsV2
    evolution: ScienceEvolutionConfigV2

    @model_validator(mode="after")
    def _bounded_canonical_document(self) -> ScienceProjectConfigV2:
        if len(_canonical_json_bytes(self)) > MAX_PROJECT_CONFIG_BYTES:
            raise ValueError("project config exceeds the 1 MiB canonical byte limit")
        return self


def _canonical_json_bytes(value: BaseModel) -> bytes:
    return json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def project_config_sha256_for(config: ScienceProjectConfigV2) -> str:
    """Digest the exact closed project config after strict normalization."""

    if type(config) is not ScienceProjectConfigV2:
        raise TypeError("project config digest requires ScienceProjectConfigV2")
    return hashlib.sha256(_canonical_json_bytes(config)).hexdigest()


class ProjectCreateV2(ContractModel):
    schema_version: Literal["2"] = "2"
    display_name: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    config: ScienceProjectConfigV2


class ProjectUpdateV2(ContractModel):
    schema_version: Literal["2"] = "2"
    expected_project_head_id: OpaqueId | None
    expected_project_head_manifest_sha256: Sha256Digest | None
    expected_project_config_sha256: Sha256Digest
    display_name: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    config: ScienceProjectConfigV2

    @model_validator(mode="after")
    def _head_cas_is_complete(self) -> ProjectUpdateV2:
        if (self.expected_project_head_id is None) != (
            self.expected_project_head_manifest_sha256 is None
        ):
            raise ValueError("expected project head ID and manifest must be present together")
        return self


class WorkspaceArchiveDeclarationV2(ContractModel):
    format: Literal["openevo_deterministic_tar_v1"]
    media_type: Literal["application/vnd.openevo.workspace-tar"]
    content_sha256: Sha256Digest
    byte_size: int = Field(ge=1024, le=MAX_SNAPSHOT_BYTES)
    entry_count: int = Field(ge=0, le=MAX_SNAPSHOT_ENTRIES)
    extracted_byte_size: int = Field(ge=0, le=MAX_SNAPSHOT_BYTES)

    @model_validator(mode="after")
    def _valid_archive_bounds(self) -> WorkspaceArchiveDeclarationV2:
        if self.byte_size % 512 != 0:
            raise ValueError("workspace archive byte size must be a multiple of 512")
        if self.entry_count == 0 and self.extracted_byte_size != 0:
            raise ValueError("an empty archive cannot declare extracted bytes")
        minimum_body_bytes = ((self.extracted_byte_size + 511) // 512) * 512
        minimum_archive_bytes = (self.entry_count + 2) * 512 + minimum_body_bytes
        if self.byte_size < minimum_archive_bytes:
            raise ValueError("workspace archive is too small for its declared contents")
        if self.entry_count == 0 and self.byte_size != 1024:
            raise ValueError("an empty deterministic archive is exactly 1024 bytes")
        return self


class WorkspaceUploadCreateV2(ContractModel):
    schema_version: Literal["2"] = "2"
    expected_project_head_id: OpaqueId | None
    expected_project_head_manifest_sha256: Sha256Digest | None
    expected_project_config_sha256: Sha256Digest
    archive: WorkspaceArchiveDeclarationV2
    chunk_byte_size: int = Field(ge=1024, le=MAX_WORKSPACE_CHUNK_BYTES)
    chunk_count: int = Field(ge=1, le=MAX_WORKSPACE_CHUNKS)

    @model_validator(mode="after")
    def _valid_upload_shape(self) -> WorkspaceUploadCreateV2:
        if (self.expected_project_head_id is None) != (
            self.expected_project_head_manifest_sha256 is None
        ):
            raise ValueError("expected project head ID and manifest must be present together")
        expected_chunks = (
            self.archive.byte_size + self.chunk_byte_size - 1
        ) // self.chunk_byte_size
        if self.chunk_count != expected_chunks:
            raise ValueError("workspace upload chunk count does not match its byte sizes")
        return self


class WorkspaceUploadSessionV2(ContractModel):
    schema_version: Literal["2"] = "2"
    upload_id: OpaqueId
    project_id: OpaqueId
    state: Literal["open", "finalizing", "finalized", "aborted"]
    expected_project_head_id: OpaqueId | None
    expected_project_head_manifest_sha256: Sha256Digest | None
    expected_project_config_sha256: Sha256Digest
    archive: WorkspaceArchiveDeclarationV2
    chunk_byte_size: int = Field(ge=1024, le=MAX_WORKSPACE_CHUNK_BYTES)
    chunk_count: int = Field(ge=1, le=MAX_WORKSPACE_CHUNKS)
    next_chunk_index: int = Field(ge=0, le=MAX_WORKSPACE_CHUNKS)
    accepted_byte_size: int = Field(ge=0, le=MAX_SNAPSHOT_BYTES)
    workspace_snapshot: WorkspaceSnapshotRefV2 | None
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    etag: StrongETag

    @model_validator(mode="after")
    def _valid_session_shape(self) -> WorkspaceUploadSessionV2:
        if (self.expected_project_head_id is None) != (
            self.expected_project_head_manifest_sha256 is None
        ):
            raise ValueError("expected project head ID and manifest must be present together")
        expected_chunks = (
            self.archive.byte_size + self.chunk_byte_size - 1
        ) // self.chunk_byte_size
        if self.chunk_count != expected_chunks:
            raise ValueError("workspace upload chunk count does not match its byte sizes")
        if self.next_chunk_index > self.chunk_count:
            raise ValueError("next chunk index exceeds the declared chunk count")
        expected_bytes = min(
            self.next_chunk_index * self.chunk_byte_size,
            self.archive.byte_size,
        )
        if self.accepted_byte_size != expected_bytes:
            raise ValueError("accepted byte size does not match the next chunk index")
        if self.state in {"finalizing", "finalized"} and (
            self.next_chunk_index != self.chunk_count
            or self.accepted_byte_size != self.archive.byte_size
        ):
            raise ValueError("finalizing uploads must contain every declared byte")
        if (self.state == "finalized") != (self.workspace_snapshot is not None):
            raise ValueError("only a finalized upload exposes a workspace snapshot")
        if (
            self.workspace_snapshot is not None
            and self.workspace_snapshot.project_id != self.project_id
        ):
            raise ValueError("workspace snapshot belongs to another project")
        return self


class WorkspaceUploadFinalizeV2(ContractModel):
    schema_version: Literal["2"] = "2"
    expected_content_sha256: Sha256Digest


class WorkspaceUploadAbortV2(ContractModel):
    schema_version: Literal["2"] = "2"
    reason: Literal["user_cancelled", "superseded", "project_deleted"]


CapabilitiesResponseV2 = EvolutionCapabilitiesV1


class ProjectValidationRequestV2(ContractModel):
    schema_version: Literal["2"] = "2"
    expected_project_head_id: OpaqueId
    expected_project_head_manifest_sha256: Sha256Digest
    expected_project_config_sha256: Sha256Digest
    expected_registry_sha256: Sha256Digest


class ProjectValidationCheckV2(ContractModel):
    check_id: OpaqueId
    status: Literal["passed", "failed", "unavailable"]
    message: Description
    target_id: OpaqueId | None = None
    method_id: OpaqueId | None = None


class ProjectValidationResponseV2(ContractModel):
    schema_version: Literal["2"] = "2"
    project_id: OpaqueId
    valid: bool
    registry_sha256: Sha256Digest
    checks: list[ProjectValidationCheckV2] = Field(max_length=256)
    validated_at: UtcTimestamp


class ProjectV2(ContractModel):
    schema_version: Literal["2"] = "2"
    project_id: OpaqueId
    display_name: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    config: ScienceProjectConfigV2
    project_config_sha256: Sha256Digest
    active_project_head: ProjectHeadRefV2 | None
    admission_etag: StrongETag | None
    state: Literal["ready", "transitioning", "not_ready", "needs_attention"]
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    etag: StrongETag

    @model_validator(mode="after")
    def _head_belongs_to_project(self) -> ProjectV2:
        if self.project_config_sha256 != project_config_sha256_for(self.config):
            raise ValueError("project config digest does not match canonical config bytes")
        if (
            self.active_project_head is not None
            and self.active_project_head.project_id != self.project_id
        ):
            raise ValueError("active project head belongs to another project")
        if (self.active_project_head is None) != (self.admission_etag is None):
            raise ValueError("an active project head and admission ETag must appear together")
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
    expected_project_admission_etag: StrongETag
    expected_project_head_id: OpaqueId
    expected_project_head_manifest_sha256: Sha256Digest
    expected_project_config_sha256: Sha256Digest


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
    attempts: list[AttemptRefV2] = Field(min_length=1, max_length=100)
    authoritative_attempt_id: OpaqueId | None
    successor_transition: SuccessorTransitionRefV2 | None
    state: TaskStateV2
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    etag: StrongETag

    @model_validator(mode="after")
    def _bind_task_ownership(self) -> TaskV2:
        if self.admission.task_id != self.task_id or self.admission.project_id != self.project_id:
            raise ValueError("admission does not belong to this task")
        expected_ordinal = 1
        attempt_ids: set[str] = set()
        for attempt in self.attempts:
            if (
                attempt.task_id != self.task_id
                or attempt.project_id != self.project_id
                or attempt.task_admission_id != self.admission.task_admission_id
                or attempt.admission_sha256 != self.admission.admission_sha256
                or attempt.predecessor_project_head_id
                != self.admission.predecessor_project_head.project_head_id
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

    @model_validator(mode="after")
    def _bind_project(self) -> TaskAdmittedEventV2:
        if self.admission.project_id != self.project_id:
            raise ValueError("event project differs from its task admission")
        return self


class AttemptAppendedEventV2(EventBaseV2):
    event_type: Literal["attempt_appended"]
    attempt: AttemptRefV2

    @model_validator(mode="after")
    def _bind_project(self) -> AttemptAppendedEventV2:
        if self.attempt.project_id != self.project_id:
            raise ValueError("event project differs from its attempt")
        return self


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

    @model_validator(mode="after")
    def _bind_project(self) -> TransitionChangedEventV2:
        if self.transition.project_id != self.project_id:
            raise ValueError("event project differs from its transition")
        if self.progress_completed > self.progress_total:
            raise ValueError("transition progress exceeds total")
        return self


class EvolutionRevisionCommittedEventV2(EventBaseV2):
    event_type: Literal["evolution_revision_committed"]
    successor_transition_id: OpaqueId
    evolution_revision: EvolutionRevisionRefV2

    @model_validator(mode="after")
    def _bind_project(self) -> EvolutionRevisionCommittedEventV2:
        if self.evolution_revision.project_id != self.project_id:
            raise ValueError("event project differs from its evolution revision")
        return self


class RuntimeContextCommittedEventV2(EventBaseV2):
    event_type: Literal["runtime_context_committed"]
    successor_transition_id: OpaqueId
    runtime_context_snapshot: RuntimeContextSnapshotRefV2

    @model_validator(mode="after")
    def _bind_project(self) -> RuntimeContextCommittedEventV2:
        if self.runtime_context_snapshot.project_id != self.project_id:
            raise ValueError("event project differs from its runtime context")
        return self


class ProjectHeadActivatedEventV2(EventBaseV2):
    event_type: Literal["project_head_activated"]
    successor_transition_id: OpaqueId
    project_head: ProjectHeadRefV2

    @model_validator(mode="after")
    def _bind_project(self) -> ProjectHeadActivatedEventV2:
        if self.project_head.project_id != self.project_id:
            raise ValueError("event project differs from its project head")
        return self


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
    "CapabilitiesResponseV2",
    "CodexSubscriptionExecutionSettingsV2",
    "ContractModel",
    "ContractOfferV2",
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
    "ProjectUpdateV2",
    "ProjectValidationCheckV2",
    "ProjectValidationRequestV2",
    "ProjectValidationResponseV2",
    "ProjectV2",
    "RuntimeContextSnapshotRefV2",
    "ScienceEvolutionConfigV2",
    "ScienceProjectConfigV2",
    "ScienceExecutionSettingsV2",
    "ScienceTaskConfigV2",
    "ScienceWorkspaceSourceV2",
    "SelfDeployedExecutionSettingsV2",
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
    "WorkspaceArchiveDeclarationV2",
    "WorkspaceUploadAbortV2",
    "WorkspaceUploadCreateV2",
    "WorkspaceUploadFinalizeV2",
    "WorkspaceUploadSessionV2",
    "project_config_sha256_for",
    "task_admission_sha256_for",
]
