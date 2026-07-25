"""Immutable cross-session revision and task-admission contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
import re
from typing import Literal
import unicodedata
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, field_validator, model_validator

from openevo.evolution.context_materialization import MaterializedAdapter
from openevo.evolution.framework.contracts import (
    MAX_JAVASCRIPT_SAFE_INTEGER,
    CaptureMode,
    ExecutionMode,
    _Contract,
    _digest,
    _stable_id,
    canonical_digest,
    canonical_json,
)


MAX_REVISION_ADAPTERS = 128
MAX_REVISION_MANIFEST_BYTES = 1024 * 1024
MAX_TASK_EXECUTION_ENVELOPE_BYTES = 64 * 1024
MAX_TASK_EXECUTION_ITEMS = 128
MAX_EXECUTION_SNAPSHOT_BYTES = 64 * 1024


class RevisionError(ValueError):
    """Base error for revision and admission operations."""


class RevisionConflictError(RevisionError):
    """A stable revision identity was reused with different content."""


class RevisionIntegrityError(RevisionError):
    """Persisted or referenced revision state failed closed validation."""


class RevisionCapacityError(RevisionError):
    """The bounded durable ledger cannot accept another record."""


class RevisionNotFoundError(RevisionError):
    """A requested revision stream or record does not exist."""


class TaskAdmissionConflictError(RevisionError):
    """A task admission identity or state transition conflicts."""


class AdmissionStatus(StrEnum):
    QUEUED = "queued"
    ADMITTED = "admitted"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AdmissionQueueReason(StrEnum):
    REQUIRED_REVISION_UNCOMMITTED = "required_revision_uncommitted"


class ModelIdentitySource(StrEnum):
    HUGGING_FACE = "hugging_face"
    MANAGED_SNAPSHOT = "managed_snapshot"
    SUBSCRIPTION = "subscription"


SnapshotKind = Literal["project", "workspace", "task", "runtime", "deployment"]
RuntimeKind = Literal["container", "managed_runtime", "subscription_client"]
ServingKind = Literal["managed_deployment", "subscription"]

_MODEL_ID_SEGMENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z", re.ASCII)
_EXECUTION_SNAPSHOT_ID_RE = re.compile(r"exec-[0-9a-f]{64}\Z", re.ASCII)


def _identity_text(value: str) -> str:
    if (
        not value
        or len(value) > 4096
        or value != value.strip()
        or unicodedata.normalize("NFC", value) != value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or any(0xD800 <= ord(character) <= 0xDFFF for character in value)
    ):
        raise ValueError("must be normalized bounded identity text")
    return value


def _model_reference_text(value: str) -> str:
    value = _identity_text(value)
    parsed = urlsplit(value)
    if (
        value.startswith(("/", "\\", "~"))
        or "\\" in value
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("must be an opaque model identity, not a path or URI")
    return value


def _strict_integer(value: object) -> object:
    if type(value) is not int:
        raise ValueError("must be an integer without coercion")
    return value


def _canonical_utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("must be a timezone-aware UTC timestamp")
    return value


def _optional_canonical_utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return _canonical_utc_datetime(value)


def _ordered_stable_ids(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    for value in values:
        _stable_id(value)
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")
    return values


class ContentAddressedSnapshotRef(_Contract):
    """A secret-free immutable snapshot reference with a deterministic ID."""

    kind: SnapshotKind
    snapshot_id: str
    content_digest: str

    _digest = field_validator("content_digest")(_digest)

    @model_validator(mode="after")
    def _content_addressed_id(self) -> ContentAddressedSnapshotRef:
        expected = f"{self.kind}-snapshot-{self.content_digest}"
        if self.snapshot_id != expected:
            raise ValueError("snapshot ID does not match kind and content digest")
        _stable_id(self.snapshot_id)
        return self


def content_addressed_snapshot_ref(
    kind: SnapshotKind,
    content_digest: str,
) -> ContentAddressedSnapshotRef:
    _digest(content_digest)
    return ContentAddressedSnapshotRef(
        kind=kind,
        snapshot_id=f"{kind}-snapshot-{content_digest}",
        content_digest=content_digest,
    )


class ExecutionModelIdentity(_Contract):
    source: ModelIdentitySource
    model_id: str
    model_revision: str
    token_limit: int = Field(ge=1, le=MAX_JAVASCRIPT_SAFE_INTEGER)

    _text = field_validator("model_id", "model_revision")(_model_reference_text)
    _token_limit = field_validator("token_limit", mode="before")(_strict_integer)

    @model_validator(mode="after")
    def _source_shape(self) -> ExecutionModelIdentity:
        if self.source is ModelIdentitySource.HUGGING_FACE:
            segments = self.model_id.split("/")
            if len(segments) > 2 or any(
                _MODEL_ID_SEGMENT_RE.fullmatch(segment) is None for segment in segments
            ):
                raise ValueError("Hugging Face model ID is invalid")
        else:
            _stable_id(self.model_id)
            _stable_id(self.model_revision)
        return self


class ExecutionRuntimeIdentity(_Contract):
    kind: RuntimeKind
    harness_id: str
    harness_version: str
    image_digest: str
    policy_id: str
    policy_digest: str
    snapshot: ContentAddressedSnapshotRef

    _harness = field_validator("harness_id")(_stable_id)
    _identity_text_fields = field_validator("harness_version")(_identity_text)
    _policy = field_validator("policy_id")(_stable_id)
    _digests = field_validator("image_digest", "policy_digest")(_digest)

    @model_validator(mode="after")
    def _runtime_snapshot_kind(self) -> ExecutionRuntimeIdentity:
        if self.snapshot.kind != "runtime":
            raise ValueError("runtime identity requires a runtime snapshot")
        return self


class ExecutionServingIdentity(_Contract):
    kind: ServingKind
    deployment_id: str
    snapshot: ContentAddressedSnapshotRef
    endpoint: None = None

    _deployment = field_validator("deployment_id")(_stable_id)

    @model_validator(mode="after")
    def _deployment_snapshot_kind(self) -> ExecutionServingIdentity:
        if self.snapshot.kind != "deployment":
            raise ValueError("serving identity requires a deployment snapshot")
        return self


def execution_task_network_policy_digest(
    *,
    policy_id: str,
    allow_internet: bool,
) -> str:
    """Return the canonical identity of one closed effective task-network policy."""

    _stable_id(policy_id)
    if type(allow_internet) is not bool:
        raise ValueError("task-network allow_internet must be a boolean")
    return canonical_digest(
        {
            "task_network_policy_contract_version": "1",
            "policy_id": policy_id,
            "allow_internet": allow_internet,
        }
    )


class ExecutionTaskNetworkPolicy(_Contract):
    task_network_policy_contract_version: Literal["1"] = "1"
    policy_id: str
    allow_internet: bool
    policy_digest: str

    _policy = field_validator("policy_id")(_stable_id)
    _digest = field_validator("policy_digest")(_digest)

    @model_validator(mode="after")
    def _identity_matches_policy(self) -> ExecutionTaskNetworkPolicy:
        expected = execution_task_network_policy_digest(
            policy_id=self.policy_id,
            allow_internet=self.allow_internet,
        )
        if self.policy_digest != expected:
            raise ValueError("task-network policy digest is inconsistent")
        return self


class ExecutionSnapshotV1(_Contract):
    """Closed typed facts whose persistence requires a verified producer seal."""

    execution_snapshot_contract_version: Literal["1"] = "1"
    execution_mode: ExecutionMode
    capture_mode: CaptureMode
    token_level_metrics_available: bool
    model: ExecutionModelIdentity
    runtime: ExecutionRuntimeIdentity
    serving: ExecutionServingIdentity
    task_network: ExecutionTaskNetworkPolicy

    @model_validator(mode="after")
    def _mode_specific_shape(self) -> ExecutionSnapshotV1:
        subscription = self.execution_mode is ExecutionMode.SUBSCRIPTION
        if subscription:
            if self.capture_mode is not CaptureMode.TRANSCRIPT:
                raise ValueError("subscription execution requires transcript capture")
            if self.token_level_metrics_available:
                raise ValueError("subscription execution cannot expose token-level metrics")
            if self.model.source is not ModelIdentitySource.SUBSCRIPTION:
                raise ValueError("subscription execution requires a subscription model")
            if self.runtime.kind != "subscription_client":
                raise ValueError("subscription execution requires a subscription client runtime")
            if self.serving.kind != "subscription":
                raise ValueError("subscription execution requires subscription serving")
        else:
            if self.model.source is ModelIdentitySource.SUBSCRIPTION:
                raise ValueError("self-deployed execution cannot use a subscription model")
            if self.runtime.kind == "subscription_client":
                raise ValueError("self-deployed execution cannot use a subscription client")
            if self.serving.kind != "managed_deployment":
                raise ValueError("self-deployed execution requires managed serving")
        if (
            self.capture_mode is CaptureMode.TRANSCRIPT
            and self.token_level_metrics_available
        ):
            raise ValueError("transcript capture cannot expose token-level metrics")
        if len(canonical_json(self).encode("utf-8")) > MAX_EXECUTION_SNAPSHOT_BYTES:
            raise ValueError("execution snapshot exceeds the byte limit")
        return self


def execution_snapshot_id_for_snapshot(snapshot: ExecutionSnapshotV1) -> str:
    return f"exec-{canonical_digest(snapshot)}"


_VERIFIED_EXECUTION_SNAPSHOT_SEAL = object()


@dataclass(frozen=True, slots=True, init=False)
class VerifiedExecutionSnapshot:
    """Ephemeral snapshot sealed only by a verified producer boundary."""

    snapshot: ExecutionSnapshotV1
    producer_id: str
    _verification_seal: object = field(repr=False, compare=False)

    def __new__(cls, *_args: object, **_kwargs: object) -> VerifiedExecutionSnapshot:
        raise TypeError("execution snapshots are issued only by a verified producer")


def require_verified_execution_snapshot(
    verified: VerifiedExecutionSnapshot,
) -> VerifiedExecutionSnapshot:
    """Reject observations that were not sealed by a verified producer."""

    if (
        type(verified) is not VerifiedExecutionSnapshot
        or getattr(verified, "_verification_seal", None) is not _VERIFIED_EXECUTION_SNAPSHOT_SEAL
    ):
        raise TypeError("execution snapshot was not issued by a verified producer")
    _stable_id(verified.producer_id)
    ExecutionSnapshotV1.model_validate(verified.snapshot.model_dump(mode="python"))
    return verified


class ExecutionSnapshotRecord(_Contract):
    execution_snapshot_id: str
    snapshot_digest: str
    producer_id: str
    snapshot: ExecutionSnapshotV1
    created_at: datetime

    _digest = field_validator("snapshot_digest")(_digest)
    _producer = field_validator("producer_id")(_stable_id)
    _created = field_validator("created_at")(_canonical_utc_datetime)

    @model_validator(mode="after")
    def _identity_matches_snapshot(self) -> ExecutionSnapshotRecord:
        digest = canonical_digest(self.snapshot)
        if self.snapshot_digest != digest or self.execution_snapshot_id != f"exec-{digest}":
            raise ValueError("execution snapshot record identity is inconsistent")
        return self


class RevisionContextIdentity(_Contract):
    context_id: str
    manifest_digest: str
    registry_digest: str
    request_digest: str
    artifact_ids: tuple[str, ...] = Field(default=(), max_length=MAX_TASK_EXECUTION_ITEMS)

    _context = field_validator("context_id")(_stable_id)
    _digests = field_validator(
        "manifest_digest",
        "registry_digest",
        "request_digest",
    )(_digest)

    @field_validator("artifact_ids")
    @classmethod
    def _artifacts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _ordered_stable_ids(value, label="context artifact IDs")


class TaskExecutionEnvelopeV1(_Contract):
    """Ephemeral closed identity envelope constructed from immutable snapshots."""

    task_envelope_contract_version: Literal["1"] = "1"
    project_id: str
    project_snapshot: ContentAddressedSnapshotRef
    workspace_snapshot: ContentAddressedSnapshotRef
    task_id: str
    task_snapshot: ContentAddressedSnapshotRef
    execution_mode: ExecutionMode
    capture_mode: CaptureMode
    execution_snapshot_id: str
    context_id: str
    context_artifact_ids: tuple[str, ...] = Field(
        default=(),
        max_length=MAX_TASK_EXECUTION_ITEMS,
    )
    artifact_families: tuple[str, ...] = Field(
        min_length=1,
        max_length=MAX_TASK_EXECUTION_ITEMS,
    )
    method_ids: tuple[str, ...] = Field(
        min_length=1,
        max_length=MAX_TASK_EXECUTION_ITEMS,
    )

    _ids = field_validator("project_id", "task_id", "context_id")(_stable_id)

    @field_validator("execution_snapshot_id")
    @classmethod
    def _execution_snapshot_id(cls, value: str) -> str:
        if _EXECUTION_SNAPSHOT_ID_RE.fullmatch(value) is None:
            raise ValueError("execution snapshot ID is invalid")
        return value

    @field_validator("context_artifact_ids", "artifact_families", "method_ids")
    @classmethod
    def _ordered_ids(cls, value: tuple[str, ...], info) -> tuple[str, ...]:
        return _ordered_stable_ids(value, label=info.field_name)

    @model_validator(mode="after")
    def _envelope_shape(self) -> TaskExecutionEnvelopeV1:
        expected_ref_kinds = (
            (self.project_snapshot, "project"),
            (self.workspace_snapshot, "workspace"),
            (self.task_snapshot, "task"),
        )
        if any(reference.kind != kind for reference, kind in expected_ref_kinds):
            raise ValueError("task envelope snapshot reference kind is invalid")
        if (
            self.execution_mode is ExecutionMode.SUBSCRIPTION
            and self.capture_mode is not CaptureMode.TRANSCRIPT
        ):
            raise ValueError("subscription execution requires transcript capture")
        if len(canonical_json(self).encode("utf-8")) > MAX_TASK_EXECUTION_ENVELOPE_BYTES:
            raise ValueError("task execution envelope exceeds the byte limit")
        return self


class TaskAdmissionIntent(_Contract):
    admission_intent_contract_version: Literal["1"] = "1"
    stream_id: str
    task_id: str
    required_generation: int = Field(ge=0, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    idempotency_key: str

    _ids = field_validator("stream_id", "task_id", "idempotency_key")(_stable_id)
    _generation = field_validator("required_generation", mode="before")(_strict_integer)


class RevisionManifestV1(_Contract):
    revision_contract_version: Literal["1"] = "1"
    stream_id: str
    generation: int = Field(ge=0, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    predecessor_revision_id: str | None = None
    project_snapshot: ContentAddressedSnapshotRef
    workspace_snapshot: ContentAddressedSnapshotRef
    context: RevisionContextIdentity
    execution_snapshot_id: str
    execution_snapshot_digest: str
    execution_snapshot: ExecutionSnapshotV1
    adapters: tuple[MaterializedAdapter, ...] = Field(
        default=(),
        max_length=MAX_REVISION_ADAPTERS,
    )

    _stream = field_validator("stream_id")(_stable_id)
    _generation = field_validator("generation", mode="before")(_strict_integer)
    _execution_digest = field_validator("execution_snapshot_digest")(_digest)

    @field_validator("execution_snapshot_id")
    @classmethod
    def _execution_id(cls, value: str) -> str:
        if _EXECUTION_SNAPSHOT_ID_RE.fullmatch(value) is None:
            raise ValueError("execution snapshot ID is invalid")
        return value

    @field_validator("predecessor_revision_id")
    @classmethod
    def _predecessor(cls, value: str | None) -> str | None:
        if value is None:
            return None
        _stable_id(value)
        if not value.startswith("rev-") or len(value) != 68:
            raise ValueError("predecessor revision ID is invalid")
        _digest(value.removeprefix("rev-"))
        return value

    @model_validator(mode="after")
    def _revision_shape(self) -> RevisionManifestV1:
        if (self.generation == 0) != (self.predecessor_revision_id is None):
            raise ValueError("only a genesis revision may omit its predecessor")
        if self.project_snapshot.kind != "project":
            raise ValueError("revision requires a project snapshot")
        if self.workspace_snapshot.kind != "workspace":
            raise ValueError("revision requires a workspace snapshot")
        execution_digest = canonical_digest(self.execution_snapshot)
        if (
            self.execution_snapshot_digest != execution_digest
            or self.execution_snapshot_id != f"exec-{execution_digest}"
        ):
            raise ValueError("revision execution snapshot identity is inconsistent")
        adapter_ids = tuple(adapter.adapter_id for adapter in self.adapters)
        if len(adapter_ids) != len(set(adapter_ids)):
            raise ValueError("revision adapter IDs must be unique")
        if any(
            adapter.base_model != self.execution_snapshot.model.model_id
            for adapter in self.adapters
        ):
            raise ValueError("revision adapters must match the pinned model")
        if self.execution_snapshot.execution_mode is ExecutionMode.SUBSCRIPTION and self.adapters:
            raise ValueError("subscription revisions cannot include adapters")
        if len(canonical_json(self).encode("utf-8")) > MAX_REVISION_MANIFEST_BYTES:
            raise ValueError("revision manifest exceeds the byte limit")
        return self


class _AtomicSuccessorContract(_Contract):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class SuccessorArtifactContributionV2(_AtomicSuccessorContract):
    """One ordered target contribution in a committed Evolution Revision."""

    target_id: str
    artifact_id: str
    artifact_type: Literal[
        "text_memory",
        "skill_bundle",
        "agent_system",
        "parametric_memory",
    ]
    owner_successor_transition_id: str
    origin: Literal["produced", "inherited"]

    _ids = field_validator(
        "target_id",
        "artifact_id",
        "owner_successor_transition_id",
    )(_stable_id)


class AtomicSuccessorManifestV2(_AtomicSuccessorContract):
    """Closed receipt for one fully prepared adjacent science successor."""

    atomic_successor_contract_version: Literal["2"] = "2"
    project_id: str
    successor_transition_id: str
    task_id: str
    task_admission_id: str
    admission_sha256: str
    accepted_attempt_id: str
    predecessor_project_head_id: str
    predecessor_generation: int = Field(ge=0, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    predecessor_manifest_sha256: str
    successor_project_head_id: str
    successor_generation: int = Field(ge=1, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    successor_manifest_sha256: str
    workspace_snapshot_id: str
    workspace_manifest_sha256: str
    evolution_revision_id: str
    evolution_revision_manifest_sha256: str
    runtime_context_snapshot_id: str
    runtime_context_manifest_sha256: str
    effective_execution_snapshot_id: str
    effective_execution_snapshot_sha256: str
    registry_sha256: str
    normalized_evolution_intent_sha256: str
    dataset_id: str
    dataset_artifact_id: str
    dataset_manifest_sha256: str
    runtime_context_source: Literal[
        "materialized_new",
        "materialized_inherited",
        "empty_inherited",
    ] = Field(
        default="materialized_new",
        exclude_if=lambda value: value == "materialized_new",
    )
    materialized_source_successor_transition_id: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    materialized_source_predecessor_project_head_id: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    materialized_context_id: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    materialized_context_manifest_sha256: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    method_artifact_ids: tuple[str, ...] = Field(default=(), max_length=128)
    artifacts: tuple[SuccessorArtifactContributionV2, ...] = Field(
        default=(),
        max_length=128,
        exclude_if=lambda value: not value,
    )

    _ids = field_validator(
        "project_id",
        "successor_transition_id",
        "task_id",
        "task_admission_id",
        "accepted_attempt_id",
        "predecessor_project_head_id",
        "successor_project_head_id",
        "workspace_snapshot_id",
        "evolution_revision_id",
        "runtime_context_snapshot_id",
        "effective_execution_snapshot_id",
        "dataset_id",
        "dataset_artifact_id",
    )(_stable_id)
    _digests = field_validator(
        "admission_sha256",
        "predecessor_manifest_sha256",
        "successor_manifest_sha256",
        "workspace_manifest_sha256",
        "evolution_revision_manifest_sha256",
        "runtime_context_manifest_sha256",
        "effective_execution_snapshot_sha256",
        "registry_sha256",
        "normalized_evolution_intent_sha256",
        "dataset_manifest_sha256",
    )(_digest)
    _generations = field_validator(
        "predecessor_generation",
        "successor_generation",
        mode="before",
    )(_strict_integer)

    @field_validator("method_artifact_ids")
    @classmethod
    def _ordered_method_artifacts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _ordered_stable_ids(value, label="method artifact IDs")

    @field_validator(
        "materialized_source_successor_transition_id",
        "materialized_source_predecessor_project_head_id",
        "materialized_context_id",
    )
    @classmethod
    def _optional_materialized_ids(
        cls,
        value: str | None,
    ) -> str | None:
        return None if value is None else _stable_id(value)

    @field_validator("materialized_context_manifest_sha256")
    @classmethod
    def _optional_materialized_digest(
        cls,
        value: str | None,
    ) -> str | None:
        return None if value is None else _digest(value)

    @model_validator(mode="after")
    def _adjacent_successor(self) -> AtomicSuccessorManifestV2:
        if self.successor_generation != self.predecessor_generation + 1:
            raise ValueError("atomic successor generation must be adjacent")
        if self.successor_project_head_id == self.predecessor_project_head_id:
            raise ValueError("atomic successor must have a new project-head identity")
        inherited_fields = (
            self.materialized_source_successor_transition_id,
            self.materialized_source_predecessor_project_head_id,
        )
        materialized_fields = (
            self.materialized_context_id,
            self.materialized_context_manifest_sha256,
        )
        if self.runtime_context_source == "materialized_new":
            if (
                any(value is not None for value in inherited_fields)
                or any(value is None for value in materialized_fields)
            ):
                raise ValueError(
                    "atomic successor new materialization is incomplete"
                )
        elif self.runtime_context_source == "materialized_inherited":
            if any(
                value is None
                for value in (*inherited_fields, *materialized_fields)
            ):
                raise ValueError(
                    "atomic successor inherited materialization is incomplete"
                )
        elif (
            any(
                value is not None
                for value in (*inherited_fields, *materialized_fields)
            )
            or self.method_artifact_ids
        ):
            raise ValueError(
                "atomic successor empty runtime exposes materialization"
            )
        if self.artifacts:
            target_ids = tuple(item.target_id for item in self.artifacts)
            artifact_ids = tuple(
                item.artifact_id for item in self.artifacts
            )
            if (
                target_ids != tuple(sorted(target_ids))
                or len(target_ids) != len(set(target_ids))
                or artifact_ids != self.method_artifact_ids
                or any(
                    item.origin == "produced"
                    and item.owner_successor_transition_id
                    != self.successor_transition_id
                    or item.origin == "inherited"
                    and item.owner_successor_transition_id
                    == self.successor_transition_id
                    for item in self.artifacts
                )
            ):
                raise ValueError(
                    "atomic successor artifact composition is invalid"
                )
        if len(canonical_json(self).encode("utf-8")) > MAX_REVISION_MANIFEST_BYTES:
            raise ValueError("atomic successor manifest exceeds the byte limit")
        return self


class AtomicEvolutionAbandonManifestV2(_AtomicSuccessorContract):
    """Closed receipt for advancing only the accepted workspace result."""

    atomic_evolution_abandon_contract_version: Literal["2"] = "2"
    project_id: str
    successor_transition_id: str
    task_id: str
    task_admission_id: str
    admission_sha256: str
    accepted_attempt_id: str
    predecessor_project_head_id: str
    predecessor_generation: int = Field(ge=0, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    predecessor_manifest_sha256: str
    successor_project_head_id: str
    successor_generation: int = Field(ge=1, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    successor_manifest_sha256: str
    workspace_snapshot_id: str
    workspace_manifest_sha256: str
    evolution_revision_id: str
    evolution_revision_manifest_sha256: str
    runtime_context_snapshot_id: str
    runtime_context_manifest_sha256: str
    effective_execution_snapshot_id: str
    effective_execution_snapshot_sha256: str
    registry_sha256: str
    normalized_evolution_intent_sha256: str
    runtime_context_source: Literal[
        "empty_inherited",
        "materialized_inherited",
    ]
    materialized_source_successor_transition_id: str | None = None
    materialized_source_predecessor_project_head_id: str | None = None
    materialized_context_id: str | None = None
    materialized_context_manifest_sha256: str | None = None
    method_artifact_ids: tuple[str, ...] = Field(default=(), max_length=128)
    artifacts: tuple[SuccessorArtifactContributionV2, ...] = Field(
        default=(),
        max_length=128,
        exclude_if=lambda value: not value,
    )

    _ids = field_validator(
        "project_id",
        "successor_transition_id",
        "task_id",
        "task_admission_id",
        "accepted_attempt_id",
        "predecessor_project_head_id",
        "successor_project_head_id",
        "workspace_snapshot_id",
        "evolution_revision_id",
        "runtime_context_snapshot_id",
        "effective_execution_snapshot_id",
    )(_stable_id)
    _digests = field_validator(
        "admission_sha256",
        "predecessor_manifest_sha256",
        "successor_manifest_sha256",
        "workspace_manifest_sha256",
        "evolution_revision_manifest_sha256",
        "runtime_context_manifest_sha256",
        "effective_execution_snapshot_sha256",
        "registry_sha256",
        "normalized_evolution_intent_sha256",
    )(_digest)
    _generations = field_validator(
        "predecessor_generation",
        "successor_generation",
        mode="before",
    )(_strict_integer)

    @field_validator(
        "materialized_source_successor_transition_id",
        "materialized_source_predecessor_project_head_id",
        "materialized_context_id",
    )
    @classmethod
    def _optional_ids(cls, value: str | None) -> str | None:
        return None if value is None else _stable_id(value)

    @field_validator("materialized_context_manifest_sha256")
    @classmethod
    def _optional_digest(cls, value: str | None) -> str | None:
        return None if value is None else _digest(value)

    @field_validator("method_artifact_ids")
    @classmethod
    def _ordered_method_artifacts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _ordered_stable_ids(value, label="method artifact IDs")

    @model_validator(mode="after")
    def _abandon_closure(self) -> AtomicEvolutionAbandonManifestV2:
        if self.successor_generation != self.predecessor_generation + 1:
            raise ValueError("atomic evolution abandon generation must be adjacent")
        if self.successor_project_head_id == self.predecessor_project_head_id:
            raise ValueError("atomic evolution abandon must publish a new project head")
        materialized_fields = (
            self.materialized_source_successor_transition_id,
            self.materialized_source_predecessor_project_head_id,
            self.materialized_context_id,
            self.materialized_context_manifest_sha256,
        )
        if self.runtime_context_source == "empty_inherited":
            if any(value is not None for value in materialized_fields):
                raise ValueError("empty inherited runtime context exposes materialization")
            if self.method_artifact_ids:
                raise ValueError("empty inherited runtime context exposes artifacts")
        elif any(value is None for value in materialized_fields):
            raise ValueError("materialized inherited runtime context is incomplete")
        if self.artifacts:
            target_ids = tuple(item.target_id for item in self.artifacts)
            artifact_ids = tuple(
                item.artifact_id for item in self.artifacts
            )
            if (
                target_ids != tuple(sorted(target_ids))
                or len(target_ids) != len(set(target_ids))
                or artifact_ids != self.method_artifact_ids
                or any(
                    item.origin != "inherited"
                    or item.owner_successor_transition_id
                    == self.successor_transition_id
                    for item in self.artifacts
                )
            ):
                raise ValueError(
                    "evolution abandon artifact composition is invalid"
                )
        if len(canonical_json(self).encode("utf-8")) > MAX_REVISION_MANIFEST_BYTES:
            raise ValueError("atomic evolution abandon manifest exceeds the byte limit")
        return self


AtomicSuccessorReceiptManifestV2 = (
    AtomicSuccessorManifestV2 | AtomicEvolutionAbandonManifestV2
)


def atomic_successor_manifest_sha256(
    manifest: AtomicSuccessorReceiptManifestV2,
) -> str:
    if type(manifest) not in {
        AtomicSuccessorManifestV2,
        AtomicEvolutionAbandonManifestV2,
    }:
        raise TypeError("atomic successor digest requires a closed v2 receipt")
    return canonical_digest(manifest)


class AtomicSuccessorCommitV2(_AtomicSuccessorContract):
    atomic_successor_commit_contract_version: Literal["2"] = "2"
    manifest_sha256: str
    manifest: AtomicSuccessorReceiptManifestV2

    _digest = field_validator("manifest_sha256")(_digest)

    @model_validator(mode="after")
    def _manifest_identity(self) -> AtomicSuccessorCommitV2:
        if self.manifest_sha256 != atomic_successor_manifest_sha256(self.manifest):
            raise ValueError("atomic successor commit digest is inconsistent")
        return self


class RevisionRecord(_Contract):
    revision_id: str
    manifest_digest: str
    manifest: RevisionManifestV1
    created_at: datetime
    active: bool

    _revision = field_validator("revision_id")(_stable_id)
    _digest = field_validator("manifest_digest")(_digest)
    _created = field_validator("created_at")(_canonical_utc_datetime)

    @model_validator(mode="after")
    def _identity_matches_manifest(self) -> RevisionRecord:
        digest = canonical_digest(self.manifest)
        if self.manifest_digest != digest or self.revision_id != f"rev-{digest}":
            raise ValueError("revision record identity does not match its manifest")
        return self


class TaskAdmissionRequest(_Contract):
    admission_contract_version: Literal["1"] = "1"
    task_envelope_contract_version: Literal["1"] = "1"
    stream_id: str
    task_id: str
    required_generation: int = Field(ge=0, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    idempotency_key: str
    project_id: str
    project_snapshot: ContentAddressedSnapshotRef
    workspace_snapshot: ContentAddressedSnapshotRef
    task_snapshot: ContentAddressedSnapshotRef
    execution_mode: ExecutionMode
    capture_mode: CaptureMode
    execution_snapshot_id: str
    context_id: str
    context_artifact_ids: tuple[str, ...] = Field(
        default=(),
        max_length=MAX_TASK_EXECUTION_ITEMS,
    )
    artifact_families: tuple[str, ...] = Field(
        min_length=1,
        max_length=MAX_TASK_EXECUTION_ITEMS,
    )
    method_ids: tuple[str, ...] = Field(
        min_length=1,
        max_length=MAX_TASK_EXECUTION_ITEMS,
    )
    context_artifact_set_digest: str
    task_envelope_digest: str

    _ids = field_validator(
        "stream_id",
        "task_id",
        "idempotency_key",
        "project_id",
        "context_id",
    )(_stable_id)
    _generation = field_validator("required_generation", mode="before")(_strict_integer)
    _digests = field_validator(
        "context_artifact_set_digest",
        "task_envelope_digest",
    )(_digest)

    @field_validator("execution_snapshot_id")
    @classmethod
    def _execution_id(cls, value: str) -> str:
        if _EXECUTION_SNAPSHOT_ID_RE.fullmatch(value) is None:
            raise ValueError("execution snapshot ID is invalid")
        return value

    @field_validator("context_artifact_ids", "artifact_families", "method_ids")
    @classmethod
    def _ordered_ids(cls, value: tuple[str, ...], info) -> tuple[str, ...]:
        return _ordered_stable_ids(value, label=info.field_name)

    def execution_envelope(self) -> TaskExecutionEnvelopeV1:
        """Reconstruct the complete closed envelope bound by this request."""

        return TaskExecutionEnvelopeV1(
            task_envelope_contract_version=self.task_envelope_contract_version,
            project_id=self.project_id,
            project_snapshot=self.project_snapshot,
            workspace_snapshot=self.workspace_snapshot,
            task_id=self.task_id,
            task_snapshot=self.task_snapshot,
            execution_mode=self.execution_mode,
            capture_mode=self.capture_mode,
            execution_snapshot_id=self.execution_snapshot_id,
            context_id=self.context_id,
            context_artifact_ids=self.context_artifact_ids,
            artifact_families=self.artifact_families,
            method_ids=self.method_ids,
        )

    @model_validator(mode="after")
    def _closed_envelope_matches(self) -> TaskAdmissionRequest:
        envelope = self.execution_envelope()
        if self.context_artifact_set_digest != canonical_digest(
            self.context_artifact_ids
        ) or self.task_envelope_digest != canonical_digest(envelope):
            raise ValueError("task admission envelope identity is inconsistent")
        return self


class TaskAdmissionRecord(_Contract):
    admission_id: str
    request_digest: str
    request: TaskAdmissionRequest
    status: AdmissionStatus
    reason: AdmissionQueueReason | None = None
    pinned_revision_id: str | None = None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None

    _admission = field_validator("admission_id")(_stable_id)
    _digest = field_validator("request_digest")(_digest)
    _timestamps = field_validator("created_at", "updated_at", "finished_at")(
        _optional_canonical_utc_datetime
    )

    @field_validator("pinned_revision_id")
    @classmethod
    def _pinned_revision(cls, value: str | None) -> str | None:
        if value is None:
            return None
        _stable_id(value)
        if not value.startswith("rev-") or len(value) != 68:
            raise ValueError("pinned revision ID is invalid")
        _digest(value.removeprefix("rev-"))
        return value

    @model_validator(mode="after")
    def _state_shape(self) -> TaskAdmissionRecord:
        digest = canonical_digest(self.request)
        if self.request_digest != digest or self.admission_id != f"adm-{digest}":
            raise ValueError("admission record identity does not match its request")
        if self.status is AdmissionStatus.QUEUED:
            if (
                self.reason is not AdmissionQueueReason.REQUIRED_REVISION_UNCOMMITTED
                or self.pinned_revision_id is not None
                or self.finished_at is not None
            ):
                raise ValueError("queued admission state is invalid")
        elif self.status is AdmissionStatus.ADMITTED:
            if (
                self.reason is not None
                or self.pinned_revision_id is None
                or self.finished_at is not None
            ):
                raise ValueError("admitted task state is invalid")
        elif self.status in {AdmissionStatus.COMPLETED, AdmissionStatus.FAILED}:
            if (
                self.reason is not None
                or self.pinned_revision_id is None
                or self.finished_at is None
            ):
                raise ValueError("finished admitted task state is invalid")
        elif self.status is AdmissionStatus.CANCELLED and (
            self.reason is not None or self.finished_at is None
        ):
            raise ValueError("cancelled task state is invalid")
        if self.updated_at < self.created_at or (
            self.finished_at is not None
            and (self.finished_at < self.created_at or self.updated_at < self.finished_at)
        ):
            raise ValueError("admission timestamps are invalid")
        return self


def revision_id_for_manifest(manifest: RevisionManifestV1) -> str:
    return f"rev-{canonical_digest(manifest)}"


def admission_id_for_request(request: TaskAdmissionRequest) -> str:
    return f"adm-{canonical_digest(request)}"


def bind_task_admission(
    intent: TaskAdmissionIntent,
    envelope: TaskExecutionEnvelopeV1,
) -> TaskAdmissionRequest:
    """Bind one validated intent to one closed canonical identity envelope."""

    validated_intent = TaskAdmissionIntent.model_validate(intent.model_dump(mode="python"))
    validated_envelope = TaskExecutionEnvelopeV1.model_validate(envelope.model_dump(mode="python"))
    if validated_envelope.task_id != validated_intent.task_id:
        raise ValueError("task execution envelope task ID does not match admission intent")
    return TaskAdmissionRequest(
        stream_id=validated_intent.stream_id,
        task_id=validated_intent.task_id,
        required_generation=validated_intent.required_generation,
        idempotency_key=validated_intent.idempotency_key,
        task_envelope_contract_version=validated_envelope.task_envelope_contract_version,
        project_id=validated_envelope.project_id,
        project_snapshot=validated_envelope.project_snapshot,
        workspace_snapshot=validated_envelope.workspace_snapshot,
        task_snapshot=validated_envelope.task_snapshot,
        execution_mode=validated_envelope.execution_mode,
        capture_mode=validated_envelope.capture_mode,
        execution_snapshot_id=validated_envelope.execution_snapshot_id,
        context_id=validated_envelope.context_id,
        context_artifact_ids=validated_envelope.context_artifact_ids,
        artifact_families=validated_envelope.artifact_families,
        method_ids=validated_envelope.method_ids,
        context_artifact_set_digest=canonical_digest(validated_envelope.context_artifact_ids),
        task_envelope_digest=canonical_digest(validated_envelope),
    )


__all__ = [
    "AdmissionQueueReason",
    "AdmissionStatus",
    "AtomicSuccessorCommitV2",
    "AtomicEvolutionAbandonManifestV2",
    "AtomicSuccessorManifestV2",
    "AtomicSuccessorReceiptManifestV2",
    "ContentAddressedSnapshotRef",
    "ExecutionModelIdentity",
    "ExecutionRuntimeIdentity",
    "ExecutionServingIdentity",
    "ExecutionSnapshotRecord",
    "ExecutionSnapshotV1",
    "ExecutionTaskNetworkPolicy",
    "MAX_EXECUTION_SNAPSHOT_BYTES",
    "MAX_REVISION_ADAPTERS",
    "MAX_REVISION_MANIFEST_BYTES",
    "MAX_TASK_EXECUTION_ENVELOPE_BYTES",
    "MAX_TASK_EXECUTION_ITEMS",
    "ModelIdentitySource",
    "RevisionCapacityError",
    "RevisionConflictError",
    "RevisionContextIdentity",
    "RevisionError",
    "RevisionIntegrityError",
    "RevisionManifestV1",
    "RevisionNotFoundError",
    "RevisionRecord",
    "SuccessorArtifactContributionV2",
    "TaskAdmissionConflictError",
    "TaskAdmissionIntent",
    "TaskAdmissionRecord",
    "TaskAdmissionRequest",
    "TaskExecutionEnvelopeV1",
    "VerifiedExecutionSnapshot",
    "admission_id_for_request",
    "atomic_successor_manifest_sha256",
    "bind_task_admission",
    "content_addressed_snapshot_ref",
    "execution_snapshot_id_for_snapshot",
    "execution_task_network_policy_digest",
    "require_verified_execution_snapshot",
    "revision_id_for_manifest",
]
