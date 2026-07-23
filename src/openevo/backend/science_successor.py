"""Closed internal evidence models for atomic science successor publication."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openevo.backend.contracts.v2 import models as m2


_SCIENCE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_SCIENCE_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class _ScienceSuccessorModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class ScienceSuccessorMethodPlanV2(_ScienceSuccessorModel):
    target_id: str = Field(pattern=_SCIENCE_ID_PATTERN)
    method_id: str = Field(pattern=_SCIENCE_ID_PATTERN)
    output_artifact_type: Literal[
        "text_memory",
        "skill_bundle",
        "agent_system",
        "parametric_memory",
    ]


class ScienceSuccessorPlanV2(_ScienceSuccessorModel):
    successor_plan_contract_version: Literal["2"] = "2"
    project_id: str = Field(pattern=_SCIENCE_ID_PATTERN)
    task_id: str = Field(pattern=_SCIENCE_ID_PATTERN)
    task_admission_id: str = Field(pattern=_SCIENCE_ID_PATTERN)
    admission_sha256: str = Field(pattern=_SCIENCE_SHA256_PATTERN)
    accepted_attempt_id: str = Field(pattern=_SCIENCE_ID_PATTERN)
    predecessor_project_head_id: str = Field(pattern=_SCIENCE_ID_PATTERN)
    normalized_evolution_intent_sha256: str = Field(
        pattern=_SCIENCE_SHA256_PATTERN
    )
    enabled_methods: tuple[ScienceSuccessorMethodPlanV2, ...] = Field(
        default=(),
        max_length=128,
    )

    @model_validator(mode="after")
    def _unique_targets(self) -> ScienceSuccessorPlanV2:
        target_ids = tuple(item.target_id for item in self.enabled_methods)
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("successor plan target IDs must be unique")
        if target_ids != tuple(sorted(target_ids)):
            raise ValueError("successor plan target IDs must be sorted")
        return self


def science_successor_plan_sha256(plan: ScienceSuccessorPlanV2) -> str:
    if type(plan) is not ScienceSuccessorPlanV2:
        raise TypeError("successor plan digest requires ScienceSuccessorPlanV2")
    payload = json.dumps(
        plan.model_dump(mode="json"),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class SealedTranscriptDatasetV2(_ScienceSuccessorModel):
    dataset_contract_version: Literal["2"] = "2"
    dataset_id: str = Field(pattern=_SCIENCE_ID_PATTERN)
    artifact_id: str = Field(pattern=_SCIENCE_ID_PATTERN)
    manifest_sha256: str = Field(pattern=_SCIENCE_SHA256_PATTERN)
    record_count: int = Field(ge=1, le=10_000_000)
    task_id: str = Field(pattern=_SCIENCE_ID_PATTERN)
    task_admission_id: str = Field(pattern=_SCIENCE_ID_PATTERN)
    accepted_attempt_id: str = Field(pattern=_SCIENCE_ID_PATTERN)
    capture_mode: Literal["transcript"]
    token_level_metrics_available: Literal[False]
    sealed: Literal[True]


class ScienceMethodOutputV2(_ScienceSuccessorModel):
    target_id: str = Field(pattern=_SCIENCE_ID_PATTERN)
    method_id: str = Field(pattern=_SCIENCE_ID_PATTERN)
    artifact_id: str = Field(pattern=_SCIENCE_ID_PATTERN)
    artifact_type: Literal[
        "text_memory",
        "skill_bundle",
        "agent_system",
        "parametric_memory",
    ]
    manifest_sha256: str = Field(pattern=_SCIENCE_SHA256_PATTERN)
    byte_size: int = Field(ge=0, le=m2.MAX_SNAPSHOT_BYTES)
    execution_boundary: Literal["outside_inference"]


class ValidatedScienceOutputsV2(_ScienceSuccessorModel):
    output_validation_contract_version: Literal["2"] = "2"
    project_id: str = Field(pattern=_SCIENCE_ID_PATTERN)
    successor_transition_id: str = Field(pattern=_SCIENCE_ID_PATTERN)
    predecessor_project_head_id: str = Field(pattern=_SCIENCE_ID_PATTERN)
    dataset: SealedTranscriptDatasetV2
    outputs: tuple[ScienceMethodOutputV2, ...] = Field(default=(), max_length=1024)
    evolution_revision: m2.EvolutionRevisionRefV2

    @model_validator(mode="after")
    def _complete_outputs(self) -> ValidatedScienceOutputsV2:
        if self.evolution_revision.project_id != self.project_id:
            raise ValueError("evolution revision belongs to another project")
        artifact_ids = tuple(item.artifact_id for item in self.outputs)
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("validated method output IDs must be unique")
        if self.evolution_revision.artifact_count != len(self.outputs):
            raise ValueError("evolution revision artifact count is incomplete")
        return self


class SuccessorMaterializationV2(_ScienceSuccessorModel):
    successor_materialization_contract_version: Literal["2"] = "2"
    project_id: str = Field(pattern=_SCIENCE_ID_PATTERN)
    successor_transition_id: str = Field(pattern=_SCIENCE_ID_PATTERN)
    predecessor_project_head_id: str = Field(pattern=_SCIENCE_ID_PATTERN)
    materialized_context_id: str = Field(pattern=_SCIENCE_ID_PATTERN)
    materialized_context_manifest_sha256: str = Field(
        pattern=_SCIENCE_SHA256_PATTERN
    )
    runtime_context_snapshot: m2.RuntimeContextSnapshotRefV2

    @model_validator(mode="after")
    def _runtime_context_project(self) -> SuccessorMaterializationV2:
        if self.runtime_context_snapshot.project_id != self.project_id:
            raise ValueError("materialized runtime context belongs to another project")
        return self


class AcceptedWorkspaceResultV2(_ScienceSuccessorModel):
    workspace_result_contract_version: Literal["2"] = "2"
    project_id: str = Field(pattern=_SCIENCE_ID_PATTERN)
    task_id: str = Field(pattern=_SCIENCE_ID_PATTERN)
    accepted_attempt_id: str = Field(pattern=_SCIENCE_ID_PATTERN)
    workspace_snapshot: m2.WorkspaceSnapshotRefV2

    @model_validator(mode="after")
    def _workspace_project(self) -> AcceptedWorkspaceResultV2:
        if self.workspace_snapshot.project_id != self.project_id:
            raise ValueError("accepted workspace result belongs to another project")
        return self


class ScienceSuccessorPreparationContextV2(_ScienceSuccessorModel):
    task: m2.TaskV2
    accepted_attempt: m2.AttemptRefV2
    transition: m2.SuccessorTransitionV2
    plan: ScienceSuccessorPlanV2

    @model_validator(mode="after")
    def _exact_ownership(self) -> ScienceSuccessorPreparationContextV2:
        reference = self.transition.transition
        if (
            self.plan.project_id != self.task.project_id
            or self.plan.task_id != self.task.task_id
            or self.plan.task_admission_id != self.task.admission.task_admission_id
            or self.plan.admission_sha256 != self.task.admission.admission_sha256
            or self.plan.accepted_attempt_id != self.accepted_attempt.attempt_id
            or self.plan.predecessor_project_head_id
            != self.task.admission.predecessor_project_head.project_head_id
            or self.plan.normalized_evolution_intent_sha256
            != self.task.admission.normalized_evolution_intent_sha256
            or self.accepted_attempt not in self.task.attempts
            or reference.task_admission != self.task.admission
            or reference.accepted_attempt != self.accepted_attempt
            or reference.predecessor_project_head
            != self.task.admission.predecessor_project_head
            or reference.plan_sha256 != science_successor_plan_sha256(self.plan)
        ):
            raise ValueError("successor preparation ownership is inconsistent")
        return self


__all__ = [
    "AcceptedWorkspaceResultV2",
    "ScienceMethodOutputV2",
    "ScienceSuccessorMethodPlanV2",
    "ScienceSuccessorPlanV2",
    "ScienceSuccessorPreparationContextV2",
    "SealedTranscriptDatasetV2",
    "SuccessorMaterializationV2",
    "ValidatedScienceOutputsV2",
    "science_successor_plan_sha256",
]
