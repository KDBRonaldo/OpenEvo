from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Callable

import pytest
from pydantic import Field

from openevo.evolution.framework import (
    EnvironmentBinding,
    EvolutionExecutionProfile,
    EvolutionMethodDescriptor,
    EvolutionTargetDescriptor,
    EvolutionTargetSelection,
    MethodInputBinding,
    ImplementationRef,
    InstructionContribution,
    PayloadManifestEntry,
    RendererPayload,
    RuntimeDestinationRoots,
    StagedPayloadContribution,
    TargetHandlerDescriptor,
    TargetHandlerInput,
    TargetHandlerOutput,
    TrustedArtifactSnapshot,
    canonical_json,
    payload_tree_digest,
)
from openevo.evolution.framework.registry import EvolutionFrameworkRegistry


def _implementation(name: str, digest: str) -> ImplementationRef:
    return ImplementationRef(
        distribution="openevo-test",
        distribution_version="1.0.0",
        distribution_digest=digest * 64,
        entry_point=f"openevo_test.{name}:implementation",
    )


def _schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "settings": {
                "type": "object",
                "properties": {
                    "rounds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 8,
                        "default": 2,
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["safe", "fast"],
                        "default": "safe",
                    },
                },
                "additionalProperties": False,
            }
        },
        "additionalProperties": False,
    }


def _descriptors(
    *,
    target_id: str = "memory",
    method_id: str = "reflect",
    artifact_type: str = "text_memory",
) -> tuple[object, ...]:
    handler_id = f"{target_id}_handler"
    return (
        EvolutionTargetDescriptor(
            id=target_id,
            display_name="Memory",
            description="Reusable memory.",
            artifact_type=artifact_type,
            handler_id=handler_id,
            renderer_kind="markdown",
            default_method_id=method_id,
            implementation_ref=_implementation(f"{target_id}_target", "1"),
        ),
        TargetHandlerDescriptor(
            id=handler_id,
            target_id=target_id,
            artifact_types=(artifact_type,),
            renderer_kind="markdown",
            allowed_uri_schemes=("file",),
            allowed_media_types=("text/markdown",),
            allowed_destination_scopes=("target_data",),
            environment_allowlist=("OPENEVO_MEMORY_FILE",),
            allowed_contribution_kinds=("staged_payload", "environment"),
            implementation_ref=_implementation(f"{target_id}_handler", "2"),
        ),
        EvolutionMethodDescriptor(
            id=method_id,
            display_name="Reflect",
            description="Reflect over trajectories.",
            target_id=target_id,
            execution_modes=("self_deployed",),
            capture_modes=("transcript",),
            supported_harness_ids=("codex",),
            harness_requirements=("stable_transcript",),
            runtime_requirements=("core_worker",),
            input_bindings=(
                MethodInputBinding(
                    binding_id="dataset",
                    source="current_dataset",
                    artifact_type="dataset",
                    min_count=1,
                ),
            ),
            output_artifact_types=(artifact_type,),
            config_schema=_schema(),
            default_config={"settings": {"rounds": 3}},
            implementation_ref=_implementation(f"{target_id}_method", "3"),
        ),
    )


def _registry(descriptors: tuple[object, ...] | None = None) -> EvolutionFrameworkRegistry:
    registry = EvolutionFrameworkRegistry()
    for descriptor in descriptors or _descriptors():
        registry.register(descriptor)
    return registry


def _profile(
    *,
    execution_mode: str = "self_deployed",
    capture_mode: str = "transcript",
    harness_id: str = "codex",
    harness_capabilities: tuple[str, ...] = ("stable_transcript",),
    runtime_capabilities: tuple[str, ...] = ("core_worker",),
) -> EvolutionExecutionProfile:
    return EvolutionExecutionProfile(
        execution_mode=execution_mode,
        capture_mode=capture_mode,
        harness_id=harness_id,
        harness_capabilities=harness_capabilities,
        runtime_capabilities=runtime_capabilities,
    )


def _replace(
    descriptors: tuple[object, ...],
    descriptor_type: type,
    **updates: object,
) -> tuple[object, ...]:
    result: list[object] = []
    for descriptor in descriptors:
        if not isinstance(descriptor, descriptor_type):
            result.append(descriptor)
            continue
        payload = descriptor.model_dump(mode="python")
        payload.update(updates)
        result.append(type(descriptor).model_validate(payload))
    return tuple(result)


