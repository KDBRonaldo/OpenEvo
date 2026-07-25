"""Closed internal authority for staging one committed v2 runtime context."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from openevo.backend.contracts.v2 import models as m2
from openevo.evolution.revisions import (
    AtomicEvolutionAbandonManifestV2,
    AtomicSuccessorCommitV2,
    AtomicSuccessorManifestV2,
)


_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class RuntimeContextBindingV2(m2.ContractModel):
    """Opaque Core-to-Gateway binding with no artifact or host path."""

    runtime_context_binding_contract_version: Literal["2"] = "2"
    source: Literal[
        "empty_genesis",
        "empty_inherited",
        "materialized_successor",
        "materialized_inherited",
    ]
    project_head: m2.ProjectHeadRefV2
    service_generation_sha256: str = Field(pattern=_SHA256_PATTERN)
    framework_lock_sha256: str = Field(pattern=_SHA256_PATTERN)
    successor_transition_id: str | None = Field(
        default=None,
        pattern=_ID_PATTERN,
    )
    source_predecessor_project_head_id: str | None = Field(
        default=None,
        pattern=_ID_PATTERN,
    )
    materialized_context_id: str | None = Field(
        default=None,
        pattern=_ID_PATTERN,
    )
    materialized_context_manifest_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
    )
    selected_artifact_ids: tuple[str, ...] = Field(default=(), max_length=128)

    @field_validator("selected_artifact_ids", mode="before")
    @classmethod
    def _json_artifact_array(cls, value):
        if isinstance(value, list):
            return tuple(value)
        return value

    @field_validator("selected_artifact_ids")
    @classmethod
    def _ordered_artifact_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if (
            len(value) != len(set(value))
            or any(
                not isinstance(item, str)
                or not item
                or len(item.encode("utf-8")) > 128
                for item in value
            )
        ):
            raise ValueError("runtime context artifact IDs are invalid")
        return value

    @model_validator(mode="after")
    def _source_closure(self) -> RuntimeContextBindingV2:
        head = self.project_head
        successor_fields = (
            self.successor_transition_id,
            self.source_predecessor_project_head_id,
            self.materialized_context_id,
            self.materialized_context_manifest_sha256,
        )
        if self.source == "empty_genesis":
            if (
                head.generation != 0
                or head.predecessor_project_head_id is not None
                or head.evolution_revision.artifact_count != 0
                or self.selected_artifact_ids
                or any(value is not None for value in successor_fields)
            ):
                raise ValueError("empty runtime context is not an exact genesis")
        elif self.source == "empty_inherited":
            if (
                head.generation < 1
                or head.predecessor_project_head_id is None
                or head.evolution_revision.artifact_count != 0
                or self.selected_artifact_ids
                or any(value is not None for value in successor_fields)
            ):
                raise ValueError("empty inherited runtime context is inconsistent")
        else:
            if (
                head.generation < 1
                or head.predecessor_project_head_id is None
                or any(value is None for value in successor_fields)
                or len(self.selected_artifact_ids)
                != head.evolution_revision.artifact_count
            ):
                raise ValueError(
                    "materialized runtime context has an incomplete successor closure"
                )
            if (
                self.source == "materialized_successor"
                and self.source_predecessor_project_head_id
                != head.predecessor_project_head_id
            ):
                raise ValueError(
                    "materialized successor context differs from its predecessor"
                )
        return self


def runtime_context_binding_for_head(
    *,
    project_head: m2.ProjectHeadRefV2,
    service_generation_sha256: str,
    framework_lock_sha256: str,
    successor_commit: AtomicSuccessorCommitV2 | None,
) -> RuntimeContextBindingV2:
    """Bind an active head to its exact private materialization receipt."""

    head = m2.ProjectHeadRefV2.model_validate(
        project_head.model_dump(mode="python")
    )
    if head.generation == 0:
        if successor_commit is not None:
            raise ValueError("generation-zero runtime context cannot have a successor receipt")
        return RuntimeContextBindingV2(
            source="empty_genesis",
            project_head=head,
            service_generation_sha256=service_generation_sha256,
            framework_lock_sha256=framework_lock_sha256,
        )
    if type(successor_commit) is not AtomicSuccessorCommitV2:
        raise ValueError("non-genesis runtime context lacks its atomic successor receipt")
    manifest = successor_commit.manifest
    if (
        manifest.project_id != head.project_id
        or manifest.successor_project_head_id != head.project_head_id
        or manifest.successor_generation != head.generation
        or manifest.successor_manifest_sha256 != head.manifest_sha256
        or manifest.predecessor_project_head_id != head.predecessor_project_head_id
        or manifest.evolution_revision_id
        != head.evolution_revision.evolution_revision_id
        or manifest.evolution_revision_manifest_sha256
        != head.evolution_revision.manifest_sha256
        or manifest.runtime_context_snapshot_id
        != head.runtime_context_snapshot.runtime_context_snapshot_id
        or manifest.runtime_context_manifest_sha256
        != head.runtime_context_snapshot.manifest_sha256
        or manifest.registry_sha256 != head.registry_sha256
    ):
        raise ValueError("atomic successor receipt differs from the active project head")
    if len(manifest.method_artifact_ids) != head.evolution_revision.artifact_count:
        raise ValueError("atomic successor receipt has a different artifact set")
    if type(manifest) is AtomicEvolutionAbandonManifestV2:
        if manifest.evolution_revision_id != head.evolution_revision.evolution_revision_id:
            raise ValueError("evolution abandon receipt differs from the inherited revision")
        if manifest.runtime_context_source == "empty_inherited":
            return RuntimeContextBindingV2(
                source="empty_inherited",
                project_head=head,
                service_generation_sha256=service_generation_sha256,
                framework_lock_sha256=framework_lock_sha256,
            )
        return RuntimeContextBindingV2(
            source="materialized_inherited",
            project_head=head,
            service_generation_sha256=service_generation_sha256,
            framework_lock_sha256=framework_lock_sha256,
            successor_transition_id=(
                manifest.materialized_source_successor_transition_id
            ),
            source_predecessor_project_head_id=(
                manifest.materialized_source_predecessor_project_head_id
            ),
            materialized_context_id=manifest.materialized_context_id,
            materialized_context_manifest_sha256=(
                manifest.materialized_context_manifest_sha256
            ),
            selected_artifact_ids=manifest.method_artifact_ids,
        )
    if type(manifest) is not AtomicSuccessorManifestV2:
        raise ValueError("atomic successor receipt has an unsupported manifest")
    if manifest.runtime_context_source == "empty_inherited":
        return RuntimeContextBindingV2(
            source="empty_inherited",
            project_head=head,
            service_generation_sha256=service_generation_sha256,
            framework_lock_sha256=framework_lock_sha256,
        )
    if manifest.runtime_context_source == "materialized_inherited":
        return RuntimeContextBindingV2(
            source="materialized_inherited",
            project_head=head,
            service_generation_sha256=service_generation_sha256,
            framework_lock_sha256=framework_lock_sha256,
            successor_transition_id=(
                manifest.materialized_source_successor_transition_id
            ),
            source_predecessor_project_head_id=(
                manifest.materialized_source_predecessor_project_head_id
            ),
            materialized_context_id=manifest.materialized_context_id,
            materialized_context_manifest_sha256=(
                manifest.materialized_context_manifest_sha256
            ),
            selected_artifact_ids=manifest.method_artifact_ids,
        )
    return RuntimeContextBindingV2(
        source="materialized_successor",
        project_head=head,
        service_generation_sha256=service_generation_sha256,
        framework_lock_sha256=framework_lock_sha256,
        successor_transition_id=manifest.successor_transition_id,
        source_predecessor_project_head_id=manifest.predecessor_project_head_id,
        materialized_context_id=manifest.materialized_context_id,
        materialized_context_manifest_sha256=(
            manifest.materialized_context_manifest_sha256
        ),
        selected_artifact_ids=manifest.method_artifact_ids,
    )


__all__ = ["RuntimeContextBindingV2", "runtime_context_binding_for_head"]
