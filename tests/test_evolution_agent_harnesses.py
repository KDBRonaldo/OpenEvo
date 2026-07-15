from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from openevo.harness.models import AgentSpec, MCPServerSpec
from openevo.harness.presets.claude_code import ClaudeCodeHarness
from openevo.harness.presets.codex import CodexHarness
from openevo.harness.presets.openhands_sdk import OpenHandsSdkHarness
from openevo.runtime.base import BaseRuntime
from openevo.runtime.managed import MANAGED_CODEX_BINARY
from openevo.runtime.models import ExecResult, RuntimeSpec

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

    async def download_file(
        self,
        remote_path: str,
        local_path: str,
    ) -> None:
        del remote_path, local_path

    async def download_dir(
        self,
        remote_path: str,
        local_path: str,
    ) -> None:
        del remote_path, local_path


def _codex_config_toml(commands: list[str]) -> str:
    marker = "config.toml << 'OPENEVO_CFG'\n"
    for command in commands:
        if marker in command:
            return command.split(marker, 1)[1].rsplit("\nOPENEVO_CFG", 1)[0]
    raise AssertionError("Codex config.toml write command not found")


@pytest.mark.asyncio
async def test_codex_setup_installs_static_and_evolution_skills(tmp_path):
    harness = CodexHarness(
        AgentSpec(
            harness="codex",
            skills_path="/openevo/static-skills",
            env={"OPENEVO_SKILLS_DIR": "/openevo/session/evolution/skills"},
        )
    )
    runtime = RecordingRuntime(tmp_path)

    await harness.setup(runtime)

    copy_commands = [command for command in runtime.commands if ".agents/skills" in command]
    joined = "\n".join(copy_commands)
    assert "cp -r /openevo/static-skills/* $HOME/.agents/skills/" in joined
    assert "cp -r /openevo/session/evolution/skills/* $HOME/.agents/skills/" in joined
    assert "|| true" not in joined


@pytest.mark.asyncio
async def test_codex_setup_fails_when_skill_installation_fails(tmp_path):
    class FailingSkillRuntime(RecordingRuntime):
        async def exec(
            self,
            command: str,
            *,
            cwd: str | None = None,
            env: dict[str, str] | None = None,
            timeout_sec: float | None = None,
        ) -> ExecResult:
            del cwd, env, timeout_sec
            self.commands.append(command)
            return ExecResult(
                return_code=1 if ".agents/skills" in command else 0,
            )

    harness = CodexHarness(
        AgentSpec(
            harness="codex",
            env={"OPENEVO_SKILLS_DIR": "/openevo/session/evolution/skills"},
        )
    )
    runtime = FailingSkillRuntime(tmp_path)

    with pytest.raises(RuntimeError, match="Codex skill installation failed"):
        await harness.setup(runtime)


@pytest.mark.asyncio
async def test_codex_setup_overwrites_config_without_mcp_servers(tmp_path):
    harness = CodexHarness(
        AgentSpec(
            harness="codex",
            env={"CODEX_HOME": "/openevo/session/preauthenticated-codex"},
        )
    )
    runtime = RecordingRuntime(tmp_path)

    await harness.setup(runtime)

    joined = "\n".join(runtime.commands)
    assert "cat > /openevo/session/preauthenticated-codex/config.toml" in joined
    assert '[mcp_servers."' not in joined


@pytest.mark.asyncio
async def test_codex_setup_preserves_native_memory_by_default(tmp_path):
    harness = CodexHarness(
        AgentSpec(
            harness="codex",
            env={"CODEX_HOME": "/openevo/session/preauthenticated-codex"},
        )
    )
    runtime = RecordingRuntime(tmp_path)

    await harness.setup(runtime)

    joined = "\n".join(runtime.commands)
    assert "/openevo/session/preauthenticated-codex/config.toml" in joined
    assert "/memories" not in joined
    assert "memories_" not in joined
    assert "auth.json" not in joined


