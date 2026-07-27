from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Mapping

from pydantic import ValidationError

from desktop.sidecar.contracts.v1.models import (
    ExecutionModeCapabilitiesV1,
    ExecutionModeCapabilityV1,
)
from desktop.sidecar.contracts.v2.canonical import (
    DESKTOP_EVENTS_SCHEMA_SHA256,
    DESKTOP_OPENAPI_SHA256,
)
from desktop.sidecar.contracts.v2.models import DesktopVersionV2
from openevo.backend.contracts.v2.models import VersionResponseV2
from openevo.backend.contracts.v2.snapshots import (
    events_schema_sha256 as core_events_schema_sha256,
    openapi_sha256 as core_openapi_sha256,
)


RELEASE_EXECUTION_MODE_CAPABILITIES_V1 = ExecutionModeCapabilitiesV1(
    modes=(
        ExecutionModeCapabilityV1(
            mode="codex_subscription_transcript",
            display_name="Subscription",
            support_state="supported",
            message="Available in this OpenEvo Desktop release.",
        ),
        ExecutionModeCapabilityV1(
            mode="self-deployed",
            display_name="Self-deployed",
            support_state="unavailable",
            reason_code="self_deployed_release_unavailable",
            message=(
                "Self-deployed execution is not available in this OpenEvo Desktop release. "
                "Choose Subscription to save or run this project."
            ),
        ),
    )
)


class ReleaseAuthorityNegotiationError(RuntimeError):
    """The discovered or composed authority cannot mutate for v0.1.10."""


@dataclass(frozen=True, slots=True)
class ReleaseAuthorityPolicyV2:
    release_version: str
    desktop_mutation_api_major: int
    core_mutation_api_major: int
    desktop_openapi_sha256: str
    desktop_event_schema_sha256: str
    core_openapi_sha256: str
    core_event_schema_sha256: str
    allowed_provider_kinds: tuple[str, ...]
    forbidden_provider_kinds: tuple[str, ...]
    required_desktop_feature_flags: tuple[str, ...]
    required_core_feature_flags: tuple[str, ...]
    core_transport: str
    allow_direct_core_url: bool
    allow_legacy_route_fallback: bool
    require_registry_identity: bool


@dataclass(frozen=True, slots=True)
class NegotiatedMutationAuthorityV2:
    release_version: str
    source_commit: str
    desktop_build_id: str
    core_build_id: str
    desktop_openapi_sha256: str
    desktop_event_schema_sha256: str
    core_openapi_sha256: str
    core_event_schema_sha256: str
    registry_sha256: str
    runtime_contract_sha256: str


_V0110_POLICY_FIELDS = {
    "accepted_core_event_schema_digests",
    "accepted_core_openapi_digests",
    "accepted_desktop_event_schema_digests",
    "accepted_desktop_openapi_digests",
    "allow_direct_core_url",
    "allow_legacy_route_fallback",
    "allowed_provider_kinds",
    "core_control_mutation_major",
    "core_transport",
    "desktop_local_mutation_major",
    "forbidden_provider_kinds",
    "release_version",
    "require_registry_identity",
    "required_core_feature_flags",
    "required_desktop_feature_flags",
}


def _exact_string_tuple(value: object, *, field: str) -> tuple[str, ...]:
    if (
        type(value) is not list
        or not value
        or any(type(item) is not str or not item for item in value)
        or len(value) != len(set(value))
    ):
        raise RuntimeError(f"v0.1.10 release authority field {field} is invalid")
    result = tuple(value)
    if result != tuple(sorted(result)):
        raise RuntimeError(f"v0.1.10 release authority field {field} must be sorted")
    return result


