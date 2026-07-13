from __future__ import annotations

from copy import deepcopy
import hashlib
import pickle

import pytest
from pydantic import TypeAdapter, ValidationError

import openevo.evolution.framework as framework
from openevo.evolution.framework import (
    CaptureMode,
    DestinationScope,
    EnvironmentBinding,
    EvolutionExecutionProfile,
    EvolutionMethodDescriptor,
    EvolutionPlan,
    ProjectEvolutionConfig,
    ProjectEvolutionTargetMap,
    ProjectEvolutionTargetSelection,
    EvolutionTargetSelection,
    ExecutionMode,
    ImplementationRef,
    MethodInvocationABI,
    MethodInputBinding,
    InstructionContribution,
    RendererPayload,
    ResolvedEvolutionSelection,
    StagedPayloadContribution,
    TargetHandlerDescriptor,
    TargetHandlerOutput,
    canonical_digest,
    canonical_json,
)


def _implementation() -> ImplementationRef:
    return ImplementationRef(
        distribution="openevo",
        distribution_version="0.1.0",
        distribution_digest="a" * 64,
        entry_point="openevo.evolution.methods:text_memory_expel_reflector",
    )


def _resolved(target_id: str = "text_memory") -> ResolvedEvolutionSelection:
    config = {"rounds": 2}
    return ResolvedEvolutionSelection(
        target_id=target_id,
        handler_id=f"{target_id}_handler",
        method_id="text_memory_expel_reflector",
        config_json=canonical_json(config),
        config_digest=canonical_digest(config),
        target_identity_digest="a" * 64,
        handler_identity_digest="b" * 64,
        method_identity_digest="c" * 64,
    )


def _profile() -> EvolutionExecutionProfile:
    return EvolutionExecutionProfile(
        execution_mode="subscription",
        capture_mode="transcript",
        harness_id="codex",
        harness_capabilities=("stable_transcript",),
    )


def test_execution_capture_harness_and_runtime_are_independent() -> None:
    assert {item.value for item in ExecutionMode} == {
        "subscription",
        "self_deployed",
    }
    assert {item.value for item in CaptureMode} == {"transcript", "token_level"}
    profile = _profile()
    assert profile.harness_id == "codex"
    assert profile.runtime_capabilities == ()

    with pytest.raises(ValidationError, match="transcript capture"):
        EvolutionExecutionProfile(
            execution_mode="subscription",
            capture_mode="token_level",
            harness_id="codex",
        )


def test_descriptors_are_strict_frozen_and_use_stable_ids() -> None:
    method = EvolutionMethodDescriptor(
        id="text_memory_expel_reflector",
        display_name="Text Memory ExpeL",
        description="Reflect transcript trajectories into reusable memory.",
        target_id="text_memory",
        invocation_abi=MethodInvocationABI.LEGACY_WORKER_JOB_V1,
        execution_modes=("subscription", "self_deployed"),
        capture_modes=("transcript",),
        supported_harness_ids=("codex",),
        harness_requirements=("stable_transcript",),
        input_bindings=(
            MethodInputBinding(
                binding_id="dataset",
                source="current_dataset",
                artifact_type="dataset",
            ),
        ),
        output_artifact_types=("text_memory",),
        implementation_ref=_implementation(),
    )
    assert method.default_config == {}
    assert method.invocation_abi is MethodInvocationABI.LEGACY_WORKER_JOB_V1
    with pytest.raises(ValidationError):
        method.id = "changed"
    with pytest.raises(ValidationError, match="stable identifier"):
        EvolutionMethodDescriptor(
            **method.model_dump(exclude={"id"}),
            id="method:ambiguous",
        )
    with pytest.raises(ValidationError):
        EvolutionMethodDescriptor(
            **method.model_dump(),
            unexpected=True,
        )


def test_method_invocation_abi_is_closed_and_required() -> None:
    assert tuple(abi.value for abi in MethodInvocationABI) == (
        "legacy_worker_job_v1",
        "method_context_v1",
    )
    payload = {
        "id": "reflect",
        "display_name": "Reflect",
        "description": "Reflect trajectories.",
        "target_id": "text_memory",
        "execution_modes": ("self_deployed",),
        "capture_modes": ("transcript",),
        "supported_harness_ids": ("codex",),
        "output_artifact_types": ("text_memory",),
    }

    with pytest.raises(ValidationError, match="invocation_abi"):
        EvolutionMethodDescriptor(**payload)
    with pytest.raises(ValidationError, match="invocation_abi"):
        EvolutionMethodDescriptor(**payload, invocation_abi="unknown_v1")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("distribution", "OpenEvo_Test"),
        ("distribution", " openevo"),
        ("distribution_version", "1.0\n"),
        ("distribution_version", "v" * 129),
        ("contract_version", " version "),
    ],
)
def test_implementation_identity_text_is_canonical(
    field: str,
    value: str,
) -> None:
    payload = _implementation().model_dump(mode="python")
    payload[field] = value
    with pytest.raises(ValidationError):
        ImplementationRef.model_validate(payload)


