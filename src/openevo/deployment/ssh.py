from __future__ import annotations

import ipaddress
import logging
import os
import re
import secrets
import shlex
import socket
import stat
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from enum import Enum
from pathlib import Path
from typing import Protocol

from pydantic import SecretStr

from openevo.deployment.host_keys import TrustedKnownHostsBinding
from openevo.deployment.preflight import RemoteCommandResult
from openevo.deployment.profile import RemoteProfileConfig

CompletedRunner = Callable[[list[str], float], subprocess.CompletedProcess[str]]
PortAllocator = Callable[[], int]
TunnelStarter = Callable[[list[str]], "TunnelProcess"]
CoreConnectionStarter = Callable[[list[str], int], "TunnelProcess"]

logger = logging.getLogger(__name__)

_TUNNEL_CLOSE_GRACE_SECONDS = 1.0
_TUNNEL_KILL_GRACE_SECONDS = 1.0
_TUNNEL_MONITOR_INTERVAL_SECONDS = 0.05
_ORPHANED_TUNNEL_GUARD = threading.Lock()
_ORPHANED_TUNNELS: dict[int, "SshTunnel"] = {}
_ORPHANED_CORE_TUNNELS: dict[int, "_CoreTunnelEndpoint"] = {}
_ORPHANED_TRUST_LEASES: dict[int, AbstractContextManager[Path]] = {}


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


class _CoreTunnelEndpoint:
    def __init__(
        self,
        *,
        connection_starter: CoreConnectionStarter,
        connection_argv: list[str],
        trust_lease: AbstractContextManager[Path],
        trusted_host: TrustedKnownHostsBinding,
    ) -> None:
        self._connection_starter = connection_starter
        self._connection_argv = connection_argv
        self._trust_lease: AbstractContextManager[Path] | None = trust_lease
        self._guard = threading.RLock()
        self._close_guard = threading.Lock()
        self._children: dict[int, TunnelProcess] = {}
        self._next_generation = 0
        self._close_requested = False
        self._finalized = threading.Event()
        self._orphaned = False
        self._unregister: Callable[[], None] | None = None
        try:
            self._unregister = trusted_host._register_tunnel(self.close)
        except BaseException:
            try:
                self._finalize()
            except BaseException:
                pass
            raise

    def verify_authority(self, *, timeout_seconds: float = 1.0) -> None:
        del timeout_seconds
        with self._guard:
            self._verify_locked()

    def open_verified_socket(self, *, timeout_seconds: float) -> socket.socket:
        if not 0 < timeout_seconds <= 60:
            raise SshTransportError(SshTransportErrorCode.INVALID_REQUEST)
        with self._guard:
            self._verify_locked()
            local_stream, child_stream = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            process: TunnelProcess | None = None
            generation: int | None = None
            try:
                local_identity = _require_parent_owned_stream(local_stream)
                child_identity = _require_parent_owned_stream(child_stream)
                local_stream.settimeout(timeout_seconds)
                process = self._connection_starter(
                    list(self._connection_argv),
                    child_stream.fileno(),
                )
                generation = self._next_generation
                self._next_generation += 1
                self._children[generation] = process
                _require_parent_owned_stream(
                    local_stream,
                    expected_identity=local_identity,
                )
                _require_parent_owned_stream(
                    child_stream,
                    expected_identity=child_identity,
                )
                child_stream.close()
                if not _tunnel_process_is_running(process):
                    raise SshTransportError(SshTransportErrorCode.CONNECTION_FAILED)
                _require_parent_owned_stream(
                    local_stream,
                    expected_identity=local_identity,
                )
                return local_stream
            except BaseException as exc:
                local_stream.close()
                child_stream.close()
                if process is not None and generation is not None:
                    exited, _failure = _terminate_tunnel_process(process)
                    if exited:
                        self._children.pop(generation, None)
                    else:
                        self._close_requested = True
                        self._retain_orphan()
                if isinstance(exc, Exception):
                    _log_transport_failure(SshTransportErrorCode.CONNECTION_FAILED)
                    raise SshTransportError(SshTransportErrorCode.CONNECTION_FAILED) from None
                raise

    @property
    def closed(self) -> bool:
        return self._finalized.is_set()

    def close(self) -> None:
        with self._close_guard:
            if self._finalized.is_set():
                return
            failures: list[BaseException] = []
            with self._guard:
                self._close_requested = True
                children = tuple(self._children.values())
            all_exited = True
            for process in children:
                exited, failure = _terminate_tunnel_process(process)
                all_exited = all_exited and exited
                if failure is not None:
                    failures.append(failure)
            if all_exited:
                self._finalize()
            else:
                self._retain_orphan()
            if failures:
                raise failures[0]

    def _verify_locked(self) -> None:
        if self._close_requested or self._finalized.is_set():
            raise SshTransportError(SshTransportErrorCode.CONNECTION_FAILED)
        completed: list[int] = []
        for generation, process in self._children.items():
            try:
                return_code = process.poll()
            except BaseException:
                raise SshTransportError(SshTransportErrorCode.CONNECTION_FAILED) from None
            if return_code is None:
                continue
            if return_code != 0:
                raise SshTransportError(SshTransportErrorCode.CONNECTION_FAILED)
            completed.append(generation)
        for generation in completed:
            self._children.pop(generation, None)

    def _retain_orphan(self) -> None:
        with self._guard:
            if self._finalized.is_set() or self._orphaned:
                return
            self._orphaned = True
        with _ORPHANED_TUNNEL_GUARD:
            _ORPHANED_CORE_TUNNELS[id(self)] = self

    def _finalize(self) -> None:
        with self._guard:
            if self._finalized.is_set():
                return
            unregister = self._unregister
            trust_lease = self._trust_lease
        failure: BaseException | None = None
        unregister_complete = unregister is None
        lease_complete = trust_lease is None
        if unregister is not None:
            try:
                unregister()
                unregister_complete = True
            except BaseException as exc:
                failure = exc
        if trust_lease is not None:
            try:
                trust_lease.__exit__(None, None, None)
                lease_complete = True
            except BaseException as exc:
                if failure is None:
                    failure = exc
        with self._guard:
            if unregister_complete:
                self._unregister = None
            if lease_complete:
                self._trust_lease = None
            finalized = self._unregister is None and self._trust_lease is None
            if finalized:
                self._children.clear()
                self._orphaned = False
                self._finalized.set()
        if finalized:
            with _ORPHANED_TUNNEL_GUARD:
                _ORPHANED_CORE_TUNNELS.pop(id(self), None)
        else:
            self._retain_orphan()
        if failure is not None:
            raise failure


