#!/usr/bin/env python3
"""Deploy the remote agent bridge over SSH and start the self-hosted WebUI."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DESKTOP_ROOT = REPOSITORY_ROOT / "desktop"
DEVELOPMENT_BROWSER_PORT = 5173
SSH_ALIAS_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
SSH_HOST_PATTERN = re.compile(
    r"(?=.{1,253}\Z)[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?"
)
SSH_USER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,31}")
BRANCH_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}")
LOCAL_WEB_LAYER_PATH_PREFIXES = (
    "desktop/",
    "docs/",
    "src/openevo/web_gateway/",
    "tests/",
)
LOCAL_WEB_LAYER_PATHS = frozenset(
    {
        "scripts/dev/development_agent_web_layer.py",
        "scripts/dev/run_remote_agent_development.py",
    }
)
REMOTE_DEVELOPMENT_STATE_ROOT = "$HOME/.openevo/dev-agent"
REMOTE_LIFECYCLE_ACTIONS = frozenset({"status", "logs", "stop"})
SOURCE_ACTIONS = frozenset({"auto", "install", "update", "start"})
SSH_CONNECT_TIMEOUT_SECONDS = 15
REMOTE_COMMAND_TIMEOUT_SECONDS = 600
SOURCE_TRANSFER_TIMEOUT_SECONDS = 180


class LauncherError(RuntimeError):
    """A user-actionable remote development setup failure."""


@dataclass(frozen=True)
class SshConnection:
    options: tuple[str, ...]
    destination: str
    display_name: str


@dataclass(frozen=True)
class SourceBundle:
    path: Path
    sha256: str
    byte_size: int


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _checked_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def ensure_local_ports_available(ports: list[int]) -> None:
    """Fail before remote token rotation when another launcher owns a local port."""
    unavailable: list[int] = []
    for port in sorted(set(ports)):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.25)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                unavailable.append(port)
        finally:
            probe.close()
    if unavailable:
        rendered = ", ".join(str(port) for port in unavailable)
        raise LauncherError(
            f"local development port(s) already in use: {rendered}. "
            "Another OpenEvo launcher may still be running. Keep using that launcher, "
            "or stop it before starting a replacement. The remote daemon was not changed."
        )


def validate_ssh_alias(value: str) -> str:
    alias = value.strip()
    if not SSH_ALIAS_PATTERN.fullmatch(alias):
        raise LauncherError(
            "SSH alias must be a literal ~/.ssh/config Host name containing only "
            "letters, numbers, '.', '_' or '-'"
        )
    return alias


def validate_ssh_host(value: str) -> str:
    host = value.strip()
    if not SSH_HOST_PATTERN.fullmatch(host) or ".." in host:
        raise LauncherError("SSH host must be a plain DNS name or IPv4 address")
    return host


def validate_ssh_user(value: str) -> str:
    user = value.strip()
    if not SSH_USER_PATTERN.fullmatch(user):
        raise LauncherError("SSH user contains unsupported characters")
    return user


def resolve_ssh_connection(args: argparse.Namespace) -> SshConnection:
    if args.ssh_alias and (args.host or args.user or args.ssh_port != 22):
        raise LauncherError(
            "use either --ssh-alias or --host/--user/--ssh-port, not both"
        )
    if args.ssh_alias:
        alias = validate_ssh_alias(args.ssh_alias)
        return SshConnection(options=(), destination=alias, display_name=alias)
    if not args.host:
        raise LauncherError("provide --ssh-alias or --host")
    if not args.user:
        raise LauncherError("--user is required when --host is used")
    host = validate_ssh_host(args.host)
    user = validate_ssh_user(args.user)
    return SshConnection(
        options=("-p", str(args.ssh_port)),
        destination=f"{user}@{host}",
        display_name=f"{user}@{host}:{args.ssh_port}",
    )


def validate_branch(value: str) -> str:
    branch = value.strip()
    if (
        not BRANCH_PATTERN.fullmatch(branch)
        or ".." in branch
        or "@{" in branch
        or branch.endswith(("/", "."))
        or branch.startswith("-")
    ):
        raise LauncherError("Git branch name is not accepted by the development launcher")
    return branch


def _git_output(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise LauncherError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout.strip()


def changed_checkout_paths() -> tuple[str, ...]:
    """Return tracked and untracked paths without parsing porcelain rename syntax."""

    tracked = _git_output("diff", "--name-only", "HEAD").splitlines()
    untracked = _git_output("ls-files", "--others", "--exclude-standard").splitlines()
    return tuple(sorted({path.replace("\\", "/") for path in [*tracked, *untracked] if path}))


def validate_checkout_for_deployment(*, web_layer: bool, deploy_only: bool) -> tuple[str, ...]:
    changed = changed_checkout_paths()
    if not changed:
        return ()
    local_web_development = web_layer and not deploy_only
    unsafe = tuple(
        path
        for path in changed
        if not local_web_development
        or not (
            path in LOCAL_WEB_LAYER_PATHS
            or path.startswith(LOCAL_WEB_LAYER_PATH_PREFIXES)
        )
    )
    if unsafe:
        details = ", ".join(unsafe[:8])
        if len(unsafe) > 8:
            details += f", and {len(unsafe) - 8} more"
        raise LauncherError(
            "the local checkout contains uncommitted changes used by the remote daemon "
            f"({details}); commit them before deployment"
        )
    return changed


def resolve_branch(explicit: str | None) -> str:
    branch = explicit or _git_output("branch", "--show-current")
    if not branch:
        raise LauncherError("detached HEAD is unsupported; pass --branch explicitly")
    return validate_branch(branch)


def _ssh_transport_options() -> list[str]:
    return [
        "-o",
        f"ConnectTimeout={SSH_CONNECT_TIMEOUT_SECONDS}",
        "-o",
        "ServerAliveInterval=10",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "TCPKeepAlive=yes",
    ]


def build_remote_source_probe_script() -> str:
    return f"""\
