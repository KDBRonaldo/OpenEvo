"""Typed plan-bound job materialization on the existing worker lifecycle."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import Field, field_validator, model_validator

from openevo.evolution.framework.contracts import (
    _Contract,
    _json_value,
    _stable_id,
    _text,
    canonical_digest,
)
from openevo.evolution.framework.execution import (
    MethodExecutionEnvelope,
    MethodInputResolution,
    build_execution_envelope,
    resolve_method_inputs,
)
from openevo.evolution.framework.plan import EvolutionPlan, EvolutionTargetSelection
from openevo.evolution.framework.registry import RegistrySnapshot
from openevo.evolution.models import WorkerClaimInputArtifact


class PlannedInputBinding(_Contract):
    """Ordered artifact IDs assigned to one descriptor input binding."""

    binding_id: str
    artifact_ids: tuple[str, ...] = ()

    _binding = field_validator("binding_id")(_stable_id)
    _artifacts = field_validator("artifact_ids")(
        lambda values: tuple(_text(value) for value in values)
    )


class PlanBoundJobCreateRequest(_Contract):
    """Create one job whose complete execution identity is fixed by a plan."""

    plan: EvolutionPlan
    target_id: str
    job_type: str = Field(min_length=1, max_length=512)
    input_bindings: tuple[PlannedInputBinding, ...]
    successor_transition_id: str | None = None
    predecessor_successor_transition_id: str | None = None
    core_config: dict[str, Any] = Field(default_factory=dict)
    priority: int = 100

    _target = field_validator("target_id")(_stable_id)
    _job_type = field_validator("job_type")(_text)
    _transition_ids = field_validator(
        "successor_transition_id",
        "predecessor_successor_transition_id",
    )(lambda value: None if value is None else _stable_id(value))

    @field_validator("core_config")
    @classmethod
    def _copy_core_config(cls, value: dict[str, Any]) -> dict[str, Any]:
        copied = _json_value(value)
        if not isinstance(copied, dict):  # Field typing makes this unreachable.
            raise ValueError("core_config must be a JSON object")
        return copied

    @model_validator(mode="after")
    def _target_is_selected_once(self) -> PlanBoundJobCreateRequest:
        matches = tuple(
            selection
            for selection in self.plan.selections
            if selection.target_id == self.target_id
        )
        if len(matches) != 1:
            raise ValueError("target_id must identify one enabled plan selection")
        binding_ids = tuple(binding.binding_id for binding in self.input_bindings)
        if len(binding_ids) != len(set(binding_ids)):
            raise ValueError("planned input binding IDs must be unique")
        if (
            self.predecessor_successor_transition_id is not None
            and self.successor_transition_id is None
        ):
            raise ValueError(
                "predecessor transition authority requires a successor transition owner"
            )
        return self

    def selection(self):
        return next(
            selection
            for selection in self.plan.selections
            if selection.target_id == self.target_id
        )


class PlanBoundJobRetryRequest(_Contract):
    """Retry one exact terminal job without changing its immutable plan."""

    retry_request_id: str
    plan_id: str
    target_id: str

    _ids = field_validator(
        "retry_request_id",
        "plan_id",
        "target_id",
    )(_stable_id)


@dataclass(frozen=True, slots=True)
class MaterializedPlanBoundJob:
    """Validated immutable inputs ready for one transactional job insert."""

    envelope: MethodExecutionEnvelope
    input_resolution: MethodInputResolution
    method_identity_digest: str
    output_artifact_types: tuple[str, ...]


def validate_plan_against_snapshot(
    plan: EvolutionPlan,
    snapshot: RegistrySnapshot,
) -> EvolutionPlan:
    """Recompile a plan and reject any descriptor or identity drift."""

    selections = tuple(
        EvolutionTargetSelection(
            target_id=selection.target_id,
            enabled=True,
            method_id=selection.method_id,
            config=selection.config(),
        )
        for selection in plan.selections
    )
    expected = snapshot.compile_plan(
        plan_id=plan.plan_id,
        selections=selections,
        profile=plan.execution_profile,
    )
    if expected != plan:
        raise ValueError("plan does not match the active registry identity")
    return expected


def materialize_plan_bound_job(
    request: PlanBoundJobCreateRequest,
    *,
    snapshot: RegistrySnapshot,
    artifacts_by_binding: Mapping[str, Sequence[WorkerClaimInputArtifact]],
) -> MaterializedPlanBoundJob:
    """Resolve ordered input snapshots and construct the legacy-safe envelope."""

    validate_plan_against_snapshot(request.plan, snapshot)
    selection = request.selection()
    descriptor = snapshot.methods[selection.method_id]
    expected_binding_ids = tuple(
        binding.binding_id for binding in descriptor.input_bindings
    )
    supplied_binding_ids = tuple(binding.binding_id for binding in request.input_bindings)
    if supplied_binding_ids != expected_binding_ids:
        raise ValueError(
            "planned input bindings do not match descriptor order: "
            f"expected {expected_binding_ids!r}, got {supplied_binding_ids!r}"
        )
    if set(artifacts_by_binding) != set(expected_binding_ids):
        raise ValueError("resolved input artifact bindings do not match the request")

    resolution = resolve_method_inputs(
        descriptor.input_bindings,
        artifacts_by_binding,
    )
    envelope = build_execution_envelope(
        plan_id=request.plan.plan_id,
        plan_digest=canonical_digest(request.plan),
        registry_snapshot_digest=request.plan.registry_snapshot_digest,
        target_id=request.target_id,
        method_id=selection.method_id,
        method_identity_digest=selection.method_identity_digest,
        user_config=selection.config(),
        core_config=request.core_config,
        input_bindings=resolution.bindings,
        output_artifact_types=descriptor.output_artifact_types,
    )
    return MaterializedPlanBoundJob(
        envelope=envelope,
        input_resolution=resolution,
        method_identity_digest=selection.method_identity_digest,
        output_artifact_types=descriptor.output_artifact_types,
    )


__all__ = [
    "MaterializedPlanBoundJob",
    "PlanBoundJobCreateRequest",
    "PlanBoundJobRetryRequest",
    "PlannedInputBinding",
    "materialize_plan_bound_job",
    "validate_plan_against_snapshot",
]
