"""Closed daemon v2 product contracts shared with the Web Layer.

The ``Development`` names remain temporarily wire-compatible with the browser
surface that was validated before the daemon composition moved into this
package.
"""

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
    project_head_id: core.OpaqueId | None = None
    state: core.TaskStateV2
    created_at: core.UtcTimestamp
    updated_at: core.UtcTimestamp


class DevelopmentTaskObservationPageV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    items: list[DevelopmentTaskObservationV2] = Field(max_length=100)
    next_cursor: core.Cursor | None = None
    has_more: bool = False


class DevelopmentTaskCreateV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    action_id: core.OpaqueId
    project_id: core.OpaqueId
    project_head_id: core.OpaqueId | None = None
    project_name: str = Field(min_length=1, max_length=512)
    task_title: str = Field(min_length=1, max_length=4_096)
    instruction: str = Field(min_length=1, max_length=256 * 1024)


class DevelopmentTaskCancelV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    action_id: core.OpaqueId


class DevelopmentResourceDeleteRequestV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    action_id: core.OpaqueId


class DevelopmentResourceDeleteReceiptV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    action_id: core.OpaqueId
    resource_kind: Literal["project", "task"]
    resource_id: core.OpaqueId
    active_project_id: core.OpaqueId | None = None


class DevelopmentEvolutionSelectionPresentationV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    target_id: core.OpaqueId
    method: core.OpaqueId
    config: dict[str, Any]


class DevelopmentEvolutionErrorPresentationV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    target_id: core.OpaqueId
    method: core.OpaqueId
    message: str = Field(min_length=1, max_length=32_000)


class DevelopmentWorkspaceDiffLineV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    kind: Literal["added", "removed", "context"]
    text: str = Field(max_length=16_384)


class DevelopmentWorkspaceChangeV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    path: str = Field(min_length=1, max_length=512)
    change_type: Literal["created", "modified", "deleted"]
    byte_size: int = Field(ge=0, le=core.MAX_SNAPSHOT_BYTES)
    media_type: str | None = Field(default=None, min_length=1, max_length=255)
    content: str | None = Field(default=None, max_length=2 * 1024 * 1024)
    previous_path: str | None = Field(default=None, min_length=1, max_length=512)
    diff_lines: list[DevelopmentWorkspaceDiffLineV2] = Field(max_length=2_000)


class DevelopmentTaskPresentationV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    task_id: core.OpaqueId
    project_id: core.OpaqueId
    project_head_id: core.OpaqueId | None = None
    task_title: str = Field(min_length=1, max_length=4_096)
    instruction: str = Field(min_length=1, max_length=256 * 1024)
    response: str | None = Field(default=None, max_length=4 * 1024 * 1024)
    model: str | None = Field(default=None, min_length=1, max_length=512)
    state: Literal["running", "cancelling", "completed", "failed", "cancelled"]
    duration_ms: int | None = Field(default=None, ge=0, le=core.MAX_JAVASCRIPT_SAFE_INTEGER)
    selected_evolution: list[DevelopmentEvolutionSelectionPresentationV2] = Field(max_length=64)
    evolution_errors: list[DevelopmentEvolutionErrorPresentationV2] = Field(max_length=64)
    workspace_changes: list[DevelopmentWorkspaceChangeV2] = Field(max_length=2_000)
    context_artifact_ids: list[core.OpaqueId] = Field(max_length=256)
    evolution_evidence_ready: bool
    error: str | None = Field(default=None, max_length=32_000)
    created_at: core.UtcTimestamp
    updated_at: core.UtcTimestamp


class DevelopmentTaskPresentationPageV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    items: list[DevelopmentTaskPresentationV2] = Field(max_length=25)
    next_cursor: core.Cursor | None = None
    has_more: bool = False


class DevelopmentProjectAuthorityV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    project_id: core.OpaqueId
    display_name: str = Field(min_length=1, max_length=512)
    config: core.ScienceProjectConfigV2
    active_project_head_id: core.OpaqueId | None = None
    created_at: core.UtcTimestamp
    updated_at: core.UtcTimestamp


