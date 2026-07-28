from __future__ import annotations

import hashlib
import inspect

import pytest

from openevo.evolution.framework import (
    EvolutionExecutionProfile,
    PayloadManifestEntry,
    RuntimeDestinationRoots,
    TargetConsumptionLimits,
    TargetHandlerInput,
    TargetHandlerServices,
    TrustedArtifactSnapshot,
    canonical_json,
    payload_tree_digest,
)
from openevo.evolution.framework.builtin_handlers import (
    BUILTIN_HANDLER_REGISTRY,
    agent_system_handler,
    parametric_memory_handler,
    skill_bundle_handler,
    text_memory_handler,
)
from openevo.evolution.framework.builtins import (
    ImplementationDistributionIdentity,
    build_builtin_registry,
)


@pytest.fixture(scope="module")
def builtin_snapshot():
    return build_builtin_registry(
        ImplementationDistributionIdentity(
            distribution="openevo",
            distribution_version="0.1.0",
            distribution_digest="a" * 64,
        )
    )


class FakePayloadService:
    def __init__(self, payloads: dict[tuple[str, str], bytes]) -> None:
        self.payloads = payloads
        self.reads: list[tuple[str, str, int, int]] = []

    def read_utf8_prefix(
        self,
        payload_handle: str,
        relative_path: str,
        *,
        max_chars: int,
        max_bytes: int,
    ) -> str:
        self.reads.append((payload_handle, relative_path, max_chars, max_bytes))
        value = self.payloads[(payload_handle, relative_path)]
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("payload is not valid UTF-8") from exc
        candidate = text[:max_chars]
        while candidate and len(candidate.encode("utf-8")) > max_bytes:
            candidate = candidate[:-1]
        return candidate