def test_duplicate_and_post_freeze_registration_fail() -> None:
    descriptors = _descriptors()
    registry = EvolutionFrameworkRegistry()
    registry.register_target(descriptors[0])
    with pytest.raises(ValueError, match="duplicate target"):
        registry.register_target(descriptors[0])
    for descriptor in descriptors[1:]:
        registry.register(descriptor)
    snapshot = registry.freeze()
    assert registry.freeze() is snapshot
    with pytest.raises(RuntimeError, match="frozen"):
        registry.register(descriptors[2].model_copy(update={"id": "later"}))


def test_registry_rejects_descriptor_subclasses_with_hidden_fields() -> None:
    class ExtendedMethod(EvolutionMethodDescriptor):
        hidden_extension: str = Field(min_length=1)

    base = next(
        item for item in _descriptors() if isinstance(item, EvolutionMethodDescriptor)
    )
    extended = ExtendedMethod(
        **base.model_dump(mode="python"),
        hidden_extension="not-part-of-v1",
    )
    with pytest.raises(TypeError, match="unsupported evolution descriptor"):
        EvolutionFrameworkRegistry().register(extended)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda values: _replace(
                values,
                EvolutionTargetDescriptor,
                handler_id="missing",
            ),
            "unknown target handler",
        ),
        (
            lambda values: _replace(
                values,
                EvolutionTargetDescriptor,
                default_method_id="missing",
            ),
            "unknown default method",
        ),
        (
            lambda values: _replace(
                values,
                EvolutionMethodDescriptor,
                target_id="missing",
            ),
            "unknown target",
        ),
        (
            lambda values: _replace(
                values,
                EvolutionMethodDescriptor,
                output_artifact_types=("skill_bundle",),
            ),
            "does not output target artifact",
        ),
        (
            lambda values: _replace(
                values,
                TargetHandlerDescriptor,
                artifact_types=("skill_bundle",),
            ),
            "handler.*mismatch",
        ),
    ],
)
def test_graph_validation_fails_closed(
    mutate: Callable[[tuple[object, ...]], tuple[object, ...]],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _registry(mutate(_descriptors())).freeze()


def test_visible_target_requires_visible_default_method() -> None:
    descriptors = _replace(
        _descriptors(),
        EvolutionTargetDescriptor,
        exposure="desktop",
    )
    descriptors = _replace(
        descriptors,
        TargetHandlerDescriptor,
        exposure="desktop",
    )
    with pytest.raises(ValueError, match="default method is hidden"):
        _registry(descriptors).freeze()


def test_visible_target_requires_visible_handler() -> None:
    descriptors = _replace(
        _descriptors(),
        EvolutionTargetDescriptor,
        exposure="desktop",
    )
    descriptors = _replace(
        descriptors,
        EvolutionMethodDescriptor,
        exposure="desktop",
    )
    with pytest.raises(ValueError, match="handler is hidden"):
        _registry(descriptors).freeze()


def test_implementation_schema_and_defaults_validate_at_freeze() -> None:
    missing_ref = _replace(
        _descriptors(),
        EvolutionMethodDescriptor,
        implementation_ref=None,
    )
    with pytest.raises(ValueError, match="implementation_ref"):
        _registry(missing_ref).freeze()

    malformed_ref = _implementation("method", "3").model_copy(
        update={"entry_point": "not an entry point"}
    )
    with pytest.raises(ValueError, match="entry_point"):
        _registry(
            _replace(
                _descriptors(),
                EvolutionMethodDescriptor,
                implementation_ref=malformed_ref,
            )
        ).freeze()

    bad_schema = _replace(
        _descriptors(),
        EvolutionMethodDescriptor,
        config_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": True,
        },
    )
    with pytest.raises(ValueError, match="config schema"):
        _registry(bad_schema).freeze()

    bad_default = _replace(
        _descriptors(),
        EvolutionMethodDescriptor,
        default_config={"settings": {"rounds": 99}},
    )
    with pytest.raises(ValueError, match="default config"):
        _registry(bad_default).freeze()


