from __future__ import annotations

from pathlib import Path

import pytest

from polar.agent.models import AgentSpec
from polar.agent.presets.claude_code import ClaudeCodeHarness
from polar.agent.presets.codex import CodexHarness
from polar.agent.presets.openhands_sdk import OpenHandsSdkHarness
from polar.runtime.base import BaseRuntime
from polar.runtime.models import ExecResult, RuntimeSpec

SUBSCRIPTION_PROXY_ENV_VARS = (
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_URL",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_GEMINI_BASE_URL",
)


class RecordingRuntime(BaseRuntime):
    def __init__(self, session_dir: Path) -> None:
        super().__init__(
            RuntimeSpec(image="runtime:latest"),
            session_id="session_1",
            session_dir=session_dir,
        )
        self.commands: list[str] = []

    @property
    def runtime_id(self) -> str:
        return "recording-runtime"

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def exec(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: float | None = None,
    ) -> ExecResult:
        self.commands.append(command)
        return ExecResult(return_code=0)

    async def upload_file(self, local_path: str, remote_path: str) -> None:
        return None

    async def upload_dir(self, local_path: str, remote_path: str) -> None:
        return None

    async def download_file(self, remote_path: str, local_path: str) -> None:
        return None

    async def download_dir(self, remote_path: str, local_path: str) -> None:
        return None


@pytest.mark.asyncio
async def test_codex_setup_installs_static_and_evolution_skills(tmp_path):
    harness = CodexHarness(
        AgentSpec(
            harness="codex",
            skills_path="/polar/static-skills",
            env={"POLAR_SKILLS_DIR": "/polar/session/evolution/skills"},
        )
    )
    runtime = RecordingRuntime(tmp_path)

    await harness.setup(runtime)

    copy_commands = [command for command in runtime.commands if ".agents/skills" in command]
    joined = "\n".join(copy_commands)
    assert "cp -r /polar/static-skills/* $HOME/.agents/skills/" in joined
    assert "cp -r /polar/session/evolution/skills/* $HOME/.agents/skills/" in joined


def test_codex_run_steps_defaults_to_proxy_auth_mode():
    harness = CodexHarness(AgentSpec(harness="codex", model_name="gpt-5.5"))

    steps = harness.run_steps("Do work.")

    assert len(steps) == 2
    assert "auth.json" in steps[0].command
    assert "OPENAI_API_KEY" in steps[0].command
    assert 'model_provider="harness_proxy"' in steps[1].command
    assert "model_providers.harness_proxy.base_url" in steps[1].command
    assert "--model gpt-5.5" in steps[1].command
    assert not steps[1].command.startswith("env -u")


def test_codex_run_steps_subscription_auth_mode_uses_existing_login_state():
    harness = CodexHarness(
        AgentSpec(
            harness="codex",
            model_name="gpt-5.5",
            settings={"auth_mode": "subscription"},
            env={"CODEX_HOME": "/polar/session/preauthenticated-codex"},
        )
    )

    steps = harness.run_steps("Do work.")

    assert len(steps) == 1
    step = steps[0]
    assert "codex exec" in step.command
    assert "--model gpt-5.5" in step.command
    assert "auth.json" not in step.command
    assert step.command.startswith("env -u")
    for key in SUBSCRIPTION_PROXY_ENV_VARS:
        assert f"-u {key}" in step.command
    assert "harness_proxy" not in step.command
    assert "model_providers.harness_proxy" not in step.command
    assert step.env is not None
    assert step.env["CODEX_HOME"] == "/polar/session/preauthenticated-codex"


def test_codex_run_steps_keeps_chatgpt_subscription_auth_mode_alias():
    harness = CodexHarness(
        AgentSpec(
            harness="codex",
            model_name="gpt-5.5",
            settings={"auth_mode": "chatgpt_subscription"},
        )
    )

    step = harness.run_steps("Do work.")[0]

    assert step.command.startswith("env -u")
    assert "codex exec" in step.command


def test_codex_run_steps_rejects_unknown_auth_mode():
    harness = CodexHarness(
        AgentSpec(
            harness="codex",
            settings={"auth_mode": "api_key"},
        )
    )

    with pytest.raises(ValueError, match="auth_mode"):
        harness.run_steps("Do work.")


def test_claude_run_steps_subscription_auth_mode_uses_existing_login_state():
    harness = ClaudeCodeHarness(
        AgentSpec(
            harness="claude_code",
            model_name="opus",
            settings={"auth_mode": "subscription"},
        )
    )

    steps = harness.run_steps("Do work.")

    assert len(steps) == 1
    step = steps[0]
    assert step.command.startswith("env -u")
    for key in SUBSCRIPTION_PROXY_ENV_VARS:
        assert f"-u {key}" in step.command
    assert "claude --verbose" in step.command
    assert "--model opus" in step.command
    assert step.env is not None
    assert step.env["CLAUDE_CONFIG_DIR"] == "/polar/session/.claude"
    assert step.env["ANTHROPIC_MODEL"] == "opus"


def test_claude_run_steps_defaults_to_proxy_auth_mode():
    harness = ClaudeCodeHarness(
        AgentSpec(
            harness="claude_code",
            model_name="opus",
        )
    )

    step = harness.run_steps("Do work.")[0]

    assert not step.command.startswith("env -u")
    assert step.command.startswith("claude --verbose")


def test_claude_run_steps_rejects_unknown_auth_mode():
    harness = ClaudeCodeHarness(
        AgentSpec(
            harness="claude_code",
            settings={"auth_mode": "api_key"},
        )
    )

    with pytest.raises(ValueError, match="auth_mode"):
        harness.run_steps("Do work.")


def test_openhands_run_steps_passes_static_and_evolution_skill_paths():
    harness = OpenHandsSdkHarness(
        AgentSpec(
            harness="openhands_sdk",
            skills_path="/polar/static-skills",
            env={"POLAR_SKILLS_DIR": "/polar/session/evolution/skills"},
        )
    )

    step = harness.run_steps("Do work.")[0]

    assert step.env is not None
    assert step.env["SKILL_PATHS"] == ("/polar/session/evolution/skills:/polar/static-skills")


def test_openhands_run_steps_uses_evolution_skill_path_without_static_path():
    harness = OpenHandsSdkHarness(
        AgentSpec(
            harness="openhands_sdk",
            env={"POLAR_SKILLS_DIR": "/polar/session/evolution/skills"},
        )
    )

    step = harness.run_steps("Do work.")[0]

    assert step.env is not None
    assert step.env["SKILL_PATHS"] == "/polar/session/evolution/skills"
