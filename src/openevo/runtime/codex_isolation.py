"""Closed Codex subscription credential-isolation policy."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import secrets
import shlex
from typing import Final

from openevo.runtime.managed import (
    MANAGED_CODEX_BINARY,
    MANAGED_CODEX_DEFAULT_MODEL,
    MANAGED_CODEX_HOME,
    MANAGED_CODEX_PACKAGE_ROOT,
    MANAGED_CODEX_VERSION,
    MANAGED_HOME,
    MANAGED_PATH,
    MANAGED_WORKSPACE,
)


CODEX_SUBSCRIPTION_POLICY_ID: Final[str] = "openevo.codex-subscription-credential-isolation.v1"
CODEX_SUBSCRIPTION_PERMISSION_PROFILE: Final[str] = "openevo_codex_subscription_v1"
CODEX_SUBSCRIPTION_CODEX_VERSION: Final[str] = MANAGED_CODEX_VERSION
CODEX_SUBSCRIPTION_SANDBOX_BACKEND: Final[str] = "linux-bubblewrap"
CODEX_SUBSCRIPTION_CONTRACT_KEY: Final[str] = "credential_isolation"
CODEX_SUBSCRIPTION_READINESS_KEY: Final[str] = "credential_isolation_receipt"
CODEX_SUBSCRIPTION_CANARY_OK: Final[str] = "openevo-codex-subscription-real-exec-ready-v1"
CODEX_SUBSCRIPTION_REFRESH_PERSISTENCE: Final[str] = "unsupported_read_only_auth_overlay"
_CANARY_RESULT: Final[str] = "isolated"
_CANARY_NONCE_BYTES: Final[int] = 16
_EVOLUTION_ROOT: Final[str] = "/openevo/session/evolution"
_EVOLUTION_SHELL_ENV: Final[tuple[tuple[str, str], ...]] = (
    ("OPENEVO_EVOLUTION_CONTEXT", f"{_EVOLUTION_ROOT}/context.json"),
    ("OPENEVO_MEMORY_FILE", f"{_EVOLUTION_ROOT}/memory.md"),
    ("OPENEVO_SKILLS_DIR", f"{_EVOLUTION_ROOT}/skills"),
    ("OPENEVO_ADAPTER_MERGE_SPEC", f"{_EVOLUTION_ROOT}/adapters.json"),
    ("OPENEVO_AGENT_SYSTEM_FILE", f"{_EVOLUTION_ROOT}/agent_system.md"),
)

_ALLOWED_SUBSCRIPTION_SETTINGS: Final[frozenset[str]] = frozenset(
    {
        "auth_mode",
        "capture_mode",
        "credential_isolation",
        "native_memory_policy",
        "reasoning_effort",
        "reasoning_summary",
    }
)
_FILESYSTEM_POLICY: Final[tuple[tuple[str, str], ...]] = (
    (":minimal", "read"),
    (MANAGED_CODEX_PACKAGE_ROOT, "read"),
    (MANAGED_WORKSPACE, "write"),
    (MANAGED_HOME, "read"),
    (_EVOLUTION_ROOT, "read"),
    (":tmpdir", "write"),
    ("/tmp", "write"),
    (MANAGED_CODEX_HOME, "deny"),
)
_DISABLED_EXECUTION_FEATURES: Final[tuple[str, ...]] = (
    "apps",
    "auth_elicitation",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "code_mode",
    "code_mode_host",
    "code_mode_only",
    "computer_use",
    "deferred_executor",
    "enable_fanout",
    "enable_mcp_apps",
    "exec_permission_approvals",
    "goals",
    "guardian_approval",
    "hooks",
    "image_generation",
    "in_app_browser",
    "multi_agent",
    "multi_agent_v2",
    "network_proxy",
    "plugin_sharing",
    "plugins",
    "remote_plugin",
    "request_permissions_tool",
    "shell_snapshot",
    "shell_zsh_fork",
    "skill_mcp_dependency_install",
    "standalone_web_search",
    "tool_call_mcp_elicitation",
    "tool_suggest",
    "unified_exec_zsh_fork",
    "workspace_dependencies",
)
_POLICY_SPEC: Final[dict[str, object]] = {
    "schema_version": 1,
    "policy_id": CODEX_SUBSCRIPTION_POLICY_ID,
    "permission_profile": CODEX_SUBSCRIPTION_PERMISSION_PROFILE,
    "codex_version": CODEX_SUBSCRIPTION_CODEX_VERSION,
    "default_model": MANAGED_CODEX_DEFAULT_MODEL,
    "sandbox_backend": CODEX_SUBSCRIPTION_SANDBOX_BACKEND,
    "approval_policy": "never",
    "task_network": {
        "owner": "runtime_spec.allow_internet",
        "science_default": True,
    },
    "filesystem": [{"path": path, "access": access} for path, access in _FILESYSTEM_POLICY],
    "shell_environment": {
        "inherit": "none",
        "set": {
            "HOME": MANAGED_HOME,
            "PATH": MANAGED_PATH,
            "TMPDIR": "/tmp",
            **dict(_EVOLUTION_SHELL_ENV),
        },
    },
    "disabled_execution_features": list(_DISABLED_EXECUTION_FEATURES),
    "project_config_trust": "untrusted",
    "credential_parent_view_access": "read-write",
    "credential_auth_overlay_access": "read-only",
    "credential_tool_filesystem_access": "deny",
    "credential_refresh_persistence": CODEX_SUBSCRIPTION_REFRESH_PERSISTENCE,
    "readiness_command": "codex exec",
    "readiness_evidence": "completed_command_execution_event",
    "readiness_canaries": [
        "exact_codex_version",
        "parent_auth_read",
        "tool_direct_auth_read_denied",
        "tool_proc_self_root_auth_read_denied",
        "tool_proc_pid_root_auth_read_denied",
        "tool_sudo_auth_read_denied_or_absent",
        "workspace_write",
        "home_read_only",
        "tmp_write",
    ],
}
CODEX_SUBSCRIPTION_POLICY_SHA256: Final[str] = hashlib.sha256(
    json.dumps(
        _POLICY_SPEC,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()


def codex_subscription_contract() -> dict[str, object]:
    """Return the stable data-only policy/profile identity."""

    return {
        "schema_version": 1,
        "policy_id": CODEX_SUBSCRIPTION_POLICY_ID,
        "policy_sha256": CODEX_SUBSCRIPTION_POLICY_SHA256,
        "permission_profile": CODEX_SUBSCRIPTION_PERMISSION_PROFILE,
        "codex_version": CODEX_SUBSCRIPTION_CODEX_VERSION,
        "default_model": MANAGED_CODEX_DEFAULT_MODEL,
        "sandbox_backend": CODEX_SUBSCRIPTION_SANDBOX_BACKEND,
        "refresh_persistence": CODEX_SUBSCRIPTION_REFRESH_PERSISTENCE,
    }


def codex_subscription_readiness_receipt() -> dict[str, object]:
    """Return the receipt published only after the real-exec canary passes."""

    return {
        **codex_subscription_contract(),
        "status": "passed",
        "canary": CODEX_SUBSCRIPTION_CANARY_OK,
        "evidence": "completed_command_execution_event",
    }


def validate_codex_subscription_surface(
    *,
    settings: Mapping[str, object],
    env: Mapping[str, str],
    mcp_servers: Sequence[object],
) -> None:
    """Reject caller-controlled subscription execution extensions."""

    unknown = sorted(set(settings) - _ALLOWED_SUBSCRIPTION_SETTINGS)
    if unknown:
        names = ", ".join(unknown)
        raise ValueError(
            f"Codex subscription settings contain forbidden execution fields: {names}"
        )
    if env:
        raise ValueError("Codex subscription agent env overrides are forbidden")
    if mcp_servers:
        raise ValueError("Codex subscription MCP servers are forbidden")
    native_memory_policy = settings.get("native_memory_policy")
    if native_memory_policy not in {None, "preserve"}:
        raise ValueError("Codex subscription native_memory_policy must be omitted or 'preserve'")
    supplied = settings.get(CODEX_SUBSCRIPTION_CONTRACT_KEY)
    if supplied is not None and supplied != codex_subscription_contract():
        raise ValueError("Codex subscription credential-isolation identity is invalid")


def validate_codex_subscription_version(version_output: str) -> None:
    """Require the exact release Codex CLI identity."""

    if version_output.strip() != f"codex-cli {CODEX_SUBSCRIPTION_CODEX_VERSION}":
        raise ValueError("Codex subscription CLI version is not the release pin")


def codex_subscription_cli_overrides(
    *,
    allow_internet: bool,
) -> tuple[str, ...]:
    """Return final, highest-precedence Codex 0.144.1 config overrides."""

    if not isinstance(allow_internet, bool):
        raise ValueError("Codex subscription network policy must be Core-owned")
    filesystem = ",".join(
        f"{json.dumps(path)}={json.dumps(access)}" for path, access in _FILESYSTEM_POLICY
    )
    shell_set = ",".join(
        f"{key}={json.dumps(value)}"
        for key, value in (
            ("HOME", MANAGED_HOME),
            ("PATH", MANAGED_PATH),
            ("TMPDIR", "/tmp"),
            *_EVOLUTION_SHELL_ENV,
        )
    )
    web_search = "live" if allow_internet else "disabled"
    overrides = [
        f"default_permissions={json.dumps(CODEX_SUBSCRIPTION_PERMISSION_PROFILE)}",
        (f"permissions.{CODEX_SUBSCRIPTION_PERMISSION_PROFILE}.filesystem={{{filesystem}}}"),
        (
            f"permissions.{CODEX_SUBSCRIPTION_PERMISSION_PROFILE}.network.enabled="
            f"{str(allow_internet).lower()}"
        ),
        'approval_policy="never"',
        "allow_login_shell=false",
        "check_for_update_on_startup=false",
        'cli_auth_credentials_store="file"',
        'forced_login_method="chatgpt"',
        f'shell_environment_policy={{inherit="none",set={{{shell_set}}}}}',
        f'projects.{json.dumps(MANAGED_WORKSPACE)}.trust_level="untrusted"',
        'model_provider="openai"',
        f'web_search="{web_search}"',
        "include_apps_instructions=false",
        "include_collaboration_mode_instructions=false",
        "mcp_servers={}",
        "plugins={}",
        "marketplaces={}",
        "profiles={}",
        "features.shell_tool=true",
        "features.unified_exec=true",
    ]
    overrides.extend(f"features.{feature}=false" for feature in _DISABLED_EXECUTION_FEATURES)
    return tuple(overrides)


def codex_subscription_cli_flags(
    *,
    allow_internet: bool,
) -> tuple[str, ...]:
    """Render the closed CLI and config profile as shell-safe arguments."""

    return (
        "--strict-config",
        "--ignore-user-config",
        "--ignore-rules",
        *(
            f"-c {shlex.quote(override)}"
            for override in codex_subscription_cli_overrides(allow_internet=allow_internet)
        ),
    )


_CANARY_SCRIPT_AUTHORITY_SOURCE: Final[str] = r"""
import hashlib
import os
import stat
import sys

