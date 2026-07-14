"""Versioned Desktop/maintainer projection of one frozen framework registry."""

from __future__ import annotations

import json
import math
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field, field_validator, model_validator

from .contracts import (
    CaptureMode,
    EvolutionExecutionProfile,
    ExecutionMode,
    Exposure,
    MAX_JAVASCRIPT_SAFE_INTEGER,
    MAX_CONTRACT_JSON_BYTES,
    Maturity,
    RendererKind,
    _Contract,
    _digest,
    _optional_stable_id,
    _stable_id,
    _text,
    canonical_json,
)
from .execution import MethodInputBinding
from .schema import normalize_partial_config, validate_config_schema
from .support import MethodSupport, MethodSupportOverall, evaluate_method_support

if TYPE_CHECKING:
    from .descriptors import EvolutionMethodDescriptor
    from .registry import RegistrySnapshot


_MAX_CAPABILITY_JSON_DEPTH = 16
_MAX_CAPABILITY_JSON_NODES = 8192
_MAX_CAPABILITY_TARGETS = 128
_MAX_CAPABILITY_METHODS = 256
_MAX_CAPABILITY_RESOLVERS = 64
_MAX_CAPABILITY_PAYLOAD_NODES = 131_072
_MAX_CAPABILITY_COLLECTION_ITEMS = 65_536
_MAX_CAPABILITY_SINGLE_COLLECTION = 4_096
_MAX_CAPABILITY_TEXT_CHARACTERS = 4 * 1024 * 1024


def _validate_capability_payload_budget(value: object) -> None:
    stack = [value]
    nodes = 0
    collection_items = 0
    text_characters = 0
    while stack:
        current = stack.pop()
        nodes += 1
        if nodes > _MAX_CAPABILITY_PAYLOAD_NODES:
            raise ValueError("capability payload exceeds the node budget")
        if isinstance(current, bool) or current is None:
            continue
        if isinstance(current, str):
            text_characters += len(current)
            if text_characters > _MAX_CAPABILITY_TEXT_CHARACTERS:
                raise ValueError("capability payload exceeds the text budget")
            continue
        if isinstance(current, int):
            if abs(current) > MAX_JAVASCRIPT_SAFE_INTEGER:
                raise ValueError(
                    "capability payload integer exceeds the JavaScript safe range"
                )
            continue
        if isinstance(current, float):
            if (
                math.isfinite(current)
                and current.is_integer()
                and abs(current) > MAX_JAVASCRIPT_SAFE_INTEGER
            ):
                raise ValueError(
                    "capability payload integer exceeds the JavaScript safe range"
                )
            continue
        if isinstance(current, dict):
            size = len(current)
            if size > _MAX_CAPABILITY_SINGLE_COLLECTION:
                raise ValueError("capability payload collection is too large")
            collection_items += size
            if collection_items > _MAX_CAPABILITY_COLLECTION_ITEMS:
                raise ValueError("capability payload exceeds the collection budget")
            for key, item in current.items():
                if isinstance(key, str):
                    text_characters += len(key)
                stack.append(item)
            if text_characters > _MAX_CAPABILITY_TEXT_CHARACTERS:
                raise ValueError("capability payload exceeds the text budget")
            continue
        if isinstance(current, list | tuple):
            size = len(current)
            if size > _MAX_CAPABILITY_SINGLE_COLLECTION:
                raise ValueError("capability payload collection is too large")
            collection_items += size
            if collection_items > _MAX_CAPABILITY_COLLECTION_ITEMS:
                raise ValueError("capability payload exceeds the collection budget")
            stack.extend(current)


