"""Strict renderer-safe models for Desktop Local API v2."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from openevo.backend.contracts.v2 import models as core


MAX_JAVASCRIPT_SAFE_INTEGER = (1 << 53) - 1
MAX_PROFILE_COUNT = 100
MAX_HOST_HINTS = 512
MAX_CATALOG_WARNINGS = 64
MAX_LIFECYCLE_OPERATION_COUNT = 16
MAX_LIFECYCLE_LOG_PAGE_ENTRIES = 100
MAX_LIFECYCLE_LOG_ENTRY_BYTES = 16 * 1024


class StrictModel(BaseModel):
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
SshHostAlias = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
    ),
]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ETag = Annotated[str, StringConstraints(pattern=r'^"[0-9a-f]{64}"$')]
DisplayName = Annotated[str, StringConstraints(min_length=1, max_length=128)]
SafeSummary = Annotated[str, StringConstraints(min_length=1, max_length=512)]
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

DesktopActionV2: TypeAlias = Literal[
    "retry",
    "rescan",
    "review_host_key",
    "rebind",
    "reconnect",
    "install_repair_daemon",
    "administrator_action",
    "correct_project",
    "wait_for_successor",
    "none",
]


class DesktopErrorV2(StrictModel):
    schema_version: Literal["2"] = "2"
    code: Annotated[
        str,
        StringConstraints(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$"),
    ]
    summary: SafeSummary
    retryable: bool
    action: DesktopActionV2
    affected_resource_id: OpaqueId | None


class CursorPageV2(StrictModel):
    next_cursor: Cursor | None = None
    has_more: bool

    @model_validator(mode="after")
    def _valid_cursor(self) -> CursorPageV2:
        if self.has_more != (self.next_cursor is not None):
            raise ValueError("has_more must match next_cursor presence")
        return self


class ContractOnlyResponseV2(StrictModel):
    schema_version: Literal["2"] = "2"
    code: Literal["contract_only_not_implemented"]
    message: SafeSummary


class SshHostHintV2(StrictModel):
    schema_version: Literal["2"] = "2"
    ssh_host_alias: SshHostAlias
    availability: Literal["selectable", "manual_entry_only", "unsupported"]
    source_kind: Literal["literal_host", "static_include"]


class SshCatalogWarningV2(StrictModel):
    schema_version: Literal["2"] = "2"
    code: Literal[
        "dynamic_hosts_not_enumerated",
        "conditional_hosts_not_enumerated",
        "include_cycle_skipped",
        "include_unreadable",
        "catalog_budget_exhausted",
        "invalid_config_text_skipped",
    ]
    action: Literal["manual_alias_available", "rescan", "administrator_action"]
    affected_entry_count: int = Field(ge=1, le=10_000)


class SshHostCatalogV2(StrictModel):
    schema_version: Literal["2"] = "2"
    catalog_generation: int = Field(ge=0, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    hosts: list[SshHostHintV2] = Field(max_length=MAX_HOST_HINTS)
    warnings: list[SshCatalogWarningV2] = Field(max_length=MAX_CATALOG_WARNINGS)
    scanned_at: UtcTimestamp

    @model_validator(mode="after")
    def _hosts_are_sorted_and_unique(self) -> SshHostCatalogV2:
        aliases = [host.ssh_host_alias for host in self.hosts]
        if len(aliases) != len(set(aliases)):
            raise ValueError("SSH host aliases must be unique")
        if aliases != sorted(aliases):
            raise ValueError("SSH host aliases must be sorted")
        return self


class SshHostCatalogRescanV2(StrictModel):
    schema_version: Literal["2"] = "2"


class SshPromptStateV2(StrictModel):
    schema_version: Literal["2"] = "2"
    connection_generation: int = Field(ge=1, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    kind: Literal["password", "passphrase", "confirmation"]
    state: Literal["pending", "completed", "cancelled", "expired"]
    requested_at: UtcTimestamp


class SshHostKeyFingerprintV2(StrictModel):
    schema_version: Literal["2"] = "2"
    algorithm: Literal[
        "ssh-ed25519",
        "ssh-rsa",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
    ]
    sha256_fingerprint: Annotated[
        str,
        StringConstraints(pattern=r"^SHA256:[A-Za-z0-9+/]{43}$"),
    ]
    role: Literal["previous", "presented"]


class SshTrustStateV2(StrictModel):
    schema_version: Literal["2"] = "2"
    connection_generation: int = Field(ge=1, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    state: Literal[
        "unverified",
        "trusted",
        "first_use_review",
        "changed_key_blocked",
        "rejected",
        "repairing",
    ]
    review_id: OpaqueId | None
    review_sha256: Digest | None
    key_fingerprints: list[SshHostKeyFingerprintV2] = Field(max_length=16)
    repair_support: Literal[
        "not_needed",
        "first_use_acceptance_available",
        "automatic_replacement_available",
        "administrator_required",
    ]

    @model_validator(mode="after")
    def _valid_review_shape(self) -> SshTrustStateV2:
        requires_review = self.state in {"first_use_review", "changed_key_blocked"}
        if requires_review:
            if self.review_id is None or self.review_sha256 is None:
                raise ValueError("host-key review state requires a review identity")
            if not self.key_fingerprints:
                raise ValueError("host-key review state requires a fingerprint")
        elif (
            self.review_id is not None
            or self.review_sha256 is not None
            or self.key_fingerprints
        ):
            raise ValueError("non-review trust state must not retain review material")
        return self


ConnectionStateV2: TypeAlias = Literal[
    "disconnected",
    "connecting",
    "prompt_pending",
    "host_key_review",
    "bootstrapping",
    "negotiating",
    "connected",
    "disconnecting",
    "failed",
]


class SystemOpenSshProfileCreateV2(StrictModel):
    schema_version: Literal["2"] = "2"
    display_name: DisplayName
    connection_authority: Literal["system_openssh"] = "system_openssh"
    ssh_host_alias: SshHostAlias


class RemoteWorkspaceProfileV2(StrictModel):
    schema_version: Literal["2"] = "2"
    profile_kind: Literal["system_openssh"] = "system_openssh"
    profile_id: OpaqueId
    display_name: DisplayName
    connection_authority: Literal["system_openssh"] = "system_openssh"
    ssh_host_alias: SshHostAlias
    catalog_generation: int = Field(ge=0, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    connection_generation: int = Field(ge=1, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    connection_state: ConnectionStateV2
    prompt: SshPromptStateV2 | None
    trust: SshTrustStateV2
    failure: DesktopErrorV2 | None
    active_project_id: OpaqueId | None
    core_api_major: Literal[2] | None
    core_openapi_sha256: Digest | None
    core_event_schema_sha256: Digest | None
    core_registry_sha256: Digest | None
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    etag: ETag

    @model_validator(mode="after")
    def _valid_connection_shape(self) -> RemoteWorkspaceProfileV2:
        if self.trust.connection_generation != self.connection_generation:
            raise ValueError("trust state has another connection generation")
        if (
            self.prompt is not None
            and self.prompt.connection_generation != self.connection_generation
        ):
            raise ValueError("prompt state has another connection generation")
        if (self.connection_state == "prompt_pending") != (self.prompt is not None):
            raise ValueError("prompt state is present only while a prompt is pending")
        if self.prompt is not None and self.prompt.state != "pending":
            raise ValueError("a profile may expose only a pending prompt")
        if (self.connection_state == "failed") != (self.failure is not None):
            raise ValueError("failure is present only for a failed connection")
        if self.connection_state == "host_key_review" and self.trust.state not in {
            "first_use_review",
            "changed_key_blocked",
        }:
            raise ValueError("host-key review connection requires a trust review")
        core_identity = (
            self.core_api_major,
            self.core_openapi_sha256,
            self.core_event_schema_sha256,
            self.core_registry_sha256,
        )
        if self.connection_state == "connected":
            if any(value is None for value in core_identity):
                raise ValueError("connected profile requires exact Core v2 identity")
            if self.prompt is not None or self.failure is not None:
                raise ValueError("connected profile cannot retain prompt or failure state")
        elif any(value is not None for value in core_identity):
            raise ValueError("only a connected profile may expose negotiated Core identity")
        return self


class LegacyExplicitProfileV2(StrictModel):
    schema_version: Literal["2"] = "2"
    profile_kind: Literal["legacy_explicit"] = "legacy_explicit"
    profile_id: OpaqueId
    display_name: DisplayName
    connectable: Literal[False] = False
    migration_state: Literal["rebind_required", "quarantined"]
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    etag: ETag


RemoteProfileV2: TypeAlias = Annotated[
    RemoteWorkspaceProfileV2 | LegacyExplicitProfileV2,
    Field(discriminator="profile_kind"),
]


class RemoteProfilePageV2(CursorPageV2):
    schema_version: Literal["2"] = "2"
    items: list[RemoteProfileV2] = Field(max_length=MAX_PROFILE_COUNT)


class ProfileDisplayNamePatchV2(StrictModel):
    schema_version: Literal["2"] = "2"
    display_name: DisplayName


class ProfileRebindV2(StrictModel):
    schema_version: Literal["2"] = "2"
    connection_authority: Literal["system_openssh"] = "system_openssh"
    ssh_host_alias: SshHostAlias
    catalog_generation: int = Field(ge=0, le=MAX_JAVASCRIPT_SAFE_INTEGER)


class ProfileConnectionActionV2(StrictModel):
    schema_version: Literal["2"] = "2"
    expected_connection_generation: int = Field(
        ge=1, le=MAX_JAVASCRIPT_SAFE_INTEGER
    )


class HostKeyReviewRequestV2(ProfileConnectionActionV2):
    review_id: OpaqueId
    review_sha256: Digest
    action: Literal["accept_first_use", "replace_changed_key", "reject"]


class DesktopVersionV2(StrictModel):
    schema_version: Literal["2"] = "2"
    api_name: Literal["openevo-desktop-local-api"]
    preferred_major: Literal[2]
    supported_majors: list[Literal[2]] = Field(min_length=1, max_length=1)
    mutation_major: Literal[2]
    openapi_sha256: Digest
    event_schema_sha256: Digest
    release_version: SafeSummary
    build_id: Digest
    source_commit: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{7,64}$")]
    build_channel: Literal["release", "development", "test"]
    provider_kind: Literal["desktop_sidecar"]
    feature_flags: list[OpaqueId] = Field(min_length=1, max_length=128)
    feature_set_sha256: Digest
    required_core_api_major: Literal[2]
    mutation_compatible: bool

    @model_validator(mode="after")
    def _bind_negotiated_authority(self) -> DesktopVersionV2:
        if self.supported_majors != [2]:
            raise ValueError("Desktop v2 discovery must support only major 2")
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


class DesktopHealthV2(StrictModel):
    schema_version: Literal["2"] = "2"
    status: Literal["ready", "starting", "unavailable"]
    checked_at: UtcTimestamp


LifecycleOperationKindV2: TypeAlias = Literal[
    "profile_connect",
    "profile_disconnect",
    "host_key_review",
    "native_workspace_prepare",
    "project_create",
    "project_activate",
]
LifecycleOperationStatusV2: TypeAlias = Literal[
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
]
LifecyclePhaseV2: TypeAlias = Literal[
    "validation",
    "queued",
    "resolving_system_openssh",
    "connecting",
    "waiting_for_user",
    "remote_preflight",
    "transferring",
    "verifying",
    "starting_daemon",
    "waiting_for_daemon",
    "opening_project_tunnel",
    "negotiating_core",
    "preparing_native_workspace",
    "creating_remote_project",
    "verifying_project",
    "activating",
    "finalizing",
]
LIFECYCLE_PHASES: tuple[LifecyclePhaseV2, ...] = (
    "validation",
    "queued",
    "resolving_system_openssh",
    "connecting",
    "waiting_for_user",
    "remote_preflight",
    "transferring",
    "verifying",
    "starting_daemon",
    "waiting_for_daemon",
    "opening_project_tunnel",
    "negotiating_core",
    "preparing_native_workspace",
    "creating_remote_project",
    "verifying_project",
    "activating",
    "finalizing",
)


class LifecycleProgressIndeterminateV2(StrictModel):
    kind: Literal["indeterminate"]


class LifecycleProgressBytesV2(StrictModel):
    kind: Literal["bytes"]
    completed: int = Field(ge=0, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    total: int = Field(ge=1, le=MAX_JAVASCRIPT_SAFE_INTEGER)

    @model_validator(mode="after")
    def _completed_not_past_total(self) -> LifecycleProgressBytesV2:
        if self.completed > self.total:
            raise ValueError("completed bytes exceed total")
        return self


class LifecycleProgressItemsV2(StrictModel):
    kind: Literal["items"]
    completed: int = Field(ge=0, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    total: int = Field(ge=1, le=MAX_JAVASCRIPT_SAFE_INTEGER)

    @model_validator(mode="after")
    def _completed_not_past_total(self) -> LifecycleProgressItemsV2:
        if self.completed > self.total:
            raise ValueError("completed items exceed total")
        return self


LifecycleProgressV2: TypeAlias = Annotated[
    LifecycleProgressIndeterminateV2
    | LifecycleProgressBytesV2
    | LifecycleProgressItemsV2,
    Field(discriminator="kind"),
]


class LifecycleProfileResourceRefV2(StrictModel):
    resource_kind: Literal["profile"]
    resource_id: OpaqueId


class LifecycleNativeWorkspaceResourceRefV2(StrictModel):
    resource_kind: Literal["native_workspace"]
    resource_id: OpaqueId


class LifecycleProjectResourceRefV2(StrictModel):
    resource_kind: Literal["project"]
    resource_id: OpaqueId


LifecycleResourceRefV2: TypeAlias = Annotated[
    LifecycleProfileResourceRefV2
    | LifecycleNativeWorkspaceResourceRefV2
    | LifecycleProjectResourceRefV2,
    Field(discriminator="resource_kind"),
]


class LifecycleProfileResultV2(StrictModel):
    result_kind: Literal["profile"]
    profile_id: OpaqueId
    connection_generation: int = Field(ge=1, le=MAX_JAVASCRIPT_SAFE_INTEGER)


class LifecycleNativeWorkspaceResultV2(StrictModel):
    result_kind: Literal["native_workspace"]
    import_id: OpaqueId
    content_sha256: Digest
    byte_size: int = Field(ge=1, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    entry_count: int = Field(ge=0, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    extracted_byte_size: int = Field(ge=0, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    display_name: DisplayName


class LifecycleProjectResultV2(StrictModel):
    result_kind: Literal["project"]
    project_id: OpaqueId


LifecycleResultV2: TypeAlias = Annotated[
    LifecycleProfileResultV2
    | LifecycleNativeWorkspaceResultV2
    | LifecycleProjectResultV2,
    Field(discriminator="result_kind"),
]


def _expected_lifecycle_resource_kind(kind: LifecycleOperationKindV2) -> str:
    if kind in {"profile_connect", "profile_disconnect", "host_key_review"}:
        return "profile"
    if kind == "native_workspace_prepare":
        return "native_workspace"
    return "project"


def _validate_lifecycle_identity(
    *,
    kind: LifecycleOperationKindV2,
    resource: LifecycleResourceRefV2,
    result: LifecycleResultV2 | None,
) -> None:
    expected_kind = _expected_lifecycle_resource_kind(kind)
    if resource.resource_kind != expected_kind:
        raise ValueError("operation kind and resource kind do not match")
    if result is None:
        return
    if result.result_kind != expected_kind:
        raise ValueError("operation resource and result kind do not match")
    if kind == "project_create":
        # The reserved resource is the Desktop bridge identity available
        # before remote mutation. Core issues the authoritative project ID only
        # when that idempotent mutation succeeds.
        return
    result_resource_id = {
        "profile": getattr(result, "profile_id", None),
        "native_workspace": getattr(result, "import_id", None),
        "project": getattr(result, "project_id", None),
    }[expected_kind]
    if result_resource_id != resource.resource_id:
        raise ValueError("operation result belongs to another resource")


def _validate_lifecycle_phase(
    *,
    phase: LifecyclePhaseV2,
    phase_index: int,
    phase_total: int,
) -> None:
    if phase_total != len(LIFECYCLE_PHASES):
        raise ValueError("lifecycle phase total differs from the fixed phase plan")
    if LIFECYCLE_PHASES[phase_index] != phase:
        raise ValueError("lifecycle phase index differs from the fixed phase plan")


def _parse_utc_timestamp(value: UtcTimestamp) -> datetime:
    try:
        return datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ValueError("lifecycle timestamp is not a real UTC instant") from exc


class LifecycleOperationRefV2(StrictModel):
    schema_version: Literal["2"] = "2"
    operation_id: OpaqueId
    kind: LifecycleOperationKindV2
    resource: LifecycleResourceRefV2
    request_sha256: Digest
    status: LifecycleOperationStatusV2
    phase: LifecyclePhaseV2
    phase_index: int = Field(ge=0, le=len(LIFECYCLE_PHASES) - 1)
    phase_total: int = Field(ge=1, le=len(LIFECYCLE_PHASES))
    log_sequence_high_watermark: int = Field(
        ge=0,
        le=MAX_JAVASCRIPT_SAFE_INTEGER,
    )
    updated_at: UtcTimestamp
    etag: ETag

    @model_validator(mode="after")
    def _bind_identity_and_phase(self) -> LifecycleOperationRefV2:
        _validate_lifecycle_identity(kind=self.kind, resource=self.resource, result=None)
        _validate_lifecycle_phase(
            phase=self.phase,
            phase_index=self.phase_index,
            phase_total=self.phase_total,
        )
        return self

    @classmethod
    def from_operation(
        cls,
        operation: LifecycleOperationV2,
    ) -> LifecycleOperationRefV2:
        return cls(
            operation_id=operation.operation_id,
            kind=operation.kind,
            resource=operation.resource,
            request_sha256=operation.request_sha256,
            status=operation.status,
            phase=operation.phase,
            phase_index=operation.phase_index,
            phase_total=operation.phase_total,
            log_sequence_high_watermark=operation.log_sequence_high_watermark,
            updated_at=operation.updated_at,
            etag=operation.etag,
        )


class LifecycleOperationV2(StrictModel):
    schema_version: Literal["2"] = "2"
    operation_id: OpaqueId
    kind: LifecycleOperationKindV2
    resource: LifecycleResourceRefV2
    request_sha256: Digest
    status: LifecycleOperationStatusV2
    phase: LifecyclePhaseV2
    phase_index: int = Field(ge=0, le=len(LIFECYCLE_PHASES) - 1)
    phase_total: int = Field(ge=1, le=len(LIFECYCLE_PHASES))
    progress: LifecycleProgressV2 | None
    cancellable: bool
    result: LifecycleResultV2 | None
    failure: DesktopErrorV2 | None
    log_sequence_high_watermark: int = Field(
        ge=0,
        le=MAX_JAVASCRIPT_SAFE_INTEGER,
    )
    created_at: UtcTimestamp
    started_at: UtcTimestamp | None
    updated_at: UtcTimestamp
    finished_at: UtcTimestamp | None
    etag: ETag

    @model_validator(mode="after")
    def _bind_lifecycle_authority(self) -> LifecycleOperationV2:
        _validate_lifecycle_identity(
            kind=self.kind,
            resource=self.resource,
            result=self.result,
        )
        _validate_lifecycle_phase(
            phase=self.phase,
            phase_index=self.phase_index,
            phase_total=self.phase_total,
        )
        if self.status == "succeeded":
            if self.result is None:
                raise ValueError("succeeded operation requires a typed result")
            if self.failure is not None:
                raise ValueError("succeeded operation cannot retain a failure")
            if self.phase != "finalizing":
                raise ValueError("succeeded operation requires the final lifecycle phase")
        elif self.status == "failed":
            if self.failure is None:
                raise ValueError("failed operation requires a typed failure")
            if self.result is not None:
                raise ValueError("failed operation cannot retain a result")
        elif self.result is not None or self.failure is not None:
            raise ValueError("result and failure are terminal status fields")

        terminal = self.status in {"succeeded", "failed", "cancelled"}
        if terminal != (self.finished_at is not None):
            raise ValueError("finished timestamp must match terminal status")
        if terminal and self.cancellable:
            raise ValueError("terminal operation cannot remain cancellable")
        if self.status == "queued" and self.started_at is not None:
            raise ValueError("queued operation cannot have a started timestamp")
        if self.status == "running" and self.started_at is None:
            raise ValueError("running operation requires a started timestamp")
        timestamps = [_parse_utc_timestamp(self.created_at)]
        if self.started_at is not None:
            timestamps.append(_parse_utc_timestamp(self.started_at))
        timestamps.append(_parse_utc_timestamp(self.updated_at))
        if timestamps != sorted(timestamps):
            raise ValueError("lifecycle timestamps cannot regress")
        if self.finished_at is not None:
            finished_at = _parse_utc_timestamp(self.finished_at)
            if finished_at != timestamps[-1]:
                raise ValueError(
                    "terminal timestamp must equal the immutable update timestamp"
                )
        return self


class LifecycleLogEntryV2(StrictModel):
    schema_version: Literal["2"] = "2"
    operation_id: OpaqueId
    sequence: int = Field(ge=1, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    occurred_at: UtcTimestamp
    source: Literal[
        "desktop",
        "ssh_stdout",
        "ssh_stderr",
        "daemon_stdout",
        "daemon_stderr",
    ]
    text: Annotated[str, StringConstraints(min_length=1)]
    truncated: bool

    @model_validator(mode="after")
    def _bounded_safe_text(self) -> LifecycleLogEntryV2:
        if len(self.text.encode("utf-8")) > MAX_LIFECYCLE_LOG_ENTRY_BYTES:
            raise ValueError("lifecycle log text exceeds the UTF-8 byte limit")
        if any(
            (ord(character) < 0x20 and character not in {"\n", "\t"})
            or 0x7F <= ord(character) <= 0x9F
            for character in self.text
        ):
            raise ValueError("lifecycle log text contains a prohibited control character")
        return self


class LifecycleLogPageV2(CursorPageV2):
    schema_version: Literal["2"] = "2"
    operation_id: OpaqueId
    dropped_before_sequence: int = Field(ge=0, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    items: list[LifecycleLogEntryV2] = Field(
        max_length=MAX_LIFECYCLE_LOG_PAGE_ENTRIES
    )

    @model_validator(mode="after")
    def _bind_log_entries(self) -> LifecycleLogPageV2:
        sequences = [item.sequence for item in self.items]
        if any(item.operation_id != self.operation_id for item in self.items):
            raise ValueError("lifecycle log entry belongs to another operation")
        if sequences != sorted(set(sequences)):
            raise ValueError("lifecycle log sequences must be unique and ascending")
        if any(sequence <= self.dropped_before_sequence for sequence in sequences):
            raise ValueError("lifecycle log entry is at or before the dropped boundary")
        return self


class LifecycleCancelV2(StrictModel):
    schema_version: Literal["2"] = "2"
    expected_operation_id: OpaqueId


class LifecycleAcknowledgeV2(StrictModel):
    schema_version: Literal["2"] = "2"
    expected_operation_id: OpaqueId
    expected_terminal_status: Literal["succeeded", "failed", "cancelled"]


class DesktopStateV2(StrictModel):
    schema_version: Literal["2"] = "2"
    profiles: list[RemoteProfileV2] = Field(max_length=MAX_PROFILE_COUNT)
    active_profile_id: OpaqueId | None
    active_project_id: OpaqueId | None
    pending_operations: list[LifecycleOperationRefV2] = Field(
        max_length=MAX_LIFECYCLE_OPERATION_COUNT
    )
    last_event_id: OpaqueId | None
    updated_at: UtcTimestamp

    @model_validator(mode="after")
    def _active_refs_exist(self) -> DesktopStateV2:
        profile_ids = {profile.profile_id for profile in self.profiles}
        if self.active_profile_id is not None and self.active_profile_id not in profile_ids:
            raise ValueError("active profile is absent from state")
        operation_ids = [operation.operation_id for operation in self.pending_operations]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("pending lifecycle operation IDs must be unique")
        return self


class LocalOperationV2(StrictModel):
    schema_version: Literal["2"] = "2"
    operation_id: OpaqueId
    kind: Literal[
        "ssh_catalog_rescan",
        "profile_connect",
        "profile_disconnect",
        "host_key_review",
        "project_activate",
        "task_cancel",
        "task_retry",
        "transition_retry",
        "transition_replace",
        "transition_abandon",
        "service_restart",
        "diagnostic",
    ]
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    failure: DesktopErrorV2 | None
    created_at: UtcTimestamp
    updated_at: UtcTimestamp

    @model_validator(mode="after")
    def _failure_matches_status(self) -> LocalOperationV2:
        if (self.status == "failed") != (self.failure is not None):
            raise ValueError("failure is required only for a failed operation")
        return self


# Core v2 models are renderer-safe only through this explicit projection list.
ProjectHeadRefV2 = core.ProjectHeadRefV2
EvolutionRevisionRefV2 = core.EvolutionRevisionRefV2
RuntimeContextSnapshotRefV2 = core.RuntimeContextSnapshotRefV2
EffectiveExecutionSnapshotRefV2 = core.EffectiveExecutionSnapshotRefV2
WorkspaceSnapshotRefV2 = core.WorkspaceSnapshotRefV2
TaskAdmissionRefV2 = core.TaskAdmissionRefV2
AttemptRefV2 = core.AttemptRefV2
SuccessorTransitionRefV2 = core.SuccessorTransitionRefV2
DesktopProjectV2 = core.ProjectV2
DesktopProjectPageV2 = core.ProjectPageV2
DesktopTaskV2 = core.TaskV2
DesktopTransitionV2 = core.SuccessorTransitionV2
DesktopArtifactV2 = core.ArtifactV2
DesktopArtifactContentV2 = core.ArtifactContentV2
DesktopArtifactPageV2 = core.ArtifactPageV2
DesktopServiceV2 = core.ServiceV2
DesktopServicePageV2 = core.ServicePageV2
DesktopDiagnosticV2 = core.DiagnosticV2
DesktopTimelinePageV2 = core.TimelinePageV2
DesktopLogPageV2 = core.LogPageV2
DesktopTaskContextV2 = core.TaskContextV2
DesktopTaskPageV2 = core.TaskPageV2
CoreTaskSubmitRequestV2 = core.TaskSubmitRequestV2
DesktopCoreOperationV2 = core.OperationV2
DesktopServiceLogPageV2 = core.LogPageV2
DesktopCacheCleanupRequestV2 = core.CacheCleanupRequestV2


class ProjectCreateV2(StrictModel):
    schema_version: Literal["2"] = "2"
    profile_id: OpaqueId
    profile_connection_generation: int = Field(
        ge=1, le=MAX_JAVASCRIPT_SAFE_INTEGER
    )
    display_name: DisplayName
    config: core.ScienceProjectConfigV2


class ProjectPatchV2(StrictModel):
    schema_version: Literal["2"] = "2"
    expected_project_head_id: OpaqueId | None
    expected_project_head_manifest_sha256: Digest | None
    expected_project_config_sha256: Digest
    display_name: DisplayName
    config: core.ScienceProjectConfigV2

    @model_validator(mode="after")
    def _head_cas_is_complete(self) -> ProjectPatchV2:
        if (self.expected_project_head_id is None) != (
            self.expected_project_head_manifest_sha256 is None
        ):
            raise ValueError("expected project head ID and manifest must be present together")
        return self


class ProjectActionV2(StrictModel):
    schema_version: Literal["2"] = "2"
    expected_project_head_id: OpaqueId
    expected_project_head_manifest_sha256: Digest


class ProjectCapabilityProjectionV2(StrictModel):
    schema_version: Literal["2"] = "2"
    project_id: OpaqueId
    execution_mode: Literal["codex_subscription_transcript", "self_deployed"]
    registry_sha256: Digest
    capabilities_sha256: Digest
    capabilities: core.CapabilitiesResponseV2
    fetched_at: UtcTimestamp

    @model_validator(mode="after")
    def _bind_complete_remote_envelope(self) -> ProjectCapabilityProjectionV2:
        if self.registry_sha256 != self.capabilities.registry_digest:
            raise ValueError("capability registry digest differs from the remote envelope")
        if self.capabilities_sha256 != evolution_capabilities_sha256_for(
            self.capabilities
        ):
            raise ValueError("capability digest differs from the remote envelope")
        expected_framework_mode = (
            "subscription"
            if self.execution_mode == "codex_subscription_transcript"
            else "self_deployed"
        )
        if self.capabilities.evaluated_profile.execution_mode != expected_framework_mode:
            raise ValueError("capability execution mode differs from the remote envelope")
        return self


def evolution_capabilities_sha256_for(
    capabilities: core.CapabilitiesResponseV2 | dict[str, object],
) -> str:
    """Hash the complete validated capability envelope forwarded by the sidecar."""

    parsed = (
        capabilities
        if isinstance(capabilities, core.CapabilitiesResponseV2)
        else core.CapabilitiesResponseV2.model_validate(capabilities)
    )
    encoded = json.dumps(
        parsed.model_dump(mode="json"),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ProjectValidationRequestV2(StrictModel):
    schema_version: Literal["2"] = "2"
    expected_project_head_id: OpaqueId
    expected_project_head_manifest_sha256: Digest
    expected_project_config_sha256: Digest
    capability_registry_sha256: Digest


class ValidationCheckV2(StrictModel):
    check_id: OpaqueId
    status: Literal["passed", "failed", "unavailable"]
    action: DesktopActionV2


class ProjectValidationV2(StrictModel):
    schema_version: Literal["2"] = "2"
    project_id: OpaqueId
    valid: bool
    registry_sha256: Digest
    checks: list[ValidationCheckV2] = Field(max_length=256)
    validated_at: UtcTimestamp


class TaskActionV2(StrictModel):
    schema_version: Literal["2"] = "2"
    task_admission_id: OpaqueId
    admission_sha256: Digest
    predecessor_project_head_id: OpaqueId


class TransitionActionV2(StrictModel):
    schema_version: Literal["2"] = "2"
    expected_predecessor_project_head_id: OpaqueId
    plan_sha256: Digest


class TransitionReplaceV2(TransitionActionV2):
    replacement_plan_sha256: Digest


class ArtifactDiffV2(StrictModel):
    schema_version: Literal["2"] = "2"
    artifact_id: OpaqueId
    previous_artifact_id: OpaqueId | None
    current_manifest_sha256: Digest
    previous_manifest_sha256: Digest | None
    status: Literal["available", "not_comparable", "unavailable"]


class ServiceRestartV2(StrictModel):
    schema_version: Literal["2"] = "2"
    expected_service_id: OpaqueId


class DiagnosticRequestV2(StrictModel):
    schema_version: Literal["2"] = "2"
    profile_id: OpaqueId
    profile_connection_generation: int = Field(
        ge=1, le=MAX_JAVASCRIPT_SAFE_INTEGER
    )
    scope: Literal["system", "project", "task", "transition", "service"]
    resource_id: OpaqueId | None


class HostCatalogEventPayloadV2(StrictModel):
    payload_kind: Literal["ssh_host_catalog_changed"]
    catalog_generation: int = Field(ge=0, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    host_count: int = Field(ge=0, le=MAX_HOST_HINTS)
    warning_count: int = Field(ge=0, le=MAX_CATALOG_WARNINGS)


class ProfileEventPayloadV2(StrictModel):
    payload_kind: Literal["profile_connection_changed"]
    profile_id: OpaqueId
    connection_generation: int = Field(ge=1, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    connection_state: ConnectionStateV2
    failure: DesktopErrorV2 | None


class CoreAuthorityEventPayloadV2(StrictModel):
    payload_kind: Literal["core_authority_changed"]
    profile_id: OpaqueId
    project_id: OpaqueId
    core_event_id: OpaqueId
    core_event_sequence: int = Field(ge=1, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    core_event_type: Literal[
        "task_admitted",
        "attempt_appended",
        "dataset_sealed",
        "transition_changed",
        "evolution_revision_committed",
        "runtime_context_committed",
        "project_head_activated",
    ]
    core_payload_sha256: Digest


class DiagnosticEventPayloadV2(StrictModel):
    payload_kind: Literal["diagnostic_changed"]
    diagnostic_id: OpaqueId
    status: Literal["queued", "running", "ready", "failed"]


class LifecycleOperationEventPayloadV2(StrictModel):
    payload_kind: Literal["lifecycle_operation_changed"]
    operation_id: OpaqueId
    kind: LifecycleOperationKindV2
    status: LifecycleOperationStatusV2
    phase: LifecyclePhaseV2
    etag: ETag
    log_sequence_high_watermark: int = Field(
        ge=0,
        le=MAX_JAVASCRIPT_SAFE_INTEGER,
    )


DesktopEventPayloadV2: TypeAlias = Annotated[
    HostCatalogEventPayloadV2
    | ProfileEventPayloadV2
    | CoreAuthorityEventPayloadV2
    | DiagnosticEventPayloadV2
    | LifecycleOperationEventPayloadV2,
    Field(discriminator="payload_kind"),
]


class DesktopEventEnvelopeV2(StrictModel):
    schema_version: Literal["2"] = "2"
    event_id: OpaqueId
    sequence: int = Field(ge=1, le=MAX_JAVASCRIPT_SAFE_INTEGER)
    occurred_at: UtcTimestamp
    event_type: Literal[
        "ssh_host_catalog_changed",
        "profile_connection_changed",
        "core_authority_changed",
        "diagnostic_changed",
        "lifecycle_operation_changed",
    ]
    payload_sha256: Digest
    payload: DesktopEventPayloadV2

    @model_validator(mode="after")
    def _bind_payload(self) -> DesktopEventEnvelopeV2:
        if self.event_type != self.payload.payload_kind:
            raise ValueError("event type differs from payload kind")
        encoded = json.dumps(
            self.payload.model_dump(mode="json"),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != self.payload_sha256:
            raise ValueError("event payload digest mismatch")
        return self


class DesktopSseFrameV2(StrictModel):
    id: OpaqueId
    event: Literal[
        "ssh_host_catalog_changed",
        "profile_connection_changed",
        "core_authority_changed",
        "diagnostic_changed",
        "lifecycle_operation_changed",
    ]
    data: DesktopEventEnvelopeV2
    retry: int | None = Field(default=None, ge=1000, le=60_000)

    @model_validator(mode="after")
    def _bind_envelope(self) -> DesktopSseFrameV2:
        if self.id != self.data.event_id:
            raise ValueError("SSE frame ID differs from event ID")
        if self.event != self.data.event_type:
            raise ValueError("SSE event name differs from event type")
        return self


__all__ = [
    "ArtifactDiffV2",
    "AttemptRefV2",
    "ContractOnlyResponseV2",
    "CoreTaskSubmitRequestV2",
    "DesktopArtifactContentV2",
    "DesktopArtifactPageV2",
    "DesktopArtifactV2",
    "DesktopCacheCleanupRequestV2",
    "DesktopCoreOperationV2",
    "DesktopDiagnosticV2",
    "DesktopErrorV2",
    "DesktopEventEnvelopeV2",
    "DesktopHealthV2",
    "DesktopLogPageV2",
    "DesktopProjectV2",
    "DesktopProjectPageV2",
    "DesktopServicePageV2",
    "DesktopServiceLogPageV2",
    "DesktopServiceV2",
    "DesktopSseFrameV2",
    "DesktopStateV2",
    "DesktopTaskContextV2",
    "DesktopTaskPageV2",
    "DesktopTaskV2",
    "DesktopTimelinePageV2",
    "DesktopTransitionV2",
    "DesktopVersionV2",
    "DiagnosticRequestV2",
    "EffectiveExecutionSnapshotRefV2",
    "EvolutionRevisionRefV2",
    "HostKeyReviewRequestV2",
    "LegacyExplicitProfileV2",
    "LifecycleAcknowledgeV2",
    "LifecycleCancelV2",
    "LifecycleLogEntryV2",
    "LifecycleLogPageV2",
    "LifecycleNativeWorkspaceResourceRefV2",
    "LifecycleNativeWorkspaceResultV2",
    "LifecycleOperationEventPayloadV2",
    "LifecycleOperationKindV2",
    "LifecycleOperationRefV2",
    "LifecycleOperationStatusV2",
    "LifecycleOperationV2",
    "LifecyclePhaseV2",
    "LifecycleProfileResourceRefV2",
    "LifecycleProfileResultV2",
    "LifecycleProgressBytesV2",
    "LifecycleProgressIndeterminateV2",
    "LifecycleProgressItemsV2",
    "LifecycleProgressV2",
    "LifecycleProjectResourceRefV2",
    "LifecycleProjectResultV2",
    "LifecycleResourceRefV2",
    "LifecycleResultV2",
    "LocalOperationV2",
    "ProfileConnectionActionV2",
    "ProfileDisplayNamePatchV2",
    "ProfileRebindV2",
    "ProjectActionV2",
    "ProjectCapabilityProjectionV2",
    "ProjectCreateV2",
    "ProjectHeadRefV2",
    "ProjectPatchV2",
    "ProjectValidationRequestV2",
    "ProjectValidationV2",
    "RemoteProfilePageV2",
    "RemoteProfileV2",
    "RemoteWorkspaceProfileV2",
    "RuntimeContextSnapshotRefV2",
    "ServiceRestartV2",
    "SshHostCatalogRescanV2",
    "SshHostCatalogV2",
    "SshHostHintV2",
    "SshPromptStateV2",
    "SshTrustStateV2",
    "SuccessorTransitionRefV2",
    "SystemOpenSshProfileCreateV2",
    "TaskActionV2",
    "TaskAdmissionRefV2",
    "TransitionActionV2",
    "TransitionReplaceV2",
    "WorkspaceSnapshotRefV2",
    "evolution_capabilities_sha256_for",
]
