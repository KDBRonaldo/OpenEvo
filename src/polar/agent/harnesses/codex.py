"""Codex CLI harness — https://github.com/openai/codex"""

from __future__ import annotations

import shlex

from polar.agent.base import BaseHarness
from polar.agent.models import AgentSpec
from polar.runtime.base import BaseRuntime, RUNTIME_AGENT_LOG_DIR, RUNTIME_SESSION_DIR
from polar.runtime.models import ExecInput


class CodexHarness(BaseHarness):
    """Run OpenAI Codex CLI in non-interactive mode."""

    def __init__(self, agent_spec: AgentSpec) -> None:
        super().__init__(agent_spec)
        # Keep credentials (auth.json, config.toml) outside the log dir so log
        # rotation or archival can't clobber them. Absolute path — $HOME won't
        # expand in docker exec -e.
        self._codex_home = f"{RUNTIME_SESSION_DIR}/.codex"

    async def setup(self, runtime: BaseRuntime) -> None:
        await runtime.exec(f"mkdir -p {self._codex_home}")

        # Host-uploaded files keep the host UID, which blocks codex's
        # exec_command-based edits (cat/tee/open) on a non-root container
        # user. Other harnesses survive by rm+recreating the file. Best-effort;
        # a no-op on images without sudo.
        workdir = runtime.spec.workdir or runtime.runtime_session_dir
        await runtime.exec(
            f'sudo chown -R "$(id -u):$(id -g)" {shlex.quote(workdir)} 2>/dev/null || true'
        )

        # Register MCP servers via TOML config
        if self.mcp_servers:
            toml_lines: list[str] = []
            for server in self.mcp_servers:
                toml_lines.append(f'[mcp_servers."{server.name}"]')
                if server.transport == "stdio":
                    toml_lines.append(f'command = "{server.command}"')
                    if server.args:
                        args_str = ", ".join(f'"{a}"' for a in server.args)
                        toml_lines.append(f"args = [{args_str}]")
                else:
                    toml_lines.append(f'url = "{server.url}"')
                    toml_lines.append(f'type = "{server.transport}"')
            toml_content = "\n".join(toml_lines)
            await runtime.exec(
                f"cat > {self._codex_home}/config.toml << 'POLARCFG'\n{toml_content}\nPOLARCFG"
            )

        # Copy skills
        if self.skills_path:
            await runtime.exec(
                f"mkdir -p $HOME/.agents/skills && "
                f"cp -r {shlex.quote(self.skills_path)}/* $HOME/.agents/skills/ 2>/dev/null || true"
            )

    def run_steps(self, instruction: str) -> list[ExecInput]:
        escaped = shlex.quote(instruction)
        env: dict[str, str] = {
            **self.env,
            "CODEX_HOME": self._codex_home,
        }

        # Canonical pattern for pointing codex at an OpenAI-compatible proxy:
        # define a custom model_provider with wire_api="responses" (see
        # codex-rs/responses-api-proxy/README.md). Dropped the three
        # features.* toggles — they don't appear in the current config schema
        # and codex silently parses them as unknown keys, which can produce
        # warnings or, in strict versions, early exits.
        flags: list[str] = [
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "--json",
            "--enable unified_exec",
            "-c 'model_provider=\"harness_proxy\"'",
            "-c 'model_providers.harness_proxy.name=\"Harness Proxy\"'",
            '-c "model_providers.harness_proxy.base_url=\\"$OPENAI_BASE_URL\\""',
            "-c 'model_providers.harness_proxy.env_key=\"OPENAI_API_KEY\"'",
            "-c 'model_providers.harness_proxy.wire_api=\"responses\"'",
        ]
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
        return [
            # Write synthetic auth.json so codex picks up OPENAI_API_KEY
            ExecInput(
                command=(
                    f"mkdir -p {self._codex_home} && "
                    f'printf \'{{"OPENAI_API_KEY": "%s"}}\' "$OPENAI_API_KEY" '
                    f"> {self._codex_home}/auth.json"
                ),
                env=env,
            ),
            ExecInput(
                command=(
                    f"codex exec {flags_str} -- {escaped} "
                    f"2>&1 </dev/null | tee {RUNTIME_AGENT_LOG_DIR}/codex.txt"
                ),
                env=env,
            ),
        ]


def _cli_model_name(model_name: str | None) -> str:
    model = model_name or "gpt-5.4"
    for prefix in ("openai/", "anthropic/", "google/", "gcp/google/"):
        if model.startswith(prefix):
            return model[len(prefix):]
    return model
