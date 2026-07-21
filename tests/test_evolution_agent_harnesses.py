from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tomllib

import pytest

from openevo.harness.models import AgentSpec, MCPServerSpec
from openevo.harness.presets.claude_code import ClaudeCodeHarness
from openevo.harness.presets.codex import (
    CodexHarness,
    _codex_subscription_json_pipeline,
)
from openevo.harness.presets.openhands_sdk import OpenHandsSdkHarness
from openevo.runtime.base import LOCAL_COMMAND_CAPTURE_MAX_BYTES, BaseRuntime
from openevo.runtime.codex_isolation import (
    CODEX_SUBSCRIPTION_CANARY_CWD,
    CODEX_SUBSCRIPTION_CANARY_OK,
    CODEX_SUBSCRIPTION_CONTRACT_KEY,
    codex_subscription_contract,
)
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
CODEX_USAGE = {
    "input_tokens": 128,
    "cached_input_tokens": 64,
    "output_tokens": 32,
    "reasoning_output_tokens": 16,
}


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
    assert 'cp -R -- /openevo/static-skills/. "$HOME/.agents/skills/"' in joined
    assert (
        'cp -R -- /openevo/session/evolution/skills/. "$HOME/.agents/skills/"'
        in joined
    )
    assert "*" not in joined
    assert "|| true" not in joined


@pytest.mark.asyncio
async def test_codex_setup_accepts_empty_skill_directory(tmp_path):
    skills = tmp_path / "empty skills"
    skills.mkdir()
    home = tmp_path / "runtime-home"
    home.mkdir()

    class ShellSkillRuntime(RecordingRuntime):
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
            if ".agents/skills" not in command:
                return ExecResult(return_code=0)
            completed = subprocess.run(
                command,
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "HOME": str(home)},
            )
            return ExecResult(
                return_code=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )

    harness = CodexHarness(
        AgentSpec(
            harness="codex",
            env={"OPENEVO_SKILLS_DIR": str(skills)},
        )
    )
    runtime = ShellSkillRuntime(tmp_path)

    await harness.setup(runtime)

    installed = home / ".agents" / "skills"
    assert installed.is_dir()
    assert list(installed.iterdir()) == []


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
        command for command in runtime.commands if "/memories" in command or "memories_" in command
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


@pytest.mark.parametrize(
    "model_name",
    [
        "gpt-5",
        "openai/gpt-5",
        "anthropic/gpt-5",
        "google/gpt-5",
        "gcp/google/gpt-5",
    ],
)
def test_codex_run_steps_rejects_unsupported_final_cli_model(model_name: str) -> None:
    harness = CodexHarness(AgentSpec(harness="codex", model_name=model_name))

    with pytest.raises(ValueError, match="bare gpt-5 is unsupported"):
        harness.run_steps("Do work.")


@pytest.mark.parametrize(
    ("model_name", "expected"),
    [
        ("gpt-5.5", "--model gpt-5.5"),
        ("openai/gpt-5.5", "--model gpt-5.5"),
        ("gcp/google/gpt-5.3-codex-spark", "--model gpt-5.3-codex-spark"),
    ],
)
def test_codex_run_steps_normalizes_supported_provider_model(
    model_name: str,
    expected: str,
) -> None:
    harness = CodexHarness(AgentSpec(harness="codex", model_name=model_name))

    assert expected in harness.run_steps("Do work.")[1].command