def test_project_selection_retains_disabled_draft_but_enabled_requires_method() -> None:
    draft_config = {"rounds": 3}
    disabled = EvolutionTargetSelection(
        target_id="text_memory",
        enabled=False,
        method_id="text_memory_expel_reflector",
        config=draft_config,
    )
    draft_config["rounds"] = 9
    assert disabled.config == {"rounds": 3}
    with pytest.raises(ValidationError, match="requires method_id"):
        EvolutionTargetSelection(target_id="text_memory", enabled=True)


def test_project_target_map_value_is_strict_frozen_and_deep_copies_json() -> None:
    source = {"nested": {"values": [1, True, None]}}
    selection = ProjectEvolutionTargetSelection(
        enabled=False,
        method="draft_method",
        config=source,
    )

    source["nested"]["values"].append(2)
    assert selection.config == {"nested": {"values": [1, True, None]}}
    mutable_view = selection.config
    mutable_view["nested"]["values"].append("local-only")
    with pytest.raises(TypeError):
        mutable_view["not_json"] = object()
    assert selection.config == {"nested": {"values": [1, True, None]}}
    assert (
        ProjectEvolutionTargetSelection.model_validate(selection.model_dump(mode="json"))
        == selection
    )
    assert (
        ProjectEvolutionTargetSelection.model_validate(selection.model_dump(round_trip=True))
        == selection
    )
    assert selection.model_dump(exclude={"config"}) == {
        "enabled": False,
        "method": "draft_method",
    }
    assert selection.model_dump(include={"enabled"}) == {"enabled": False}
    assert selection.model_dump(include={"config": {"nested"}}) == {
        "config": {"nested": {"values": [1, True, None]}},
    }
    assert selection.model_dump(exclude={"config": {"nested"}}) == {
        "enabled": False,
        "method": "draft_method",
        "config": {},
    }
    with pytest.raises(ValidationError):
        selection.enabled = True
    with pytest.raises(ValidationError, match="valid boolean"):
        ProjectEvolutionTargetSelection(enabled="true", method="auto")
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ProjectEvolutionTargetSelection(
            enabled=False,
            method=None,
            config={},
            target_id="text_memory",
        )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ProjectEvolutionTargetSelection(
            enabled=False,
            config_json="{}",
        )
    with pytest.raises(ValidationError, match="non-JSON"):
        ProjectEvolutionTargetSelection(
            enabled=False,
            config={"bad": object()},
        )

    changed = selection.model_copy(update={"config": {"replacement": [1, 2]}})
    assert changed.config == {"replacement": [1, 2]}
    assert selection.config == {"nested": {"values": [1, True, None]}}
    with pytest.raises(ValueError, match="unknown.*config_json"):
        selection.model_copy(update={"config_json": "{}"})


def test_project_target_config_uses_lossless_cross_language_numbers() -> None:
    selection = ProjectEvolutionTargetSelection(
        enabled=False,
        config={
            "largest_safe_integer": 9_007_199_254_740_991,
            "integral_float": 1.0,
            "fraction": 0.125,
        },
    )

    assert selection.config == {
        "largest_safe_integer": 9_007_199_254_740_991,
        "integral_float": 1,
        "fraction": 0.125,
    }
    with pytest.raises(ValidationError, match="exceeds safe JSON range"):
        ProjectEvolutionTargetSelection(
            enabled=False,
            config={"unsafe": 9_007_199_254_740_992},
        )


def test_project_target_config_does_not_alias_prebuilt_or_deep_copied_values() -> None:
    supplied = ProjectEvolutionConfig({"value": "original"})
    selection = ProjectEvolutionTargetSelection(enabled=False, config=supplied)
    copied = deepcopy(selection)

    object.__setattr__(supplied, "_encoded", '{"value":"supplied-mutated"}')
    object.__setattr__(selection.config, "_encoded", '{"value":"selection-mutated"}')

    assert selection.config == {"value": "selection-mutated"}
    assert copied.config == {"value": "original"}