set -eu
state_root="{REMOTE_DEVELOPMENT_STATE_ROOT}"
source_root="$state_root/source"
source_marker="$state_root/managed-source-v1"

if [ ! -e "$source_root" ]; then
  printf 'absent\n'
elif [ ! -f "$source_marker" ] || [ ! -d "$source_root/.git" ]; then
  printf 'unrecognized\n'
else
  commit="$(git -C "$source_root" rev-parse HEAD 2>/dev/null || true)"
  case "$commit" in
    ''|*[!0-9a-f]*) printf 'invalid\n' ;;
    *) printf 'managed:%s\n' "$commit" ;;
  esac
fi
"""


def probe_remote_source_commit(
    ssh_binary: str,
    connection: SshConnection,
) -> str | None:
    result = _run_remote_capture(
        ssh_binary,
        connection,
        build_remote_source_probe_script(),
        timeout=30,
    ).strip()
    if result == "absent":
        return None
    if result.startswith("managed:"):
        commit = result.removeprefix("managed:")
        if re.fullmatch(r"[0-9a-f]{40,64}", commit):
            return commit
    if result == "unrecognized":
        raise LauncherError(
            "remote source path exists but is not owned by the OpenEvo launcher"
        )
    raise LauncherError(f"remote source probe returned an invalid result: {result!r}")


def _git_is_ancestor(older_commit: str, newer_commit: str) -> bool:
    known = subprocess.run(
        ["git", "cat-file", "-e", f"{older_commit}^{{commit}}"],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        timeout=60,
    )
    if known.returncode != 0:
        return False
    completed = subprocess.run(
        ["git", "merge-base", "--is-ancestor", older_commit, newer_commit],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    detail = completed.stderr.strip() or completed.stdout.strip()
    raise LauncherError(f"could not compare local and installed commits: {detail}")


@contextmanager
def create_source_bundle(
    *,
    branch: str,
    expected_commit: str,
    remote_commit: str | None,
) -> Iterator[SourceBundle]:
    temporary_directory = tempfile.TemporaryDirectory(prefix="openevo-source-bundle-")
    try:
        bundle_path = Path(temporary_directory.name) / f"openevo-{expected_commit}.bundle"
        revisions = [branch]
        if remote_commit is not None and _git_is_ancestor(remote_commit, expected_commit):
            revisions = [f"^{remote_commit}", branch]
        completed = subprocess.run(
            ["git", "bundle", "create", os.fspath(bundle_path), *revisions],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise LauncherError(f"local source bundle creation failed: {detail}")
        yield SourceBundle(
            path=bundle_path,
            sha256=_sha256_file(bundle_path),
            byte_size=bundle_path.stat().st_size,
        )
    finally:
        temporary_directory.cleanup()


def upload_source_bundle(
    ssh_binary: str,
    connection: SshConnection,
    bundle: SourceBundle,
    *,
    expected_commit: str,
) -> None:
    remote_command = (
        "set -eu; umask 077; "
        'mkdir -p "$HOME/.openevo/dev-agent/incoming"; '
        f'cat > "$HOME/.openevo/dev-agent/incoming/source-{expected_commit}.bundle"'
    )
    try:
        with bundle.path.open("rb") as source:
            completed = subprocess.run(
                [
                    ssh_binary,
                    *connection.options,
                    *_ssh_transport_options(),
                    connection.destination,
                    remote_command,
                ],
                stdin=source,
                check=False,
                timeout=SOURCE_TRANSFER_TIMEOUT_SECONDS,
            )
    except subprocess.TimeoutExpired as exc:
        raise LauncherError(
            f"source upload exceeded {SOURCE_TRANSFER_TIMEOUT_SECONDS} seconds"
        ) from exc
    if completed.returncode != 0:
        raise LauncherError(
            f"source upload failed with SSH exit code {completed.returncode}"
        )


def build_remote_lifecycle_script(*, action: str, tail_lines: int = 200) -> str:
    """Build a bounded management command for the remote development stack.

    The shape follows nanobot's explicit gateway status/logs/stop management,
    while retaining OpenEvo's stricter process-command ownership check before a
    signal is sent. It deliberately does not update the checkout or rotate
    authentication tokens.
    """

    if action not in REMOTE_LIFECYCLE_ACTIONS:
        raise LauncherError(f"unsupported remote lifecycle action: {action}")
    if not 1 <= tail_lines <= 2_000:
        raise LauncherError("--tail must be between 1 and 2000")

    common = f"""\
set -eu
umask 077

state_root=\"{REMOTE_DEVELOPMENT_STATE_ROOT}\"
daemon_pid_file=\"$state_root/daemon.pid\"
daemon_log_file=\"$state_root/daemon.log\"
web_pid_file=\"$state_root/web-layer.pid\"
web_log_file=\"$state_root/web-layer.log\"

process_status() {{
  label=$1
  pid_file=$2
  marker=$3
  log_file=$4
  legacy_marker=${{5:-$marker}}
  if [ ! -f \"$pid_file\" ]; then
    printf '%s: stopped (no managed PID receipt)\\n' \"$label\"
    printf '  log: %s\\n' \"$log_file\"
    return
  fi
  pid=\"$(cat \"$pid_file\" 2>/dev/null || true)\"
  case \"$pid\" in
    ''|*[!0-9]*)
      printf '%s: stale (invalid managed PID receipt)\\n' \"$label\"
      printf '  log: %s\\n' \"$log_file\"
      return
      ;;
  esac
  if ! kill -0 \"$pid\" 2>/dev/null; then
    printf '%s: stopped (stale PID %s)\\n' \"$label\" \"$pid\"
    printf '  log: %s\\n' \"$log_file\"
    return
  fi
  command_line=\"$(tr '\\000' ' ' < \"/proc/$pid/cmdline\" 2>/dev/null || true)\"
  case \"$command_line\" in
    *\"$marker\"*|*\"$legacy_marker\"*)
      printf '%s: running\\n' \"$label\"
      printf '  pid: %s\\n' \"$pid\"
      printf '  log: %s\\n' \"$log_file\"
      ;;
    *)
      printf '%s: unsafe receipt (PID %s belongs to another command)\\n' \"$label\" \"$pid\"
      printf '  log: %s\\n' \"$log_file\"
      ;;
  esac
}}
"""

    if action == "status":
        return common + """
