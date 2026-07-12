"""Deterministic descriptors for OpenEvo's existing evolution implementations.

A2.2 catalogs the legacy callables without changing worker dispatch.  Target
and handler entry points are identity anchors until their A2.4 runtime cutover.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from pydantic import field_validator

from .contracts import (
    CaptureMode,
    ContributionKind,
    DescriptorKind,
    DestinationScope,
    ExecutionMode,
    Exposure,
    ImplementationIdentity,
    ImplementationRef,
    Maturity,
    RendererKind,
    _Contract,
    _digest,
    _distribution_name,
    _distribution_version,
)
from .descriptors import (
    EvolutionMethodDescriptor,
    EvolutionTargetDescriptor,
    TargetHandlerDescriptor,
)
from .execution import LegacyEvolutionMethod, MethodInputBinding
from .loading import (
    DescriptorImplementationAnchor,
    VerifiedDistribution,
    load_verified_entry_point,
)
from .registry import EvolutionFrameworkRegistry, RegistrySnapshot


BUILTIN_METHOD_IDS = (
    "text_memory",
    "text_memory_reflector",
    "text_memory_expel_reflector",
    "skill_bundle",
    "skill_bundle_reflector",
    "agent_system",
    "agent_system_reflector",
    "agent_system_history_reflector",
    "agent_system_pareto_reflector",
    "agent_system_gepa_reflector",
    "parametric_memory_register",
    "parametric_memory_lora_sft",
)

_METHODS_MODULE = "openevo.evolution.methods"
_BUILTINS_MODULE = "openevo.evolution.framework.builtins"
_BOTH_EXECUTION_MODES = (
    ExecutionMode.SUBSCRIPTION,
    ExecutionMode.SELF_DEPLOYED,
)
_SELF_DEPLOYED = (ExecutionMode.SELF_DEPLOYED,)
_TEXT_AND_TOKEN_CAPTURE = (CaptureMode.TRANSCRIPT, CaptureMode.TOKEN_LEVEL)
_CODEX = ("codex",)


class ImplementationDistributionIdentity(_Contract):
    """Immutable artifact identity supplied by release/bootstrap verification."""

    distribution: str
    distribution_version: str
    distribution_digest: str

    _name = field_validator("distribution")(_distribution_name)
    _version = field_validator("distribution_version")(_distribution_version)
    _sha = field_validator("distribution_digest")(_digest)

    def ref(self, entry_point: str) -> ImplementationRef:
        return ImplementationRef(
            distribution=self.distribution,
            distribution_version=self.distribution_version,
            distribution_digest=self.distribution_digest,
            entry_point=entry_point,
        )


@dataclass(frozen=True, slots=True)
class LoadedBuiltinRegistry:
    """Frozen catalog plus the exact handles proven against its distribution."""

    snapshot: RegistrySnapshot
    method_handles: Mapping[str, LegacyEvolutionMethod]
    descriptor_anchors: Mapping[str, DescriptorImplementationAnchor]


text_memory_target_anchor = DescriptorImplementationAnchor(
    descriptor_kind=DescriptorKind.TARGET,
    descriptor_id="text_memory",
)
skill_bundle_target_anchor = DescriptorImplementationAnchor(
    descriptor_kind=DescriptorKind.TARGET,
    descriptor_id="skill_bundle",
)
agent_system_target_anchor = DescriptorImplementationAnchor(
    descriptor_kind=DescriptorKind.TARGET,
    descriptor_id="agent_system",
)
parametric_memory_target_anchor = DescriptorImplementationAnchor(
    descriptor_kind=DescriptorKind.TARGET,
    descriptor_id="parametric_memory",
)

text_memory_handler_anchor = DescriptorImplementationAnchor(
    descriptor_kind=DescriptorKind.TARGET_HANDLER,
    descriptor_id="text_memory_handler",
)
skill_bundle_handler_anchor = DescriptorImplementationAnchor(
    descriptor_kind=DescriptorKind.TARGET_HANDLER,
    descriptor_id="skill_bundle_handler",
)
agent_system_handler_anchor = DescriptorImplementationAnchor(
    descriptor_kind=DescriptorKind.TARGET_HANDLER,
    descriptor_id="agent_system_handler",
)
parametric_memory_handler_anchor = DescriptorImplementationAnchor(
    descriptor_kind=DescriptorKind.TARGET_HANDLER,
    descriptor_id="parametric_memory_handler",
)


def _closed_object(
    properties: Mapping[str, dict[str, Any]] | None = None,
    *,
    required: tuple[str, ...] = (),
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": dict(properties or {}),
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


def _string(*, minimum: int = 1, maximum: int = 4096) -> dict[str, Any]:
    return {
        "type": "string",
        "minLength": minimum,
        "maxLength": maximum,
    }


def _positive_integer(*, maximum: int = 10_000) -> dict[str, Any]:
    return {"type": "integer", "minimum": 1, "maximum": maximum}


def _string_array(*, maximum: int) -> dict[str, Any]:
    return {
        "type": "array",
        "items": _string(),
        "minItems": 1,
        "maxItems": maximum,
    }


def _reflector_llm_schema() -> dict[str, Any]:
    return _closed_object(
        {
            "model": _string(),
            "provider": {
                "type": "string",
                "enum": ["codex_cli"],
            },
            "temperature": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 2.0,
            },
            "timeout_seconds": {
                "type": "number",
                "exclusiveMinimum": 0.0,
                "maximum": 86_400.0,
            },
            "max_tokens": _positive_integer(maximum=1_048_576),
        },
        required=("model", "provider"),
    )


def _reflector_schema(
    *,
    record_limit_name: str,
    extra: Mapping[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return _closed_object(
        {
            record_limit_name: _positive_integer(),
            "reflector_llm": _reflector_llm_schema(),
            **dict(extra or {}),
        },
        required=("reflector_llm",),
    )


def _agent_system_fields() -> dict[str, dict[str, Any]]:
    return {
        "target_path": _string(),
        "base_agent_system_markdown": _string(),
        "agent_system_markdown": _string(),
        "content": _string(),
    }


def _current_dataset() -> MethodInputBinding:
    return MethodInputBinding(
        binding_id="current_dataset",
        source="current_dataset",
        artifact_type="dataset",
        min_count=1,
        max_count=1,
    )


def _ordered_dataset_inputs() -> MethodInputBinding:
    return MethodInputBinding(
        binding_id="dataset_inputs",
        source="explicit_inputs",
        artifact_type="dataset",
        min_count=1,
        max_count=128,
    )


def _prior_target(artifact_type: str) -> MethodInputBinding:
    return MethodInputBinding(
        binding_id="prior_target_artifacts",
        source="current_target_artifacts",
        artifact_type=artifact_type,
        max_count=128,
    )


def _target_descriptors(
    identity: ImplementationDistributionIdentity,
) -> tuple[EvolutionTargetDescriptor, ...]:
    values = (
        (
            "agent_system",
            "Agent system",
            "Reusable harness instruction text evolved from prior runs.",
            "agent_system_handler",
            RendererKind.MARKDOWN,
            "agent_system_gepa_reflector",
            10,
            Exposure.DESKTOP,
            "agent_system_target_anchor",
        ),
        (
            "text_memory",
            "Text memory",
            "Reusable natural-language memory evolved from prior runs.",
            "text_memory_handler",
            RendererKind.MARKDOWN,
            "text_memory_expel_reflector",
            20,
            Exposure.DESKTOP,
            "text_memory_target_anchor",
        ),
        (
            "skill_bundle",
            "Skill bundle",
            "Harness-loadable workflow skills evolved from trajectories.",
            "skill_bundle_handler",
            RendererKind.FILE_BUNDLE,
            "skill_bundle_reflector",
            30,
            Exposure.DESKTOP,
            "skill_bundle_target_anchor",
        ),
        (
            "parametric_memory",
            "Parametric memory",
            "Model adapter state for self-deployed inference backends.",
            "parametric_memory_handler",
            RendererKind.ADAPTER,
            "parametric_memory_register",
            40,
            Exposure.INTERNAL,
            "parametric_memory_target_anchor",
        ),
    )
    return tuple(
        EvolutionTargetDescriptor(
            id=target_id,
            display_name=display_name,
            description=description,
            artifact_type=target_id,
            handler_id=handler_id,
            renderer_kind=renderer,
            default_method_id=default_method,
            context_order=context_order,
            exposure=exposure,
            maturity=Maturity.EXPERIMENTAL,
            implementation_ref=identity.ref(f"{_BUILTINS_MODULE}:{anchor}"),
        )
        for (
            target_id,
            display_name,
            description,
            handler_id,
            renderer,
            default_method,
            context_order,
            exposure,
            anchor,
        ) in values
    )


def _handler_descriptors(
    identity: ImplementationDistributionIdentity,
) -> tuple[TargetHandlerDescriptor, ...]:
    return (
        TargetHandlerDescriptor(
            id="text_memory_handler",
            target_id="text_memory",
            artifact_types=("text_memory",),
            renderer_kind=RendererKind.MARKDOWN,
            allowed_uri_schemes=("file",),
            allowed_media_types=("text/markdown", "text/plain"),
            allowed_destination_scopes=(DestinationScope.TARGET_DATA,),
            environment_allowlist=("OPENEVO_MEMORY_FILE",),
            allowed_contribution_kinds=(
                ContributionKind.INSTRUCTION,
                ContributionKind.STAGED_PAYLOAD,
                ContributionKind.ENVIRONMENT,
            ),
            exposure=Exposure.DESKTOP,
            implementation_ref=identity.ref(
                f"{_BUILTINS_MODULE}:text_memory_handler_anchor"
            ),
        ),
        TargetHandlerDescriptor(
            id="skill_bundle_handler",
            target_id="skill_bundle",
            artifact_types=("skill_bundle",),
            renderer_kind=RendererKind.FILE_BUNDLE,
            allowed_uri_schemes=("file",),
            allowed_media_types=(
                "application/json",
                "application/octet-stream",
                "application/toml",
                "application/x-sh",
                "application/yaml",
                "image/jpeg",
                "image/png",
                "image/svg+xml",
                "text/css",
                "text/csv",
                "text/html",
                "text/javascript",
                "text/markdown",
                "text/plain",
                "text/x-python",
                "text/x-shellscript",
                "text/yaml",
            ),
            allowed_destination_scopes=(DestinationScope.HARNESS_SKILLS,),
            environment_allowlist=("OPENEVO_SKILLS_DIR",),
            allowed_contribution_kinds=(
                ContributionKind.STAGED_PAYLOAD,
                ContributionKind.ENVIRONMENT,
            ),
            exposure=Exposure.DESKTOP,
            implementation_ref=identity.ref(
                f"{_BUILTINS_MODULE}:skill_bundle_handler_anchor"
            ),
        ),
        TargetHandlerDescriptor(
            id="agent_system_handler",
            target_id="agent_system",
            artifact_types=("agent_system",),
            renderer_kind=RendererKind.MARKDOWN,
            allowed_uri_schemes=("file",),
            allowed_media_types=("text/markdown", "text/plain"),
            allowed_destination_scopes=(
                DestinationScope.TARGET_DATA,
                DestinationScope.HARNESS_INSTRUCTION,
            ),
            environment_allowlist=(
                "OPENEVO_AGENTS_MD",
                "OPENEVO_AGENT_SYSTEM_FILE",
                "OPENEVO_AGENT_SYSTEM_TARGET",
                "OPENEVO_AGENT_SYSTEM_TARGETS",
            ),
            allowed_contribution_kinds=(
                ContributionKind.INSTRUCTION,
                ContributionKind.STAGED_PAYLOAD,
                ContributionKind.ENVIRONMENT,
            ),
            exposure=Exposure.DESKTOP,
            implementation_ref=identity.ref(
                f"{_BUILTINS_MODULE}:agent_system_handler_anchor"
            ),
        ),
        TargetHandlerDescriptor(
            id="parametric_memory_handler",
            target_id="parametric_memory",
            artifact_types=("parametric_memory",),
            renderer_kind=RendererKind.ADAPTER,
            allowed_uri_schemes=("file", "hf", "https", "s3"),
            allowed_media_types=("application/json", "application/octet-stream"),
            allowed_destination_scopes=(DestinationScope.TARGET_DATA,),
            allowed_contribution_kinds=(ContributionKind.ADAPTER,),
            exposure=Exposure.INTERNAL,
            implementation_ref=identity.ref(
                f"{_BUILTINS_MODULE}:parametric_memory_handler_anchor"
            ),
        ),
    )


def _method(
    identity: ImplementationDistributionIdentity,
    *,
    method_id: str,
    display_name: str,
    description: str,
    target_id: str,
    execution_modes: tuple[ExecutionMode, ...],
    input_bindings: tuple[MethodInputBinding, ...],
    output_artifact_types: tuple[str, ...],
    config_schema: dict[str, Any],
    default_config: dict[str, Any] | None = None,
    exposure: Exposure = Exposure.MAINTAINER,
    runtime_requirements: tuple[str, ...] = (),
) -> EvolutionMethodDescriptor:
    return EvolutionMethodDescriptor(
        id=method_id,
        display_name=display_name,
        description=description,
        target_id=target_id,
        execution_modes=execution_modes,
        capture_modes=_TEXT_AND_TOKEN_CAPTURE,
        supported_harness_ids=_CODEX,
        runtime_requirements=runtime_requirements,
        input_bindings=input_bindings,
        output_artifact_types=output_artifact_types,
        config_schema=config_schema,
        default_config=default_config or {},
        exposure=exposure,
        maturity=Maturity.EXPERIMENTAL,
        implementation_ref=identity.ref(f"{_METHODS_MODULE}:{method_id}"),
    )


def _method_descriptors(
    identity: ImplementationDistributionIdentity,
) -> tuple[EvolutionMethodDescriptor, ...]:
    agent_fields = _agent_system_fields()
    return (
        _method(
            identity,
            method_id="text_memory",
            display_name="Text memory materializer",
            description="Build text memory directly from one dataset.",
            target_id="text_memory",
            execution_modes=_SELF_DEPLOYED,
            input_bindings=(_current_dataset(),),
            output_artifact_types=("text_memory",),
            config_schema=_closed_object(),
        ),
        _method(
            identity,
            method_id="text_memory_reflector",
            display_name="Text memory reflector",
            description="Reflect over trajectories to synthesize reusable memory.",
            target_id="text_memory",
            execution_modes=_BOTH_EXECUTION_MODES,
            input_bindings=(_current_dataset(), _prior_target("text_memory")),
            output_artifact_types=("text_memory",),
            config_schema=_reflector_schema(record_limit_name="max_records"),
        ),
        _method(
            identity,
            method_id="text_memory_expel_reflector",
            display_name="Text memory ExpeL reflector",
            description="Synthesize structured ExpeL memory from success and failure traces.",
            target_id="text_memory",
            execution_modes=_BOTH_EXECUTION_MODES,
            input_bindings=(
                _ordered_dataset_inputs(),
                _prior_target("text_memory"),
            ),
            output_artifact_types=("text_memory",),
            config_schema=_reflector_schema(record_limit_name="max_records"),
            exposure=Exposure.DESKTOP,
        ),
        _method(
            identity,
            method_id="skill_bundle",
            display_name="Skill bundle materializer",
            description="Register configured Markdown as a skill bundle.",
            target_id="skill_bundle",
            execution_modes=_SELF_DEPLOYED,
            input_bindings=(),
            output_artifact_types=("skill_bundle",),
            config_schema=_closed_object(
                {
                    "skill_markdown": _string(),
                    "content": _string(),
                }
            ),
        ),
        _method(
            identity,
            method_id="skill_bundle_reflector",
            display_name="Skill bundle reflector",
            description="Synthesize a harness-loadable skill from trajectories.",
            target_id="skill_bundle",
            execution_modes=_BOTH_EXECUTION_MODES,
            input_bindings=(_current_dataset(), _prior_target("skill_bundle")),
            output_artifact_types=("skill_bundle",),
            config_schema=_reflector_schema(
                record_limit_name="max_records",
                extra={
                    "base_skill_markdown": _string(),
                    "skill_markdown": _string(),
                    "content": _string(),
                },
            ),
            exposure=Exposure.DESKTOP,
        ),
        _method(
            identity,
            method_id="agent_system",
            display_name="Agent system materializer",
            description="Register configured Markdown as harness instructions.",
            target_id="agent_system",
            execution_modes=_SELF_DEPLOYED,
            input_bindings=(),
            output_artifact_types=("agent_system",),
            config_schema=_closed_object(agent_fields),
            default_config={"target_path": "AGENTS.md"},
        ),
        _method(
            identity,
            method_id="agent_system_reflector",
            display_name="Agent system reflector",
            description="Reflect over trajectories to improve harness instructions.",
            target_id="agent_system",
            execution_modes=_BOTH_EXECUTION_MODES,
            input_bindings=(_current_dataset(), _prior_target("agent_system")),
            output_artifact_types=("agent_system",),
            config_schema=_reflector_schema(
                record_limit_name="max_records",
                extra=agent_fields,
            ),
            default_config={"target_path": "AGENTS.md"},
        ),
        _method(
            identity,
            method_id="agent_system_history_reflector",
            display_name="Agent system history reflector",
            description="Use accumulated evolution rounds to improve harness instructions.",
            target_id="agent_system",
            execution_modes=_BOTH_EXECUTION_MODES,
            input_bindings=(
                _ordered_dataset_inputs(),
                _prior_target("agent_system"),
            ),
            output_artifact_types=("agent_system",),
            config_schema=_reflector_schema(
                record_limit_name="max_records_per_round",
                extra=agent_fields,
            ),
            default_config={"target_path": "AGENTS.md"},
        ),
        _method(
            identity,
            method_id="agent_system_pareto_reflector",
            display_name="Agent system Pareto reflector",
            description="Generate audited instruction candidates with Pareto-style evidence.",
            target_id="agent_system",
            execution_modes=_BOTH_EXECUTION_MODES,
            input_bindings=(
                _ordered_dataset_inputs(),
                _prior_target("agent_system"),
            ),
            output_artifact_types=("agent_system", "report"),
            config_schema=_reflector_schema(
                record_limit_name="max_records_per_round",
                extra={
                    **agent_fields,
                    "candidate_count": _positive_integer(maximum=5),
                    "candidate_strategies": _string_array(maximum=16),
                },
            ),
            default_config={"target_path": "AGENTS.md"},
        ),
        _method(
            identity,
            method_id="agent_system_gepa_reflector",
            display_name="Agent system GEPA reflector",
            description="Generate GEPA-style audited instruction mutation candidates.",
            target_id="agent_system",
            execution_modes=_BOTH_EXECUTION_MODES,
            input_bindings=(
                _ordered_dataset_inputs(),
                _prior_target("agent_system"),
            ),
            output_artifact_types=("agent_system", "report"),
            config_schema=_reflector_schema(
                record_limit_name="max_records_per_round",
                extra={
                    **agent_fields,
                    "candidate_count": _positive_integer(maximum=5),
                    "mutation_strategies": _string_array(maximum=16),
                },
            ),
            default_config={"target_path": "AGENTS.md"},
            exposure=Exposure.DESKTOP,
        ),
        _method(
            identity,
            method_id="parametric_memory_register",
            display_name="Parametric memory register",
            description="Register one prebuilt model adapter artifact.",
            target_id="parametric_memory",
            execution_modes=_SELF_DEPLOYED,
            input_bindings=(),
            output_artifact_types=("parametric_memory",),
            config_schema=_closed_object(
                {
                    "adapter_uri": _string(),
                    "base_model": _string(),
                    "adapter_id": _string(),
                    "adapter_format": _string(),
                },
                required=("adapter_uri", "base_model"),
            ),
            exposure=Exposure.INTERNAL,
            runtime_requirements=("adapter_serving",),
        ),
        _method(
            identity,
            method_id="parametric_memory_lora_sft",
            display_name="Parametric memory LoRA SFT",
            description="Legacy internal LoRA trainer pending a constrained trainer contract.",
            target_id="parametric_memory",
            execution_modes=_SELF_DEPLOYED,
            input_bindings=(
                _ordered_dataset_inputs(),
                _prior_target("parametric_memory"),
            ),
            output_artifact_types=("parametric_memory",),
            config_schema=_closed_object(),
            exposure=Exposure.INTERNAL,
            runtime_requirements=(
                "adapter_serving",
                "constrained_trainer_contract",
                "trainer",
            ),
        ),
    )


def build_builtin_registry(
    distribution: ImplementationDistributionIdentity,
) -> RegistrySnapshot:
    """Build and freeze the deterministic A2.2 built-in catalog."""

    registry = EvolutionFrameworkRegistry()
    for descriptor in (
        *_target_descriptors(distribution),
        *_handler_descriptors(distribution),
        *_method_descriptors(distribution),
    ):
        registry.register(descriptor)
    snapshot = registry.freeze()
    if set(snapshot.methods) != set(BUILTIN_METHOD_IDS):
        raise ValueError("built-in method descriptor set mismatch")
    return snapshot


def load_builtin_method_handles(
    snapshot: RegistrySnapshot,
    *,
    verified_loader: Callable[[ImplementationIdentity], object],
) -> Mapping[str, LegacyEvolutionMethod]:
    """Verify catalog entry points while legacy dispatch remains authoritative."""

    expected = set(BUILTIN_METHOD_IDS)
    actual_descriptors = set(snapshot.methods)
    if actual_descriptors != expected:
        missing = sorted(expected - actual_descriptors)
        extra = sorted(actual_descriptors - expected)
        raise ValueError(
            f"built-in method key mismatch; missing={missing!r}, extra={extra!r}"
        )

    loaded_handles: dict[str, object] = {}
    for method_id in BUILTIN_METHOD_IDS:
        identity = snapshot.identity_for(DescriptorKind.METHOD, method_id)
        loaded_handles[method_id] = verified_loader(identity)

    from openevo.evolution.methods import METHOD_REGISTRY

    actual_registry = set(METHOD_REGISTRY)
    if actual_registry != expected:
        missing = sorted(expected - actual_registry)
        extra = sorted(actual_registry - expected)
        raise ValueError(
            f"built-in method key mismatch; missing={missing!r}, extra={extra!r}"
        )

    handles: dict[str, LegacyEvolutionMethod] = {}
    for method_id in BUILTIN_METHOD_IDS:
        loaded = loaded_handles[method_id]
        expected_callable = METHOD_REGISTRY[method_id]
        if loaded is not expected_callable:
            raise ValueError(f"built-in method callable identity mismatch for {method_id!r}")
        handles[method_id] = expected_callable
    return MappingProxyType(handles)


def load_verified_builtin_registry(
    verified: VerifiedDistribution,
    *,
    entry_point_loader: Callable[..., object] = load_verified_entry_point,
) -> LoadedBuiltinRegistry:
    """Build the catalog and verify every built-in entry point before use."""

    expectation = verified.expectation
    identity = ImplementationDistributionIdentity(
        distribution=expectation.distribution,
        distribution_version=expectation.distribution_version,
        distribution_digest=expectation.distribution_digest,
    )
    snapshot = build_builtin_registry(identity)

    anchors: dict[str, DescriptorImplementationAnchor] = {}
    for kind, descriptors in (
        (DescriptorKind.TARGET, snapshot.targets),
        (DescriptorKind.TARGET_HANDLER, snapshot.target_handlers),
    ):
        for descriptor_id in descriptors:
            implementation_identity = snapshot.identity_for(kind, descriptor_id)
            loaded = entry_point_loader(
                implementation_identity.implementation,
                verified,
                expected_kind=kind,
                expected_id=descriptor_id,
            )
            if not isinstance(loaded, DescriptorImplementationAnchor):
                raise ValueError(
                    f"built-in descriptor anchor identity mismatch for {descriptor_id!r}"
                )
            anchors[f"{kind.value}:{descriptor_id}"] = loaded

    method_handles = load_builtin_method_handles(
        snapshot,
        verified_loader=lambda implementation_identity: entry_point_loader(
            implementation_identity.implementation,
            verified,
            expected_kind=DescriptorKind.METHOD,
            expected_id=implementation_identity.descriptor_id,
            expected_parameters=("job", "artifact_root"),
        ),
    )
    return LoadedBuiltinRegistry(
        snapshot=snapshot,
        method_handles=method_handles,
        descriptor_anchors=MappingProxyType(anchors),
    )


__all__ = [
    "BUILTIN_METHOD_IDS",
    "ImplementationDistributionIdentity",
    "LoadedBuiltinRegistry",
    "agent_system_handler_anchor",
    "agent_system_target_anchor",
    "build_builtin_registry",
    "load_builtin_method_handles",
    "load_verified_builtin_registry",
    "parametric_memory_handler_anchor",
    "parametric_memory_target_anchor",
    "skill_bundle_handler_anchor",
    "skill_bundle_target_anchor",
    "text_memory_handler_anchor",
    "text_memory_target_anchor",
]
