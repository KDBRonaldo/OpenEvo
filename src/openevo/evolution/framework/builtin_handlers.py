"""Pure target handlers for OpenEvo's built-in evolution targets."""

from __future__ import annotations

import re
from collections import OrderedDict

from openevo.evolution.agent_system import normalize_agent_system_target_path

from .contracts import MAX_PAYLOAD_ENTRIES, DestinationScope, EnvironmentValueKind
from .contributions import (
    AdapterContribution,
    AdapterRendererData,
    EnvironmentBinding,
    FileBundleEntry,
    FileBundleRendererData,
    InlineTextPayloadContribution,
    InstructionContribution,
    MarkdownRendererData,
    RendererPayload,
    StagedPayloadContribution,
    TargetHandlerOutput,
)
from .handlers import (
    PayloadManifestEntry,
    TargetHandlerInput,
    TargetHandlerServices,
    TrustedArtifactSnapshot,
    payload_tree_digest,
    payload_tree_size,
)


_SAFE_SKILL_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_STABLE_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}\Z", re.ASCII)
_BUILTIN_TEXT_MEDIA_TYPES = frozenset({"text/markdown", "text/plain"})


def _require_handler(handler_input: TargetHandlerInput, target_id: str) -> None:
    if handler_input.target_id != target_id or handler_input.handler_id != f"{target_id}_handler":
        raise ValueError(f"handler input is not for {target_id!r}")


def _selected_artifacts(
    handler_input: TargetHandlerInput,
) -> tuple[TrustedArtifactSnapshot, ...]:
    return handler_input.ranked_artifacts[: handler_input.limits.max_artifacts]


def _manifest_content_entry(
    artifact: TrustedArtifactSnapshot,
    *,
    require_content_path: bool,
) -> PayloadManifestEntry:
    manifest = artifact.manifest()
    content_path = manifest.get("content_path")
    if content_path is not None:
        if not isinstance(content_path, str) or not content_path:
            raise ValueError(
                f"artifact {artifact.artifact_id!r} has invalid manifest content_path"
            )
        entry = next(
            (item for item in artifact.payload_entries if item.relative_path == content_path),
            None,
        )
        if entry is None or entry.media_type not in _BUILTIN_TEXT_MEDIA_TYPES:
            raise ValueError(
                f"artifact {artifact.artifact_id!r} content_path MIME is not allowed"
            )
        return entry
    if require_content_path:
        raise ValueError(f"artifact {artifact.artifact_id!r} manifest requires content_path")
    text_entries = tuple(
        item for item in artifact.payload_entries if item.media_type.startswith("text/")
    )
    if not text_entries:
        raise ValueError(f"artifact {artifact.artifact_id!r} has no text payload entry")
    entry = text_entries[0]
    if entry.media_type not in _BUILTIN_TEXT_MEDIA_TYPES:
        raise ValueError(f"artifact {artifact.artifact_id!r} text MIME is not allowed")
    return entry


def _read_utf8_prefix(
    artifact: TrustedArtifactSnapshot,
    entry: PayloadManifestEntry,
    services: TargetHandlerServices,
    *,
    max_chars: int,
    max_bytes: int,
) -> str:
    text = services.payloads.read_utf8_prefix(
        artifact.payload_handle,
        entry.relative_path,
        max_chars=max_chars,
        max_bytes=max_bytes,
    )
    if not isinstance(text, str):
        raise TypeError("payload service must return UTF-8 text")
    if len(text) > max_chars or len(text.encode("utf-8")) > max_bytes:
        raise ValueError("payload service returned text beyond the requested limit")
    return text


