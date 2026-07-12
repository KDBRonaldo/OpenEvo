"""Behavior-compatible default target selections for Core project configs."""

from __future__ import annotations

from openevo.evolution.framework import ProjectEvolutionTargetSelection


def default_project_evolution_targets() -> dict[
    str,
    ProjectEvolutionTargetSelection,
]:
    """Return fresh defaults without consulting profile-dependent registry defaults."""

    return {
        "text_memory": ProjectEvolutionTargetSelection(
            enabled=True,
            method="text_memory_reflector",
        ),
        "parametric_memory": ProjectEvolutionTargetSelection(
            enabled=False,
            method="parametric_memory_register",
        ),
        "skill_bundle": ProjectEvolutionTargetSelection(
            enabled=True,
            method="skill_bundle_reflector",
        ),
        "agent_system": ProjectEvolutionTargetSelection(
            enabled=True,
            method="auto",
            config={"target_path": "AGENTS.md"},
        ),
    }


__all__ = ["default_project_evolution_targets"]