def _validate_desktop_safe_integers(value: object, path: str) -> None:
    stack: list[tuple[object, str, int]] = [(value, path, 1)]
    nodes = 0
    while stack:
        current, current_path, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_CAPABILITY_JSON_NODES:
            raise ValueError(f"{path} exceeds the capability JSON node limit")
        if depth > _MAX_CAPABILITY_JSON_DEPTH:
            raise ValueError(f"{path} exceeds the capability JSON depth limit")
        if isinstance(current, bool) or current is None or isinstance(current, str):
            continue
        if isinstance(current, int):
            if abs(current) > MAX_JAVASCRIPT_SAFE_INTEGER:
                raise ValueError(
                    f"{current_path} exceeds the JavaScript safe integer range"
                )
            continue
        if isinstance(current, float):
            if (
                math.isfinite(current)
                and current.is_integer()
                and abs(current) > MAX_JAVASCRIPT_SAFE_INTEGER
            ):
                raise ValueError(
                    f"{current_path} exceeds the JavaScript safe integer range"
                )
            continue
        if isinstance(current, list):
            stack.extend(
                (item, f"{current_path}[{index}]", depth + 1)
                for index, item in enumerate(current)
            )
            continue
        if isinstance(current, dict):
            stack.extend(
                (item, f"{current_path}.{key}", depth + 1)
                for key, item in current.items()
            )


class CapabilityAudience(StrEnum):
    DESKTOP = "desktop"
    MAINTAINER = "maintainer"
    INTERNAL = "internal"


class EvolutionMethodCapabilityV1(_Contract):
    method_id: str
    display_name: str = Field(max_length=4096)
    description: str = Field(max_length=4096)
    exposure: Exposure
    maturity: Maturity
    execution_modes: tuple[ExecutionMode, ...] = Field(max_length=len(ExecutionMode))
    capture_modes: tuple[CaptureMode, ...] = Field(max_length=len(CaptureMode))
    supported_harness_ids: tuple[str, ...] = Field(max_length=256)
    harness_requirements: tuple[str, ...] = Field(max_length=256)
    runtime_requirements: tuple[str, ...] = Field(max_length=256)
    input_bindings: tuple[MethodInputBinding, ...] = Field(max_length=256)
    output_artifact_types: tuple[str, ...] = Field(max_length=256)
    config_schema_json: str
    default_config_json: str
    implementation_identity_digest: str
    support: MethodSupport

    _id = field_validator("method_id")(_stable_id)
    _text_fields = field_validator("display_name", "description")(_text)
    _digest = field_validator("implementation_identity_digest")(_digest)

    @model_validator(mode="after")
    def _canonical_config_json(self) -> EvolutionMethodCapabilityV1:
        decoded: dict[str, dict[str, Any]] = {}
        for label, encoded in (
            ("config_schema", self.config_schema_json),
            ("default_config", self.default_config_json),
        ):
            if len(encoded.encode("utf-8")) > MAX_CONTRACT_JSON_BYTES:
                raise ValueError(f"{label}_json exceeds maximum bytes")
            try:
                value = json.loads(encoded)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{label}_json must contain canonical JSON") from exc
            if not isinstance(value, dict) or canonical_json(value) != encoded:
                raise ValueError(f"{label}_json must be a canonical JSON object")
            _validate_desktop_safe_integers(value, f"{label}_json")
            decoded[label] = value
        schema = decoded["config_schema"]
        default = decoded["default_config"]
        validate_config_schema(schema)
        normalize_partial_config(schema, default)
        return self


class EvolutionResolvedMethodCapabilityV1(_Contract):
    method_id: str
    implementation_identity_digest: str
    support: MethodSupport

    _id = field_validator("method_id")(_stable_id)
    _digest = field_validator("implementation_identity_digest")(_digest)


