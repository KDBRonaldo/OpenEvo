"""Codex CLI harness — https://github.com/openai/codex"""

from __future__ import annotations

import json
import shlex

from openevo.harness.base import BaseHarness
from openevo.harness.models import AgentSpec
from openevo.harness.presets._subscription import (
    AUTH_MODE_PROXY,
    AUTH_MODE_SUBSCRIPTION,
    command_with_unset_proxy_env,
    normalize_auth_mode,
)
from openevo.runtime.base import BaseRuntime, RUNTIME_AGENT_LOG_DIR, RUNTIME_SESSION_DIR
from openevo.runtime.managed import MANAGED_CODEX_BINARY, MANAGED_CODEX_HOME
from openevo.runtime.models import ExecInput


_NATIVE_MEMORY_POLICY_PRESERVE = "preserve"
_NATIVE_MEMORY_POLICY_CLEAR = "clear"
_NATIVE_MEMORY_POLICIES = {
    _NATIVE_MEMORY_POLICY_PRESERVE,
    _NATIVE_MEMORY_POLICY_CLEAR,
}


class CodexHarness(BaseHarness):
    """Run OpenAI Codex CLI in non-interactive mode."""

    _AUTH_MODE_PROXY = AUTH_MODE_PROXY
    _AUTH_MODE_CHATGPT_SUBSCRIPTION = "chatgpt_subscription"

    def __init__(self, agent_spec: AgentSpec) -> None:
        super().__init__(agent_spec)
        # Keep credentials (auth.json, config.toml) outside the log dir so log
        # rotation or archival can't clobber them. Absolute path — $HOME won't
        # expand in docker exec -e.
        self._codex_home = f"{RUNTIME_SESSION_DIR}/.codex"

    def _codex_home_path(self) -> str:
        if self.settings.get("auth_mode") in {
            AUTH_MODE_SUBSCRIPTION,
            self._AUTH_MODE_CHATGPT_SUBSCRIPTION,
        }:
            return MANAGED_CODEX_HOME
        return _nonempty_env_path(self.env.get("CODEX_HOME")) or self._codex_home

    async def setup(self, runtime: BaseRuntime) -> None:
        codex_home = self._codex_home_path()
        await runtime.exec(f"mkdir -p {shlex.quote(codex_home)}")
        if _native_memory_policy(self.settings) == _NATIVE_MEMORY_POLICY_CLEAR:
            await runtime.exec(_clear_native_memory_command(codex_home))

        # Host-uploaded files keep the host UID, which blocks codex's
        # exec_command-based edits (cat/tee/open) on a non-root container
        # user. Other harnesses survive by rm+recreating the file. Best-effort;
        # a no-op on images without sudo.
        workdir = runtime.spec.workdir or runtime.runtime_session_dir
        await runtime.exec(
            f'sudo chown -R "$(id -u):$(id -g)" {shlex.quote(workdir)} 2>/dev/null || true'
        )

        toml_lines: list[str] = []
        for server in self.mcp_servers:
            toml_lines.append(f"[mcp_servers.{_toml_string(server.name)}]")
            if server.transport == "stdio":
                toml_lines.append(f"command = {_toml_string(server.command or '')}")
                if server.args:
                    args_str = ", ".join(_toml_string(arg) for arg in server.args)
                    toml_lines.append(f"args = [{args_str}]")
            else:
                toml_lines.append(f"url = {_toml_string(server.url or '')}")
                toml_lines.append(f"type = {_toml_string(server.transport)}")
        toml_content = "\n".join(toml_lines)
        await runtime.exec(
            f"cat > {shlex.quote(codex_home)}/config.toml << 'OPENEVO_CFG'\n{toml_content}\nOPENEVO_CFG"
        )

        # Copy skills
        for skills_path in self.effective_skill_paths():
            result = await runtime.exec(
                f"mkdir -p $HOME/.agents/skills && "
                f"cp -r {shlex.quote(skills_path)}/* $HOME/.agents/skills/"
            )
            if result.return_code != 0:
                raise RuntimeError("Codex skill installation failed")

    def run_steps(self, instruction: str) -> list[ExecInput]:
        escaped = shlex.quote(instruction)
        auth_mode = normalize_auth_mode(
            self.settings.get("auth_mode"),
            harness="codex",
            subscription_aliases=(self._AUTH_MODE_CHATGPT_SUBSCRIPTION,),
            capture_mode=self.settings.get("capture_mode"),
        )

        codex_home = self._codex_home_path()
        env: dict[str, str] = {
            **self.env,
            "CODEX_HOME": codex_home,
        }

        flags: list[str] = [
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "--json",
            "--enable unified_exec",
        ]
        if auth_mode == AUTH_MODE_PROXY:
            # Canonical pattern for pointing codex at an OpenAI-compatible proxy:
            # define a custom model_provider with wire_api="responses" (see
            # codex-rs/responses-api-proxy/README.md). Dropped the three
            # features.* toggles — they don't appear in the current config schema
            # and codex silently parses them as unknown keys, which can produce
            # warnings or, in strict versions, early exits.
            flags.extend(
                [
                    "-c 'model_provider=\"harness_proxy\"'",
                    "-c 'model_providers.harness_proxy.name=\"Harness Proxy\"'",
                    '-c "model_providers.harness_proxy.base_url=\\"$OPENAI_BASE_URL\\""',
                    "-c 'model_providers.harness_proxy.env_key=\"OPENAI_API_KEY\"'",
                    "-c 'model_providers.harness_proxy.wire_api=\"responses\"'",
                ]
            )
        model = _cli_model_name(self.model_name)
        flags.append(f"--model {shlex.quote(model)}")

        for key, cli in [
            ("reasoning_effort", "-c model_reasoning_effort"),
            ("reasoning_summary", "-c model_reasoning_summary"),
        ]:
            value = self.settings.get(key)
            if value is not None:
                flags.append(f"{cli}={shlex.quote(str(value))}")

        flags_str = " ".join(flags)
        executable = (
            MANAGED_CODEX_BINARY
            if auth_mode == AUTH_MODE_SUBSCRIPTION
            else "codex"
        )
        command = (
            f"{executable} exec {flags_str} -- {escaped} "
            f"2>&1 </dev/null | tee {RUNTIME_AGENT_LOG_DIR}/codex.txt"
        )
        if auth_mode == AUTH_MODE_SUBSCRIPTION:
            command = command_with_unset_proxy_env(command)

        run_step = ExecInput(
            command=command,
            env=env,
        )
        if auth_mode == AUTH_MODE_SUBSCRIPTION:
            return [run_step]

        return [
            # Write synthetic auth.json so codex picks up OPENAI_API_KEY
            ExecInput(
                command=(
                    f"mkdir -p {shlex.quote(codex_home)} && "
                    f'printf \'{{"OPENAI_API_KEY": "%s"}}\' "$OPENAI_API_KEY" '
                    f"> {shlex.quote(codex_home)}/auth.json"
                ),
                env=env,
            ),
            run_step,
        ]


def _cli_model_name(model_name: str | None) -> str:
    model = model_name or "gpt-5.4"
    for prefix in ("openai/", "anthropic/", "google/", "gcp/google/"):
        if model.startswith(prefix):
            return model[len(prefix) :]
    return model


def _nonempty_env_path(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _native_memory_policy(settings: dict[str, object]) -> str:
    raw_policy = settings.get("native_memory_policy")
    if raw_policy is None:
        return _NATIVE_MEMORY_POLICY_PRESERVE
    if not isinstance(raw_policy, str):
        raise ValueError("native_memory_policy must be 'preserve' or 'clear'")
    if raw_policy not in _NATIVE_MEMORY_POLICIES:
        raise ValueError("native_memory_policy must be 'preserve' or 'clear'")
    return raw_policy


def _clear_native_memory_command(codex_home: str) -> str:
    quoted_home = shlex.quote(codex_home)
    return (
        "rm -rf -- "
        f"{quoted_home}/memories "
        f"{quoted_home}/memories_*.sqlite*"
    )


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)
