"""Fail-closed validation and aggregation for target-handler outputs."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from openevo.evolution.agent_system import is_allowed_agent_system_target_path

from .contracts import (
    ContributionKind,
    DestinationScope,
    EnvironmentValueKind,
    PayloadKind,
    canonical_json,
    paths_conflict,
)
from .contributions import (
    AdapterContribution,
    AdapterRendererData,
    FileBundleRendererData,
    InlineTextPayloadContribution,
    InstructionContribution,
    MarkdownRendererData,
    StagedPayloadContribution,
    StructuredSummaryRendererData,
    TargetHandlerOutput,
)
from .handlers import (
    PayloadManifestEntry,
    TargetHandlerInput,
    TrustedArtifactSnapshot,
    payload_entries_under_root,
    payload_tree_digest,
    payload_tree_size,
)

if TYPE_CHECKING:
    from .registry import RegistrySnapshot


_STABLE_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}\Z", re.ASCII)
_MAX_TEXT_PROJECTION_COPIES = 3


def _is_ordered_subsequence(
    selected: tuple[str, ...],
    ranked: tuple[str, ...],
) -> bool:
    iterator = iter(ranked)
    return all(any(candidate == item for candidate in iterator) for item in selected)


def _source_artifact_ids(
    contribution: object,
) -> tuple[str, ...]:
    if isinstance(contribution, StagedPayloadContribution | AdapterContribution):
        return (contribution.source_artifact_id,)
    if isinstance(contribution, InstructionContribution | InlineTextPayloadContribution):
        return contribution.source_artifact_ids
    return ()


def _validate_projection_source_order(output: TargetHandlerOutput) -> None:
    rank = {
        artifact_id: index for index, artifact_id in enumerate(output.artifact_ids)
    }
    category_first_ranks: list[int] = []
    for contributions in (
        output.instructions,
        output.staged_payloads,
        output.adapters,
    ):
        contribution_ranks = [
            tuple(rank[artifact_id] for artifact_id in _source_artifact_ids(item))
            for item in contributions
        ]
        if any(values != tuple(sorted(values)) for values in contribution_ranks):
            raise ValueError("contributions must preserve source artifact order")
        first_ranks = [values[0] for values in contribution_ranks if values]
        if first_ranks != sorted(first_ranks):
            raise ValueError("contributions must preserve first source artifact order")
        if first_ranks:
            category_first_ranks.append(first_ranks[0])
    if category_first_ranks != sorted(category_first_ranks):
        raise ValueError(
            "contribution categories must preserve first source artifact order"
        )


def _resolved_destination(
    payload: object,
    handler_input: TargetHandlerInput,
) -> str:
    scope = payload.destination_scope
    path = payload.destination_relative_path
    if scope is DestinationScope.TARGET_DATA:
        root = handler_input.destination_roots.target_data
    elif scope is DestinationScope.HARNESS_SKILLS:
        root = handler_input.destination_roots.harness_skills
    else:
        root = handler_input.destination_roots.harness_instruction
    return str(PurePosixPath(root) / path)


def _semantic_text_usage(output: TargetHandlerOutput) -> tuple[int, int]:
    instruction_texts = Counter(
        (item.source_artifact_ids, item.text) for item in output.instructions
    )
    inline_texts = Counter(
        (item.source_artifact_ids, item.text)
        for item in output.staged_payloads
        if isinstance(item, InlineTextPayloadContribution)
    )
    staged_texts: Counter[tuple[tuple[str, ...], str]] = Counter()
    if isinstance(output.renderer.data, MarkdownRendererData):
        contributions = {
            item.contribution_id: item
            for values in (
                output.instructions,
                output.staged_payloads,
                output.adapters,
            )
            for item in values
        }
        sources = [
            contributions[item]
            for item in output.renderer.source_contribution_ids
        ]
        if len(sources) == 1 and isinstance(sources[0], StagedPayloadContribution):
            staged_texts[
                (
                    (sources[0].source_artifact_id,),
                    output.renderer.data.markdown,
                )
            ] += 1
    keys = set(instruction_texts) | set(inline_texts) | set(staged_texts)
    counts = {
        (source_ids, text): max(
            instruction_texts[(source_ids, text)],
            inline_texts[(source_ids, text)],
            staged_texts[(source_ids, text)],
        )
        for source_ids, text in keys
    }
    return (
        sum(len(text) * count for (_, text), count in counts.items()),
        sum(len(text.encode("utf-8")) * count for (_, text), count in counts.items()),
    )


def _projected_text_usage(output: TargetHandlerOutput) -> tuple[int, int]:
    texts = [item.text for item in output.instructions]
    texts.extend(
        item.text
        for item in output.staged_payloads
        if isinstance(item, InlineTextPayloadContribution)
    )
    return (
        sum(len(text) for text in texts),
        sum(len(text.encode("utf-8")) for text in texts),
    )


def _text_usage(texts: Iterable[str]) -> tuple[int, int]:
    values = tuple(texts)
    return (
        sum(len(text) for text in values),
        sum(len(text.encode("utf-8")) for text in values),
    )


def _renderer_text_usage(output: TargetHandlerOutput) -> tuple[int, int]:
    data = output.renderer.data
    if isinstance(data, MarkdownRendererData):
        return _text_usage((data.markdown,))
    if isinstance(data, StructuredSummaryRendererData):
        return _text_usage(field.value for field in data.fields)
    return (0, 0)


def _validate_text_source_media_types(
    output: TargetHandlerOutput,
    input_artifacts: Mapping[str, TrustedArtifactSnapshot],
    allowed_media_types: set[str],
) -> None:
    artifact_ids = {
        artifact_id
        for contributions in (output.instructions, output.staged_payloads)
        for contribution in contributions
        if isinstance(
            contribution,
            InstructionContribution | InlineTextPayloadContribution,
        )
        for artifact_id in contribution.source_artifact_ids
    }
    for artifact_id in artifact_ids:
        artifact = input_artifacts[artifact_id]
        manifest = artifact.manifest()
        content_path = manifest.get("content_path")
        if content_path is not None:
            entry = next(
                (
                    item
                    for item in artifact.payload_entries
                    if item.relative_path == content_path
                ),
                None,
            )
        else:
            entry = next(
                (
                    item
                    for item in artifact.payload_entries
                    if item.media_type.startswith("text/")
                ),
                None,
            )
        if entry is None or entry.media_type not in allowed_media_types:
            raise ValueError("handler text source MIME type is not allowed")


def _canonical_adapter_manifest(
    artifact: TrustedArtifactSnapshot,
    requested_base_model: str,
) -> dict[str, object]:
    manifest = artifact.manifest()
    adapter_id = manifest.get("adapter_id")
    if adapter_id is None or adapter_id == "":
        adapter_id = (
            artifact.name
            if _STABLE_ID_RE.fullmatch(artifact.name)
            else artifact.artifact_id
        )
    adapter_format = manifest.get("adapter_format")
    if adapter_format is None or adapter_format == "":
        adapter_format = "lora"
    base_model = manifest.get("base_model")
    if base_model is None or base_model == "":
        base_model = requested_base_model
    return {
        "adapter_id": adapter_id,
        "adapter_format": adapter_format,
        "base_model": base_model,
    }


def _validate_staged_payload_source(
    payload: StagedPayloadContribution,
    input_artifacts: Mapping[str, TrustedArtifactSnapshot],
    allowed_media_types: set[str],
) -> None:
    try:
        artifact = input_artifacts[payload.source_artifact_id]
    except KeyError as exc:
        raise ValueError("staged payload references an unknown artifact") from exc
    if payload.payload_kind.value == "file":
        entry = next(
            (
                item
                for item in artifact.payload_entries
                if item.relative_path == payload.source_relative_path
            ),
            None,
        )
        if entry is None:
            raise ValueError("staged payload source file is absent from manifest")
        if (
            entry.sha256 != payload.source_sha256
            or entry.size_bytes != payload.source_size_bytes
            or entry.media_type != payload.media_type
        ):
            raise ValueError("staged payload digest or metadata does not match manifest")
        return
    try:
        directory_entries = payload_entries_under_root(
            artifact.payload_entries,
            root=payload.source_relative_path,
        )
        digest = payload_tree_digest(
            artifact.payload_entries,
            root=payload.source_relative_path,
        )
        size = payload_tree_size(
            artifact.payload_entries,
            root=payload.source_relative_path,
        )
    except ValueError as exc:
        raise ValueError("staged payload directory is absent from manifest") from exc
    if any(entry.media_type not in allowed_media_types for entry in directory_entries):
        raise ValueError("staged payload directory contains a disallowed MIME type")
    if digest != payload.source_sha256 or size != payload.source_size_bytes:
        raise ValueError("staged payload digest or metadata does not match manifest")


def _validate_renderer_sources(output: TargetHandlerOutput) -> None:
    contributions = {
        item.contribution_id: item
        for values in (
            output.instructions,
            output.staged_payloads,
            output.adapters,
        )
        for item in values
    }
    sources = [
        contributions[contribution_id]
        for contribution_id in output.renderer.source_contribution_ids
    ]
    data = output.renderer.data
    if isinstance(data, MarkdownRendererData):
        if all(isinstance(item, InstructionContribution) for item in sources):
            expected = "\n\n".join(item.text for item in sources)
            if data.markdown != expected:
                raise ValueError("markdown renderer does not match instruction output")
            return
        if all(isinstance(item, InlineTextPayloadContribution) for item in sources):
            expected = "\n\n".join(item.text for item in sources)
            if data.markdown != expected:
                raise ValueError("markdown renderer does not match staged text output")
            return
        if len(sources) == 1 and isinstance(sources[0], StagedPayloadContribution):
            encoded = data.markdown.encode("utf-8")
            if (
                hashlib.sha256(encoded).hexdigest() != sources[0].source_sha256
                or len(encoded) != sources[0].source_size_bytes
            ):
                raise ValueError("markdown renderer does not match staged payload")
            return
        raise ValueError("markdown renderer has incompatible source contributions")
    if isinstance(data, FileBundleRendererData):
        if not all(isinstance(item, StagedPayloadContribution) for item in sources):
            raise ValueError("file bundle renderer requires staged payload sources")
        remaining_paths = {item.relative_path for item in data.files}
        expected_path_order: list[str] = []
        for source in sources:
            if source.payload_kind is PayloadKind.FILE:
                rendered = next(
                    (
                        item
                        for item in data.files
                        if item.relative_path == source.destination_relative_path
                    ),
                    None,
                )
                if rendered is None or (
                    rendered.sha256 != source.source_sha256
                    or rendered.size_bytes != source.source_size_bytes
                    or rendered.media_type != source.media_type
                ):
                    raise ValueError("file bundle renderer does not match staged payload")
                remaining_paths.discard(rendered.relative_path)
                expected_path_order.append(rendered.relative_path)
                continue
            prefix = f"{source.destination_relative_path}/"
            rendered_entries = tuple(
                PayloadManifestEntry(
                    relative_path=item.relative_path[len(prefix) :],
                    media_type=item.media_type,
                    size_bytes=item.size_bytes,
                    sha256=item.sha256,
                )
                for item in data.files
                if item.relative_path.startswith(prefix)
            )
            if (
                not rendered_entries
                or payload_tree_digest(rendered_entries) != source.source_sha256
                or sum(item.size_bytes for item in rendered_entries)
                != source.source_size_bytes
            ):
                raise ValueError("file bundle renderer does not match staged payload")
            remaining_paths.difference_update(
                f"{source.destination_relative_path}/{item.relative_path}"
                for item in rendered_entries
            )
            expected_path_order.extend(
                sorted(
                    item.relative_path
                    for item in data.files
                    if item.relative_path.startswith(prefix)
                )
            )
        if remaining_paths:
            raise ValueError("file bundle renderer contains unstaged files")
        if tuple(item.relative_path for item in data.files) != tuple(expected_path_order):
            raise ValueError("file bundle renderer file order is not canonical")
        return
    if isinstance(data, AdapterRendererData):
        if len(sources) != 1 or not isinstance(sources[0], AdapterContribution):
            raise ValueError("adapter renderer requires one adapter contribution")
        adapter = sources[0]
        if (
            data.adapter_id != adapter.adapter_id
            or data.adapter_format != adapter.adapter_format
            or data.base_model != adapter.base_model
        ):
            raise ValueError("adapter renderer does not match adapter contribution")
        return
    if isinstance(data, StructuredSummaryRendererData):
        field_sources = tuple(field.source_contribution_id for field in data.fields)
        renderer_sources = output.renderer.source_contribution_ids
        source_rank = {
            contribution_id: index
            for index, contribution_id in enumerate(renderer_sources)
        }
        field_ranks = [
            source_rank.get(contribution_id, -1)
            for contribution_id in field_sources
        ]
        if (
            set(field_sources) != set(renderer_sources)
            or field_ranks != sorted(field_ranks)
        ):
            raise ValueError(
                "structured summary fields must preserve renderer source order"
            )
        contributions = {
            item.contribution_id: item
            for values in (output.instructions, output.staged_payloads)
            for item in values
        }
        for field in data.fields:
            source = contributions.get(field.source_contribution_id)
            if not isinstance(
                source,
                InstructionContribution | InlineTextPayloadContribution,
            ) or field.value != source.text:
                raise ValueError(
                    "structured summary value must match referenced text contribution"
                )


def validate_handler_output(
    snapshot: RegistrySnapshot,
    output: TargetHandlerOutput,
    *,
    handler_input: TargetHandlerInput,
) -> TargetHandlerOutput:
    output = TargetHandlerOutput.model_validate_json(canonical_json(output))
    handler_input = TargetHandlerInput.model_validate_json(canonical_json(handler_input))
    try:
        target = snapshot.targets[output.target_id]
    except KeyError as exc:
        raise ValueError(f"unknown target {output.target_id!r}") from exc
    if handler_input.target_id != target.id:
        raise ValueError("handler input does not match output target")
    if handler_input.handler_id != target.handler_id:
        raise ValueError("handler input does not match target handler")
    if output.handler_id != target.handler_id:
        raise ValueError("handler output does not match target handler")
    handler = snapshot.target_handlers[target.handler_id]
    if handler_input.contract_version != handler.input_contract_version:
        raise ValueError("handler input contract mismatch")
    if output.contract_version != handler.contribution_contract_version:
        raise ValueError("handler output contribution contract mismatch")
    input_artifacts = {
        artifact.artifact_id: artifact for artifact in handler_input.ranked_artifacts
    }
    if not _is_ordered_subsequence(output.artifact_ids, handler_input.artifact_ids()):
        raise ValueError("handler output artifacts must preserve ranked input order")
    if len(output.artifact_ids) > handler_input.limits.max_artifacts:
        raise ValueError("handler output exceeds artifact consumption limit")
    for artifact_id in output.artifact_ids:
        artifact = input_artifacts[artifact_id]
        if artifact.artifact_type not in handler.artifact_types:
            raise ValueError("handler does not allow source artifact type")
        if artifact.uri_scheme not in handler.allowed_uri_schemes:
            raise ValueError("handler does not allow source artifact URI scheme")
    _validate_projection_source_order(output)
    if (
        output.renderer.kind != handler.renderer_kind
        or output.renderer.contract_version != handler.renderer_contract_version
    ):
        raise ValueError("handler output renderer contract mismatch")

    present_kinds: set[ContributionKind] = set()
    if output.instructions:
        present_kinds.add(ContributionKind.INSTRUCTION)
    if output.staged_payloads:
        present_kinds.add(ContributionKind.STAGED_PAYLOAD)
    if output.adapters:
        present_kinds.add(ContributionKind.ADAPTER)
    if output.environment:
        present_kinds.add(ContributionKind.ENVIRONMENT)
    if not present_kinds.issubset(handler.allowed_contribution_kinds):
        raise ValueError("handler output contribution kind is not allowed")

    allowed_scopes = set(handler.allowed_destination_scopes)
    allowed_media_types = set(handler.allowed_media_types)
    _validate_text_source_media_types(
        output,
        input_artifacts,
        allowed_media_types,
    )
    destinations: list[str] = []
    payload_bytes = 0
    for payload in output.staged_payloads:
        if payload.destination_scope not in allowed_scopes:
            raise ValueError("handler output destination scope is not allowed")
        if payload.media_type not in allowed_media_types:
            raise ValueError("handler output MIME type is not allowed")
        if isinstance(payload, StagedPayloadContribution):
            _validate_staged_payload_source(
                payload,
                input_artifacts,
                allowed_media_types,
            )
        payload_bytes += payload.source_size_bytes
        if (
            payload.destination_scope is DestinationScope.HARNESS_INSTRUCTION
            and not is_allowed_agent_system_target_path(
                payload.destination_relative_path
            )
        ):
            raise ValueError("handler output harness instruction path is not allowed")
        destination = _resolved_destination(payload, handler_input)
        if any(
            paths_conflict(path, destination)
            for path in destinations
        ):
            raise ValueError("handler output contains a destination collision")
        destinations.append(destination)

    if payload_bytes > handler_input.limits.max_payload_bytes:
        raise ValueError("handler output exceeds payload byte limit")
    instruction_chars, instruction_bytes = _text_usage(
        item.text for item in output.instructions
    )
    renderer_chars, renderer_bytes = _renderer_text_usage(output)
    if max(instruction_chars, renderer_chars) > handler_input.limits.max_text_chars:
        raise ValueError("handler output exceeds text consumption limit")
    if max(instruction_bytes, renderer_bytes) > handler_input.limits.max_text_bytes:
        raise ValueError("handler output exceeds UTF-8 text byte limit")
    text_chars, text_bytes = _semantic_text_usage(output)
    if text_chars > handler_input.limits.max_text_chars * 2:
        raise ValueError("handler output exceeds text projection limit")
    if text_bytes > handler_input.limits.max_text_bytes * 2:
        raise ValueError("handler output exceeds UTF-8 text projection limit")
    projection_chars, projection_bytes = _projected_text_usage(output)
    if projection_chars > (
        handler_input.limits.max_text_chars * _MAX_TEXT_PROJECTION_COPIES
    ):
        raise ValueError("handler output exceeds text projection limit")
    if projection_bytes > (
        handler_input.limits.max_text_bytes * _MAX_TEXT_PROJECTION_COPIES
    ):
        raise ValueError("handler output exceeds UTF-8 text projection limit")
    if (
        output.adapters
        and handler_input.execution_profile.execution_mode.value == "subscription"
    ):
        raise ValueError("subscription context cannot consume adapter contributions")
    if output.adapters:
        if handler_input.base_model is None:
            raise ValueError("adapter contributions require a requested base model")
        if any(
            adapter.base_model != handler_input.base_model
            for adapter in output.adapters
        ):
            raise ValueError("adapter contribution does not match requested base model")
        for adapter in output.adapters:
            artifact = input_artifacts[adapter.source_artifact_id]
            if (
                adapter.source_payload_digest
                != artifact.payload_manifest_digest
                or adapter.source_size_bytes
                != sum(entry.size_bytes for entry in artifact.payload_entries)
            ):
                raise ValueError(
                    "adapter contribution does not match source payload inventory"
                )
            expected = _canonical_adapter_manifest(
                artifact,
                handler_input.base_model,
            )
            actual = {
                "adapter_id": adapter.adapter_id,
                "adapter_format": adapter.adapter_format,
                "base_model": adapter.base_model,
            }
            if actual != expected:
                raise ValueError(
                    "adapter contribution does not match source artifact manifest"
                )
        application_limit = handler_input.limits.max_adapters
        if "multi_adapter_application" not in (
            handler_input.execution_profile.runtime_capabilities
        ):
            application_limit = min(application_limit, 1)
        if len(output.adapters) > application_limit:
            raise ValueError("handler output exceeds runtime adapter application limit")

    environment_allowlist = set(handler.environment_allowlist)
    if any(binding.name not in environment_allowlist for binding in output.environment):
        raise ValueError("handler output environment binding is not allowed")
    if any(
        binding.value_kind is EnvironmentValueKind.SCOPE_ROOT
        and binding.destination_scope not in allowed_scopes
        for binding in output.environment
    ):
        raise ValueError("handler output environment scope is not allowed")
    _validate_renderer_sources(output)
    return output


def validate_handler_outputs(
    snapshot: RegistrySnapshot,
    pairs: Iterable[tuple[TargetHandlerInput, TargetHandlerOutput]],
) -> tuple[TargetHandlerOutput, ...]:
    validated: list[TargetHandlerOutput] = []
    inputs_by_target: dict[str, TargetHandlerInput] = {}
    seen_targets: set[str] = set()
    for handler_input, output in pairs:
        item = validate_handler_output(
            snapshot,
            output,
            handler_input=handler_input,
        )
        if item.target_id in seen_targets:
            raise ValueError("context contains duplicate target handler output")
        seen_targets.add(item.target_id)
        validated.append(item)
        inputs_by_target[item.target_id] = TargetHandlerInput.model_validate_json(
            canonical_json(handler_input)
        )

    if validated:
        first_input = inputs_by_target[validated[0].target_id]
        for item in validated[1:]:
            handler_input = inputs_by_target[item.target_id]
            if handler_input.execution_profile != first_input.execution_profile:
                raise ValueError("context handler inputs have different execution profiles")
            if handler_input.destination_roots != first_input.destination_roots:
                raise ValueError("context handler inputs have different destination roots")
            if handler_input.base_model != first_input.base_model:
                raise ValueError("context handler inputs have different base models")
            if handler_input.limits.max_adapters != first_input.limits.max_adapters:
                raise ValueError("context handler inputs have different adapter limits")

    destinations: list[tuple[str, str]] = []
    environment_names: dict[str, str] = {}
    adapter_ids: dict[str, str] = {}
    adapter_base_models: set[str] = set()
    for output in validated:
        handler_input = inputs_by_target[output.target_id]
        for payload in output.staged_payloads:
            resolved_path = _resolved_destination(payload, handler_input)
            for path, target_id in destinations:
                if paths_conflict(path, resolved_path):
                    raise ValueError(
                        "cross-target destination conflict between "
                        f"{target_id!r} and {output.target_id!r}"
                    )
            destinations.append(
                (
                    resolved_path,
                    output.target_id,
                )
            )
        for binding in output.environment:
            if binding.name in environment_names:
                raise ValueError(
                    "cross-target environment conflict between "
                    f"{environment_names[binding.name]!r} and {output.target_id!r}"
                )
            environment_names[binding.name] = output.target_id
        for adapter in output.adapters:
            if adapter.adapter_id in adapter_ids:
                raise ValueError("cross-target adapter ID conflict")
            adapter_ids[adapter.adapter_id] = output.target_id
            adapter_base_models.add(adapter.base_model)
    if len(adapter_base_models) > 1:
        raise ValueError("cross-target adapters require incompatible base models")
    if validated:
        first_input = inputs_by_target[validated[0].target_id]
        application_limit = first_input.limits.max_adapters
        if "multi_adapter_application" not in (
            first_input.execution_profile.runtime_capabilities
        ):
            application_limit = min(application_limit, 1)
        if len(adapter_ids) > application_limit:
            raise ValueError("context exceeds runtime adapter application limit")
    return tuple(
        sorted(
            validated,
            key=lambda item: (
                snapshot.targets[item.target_id].context_order,
                item.target_id,
            ),
        )
    )


__all__ = ["validate_handler_output", "validate_handler_outputs"]