def load_v0110_release_authority_policy(
    path: Path | None = None,
) -> ReleaseAuthorityPolicyV2:
    manifest_path = path or Path(__file__).resolve().parents[1] / "release-contract.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("v0.1.10 release authority manifest is unavailable") from exc
    policy = manifest.get("v0110") if type(manifest) is dict else None
    if type(policy) is not dict or set(policy) != _V0110_POLICY_FIELDS:
        raise RuntimeError("v0.1.10 release authority manifest is not closed")

    desktop_digests = _exact_string_tuple(
        policy["accepted_desktop_openapi_digests"],
        field="accepted_desktop_openapi_digests",
    )
    desktop_event_digests = _exact_string_tuple(
        policy["accepted_desktop_event_schema_digests"],
        field="accepted_desktop_event_schema_digests",
    )
    core_digests = _exact_string_tuple(
        policy["accepted_core_openapi_digests"],
        field="accepted_core_openapi_digests",
    )
    core_event_digests = _exact_string_tuple(
        policy["accepted_core_event_schema_digests"],
        field="accepted_core_event_schema_digests",
    )
    allowed_provider_kinds = _exact_string_tuple(
        policy["allowed_provider_kinds"], field="allowed_provider_kinds"
    )
    forbidden_provider_kinds = _exact_string_tuple(
        policy["forbidden_provider_kinds"], field="forbidden_provider_kinds"
    )
    desktop_features = _exact_string_tuple(
        policy["required_desktop_feature_flags"],
        field="required_desktop_feature_flags",
    )
    core_features = _exact_string_tuple(
        policy["required_core_feature_flags"], field="required_core_feature_flags"
    )
    expected_scalars = {
        "release_version": "0.1.10",
        "desktop_local_mutation_major": 2,
        "core_control_mutation_major": 2,
        "core_transport": "active_project_ssh_tunnel",
        "allow_direct_core_url": False,
        "allow_legacy_route_fallback": False,
        "require_registry_identity": True,
    }
    if any(policy.get(key) != value for key, value in expected_scalars.items()):
        raise RuntimeError("v0.1.10 release authority manifest weakens the release policy")
    if desktop_digests != (DESKTOP_OPENAPI_SHA256,):
        raise RuntimeError("v0.1.10 release authority has the wrong Desktop OpenAPI digest")
    if desktop_event_digests != (DESKTOP_EVENTS_SCHEMA_SHA256,):
        raise RuntimeError("v0.1.10 release authority has the wrong Desktop event digest")
    if core_digests != (core_openapi_sha256(),):
        raise RuntimeError("v0.1.10 release authority has the wrong Core OpenAPI digest")
    if core_event_digests != (core_events_schema_sha256(),):
        raise RuntimeError("v0.1.10 release authority has the wrong Core event digest")
    if allowed_provider_kinds != ("desktop_sidecar",):
        raise RuntimeError("v0.1.10 release authority requires the packaged Desktop sidecar")
    required_desktop_features = (
        "core_control_v2",
        "daemon_bundle_v2",
        "event_replay_v2",
        "host_key_review",
        "lifecycle_operations_v2",
        "lifecycle_process_logs_v2",
        "mutation_idempotency_v2",
        "native_askpass",
        "system_openssh_profiles",
        "task_admission_v2",
    )
    if desktop_features != required_desktop_features:
        raise RuntimeError(
            "v0.1.10 release authority has an incomplete Desktop feature set"
        )
    required_forbidden = {
        "contract_simulator",
        "direct_backend",
        "dry_run",
        "scaffold",
        "source_sidecar",
    }
    if set(forbidden_provider_kinds) != required_forbidden:
        raise RuntimeError("v0.1.10 release authority has an incomplete provider denylist")

    return ReleaseAuthorityPolicyV2(
        release_version="0.1.10",
        desktop_mutation_api_major=2,
        core_mutation_api_major=2,
        desktop_openapi_sha256=desktop_digests[0],
        desktop_event_schema_sha256=desktop_event_digests[0],
        core_openapi_sha256=core_digests[0],
        core_event_schema_sha256=core_event_digests[0],
        allowed_provider_kinds=allowed_provider_kinds,
        forbidden_provider_kinds=forbidden_provider_kinds,
        required_desktop_feature_flags=desktop_features,
        required_core_feature_flags=core_features,
        core_transport="active_project_ssh_tunnel",
        allow_direct_core_url=False,
        allow_legacy_route_fallback=False,
        require_registry_identity=True,
    )


V0110_RELEASE_AUTHORITY_POLICY = load_v0110_release_authority_policy()


def negotiate_desktop_v2_mutation(
    payload: Mapping[str, object],
    *,
    policy: ReleaseAuthorityPolicyV2 = V0110_RELEASE_AUTHORITY_POLICY,
) -> DesktopVersionV2:
    try:
        version = DesktopVersionV2.model_validate(dict(payload))
    except ValidationError as exc:
        raise ReleaseAuthorityNegotiationError(
            "Desktop v2 discovery is invalid or is not a release sidecar."
        ) from exc
    if version.release_version != policy.release_version or version.build_channel != "release":
        raise ReleaseAuthorityNegotiationError("Desktop release identity is incompatible.")
    if version.openapi_sha256 != policy.desktop_openapi_sha256:
        raise ReleaseAuthorityNegotiationError("Desktop OpenAPI digest is incompatible.")
    if version.event_schema_sha256 != policy.desktop_event_schema_sha256:
        raise ReleaseAuthorityNegotiationError("Desktop event schema digest is incompatible.")
    if tuple(version.feature_flags) != policy.required_desktop_feature_flags:
        raise ReleaseAuthorityNegotiationError("Desktop feature set is incompatible.")
    if not version.mutation_compatible:
        raise ReleaseAuthorityNegotiationError("Desktop v2 is not mutation-compatible.")
    return version