printf 'OpenEvo remote development stack\\n'
printf 'state: %s\\n' "$state_root"
process_status "daemon" "$daemon_pid_file" "openevo.daemon.product_app" "$daemon_log_file" "scripts/dev/live_agent_daemon.py"
process_status "web-layer" "$web_pid_file" "openevo.web_gateway.product_app" "$web_log_file" "scripts/dev/development_agent_web_layer.py"
if [ -d "$state_root/source/.git" ]; then
  source_commit="$(git -C "$state_root/source" rev-parse --short=12 HEAD 2>/dev/null || true)"
  if [ -n "$source_commit" ]; then printf 'source commit: %s\\n' "$source_commit"; fi
fi
"""

    if action == "logs":
        return common + f"""
printf 'OpenEvo remote development logs (last {tail_lines} lines each)\\n'
printf '\\n[daemon] %s\\n' "$daemon_log_file"
if [ -f "$daemon_log_file" ]; then tail -n {tail_lines} "$daemon_log_file"; else printf 'no daemon log yet\\n'; fi
printf '\\n[web-layer] %s\\n' "$web_log_file"
if [ -f "$web_log_file" ]; then tail -n {tail_lines} "$web_log_file"; else printf 'no Web Layer log yet\\n'; fi
"""

    return common + """
stop_managed_process() {
  label=$1
  pid_file=$2
  marker=$3
  legacy_marker=${4:-$marker}
  if [ ! -f "$pid_file" ]; then
    printf '%s: already stopped\\n' "$label"
    return
  fi
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  case "$pid" in
    ''|*[!0-9]*)
      printf '%s: refusing invalid managed PID receipt\\n' "$label" >&2
      exit 41
      ;;
  esac
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$pid_file"
    printf '%s: removed stale PID receipt\\n' "$label"
    return
  fi
  command_line="$(tr '\\000' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
  case "$command_line" in
    *"$marker"*|*"$legacy_marker"*) ;;
    *)
      printf '%s: refusing to signal PID %s because it is not the managed process\\n' "$label" "$pid" >&2
      exit 42
      ;;
  esac
  kill "$pid"
  wait_count=0
  while kill -0 "$pid" 2>/dev/null && [ "$wait_count" -lt 40 ]; do
    wait_count=$((wait_count + 1))
    sleep 0.25
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill -KILL "$pid"
    sleep 0.25
  fi
  if kill -0 "$pid" 2>/dev/null; then
    printf '%s: PID %s did not stop\\n' "$label" "$pid" >&2
    exit 43
  fi
  rm -f "$pid_file"
  printf '%s: stopped\\n' "$label"
}

# Stop the HTTP/WebUI edge before its daemon authority.
stop_managed_process "web-layer" "$web_pid_file" "openevo.web_gateway.product_app" "scripts/dev/development_agent_web_layer.py"
stop_managed_process "daemon" "$daemon_pid_file" "openevo.daemon.product_app" "scripts/dev/live_agent_daemon.py"
"""


def build_remote_script(
    *,
    branch: str,
    expected_commit: str,
    token: str,
    remote_port: int,
    evolution_model: str,
    source_bundle_sha256: str = "",
    prepare_runtime: bool = True,
    start_services: bool = True,
    self_hosted_webui: bool = False,
    web_session_token: str = "",
    web_bootstrap_token: str = "",
    remote_web_port: int = 8788,
    browser_endpoint: str = "http://127.0.0.1:8765",
) -> str:
    values = {
        "branch": branch,
        "expected_commit": expected_commit,
        "source_bundle_sha256": source_bundle_sha256,
        "prepare_runtime": "1" if prepare_runtime else "0",
        "start_services": "1" if start_services else "0",
        "token": token,
        "remote_port": str(remote_port),
        "evolution_model": evolution_model,
        "web_session_token": web_session_token,
        "web_bootstrap_token": web_bootstrap_token,
        "remote_web_port": str(remote_web_port),
        "browser_endpoint": browser_endpoint,
    }
    quoted = {key: shlex.quote(value) for key, value in values.items()}
    webui_script = ""
    if self_hosted_webui:
        webui_script = f"""
web_pid_file="$state_root/web-layer.pid"
web_log_file="$state_root/web-layer.log"
web_session_token={quoted['web_session_token']}
web_bootstrap_token={quoted['web_bootstrap_token']}
remote_web_port={quoted['remote_web_port']}
browser_endpoint={quoted['browser_endpoint']}

if [ -f "$web_pid_file" ]; then
  old_web_pid="$(cat "$web_pid_file" 2>/dev/null || true)"
  case "$old_web_pid" in
    ''|*[!0-9]*) old_web_pid='' ;;
  esac
  if [ -n "$old_web_pid" ] && kill -0 "$old_web_pid" 2>/dev/null; then
    old_web_command="$(tr '\\000' ' ' < "/proc/$old_web_pid/cmdline" 2>/dev/null || true)"
    case "$old_web_command" in
      *openevo.web_gateway.product_app*|*scripts/dev/development_agent_web_layer.py*)
        kill "$old_web_pid"
        web_wait_count=0
        while kill -0 "$old_web_pid" 2>/dev/null && [ "$web_wait_count" -lt 20 ]; do
          web_wait_count=$((web_wait_count + 1))
          sleep 0.25
        done
        ;;
      *) echo "Refusing to stop PID $old_web_pid because it is not the managed Web Layer." >&2; exit 29 ;;
    esac
  fi
