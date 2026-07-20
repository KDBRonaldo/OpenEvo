from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tomllib

import pytest

from openevo.runtime.codex_isolation import (
    CODEX_SUBSCRIPTION_CANARY_OK,
    CODEX_SUBSCRIPTION_PERMISSION_PROFILE,
    CODEX_SUBSCRIPTION_POLICY_SHA256,
    CODEX_SUBSCRIPTION_REFRESH_PERSISTENCE,
    _canary_event_validator_command,
    codex_subscription_cli_flags,
    codex_subscription_cli_overrides,
    codex_subscription_contract,
    codex_subscription_exec_canary_command,
    validate_codex_subscription_surface,
    validate_codex_subscription_version,
)
from openevo.runtime.managed import (
    MANAGED_CODEX_DEFAULT_MODEL,
    MANAGED_CODEX_NPM_PACKAGE,
    MANAGED_CODEX_PACKAGE_ROOT,
    MANAGED_CODEX_VERSION,
)


_NONCE = "0123456789abcdef0123456789abcdef"
_SCRIPT_PATH = "/openevo/session/workspace/.openevo-test-canary-probe.sh"


def test_codex_subscription_policy_identity_is_stable() -> None:
    assert CODEX_SUBSCRIPTION_POLICY_SHA256 == (
        "59ea503b553aa414ddcc35ede66210ee901621eebcbd1cfbeb06023410e35d38"
    )
    assert codex_subscription_contract() == {
        "schema_version": 1,
        "policy_id": "openevo.codex-subscription-credential-isolation.v1",
        "policy_sha256": CODEX_SUBSCRIPTION_POLICY_SHA256,
        "permission_profile": CODEX_SUBSCRIPTION_PERMISSION_PROFILE,
        "codex_version": "0.144.1",
        "default_model": "gpt-5.5",
        "sandbox_backend": "linux-bubblewrap",
        "refresh_persistence": "unsupported_read_only_auth_overlay",
    }
    assert MANAGED_CODEX_VERSION == "0.144.1"
    assert MANAGED_CODEX_NPM_PACKAGE == "@openai/codex@0.144.1"
    assert MANAGED_CODEX_DEFAULT_MODEL == "gpt-5.5"
    assert CODEX_SUBSCRIPTION_REFRESH_PERSISTENCE == "unsupported_read_only_auth_overlay"


def test_codex_subscription_overrides_are_valid_toml_and_closed() -> None:
    parsed = [
        tomllib.loads(value) for value in codex_subscription_cli_overrides(allow_internet=True)
    ]
    rendered = "\n".join(codex_subscription_cli_overrides(allow_internet=True))
    flags = " ".join(codex_subscription_cli_flags(allow_internet=True))

    assert f'"{MANAGED_CODEX_PACKAGE_ROOT}"="read"' in rendered
    assert '"/openevo/credentials/codex"="deny"' in rendered
    assert '"/openevo/session/workspace"="write"' in rendered
    assert '"/openevo/session/home"="read"' in rendered
    assert '"/openevo/session/evolution"="read"' in rendered
    assert '"/tmp"="write"' in rendered
    assert "network.enabled=true" in rendered
    assert 'approval_policy="never"' in rendered
    assert 'inherit="none"' in rendered
    assert 'trust_level="untrusted"' in rendered
    assert "features.unified_exec=true" in rendered
    assert "use_legacy_landlock" not in rendered
    assert "use_linux_sandbox_bwrap" not in rendered
    assert "web_search_cached" not in rendered
    assert "web_search_request" not in rendered
    assert "features.hooks=false" in rendered
    assert "features.multi_agent=false" in rendered
    assert "features.apps=false" in rendered
    assert "features.plugins=false" in rendered
    assert "features.request_permissions_tool=false" in rendered
    assert "features.exec_permission_approvals=false" in rendered
    assert "mcp_servers={}" in rendered
    assert 'web_search="live"' in rendered
    assert 'OPENEVO_EVOLUTION_CONTEXT="/openevo/session/evolution/context.json"' in rendered
    assert 'OPENEVO_MEMORY_FILE="/openevo/session/evolution/memory.md"' in rendered
    assert 'OPENEVO_SKILLS_DIR="/openevo/session/evolution/skills"' in rendered
    assert 'OPENEVO_ADAPTER_MERGE_SPEC="/openevo/session/evolution/adapters.json"' in rendered
    assert 'OPENEVO_AGENT_SYSTEM_FILE="/openevo/session/evolution/agent_system.md"' in rendered
    assert "--strict-config" in flags
    assert "--ignore-user-config" in flags
    assert "--ignore-rules" in flags
    assert parsed


