"""Harness factory with built-in name map and import_path support."""

from __future__ import annotations

from openevo.harness.base import BaseHarness
from openevo.harness.models import AgentSpec
from openevo._imports import import_subclass


def _builtin_harness_map() -> dict[str, type[BaseHarness]]:
    """Lazy import to avoid circular imports at module level."""
    from openevo.harness.presets.claude_code import ClaudeCodeHarness
    from openevo.harness.presets.codex import CodexHarness
    from openevo.harness.presets.gemini_cli import GeminiCliHarness
    from openevo.harness.presets.hermes import HermesHarness
    from openevo.harness.presets.openclaw import OpenClawHarness
    from openevo.harness.presets.openhands_sdk import OpenHandsSdkHarness
    from openevo.harness.presets.opencode import OpenCodeHarness
    from openevo.harness.presets.pi import PiHarness
    from openevo.harness.presets.qwen_code import QwenCodeHarness
    from openevo.harness.presets.shell import ShellHarness

    return {
        "claude_code": ClaudeCodeHarness,
        "codex": CodexHarness,
        "gemini_cli": GeminiCliHarness,
        "hermes": HermesHarness,
        "openclaw": OpenClawHarness,
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