operation, path, *rest = sys.argv[1:]
max_bytes = 65536
flags = getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)

def fail():
    raise SystemExit("Codex subscription canary script authority is invalid")

def identity(metadata, digest):
    return "{}:{}:{}:{}".format(
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        digest,
    )

if operation == "publish":
    if len(rest) != 1:
        fail()
    payload = rest[0].encode("utf-8")
    if not payload or len(payload) > max_bytes or b"\x00" in payload:
        fail()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | flags, 0o500)
        try:
            os.fchmod(fd, 0o500)
            written = 0
            while written < len(payload):
                count = os.write(fd, payload[written:])
                if count <= 0:
                    fail()
                written += count
            os.fsync(fd)
            metadata = os.fstat(fd)
        finally:
            os.close(fd)
    except OSError:
        fail()
    digest = hashlib.sha256(payload).hexdigest()
elif operation == "verify":
    if rest:
        fail()
    try:
        fd = os.open(path, os.O_RDONLY | flags)
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o500
                or before.st_size <= 0
                or before.st_size > max_bytes
            ):
                fail()
            payload = b""
            while len(payload) <= before.st_size:
                chunk = os.read(fd, min(65536, before.st_size - len(payload) + 1))
                if not chunk:
                    break
                payload += chunk
            after = os.fstat(fd)
        finally:
            os.close(fd)
        path_metadata = os.lstat(path)
    except OSError:
        fail()
    if (
        len(payload) != before.st_size
        or (before.st_dev, before.st_ino, before.st_size)
        != (after.st_dev, after.st_ino, after.st_size)
        or (before.st_dev, before.st_ino)
        != (path_metadata.st_dev, path_metadata.st_ino)
        or not stat.S_ISREG(path_metadata.st_mode)
        or stat.S_IMODE(path_metadata.st_mode) != 0o500
    ):
        fail()
    metadata = after
    digest = hashlib.sha256(payload).hexdigest()