def test_project_target_map_is_deeply_copyable_and_has_typed_schema() -> None:
    target_map = ProjectEvolutionTargetMap(
        {
            "future_target": {
                "enabled": False,
                "method": "future_method",
                "config": {"nested": [1]},
            }
        }
    )

    copied = deepcopy(target_map)
    assert copied == target_map
    assert copied is not target_map
    assert ProjectEvolutionTargetMap(target_map).to_dict() == target_map.to_dict()
    assert pickle.loads(pickle.dumps(target_map)) == target_map
    adapter = TypeAdapter(ProjectEvolutionTargetMap)
    nullable_map = ProjectEvolutionTargetMap(
        {"draft_target": {"enabled": False, "method": None, "config": {}}}
    )
    assert adapter.dump_python(nullable_map, exclude_none=True) == {
        "draft_target": {"enabled": False, "config": {}}
    }
    assert adapter.dump_python(nullable_map, exclude_defaults=True) == {
        "draft_target": {"enabled": False}
    }
    with pytest.raises(TypeError):
        target_map["other"] = target_map["future_target"]
    with pytest.raises(TypeError, match="immutable"):
        target_map._items = ()
    with pytest.raises(ValueError, match="target IDs must be strings"):
        ProjectEvolutionTargetMap({b"future_target": {"enabled": False}})

    object.__setattr__(
        target_map,
        "_items",
        (("future_target", '{"enabled":true,"method":null,"config":{}}'),),
    )
    with pytest.raises(ValidationError, match="enabled target requires method"):
        adapter.validate_python(target_map)


def test_project_config_and_target_map_are_resource_bounded() -> None:
    nested: dict[str, object] = {}
    for _ in range(17):
        nested = {"next": nested}

    with pytest.raises(ValueError, match="depth budget"):
        ProjectEvolutionConfig(nested)
    with pytest.raises(ValueError, match="collection is too large"):
        ProjectEvolutionConfig({"items": [None] * 4097})
    with pytest.raises(ValueError, match="node budget"):
        ProjectEvolutionConfig({"items": [[None] for _ in range(4096)]})
    with pytest.raises(ValueError, match="text budget"):
        ProjectEvolutionConfig({"text": "x" * (512 * 1024 + 1)})
    with pytest.raises(ValueError, match="at most 128 targets"):
        ProjectEvolutionTargetMap({f"target_{index}": {"enabled": False} for index in range(129)})


def test_enabled_project_target_requires_method_or_auto() -> None:
    with pytest.raises(ValidationError, match="enabled target requires method"):
        ProjectEvolutionTargetSelection(enabled=True)
    with pytest.raises(ValidationError, match="stable identifier"):
        ProjectEvolutionTargetSelection(enabled=True, method="")

    assert ProjectEvolutionTargetSelection(enabled=True, method="auto").method == "auto"


def test_project_target_selection_revalidates_existing_instances() -> None:
    selection = ProjectEvolutionTargetSelection(enabled=False, method="valid_method")
    object.__setattr__(selection, "method", "bad/method")

    with pytest.raises(ValidationError, match="stable identifier"):
        ProjectEvolutionTargetSelection.model_validate(selection)
    with pytest.raises(ValidationError, match="stable identifier"):
        TypeAdapter(ProjectEvolutionTargetSelection).validate_python(selection)


def test_project_target_public_schema_keeps_config_as_an_object() -> None:
    validation = ProjectEvolutionTargetSelection.model_json_schema(mode="validation")
    serialization = ProjectEvolutionTargetSelection.model_json_schema(mode="serialization")

    assert validation["properties"]["config"]["type"] == "object"
    assert serialization["properties"]["config"]["type"] == "object"
    assert "config_json" not in validation["properties"]
    assert "config_json" not in serialization["properties"]

    selection = ProjectEvolutionTargetSelection(
        enabled=False,
        config={"nested": {"keep": 1, "drop": 2}, "top": 3},
    )
    assert selection.model_dump(include={"config": {"nested": {"keep"}}}) == {
        "config": {"nested": {"keep": 1}}
    }
    assert selection.model_dump(exclude={"config": {"nested": {"drop"}}}) == {
        "enabled": False,
        "method": None,
        "config": {"nested": {"keep": 1}, "top": 3},
    }

    nullable = ProjectEvolutionTargetSelection(enabled=False, method=None)
    assert nullable.model_dump(exclude_none=True) == {
        "enabled": False,
        "config": {},
    }


