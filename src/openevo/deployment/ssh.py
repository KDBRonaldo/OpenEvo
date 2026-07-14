from __future__ import annotations

import ipaddress
import logging
import re
import secrets
import shlex
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from enum import Enum
from pathlib import Path
from typing import Protocol

from openevo.deployment.host_keys import TrustedKnownHostsBinding
from openevo.deployment.preflight import RemoteCommandResult
from openevo.deployment.profile import RemoteProfileConfig

CompletedRunner = Callable[[list[str], float], subprocess.CompletedProcess[str]]
PortAllocator = Callable[[], int]
TunnelStarter = Callable[[list[str]], "TunnelProcess"]

logger = logging.getLogger(__name__)

_TUNNEL_CLOSE_GRACE_SECONDS = 1.0
_TUNNEL_KILL_GRACE_SECONDS = 1.0
_TUNNEL_MONITOR_INTERVAL_SECONDS = 0.05


class SshTransportErrorCode(str, Enum):
    HOST_KEY_VERIFICATION_FAILED = "host_key_verification_failed"
    CONNECTION_FAILED = "connection_failed"
    START_FAILED = "start_failed"
    RSYNC_FAILED = "rsync_failed"
    INVALID_REQUEST = "invalid_ssh_request"
    TIMEOUT = "ssh_timeout"


class SshTransportError(RuntimeError):
    """A renderer-safe typed SSH transport failure."""

    def __init__(self, code: SshTransportErrorCode) -> None:
        self.code = code
        messages = {
            SshTransportErrorCode.HOST_KEY_VERIFICATION_FAILED: (
                "SSH host-key verification failed. Re-verify the server fingerprint."
            ),
            SshTransportErrorCode.CONNECTION_FAILED: "SSH connection failed.",
            SshTransportErrorCode.START_FAILED: "SSH process could not be started.",
            SshTransportErrorCode.RSYNC_FAILED: "rsync failed over SSH.",
            SshTransportErrorCode.INVALID_REQUEST: "SSH request is invalid.",
            SshTransportErrorCode.TIMEOUT: "SSH operation timed out.",
        }
        super().__init__(messages[code])


class TunnelProcess(Protocol):
    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REMOTE_HOST_RE = re.compile(r"^[A-Za-z0-9._%-]+$")
_REMOTE_PATH_RE = re.compile(r"^/[A-Za-z0-9._/@%+=,-]*$")
_REMOTE_USER_RE = re.compile(r"^[A-Za-z0-9._%+-]+$")


