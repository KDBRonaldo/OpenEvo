#!/usr/bin/env python3
"""Deploy the development agent bridge over SSH and start the Vite UI.

This is deliberately a development-only convenience launcher.  The packaged
Desktop uses its authenticated native sidecar and sealed release assets.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import time
import threading
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DESKTOP_ROOT = REPOSITORY_ROOT / "desktop"
DEVELOPMENT_BROWSER_PORT = 5173
SSH_ALIAS_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
SSH_HOST_PATTERN = re.compile(
    r"(?=.{1,253}\Z)[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?"
)
SSH_USER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,31}")
BRANCH_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}")
LOCAL_WEB_LAYER_PATH_PREFIXES = ("desktop/", "docs/", "tests/")
LOCAL_WEB_LAYER_PATHS = frozenset(
    {
        "scripts/dev/development_agent_web_layer.py",
        "scripts/dev/run_remote_agent_development.py",
    }
)


class LauncherError(RuntimeError):
    """A user-actionable remote development setup failure."""


@dataclass(frozen=True)
class SshConnection:
    options: tuple[str, ...]
    destination: str
    display_name: str


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


def normalize_repository_url(value: str) -> str:
    repository_url = value.strip()
    if repository_url.startswith("git@github.com:"):
        repository_url = "https://github.com/" + repository_url.removeprefix(
            "git@github.com:"
        )
    parsed = urlsplit(repository_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise LauncherError(
            "repository URL must be a credential-free https://github.com/<owner>/<repo> URL"
        )
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) != 2 or any(part in {".", ".."} for part in path_parts):
        raise LauncherError("repository URL must name one GitHub owner and repository")
    normalized_path = "/" + "/".join(path_parts)
    if not normalized_path.endswith(".git"):
        normalized_path += ".git"
    return urlunsplit(("https", "github.com", normalized_path, "", ""))


def _git_output(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
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
            f"({details}); commit and push them before deployment"
        )
    return changed


def resolve_repository_url(explicit: str | None) -> str:
    return normalize_repository_url(explicit or _git_output("remote", "get-url", "origin"))


def resolve_branch(explicit: str | None) -> str:
    branch = explicit or _git_output("branch", "--show-current")
    if not branch:
        raise LauncherError("detached HEAD is unsupported; pass --branch explicitly")
    return validate_branch(branch)


def build_remote_script(
    *,
    repository_url: str,
    branch: str,
    expected_commit: str,
    token: str,
    remote_port: int,
    evolution_model: str,
    self_hosted_webui: bool = False,
    web_session_token: str = "",
    web_bootstrap_token: str = "",
    remote_web_port: int = 8788,
    browser_endpoint: str = "http://127.0.0.1:8765",
) -> str:
    values = {
        "repository_url": repository_url,
        "branch": branch,
        "expected_commit": expected_commit,
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
      *scripts/dev/development_agent_web_layer.py*)
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
  "$uv_bin" run --frozen --python 3.11 python \\
  scripts/dev/development_agent_web_layer.py \\
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
  if curl --silent --fail "http://127.0.0.1:$remote_web_port/openevo" >/dev/null 2>&1; then
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
echo "Remote Desktop Web Layer is ready on loopback port $remote_web_port."
"""
    return f"""\
set -eu
umask 077

repository_url={quoted['repository_url']}
branch={quoted['branch']}
expected_commit={quoted['expected_commit']}
agent_token={quoted['token']}
remote_port={quoted['remote_port']}
evolution_model={quoted['evolution_model']}
state_root="$HOME/.openevo/dev-agent"
source_root="$state_root/source"
source_marker="$state_root/managed-source-v1"
pid_file="$state_root/daemon.pid"
log_file="$state_root/daemon.log"

mkdir -p "$state_root"

if [ ! -e "$source_root" ]; then
  echo "[remote 1/4] Cloning the selected OpenEvo branch..."
  git clone --branch "$branch" --single-branch "$repository_url" "$source_root"
  printf '%s\n' 'managed by scripts/dev/run_remote_agent_development.py' > "$source_marker"
elif [ ! -f "$source_marker" ] || [ ! -d "$source_root/.git" ]; then
  echo "Refusing to modify an unrecognized path: $source_root" >&2
  exit 20
else
  echo "[remote 1/4] Updating the managed OpenEvo checkout..."
  if [ -n "$(git -C "$source_root" status --porcelain)" ]; then
    echo "Managed checkout has local changes; commit, stash, or remove them before deployment." >&2
    exit 21
  fi
  git -C "$source_root" remote set-url origin "$repository_url"
  git -C "$source_root" fetch origin "$branch"
  current_branch="$(git -C "$source_root" branch --show-current)"
  if [ "$current_branch" != "$branch" ]; then
    if git -C "$source_root" show-ref --verify --quiet "refs/heads/$branch"; then
      git -C "$source_root" switch "$branch"
    else
      git -C "$source_root" switch --create "$branch" --track "origin/$branch"
    fi
  fi
  git -C "$source_root" merge --ff-only "origin/$branch"
fi

deployed_commit="$(git -C "$source_root" rev-parse HEAD)"
if [ "$deployed_commit" != "$expected_commit" ]; then
  echo "The fork branch does not contain local commit $expected_commit." >&2
  echo "Commit and push the local branch before deploying." >&2
  exit 26
fi

echo "[remote 2/4] Preparing uv and Python 3.11..."
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
  if ! command -v curl >/dev/null 2>&1; then
    echo "curl is required to install uv automatically." >&2
    exit 22
  fi
  curl -LsSf https://astral.sh/uv/install.sh | sh
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
"$uv_bin" sync --frozen --python 3.11

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
if ! env "PATH=$codex_dir:$PATH" "$codex_bin" login status >/dev/null 2>&1; then
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
      *scripts/dev/live_agent_daemon.py*)
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
  "$uv_bin" run --frozen --python 3.11 python \
  scripts/dev/live_agent_daemon.py \
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
     curl --silent --fail \
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


def _run_remote(ssh_binary: str, connection: SshConnection, script: str) -> None:
    completed = subprocess.run(
        [
            ssh_binary,
            *connection.options,
            connection.destination,
            "sh",
            "-s",
        ],
        input=script,
        text=True,
        check=False,
    )
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
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=10",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "TCPKeepAlive=yes",
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
            if response.status == 200 and "OpenEvo Desktop" in body:
                return
            last_error = f"unexpected response status/body from {url}"
        except (OSError, urllib.error.URLError) as exc:
            last_error = str(exc)
        time.sleep(0.2)
    raise LauncherError(f"local Desktop Web Layer health check failed: {last_error}")


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
        "--repository-url",
        help="credential-free GitHub fork URL; defaults to the local origin remote",
    )
    parser.add_argument(
        "--branch",
        help="fork branch to deploy; defaults to the current local branch",
    )
    parser.add_argument("--local-port", type=_checked_port, default=8765)
    parser.add_argument("--remote-port", type=_checked_port, default=8787)
    parser.add_argument("--web-port", type=_checked_port, default=8766)
    parser.add_argument("--remote-web-port", type=_checked_port, default=8788)
    parser.add_argument(
        "--web-layer",
        action="store_true",
        help="run the development-only Desktop Local API v2 bridge in front of the daemon",
    )
    parser.add_argument(
        "--self-hosted-webui",
        action="store_true",
        help=(
            "serve the unchanged Desktop renderer and v2 Web Layer beside the remote "
            "daemon, using one local SSH tunnel"
        ),
    )
    parser.add_argument("--evolution-model", default="gpt-5.5")
    parser.add_argument(
        "--deploy-only",
        action="store_true",
        help="deploy and verify the daemon without opening a tunnel or Vite",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.web_layer and args.self_hosted_webui:
        raise LauncherError("--web-layer and --self-hosted-webui are mutually exclusive")
    if not args.deploy_only:
        local_ports = [args.local_port]
        if not args.self_hosted_webui:
            local_ports.append(DEVELOPMENT_BROWSER_PORT)
        if args.web_layer:
            local_ports.append(args.web_port)
        ensure_local_ports_available(local_ports)
    connection = resolve_ssh_connection(args)
    repository_url = resolve_repository_url(args.repository_url)
    branch = resolve_branch(args.branch)
    local_only_changes = validate_checkout_for_deployment(
        web_layer=args.web_layer and not args.self_hosted_webui,
        deploy_only=args.deploy_only,
    )
    if local_only_changes:
        print(
            "Using uncommitted local Desktop/Web Layer changes; the remote daemon will "
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
        f"Deploying {repository_url} branch {branch} through SSH "
        f"{connection.display_name}..."
    )
    remote_script = build_remote_script(
        repository_url=repository_url,
        branch=branch,
        expected_commit=expected_commit,
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
            print("Remote daemon, Web Layer, unchanged Desktop UI, and SSH tunnel are ready.")
            print(f"OpenEvo Desktop URL: {browser_url}")
            webbrowser.open(browser_url)
            print("Keep this launcher running; press Ctrl+C to close the local tunnel.")
            try:
                return tunnel.wait()
            except KeyboardInterrupt:
                return 0

        _wait_for_local_health(args.local_port, token)
        npm_binary = shutil.which("npm.cmd" if os.name == "nt" else "npm")
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
            from development_agent_web_layer import create_development_agent_web_app

            session_token = secrets.token_hex(32)
            bootstrap_token = secrets.token_hex(32)
            browser_endpoint = f"http://127.0.0.1:{DEVELOPMENT_BROWSER_PORT}"
            app = create_development_agent_web_app(
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
            environment["OPENEVO_DEV_WEB_TOKEN"] = session_token
            npm_script = "dev:agent:web"
            npm_arguments = ["--", "--open", "/product-preview.html"]
            print("Desktop Local API v2 bridge is ready; the browser will use only the web-layer session token.")
        print("Remote daemon and tunnel are ready. Starting the real product UI...")
        completed = subprocess.run(
            [npm_binary, "run", npm_script, *npm_arguments],
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
