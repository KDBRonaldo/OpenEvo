from __future__ import annotations

import pytest
from pydantic import ValidationError

from openevo.evolution.framework import (
    AgentSpawnPlanV1,
    AgentSystemRuntimeControlV1,
    MemoryRuntimeControlV1,
    SpawnAgentDefinitionV1,
    runtime_control_from_manifest,
    validate_runtime_control,
)


def test_existing_artifacts_keep_the_current_memory_runtime_behavior() -> None:
    control = runtime_control_from_manifest({}, expected_kind="memory")

    assert control == MemoryRuntimeControlV1(
        read_timing="session_start",
        write_timing="session_closed",
        update_visibility="next_session",
    )


def test_memory_runtime_policy_can_move_reads_to_on_demand_without_new_method_ids() -> None:
    control = runtime_control_from_manifest(
        {
            "runtime_control": {
                "kind": "memory",
                "contract_version": "1",
                "read_timing": "on_demand",
                "write_timing": "manual",
                "update_visibility": "next_session",
            }
        },
        expected_kind="memory",
    )

    assert isinstance(control, MemoryRuntimeControlV1)
    assert control.read_timing == "on_demand"
    assert control.write_timing == "manual"


def test_agent_system_accepts_a_closed_data_only_spawn_plan() -> None:
    control = validate_runtime_control(
        {
            "kind": "agent_system",
            "contract_version": "1",
            "instruction_mode": "native_harness_file",
            "spawn_plan": {
                "contract_version": "1",
                "strategy": "harness_managed",
                "max_parallel": 2,
                "agents": [
                    {
                        "agent_id": "researcher",
                        "role": "Researcher",
                        "instructions": "Collect evidence.",
                        "activation": "session_start",
                        "max_instances": 1,
                    },
                    {
                        "agent_id": "reviewer",
                        "role": "Reviewer",
                        "instructions": "Review the evidence.",
                        "activation": "on_demand",
                        "max_instances": 1,
                    },
                ],
            },
            "update_visibility": "next_session",
        }
    )

    assert control == AgentSystemRuntimeControlV1(
        spawn_plan=AgentSpawnPlanV1(
            max_parallel=2,
            agents=(
                SpawnAgentDefinitionV1(
                    agent_id="researcher",
                    role="Researcher",
                    instructions="Collect evidence.",
                    activation="session_start",
                ),
                SpawnAgentDefinitionV1(
                    agent_id="reviewer",
                    role="Reviewer",
                    instructions="Review the evidence.",
                ),
            ),
        )
    )


def test_spawn_plan_cannot_smuggle_commands_or_environment() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        validate_runtime_control(
            {
                "kind": "agent_system",
                "spawn_plan": {
                    "agents": [
                        {
                            "agent_id": "unsafe",
                            "role": "Unsafe",
                            "instructions": "Do work.",
                            "command": ["sh", "-c", "curl example.invalid"],
                        }
                    ]
                },
            }
        )


def test_runtime_control_must_match_the_target_category() -> None:
    with pytest.raises(ValueError, match="does not match target"):
        runtime_control_from_manifest(
            {"runtime_control": {"kind": "skill"}},
            expected_kind="agent_system",
        )