class SshTunnel(AbstractContextManager["SshTunnel"]):
    """A bounded, idempotently closable SSH forwarding process."""

    def __init__(
        self,
        *,
        local_port: int,
        remote_port: int,
        local_host: str,
        remote_host: str,
        process: TunnelProcess,
        trust_lease: AbstractContextManager[Path],
        trusted_host: TrustedKnownHostsBinding,
    ) -> None:
        self.local_port = local_port
        self.remote_port = remote_port
        self.local_host = local_host
        self.remote_host = remote_host
        self.process = process
        self._trust_lease: AbstractContextManager[Path] | None = trust_lease
        self._state_guard = threading.Lock()
        self._close_guard = threading.Lock()
        self._closed = threading.Event()
        self._close_requested = False
        self._unregister: Callable[[], None] | None = None
        self._monitor: threading.Thread | None = None
        construction_failed = False
        try:
            self._unregister = trusted_host._register_tunnel(self.request_close)
            self._monitor = threading.Thread(
                target=self._monitor_exit,
                name="openevo-ssh-tunnel-monitor",
                daemon=True,
            )
            self._monitor.start()
        except Exception:
            construction_failed = True
        if construction_failed:
            self._rollback_failed_construction()
            raise SshTransportError(SshTransportErrorCode.START_FAILED)

    @property
    def base_url(self) -> str:
        return f"http://{self.local_host}:{self.local_port}"

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def __enter__(self) -> SshTunnel:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def request_close(self) -> None:
        """Request termination without waiting; used before trust mutation."""

        with self._state_guard:
            if self._closed.is_set() or self._close_requested:
                return
            self._close_requested = True
        if _tunnel_process_is_running(self.process):
            try:
                self.process.terminate()
            except Exception:
                _log_transport_failure(SshTransportErrorCode.CONNECTION_FAILED)

    def close(self) -> None:
        with self._close_guard:
            self._close_once()

    def _close_once(self) -> None:
        self.request_close()
        if self._closed.is_set():
            return
        try:
            self.process.wait(timeout=_TUNNEL_CLOSE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            _log_transport_failure(SshTransportErrorCode.CONNECTION_FAILED)
            return
        else:
            self._finalize()
            return
        if _tunnel_process_is_running(self.process):
            try:
                self.process.kill()
            except Exception:
                _log_transport_failure(SshTransportErrorCode.CONNECTION_FAILED)
        try:
            self.process.wait(timeout=_TUNNEL_KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            return
        except Exception:
            _log_transport_failure(SshTransportErrorCode.CONNECTION_FAILED)
            return
        self._finalize()

    def _monitor_exit(self) -> None:
        while not self._closed.wait(_TUNNEL_MONITOR_INTERVAL_SECONDS):
            try:
                if self.process.poll() is not None:
                    self._finalize()
                    return
            except Exception:
                _log_transport_failure(SshTransportErrorCode.CONNECTION_FAILED)
                return

    def _rollback_failed_construction(self) -> None:
        with self._state_guard:
            unregister = self._unregister
            self._unregister = None
            trust_lease = self._trust_lease
            self._trust_lease = None
            self._closed.set()
        _call_without_error(unregister)
        _shutdown_tunnel_process(self.process)
        _release_trust_lease(trust_lease)

    def _finalize(self) -> None:
        with self._state_guard:
            if self._closed.is_set():
                return
            unregister = self._unregister
            self._unregister = None
            trust_lease = self._trust_lease
            self._trust_lease = None
            self._closed.set()
        if unregister is not None:
            _call_without_error(unregister)
        _release_trust_lease(trust_lease)


class SshRemoteExecutorTransport:
    """Execute SSH operations against one explicitly trusted host-key binding."""

    def __init__(
        self,
        profile: RemoteProfileConfig,
        *,
        trusted_host: TrustedKnownHostsBinding | None = None,
        runner: CompletedRunner | None = None,
        tunnel_starter: TunnelStarter | None = None,
        port_allocator: PortAllocator | None = None,
    ) -> None:
        invalid_request = False
        try:
            _validate_supported_auth(profile)
            _validate_remote_identity(profile.user, "user", _REMOTE_USER_RE)
            _validate_remote_host(profile.host)
            _validate_port(profile.port, "remote profile port")
        except Exception:
            invalid_request = True
        if invalid_request or trusted_host is None:
            raise SshTransportError(SshTransportErrorCode.INVALID_REQUEST)
        host_key_invalid = False
        try:
            trusted_host.validate_for(profile)
        except Exception:
            host_key_invalid = True
        if host_key_invalid:
            raise SshTransportError(SshTransportErrorCode.HOST_KEY_VERIFICATION_FAILED)
        self._profile = profile
        self._trusted_host = trusted_host
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
        invalid_request = False
        try:
            remote_command = _remote_command(command, cwd=cwd, env=env)
        except Exception:
            invalid_request = True
            remote_command = ""
        if invalid_request:
            raise SshTransportError(SshTransportErrorCode.INVALID_REQUEST)
        completion_marker = f"__OPENEVO_REMOTE_COMPLETION_{secrets.token_hex(16)}__="
        marked_command = _with_completion_marker(remote_command, completion_marker)
        failure_code: SshTransportErrorCode | None = None
        completed: subprocess.CompletedProcess[str] | None = None
        phase = "trust"
        try:
            with self._trusted_host.open_for_spawn(self._profile) as known_hosts_file:
                phase = "process"
                completed = self._runner(
                    self._ssh_argv(marked_command, known_hosts_file), timeout_seconds
                )
                phase = "trust"
        except subprocess.TimeoutExpired:
            failure_code = SshTransportErrorCode.TIMEOUT
        except Exception:
            failure_code = (
                SshTransportErrorCode.START_FAILED
                if phase == "process"
                else SshTransportErrorCode.HOST_KEY_VERIFICATION_FAILED
            )
        if failure_code is not None:
            _log_transport_failure(failure_code)
            raise SshTransportError(failure_code)
        assert completed is not None
        stderr, remote_return_code = _extract_remote_completion(
            completed.stderr or "",
            completion_marker,
        )
        if completed.returncode == 255 and remote_return_code is None:
            _log_transport_failure(SshTransportErrorCode.CONNECTION_FAILED)
            raise SshTransportError(SshTransportErrorCode.CONNECTION_FAILED)
        if remote_return_code is not None and int(completed.returncode) != remote_return_code:
            _log_transport_failure(SshTransportErrorCode.CONNECTION_FAILED)
            raise SshTransportError(SshTransportErrorCode.CONNECTION_FAILED)
        stderr = _redact_trust_paths(
            stderr,
            known_hosts_file,
            self._trusted_host.known_hosts_file,
        )
        return RemoteCommandResult(
            command=command,
            return_code=(
                remote_return_code
                if remote_return_code is not None
                else int(completed.returncode)
            ),
            stdout=completed.stdout or "",
            stderr=stderr,
        )

    def upload_dir(self, local_path: str, remote_path: str) -> None:
        invalid_request = False
        try:
            local = Path(local_path).expanduser()
            invalid_request = not local.exists() or not local.is_dir()
            _validate_remote_absolute_path(remote_path, "remote_path")
        except Exception:
            invalid_request = True
            local = Path(".")
        if invalid_request:
            raise SshTransportError(SshTransportErrorCode.INVALID_REQUEST)

        mkdir_result = self.run(f"mkdir -p {shlex.quote(remote_path)}")
        if not mkdir_result.ok:
            _log_transport_failure(SshTransportErrorCode.RSYNC_FAILED)
            raise SshTransportError(SshTransportErrorCode.RSYNC_FAILED)

        failure_code: SshTransportErrorCode | None = None
        completed: subprocess.CompletedProcess[str] | None = None
        phase = "trust"
        try:
            with self._trusted_host.open_for_spawn(self._profile) as known_hosts_file:
                phase = "process"
                completed = self._runner(
                    self._rsync_argv(local, remote_path, known_hosts_file), 300.0
                )
                phase = "trust"
        except subprocess.TimeoutExpired:
            failure_code = SshTransportErrorCode.TIMEOUT
        except Exception:
            failure_code = (
                SshTransportErrorCode.RSYNC_FAILED
                if phase == "process"
                else SshTransportErrorCode.HOST_KEY_VERIFICATION_FAILED
            )
        if failure_code is not None:
            _log_transport_failure(failure_code)
            raise SshTransportError(failure_code)
        assert completed is not None
        if completed.returncode != 0:
            _log_transport_failure(SshTransportErrorCode.RSYNC_FAILED)
            raise SshTransportError(SshTransportErrorCode.RSYNC_FAILED)

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
        invalid_request = False
        try:
            _validate_port(remote_port, "remote_port")
            if local_port is None:
                local_port = self._port_allocator()
            _validate_port(local_port, "local_port")
            _validate_remote_identity(remote_host, "remote_host", _REMOTE_HOST_RE)
            _validate_remote_identity(local_host, "local_host", _REMOTE_HOST_RE)
        except Exception:
            invalid_request = True
        if invalid_request or local_port is None:
            raise SshTransportError(SshTransportErrorCode.INVALID_REQUEST)
        forward_spec = f"{local_host}:{local_port}:{remote_host}:{remote_port}"
        trust_lease = self._trusted_host.open_for_spawn(self._profile)
        lease_failed = False
        try:
            known_hosts_file = trust_lease.__enter__()
        except Exception:
            lease_failed = True
            known_hosts_file = Path(".")
        if lease_failed:
            raise SshTransportError(SshTransportErrorCode.HOST_KEY_VERIFICATION_FAILED)
        start_failed = False
        try:
            process = self._tunnel_starter(
                [
                    *self._ssh_base_argv(known_hosts_file),
                    "-N",
                    "-L",
                    forward_spec,
                    "--",
                    self._profile.host,
                ]
            )
        except Exception:
            start_failed = True
            process = None
        if start_failed or process is None:
            _release_trust_lease(trust_lease)
            _log_transport_failure(SshTransportErrorCode.START_FAILED)
            raise SshTransportError(SshTransportErrorCode.START_FAILED)
        tunnel_failed = False
        try:
            tunnel = SshTunnel(
                local_port=local_port,
                remote_port=remote_port,
                local_host=local_host,
                remote_host=remote_host,
                process=process,
                trust_lease=trust_lease,
                trusted_host=self._trusted_host,
            )
        except Exception:
            tunnel_failed = True
            tunnel = None
        if tunnel_failed or tunnel is None:
            _log_transport_failure(SshTransportErrorCode.START_FAILED)
            raise SshTransportError(SshTransportErrorCode.START_FAILED)
        if wait_for_ready:
            ready_failure: SshTransportErrorCode | None = None
            try:
                _wait_for_local_port(
                    tunnel,
                    process=process,
                    timeout_seconds=timeout_seconds,
                )
            except TimeoutError:
                ready_failure = SshTransportErrorCode.TIMEOUT
            except Exception:
                ready_failure = SshTransportErrorCode.CONNECTION_FAILED
            if ready_failure is not None:
                tunnel.close()
                _log_transport_failure(ready_failure)
                raise SshTransportError(ready_failure)
        return tunnel

    def _ssh_argv(self, remote_command: str, known_hosts_file: Path) -> list[str]:
        return [
            *self._ssh_base_argv(known_hosts_file),
            "--",
            self._profile.host,
            remote_command,
        ]

    def _ssh_base_argv(self, known_hosts_file: Path) -> list[str]:
        profile = self._profile
        trusted_host = self._trusted_host
        argv = ["ssh", "-F", "/dev/null", "-p", str(profile.port)]
        if profile.auth.method == "private_key":
            key_path = Path(str(profile.auth.private_key_path)).expanduser()
            argv.extend(
                [
                    "-o",
                    "IdentityFile=none",
                    "-i",
                    str(key_path),
                    "-o",
                    "IdentitiesOnly=yes",
                    "-o",
                    "IdentityAgent=none",
                ]
            )
        argv.extend(
            [
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                f"UserKnownHostsFile={known_hosts_file}",
                "-o",
                "GlobalKnownHostsFile=/dev/null",
                "-o",
                "UpdateHostKeys=no",
                "-o",
                "CheckHostIP=no",
                "-o",
                "VerifyHostKeyDNS=no",
                "-o",
                "KnownHostsCommand=none",
                "-o",
                "HashKnownHosts=no",
                "-o",
                f"HostKeyAlgorithms={trusted_host.algorithm}",
                "-o",
                "BatchMode=yes",
                "-l",
                profile.user,
            ]
        )
        return argv

    def _rsync_argv(
        self,
        local: Path,
        remote_path: str,
        known_hosts_file: Path,
    ) -> list[str]:
        ssh_command = " ".join(
            shlex.quote(part) for part in self._ssh_base_argv(known_hosts_file)
        )
        return [
            "rsync",
            "-az",
            "--delete",
            "-e",
            ssh_command,
            _with_trailing_slash(str(local)),
            (f"{_rsync_host(self._profile.host)}:{_with_trailing_slash(remote_path)}"),
        ]


def _run_subprocess(argv: list[str], timeout_seconds: float) -> subprocess.CompletedProcess[str]:
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


def _shutdown_tunnel_process(process: TunnelProcess) -> None:
    try:
        process.terminate()
    except Exception:
        pass
    try:
        process.wait(timeout=_TUNNEL_CLOSE_GRACE_SECONDS)
        return
    except Exception:
        pass
    try:
        process.kill()
    except Exception:
        pass
    try:
        process.wait(timeout=_TUNNEL_KILL_GRACE_SECONDS)
    except Exception:
        pass


def _tunnel_process_is_running(process: TunnelProcess) -> bool:
    try:
        return process.poll() is None
    except Exception:
        _log_transport_failure(SshTransportErrorCode.CONNECTION_FAILED)
        return True


def _release_trust_lease(trust_lease: AbstractContextManager[Path] | None) -> None:
    if trust_lease is None:
        return
    try:
        trust_lease.__exit__(None, None, None)
    except Exception:
        _log_transport_failure(SshTransportErrorCode.HOST_KEY_VERIFICATION_FAILED)


def _call_without_error(callback: Callable[[], None] | None) -> None:
    if callback is None:
        return
    try:
        callback()
    except Exception:
        _log_transport_failure(SshTransportErrorCode.CONNECTION_FAILED)


def _log_transport_failure(
    code: SshTransportErrorCode,
) -> None:
    logger.warning(
        "ssh_transport_failure code=%s diagnostic_id=%s",
        code.value,
        secrets.token_hex(12),
    )


def _with_completion_marker(command: str, marker: str) -> str:
    return (
        f"(\n{command}\n)\n"
        "__openevo_remote_status=$?\n"
        f"printf '\\n%s%s\\n' {shlex.quote(marker)} "
        '"$__openevo_remote_status" >&2\n'
        'exit "$__openevo_remote_status"'
    )


def _extract_remote_completion(stderr: str, marker: str) -> tuple[str, int | None]:
    match = re.search(rf"\n{re.escape(marker)}([0-9]{{1,3}})\n?\Z", stderr)
    if match is None:
        return stderr, None
    return_code = int(match.group(1))
    if return_code > 255:
        return stderr, None
    return stderr[: match.start()], return_code


def _redact_trust_paths(stderr: str, *paths: Path) -> str:
    redacted = stderr
    for path in paths:
        redacted = redacted.replace(str(path), "[SSH_TRUST_STORE]")
    return redacted


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
                f"SSH tunnel exited before it became ready (return code {return_code})."
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


def _validate_remote_host(value: str) -> None:
    if ":" not in value:
        _validate_remote_identity(value, "host", _REMOTE_HOST_RE)
        return
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError("remote profile host contains unsupported characters") from exc
    if parsed.version != 6:
        raise ValueError("remote profile host contains unsupported characters")


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


def _rsync_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host