class EvolutionSelectionResolverCapabilityV1(_Contract):
    selection_value: str
    display_name: str = Field(max_length=4096)
    description: str = Field(max_length=4096)
    resolved_methods: tuple[EvolutionResolvedMethodCapabilityV1, ...] = Field(
        max_length=_MAX_CAPABILITY_METHODS
    )

    _selection = field_validator("selection_value")(_stable_id)
    _text_fields = field_validator("display_name", "description")(_text)

    @field_validator("resolved_methods")
    @classmethod
    def _canonical_resolved_methods(
        cls,
        values: tuple[EvolutionResolvedMethodCapabilityV1, ...],
    ) -> tuple[EvolutionResolvedMethodCapabilityV1, ...]:
        method_ids = tuple(value.method_id for value in values)
        if not method_ids or len(method_ids) != len(set(method_ids)):
            raise ValueError("selection resolver method IDs must be non-empty and unique")
        return tuple(sorted(values, key=lambda value: value.method_id))


class EvolutionTargetCapabilityV1(_Contract):
    target_id: str
    display_name: str = Field(max_length=4096)
    description: str = Field(max_length=4096)
    artifact_type: str
    exposure: Exposure
    maturity: Maturity
    handler_id: str
    configured_default_method_id: str
    effective_default_method_id: str | None
    configured_default_support: MethodSupport
    renderer_kind: RendererKind
    renderer_contract_version: str
    contribution_contract_version: str
    context_order: int = Field(ge=0, le=10_000)
    implementation_identity_digest: str
    handler_identity_digest: str
    accepted_methods: tuple[EvolutionResolvedMethodCapabilityV1, ...] = Field(
        max_length=_MAX_CAPABILITY_METHODS
    )
    selection_resolvers: tuple[EvolutionSelectionResolverCapabilityV1, ...] = Field(
        max_length=_MAX_CAPABILITY_RESOLVERS
    )
    methods: tuple[EvolutionMethodCapabilityV1, ...] = Field(
        max_length=_MAX_CAPABILITY_METHODS
    )

    _ids = field_validator(
        "target_id", "artifact_type", "handler_id", "configured_default_method_id"
    )(_stable_id)
    _effective_default = field_validator("effective_default_method_id")(
        _optional_stable_id
    )
    _text_fields = field_validator(
        "display_name", "description", "renderer_contract_version",
        "contribution_contract_version"
    )(_text)
    _digests = field_validator(
        "implementation_identity_digest", "handler_identity_digest"
    )(_digest)

    @field_validator("methods")
    @classmethod
    def _canonical_methods(
        cls,
        values: tuple[EvolutionMethodCapabilityV1, ...],
    ) -> tuple[EvolutionMethodCapabilityV1, ...]:
        method_ids = tuple(value.method_id for value in values)
        if len(method_ids) != len(set(method_ids)):
            raise ValueError("capability target method IDs must be unique")
        return tuple(sorted(values, key=lambda value: value.method_id))

    @field_validator("accepted_methods")
    @classmethod
    def _canonical_accepted_methods(
        cls,
        values: tuple[EvolutionResolvedMethodCapabilityV1, ...],
    ) -> tuple[EvolutionResolvedMethodCapabilityV1, ...]:
        method_ids = tuple(value.method_id for value in values)
        if not method_ids or len(method_ids) != len(set(method_ids)):
            raise ValueError("accepted method IDs must be non-empty and unique")
        return tuple(sorted(values, key=lambda value: value.method_id))

    @field_validator("selection_resolvers")
    @classmethod
    def _canonical_selection_resolvers(
        cls,
        values: tuple[EvolutionSelectionResolverCapabilityV1, ...],
    ) -> tuple[EvolutionSelectionResolverCapabilityV1, ...]:
        selection_values = tuple(value.selection_value for value in values)
        if len(selection_values) != len(set(selection_values)):
            raise ValueError("capability selection resolver values must be unique")
        return tuple(sorted(values, key=lambda value: value.selection_value))

    @model_validator(mode="after")
    def _default_is_visible(self) -> EvolutionTargetCapabilityV1:
        methods = {method.method_id: method for method in self.methods}
        accepted_methods = {
            method.method_id: method for method in self.accepted_methods
        }
        if self.configured_default_method_id not in methods:
            raise ValueError("capability target default method is not visible")
        configured = methods[self.configured_default_method_id]
        if self.configured_default_support != configured.support:
            raise ValueError("capability target default support does not match method")
        for method in self.methods:
            accepted = accepted_methods.get(method.method_id)
            if accepted is None:
                raise ValueError("visible capability method is not accepted")
            if (
                accepted.implementation_identity_digest
                != method.implementation_identity_digest
                or accepted.support != method.support
            ):
                raise ValueError("visible and accepted method metadata does not match")
        for resolver in self.selection_resolvers:
            for method in resolver.resolved_methods:
                accepted = accepted_methods.get(method.method_id)
                if accepted is None:
                    raise ValueError("selection resolver method is not accepted")
                if accepted != method:
                    raise ValueError(
                        "selection resolver and accepted method metadata does not match"
                    )
        if self.effective_default_method_id is not None:
            if self.effective_default_method_id != self.configured_default_method_id:
                raise ValueError(
                    "capability effective default cannot replace configured default"
                )
            try:
                effective = methods[self.effective_default_method_id]
            except KeyError as exc:
                raise ValueError("capability effective default method is not visible") from exc
            if effective.support.overall is not MethodSupportOverall.SUPPORTED:
                raise ValueError("capability effective default method is not supported")
        return self


