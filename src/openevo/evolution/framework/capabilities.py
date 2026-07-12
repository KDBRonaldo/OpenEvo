"""Versioned Desktop/maintainer projection of one frozen framework registry."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from pydantic import field_validator, model_validator

from .contracts import (
    CaptureMode,
    EvolutionExecutionProfile,
    ExecutionMode,
    Exposure,
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
from .support import MethodSupport, MethodSupportOverall, evaluate_method_support

if TYPE_CHECKING:
    from .registry import RegistrySnapshot


class CapabilityAudience(StrEnum):
    DESKTOP = "desktop"
    MAINTAINER = "maintainer"
    INTERNAL = "internal"


class EvolutionMethodCapabilityV1(_Contract):
    method_id: str
    display_name: str
    description: str
    exposure: Exposure
    maturity: Maturity
    execution_modes: tuple[ExecutionMode, ...]
    capture_modes: tuple[CaptureMode, ...]
    supported_harness_ids: tuple[str, ...]
    harness_requirements: tuple[str, ...]
    runtime_requirements: tuple[str, ...]
    input_bindings: tuple[MethodInputBinding, ...]
    output_artifact_types: tuple[str, ...]
    config_schema_json: str
    default_config_json: str
    implementation_identity_digest: str
    support: MethodSupport

    _id = field_validator("method_id")(_stable_id)
    _text_fields = field_validator("display_name", "description")(_text)
    _digest = field_validator("implementation_identity_digest")(_digest)

    @model_validator(mode="after")
    def _canonical_config_json(self) -> EvolutionMethodCapabilityV1:
        for label, encoded in (
            ("config_schema", self.config_schema_json),
            ("default_config", self.default_config_json),
        ):
            try:
                value = json.loads(encoded)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{label}_json must contain canonical JSON") from exc
            if not isinstance(value, dict) or canonical_json(value) != encoded:
                raise ValueError(f"{label}_json must be a canonical JSON object")
        return self


class EvolutionTargetCapabilityV1(_Contract):
    target_id: str
    display_name: str
    description: str
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
    context_order: int
    implementation_identity_digest: str
    handler_identity_digest: str
    methods: tuple[EvolutionMethodCapabilityV1, ...]

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

    @model_validator(mode="after")
    def _default_is_visible(self) -> EvolutionTargetCapabilityV1:
        methods = {method.method_id: method for method in self.methods}
        if self.configured_default_method_id not in methods:
            raise ValueError("capability target default method is not visible")
        configured = methods[self.configured_default_method_id]
        if self.configured_default_support != configured.support:
            raise ValueError("capability target default support does not match method")
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
    targets: tuple[EvolutionTargetCapabilityV1, ...]

    _version = field_validator("core_version")(_text)
    _digest = field_validator("registry_digest")(_digest)

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
        methods: list[EvolutionMethodCapabilityV1] = []
        for method_id in sorted(snapshot.methods):
            method = snapshot.methods[method_id]
            if (
                method.target_id != target.id
                or method.exposure not in visible_exposures
            ):
                continue
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
                    config_schema_json=canonical_json(method.config_schema),
                    default_config_json=canonical_json(method.default_config),
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
                methods=tuple(methods),
            )
        )
    return EvolutionCapabilitiesV1(
        core_version=core_version,
        registry_digest=snapshot.registry_digest,
        evaluated_profile=profile,
        targets=tuple(targets),
    )


__all__ = [
    "CapabilityAudience",
    "EvolutionCapabilitiesV1",
    "EvolutionMethodCapabilityV1",
    "EvolutionTargetCapabilityV1",
    "build_evolution_capabilities",
]