def _entry(path: str, content: bytes, media_type: str) -> PayloadManifestEntry:
    return PayloadManifestEntry(
        relative_path=path,
        media_type=media_type,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _artifact(
    artifact_id: str,
    artifact_type: str,
    rank_index: int,
    files: dict[str, tuple[bytes, str]],
    *,
    manifest: dict[str, object] | None = None,
    name: str | None = None,
) -> tuple[TrustedArtifactSnapshot, dict[tuple[str, str], bytes]]:
    entries = tuple(_entry(path, content, media) for path, (content, media) in files.items())
    handle = f"payload-{hashlib.sha256(artifact_id.encode()).hexdigest()[:16]}"
    return (
        TrustedArtifactSnapshot(
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            name=name or artifact_id,
            uri_scheme="file",
            payload_handle=handle,
            payload_entries=entries,
            payload_manifest_digest=payload_tree_digest(entries),
            manifest_json=canonical_json(manifest or {}),
            scores_json="{}",
            rank_index=rank_index,
        ),
        {(handle, path): content for path, (content, _) in files.items()},
    )


def _input(
    target_id: str,
    artifacts: tuple[TrustedArtifactSnapshot, ...],
    *,
    mode: str = "self_deployed",
    base_model: str | None = None,
    limits: TargetConsumptionLimits | None = None,
    runtime_capabilities: tuple[str, ...] = (),
) -> TargetHandlerInput:
    return TargetHandlerInput(
        target_id=target_id,
        handler_id=f"{target_id}_handler",
        execution_profile=EvolutionExecutionProfile(
            execution_mode=mode,
            capture_mode="transcript",
            harness_id="codex",
            runtime_capabilities=runtime_capabilities,
        ),
        destination_roots=RuntimeDestinationRoots(
            target_data="/openevo/session/evolution",
            harness_skills="/openevo/session/evolution/skills",
            harness_instruction="/workspace/repo",
        ),
        base_model=base_model,
        limits=limits or TargetConsumptionLimits(),
        ranked_artifacts=artifacts,
    )


def _services(*payload_maps: dict[tuple[str, str], bytes]) -> TargetHandlerServices:
    return TargetHandlerServices(
        payloads=FakePayloadService(
            {key: value for item in payload_maps for key, value in item.items()}
        )
    )


def test_registry_and_public_handler_signatures_are_exact() -> None:
    expected = {
        "text_memory_handler": text_memory_handler,
        "skill_bundle_handler": skill_bundle_handler,
        "agent_system_handler": agent_system_handler,
        "parametric_memory_handler": parametric_memory_handler,
    }
    assert BUILTIN_HANDLER_REGISTRY == expected
    for handler in expected.values():
        assert tuple(inspect.signature(handler).parameters) == ("handler_input", "services")


def test_text_memory_preserves_rank_and_clips_by_char_and_utf8_byte_limits(
    builtin_snapshot,
) -> None:
    first, first_payload = _artifact(
        "memory-first",
        "text_memory",
        0,
        {
            "ignored.txt": (b"ignored", "text/plain"),
            "chosen.md": ("A界B".encode(), "text/markdown"),
        },
        manifest={"content_path": "chosen.md"},
    )
    second, second_payload = _artifact(
        "memory-second",
        "text_memory",
        1,
        {"memory.md": (b"CDEF", "text/markdown")},
    )
    handler_input = _input(
        "text_memory",
        (first, second),
        limits=TargetConsumptionLimits(max_text_chars=6, max_text_bytes=8),
    )
    output = text_memory_handler(
        handler_input,
        _services(first_payload, second_payload),
    )

    assert output.artifact_ids == ("memory-first", "memory-second")
    assert output.instructions[0].text == "A界B\n\nC"
    assert output.staged_payloads[0].text == "A界B\n\nC"
    assert output.staged_payloads[0].destination_relative_path == "memory.md"
    assert output.environment[0].name == "OPENEVO_MEMORY_FILE"
    assert output.renderer.data.markdown == "A界B\n\nC"
    assert builtin_snapshot.validate_handler_output(
        output,
        handler_input=handler_input,
    ) == output


@pytest.mark.parametrize(
    ("files", "manifest", "message"),
    [
        ({"memory.md": (b"\xff", "text/markdown")}, {}, "UTF-8"),
        (
            {"memory.md": (b"ok", "text/markdown")},
            {"content_path": "missing.md"},
            "content_path",
        ),
        (
            {"memory.md": (b"ok", "text/markdown")},
            {"content_path": 3},
            "content_path",
        ),
    ],
)
def test_text_memory_rejects_bad_utf8_or_manifest(
    files: dict[str, tuple[bytes, str]], manifest: dict[str, object], message: str
) -> None:
    artifact, payload = _artifact("memory", "text_memory", 0, files, manifest=manifest)
    with pytest.raises(ValueError, match=message):
        text_memory_handler(_input("text_memory", (artifact,)), _services(payload))


def test_text_handlers_reject_source_mime_outside_descriptor_allowlist() -> None:
    memory, memory_payload = _artifact(
        "memory-html",
        "text_memory",
        0,
        {"memory.html": (b"<p>memory</p>", "text/html")},
    )
    with pytest.raises(ValueError, match="MIME"):
        text_memory_handler(
            _input("text_memory", (memory,)),
            _services(memory_payload),
        )

    agent, agent_payload = _artifact(
        "agent-html",
        "agent_system",
        0,
        {"AGENTS.md": (b"<p>agent</p>", "text/html")},
        manifest={"content_path": "AGENTS.md", "target_path": "AGENTS.md"},
    )
    with pytest.raises(ValueError, match="MIME"):
        agent_system_handler(
            _input("agent_system", (agent,)),
            _services(agent_payload),
        )


def test_skill_bundle_stages_each_ranked_tree_and_renders_prefixed_entries(
    builtin_snapshot,
) -> None:
    first, first_payload = _artifact(
        "skill/one",
        "skill_bundle",
        0,
        {
            "SKILL.md": (b"# One", "text/markdown"),
            "scripts/run.py": (b"pass\n", "text/x-python"),
        },
    )
    second, second_payload = _artifact(
        "skill two", "skill_bundle", 1, {"SKILL.md": (b"# Two", "text/markdown")}
    )
    handler_input = _input("skill_bundle", (first, second))
    output = skill_bundle_handler(
        handler_input,
        _services(first_payload, second_payload),
    )

    assert [item.destination_relative_path for item in output.staged_payloads] == [
        "skill-one",
        "skill-two",
    ]
    assert [item.relative_path for item in output.renderer.data.files] == [
        "skill-one/SKILL.md",
        "skill-one/scripts/run.py",
        "skill-two/SKILL.md",
    ]
    assert output.renderer.source_contribution_ids == ("skill_bundle_0", "skill_bundle_1")
    assert output.environment[0].value_kind == "scope_root"
    assert builtin_snapshot.validate_handler_output(
        output,
        handler_input=handler_input,
    ) == output


def test_skill_bundle_requires_root_skill_markdown() -> None:
    artifact, payload = _artifact(
        "invalid-skill",
        "skill_bundle",
        0,
        {"README.md": (b"not a skill", "text/markdown")},
    )

    with pytest.raises(ValueError, match="SKILL.md"):
        skill_bundle_handler(
            _input("skill_bundle", (artifact,)),
            _services(payload),
        )


def test_agent_system_merges_native_targets_without_prompt_instruction(
    builtin_snapshot,
) -> None:
    artifacts = []
    payloads = []
    for index, (artifact_id, target, text) in enumerate(
        (
            ("agent-a", "AGENTS.md", "First"),
            ("agent-b", "CLAUDE.md", "Second"),
            ("agent-c", "AGENTS.md", "Third"),
        )
    ):
        artifact, payload = _artifact(
            artifact_id,
            "agent_system",
            index,
            {target: (text.encode(), "text/markdown")},
            manifest={"content_path": target, "target_path": target},
        )
        artifacts.append(artifact)
        payloads.append(payload)

    handler_input = _input("agent_system", tuple(artifacts))
    output = agent_system_handler(handler_input, _services(*payloads))

    assert output.instructions == ()
    assert output.staged_payloads[0].destination_relative_path == "agent_system.md"
    assert output.staged_payloads[0].text == "First\n\nSecond\n\nThird"
    assert output.renderer.source_contribution_ids == ("agent_system_file",)
    target_payloads = output.staged_payloads[1:]
    assert [(item.destination_relative_path, item.text) for item in target_payloads] == [
        ("AGENTS.md", "First\n\nThird"),
        ("CLAUDE.md", "Second"),
    ]
    assert {binding.name: binding.value_contribution_ids for binding in output.environment} == {
        "OPENEVO_AGENT_SYSTEM_FILE": ("agent_system_file",),
        "OPENEVO_AGENT_SYSTEM_TARGET": ("agent_system_target_0",),
        "OPENEVO_AGENT_SYSTEM_TARGETS": (
            "agent_system_target_0",
            "agent_system_target_1",
        ),
        "OPENEVO_AGENTS_MD": ("agent_system_target_0",),
    }
    assert builtin_snapshot.validate_handler_output(
        output,
        handler_input=handler_input,
    ) == output


@pytest.mark.parametrize(
    "target_texts",
    [
        (("AGENTS.md", "abcdef"),),
        (("AGENTS.md", "ab"), ("CLAUDE.md", "cd")),
        (("AGENTS.md", "a"), ("CLAUDE.md", "b"), ("AGENTS.md", "c")),
    ],
)
def test_agent_system_validates_at_exact_source_text_limits(
    builtin_snapshot,
    target_texts: tuple[tuple[str, str], ...],
) -> None:
    artifacts = []
    payloads = []
    for index, (target, value) in enumerate(target_texts):
        artifact, payload = _artifact(
            f"agent-{index}",
            "agent_system",
            index,
            {target: (value.encode(), "text/markdown")},
            manifest={"content_path": target, "target_path": target},
        )
        artifacts.append(artifact)
        payloads.append(payload)
    source_text = "\n\n".join(value for _, value in target_texts)
    handler_input = _input(
        "agent_system",
        tuple(artifacts),
        limits=TargetConsumptionLimits(
            max_text_chars=len(source_text),
            max_text_bytes=len(source_text.encode("utf-8")),
        ),
    )

    output = agent_system_handler(handler_input, _services(*payloads))

    assert builtin_snapshot.validate_handler_output(
        output,
        handler_input=handler_input,
    ) == output


def test_agent_system_rejects_disallowed_target_path() -> None:
    artifact, payload = _artifact(
        "agent",
        "agent_system",
        0,
        {"prompt.md": (b"text", "text/markdown")},
        manifest={"content_path": "prompt.md", "target_path": "../AGENTS.md"},
    )
    with pytest.raises(ValueError, match="target_path"):
        agent_system_handler(_input("agent_system", (artifact,)), _services(payload))


def test_parametric_memory_rejects_subscription_and_model_mismatch() -> None:
    artifact, payload = _artifact(
        "adapter",
        "parametric_memory",
        0,
        {"adapter.bin": (b"data", "application/octet-stream")},
        manifest={
            "adapter_id": "adapter-1",
            "adapter_format": "lora",
            "base_model": "base-a",
        },
    )
    with pytest.raises(ValueError, match="subscription"):
        parametric_memory_handler(
            _input("parametric_memory", (artifact,), mode="subscription", base_model="base-a"),
            _services(payload),
        )
    with pytest.raises(ValueError, match="base_model"):
        parametric_memory_handler(
            _input("parametric_memory", (artifact,), base_model="base-b"), _services(payload)
        )


def test_parametric_memory_fallbacks_and_first_adapter_renderer_semantics(
    builtin_snapshot,
) -> None:
    first, first_payload = _artifact(
        "adapter-first",
        "parametric_memory",
        0,
        {"adapter.bin": (b"one", "application/octet-stream")},
        name="fallback-adapter",
    )
    second, second_payload = _artifact(
        "adapter-second",
        "parametric_memory",
        1,
        {"adapter.bin": (b"two", "application/octet-stream")},
        manifest={
            "adapter_id": "adapter-2",
            "adapter_format": "qlora",
            "base_model": "base",
        },
    )
    handler_input = _input(
        "parametric_memory",
        (first, second),
        base_model="base",
        runtime_capabilities=("multi_adapter_application",),
    )
    output = parametric_memory_handler(
        handler_input,
        _services(first_payload, second_payload),
    )

    assert [
        (item.adapter_id, item.adapter_format, item.base_model) for item in output.adapters
    ] == [
        ("fallback-adapter", "lora", "base"),
        ("adapter-2", "qlora", "base"),
    ]
    assert output.renderer.source_contribution_ids == ("adapter_0",)
    assert output.renderer.data.adapter_id == "fallback-adapter"
    assert builtin_snapshot.validate_handler_output(
        output,
        handler_input=handler_input,
    ) == output

    forged_adapter = output.adapters[0].model_copy(
        update={"adapter_id": "forged", "adapter_format": "forged"}
    )
    forged_renderer = output.renderer.model_copy(
        update={
            "data": output.renderer.data.model_copy(
                update={"adapter_id": "forged", "adapter_format": "forged"}
            )
        }
    )
    with pytest.raises(ValueError, match="source artifact manifest"):
        builtin_snapshot.validate_handler_output(
            output.model_copy(
                update={
                    "adapters": (forged_adapter, *output.adapters[1:]),
                    "renderer": forged_renderer,
                }
            ),
            handler_input=handler_input,
        )


def test_parametric_memory_empty_manifest_values_use_canonical_fallbacks(
    builtin_snapshot,
) -> None:
    artifact, payload = _artifact(
        "adapter-fallback",
        "parametric_memory",
        0,
        {"adapter.bin": (b"data", "application/octet-stream")},
        manifest={"adapter_id": "", "adapter_format": "", "base_model": ""},
        name="fallback-adapter",
    )
    handler_input = _input(
        "parametric_memory",
        (artifact,),
        base_model="base",
    )

    output = parametric_memory_handler(handler_input, _services(payload))

    assert output.adapters[0].adapter_id == "fallback-adapter"
    assert output.adapters[0].adapter_format == "lora"
    assert output.adapters[0].base_model == "base"
    assert builtin_snapshot.validate_handler_output(
        output,
        handler_input=handler_input,
    ) == output


@pytest.mark.parametrize("size", [262_144, 1_048_576])
def test_text_memory_renderer_accepts_full_configured_text_budget(
    builtin_snapshot,
    size: int,
) -> None:
    content = b"x" * size
    artifact, payload = _artifact(
        f"memory-{size}",
        "text_memory",
        0,
        {"memory.md": (content, "text/markdown")},
    )
    handler_input = _input(
        "text_memory",
        (artifact,),
        limits=TargetConsumptionLimits(max_text_chars=size, max_text_bytes=size),
    )

    output = text_memory_handler(handler_input, _services(payload))

    assert len(output.renderer.data.markdown) == size
    assert builtin_snapshot.validate_handler_output(
        output,
        handler_input=handler_input,
    ) == output


def test_parametric_memory_output_validates_against_builtin_descriptor(
    builtin_snapshot,
) -> None:
    artifact, payload = _artifact(
        "adapter",
        "parametric_memory",
        0,
        {"adapter.bin": (b"data", "application/octet-stream")},
        manifest={
            "adapter_id": "adapter-1",
            "adapter_format": "lora",
            "base_model": "base",
        },
    )
    handler_input = _input(
        "parametric_memory",
        (artifact,),
        base_model="base",
    )
    output = parametric_memory_handler(handler_input, _services(payload))

    assert builtin_snapshot.validate_handler_output(
        output,
        handler_input=handler_input,
    ) == output