class EvolutionCapabilitiesV1(_Contract):
    schema_version: Literal["1"] = "1"
    core_version: str
    registry_digest: str
    evaluated_profile: EvolutionExecutionProfile
    targets: tuple[EvolutionTargetCapabilityV1, ...] = Field(
        max_length=_MAX_CAPABILITY_TARGETS
    )

    _version = field_validator("core_version")(_text)
    _digest = field_validator("registry_digest")(_digest)

    @model_validator(mode="before")
    @classmethod
    def _bounded_payload(cls, value: object) -> object:
        if not isinstance(value, cls):
            _validate_capability_payload_budget(value)
        return value

    @field_validator("targets")
    @classmethod
    def _canonical_targets(
        cls,
        values: tuple[EvolutionTargetCapabilityV1, ...],
    ) -> tuple[EvolutionTargetCapabilityV1, ...]:
        target_ids = tuple(value.target_id for value in values)
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("capability target IDs must be unique")
        return tuple(sorted(values, key=lambda value: value.target_id))


_AUDIENCE_EXPOSURES = {
    CapabilityAudience.DESKTOP: frozenset({Exposure.DESKTOP}),
    CapabilityAudience.MAINTAINER: frozenset(
        {Exposure.DESKTOP, Exposure.MAINTAINER}
    ),
    CapabilityAudience.INTERNAL: frozenset(Exposure),
}


