"""The single mapping from product release modes to generic framework axes."""

from __future__ import annotations

from enum import StrEnum

from .contracts import EvolutionExecutionProfile


class ReleaseExecutionMode(StrEnum):
    CODEX_SUBSCRIPTION_TRANSCRIPT = "codex_subscription_transcript"
    SELF_DEPLOYED = "self-deployed"


def execution_profile_for_release_mode(
    mode: ReleaseExecutionMode | str,
    *,
    harness_capabilities: tuple[str, ...] = (),
    runtime_capabilities: tuple[str, ...] = (),
) -> EvolutionExecutionProfile:
    try:
        release_mode = ReleaseExecutionMode(mode)
    except ValueError as exc:
        raise ValueError(f"unsupported OpenEvo release execution mode: {mode!r}") from exc
    return EvolutionExecutionProfile(
        execution_mode=(
            "subscription"
            if release_mode is ReleaseExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT
            else "self_deployed"
        ),
        capture_mode="transcript",
        harness_id="codex",
        harness_capabilities=harness_capabilities,
        runtime_capabilities=runtime_capabilities,
    )


__all__ = ["ReleaseExecutionMode", "execution_profile_for_release_mode"]
