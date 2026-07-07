from __future__ import annotations

import re
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path

from openevo.remote.preflight import RemoteCommandResult
from openevo.sidecar import RemoteProfileConfig

CompletedRunner = Callable[[list[str], float], subprocess.CompletedProcess[str]]

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REMOTE_HOST_RE = re.compile(r"^[A-Za-z0-9._%-]+$")
_REMOTE_PATH_RE = re.compile(r"^/[A-Za-z0-9._/@%+=,-]*$")
_REMOTE_USER_RE = re.compile(r"^[A-Za-z0-9._%+-]+$")


class SshRemoteExecutorTransport:
    def __init__(
        self,
        profile: RemoteProfileConfig,
        *,
        runner: CompletedRunner | None = None,
    ) -> None:
        _validate_supported_auth(profile)
        _validate_remote_identity(profile.user, "user", _REMOTE_USER_RE)
        _validate_remote_identity(profile.host, "host", _REMOTE_HOST_RE)
        self._profile = profile
        self._runner = runner or _run_subprocess

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        remote_command = _remote_command(command, cwd=cwd, env=env)
        try:
            completed = self._runner(self._ssh_argv(remote_command), timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"SSH command timed out after {timeout_seconds}s") from exc
        return RemoteCommandResult(
            command=command,
            return_code=int(completed.returncode),
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )

    def upload_dir(self, local_path: str, remote_path: str) -> None:
        local = Path(local_path).expanduser()
        if not local.exists():
            raise FileNotFoundError(f"Local workspace path not found: {local}")
        if not local.is_dir():
            raise ValueError(f"Local workspace path is not a directory: {local}")
        _validate_remote_absolute_path(remote_path, "remote_path")

        mkdir_result = self.run(f"mkdir -p {shlex.quote(remote_path)}")
        if not mkdir_result.ok:
            raise RuntimeError(f"remote mkdir failed: {mkdir_result.stderr}")

        completed = self._runner(self._rsync_argv(local, remote_path), 300.0)
        if completed.returncode != 0:
            message = completed.stderr or completed.stdout or "unknown rsync error"
            raise RuntimeError(f"rsync failed: {message}")

    def _ssh_argv(self, remote_command: str) -> list[str]:
        return [
            *self._ssh_base_argv(),
            "--",
            self._profile.host,
            remote_command,
        ]

    def _ssh_base_argv(self) -> list[str]:
        profile = self._profile
        argv = ["ssh", "-p", str(profile.port)]
        if profile.auth.method == "private_key":
            key_path = Path(str(profile.auth.private_key_path)).expanduser()
            argv.extend(["-i", str(key_path)])
        argv.extend(["-o", "BatchMode=yes", "-l", profile.user])
        return argv

    def _rsync_argv(self, local: Path, remote_path: str) -> list[str]:
        ssh_command = " ".join(shlex.quote(part) for part in self._ssh_base_argv())
        return [
            "rsync",
            "-az",
            "--delete",
            "-e",
            ssh_command,
            _with_trailing_slash(str(local)),
            (
                f"{self._profile.host}:{_with_trailing_slash(remote_path)}"
            ),
        ]


def _run_subprocess(
    argv: list[str], timeout_seconds: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )


def _validate_supported_auth(profile: RemoteProfileConfig) -> None:
    if profile.auth.method == "password_ref":
        raise ValueError("SSH transport does not support password_ref auth yet")
    if profile.auth.passphrase_ref is not None:
        raise ValueError("SSH transport does not support passphrase_ref auth yet")


def _validate_remote_identity(value: str, field_name: str, pattern: re.Pattern[str]) -> None:
    if value.startswith("-") or not pattern.fullmatch(value):
        raise ValueError(f"remote profile {field_name} contains unsupported characters")


def _remote_command(
    command: str,
    *,
    cwd: str | None,
    env: dict[str, str] | None,
) -> str:
    pieces: list[str] = []
    if cwd is not None:
        _validate_remote_absolute_path(cwd, "cwd")
        pieces.append(f"cd {shlex.quote(cwd)}")
    env_prefix = _env_prefix(env or {})
    pieces.append(f"{env_prefix}{command}" if env_prefix else command)
    return " && ".join(pieces)


def _env_prefix(env: dict[str, str]) -> str:
    assignments: list[str] = []
    for key, value in env.items():
        if not _ENV_KEY_RE.fullmatch(key):
            raise ValueError(f"invalid remote environment key: {key!r}")
        assignments.append(f"{key}={shlex.quote(value)}")
    if not assignments:
        return ""
    return "env " + " ".join(assignments) + " "


def _validate_remote_absolute_path(path: str, field_name: str) -> None:
    if not path.startswith("/"):
        raise ValueError(f"{field_name} must be an absolute remote path")
    if any(ord(character) < 32 or ord(character) == 127 for character in path):
        raise ValueError(f"{field_name} must not contain control characters")
    if not _REMOTE_PATH_RE.fullmatch(path):
        raise ValueError(f"{field_name} contains unsupported characters")


def _with_trailing_slash(value: str) -> str:
    return value if value.endswith("/") else f"{value}/"