fi

nohup env \\
  "OPENEVO_DEV_AGENT_TOKEN=$agent_token" \\
  "OPENEVO_DEV_WEB_SESSION_TOKEN=$web_session_token" \\
  "OPENEVO_DEV_WEB_BOOTSTRAP_TOKEN=$web_bootstrap_token" \\
  "$uv_bin" run --frozen --no-sync --python 3.11 python \\
  -m openevo.web_gateway.product_app \\
  --daemon-endpoint "http://127.0.0.1:$remote_port" \\
  --browser-endpoint "$browser_endpoint" \\
  --source-commit "$expected_commit" \\
  --static-root "$source_root/src/openevo/web_gateway/static" \\
  --host 127.0.0.1 --port "$remote_web_port" \\
  >"$web_log_file" 2>&1 </dev/null &
web_pid=$!
printf '%s\\n' "$web_pid" > "$web_pid_file"

web_ready=0
web_attempt=0
while [ "$web_attempt" -lt 60 ]; do
  web_attempt=$((web_attempt + 1))
  if curl --connect-timeout 1 --max-time 2 --silent --fail \\
    "http://127.0.0.1:$remote_web_port/openevo" >/dev/null 2>&1; then
    web_ready=1
    break
  fi
  if ! kill -0 "$web_pid" 2>/dev/null; then break; fi
  sleep 1
done
if [ "$web_ready" -ne 1 ]; then
  echo "The self-hosted Web Layer did not become ready. Recent log output:" >&2
  tail -n 40 "$web_log_file" >&2 || true
  exit 30
fi
echo "Remote OpenEvo Web Layer is ready on loopback port $remote_web_port."
"""
    return f"""\
set -eu
umask 077

branch={quoted['branch']}
expected_commit={quoted['expected_commit']}
source_bundle_sha256={quoted['source_bundle_sha256']}
prepare_runtime={quoted['prepare_runtime']}
start_services={quoted['start_services']}
agent_token={quoted['token']}
remote_port={quoted['remote_port']}
evolution_model={quoted['evolution_model']}
state_root="$HOME/.openevo/dev-agent"
source_root="$state_root/source"
source_marker="$state_root/managed-source-v1"
source_bundle="$state_root/incoming/source-$expected_commit.bundle"
pid_file="$state_root/daemon.pid"
log_file="$state_root/daemon.log"

mkdir -p "$state_root"

installed_commit=''
if [ -e "$source_root" ]; then
  if [ ! -f "$source_marker" ] || [ ! -d "$source_root/.git" ]; then
    echo "Refusing to modify an unrecognized path: $source_root" >&2
    exit 20
  fi
  installed_commit="$(git -C "$source_root" rev-parse HEAD 2>/dev/null || true)"
  if [ -n "$(git -C "$source_root" status --porcelain)" ]; then
    echo "Managed checkout has local changes; refusing to run non-committed source." >&2
    exit 21
  fi
fi

if [ "$installed_commit" = "$expected_commit" ]; then
  echo "[remote 1/4] Installed OpenEvo source already matches $expected_commit; skipping update."
elif [ -z "$source_bundle_sha256" ]; then
  echo "Installed source does not match $expected_commit and no local source bundle was uploaded." >&2
  exit 26
elif [ ! -f "$source_bundle" ]; then
  echo "Uploaded source bundle is missing: $source_bundle" >&2
  exit 31
else
  if ! command -v sha256sum >/dev/null 2>&1; then
    echo "sha256sum is required to verify the uploaded source bundle." >&2
    exit 32
  fi
  actual_bundle_sha256="$(sha256sum "$source_bundle" | awk '{{print $1}}')"
  if [ "$actual_bundle_sha256" != "$source_bundle_sha256" ]; then
    echo "Uploaded source bundle digest does not match the local receipt." >&2
    exit 33
  fi
  if [ -e "$source_root" ]; then
    git -C "$source_root" bundle verify "$source_bundle" >/dev/null
  else
    verify_root="$state_root/incoming/bundle-verifier.git"
    if [ ! -d "$verify_root" ]; then
      git init --quiet --bare "$verify_root"
    fi
    git -C "$verify_root" bundle verify "$source_bundle" >/dev/null
  fi
  if [ ! -e "$source_root" ]; then
    echo "[remote 1/4] Installing the locally delivered OpenEvo source..."
    install_parent="$(mktemp -d "$state_root/incoming/install.XXXXXX")"
    git clone --branch "$branch" --single-branch "$source_bundle" "$install_parent/source"
    if [ -e "$source_root" ]; then
      echo "Another launcher created $source_root during installation; refusing to replace it." >&2
      exit 37
    fi
    mv "$install_parent/source" "$source_root"
    rmdir "$install_parent"
    printf '%s\n' 'managed by scripts/dev/run_remote_agent_development.py' > "$source_marker"
  elif [ ! -f "$source_marker" ] || [ ! -d "$source_root/.git" ]; then
    echo "Refusing to modify an unrecognized path: $source_root" >&2
    exit 20
  else
    echo "[remote 1/4] Updating from the locally delivered OpenEvo source bundle..."
    git -C "$source_root" fetch "$source_bundle" \
      "refs/heads/$branch:refs/remotes/openevo-local/$branch"
    current_branch="$(git -C "$source_root" branch --show-current)"
    if [ "$current_branch" != "$branch" ]; then
      if git -C "$source_root" show-ref --verify --quiet "refs/heads/$branch"; then
        git -C "$source_root" switch "$branch"
      else
        git -C "$source_root" switch --create "$branch" \
          --track "refs/remotes/openevo-local/$branch"
      fi
    fi
    git -C "$source_root" merge --ff-only "refs/remotes/openevo-local/$branch"
  fi
  rm -f "$source_bundle"