def test_canonical_json_and_resolved_config_are_immutable_by_copy() -> None:
    assert canonical_json({"b": 2, "a": [True, None]}) == ('{"a":[true,null],"b":2}')
    with pytest.raises(ValueError, match="non-finite"):
        canonical_json({"value": float("nan")})
    with pytest.raises(ValueError, match="surrogate"):
        canonical_digest({"value": "\ud800"})

    resolved = _resolved()
    config = resolved.config()
    config["rounds"] = 9
    assert resolved.config() == {"rounds": 2}
    with pytest.raises(ValidationError, match="config_digest"):
        ResolvedEvolutionSelection(
            **resolved.model_dump(exclude={"config_digest"}),
            config_digest="d" * 64,
        )
    with pytest.raises(ValidationError, match="canonical"):
        ResolvedEvolutionSelection(
            **resolved.model_dump(exclude={"config_json"}),
            config_json='{ "rounds": 2 }',
        )


def test_plan_contains_only_deeply_immutable_resolved_values() -> None:
    plan = EvolutionPlan(
        plan_id="plan-1",
        registry_snapshot_digest="d" * 64,
        execution_profile=_profile(),
        selections=(_resolved(),),
    )
    assert plan.selections[0].config() == {"rounds": 2}
    with pytest.raises(ValidationError):
        plan.plan_id = "changed"
    with pytest.raises(ValidationError, match="duplicate target"):
        EvolutionPlan(
            plan_id="plan-2",
            registry_snapshot_digest="d" * 64,
            execution_profile=_profile(),
            selections=(_resolved(), _resolved()),
        )

    ordered = EvolutionPlan(
        plan_id="plan-3",
        registry_snapshot_digest="d" * 64,
        execution_profile=_profile(),
        selections=(_resolved("z_target"), _resolved("a_target")),
    )
    assert tuple(item.target_id for item in ordered.selections) == (
        "a_target",
        "z_target",
    )


@pytest.mark.parametrize(
    "path",
    [
        "/absolute",
        "../escape",
        "a/../../escape",
        "a\\b",
        "a//b",
        "a\nb",
        "a\tb",
        " trailing",
        "trailing ",
        "C:/file",
    ],
)
def test_staged_payload_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(ValidationError, match="relative path"):
        StagedPayloadContribution(
            contribution_id="payload",
            source_artifact_id="artifact-1",
            source_relative_path=path,
            source_sha256="a" * 64,
            source_size_bytes=1,
            media_type="text/markdown",
            payload_kind="file",
            destination_scope="target_data",
            destination_relative_path="memory.md",
        )


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"allowed_uri_schemes": ("javascript",)}, "URI scheme"),
        ({"allowed_uri_schemes": ("HTTP",)}, "URI scheme"),
        ({"allowed_media_types": ("not-a-mime",)}, "MIME"),
        ({"instruction_preamble": " trailing "}, "identity text"),
        ({"instruction_preamble": "x\nsecret"}, "identity text"),
        ({"input_contract_version": "999"}, "input_contract_version"),
        ({"renderer_contract_version": "999"}, "renderer_contract_version"),
        (
            {"contribution_contract_version": "999"},
            "contribution_contract_version",
        ),
    ],
)
def test_handler_descriptor_rejects_unsafe_policies(
    updates: dict[str, object],
    message: str,
) -> None:
    payload = {
        "id": "memory_handler",
        "target_id": "text_memory",
        "artifact_types": ("text_memory",),
        "renderer_kind": "markdown",
        "allowed_uri_schemes": ("file",),
        "allowed_media_types": ("text/markdown",),
        "allowed_destination_scopes": ("target_data",),
        "allowed_contribution_kinds": ("staged_payload",),
        "implementation_ref": _implementation(),
        **updates,
    }
    with pytest.raises(ValidationError, match=message):
        TargetHandlerDescriptor(**payload)