def negotiate_core_v2_mutation(
    payload: Mapping[str, object],
    *,
    policy: ReleaseAuthorityPolicyV2 = V0110_RELEASE_AUTHORITY_POLICY,
) -> VersionResponseV2:
    registry_identity = payload.get("registry_sha256")
    if payload.get("schema_version") == "2" and policy.require_registry_identity and (
        type(registry_identity) is not str
        or len(registry_identity) != 64
        or any(character not in "0123456789abcdef" for character in registry_identity)
    ):
        raise ReleaseAuthorityNegotiationError("Core registry identity is unavailable.")
    try:
        version = VersionResponseV2.model_validate(dict(payload))
    except ValidationError as exc:
        raise ReleaseAuthorityNegotiationError(
            "Core v2 discovery is invalid or lacks verified authority."
        ) from exc
    if version.release_version != policy.release_version or version.build_channel != "release":
        raise ReleaseAuthorityNegotiationError("Core release identity is incompatible.")
    v2_offer = next(
        (offer for offer in version.contracts if offer.api_major == policy.core_mutation_api_major),
        None,
    )
    if v2_offer is None or not v2_offer.mutation_compatible:
        raise ReleaseAuthorityNegotiationError("Core v2 is not mutation-compatible.")
    if v2_offer.openapi_sha256 != policy.core_openapi_sha256:
        raise ReleaseAuthorityNegotiationError("Core OpenAPI digest is incompatible.")
    if v2_offer.event_schema_sha256 != policy.core_event_schema_sha256:
        raise ReleaseAuthorityNegotiationError("Core event schema digest is incompatible.")
    if tuple(version.feature_flags) != policy.required_core_feature_flags:
        raise ReleaseAuthorityNegotiationError("Core feature set is incompatible.")
    if not version.mutation_compatible:
        raise ReleaseAuthorityNegotiationError("Core v2 is not mutation-compatible.")
    return version


def validate_v0110_release_composition(
    *,
    provider_kind: object,
    local_api_major: object,
    core_transport: object,
    allow_direct_core_url: object,
    allow_legacy_route_fallback: object,
    policy: ReleaseAuthorityPolicyV2 = V0110_RELEASE_AUTHORITY_POLICY,
) -> None:
    if provider_kind not in policy.allowed_provider_kinds:
        raise ReleaseAuthorityNegotiationError("Release provider kind is forbidden.")
    if provider_kind in policy.forbidden_provider_kinds:
        raise ReleaseAuthorityNegotiationError("Release provider kind is forbidden.")
    if local_api_major != policy.desktop_mutation_api_major:
        raise ReleaseAuthorityNegotiationError("The 0.1.10 release requires Local API v2.")
    if core_transport != policy.core_transport:
        raise ReleaseAuthorityNegotiationError("Core must use the active project SSH tunnel.")
    if allow_direct_core_url is not False or policy.allow_direct_core_url:
        raise ReleaseAuthorityNegotiationError("Direct Core URLs are forbidden in release.")
    if allow_legacy_route_fallback is not False or policy.allow_legacy_route_fallback:
        raise ReleaseAuthorityNegotiationError("Legacy route fallback is forbidden in release.")


def negotiate_v0110_mutation_authority(
    desktop_payload: Mapping[str, object],
    core_payload: Mapping[str, object],
    *,
    policy: ReleaseAuthorityPolicyV2 = V0110_RELEASE_AUTHORITY_POLICY,
) -> NegotiatedMutationAuthorityV2:
    desktop = negotiate_desktop_v2_mutation(desktop_payload, policy=policy)
    core = negotiate_core_v2_mutation(core_payload, policy=policy)
    if desktop.release_version != core.release_version:
        raise ReleaseAuthorityNegotiationError("Desktop and Core release identities differ.")
    if desktop.source_commit != core.source_commit:
        raise ReleaseAuthorityNegotiationError("Desktop and Core source identities differ.")
    core_offer = next(
        offer for offer in core.contracts if offer.api_major == policy.core_mutation_api_major
    )
    return NegotiatedMutationAuthorityV2(
        release_version=desktop.release_version,
        source_commit=desktop.source_commit,
        desktop_build_id=desktop.build_id,
        core_build_id=core.build_id,
        desktop_openapi_sha256=desktop.openapi_sha256,
        desktop_event_schema_sha256=desktop.event_schema_sha256,
        core_openapi_sha256=core_offer.openapi_sha256,
        core_event_schema_sha256=core_offer.event_schema_sha256,
        registry_sha256=core.registry_sha256,
        runtime_contract_sha256=core.runtime_contract_sha256,
    )


__all__ = (
    "RELEASE_EXECUTION_MODE_CAPABILITIES_V1",
    "NegotiatedMutationAuthorityV2",
    "ReleaseAuthorityNegotiationError",
    "ReleaseAuthorityPolicyV2",
    "V0110_RELEASE_AUTHORITY_POLICY",
    "load_v0110_release_authority_policy",
    "negotiate_core_v2_mutation",
    "negotiate_desktop_v2_mutation",
    "negotiate_v0110_mutation_authority",
    "validate_v0110_release_composition",
)
