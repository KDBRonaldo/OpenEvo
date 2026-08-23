"""Closed development-only daemon v2 observations shared with the Web Layer."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator

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


__all__ = [
    "DevelopmentAttemptAppendedObservationV2",
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
