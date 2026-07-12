"""Narrow compatibility resolution for existing algorithm-owned method requests."""

from __future__ import annotations

from collections.abc import Iterable


def resolve_agent_system_method(
    requested_method: str,
    prior_dataset_artifact_ids: Iterable[str],
) -> str:
    """Preserve the existing agent-system ``auto`` history-selection behavior."""

    if not requested_method.strip():
        raise ValueError("requested agent-system method must not be empty")
    if requested_method != "auto":
        return requested_method
    prior_ids = tuple(prior_dataset_artifact_ids)
    if any(not artifact_id.strip() for artifact_id in prior_ids):
        raise ValueError("prior dataset artifact IDs must not be empty")
    return (
        "agent_system_history_reflector"
        if prior_ids
        else "agent_system_reflector"
    )


__all__ = ["resolve_agent_system_method"]
