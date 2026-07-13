from __future__ import annotations

import hashlib

import pytest

from openevo.evolution.framework import (
    AdapterContribution,
    EnvironmentBinding,
    EvolutionExecutionProfile,
    EvolutionFrameworkRegistry,
    EvolutionMethodDescriptor,
    EvolutionTargetDescriptor,
    ImplementationRef,
    InlineTextPayloadContribution,
    InstructionContribution,
    PayloadManifestEntry,
    RendererPayload,
    RuntimeDestinationRoots,
    StagedPayloadContribution,
    TargetConsumptionLimits,
    TargetHandlerDescriptor,
    TargetHandlerInput,
    TargetHandlerOutput,
    TrustedArtifactSnapshot,
    canonical_json,
    payload_tree_digest,
    payload_tree_size,
)


def _implementation(name: str, digit: str) -> ImplementationRef:
    return ImplementationRef(
        distribution="openevo-test",
        distribution_version="1",
        distribution_digest=digit * 64,
        entry_point=f"openevo_test.{name}:implementation",
    )


def _registry(
    *,
    target_id: str = "memory",
    artifact_type: str = "text_memory",
    handler_id: str = "memory_handler",
    registry: EvolutionFrameworkRegistry | None = None,
    destination_scopes: tuple[str, ...] = ("target_data",),
    contribution_kinds: tuple[str, ...] = ("staged_payload", "environment"),
    renderer_kind: str = "markdown",
) -> EvolutionFrameworkRegistry:
    registry = registry or EvolutionFrameworkRegistry()
    registry.register_target(
        EvolutionTargetDescriptor(
            id=target_id,
            display_name=target_id,
            description=f"{target_id} target",
            artifact_type=artifact_type,
            handler_id=handler_id,
            renderer_kind=renderer_kind,
            default_method_id=f"{target_id}_method",
            implementation_ref=_implementation(f"{target_id}_target", "1"),
        )
    )
    registry.register_target_handler(
        TargetHandlerDescriptor(
            id=handler_id,
            target_id=target_id,
            artifact_types=(artifact_type,),
            renderer_kind=renderer_kind,
            allowed_uri_schemes=("file",),
            allowed_media_types=("text/markdown",),
            allowed_destination_scopes=destination_scopes,
            environment_allowlist=("OPENEVO_MEMORY_FILE",),
            allowed_contribution_kinds=contribution_kinds,
            implementation_ref=_implementation(f"{target_id}_handler", "2"),
        )
    )
    registry.register_method(
        EvolutionMethodDescriptor(
            id=f"{target_id}_method",
            display_name="Method",
            description="Method",
            target_id=target_id,
            invocation_abi="method_context_v1",
            execution_modes=("self_deployed",),
            capture_modes=("transcript",),
            supported_harness_ids=("codex",),
            output_artifact_types=(artifact_type,),
            implementation_ref=_implementation(f"{target_id}_method", "3"),
        )
    )
    return registry