class DevelopmentProjectHeadV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    project_head_id: core.OpaqueId
    project_id: core.OpaqueId
    generation: int = Field(ge=0, le=core.MAX_JAVASCRIPT_SAFE_INTEGER)
    predecessor_project_head_id: core.OpaqueId | None = None
    source_evolution_run_id: core.OpaqueId | None = None
    artifact_ids: list[core.OpaqueId] = Field(max_length=256)
    workspace_manifest_sha256: core.Sha256Digest
    workspace_entry_count: int = Field(ge=0, le=core.MAX_JAVASCRIPT_SAFE_INTEGER)
    workspace_byte_size: int = Field(ge=0, le=core.MAX_SNAPSHOT_BYTES)
    manifest_sha256: core.Sha256Digest
    created_at: core.UtcTimestamp


class DevelopmentStateV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    active_project_id: core.OpaqueId | None = None
    projects: list[DevelopmentProjectAuthorityV2] = Field(max_length=1_000)
    project_heads: list[DevelopmentProjectHeadV2] = Field(default_factory=list, max_length=10_000)


class DevelopmentProjectCreateV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    action_id: core.OpaqueId
    project_id: core.OpaqueId
    display_name: str = Field(min_length=1, max_length=512)
    config: core.ScienceProjectConfigV2


class DevelopmentProjectUpdateV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    action_id: core.OpaqueId
    display_name: str = Field(min_length=1, max_length=512)
    config: core.ScienceProjectConfigV2


class DevelopmentProjectActivateV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    action_id: core.OpaqueId