else:
    fail()

print(identity(metadata, digest))
"""


_CANARY_INVENTORY_SOURCE: Final[str] = r"""
import os
import stat
import sys

prefix, workspace, home, tmpdir, credential_root, *expected = sys.argv[1:]
expected_paths = set(expected)
observed = set()
for root in (workspace, home, tmpdir, credential_root):
    try:
        entries = os.scandir(root)
        with entries:
            for entry in entries:
                if not entry.name.startswith(prefix):
                    continue
                path = os.path.join(root, entry.name)
                metadata = os.lstat(path)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise SystemExit(
                        "Codex subscription canary inventory is invalid"
                    )
                observed.add(path)
    except OSError:
        raise SystemExit("Codex subscription canary inventory is invalid")
if observed != expected_paths:
    raise SystemExit("Codex subscription canary inventory is invalid")
"""


def codex_subscription_exec_canary_command(
    *,
    model: str = MANAGED_CODEX_DEFAULT_MODEL,
    allow_internet: bool = True,
    nonce: str | None = None,
) -> str:
    """Build the trusted-parent real ``codex exec`` readiness probe."""

    checked_model = _require_cli_value(model, owner="model")
    checked_nonce = _new_or_checked_nonce(nonce)
    retry_nonce = hashlib.sha256(f"{checked_nonce}:retry".encode()).hexdigest()[
        : _CANARY_NONCE_BYTES * 2
    ]
    marker = CODEX_SUBSCRIPTION_CANARY_OK
    auth_file = f"{MANAGED_CODEX_HOME}/auth.json"
    profile_flags = " ".join(codex_subscription_cli_flags(allow_internet=allow_internet))
    attempts: list[dict[str, str]] = []
    for attempt_number, attempt_nonce in enumerate((checked_nonce, retry_nonce), start=1):
        script_id = hashlib.sha256(f"{attempt_nonce}:script".encode()).hexdigest()[:24]
        prefix = f".openevo-{script_id}"
        expected_output = _canary_expected_output(attempt_nonce, marker)
        attempt = {
            "nonce": attempt_nonce,
            "script": f"{MANAGED_WORKSPACE}/{prefix}-probe.sh",
            "event": f"{MANAGED_CODEX_HOME}/{prefix}-events.jsonl",
            "stderr": f"{MANAGED_CODEX_HOME}/{prefix}-stderr",
            "workspace": f"{MANAGED_WORKSPACE}/{prefix}-workspace",
            "home_read": f"{MANAGED_HOME}/{prefix}-read",
            "home_write": f"{MANAGED_HOME}/{prefix}-write",
            "tmp": f"/tmp/{prefix}-write",
            "expected_output": expected_output,
            "number": str(attempt_number),
        }
        attempt["script_content"] = _canary_probe_script(
            auth_file=auth_file,
            workspace_canary=attempt["workspace"],
            home_read_canary=attempt["home_read"],
            home_write_canary=attempt["home_write"],
            tmp_canary=attempt["tmp"],
            nonce=attempt_nonce,
            expected_output=expected_output,
        )
        attempts.append(attempt)
    cleanup_paths = " ".join(
        shlex.quote(path)
        for attempt in attempts
        for path in (
            attempt["script"],
            attempt["event"],
            attempt["stderr"],
            attempt["workspace"],
            attempt["home_read"],
            attempt["home_write"],
            attempt["tmp"],
        )
    )
    lines = [
        "set -eu",
        "umask 077",
        f"cleanup() {{ rm -f -- {cleanup_paths}; }}",
        "trap cleanup EXIT HUP INT TERM",
        (
            f'test "$({shlex.quote(MANAGED_CODEX_BINARY)} --version)" = '
            f"{shlex.quote(f'codex-cli {CODEX_SUBSCRIPTION_CODEX_VERSION}')}"
        ),
        f"test -x {shlex.quote(MANAGED_CODEX_BINARY)}",
        f"test -d {shlex.quote(MANAGED_CODEX_PACKAGE_ROOT)}",
        f"test -r {shlex.quote(auth_file)}",
        f"/bin/cat {shlex.quote(auth_file)} >/dev/null",
        f"rm -f -- {cleanup_paths}",
        "canary_passed=0",
    ]
    for attempt in attempts:
        invocation = f"/bin/sh {shlex.quote(attempt['script'])} {shlex.quote(attempt['nonce'])}"
        prompt = (
            "You must invoke the shell tool exactly once with the exact command "
            "below. Permission denial inside the script is expected and is checked "
            "by the script itself. Do not inspect the script, do not use any other "
            "tool, and report only the command exit status.\n\n"
            f"{invocation}"
        )
        validator = _canary_event_validator_command(
            event_file=attempt["event"],
            stderr_file=attempt["stderr"],
            nonce=attempt["nonce"],
            marker=marker,
            script_path=attempt["script"],
            expected_output=attempt["expected_output"],
        )
        prefix = attempt["script"].rsplit("/", 1)[1].removesuffix("-probe.sh")
        refusal_inventory = _canary_inventory_command(
            prefix=prefix,
            expected_paths=(
                attempt["script"],
                attempt["event"],
                attempt["stderr"],
                attempt["home_read"],
            ),
        )
        success_inventory = _canary_inventory_command(
            prefix=prefix,
            expected_paths=(
                attempt["script"],
                attempt["event"],
                attempt["stderr"],
                attempt["workspace"],
                attempt["home_read"],
                attempt["tmp"],
            ),
        )
        lines.extend(
            [
                'if test "$canary_passed" -eq 0; then',
                (
                    "  script_evidence=$("
                    f"python3 -c {shlex.quote(_CANARY_SCRIPT_AUTHORITY_SOURCE)} "
                    f"publish {shlex.quote(attempt['script'])} "
                    f"{shlex.quote(attempt['script_content'])})"
                ),
                (
                    f"  printf '%s' {shlex.quote(attempt['nonce'])} > "
                    f"{shlex.quote(attempt['home_read'])}"
                ),
                "  set +e",
                (
                    f"  {shlex.quote(MANAGED_CODEX_BINARY)} exec "
                    "--skip-git-repo-check --json --ephemeral "
                    f"--model {shlex.quote(checked_model)} {profile_flags} "
                    f"{shlex.quote(prompt)} < /dev/null "
                    f"> {shlex.quote(attempt['event'])} "
                    f"2> {shlex.quote(attempt['stderr'])}"
                ),
                "  codex_rc=$?",
                "  set -e",
                (
                    '  test "$script_evidence" = "$('
                    f"python3 -c {shlex.quote(_CANARY_SCRIPT_AUTHORITY_SOURCE)} "
                    f'verify {shlex.quote(attempt["script"])})"'
                ),
                '  test "$codex_rc" -eq 0',
                "  set +e",
                f"  {validator}",
                "  validator_rc=$?",
                "  set -e",
                '  if test "$validator_rc" -eq 0; then',
                (
                    f'    test "$(/bin/cat {shlex.quote(attempt["workspace"])})" = '
                    f"{shlex.quote(attempt['nonce'])}"
                ),
                (
                    f'    test "$(/bin/cat {shlex.quote(attempt["tmp"])})" = '
                    f"{shlex.quote(attempt['nonce'])}"
                ),
                f"    test ! -e {shlex.quote(attempt['home_write'])}",
                f"    {success_inventory}",
                "    canary_passed=1",
                '  elif test "$validator_rc" -ne 42; then',
                "    exit 1",
                "  else",
                f"    {refusal_inventory}",
                f"    if test {attempt['number']} -eq 2; then exit 1; fi",
                "  fi",
                "fi",
            ]
        )
    lines.extend(
        [
            'test "$canary_passed" -eq 1',
            f"printf '%s\\n' {shlex.quote(marker)}",
        ]
    )
    return "\n".join(lines)


def _canary_inventory_command(
    *,
    prefix: str,
    expected_paths: Sequence[str],
) -> str:
    args = " ".join(
        shlex.quote(value)
        for value in (
            prefix,
            MANAGED_WORKSPACE,
            MANAGED_HOME,
            "/tmp",
            MANAGED_CODEX_HOME,
            *expected_paths,
        )
    )
    return f"python3 -c {shlex.quote(_CANARY_INVENTORY_SOURCE)} {args}"


def _canary_expected_output(nonce: str, marker: str) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "nonce": nonce,
            "marker": marker,
            "result": _CANARY_RESULT,
            "leak": False,
            "forbidden_file": False,
        },
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canary_probe_script(
    *,
    auth_file: str,
    workspace_canary: str,
    home_read_canary: str,
    home_write_canary: str,
    tmp_canary: str,
    nonce: str,
    expected_output: str,
) -> str:
    return "\n".join(
        [
            "set -eu",
            'test "$#" -eq 1',
            f'test "$1" = {shlex.quote(nonce)}',
            f"auth_file={shlex.quote(auth_file)}",
            f"workspace_canary={shlex.quote(workspace_canary)}",
            f"home_read_canary={shlex.quote(home_read_canary)}",
            f"home_write_canary={shlex.quote(home_write_canary)}",
            f"tmp_canary={shlex.quote(tmp_canary)}",
            'test ! -r "$auth_file"',
            '! /bin/cat "$auth_file" >/dev/null 2>&1',
            "for root_link in /proc/self/root /proc/[0-9]*/root; do",
            '  test -e "$root_link" || continue',
            '  candidate="$root_link$auth_file"',
            '  test ! -r "$candidate"',
            '  ! /bin/cat "$candidate" >/dev/null 2>&1',
            "done",
            "if command -v sudo >/dev/null 2>&1; then",
            '  ! sudo -n /bin/cat "$auth_file" >/dev/null 2>&1',
            "  for root_link in /proc/self/root /proc/[0-9]*/root; do",
            '    test -e "$root_link" || continue',
            '    ! sudo -n /bin/cat "$root_link$auth_file" >/dev/null 2>&1',
            "  done",
            "fi",
            f"printf '%s' {shlex.quote(nonce)} > \"$workspace_canary\"",
            (f'test "$(/bin/cat "$home_read_canary")" = {shlex.quote(nonce)}'),
            ("if (printf '%s' blocked > \"$home_write_canary\") 2>/dev/null; then exit 20; fi"),
            'test ! -e "$home_write_canary"',
            f"printf '%s' {shlex.quote(nonce)} > \"$tmp_canary\"",
            f"printf '%s\\n' {shlex.quote(expected_output)}",
        ]
    )


_CANARY_EVENT_VALIDATOR_SOURCE: Final[str] = r"""
import json
import os
import shlex
import stat
import sys