def _read_ranked_text(
    handler_input: TargetHandlerInput,
    services: TargetHandlerServices,
    *,
    require_content_path: bool,
) -> tuple[tuple[TrustedArtifactSnapshot, str], ...]:
    selected: list[tuple[TrustedArtifactSnapshot, str]] = []
    chars_left = handler_input.limits.max_text_chars
    bytes_left = handler_input.limits.max_text_bytes
    for artifact in _selected_artifacts(handler_input):
        separator = "\n\n" if selected else ""
        separator_bytes = len(separator.encode("utf-8"))
        if chars_left <= len(separator) or bytes_left <= separator_bytes:
            break
        entry = _manifest_content_entry(
            artifact,
            require_content_path=require_content_path,
        )
        clipped = _read_utf8_prefix(
            artifact,
            entry,
            services,
            max_chars=chars_left - len(separator),
            max_bytes=bytes_left - separator_bytes,
        )
        if not clipped:
            continue
        selected.append((artifact, clipped))
        chars_left -= len(separator) + len(clipped)
        bytes_left -= separator_bytes + len(clipped.encode("utf-8"))
    if not selected:
        raise ValueError("handler limits or payloads selected no text artifacts")
    return tuple(selected)


def text_memory_handler(
    handler_input: TargetHandlerInput,
    services: TargetHandlerServices,
) -> TargetHandlerOutput:
    _require_handler(handler_input, "text_memory")
    selected = _read_ranked_text(
        handler_input,
        services,
        require_content_path=False,
    )
    artifact_ids = tuple(artifact.artifact_id for artifact, _ in selected)
    markdown = "\n\n".join(text for _, text in selected)
    instruction = InstructionContribution(
        contribution_id="memory_instruction",
        source_artifact_ids=artifact_ids,
        text=markdown,
    )
    payload = InlineTextPayloadContribution(
        contribution_id="memory_file",
        source_artifact_ids=artifact_ids,
        text=markdown,
        media_type="text/markdown",
        destination_scope=DestinationScope.TARGET_DATA,
        destination_relative_path="memory.md",
    )
    return TargetHandlerOutput(
        target_id="text_memory",
        handler_id="text_memory_handler",
        artifact_ids=artifact_ids,
        instructions=(instruction,),
        staged_payloads=(payload,),
        environment=(
            EnvironmentBinding(
                name="OPENEVO_MEMORY_FILE",
                value_contribution_ids=(payload.contribution_id,),
                value_kind=EnvironmentValueKind.PATH,
            ),
        ),
        renderer=RendererPayload(
            kind="markdown",
            title="Text memory",
            source_contribution_ids=(instruction.contribution_id,),
            data=MarkdownRendererData(markdown=markdown),
        ),
    )


def _safe_skill_dir_name(artifact: TrustedArtifactSnapshot, index: int) -> str:
    raw = artifact.artifact_id or artifact.name or f"skill-{index}"
    normalized = _SAFE_SKILL_NAME_RE.sub("-", str(raw)).strip(".-")
    return normalized or f"skill-{index}"


