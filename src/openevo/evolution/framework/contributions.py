"""Versioned data-only outputs from target handlers."""

from __future__ import annotations

import hashlib
from typing import Literal, TypeAlias

from pydantic import Field, field_validator, model_validator

from .contracts import (
    MAX_CONTRIBUTION_TEXT,
    MAX_HANDLER_ARTIFACTS,
    MAX_HANDLER_CONTRIBUTIONS,
    MAX_RENDERER_PAYLOAD_BYTES,
    DestinationScope,
    EnvironmentValueKind,
    PayloadKind,
    RendererKind,
    _Contract,
    _digest,
    _environment_name,
    _mime_type,
    _stable_id,
    _text,
    _ordered_unique_ids,
    canonical_json,
    paths_conflict,
    validate_payload_source_path,
    validate_relative_path,
)


def _ordered_unique_text(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(_text(value) for value in values)
    if len(normalized) != len(set(normalized)):
        raise ValueError("values must be unique")
    return normalized


class InstructionContribution(_Contract):
    contribution_id: str
    source_artifact_ids: tuple[str, ...] = Field(
        min_length=1,
        max_length=MAX_HANDLER_ARTIFACTS,
    )
    text: str = Field(min_length=1, max_length=MAX_CONTRIBUTION_TEXT)
    placement: Literal["prepend"] = "prepend"

    _id = field_validator("contribution_id")(_stable_id)
    _sources = field_validator("source_artifact_ids")(_ordered_unique_text)


class StagedPayloadContribution(_Contract):
    source_kind: Literal["artifact"] = "artifact"
    contribution_id: str
    source_artifact_id: str
    source_relative_path: str
    source_sha256: str
    source_size_bytes: int = Field(ge=0)
    media_type: str
    payload_kind: PayloadKind
    destination_scope: DestinationScope
    destination_relative_path: str

    _id = field_validator("contribution_id")(_stable_id)
    _artifact = field_validator("source_artifact_id")(_text)
    _source = field_validator("source_relative_path")(validate_payload_source_path)
    _destination = field_validator("destination_relative_path")(
        validate_relative_path
    )
    _sha = field_validator("source_sha256")(_digest)
    _mime = field_validator("media_type")(_mime_type)


class InlineTextPayloadContribution(_Contract):
    source_kind: Literal["inline_text"] = "inline_text"
    contribution_id: str
    source_artifact_ids: tuple[str, ...] = Field(
        min_length=1,
        max_length=MAX_HANDLER_ARTIFACTS,
    )
    text: str = Field(min_length=1, max_length=MAX_CONTRIBUTION_TEXT)
    media_type: str
    payload_kind: Literal[PayloadKind.FILE] = PayloadKind.FILE
    destination_scope: DestinationScope
    destination_relative_path: str

    _id = field_validator("contribution_id")(_stable_id)
    _sources = field_validator("source_artifact_ids")(_ordered_unique_text)
    _mime = field_validator("media_type")(_mime_type)
    _destination = field_validator("destination_relative_path")(
        validate_relative_path
    )

    @model_validator(mode="after")
    def _text_media_type(self) -> InlineTextPayloadContribution:
        if not self.media_type.startswith("text/"):
            raise ValueError("inline text payload requires a text/* MIME type")
        return self

    @property
    def source_sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def source_size_bytes(self) -> int:
        return len(self.text.encode("utf-8"))


PayloadContribution: TypeAlias = (
    StagedPayloadContribution | InlineTextPayloadContribution
)


class AdapterContribution(_Contract):
    contribution_id: str
    source_artifact_id: str
    source_payload_digest: str
    source_size_bytes: int = Field(ge=0)
    adapter_id: str
    adapter_format: str
    base_model: str
    weight: float = Field(default=1.0, gt=0.0, le=100.0)

    _id = field_validator("contribution_id", "adapter_id", "adapter_format")(
        _stable_id
    )
    _artifact = field_validator("source_artifact_id")(_text)
    _payload_digest = field_validator("source_payload_digest")(_digest)
    _model = field_validator("base_model")(_text)


class EnvironmentBinding(_Contract):
    name: str
    value_contribution_ids: tuple[str, ...] = ()
    value_kind: EnvironmentValueKind
    destination_scope: DestinationScope | None = None

    _name = field_validator("name")(_environment_name)
    _references = field_validator("value_contribution_ids")(_ordered_unique_ids)

    @model_validator(mode="after")
    def _source(self) -> EnvironmentBinding:
        if self.value_kind is EnvironmentValueKind.SCOPE_ROOT:
            if self.value_contribution_ids or self.destination_scope is None:
                raise ValueError(
                    "scope-root environment binding requires one destination scope"
                )
        elif not self.value_contribution_ids or self.destination_scope is not None:
            raise ValueError(
                "staged environment binding requires contribution references"
            )
        return self


class MarkdownRendererData(_Contract):
    markdown: str = Field(max_length=MAX_CONTRIBUTION_TEXT)


class FileBundleEntry(_Contract):
    relative_path: str
    media_type: str
    size_bytes: int = Field(ge=0)
    sha256: str

    _path = field_validator("relative_path")(validate_relative_path)
    _mime = field_validator("media_type")(_mime_type)
    _sha = field_validator("sha256")(_digest)


class FileBundleRendererData(_Contract):
    files: tuple[FileBundleEntry, ...] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def _unique_paths(self) -> FileBundleRendererData:
        paths = tuple(item.relative_path for item in self.files)
        if any(
            paths_conflict(left, right)
            for index, left in enumerate(paths)
            for right in paths[index + 1 :]
        ):
            raise ValueError("file bundle renderer paths must not conflict")
        return self


class SummaryField(_Contract):
    source_contribution_id: str
    label: str = Field(min_length=1, max_length=4096)
    value: str = Field(max_length=MAX_CONTRIBUTION_TEXT)

    _source = field_validator("source_contribution_id")(_stable_id)


class StructuredSummaryRendererData(_Contract):
    fields: tuple[SummaryField, ...] = Field(min_length=1, max_length=128)


class AdapterRendererData(_Contract):
    adapter_id: str
    adapter_format: str
    base_model: str

    _ids = field_validator("adapter_id", "adapter_format")(_stable_id)
    _model = field_validator("base_model")(_text)


RendererData: TypeAlias = (
    MarkdownRendererData
    | FileBundleRendererData
    | StructuredSummaryRendererData
    | AdapterRendererData
)


class RendererPayload(_Contract):
    kind: RendererKind
    contract_version: Literal["1"] = "1"
    title: str = Field(min_length=1, max_length=4096)
    source_contribution_ids: tuple[str, ...] = Field(
        min_length=1,
        max_length=MAX_HANDLER_CONTRIBUTIONS,
    )
    data: RendererData

    _sources = field_validator("source_contribution_ids")(_ordered_unique_ids)

    @field_validator("data")
    @classmethod
    def _bounded_json(cls, value: RendererData) -> RendererData:
        if len(canonical_json(value).encode("utf-8")) > MAX_RENDERER_PAYLOAD_BYTES:
            raise ValueError("renderer payload is too large")
        return value

    @model_validator(mode="after")
    def _matching_data(self) -> RendererPayload:
        expected = {
            RendererKind.MARKDOWN: MarkdownRendererData,
            RendererKind.FILE_BUNDLE: FileBundleRendererData,
            RendererKind.STRUCTURED_SUMMARY: StructuredSummaryRendererData,
            RendererKind.ADAPTER: AdapterRendererData,
        }[self.kind]
        if not isinstance(self.data, expected):
            raise ValueError("renderer data does not match renderer kind")
        return self


class TargetHandlerOutput(_Contract):
    contract_version: Literal["2"] = "2"
    target_id: str
    handler_id: str
    artifact_ids: tuple[str, ...] = Field(min_length=1, max_length=MAX_HANDLER_ARTIFACTS)
    instructions: tuple[InstructionContribution, ...] = Field(
        default=(), max_length=MAX_HANDLER_CONTRIBUTIONS
    )
    staged_payloads: tuple[PayloadContribution, ...] = Field(
        default=(), max_length=MAX_HANDLER_CONTRIBUTIONS
    )
    adapters: tuple[AdapterContribution, ...] = Field(
        default=(), max_length=MAX_HANDLER_CONTRIBUTIONS
    )
    environment: tuple[EnvironmentBinding, ...] = Field(
        default=(), max_length=MAX_HANDLER_CONTRIBUTIONS
    )
    renderer: RendererPayload

    _ids = field_validator("target_id", "handler_id")(_stable_id)
    _artifacts = field_validator("artifact_ids")(
        lambda values: tuple(_text(value) for value in values)
    )

    @model_validator(mode="after")
    def _references(self) -> TargetHandlerOutput:
        if len(self.artifact_ids) != len(set(self.artifact_ids)):
            raise ValueError("handler output artifact IDs must be unique")
        contribution_ids = [
            item.contribution_id
            for values in (self.instructions, self.staged_payloads, self.adapters)
            for item in values
        ]
        if len(contribution_ids) != len(set(contribution_ids)):
            raise ValueError("handler output contribution IDs must be unique")
        if len(contribution_ids) + len(self.environment) > MAX_HANDLER_CONTRIBUTIONS:
            raise ValueError("handler output has too many total contributions")
        if not set(self.renderer.source_contribution_ids).issubset(contribution_ids):
            raise ValueError("renderer must reference handler output contributions")
        contribution_iterator = iter(contribution_ids)
        if not all(
            any(candidate == contribution_id for candidate in contribution_iterator)
            for contribution_id in self.renderer.source_contribution_ids
        ):
            raise ValueError("renderer sources must preserve contribution order")
        artifact_ids = set(self.artifact_ids)
        source_groups: list[tuple[str, ...]] = []
        for instruction in self.instructions:
            source_groups.append(instruction.source_artifact_ids)
            if not set(instruction.source_artifact_ids).issubset(artifact_ids):
                raise ValueError("instruction references an unselected artifact")
        for payload in self.staged_payloads:
            source_ids = (
                (payload.source_artifact_id,)
                if isinstance(payload, StagedPayloadContribution)
                else payload.source_artifact_ids
            )
            source_groups.append(source_ids)
            if not set(source_ids).issubset(artifact_ids):
                raise ValueError("staged payload references an unselected artifact")
        source_groups.extend((adapter.source_artifact_id,) for adapter in self.adapters)
        if any(adapter.source_artifact_id not in artifact_ids for adapter in self.adapters):
            raise ValueError("adapter references an unselected artifact")
        for source_ids in source_groups:
            artifact_iterator = iter(self.artifact_ids)
            if not all(
                any(candidate == artifact_id for candidate in artifact_iterator)
                for artifact_id in source_ids
            ):
                raise ValueError("contribution source artifact order must be preserved")
        used_artifact_ids = {
            artifact_id for source_ids in source_groups for artifact_id in source_ids
        }
        if used_artifact_ids != artifact_ids:
            raise ValueError("handler output artifact IDs must match contribution sources")
        staged = {item.contribution_id: item for item in self.staged_payloads}
        staged_ids = tuple(staged)
        for binding in self.environment:
            if binding.value_kind is EnvironmentValueKind.SCOPE_ROOT:
                continue
            if not set(binding.value_contribution_ids).issubset(staged):
                raise ValueError("environment binding must reference staged payloads")
            staged_iterator = iter(staged_ids)
            if not all(
                any(candidate == contribution_id for candidate in staged_iterator)
                for contribution_id in binding.value_contribution_ids
            ):
                raise ValueError(
                    "environment binding must preserve staged payload order"
                )
            referenced = [staged[item] for item in binding.value_contribution_ids]
            if binding.value_kind is EnvironmentValueKind.PATH and (
                len(referenced) != 1 or referenced[0].payload_kind is not PayloadKind.FILE
            ):
                raise ValueError("path environment binding requires exactly one file")
            if binding.value_kind is EnvironmentValueKind.DIRECTORY and (
                len(referenced) != 1
                or referenced[0].payload_kind is not PayloadKind.DIRECTORY
            ):
                raise ValueError(
                    "directory environment binding requires exactly one directory"
                )
        names = tuple(binding.name for binding in self.environment)
        if len(names) != len(set(names)):
            raise ValueError("handler output environment names must be unique")
        return self


__all__ = [
    "AdapterContribution",
    "AdapterRendererData",
    "EnvironmentBinding",
    "FileBundleEntry",
    "FileBundleRendererData",
    "InlineTextPayloadContribution",
    "InstructionContribution",
    "MarkdownRendererData",
    "PayloadContribution",
    "RendererPayload",
    "StagedPayloadContribution",
    "StructuredSummaryRendererData",
    "SummaryField",
    "TargetHandlerOutput",
]
