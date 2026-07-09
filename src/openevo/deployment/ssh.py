from __future__ import annotations

import re
import shlex
import socket
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from openevo.deployment.preflight import RemoteCommandResult
from openevo.deployment.profile import RemoteProfileConfig

CompletedRunner = Callable[[list[str], float], subprocess.CompletedProcess[str]]
PortAllocator = Callable[[], int]
TunnelStarter = Callable[[list[str]], "TunnelProcess"]


class TunnelProcess(Protocol):
    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REMOTE_HOST_RE = re.compile(r"^[A-Za-z0-9._%-]+$")
_REMOTE_PATH_RE = re.compile(r"^/[A-Za-z0-9._/@%+=,-]*$")
_REMOTE_USER_RE = re.compile(r"^[A-Za-z0-9._%+-]+$")


@dataclass(frozen=True)
class SshTunnel:
    local_port: int
    remote_port: int
    local_host: str
    remote_host: str
    process: TunnelProcess

    @property
    def base_url(self) -> str:
        return f"http://{self.local_host}:{self.local_port}"

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5.0)


class SshRemoteExecutorTransport:
    def __init__(
        self,
        profile: RemoteProfileConfig,
        *,
        runner: CompletedRunner | None = None,
        tunnel_starter: TunnelStarter | None = None,
        port_allocator: PortAllocator | None = None,
    ) -> None:
        _validate_supported_auth(profile)
        _validate_remote_identity(profile.user, "user", _REMOTE_USER_RE)
        _validate_remote_identity(profile.host, "host", _REMOTE_HOST_RE)
        self._profile = profile
        self._runner = runner or _run_subprocess
        self._tunnel_starter = tunnel_starter or _start_tunnel_subprocess
        self._port_allocator = port_allocator or _allocate_local_port

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

    def open_tunnel(
        self,
        *,
        remote_port: int,
        local_port: int | None = None,
        remote_host: str = "127.0.0.1",
        local_host: str = "127.0.0.1",
        wait_for_ready: bool = True,
        timeout_seconds: float = 10.0,
    ) -> SshTunnel:
        _validate_port(remote_port, "remote_port")
        if local_port is None:
            local_port = self._port_allocator()
        _validate_port(local_port, "local_port")
        _validate_remote_identity(remote_host, "remote_host", _REMOTE_HOST_RE)
        _validate_remote_identity(local_host, "local_host", _REMOTE_HOST_RE)
        forward_spec = f"{local_host}:{local_port}:{remote_host}:{remote_port}"
        process = self._tunnel_starter(
            [
                *self._ssh_base_argv(),
                "-N",
                "-L",
                forward_spec,
                "--",
                self._profile.host,
            ]
        )
        tunnel = SshTunnel(
            local_port=local_port,
            remote_port=remote_port,
            local_host=local_host,
            remote_host=remote_host,
            process=process,
        )
        if wait_for_ready:
            try:
                _wait_for_local_port(
                    tunnel,
                    process=process,
                    timeout_seconds=timeout_seconds,
                )
            except Exception:
                tunnel.close()
                raise
        return tunnel

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


def _start_tunnel_subprocess(argv: list[str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )


def _allocate_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_local_port(
    tunnel: SshTunnel,
    *,
    process: TunnelProcess,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(
                "SSH tunnel exited before it became ready "
                f"(return code {return_code})."
            )
        try:
            with socket.create_connection(
                (tunnel.local_host, tunnel.local_port),
                timeout=0.25,
            ):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(
        "SSH tunnel did not become ready within "
        f"{timeout_seconds:.1f}s on {tunnel.local_host}:{tunnel.local_port}."
    )


def _validate_supported_auth(profile: RemoteProfileConfig) -> None:
    if profile.auth.method == "password_ref":
        raise ValueError("SSH transport does not support password_ref auth yet")
    if profile.auth.passphrase_ref is not None:
        raise ValueError("SSH transport does not support passphrase_ref auth yet")


def _validate_remote_identity(value: str, field_name: str, pattern: re.Pattern[str]) -> None:
    if value.startswith("-") or not pattern.fullmatch(value):
        raise ValueError(f"remote profile {field_name} contains unsupported characters")


def _validate_port(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not 1 <= int(value) <= 65535:
        raise ValueError(f"{field_name} must be a TCP port between 1 and 65535")


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
    env_export = _env_export(env or {})
    if env_export:
        pieces.append(env_export)
    pieces.append(command)
    return " && ".join(pieces)


def _env_export(env: dict[str, str]) -> str:
    assignments: list[str] = []
    for key, value in env.items():
        if not _ENV_KEY_RE.fullmatch(key):
            raise ValueError(f"invalid remote environment key: {key!r}")
        assignments.append(f"{key}={shlex.quote(value)}")
    if not assignments:
        return ""
    return "export " + " ".join(assignments)


def _validate_remote_absolute_path(path: str, field_name: str) -> None:
    if not path.startswith("/"):
        raise ValueError(f"{field_name} must be an absolute remote path")
    if any(ord(character) < 32 or ord(character) == 127 for character in path):
        raise ValueError(f"{field_name} must not contain control characters")
    if not _REMOTE_PATH_RE.fullmatch(path):
        raise ValueError(f"{field_name} contains unsupported characters")


def _with_trailing_slash(value: str) -> str:
    return value if value.endswith("/") else f"{value}/"