@pytest.mark.asyncio
async def test_codex_setup_clears_native_memory_when_requested(tmp_path):
    harness = CodexHarness(
        AgentSpec(
            harness="codex",
            settings={"native_memory_policy": "clear"},
            env={"CODEX_HOME": "/openevo/session/preauthenticated-codex"},
        )
    )
    runtime = RecordingRuntime(tmp_path)

    await harness.setup(runtime)

    cleanup_commands = [
        command
        for command in runtime.commands
        if "/memories" in command or "memories_" in command
    ]
    assert len(cleanup_commands) == 1
    cleanup = cleanup_commands[0]
    assert cleanup.startswith("rm -rf -- ")
    assert "/openevo/session/preauthenticated-codex/memories" in cleanup
    assert "/openevo/session/preauthenticated-codex/memories_*.sqlite*" in cleanup
    assert "auth.json" not in cleanup
    assert "state_" not in cleanup
    assert "logs_" not in cleanup
    assert "history.jsonl" not in cleanup
    assert "session_index.jsonl" not in cleanup


@pytest.mark.asyncio
async def test_codex_setup_rejects_unknown_native_memory_policy(tmp_path):
    harness = CodexHarness(
        AgentSpec(
            harness="codex",
            settings={"native_memory_policy": "wipe"},
        )
    )
    runtime = RecordingRuntime(tmp_path)

    with pytest.raises(ValueError, match="native_memory_policy"):
        await harness.setup(runtime)


@pytest.mark.asyncio
async def test_codex_setup_rejects_native_memory_policy_with_whitespace(tmp_path):
    harness = CodexHarness(
        AgentSpec(
            harness="codex",
            settings={"native_memory_policy": " clear "},
        )
    )
    runtime = RecordingRuntime(tmp_path)

    with pytest.raises(ValueError, match="native_memory_policy"):
        await harness.setup(runtime)


@pytest.mark.asyncio
async def test_codex_setup_uses_configured_codex_home_for_mcp_servers(tmp_path):
    harness = CodexHarness(
        AgentSpec(
            harness="codex",
            env={"CODEX_HOME": "/openevo/session/preauthenticated-codex"},
            mcp_servers=[
                MCPServerSpec(
                    name="repo-tools",
                    transport="stdio",
                    command="python",
                    args=["-m", "repo_tools"],
                )
            ],
        )
    )
    runtime = RecordingRuntime(tmp_path)

    await harness.setup(runtime)

    joined = "\n".join(runtime.commands)
    assert "mkdir -p /openevo/session/preauthenticated-codex" in joined
    assert "cat > /openevo/session/preauthenticated-codex/config.toml" in joined
    assert "cat > /openevo/session/.codex/config.toml" not in joined
    assert '[mcp_servers."repo-tools"]' in joined


@pytest.mark.asyncio
async def test_codex_setup_escapes_mcp_config_toml_strings(tmp_path):
    server_name = 'repo"tools\\alpha'
    remote_server_name = "remote.server"
    command = 'python"tool'
    args = ["-m", "repo\\tools", 'flag "quoted"']
    url = 'https://example.test/mcp?label="x"&path=repo\\tools'
    harness = CodexHarness(
        AgentSpec(
            harness="codex",
            mcp_servers=[
                MCPServerSpec(
                    name=server_name,
                    transport="stdio",
                    command=command,
                    args=args,
                ),
                MCPServerSpec(
                    name=remote_server_name,
                    transport="streamable-http",
                    url=url,
                ),
            ],
        )
    )
    runtime = RecordingRuntime(tmp_path)

    await harness.setup(runtime)

    config = tomllib.loads(_codex_config_toml(runtime.commands))
    assert config["mcp_servers"][server_name] == {
        "command": command,
        "args": args,
    }
    assert config["mcp_servers"][remote_server_name] == {
        "url": url,
        "type": "streamable-http",
    }


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
            settings={"auth_mode": "subscription", "capture_mode": "transcript"},
            env={"CODEX_HOME": "/openevo/session/preauthenticated-codex"},
        )
    )

    steps = harness.run_steps("Do work.")

    assert len(steps) == 1
    step = steps[0]
    assert "codex exec" in step.command
    assert "--model gpt-5.5" in step.command
    assert f"{MANAGED_CODEX_BINARY} exec " in step.command
    assert "auth.json" not in step.command
    assert step.command.startswith("env -u")
    for key in SUBSCRIPTION_PROXY_ENV_VARS:
        assert f"-u {key}" in step.command
    assert "harness_proxy" not in step.command
    assert "model_providers.harness_proxy" not in step.command
    assert step.env is not None
    assert step.env["CODEX_HOME"] == "/openevo/credentials/codex"