event_path, stderr_path, nonce, marker, script_path, expected_output = sys.argv[1:]
max_bytes = 262144

def fail():
    raise SystemExit("Codex subscription real-exec evidence is invalid")

def clean_refusal():
    raise SystemExit(42)

def read_regular(path, *, allow_empty):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        try:
            metadata = os.fstat(fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size > max_bytes
                or (not allow_empty and metadata.st_size == 0)
            ):
                fail()
            payload = b""
            while len(payload) <= metadata.st_size:
                chunk = os.read(fd, min(65536, metadata.st_size - len(payload) + 1))
                if not chunk:
                    break
                payload += chunk
            if len(payload) != metadata.st_size:
                fail()
            return payload
        finally:
            os.close(fd)
    except (OSError, ValueError):
        fail()

stderr_payload = read_regular(stderr_path, allow_empty=True)
if stderr_payload not in {b"", b"Reading additional input from stdin...\n"}:
    fail()
payload = read_regular(event_path, allow_empty=False)
if not payload.endswith(b"\n"):
    fail()
try:
    text = payload.decode("utf-8")
except UnicodeError:
    fail()

counts = {
    "thread.started": 0,
    "turn.started": 0,
    "turn.completed": 0,
}
command_started = []
command_completed = []
expected_invocation = ["/bin/sh", script_path, nonce]

