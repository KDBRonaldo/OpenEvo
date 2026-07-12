from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
import subprocess
import sys

import pytest

from openevo.evolution import methods
from openevo.evolution.framework import builtins
from openevo.evolution.framework.builtins import (
    BUILTIN_METHOD_IDS,
    ImplementationDistributionIdentity,
    build_builtin_registry,
    load_builtin_method_handles,
    load_verified_builtin_registry,
)
from openevo.evolution.framework.contracts import (
    DescriptorKind,
    EvolutionExecutionProfile,
    Exposure,
    ImplementationIdentity,
    Maturity,
)
from openevo.evolution.framework.execution import (
    CORE_CONFIG_RESERVED_KEYS,
    MethodExecutionContext,
    MethodExecutionServices,
    build_execution_envelope,
    invoke_legacy_method,
    resolve_method_inputs,
)
from openevo.evolution.framework.loading import (
    DistributionArtifactExpectation,
    VerifiedDistribution,
)
from openevo.evolution.framework.registry import RegistrySnapshot
from openevo.evolution.framework.support import (
    MethodSupportOverall,
    evaluate_method_support,
)
from openevo.evolution.models import WorkerClaimInputArtifact, WorkerClaimedJob


TARGET_IDS = {
    "text_memory",
    "skill_bundle",
    "agent_system",
    "parametric_memory",
}
HANDLER_IDS = {f"{target_id}_handler" for target_id in TARGET_IDS}
METHOD_IDS = {
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
}
PROTECTED_METHOD_IDS = {
    "text_memory_expel_reflector",
    "skill_bundle_reflector",
    "agent_system_gepa_reflector",
}
REFLECTOR_METHOD_IDS = {
    "text_memory_reflector",
    "text_memory_expel_reflector",
    "skill_bundle_reflector",
    "agent_system_reflector",
    "agent_system_history_reflector",
    "agent_system_pareto_reflector",
    "agent_system_gepa_reflector",
}
METHODS_MODULE = "openevo.evolution.methods"
BUILTINS_MODULE = "openevo.evolution.framework.builtins"


@pytest.fixture(scope="module")
def distribution_identity() -> ImplementationDistributionIdentity:
    return ImplementationDistributionIdentity(
        distribution="openevo",
        distribution_version="0.1.0",
        distribution_digest="a" * 64,
    )


@pytest.fixture(scope="module")
def snapshot(
    distribution_identity: ImplementationDistributionIdentity,
) -> RegistrySnapshot:
    value = build_builtin_registry(distribution_identity)
    assert type(value) is RegistrySnapshot
    return value


def _entry_point_parts(entry_point: str) -> tuple[str, str]:
    module_name, separator, attribute_name = entry_point.partition(":")
    assert separator == ":"
    assert module_name
    assert attribute_name
    return module_name, attribute_name


def _schema_nodes(schema: dict) -> list[dict]:
    nodes = [schema]
    for child in schema.get("properties", {}).values():
        nodes.extend(_schema_nodes(child))
    if "items" in schema:
        nodes.extend(_schema_nodes(schema["items"]))
    for child in schema.get("anyOf", ()):  # Nullable schemas have one null leaf.
        nodes.extend(_schema_nodes(child))
    return nodes


def test_builtin_catalog_is_complete_frozen_and_has_expected_defaults(
    snapshot: RegistrySnapshot,
) -> None:
    assert frozenset(BUILTIN_METHOD_IDS) == frozenset(METHOD_IDS)
    assert frozenset(snapshot.targets) == frozenset(TARGET_IDS)
    assert frozenset(snapshot.target_handlers) == frozenset(HANDLER_IDS)
    assert frozenset(snapshot.methods) == frozenset(METHOD_IDS)

    assert snapshot.targets["text_memory"].default_method_id == (
        "text_memory_expel_reflector"
    )
    assert snapshot.targets["skill_bundle"].default_method_id == (
        "skill_bundle_reflector"
    )
    assert snapshot.targets["agent_system"].default_method_id == (
        "agent_system_gepa_reflector"
    )
    assert snapshot.targets["parametric_memory"].exposure is Exposure.INTERNAL
    assert all(
        snapshot.targets[target_id].exposure is Exposure.DESKTOP
        for target_id in TARGET_IDS - {"parametric_memory"}
    )

    with pytest.raises(TypeError):
        snapshot.identity_digests["method:text_memory"] = "b" * 64