def test_handler_output_is_data_only_and_environment_refs_staged_payload() -> None:
    staged = StagedPayloadContribution(
        contribution_id="memory_file",
        source_artifact_id="artifact-1",
        source_relative_path="memory.md",
        source_sha256=hashlib.sha256(b"Remember parser precedence.").hexdigest(),
        source_size_bytes=len(b"Remember parser precedence."),
        media_type="text/markdown",
        payload_kind="file",
        destination_scope=DestinationScope.TARGET_DATA,
        destination_relative_path="memory.md",
    )
    output = TargetHandlerOutput(
        target_id="text_memory",
        handler_id="memory_handler",
        artifact_ids=("artifact-1",),
        instructions=(
            InstructionContribution(
                contribution_id="memory_instruction",
                source_artifact_ids=("artifact-1",),
                text="Remember parser precedence.",
            ),
        ),
        staged_payloads=(staged,),
        environment=(
            EnvironmentBinding(
                name="OPENEVO_MEMORY_FILE",
                value_contribution_ids=("memory_file",),
                value_kind="path",
            ),
        ),
        renderer=RendererPayload(
            kind="markdown",
            title="Text memory",
            source_contribution_ids=("memory_instruction",),
            data={"markdown": "Remember parser precedence."},
        ),
    )
    assert output.contract_version == "2"
    assert output.renderer.kind.value == "markdown"

    incompatible = output.model_dump(mode="python")
    incompatible["contract_version"] = "1"
    with pytest.raises(ValidationError, match="literal_error"):
        TargetHandlerOutput.model_validate(incompatible)

    with pytest.raises(ValidationError, match="reference staged payloads"):
        TargetHandlerOutput(
            **output.model_dump(exclude={"environment"}),
            environment=(
                EnvironmentBinding(
                    name="OPENEVO_MEMORY_FILE",
                    value_contribution_ids=("missing",),
                    value_kind="path",
                ),
            ),
        )


def test_file_bundle_renderer_rejects_ancestor_path_collisions() -> None:
    entry = {
        "media_type": "text/plain",
        "size_bytes": 1,
        "sha256": "a" * 64,
    }
    with pytest.raises(ValidationError, match="must not conflict"):
        RendererPayload(
            kind="file_bundle",
            title="Bundle",
            source_contribution_ids=("bundle",),
            data={
                "files": [
                    {"relative_path": "bundle", **entry},
                    {"relative_path": "bundle/file.txt", **entry},
                ]
            },
        )

    staged = StagedPayloadContribution(
        contribution_id="memory_file",
        source_artifact_id="artifact-1",
        source_relative_path="memory.md",
        source_sha256=hashlib.sha256(b"Remember parser precedence.").hexdigest(),
        source_size_bytes=len(b"Remember parser precedence."),
        media_type="text/markdown",
        payload_kind="file",
        destination_scope="target_data",
        destination_relative_path="memory.md",
    )
    output = TargetHandlerOutput(
        target_id="text_memory",
        handler_id="memory_handler",
        artifact_ids=("artifact-1",),
        staged_payloads=(staged,),
        renderer=RendererPayload(
            kind="markdown",
            title="Text memory",
            source_contribution_ids=("memory_file",),
            data={"markdown": "Remember parser precedence."},
        ),
    )
    with pytest.raises(ValidationError, match="directory environment binding"):
        TargetHandlerOutput(
            **output.model_dump(exclude={"environment"}),
            environment=(
                EnvironmentBinding(
                    name="OPENEVO_MEMORY_FILE",
                    value_contribution_ids=("memory_file",),
                    value_kind="directory",
                ),
            ),
        )


def test_public_exports_are_explicit_and_complete() -> None:
    assert len(framework.__all__) == len(set(framework.__all__))
    assert all(hasattr(framework, name) for name in framework.__all__)
    assert {"contracts", "registry", "schema"}.isdisjoint(framework.__all__)
    assert "validate_schema" in framework.__all__
    assert "EvolutionFrameworkRegistry" in framework.__all__


def test_caller_mutation_does_not_change_descriptor_copy_input() -> None:
    schema = {
        "type": "object",
        "properties": {"rounds": {"type": "integer", "default": 2}},
        "additionalProperties": False,
    }
    original = deepcopy(schema)
    method = EvolutionMethodDescriptor(
        id="reflect",
        display_name="Reflect",
        description="Reflect.",
        target_id="memory",
        invocation_abi=MethodInvocationABI.METHOD_CONTEXT_V1,
        execution_modes=("self_deployed",),
        capture_modes=("transcript",),
        supported_harness_ids=("codex",),
        output_artifact_types=("text_memory",),
        config_schema=schema,
    )
    with pytest.raises(TypeError, match="immutable"):
        method.config_schema["properties"]["rounds"]["default"] = 4
    assert schema == original
