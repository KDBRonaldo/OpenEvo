"""Editable target selections and deeply immutable resolved plans."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from .contracts import (
    EvolutionExecutionProfile,
    _Contract,
    _digest,
    _json_value,
    _optional_stable_id,
    _stable_id,
    canonical_digest,
    canonical_json,
)


class EvolutionTargetSelection(_Contract):
    """Project editing selection; disabled targets may retain draft settings."""

    target_id: str
    enabled: bool
    method_id: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)

    _target = field_validator("target_id")(_stable_id)
    _method = field_validator("method_id")(_optional_stable_id)

    @field_validator("config")
    @classmethod
    def _copy_config(cls, value: dict[str, Any]) -> dict[str, Any]:
        copied = _json_value(value)
        if not isinstance(copied, dict):  # Field typing makes this unreachable.
            raise ValueError("config must be a JSON object")
        return copied

    @model_validator(mode="after")
    def _selection(self) -> EvolutionTargetSelection:
        if self.enabled and self.method_id is None:
            raise ValueError("enabled target requires method_id")
        return self


class ResolvedEvolutionSelection(_Contract):
    target_id: str
    handler_id: str
    method_id: str
    config_json: str
    config_digest: str
    target_identity_digest: str
    handler_identity_digest: str
    method_identity_digest: str

    _ids = field_validator("target_id", "handler_id", "method_id")(_stable_id)
    _digests = field_validator(
        "config_digest",
        "target_identity_digest",
        "handler_identity_digest",
        "method_identity_digest",
    )(_digest)

    @model_validator(mode="after")
    def _canonical_config(self) -> ResolvedEvolutionSelection:
        try:
            value = json.loads(self.config_json)
        except json.JSONDecodeError as exc:
            raise ValueError("config_json must contain canonical JSON") from exc
        if not isinstance(value, dict) or canonical_json(value) != self.config_json:
            raise ValueError("config_json must be a canonical JSON object")
        if canonical_digest(value) != self.config_digest:
            raise ValueError("config_digest does not match config_json")
        return self

    def config(self) -> dict[str, Any]:
        return json.loads(self.config_json)


class EvolutionPlan(_Contract):
    schema_version: Literal["1"] = "1"
    plan_id: str
    registry_snapshot_digest: str
    execution_profile: EvolutionExecutionProfile
    selections: tuple[ResolvedEvolutionSelection, ...]

    _plan = field_validator("plan_id")(_stable_id)
    _digest = field_validator("registry_snapshot_digest")(_digest)

    @field_validator("selections")
    @classmethod
    def _canonical_order(
        cls,
        values: tuple[ResolvedEvolutionSelection, ...],
    ) -> tuple[ResolvedEvolutionSelection, ...]:
        return tuple(sorted(values, key=lambda value: value.target_id))

    @model_validator(mode="after")
    def _unique_targets(self) -> EvolutionPlan:
        targets = tuple(selection.target_id for selection in self.selections)
        if len(targets) != len(set(targets)):
            raise ValueError("plan contains duplicate target selections")
        return self


__all__ = [
    "EvolutionPlan",
    "EvolutionTargetSelection",
    "ResolvedEvolutionSelection",
]
