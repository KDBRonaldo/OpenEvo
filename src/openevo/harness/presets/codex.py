"""Codex CLI harness — https://github.com/openai/codex"""

from __future__ import annotations

import json
import shlex

from openevo.codex_models import codex_cli_model_name, validate_codex_model_ref
from openevo.harness.base import BaseHarness
from openevo.harness.models import AgentSpec
from openevo.harness.presets._subscription import (
    AUTH_MODE_PROXY,
    AUTH_MODE_SUBSCRIPTION,
    command_with_unset_proxy_env,
    normalize_auth_mode,
)
from openevo.runtime.base import (
    LOCAL_COMMAND_CAPTURE_MAX_BYTES,
    BaseRuntime,
    RUNTIME_AGENT_LOG_DIR,
    RUNTIME_SESSION_DIR,
)
from openevo.runtime.codex_isolation import (
    CODEX_SUBSCRIPTION_CANARY_CWD,
    CODEX_SUBSCRIPTION_CANARY_OK,
    codex_subscription_cli_flags,
    codex_subscription_exec_canary_command,
    codex_subscription_readiness_receipt,
    validate_codex_subscription_surface,
)
from openevo.runtime.managed import (
    MANAGED_CODEX_BINARY,
    MANAGED_CODEX_DEFAULT_MODEL,
    MANAGED_CODEX_HOME,
)
from openevo.runtime.models import ExecInput


