from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from openevo.evolution.framework import (
    EvolutionCapabilitiesV1,
    EvolutionFrameworkRegistry,
    EvolutionMethodDescriptor,
    EvolutionTargetDescriptor,
    ImplementationRef,
    MethodInputBinding,
    ProjectConfigInjection,
    TargetHandlerDescriptor,
    build_evolution_capabilities,
    execution_profile_for_release_mode,
)


def _implementation(name: str, digit: str) -> ImplementationRef:
    return ImplementationRef(
        distribution="openevo-test",
        distribution_version="1",
        distribution_digest=digit * 64,
        entry_point=f"openevo_test.{name}:implementation",
    )


def _snapshot():
    registry = EvolutionFrameworkRegistry()
    registry.register_target(
        EvolutionTargetDescriptor(
            id="memory",
            exposure="desktop",
            display_name="Memory",
            description="Reusable memory",
            artifact_type="text_memory",
            handler_id="memory_handler",
            renderer_kind="markdown",
            default_method_id="reflect",
            implementation_ref=_implementation("target", "1"),
        )
    )
    registry.register_target_handler(
        TargetHandlerDescriptor(
            id="memory_handler",
            exposure="desktop",
            target_id="memory",
            artifact_types=("text_memory",),
            renderer_kind="markdown",
            allowed_uri_schemes=("file",),
            allowed_media_types=("text/markdown",),
            allowed_destination_scopes=("target_data",),
            allowed_contribution_kinds=("instruction",),
            implementation_ref=_implementation("handler", "2"),
        )
    )
    for method_id, runtime_requirements, digit in (
        ("reflect", (), "3"),
        ("gpu_reflect", ("gpu",), "4"),
    ):
        registry.register_method(
            EvolutionMethodDescriptor(
                id=method_id,
                exposure="desktop",
                display_name=method_id,
                description=f"{method_id} method",
                target_id="memory",
                invocation_abi="method_context_v1",
                execution_modes=(
                    ("subscription",)
                    if method_id == "reflect"
                    else ("subscription", "self_deployed")
                ),
                capture_modes=("transcript",),
                supported_harness_ids=("codex",),
                harness_requirements=("stable_transcript",),
                runtime_requirements=runtime_requirements,
                input_bindings=(
                    MethodInputBinding(
                        binding_id="dataset",
                        source="current_dataset",
                        artifact_type="dataset",
                        min_count=1,
                    ),
                ),
                output_artifact_types=("text_memory",),
                config_schema=(
                    {
                        "type": "object",
                        "properties": {
                            "operator": {"type": "string"},
                            "runtime_context": {"type": "string"},
                        },
                        "required": ["operator", "runtime_context"],
                        "additionalProperties": False,
                    }
                    if method_id == "reflect"
                    else {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    }
                ),
                project_config_injections=(
                    (
                        ProjectConfigInjection(
                            field_name="runtime_context",
                            source="agent_model",
                        ),
                    )
                    if method_id == "reflect"
                    else ()
                ),
                implementation_ref=_implementation(method_id, digit),
            )
        )
    return registry.freeze()


def test_release_modes_map_once_to_generic_execution_profile() -> None:
    subscription = execution_profile_for_release_mode(
        "codex_subscription_transcript",
        harness_capabilities=("stable_transcript",),
    )
    assert subscription.execution_mode.value == "subscription"
    assert subscription.capture_mode.value == "transcript"
    assert subscription.harness_id == "codex"

    self_deployed = execution_profile_for_release_mode("self-deployed")
    assert self_deployed.execution_mode.value == "self_deployed"
    assert self_deployed.capture_mode.value == "transcript"


