from __future__ import annotations

from datetime import datetime
from ipaddress import ip_address
from math import isfinite
import re
from typing import Annotated, Generic, Literal, TypeVar
from urllib.parse import urlsplit

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
from typing_extensions import TypeAliasType


SCHEMA_VERSION = "1"
MAX_PAGE_SIZE = 100
MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 8_192
MAX_JSON_COLLECTION_ITEMS = 1_024
MAX_JSON_TEXT_BYTES = 262_144
MAX_JSON_TOTAL_BYTES = 1_048_576
MAX_SAFE_INTEGER = 9_007_199_254_740_991
MAX_ARTIFACT_PREVIEW_BYTES = 2 * 1024 * 1024
MAX_ARTIFACT_PREVIEW_DOCUMENTS = 128

JsonValueV1 = TypeAliasType(
    "JsonValueV1",
    str | int | float | bool | None | list["JsonValueV1"] | dict[str, "JsonValueV1"],
)


def _validate_opaque_text(value: str) -> str:
    if value != value.strip() or any(ord(char) < 0x20 for char in value):
        raise ValueError("must be trimmed text without control characters")
    return value


def _validate_user_text(value: str) -> str:
    if "\x00" in value:
        raise ValueError("must not contain NUL characters")
    return value


def _validate_utc_timestamp(value: str) -> str:
    try:
        datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError as exc:
        raise ValueError("must be a valid UTC RFC 3339 timestamp") from exc
    return value


def _validate_network_host(value: str) -> str:
    _validate_opaque_text(value)
    if any(marker in value for marker in ("/", "\\", "://", "@")):
        raise ValueError("host must be a hostname or IP address, not a URL or path")
    try:
        ip_address(value)
        return value
    except ValueError:
        pass
    if len(value) > 253 or any(
        re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) is None
        for label in value.rstrip(".").split(".")
    ):
        raise ValueError("host must be a valid hostname or IP address")
    return value


def _validate_remote_user(value: str) -> str:
    _validate_opaque_text(value)
    if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value) is None:
        raise ValueError("user must be a remote account name, not a path")
    return value


OpaqueId = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=256),
    AfterValidator(_validate_opaque_text),
]
ShortText = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=512),
    AfterValidator(_validate_user_text),
]
LongText = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=65_536),
    AfterValidator(_validate_user_text),
]
DiffText = Annotated[
    str,
    StringConstraints(strict=True, max_length=65_536),
    AfterValidator(_validate_user_text),
]
Digest = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$"),
]
ETag = Annotated[
    str,
    StringConstraints(strict=True, pattern=r'^"[0-9a-f]{64}"$'),
]
UtcTimestamp = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=(
            r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])"
            r"T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,9})?Z$"
        ),
    ),
    AfterValidator(_validate_utc_timestamp),
]
NetworkHost = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=253),
    AfterValidator(_validate_network_host),
]
RemoteUser = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=128),
    AfterValidator(_validate_remote_user),
]


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        use_enum_values=True,
    )


class BoundedJsonObjectV1(RootModel[dict[str, JsonValueV1]]):
    """An explicitly budgeted JSON object for method config and safe details."""

    model_config = ConfigDict(
        frozen=True,
        strict=True,
        validate_default=True,
        json_schema_extra={
            "maxProperties": MAX_JSON_COLLECTION_ITEMS,
            "x-openevo-max-depth": MAX_JSON_DEPTH,
            "x-openevo-max-nodes": MAX_JSON_NODES,
            "x-openevo-max-text-bytes": MAX_JSON_TEXT_BYTES,
            "x-openevo-max-total-bytes": MAX_JSON_TOTAL_BYTES,
        },
    )

    @model_validator(mode="after")
    def _validate_budget(self) -> BoundedJsonObjectV1:
        nodes = 0
        text_bytes = 0
        encoded_bytes = 2
        pending: list[tuple[JsonValueV1, int]] = [(self.root, 1)]
        while pending:
            value, depth = pending.pop()
            nodes += 1
            if nodes > MAX_JSON_NODES:
                raise ValueError("JSON detail exceeds the node budget")
            if depth > MAX_JSON_DEPTH:
                raise ValueError("JSON detail exceeds the depth budget")
            if isinstance(value, dict):
                if len(value) > MAX_JSON_COLLECTION_ITEMS:
                    raise ValueError("JSON object exceeds the item budget")
                for key, child in value.items():
                    if not key or len(key) > 256 or key != key.strip():
                        raise ValueError("JSON object keys must be short trimmed strings")
                    key_size = len(key.encode("utf-8"))
                    text_bytes += key_size
                    encoded_bytes += key_size + 4
                    pending.append((child, depth + 1))
            elif isinstance(value, list):
                if len(value) > MAX_JSON_COLLECTION_ITEMS:
                    raise ValueError("JSON array exceeds the item budget")
                pending.extend((child, depth + 1) for child in value)
            elif isinstance(value, str):
                size = len(value.encode("utf-8"))
                text_bytes += size
                encoded_bytes += size + 2
            elif isinstance(value, bool) or value is None:
                encoded_bytes += 5
            elif isinstance(value, int):
                if abs(value) > MAX_SAFE_INTEGER:
                    raise ValueError("JSON integers must be JavaScript safe integers")
                encoded_bytes += len(str(value))
            elif isinstance(value, float):
                if not isfinite(value):
                    raise ValueError("JSON numbers must be finite")
                encoded_bytes += 32
            else:  # pragma: no cover - JsonValue validation rejects this first.
                raise ValueError("unsupported JSON detail value")
            if text_bytes > MAX_JSON_TEXT_BYTES:
                raise ValueError("JSON detail exceeds the text budget")
            if encoded_bytes > MAX_JSON_TOTAL_BYTES:
                raise ValueError("JSON detail exceeds the byte budget")
        return self