def command_matches(command):
    try:
        parsed = shlex.split(command)
    except ValueError:
        return False
    if parsed == expected_invocation:
        return True
    if (
        len(parsed) == 3
        and parsed[0] in {"/bin/bash", "/bin/sh"}
        and parsed[1] in {"-c", "-lc"}
    ):
        try:
            return shlex.split(parsed[2]) == expected_invocation
        except ValueError:
            return False
    return False

for raw_line in text.splitlines():
    if not raw_line or len(raw_line.encode("utf-8")) > max_bytes:
        fail()
    try:
        event = json.loads(raw_line)
    except (json.JSONDecodeError, RecursionError):
        fail()
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        fail()
    event_type = event["type"]
    if event_type in counts:
        counts[event_type] += 1
        continue
    if event_type in {"turn.failed", "error"}:
        fail()
    if event_type not in {"item.started", "item.updated", "item.completed"}:
        fail()
    item = event.get("item")
    if not isinstance(item, dict) or not isinstance(item.get("type"), str):
        fail()
    item_type = item["type"]
    if item_type == "command_execution":
        if event_type == "item.updated":
            fail()
        command = item.get("command")
        if (
            not isinstance(command, str)
            or not command_matches(command)
            or command.count(script_path) != 1
            or command.count(nonce) != 1
            or not isinstance(item.get("id"), str)
            or not item["id"]
        ):
            fail()
        if event_type == "item.started":
            if (
                item.get("status") != "in_progress"
                or item.get("aggregated_output") != ""
                or item.get("exit_code") is not None
            ):
                fail()
            command_started.append((item["id"], command))
        else:
            if (
                item.get("status") != "completed"
                or item.get("exit_code") != 0
                or item.get("aggregated_output") != expected_output + "\n"
            ):
                fail()
            command_completed.append((item["id"], command))
        continue
    if item_type not in {"reasoning", "agent_message"}:
        fail()
    if event_type == "item.updated":
        fail()
    item_text = item.get("text")
    if event_type == "item.completed" and not isinstance(item_text, str):
        fail()