def build_evolution_capabilities(
    snapshot: RegistrySnapshot,
    *,
    profile: EvolutionExecutionProfile,
    audience: CapabilityAudience | str,
    core_version: str,
) -> EvolutionCapabilitiesV1:
    normalized_audience = CapabilityAudience(audience)
    visible_exposures = _AUDIENCE_EXPOSURES[normalized_audience]
    targets: list[EvolutionTargetCapabilityV1] = []
    for target_id in sorted(snapshot.targets):
        target = snapshot.targets[target_id]
        if target.exposure not in visible_exposures:
            continue
        handler = snapshot.target_handlers[target.handler_id]
        accepted_methods = tuple(
            EvolutionResolvedMethodCapabilityV1(
                method_id=method.id,
                implementation_identity_digest=snapshot.identity_digest_for(
                    "method", method.id
                ),
                support=evaluate_method_support(method, profile),
            )
            for method in (
                snapshot.methods[method_id]
                for method_id in sorted(snapshot.methods)
                if snapshot.methods[method_id].target_id == target.id
            )
        )
        methods: list[EvolutionMethodCapabilityV1] = []
        for method_id in sorted(snapshot.methods):
            method = snapshot.methods[method_id]
            if (
                method.target_id != target.id
                or method.exposure not in visible_exposures
            ):
                continue
            project_schema, project_default = _project_config_contract(method)
            methods.append(
                EvolutionMethodCapabilityV1(
                    method_id=method.id,
                    display_name=method.display_name,
                    description=method.description,
                    exposure=method.exposure,
                    maturity=method.maturity,
                    execution_modes=method.execution_modes,
                    capture_modes=method.capture_modes,
                    supported_harness_ids=method.supported_harness_ids,
                    harness_requirements=method.harness_requirements,
                    runtime_requirements=method.runtime_requirements,
                    input_bindings=method.input_bindings,
                    output_artifact_types=method.output_artifact_types,
                    config_schema_json=canonical_json(project_schema),
                    default_config_json=canonical_json(project_default),
                    implementation_identity_digest=snapshot.identity_digest_for(
                        "method", method.id
                    ),
                    support=evaluate_method_support(method, profile),
                )
            )
        methods_by_id = {method.method_id: method for method in methods}
        configured_default = methods_by_id[target.default_method_id]
        effective_default_method_id = (
            target.default_method_id
            if configured_default.support.overall is MethodSupportOverall.SUPPORTED
            else None
        )
        targets.append(
            EvolutionTargetCapabilityV1(
                target_id=target.id,
                display_name=target.display_name,
                description=target.description,
                artifact_type=target.artifact_type,
                exposure=target.exposure,
                maturity=target.maturity,
                handler_id=handler.id,
                configured_default_method_id=target.default_method_id,
                effective_default_method_id=effective_default_method_id,
                configured_default_support=configured_default.support,
                renderer_kind=target.renderer_kind,
                renderer_contract_version=target.renderer_contract_version,
                contribution_contract_version=handler.contribution_contract_version,
                context_order=target.context_order,
                implementation_identity_digest=snapshot.identity_digest_for(
                    "target", target.id
                ),
                handler_identity_digest=snapshot.identity_digest_for(
                    "target_handler", handler.id
                ),
                accepted_methods=accepted_methods,
                selection_resolvers=tuple(
                    EvolutionSelectionResolverCapabilityV1(
                        selection_value=resolver.selection_value,
                        display_name=resolver.display_name,
                        description=resolver.description,
                        resolved_methods=tuple(
                            EvolutionResolvedMethodCapabilityV1(
                                method_id=method_id,
                                implementation_identity_digest=(
                                    snapshot.identity_digest_for("method", method_id)
                                ),
                                support=evaluate_method_support(
                                    snapshot.methods[method_id],
                                    profile,
                                ),
                            )
                            for method_id in resolver.resolved_method_ids
                        ),
                    )
                    for resolver in target.selection_resolvers
                ),
                methods=tuple(methods),
            )
        )
    return EvolutionCapabilitiesV1(
        core_version=core_version,
        registry_digest=snapshot.registry_digest,
        evaluated_profile=profile,
        targets=tuple(targets),
    )


def _project_config_contract(
    method: EvolutionMethodDescriptor,
) -> tuple[dict[str, object], dict[str, object]]:
    schema = json.loads(canonical_json(method.config_schema))
    default = json.loads(canonical_json(method.default_config))
    injected = {
        injection.field_name for injection in method.project_config_injections
    }
    properties = schema.get("properties", {})
    if isinstance(properties, dict):
        for field_name in injected:
            properties.pop(field_name, None)
    required = schema.get("required")
    if isinstance(required, list):
        remaining = [field_name for field_name in required if field_name not in injected]
        if remaining:
            schema["required"] = remaining
        else:
            schema.pop("required", None)
    for field_name in injected:
        default.pop(field_name, None)
    return schema, default


__all__ = [
    "CapabilityAudience",
    "EvolutionCapabilitiesV1",
    "EvolutionMethodCapabilityV1",
    "EvolutionResolvedMethodCapabilityV1",
    "EvolutionSelectionResolverCapabilityV1",
    "EvolutionTargetCapabilityV1",
    "build_evolution_capabilities",
]
