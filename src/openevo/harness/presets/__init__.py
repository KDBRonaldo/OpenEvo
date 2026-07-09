"""Preset harnesses — ready-made launchers for popular agents.

These are *conveniences*, not integrations. OpenEvo runs any agent unmodified:
a harness only starts the agent process and lets its LLM calls flow through
the gateway proxy (which serves the model and captures the trajectory). Each
preset here is a thin ``BaseHarness`` that writes the agent's config and emits
its run command — typically a few dozen lines.

To run an agent that isn't listed here, you do **not** add code to OpenEvo:
- use the ``shell`` preset to wrap any command, or
- point ``agent.import_path`` at your own ``BaseHarness`` subclass.

See ``openevo/harness/README.md`` for the contract and a "bring your own" guide.
"""

from openevo.harness.presets.claude_code import ClaudeCodeHarness
from openevo.harness.presets.codex import CodexHarness
from openevo.harness.presets.gemini_cli import GeminiCliHarness
from openevo.harness.presets.hermes import HermesHarness
from openevo.harness.presets.openclaw import OpenClawHarness
from openevo.harness.presets.opencode import OpenCodeHarness
from openevo.harness.presets.openhands_sdk import OpenHandsSdkHarness
from openevo.harness.presets.pi import PiHarness
from openevo.harness.presets.qwen_code import QwenCodeHarness
from openevo.harness.presets.shell import ShellHarness

__all__ = [
    "ClaudeCodeHarness",
    "CodexHarness",
    "GeminiCliHarness",
    "HermesHarness",
    "OpenClawHarness",
    "OpenCodeHarness",
    "OpenHandsSdkHarness",
    "PiHarness",
    "QwenCodeHarness",
    "ShellHarness",
]