def skill_bundle_handler(
    handler_input: TargetHandlerInput,
    services: TargetHandlerServices,
) -> TargetHandlerOutput:
    del services
    _require_handler(handler_input, "skill_bundle")
    payloads: list[StagedPayloadContribution] = []
    renderer_entries: list[FileBundleEntry] = []
    payload_bytes = 0
    for index, artifact in enumerate(_selected_artifacts(handler_input)):
        if not any(
            entry.relative_path == "SKILL.md"
            and entry.media_type in _BUILTIN_TEXT_MEDIA_TYPES
            for entry in artifact.payload_entries
        ):
            raise ValueError(
                f"skill artifact {artifact.artifact_id!r} requires root SKILL.md"
            )
        tree_size = payload_tree_size(artifact.payload_entries)
        if payload_bytes + tree_size > handler_input.limits.max_payload_bytes:
            continue
        if len(renderer_entries) + len(artifact.payload_entries) > MAX_PAYLOAD_ENTRIES:
            break
        destination = _safe_skill_dir_name(artifact, index)
        contribution_id = f"skill_bundle_{index}"
        payloads.append(
            StagedPayloadContribution(
                contribution_id=contribution_id,
                source_artifact_id=artifact.artifact_id,
                source_relative_path=".",
                source_sha256=payload_tree_digest(artifact.payload_entries),
                source_size_bytes=tree_size,
                media_type="application/octet-stream",
                payload_kind="directory",
                destination_scope=DestinationScope.HARNESS_SKILLS,
                destination_relative_path=destination,
            )
        )
        renderer_entries.extend(
            FileBundleEntry(
                relative_path=f"{destination}/{entry.relative_path}",
                media_type=entry.media_type,
                size_bytes=entry.size_bytes,
                sha256=entry.sha256,
            )
            for entry in artifact.payload_entries
        )
        payload_bytes += tree_size
    if not payloads:
        raise ValueError("handler limits selected no skill bundles")
    return TargetHandlerOutput(
        target_id="skill_bundle",
        handler_id="skill_bundle_handler",
        artifact_ids=tuple(item.source_artifact_id for item in payloads),
        staged_payloads=tuple(payloads),
        environment=(
            EnvironmentBinding(
                name="OPENEVO_SKILLS_DIR",
                value_kind=EnvironmentValueKind.SCOPE_ROOT,
                destination_scope=DestinationScope.HARNESS_SKILLS,
            ),
        ),
        renderer=RendererPayload(
            kind="file_bundle",
            title="Skill bundles",
            source_contribution_ids=tuple(item.contribution_id for item in payloads),
            data=FileBundleRendererData(files=tuple(renderer_entries)),
        ),
    )


def agent_system_handler(
    handler_input: TargetHandlerInput,
    services: TargetHandlerServices,
) -> TargetHandlerOutput:
    _require_handler(handler_input, "agent_system")
    selected = _read_ranked_text(
        handler_input,
        services,
        require_content_path=True,
    )
    grouped: OrderedDict[str, list[tuple[TrustedArtifactSnapshot, str]]] = OrderedDict()
    for artifact, text in selected:
        try:
            target_path = normalize_agent_system_target_path(
                artifact.manifest().get("target_path")
            )
        except ValueError as exc:
            raise ValueError(f"artifact {artifact.artifact_id!r} has invalid target_path") from exc
        grouped.setdefault(target_path, []).append((artifact, text))

    artifact_ids = tuple(artifact.artifact_id for artifact, _ in selected)
    markdown = "\n\n".join(text for _, text in selected)
    canonical = InlineTextPayloadContribution(
        contribution_id="agent_system_file",
        source_artifact_ids=artifact_ids,
        text=markdown,
        media_type="text/markdown",
        destination_scope=DestinationScope.TARGET_DATA,
        destination_relative_path="agent_system.md",
    )
    targets = tuple(
        InlineTextPayloadContribution(
            contribution_id=f"agent_system_target_{index}",
            source_artifact_ids=tuple(artifact.artifact_id for artifact, _ in values),
            text="\n\n".join(text for _, text in values),
            media_type="text/markdown",
            destination_scope=DestinationScope.HARNESS_INSTRUCTION,
            destination_relative_path=target_path,
        )
        for index, (target_path, values) in enumerate(grouped.items())
    )
    environment = [
        EnvironmentBinding(
            name="OPENEVO_AGENT_SYSTEM_FILE",
            value_contribution_ids=(canonical.contribution_id,),
            value_kind=EnvironmentValueKind.PATH,
        ),
        EnvironmentBinding(
            name="OPENEVO_AGENT_SYSTEM_TARGET",
            value_contribution_ids=(targets[0].contribution_id,),
            value_kind=EnvironmentValueKind.PATH,
        ),
        EnvironmentBinding(
            name="OPENEVO_AGENT_SYSTEM_TARGETS",
            value_contribution_ids=tuple(item.contribution_id for item in targets),
            value_kind=EnvironmentValueKind.JSON_PATHS,
        ),
    ]
    agents_md = next(
        (item for item in targets if item.destination_relative_path == "AGENTS.md"),
        None,
    )
    if agents_md is not None:
        environment.append(
            EnvironmentBinding(
                name="OPENEVO_AGENTS_MD",
                value_contribution_ids=(agents_md.contribution_id,),
                value_kind=EnvironmentValueKind.PATH,
            )
        )
    return TargetHandlerOutput(
        target_id="agent_system",
        handler_id="agent_system_handler",
        artifact_ids=artifact_ids,
        staged_payloads=(canonical, *targets),
        environment=tuple(environment),
        renderer=RendererPayload(
            kind="markdown",
            title="Agent system",
            source_contribution_ids=(canonical.contribution_id,),
            data=MarkdownRendererData(markdown=markdown),
        ),
    )


