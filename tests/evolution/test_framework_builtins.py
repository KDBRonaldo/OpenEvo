from __future__ import annotations

from collections.abc import Callable, Mapping
import hashlib
from pathlib import Path
import subprocess
import sys
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from openevo.evolution import memevolve, methods
from openevo.evolution.framework import builtin_handlers, builtins
from openevo.evolution.framework.builtins import (
    BUILTIN_METHOD_IDS,
    ImplementationDistributionIdentity,
    VerifiedExecutableRegistry,
    build_builtin_registry,
    load_builtin_handler_handles,
    load_builtin_method_handles,
    load_verified_builtin_registry,
)
from openevo.evolution.framework.contracts import (
    ContributionKind,
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
from openevo.evolution.framework.resolution import resolve_agent_system_method
from openevo.evolution.framework.support import (
    MethodSupportOverall,
    evaluate_method_support,
)
from openevo.evolution.models import WorkerClaimInputArtifact, WorkerClaimedJob
from openevo.evolution.parametric import sd_lora
from tests.framework_testkit import verify_distribution_install_for_test


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
}
CONTEXT_METHOD_IDS = {"text_memory_memevolve", "parametric_memory_sd_lora"}
PROTECTED_METHOD_IDS = {
    "text_memory_expel_reflector",
    "skill_bundle_reflector",
    "agent_system_gepa_reflector",
}
REFLECTOR_METHOD_IDS = {
    "text_memory_reflector",
    "text_memory_expel_reflector",
    "text_memory_memevolve",
    "skill_bundle_reflector",
    "agent_system_reflector",
    "agent_system_history_reflector",
    "agent_system_pareto_reflector",
    "agent_system_gepa_reflector",
}


METHODS_MODULE = "openevo.evolution.methods"
MEMEVOLVE_MODULE = "openevo.evolution.memevolve"
SD_LORA_MODULE = "openevo.evolution.parametric.sd_lora"
BUILTINS_MODULE = "openevo.evolution.framework.builtins"
BUILTIN_HANDLERS_MODULE = "openevo.evolution.framework.builtin_handlers"


class _SourceDistribution:
    version = "0.1.0"
    metadata = {"Name": "openevo"}

    def __init__(self, install_root: Path) -> None:
        self._install_root = install_root

    def locate_file(self, path: str) -> Path:
        return self._install_root / path

    def read_text(self, filename: str) -> None:
        return None


def _verify_source_distribution(tmp_path: Path) -> VerifiedDistribution:
    install_root = Path(builtins.__file__).resolve().parents[3]
    artifact = tmp_path / "openevo-0.1.0-py3-none-any.whl"
    with ZipFile(artifact, "w", compression=ZIP_DEFLATED) as wheel:
        for path in sorted((install_root / "openevo").rglob("*")):
            if path.is_file() and path.name.endswith(
                (".py", ".pyi", ".so", ".pyd", ".dll", ".dylib")
            ):
                wheel.write(path, path.relative_to(install_root).as_posix())
        wheel.writestr(
            "openevo-0.1.0.dist-info/METADATA",
            "Name: openevo\nVersion: 0.1.0\n",
        )
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    return verify_distribution_install_for_test(
        DistributionArtifactExpectation(
            distribution="openevo",
            distribution_version="0.1.0",
            distribution_digest=digest,
        ),
        artifact,
        lambda _name: _SourceDistribution(install_root),
    )


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