def _entry(content: bytes, path: str = "memory.md") -> PayloadManifestEntry:
    return PayloadManifestEntry(
        relative_path=path,
        media_type="text/markdown",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _artifact(
    artifact_id: str,
    content: bytes,
    *,
    rank_index: int,
    artifact_type: str = "text_memory",
    manifest: dict[str, object] | None = None,
) -> TrustedArtifactSnapshot:
    entries = (_entry(content),)
    return TrustedArtifactSnapshot(
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        name=artifact_id,
        uri_scheme="file",
        payload_handle=f"payload-{artifact_id}",
        payload_entries=entries,
        payload_manifest_digest=payload_tree_digest(entries),
        manifest_json=canonical_json(manifest or {}),
        scores_json="{}",
        rank_index=rank_index,
    )


def _profile() -> EvolutionExecutionProfile:
    return EvolutionExecutionProfile(
        execution_mode="self_deployed",
        capture_mode="transcript",
        harness_id="codex",
    )


def _roots(
    *,
    instruction: str = "/workspace/repo",
) -> RuntimeDestinationRoots:
    return RuntimeDestinationRoots(
        target_data="/openevo/session/evolution",
        harness_skills="/openevo/session/evolution/skills",
        harness_instruction=instruction,
    )


def _input(*artifacts: TrustedArtifactSnapshot) -> TargetHandlerInput:
    return TargetHandlerInput(
        target_id="memory",
        handler_id="memory_handler",
        execution_profile=_profile(),
        destination_roots=_roots(),
        limits=TargetConsumptionLimits(max_text_chars=12_000),
        ranked_artifacts=artifacts,
    )


def _output(
    *artifact_ids: str,
    destination: str = "memory.md",
    environment_name: str = "OPENEVO_MEMORY_FILE",
) -> TargetHandlerOutput:
    content = b"First"
    payload = StagedPayloadContribution(
        contribution_id="memory_file",
        source_artifact_id=artifact_ids[0],
        source_relative_path="memory.md",
        source_sha256=hashlib.sha256(content).hexdigest(),
        source_size_bytes=len(content),
        media_type="text/markdown",
        payload_kind="file",
        destination_scope="target_data",
        destination_relative_path=destination,
    )
    return TargetHandlerOutput(
        target_id="memory",
        handler_id="memory_handler",
        artifact_ids=artifact_ids,
        staged_payloads=(payload,),
        environment=(
            EnvironmentBinding(
                name=environment_name,
                value_contribution_ids=("memory_file",),
                value_kind="path",
            ),
        ),
        renderer=RendererPayload(
            kind="markdown",
            title="Memory",
            source_contribution_ids=("memory_file",),
            data={"markdown": "First"},
        ),
    )


def _inline_output(
    *artifact_ids: str,
    text: str,
) -> TargetHandlerOutput:
    return TargetHandlerOutput(
        target_id="memory",
        handler_id="memory_handler",
        artifact_ids=artifact_ids,
        staged_payloads=(
            InlineTextPayloadContribution(
                contribution_id="memory_file",
                source_artifact_ids=artifact_ids,
                text=text,
                media_type="text/markdown",
                destination_scope="target_data",
                destination_relative_path="memory.md",
            ),
        ),
        renderer=RendererPayload(
            kind="markdown",
            title="Memory",
            source_contribution_ids=("memory_file",),
            data={"markdown": text},
        ),
    )


def test_handler_input_preserves_existing_resolver_rank_order() -> None:
    first = _artifact("artifact-high", b"First", rank_index=0)
    second = _artifact("artifact-low", b"Second", rank_index=1)
    handler_input = _input(first, second)
    assert tuple(item.artifact_id for item in handler_input.ranked_artifacts) == (
        "artifact-high",
        "artifact-low",
    )
    assert "uri" not in TrustedArtifactSnapshot.model_fields
    assert "path" not in TrustedArtifactSnapshot.model_fields


def test_handler_output_is_bound_to_trusted_payload_manifest() -> None:
    first = _artifact("artifact-high", b"First", rank_index=0)
    second = _artifact("artifact-low", b"Second", rank_index=1)
    validated = _registry().freeze().validate_handler_output(
        _inline_output(
            "artifact-high",
            "artifact-low",
            text="First\n\nSecond",
        ),
        handler_input=_input(first, second),
    )
    assert validated.artifact_ids == ("artifact-high", "artifact-low")

    tampered = _output("artifact-high").model_dump(mode="python")
    tampered["staged_payloads"][0]["source_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="payload digest"):
        _registry().freeze().validate_handler_output(
            TargetHandlerOutput.model_validate(tampered),
            handler_input=_input(first),
        )

    renderer_mismatch = _output("artifact-high").model_dump(mode="python")
    renderer_mismatch["renderer"]["data"]["markdown"] = "Different"
    with pytest.raises(ValueError, match="renderer does not match"):
        _registry().freeze().validate_handler_output(
            TargetHandlerOutput.model_validate(renderer_mismatch),
            handler_input=_input(first),
        )

    zero_text_limit = _input(first).model_copy(
        update={"limits": TargetConsumptionLimits(max_text_chars=0)}
    )
    with pytest.raises(ValueError, match="text consumption limit"):
        _registry().freeze().validate_handler_output(
            _output("artifact-high"),
            handler_input=zero_text_limit,
        )

    html_entry = PayloadManifestEntry(
        relative_path="memory.html",
        media_type="text/html",
        size_bytes=5,
        sha256=hashlib.sha256(b"First").hexdigest(),
    )
    html_artifact = TrustedArtifactSnapshot(
        artifact_id="artifact-html",
        artifact_type="text_memory",
        name="artifact-html",
        uri_scheme="file",
        payload_handle="payload-artifact-html",
        payload_entries=(html_entry,),
        payload_manifest_digest=payload_tree_digest((html_entry,)),
        rank_index=0,
    )
    with pytest.raises(ValueError, match="source MIME"):
        _registry().freeze().validate_handler_output(
            _inline_output("artifact-html", text="First"),
            handler_input=_input(html_artifact),
        )


def test_handler_output_artifacts_must_be_ranked_input_subsequence() -> None:
    first = _artifact("artifact-high", b"First", rank_index=0)
    second = _artifact("artifact-low", b"Second", rank_index=1)
    with pytest.raises(ValueError, match="ranked input order"):
        _registry().freeze().validate_handler_output(
            _inline_output(
                "artifact-low",
                "artifact-high",
                text="Second\n\nFirst",
            ),
            handler_input=_input(first, second),
        )

    with pytest.raises(ValueError, match="source artifact order"):
        TargetHandlerOutput(
            **_inline_output(
                "artifact-high",
                "artifact-low",
                text="First\n\nSecond",
            ).model_dump(exclude={"staged_payloads"}),
            staged_payloads=(
                InlineTextPayloadContribution(
                    contribution_id="memory_file",
                    source_artifact_ids=("artifact-low", "artifact-high"),
                    text="First\n\nSecond",
                    media_type="text/markdown",
                    destination_scope="target_data",
                    destination_relative_path="memory.md",
                ),
            ),
        )


def test_text_limit_counts_one_semantic_text_across_runtime_projections() -> None:
    registry = _registry(
        contribution_kinds=("instruction", "staged_payload"),
    ).freeze()
    artifact = _artifact("artifact-high", b"First", rank_index=0)
    output = TargetHandlerOutput(
        target_id="memory",
        handler_id="memory_handler",
        artifact_ids=("artifact-high",),
        instructions=(
            InstructionContribution(
                contribution_id="instruction",
                source_artifact_ids=("artifact-high",),
                text="First",
            ),
        ),
        staged_payloads=(
            InlineTextPayloadContribution(
                contribution_id="memory_file",
                source_artifact_ids=("artifact-high",),
                text="First",
                media_type="text/markdown",
                destination_scope="target_data",
                destination_relative_path="memory.md",
            ),
        ),
        renderer=RendererPayload(
            kind="markdown",
            title="Memory",
            source_contribution_ids=("memory_file",),
            data={"markdown": "First"},
        ),
    )
    handler_input = _input(artifact).model_copy(
        update={"limits": TargetConsumptionLimits(max_text_chars=5)}
    )
    assert registry.validate_handler_output(
        output,
        handler_input=handler_input,
    ).artifact_ids == ("artifact-high",)

    zero_limit = handler_input.model_copy(
        update={"limits": TargetConsumptionLimits(max_text_chars=0)}
    )
    with pytest.raises(ValueError, match="text consumption limit"):
        registry.validate_handler_output(output, handler_input=zero_limit)


def test_text_limit_does_not_deduplicate_same_category_instructions() -> None:
    snapshot = _registry(contribution_kinds=("instruction",)).freeze()
    artifact = _artifact("artifact-high", b"First", rank_index=0)
    text = "x" * 200_000
    output = TargetHandlerOutput(
        target_id="memory",
        handler_id="memory_handler",
        artifact_ids=("artifact-high",),
        instructions=(
            InstructionContribution(
                contribution_id="instruction_1",
                source_artifact_ids=("artifact-high",),
                text=text,
            ),
            InstructionContribution(
                contribution_id="instruction_2",
                source_artifact_ids=("artifact-high",),
                text=text,
            ),
        ),
        renderer=RendererPayload(
            kind="markdown",
            title="Memory",
            source_contribution_ids=("instruction_1",),
            data={"markdown": text},
        ),
    )
    handler_input = _input(artifact).model_copy(
        update={"limits": TargetConsumptionLimits(max_text_chars=300_000)}
    )
    with pytest.raises(ValueError, match="text consumption limit"):
        snapshot.validate_handler_output(output, handler_input=handler_input)


def test_text_projection_has_an_independent_bounded_copy_limit() -> None:
    snapshot = _registry(
        contribution_kinds=("instruction", "staged_payload"),
    ).freeze()
    artifact = _artifact("artifact-high", b"First", rank_index=0)
    output = TargetHandlerOutput(
        target_id="memory",
        handler_id="memory_handler",
        artifact_ids=("artifact-high",),
        instructions=(
            InstructionContribution(
                contribution_id="instruction",
                source_artifact_ids=("artifact-high",),
                text="x",
            ),
        ),
        staged_payloads=(
            InlineTextPayloadContribution(
                contribution_id="memory_file",
                source_artifact_ids=("artifact-high",),
                text="y" * 20,
                media_type="text/markdown",
                destination_scope="target_data",
                destination_relative_path="memory.md",
            ),
        ),
        renderer=RendererPayload(
            kind="markdown",
            title="Memory",
            source_contribution_ids=("instruction",),
            data={"markdown": "x"},
        ),
    )
    handler_input = _input(artifact).model_copy(
        update={
            "limits": TargetConsumptionLimits(
                max_text_chars=10,
                max_text_bytes=10,
            )
        }
    )

    with pytest.raises(ValueError, match="text projection limit"):
        snapshot.validate_handler_output(output, handler_input=handler_input)


def test_semantic_text_has_an_independent_utf8_byte_limit() -> None:
    snapshot = _registry(contribution_kinds=("instruction",)).freeze()
    artifact = _artifact("artifact-high", b"First", rank_index=0)
    text = "界" * 400_000
    output = TargetHandlerOutput(
        target_id="memory",
        handler_id="memory_handler",
        artifact_ids=("artifact-high",),
        instructions=(
            InstructionContribution(
                contribution_id="instruction",
                source_artifact_ids=("artifact-high",),
                text=text,
            ),
        ),
        renderer=RendererPayload(
            kind="markdown",
            title="Memory",
            source_contribution_ids=("instruction",),
            data={"markdown": text[:100]},
        ),
    )
    handler_input = _input(artifact).model_copy(
        update={
            "limits": TargetConsumptionLimits(
                max_text_chars=500_000,
                max_text_bytes=1_048_576,
            )
        }
    )
    with pytest.raises(ValueError, match="UTF-8 text byte limit"):
        snapshot.validate_handler_output(output, handler_input=handler_input)


def test_destination_conflicts_use_resolved_runtime_paths() -> None:
    snapshot = _registry(
        destination_scopes=("target_data", "harness_skills"),
    ).freeze()
    artifact = _artifact("artifact-high", b"First", rank_index=0)
    baseline = _output("artifact-high")
    payload = baseline.staged_payloads[0]
    second_data = payload.model_dump(mode="python")
    second_data.update(
        contribution_id="skill_copy",
        destination_scope="harness_skills",
        destination_relative_path="collision",
    )
    first_data = payload.model_dump(mode="python")
    first_data.update(
        destination_relative_path="skills/collision",
    )
    data = baseline.model_dump(mode="python")
    data["staged_payloads"] = [first_data, second_data]
    with pytest.raises(ValueError, match="destination collision"):
        snapshot.validate_handler_output(
            TargetHandlerOutput.model_validate(data),
            handler_input=_input(artifact),
        )

    instruction_snapshot = _registry(
        destination_scopes=("target_data", "harness_instruction"),
    ).freeze()
    instruction_data = baseline.model_dump(mode="python")
    target_payload = payload.model_dump(mode="python")
    target_payload["destination_relative_path"] = "AGENTS.md"
    instruction_payload = payload.model_dump(mode="python")
    instruction_payload.update(
        contribution_id="instruction_copy",
        destination_scope="harness_instruction",
        destination_relative_path="AGENTS.md",
    )
    instruction_data["staged_payloads"] = [target_payload, instruction_payload]
    same_root_input = _input(artifact).model_copy(
        update={
            "destination_roots": _roots(
                instruction="/openevo/session/evolution"
            )
        }
    )
    with pytest.raises(ValueError, match="destination collision"):
        instruction_snapshot.validate_handler_output(
            TargetHandlerOutput.model_validate(instruction_data),
            handler_input=same_root_input,
        )


def test_each_contribution_preserves_first_artifact_use_order() -> None:
    snapshot = _registry(
        contribution_kinds=("instruction", "staged_payload"),
    ).freeze()
    high = _artifact("artifact-high", b"First", rank_index=0)
    low = _artifact("artifact-low", b"Second", rank_index=1)
    staged = _output("artifact-high").staged_payloads[0]
    output = TargetHandlerOutput(
        target_id="memory",
        handler_id="memory_handler",
        artifact_ids=("artifact-high", "artifact-low"),
        instructions=(
            InstructionContribution(
                contribution_id="instruction",
                source_artifact_ids=("artifact-low",),
                text="Second",
            ),
        ),
        staged_payloads=(staged,),
        renderer=RendererPayload(
            kind="markdown",
            title="Memory",
            source_contribution_ids=("memory_file",),
            data={"markdown": "First"},
        ),
    )
    with pytest.raises(ValueError, match="first source artifact order"):
        snapshot.validate_handler_output(
            output,
            handler_input=_input(high, low),
        )


def test_scope_root_environment_binding_is_explicit_and_allowlisted() -> None:
    artifact = _artifact("artifact-high", b"First", rank_index=0)
    output_data = _output("artifact-high").model_dump(mode="python")
    output_data["environment"] = [
        {
            "name": "OPENEVO_MEMORY_FILE",
            "value_kind": "scope_root",
            "destination_scope": "target_data",
        }
    ]
    validated = _registry().freeze().validate_handler_output(
        TargetHandlerOutput.model_validate(output_data),
        handler_input=_input(artifact),
    )
    assert validated.environment[0].destination_scope == "target_data"

    invalid = output_data.copy()
    invalid["environment"] = [
        {
            "name": "OPENEVO_MEMORY_FILE",
            "value_contribution_ids": ["memory_file"],
            "value_kind": "scope_root",
            "destination_scope": "target_data",
        }
    ]
    with pytest.raises(ValueError, match="scope-root"):
        TargetHandlerOutput.model_validate(invalid)


def test_file_bundle_renderer_can_cover_multiple_staged_directories() -> None:
    snapshot = _registry(
        target_id="skills",
        artifact_type="skill_bundle",
        handler_id="skills_handler",
        destination_scopes=("harness_skills",),
        contribution_kinds=("staged_payload", "environment"),
        renderer_kind="file_bundle",
    ).freeze()
    high_entry = _entry(b"First", "SKILL.md")
    low_entry = _entry(b"Second", "SKILL.md")

    def skill_artifact(
        artifact_id: str,
        entry: PayloadManifestEntry,
        rank: int,
    ) -> TrustedArtifactSnapshot:
        return TrustedArtifactSnapshot(
            artifact_id=artifact_id,
            artifact_type="skill_bundle",
            name=artifact_id,
            uri_scheme="file",
            payload_handle=f"payload-{artifact_id}",
            payload_entries=(entry,),
            payload_manifest_digest=payload_tree_digest((entry,)),
            rank_index=rank,
        )

    artifacts = (
        skill_artifact("skill-high", high_entry, 0),
        skill_artifact("skill-low", low_entry, 1),
    )
    payloads = tuple(
        StagedPayloadContribution(
            contribution_id=f"skill_{index}",
            source_artifact_id=artifact.artifact_id,
            source_relative_path=".",
            source_sha256=artifact.payload_manifest_digest,
            source_size_bytes=sum(
                item.size_bytes for item in artifact.payload_entries
            ),
            media_type="text/markdown",
            payload_kind="directory",
            destination_scope="harness_skills",
            destination_relative_path=artifact.artifact_id,
        )
        for index, artifact in enumerate(artifacts)
    )
    output = TargetHandlerOutput(
        target_id="skills",
        handler_id="skills_handler",
        artifact_ids=tuple(item.artifact_id for item in artifacts),
        staged_payloads=payloads,
        environment=(
            EnvironmentBinding(
                name="OPENEVO_MEMORY_FILE",
                value_kind="scope_root",
                destination_scope="harness_skills",
            ),
        ),
        renderer=RendererPayload(
            kind="file_bundle",
            title="Skills",
            source_contribution_ids=tuple(item.contribution_id for item in payloads),
            data={
                "files": [
                    {
                        "relative_path": f"{artifact.artifact_id}/SKILL.md",
                        "media_type": entry.media_type,
                        "size_bytes": entry.size_bytes,
                        "sha256": entry.sha256,
                    }
                    for artifact, entry in zip(artifacts, (high_entry, low_entry))
                ]
            },
        ),
    )
    handler_input = TargetHandlerInput(
        target_id="skills",
        handler_id="skills_handler",
        execution_profile=_profile(),
        destination_roots=_roots(),
        ranked_artifacts=artifacts,
    )

    assert snapshot.validate_handler_output(
        output,
        handler_input=handler_input,
    ).artifact_ids == ("skill-high", "skill-low")

    reversed_renderer = output.renderer.model_copy(
        update={
            "data": output.renderer.data.model_copy(
                update={"files": tuple(reversed(output.renderer.data.files))}
            )
        }
    )
    with pytest.raises(ValueError, match="renderer file order"):
        snapshot.validate_handler_output(
            output.model_copy(update={"renderer": reversed_renderer}),
            handler_input=handler_input,
        )


def test_environment_json_paths_preserve_staged_payload_order() -> None:
    high_payload = InlineTextPayloadContribution(
        contribution_id="high_path",
        source_artifact_ids=("artifact-high",),
        text="First",
        media_type="text/markdown",
        destination_scope="target_data",
        destination_relative_path="high.md",
    )
    low_payload = InlineTextPayloadContribution(
        contribution_id="low_path",
        source_artifact_ids=("artifact-low",),
        text="Second",
        media_type="text/markdown",
        destination_scope="target_data",
        destination_relative_path="low.md",
    )
    with pytest.raises(ValueError, match="preserve staged payload order"):
        TargetHandlerOutput(
            target_id="memory",
            handler_id="memory_handler",
            artifact_ids=("artifact-high", "artifact-low"),
            staged_payloads=(high_payload, low_payload),
            environment=(
                EnvironmentBinding(
                    name="OPENEVO_MEMORY_FILE",
                    value_contribution_ids=("low_path", "high_path"),
                    value_kind="json_paths",
                ),
            ),
            renderer=RendererPayload(
                kind="markdown",
                title="Memory",
                source_contribution_ids=("high_path",),
                data={"markdown": "First"},
            ),
        )


def test_context_aggregation_rejects_cross_target_runtime_conflicts() -> None:
    registry = EvolutionFrameworkRegistry()
    _registry(registry=registry)
    _registry(
        target_id="agent_system",
        artifact_type="agent_system",
        handler_id="agent_system_handler",
        registry=registry,
    )
    snapshot = registry.freeze()

    memory_artifact = _artifact("memory-artifact", b"First", rank_index=0)
    agent_artifact = _artifact(
        "agent-artifact",
        b"First",
        rank_index=0,
        artifact_type="agent_system",
    )
    agent_input = TargetHandlerInput(
        target_id="agent_system",
        handler_id="agent_system_handler",
        execution_profile=_profile(),
        destination_roots=_roots(),
        limits=TargetConsumptionLimits(max_text_chars=12_000),
        ranked_artifacts=(agent_artifact,),
    )
    agent_output_data = _output(
        "agent-artifact",
        destination="memory.md/nested",
    ).model_dump(mode="python")
    agent_output_data.update(
        target_id="agent_system",
        handler_id="agent_system_handler",
    )

    with pytest.raises(ValueError, match="cross-target destination conflict"):
        snapshot.validate_handler_outputs(
            (
                (_input(memory_artifact), _output("memory-artifact")),
                (agent_input, TargetHandlerOutput.model_validate(agent_output_data)),
            )
        )

    nonconflicting_data = agent_output_data.copy()
    nonconflicting_data["staged_payloads"] = [
        {
            **nonconflicting_data["staged_payloads"][0],
            "destination_relative_path": "agent_system.md",
        }
    ]
    with pytest.raises(ValueError, match="cross-target environment conflict"):
        snapshot.validate_handler_outputs(
            (
                (_input(memory_artifact), _output("memory-artifact")),
                (
                    agent_input,
                    TargetHandlerOutput.model_validate(nonconflicting_data),
                ),
            )
        )


def test_payload_tree_digest_is_canonical_and_rooted() -> None:
    first = _entry(b"a", "nested/a.md")
    second = _entry(b"b", "nested/b.md")
    assert payload_tree_digest((first, second)) == payload_tree_digest((second, first))
    assert payload_tree_digest((first, second), root="nested") != payload_tree_digest(
        (first, second)
    )


def test_directory_payload_checks_every_manifest_entry_mime() -> None:
    snapshot = _registry(
        target_id="skills",
        artifact_type="skill_bundle",
        handler_id="skills_handler",
        destination_scopes=("harness_skills",),
        contribution_kinds=("staged_payload",),
        renderer_kind="file_bundle",
    ).freeze()
    entries = (
        _entry(b"skill", "bundle/SKILL.md"),
        PayloadManifestEntry(
            relative_path="bundle/run.bin",
            media_type="application/x-executable",
            size_bytes=3,
            sha256=hashlib.sha256(b"bin").hexdigest(),
        ),
    )
    artifact = TrustedArtifactSnapshot(
        artifact_id="skill-artifact",
        artifact_type="skill_bundle",
        name="Skill",
        uri_scheme="file",
        payload_handle="payload-skill",
        payload_entries=entries,
        payload_manifest_digest=payload_tree_digest(entries),
        rank_index=0,
    )
    payload = StagedPayloadContribution(
        contribution_id="skill_dir",
        source_artifact_id="skill-artifact",
        source_relative_path="bundle",
        source_sha256=payload_tree_digest(entries, root="bundle"),
        source_size_bytes=payload_tree_size(entries, root="bundle"),
        media_type="text/markdown",
        payload_kind="directory",
        destination_scope="harness_skills",
        destination_relative_path="skill",
    )
    output = TargetHandlerOutput(
        target_id="skills",
        handler_id="skills_handler",
        artifact_ids=("skill-artifact",),
        staged_payloads=(payload,),
        renderer=RendererPayload(
            kind="file_bundle",
            title="Skill",
            source_contribution_ids=("skill_dir",),
            data={
                "files": [
                    {
                        "relative_path": "SKILL.md",
                        "media_type": "text/markdown",
                        "size_bytes": 5,
                        "sha256": hashlib.sha256(b"skill").hexdigest(),
                    },
                    {
                        "relative_path": "run.bin",
                        "media_type": "application/x-executable",
                        "size_bytes": 3,
                        "sha256": hashlib.sha256(b"bin").hexdigest(),
                    },
                ]
            },
        ),
    )
    handler_input = TargetHandlerInput(
        target_id="skills",
        handler_id="skills_handler",
        execution_profile=_profile(),
        destination_roots=_roots(),
        ranked_artifacts=(artifact,),
    )
    with pytest.raises(ValueError, match="directory contains a disallowed MIME"):
        snapshot.validate_handler_output(output, handler_input=handler_input)


def test_adapter_output_is_bound_to_request_model_and_runtime_limit() -> None:
    snapshot = _registry(
        target_id="parametric_memory",
        artifact_type="parametric_memory",
        handler_id="adapter_handler",
        contribution_kinds=("adapter",),
        renderer_kind="adapter",
    ).freeze()
    artifact = _artifact(
        "adapter-artifact",
        b"adapter",
        rank_index=0,
        artifact_type="parametric_memory",
        manifest={
            "adapter_id": "adapter-1",
            "adapter_format": "lora",
            "base_model": "Qwen/base",
        },
    )
    handler_input = TargetHandlerInput(
        target_id="parametric_memory",
        handler_id="adapter_handler",
        execution_profile=_profile(),
        destination_roots=_roots(),
        base_model="Qwen/base",
        ranked_artifacts=(artifact,),
    )

    def output(*adapters: AdapterContribution) -> TargetHandlerOutput:
        first_adapter = adapters[0]
        return TargetHandlerOutput(
            target_id="parametric_memory",
            handler_id="adapter_handler",
            artifact_ids=("adapter-artifact",),
            adapters=adapters,
            renderer=RendererPayload(
                kind="adapter",
                title="Adapter",
                source_contribution_ids=(first_adapter.contribution_id,),
                data={
                    "adapter_id": first_adapter.adapter_id,
                    "adapter_format": first_adapter.adapter_format,
                    "base_model": first_adapter.base_model,
                },
            ),
        )

    wrong_model = AdapterContribution(
        contribution_id="adapter",
        source_artifact_id="adapter-artifact",
        source_payload_digest=artifact.payload_manifest_digest,
        source_size_bytes=sum(
            entry.size_bytes for entry in artifact.payload_entries
        ),
        adapter_id="adapter-1",
        adapter_format="lora",
        base_model="Other/base",
    )
    with pytest.raises(ValueError, match="requested base model"):
        snapshot.validate_handler_output(
            output(wrong_model),
            handler_input=handler_input,
        )

    first_adapter = wrong_model.model_copy(
        update={"base_model": "Qwen/base"}
    )
    assert snapshot.validate_handler_output(
        output(first_adapter),
        handler_input=handler_input,
    ).adapters == (first_adapter,)

    forged = first_adapter.model_copy(update={"adapter_id": "forged"})
    with pytest.raises(ValueError, match="source artifact manifest"):
        snapshot.validate_handler_output(output(forged), handler_input=handler_input)

    forged_payload = first_adapter.model_copy(
        update={"source_payload_digest": "0" * 64}
    )
    with pytest.raises(ValueError, match="source payload inventory"):
        snapshot.validate_handler_output(
            output(forged_payload),
            handler_input=handler_input,
        )


def test_adapter_application_limit_is_context_wide() -> None:
    registry = EvolutionFrameworkRegistry()
    _registry(
        target_id="parametric_memory",
        artifact_type="parametric_memory",
        handler_id="adapter_handler",
        contribution_kinds=("adapter",),
        renderer_kind="adapter",
        registry=registry,
    )
    _registry(
        target_id="secondary_parametric_memory",
        artifact_type="parametric_memory",
        handler_id="secondary_adapter_handler",
        contribution_kinds=("adapter",),
        renderer_kind="adapter",
        registry=registry,
    )
    snapshot = registry.freeze()

    def pair(
        target_id: str,
        handler_id: str,
        artifact_id: str,
        adapter_id: str,
    ) -> tuple[TargetHandlerInput, TargetHandlerOutput]:
        artifact = _artifact(
            artifact_id,
            b"adapter",
            rank_index=0,
            artifact_type="parametric_memory",
            manifest={
                "adapter_id": adapter_id,
                "adapter_format": "lora",
                "base_model": "Qwen/base",
            },
        )
        handler_input = TargetHandlerInput(
            target_id=target_id,
            handler_id=handler_id,
            execution_profile=_profile(),
            destination_roots=_roots(),
            base_model="Qwen/base",
            ranked_artifacts=(artifact,),
        )
        adapter = AdapterContribution(
            contribution_id=f"contribution_{adapter_id.replace('-', '_')}",
            source_artifact_id=artifact_id,
            source_payload_digest=artifact.payload_manifest_digest,
            source_size_bytes=sum(
                entry.size_bytes for entry in artifact.payload_entries
            ),
            adapter_id=adapter_id,
            adapter_format="lora",
            base_model="Qwen/base",
        )
        output = TargetHandlerOutput(
            target_id=target_id,
            handler_id=handler_id,
            artifact_ids=(artifact_id,),
            adapters=(adapter,),
            renderer=RendererPayload(
                kind="adapter",
                title="Adapter",
                source_contribution_ids=(adapter.contribution_id,),
                data={
                    "adapter_id": adapter.adapter_id,
                    "adapter_format": adapter.adapter_format,
                    "base_model": adapter.base_model,
                },
            ),
        )
        return handler_input, output

    with pytest.raises(ValueError, match="context exceeds.*adapter application limit"):
        snapshot.validate_handler_outputs(
            (
                pair(
                    "parametric_memory",
                    "adapter_handler",
                    "adapter-artifact-1",
                    "adapter-1",
                ),
                pair(
                    "secondary_parametric_memory",
                    "secondary_adapter_handler",
                    "adapter-artifact-2",
                    "adapter-2",
                ),
            )
        )


def test_structured_summary_fields_bind_to_renderer_sources() -> None:
    snapshot = _registry(
        contribution_kinds=("instruction",),
        renderer_kind="structured_summary",
    ).freeze()
    artifact = _artifact("artifact-high", b"First", rank_index=0)
    instruction = InstructionContribution(
        contribution_id="instruction",
        source_artifact_ids=("artifact-high",),
        text="First",
    )
    output = TargetHandlerOutput(
        target_id="memory",
        handler_id="memory_handler",
        artifact_ids=("artifact-high",),
        instructions=(instruction,),
        renderer=RendererPayload(
            kind="structured_summary",
            title="Summary",
            source_contribution_ids=("instruction",),
            data={
                "fields": [
                    {
                        "source_contribution_id": "unrelated",
                        "label": "Result",
                        "value": "First",
                    }
                ]
            },
        ),
    )
    with pytest.raises(ValueError, match="summary fields"):
        snapshot.validate_handler_output(output, handler_input=_input(artifact))

    forged_data = output.model_dump(mode="python")
    forged_data["renderer"]["data"]["fields"][0].update(
        source_contribution_id="instruction",
        value="Forged",
    )
    with pytest.raises(ValueError, match="must match referenced text"):
        snapshot.validate_handler_output(
            TargetHandlerOutput.model_validate(forged_data),
            handler_input=_input(artifact),
        )