fi

if [ ! -f "$source_marker" ] || [ ! -d "$source_root/.git" ]; then
  echo "Refusing to modify an unrecognized path: $source_root" >&2
  exit 20
fi

# The managed checkout is intentionally detached from every network remote.
if git -C "$source_root" remote get-url origin >/dev/null 2>&1; then
  git -C "$source_root" remote remove origin
fi

deployed_commit="$(git -C "$source_root" rev-parse HEAD)"
if [ "$deployed_commit" != "$expected_commit" ]; then
  echo "The locally delivered source did not activate commit $expected_commit." >&2
  exit 26
fi

runtime_marker="$state_root/runtime-commit-v1"
runtime_commit="$(cat "$runtime_marker" 2>/dev/null || true)"
if [ "$runtime_commit" = "$expected_commit" ]; then
  echo "[remote 2/4] Runtime already prepared for $expected_commit; skipping dependency sync."
elif [ "$prepare_runtime" -ne 1 ]; then
  echo "The runtime is not prepared for commit $expected_commit; run --source-action install or update first." >&2
  exit 35
else
  echo "[remote 2/4] Preparing uv and Python 3.11..."
fi

uv_bin="$(command -v uv || true)"
if [ -z "$uv_bin" ]; then
  for candidate in \
    "$HOME/.local/bin/uv" \
    "$HOME/.cargo/bin/uv"; do
    if [ -x "$candidate" ]; then
      uv_bin="$candidate"
      break
    fi
  done
fi
if [ -z "$uv_bin" ]; then
  if [ "$prepare_runtime" -ne 1 ]; then
    echo "uv is unavailable; run --source-action install or update first." >&2
    exit 22
  fi
  if ! command -v curl >/dev/null 2>&1; then
    echo "curl is required to install uv automatically." >&2
    exit 22
  fi
  curl --connect-timeout 15 --max-time 120 -LsSf https://astral.sh/uv/install.sh | sh
  for candidate in \
    "$HOME/.local/bin/uv" \
    "$HOME/.cargo/bin/uv"; do
    if [ -x "$candidate" ]; then
      uv_bin="$candidate"
      break
    fi
  done
  if [ -z "$uv_bin" ]; then
    echo "uv installation completed but the uv executable was not found." >&2
    exit 23
  fi
fi

cd "$source_root"
if [ "$runtime_commit" != "$expected_commit" ]; then
  if ! command -v timeout >/dev/null 2>&1; then
    echo "timeout is required for bounded dependency installation." >&2
    exit 36
  fi
  timeout 300 "$uv_bin" sync --frozen --python 3.11
  runtime_marker_tmp="$runtime_marker.tmp.$$"
  printf '%s\n' "$expected_commit" > "$runtime_marker_tmp"
  mv "$runtime_marker_tmp" "$runtime_marker"
fi

if [ "$start_services" -ne 1 ]; then
  echo "Remote OpenEvo source and runtime are installed at commit $deployed_commit."
  exit 0
fi

codex_bin="$(command -v codex || true)"
if [ -z "$codex_bin" ] && command -v bash >/dev/null 2>&1; then
  codex_bin="$(bash -lc 'command -v codex' 2>/dev/null || true)"
