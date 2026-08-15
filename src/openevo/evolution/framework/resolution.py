"""Core-owned resolution for algorithm method selections."""

from __future__ import annotations

from collections.abc import Sequence


def resolve_agent_system_method(
    requested_method: str,
    prior_dataset_artifact_ids: Sequence[str],
) -> str:
    """Preserve the existing agent-system ``auto`` history-selection behavior."""

    if not isinstance(requested_method, str) or not requested_method.strip():
        raise ValueError("requested agent-system method must not be empty")
    if isinstance(prior_dataset_artifact_ids, str) or not isinstance(
        prior_dataset_artifact_ids,
        Sequence,
    ):
        raise TypeError("prior dataset artifact IDs must be a sequence of strings")
    prior_ids: list[str] = []
    for artifact_id in prior_dataset_artifact_ids:
        if not isinstance(artifact_id, str):
            raise TypeError("prior dataset artifact IDs must contain only strings")
        if not artifact_id.strip():
            raise ValueError("prior dataset artifact IDs must not be empty")
        prior_ids.append(artifact_id)
    if requested_method != "auto":
        return requested_method
    return (
        "agent_system_history_reflector"
        if prior_ids
        else "agent_system_reflector"
    )


def resolve_evolution_method(
    *,
    target_id: str,
    requested_method: str,
    prior_dataset_artifact_ids: Sequence[str],
) -> str:
    """Resolve a selection without exposing algorithm policy to a product client.

    Desktop and Daemon call this target-neutral boundary. Core remains responsible
    for mapping resolver values such as ``auto`` to concrete registered methods.
    """

    if not isinstance(target_id, str) or not target_id.strip():
        raise ValueError("evolution target ID must not be empty")
    if target_id == "agent_system":
        return resolve_agent_system_method(
            requested_method,
            prior_dataset_artifact_ids,
        )
    if not isinstance(requested_method, str) or not requested_method.strip():
        raise ValueError("requested evolution method must not be empty")
    return requested_method


__all__ = ["resolve_agent_system_method", "resolve_evolution_method"]