def test_capabilities_are_versioned_registry_projection_with_four_axis_support() -> None:
    snapshot = _snapshot()
    profile = execution_profile_for_release_mode(
        "codex_subscription_transcript",
        harness_capabilities=("stable_transcript",),
    )
    capabilities = build_evolution_capabilities(
        snapshot,
        profile=profile,
        audience="desktop",
        core_version="0.1.0",
    )

    assert capabilities.schema_version == "1"
    assert capabilities.core_version == "0.1.0"
    assert capabilities.registry_digest == snapshot.registry_digest
    assert len(capabilities.targets) == 1
    target = capabilities.targets[0]
    assert target.target_id == "memory"
    assert target.renderer_kind.value == "markdown"
    assert target.renderer_contract_version == "1"
    assert target.configured_default_method_id == "reflect"
    assert target.effective_default_method_id == "reflect"
    assert [method.method_id for method in target.methods] == [
        "gpu_reflect",
        "reflect",
    ]

    gpu_support = target.methods[0].support
    assert gpu_support.overall.value == "unavailable"
    assert gpu_support.execution.state.value == "supported"
    assert gpu_support.capture.state.value == "supported"
    assert gpu_support.harness.state.value == "supported"
    assert gpu_support.runtime.state.value == "unavailable"
    assert gpu_support.runtime.reason_code == "missing_runtime_capabilities"
    assert gpu_support.runtime.missing_requirements == ("gpu",)

    supported = target.methods[1]
    assert supported.support.overall.value == "supported"
    assert supported.input_bindings[0].binding_id == "dataset"
    assert supported.config_schema_json.startswith("{")
    assert json.loads(supported.config_schema_json) == {
        "additionalProperties": False,
        "properties": {"operator": {"type": "string"}},
        "required": ["operator"],
        "type": "object",
    }
    assert supported.implementation_identity_digest


def test_capability_projection_keeps_unsupported_method_visible_with_reason() -> None:
    profile = execution_profile_for_release_mode("self-deployed")
    payload = build_evolution_capabilities(
        _snapshot(),
        profile=profile.model_copy(update={"harness_id": "claude"}),
        audience="desktop",
        core_version="0.1.0",
    )
    method = payload.targets[0].methods[0]
    assert method.support.overall.value == "unsupported"
    assert method.support.harness.reason_code == "unsupported_harness"


def test_unsupported_configured_default_is_not_published_as_effective() -> None:
    payload = build_evolution_capabilities(
        _snapshot(),
        profile=execution_profile_for_release_mode("self-deployed"),
        audience="desktop",
        core_version="0.1.0",
    )
    target = payload.targets[0]
    assert target.configured_default_method_id == "reflect"
    assert target.effective_default_method_id is None
    assert target.configured_default_support.overall.value == "unsupported"


def test_wire_projection_cannot_substitute_another_supported_default() -> None:
    capabilities = build_evolution_capabilities(
        _snapshot(),
        profile=execution_profile_for_release_mode(
            "codex_subscription_transcript",
            harness_capabilities=("stable_transcript",),
        ),
        audience="desktop",
        core_version="0.1.0",
    )
    payload = capabilities.model_dump(mode="python")
    payload["targets"][0]["effective_default_method_id"] = "gpu_reflect"
    with pytest.raises(ValidationError, match="cannot replace configured default"):
        EvolutionCapabilitiesV1.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "encoded", "message"),
    [
        (
            "config_schema_json",
            '{"$ref":"#","additionalProperties":false,"properties":{},"type":"object"}',
            "unsupported schema keyword",
        ),
        (
            "default_config_json",
            '{"unknown":true}',
            "unknown property",
        ),
        (
            "config_schema_json",
            (
                '{"additionalProperties":false,"properties":{"count":'
                '{"maximum":9007199254740992,"type":"integer"}},"type":"object"}'
            ),
            "JavaScript safe integer range",
        ),
        (
            "default_config_json",
            '{"count":9007199254740992}',
            "JavaScript safe integer range",
        ),
        (
            "config_schema_json",
            (
                '{"additionalProperties":false,"properties":{"count":'
                '{"maximum":9007199254740992.0,"type":"number"}},"type":"object"}'
            ),
            "JavaScript safe integer range",
        ),
        (
            "default_config_json",
            '{"count":9007199254740992.0}',
            "JavaScript safe integer range",
        ),
    ],
)
def test_capability_config_contract_rejects_unrenderable_payloads(
    field: str,
    encoded: str,
    message: str,
) -> None:
    capabilities = build_evolution_capabilities(
        _snapshot(),
        profile=execution_profile_for_release_mode(
            "codex_subscription_transcript",
            harness_capabilities=("stable_transcript",),
        ),
        audience="desktop",
        core_version="0.1.0",
    )
    payload = capabilities.model_dump(mode="python")
    method = next(
        item
        for item in payload["targets"][0]["methods"]
        if item["method_id"] == "reflect"
    )
    if field == "default_config_json" and "count" in encoded:
        method["config_schema_json"] = (
            '{"additionalProperties":false,"properties":{"count":'
            '{"type":"integer"}},"type":"object"}'
        )
    method[field] = encoded

    with pytest.raises(ValidationError, match=message):
        EvolutionCapabilitiesV1.model_validate(payload)


