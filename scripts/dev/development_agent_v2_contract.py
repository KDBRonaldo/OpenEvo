"""Closed development-only daemon v2 observations shared with the Web Layer."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

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


__all__ = [
    "DevelopmentAttemptAppendedObservationV2",
    "DevelopmentDatasetSealedObservationV2",
    "DevelopmentTaskAdmittedObservationV2",
    "DevelopmentTaskObservationPageV2",
    "DevelopmentTaskObservationV2",
    "DevelopmentTaskTimelineObservationV2",
    "DevelopmentTaskTimelinePageV2",
]
