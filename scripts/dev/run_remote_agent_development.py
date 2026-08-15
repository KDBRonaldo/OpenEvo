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
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DESKTOP_ROOT = REPOSITORY_ROOT / "desktop"
SSH_ALIAS_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
SSH_HOST_PATTERN = re.compile(
    r"(?=.{1,253}\Z)[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?"
)
SSH_USER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,31}")
BRANCH_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}")


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
) -> str:
    values = {
        "repository_url": repository_url,
        "branch": branch,
        "expected_commit": expected_commit,
        "token": token,
        "remote_port": str(remote_port),
        "evolution_model": evolution_model,
    }
    quoted = {key: shlex.quote(value) for key, value in values.items()}
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
  if ! command -v curl >/dev/null 2>&1; then
    echo "curl is required to install uv automatically." >&2
    exit 22
  fi
  curl -LsSf https://astral.sh/uv/install.sh | sh
  if [ -x "$HOME/.local/bin/uv" ]; then
    uv_bin="$HOME/.local/bin/uv"
  elif [ -x "$HOME/.cargo/bin/uv" ]; then
    uv_bin="$HOME/.cargo/bin/uv"
  else
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
    parser.add_argument("--evolution-model", default="gpt-5.5")
    parser.add_argument(
        "--deploy-only",
        action="store_true",
        help="deploy and verify the daemon without opening a tunnel or Vite",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    connection = resolve_ssh_connection(args)
    repository_url = resolve_repository_url(args.repository_url)
    branch = resolve_branch(args.branch)
    dirty = _git_output("status", "--porcelain")
    if dirty:
        raise LauncherError(
            "the local checkout has uncommitted changes; commit and push them before "
            "deploying so the server cannot silently run older code"
        )
    expected_commit = _git_output("rev-parse", "HEAD")
    token = secrets.token_urlsafe(32)
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
    )
    _run_remote(ssh_binary, connection, remote_script)
    if args.deploy_only:
        print("Deployment complete. The remote daemon remains running.")
        return 0

    tunnel: subprocess.Popen[str] | None = None
    try:
        print(
            f"Opening SSH tunnel 127.0.0.1:{args.local_port} -> "
            f"remote 127.0.0.1:{args.remote_port}..."
        )
        tunnel = _start_tunnel(
            ssh_binary,
            connection,
            args.local_port,
            args.remote_port,
        )
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
        print("Remote daemon and tunnel are ready. Starting the real product UI...")
        completed = subprocess.run(
            [npm_binary, "run", "dev:agent"],
            cwd=DESKTOP_ROOT,
            env=environment,
            check=False,
        )
        return completed.returncode
    finally:
        if tunnel is not None:
            print("Closing the local SSH tunnel. The remote daemon remains running.")
            _stop_process(tunnel)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LauncherError as exc:
        print(f"Remote development setup failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