def test_builtin_descriptors_use_exact_method_and_public_anchor_entry_points(
    snapshot: RegistrySnapshot,
) -> None:
    for method_id, descriptor in snapshot.methods.items():
        assert descriptor.implementation_ref is not None
        assert descriptor.implementation_ref.entry_point == (
            f"{METHODS_MODULE}:{method_id}"
        )

    target_anchor_names: set[str] = set()
    for descriptor in snapshot.targets.values():
        assert descriptor.implementation_ref is not None
        module_name, attribute_name = _entry_point_parts(
            descriptor.implementation_ref.entry_point
        )
        assert module_name == BUILTINS_MODULE
        assert not attribute_name.startswith("_")
        assert callable(getattr(builtins, attribute_name))
        target_anchor_names.add(attribute_name)

    handler_anchor_names: set[str] = set()
    for descriptor in snapshot.target_handlers.values():
        assert descriptor.implementation_ref is not None
        module_name, attribute_name = _entry_point_parts(
            descriptor.implementation_ref.entry_point
        )
        assert module_name == BUILTINS_MODULE
        assert not attribute_name.startswith("_")
        assert callable(getattr(builtins, attribute_name))
        handler_anchor_names.add(attribute_name)

    assert len(target_anchor_names) == 4
    assert len(handler_anchor_names) == 4
    assert target_anchor_names.isdisjoint(handler_anchor_names)


def test_builtin_output_and_protected_method_contracts(
    snapshot: RegistrySnapshot,
) -> None:
    assert "report" in snapshot.methods[
        "agent_system_pareto_reflector"
    ].output_artifact_types
    assert "report" in snapshot.methods[
        "agent_system_gepa_reflector"
    ].output_artifact_types
    assert snapshot.methods["parametric_memory_register"].input_bindings == ()
    assert "constrained_trainer_contract" in snapshot.methods[
        "parametric_memory_lora_sft"
    ].runtime_requirements

    for method_id in PROTECTED_METHOD_IDS:
        descriptor = snapshot.methods[method_id]
        assert descriptor.exposure is Exposure.DESKTOP
        assert descriptor.maturity is Maturity.EXPERIMENTAL


def test_builtin_method_schemas_are_closed_and_reject_unsafe_ownership(
    snapshot: RegistrySnapshot,
) -> None:
    unsafe_values = {
        **{key: "forged" for key in CORE_CONFIG_RESERVED_KEYS},
        "api_key": "raw-secret",
        "credential": "raw-secret",
        "endpoint": "https://untrusted.invalid/v1",
        "base_url": "https://untrusted.invalid/v1",
        "trainer_command": "python trainer.py",
        "trainer": {"command": "python trainer.py"},
    }

    for method_id, descriptor in snapshot.methods.items():
        for node in _schema_nodes(dict(descriptor.config_schema)):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False, method_id
        declared_fields = set(descriptor.config_schema.get("properties", {}))
        assert declared_fields.isdisjoint(CORE_CONFIG_RESERVED_KEYS), method_id
        assert declared_fields.isdisjoint(unsafe_values), method_id

        for field, value in unsafe_values.items():
            with pytest.raises(ValueError, match="invalid config"):
                snapshot.normalize_method_config(method_id, {field: value})


def test_reflector_catalog_requires_model_and_codex_harness_provider(
    snapshot: RegistrySnapshot,
) -> None:
    for method_id in REFLECTOR_METHOD_IDS:
        with pytest.raises(ValueError, match="required"):
            snapshot.normalize_method_config(method_id, {})
        with pytest.raises(ValueError, match="enum"):
            snapshot.normalize_method_config(
                method_id,
                {
                    "reflector_llm": {
                        "model": "gpt-5.5",
                        "provider": "openai_chat",
                    }
                },
            )

        normalized = snapshot.normalize_method_config(
            method_id,
            {
                "reflector_llm": {
                    "model": "gpt-5.5",
                    "provider": "codex_cli",
                }
            },
        )
        assert normalized["reflector_llm"] == {
            "model": "gpt-5.5",
            "provider": "codex_cli",
        }