def _method_entry_point(identity: ImplementationIdentity) -> Callable:
    module_name, attribute_name = _entry_point_parts(identity.implementation.entry_point)
    if module_name == METHODS_MODULE:
        return getattr(methods, attribute_name)
    if module_name == MEMEVOLVE_MODULE:
        return getattr(memevolve, attribute_name)
    if module_name == SD_LORA_MODULE:
        return getattr(sd_lora, attribute_name)
    raise AssertionError(f"unexpected method module: {module_name}")


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
    assert {
        descriptor.invocation_abi.value for descriptor in snapshot.methods.values()
    } == {"legacy_worker_job_v1", "method_context_v1"}
    assert snapshot.methods["text_memory_memevolve"].invocation_abi.value == (
        "method_context_v1"
    )
    assert snapshot.methods["parametric_memory_sd_lora"].invocation_abi.value == (
        "method_context_v1"
    )

    assert snapshot.targets["text_memory"].default_method_id == ("text_memory_expel_reflector")
    assert snapshot.targets["skill_bundle"].default_method_id == ("skill_bundle_reflector")
    assert snapshot.targets["agent_system"].default_method_id == ("agent_system_gepa_reflector")
    assert snapshot.targets["parametric_memory"].default_method_id == (
        "parametric_memory_sd_lora"
    )
    auto_resolver = snapshot.targets["agent_system"].selection_resolvers
    assert len(auto_resolver) == 1
    assert auto_resolver[0].selection_value == "auto"
    assert set(auto_resolver[0].resolved_method_ids) == {
        resolve_agent_system_method("auto", ()),
        resolve_agent_system_method("auto", ("dataset-prior",)),
    }
    assert snapshot.targets["parametric_memory"].exposure is Exposure.INTERNAL
    assert all(
        snapshot.targets[target_id].exposure is Exposure.DESKTOP
        for target_id in TARGET_IDS - {"parametric_memory"}
    )

    with pytest.raises(TypeError):
        snapshot.identity_digests["method:text_memory"] = "b" * 64


def test_builtin_descriptors_use_exact_method_target_and_handler_entry_points(
    snapshot: RegistrySnapshot,
) -> None:
    for method_id, descriptor in snapshot.methods.items():
        assert descriptor.implementation_ref is not None
        if method_id == "text_memory_memevolve":
            expected_module = MEMEVOLVE_MODULE
        elif method_id == "parametric_memory_sd_lora":
            expected_module = SD_LORA_MODULE
        else:
            expected_module = METHODS_MODULE
        assert descriptor.implementation_ref.entry_point == f"{expected_module}:{method_id}"

    target_anchor_names: set[str] = set()
    for descriptor in snapshot.targets.values():
        assert descriptor.implementation_ref is not None
        module_name, attribute_name = _entry_point_parts(descriptor.implementation_ref.entry_point)
        assert module_name == BUILTINS_MODULE
        assert not attribute_name.startswith("_")
        assert callable(getattr(builtins, attribute_name))
        target_anchor_names.add(attribute_name)

    handler_names: set[str] = set()
    for descriptor in snapshot.target_handlers.values():
        assert descriptor.implementation_ref is not None
        assert descriptor.input_contract_version == "1"
        assert descriptor.renderer_contract_version == "1"
        assert descriptor.contribution_contract_version == "2"
        expected_preambles = {
            "text_memory_handler": ("Use the following long-term memory for this task:"),
        }
        assert descriptor.instruction_preamble == expected_preambles.get(
            descriptor.id,
            "",
        )
        if descriptor.id == "agent_system_handler":
            assert set(descriptor.allowed_contribution_kinds) == {
                ContributionKind.STAGED_PAYLOAD,
                ContributionKind.ENVIRONMENT,
            }
        module_name, attribute_name = _entry_point_parts(descriptor.implementation_ref.entry_point)
        assert module_name == BUILTIN_HANDLERS_MODULE
        assert not attribute_name.startswith("_")
        assert (
            getattr(builtin_handlers, attribute_name)
            is (builtin_handlers.BUILTIN_HANDLER_REGISTRY[descriptor.id])
        )
        handler_names.add(attribute_name)

    assert len(target_anchor_names) == 4
    assert len(handler_names) == 4


def test_builtin_output_and_protected_method_contracts(
    snapshot: RegistrySnapshot,
) -> None:
    assert "report" in snapshot.methods["agent_system_pareto_reflector"].output_artifact_types
    assert "report" in snapshot.methods["agent_system_gepa_reflector"].output_artifact_types
    register_bindings = snapshot.methods["parametric_memory_register"].input_bindings
    assert [binding.binding_id for binding in register_bindings] == [
        "current_dataset",
        "prior_target_artifacts",
    ]
    assert register_bindings[0].min_count == 0
    sd_lora_descriptor = snapshot.methods["parametric_memory_sd_lora"]
    assert sd_lora_descriptor.invocation_abi.value == "method_context_v1"
    assert tuple(mode.value for mode in sd_lora_descriptor.execution_modes) == (
        "self_deployed",
    )
    assert sd_lora_descriptor.runtime_requirements == (
        "adapter_serving",
        "gpu",
        "sd_lora_continual_trainer",
    )
    assert sd_lora_descriptor.input_bindings[0].binding_id == "current_dataset"
    assert sd_lora_descriptor.input_bindings[0].max_count == 1
    assert sd_lora_descriptor.input_bindings[1].max_count == 1
    assert sd_lora_descriptor.config_schema["properties"]["model_revision"] == {
        "type": "string",
        "minLength": 40,
        "maxLength": 64,
    }
    assert "parametric_memory_lora_sft" not in methods.METHOD_REGISTRY
    assert "parametric_memory_lora_sft" not in methods.METHOD_METADATA

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
        assert tuple(
            injection.model_dump(mode="json")
            for injection in snapshot.methods[method_id].project_config_injections
        ) == ({"field_name": "reflector_llm", "source": "reflector_llm"},)
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