class DevelopmentCapabilitiesV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    authority: Literal["development_catalog_unverified"]
    capabilities: dict[str, Any]

    @model_validator(mode="after")
    def _validate_capability_budget(self) -> "DevelopmentCapabilitiesV2":
        encoded = json.dumps(
            self.capabilities,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > 1024 * 1024:
            raise ValueError("development capabilities exceed the v2 byte budget")
        return self


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
        document_bytes = sum(len(document.content.encode("utf-8")) for document in self.documents)
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


class DevelopmentEvolutionSelectionV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    target_id: core.OpaqueId
    method: core.OpaqueId
    config: dict[str, Any]

    @model_validator(mode="after")
    def _validate_config_budget(self) -> "DevelopmentEvolutionSelectionV2":
        encoded = json.dumps(
            self.config,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > 192 * 1024:
            raise ValueError("Evolution method config exceeds the development v2 byte budget")
        return self


class DevelopmentEvolutionRunCreateV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    action_id: core.OpaqueId
    project_id: core.OpaqueId
    base_project_head_id: core.OpaqueId | None = None
    base_project_head_manifest_sha256: core.Sha256Digest | None = None
    source_task_ids: list[core.OpaqueId] = Field(min_length=1, max_length=128)
    selections: list[DevelopmentEvolutionSelectionV2] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _validate_unique_inputs(self) -> "DevelopmentEvolutionRunCreateV2":
        if len(set(self.source_task_ids)) != len(self.source_task_ids):
            raise ValueError("source_task_ids must not contain duplicates")
        target_ids = [selection.target_id for selection in self.selections]
        if len(set(target_ids)) != len(target_ids):
            raise ValueError("selections must contain at most one method per target")
        return self


class DevelopmentEvolutionRunApplyV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"


class DevelopmentEvolutionRunV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    run_id: core.OpaqueId
    action_id: core.OpaqueId
    project_id: core.OpaqueId
    base_project_head_id: core.OpaqueId | None = None
    base_project_head_manifest_sha256: core.Sha256Digest | None = None
    applied_project_head_id: core.OpaqueId | None = None
    source_task_ids: list[core.OpaqueId] = Field(min_length=1, max_length=128)
    selections: list[DevelopmentEvolutionSelectionV2] = Field(min_length=1, max_length=64)
    state: Literal["running", "candidate_ready", "applied", "failed"]
    artifact_ids: list[core.OpaqueId] = Field(max_length=256)
    error: str | None = Field(default=None, max_length=32_000)
    created_at: core.UtcTimestamp
    updated_at: core.UtcTimestamp


class DevelopmentEvolutionRunPageV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    items: list[DevelopmentEvolutionRunV2] = Field(max_length=25)
    next_cursor: core.Cursor | None = None
    has_more: bool = False


class DevelopmentEvolutionJobRetryV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    action_id: core.OpaqueId


class DevelopmentEvolutionAttemptV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    attempt_id: core.OpaqueId
    action_id: core.OpaqueId | None = None
    job_id: core.OpaqueId
    ordinal: int = Field(ge=1, le=100)
    state: Literal["queued", "running", "completed", "failed", "cancelled"]
    stage: str = Field(min_length=1, max_length=128)
    artifact_ids: list[core.OpaqueId] = Field(max_length=256)
    error_code: str | None = Field(default=None, min_length=1, max_length=128)
    error_message: str | None = Field(default=None, min_length=1, max_length=32_000)
    logs: list[str] = Field(max_length=512)
    created_at: core.UtcTimestamp
    started_at: core.UtcTimestamp | None = None
    completed_at: core.UtcTimestamp | None = None
    updated_at: core.UtcTimestamp

    @field_validator("logs")
    @classmethod
    def _validate_logs(cls, value: list[str]) -> list[str]:
        if any(not item or len(item) > 16_384 for item in value):
            raise ValueError("Evolution attempt logs must be non-empty and bounded")
        if sum(len(item.encode("utf-8")) for item in value) > 2 * 1024 * 1024:
            raise ValueError("Evolution attempt logs exceed the development v2 byte budget")
        return value


class DevelopmentEvolutionJobV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    job_id: core.OpaqueId
    project_id: core.OpaqueId
    task_id: core.OpaqueId
    run_id: core.OpaqueId | None = None
    target_id: core.OpaqueId
    method_id: core.OpaqueId
    requested_method_id: core.OpaqueId
    resolver_input_artifact_ids: list[core.OpaqueId] = Field(max_length=256)
    previous_artifact_id: core.OpaqueId | None = None
    config: dict[str, Any]
    state: Literal["queued", "running", "completed", "failed"]
    artifact_ids: list[core.OpaqueId] = Field(max_length=256)
    error: str | None = Field(default=None, max_length=32_000)
    attempts: list[DevelopmentEvolutionAttemptV2] = Field(max_length=100)
    created_at: core.UtcTimestamp
    updated_at: core.UtcTimestamp

    @model_validator(mode="after")
    def _validate_job(self) -> "DevelopmentEvolutionJobV2":
        encoded = json.dumps(
            self.config,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > 192 * 1024:
            raise ValueError("Evolution Job config exceeds the development v2 byte budget")
        if any(attempt.job_id != self.job_id for attempt in self.attempts):
            raise ValueError("Evolution attempt crossed Job authority")
        if [attempt.ordinal for attempt in self.attempts] != list(
            range(1, len(self.attempts) + 1)
        ):
            raise ValueError("Evolution attempt ordinals must be contiguous")
        return self


class DevelopmentEvolutionJobPageV2(StrictDevelopmentModelV2):
    schema_version: Literal["2"] = "2"
    items: list[DevelopmentEvolutionJobV2] = Field(max_length=25)
    next_cursor: core.Cursor | None = None
    has_more: bool = False


__all__ = [
    "DevelopmentAttemptAppendedObservationV2",
    "DevelopmentCapabilitiesV2",
    "DevelopmentArtifactDocumentV2",
    "DevelopmentArtifactPageV2",
    "DevelopmentArtifactV2",
    "DevelopmentEvolutionRunApplyV2",
    "DevelopmentEvolutionRunCreateV2",
    "DevelopmentEvolutionRunPageV2",
    "DevelopmentEvolutionRunV2",
    "DevelopmentEvolutionSelectionV2",
    "DevelopmentEvolutionAttemptV2",
    "DevelopmentEvolutionJobPageV2",
    "DevelopmentEvolutionJobRetryV2",
    "DevelopmentEvolutionJobV2",
    "DevelopmentDatasetSealedObservationV2",
    "DevelopmentTaskAdmittedObservationV2",
    "DevelopmentProjectActivateV2",
    "DevelopmentProjectAuthorityV2",
    "DevelopmentProjectCreateV2",
    "DevelopmentProjectUpdateV2",
    "DevelopmentResourceDeleteReceiptV2",
    "DevelopmentResourceDeleteRequestV2",
    "DevelopmentStateV2",
    "DevelopmentTaskCancelV2",
    "DevelopmentTaskCreateV2",
    "DevelopmentTaskObservationPageV2",
    "DevelopmentTaskObservationV2",
    "DevelopmentTaskPresentationPageV2",
    "DevelopmentTaskPresentationV2",
    "DevelopmentTaskTimelineObservationV2",
    "DevelopmentTaskTimelinePageV2",
    "DevelopmentWorkspaceDeleteV2",
    "DevelopmentWorkspaceEntryV2",
    "DevelopmentWorkspaceMutationV2",
    "DevelopmentWorkspacePageV2",
]
