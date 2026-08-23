"""Closed development-only daemon v2 observations shared with the Web Layer."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openevo.backend.contracts.v2 import models as core


class StrictDevelopmentModelV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DevelopmentTaskObservationV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    task_id: core.OpaqueId
    project_id: core.OpaqueId
    state: core.TaskStateV2
    created_at: core.UtcTimestamp
    updated_at: core.UtcTimestamp


class DevelopmentTaskObservationPageV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    items: list[DevelopmentTaskObservationV2] = Field(max_length=100)
    next_cursor: core.Cursor | None = None
    has_more: bool = False


class DevelopmentTaskTimelineEventBaseV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    event_id: core.OpaqueId
    sequence: int = Field(ge=1, le=core.MAX_JAVASCRIPT_SAFE_INTEGER)
    occurred_at: core.UtcTimestamp
    project_id: core.OpaqueId
    task_id: core.OpaqueId


class DevelopmentTaskAdmittedObservationV2(DevelopmentTaskTimelineEventBaseV2):
    event_type: Literal["task_admitted"]


class DevelopmentAttemptAppendedObservationV2(DevelopmentTaskTimelineEventBaseV2):
    event_type: Literal["attempt_appended"]


class DevelopmentDatasetSealedObservationV2(DevelopmentTaskTimelineEventBaseV2):
    event_type: Literal["dataset_sealed"]
    dataset_id: core.OpaqueId
    dataset_sha256: core.Sha256Digest


DevelopmentTaskTimelineObservationV2: TypeAlias = Annotated[
    DevelopmentTaskAdmittedObservationV2
    | DevelopmentAttemptAppendedObservationV2
    | DevelopmentDatasetSealedObservationV2,
    Field(discriminator="event_type"),
]


class DevelopmentTaskTimelinePageV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    items: list[DevelopmentTaskTimelineObservationV2] = Field(max_length=100)
    next_cursor: core.Cursor | None = None
    has_more: bool = False


class DevelopmentWorkspaceEntryV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    path: str = Field(min_length=1, max_length=512)
    kind: Literal["file", "directory", "symlink", "unreadable"]
    byte_size: int = Field(ge=0, le=core.MAX_JAVASCRIPT_SAFE_INTEGER)
    content_sha256: core.Sha256Digest | None = None
    media_type: str | None = Field(default=None, min_length=1, max_length=255)
    content: str | None = Field(default=None, max_length=2 * 1024 * 1024)
    modified_at: core.UtcTimestamp

    @field_validator("path")
    @classmethod
    def _validate_relative_path(cls, value: str) -> str:
        if (
            value.startswith("/")
            or "\\" in value
            or "\x00" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or value.split("/", 1)[0] in {".git", ".openevo"}
        ):
            raise ValueError("workspace path must be a safe relative POSIX path")
        return value


class DevelopmentWorkspacePageV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    project_id: core.OpaqueId
    manifest_sha256: core.Sha256Digest
    items: list[DevelopmentWorkspaceEntryV2] = Field(max_length=100)
    next_cursor: core.Cursor | None = None
    has_more: bool = False
    truncated: bool = False


class DevelopmentWorkspaceMutationV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    project_id: core.OpaqueId
    manifest_sha256: core.Sha256Digest
    entry: DevelopmentWorkspaceEntryV2


class DevelopmentWorkspaceDeleteV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    project_id: core.OpaqueId
    manifest_sha256: core.Sha256Digest
    deleted_path: str = Field(min_length=1, max_length=512)

    @field_validator("deleted_path")
    @classmethod
    def _validate_deleted_path(cls, value: str) -> str:
        if (
            value.startswith("/")
            or "\\" in value
            or "\x00" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or value.split("/", 1)[0] in {".git", ".openevo"}
        ):
            raise ValueError("workspace path must be a safe relative POSIX path")
        return value


class DevelopmentArtifactDocumentV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    path: str = Field(min_length=1, max_length=512)
    media_type: str = Field(min_length=1, max_length=255)
    content: str = Field(max_length=2 * 1024 * 1024)

    @field_validator("path")
    @classmethod
    def _validate_document_path(cls, value: str) -> str:
        if (
            value.startswith("/")
            or "\\" in value
            or "\x00" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("artifact document path must be a safe relative POSIX path")
        return value


class DevelopmentArtifactV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    artifact_id: core.OpaqueId
    project_id: core.OpaqueId
    session_id: core.OpaqueId
    run_id: core.OpaqueId | None = None
    target_id: core.OpaqueId
    artifact_type: Literal[
        "text_memory", "skill_bundle", "agent_system", "parametric_memory", "report"
    ]
    method: core.OpaqueId
    renderer_kind: Literal["markdown", "file_bundle", "structured_summary", "adapter"]
    documents: list[DevelopmentArtifactDocumentV2] = Field(max_length=128)
    manifest: dict[str, Any]
    content_path: str | None = Field(default=None, min_length=1, max_length=512)
    content: str | None = Field(default=None, max_length=2 * 1024 * 1024)
    content_sha256: core.Sha256Digest
    byte_size: int = Field(ge=0, le=core.MAX_SNAPSHOT_BYTES)
    previous_artifact_id: core.OpaqueId | None = None
    promoted: bool
    created_at: core.UtcTimestamp

    @model_validator(mode="after")
    def _validate_bounded_authority(self) -> "DevelopmentArtifactV2":
        document_bytes = sum(
            len(document.content.encode("utf-8")) for document in self.documents
        )
        if document_bytes != self.byte_size:
            raise ValueError("artifact byte_size does not match its document contents")
        if document_bytes > 8 * 1024 * 1024:
            raise ValueError("artifact documents exceed the development v2 byte budget")
        manifest_bytes = json.dumps(
            self.manifest,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(manifest_bytes) > 1024 * 1024:
            raise ValueError("artifact manifest exceeds the development v2 byte budget")
        primary = self.documents[0] if self.documents else None
        if self.content_path != (primary.path if primary is not None else None):
            raise ValueError("artifact content_path does not identify its primary document")
        if self.content != (primary.content if primary is not None else None):
            raise ValueError("artifact content does not match its primary document")
        return self


class DevelopmentArtifactPageV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    items: list[DevelopmentArtifactV2] = Field(max_length=5)
    next_cursor: core.Cursor | None = None
    has_more: bool = False


__all__ = [
    "DevelopmentAttemptAppendedObservationV2",
    "DevelopmentArtifactDocumentV2",
    "DevelopmentArtifactPageV2",
    "DevelopmentArtifactV2",
    "DevelopmentDatasetSealedObservationV2",
    "DevelopmentTaskAdmittedObservationV2",
    "DevelopmentTaskObservationPageV2",
    "DevelopmentTaskObservationV2",
    "DevelopmentTaskTimelineObservationV2",
    "DevelopmentTaskTimelinePageV2",
    "DevelopmentWorkspaceDeleteV2",
    "DevelopmentWorkspaceEntryV2",
    "DevelopmentWorkspaceMutationV2",
    "DevelopmentWorkspacePageV2",
]