def test_codex_subscription_network_policy_is_core_owned() -> None:
    enabled = "\n".join(codex_subscription_cli_overrides(allow_internet=True))
    disabled = "\n".join(codex_subscription_cli_overrides(allow_internet=False))

    assert "network.enabled=true" in enabled
    assert "network.enabled=false" in disabled
    assert 'web_search="live"' in enabled
    assert 'web_search="disabled"' in disabled
    assert '"/openevo/credentials/codex"="deny"' in enabled
    assert '"/openevo/credentials/codex"="deny"' in disabled
    with pytest.raises(ValueError, match="Core-owned"):
        codex_subscription_cli_overrides(allow_internet=1)  # type: ignore[arg-type]


def test_codex_subscription_canary_uses_real_exec_and_validates_boundaries() -> None:
    command = codex_subscription_exec_canary_command(
        model="gpt-5.5",
        allow_internet=True,
        nonce=_NONCE,
    )

    subprocess.run(
        ["/bin/bash", "-n"],
        input=command,
        text=True,
        check=True,
        capture_output=True,
    )
    assert "codex-cli 0.144.1" in command
    assert "/opt/codex/bin/codex exec " in command
    assert "--model gpt-5.5" in command
    assert "--json" in command
    assert command.count("--strict-config") == 2
    assert command.count("--ignore-user-config") == 2
    assert command.count("--ignore-rules") == 2
    assert command.count("network.enabled=true") == 2
    assert "sandbox linux" not in command
    assert "command_execution" in command
    assert "turn.completed" in command
    assert command.count("/opt/codex/bin/codex exec ") == 2
    assert command.count("< /dev/null") == 2
    assert "O_NOFOLLOW" in command
    assert "0o500" in command
    assert "hashlib.sha256" in command
    assert "/proc/self/root /proc/[0-9]*/root" in command
    assert "command -v sudo" in command
    assert "sudo -n /bin/cat" in command
    assert "/openevo/credentials/codex/auth.json >/dev/null" in command
    assert re.search(r"\.openevo-[0-9a-f]{24}-probe\.sh", command)
    assert re.search(r"\.openevo-[0-9a-f]{24}-events\.jsonl", command)
    assert re.search(r"\.openevo-[0-9a-f]{24}-stderr", command)
    assert "ulimit -f" not in command
    assert '"result":"isolated"' in command
    assert '"leak":false' in command
    assert '"forbidden_file":false' in command
    parsed = shlex.split(command)
    exec_indexes = [
        index
        for index, token in enumerate(parsed)
        if token == "/opt/codex/bin/codex" and parsed[index + 1] == "exec"
    ]
    assert len(exec_indexes) == 2
    for index in exec_indexes:
        stdin_index = parsed.index("<", index)
        prompt = parsed[stdin_index - 1]
        assert "invoke the shell tool exactly once" in prompt
        assert "/bin/sh /openevo/session/workspace/" in prompt
        assert "/openevo/credentials" not in prompt
        assert "/proc/" not in prompt
        assert "sudo" not in prompt


def test_canary_event_validator_accepts_exact_completed_command(
    tmp_path: Path,
) -> None:
    result = _run_validator(tmp_path, _canary_events())

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_canary_event_validator_accepts_codex_stdin_notice(tmp_path: Path) -> None:
    result = _run_validator(
        tmp_path,
        _canary_events(),
        stderr=b"Reading additional input from stdin...\n",
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.parametrize(
    "case",
    [
        pytest.param({"include_started": False}, id="missing-command-start"),
        pytest.param(
            {"command_output": '{"result":"isolated"}\n'},
            id="incomplete-marker",
        ),
        pytest.param({"extra_tool_type": "mcp_tool_call"}, id="extra-tool"),
        pytest.param({"extra_event_type": "error"}, id="error-event"),
        pytest.param({"extra_event_type": "deprecated"}, id="unknown-event"),
        pytest.param({"command_suffix": "; /bin/true"}, id="appended-command"),
    ],
)
def test_canary_event_validator_fails_closed_on_ambiguous_evidence(
    tmp_path: Path,
    case: dict[str, object],
) -> None:
    result = _run_validator(tmp_path, _canary_events(**case))

    assert result.returncode != 0
    assert result.returncode != 42
    assert "real-exec evidence is invalid" in result.stderr
    assert CODEX_SUBSCRIPTION_CANARY_OK not in result.stderr


def test_canary_event_validator_allows_only_clean_no_tool_retry(
    tmp_path: Path,
) -> None:
    result = _run_validator(
        tmp_path,
        _canary_events(
            include_command=False,
            final_text=CODEX_SUBSCRIPTION_CANARY_OK,
        ),
    )

    assert result.returncode == 42
    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("version", "accepted"),
    [
        ("codex-cli 0.144.1", True),
        ("codex-cli 0.121.0", False),
        ("codex-cli 0.144.2", False),
    ],
)
def test_codex_subscription_version_is_an_exact_pin(
    version: str,
    accepted: bool,
) -> None:
    if accepted:
        validate_codex_subscription_version(version)
    else:
        with pytest.raises(ValueError, match="release pin"):
            validate_codex_subscription_version(version)