class ApiErrorV1(StrictModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    request_id: OpaqueId
    code: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,127}$")]
    http_status: int = Field(ge=400, le=599)
    message: ShortText
    severity: Literal["info", "warning", "blocking"]
    category: Literal[
        "contract",
        "authentication",
        "profile",
        "connection",
        "project",
        "capability",
        "operation",
        "run",
        "artifact",
        "service",
        "diagnostic",
        "maintenance",
    ]
    retryable: bool
    repair_action: Literal[
        "none",
        "openevo_can_retry",
        "user_input_required",
        "reconnect_required",
        "upgrade_required",
    ]
    next_action: ShortText | None = None
    details: BoundedJsonObjectV1 = Field(default_factory=lambda: BoundedJsonObjectV1({}))
    logs_ref: OpaqueId | None = None


BuildChannelV1 = Literal["release", "development", "test"]
ProviderKindV1 = Literal[
    "desktop_sidecar",
    "contract_simulator",
    "scaffold",
    "dry_run",
]
FeatureFlagV1 = Literal[
    "remote_profiles",
    "project_validation",
    "operation_events",
    "run_observability",
    "artifact_inspection",
    "service_control",
    "diagnostics",
    "maintenance",
]


class VersionV1(StrictModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    api_name: Literal["openevo-desktop-local-api"] = "openevo-desktop-local-api"
    preferred_major: Literal[1] = 1
    supported_majors: tuple[Literal[1], ...] = (1,)
    openapi_sha256: Digest
    build_version: ShortText
    source_commit: Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{7,40}$")]
    build_channel: BuildChannelV1
    provider_kind: ProviderKindV1
    feature_flags: tuple[FeatureFlagV1, ...] = ()

    @model_validator(mode="after")
    def _release_provider_is_real_sidecar(self) -> VersionV1:
        if self.build_channel == "release" and self.provider_kind != "desktop_sidecar":
            raise ValueError("release builds require provider_kind=desktop_sidecar")
        if not self.supported_majors or self.preferred_major not in self.supported_majors:
            raise ValueError("preferred_major must be included in supported_majors")
        if len(self.feature_flags) != len(set(self.feature_flags)):
            raise ValueError("feature_flags must be unique")
        return self


class HealthV1(StrictModel):
    service: Literal["openevo-sidecar"] = "openevo-sidecar"
    status: Literal["ok", "degraded", "starting"]
    protocol: Literal["openevo-native-sidecar-v1"] | None = None
    instance_id: (
        Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{32}$")] | None
    ) = None
    instance_proof: Digest | None = None

    @model_validator(mode="after")
    def _native_proof_is_atomic(self) -> HealthV1:
        values = (self.protocol, self.instance_id, self.instance_proof)
        if any(value is not None for value in values) and not all(
            value is not None for value in values
        ):
            raise ValueError("native health proof fields must all be present or all be absent")
        if self.protocol is not None and self.status != "ok":
            raise ValueError("native readiness proof is valid only for an ok sidecar")
        return self


class ContractNegotiationV1(StrictModel):
    selected_major: Literal[1]
    desktop_openapi_sha256: Digest
    core_openapi_sha256: Digest | None = None
    compatible: bool


class HostKeyReviewV1(StrictModel):
    algorithm: Literal["ssh-ed25519", "ecdsa-sha2-nistp256", "rsa-sha2-512"]
    fingerprint: Annotated[
        str,
        StringConstraints(strict=True, pattern=r"^SHA256:[A-Za-z0-9+/]{20,88}={0,2}$"),
    ]


class CoreCompatibilityV1(StrictModel):
    contract_version: Literal["1"] = "1"
    contract_digest: Digest
    core_version: ShortText


class ConnectionFailureV1(StrictModel):
    code: Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z][a-z0-9_]{0,127}$")]
    message: ShortText
    retryable: bool
    next_action: ShortText | None = None


