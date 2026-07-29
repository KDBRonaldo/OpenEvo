"""Method invocation, input binding, and Core harness service contracts."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TypeAlias, runtime_checkable

from pydantic import Field, field_validator, model_validator

from openevo.evolution.models import (
    ArtifactRegisterRequest,
    ArtifactType,
    WorkerClaimInputArtifact,
    WorkerClaimedJob,
)

from .contracts import (
    CaptureMode,
    MAX_JAVASCRIPT_SAFE_INTEGER,
    _Contract,
    _digest,
    _stable_id,
    _text,
    canonical_digest,
    canonical_json,
)

if TYPE_CHECKING:
    from openevo.evolution.parametric.contracts import CoreParametricTrainer


CORE_CONFIG_RESERVED_KEYS = frozenset(
    {
        "agent_system_audit",
        "candidate_evaluations",
        "compatibility",
        "experiment_id",
        "experiment_name",
        "forbidden_literals",
        "lineage",
        "name",
        "policy_version",
        "promoted",
        "promotion_contract",
        "promotion_gate",
        "promotion_support",
        "round_index",
        "scores",
        "task_id",
        "task_tags",
        "tags",
    }
)
MAX_HARNESS_OUTPUT_TOKENS = 1_048_576


class InputBindingSource(StrEnum):
    CURRENT_DATASET = "current_dataset"
    HISTORY_DATASETS = "history_datasets"
    CURRENT_TARGET_ARTIFACTS = "current_target_artifacts"
    EXPLICIT_INPUTS = "explicit_inputs"


class MethodInputBinding(_Contract):
    binding_id: str
    source: InputBindingSource
    artifact_type: str
    min_count: int = Field(
        default=0,
        ge=0,
        le=MAX_JAVASCRIPT_SAFE_INTEGER,
    )
    max_count: int | None = Field(
        default=None,
        ge=1,
        le=MAX_JAVASCRIPT_SAFE_INTEGER,
    )

    _ids = field_validator("binding_id", "artifact_type")(_stable_id)

    @model_validator(mode="after")
    def _count_range(self) -> MethodInputBinding:
        if self.max_count is not None and self.min_count > self.max_count:
            raise ValueError("input binding min_count must not exceed max_count")
        return self


class ResolvedMethodInputBinding(_Contract):
    binding_id: str
    artifact_ids: tuple[str, ...]
    artifact_digests: tuple[str, ...]

    _binding = field_validator("binding_id")(_stable_id)
    _artifacts = field_validator("artifact_ids")(
        lambda values: tuple(_text(value) for value in values)
    )
    _digests = field_validator("artifact_digests")(
        lambda values: tuple(_digest(value) for value in values)
    )

    @model_validator(mode="after")
    def _paired_artifacts(self) -> ResolvedMethodInputBinding:
        if len(self.artifact_ids) != len(self.artifact_digests):
            raise ValueError("resolved artifact IDs and digests must have equal length")
        return self


def worker_input_artifact_digest(artifact: WorkerClaimInputArtifact) -> str:
    # Keep the v1 envelope identity stable across the v0.1.9 addition of
    # consumer-side file receipts. Artifact IDs bind immutable Store authority;
    # the manifest/records receipts are reissued at claim and reverified by the
    # method immediately before consumption.
    return canonical_digest(
        {
            "artifact_id": artifact.artifact_id,
            "type": (
                artifact.type.value
                if isinstance(artifact.type, ArtifactType)
                else artifact.type
            ),
            "uri": artifact.uri,
            "name": artifact.name,
        }
    )


@dataclass(frozen=True, slots=True)
class MethodInputResolution:
    """Ordered binding result used to materialize the existing worker job."""

    input_artifacts: tuple[WorkerClaimInputArtifact, ...]
    bindings: tuple[ResolvedMethodInputBinding, ...]


def resolve_method_inputs(
    bindings: Sequence[MethodInputBinding],
    candidates_by_binding: Mapping[str, Sequence[WorkerClaimInputArtifact]],
) -> MethodInputResolution:
    """Flatten candidates without changing binding, source, or duplicate order."""

    binding_ids = tuple(binding.binding_id for binding in bindings)
    if len(binding_ids) != len(set(binding_ids)):
        raise ValueError("method input binding IDs must be unique")
    unknown = set(candidates_by_binding).difference(binding_ids)
    if unknown:
        raise ValueError(f"unknown input binding: {', '.join(sorted(unknown))}")

    resolved_artifacts: list[WorkerClaimInputArtifact] = []
    resolved_bindings: list[ResolvedMethodInputBinding] = []
    for binding in bindings:
        candidates = tuple(candidates_by_binding.get(binding.binding_id, ()))
        if len(candidates) < binding.min_count:
            raise ValueError(
                f"input binding {binding.binding_id!r} requires at least "
                f"{binding.min_count} artifact(s)"
            )
        if binding.max_count is not None and len(candidates) > binding.max_count:
            raise ValueError(
                f"input binding {binding.binding_id!r} allows at most "
                f"{binding.max_count} artifact(s)"
            )
        for candidate in candidates:
            if str(candidate.type) != binding.artifact_type:
                raise ValueError(
                    f"input binding {binding.binding_id!r} requires artifact type "
                    f"{binding.artifact_type!r}"
                )
            resolved_artifacts.append(
                WorkerClaimInputArtifact.model_validate(
                    candidate.model_dump(mode="python")
                )
            )
        resolved_bindings.append(
            ResolvedMethodInputBinding(
                binding_id=binding.binding_id,
                artifact_ids=tuple(
                    candidate.artifact_id for candidate in candidates
                ),
                artifact_digests=tuple(
                    worker_input_artifact_digest(candidate)
                    for candidate in candidates
                ),
            )
        )
    return MethodInputResolution(
        input_artifacts=tuple(resolved_artifacts),
        bindings=tuple(resolved_bindings),
    )


def validate_user_config_schema_ownership(schema: Mapping[str, Any]) -> None:
    """Reject top-level method fields owned by the Core execution envelope."""

    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        return
    reserved = set(properties).intersection(CORE_CONFIG_RESERVED_KEYS)
    if reserved:
        raise ValueError(
            "method user config schema declares Core-owned fields: "
            + ", ".join(sorted(reserved))
        )


class HarnessInferenceRequest(_Contract):
    request_id: str
    harness_id: str
    system_instruction: str = Field(default="", max_length=1_048_576)
    prompt: str = Field(min_length=1, max_length=1_048_576)
    model_name: str | None = Field(default=None, max_length=4096)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_output_tokens: int | None = Field(
        default=None,
        ge=1,
        le=MAX_HARNESS_OUTPUT_TOKENS,
    )
    timeout_seconds: float = Field(default=300.0, gt=0.0, le=86_400.0)

    _ids = field_validator("request_id", "harness_id")(_stable_id)
    _model = field_validator("model_name")(
        lambda value: None if value is None else _text(value)
    )

    @model_validator(mode="after")
    def _bounded_utf8(self) -> HarnessInferenceRequest:
        for label, value, limit in (
            ("system_instruction", self.system_instruction, 1_048_576),
            ("prompt", self.prompt, 1_048_576),
            ("model_name", self.model_name, 4096),
        ):
            if value is not None and len(value.encode("utf-8")) > limit:
                raise ValueError(f"{label} exceeds maximum UTF-8 bytes")
        return self


class HarnessInferenceResponse(_Contract):
    request_id: str
    text: str = Field(max_length=1_048_576)
    capture_mode: CaptureMode
    transcript_ref: str | None = Field(default=None, max_length=4096)

    _id = field_validator("request_id")(_stable_id)
    _transcript = field_validator("transcript_ref")(
        lambda value: None if value is None else _text(value)
    )

    @model_validator(mode="after")
    def _bounded_utf8(self) -> HarnessInferenceResponse:
        for label, value, limit in (
            ("text", self.text, 1_048_576),
            ("transcript_ref", self.transcript_ref, 4096),
        ):
            if value is not None and len(value.encode("utf-8")) > limit:
                raise ValueError(f"{label} exceeds maximum UTF-8 bytes")
        return self


@runtime_checkable
class CoreHarnessService(Protocol):
    """The only model/harness inference surface available to method plugins."""

    def infer(self, request: HarnessInferenceRequest) -> HarnessInferenceResponse: ...


class MethodExecutionEnvelope(_Contract):
    plan_id: str
    plan_digest: str
    registry_snapshot_digest: str
    target_id: str
    method_id: str
    method_identity_digest: str
    user_config_json: str
    user_config_digest: str
    core_config_json: str
    core_config_digest: str
    input_bindings: tuple[ResolvedMethodInputBinding, ...]
    output_artifact_types: tuple[str, ...]

    _ids = field_validator("plan_id", "target_id", "method_id")(_stable_id)
    _digests = field_validator(
        "plan_digest",
        "registry_snapshot_digest",
        "method_identity_digest",
        "user_config_digest",
        "core_config_digest",
    )(_digest)
    _output_types = field_validator("output_artifact_types")(
        lambda values: tuple(_stable_id(value) for value in values)
    )

    @model_validator(mode="after")
    def _canonical_configs(self) -> MethodExecutionEnvelope:
        for label, encoded, digest in (
            ("user", self.user_config_json, self.user_config_digest),
            ("core", self.core_config_json, self.core_config_digest),
        ):
            try:
                value = json.loads(encoded)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{label}_config_json must contain canonical JSON") from exc
            if not isinstance(value, dict) or canonical_json(value) != encoded:
                raise ValueError(f"{label}_config_json must be a canonical JSON object")
            if canonical_digest(value) != digest:
                raise ValueError(f"{label}_config_digest does not match config JSON")
        overlap = set(self.user_config()).intersection(self.core_config())
        if overlap:
            raise ValueError(
                "user config cannot shadow Core-owned config fields: "
                + ", ".join(sorted(overlap))
            )
        reserved_user_keys = set(self.user_config()).intersection(
            CORE_CONFIG_RESERVED_KEYS
        )
        if reserved_user_keys:
            raise ValueError(
                "user config fields are reserved for Core: "
                + ", ".join(sorted(reserved_user_keys))
            )
        non_core_keys = set(self.core_config()).difference(CORE_CONFIG_RESERVED_KEYS)
        if non_core_keys:
            raise ValueError(
                "core config may only contain Core-owned fields: "
                + ", ".join(sorted(non_core_keys))
            )
        binding_ids = tuple(binding.binding_id for binding in self.input_bindings)
        if len(binding_ids) != len(set(binding_ids)):
            raise ValueError("resolved input binding IDs must be unique")
        if not self.output_artifact_types:
            raise ValueError("execution envelope must declare output artifact types")
        if len(self.output_artifact_types) != len(set(self.output_artifact_types)):
            raise ValueError("execution envelope output artifact types must be unique")
        return self

    def user_config(self) -> dict[str, Any]:
        return json.loads(self.user_config_json)

    def core_config(self) -> dict[str, Any]:
        return json.loads(self.core_config_json)

    def legacy_flat_config(self) -> dict[str, Any]:
        """Build the exact flat config consumed by current method callables."""

        return {**self.user_config(), **self.core_config()}

    def input_artifact_ids(self) -> tuple[str, ...]:
        return tuple(
            artifact_id
            for binding in self.input_bindings
            for artifact_id in binding.artifact_ids
        )

    def input_artifact_digests(self) -> tuple[str, ...]:
        return tuple(
            digest
            for binding in self.input_bindings
            for digest in binding.artifact_digests
        )


@dataclass(frozen=True, slots=True)
class MethodExecutionServices:
    harness: CoreHarnessService
    parametric_trainer: CoreParametricTrainer | None = None


@dataclass(frozen=True, slots=True)
class MethodExecutionContext:
    job: WorkerClaimedJob
    artifact_root: Path
    envelope: MethodExecutionEnvelope
    services: MethodExecutionServices

    def __post_init__(self) -> None:
        self.validate_job_projection()

    def validate_job_projection(self) -> None:
        if self.job.method != self.envelope.method_id:
            raise ValueError("worker job method does not match execution envelope")
        artifact_ids = tuple(
            artifact.artifact_id for artifact in self.job.input_artifacts
        )
        if artifact_ids != self.envelope.input_artifact_ids():
            raise ValueError(
                "worker job input artifact order does not match execution envelope"
            )
        artifact_digests = tuple(
            worker_input_artifact_digest(artifact)
            for artifact in self.job.input_artifacts
        )
        if artifact_digests != self.envelope.input_artifact_digests():
            raise ValueError(
                "worker job input artifact snapshots do not match execution envelope"
            )


@runtime_checkable
class EvolutionMethodPlugin(Protocol):
    def __call__(
        self,
        context: MethodExecutionContext,
    ) -> list[ArtifactRegisterRequest]: ...


LegacyEvolutionMethod: TypeAlias = Callable[
    [WorkerClaimedJob, Path],
    list[ArtifactRegisterRequest],
]
EvolutionMethodHandle: TypeAlias = LegacyEvolutionMethod | EvolutionMethodPlugin


def invoke_legacy_method(
    method: LegacyEvolutionMethod,
    context: MethodExecutionContext,
) -> list[ArtifactRegisterRequest]:
    """Invoke an existing algorithm through one behavior-preserving adapter."""

    context.validate_job_projection()
    payload = context.job.model_dump(mode="python")
    payload["config"] = context.envelope.legacy_flat_config()
    projected_job = WorkerClaimedJob.model_validate(payload)
    return method(projected_job, context.artifact_root)


def build_execution_envelope(
    *,
    plan_id: str,
    plan_digest: str,
    registry_snapshot_digest: str,
    target_id: str,
    method_id: str,
    method_identity_digest: str,
    user_config: dict[str, Any],
    core_config: dict[str, Any],
    input_bindings: tuple[ResolvedMethodInputBinding, ...],
    output_artifact_types: tuple[str, ...],
) -> MethodExecutionEnvelope:
    user_config_json = canonical_json(user_config)
    core_config_json = canonical_json(core_config)
    return MethodExecutionEnvelope(
        plan_id=plan_id,
        plan_digest=plan_digest,
        registry_snapshot_digest=registry_snapshot_digest,
        target_id=target_id,
        method_id=method_id,
        method_identity_digest=method_identity_digest,
        user_config_json=user_config_json,
        user_config_digest=canonical_digest(user_config),
        core_config_json=core_config_json,
        core_config_digest=canonical_digest(core_config),
        input_bindings=input_bindings,
        output_artifact_types=output_artifact_types,
    )


__all__ = [
    "CORE_CONFIG_RESERVED_KEYS",
    "CoreHarnessService",
    "EvolutionMethodHandle",
    "EvolutionMethodPlugin",
    "HarnessInferenceRequest",
    "HarnessInferenceResponse",
    "InputBindingSource",
    "LegacyEvolutionMethod",
    "MAX_HARNESS_OUTPUT_TOKENS",
    "MethodExecutionContext",
    "MethodExecutionEnvelope",
    "MethodExecutionServices",
    "MethodInputBinding",
    "MethodInputResolution",
    "ResolvedMethodInputBinding",
    "build_execution_envelope",
    "invoke_legacy_method",
    "resolve_method_inputs",
    "validate_user_config_schema_ownership",
    "worker_input_artifact_digest",
]