def test_config_precedence_is_schema_then_descriptor_then_user() -> None:
    snapshot = _registry().freeze()
    normalized = snapshot.normalize_method_config(
        "reflect",
        {"settings": {"mode": "fast"}},
    )
    assert normalized == {"settings": {"rounds": 3, "mode": "fast"}}

    user_override = snapshot.normalize_method_config(
        "reflect",
        {"settings": {"rounds": 5}},
    )
    assert user_override == {"settings": {"rounds": 5, "mode": "safe"}}


def test_required_user_config_is_checked_at_plan_time_not_registry_freeze() -> None:
    schema = {
        "type": "object",
        "properties": {"prompt": {"type": "string", "minLength": 1}},
        "required": ["prompt"],
        "additionalProperties": False,
    }
    descriptors = _replace(
        _descriptors(),
        EvolutionMethodDescriptor,
        config_schema=schema,
        default_config={},
    )
    snapshot = _registry(descriptors).freeze()
    with pytest.raises(ValueError, match=r"config\.prompt"):
        snapshot.normalize_method_config("reflect", {})
    assert snapshot.normalize_method_config("reflect", {"prompt": "Reflect."}) == {
        "prompt": "Reflect."
    }


def test_method_schema_cannot_claim_core_execution_fields() -> None:
    schema = {
        "type": "object",
        "properties": {"task_id": {"type": "string"}},
        "additionalProperties": False,
    }
    descriptors = _replace(
        _descriptors(),
        EvolutionMethodDescriptor,
        config_schema=schema,
    )
    with pytest.raises(ValueError, match="Core-owned fields"):
        _registry(descriptors).freeze()


@pytest.mark.parametrize(
    ("profile", "message"),
    [
        (_profile(execution_mode="subscription"), "execution mode"),
        (_profile(capture_mode="token_level"), "capture mode"),
        (_profile(harness_id="claude"), "does not support harness"),
        (_profile(harness_capabilities=()), "harness capabilities"),
        (_profile(runtime_capabilities=()), "runtime capabilities"),
    ],
)
def test_profile_compatibility_checks_every_axis(
    profile: EvolutionExecutionProfile,
    message: str,
) -> None:
    selection = EvolutionTargetSelection(
        target_id="memory",
        enabled=True,
        method_id="reflect",
    )
    with pytest.raises(ValueError, match=message):
        _registry().freeze().resolve_selection(selection, profile)


def test_compile_plan_ignores_disabled_draft_and_is_internally_consistent() -> None:
    snapshot = _registry().freeze()
    enabled = EvolutionTargetSelection(
        target_id="memory",
        enabled=True,
        method_id="reflect",
        config={"settings": {"mode": "fast"}},
    )
    disabled = EvolutionTargetSelection(
        target_id="memory",
        enabled=False,
        method_id="reflect",
        config={"settings": {"rounds": 8}},
    )
    plan = snapshot.compile_plan(
        plan_id="plan-1",
        selections=(enabled,),
        profile=_profile(),
    )
    assert len(plan.selections) == 1
    assert plan.selections[0].target_id == "memory"
    assert plan.selections[0].method_id == "reflect"
    assert plan.selections[0].config() == {
        "settings": {"mode": "fast", "rounds": 3}
    }

    disabled_plan = snapshot.compile_plan(
        plan_id="plan-2",
        selections=(disabled,),
        profile=_profile(),
    )
    assert disabled_plan.selections == ()
    with pytest.raises(ValueError, match="duplicate target"):
        snapshot.compile_plan(
            plan_id="plan-3",
            selections=(enabled, disabled),
            profile=_profile(),
        )