def test_multi_dataset_bindings_preserve_each_legacy_callers_input_order(
    snapshot: RegistrySnapshot,
) -> None:
    expel = snapshot.methods["text_memory_expel_reflector"].input_bindings
    assert tuple((binding.source.value, binding.artifact_type) for binding in expel) == (
        ("explicit_inputs", "dataset"),
        ("current_target_artifacts", "text_memory"),
    )

    gepa = snapshot.methods["agent_system_gepa_reflector"].input_bindings
    assert tuple((binding.source.value, binding.artifact_type) for binding in gepa) == (
        ("explicit_inputs", "dataset"),
        ("current_target_artifacts", "agent_system"),
    )


@pytest.mark.parametrize(
    "dataset_ids",
    [
        ("current", "history-1", "history-2"),
        ("history-1", "history-2", "current"),
    ],
    ids=["experiment-current-first", "terminal-bench-history-first"],
)
def test_gepa_descriptor_and_legacy_adapter_keep_caller_dataset_order(
    snapshot: RegistrySnapshot,
    tmp_path: Path,
    dataset_ids: tuple[str, ...],
) -> None:
    descriptor = snapshot.methods["agent_system_gepa_reflector"]
    datasets = tuple(
        WorkerClaimInputArtifact(
            artifact_id=artifact_id,
            type="dataset",
            uri=f"file:///datasets/{artifact_id}.json",
        )
        for artifact_id in dataset_ids
    )
    prior = WorkerClaimInputArtifact(
        artifact_id="prior-agent-system",
        type="agent_system",
        uri="file:///artifacts/AGENTS.md",
    )
    resolution = resolve_method_inputs(
        descriptor.input_bindings,
        {
            "dataset_inputs": datasets,
            "prior_target_artifacts": (prior,),
        },
    )
    envelope = build_execution_envelope(
        plan_id="plan-gepa-order",
        target_id="agent_system",
        method_id="agent_system_gepa_reflector",
        user_config={
            "reflector_llm": {"model": "gpt-5.5", "provider": "codex_cli"}
        },
        core_config={},
        input_bindings=resolution.bindings,
    )
    observed: list[str] = []

    def legacy_method(job, artifact_root):
        assert artifact_root == tmp_path
        observed.extend(item.artifact_id for item in job.input_artifacts)
        return []

    context = MethodExecutionContext(
        job=WorkerClaimedJob(
            job_id="job-gepa-order",
            lease_id="lease-gepa-order",
            job_type="agent_system_gepa_reflector",
            method="agent_system_gepa_reflector",
            input_artifacts=list(resolution.input_artifacts),
        ),
        artifact_root=tmp_path,
        envelope=envelope,
        services=MethodExecutionServices(harness=object()),
    )

    assert invoke_legacy_method(legacy_method, context) == []
    assert observed == [*dataset_ids, "prior-agent-system"]


def test_incomplete_lora_method_remains_unavailable_with_legacy_runtime_caps(
    snapshot: RegistrySnapshot,
) -> None:
    support = evaluate_method_support(
        snapshot.methods["parametric_memory_lora_sft"],
        EvolutionExecutionProfile(
            execution_mode="self_deployed",
            capture_mode="transcript",
            harness_id="codex",
            runtime_capabilities=("adapter_serving", "trainer"),
        ),
    )

    assert support.overall is MethodSupportOverall.UNAVAILABLE
    assert support.runtime.missing_requirements == ("constrained_trainer_contract",)


def test_load_builtin_method_handles_returns_exact_legacy_callables(
    snapshot: RegistrySnapshot,
) -> None:
    loaded_entry_points: list[str] = []

    def verified_loader(identity: ImplementationIdentity) -> Callable:
        entry_point = identity.implementation.entry_point
        loaded_entry_points.append(entry_point)
        module_name, attribute_name = _entry_point_parts(entry_point)
        assert module_name == METHODS_MODULE
        return getattr(methods, attribute_name)

    handles = load_builtin_method_handles(snapshot, verified_loader=verified_loader)

    assert isinstance(handles, Mapping)
    assert frozenset(handles) == frozenset(METHOD_IDS)
    assert set(loaded_entry_points) == {
        f"{METHODS_MODULE}:{method_id}" for method_id in METHOD_IDS
    }
    for method_id in METHOD_IDS:
        assert handles[method_id] is methods.METHOD_REGISTRY[method_id]


