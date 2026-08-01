from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from openevo.harness.models import AgentSpec
from openevo.harness.presets.codex import CodexHarness
from openevo.runtime.models import ExecInput


_CODEX_EXEC_MARKER = "codex exec "
_HARBOR_DOCKER_BYPASS = "--dangerously-bypass-approvals-and-sandbox "


@dataclass(frozen=True)
class CoreCodexRun:
    harness: CodexHarness
    steps: tuple[ExecInput, ...]


def build_core_codex_run(
    *,
    instruction: str,
    model: str,
    gateway_url: str,
    reasoning_effort: str = "high",
) -> CoreCodexRun:
    """Build one local-inference run through OpenEvo's canonical Codex harness."""

    gateway_host = urlparse(gateway_url).hostname
    if not gateway_host:
        raise ValueError("gateway_url must include a host")
    no_proxy = ",".join(dict.fromkeys(("127.0.0.1", "localhost", gateway_host)))
    harness = CodexHarness(
        AgentSpec(
            harness="codex",
            model_name=model,
            settings={
                "auth_mode": "proxy",
                "capture_mode": "proxy",
                "native_memory_policy": "preserve",
                "reasoning_effort": reasoning_effort,
            },
            env={
                "CODEX_HOME": "/openevo/session/.codex",
                "NO_PROXY": no_proxy,
                "OPENAI_API_KEY": "openevo-local-inference",
                "OPENAI_BASE_URL": gateway_url.rstrip("/"),
                "no_proxy": no_proxy,
            },
        )
    )
    steps = tuple(
        _allow_isolated_harbor_container_execution(step)
        for step in harness.run_steps(instruction)
    )
    return CoreCodexRun(harness=harness, steps=steps)


def _allow_isolated_harbor_container_execution(step: ExecInput) -> ExecInput:
    """Add the benchmark-only Codex bypass to the generated run step."""

    if _CODEX_EXEC_MARKER not in step.command:
        return step
    if _HARBOR_DOCKER_BYPASS in step.command:
        raise ValueError("Core Codex command unexpectedly already contains a sandbox bypass")
    command = step.command.replace(
        _CODEX_EXEC_MARKER,
        _CODEX_EXEC_MARKER + _HARBOR_DOCKER_BYPASS,
        1,
    )
    return step.model_copy(update={"command": command})


__all__ = ["CoreCodexRun", "build_core_codex_run"]
