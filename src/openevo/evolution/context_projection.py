"""Internal handler-driven context projection before runtime materialization."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, field_validator, model_validator

from openevo.evolution.artifact_payloads import (
    ArtifactPayloadBudgetExceeded,
    ArtifactPayloadService,
)
from openevo.evolution.context import (
    ContextCompatibilityRequest,
    artifact_matches,
    artifact_scores,
    request_auth_mode,
    requested_context_artifact_ids,
    sort_candidates,
)
from openevo.evolution.framework.builtins import (
    VerifiedExecutableRegistry,
    require_verified_executable_registry,
)
from openevo.evolution.framework.contracts import (
    ContributionKind,
    EvolutionExecutionProfile,
    ExecutionMode,
    MAX_CONTRIBUTION_TEXT,
    MAX_HANDLER_ARTIFACTS,
    MAX_JAVASCRIPT_SAFE_INTEGER,
    MAX_CONTRACT_JSON_BYTES,
    MAX_RENDERER_PAYLOAD_BYTES,
    _Contract,
    _bounded_canonical_json_object,
    canonical_digest,
    canonical_json,
    _digest,
    _stable_id,
    _text,
)
from openevo.evolution.framework.contributions import TargetHandlerOutput
from openevo.evolution.framework.handlers import (
    RuntimeDestinationRoots,
    TargetConsumptionLimits,
    TargetHandlerInput,
    TargetHandlerServices,
)


MAX_CONTEXT_PROJECTION_CANDIDATES = 4096
MAX_CONTEXT_CANDIDATES_PER_TARGET = MAX_HANDLER_ARTIFACTS * 2
MAX_ARTIFACT_ROUTING_JSON_BYTES = 16_384
MAX_CONTEXT_ARTIFACT_NAME_BYTES = 16_384
MAX_CONTEXT_ARTIFACT_URI_BYTES = 8_192
MAX_CONTEXT_PROJECTION_REQUEST_BYTES = MAX_RENDERER_PAYLOAD_BYTES + MAX_CONTRACT_JSON_BYTES
MAX_CONTEXT_ARTIFACT_ID_LENGTH = 256
MAX_CONTEXT_TASK_TAG_LENGTH = 4096


class _UnboundLegacyArtifact(ValueError):
    pass


class _ProjectionMetadataRejected(ValueError):
    pass


def _bounded_text(value: str, *, maximum: int, label: str) -> str:
    normalized = _text(value)
    if len(normalized) > maximum:
        raise ValueError(f"{label} exceeds the length limit")
    return normalized


def _projection_uses_subscription_auth(
    request: ContextCompatibilityRequest,
) -> bool:
    auth_mode = request_auth_mode(request)
    return auth_mode == "subscription" or bool(auth_mode and auth_mode.endswith("_subscription"))


def _registered_artifact_manifest(row: Mapping[str, object]) -> dict[str, object]:
    encoded = row.get("manifest_json")
    if not isinstance(encoded, str) or not encoded:
        raise _UnboundLegacyArtifact("artifact lacks immutable registered manifest metadata")
    if len(encoded.encode("utf-8")) > MAX_CONTRACT_JSON_BYTES:
        raise _ProjectionMetadataRejected(
            "registered artifact manifest exceeds the projection limit"
        )
    try:
        payload = json.loads(encoded)
    except ValueError as exc:
        raise ValueError("registered artifact manifest is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("registered artifact manifest must be a JSON object")
    stable = json.dumps(payload, sort_keys=True, allow_nan=False)
    if stable != encoded:
        raise ValueError("registered artifact manifest must match its registration binding")
    try:
        _bounded_canonical_json_object(
            payload,
            label="registered artifact manifest",
        )
    except (TypeError, ValueError) as exc:
        raise _ProjectionMetadataRejected(
            "registered artifact manifest is outside projection policy"
        ) from exc
    return payload


def _registered_artifact_json_object(
    encoded: object,
    *,
    label: str,
    max_bytes: int = MAX_CONTRACT_JSON_BYTES,
) -> tuple[dict[str, object], str]:
    if not isinstance(encoded, str) or not encoded:
        raise ValueError(f"{label} must be a non-empty JSON object")
    if len(encoded.encode("utf-8")) > max_bytes:
        raise ValueError(f"{label} exceeds the contract limit")
    try:
        payload = json.loads(encoded)
    except ValueError as exc:
        raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    canonical = _bounded_canonical_json_object(
        payload,
        label=label,
        max_bytes=max_bytes,
    )
    return payload, canonical


def _validated_compatibility_row(row: Mapping[str, object]) -> dict[str, object]:
    candidate = dict(row)
    compatibility, _ = _registered_artifact_json_object(
        candidate.get("compatibility_json"),
        label="registered artifact compatibility",
        max_bytes=MAX_ARTIFACT_ROUTING_JSON_BYTES,
    )
    candidate["compatibility_json"] = compatibility
    return candidate


def _validated_candidate_row(row: Mapping[str, object]) -> dict[str, object]:
    candidate = dict(row)
    if not isinstance(candidate.get("compatibility_json"), dict):
        candidate = _validated_compatibility_row(candidate)
    scores, _ = _registered_artifact_json_object(
        candidate.get("scores_json"),
        label="registered artifact scores",
        max_bytes=MAX_ARTIFACT_ROUTING_JSON_BYTES,
    )
    candidate["scores_json"] = scores
    return candidate


class _ProjectionContract(_Contract):
    model_config = ConfigDict(strict=True)


def _immutable_limits(*_args: object, **_kwargs: object) -> None:
    raise TypeError("context target limits are immutable")


class _FrozenTargetLimits(dict[str, TargetConsumptionLimits]):
    __setitem__ = _immutable_limits
    __delitem__ = _immutable_limits
    clear = _immutable_limits
    pop = _immutable_limits
    popitem = _immutable_limits
    setdefault = _immutable_limits
    update = _immutable_limits
    __ior__ = _immutable_limits


class ContextProjectionAgentSettings(_ProjectionContract):
    auth_mode: str | None = None

    @field_validator("auth_mode")
    @classmethod
    def _optional_auth_mode(cls, value: str | None) -> str | None:
        return None if value is None else _stable_id(value)


class ContextProjectionAgent(_ProjectionContract):
    harness: str
    settings: ContextProjectionAgentSettings = Field(
        default_factory=ContextProjectionAgentSettings
    )

    _harness = field_validator("harness")(_stable_id)


class ContextProjectionEvolutionMetadata(_ProjectionContract):
    context_artifact_ids: tuple[str, ...] | None = Field(
        default=None,
        max_length=128,
    )

    @field_validator("context_artifact_ids", mode="before")
    @classmethod
    def _json_array(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("context_artifact_ids")
    @classmethod
    def _bounded_artifacts(
        cls,
        values: tuple[str, ...] | None,
    ) -> tuple[str, ...] | None:
        if values is None:
            return None
        return tuple(
            _bounded_text(
                value,
                maximum=MAX_CONTEXT_ARTIFACT_ID_LENGTH,
                label="context artifact ID",
            )
            for value in values
        )


class ContextProjectionMetadata(_ProjectionContract):
    task_tags: tuple[str, ...] = Field(default=(), max_length=256)
    evolution: ContextProjectionEvolutionMetadata | None = None

    @field_validator("task_tags", mode="before")
    @classmethod
    def _json_array(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("task_tags")
    @classmethod
    def _bounded_tags(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            _bounded_text(
                value,
                maximum=MAX_CONTEXT_TASK_TAG_LENGTH,
                label="task tag",
            )
            for value in values
        )


@dataclass(frozen=True, slots=True)
class _CompatibilityFacts:
    agent: dict[str, Any]
    base_model: str | None
    metadata: dict[str, Any]


class ContextProjectionResolveRequest(_ProjectionContract):
    """Trusted Core-to-Core facts used to invoke context target handlers."""

    projection_contract_version: Literal["1"] = "1"
    task_id: str = Field(min_length=1, max_length=4096)
    instruction: str = Field(min_length=1, max_length=MAX_CONTRIBUTION_TEXT)
    successor_transition_id: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    predecessor_project_head_id: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    agent: ContextProjectionAgent
    base_model: str | None = Field(default=None, max_length=4096)
    policy_version: str | None = Field(default=None, max_length=4096)
    rollout_step: int | None = Field(
        default=None,
        ge=0,
        le=MAX_JAVASCRIPT_SAFE_INTEGER,
    )
    metadata: ContextProjectionMetadata = Field(default_factory=ContextProjectionMetadata)
    execution_profile: EvolutionExecutionProfile
    destination_roots: RuntimeDestinationRoots
    target_limits: dict[str, TargetConsumptionLimits] = Field(
        default_factory=dict,
        max_length=128,
    )

    _text_fields = field_validator("task_id", "instruction")(_text)

    @field_validator("successor_transition_id", "predecessor_project_head_id")
    @classmethod
    def _optional_successor_identity(cls, value: str | None) -> str | None:
        return None if value is None else _stable_id(value)

    @field_validator("base_model", "policy_version")
    @classmethod
    def _optional_text(cls, value: str | None) -> str | None:
        return None if value is None else _text(value)

    @field_validator("execution_profile", mode="before")
    @classmethod
    def _strict_execution_profile(cls, value: object) -> object:
        if isinstance(value, EvolutionExecutionProfile):
            return value
        if not isinstance(value, dict):
            raise ValueError("execution_profile must be an object")
        for field in ("execution_mode", "capture_mode", "harness_id"):
            if field in value and not isinstance(value[field], str):
                raise ValueError(f"execution_profile.{field} must be text")
        for field in ("harness_capabilities", "runtime_capabilities"):
            items = value.get(field, ())
            if not isinstance(items, list | tuple) or any(
                not isinstance(item, str) for item in items
            ):
                raise ValueError(f"execution_profile.{field} must be a text array")
        return value

    @field_validator("destination_roots", mode="before")
    @classmethod
    def _strict_destination_roots(cls, value: object) -> object:
        if isinstance(value, RuntimeDestinationRoots):
            return value
        if not isinstance(value, dict):
            raise ValueError("destination_roots must be an object")
        if any(not isinstance(item, str) for item in value.values()):
            raise ValueError("destination roots must be text")
        return value

    @field_validator("target_limits", mode="before")
    @classmethod
    def _strict_limit_values(cls, value: object) -> object:
        if not isinstance(value, dict):
            raise ValueError("target_limits must be an object")
        numeric_fields = {
            "max_artifacts",
            "max_text_chars",
            "max_text_bytes",
            "max_payload_bytes",
            "max_adapters",
        }
        for limits in value.values():
            if isinstance(limits, TargetConsumptionLimits):
                continue
            if not isinstance(limits, dict):
                raise ValueError("target limit values must be objects")
            for field in numeric_fields.intersection(limits):
                item = limits[field]
                if not isinstance(item, int) or isinstance(item, bool):
                    raise ValueError(f"target limit {field} must be an integer")
        return value

    @field_validator("target_limits")
    @classmethod
    def _canonical_target_limits(
        cls,
        value: dict[str, TargetConsumptionLimits],
    ) -> dict[str, TargetConsumptionLimits]:
        normalized = {_stable_id(key): item for key, item in value.items()}
        return _FrozenTargetLimits(sorted(normalized.items()))

    @model_validator(mode="after")
    def _bounded_canonical_request(self) -> ContextProjectionResolveRequest:
        if (self.successor_transition_id is None) != (
            self.predecessor_project_head_id is None
        ):
            raise ValueError(
                "successor transition and predecessor project head must be provided together"
            )
        encoded = canonical_json(self.model_dump(mode="json"))
        if len(encoded.encode("utf-8")) > MAX_CONTEXT_PROJECTION_REQUEST_BYTES:
            raise ValueError("context projection request exceeds the byte budget")
        return self

    def compatibility_facts(self) -> _CompatibilityFacts:
        return _CompatibilityFacts(
            agent=self.agent.model_dump(mode="python", exclude_none=True),
            base_model=self.base_model,
            metadata=self.metadata.model_dump(mode="python", exclude_none=True),
        )


class ContextProjectionSkippedArtifact(_ProjectionContract):
    artifact_id: str = Field(max_length=256)
    reason: Literal[
        "unsupported_uri_scheme",
        "payload_policy_rejected",
        "metadata_policy_rejected",
        "unbound_legacy_metadata",
    ]

    _artifact = field_validator("artifact_id")(_text)


class ContextProjectionSelection(_ProjectionContract):
    artifact_ids: tuple[str, ...] = Field(
        default=(),
        max_length=MAX_CONTEXT_PROJECTION_CANDIDATES,
    )
    skipped_artifacts: tuple[ContextProjectionSkippedArtifact, ...] = Field(
        default=(),
        max_length=MAX_CONTEXT_PROJECTION_CANDIDATES,
    )
    reasons: tuple[str, ...] = Field(min_length=1, max_length=16)

    _artifact_ids = field_validator("artifact_ids")(
        lambda values: tuple(_text(value) for value in values)
    )
    _reasons = field_validator("reasons")(lambda values: tuple(_text(value) for value in values))

    @model_validator(mode="after")
    def _unique_artifacts(self) -> ContextProjectionSelection:
        if len(self.artifact_ids) != len(set(self.artifact_ids)):
            raise ValueError("selected artifact IDs must be unique")
        skipped_ids = self.skipped_artifact_ids
        if len(skipped_ids) != len(set(skipped_ids)):
            raise ValueError("skipped artifact IDs must be unique")
        if set(self.artifact_ids).intersection(skipped_ids):
            raise ValueError("selected and skipped artifact IDs must not overlap")
        return self

    @property
    def skipped_artifact_ids(self) -> tuple[str, ...]:
        return tuple(item.artifact_id for item in self.skipped_artifacts)


class ContextProjectionResolveResponse(_ProjectionContract):
    projection_contract_version: Literal["1"] = "1"
    context_id: str = Field(max_length=256)
    request_digest: str
    registry_digest: str
    base_model: str | None = None
    destination_roots: RuntimeDestinationRoots
    projections: tuple[TargetHandlerOutput, ...] = Field(
        default=(),
        max_length=128,
    )
    selection: ContextProjectionSelection

    _context = field_validator("context_id")(_stable_id)
    _digests = field_validator("request_digest", "registry_digest")(_digest)

    @field_validator("base_model")
    @classmethod
    def _optional_text(cls, value: str | None) -> str | None:
        return None if value is None else _text(value)

    @model_validator(mode="after")
    def _projection_selection(self) -> ContextProjectionResolveResponse:
        target_ids = tuple(item.target_id for item in self.projections)
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("context projections must have unique target IDs")
        consumed_values = tuple(
            artifact_id
            for projection in self.projections
            for artifact_id in projection.artifact_ids
        )
        if set(consumed_values) != set(self.selection.artifact_ids):
            raise ValueError("context selection must match projection artifacts")
        return self


class ContextProjectionResolver:
    """Invoke only attested target handlers and return validated data projections."""

    def __init__(
        self,
        artifact_root: str | os.PathLike[str],
        executable_registry: VerifiedExecutableRegistry,
    ) -> None:
        self._registry = require_verified_executable_registry(executable_registry)
        self._artifact_root = artifact_root

    def resolve(
        self,
        request: ContextProjectionResolveRequest,
        promoted_rows: Sequence[Mapping[str, object]],
        *,
        context_id: str,
    ) -> ContextProjectionResolveResponse:
        snapshot = self._registry.snapshot
        unknown_limits = set(request.target_limits).difference(snapshot.targets)
        if unknown_limits:
            raise ValueError("context projection limits reference unknown targets")

        compatibility_facts = request.compatibility_facts()
        auth_mode = request_auth_mode(compatibility_facts)
        if auth_mode is not None:
            declared_subscription = _projection_uses_subscription_auth(compatibility_facts)
            if declared_subscription != (
                request.execution_profile.execution_mode is ExecutionMode.SUBSCRIPTION
            ):
                raise ValueError("context execution profile does not match agent auth mode")
        agent_harness = request.agent.harness
        if agent_harness != request.execution_profile.harness_id:
            raise ValueError("context execution profile does not match agent harness")
        requested_ids = requested_context_artifact_ids(compatibility_facts)
        compatible_rows: list[dict[str, object]] = []
        pre_skipped_rows: list[dict[str, object]] = []
        pre_skipped_reasons: dict[str, str] = {}
        for row in promoted_rows:
            if row.get("promoted") not in (True, 1) or str(row.get("state") or "") not in {
                "active",
                "experimental",
            }:
                continue
            if (
                requested_ids is not None
                and str(row.get("artifact_id") or "") not in requested_ids
            ):
                continue
            try:
                compatibility_row = _validated_compatibility_row(row)
            except (TypeError, ValueError):
                # Compatibility must be established before an artifact identity
                # can enter this context, even as a typed skip.
                continue
            if not artifact_matches(compatibility_facts, compatibility_row):
                continue
            projected_skip_reason = row.get("projection_skip_reason")
            if projected_skip_reason is not None:
                if projected_skip_reason not in {
                    "unsupported_uri_scheme",
                    "metadata_policy_rejected",
                    "unbound_legacy_metadata",
                }:
                    raise ValueError("store returned an invalid projection skip reason")
                skipped_row = compatibility_row
                artifact_id = str(row.get("artifact_id") or "")
                pre_skipped_rows.append(skipped_row)
                pre_skipped_reasons[artifact_id] = str(projected_skip_reason)
                continue
            if not isinstance(row.get("manifest_json"), str) or not row.get("manifest_json"):
                skipped_row = compatibility_row
                artifact_id = str(row.get("artifact_id") or "")
                pre_skipped_rows.append(skipped_row)
                pre_skipped_reasons[artifact_id] = "unbound_legacy_metadata"
                continue
            try:
                candidate = _validated_candidate_row(compatibility_row)
            except (TypeError, ValueError):
                skipped_row = compatibility_row
                artifact_id = str(row.get("artifact_id") or "")
                pre_skipped_rows.append(skipped_row)
                pre_skipped_reasons[artifact_id] = "metadata_policy_rejected"
                continue
            compatible_rows.append(candidate)
        ranked_rows = sort_candidates(compatible_rows)
        pairs: list[tuple[TargetHandlerInput, TargetHandlerOutput]] = []
        skipped_reasons = dict(pre_skipped_reasons)
        targets = sorted(
            snapshot.targets.values(),
            key=lambda item: (item.context_order, item.id),
        )

        with ArtifactPayloadService(self._artifact_root) as payloads:
            services = TargetHandlerServices(payloads=payloads)
            for target in targets:
                handler_descriptor = snapshot.target_handlers[target.handler_id]
                contribution_kinds = set(handler_descriptor.allowed_contribution_kinds)
                if (
                    request.execution_profile.execution_mode is ExecutionMode.SUBSCRIPTION
                    and ContributionKind.ADAPTER in contribution_kinds
                    and not contribution_kinds.difference(
                        {
                            ContributionKind.ADAPTER,
                            ContributionKind.ENVIRONMENT,
                        }
                    )
                ):
                    continue
                limits = request.target_limits.get(
                    target.id,
                    TargetConsumptionLimits(),
                )
                if limits.max_artifacts == 0:
                    continue
                candidate_rows = [
                    row
                    for row in ranked_rows
                    if str(row.get("type") or "") == target.artifact_type
                ]
                trusted_artifacts = []
                payload_attempts = 0
                for row in candidate_rows:
                    if len(trusted_artifacts) >= MAX_HANDLER_ARTIFACTS:
                        break
                    artifact_id = str(row.get("artifact_id") or "")
                    try:
                        uri_scheme = urlsplit(str(row.get("uri") or "")).scheme
                    except ValueError:
                        skipped_reasons[artifact_id] = "payload_policy_rejected"
                        continue
                    if uri_scheme != "file":
                        skipped_reasons[artifact_id] = "unsupported_uri_scheme"
                        continue
                    try:
                        manifest = _registered_artifact_manifest(row)
                    except _UnboundLegacyArtifact:
                        skipped_reasons[artifact_id] = "unbound_legacy_metadata"
                        continue
                    except _ProjectionMetadataRejected:
                        skipped_reasons[artifact_id] = "metadata_policy_rejected"
                        continue
                    payload_attempts += 1
                    if payload_attempts > MAX_CONTEXT_CANDIDATES_PER_TARGET:
                        raise ValueError("context target exceeds the payload attempt budget")
                    try:
                        trusted_artifacts.append(
                            payloads.issue_snapshot(
                                artifact_id=artifact_id,
                                artifact_type=str(row.get("type") or ""),
                                name=str(row.get("name") or artifact_id),
                                uri=str(row.get("uri") or ""),
                                manifest=manifest,
                                scores=artifact_scores(row),
                                rank_index=len(trusted_artifacts),
                            )
                        )
                    except ArtifactPayloadBudgetExceeded:
                        raise
                    except ValueError:
                        skipped_reasons[artifact_id] = "payload_policy_rejected"
                if not trusted_artifacts:
                    continue
                handler_input = TargetHandlerInput(
                    target_id=target.id,
                    handler_id=target.handler_id,
                    execution_profile=request.execution_profile,
                    destination_roots=request.destination_roots,
                    base_model=request.base_model,
                    limits=limits,
                    ranked_artifacts=tuple(trusted_artifacts),
                )
                handler = self._registry.handler_handles[target.handler_id]
                output = snapshot.validate_handler_output(
                    handler(handler_input, services),
                    handler_input=handler_input,
                )
                pairs.append((handler_input, output))

            projections = snapshot.validate_handler_outputs(pairs)

        consumed_ids = {
            artifact_id for projection in projections for artifact_id in projection.artifact_ids
        }
        selection_ids = tuple(
            str(row["artifact_id"])
            for row in ranked_rows
            if str(row["artifact_id"]) in consumed_ids
        )
        skipped_set = set(skipped_reasons).difference(consumed_ids)
        ordered_response_rows = [*ranked_rows, *pre_skipped_rows]
        skipped = tuple(
            ContextProjectionSkippedArtifact(
                artifact_id=str(row["artifact_id"]),
                reason=skipped_reasons[str(row["artifact_id"])],
            )
            for row in ordered_response_rows
            if str(row["artifact_id"]) in skipped_set
        )
        return ContextProjectionResolveResponse(
            context_id=context_id,
            request_digest=canonical_digest(request),
            registry_digest=snapshot.registry_digest,
            base_model=request.base_model,
            destination_roots=request.destination_roots,
            projections=projections,
            selection=ContextProjectionSelection(
                artifact_ids=selection_ids,
                skipped_artifacts=skipped,
                reasons=(
                    "matched requested promoted compatible artifacts"
                    if requested_ids is not None
                    else "matched promoted compatible artifacts",
                ),
            ),
        )


__all__ = [
    "MAX_CONTEXT_PROJECTION_REQUEST_BYTES",
    "ContextProjectionAgent",
    "ContextProjectionAgentSettings",
    "ContextProjectionEvolutionMetadata",
    "ContextProjectionMetadata",
    "ContextProjectionResolveRequest",
    "ContextProjectionResolveResponse",
    "ContextProjectionResolver",
    "ContextProjectionSelection",
    "ContextProjectionSkippedArtifact",
]
