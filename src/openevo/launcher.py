#!/usr/bin/env python3
"""Install, connect to, and operate a self-hosted OpenEvo WebUI over SSH."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import glob
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
import webbrowser
from pathlib import Path
from typing import Callable, Iterator

from openevo.release_bundle import (
    RELEASE_ID_PATTERN,
    ReleaseBundleError,
    ReleaseBundleReceipt,
    verify_release_bundle,
)


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
SSH_CONFIG_MAX_INCLUDE_DEPTH = 8
SSH_CONFIG_MAX_FILES = 64
LAUNCHER_PREFERENCES_SCHEMA_VERSION = 1


class LauncherError(RuntimeError):
    """A user-actionable OpenEvo launcher failure."""


@dataclass(frozen=True)
class SshConnection:
    options: tuple[str, ...]
    destination: str
    display_name: str


@dataclass(frozen=True)
class SshHostProfile:
    """A concrete Host alias discovered in the user's OpenSSH configuration."""

    alias: str
    hostname: str | None
    user: str | None
    port: int | None
    source: Path


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


def _default_ssh_config_path() -> Path:
    return Path.home() / ".ssh" / "config"


def _default_preferences_path() -> Path:
    return Path.home() / ".openevo" / "launcher.json"


def discover_ssh_hosts(config_path: Path | None = None) -> tuple[SshHostProfile, ...]:
    """Return literal SSH aliases from config and bounded Include files.

    OpenSSH remains authoritative for resolving an alias.  Parsed connection fields
    are used only to make the selection prompt understandable.
    """

    root = (config_path or _default_ssh_config_path()).expanduser()
    seen: set[Path] = set()
    profiles: dict[str, dict[str, object]] = {}

    def visit(path: Path, depth: int) -> None:
        if depth > SSH_CONFIG_MAX_INCLUDE_DEPTH or len(seen) >= SSH_CONFIG_MAX_FILES:
            return
        normalized = path.expanduser().resolve(strict=False)
        if normalized in seen or not normalized.is_file():
            return
        seen.add(normalized)
        active_aliases: list[str] = []
        try:
            lines = normalized.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            raise LauncherError(f"could not read SSH config {normalized}: {exc}") from exc
        for line in lines:
            try:
                tokens = shlex.split(line, comments=True, posix=True)
            except ValueError:
                continue
            if not tokens:
                continue
            keyword = tokens[0].lower()
            values = tokens[1:]
            if keyword == "include":
                for value in values:
                    include = Path(value).expanduser()
                    if not include.is_absolute():
                        include = normalized.parent / include
                    for match in sorted(glob.glob(str(include))):
                        visit(Path(match), depth + 1)
                continue
            if keyword == "match":
                active_aliases = []
                continue
            if keyword == "host":
                active_aliases = []
                for value in values:
                    if any(marker in value for marker in ("*", "?", "!")):
                        continue
                    if not SSH_ALIAS_PATTERN.fullmatch(value):
                        continue
                    profiles.setdefault(
                        value,
                        {
                            "hostname": None,
                            "user": None,
                            "port": None,
                            "source": normalized,
                        },
                    )
                    active_aliases.append(value)
                continue
            if not active_aliases or not values:
                continue
            field = {"hostname": "hostname", "user": "user", "port": "port"}.get(keyword)
            if field is None:
                continue
            for alias in active_aliases:
                profile = profiles[alias]
                if profile[field] is not None:
                    continue
                if field == "port":
                    try:
                        port = int(values[0])
                    except ValueError:
                        continue
                    if not 1 <= port <= 65_535:
                        continue
                    profile[field] = port
                else:
                    profile[field] = values[0]

    visit(root, 0)
    return tuple(
        SshHostProfile(
            alias=alias,
            hostname=value["hostname"] if isinstance(value["hostname"], str) else None,
            user=value["user"] if isinstance(value["user"], str) else None,
            port=value["port"] if isinstance(value["port"], int) else None,
            source=value["source"] if isinstance(value["source"], Path) else root,
        )
        for alias, value in sorted(profiles.items(), key=lambda item: item[0].casefold())
    )