class SshCoreTunnel(AbstractContextManager["SshCoreTunnel"]):
    """Parent-owned per-connection forwarding for authenticated Core traffic."""

    base_url = "http://openevo-core.local"

    def __init__(self, endpoint: _CoreTunnelEndpoint) -> None:
        self._endpoint = endpoint

    @property
    def closed(self) -> bool:
        return self._endpoint.closed

    def verify_authority(self) -> None:
        self._endpoint.verify_authority()

    def open_verified_socket(self, *, timeout_seconds: float) -> socket.socket:
        return self._endpoint.open_verified_socket(timeout_seconds=timeout_seconds)

    def close(self) -> None:
        self._endpoint.close()

    def __enter__(self) -> SshCoreTunnel:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


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
        on_finalize: Callable[[], None] | None = None,
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
        self._orphaned = False
        self._unregister: Callable[[], None] | None = None
        self._monitor: threading.Thread | None = None
        self._on_finalize = on_finalize
        try:
            self._unregister = trusted_host._register_tunnel(self._request_registered_close)
            self._monitor = threading.Thread(
                target=self._monitor_exit,
                name="openevo-ssh-tunnel-monitor",
                daemon=True,
            )
            self._monitor.start()
        except BaseException as exc:
            self._rollback_failed_construction()
            if isinstance(exc, Exception):
                raise SshTransportError(SshTransportErrorCode.START_FAILED) from None
            raise

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

        self._request_process_termination(retry=False)

    def _request_process_termination(self, *, retry: bool) -> None:
        with self._state_guard:
            if self._closed.is_set() or (self._close_requested and not retry):
                return
            self._close_requested = True
        probe_failure: BaseException | None = None
        try:
            running = _tunnel_process_is_running(self.process)
        except BaseException as exc:
            running = True
            probe_failure = exc
        if running:
            try:
                self.process.terminate()
            except BaseException as exc:
                self._retain_orphan()
                _log_transport_failure(SshTransportErrorCode.CONNECTION_FAILED)
                if not isinstance(exc, Exception):
                    raise
        if probe_failure is not None:
            _log_transport_failure(SshTransportErrorCode.CONNECTION_FAILED)
            if not isinstance(probe_failure, Exception):
                raise probe_failure

    def _request_registered_close(self) -> None:
        with self._state_guard:
            orphaned = self._orphaned
        if orphaned:
            self.close()
        else:
            self.request_close()

    def close(self) -> None:
        with self._close_guard:
            self._close_once()

    def _close_once(self) -> None:
        if self._closed.is_set():
            return
        with self._state_guard:
            self._close_requested = True
        exited, failure = _terminate_tunnel_process(self.process)
        if exited:
            self._finalize()
        else:
            self._retain_orphan()
        if failure is not None:
            raise failure

    def _monitor_exit(self) -> None:
        while not self._closed.wait(_TUNNEL_MONITOR_INTERVAL_SECONDS):
            try:
                if self.process.poll() is not None:
                    self._finalize()
                    return
            except Exception:
                _log_transport_failure(SshTransportErrorCode.CONNECTION_FAILED)
                continue

    def _rollback_failed_construction(self) -> None:
        try:
            self.close()
        except BaseException:
            pass
        if not self._closed.is_set():
            self._retain_orphan()

    def _retain_orphan(self) -> None:
        with self._state_guard:
            if self._closed.is_set() or self._orphaned:
                return
            self._orphaned = True
        with _ORPHANED_TUNNEL_GUARD:
            _ORPHANED_TUNNELS[id(self)] = self
        try:
            monitor = threading.Thread(
                target=self._monitor_orphan_cleanup,
                name="openevo-ssh-orphan-monitor",
                daemon=True,
            )
            monitor.start()
        except Exception:
            _log_transport_failure(SshTransportErrorCode.CONNECTION_FAILED)

    def _monitor_orphan_cleanup(self) -> None:
        while not self._closed.wait(_TUNNEL_MONITOR_INTERVAL_SECONDS):
            try:
                self.close()
            except BaseException:
                _log_transport_failure(SshTransportErrorCode.CONNECTION_FAILED)

    def _finalize(self) -> None:
        with self._state_guard:
            if self._closed.is_set():
                return
            unregister = self._unregister
            trust_lease = self._trust_lease
            on_finalize = self._on_finalize
        failure: BaseException | None = None
        unregister_complete = unregister is None
        lease_complete = trust_lease is None
        callback_complete = on_finalize is None
        if unregister is not None:
            try:
                unregister()
                unregister_complete = True
            except BaseException as exc:
                failure = exc
        if trust_lease is not None:
            try:
                trust_lease.__exit__(None, None, None)
                lease_complete = True
            except BaseException as exc:
                if failure is None:
                    failure = exc
        if on_finalize is not None:
            try:
                on_finalize()
                callback_complete = True
            except BaseException as exc:
                if failure is None:
                    failure = exc
        with self._state_guard:
            if unregister_complete:
                self._unregister = None
            if lease_complete:
                self._trust_lease = None
            if callback_complete:
                self._on_finalize = None
            finalized = (
                self._unregister is None
                and self._trust_lease is None
                and self._on_finalize is None
            )
            if finalized:
                self._orphaned = False
                self._closed.set()
        if finalized:
            _forget_orphaned_tunnel(self)
        else:
            self._retain_orphan()
        if failure is not None:
            raise failure


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
        core_connection_starter: CoreConnectionStarter | None = None,
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
        self._core_connection_starter = (
            core_connection_starter or _start_core_connection_subprocess
        )

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
                remote_return_code if remote_return_code is not None else int(completed.returncode)
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

    def run_secret(
        self,
        command: str,
        *,
        timeout_seconds: float = 30.0,
    ) -> SecretStr:
        try:
            remote_command = _remote_command(command, cwd=None, env=None)
        except Exception:
            raise SshTransportError(SshTransportErrorCode.INVALID_REQUEST) from None
        completion_marker = f"__OPENEVO_REMOTE_COMPLETION_{secrets.token_hex(16)}__="
        marked_command = _with_completion_marker(remote_command, completion_marker)
        completed: subprocess.CompletedProcess[str] | None = None
        phase = "trust"
        failure_code: SshTransportErrorCode | None = None
        try:
            with self._trusted_host.open_for_spawn(self._profile) as known_hosts_file:
                phase = "process"
                completed = self._runner(
                    self._ssh_argv(marked_command, known_hosts_file),
                    timeout_seconds,
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
        _stderr, remote_return_code = _extract_remote_completion(
            completed.stderr or "",
            completion_marker,
        )
        if (
            remote_return_code is None
            or completed.returncode == 255
            or int(completed.returncode) != remote_return_code
            or remote_return_code != 0
        ):
            _log_transport_failure(SshTransportErrorCode.CONNECTION_FAILED)
            raise SshTransportError(SshTransportErrorCode.CONNECTION_FAILED)
        return SecretStr(completed.stdout or "")

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
        _retry_orphaned_tunnel_cleanup()
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
        try:
            known_hosts_file = trust_lease.__enter__()
        except BaseException as exc:
            _release_trust_lease(trust_lease)
            if isinstance(exc, Exception):
                raise SshTransportError(
                    SshTransportErrorCode.HOST_KEY_VERIFICATION_FAILED
                ) from None
            raise
        try:
            process = self._tunnel_starter(
                [
                    *self._ssh_base_argv(known_hosts_file),
                    "-o",
                    "ExitOnForwardFailure=yes",
                    "-N",
                    "-L",
                    forward_spec,
                    "--",
                    self._profile.host,
                ]
            )
        except BaseException as exc:
            _release_trust_lease(trust_lease)
            if isinstance(exc, Exception):
                _log_transport_failure(SshTransportErrorCode.START_FAILED)
                raise SshTransportError(SshTransportErrorCode.START_FAILED) from None
            raise
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
                try:
                    tunnel.close()
                except BaseException:
                    pass
                _log_transport_failure(ready_failure)
                raise SshTransportError(ready_failure)
        return tunnel

    def open_core_tunnel(
        self,
        *,
        remote_port: int,
        remote_host: str = "127.0.0.1",
        wait_for_ready: bool = True,
        timeout_seconds: float = 10.0,
    ) -> SshCoreTunnel:
        _retry_orphaned_tunnel_cleanup()
        try:
            _validate_port(remote_port, "remote_port")
            _validate_remote_identity(remote_host, "remote_host", _REMOTE_HOST_RE)
            if not 0 < timeout_seconds <= 60:
                raise ValueError("invalid timeout")
        except Exception:
            raise SshTransportError(SshTransportErrorCode.INVALID_REQUEST) from None

        trust_lease = self._trusted_host.open_for_spawn(self._profile)
        try:
            known_hosts_file = trust_lease.__enter__()
        except BaseException as exc:
            _release_trust_lease(trust_lease)
            if isinstance(exc, Exception):
                raise SshTransportError(
                    SshTransportErrorCode.HOST_KEY_VERIFICATION_FAILED
                ) from None
            raise
        connection_argv = [
            *self._ssh_base_argv(known_hosts_file),
            "-o",
            "ExitOnForwardFailure=yes",
            "-W",
            f"{remote_host}:{remote_port}",
            "--",
            self._profile.host,
        ]
        try:
            endpoint = _CoreTunnelEndpoint(
                connection_starter=self._core_connection_starter,
                connection_argv=connection_argv,
                trust_lease=trust_lease,
                trusted_host=self._trusted_host,
            )
        except BaseException as exc:
            if isinstance(exc, Exception):
                _log_transport_failure(SshTransportErrorCode.START_FAILED)
                raise SshTransportError(SshTransportErrorCode.START_FAILED) from None
            raise
        tunnel = SshCoreTunnel(endpoint)
        if wait_for_ready:
            try:
                endpoint.verify_authority(timeout_seconds=timeout_seconds)
            except BaseException:
                try:
                    tunnel.close()
                except BaseException:
                    pass
                raise
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
        ssh_command = " ".join(shlex.quote(part) for part in self._ssh_base_argv(known_hosts_file))
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


def _start_core_connection_subprocess(
    argv: list[str],
    stream_fd: int,
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        argv,
        stdin=stream_fd,
        stdout=stream_fd,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        pass_fds=(stream_fd,),
        text=False,
    )


def _require_parent_owned_stream(
    stream: socket.socket,
    *,
    expected_identity: tuple[int, int, int, int] | None = None,
) -> tuple[int, int, int, int]:
    stream_fd = stream.fileno()
    metadata = os.fstat(stream_fd)
    identity = (stream_fd, metadata.st_dev, metadata.st_ino, metadata.st_ctime_ns)
    if (
        stream.family != socket.AF_UNIX
        or stream.type != socket.SOCK_STREAM
        or stream.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM
        or not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stream.getsockname() not in ("", b"")
        or stream.getpeername() not in ("", b"")
        or (expected_identity is not None and identity != expected_identity)
    ):
        raise SshTransportError(SshTransportErrorCode.CONNECTION_FAILED)
    return identity


def _terminate_tunnel_process(
    process: TunnelProcess,
) -> tuple[bool, BaseException | None]:
    failure: BaseException | None = None
    try:
        running = _tunnel_process_is_running(process)
    except BaseException as exc:
        running = True
        failure = exc
    if running:
        try:
            process.terminate()
        except BaseException as exc:
            if failure is None:
                failure = exc
    try:
        exited = _wait_for_tunnel_process_exit(
            process,
            timeout_seconds=_TUNNEL_CLOSE_GRACE_SECONDS,
        )
    except BaseException as exc:
        exited = False
        if failure is None:
            failure = exc
    try:
        running = _tunnel_process_is_running(process)
    except BaseException as exc:
        running = True
        if failure is None:
            failure = exc
    if not exited and running:
        try:
            process.kill()
        except BaseException as exc:
            if failure is None:
                failure = exc
        try:
            exited = _wait_for_tunnel_process_exit(
                process,
                timeout_seconds=_TUNNEL_KILL_GRACE_SECONDS,
            )
        except BaseException as exc:
            exited = False
            if failure is None:
                failure = exc
    return exited, failure


def _wait_for_tunnel_process_exit(
    process: TunnelProcess,
    *,
    timeout_seconds: float,
) -> bool:
    try:
        process.wait(timeout=timeout_seconds)
        return True
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        _log_transport_failure(SshTransportErrorCode.CONNECTION_FAILED)
    return not _tunnel_process_is_running(process)


def _tunnel_process_is_running(process: TunnelProcess) -> bool:
    return process.poll() is None


def _forget_orphaned_tunnel(tunnel: SshTunnel) -> None:
    with _ORPHANED_TUNNEL_GUARD:
        _ORPHANED_TUNNELS.pop(id(tunnel), None)


def _retry_orphaned_tunnel_cleanup() -> None:
    with _ORPHANED_TUNNEL_GUARD:
        tunnels = tuple(_ORPHANED_TUNNELS.values())
        core_tunnels = tuple(_ORPHANED_CORE_TUNNELS.values())
        trust_leases = tuple(_ORPHANED_TRUST_LEASES.values())
    for tunnel in tunnels:
        try:
            tunnel.close()
        except Exception:
            _log_transport_failure(SshTransportErrorCode.CONNECTION_FAILED)
    for tunnel in core_tunnels:
        try:
            tunnel.close()
        except Exception:
            _log_transport_failure(SshTransportErrorCode.CONNECTION_FAILED)
    for trust_lease in trust_leases:
        _release_trust_lease(trust_lease)


def _release_trust_lease(trust_lease: AbstractContextManager[Path] | None) -> None:
    if trust_lease is None:
        return
    try:
        trust_lease.__exit__(None, None, None)
    except BaseException:
        with _ORPHANED_TUNNEL_GUARD:
            _ORPHANED_TRUST_LEASES[id(trust_lease)] = trust_lease
        _log_transport_failure(SshTransportErrorCode.HOST_KEY_VERIFICATION_FAILED)
    else:
        with _ORPHANED_TUNNEL_GUARD:
            _ORPHANED_TRUST_LEASES.pop(id(trust_lease), None)


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