if counts != {"thread.started": 1, "turn.started": 1, "turn.completed": 1}:
    fail()
if not command_started and not command_completed:
    clean_refusal()
if len(command_started) != 1 or command_started != command_completed:
    fail()
try:
    evidence = json.loads(expected_output)
except (json.JSONDecodeError, RecursionError):
    fail()
if evidence != {
    "schema_version": 1,
    "nonce": nonce,
    "marker": marker,
    "result": "isolated",
    "leak": False,
    "forbidden_file": False,
}:
    fail()
"""


def _canary_event_validator_command(
    *,
    event_file: str,
    stderr_file: str,
    nonce: str,
    marker: str,
    script_path: str,
    expected_output: str,
) -> str:
    args = " ".join(
        shlex.quote(value)
        for value in (
            event_file,
            stderr_file,
            nonce,
            marker,
            script_path,
            expected_output,
        )
    )
    return f"python3 -c {shlex.quote(_CANARY_EVENT_VALIDATOR_SOURCE)} {args}"


def _new_or_checked_nonce(nonce: str | None) -> str:
    candidate = secrets.token_hex(_CANARY_NONCE_BYTES) if nonce is None else nonce
    if (
        not isinstance(candidate, str)
        or len(candidate) != _CANARY_NONCE_BYTES * 2
        or any(character not in "0123456789abcdef" for character in candidate)
    ):
        raise ValueError("Codex subscription canary nonce is invalid")
    return candidate


def _require_cli_value(value: str, *, owner: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in value)
    ):
        raise ValueError(f"Codex subscription {owner} is invalid")
    return value


__all__ = [
    "CODEX_SUBSCRIPTION_CANARY_OK",
    "CODEX_SUBSCRIPTION_CODEX_VERSION",
    "CODEX_SUBSCRIPTION_CONTRACT_KEY",
    "CODEX_SUBSCRIPTION_PERMISSION_PROFILE",
    "CODEX_SUBSCRIPTION_POLICY_ID",
    "CODEX_SUBSCRIPTION_POLICY_SHA256",
    "CODEX_SUBSCRIPTION_READINESS_KEY",
    "CODEX_SUBSCRIPTION_REFRESH_PERSISTENCE",
    "CODEX_SUBSCRIPTION_SANDBOX_BACKEND",
    "codex_subscription_cli_flags",
    "codex_subscription_cli_overrides",
    "codex_subscription_contract",
    "codex_subscription_exec_canary_command",
    "codex_subscription_readiness_receipt",
    "validate_codex_subscription_surface",
    "validate_codex_subscription_version",
]