@pytest.mark.parametrize("failure", ["missing", "extra"])
def test_load_builtin_method_handles_rejects_legacy_registry_key_drift(
    snapshot: RegistrySnapshot,
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drifted_registry = dict(methods.METHOD_REGISTRY)
    if failure == "missing":
        drifted_registry.pop("text_memory")
    else:
        drifted_registry["not_builtin"] = lambda: None
    monkeypatch.setattr(methods, "METHOD_REGISTRY", drifted_registry)

    with pytest.raises((TypeError, ValueError), match="missing|extra|mismatch"):
        load_builtin_method_handles(
            snapshot,
            verified_loader=lambda identity: getattr(
                methods,
                _entry_point_parts(identity.implementation.entry_point)[1],
            ),
        )


def test_load_builtin_method_handles_rejects_wrong_callable_identity(
    snapshot: RegistrySnapshot,
) -> None:
    def verified_loader(identity: ImplementationIdentity) -> Callable:
        _, attribute_name = _entry_point_parts(identity.implementation.entry_point)
        if attribute_name == "text_memory":
            return methods.text_memory_reflector
        return methods.METHOD_REGISTRY[attribute_name]

    with pytest.raises((TypeError, ValueError), match="mismatch|identity"):
        load_builtin_method_handles(snapshot, verified_loader=verified_loader)


def test_verified_builtin_registry_loads_every_anchor_and_exact_method_handle(
    tmp_path: Path,
) -> None:
    verified = VerifiedDistribution(
        expectation=DistributionArtifactExpectation(
            distribution="openevo",
            distribution_version="0.1.0",
            distribution_digest="a" * 64,
        ),
        install_root=tmp_path,
        inventory={},
        inventory_digest="b" * 64,
    )
    loaded_keys: list[tuple[DescriptorKind, str]] = []

    def fake_loader(implementation, verified_distribution, **expected):
        assert verified_distribution is verified
        kind = DescriptorKind(expected["expected_kind"])
        descriptor_id = expected["expected_id"]
        loaded_keys.append((kind, descriptor_id))
        if kind is DescriptorKind.METHOD:
            assert expected["expected_parameters"] == ("job", "artifact_root")
            return methods.METHOD_REGISTRY[descriptor_id]
        _, attribute_name = _entry_point_parts(implementation.entry_point)
        return getattr(builtins, attribute_name)

    loaded = load_verified_builtin_registry(
        verified,
        entry_point_loader=fake_loader,
    )

    assert frozenset(loaded.method_handles) == frozenset(METHOD_IDS)
    assert frozenset(loaded.descriptor_anchors) == frozenset(
        {
            *(f"target:{target_id}" for target_id in TARGET_IDS),
            *(f"target_handler:{handler_id}" for handler_id in HANDLER_IDS),
        }
    )
    assert set(loaded_keys) == {
        *((DescriptorKind.METHOD, method_id) for method_id in METHOD_IDS),
        *((DescriptorKind.TARGET, target_id) for target_id in TARGET_IDS),
        *((DescriptorKind.TARGET_HANDLER, handler_id) for handler_id in HANDLER_IDS),
    }


def test_builtin_registry_digest_is_stable_in_fresh_processes() -> None:
    script = """
from openevo.evolution.framework.builtins import (
    ImplementationDistributionIdentity,
    build_builtin_registry,
)
identity = ImplementationDistributionIdentity(
    distribution='openevo',
    distribution_version='0.1.0',
    distribution_digest='a' * 64,
)
print(build_builtin_registry(identity).registry_digest)
"""
    values = [
        subprocess.check_output([sys.executable, "-c", script], text=True).strip()
        for _ in range(2)
    ]

    assert values[0] == values[1]
    assert len(values[0]) == 64