def test_codex_run_steps_subscription_auth_mode_uses_existing_login_state():
    harness = CodexHarness(
        AgentSpec(
            harness="codex",
            model_name="gpt-5.5",
            settings={"auth_mode": "subscription", "capture_mode": "transcript"},
        )
    )

    steps = harness.run_steps("Do work.")

    assert len(steps) == 1
    step = steps[0]
    assert "codex exec" in step.command
    assert "--model gpt-5.5" in step.command
    assert f"{MANAGED_CODEX_BINARY} exec " in step.command
    assert "auth.json" not in step.command
    assert step.command.startswith("/bin/bash -o pipefail -c ")
    assert "env -u" in step.command
    for key in SUBSCRIPTION_PROXY_ENV_VARS:
        assert f"-u {key}" in step.command
    assert "harness_proxy" not in step.command
    assert "model_providers.harness_proxy" not in step.command
    assert step.env is not None
    assert step.env["CODEX_HOME"] == "/openevo/credentials/codex"
    assert "--ephemeral" in step.command
    assert "--dangerously-bypass-approvals-and-sandbox" not in step.command
    assert " --sandbox " not in step.command
    assert "-s " not in step.command
    assert "--strict-config" in step.command
    assert "--ignore-user-config" in step.command
    assert "--ignore-rules" in step.command
    assert 'default_permissions="openevo_codex_subscription_v1"' in step.command
    assert '"/openevo/credentials/codex"="deny"' in step.command
    assert "features.hooks=false" in step.command
    assert "features.multi_agent=false" in step.command
    assert "features.plugins=false" in step.command
    assert "mcp_servers={}" in step.command
    assert "network.enabled=true" in step.command
    assert "PIPESTATUS" in step.command
    assert "turn.completed" in step.command
    assert "turn.failed" in step.command