class CoreConnectionStateV1(StrictModel):
    state: Literal[
        "disconnected",
        "connecting",
        "host_key_review",
        "checking",
        "bootstrapping",
        "core_starting",
        "online",
        "degraded",
        "reconnecting",
        "offline",
    ]
    profile_id: OpaqueId | None = None
    active_tunnel: bool
    operation_id: OpaqueId | None = None
    host_key_review: HostKeyReviewV1 | None = None
    core: CoreCompatibilityV1 | None = None
    failure: ConnectionFailureV1 | None = None

    @model_validator(mode="after")
    def _valid_state_shape(self) -> CoreConnectionStateV1:
        operation_states = {
            "connecting",
            "host_key_review",
            "checking",
            "bootstrapping",
            "core_starting",
            "reconnecting",
        }
        active_states = operation_states | {"online", "degraded", "offline"}
        if self.state in operation_states and self.operation_id is None:
            raise ValueError(f"{self.state} requires an operation_id")
        if self.state in active_states and self.profile_id is None:
            raise ValueError(f"{self.state} requires a profile_id")
        if (self.state == "host_key_review") != (self.host_key_review is not None):
            raise ValueError("host_key_review data is valid only in host_key_review state")
        if self.state == "online":
            if not self.active_tunnel or self.core is None:
                raise ValueError("online requires an active tunnel and compatible Core metadata")
        elif self.state not in {"degraded", "reconnecting"} and self.core is not None:
            raise ValueError("Core metadata is valid only after a compatible connection")
        if self.state in {"degraded", "offline"}:
            if self.failure is None:
                raise ValueError(f"{self.state} requires a typed failure")
        elif self.failure is not None:
            raise ValueError("failure data is valid only in degraded or offline state")
        if self.state in {"disconnected", "offline"} and self.active_tunnel:
            raise ValueError(f"{self.state} cannot report an active tunnel")
        return self


class ActiveProjectStateV1(StrictModel):
    project_id: OpaqueId
    project_etag: ETag
    profile_id: OpaqueId
    connection_state: Literal["offline", "connecting", "ready", "blocked"]


