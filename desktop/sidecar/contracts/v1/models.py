from __future__ import annotations

from datetime import datetime
from ipaddress import ip_address
import json
from math import isfinite
import re
from typing import Annotated, Any, Generic, Literal, TypeVar
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    SerializerFunctionWrapHandler,
    StringConstraints,
    field_validator,
    model_serializer,
    model_validator,
)
from typing_extensions import TypeAliasType

from openevo.codex_models import validate_codex_model_ref
from openevo.backend.contracts.v1 import models as _core_contract
from openevo.evolution.framework.capabilities import EvolutionCapabilitiesV1


ApiErrorV1 = _core_contract.ApiErrorV1
ArtifactContentV1 = _core_contract.ArtifactContentV1
ArtifactDiffV1 = _core_contract.ArtifactDiffV1
ArtifactPageV1 = _core_contract.ArtifactPageV1
ArtifactV1 = _core_contract.ArtifactSummaryV1
CacheCleanupRequestV1 = _core_contract.CacheCleanupRequestV1
DiagnosticV1 = _core_contract.DiagnosticV1
DiagnosticsRequestV1 = _core_contract.DiagnosticsRequestV1
LogEntryV1 = _core_contract.LogEntryV1
LogPageV1 = _core_contract.LogPageV1
ModelPreparationV1 = _core_contract.ModelPreparationV1
OperationV1 = _core_contract.OperationV1
ReferencedLogPageV1 = _core_contract.ReferencedLogPageV1
RevisionRefV1 = _core_contract.RevisionRefV1
RunContextV1 = _core_contract.RunContextV1
RunPageV1 = _core_contract.RunPageV1
RunSummaryV1 = _core_contract.RunSummaryV1
RunTimelinePageV1 = _core_contract.RunTimelinePageV1
RunV1 = _core_contract.RunV1
ServicePageV1 = _core_contract.ServicePageV1
ServiceSummaryV1 = _core_contract.ServiceSummaryV1
TimelineEntryV1 = _core_contract.TimelineEntryV1
ValidationCheckV1 = _core_contract.ValidationCheckV1


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
MAX_PROJECT_EVOLUTION_BYTES = 1_048_576

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


def _validate_model_ref(value: str) -> str:
    if value != value.strip() or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ValueError("model reference must be trimmed text without control characters")
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
ProjectDisplayName = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=128),
    AfterValidator(_validate_user_text),
]
CoreShortText = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=256),
    AfterValidator(_validate_user_text),
]
EvolutionId = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$",
    ),
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
AgentModelRefText = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=256),
    AfterValidator(_validate_model_ref),
]
HuggingFaceModel = AgentModelRefText


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        use_enum_values=True,
    )


def _non_nullable_patch_json_schema(schema: dict[str, Any]) -> None:
    for property_schema in schema.get("properties", {}).values():
        property_schema.pop("default", None)
        any_of = property_schema.get("anyOf")
        if not any_of:
            continue
        non_null = [entry for entry in any_of if entry.get("type") != "null"]
        if len(non_null) != 1 or len(non_null) == len(any_of):
            continue
        title = property_schema.get("title")
        property_schema.clear()
        property_schema.update(non_null[0])
        if title is not None:
            property_schema["title"] = title


class PatchModel(StrictModel):
    model_config = ConfigDict(json_schema_extra=_non_nullable_patch_json_schema)

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_null(cls, value: Any) -> Any:
        if isinstance(value, dict) and any(child is None for child in value.values()):
            raise ValueError("patch properties may be omitted but must not be null")
        return value

    @model_serializer(mode="wrap")
    def _serialize_only_set_fields(self, handler: SerializerFunctionWrapHandler) -> Any:
        serialized = handler(self)
        if not isinstance(serialized, dict):  # pragma: no cover - model handlers return dicts.
            return serialized
        return {key: value for key, value in serialized.items() if key in self.model_fields_set}


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


ExecutionModeV1 = Literal["codex_subscription_transcript", "self-deployed"]
ExecutionModeSupportStateV1 = Literal["supported", "unavailable", "unsupported"]
ExecutionModeReasonCodeV1 = Literal[
    "self_deployed_release_unavailable",
    "execution_mode_release_unsupported",
]