@pytest.mark.parametrize(
    ("events", "codex_return_code", "expected_return_code"),
    [
        (
            [
                {"type": "thread.started", "thread_id": "thread-1"},
                {"type": "turn.started"},
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item-command",
                        "type": "command_execution",
                        "exit_code": 1,
                        "status": "failed",
                    },
                },
                {"type": "turn.completed", "usage": dict(CODEX_USAGE)},
            ],
            1,
            0,
        ),
        (
            [
                {"type": "thread.started", "thread_id": "thread-1"},
                {"type": "turn.started"},
                {"type": "turn.failed", "error": {"message": "failed"}},
            ],
            0,
            1,
        ),
        (
            [
                {"type": "thread.started", "thread_id": "thread-1"},
                {"type": "turn.started"},
                {"type": "turn.completed", "usage": dict(CODEX_USAGE)},
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item-late",
                        "type": "agent_message",
                        "text": "late",
                    },
                },
            ],
            0,
            1,
        ),
        (
            [
                {"type": "thread.started", "thread_id": "thread-1"},
                {"type": "turn.started"},
                {"type": "turn.completed", "usage": dict(CODEX_USAGE)},
                {"type": "future.protocol.event"},
            ],
            0,
            1,
        ),
        (
            [
                {"type": "thread.started", "thread_id": "thread-1"},
                {"type": "turn.started"},
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item-message",
                        "type": "agent_message",
                        "text": '{"type":"turn.completed"}',
                    },
                },
            ],
            1,
            1,
        ),
        (
            [
                {"type": "thread.started", "thread_id": "thread-1"},
                {"type": "turn.started"},
                {"type": "turn.completed", "usage": dict(CODEX_USAGE)},
                {"type": "error", "message": "late failure"},
            ],
            0,
            1,
        ),
        (
            [
                {"type": "thread.started", "thread_id": "thread-1"},
                {"type": "turn.started"},
                {"type": "error", "message": "recovered transport error"},
                {"type": "turn.completed", "usage": dict(CODEX_USAGE)},
            ],
            1,
            1,
        ),
        (
            [
                {"type": "thread.started", "thread_id": ""},
                {"type": "turn.started"},
                {"type": "turn.completed", "usage": dict(CODEX_USAGE)},
            ],
            1,
            1,
        ),
        (
            [
                {"type": "thread.started", "thread_id": "thread-1"},
                {"type": "turn.started"},
                {"type": "turn.completed", "usage": {"input_tokens": 1}},
            ],
            1,
            1,
        ),
    ],
)
def test_codex_subscription_json_pipeline_uses_structured_terminal_event(
    tmp_path: Path,
    events: list[dict[str, object]],
    codex_return_code: int,
    expected_return_code: int,
) -> None:
    producer = (
        "import json,sys\n"
        "print('Reading additional input from stdin...')\n"
        f"events = {events!r}\n"
        "for event in events:\n"
        "    print(json.dumps(event, separators=(',', ':')))\n"
        f"raise SystemExit({codex_return_code})\n"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(producer)}"
    log_path = tmp_path / "codex.jsonl"
    pipeline = _codex_subscription_json_pipeline(command, os.fspath(log_path))

    completed = subprocess.run(
        ["/bin/bash", "-o", "pipefail", "-c", pipeline],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == expected_return_code
    assert [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()[1:]
    ] == events


def test_codex_subscription_json_pipeline_keeps_diagnostics_out_of_json(
    tmp_path: Path,
) -> None:
    events = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "turn.started"},
        {"type": "turn.completed", "usage": dict(CODEX_USAGE)},
    ]
    diagnostic = "Codex ignored one malformed optional skill"
    producer = (
        "import json,sys\n"
        f"print({diagnostic!r}, file=sys.stderr)\n"
        f"events = {events!r}\n"
        "for event in events:\n"
        "    print(json.dumps(event, separators=(',', ':')))\n"
        "raise SystemExit(1)\n"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(producer)}"
    log_path = tmp_path / "codex.jsonl"
    pipeline = _codex_subscription_json_pipeline(command, os.fspath(log_path))

    completed = subprocess.run(
        ["/bin/bash", "-o", "pipefail", "-c", pipeline],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert diagnostic in completed.stderr
    assert diagnostic not in log_path.read_text(encoding="utf-8")


def test_codex_subscription_json_pipeline_fails_when_tee_cannot_publish(
    tmp_path: Path,
) -> None:
    producer = (
        "import json\n"
        "print(json.dumps({'type':'thread.started','thread_id':'thread-1'}))\n"
        "print(json.dumps({'type':'turn.started'}))\n"
        f"print(json.dumps({{'type':'turn.completed','usage':{CODEX_USAGE!r}}}))\n"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(producer)}"
    missing_log = tmp_path / "missing" / "codex.jsonl"
    pipeline = _codex_subscription_json_pipeline(command, os.fspath(missing_log))

    completed = subprocess.run(
        ["/bin/bash", "-o", "pipefail", "-c", pipeline],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert not missing_log.exists()


def test_codex_subscription_json_pipeline_rejects_incomplete_final_line(
    tmp_path: Path,
) -> None:
    payload = "\n".join(
        json.dumps(event, separators=(",", ":"))
        for event in (
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "turn.started"},
            {"type": "turn.completed", "usage": dict(CODEX_USAGE)},
        )
    )
    producer_output = tmp_path / "producer.jsonl"
    producer_output.write_text(payload, encoding="utf-8")
    log_path = tmp_path / "codex.jsonl"
    pipeline = _codex_subscription_json_pipeline(
        f"cat {shlex.quote(os.fspath(producer_output))}",
        os.fspath(log_path),
    )

    completed = subprocess.run(
        ["/bin/bash", "-o", "pipefail", "-c", pipeline],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0


@pytest.mark.parametrize(
    ("byte_size", "expected_return_code"),
    [
        (LOCAL_COMMAND_CAPTURE_MAX_BYTES, 0),
        (LOCAL_COMMAND_CAPTURE_MAX_BYTES + 1, 1),
    ],
)
def test_codex_subscription_json_pipeline_matches_gateway_capture_limit(
    tmp_path: Path,
    byte_size: int,
    expected_return_code: int,
) -> None:
    prefix = (
        json.dumps(
            {"type": "thread.started", "thread_id": "thread-1"},
            separators=(",", ":"),
        )
        + "\n"
        + json.dumps({"type": "turn.started"}, separators=(",", ":"))
        + "\n"
    )
    item_template = {
        "type": "item.completed",
        "item": {
            "id": "item-1",
            "type": "agent_message",
            "text": "",
        },
    }
    suffix = "\n" + json.dumps(
        {"type": "turn.completed", "usage": dict(CODEX_USAGE)},
        separators=(",", ":"),
    ) + "\n"
    fixed = prefix + json.dumps(item_template, separators=(",", ":")) + suffix
    item_template["item"]["text"] = "x" * (byte_size - len(fixed.encode("utf-8")))
    payload = (
        prefix
        + json.dumps(item_template, separators=(",", ":"))
        + suffix
    ).encode("utf-8")
    assert len(payload) == byte_size
    producer_output = tmp_path / f"producer-{byte_size}.jsonl"
    producer_output.write_bytes(payload)
    log_path = tmp_path / f"codex-{byte_size}.jsonl"
    producer = (
        "from pathlib import Path\n"
        "import sys\n"
        "sys.stdout.buffer.write(Path(sys.argv[1]).read_bytes())\n"
        "raise SystemExit(1)\n"
    )
    command = (
        f"{shlex.quote(sys.executable)} -c {shlex.quote(producer)} "
        f"{shlex.quote(os.fspath(producer_output))}"
    )
    pipeline = _codex_subscription_json_pipeline(command, os.fspath(log_path))

    completed = subprocess.run(
        ["/bin/bash", "-o", "pipefail", "-c", pipeline],
        check=False,
        capture_output=True,
    )

    assert completed.returncode == expected_return_code
    assert log_path.stat().st_size == byte_size


@pytest.mark.parametrize("allow_internet", [True, False])
@pytest.mark.asyncio
async def test_codex_subscription_setup_requires_real_exec_canary(
    tmp_path: Path,
    allow_internet: bool,
) -> None:
    class ReadyRuntime(RecordingRuntime):
        async def exec(
            self,
            command: str,
            *,
            cwd: str | None = None,
            env: dict[str, str] | None = None,
            timeout_sec: float | None = None,
        ) -> ExecResult:
            del env, timeout_sec
            self.commands.append(command)
            if "-probe.sh" in command and "command_execution" in command:
                assert cwd == CODEX_SUBSCRIPTION_CANARY_CWD
                return ExecResult(
                    stdout=f"{CODEX_SUBSCRIPTION_CANARY_OK}\n",
                    return_code=0,
                )
            return ExecResult(return_code=0)

    harness = CodexHarness(
        AgentSpec(
            harness="codex",
            model_name="gpt-5.5",
            settings={
                "auth_mode": "subscription",
                "capture_mode": "transcript",
                CODEX_SUBSCRIPTION_CONTRACT_KEY: codex_subscription_contract(),
            },
        )
    )
    harness.env["OPENEVO_SKILLS_DIR"] = "/openevo/session/evolution/skills"
    runtime = ReadyRuntime(tmp_path)
    runtime.spec = runtime.spec.model_copy(update={"allow_internet": allow_internet})

    await harness.setup(runtime)

    joined = "\n".join(runtime.commands)
    assert "config.toml" not in joined
    assert "/opt/codex/bin/codex exec " in joined
    assert joined.count("/opt/codex/bin/codex exec ") == 2
    assert joined.count(f"network.enabled={str(allow_internet).lower()}") == 2
    assert "sandbox linux" not in joined
    assert "command_execution" in joined
    assert "turn.completed" in joined
    assert "test -r /openevo/credentials/codex/auth.json" in joined
    assert "/proc/self/root /proc/[0-9]*/root" in joined
    assert "command -v sudo" in joined
    assert "sudo -n /bin/cat" in joined
    assert "-events.jsonl" in joined
    assert "-workspace" in joined
    assert "-write" in joined
    assert "test ! -e /openevo/session/home/.openevo-codex-readiness/AGENTS.md" in joined
    assert "test ! -e /openevo/session/home/.agents/skills" in joined
    assert harness.subscription_credential_isolation_receipt is not None
    canary_index = next(
        index for index, command in enumerate(runtime.commands) if "-probe.sh" in command
    )
    assert not any("cp -R --" in command for command in runtime.commands[:canary_index])
    assert any("cp -R --" in command for command in runtime.commands[canary_index + 1 :])
    normal_command = harness.run_steps("Do work.")[0].command
    assert f"network.enabled={str(allow_internet).lower()}" in normal_command


@pytest.mark.asyncio
async def test_codex_subscription_setup_fails_closed_without_exact_canary(
    tmp_path: Path,
) -> None:
    harness = CodexHarness(
        AgentSpec(
            harness="codex",
            settings={"auth_mode": "subscription", "capture_mode": "transcript"},
        )
    )
    runtime = RecordingRuntime(tmp_path)
    harness.env["OPENEVO_SKILLS_DIR"] = "/openevo/session/evolution/skills"

    with pytest.raises(RuntimeError, match="credential isolation could not be proven"):
        await harness.setup(runtime)

    assert harness.subscription_credential_isolation_receipt is None
    assert not any("cp -R --" in command for command in runtime.commands)


@pytest.mark.asyncio
async def test_codex_subscription_publishes_readiness_only_after_skill_install(
    tmp_path: Path,
) -> None:
    class SkillFailureRuntime(RecordingRuntime):
        async def exec(
            self,
            command: str,
            *,
            cwd: str | None = None,
            env: dict[str, str] | None = None,
            timeout_sec: float | None = None,
        ) -> ExecResult:
            del env, timeout_sec
            self.commands.append(command)
            if "-probe.sh" in command and "command_execution" in command:
                assert cwd == CODEX_SUBSCRIPTION_CANARY_CWD
                return ExecResult(
                    stdout=f"{CODEX_SUBSCRIPTION_CANARY_OK}\n",
                    return_code=0,
                )
            if "cp -R --" in command:
                return ExecResult(return_code=1)
            return ExecResult(return_code=0)

    harness = CodexHarness(
        AgentSpec(
            harness="codex",
            settings={"auth_mode": "subscription", "capture_mode": "transcript"},
        )
    )
    harness.env["OPENEVO_SKILLS_DIR"] = "/openevo/session/evolution/skills"
    runtime = SkillFailureRuntime(tmp_path)

    with pytest.raises(RuntimeError, match="skill installation failed"):
        await harness.setup(runtime)

    assert harness.subscription_credential_isolation_receipt is None
    assert any("cp -R --" in command for command in runtime.commands)


@pytest.mark.parametrize(
    "agent",
    [
        AgentSpec(
            harness="codex",
            settings={
                "auth_mode": "subscription",
                "capture_mode": "transcript",
                "sandbox_mode": "danger-full-access",
            },
        ),
        AgentSpec(
            harness="codex",
            settings={"auth_mode": "subscription", "capture_mode": "transcript"},
            env={"BWRAP": "/openevo/session/workspace/evil-bwrap"},
        ),
        AgentSpec(
            harness="codex",
            settings={"auth_mode": "subscription", "capture_mode": "transcript"},
            mcp_servers=[
                MCPServerSpec(
                    name="evil",
                    transport="stdio",
                    command="/openevo/session/workspace/evil-mcp",
                )
            ],
        ),
    ],
)
def test_codex_subscription_rejects_caller_execution_surfaces(
    agent: AgentSpec,
) -> None:
    harness = CodexHarness(agent)

    with pytest.raises(ValueError, match="forbidden|env overrides|MCP"):
        harness.run_steps("Do work.")


def test_codex_subscription_fixed_overrides_follow_optional_config() -> None:
    harness = CodexHarness(
        AgentSpec(
            harness="codex",
            settings={
                "auth_mode": "subscription",
                "capture_mode": "transcript",
                "reasoning_effort": "high",
            },
        )
    )

    command = harness.run_steps("Do work.")[0].command

    assert command.index("-c model_reasoning_effort=high") < command.index(
        'default_permissions="openevo_codex_subscription_v1"'
    )
    assert command.rindex("features.plugins=false") < command.rindex("Do work.")


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

    assert step.command.startswith("/bin/bash -o pipefail -c ")
    assert "env -u" in step.command
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

    assert step.command.startswith("/bin/bash -o pipefail -c ")
    assert "env -u" in step.command
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