fi
if [ -z "$codex_bin" ]; then
  for candidate in \
    "$HOME/.local/bin/codex" \
    "$HOME/.npm-global/bin/codex" \
    /usr/local/bin/codex \
    /usr/bin/codex \
    "$HOME"/.nvm/versions/node/*/bin/codex; do
    if [ -x "$candidate" ]; then
      codex_bin="$candidate"
      break
    fi
  done
fi
case "$codex_bin" in
  /*) ;;
  *) codex_bin='' ;;
esac
if [ -z "$codex_bin" ] || [ ! -x "$codex_bin" ]; then
  echo "Codex CLI was not found in the non-interactive SSH environment." >&2
  echo "Log in normally and run: command -v codex" >&2
  exit 27
fi
codex_dir="$(dirname "$codex_bin")"
if ! timeout 30 env "PATH=$codex_dir:$PATH" "$codex_bin" login status >/dev/null 2>&1; then
  echo "Codex CLI is installed but is not logged in for remote user $(id -un)." >&2
  echo "Run 'codex login --device-auth' as that same remote user, then retry." >&2
  exit 28
fi

echo "[remote 3/4] Restarting the development daemon..."
if [ -f "$pid_file" ]; then
  old_pid="$(cat "$pid_file" 2>/dev/null || true)"
  case "$old_pid" in
    ''|*[!0-9]*) old_pid='' ;;
  esac
  if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
    old_command="$(tr '\\000' ' ' < "/proc/$old_pid/cmdline" 2>/dev/null || true)"
    case "$old_command" in
      *openevo.daemon.product_app*|*scripts/dev/live_agent_daemon.py*)
        kill "$old_pid"
        wait_count=0
        while kill -0 "$old_pid" 2>/dev/null && [ "$wait_count" -lt 20 ]; do
          wait_count=$((wait_count + 1))
          sleep 0.25
        done
        ;;
      *)
        echo "Refusing to stop PID $old_pid because it is not the managed daemon." >&2
        exit 24
        ;;
    esac
  fi
fi

nohup env \
  "PATH=$codex_dir:$PATH" \
  "OPENEVO_DEV_AGENT_TOKEN=$agent_token" \
  "OPENEVO_DEV_EVOLUTION_MODEL=$evolution_model" \
  "$uv_bin" run --frozen --no-sync --python 3.11 python \
  -m openevo.daemon.product_app \
  --port "$remote_port" --codex-binary "$codex_bin" \
  >"$log_file" 2>&1 </dev/null &
daemon_pid=$!
printf '%s\n' "$daemon_pid" > "$pid_file"

echo "[remote 4/4] Waiting for daemon readiness..."
ready=0
attempt=0
while [ "$attempt" -lt 60 ]; do
  attempt=$((attempt + 1))
  if command -v curl >/dev/null 2>&1 && \
     curl --connect-timeout 1 --max-time 2 --silent --fail \
       --header "Authorization: Bearer $agent_token" \
       "http://127.0.0.1:$remote_port/openevo-dev-agent/health" \
       >/dev/null 2>&1; then
    ready=1
    break
  fi
  if ! kill -0 "$daemon_pid" 2>/dev/null; then
    break
  fi
  sleep 1
done

if [ "$ready" -ne 1 ]; then
  echo "The daemon did not become ready. Recent remote log output:" >&2
  tail -n 40 "$log_file" >&2 || true
  exit 25
fi

echo "Remote development daemon is ready on loopback port $remote_port."
{webui_script}
"""


def _run_remote_capture(
    ssh_binary: str,
    connection: SshConnection,
    script: str,
    *,
    timeout: int = REMOTE_COMMAND_TIMEOUT_SECONDS,
) -> str:
    try:
        completed = subprocess.run(
            [
                ssh_binary,
                *connection.options,
                *_ssh_transport_options(),
                connection.destination,
                "sh",
                "-s",
            ],
            input=script,
            text=True,
            check=False,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise LauncherError(f"remote SSH command exceeded {timeout} seconds") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise LauncherError(
            f"remote command failed with exit code {completed.returncode}: {detail}"
        )
    return completed.stdout


def _run_remote(ssh_binary: str, connection: SshConnection, script: str) -> None:
    try:
        completed = subprocess.run(
            [
                ssh_binary,
                *connection.options,
                *_ssh_transport_options(),
                connection.destination,
                "sh",
                "-s",
            ],
            input=script,
            text=True,
            check=False,
            timeout=REMOTE_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise LauncherError(
            f"remote deployment exceeded {REMOTE_COMMAND_TIMEOUT_SECONDS} seconds"
        ) from exc
    if completed.returncode != 0:
        raise LauncherError(
            f"remote deployment failed with exit code {completed.returncode}"
        )


def _start_tunnel(
    ssh_binary: str,
    connection: SshConnection,
    local_port: int,
    remote_port: int,
) -> subprocess.Popen[str]:
    command = [
        ssh_binary,
        *connection.options,
        *_ssh_transport_options(),
        "-o",
        "ExitOnForwardFailure=yes",
        "-N",
        "-L",
        f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}",
        connection.destination,
    ]
    tunnel = subprocess.Popen(command, text=True)
    time.sleep(0.8)
    if tunnel.poll() is not None:
        raise LauncherError(
            "SSH tunnel exited immediately; check the alias and whether the local port is already in use"
        )
    return tunnel


def _wait_for_local_health(local_port: int, token: str) -> None:
    request = urllib.request.Request(
        f"http://127.0.0.1:{local_port}/openevo-dev-agent/health",
        headers={"Authorization": f"Bearer {token}"},
    )
    last_error = "no response"
    for _ in range(30):
        try:
            with urllib.request.urlopen(request, timeout=1) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload == {"schema_version": "1", "status": "ready"}:
                return
            last_error = f"unexpected health response: {payload!r}"
        except (OSError, ValueError, urllib.error.URLError) as exc:
            last_error = str(exc)
        time.sleep(0.2)
    raise LauncherError(f"local SSH tunnel health check failed: {last_error}")


def _wait_for_local_webui(local_port: int) -> None:
    url = f"http://127.0.0.1:{local_port}/openevo"
    last_error = "no response"
    for _ in range(50):
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                body = response.read(4096).decode("utf-8", errors="replace")
            if response.status == 200 and "<title>OpenEvo</title>" in body:
                return
            last_error = f"unexpected response status/body from {url}"
        except (OSError, urllib.error.URLError) as exc:
            last_error = str(exc)
        time.sleep(0.2)
    raise LauncherError(f"local OpenEvo WebUI health check failed: {last_error}")


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Deploy the real development Codex bridge through a system OpenSSH "
            "alias, create a local tunnel, and start the Vite product UI"
        )
    )
    parser.add_argument(
        "--ssh-alias",
        default=os.environ.get("OPENEVO_DEV_SSH_ALIAS"),
        help="literal Host alias from ~/.ssh/config (or OPENEVO_DEV_SSH_ALIAS)",
    )
    parser.add_argument(
        "--host",
        help="development server DNS name or IPv4 address (alternative to --ssh-alias)",
    )
    parser.add_argument(
        "--user",
        help="SSH login user; required with --host",
    )
    parser.add_argument("--ssh-port", type=_checked_port, default=22)
    parser.add_argument(
        "--branch",
        help="committed local branch to deliver; defaults to the current branch",
    )
    parser.add_argument(
        "--source-action",
        choices=sorted(SOURCE_ACTIONS),
        default="auto",
        help=(
            "auto installs or updates then starts; install/update prepare source and "
            "runtime without starting; start requires that exact commit to be prepared"
        ),
    )
    parser.add_argument("--local-port", type=_checked_port, default=8765)
    parser.add_argument("--remote-port", type=_checked_port, default=8787)
    parser.add_argument("--web-port", type=_checked_port, default=8766)
    parser.add_argument("--remote-web-port", type=_checked_port, default=8788)
    parser.add_argument(
        "--web-layer",
        action="store_true",
        help="run the local WebUI API bridge in front of the daemon",
    )
    parser.add_argument(
        "--self-hosted-webui",
        action="store_true",
        help=(
            "serve the OpenEvo WebUI and v2 Web Layer beside the remote "
            "daemon, using one local SSH tunnel"
        ),
    )
    parser.add_argument("--evolution-model", default="gpt-5.5")
    parser.add_argument(
        "--deploy-only",
        action="store_true",
        help="deploy and verify the daemon without opening a tunnel or Vite",
    )
    parser.add_argument(
        "--browser-e2e",
        action="store_true",
        help=(
            "run the real Playwright product acceptance path against the self-hosted "
            "WebUI, then close the local tunnel"
        ),
    )
    lifecycle = parser.add_mutually_exclusive_group()
    lifecycle.add_argument(
        "--status",
        action="store_true",
        help="show the managed remote daemon and Web Layer lifecycle state",
    )
    lifecycle.add_argument(
        "--logs",
        action="store_true",
        help="show bounded recent logs for the managed remote daemon and Web Layer",
    )
    lifecycle.add_argument(
        "--stop",
        action="store_true",
        help="stop only the remote processes owned by this development launcher",
    )
    parser.add_argument(
        "--tail",
        type=int,
        default=200,
        help="number of lines per process for --logs (1-2000)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source_only = args.source_action in {"install", "update"}
    if args.web_layer and args.self_hosted_webui:
        raise LauncherError("--web-layer and --self-hosted-webui are mutually exclusive")
    if args.browser_e2e and not args.self_hosted_webui:
        raise LauncherError("--browser-e2e requires --self-hosted-webui")
    if args.browser_e2e and args.deploy_only:
        raise LauncherError("--browser-e2e cannot be combined with --deploy-only")
    if source_only and (args.browser_e2e or args.deploy_only):
        raise LauncherError(
            f"--source-action {args.source_action} cannot be combined with startup options"
        )
    lifecycle_action = next(
        (name for name in ("status", "logs", "stop") if getattr(args, name)),
        None,
    )
    if lifecycle_action is not None:
        if args.browser_e2e or args.deploy_only or args.source_action != "auto":
            raise LauncherError(
                f"--{lifecycle_action} cannot be combined with deployment or browser acceptance"
            )
        connection = resolve_ssh_connection(args)
        ssh_binary = shutil.which("ssh")
        if not ssh_binary:
            raise LauncherError("system OpenSSH client was not found")
        print(
            "Managing the OpenEvo remote development stack through SSH "
            f"{connection.display_name}...",
            flush=True,
        )
        _run_remote(
            ssh_binary,
            connection,
            build_remote_lifecycle_script(
                action=lifecycle_action,
                tail_lines=args.tail,
            ),
        )
        return 0
    if not args.deploy_only and not source_only:
        local_ports = [args.local_port]
        if not args.self_hosted_webui:
            local_ports.append(DEVELOPMENT_BROWSER_PORT)
        if args.web_layer:
            local_ports.append(args.web_port)
        ensure_local_ports_available(local_ports)
    connection = resolve_ssh_connection(args)
    branch = resolve_branch(args.branch)
    local_only_changes = validate_checkout_for_deployment(
        web_layer=args.web_layer and not args.self_hosted_webui,
        deploy_only=args.deploy_only or source_only,
    )
    if local_only_changes:
        print(
            "Using uncommitted local WebUI/Web Layer changes; the remote daemon will "
            "remain pinned to the committed branch head."
        )
    expected_commit = _git_output("rev-parse", "HEAD")
    token = secrets.token_urlsafe(32)
    web_session_token = secrets.token_hex(32) if args.self_hosted_webui else ""
    web_bootstrap_token = secrets.token_hex(32) if args.self_hosted_webui else ""
    ssh_binary = shutil.which("ssh")
    if not ssh_binary:
        raise LauncherError("system OpenSSH client was not found")

    print(
        f"Checking installed OpenEvo source through SSH {connection.display_name}...",
        flush=True,
    )
    remote_commit = probe_remote_source_commit(ssh_binary, connection)
    if args.source_action == "install" and remote_commit is not None:
        raise LauncherError(
            f"OpenEvo is already installed at {remote_commit}; use --source-action update"
        )
    if args.source_action == "update" and remote_commit is None:
        raise LauncherError(
            "OpenEvo is not installed; use --source-action install"
        )
    if args.source_action == "start" and remote_commit != expected_commit:
        installed = remote_commit or "nothing"
        raise LauncherError(
            f"start requires local commit {expected_commit}, but the server has {installed}; "
            "run --source-action update first"
        )

    needs_source_delivery = remote_commit != expected_commit
    prepare_runtime = args.source_action != "start"
    start_services = not source_only
    if needs_source_delivery:
        with create_source_bundle(
            branch=branch,
            expected_commit=expected_commit,
            remote_commit=remote_commit,
        ) as bundle:
            print(
                f"Uploading committed source {expected_commit[:12]} "
                f"({bundle.byte_size} bytes) over SSH...",
                flush=True,
            )
            upload_source_bundle(
                ssh_binary,
                connection,
                bundle,
                expected_commit=expected_commit,
            )
            remote_script = build_remote_script(
                branch=branch,
                expected_commit=expected_commit,
                source_bundle_sha256=bundle.sha256,
                prepare_runtime=prepare_runtime,
                start_services=start_services,
                token=token,
                remote_port=args.remote_port,
                evolution_model=args.evolution_model,
                self_hosted_webui=args.self_hosted_webui,
                web_session_token=web_session_token,
                web_bootstrap_token=web_bootstrap_token,
                remote_web_port=args.remote_web_port,
                browser_endpoint=f"http://127.0.0.1:{args.local_port}",
            )
            _run_remote(ssh_binary, connection, remote_script)
    else:
        print(
            f"Installed source already matches {expected_commit[:12]}; no upload is needed.",
            flush=True,
        )
        remote_script = build_remote_script(
            branch=branch,
            expected_commit=expected_commit,
            prepare_runtime=prepare_runtime,
            start_services=start_services,
            token=token,
            remote_port=args.remote_port,
            evolution_model=args.evolution_model,
            self_hosted_webui=args.self_hosted_webui,
            web_session_token=web_session_token,
            web_bootstrap_token=web_bootstrap_token,
            remote_web_port=args.remote_web_port,
            browser_endpoint=f"http://127.0.0.1:{args.local_port}",
        )
        _run_remote(ssh_binary, connection, remote_script)
    if source_only:
        print(
            f"Source and runtime {args.source_action} completed. Services were not started.",
            flush=True,
        )
        return 0
    if args.deploy_only:
        print("Deployment complete. The remote daemon remains running.")
        return 0

    tunnel: subprocess.Popen[str] | None = None
    web_server = None
    web_thread: threading.Thread | None = None
    try:
        print(
            f"Opening SSH tunnel 127.0.0.1:{args.local_port} -> "
            f"remote 127.0.0.1:{args.remote_web_port if args.self_hosted_webui else args.remote_port}..."
        )
        tunnel = _start_tunnel(
            ssh_binary,
            connection,
            args.local_port,
            args.remote_web_port if args.self_hosted_webui else args.remote_port,
        )
        if args.self_hosted_webui:
            _wait_for_local_webui(args.local_port)
            browser_url = (
                f"http://127.0.0.1:{args.local_port}/openevo"
                f"#browser-bootstrap={web_bootstrap_token}"
            )
            print("Remote daemon, Web Layer, WebUI, and SSH tunnel are ready.")
            print(f"OpenEvo WebUI URL: {browser_url}")
            if args.browser_e2e:
                npm_binary = shutil.which("npm.cmd") or shutil.which("npm")
                if not npm_binary:
                    raise LauncherError("npm was not found for the browser acceptance test")
                environment = os.environ.copy()
                environment["OPENEVO_E2E_BASE_URL"] = browser_url
                command = [npm_binary, "run", "test:webui:e2e"]
                if os.name != "nt" and npm_binary.lower().endswith((".cmd", ".bat")):
                    command_interpreter = shutil.which("cmd.exe")
                    if not command_interpreter:
                        raise LauncherError("Windows npm was found from WSL, but cmd.exe was unavailable")
                    command = [
                        command_interpreter, "/d", "/c", "npm.cmd", "run",
                        "test:webui:e2e",
                    ]
                print("Running the real browser acceptance path; the tunnel will close afterward.")
                return subprocess.run(
                    command,
                    cwd=DESKTOP_ROOT,
                    env=environment,
                    check=False,
                ).returncode
            print("Keep this launcher running; press Ctrl+C to close the local tunnel.")
            try:
                return tunnel.wait()
            except KeyboardInterrupt:
                return 0

        _wait_for_local_health(args.local_port, token)
        npm_binary = shutil.which("npm.cmd") or shutil.which("npm")
        if not npm_binary:
            raise LauncherError("npm was not found after the remote daemon was deployed")
        environment = os.environ.copy()
        environment.update(
            {
                "OPENEVO_DEV_AGENT_TOKEN": token,
                "OPENEVO_DEV_AGENT_URL": f"http://127.0.0.1:{args.local_port}",
            }
        )
        npm_script = "dev:agent"
        npm_arguments: list[str] = []
        if args.web_layer:
            import uvicorn
            from openevo.web_gateway.product_app import create_web_gateway_app

            session_token = secrets.token_hex(32)
            bootstrap_token = secrets.token_hex(32)
            browser_endpoint = f"http://127.0.0.1:{DEVELOPMENT_BROWSER_PORT}"
            app = create_web_gateway_app(
                daemon_endpoint=f"http://127.0.0.1:{args.local_port}",
                daemon_token=token,
                session_token=session_token,
                bootstrap_token=bootstrap_token,
                browser_endpoint=browser_endpoint,
                source_commit=expected_commit,
            )
            web_server = uvicorn.Server(uvicorn.Config(
                app, host="127.0.0.1", port=args.web_port, log_level="info"
            ))
            web_thread = threading.Thread(target=web_server.run, name="development-agent-web", daemon=True)
            web_thread.start()
            deadline = time.monotonic() + 10
            while not web_server.started and web_thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.05)
            if not web_server.started:
                raise LauncherError("development web layer did not start")
            environment["OPENEVO_DEV_WEB_URL"] = f"http://127.0.0.1:{args.web_port}"
            npm_script = "dev:agent:web"
            npm_arguments = []
            print(
                "OpenEvo WebUI URL: "
                f"http://127.0.0.1:5173/product-preview.html#browser-bootstrap={bootstrap_token}"
            )
            print("Local WebUI API bridge is ready; the browser will use only the web-layer session token.")
        print("Remote daemon and tunnel are ready. Starting the real product UI...")
        npm_command = [npm_binary, "run", npm_script, *npm_arguments]
        if os.name != "nt" and npm_binary.lower().endswith((".cmd", ".bat")):
            command_interpreter = shutil.which("cmd.exe")
            if not command_interpreter:
                raise LauncherError("Windows npm was found from WSL, but cmd.exe was unavailable")
            npm_command = [command_interpreter, "/d", "/c", "npm.cmd", "run", npm_script, *npm_arguments]
        completed = subprocess.run(
            npm_command,
            cwd=DESKTOP_ROOT,
            env=environment,
            check=False,
        )
        return completed.returncode
    finally:
        if web_server is not None:
            web_server.should_exit = True
        if web_thread is not None:
            web_thread.join(timeout=5)
        if tunnel is not None:
            suffix = " and Web Layer remain running" if args.self_hosted_webui else " remains running"
            print(f"Closing the local SSH tunnel. The remote daemon{suffix}.")
            _stop_process(tunnel)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LauncherError as exc:
        print(f"Remote development setup failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