_NATIVE_MEMORY_POLICY_PRESERVE = "preserve"
_NATIVE_MEMORY_POLICY_CLEAR = "clear"
_NATIVE_MEMORY_POLICIES = {
    _NATIVE_MEMORY_POLICY_PRESERVE,
    _NATIVE_MEMORY_POLICY_CLEAR,
}
_CODEX_TERMINAL_EVENT_VALIDATOR_SOURCE = rf"""
import json
import os
import stat
import sys

max_bytes = {LOCAL_COMMAND_CAPTURE_MAX_BYTES}
flags = os.O_RDONLY | os.O_CLOEXEC
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW

try:
    descriptor = os.open(sys.argv[1], flags)
except OSError:
    raise SystemExit(1)

counts = {{
    "thread.started": 0,
    "turn.started": 0,
    "turn.completed": 0,
}}
turn_active = False
try:
    before = os.fstat(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_uid != os.geteuid()
        or not 0 < before.st_size <= max_bytes
    ):
        raise SystemExit(1)
    payload = b""
    while len(payload) <= before.st_size:
        chunk = os.read(descriptor, min(65536, before.st_size - len(payload) + 1))
        if not chunk:
            break
        payload += chunk
    after = os.fstat(descriptor)
    path_metadata = os.lstat(sys.argv[1])
finally:
    os.close(descriptor)

def identity(metadata):
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )

if (
    len(payload) != before.st_size
    or payload[-1:] != b"\n"
    or identity(before) != identity(after)
    or identity(before) != identity(path_metadata)
    or not stat.S_ISREG(path_metadata.st_mode)
    or path_metadata.st_nlink != 1
):
    raise SystemExit(1)

try:
    lines = payload.decode("utf-8").splitlines()
except UnicodeError:
    raise SystemExit(1)
if lines and lines[0] == "Reading additional input from stdin...":
    lines = lines[1:]
if not lines or any(not line for line in lines):
    raise SystemExit(1)

last_event_type = None
for raw_line in lines:
    try:
        event = json.loads(raw_line)
    except (json.JSONDecodeError, RecursionError):
        raise SystemExit(1)
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        raise SystemExit(1)
    event_type = event["type"]
    if event_type == "thread.started":
        if (
            any(counts.values())
            or turn_active
            or not isinstance(event.get("thread_id"), str)
            or not event["thread_id"]
        ):
            raise SystemExit(1)
        counts[event_type] += 1
    elif event_type == "turn.started":
        if counts["thread.started"] != 1 or any(
            counts[name] for name in ("turn.started", "turn.completed")
        ) or turn_active:
            raise SystemExit(1)
        counts[event_type] += 1
        turn_active = True
    elif event_type in {{"item.started", "item.updated", "item.completed"}}:
        item = event.get("item")
        if (
            not turn_active
            or not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not item["id"]
            or not isinstance(item.get("type"), str)
            or not item["type"]
        ):
            raise SystemExit(1)
    elif event_type == "turn.completed":
        usage = event.get("usage")
        usage_fields = {{
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
        }}
        if (
            not turn_active
            or counts[event_type] != 0
            or not isinstance(usage, dict)
            or set(usage) != usage_fields
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in usage.values()
            )
        ):
            raise SystemExit(1)
        counts[event_type] += 1
        turn_active = False
    elif event_type in {{"turn.failed", "error"}}:
        raise SystemExit(1)
    else:
        raise SystemExit(1)
    last_event_type = event_type

if (
    counts
    != {{
        "thread.started": 1,
        "turn.started": 1,
        "turn.completed": 1,
    }}
    or turn_active
    or last_event_type != "turn.completed"
):
    raise SystemExit(1)
"""


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
        self._configured_env = dict(agent_spec.env)
        self._configured_mcp_servers = tuple(agent_spec.mcp_servers)
        self._subscription_credential_isolation: dict[str, object] | None = None
        self._subscription_allow_internet = True

    def _codex_home_path(self) -> str:
        if self.settings.get("auth_mode") in {
            AUTH_MODE_SUBSCRIPTION,
            self._AUTH_MODE_CHATGPT_SUBSCRIPTION,
        }:
            return MANAGED_CODEX_HOME
        return _nonempty_env_path(self.env.get("CODEX_HOME")) or self._codex_home

    async def setup(self, runtime: BaseRuntime) -> None:
        auth_mode = self._auth_mode()
        codex_home = self._codex_home_path()
        if auth_mode == AUTH_MODE_SUBSCRIPTION:
            self._validate_subscription_surface()
            self._subscription_credential_isolation = None
            self._subscription_allow_internet = runtime.spec.allow_internet
        else:
            await runtime.exec(f"mkdir -p {shlex.quote(codex_home)}")
            if _native_memory_policy(self.settings) == _NATIVE_MEMORY_POLICY_CLEAR:
                await runtime.exec(_clear_native_memory_command(codex_home))

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
                f"cat > {shlex.quote(codex_home)}/config.toml << 'OPENEVO_CFG'\n"
                f"{toml_content}\nOPENEVO_CFG"
            )

        if auth_mode == AUTH_MODE_SUBSCRIPTION:
            result = await runtime.exec(
                codex_subscription_exec_canary_command(
                    model=_validated_cli_model_name(self.model_name),
                    allow_internet=self._subscription_allow_internet,
                ),
                cwd=CODEX_SUBSCRIPTION_CANARY_CWD,
            )
            if (
                result.return_code != 0
                or (result.stdout or "").strip() != CODEX_SUBSCRIPTION_CANARY_OK
            ):
                raise RuntimeError("Codex subscription credential isolation could not be proven")

        # Runtime-provided skills are untrusted task context. Subscription
        # readiness must be proven before Codex can discover them.
        for skills_path in self.effective_skill_paths():
            skill_source = shlex.quote(skills_path.rstrip("/") + "/.")
            result = await runtime.exec(
                'mkdir -p "$HOME/.agents/skills" && '
                f'cp -R -- {skill_source} "$HOME/.agents/skills/"'
            )
            if result.return_code != 0:
                raise RuntimeError("Codex skill installation failed")

        if auth_mode == AUTH_MODE_SUBSCRIPTION:
            self._subscription_credential_isolation = codex_subscription_readiness_receipt()

    def run_steps(self, instruction: str) -> list[ExecInput]:
        escaped = shlex.quote(instruction)
        auth_mode = self._auth_mode()
        if auth_mode == AUTH_MODE_SUBSCRIPTION:
            self._validate_subscription_surface()

        codex_home = self._codex_home_path()
        env: dict[str, str] = {
            **self.env,
            "CODEX_HOME": codex_home,
        }

        flags: list[str] = [
            "--skip-git-repo-check",
            "--json",
            "--ephemeral",
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
        model = _validated_cli_model_name(self.model_name)
        flags.append(f"--model {shlex.quote(model)}")

        for key, cli in [
            ("reasoning_effort", "-c model_reasoning_effort"),
            ("reasoning_summary", "-c model_reasoning_summary"),
        ]:
            value = self.settings.get(key)
            if value is not None:
                flags.append(f"{cli}={shlex.quote(str(value))}")

        if auth_mode == AUTH_MODE_SUBSCRIPTION:
            # These are deliberately final so no caller-controlled option can
            # supersede the credential-isolation profile.
            flags.extend(
                codex_subscription_cli_flags(allow_internet=self._subscription_allow_internet)
            )

        flags_str = " ".join(flags)
        executable = MANAGED_CODEX_BINARY if auth_mode == AUTH_MODE_SUBSCRIPTION else "codex"
        codex_command = f"{executable} exec {flags_str} -- {escaped}"
        if auth_mode == AUTH_MODE_SUBSCRIPTION:
            codex_command = command_with_unset_proxy_env(codex_command)
            pipeline = _codex_subscription_json_pipeline(
                codex_command,
                f"{RUNTIME_AGENT_LOG_DIR}/codex.txt",
            )
        else:
            pipeline = (
                f"{codex_command} 2>&1 </dev/null | "
                f"tee {RUNTIME_AGENT_LOG_DIR}/codex.txt"
            )
        command = f"/bin/bash -o pipefail -c {shlex.quote(pipeline)}"

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

    @property
    def subscription_credential_isolation_receipt(
        self,
    ) -> dict[str, object] | None:
        if self._subscription_credential_isolation is None:
            return None
        return dict(self._subscription_credential_isolation)

    def _auth_mode(self) -> str:
        return normalize_auth_mode(
            self.settings.get("auth_mode"),
            harness="codex",
            subscription_aliases=(self._AUTH_MODE_CHATGPT_SUBSCRIPTION,),
            capture_mode=self.settings.get("capture_mode"),
        )

    def _validate_subscription_surface(self) -> None:
        validate_codex_subscription_surface(
            settings=self.settings,
            env=self._configured_env,
            mcp_servers=self._configured_mcp_servers,
        )


def _validated_cli_model_name(model_name: str | None) -> str:
    model = model_name or MANAGED_CODEX_DEFAULT_MODEL
    validated = validate_codex_model_ref(model)
    return codex_cli_model_name(validated)


def _nonempty_env_path(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _codex_subscription_json_pipeline(codex_command: str, log_path: str) -> str:
    quoted_log = shlex.quote(log_path)
    validator = (
        f"python3 -c {shlex.quote(_CODEX_TERMINAL_EVENT_VALIDATOR_SOURCE)} "
        f"{quoted_log}"
    )
    return (
        "set +e; "
        f"{codex_command} </dev/null | tee {quoted_log}; "
        'pipeline_status=("${PIPESTATUS[@]}"); '
        'codex_rc="${pipeline_status[0]:-1}"; '
        'tee_rc="${pipeline_status[1]:-1}"; '
        "set -e; "
        'if test "$tee_rc" -ne 0; then exit "$tee_rc"; fi; '
        f"if {validator}; then "
        'if test "$codex_rc" -ne 0; then '
        "printf '%s\\n' "
        "'OpenEvo accepted a completed Codex JSON turn after a nonzero CLI exit.' "
        ">&2; "
        "fi; "
        "exit 0; "
        "fi; "
        'if test "$codex_rc" -ne 0; then exit "$codex_rc"; fi; '
        "exit 1"
    )


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
    return f"rm -rf -- {quoted_home}/memories {quoted_home}/memories_*.sqlite*"


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)
