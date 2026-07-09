"""Capability metadata exposed by OpenEvo Core."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ExecutionMode = Literal["codex_subscription_transcript", "self-deployed"]
ArtifactType = Literal["text_memory", "skill_bundle", "agent_system", "parametric_memory"]
StabilityLevel = Literal["stable", "experimental"]


class MethodVisibility(StrEnum):
    ORDINARY_USER = "ordinary_user"
    DEV_KIT = "dev_kit"
    INTERNAL = "internal"


class _CoreModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class ExecutionModeCapability(_CoreModel):
    mode: ExecutionMode
    display_name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    visible_in_desktop: bool = False


class ArtifactTarget(_CoreModel):
    artifact_type: ArtifactType
    display_name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    visible_in_desktop: bool = False
    stability_level: StabilityLevel = "stable"


class EvolutionMethodCapability(_CoreModel):
    method_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    artifact_type: ArtifactType
    visibility: MethodVisibility
    visible_in_desktop: bool = False
    input_requirements: tuple[str, ...] = ()
    supported_execution_modes: tuple[ExecutionMode, ...]
    default_config: dict[str, Any] = Field(default_factory=dict)
    config_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    stability_level: StabilityLevel = "stable"


class CoreCapabilities(_CoreModel):
    execution_modes: tuple[ExecutionModeCapability, ...]
    artifact_targets: tuple[ArtifactTarget, ...]
    evolution_methods: tuple[EvolutionMethodCapability, ...]


def method_metadata_by_id() -> dict[str, EvolutionMethodCapability]:
    from openevo.evolution.methods import METHOD_METADATA

    metadata_by_id: dict[str, EvolutionMethodCapability] = {}
    for method_id, metadata in METHOD_METADATA.items():
        payload = dict(metadata)
        if payload.get("method_id") != method_id:
            raise ValueError(f"Method metadata ID mismatch for {method_id}")
        metadata_by_id[method_id] = EvolutionMethodCapability(**payload)
    return metadata_by_id


def build_core_capabilities() -> CoreCapabilities:
    return CoreCapabilities(
        execution_modes=(
            ExecutionModeCapability(
                mode="codex_subscription_transcript",
                display_name="Codex Subscription Transcript",
                description=(
                    "Run a subscription-authenticated Codex harness with transcript capture "
                    "for pure-text evolution."
                ),
                visible_in_desktop=True,
            ),
            ExecutionModeCapability(
                mode="self-deployed",
                display_name="Self-Deployed",
                description=(
                    "Run against self-deployed model serving through Polar proxy or compatible "
                    "infrastructure."
                ),
                visible_in_desktop=True,
            ),
        ),
        artifact_targets=(
            ArtifactTarget(
                artifact_type="text_memory",
                display_name="Text Memory",
                description="Natural-language long-term memory injected into later sessions.",
                visible_in_desktop=True,
            ),
            ArtifactTarget(
                artifact_type="skill_bundle",
                display_name="Skill Bundle",
                description="Agent skill bundle staged for harness-specific loading.",
                visible_in_desktop=True,
            ),
            ArtifactTarget(
                artifact_type="agent_system",
                display_name="Agent System",
                description="Evolved agent instructions or harness instruction files.",
                visible_in_desktop=True,
            ),
            ArtifactTarget(
                artifact_type="parametric_memory",
                display_name="Parametric Memory",
                description="Adapter or LoRA-style parameterized long-term memory.",
                visible_in_desktop=False,
                stability_level="experimental",
            ),
        ),
        evolution_methods=tuple(method_metadata_by_id().values()),
    )