def test_registration_order_and_fresh_process_do_not_change_identity() -> None:
    descriptors = _descriptors()
    forward = _registry(descriptors).freeze()
    reverse = _registry(tuple(reversed(descriptors))).freeze()
    assert forward.registry_digest == reverse.registry_digest
    assert forward.identity_digests == reverse.identity_digests

    script = f"""
import importlib.util
import json
spec = importlib.util.spec_from_file_location('registry_fixture', {__file__!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
snapshot = module._registry(tuple(reversed(module._descriptors()))).freeze()
selection = module.EvolutionTargetSelection(target_id='memory', enabled=True, method_id='reflect')
print(json.dumps({{
    'registry': snapshot.registry_digest,
    'plan': snapshot.plan_snapshot_digest((selection,), module._profile()),
    'identities': dict(snapshot.identity_digests),
}}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    fresh = json.loads(completed.stdout)
    selection = EvolutionTargetSelection(
        target_id="memory", enabled=True, method_id="reflect"
    )
    assert fresh == {
        "registry": forward.registry_digest,
        "plan": forward.plan_snapshot_digest((selection,), _profile()),
        "identities": dict(forward.identity_digests),
    }


def test_set_like_descriptor_fields_have_canonical_order() -> None:
    left = _replace(
        _descriptors(),
        EvolutionMethodDescriptor,
        execution_modes=("subscription", "self_deployed"),
        capture_modes=("token_level", "transcript"),
        supported_harness_ids=("codex", "claude"),
        harness_requirements=("stable_transcript", "core_harness"),
        runtime_requirements=("trainer", "core_worker"),
        input_bindings=(
            MethodInputBinding(
                binding_id="report",
                source="explicit_inputs",
                artifact_type="report",
            ),
            MethodInputBinding(
                binding_id="dataset",
                source="current_dataset",
                artifact_type="dataset",
            ),
        ),
        output_artifact_types=("text_memory", "report"),
    )
    right = _replace(
        _descriptors(),
        EvolutionMethodDescriptor,
        execution_modes=("self_deployed", "subscription"),
        capture_modes=("transcript", "token_level"),
        supported_harness_ids=("claude", "codex"),
        harness_requirements=("core_harness", "stable_transcript"),
        runtime_requirements=("core_worker", "trainer"),
        input_bindings=(
            MethodInputBinding(
                binding_id="report",
                source="explicit_inputs",
                artifact_type="report",
            ),
            MethodInputBinding(
                binding_id="dataset",
                source="current_dataset",
                artifact_type="dataset",
            ),
        ),
        output_artifact_types=("report", "text_memory"),
    )
    assert _registry(left).freeze().registry_digest == _registry(right).freeze().registry_digest

    reversed_bindings = _replace(
        left,
        EvolutionMethodDescriptor,
        input_bindings=tuple(
            reversed(
                next(
                    item.input_bindings
                    for item in left
                    if isinstance(item, EvolutionMethodDescriptor)
                )
            )
        ),
    )
    assert (
        _registry(left).freeze().registry_digest
        != _registry(reversed_bindings).freeze().registry_digest
    )


def test_resolved_plan_records_exact_handler_identity() -> None:
    selection = EvolutionTargetSelection(
        target_id="memory",
        enabled=True,
        method_id="reflect",
    )
    resolved = _registry().freeze().compile_plan(
        plan_id="plan-1",
        selections=(selection,),
        profile=_profile(),
    ).selections[0]
    assert resolved.handler_id == "memory_handler"


def test_plan_digest_ignores_unrelated_plugin_and_tracks_reachable_changes() -> None:
    selection = EvolutionTargetSelection(
        target_id="memory", enabled=True, method_id="reflect"
    )
    baseline = _registry().freeze().plan_snapshot_digest((selection,), _profile())

    unrelated = _descriptors(
        target_id="skills",
        method_id="synthesize",
        artifact_type="skill_bundle",
    )
    assert (
        _registry(_descriptors() + unrelated)
        .freeze()
        .plan_snapshot_digest((selection,), _profile())
        == baseline
    )

    changed_method = _replace(
        _descriptors(),
        EvolutionMethodDescriptor,
        implementation_ref=_implementation("changed", "9"),
    )
    assert (
        _registry(changed_method)
        .freeze()
        .plan_snapshot_digest((selection,), _profile())
        != baseline
    )

    extra_method = next(
        descriptor
        for descriptor in _descriptors(method_id="alternate")
        if isinstance(descriptor, EvolutionMethodDescriptor)
    )
    assert (
        _registry(_descriptors() + (extra_method,))
        .freeze()
        .plan_snapshot_digest((selection,), _profile())
        == baseline
    )


def test_plan_digest_closes_over_referenced_default_method() -> None:
    descriptors = _descriptors()
    alternate = next(
        descriptor
        for descriptor in _descriptors(method_id="alternate")
        if isinstance(descriptor, EvolutionMethodDescriptor)
    )
    selection = EvolutionTargetSelection(
        target_id="memory",
        enabled=True,
        method_id="alternate",
    )
    baseline = _registry(descriptors + (alternate,)).freeze().plan_snapshot_digest(
        (selection,),
        _profile(),
    )
    changed_default = _replace(
        descriptors,
        EvolutionMethodDescriptor,
        implementation_ref=_implementation("changed_default", "8"),
    )
    assert (
        _registry(changed_default + (alternate,))
        .freeze()
        .plan_snapshot_digest((selection,), _profile())
        != baseline
    )


def test_snapshot_uses_defensive_views_not_mutable_dict_subclasses() -> None:
    snapshot = _registry().freeze()
    identity_before = snapshot.identity_digest_for("method", "reflect")
    method = snapshot.methods["reflect"]
    rounds_schema = method.config_schema["properties"]["settings"]["properties"][
        "rounds"
    ]
    dict.__setitem__(rounds_schema, "default", 8)
    assert snapshot.normalize_method_config("reflect", {}) == {
        "settings": {"rounds": 3, "mode": "safe"}
    }
    assert snapshot.identity_digest_for("method", "reflect") == identity_before
    identity = snapshot.identity_for("method", "reflect")
    identity.__dict__["descriptor_digest"] = "f" * 64
    assert snapshot.identity_for("method", "reflect").descriptor_digest != "f" * 64
    assert snapshot.identity_digest_for("method", "reflect") == identity_before
    with pytest.raises(TypeError):
        snapshot.methods["new"] = method
    with pytest.raises(AttributeError, match="immutable"):
        snapshot.methods._serialized = {"reflect": canonical_json(method)}


def test_registered_descriptor_is_detached_from_caller_mutation() -> None:
    descriptors = _descriptors()
    method = next(
        item for item in descriptors if isinstance(item, EvolutionMethodDescriptor)
    )
    registry = _registry(descriptors)
    with pytest.raises(TypeError, match="immutable"):
        method.default_config["settings"]["rounds"] = 8
    dict.__setitem__(method.default_config["settings"], "rounds", 8)
    assert registry.freeze().normalize_method_config("reflect", {}) == {
        "settings": {"rounds": 3, "mode": "safe"}
    }


def _handler_output(**updates: object) -> TargetHandlerOutput:
    content = b"Remember."
    payload = StagedPayloadContribution(
        contribution_id="memory_file",
        source_artifact_id="artifact-1",
        source_relative_path="memory.md",
        source_sha256=hashlib.sha256(content).hexdigest(),
        source_size_bytes=len(content),
        media_type="text/markdown",
        payload_kind="file",
        destination_scope="target_data",
        destination_relative_path="memory.md",
    )
    values = {
        "target_id": "memory",
        "handler_id": "memory_handler",
        "artifact_ids": ("artifact-1",),
        "staged_payloads": (payload,),
        "environment": (
            EnvironmentBinding(
                name="OPENEVO_MEMORY_FILE",
                value_contribution_ids=("memory_file",),
                value_kind="path",
            ),
        ),
        "renderer": RendererPayload(
            kind="markdown",
            title="Memory",
            source_contribution_ids=("memory_file",),
            data={"markdown": "Remember."},
        ),
        **updates,
    }
    return TargetHandlerOutput(**values)


def _handler_input(
    *,
    artifact_id: str = "artifact-1",
    artifact_type: str = "text_memory",
    uri_scheme: str = "file",
) -> TargetHandlerInput:
    content = b"Remember."
    entries = (
        PayloadManifestEntry(
            relative_path="memory.md",
            media_type="text/markdown",
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        ),
    )
    return TargetHandlerInput(
        target_id="memory",
        handler_id="memory_handler",
        execution_profile=_profile(),
        destination_roots=RuntimeDestinationRoots(
            target_data="/openevo/session/evolution",
            harness_skills="/openevo/session/evolution/skills",
            harness_instruction="/workspace/repo",
        ),
        ranked_artifacts=(
            TrustedArtifactSnapshot(
                artifact_id=artifact_id,
                artifact_type=artifact_type,
                name="Memory",
                uri_scheme=uri_scheme,
                payload_handle="payload-1",
                payload_entries=entries,
                payload_manifest_digest=payload_tree_digest(entries),
                rank_index=0,
            ),
        ),
    )


def _updated_payload(
    payload: StagedPayloadContribution,
    **updates: object,
) -> StagedPayloadContribution:
    values = payload.model_dump(mode="python")
    values.update(updates)
    return StagedPayloadContribution.model_validate(values)


@pytest.mark.parametrize(
    ("output", "handler_input", "message"),
    [
        (_handler_output(handler_id="other_handler"), _handler_input(), "target handler"),
        (_handler_output(), _handler_input(artifact_id="artifact-2"), "ranked input"),
        (_handler_output(), _handler_input(artifact_type="skill_bundle"), "artifact type"),
        (_handler_output(), _handler_input(uri_scheme="https"), "URI scheme"),
        (
            _handler_output(
                renderer=RendererPayload(
                    kind="file_bundle",
                    title="Wrong",
                    source_contribution_ids=("memory_file",),
                    data={
                        "files": [
                            {
                                "relative_path": "SKILL.md",
                                "media_type": "text/markdown",
                                "size_bytes": 12,
                                "sha256": "b" * 64,
                            }
                        ]
                    },
                )
            ),
            _handler_input(),
            "renderer contract",
        ),
    ],
)
def test_handler_output_validation_fails_closed(
    output: TargetHandlerOutput,
    handler_input: TargetHandlerInput,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _registry().freeze().validate_handler_output(
            output,
            handler_input=handler_input,
        )


def test_handler_output_policy_rejects_scope_mime_environment_and_collisions() -> None:
    baseline = _handler_output()
    payload = baseline.staged_payloads[0]
    invalid_outputs = (
        (
            _handler_output(
                staged_payloads=(
                    StagedPayloadContribution(
                        **payload.model_dump(exclude={"destination_scope"}),
                        destination_scope="harness_skills",
                    ),
                )
            ),
            "destination scope",
        ),
        (
            _handler_output(
                staged_payloads=(
                    _updated_payload(payload, media_type="application/json"),
                )
            ),
            "MIME type",
        ),
        (
            _handler_output(
                environment=(
                    EnvironmentBinding(
                        name="OPENEVO_UNDECLARED_FILE",
                        value_contribution_ids=("memory_file",),
                        value_kind="path",
                    ),
                )
            ),
            "environment binding",
        ),
        (
            _handler_output(
                staged_payloads=(
                    payload,
                    _updated_payload(payload, contribution_id="memory_copy"),
                )
            ),
            "destination collision",
        ),
        (
            _handler_output(
                staged_payloads=(
                    payload,
                    _updated_payload(
                        payload,
                        contribution_id="bundle",
                        destination_relative_path="bundle",
                    ),
                    _updated_payload(
                        payload,
                        contribution_id="nested",
                        destination_relative_path="bundle/memory.md",
                    ),
                ),
            ),
            "destination collision",
        ),
        (
            _handler_output(
                instructions=(
                    InstructionContribution(
                        contribution_id="instruction",
                        source_artifact_ids=("artifact-1",),
                        text="Remember.",
                    ),
                )
            ),
            "contribution kind",
        ),
    )
    snapshot = _registry().freeze()
    for output, message in invalid_outputs:
        with pytest.raises(ValueError, match=message):
            snapshot.validate_handler_output(
                output,
                handler_input=_handler_input(),
            )


def test_valid_handler_output_passes_registry_policy() -> None:
    validated = _registry().freeze().validate_handler_output(
        _handler_output(),
        handler_input=_handler_input(),
    )
    assert validated.artifact_ids == ("artifact-1",)


@pytest.mark.parametrize("path", [".ssh/authorized_keys", ".env", "docs/prompt.md"])
def test_harness_instruction_scope_uses_existing_path_allowlist(path: str) -> None:
    descriptors = _replace(
        _descriptors(),
        TargetHandlerDescriptor,
        allowed_destination_scopes=("target_data", "harness_instruction"),
    )
    baseline = _handler_output()
    payload = StagedPayloadContribution(
        **baseline.staged_payloads[0].model_dump(
            exclude={"destination_scope", "destination_relative_path"}
        ),
        destination_scope="harness_instruction",
        destination_relative_path=path,
    )
    output = _handler_output(staged_payloads=(payload,))
    with pytest.raises(ValueError, match="instruction path"):
        _registry(descriptors).freeze().validate_handler_output(
            output,
            handler_input=_handler_input(),
        )