def load_last_ssh_alias(preferences_path: Path | None = None) -> str | None:
    path = (preferences_path or _default_preferences_path()).expanduser()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != LAUNCHER_PREFERENCES_SCHEMA_VERSION:
        return None
    alias = payload.get("last_ssh_alias")
    if not isinstance(alias, str) or not SSH_ALIAS_PATTERN.fullmatch(alias):
        return None
    return alias


def save_last_ssh_alias(alias: str, preferences_path: Path | None = None) -> None:
    validated = validate_ssh_alias(alias)
    path = (preferences_path or _default_preferences_path()).expanduser()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix="launcher-", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as destination:
                json.dump(
                    {
                        "schema_version": LAUNCHER_PREFERENCES_SCHEMA_VERSION,
                        "last_ssh_alias": validated,
                    },
                    destination,
                    sort_keys=True,
                )
                destination.write("\n")
                destination.flush()
                os.fsync(destination.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    except OSError as exc:
        raise LauncherError(f"could not save OpenEvo launcher preferences: {exc}") from exc


def select_ssh_alias(
    profiles: tuple[SshHostProfile, ...],
    *,
    last_alias: str | None,
    interactive: bool,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> str:
    if not profiles:
        raise LauncherError(
            "no literal Host aliases were found in ~/.ssh/config; add one or pass "
            "--host, --user and --ssh-port"
        )
    aliases = {profile.alias for profile in profiles}
    default_alias = last_alias if last_alias in aliases else None
    if not interactive:
        if default_alias is not None:
            return default_alias
        if len(profiles) == 1:
            return profiles[0].alias
        raise LauncherError(
            "multiple SSH hosts are configured; run interactively or pass --ssh-alias"
        )

    output_fn("Available SSH workspaces:")
    for index, profile in enumerate(profiles, start=1):
        endpoint = profile.hostname or profile.alias
        if profile.user:
            endpoint = f"{profile.user}@{endpoint}"
        if profile.port:
            endpoint = f"{endpoint}:{profile.port}"
        marker = " (last used)" if profile.alias == default_alias else ""
        output_fn(f"  {index}. {profile.alias}  [{endpoint}]{marker}")
    default_index = next(
        (index for index, profile in enumerate(profiles, start=1) if profile.alias == default_alias),
        None,
    )
    prompt = "Choose a workspace"
    if default_index is not None:
        prompt += f" [{default_index}]"
    answer = input_fn(f"{prompt}: ").strip()
    if not answer and default_index is not None:
        return profiles[default_index - 1].alias
    try:
        chosen_index = int(answer)
    except ValueError as exc:
        raise LauncherError("choose a workspace by its number") from exc
    if not 1 <= chosen_index <= len(profiles):
        raise LauncherError("SSH workspace selection is out of range")
    return profiles[chosen_index - 1].alias


def resolve_launcher_connection(args: argparse.Namespace) -> SshConnection:
    """Resolve explicit SSH arguments or select a configured workspace."""

    if args.ssh_alias or args.host or args.user or args.ssh_port != 22:
        return resolve_ssh_connection(args)
    config_path = Path(args.ssh_config).expanduser() if args.ssh_config else None
    preferences_path = Path(args.preferences_file).expanduser() if args.preferences_file else None
    profiles = discover_ssh_hosts(config_path)
    last_alias = None if args.no_remember else load_last_ssh_alias(preferences_path)
    alias = select_ssh_alias(
        profiles,
        last_alias=last_alias,
        interactive=not args.non_interactive and sys.stdin.isatty(),
    )
    args.ssh_alias = alias
    connection = resolve_ssh_connection(args)
    if not args.no_remember:
        save_last_ssh_alias(alias, preferences_path)
    return connection


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
        raise LauncherError("Git branch name is not accepted by the OpenEvo launcher")
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


def build_remote_release_probe_script() -> str:
    return f"""\
set -eu
state_root="{REMOTE_DEVELOPMENT_STATE_ROOT}"
active_marker="$state_root/active-release-v1"
if [ ! -f "$active_marker" ]; then
  printf 'absent\n'
  exit 0
fi
release_id="$(cat "$active_marker" 2>/dev/null || true)"
case "$release_id" in
  ''|*[!0-9a-f]*) printf 'invalid\n'; exit 0 ;;
esac
release_root="$state_root/releases/$release_id"
if [ ! -f "$release_root/manifest.json" ] || [ ! -d "$release_root/payload" ]; then
  printf 'invalid\n'
else
  printf 'managed:%s\n' "$release_id"
fi
"""


def probe_remote_release_id(ssh_binary: str, connection: SshConnection) -> str | None:
    result = _run_remote_capture(
        ssh_binary,
        connection,
        build_remote_release_probe_script(),
        timeout=30,
    ).strip()
    if result == "absent":
        return None
    if result.startswith("managed:"):
        release_id = result.removeprefix("managed:")
        if RELEASE_ID_PATTERN.fullmatch(release_id):
            return release_id
    raise LauncherError(f"remote release probe returned an invalid result: {result!r}")


def upload_release_bundle(
    ssh_binary: str,
    connection: SshConnection,
    bundle: ReleaseBundleReceipt,
) -> None:
    remote_command = (
        "set -eu; umask 077; "
        'mkdir -p "$HOME/.openevo/dev-agent/incoming"; '
        f'cat > "$HOME/.openevo/dev-agent/incoming/release-{bundle.release_id}.oevobundle"'
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
            f"release upload exceeded {SOURCE_TRANSFER_TIMEOUT_SECONDS} seconds"
        ) from exc
    if completed.returncode != 0:
        raise LauncherError(
            f"release upload failed with SSH exit code {completed.returncode}"
        )


def build_remote_release_install_script(
    bundle: ReleaseBundleReceipt,
    *,
    archive_uploaded: bool,
) -> str:
    """Install or re-verify one immutable release and atomically activate it."""

    values = {
        "release_id": bundle.release_id,
        "bundle_sha256": bundle.sha256 if archive_uploaded else "",
        "source_commit": bundle.source_commit,
        "product_version": bundle.product_version,
    }
    quoted = {key: shlex.quote(value) for key, value in values.items()}
    return rf"""\
set -eu
umask 077
state_root="{REMOTE_DEVELOPMENT_STATE_ROOT}"
release_id={quoted['release_id']}
bundle_sha256={quoted['bundle_sha256']}
expected_commit={quoted['source_commit']}
expected_version={quoted['product_version']}
archive="$state_root/incoming/release-$release_id.oevobundle"
releases_root="$state_root/releases"
release_root="$releases_root/$release_id"
active_marker="$state_root/active-release-v1"
mkdir -p "$state_root/incoming" "$releases_root"

if [ ! -e "$release_root" ]; then
  if [ -z "$bundle_sha256" ] || [ ! -f "$archive" ]; then
    echo "Release $release_id is not installed and its uploaded bundle is missing." >&2
    exit 51
  fi
  if ! command -v sha256sum >/dev/null 2>&1; then
    echo "sha256sum is required to verify the uploaded release." >&2
    exit 52
  fi
  actual_sha256="$(sha256sum "$archive" | awk '{{print $1}}')"
  if [ "$actual_sha256" != "$bundle_sha256" ]; then
    echo "Uploaded release digest does not match the local receipt." >&2
    exit 53
  fi
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is required to install the OpenEvo release." >&2
  exit 54
fi

python3 - "$archive" "$releases_root" "$release_id" "$expected_commit" "$expected_version" <<'PY'
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import tempfile
import zipfile

archive_path = Path(sys.argv[1])
releases_root = Path(sys.argv[2])
expected_id, expected_commit, expected_version = sys.argv[3:]
release_root = releases_root / expected_id

def canonical(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()

def safe_path(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise RuntimeError("release manifest contains an invalid path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts) or path.as_posix() != value:
        raise RuntimeError("release manifest contains an unsafe path")
    return path

def validate_manifest(value):
    required = {{"schema_version", "release_id", "product_version", "source_commit", "payload_root", "files"}}
    if not isinstance(value, dict) or set(value) != required:
        raise RuntimeError("release manifest does not match the closed schema")
    if value["schema_version"] != "1" or value["payload_root"] != "payload":
        raise RuntimeError("release manifest schema is unsupported")
    if value["release_id"] != expected_id or value["source_commit"] != expected_commit or value["product_version"] != expected_version:
        raise RuntimeError("release manifest identity does not match the requested release")
    files = value["files"]
    if not isinstance(files, list) or not 1 <= len(files) <= 10000:
        raise RuntimeError("release manifest file inventory is invalid")
    seen = set()
    normalized = []
    total = 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {{"path", "byte_size", "sha256"}}:
            raise RuntimeError("release manifest file record is invalid")
        path = safe_path(item["path"])
        size, digest = item["byte_size"], item["sha256"]
        if path.as_posix() in seen or not isinstance(size, int) or isinstance(size, bool) or not 0 <= size <= 134217728:
            raise RuntimeError("release manifest file identity is invalid")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise RuntimeError("release manifest file digest is invalid")
        total += size
        if total > 536870912:
            raise RuntimeError("release manifest payload is too large")
        seen.add(path.as_posix())
        normalized.append({{"path": path.as_posix(), "byte_size": size, "sha256": digest}})
    if [item["path"] for item in normalized] != sorted(seen):
        raise RuntimeError("release manifest is not canonically sorted")
    identity = {{
        "schema_version": "1",
        "product_version": value["product_version"],
        "source_commit": value["source_commit"],
        "payload_root": "payload",
        "files": normalized,
    }}
    if hashlib.sha256(canonical(identity)).hexdigest() != expected_id:
        raise RuntimeError("release manifest digest is invalid")
    return normalized

def verify_installed(root, manifest, files):
    manifest_bytes = canonical(manifest) + b"\n"
    if (root / "manifest.json").read_bytes() != manifest_bytes:
        raise RuntimeError("installed release manifest changed")
    payload = root / "payload"
    expected_files = {{item["path"] for item in files}}
    expected_directories = set()
    for expected_path in expected_files:
        parts = PurePosixPath(expected_path).parts
        expected_directories.update(
            PurePosixPath(*parts[:index]).as_posix()
            for index in range(1, len(parts))
        )
    for installed_path in payload.rglob("*"):
        relative = installed_path.relative_to(payload).as_posix()
        if installed_path.is_symlink():
            raise RuntimeError(f"installed release contains a symbolic link: {{relative}}")
        if installed_path.is_dir():
            if relative not in expected_directories:
                raise RuntimeError(f"installed release contains an extra directory: {{relative}}")
        elif not installed_path.is_file() or relative not in expected_files:
            raise RuntimeError(f"installed release contains an extra file: {{relative}}")
    for item in files:
        path = payload.joinpath(*PurePosixPath(item["path"]).parts)
        if not path.is_file() or path.is_symlink() or path.stat().st_size != item["byte_size"]:
            raise RuntimeError(f"installed release file changed: {{item['path']}}")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1048576):
                digest.update(chunk)
        if digest.hexdigest() != item["sha256"]:
            raise RuntimeError(f"installed release file digest changed: {{item['path']}}")

if release_root.exists():
    manifest = json.loads((release_root / "manifest.json").read_text(encoding="utf-8"))
    files = validate_manifest(manifest)
    verify_installed(release_root, manifest, files)
else:
    if not archive_path.is_file():
        raise RuntimeError("uploaded release archive is missing")
    temporary = Path(tempfile.mkdtemp(prefix=".install-", dir=releases_root))
    candidate = temporary / "release"
    candidate.mkdir(mode=0o700)
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or names.count("manifest.json") != 1:
                raise RuntimeError("release archive entries are invalid")
            manifest = json.loads(archive.read("manifest.json"))
            files = validate_manifest(manifest)
            expected_names = {{"manifest.json", *(f"payload/{{item['path']}}" for item in files)}}
            if set(names) != expected_names:
                raise RuntimeError("release archive does not match its manifest")
            (candidate / "manifest.json").write_bytes(canonical(manifest) + b"\n")
            for item in files:
                name = f"payload/{{item['path']}}"
                info = archive.getinfo(name)
                if info.file_size != item["byte_size"]:
                    raise RuntimeError("release archive file size is invalid")
                data = archive.read(info)
                if hashlib.sha256(data).hexdigest() != item["sha256"]:
                    raise RuntimeError("release archive file digest is invalid")
                destination = candidate / "payload" / PurePosixPath(item["path"])
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with destination.open("xb") as output:
                    output.write(data)
                os.chmod(destination, 0o600)
        verify_installed(candidate, manifest, files)
        try:
            os.rename(candidate, release_root)
        except FileExistsError:
            existing_manifest = json.loads((release_root / "manifest.json").read_text(encoding="utf-8"))
            verify_installed(release_root, existing_manifest, validate_manifest(existing_manifest))
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
PY

marker_tmp="$active_marker.tmp.$$"
printf '%s\n' "$release_id" > "$marker_tmp"
mv "$marker_tmp" "$active_marker"
rm -f "$archive"
echo "OpenEvo release $expected_version ($release_id) is installed and verified."
"""


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
    release_id: str = "",
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
        "release_id": release_id,
        "delivery_mode": "release" if release_id else "git",
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
delivery_mode={quoted['delivery_mode']}
release_id={quoted['release_id']}
if [ "$delivery_mode" = release ]; then
  source_root="$state_root/releases/$release_id/payload"
else
  source_root="$state_root/source"
fi
source_marker="$state_root/managed-source-v1"
source_bundle="$state_root/incoming/source-$expected_commit.bundle"
pid_file="$state_root/daemon.pid"
log_file="$state_root/daemon.log"

mkdir -p "$state_root"

if [ "$delivery_mode" = release ]; then
  active_release="$(cat "$state_root/active-release-v1" 2>/dev/null || true)"
  if [ "$active_release" != "$release_id" ] || [ ! -d "$source_root" ] || \
     [ ! -f "$state_root/releases/$release_id/manifest.json" ]; then
    echo "Requested OpenEvo release is not installed and active." >&2
    exit 50
  fi
  deployed_commit="$expected_commit"
else
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
    printf '%s\n' 'managed by openevo.launcher' > "$source_marker"
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
fi

if [ "$delivery_mode" = release ]; then
  runtime_marker="$state_root/runtime-release-v1"
  runtime_identity="$release_id"
  runtime_environment="$state_root/runtimes/$release_id"
  mkdir -p "$state_root/runtimes"
else
  runtime_marker="$state_root/runtime-commit-v1"
  runtime_identity="$expected_commit"
  runtime_environment="$source_root/.venv"
fi
export UV_PROJECT_ENVIRONMENT="$runtime_environment"
export PYTHONDONTWRITEBYTECODE=1
runtime_commit="$(cat "$runtime_marker" 2>/dev/null || true)"
if [ ! -x "$runtime_environment/bin/python" ]; then
  runtime_commit=''
fi
if [ "$runtime_commit" = "$runtime_identity" ]; then
  echo "[remote 2/4] Runtime already prepared for $runtime_identity; skipping dependency sync."
elif [ "$prepare_runtime" -ne 1 ]; then
  echo "The runtime is not prepared for $runtime_identity; run --source-action install or update first." >&2
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
if [ "$runtime_commit" != "$runtime_identity" ]; then
  if ! command -v timeout >/dev/null 2>&1; then
    echo "timeout is required for bounded dependency installation." >&2
    exit 36
  fi
  if [ "$delivery_mode" = release ]; then
    timeout 300 "$uv_bin" sync --frozen --no-dev --python 3.11
  else
    timeout 300 "$uv_bin" sync --frozen --python 3.11
  fi
  runtime_marker_tmp="$runtime_marker.tmp.$$"
  printf '%s\n' "$runtime_identity" > "$runtime_marker_tmp"
  mv "$runtime_marker_tmp" "$runtime_marker"
fi

if [ "$start_services" -ne 1 ]; then
  echo "Remote OpenEvo source and runtime are installed at $runtime_identity."
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


def open_browser(url: str) -> bool:
    """Best-effort browser opening; the printed URL remains the reliable fallback."""

    try:
        if os.name != "nt" and os.environ.get("WSL_DISTRO_NAME"):
            explorer = shutil.which("explorer.exe")
            if explorer:
                return subprocess.run(
                    [explorer, url],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                ).returncode == 0
        return bool(webbrowser.open(url, new=2))
    except (OSError, subprocess.SubprocessError, webbrowser.Error):
        return False


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
            "Install or update self-hosted OpenEvo through system OpenSSH, "
            "open a private local tunnel, and launch the WebUI"
        )
    )
    parser.add_argument(
        "--ssh-alias",
        default=os.environ.get("OPENEVO_DEV_SSH_ALIAS"),
        help="literal Host alias from ~/.ssh/config (or OPENEVO_DEV_SSH_ALIAS)",
    )
    parser.add_argument(
        "--ssh-config",
        help="SSH config to discover workspaces from (defaults to ~/.ssh/config)",
    )
    parser.add_argument(
        "--preferences-file",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="do not prompt when selecting an SSH workspace",
    )
    parser.add_argument(
        "--no-remember",
        action="store_true",
        help="do not load or save the last selected SSH workspace",
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
        "--release-bundle",
        type=Path,
        help=(
            "verified .oevobundle server release to install instead of deploying "
            "the current Git checkout"
        ),
    )
    parser.add_argument(
        "--source-action",
        choices=sorted(SOURCE_ACTIONS),
        default="auto",
        help=(
            "auto installs or updates then starts; install/update prepare source and "
            "runtime without starting; start requires that exact release to be prepared"
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
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="print the WebUI URL without opening the default browser",
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
        help="stop only the remote processes owned by this OpenEvo launcher",
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
        connection = resolve_launcher_connection(args)
        ssh_binary = shutil.which("ssh")
        if not ssh_binary:
            raise LauncherError("system OpenSSH client was not found")
        print(
            "Managing the OpenEvo remote stack through SSH "
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
    connection = resolve_launcher_connection(args)
    release_bundle: ReleaseBundleReceipt | None = None
    if args.release_bundle is not None:
        if args.branch is not None:
            raise LauncherError("--branch cannot be combined with --release-bundle")
        try:
            release_bundle = verify_release_bundle(args.release_bundle)
        except ReleaseBundleError as exc:
            raise LauncherError(f"release bundle verification failed: {exc}") from exc
        branch = "release"
        expected_commit = release_bundle.source_commit
    else:
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

    delivery_label = "release" if release_bundle is not None else "source"
    print(
        f"Checking installed OpenEvo {delivery_label} through SSH {connection.display_name}...",
        flush=True,
    )
    expected_identity = (
        release_bundle.release_id if release_bundle is not None else expected_commit
    )
    remote_identity = (
        probe_remote_release_id(ssh_binary, connection)
        if release_bundle is not None
        else probe_remote_source_commit(ssh_binary, connection)
    )
    if args.source_action == "install" and remote_identity is not None:
        raise LauncherError(
            f"OpenEvo is already installed at {remote_identity}; use --source-action update"
        )
    if args.source_action == "update" and remote_identity is None:
        raise LauncherError(
            "OpenEvo is not installed; use --source-action install"
        )
    if args.source_action == "start" and remote_identity != expected_identity:
        installed = remote_identity or "nothing"
        raise LauncherError(
            f"start requires {expected_identity}, but the server has {installed}; "
            "run --source-action update first"
        )

    needs_source_delivery = remote_identity != expected_identity
    prepare_runtime = args.source_action != "start"
    start_services = not source_only
    if release_bundle is not None:
        if needs_source_delivery:
            print(
                f"Uploading OpenEvo {release_bundle.product_version} release "
                f"{release_bundle.release_id[:12]} ({release_bundle.byte_size} bytes) over SSH...",
                flush=True,
            )
            upload_release_bundle(ssh_binary, connection, release_bundle)
        else:
            print(
                f"Installed release already matches {release_bundle.release_id[:12]}; "
                "no upload is needed.",
                flush=True,
            )
        _run_remote(
            ssh_binary,
            connection,
            build_remote_release_install_script(
                release_bundle,
                archive_uploaded=needs_source_delivery,
            ),
        )
        _run_remote(
            ssh_binary,
            connection,
            build_remote_script(
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
                release_id=release_bundle.release_id,
            ),
        )
    elif needs_source_delivery:
        with create_source_bundle(
            branch=branch,
            expected_commit=expected_commit,
            remote_commit=remote_identity,
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
            if not args.no_open and not open_browser(browser_url):
                print("The browser could not be opened automatically; use the URL above.")
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


def command_main(argv: list[str] | None = None) -> int:
    """Console boundary that renders launcher failures without a traceback."""

    try:
        return main(argv)
    except LauncherError as exc:
        print(f"OpenEvo WebUI setup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(command_main())