def _manifest_stable_id(
    manifest: dict[str, object],
    key: str,
    fallback: str,
) -> str:
    value = manifest.get(key)
    if value is None or value == "":
        value = fallback
    if not isinstance(value, str) or _STABLE_ID_RE.fullmatch(value) is None:
        raise ValueError(f"adapter manifest {key} must be a stable identifier")
    return value


def parametric_memory_handler(
    handler_input: TargetHandlerInput,
    services: TargetHandlerServices,
) -> TargetHandlerOutput:
    del services
    _require_handler(handler_input, "parametric_memory")
    if handler_input.execution_profile.execution_mode.value == "subscription":
        raise ValueError("subscription execution cannot consume parametric memory")
    if handler_input.base_model is None:
        raise ValueError("parametric memory requires a base_model")
    application_limit = handler_input.limits.max_adapters
    if "multi_adapter_application" not in handler_input.execution_profile.runtime_capabilities:
        application_limit = min(application_limit, 1)
    artifacts = _selected_artifacts(handler_input)[:application_limit]
    adapters: list[AdapterContribution] = []
    for index, artifact in enumerate(artifacts):
        manifest = artifact.manifest()
        manifest_base_model = manifest.get("base_model") or handler_input.base_model
        if not isinstance(manifest_base_model, str):
            raise ValueError("adapter manifest base_model must be a string")
        if manifest_base_model != handler_input.base_model:
            raise ValueError("adapter manifest base_model does not match requested base_model")
        adapter_id = _manifest_stable_id(
            manifest,
            "adapter_id",
            artifact.name if _STABLE_ID_RE.fullmatch(artifact.name) else artifact.artifact_id,
        )
        adapter_format = _manifest_stable_id(manifest, "adapter_format", "lora")
        adapters.append(
            AdapterContribution(
                contribution_id=f"adapter_{index}",
                source_artifact_id=artifact.artifact_id,
                source_payload_digest=artifact.payload_manifest_digest,
                source_size_bytes=sum(
                    entry.size_bytes for entry in artifact.payload_entries
                ),
                adapter_id=adapter_id,
                adapter_format=adapter_format,
                base_model=manifest_base_model,
            )
        )
    if not adapters:
        raise ValueError("handler limits selected no adapters")
    first = adapters[0]
    return TargetHandlerOutput(
        target_id="parametric_memory",
        handler_id="parametric_memory_handler",
        artifact_ids=tuple(item.source_artifact_id for item in adapters),
        adapters=tuple(adapters),
        renderer=RendererPayload(
            kind="adapter",
            title="Parametric memory",
            source_contribution_ids=(first.contribution_id,),
            data=AdapterRendererData(
                adapter_id=first.adapter_id,
                adapter_format=first.adapter_format,
                base_model=first.base_model,
            ),
        ),
    )


BUILTIN_HANDLER_REGISTRY = {
    "text_memory_handler": text_memory_handler,
    "skill_bundle_handler": skill_bundle_handler,
    "agent_system_handler": agent_system_handler,
    "parametric_memory_handler": parametric_memory_handler,
}


__all__ = [
    "BUILTIN_HANDLER_REGISTRY",
    "agent_system_handler",
    "parametric_memory_handler",
    "skill_bundle_handler",
    "text_memory_handler",
]