def test_codex_subscription_auth_mode_requires_transcript_capture_option():
    harness = CodexHarness(
        AgentSpec(
            harness="codex",
            settings={"auth_mode": "subscription"},
        )
    )

    with pytest.raises(ValueError, match="capture_mode"):
        harness.run_steps("Do work.")


@pytest.mark.parametrize("capture_mode", ["transcript", "agent_transcript", "pure_text"])
def test_codex_subscription_auth_mode_accepts_shared_transcript_aliases(
    capture_mode: str,
) -> None:
    harness = CodexHarness(
        AgentSpec(
            harness="codex",
            settings={"auth_mode": "subscription", "capture_mode": capture_mode},
        )
    )

    step = harness.run_steps("Do work.")[0]

    assert step.command.startswith("env -u")
    assert harness.settings["capture_mode"] == "transcript"


@pytest.mark.parametrize("capture_mode", ["transcript", "agent_transcript", "pure_text"])
def test_codex_proxy_mode_writes_back_canonical_transcript_capture(
    capture_mode: str,
) -> None:
    harness = CodexHarness(
        AgentSpec(
            harness="codex",
            settings={"auth_mode": "proxy", "capture_mode": capture_mode},
        )
    )

    harness.run_steps("Do work.")

    assert harness.settings["capture_mode"] == "transcript"


def test_codex_subscription_auth_mode_rejects_token_capture() -> None:
    harness = CodexHarness(
        AgentSpec(
            harness="codex",
            settings={"auth_mode": "subscription", "capture_mode": "token"},
        )
    )

    with pytest.raises(ValueError, match="capture_mode"):
        harness.run_steps("Do work.")


def test_codex_run_steps_keeps_chatgpt_subscription_auth_mode_alias():
    harness = CodexHarness(
        AgentSpec(
            harness="codex",
            model_name="gpt-5.5",
            settings={"auth_mode": "chatgpt_subscription", "capture_mode": "transcript"},
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
            settings={"auth_mode": "subscription", "capture_mode": "transcript"},
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
    assert step.env["CLAUDE_CONFIG_DIR"] == "/openevo/session/.claude"
    assert step.env["ANTHROPIC_MODEL"] == "opus"


def test_claude_subscription_auth_mode_requires_transcript_capture_option():
    harness = ClaudeCodeHarness(
        AgentSpec(
            harness="claude_code",
            settings={"auth_mode": "subscription"},
        )
    )

    with pytest.raises(ValueError, match="capture_mode"):
        harness.run_steps("Do work.")


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
            skills_path="/openevo/static-skills",
            env={"OPENEVO_SKILLS_DIR": "/openevo/session/evolution/skills"},
        )
    )

    step = harness.run_steps("Do work.")[0]

    assert step.env is not None
    assert step.env["SKILL_PATHS"] == ("/openevo/session/evolution/skills:/openevo/static-skills")


def test_openhands_run_steps_uses_evolution_skill_path_without_static_path():
    harness = OpenHandsSdkHarness(
        AgentSpec(
            harness="openhands_sdk",
            env={"OPENEVO_SKILLS_DIR": "/openevo/session/evolution/skills"},
        )
    )

    step = harness.run_steps("Do work.")[0]

    assert step.env is not None
    assert step.env["SKILL_PATHS"] == "/openevo/session/evolution/skills"
