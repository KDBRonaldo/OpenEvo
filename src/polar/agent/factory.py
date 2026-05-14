"""Harness factory with built-in name map and import_path support."""

from __future__ import annotations

from polar.agent.base import BaseHarness
from polar.agent.models import AgentSpec
from polar._imports import import_subclass


def _builtin_harness_map() -> dict[str, type[BaseHarness]]:
    """Lazy import to avoid circular imports at module level."""
    from polar.agent.harnesses.claude_code import ClaudeCodeHarness
    from polar.agent.harnesses.codex import CodexHarness
    from polar.agent.harnesses.gemini_cli import GeminiCliHarness
    from polar.agent.harnesses.openhands_sdk import OpenHandsSdkHarness
    from polar.agent.harnesses.opencode import OpenCodeHarness
    from polar.agent.harnesses.pi import PiHarness
    from polar.agent.harnesses.qwen_code import QwenCodeHarness
    from polar.agent.harnesses.shell import ShellHarness

    return {
        "claude_code": ClaudeCodeHarness,
        "codex": CodexHarness,
        "gemini_cli": GeminiCliHarness,
        "openhands_sdk": OpenHandsSdkHarness,
        "opencode": OpenCodeHarness,
        "pi": PiHarness,
        "qwen_code": QwenCodeHarness,
        "shell": ShellHarness,
    }


def create_harness(agent_spec: AgentSpec) -> BaseHarness:
    """Resolve and instantiate a harness from an AgentSpec."""
    if agent_spec.import_path is not None:
        cls = _import_harness_class(agent_spec.import_path)
        return cls(agent_spec)

    if agent_spec.harness is not None:
        harness_map = _builtin_harness_map()
        cls = harness_map.get(agent_spec.harness)
        if cls is None:
            raise ValueError(f"Unknown harness: {agent_spec.harness!r}")
        return cls(agent_spec)

    raise ValueError("AgentSpec must specify harness or import_path")


def _import_harness_class(import_path: str) -> type[BaseHarness]:
    return import_subclass(import_path, BaseHarness, kind="harness import path")