class ExecutionModeCapabilityV1(StrictModel):
    mode: ExecutionModeV1
    display_name: ShortText
    support_state: ExecutionModeSupportStateV1
    reason_code: ExecutionModeReasonCodeV1 | None = None
    message: ShortText

    @model_validator(mode="after")
    def _reason_matches_support(self) -> ExecutionModeCapabilityV1:
        if self.support_state == "supported" and self.reason_code is not None:
            raise ValueError("supported execution modes cannot include a reason code")
        if self.support_state != "supported" and self.reason_code is None:
            raise ValueError("unavailable and unsupported execution modes require a reason code")
        return self


class ExecutionModeCapabilitiesV1(StrictModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    modes: tuple[ExecutionModeCapabilityV1, ...] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def _exact_known_modes(self) -> ExecutionModeCapabilitiesV1:
        mode_ids = tuple(capability.mode for capability in self.modes)
        if len(mode_ids) != len(set(mode_ids)):
            raise ValueError("execution mode capabilities must not contain duplicates")
        if set(mode_ids) != {"codex_subscription_transcript", "self-deployed"}:
            raise ValueError(
                "execution mode capabilities must contain every known mode exactly once"
            )
        return self


class DesktopStateV1(StrictModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    observed_at: UtcTimestamp
    contract: ContractNegotiationV1
    execution_mode_capabilities: ExecutionModeCapabilitiesV1
    core: CoreConnectionStateV1
    active_project: ActiveProjectStateV1 | None = None
    pending_operation_ids: tuple[OpaqueId, ...] = ()


CredentialSlotKindV1 = Literal[
    "ssh_password",
    "ssh_private_key",
    "ssh_private_key_passphrase",
    "http_proxy_password",
    "https_proxy_password",
    "hugging_face_token",
]


class CredentialSlotStatusV1(StrictModel):
    kind: CredentialSlotKindV1
    status: Literal["empty", "stored", "unavailable"]
    updated_at: UtcTimestamp | None = None


class NetworkProxyV1(StrictModel):
    http_url: ShortText | None = None
    https_url: ShortText | None = None
    no_proxy: tuple[ShortText, ...] = ()

    @field_validator("no_proxy", mode="before")
    @classmethod
    def _normalize_no_proxy_json_array(cls, value: Any) -> tuple[Any, ...]:
        if isinstance(value, tuple):
            return value
        if isinstance(value, list):
            return tuple(value)
        raise ValueError("no_proxy must be a JSON array")

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


class RemoteProfilePatchV1(PatchModel):
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


class ExecutionSettingsV1(StrictModel):
    mode: ExecutionModeV1
    capture_mode: Literal["transcript"] = "transcript"
    token_level_metrics_available: Literal[False] = False
    codex_model: AgentModelRefText | None = None
    reasoning_effort: Literal["low", "medium", "high", "xhigh"] | None = None
    hf_model: HuggingFaceModel | None = None

    @model_validator(mode="after")
    def _mode_fields(self) -> ExecutionSettingsV1:
        if self.mode == "codex_subscription_transcript":
            if self.codex_model is None or self.hf_model is not None:
                raise ValueError(
                    "subscription mode requires codex_model, forbids hf_model, "
                    "and may include reasoning_effort"
                )
            validate_codex_model_ref(
                self.codex_model,
                field_name="subscription codex_model",
            )
        elif (
            self.hf_model is None
            or self.codex_model is not None
            or self.reasoning_effort is not None
        ):
            raise ValueError(
                "self-deployed mode requires only hf_model and no reasoning_effort"
            )
        return self


class ProjectTaskV1(StrictModel):
    title: CoreShortText
    objective: LongText


class WorkspaceImportRefV1(StrictModel):
    """Opaque native-to-sidecar handoff; it never contains a host path."""

    import_id: OpaqueId
    content_sha256: Digest
    byte_size: int = Field(ge=1_024, le=_core_contract.MAX_WORKSPACE_UPLOAD_BYTES)
    entry_count: int = Field(ge=0, le=_core_contract.MAX_WORKSPACE_ENTRIES)
    extracted_byte_size: int = Field(ge=0, le=_core_contract.MAX_WORKSPACE_UPLOAD_BYTES)

    @model_validator(mode="after")
    def _empty_archive_is_empty(self) -> WorkspaceImportRefV1:
        if self.byte_size % 512 != 0:
            raise ValueError("workspace import size must align to a tar block")
        if self.entry_count == 0 and self.extracted_byte_size != 0:
            raise ValueError("an empty import cannot declare extracted bytes")
        return self


class ProjectSourceV1(StrictModel):
    kind: Literal["scratch", "native_folder_snapshot"]
    display_name: CoreShortText
    import_ref: WorkspaceImportRefV1 | None = None

    @model_validator(mode="after")
    def _snapshot_required(self) -> ProjectSourceV1:
        if self.kind == "scratch" and self.import_ref is not None:
            raise ValueError("scratch sources must not include import_ref")
        if self.kind == "native_folder_snapshot" and self.import_ref is None:
            raise ValueError("native folder sources require an opaque import_ref")
        return self


class EvolutionTargetSelectionV1(StrictModel):
    enabled: bool
    method: EvolutionId | None = None
    config: BoundedJsonObjectV1 = Field(default_factory=lambda: BoundedJsonObjectV1({}))

    @model_validator(mode="after")
    def _enabled_method(self) -> EvolutionTargetSelectionV1:
        if self.enabled and self.method is None:
            raise ValueError("enabled evolution targets require a method")
        return self


class EvolutionSelectionsV1(RootModel[dict[EvolutionId, EvolutionTargetSelectionV1]]):
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
        return self


class EvolutionConfigV1(StrictModel):
    targets: EvolutionSelectionsV1

    @model_validator(mode="after")
    def _aggregate_budget(self) -> EvolutionConfigV1:
        encoded = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_PROJECT_EVOLUTION_BYTES:
            raise ValueError("evolution config exceeds the aggregate byte budget")
        return self


class ProjectCreateV1(StrictModel):
    name: ProjectDisplayName
    profile_id: OpaqueId
    task: ProjectTaskV1
    source: ProjectSourceV1
    execution: ExecutionSettingsV1
    evolution: EvolutionConfigV1
    evolution_configuration_state: Literal["pending", "configured"] = "configured"


class ProjectPatchV1(PatchModel):
    name: ProjectDisplayName | None = None
    profile_id: OpaqueId | None = None
    task: ProjectTaskV1 | None = None
    source: ProjectSourceV1 | None = None
    execution: ExecutionSettingsV1 | None = None
    evolution: EvolutionConfigV1 | None = None
    evolution_configuration_state: Literal["pending", "configured"] | None = None

    @model_validator(mode="after")
    def _not_empty(self) -> ProjectPatchV1:
        if not self.model_fields_set:
            raise ValueError("project patch must include at least one field")
        return self


class RemoteProjectStateV1(StrictModel):
    core_project_id: OpaqueId
    status: Literal["draft", "ready", "blocked", "archived"]
    active_revision: RevisionRefV1 | None = None
    registry_digest: Digest | None = None
    model_preparation: ModelPreparationV1
    observed_at: UtcTimestamp
    etag: ETag

    @model_validator(mode="after")
    def _ready_state_is_complete(self) -> RemoteProjectStateV1:
        if self.status == "ready" and (
            self.active_revision is None
            or self.registry_digest is None
            or self.model_preparation.status is not _core_contract.ModelPreparationStatus.READY
        ):
            raise ValueError(
                "ready remote projects require a revision, registry, and prepared model"
            )
        if (
            self.active_revision is not None
            and self.active_revision.project_id != self.core_project_id
        ):
            raise ValueError("remote project revision belongs to another project")
        return self


class ProjectV1(StrictModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    project_id: OpaqueId
    name: ShortText
    profile_id: OpaqueId
    task: ProjectTaskV1
    source: ProjectSourceV1
    execution: ExecutionSettingsV1
    evolution: EvolutionConfigV1
    evolution_configuration_state: Literal["pending", "configured"]
    state: Literal["draft", "active", "archived", "blocked"]
    remote: RemoteProjectStateV1 | None = None
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


class DesktopDiagnosticReportV1(StrictModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    diagnostic_id: OpaqueId
    status: Literal["healthy", "degraded", "blocked"]
    generated_at: UtcTimestamp
    checks: tuple[NormalizedCheckV1, ...]
    findings: tuple[DiagnosticFindingV1, ...]
    etag: ETag


class DiagnosticOperationResultV1(StrictModel):
    kind: Literal["diagnostic"] = "diagnostic"
    report: DesktopDiagnosticReportV1


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


class LocalLogEntryV1(StrictModel):
    log_id: OpaqueId
    occurred_at: UtcTimestamp
    level: Literal["debug", "info", "warning", "error"]
    source: Literal["desktop", "connection", "core", "run", "evolution", "service"]
    message: LongText
    code: ShortText | None = None


class CapabilitiesEnvelopeV1(StrictModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    project_id: OpaqueId
    project_etag: ETag
    source: Literal["verified_remote_core"] = "verified_remote_core"
    registry_verified: Literal[True] = True
    fetched_at: UtcTimestamp
    capabilities: EvolutionCapabilitiesV1


class ProjectValidationV1(StrictModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    project_id: OpaqueId
    project_etag: ETag
    registry_digest: Digest
    valid: bool
    checks: tuple[ValidationCheckV1, ...]
    validated_at: UtcTimestamp


class RunCreateV1(StrictModel):
    project_id: OpaqueId


class RunRetryV1(StrictModel):
    terminal_attempt_id: _core_contract.OpaqueId


ServiceV1 = ServiceSummaryV1
DiagnosticRequestV1 = DiagnosticsRequestV1
DiagnosticReportV1 = DiagnosticV1
TimelinePageV1 = RunTimelinePageV1


class StateEventV1(StrictModel):
    kind: Literal["state_changed"] = "state_changed"
    state: DesktopStateV1


class ResourceEventV1(StrictModel):
    kind: Literal["resource_changed"] = "resource_changed"
    authority: Literal["desktop", "core"]
    resource: ResourceRefV1
    change: Literal["created", "updated", "deleted", "appended"]
    change_id: OpaqueId
    resource_etag: ETag | None = None
    content_sha256: Digest | None = None

    @model_validator(mode="after")
    def _has_authoritative_identity(self) -> ResourceEventV1:
        if self.resource_etag is None and self.content_sha256 is None:
            raise ValueError("resource events require an authoritative ETag or digest")
        desktop_resources = {"profile", "project", "operation", "maintenance"}
        if self.authority == "desktop" and self.resource.resource_type not in desktop_resources:
            raise ValueError("Desktop authority cannot identify a Core-owned resource")
        if self.authority == "core" and self.resource.resource_type in {
            "profile",
            "project",
            "maintenance",
        }:
            raise ValueError("Core changes must use a mapped Desktop project resource")
        return self


class HeartbeatEventV1(StrictModel):
    kind: Literal["heartbeat"] = "heartbeat"


EventDataV1 = Annotated[
    StateEventV1 | ResourceEventV1 | HeartbeatEventV1,
    Field(discriminator="kind"),
]


EventNameV1 = Literal[
    "desktop.v1.state.changed",
    "desktop.v1.resource.changed",
    "desktop.v1.heartbeat",
]


_EVENT_NAMES_BY_KIND: dict[str, str] = {
    "state_changed": "desktop.v1.state.changed",
    "resource_changed": "desktop.v1.resource.changed",
    "heartbeat": "desktop.v1.heartbeat",
}


class EventEnvelopeV1(StrictModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    event_id: OpaqueId
    event_name: EventNameV1
    occurred_at: UtcTimestamp
    sequence: int = Field(ge=0, le=MAX_SAFE_INTEGER)
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
LocalLogPageV1 = PageV1[LocalLogEntryV1]


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