def test_builtin_method_helper_does_not_infer_project_config_ownership(
    distribution_identity: ImplementationDistributionIdentity,
) -> None:
    descriptor = builtins._method(
        distribution_identity,
        method_id="same_name_user_config",
        display_name="Same-name user config",
        description="Keep same-name fields editable without a declaration.",
        target_id="text_memory",
        execution_modes=("self_deployed",),
        input_bindings=(),
        output_artifact_types=("text_memory",),
        config_schema={
            "type": "object",
            "properties": {
                "reflector_llm": {"type": "string"},
                "base_model": {"type": "string"},
            },
            "additionalProperties": False,
        },
    )

    assert descriptor.project_config_injections == ()


def test_parametric_registration_explicitly_owns_base_model(
    snapshot: RegistrySnapshot,
) -> None:
    assert tuple(
        injection.model_dump(mode="json")
        for injection in snapshot.methods["parametric_memory_register"].project_config_injections
    ) == ({"field_name": "base_model", "source": "agent_model"},)


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
        plan_digest="a" * 64,
        registry_snapshot_digest="b" * 64,
        target_id="agent_system",
        method_id="agent_system_gepa_reflector",
        method_identity_digest="c" * 64,
        user_config={"reflector_llm": {"model": "gpt-5.5", "provider": "codex_cli"}},
        core_config={},
        input_bindings=resolution.bindings,
        output_artifact_types=descriptor.output_artifact_types,
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


def test_sd_lora_requires_the_exact_daemon_training_profile(
    snapshot: RegistrySnapshot,
) -> None:
    support = evaluate_method_support(
        snapshot.methods["parametric_memory_sd_lora"],
        EvolutionExecutionProfile(
            execution_mode="self_deployed",
            capture_mode="transcript",
            harness_id="codex",
            runtime_capabilities=("adapter_serving", "trainer"),
        ),
    )

    assert support.overall is MethodSupportOverall.UNAVAILABLE
    assert support.runtime.missing_requirements == ("gpu", "sd_lora_continual_trainer")

    available = evaluate_method_support(
        snapshot.methods["parametric_memory_sd_lora"],
        EvolutionExecutionProfile(
            execution_mode="self_deployed",
            capture_mode="transcript",
            harness_id="codex",
            runtime_capabilities=(
                "adapter_serving",
                "gpu",
                "sd_lora_continual_trainer",
            ),
        ),
    )
    assert available.overall is MethodSupportOverall.SUPPORTED


def test_load_builtin_method_handles_returns_exact_verified_callables(
    snapshot: RegistrySnapshot,
) -> None:
    loaded_entry_points: list[str] = []

    def verified_loader(identity: ImplementationIdentity) -> Callable:
        entry_point = identity.implementation.entry_point
        loaded_entry_points.append(entry_point)
        return _method_entry_point(identity)

    handles = load_builtin_method_handles(snapshot, verified_loader=verified_loader)

    assert isinstance(handles, Mapping)
    assert frozenset(handles) == frozenset(METHOD_IDS)
    assert set(loaded_entry_points) == {
        descriptor.implementation_ref.entry_point
        for descriptor in snapshot.methods.values()
        if descriptor.implementation_ref is not None
    }
    for method_id in METHOD_IDS - CONTEXT_METHOD_IDS:
        assert handles[method_id] is methods.METHOD_REGISTRY[method_id]
    assert handles["text_memory_memevolve"] is memevolve.text_memory_memevolve
    assert handles["parametric_memory_sd_lora"] is sd_lora.parametric_memory_sd_lora


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
            verified_loader=_method_entry_point,
        )