def test_capability_payload_rejects_oversized_profile_collections() -> None:
    capabilities = build_evolution_capabilities(
        _snapshot(),
        profile=execution_profile_for_release_mode(
            "codex_subscription_transcript",
            harness_capabilities=("stable_transcript",),
        ),
        audience="desktop",
        core_version="0.1.0",
    )
    payload = capabilities.model_dump(mode="python")
    payload["evaluated_profile"]["harness_capabilities"] = [
        f"capability_{index}" for index in range(257)
    ]

    with pytest.raises(ValidationError, match="at most 256 items"):
        EvolutionCapabilitiesV1.model_validate(payload)


def test_capability_payload_rejects_oversized_support_text() -> None:
    capabilities = build_evolution_capabilities(
        _snapshot(),
        profile=execution_profile_for_release_mode(
            "codex_subscription_transcript",
            harness_capabilities=("stable_transcript",),
        ),
        audience="desktop",
        core_version="0.1.0",
    )
    payload = capabilities.model_dump(mode="python")
    payload["targets"][0]["methods"][0]["support"]["runtime"]["message"] = (
        "x" * 4097
    )

    with pytest.raises(ValidationError, match="at most 4096 characters"):
        EvolutionCapabilitiesV1.model_validate(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["targets"][0].__setitem__(
            "context_order", 9_007_199_254_740_992
        ),
        lambda payload: payload["targets"][0]["methods"][1]["input_bindings"][
            0
        ].__setitem__("min_count", 9_007_199_254_740_992),
    ],
    ids=["context-order", "input-binding-count"],
)
def test_capability_payload_rejects_outer_unsafe_integers(mutate) -> None:
    capabilities = build_evolution_capabilities(
        _snapshot(),
        profile=execution_profile_for_release_mode(
            "codex_subscription_transcript",
            harness_capabilities=("stable_transcript",),
        ),
        audience="desktop",
        core_version="0.1.0",
    )
    payload = capabilities.model_dump(mode="python")
    mutate(payload)

    with pytest.raises(ValidationError, match="JavaScript safe range"):
        EvolutionCapabilitiesV1.model_validate(payload)


def test_capability_payload_rejects_single_collection_amplification() -> None:
    capabilities = build_evolution_capabilities(
        _snapshot(),
        profile=execution_profile_for_release_mode(
            "codex_subscription_transcript",
            harness_capabilities=("stable_transcript",),
        ),
        audience="desktop",
        core_version="0.1.0",
    )
    payload = capabilities.model_dump(mode="python")
    payload["evaluated_profile"]["runtime_capabilities"] = [
        f"runtime_{index}" for index in range(4097)
    ]

    with pytest.raises(ValidationError, match="collection is too large"):
        EvolutionCapabilitiesV1.model_validate(payload)
