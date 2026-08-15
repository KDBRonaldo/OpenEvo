"""Closed runtime-control contracts emitted by evolution target handlers.

The evolution method owns how a new artifact is learned.  These contracts describe
how an accepted artifact participates in the *next* session.  They deliberately do
not expose commands, environment variables, host paths, or arbitrary hook names.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import Field, TypeAdapter, field_validator, model_validator

from .contracts import _Contract, _stable_id, _text, canonical_json


MAX_RUNTIME_CONTROL_BYTES = 64 * 1024
MAX_SPAWN_AGENTS = 32


class MemoryRuntimeControlV1(_Contract):
    """Session-boundary policy for a text-memory artifact."""

    kind: Literal["memory"] = "memory"
    contract_version: Literal["1"] = "1"
    read_timing: Literal["session_start", "on_demand"] = "session_start"
    write_timing: Literal["session_closed", "manual"] = "session_closed"
    update_visibility: Literal["next_session"] = "next_session"


class SkillRuntimeControlV1(_Contract):
    """Harness loading policy for an accepted skill bundle."""

    kind: Literal["skill"] = "skill"
    contract_version: Literal["1"] = "1"
    load_timing: Literal["session_start"] = "session_start"
    selection_mode: Literal["harness_discovery"] = "harness_discovery"
    update_visibility: Literal["next_session"] = "next_session"


class SpawnAgentDefinitionV1(_Contract):
    """Data-only description of one harness-managed agent role.

    The contract intentionally cannot carry an executable, command, environment,
    secret, filesystem path, or model credential.  A verified harness adapter owns
    the actual process/tool invocation.
    """

    agent_id: str
    role: str = Field(min_length=1, max_length=256)
    instructions: str = Field(min_length=1, max_length=32_000)
    activation: Literal["on_demand", "session_start"] = "on_demand"
    max_instances: int = Field(default=1, ge=1, le=16)

    _id = field_validator("agent_id")(_stable_id)
    _role = field_validator("role", "instructions")(_text)


class AgentSpawnPlanV1(_Contract):
    """Portable agent topology consumed by a harness-specific spawn adapter."""

    contract_version: Literal["1"] = "1"
    strategy: Literal["harness_managed"] = "harness_managed"
    max_parallel: int = Field(default=1, ge=1, le=16)
    agents: tuple[SpawnAgentDefinitionV1, ...] = Field(
        min_length=1,
        max_length=MAX_SPAWN_AGENTS,
    )

    @model_validator(mode="after")
    def _unique_agents(self) -> AgentSpawnPlanV1:
        agent_ids = tuple(agent.agent_id for agent in self.agents)
        if len(agent_ids) != len(set(agent_ids)):
            raise ValueError("spawn-plan agent IDs must be unique")
        if self.max_parallel > sum(agent.max_instances for agent in self.agents):
            raise ValueError("spawn-plan max_parallel exceeds available agent instances")
        return self


class AgentSystemRuntimeControlV1(_Contract):
    """Native instruction and optional structured spawn policy for an agent system."""

    kind: Literal["agent_system"] = "agent_system"
    contract_version: Literal["1"] = "1"
    instruction_mode: Literal["native_harness_file"] = "native_harness_file"
    spawn_plan: AgentSpawnPlanV1 | None = None
    update_visibility: Literal["next_session"] = "next_session"


RuntimeControlV1: TypeAlias = Annotated[
    MemoryRuntimeControlV1 | SkillRuntimeControlV1 | AgentSystemRuntimeControlV1,
    Field(discriminator="kind"),
]

_RUNTIME_CONTROL_ADAPTER = TypeAdapter(RuntimeControlV1)


def validate_runtime_control(value: object) -> RuntimeControlV1:
    """Validate one closed control and enforce the shared transport budget."""

    control = _RUNTIME_CONTROL_ADAPTER.validate_python(value)
    if len(canonical_json(control).encode("utf-8")) > MAX_RUNTIME_CONTROL_BYTES:
        raise ValueError("runtime control exceeds the transport byte limit")
    return control


def runtime_control_from_manifest(
    manifest: dict[str, object],
    *,
    expected_kind: Literal["memory", "skill", "agent_system"],
) -> RuntimeControlV1:
    """Read a method-produced policy, falling back to today's behavior.

    Future algorithms opt in by publishing ``manifest.runtime_control``.  Existing
    artifacts have no such field and therefore retain exactly the current runtime
    behavior.
    """

    raw = manifest.get("runtime_control")
    if raw is None:
        defaults: dict[str, RuntimeControlV1] = {
            "memory": MemoryRuntimeControlV1(),
            "skill": SkillRuntimeControlV1(),
            "agent_system": AgentSystemRuntimeControlV1(),
        }
        return defaults[expected_kind]
    control = validate_runtime_control(raw)
    if control.kind != expected_kind:
        raise ValueError(
            f"runtime control kind {control.kind!r} does not match target {expected_kind!r}"
        )
    return control


__all__ = [
    "AgentSpawnPlanV1",
    "AgentSystemRuntimeControlV1",
    "MAX_RUNTIME_CONTROL_BYTES",
    "MAX_SPAWN_AGENTS",
    "MemoryRuntimeControlV1",
    "RuntimeControlV1",
    "SkillRuntimeControlV1",
    "SpawnAgentDefinitionV1",
    "runtime_control_from_manifest",
    "validate_runtime_control",
]