class DesktopStateV1(StrictModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    observed_at: UtcTimestamp
    contract: ContractNegotiationV1
    core: CoreConnectionStateV1
    active_project: ActiveProjectStateV1 | None = None
    pending_operation_ids: tuple[OpaqueId, ...] = ()


CredentialSlotKindV1 = Literal[
    "ssh_password",
    "ssh_private_key",
    "ssh_private_key_passphrase",
    "http_proxy_password",
    "https_proxy_password",
]


class CredentialSlotStatusV1(StrictModel):
    kind: CredentialSlotKindV1
    status: Literal["empty", "stored", "unavailable"]
    updated_at: UtcTimestamp | None = None


class NetworkProxyV1(StrictModel):
    http_url: ShortText | None = None
    https_url: ShortText | None = None
    no_proxy: tuple[ShortText, ...] = ()

    @field_validator("http_url", "https_url")
    @classmethod
    def _safe_proxy_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("proxy URL must use http or https and include a host")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("proxy URL must not contain user information")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("proxy URL must not contain path, query, or fragment data")
        return value


SshAuthenticationKindV1 = Literal["ssh_agent", "native_private_key", "native_password"]


class RemoteProfileCreateV1(StrictModel):
    name: ShortText
    host: NetworkHost
    port: int = Field(default=22, ge=1, le=65_535)
    user: RemoteUser
    authentication_kind: SshAuthenticationKindV1 = "ssh_agent"
    proxy: NetworkProxyV1 = Field(default_factory=NetworkProxyV1)


class RemoteProfilePatchV1(StrictModel):
    name: ShortText | None = None
    host: NetworkHost | None = None
    port: int | None = Field(default=None, ge=1, le=65_535)
    user: RemoteUser | None = None
    authentication_kind: SshAuthenticationKindV1 | None = None
    proxy: NetworkProxyV1 | None = None

    @model_validator(mode="after")
    def _not_empty(self) -> RemoteProfilePatchV1:
        if not self.model_fields_set:
            raise ValueError("profile patch must include at least one field")
        return self


class RemoteProfileV1(StrictModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    profile_id: OpaqueId
    name: ShortText
    host: NetworkHost
    port: int = Field(ge=1, le=65_535)
    user: RemoteUser
    authentication_kind: SshAuthenticationKindV1
    credential_slots: tuple[CredentialSlotStatusV1, ...] = ()
    proxy: NetworkProxyV1 = Field(default_factory=NetworkProxyV1)
    connection_state: Literal[
        "disconnected",
        "connecting",
        "host_key_required",
        "connected",
        "failed",
    ] = "disconnected"
    host_key_fingerprint: ShortText | None = None
    etag: ETag
    created_at: UtcTimestamp
    updated_at: UtcTimestamp

    @field_validator("credential_slots")
    @classmethod
    def _unique_slots(
        cls, values: tuple[CredentialSlotStatusV1, ...]
    ) -> tuple[CredentialSlotStatusV1, ...]:
        kinds = tuple(value.kind for value in values)
        if len(kinds) != len(set(kinds)):
            raise ValueError("credential slot kinds must be unique")
        return values


class HostKeyAcceptV1(StrictModel):
    algorithm: Literal["ssh-ed25519", "ecdsa-sha2-nistp256", "rsa-sha2-512"]
    fingerprint: Annotated[
        str,
        StringConstraints(strict=True, pattern=r"^SHA256:[A-Za-z0-9+/]{20,88}={0,2}$"),
    ]


class ResourceRefV1(StrictModel):
    resource_type: Literal[
        "profile",
        "project",
        "operation",
        "run",
        "artifact",
        "service",
        "diagnostic",
        "maintenance",
    ]
    resource_id: OpaqueId


class ContentRefV1(StrictModel):
    content_id: OpaqueId
    sha256: Digest
    byte_size: int = Field(ge=0, le=1_000_000_000_000)


ExecutionModeV1 = Literal["codex_subscription_transcript", "self-deployed"]


class ExecutionSettingsV1(StrictModel):
    mode: ExecutionModeV1
    capture_mode: Literal["transcript"] = "transcript"
    token_level_metrics_available: Literal[False] = False
    codex_model: ShortText | None = None
    managed_model_id: OpaqueId | None = None

    @model_validator(mode="after")
    def _mode_fields(self) -> ExecutionSettingsV1:
        if self.mode == "codex_subscription_transcript":
            if self.codex_model is None or self.managed_model_id is not None:
                raise ValueError("subscription mode requires only codex_model")
        elif self.managed_model_id is None or self.codex_model is not None:
            raise ValueError("self-deployed mode requires only managed_model_id")
        return self


class ProjectTaskV1(StrictModel):
    title: ShortText
    objective: LongText
    task_ref: ContentRefV1 | None = None


class ProjectSourceV1(StrictModel):
    kind: Literal["scratch", "native_folder_snapshot", "git_snapshot", "remote_snapshot"]
    display_name: ShortText
    source_ref: ContentRefV1 | None = None

    @model_validator(mode="after")
    def _snapshot_required(self) -> ProjectSourceV1:
        if self.kind == "scratch" and self.source_ref is not None:
            raise ValueError("scratch sources must not include source_ref")
        if self.kind != "scratch" and self.source_ref is None:
            raise ValueError("non-scratch sources require a content-addressed source_ref")
        return self


class EvolutionTargetSelectionV1(StrictModel):
    enabled: bool
    method: OpaqueId | None = None
    config: BoundedJsonObjectV1 = Field(default_factory=lambda: BoundedJsonObjectV1({}))

    @model_validator(mode="after")
    def _enabled_method(self) -> EvolutionTargetSelectionV1:
        if self.enabled and self.method is None:
            raise ValueError("enabled evolution targets require a method")
        return self


class EvolutionSelectionsV1(RootModel[dict[str, EvolutionTargetSelectionV1]]):
    model_config = ConfigDict(
        frozen=True,
        strict=True,
        validate_default=True,
        json_schema_extra={"maxProperties": 128},
    )

    @model_validator(mode="after")
    def _validate_targets(self) -> EvolutionSelectionsV1:
        if len(self.root) > 128:
            raise ValueError("at most 128 evolution targets are allowed")
        for target_id in self.root:
            _validate_opaque_text(target_id)
            if len(target_id) > 256:
                raise ValueError("evolution target IDs must not exceed 256 characters")
        return self


class ProjectCreateV1(StrictModel):
    name: ShortText
    profile_id: OpaqueId
    task: ProjectTaskV1
    source: ProjectSourceV1
    execution: ExecutionSettingsV1
    evolution: EvolutionSelectionsV1


class ProjectPatchV1(StrictModel):
    name: ShortText | None = None
    profile_id: OpaqueId | None = None
    task: ProjectTaskV1 | None = None
    source: ProjectSourceV1 | None = None
    execution: ExecutionSettingsV1 | None = None
    evolution: EvolutionSelectionsV1 | None = None

    @model_validator(mode="after")
    def _not_empty(self) -> ProjectPatchV1:
        if not self.model_fields_set:
            raise ValueError("project patch must include at least one field")
        return self


class ProjectV1(StrictModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    project_id: OpaqueId
    name: ShortText
    profile_id: OpaqueId
    task: ProjectTaskV1
    source: ProjectSourceV1
    execution: ExecutionSettingsV1
    evolution: EvolutionSelectionsV1
    state: Literal["draft", "active", "archived", "blocked"]
    current_revision_id: OpaqueId | None = None
    etag: ETag
    created_at: UtcTimestamp
    updated_at: UtcTimestamp


class OperationProgressV1(StrictModel):
    current: int = Field(ge=0)
    total: int = Field(ge=1)
    label: ShortText

    @model_validator(mode="after")
    def _current_within_total(self) -> OperationProgressV1:
        if self.current > self.total:
            raise ValueError("operation progress current must not exceed total")
        return self


class NormalizedCheckV1(StrictModel):
    check_id: OpaqueId
    label: ShortText
    status: Literal["pending", "running", "passed", "warning", "failed", "skipped"]
    summary: ShortText
    repair_action: Literal[
        "none", "openevo_can_retry", "user_input_required", "reconnect_required"
    ] = "none"


class ConnectionOperationResultV1(StrictModel):
    kind: Literal["connection"] = "connection"
    profile_id: OpaqueId
    connection_state: Literal["connected", "disconnected", "host_key_required"]


class ProjectOperationResultV1(StrictModel):
    kind: Literal["project"] = "project"
    project_id: OpaqueId
    project_etag: ETag
    active: bool


class DiagnosticFindingV1(StrictModel):
    finding_id: OpaqueId
    severity: Literal["info", "warning", "blocking"]
    category: Literal["desktop", "ssh", "core", "model_service", "workspace", "run", "evolution"]
    summary: ShortText
    next_action: ShortText | None = None


class DiagnosticReportV1(StrictModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    diagnostic_id: OpaqueId
    status: Literal["healthy", "degraded", "blocked"]
    generated_at: UtcTimestamp
    checks: tuple[NormalizedCheckV1, ...]
    findings: tuple[DiagnosticFindingV1, ...]
    etag: ETag


class DiagnosticOperationResultV1(StrictModel):
    kind: Literal["diagnostic"] = "diagnostic"
    report: DiagnosticReportV1


LocalOperationResultV1 = Annotated[
    ConnectionOperationResultV1 | ProjectOperationResultV1 | DiagnosticOperationResultV1,
    Field(discriminator="kind"),
]


class LocalOperationV1(StrictModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    operation_id: OpaqueId
    operation_kind: Literal[
        "profile_connect",
        "profile_disconnect",
        "host_key_accept",
        "project_activate",
        "project_doctor",
        "project_repair",
        "bootstrap",
        "workspace_sync",
        "service_restart",
        "service_stop",
        "diagnostics",
        "cache_cleanup",
    ]
    state: Literal["queued", "running", "succeeded", "failed", "cancelling", "cancelled"]
    resource: ResourceRefV1
    progress: OperationProgressV1 | None = None
    checks: tuple[NormalizedCheckV1, ...] = ()
    result: LocalOperationResultV1 | None = None
    error: ApiErrorV1 | None = None
    created_at: UtcTimestamp
    started_at: UtcTimestamp | None = None
    finished_at: UtcTimestamp | None = None
    etag: ETag

    @model_validator(mode="after")
    def _terminal_shape(self) -> LocalOperationV1:
        terminal = self.state in {"succeeded", "failed", "cancelled"}
        if terminal != (self.finished_at is not None):
            raise ValueError("terminal operation state and finished_at must agree")
        if self.state == "failed" and self.error is None:
            raise ValueError("failed operations require an error")
        if self.state != "failed" and self.error is not None:
            raise ValueError("only failed operations may include an error")
        return self


class LogEntryV1(StrictModel):
    log_id: OpaqueId
    occurred_at: UtcTimestamp
    level: Literal["debug", "info", "warning", "error"]
    source: Literal["desktop", "connection", "core", "run", "evolution", "service"]
    message: LongText
    code: ShortText | None = None


class CapabilitySupportAxisV1(StrictModel):
    supported: bool
    reason_code: ShortText | None = None
    summary: ShortText | None = None

    @model_validator(mode="after")
    def _unsupported_reason(self) -> CapabilitySupportAxisV1:
        if self.supported and (self.reason_code is not None or self.summary is not None):
            raise ValueError("supported axes must not include an unsupported reason")
        if not self.supported and (self.reason_code is None or self.summary is None):
            raise ValueError("unsupported axes require reason_code and summary")
        return self


class MethodSupportV1(StrictModel):
    overall: Literal["supported", "unsupported"]
    execution: CapabilitySupportAxisV1
    capture: CapabilitySupportAxisV1
    harness: CapabilitySupportAxisV1
    runtime: CapabilitySupportAxisV1


class ResolvedMethodCapabilityV1(StrictModel):
    method_id: OpaqueId
    identity_digest: Digest
    support: MethodSupportV1


class MethodCapabilityV1(StrictModel):
    method_id: OpaqueId
    display_name: ShortText
    description: ShortText
    maturity: Literal["experimental", "preview", "stable"]
    identity_digest: Digest
    config_schema: BoundedJsonObjectV1
    default_config: BoundedJsonObjectV1
    support: MethodSupportV1


class SelectionResolverCapabilityV1(StrictModel):
    selection_value: OpaqueId
    display_name: ShortText
    description: ShortText
    resolved_methods: tuple[ResolvedMethodCapabilityV1, ...]


class TargetCapabilityV1(StrictModel):
    target_id: OpaqueId
    display_name: ShortText
    description: ShortText
    artifact_type: Literal["text_memory", "skill_bundle", "agent_system", "parametric_memory"]
    release_enabled: bool
    configured_default_method_id: OpaqueId
    effective_default_method_id: OpaqueId | None = None
    methods: tuple[MethodCapabilityV1, ...]
    accepted_methods: tuple[ResolvedMethodCapabilityV1, ...]
    selection_resolvers: tuple[SelectionResolverCapabilityV1, ...] = ()


class CapabilitiesEnvelopeV1(StrictModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    project_id: OpaqueId
    execution_mode: ExecutionModeV1
    source: Literal["verified_remote_core"] = "verified_remote_core"
    registry_verified: Literal[True] = True
    registry_digest: Digest
    core_version: ShortText
    fetched_at: UtcTimestamp
    targets: tuple[TargetCapabilityV1, ...]


class ProjectValidationRequestV1(StrictModel):
    project_etag: ETag
    capability_registry_digest: Digest
    execution: ExecutionSettingsV1
    evolution: EvolutionSelectionsV1


class ValidationIssueV1(StrictModel):
    issue_id: OpaqueId
    severity: Literal["warning", "blocking"]
    field: ShortText
    code: ShortText
    message: ShortText
    next_action: ShortText | None = None


class ProjectValidationV1(StrictModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    project_id: OpaqueId
    project_etag: ETag
    capability_registry_digest: Digest
    valid: bool
    issues: tuple[ValidationIssueV1, ...]
    validated_at: UtcTimestamp

    @model_validator(mode="after")
    def _validity_matches_issues(self) -> ProjectValidationV1:
        has_blocker = any(issue.severity == "blocking" for issue in self.issues)
        if self.valid == has_blocker:
            raise ValueError("valid must be false exactly when blocking issues exist")
        return self


class ImmutableSnapshotRefV1(StrictModel):
    snapshot_id: OpaqueId
    digest: Digest


class RevisionRefV1(StrictModel):
    revision_id: OpaqueId
    generation: int = Field(ge=0)
    manifest_digest: Digest
    state: Literal["active", "queued", "preparing", "failed", "cancelled"]


class RunCreateV1(StrictModel):
    project_id: OpaqueId
    project_snapshot: ImmutableSnapshotRefV1
    task_snapshot: ImmutableSnapshotRefV1
    workspace_snapshot: ImmutableSnapshotRefV1
    capability_registry_digest: Digest
    required_revision: RevisionRefV1

    @model_validator(mode="after")
    def _required_revision_must_be_reachable(self) -> RunCreateV1:
        if self.required_revision.state in {"failed", "cancelled"}:
            raise ValueError("a run cannot require a terminal revision")
        return self


class RunQueuedReasonV1(StrictModel):
    code: Literal[
        "capacity_unavailable",
        "required_revision_uncommitted",
        "service_starting",
        "project_activation_pending",
    ]
    summary: ShortText
    retry_after_seconds: int | None = Field(default=None, ge=1, le=86_400)


class RunAttemptV1(StrictModel):
    attempt_id: OpaqueId
    number: int = Field(ge=1)
    state: Literal[
        "queued", "preparing", "running", "cancelling", "succeeded", "failed", "cancelled"
    ]
    started_at: UtcTimestamp | None = None
    finished_at: UtcTimestamp | None = None


class RunV1(StrictModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    run_id: OpaqueId
    project_id: OpaqueId
    state: Literal[
        "queued", "preparing", "running", "cancelling", "succeeded", "failed", "cancelled"
    ]
    queued_reason: RunQueuedReasonV1 | None = None
    project_snapshot: ImmutableSnapshotRefV1
    task_snapshot: ImmutableSnapshotRefV1
    workspace_snapshot: ImmutableSnapshotRefV1
    capability_registry_digest: Digest
    pinned_revision: RevisionRefV1
    successor_revision: RevisionRefV1 | None = None
    latest_attempt: RunAttemptV1
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    etag: ETag
    error: ApiErrorV1 | None = None

    @model_validator(mode="after")
    def _run_state_shape(self) -> RunV1:
        if (self.state == "queued") != (self.queued_reason is not None):
            raise ValueError("queued runs require a queued_reason and other states forbid it")
        if self.state == "failed" and self.error is None:
            raise ValueError("failed runs require an error")
        if self.state != "failed" and self.error is not None:
            raise ValueError("only failed runs may include an error")
        return self


class TimelineEntryV1(StrictModel):
    entry_id: OpaqueId
    occurred_at: UtcTimestamp
    stage: Literal[
        "admission",
        "workspace",
        "agent",
        "capture",
        "dataset",
        "evolution",
        "materialization",
        "revision",
    ]
    state: Literal["queued", "running", "succeeded", "failed", "cancelled", "blocked"]
    title: ShortText
    summary: ShortText
    progress: OperationProgressV1 | None = None


class ContextContributionV1(StrictModel):
    target_id: OpaqueId
    artifact_id: OpaqueId
    artifact_type: Literal["text_memory", "skill_bundle", "agent_system", "parametric_memory"]
    selected: bool
    summary: ShortText


class RunContextV1(StrictModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    run_id: OpaqueId
    pinned_revision: RevisionRefV1
    successor_revision: RevisionRefV1 | None = None
    contributions: tuple[ContextContributionV1, ...]


class ArtifactLineageV1(StrictModel):
    source_dataset_ids: tuple[OpaqueId, ...] = ()
    parent_artifact_ids: tuple[OpaqueId, ...] = ()
    producing_job_id: OpaqueId | None = None


class ArtifactCompatibilityV1(StrictModel):
    execution_modes: tuple[ExecutionModeV1, ...]
    harness_ids: tuple[OpaqueId, ...] = ()
    base_model_ids: tuple[OpaqueId, ...] = ()


class ArtifactScoreV1(StrictModel):
    name: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,127}$")]
    value: float

    @field_validator("value")
    @classmethod
    def _finite_score(cls, value: float) -> float:
        if not isfinite(value):
            raise ValueError("artifact scores must be finite")
        return value


class ArtifactBaseV1(StrictModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    artifact_id: OpaqueId
    project_id: OpaqueId
    run_id: OpaqueId
    target_id: OpaqueId
    display_name: ShortText
    summary: ShortText
    content_digest: Digest
    byte_size: int = Field(ge=0, le=1_000_000_000_000)
    lineage: ArtifactLineageV1
    compatibility: ArtifactCompatibilityV1
    scores: tuple[ArtifactScoreV1, ...] = ()
    selected: bool
    promoted: bool
    revision_ids: tuple[OpaqueId, ...] = ()
    created_at: UtcTimestamp


class TextMemoryArtifactV1(ArtifactBaseV1):
    artifact_type: Literal["text_memory"] = "text_memory"
    format: Literal["markdown", "plain_text"]


class SkillBundleArtifactV1(ArtifactBaseV1):
    artifact_type: Literal["skill_bundle"] = "skill_bundle"
    skill_count: int = Field(ge=1, le=1_024)


class AgentSystemArtifactV1(ArtifactBaseV1):
    artifact_type: Literal["agent_system"] = "agent_system"
    instruction_kind: Literal["agents", "claude", "gemini", "openhands_microagent", "generic"]


class ParametricMemoryArtifactV1(ArtifactBaseV1):
    artifact_type: Literal["parametric_memory"] = "parametric_memory"
    release_enabled: Literal[False] = False
    adapter_id: OpaqueId
    base_model_id: OpaqueId
    adapter_format: ShortText


ArtifactV1 = Annotated[
    TextMemoryArtifactV1
    | SkillBundleArtifactV1
    | AgentSystemArtifactV1
    | ParametricMemoryArtifactV1,
    Field(discriminator="artifact_type"),
]


class ArtifactDocumentV1(StrictModel):
    document_id: OpaqueId
    title: ShortText
    media_type: Literal["text/markdown", "text/plain"]
    content: LongText


class ArtifactContentV1(StrictModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    artifact_id: OpaqueId
    content_digest: Digest
    documents: tuple[ArtifactDocumentV1, ...] = Field(
        min_length=1, max_length=MAX_ARTIFACT_PREVIEW_DOCUMENTS
    )
    total_documents: int = Field(ge=1, le=1_000_000)
    truncated: bool

    @model_validator(mode="after")
    def _bounded_preview(self) -> ArtifactContentV1:
        if self.total_documents < len(self.documents):
            raise ValueError("total_documents cannot be smaller than the returned preview")
        if self.truncated != (self.total_documents > len(self.documents)):
            raise ValueError("truncated must agree with total_documents")
        aggregate_bytes = sum(len(document.content.encode("utf-8")) for document in self.documents)
        if aggregate_bytes > MAX_ARTIFACT_PREVIEW_BYTES:
            raise ValueError("artifact preview exceeds the aggregate byte budget")
        return self


class DiffLineV1(StrictModel):
    kind: Literal["context", "added", "removed"]
    old_line: int | None = Field(default=None, ge=1)
    new_line: int | None = Field(default=None, ge=1)
    text: DiffText


class DiffHunkV1(StrictModel):
    hunk_id: OpaqueId
    heading: ShortText
    lines: tuple[DiffLineV1, ...] = Field(max_length=10_000)


class ArtifactDiffV1(StrictModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    artifact_id: OpaqueId
    base_artifact_id: OpaqueId | None = None
    hunks: tuple[DiffHunkV1, ...] = Field(max_length=1_024)
    truncated: bool


class ServiceV1(StrictModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    service_id: OpaqueId
    display_name: ShortText
    kind: Literal["core", "gateway", "model", "worker", "artifact_store"]
    state: Literal["starting", "healthy", "degraded", "stopped", "failed", "unavailable"]
    health_summary: ShortText
    restart_supported: bool
    observed_at: UtcTimestamp
    etag: ETag


class DiagnosticRequestV1(StrictModel):
    scope: Literal["active_project", "connection", "core", "run", "services"]
    resource_id: OpaqueId | None = None

    @model_validator(mode="after")
    def _resource_scope(self) -> DiagnosticRequestV1:
        if self.scope in {"run", "services"} and self.resource_id is None:
            raise ValueError("run and services diagnostics require resource_id")
        if self.scope not in {"run", "services"} and self.resource_id is not None:
            raise ValueError("resource_id is only valid for run or services diagnostics")
        return self


class StateEventV1(StrictModel):
    kind: Literal["state_changed"] = "state_changed"
    state: DesktopStateV1


class ResourceEventV1(StrictModel):
    kind: Literal[
        "profile_changed",
        "project_changed",
        "operation_changed",
        "run_changed",
        "artifact_available",
        "service_changed",
    ]
    resource: ResourceRefV1
    change: Literal["created", "updated", "deleted"]


class TimelineEventV1(StrictModel):
    kind: Literal["run_timeline"] = "run_timeline"
    run_id: OpaqueId
    entry: TimelineEntryV1


class LogEventV1(StrictModel):
    kind: Literal["log_appended"] = "log_appended"
    resource: ResourceRefV1
    entry: LogEntryV1


class DiagnosticEventV1(StrictModel):
    kind: Literal["diagnostic_ready"] = "diagnostic_ready"
    diagnostic_id: OpaqueId
    operation_id: OpaqueId


class HeartbeatEventV1(StrictModel):
    kind: Literal["heartbeat"] = "heartbeat"


EventDataV1 = Annotated[
    StateEventV1
    | ResourceEventV1
    | TimelineEventV1
    | LogEventV1
    | DiagnosticEventV1
    | HeartbeatEventV1,
    Field(discriminator="kind"),
]


EventNameV1 = Literal[
    "desktop.v1.state.changed",
    "desktop.v1.profile.changed",
    "desktop.v1.project.changed",
    "desktop.v1.operation.changed",
    "desktop.v1.run.changed",
    "desktop.v1.run.timeline",
    "desktop.v1.log.appended",
    "desktop.v1.artifact.available",
    "desktop.v1.service.changed",
    "desktop.v1.diagnostic.ready",
    "desktop.v1.heartbeat",
]


_EVENT_NAMES_BY_KIND: dict[str, str] = {
    "state_changed": "desktop.v1.state.changed",
    "profile_changed": "desktop.v1.profile.changed",
    "project_changed": "desktop.v1.project.changed",
    "operation_changed": "desktop.v1.operation.changed",
    "run_changed": "desktop.v1.run.changed",
    "run_timeline": "desktop.v1.run.timeline",
    "log_appended": "desktop.v1.log.appended",
    "artifact_available": "desktop.v1.artifact.available",
    "service_changed": "desktop.v1.service.changed",
    "diagnostic_ready": "desktop.v1.diagnostic.ready",
    "heartbeat": "desktop.v1.heartbeat",
}


class EventEnvelopeV1(StrictModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    event_id: OpaqueId
    event_name: EventNameV1
    occurred_at: UtcTimestamp
    sequence: int = Field(ge=0)
    data: EventDataV1

    @model_validator(mode="after")
    def _name_matches_data(self) -> EventEnvelopeV1:
        expected = _EVENT_NAMES_BY_KIND[self.data.kind]
        if self.event_name != expected:
            raise ValueError("event_name does not match the typed event data")
        return self


class SseFrameV1(StrictModel):
    id: OpaqueId
    event: EventNameV1
    data: EventEnvelopeV1

    @model_validator(mode="after")
    def _frame_matches_envelope(self) -> SseFrameV1:
        if self.id != self.data.event_id or self.event != self.data.event_name:
            raise ValueError("SSE frame id and event must match the envelope")
        return self


PageItemT = TypeVar("PageItemT")


class PageV1(StrictModel, Generic[PageItemT]):
    schema_version: Literal["1"] = SCHEMA_VERSION
    items: tuple[PageItemT, ...] = Field(max_length=MAX_PAGE_SIZE)
    next_cursor: OpaqueId | None = None
    has_more: bool

    @model_validator(mode="after")
    def _cursor_matches_has_more(self) -> PageV1[PageItemT]:
        if self.has_more != (self.next_cursor is not None):
            raise ValueError("has_more and next_cursor must agree")
        return self


RemoteProfilePageV1 = PageV1[RemoteProfileV1]
ProjectPageV1 = PageV1[ProjectV1]
LogPageV1 = PageV1[LogEntryV1]
RunPageV1 = PageV1[RunV1]
TimelinePageV1 = PageV1[TimelineEntryV1]
ArtifactPageV1 = PageV1[ArtifactV1]
ServicePageV1 = PageV1[ServiceV1]


__all__ = tuple(sorted(name for name in globals() if name.endswith("V1"))) + (
    "Digest",
    "ETag",
    "LongText",
    "MAX_PAGE_SIZE",
    "OpaqueId",
    "SCHEMA_VERSION",
    "ShortText",
    "UtcTimestamp",
)
