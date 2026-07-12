"""Trusted artifact snapshots and target-handler invocation contracts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from pydantic import Field, field_validator, model_validator

from .contracts import (
    MAX_CONTRIBUTION_TEXT,
    MAX_CONTRACT_JSON_BYTES,
    MAX_HANDLER_ARTIFACTS,
    MAX_PAYLOAD_ENTRIES,
    MAX_PAYLOAD_ENTRY_BYTES,
    MAX_PAYLOAD_TOTAL_BYTES,
    MAX_PAYLOAD_TREE_DEPTH,
    EvolutionExecutionProfile,
    _Contract,
    _digest,
    _mime_type,
    _stable_id,
    _text,
    _uri_scheme,
    canonical_digest,
    canonical_json,
    paths_conflict,
    validate_absolute_runtime_path,
    validate_payload_source_path,
    validate_relative_path,
)
from .contributions import TargetHandlerOutput


class PayloadManifestEntry(_Contract):
    relative_path: str
    media_type: str
    size_bytes: int = Field(ge=0, le=MAX_PAYLOAD_ENTRY_BYTES)
    sha256: str

    _path = field_validator("relative_path")(validate_relative_path)
    _mime = field_validator("media_type")(_mime_type)
    _sha = field_validator("sha256")(_digest)


def _entries_under_root(
    entries: tuple[PayloadManifestEntry, ...],
    root: str,
) -> tuple[dict[str, object], ...]:
    normalized_root = validate_payload_source_path(root)
    prefix = "" if normalized_root == "." else f"{normalized_root}/"
    selected: list[dict[str, object]] = []
    for entry in entries:
        if prefix and not entry.relative_path.startswith(prefix):
            continue
        relative_path = (
            entry.relative_path if not prefix else entry.relative_path[len(prefix) :]
        )
        if not relative_path:
            continue
        selected.append(
            {
                "relative_path": relative_path,
                "media_type": entry.media_type,
                "size_bytes": entry.size_bytes,
                "sha256": entry.sha256,
            }
        )
    if not selected:
        raise ValueError(f"payload tree root {normalized_root!r} contains no files")
    return tuple(sorted(selected, key=lambda item: str(item["relative_path"])))


def payload_entries_under_root(
    entries: tuple[PayloadManifestEntry, ...],
    *,
    root: str = ".",
) -> tuple[PayloadManifestEntry, ...]:
    normalized_root = validate_payload_source_path(root)
    prefix = "" if normalized_root == "." else f"{normalized_root}/"
    selected = tuple(
        entry
        for entry in entries
        if not prefix or entry.relative_path.startswith(prefix)
    )
    if not selected:
        raise ValueError(f"payload tree root {normalized_root!r} contains no files")
    return selected


def payload_tree_digest(
    entries: tuple[PayloadManifestEntry, ...],
    *,
    root: str = ".",
) -> str:
    """Digest a canonical file inventory relative to one payload tree root."""

    return canonical_digest(
        {
            "contract_version": "1",
            "entries": _entries_under_root(entries, root),
        }
    )


def payload_tree_size(
    entries: tuple[PayloadManifestEntry, ...],
    *,
    root: str = ".",
) -> int:
    return sum(
        int(entry["size_bytes"])
        for entry in _entries_under_root(entries, root)
    )


class TrustedArtifactSnapshot(_Contract):
    """Core-issued payload inventory; no host path or artifact URI is exposed."""

    artifact_id: str = Field(min_length=1, max_length=256)
    artifact_type: str
    name: str = Field(min_length=1, max_length=4096)
    uri_scheme: str
    payload_handle: str
    payload_entries: tuple[PayloadManifestEntry, ...] = Field(
        min_length=1,
        max_length=MAX_PAYLOAD_ENTRIES,
    )
    payload_manifest_digest: str
    manifest_json: str = Field(default="{}", max_length=MAX_CONTRACT_JSON_BYTES)
    scores_json: str = Field(default="{}", max_length=MAX_CONTRACT_JSON_BYTES)
    rank_index: int = Field(ge=0, lt=MAX_HANDLER_ARTIFACTS)

    _text_fields = field_validator("artifact_id", "name")(_text)
    _handle = field_validator("payload_handle")(_stable_id)
    _type = field_validator("artifact_type")(_stable_id)
    _scheme = field_validator("uri_scheme")(_uri_scheme)
    _digest = field_validator("payload_manifest_digest")(_digest)

    @field_validator("payload_entries")
    @classmethod
    def _canonical_entries(
        cls,
        values: tuple[PayloadManifestEntry, ...],
    ) -> tuple[PayloadManifestEntry, ...]:
        ordered = tuple(sorted(values, key=lambda value: value.relative_path))
        paths = tuple(entry.relative_path for entry in ordered)
        if any(
            paths_conflict(left, right)
            for index, left in enumerate(paths)
            for right in paths[index + 1 :]
        ):
            raise ValueError("payload manifest file paths must not conflict")
        if any(len(entry.relative_path.split("/")) > MAX_PAYLOAD_TREE_DEPTH for entry in ordered):
            raise ValueError("payload manifest exceeds maximum tree depth")
        if sum(entry.size_bytes for entry in ordered) > MAX_PAYLOAD_TOTAL_BYTES:
            raise ValueError("payload manifest exceeds maximum total bytes")
        return ordered

    @model_validator(mode="after")
    def _canonical_payload(self) -> TrustedArtifactSnapshot:
        if payload_tree_digest(self.payload_entries) != self.payload_manifest_digest:
            raise ValueError("payload_manifest_digest does not match payload entries")
        for label, encoded in (
            ("manifest", self.manifest_json),
            ("scores", self.scores_json),
        ):
            if len(encoded.encode("utf-8")) > MAX_CONTRACT_JSON_BYTES:
                raise ValueError(f"{label}_json exceeds maximum bytes")
            try:
                value = json.loads(encoded)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{label}_json must contain canonical JSON") from exc
            if not isinstance(value, dict) or canonical_json(value) != encoded:
                raise ValueError(f"{label}_json must be a canonical JSON object")
        return self

    def manifest(self) -> dict[str, object]:
        return json.loads(self.manifest_json)


class TargetConsumptionLimits(_Contract):
    max_artifacts: int = Field(default=MAX_HANDLER_ARTIFACTS, ge=0, le=MAX_HANDLER_ARTIFACTS)
    max_text_chars: int = Field(default=MAX_CONTRIBUTION_TEXT, ge=0, le=MAX_CONTRIBUTION_TEXT)
    max_text_bytes: int = Field(default=MAX_CONTRIBUTION_TEXT, ge=0, le=MAX_CONTRIBUTION_TEXT)
    max_payload_bytes: int = Field(
        default=MAX_PAYLOAD_TOTAL_BYTES,
        ge=0,
        le=MAX_PAYLOAD_TOTAL_BYTES,
    )
    max_adapters: int = Field(default=2, ge=0, le=16)


class RuntimeDestinationRoots(_Contract):
    target_data: str
    harness_skills: str
    harness_instruction: str

    _paths = field_validator(
        "target_data", "harness_skills", "harness_instruction"
    )(validate_absolute_runtime_path)


class TargetHandlerInput(_Contract):
    contract_version: Literal["1"] = "1"
    target_id: str
    handler_id: str
    execution_profile: EvolutionExecutionProfile
    destination_roots: RuntimeDestinationRoots
    base_model: str | None = None
    limits: TargetConsumptionLimits = Field(default_factory=TargetConsumptionLimits)
    ranked_artifacts: tuple[TrustedArtifactSnapshot, ...] = Field(
        max_length=MAX_HANDLER_ARTIFACTS
    )

    _ids = field_validator("target_id", "handler_id")(_stable_id)
    _model = field_validator("base_model")(
        lambda value: None if value is None else _text(value)
    )

    @model_validator(mode="after")
    def _ranked_input(self) -> TargetHandlerInput:
        artifact_ids = tuple(item.artifact_id for item in self.ranked_artifacts)
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("handler input artifact IDs must be unique")
        ranks = tuple(item.rank_index for item in self.ranked_artifacts)
        if ranks != tuple(range(len(ranks))):
            raise ValueError("handler input artifacts must retain contiguous resolver ranks")
        return self

    def artifact_ids(self) -> tuple[str, ...]:
        return tuple(item.artifact_id for item in self.ranked_artifacts)


@runtime_checkable
class CoreArtifactPayloadService(Protocol):
    """Contained reads for payload handles issued in ``TargetHandlerInput``."""

    def read_bytes(
        self,
        payload_handle: str,
        relative_path: str,
        *,
        max_bytes: int,
    ) -> bytes: ...


@dataclass(frozen=True, slots=True)
class TargetHandlerServices:
    payloads: CoreArtifactPayloadService


@runtime_checkable
class EvolutionTargetHandler(Protocol):
    def __call__(
        self,
        handler_input: TargetHandlerInput,
        services: TargetHandlerServices,
    ) -> TargetHandlerOutput: ...


__all__ = [
    "CoreArtifactPayloadService",
    "EvolutionTargetHandler",
    "PayloadManifestEntry",
    "RuntimeDestinationRoots",
    "TargetConsumptionLimits",
    "TargetHandlerInput",
    "TargetHandlerServices",
    "TrustedArtifactSnapshot",
    "payload_tree_digest",
    "payload_entries_under_root",
    "payload_tree_size",
]