@pytest.mark.parametrize(
    ("settings", "env", "mcp_servers", "message"),
    [
        (
            {
                "auth_mode": "subscription",
                "capture_mode": "transcript",
                "config": {"sandbox_mode": "danger-full-access"},
            },
            {},
            (),
            "forbidden execution fields",
        ),
        (
            {"auth_mode": "subscription", "capture_mode": "transcript"},
            {"PATH": "/tmp/evil"},
            (),
            "env overrides",
        ),
        (
            {"auth_mode": "subscription", "capture_mode": "transcript"},
            {},
            (object(),),
            "MCP servers",
        ),
        (
            {
                "auth_mode": "subscription",
                "capture_mode": "transcript",
                "credential_isolation": {
                    **codex_subscription_contract(),
                    "codex_version": "0.121.0",
                },
            },
            {},
            (),
            "identity is invalid",
        ),
    ],
)
def test_codex_subscription_surface_rejects_extension_points(
    settings: dict[str, object],
    env: dict[str, str],
    mcp_servers: tuple[object, ...],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_codex_subscription_surface(
            settings=settings,
            env=env,
            mcp_servers=mcp_servers,
        )


def _expected_output() -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "nonce": _NONCE,
            "marker": CODEX_SUBSCRIPTION_CANARY_OK,
            "result": "isolated",
            "leak": False,
            "forbidden_file": False,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _canary_events(
    *,
    include_command: bool = True,
    include_started: bool = True,
    command_output: str | None = None,
    extra_tool_type: str | None = None,
    extra_event_type: str | None = None,
    command_suffix: str = "",
    final_text: str = "done",
) -> bytes:
    invocation = f"/bin/sh {_SCRIPT_PATH} {_NONCE}{command_suffix}"
    command = f"/bin/bash -c {shlex.quote(invocation)}"
    events: list[dict[str, object]] = [
        {"type": "thread.started", "thread_id": "thread_1"},
        {"type": "turn.started"},
    ]
    if include_command and include_started:
        events.append(
            {
                "type": "item.started",
                "item": {
                    "id": "item_1",
                    "type": "command_execution",
                    "command": command,
                    "aggregated_output": "",
                    "exit_code": None,
                    "status": "in_progress",
                },
            }
        )
    if include_command:
        events.append(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_1",
                    "type": "command_execution",
                    "command": command,
                    "aggregated_output": command_output or _expected_output() + "\n",
                    "exit_code": 0,
                    "status": "completed",
                },
            }
        )
    if extra_tool_type is not None:
        events.append(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_2",
                    "type": extra_tool_type,
                    "status": "completed",
                },
            }
        )
    if extra_event_type is not None:
        events.append({"type": extra_event_type, "message": "must fail closed"})
    events.extend(
        [
            {
                "type": "item.completed",
                "item": {
                    "id": "item_3",
                    "type": "agent_message",
                    "text": final_text,
                },
            },
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 1,
                    "cached_input_tokens": 0,
                    "output_tokens": 1,
                    "reasoning_output_tokens": 0,
                },
            },
        ]
    )
    return (
        "\n".join(json.dumps(event, sort_keys=True, separators=(",", ":")) for event in events)
        + "\n"
    ).encode()


def _run_validator(
    tmp_path: Path,
    events: bytes,
    *,
    stderr: bytes = b"",
) -> subprocess.CompletedProcess[str]:
    event_file = tmp_path / "events.jsonl"
    stderr_file = tmp_path / "stderr"
    event_file.write_bytes(events)
    stderr_file.write_bytes(stderr)
    command = _canary_event_validator_command(
        event_file=str(event_file),
        stderr_file=str(stderr_file),
        nonce=_NONCE,
        marker=CODEX_SUBSCRIPTION_CANARY_OK,
        script_path=_SCRIPT_PATH,
        expected_output=_expected_output(),
    )
    return subprocess.run(
        ["/bin/bash", "-c", command],
        text=True,
        check=False,
        capture_output=True,
        env={**os.environ, "LC_ALL": "C"},
    )
