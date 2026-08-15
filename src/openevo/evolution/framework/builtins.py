"""Deterministic descriptors for OpenEvo's built-in evolution implementations.

Legacy methods retain their behavior-preserving dispatch during A2 migration.
New methods use the context ABI and are loaded only from verified entry points.
Targets retain non-executable identity anchors; target handlers are verified
callables used by the A2.4 runtime projection boundary.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
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
    MethodInvocationABI,
    ProjectConfigInjectionSource,
    RendererKind,
    _Contract,
    _digest,
    _distribution_name,
    _distribution_version,
)
from .descriptors import (
    EvolutionMethodDescriptor,
    ProjectConfigInjection,
    EvolutionSelectionResolverDescriptor,
    EvolutionTargetDescriptor,
    TargetHandlerDescriptor,
)
from .execution import EvolutionMethodHandle, MethodInputBinding
from .handlers import EvolutionTargetHandler
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
    "text_memory_memevolve",
    "skill_bundle",
    "skill_bundle_reflector",
    "agent_system",
    "agent_system_reflector",
    "agent_system_history_reflector",
    "agent_system_pareto_reflector",
    "agent_system_gepa_reflector",
    "parametric_memory_register",
    "parametric_memory_sd_lora",
)

_METHODS_MODULE = "openevo.evolution.methods"
_MEMEVOLVE_MODULE = "openevo.evolution.memevolve"
_SD_LORA_MODULE = "openevo.evolution.parametric.sd_lora"
_BUILTINS_MODULE = "openevo.evolution.framework.builtins"
_BUILTIN_HANDLERS_MODULE = "openevo.evolution.framework.builtin_handlers"
_BOTH_EXECUTION_MODES = (
    ExecutionMode.SUBSCRIPTION,
    ExecutionMode.SELF_DEPLOYED,
)
_SELF_DEPLOYED = (ExecutionMode.SELF_DEPLOYED,)
_TEXT_AND_TOKEN_CAPTURE = (CaptureMode.TRANSCRIPT, CaptureMode.TOKEN_LEVEL)
_CODEX = ("codex",)
_REFLECTOR_LLM_PROJECT_CONFIG_INJECTIONS = (
    ProjectConfigInjection(
        field_name="reflector_llm",
        source=ProjectConfigInjectionSource.REFLECTOR_LLM,
    ),
)
_AGENT_MODEL_PROJECT_CONFIG_INJECTIONS = (
    ProjectConfigInjection(
        field_name="base_model",
        source=ProjectConfigInjectionSource.AGENT_MODEL,
    ),
)


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


_VERIFIED_REGISTRY_SEAL = object()


@dataclass(frozen=True, slots=True, init=False)
class VerifiedExecutableRegistry:
    """Frozen catalog plus the exact handles proven against its distribution."""

    snapshot: RegistrySnapshot
    method_handles: Mapping[str, EvolutionMethodHandle]
    handler_handles: Mapping[str, EvolutionTargetHandler]
    descriptor_anchors: Mapping[str, DescriptorImplementationAnchor]
    distribution_attestations: Mapping[str, VerifiedDistribution]
    _verification_seal: object = field(repr=False, compare=False)

    def __new__(cls, *_args: object, **_kwargs: object) -> VerifiedExecutableRegistry:
        raise TypeError("VerifiedExecutableRegistry is issued only by a verified registry loader")

    def __post_init__(self) -> None:
        expected_methods = set(self.snapshot.methods)
        if set(self.method_handles) != expected_methods:
            raise ValueError("executable method handles do not match the frozen registry")
        if any(not callable(handle) for handle in self.method_handles.values()):
            raise TypeError("executable method handles must be callable")
        expected_handlers = set(self.snapshot.target_handlers)
        if set(self.handler_handles) != expected_handlers:
            raise ValueError("executable handler handles do not match the frozen registry")
        if any(not callable(handle) for handle in self.handler_handles.values()):
            raise TypeError("executable handler handles must be callable")

        attestations = dict(self.distribution_attestations)
        for digest, attestation in attestations.items():
            if digest != attestation.expectation.distribution_digest:
                raise ValueError("distribution attestation key does not match its digest")
        expected_distribution_digests = {
            identity.implementation.distribution_digest
            for identity in self.snapshot.identities.values()
        }
        if not expected_distribution_digests.issubset(attestations):
            raise ValueError("registry implementation is missing a distribution attestation")
        if set(attestations) != expected_distribution_digests:
            raise ValueError("distribution attestations do not match the frozen registry")
        for identity in self.snapshot.identities.values():
            implementation = identity.implementation
            attestation = attestations.get(implementation.distribution_digest)
            if attestation is None:
                raise ValueError("registry implementation is missing a distribution attestation")
            expectation = attestation.expectation
            if (
                expectation.distribution != implementation.distribution
                or expectation.distribution_version != implementation.distribution_version
                or expectation.distribution_digest != implementation.distribution_digest
            ):
                raise ValueError(
                    "registry implementation does not match its distribution attestation"
                )

        expected_anchors = {
            f"target:{descriptor_id}": (DescriptorKind.TARGET, descriptor_id)
            for descriptor_id in self.snapshot.targets
        }
        if set(self.descriptor_anchors) != set(expected_anchors):
            raise ValueError("descriptor anchors do not match the frozen target registry")
        for key, (kind, descriptor_id) in expected_anchors.items():
            anchor = self.descriptor_anchors[key]
            if anchor.descriptor_kind is not kind or anchor.descriptor_id != descriptor_id:
                raise ValueError("descriptor anchor identity does not match its registry key")

        object.__setattr__(
            self,
            "method_handles",
            MappingProxyType(dict(self.method_handles)),
        )
        object.__setattr__(
            self,
            "handler_handles",
            MappingProxyType(dict(self.handler_handles)),
        )
        object.__setattr__(
            self,
            "descriptor_anchors",
            MappingProxyType(dict(self.descriptor_anchors)),
        )
        object.__setattr__(
            self,
            "distribution_attestations",
            MappingProxyType(attestations),
        )


def _publish_verified_executable_registry(
    *,
    snapshot: RegistrySnapshot,
    method_handles: Mapping[str, EvolutionMethodHandle],
    handler_handles: Mapping[str, EvolutionTargetHandler],
    descriptor_anchors: Mapping[str, DescriptorImplementationAnchor],
    distribution_attestations: Mapping[str, VerifiedDistribution],
) -> VerifiedExecutableRegistry:
    registry = object.__new__(VerifiedExecutableRegistry)
    object.__setattr__(registry, "snapshot", snapshot)
    object.__setattr__(registry, "method_handles", method_handles)
    object.__setattr__(registry, "handler_handles", handler_handles)
    object.__setattr__(registry, "descriptor_anchors", descriptor_anchors)
    object.__setattr__(
        registry,
        "distribution_attestations",
        distribution_attestations,
    )
    object.__setattr__(registry, "_verification_seal", _VERIFIED_REGISTRY_SEAL)
    registry.__post_init__()
    return registry


def require_verified_executable_registry(
    registry: VerifiedExecutableRegistry,
) -> VerifiedExecutableRegistry:
    """Reject executable registries not published by the verified loader."""

    if (
        type(registry) is not VerifiedExecutableRegistry
        or getattr(registry, "_verification_seal", None) is not _VERIFIED_REGISTRY_SEAL
    ):
        raise TypeError("executable registry was not issued by a verified registry loader")
    return registry


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


def _bounded_number(
    *,
    minimum: float | None = None,
    exclusive_minimum: float | None = None,
    maximum: float,
) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "number", "maximum": maximum}
    if minimum is not None:
        schema["minimum"] = minimum
    if exclusive_minimum is not None:
        schema["exclusiveMinimum"] = exclusive_minimum
    return schema


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


def _memevolve_schema() -> dict[str, Any]:
    return _closed_object(
        {
            "candidate_count": {
                "type": "integer",
                "minimum": 2,
                "maximum": 5,
            },
            "max_records": _positive_integer(maximum=100),
            "reflector_llm": _closed_object(
                {
                    "model": _string(),
                    "provider": {
                        "type": "string",
                        "enum": ["codex_cli"],
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "exclusiveMinimum": 0.0,
                        "maximum": 86_400.0,
                    },
                },
                required=("model", "provider"),
            ),
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


def _sd_lora_schema() -> dict[str, Any]:
    return _closed_object(
        {
            "base_model": _string(maximum=193),
            "model_revision": {
                "type": "string",
                "minLength": 40,
                "maxLength": 64,
            },
            "rank": _positive_integer(maximum=128),
            "target_modules": _string_array(maximum=128),
            "learning_rate": _bounded_number(exclusive_minimum=0.0, maximum=1.0),
            "coefficient_learning_rate": _bounded_number(
                exclusive_minimum=0.0,
                maximum=1.0,
            ),
            "weight_decay": _bounded_number(minimum=0.0, maximum=1.0),
            "epochs": _positive_integer(maximum=100),
            "max_steps": _positive_integer(maximum=1_000_000),
            "max_length": {
                "type": "integer",
                "minimum": 32,
                "maximum": 131_072,
            },
            "max_records": _positive_integer(maximum=100_000),
            "replay_capacity": _positive_integer(maximum=100_000),
            "per_device_train_batch_size": _positive_integer(maximum=128),
            "gradient_accumulation_steps": _positive_integer(maximum=4096),
            "max_grad_norm": _bounded_number(
                exclusive_minimum=0.0,
                maximum=1_000.0,
            ),
            "dtype": {
                "type": "string",
                "enum": ["bfloat16", "float16", "float32"],
            },
            "load_in_4bit": {"type": "boolean"},
            "gradient_checkpointing": {"type": "boolean"},
            "coefficient_init": _bounded_number(
                exclusive_minimum=0.0,
                maximum=100.0,
            ),
            "minimum_reward": _bounded_number(
                minimum=-1_000_000.0,
                maximum=1_000_000.0,
            ),
            "seed": {
                "type": "integer",
                "minimum": 0,
                "maximum": 9_007_199_254_740_991,
            },
            "timeout_seconds": _bounded_number(
                exclusive_minimum=0.0,
                maximum=86_400.0,
            ),
        },
        required=("base_model", "model_revision"),
    )


def _current_dataset() -> MethodInputBinding:
    return MethodInputBinding(
        binding_id="current_dataset",
        source="current_dataset",
        artifact_type="dataset",
        min_count=1,
        max_count=1,
    )


def _optional_current_dataset() -> MethodInputBinding:
    return MethodInputBinding(
        binding_id="current_dataset",
        source="current_dataset",
        artifact_type="dataset",
        min_count=0,
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


def _single_prior_target(artifact_type: str) -> MethodInputBinding:
    return MethodInputBinding(
        binding_id="prior_target_artifacts",
        source="current_target_artifacts",
        artifact_type=artifact_type,
        max_count=1,
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
            "parametric_memory_sd_lora",
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
            selection_resolvers=(
                (
                    EvolutionSelectionResolverDescriptor(
                        selection_value="auto",
                        display_name="Automatic",
                        description=(
                            "Select the agent-system reflector from prior-round "
                            "dataset availability."
                        ),
                        resolved_method_ids=(
                            "agent_system_reflector",
                            "agent_system_history_reflector",
                        ),
                    ),
                )
                if target_id == "agent_system"
                else ()
            ),
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
            instruction_preamble=("Use the following long-term memory for this task:"),
            allowed_uri_schemes=("file",),
            allowed_media_types=("text/markdown", "text/plain"),
            allowed_destination_scopes=(DestinationScope.TARGET_DATA,),
            environment_allowlist=(
                "OPENEVO_MEMORY_FILE",
                "OPENEVO_MEMORY_RUNTIME_CONTROL",
            ),
            allowed_contribution_kinds=(
                ContributionKind.INSTRUCTION,
                ContributionKind.STAGED_PAYLOAD,
                ContributionKind.ENVIRONMENT,
            ),
            exposure=Exposure.DESKTOP,
            implementation_ref=identity.ref(f"{_BUILTIN_HANDLERS_MODULE}:text_memory_handler"),
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
            allowed_destination_scopes=(
                DestinationScope.HARNESS_SKILLS,
                DestinationScope.TARGET_DATA,
            ),
            environment_allowlist=(
                "OPENEVO_SKILLS_DIR",
                "OPENEVO_SKILL_RUNTIME_CONTROL",
            ),
            allowed_contribution_kinds=(
                ContributionKind.STAGED_PAYLOAD,
                ContributionKind.ENVIRONMENT,
            ),
            exposure=Exposure.DESKTOP,
            implementation_ref=identity.ref(f"{_BUILTIN_HANDLERS_MODULE}:skill_bundle_handler"),
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
                "OPENEVO_AGENT_SYSTEM_RUNTIME_CONTROL",
            ),
            allowed_contribution_kinds=(
                ContributionKind.STAGED_PAYLOAD,
                ContributionKind.ENVIRONMENT,
            ),
            exposure=Exposure.DESKTOP,
            implementation_ref=identity.ref(f"{_BUILTIN_HANDLERS_MODULE}:agent_system_handler"),
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
                f"{_BUILTIN_HANDLERS_MODULE}:parametric_memory_handler"
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
    project_config_injections: tuple[ProjectConfigInjection, ...] = (),
    invocation_abi: MethodInvocationABI = MethodInvocationABI.LEGACY_WORKER_JOB_V1,
    implementation_module: str = _METHODS_MODULE,
) -> EvolutionMethodDescriptor:
    return EvolutionMethodDescriptor(
        id=method_id,
        display_name=display_name,
        description=description,
        target_id=target_id,
        invocation_abi=invocation_abi,
        execution_modes=execution_modes,
        capture_modes=_TEXT_AND_TOKEN_CAPTURE,
        supported_harness_ids=_CODEX,
        runtime_requirements=runtime_requirements,
        input_bindings=input_bindings,
        output_artifact_types=output_artifact_types,
        config_schema=config_schema,
        default_config=default_config or {},
        project_config_injections=project_config_injections,
        exposure=exposure,
        maturity=Maturity.EXPERIMENTAL,
        implementation_ref=identity.ref(f"{implementation_module}:{method_id}"),
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
            project_config_injections=_REFLECTOR_LLM_PROJECT_CONFIG_INJECTIONS,
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
            project_config_injections=_REFLECTOR_LLM_PROJECT_CONFIG_INJECTIONS,
        ),
        _method(
            identity,
            method_id="text_memory_memevolve",
            display_name="MemEvolve textual adaptation",
            description=(
                "Evolve and select declarative Markdown memory candidates without "
                "executing generated provider code."
            ),
            target_id="text_memory",
            execution_modes=_BOTH_EXECUTION_MODES,
            input_bindings=(
                _ordered_dataset_inputs(),
                _prior_target("text_memory"),
            ),
            output_artifact_types=("text_memory",),
            config_schema=_memevolve_schema(),
            default_config={"candidate_count": 3, "max_records": 20},
            project_config_injections=_REFLECTOR_LLM_PROJECT_CONFIG_INJECTIONS,
            invocation_abi=MethodInvocationABI.METHOD_CONTEXT_V1,
            implementation_module=_MEMEVOLVE_MODULE,
        ),
        _method(
            identity,
            method_id="skill_bundle",
            display_name="Skill bundle materializer",
            description="Register configured Markdown as a skill bundle.",
            target_id="skill_bundle",
            execution_modes=_SELF_DEPLOYED,
            input_bindings=(
                _optional_current_dataset(),
                _prior_target("skill_bundle"),
            ),
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
            project_config_injections=_REFLECTOR_LLM_PROJECT_CONFIG_INJECTIONS,
        ),
        _method(
            identity,
            method_id="agent_system",
            display_name="Agent system materializer",
            description="Register configured Markdown as harness instructions.",
            target_id="agent_system",
            execution_modes=_SELF_DEPLOYED,
            input_bindings=(
                _optional_current_dataset(),
                _prior_target("agent_system"),
            ),
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
            project_config_injections=_REFLECTOR_LLM_PROJECT_CONFIG_INJECTIONS,
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
            project_config_injections=_REFLECTOR_LLM_PROJECT_CONFIG_INJECTIONS,
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
            project_config_injections=_REFLECTOR_LLM_PROJECT_CONFIG_INJECTIONS,
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
            project_config_injections=_REFLECTOR_LLM_PROJECT_CONFIG_INJECTIONS,
        ),
        _method(
            identity,
            method_id="parametric_memory_register",
            display_name="Parametric memory register",
            description="Register one prebuilt model adapter artifact.",
            target_id="parametric_memory",
            execution_modes=_SELF_DEPLOYED,
            input_bindings=(
                _optional_current_dataset(),
                _prior_target("parametric_memory"),
            ),
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
            project_config_injections=_AGENT_MODEL_PROJECT_CONFIG_INJECTIONS,
        ),
        _method(
            identity,
            method_id="parametric_memory_sd_lora",
            display_name="SD-LoRA continual parametric memory",
            description=(
                "Train one new SD-LoRA component with bounded trajectory replay and export "
                "one cumulative PEFT adapter."
            ),
            target_id="parametric_memory",
            execution_modes=_SELF_DEPLOYED,
            input_bindings=(
                _current_dataset(),
                _single_prior_target("parametric_memory"),
            ),
            output_artifact_types=("parametric_memory",),
            config_schema=_sd_lora_schema(),
            default_config={
                "rank": 8,
                "target_modules": ["q_proj", "k_proj", "v_proj", "o_proj"],
                "learning_rate": 0.0002,
                "coefficient_learning_rate": 0.01,
                "weight_decay": 0.0,
                "epochs": 1,
                "max_length": 2048,
                "max_records": 256,
                "replay_capacity": 64,
                "per_device_train_batch_size": 1,
                "gradient_accumulation_steps": 1,
                "max_grad_norm": 1.0,
                "dtype": "bfloat16",
                "load_in_4bit": False,
                "gradient_checkpointing": True,
                "coefficient_init": 0.8,
                "minimum_reward": 0.5,
                "seed": 1993,
                "timeout_seconds": 3600.0,
            },
            exposure=Exposure.INTERNAL,
            runtime_requirements=(
                "adapter_serving",
                "gpu",
                "sd_lora_continual_trainer",
            ),
            project_config_injections=_AGENT_MODEL_PROJECT_CONFIG_INJECTIONS,
            invocation_abi=MethodInvocationABI.METHOD_CONTEXT_V1,
            implementation_module=_SD_LORA_MODULE,
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
) -> Mapping[str, EvolutionMethodHandle]:
    """Load context methods and preserve anti-drift checks for legacy dispatch."""

    expected = set(BUILTIN_METHOD_IDS)
    actual_descriptors = set(snapshot.methods)
    if actual_descriptors != expected:
        missing = sorted(expected - actual_descriptors)
        extra = sorted(actual_descriptors - expected)
        raise ValueError(f"built-in method key mismatch; missing={missing!r}, extra={extra!r}")

    loaded_handles: dict[str, object] = {}
    for method_id in BUILTIN_METHOD_IDS:
        identity = snapshot.identity_for(DescriptorKind.METHOD, method_id)
        loaded_handles[method_id] = verified_loader(identity)

    legacy_method_ids = {
        method_id
        for method_id, descriptor in snapshot.methods.items()
        if descriptor.invocation_abi is MethodInvocationABI.LEGACY_WORKER_JOB_V1
    }

    from openevo.evolution.methods import METHOD_REGISTRY

    actual_registry = set(METHOD_REGISTRY)
    if actual_registry != legacy_method_ids:
        missing = sorted(legacy_method_ids - actual_registry)
        extra = sorted(actual_registry - legacy_method_ids)
        raise ValueError(f"built-in method key mismatch; missing={missing!r}, extra={extra!r}")

    handles: dict[str, EvolutionMethodHandle] = {}
    for method_id in BUILTIN_METHOD_IDS:
        loaded = loaded_handles[method_id]
        if not callable(loaded):
            raise ValueError(f"built-in method entry point is not callable for {method_id!r}")
        if method_id in legacy_method_ids:
            expected_callable = METHOD_REGISTRY[method_id]
            if loaded is not expected_callable:
                raise ValueError(f"built-in method callable identity mismatch for {method_id!r}")
            handles[method_id] = expected_callable
        else:
            handles[method_id] = loaded
    return MappingProxyType(handles)


def load_builtin_handler_handles(
    snapshot: RegistrySnapshot,
    *,
    verified_loader: Callable[[ImplementationIdentity], object],
) -> Mapping[str, EvolutionTargetHandler]:
    """Load the exact built-in target handlers from verified entry points."""

    from . import builtin_handlers

    expected = set(snapshot.target_handlers)
    actual_registry = set(builtin_handlers.BUILTIN_HANDLER_REGISTRY)
    if actual_registry != expected:
        missing = sorted(expected - actual_registry)
        extra = sorted(actual_registry - expected)
        raise ValueError(f"built-in handler key mismatch; missing={missing!r}, extra={extra!r}")

    handles: dict[str, EvolutionTargetHandler] = {}
    for handler_id in sorted(expected):
        identity = snapshot.identity_for(DescriptorKind.TARGET_HANDLER, handler_id)
        loaded = verified_loader(identity)
        expected_callable = builtin_handlers.BUILTIN_HANDLER_REGISTRY[handler_id]
        if loaded is not expected_callable:
            raise ValueError(f"built-in handler callable identity mismatch for {handler_id!r}")
        handles[handler_id] = expected_callable
    return MappingProxyType(handles)


def load_verified_builtin_registry(
    verified: VerifiedDistribution,
) -> VerifiedExecutableRegistry:
    """Build the catalog and verify every built-in entry point before use."""

    expectation = verified.expectation
    identity = ImplementationDistributionIdentity(
        distribution=expectation.distribution,
        distribution_version=expectation.distribution_version,
        distribution_digest=expectation.distribution_digest,
    )
    snapshot = build_builtin_registry(identity)

    anchors: dict[str, DescriptorImplementationAnchor] = {}
    for descriptor_id in snapshot.targets:
        implementation_identity = snapshot.identity_for(
            DescriptorKind.TARGET,
            descriptor_id,
        )
        loaded = load_verified_entry_point(
            implementation_identity.implementation,
            verified,
            expected_kind=DescriptorKind.TARGET,
            expected_id=descriptor_id,
        )
        if not isinstance(loaded, DescriptorImplementationAnchor):
            raise ValueError(f"built-in descriptor anchor identity mismatch for {descriptor_id!r}")
        anchors[f"target:{descriptor_id}"] = loaded

    handler_handles = load_builtin_handler_handles(
        snapshot,
        verified_loader=lambda implementation_identity: load_verified_entry_point(
            implementation_identity.implementation,
            verified,
            expected_kind=DescriptorKind.TARGET_HANDLER,
            expected_id=implementation_identity.descriptor_id,
        ),
    )

    method_handles = load_builtin_method_handles(
        snapshot,
        verified_loader=lambda implementation_identity: load_verified_entry_point(
            implementation_identity.implementation,
            verified,
            expected_kind=DescriptorKind.METHOD,
            expected_id=implementation_identity.descriptor_id,
            invocation_abi=snapshot.methods[implementation_identity.descriptor_id].invocation_abi,
        ),
    )
    return _publish_verified_executable_registry(
        snapshot=snapshot,
        method_handles=method_handles,
        handler_handles=handler_handles,
        descriptor_anchors=MappingProxyType(anchors),
        distribution_attestations={
            verified.expectation.distribution_digest: verified,
        },
    )


__all__ = [
    "BUILTIN_METHOD_IDS",
    "ImplementationDistributionIdentity",
    "VerifiedExecutableRegistry",
    "agent_system_target_anchor",
    "build_builtin_registry",
    "load_builtin_handler_handles",
    "load_builtin_method_handles",
    "load_verified_builtin_registry",
    "require_verified_executable_registry",
    "parametric_memory_target_anchor",
    "skill_bundle_target_anchor",
    "text_memory_target_anchor",
]