def test_load_builtin_method_handles_rejects_wrong_callable_identity(
    snapshot: RegistrySnapshot,
) -> None:
    def verified_loader(identity: ImplementationIdentity) -> Callable:
        module_name, attribute_name = _entry_point_parts(identity.implementation.entry_point)
        if attribute_name == "text_memory":
            return methods.text_memory_reflector
        return _method_entry_point(identity)

    with pytest.raises((TypeError, ValueError), match="mismatch|identity"):
        load_builtin_method_handles(snapshot, verified_loader=verified_loader)


def test_load_builtin_handler_handles_returns_exact_callables(
    snapshot: RegistrySnapshot,
) -> None:
    loaded_entry_points: list[str] = []

    def verified_loader(identity: ImplementationIdentity) -> Callable:
        entry_point = identity.implementation.entry_point
        loaded_entry_points.append(entry_point)
        module_name, attribute_name = _entry_point_parts(entry_point)
        assert module_name == BUILTIN_HANDLERS_MODULE
        return getattr(builtin_handlers, attribute_name)

    handles = load_builtin_handler_handles(
        snapshot,
        verified_loader=verified_loader,
    )

    assert isinstance(handles, Mapping)
    assert frozenset(handles) == frozenset(HANDLER_IDS)
    assert set(loaded_entry_points) == {
        descriptor.implementation_ref.entry_point
        for descriptor in snapshot.target_handlers.values()
        if descriptor.implementation_ref is not None
    }
    for handler_id in HANDLER_IDS:
        assert handles[handler_id] is builtin_handlers.BUILTIN_HANDLER_REGISTRY[handler_id]


@pytest.mark.parametrize("failure", ["missing", "extra"])
def test_load_builtin_handler_handles_rejects_registry_key_drift(
    snapshot: RegistrySnapshot,
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drifted_registry = dict(builtin_handlers.BUILTIN_HANDLER_REGISTRY)
    if failure == "missing":
        drifted_registry.pop("text_memory_handler")
    else:
        drifted_registry["not_builtin"] = lambda *_args: None
    monkeypatch.setattr(
        builtin_handlers,
        "BUILTIN_HANDLER_REGISTRY",
        drifted_registry,
    )

    with pytest.raises((TypeError, ValueError), match="missing|extra|mismatch"):
        load_builtin_handler_handles(
            snapshot,
            verified_loader=lambda identity: getattr(
                builtin_handlers,
                _entry_point_parts(identity.implementation.entry_point)[1],
            ),
        )


def test_load_builtin_handler_handles_rejects_wrong_callable_identity(
    snapshot: RegistrySnapshot,
) -> None:
    def verified_loader(identity: ImplementationIdentity) -> Callable:
        _, attribute_name = _entry_point_parts(identity.implementation.entry_point)
        if attribute_name == "text_memory_handler":
            return builtin_handlers.skill_bundle_handler
        return getattr(builtin_handlers, attribute_name)

    with pytest.raises((TypeError, ValueError), match="mismatch|identity"):
        load_builtin_handler_handles(snapshot, verified_loader=verified_loader)


def test_verified_distribution_cannot_be_constructed_publicly() -> None:
    with pytest.raises(TypeError, match="verify_distribution_install"):
        VerifiedDistribution()


def test_verified_registry_cannot_be_constructed_publicly() -> None:
    with pytest.raises(TypeError, match="verified registry loader"):
        VerifiedExecutableRegistry()


def test_verified_builtin_registry_loads_every_anchor_and_exact_method_handle(
    tmp_path: Path,
) -> None:
    verified = _verify_source_distribution(tmp_path)
    loaded = load_verified_builtin_registry(verified)

    assert frozenset(loaded.method_handles) == frozenset(METHOD_IDS)
    assert loaded.method_handles["text_memory_memevolve"] is (
        memevolve.text_memory_memevolve
    )
    assert frozenset(loaded.handler_handles) == frozenset(HANDLER_IDS)
    assert loaded.distribution_attestations == {verified.expectation.distribution_digest: verified}
    assert frozenset(loaded.descriptor_anchors) == frozenset(
        f"target:{target_id}" for target_id in TARGET_IDS
    )


def test_verified_builtin_registry_has_no_loader_injection_hook(
    tmp_path: Path,
) -> None:
    verified = _verify_source_distribution(tmp_path)

    with pytest.raises(TypeError, match="entry_point_loader"):
        load_verified_builtin_registry(  # type: ignore[call-arg]
            verified,
            entry_point_loader=lambda *_args, **_kwargs: None,
        )


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
