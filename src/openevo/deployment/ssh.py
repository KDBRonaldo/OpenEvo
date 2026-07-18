from __future__ import annotations

import ipaddress
import locale
import logging
import os
import re
import select
import selectors
import secrets
import shlex
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, NoReturn, Protocol

from pydantic import SecretStr

from openevo.deployment.daemon_bundle_transport import (
    DOCKER_USER_CONTAINER_V1,
    DaemonBundleIdentity,
    DaemonBundleServiceStatus,
    DaemonBundleStopReceipt,
    DaemonBundleTransportContractError,
    OpenedDaemonBundle,
    StagedDaemonBundle,
    build_daemon_bundle_ensure_command,
    build_daemon_bundle_identity_command,
    build_daemon_bundle_inspect_command,
    build_daemon_bundle_stage_command,
    build_daemon_bundle_stop_command,
    parse_daemon_bundle_identity,
    parse_daemon_bundle_service_status,
    parse_daemon_bundle_stop_receipt,
    parse_staged_daemon_bundle,
)
from openevo.deployment.core_assets import (
    CORE_ASSET_TRANSFER_LEASE,
    CoreBootstrapAssetSnapshotError,
    StagedCoreBootstrapAssets,
    build_core_asset_consumer_command,
    build_core_asset_discard_command,
    build_core_asset_finalize_command,
    build_core_asset_prepare_command,
    build_core_asset_rsync_path,
    parse_core_asset_prepare,
    parse_staged_core_assets,
    snapshot_core_bootstrap_assets,
)
from openevo.deployment.core_runtime import (
    CorePythonRuntimeAuthority,
    build_core_supervisor_runtime_preflight_command,
    parse_core_supervisor_runtime_preflight,
)
from openevo.deployment.host_keys import TrustedKnownHostsBinding
from openevo.deployment.managed_runtime_assets import (
    MANAGED_RUNTIME_TRANSFER_LEASE,
    ManagedRuntimeArchiveSnapshotError,
    ManagedRuntimeLoadReceipt,
    ManagedRuntimeTransfer,
    OpenedManagedRuntimeArchive,
    build_daemon_managed_runtime_discard_command,
    build_daemon_managed_runtime_finalize_command,
    build_daemon_managed_runtime_prepare_command,
    build_daemon_managed_runtime_probe_command,
    build_daemon_managed_runtime_receive_command,
    build_managed_runtime_discard_command,
    build_managed_runtime_finalize_command,
    build_managed_runtime_prepare_command,
    build_managed_runtime_probe_command,
    build_managed_runtime_rsync_path,
    parse_managed_runtime_discard,
    parse_managed_runtime_prepare,
    parse_managed_runtime_probe,
    parse_managed_runtime_receive,
    parse_managed_runtime_receipt,
    snapshot_managed_runtime_archive,
)
from openevo.deployment.preflight import RemoteCommandResult
from openevo.deployment.profile import RemoteProfileConfig
from openevo.deployment.system_executables import (
    OWNED_SUBPROCESS_BIRTH_ARGUMENT,
    RSYNC_EXECUTABLE,
    SSH_EXECUTABLE,
    SshAgentProxy,
    SshAgentSocketSource,
    VerifiedSystemExecutable,
)

if TYPE_CHECKING:
    from openevo.deployment.core_control import RemoteCoreControlAttachment

CompletedRunner = Callable[[list[str], float], subprocess.CompletedProcess[str]]
StreamingRunner = Callable[
    [list[str], float, int, threading.Event | None],
    subprocess.CompletedProcess[str],
]
PortAllocator = Callable[[], int]
TunnelStarter = Callable[[list[str]], "TunnelProcess"]
CoreConnectionStarter = Callable[[list[str], int], "TunnelProcess"]

logger = logging.getLogger(__name__)

_TUNNEL_CLOSE_GRACE_SECONDS = 1.0
_TUNNEL_KILL_GRACE_SECONDS = 1.0
_TUNNEL_MONITOR_INTERVAL_SECONDS = 0.05
_MAX_SUBPROCESS_CAPTURE_BYTES = 4 * 1024 * 1024
_SUBPROCESS_CAPTURE_CHUNK_BYTES = 64 * 1024
_SUBPROCESS_BIRTH_RECOVERY_SECONDS = 1.0
_SUBPROCESS_TERMINATE_GRACE_SECONDS = 1.0
_SUBPROCESS_DESCENDANT_PIPE_GRACE_SECONDS = 0.1
_SUBPROCESS_STATUS_INTERVAL_SECONDS = 0.05
_PROCESS_GROUP_OBSERVER_TIMEOUT_SECONDS = 0.5
_MAX_PROCESS_GROUP_SCAN_ENTRIES = 131_072
_MAX_PROCESS_GROUP_STATUS_BYTES = 4 * 1024 * 1024
_MAX_OWNED_SUBPROCESSES = 32
_MAX_SUBPROCESS_ORPHAN_RETRIES = 4
_CORE_ASSET_CLEANUP_SECONDS = 10.0
_MAX_CORE_ASSET_CLEANUP_AUTHORITIES = 16
_ORPHANED_TUNNEL_GUARD = threading.Lock()
_ORPHANED_TUNNELS: dict[int, "SshTunnel"] = {}
_ORPHANED_CORE_TUNNELS: dict[int, "_CoreTunnelEndpoint"] = {}
_ORPHANED_TRUST_LEASES: dict[int, AbstractContextManager[Path]] = {}
_ORPHANED_SUBPROCESS_GUARD = threading.Lock()
_ORPHANED_SUBPROCESSES: dict[int, "_OwnedSubprocessAuthority"] = {}

_SUBPROCESS_BIRTH_LAUNCHER = """
import os
import sys

if sys.argv[1] != "--openevo-owned-subprocess-birth-v1":
    raise SystemExit(126)
birth_fd = int(sys.argv[2])
executable_fd = int(sys.argv[3])
argv = sys.argv[4:]
if not argv:
    raise SystemExit(126)
try:
    os.fchmod(birth_fd, 0o600)
    os.ftruncate(birth_fd, 0)
    os.lseek(birth_fd, 0, os.SEEK_SET)
    payload = f"{os.getpid()} {os.getpgrp()} {os.getsid(0)}\\n".encode("ascii")
    offset = 0
    while offset < len(payload):
        offset += os.write(birth_fd, payload[offset:])
    os.fsync(birth_fd)
finally:
    os.close(birth_fd)
identity_fields = (
    "st_dev",
    "st_ino",
    "st_mode",
    "st_uid",
    "st_nlink",
    "st_size",
    "st_mtime_ns",
    "st_ctime_ns",
)
opened_identity = os.fstat(executable_fd)
path_identity = os.stat(argv[0], follow_symlinks=True)
if tuple(getattr(opened_identity, field) for field in identity_fields) != tuple(
    getattr(path_identity, field) for field in identity_fields
):
    raise SystemExit(126)
os.set_inheritable(executable_fd, False)
environment = {}
agent_socket = os.environ.get("SSH_AUTH_SOCK")
if agent_socket is not None:
    environment["SSH_AUTH_SOCK"] = agent_socket
if sys.platform == "darwin":
    execution_path = argv[0]
elif sys.platform.startswith("linux"):
    execution_path = f"/dev/fd/{executable_fd}"
else:
    raise SystemExit(126)
os.execve(execution_path, argv, environment)
"""


@dataclass(frozen=True, slots=True)
class _CoreAssetTransferAuthority:
    runtime: CorePythonRuntimeAuthority
    service_root: str
    bundle_id: str
    transfer_id: str
    wheel_filename: str
    wheel_sha256: str
    wheel_size: int
    framework_lock_sha256: str
    framework_lock_size: int
    finalize_started: bool = False

    @property
    def key(self) -> tuple[str, str, str]:
        return self.service_root, self.bundle_id, self.transfer_id

    def with_finalize_started(self) -> _CoreAssetTransferAuthority:
        return _CoreAssetTransferAuthority(
            runtime=self.runtime,
            service_root=self.service_root,
            bundle_id=self.bundle_id,
            transfer_id=self.transfer_id,
            wheel_filename=self.wheel_filename,
            wheel_sha256=self.wheel_sha256,
            wheel_size=self.wheel_size,
            framework_lock_sha256=self.framework_lock_sha256,
            framework_lock_size=self.framework_lock_size,
            finalize_started=True,
        )


class _CoreAssetTransferAdmission:
    __slots__ = ("active", "authority")

    def __init__(self) -> None:
        self.active = False
        self.authority: _CoreAssetTransferAuthority | None = None


class SshTransportErrorCode(str, Enum):
    HOST_KEY_VERIFICATION_FAILED = "host_key_verification_failed"
    CONNECTION_FAILED = "connection_failed"
    START_FAILED = "start_failed"
    RSYNC_FAILED = "rsync_failed"
    CORE_ASSET_FAILED = "core_asset_failed"
    CORE_PYTHON_UNAVAILABLE = "core_python_unavailable"
    CORE_PYTHON_PROVISION_FAILED = "core_python_provision_failed"
    CORE_KERNEL_SYSCALL_UNSUPPORTED = "core_kernel_syscall_unsupported"
    CORE_RUNTIME_UNSUPPORTED = "core_runtime_unsupported"
    CORE_RUNTIME_PREFLIGHT_FAILED = "core_runtime_preflight_failed"
    MANAGED_RUNTIME_FAILED = "managed_runtime_failed"
    DAEMON_BUNDLE_FAILED = "daemon_bundle_failed"
    CANCELLED = "ssh_operation_cancelled"
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
            SshTransportErrorCode.CORE_ASSET_FAILED: ("Core bootstrap asset verification failed."),
            SshTransportErrorCode.CORE_PYTHON_UNAVAILABLE: (
                "No supported remote Python runtime is available."
            ),
            SshTransportErrorCode.CORE_PYTHON_PROVISION_FAILED: (
                "OpenEvo could not provision a supported remote Python runtime."
            ),
            SshTransportErrorCode.CORE_KERNEL_SYSCALL_UNSUPPORTED: (
                "The remote Linux kernel lacks required Core supervision syscalls."
            ),
            SshTransportErrorCode.CORE_RUNTIME_UNSUPPORTED: (
                "The remote host lacks required Core service supervision primitives."
            ),
            SshTransportErrorCode.CORE_RUNTIME_PREFLIGHT_FAILED: (
                "Remote Core service runtime preflight failed."
            ),
            SshTransportErrorCode.MANAGED_RUNTIME_FAILED: (
                "Managed Science runtime preparation failed."
            ),
            SshTransportErrorCode.DAEMON_BUNDLE_FAILED: (
                "OpenEvo Daemon bundle staging or control failed."
            ),
            SshTransportErrorCode.CANCELLED: "SSH operation was cancelled.",
            SshTransportErrorCode.INVALID_REQUEST: "SSH request is invalid.",
            SshTransportErrorCode.TIMEOUT: "SSH operation timed out.",
        }
        super().__init__(messages[code])


class TunnelProcess(Protocol):
    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


class _OwnedSubprocessProcess(Protocol):
    pid: int
    returncode: int | None
    stdout: BinaryIO | None
    stderr: BinaryIO | None

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...


@dataclass(slots=True)
class _TunnelChild:
    process: TunnelProcess | _OwnedSubprocessProcess | None
    authority: _OwnedSubprocessAuthority | None = None


class _CoreTunnelEndpoint:
    def __init__(
        self,
        *,
        connection_starter: CoreConnectionStarter | None,
        connection_argv: list[str],
        trust_lease: AbstractContextManager[Path],
        trusted_host: TrustedKnownHostsBinding,
        agent_socket_source: SshAgentSocketSource | None,
    ) -> None:
        self._connection_starter = connection_starter
        self._connection_argv = connection_argv
        self._agent_socket_source = agent_socket_source
        self._trust_lease: AbstractContextManager[Path] | None = trust_lease
        self._guard = threading.RLock()
        self._close_guard = threading.Lock()
        self._children: dict[int, _TunnelChild] = {}
        self._pending_child: _TunnelChild | None = None
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
        failure: BaseException | None = None
        with self._guard:
            if self._close_requested or self._finalized.is_set():
                raise SshTransportError(SshTransportErrorCode.CONNECTION_FAILED)
            try:
                self._verify_locked()
                return
            except BaseException as exc:
                failure = exc
                self._poison_locked()
        if failure is None:
            raise SshTransportError(SshTransportErrorCode.CONNECTION_FAILED)
        self._finish_failed_operation(failure)

    def open_verified_socket(self, *, timeout_seconds: float) -> socket.socket:
        if not 0 < timeout_seconds <= 60:
            raise SshTransportError(SshTransportErrorCode.INVALID_REQUEST)
        local_stream: socket.socket | None = None
        child_stream: socket.socket | None = None
        child: _TunnelChild | None = None
        failure: BaseException | None = None
        with self._guard:
            if self._close_requested or self._finalized.is_set():
                raise SshTransportError(SshTransportErrorCode.CONNECTION_FAILED)
            try:
                self._verify_locked()
                local_stream, child_stream = socket.socketpair(
                    socket.AF_UNIX,
                    socket.SOCK_STREAM,
                )
                local_identity = _require_parent_owned_stream(local_stream)
                child_identity = _require_parent_owned_stream(child_stream)
                local_stream.settimeout(timeout_seconds)
                if self._connection_starter is None:
                    authority = _OwnedSubprocessAuthority(trust_ownership=None)
                    child = _TunnelChild(process=None, authority=authority)
                    self._pending_child = child
                    authority.acquire()
                    authority.spawn_tunnel(
                        list(self._connection_argv),
                        stream_fd=child_stream.fileno(),
                        agent_socket_source=self._agent_socket_source,
                    )
                    child.process = authority.process
                else:
                    process = self._connection_starter(
                        list(self._connection_argv),
                        child_stream.fileno(),
                    )
                    child = _TunnelChild(process=process)
                    self._pending_child = child
                generation = self._next_generation
                self._next_generation += 1
                self._children[generation] = child
                _require_parent_owned_stream(
                    local_stream,
                    expected_identity=local_identity,
                )
                _require_parent_owned_stream(
                    child_stream,
                    expected_identity=child_identity,
                )
                child_stream.close()
                if child.process is None or _tunnel_child_has_exited(child):
                    raise SshTransportError(SshTransportErrorCode.CONNECTION_FAILED)
                _require_parent_owned_stream(
                    local_stream,
                    expected_identity=local_identity,
                )
                self._pending_child = None
                return local_stream
            except BaseException as exc:
                failure = exc
                self._poison_locked(pending_child=child)
        if failure is None:
            raise SshTransportError(SshTransportErrorCode.CONNECTION_FAILED)
        self._finish_failed_operation(
            failure,
            streams=(local_stream, child_stream),
        )

    def _poison_locked(self, *, pending_child: _TunnelChild | None = None) -> None:
        if pending_child is not None:
            self._pending_child = pending_child
        self._close_requested = True
        self._retain_orphan()

    def _finish_failed_operation(
        self,
        failure: BaseException,
        *,
        streams: tuple[socket.socket | None, ...] = (),
    ) -> NoReturn:
        for stream in streams:
            if stream is None:
                continue
            try:
                stream.close()
            except BaseException:
                _log_transport_failure(SshTransportErrorCode.CONNECTION_FAILED)
        try:
            self.close()
        except BaseException:
            _log_transport_failure(SshTransportErrorCode.CONNECTION_FAILED)
        if isinstance(failure, Exception):
            _log_transport_failure(SshTransportErrorCode.CONNECTION_FAILED)
            raise SshTransportError(SshTransportErrorCode.CONNECTION_FAILED) from None
        raise failure

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
                children = list(self._children.values())
                pending_child = self._pending_child
                if pending_child is not None and not any(
                    child is pending_child for child in children
                ):
                    children.append(pending_child)
            all_exited = True
            for child in children:
                exited, failure = _terminate_tunnel_child(child)
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
        for generation, child in self._children.items():
            process = child.process
            if process is None:
                raise SshTransportError(SshTransportErrorCode.CONNECTION_FAILED)
            try:
                exited = _tunnel_child_has_exited(child)
            except BaseException:
                raise SshTransportError(SshTransportErrorCode.CONNECTION_FAILED) from None
            if not exited:
                continue
            if child.authority is not None:
                try:
                    child.authority.cleanup()
                except BaseException:
                    raise SshTransportError(SshTransportErrorCode.CONNECTION_FAILED) from None
                if not child.authority.released:
                    raise SshTransportError(SshTransportErrorCode.CONNECTION_FAILED)
                return_code = process.returncode
            else:
                return_code = process.poll()
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
                self._pending_child = None
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
        _retry_orphaned_subprocess_cleanup()
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
        process: TunnelProcess | _OwnedSubprocessProcess,
        trust_lease: AbstractContextManager[Path] | None,
        trusted_host: TrustedKnownHostsBinding,
        process_authority: _OwnedSubprocessAuthority | None = None,
        on_finalize: Callable[[], None] | None = None,
    ) -> None:
        self.local_port = local_port
        self.remote_port = remote_port
        self.local_host = local_host
        self.remote_host = remote_host
        self.process = process
        self._process_authority = process_authority
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
        if self._process_authority is not None:
            try:
                self._process_authority.request_group_termination()
            except BaseException as exc:
                self._retain_orphan()
                _log_transport_failure(SshTransportErrorCode.CONNECTION_FAILED)
                if not isinstance(exc, Exception):
                    raise
        else:
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
        _retry_orphaned_subprocess_cleanup()
        with self._close_guard:
            self._close_once()

    def _close_once(self) -> None:
        if self._closed.is_set():
            return
        with self._state_guard:
            self._close_requested = True
        exited, failure = _terminate_tunnel_child(
            _TunnelChild(self.process, self._process_authority)
        )
        if exited:
            self._finalize()
        else:
            self._retain_orphan()
        if failure is not None:
            raise failure

    def _monitor_exit(self) -> None:
        while not self._closed.wait(_TUNNEL_MONITOR_INTERVAL_SECONDS):
            try:
                if self._leader_exited():
                    self.close()
                    return
            except Exception:
                _log_transport_failure(SshTransportErrorCode.CONNECTION_FAILED)
                continue

    def _leader_exited(self) -> bool:
        authority = self._process_authority
        if authority is not None:
            return authority.leader_exited()
        return self.process.poll() is not None

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
                self._process_authority = None
                self._closed.set()
        if finalized:
            _forget_orphaned_tunnel(self)
        else:
            self._retain_orphan()
        if failure is not None:
            raise failure


def _close_transport_tunnel(tunnel: SshTunnel | SshCoreTunnel) -> None:
    try:
        tunnel.close()
    except Exception:
        _log_transport_failure(SshTransportErrorCode.CONNECTION_FAILED)


class SshRemoteExecutorTransport:
    """Execute SSH operations against one explicitly trusted host-key binding."""

    def __init__(
        self,
        profile: RemoteProfileConfig,
        *,
        trusted_host: TrustedKnownHostsBinding | None = None,
        runner: CompletedRunner | None = None,
        streaming_runner: StreamingRunner | None = None,
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
        self._uses_default_runner = runner is None
        self._streaming_runner = streaming_runner or _run_streaming_subprocess
        self._uses_default_streaming_runner = streaming_runner is None
        self._tunnel_starter = tunnel_starter
        self._port_allocator = port_allocator or _allocate_local_port
        self._core_connection_starter = core_connection_starter
        self._subprocess_environment: dict[str, str] = {}
        self._agent_socket_source = SshAgentSocketSource.from_environment(profile.auth.method)
        self._closed = False
        self._core_asset_authority_lock = threading.Lock()
        self._core_asset_authorities: dict[
            str, tuple[StagedCoreBootstrapAssets, CorePythonRuntimeAuthority]
        ] = {}
        self._core_asset_transfer_ownerships: set[_CoreAssetTransferAdmission] = set()
        self._managed_runtime_lock = threading.Lock()
        self._operation_guard = threading.Lock()
        self._active_subprocesses: set[_OwnedSubprocessAuthority] = set()
        self._active_tunnels: set[SshTunnel | SshCoreTunnel] = set()

    def close(self) -> None:
        """Cancel transport-owned subprocesses and tunnels without waiting on their timeout."""

        with self._operation_guard:
            if self._closed:
                return
            self._closed = True
            subprocesses = tuple(self._active_subprocesses)
            tunnels = tuple(self._active_tunnels)
        for authority in subprocesses:
            try:
                authority.request_group_termination()
            except Exception:
                _log_transport_failure(SshTransportErrorCode.START_FAILED)
        for tunnel in tunnels:
            request_close = getattr(tunnel, "request_close", None)
            if callable(request_close):
                try:
                    request_close()
                except Exception:
                    _log_transport_failure(SshTransportErrorCode.CONNECTION_FAILED)
                continue
            threading.Thread(
                target=_close_transport_tunnel,
                args=(tunnel,),
                name="openevo-ssh-transport-close",
                daemon=True,
            ).start()

    def _require_open(self) -> None:
        with self._operation_guard:
            if self._closed:
                raise SshTransportError(SshTransportErrorCode.CONNECTION_FAILED)

    def _register_subprocess(self, authority: _OwnedSubprocessAuthority) -> None:
        with self._operation_guard:
            if not self._closed:
                self._active_subprocesses.add(authority)
                return
        authority.request_group_termination()
        raise SshTransportError(SshTransportErrorCode.CONNECTION_FAILED)

    def _unregister_subprocess(self, authority: _OwnedSubprocessAuthority) -> None:
        with self._operation_guard:
            self._active_subprocesses.discard(authority)

    def _register_tunnel(self, tunnel: SshTunnel | SshCoreTunnel) -> None:
        with self._operation_guard:
            self._active_tunnels = {active for active in self._active_tunnels if not active.closed}
            if not self._closed:
                self._active_tunnels.add(tunnel)
                return
        _close_transport_tunnel(tunnel)
        raise SshTransportError(SshTransportErrorCode.CONNECTION_FAILED)

    def _run_trusted_subprocess(
        self,
        argv_factory: Callable[[Path], list[str]],
        timeout_seconds: float,
        *,
        cancel_event: threading.Event | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        _raise_if_cancelled(cancel_event)
        self._require_open()
        trust_lease = self._trusted_host.open_for_spawn(self._profile)
        try:
            known_hosts_file = trust_lease.__enter__()
        except Exception:
            _release_trust_lease(trust_lease)
            raise _KnownHostsSpawnFailure from None
        except BaseException:
            _release_trust_lease(trust_lease)
            raise
        try:
            trust_ownership = _KnownHostsLeaseOwnership(trust_lease)
        except BaseException:
            _release_trust_lease(trust_lease)
            raise
        try:
            argv = argv_factory(known_hosts_file)
            if self._uses_default_runner:
                common = {
                    "trust_ownership": trust_ownership,
                    "env": self._process_environment(),
                    "agent_socket_source": self._agent_socket_source,
                    "on_start": self._register_subprocess,
                    "on_finish": self._unregister_subprocess,
                }
                completed = (
                    _run_subprocess(argv, timeout_seconds, **common)
                    if cancel_event is None
                    else _run_subprocess(
                        argv,
                        timeout_seconds,
                        cancel_event=cancel_event,
                        **common,
                    )
                )
            else:
                _raise_if_cancelled(cancel_event)
                self._require_open()
                completed = self._runner(argv, timeout_seconds)
            _raise_if_cancelled(cancel_event)
            self._require_open()
            return completed, known_hosts_file
        finally:
            trust_ownership.release_if_caller_owned()

    def _run_trusted_streaming_subprocess(
        self,
        argv_factory: Callable[[Path], list[str]],
        timeout_seconds: float,
        *,
        stdin_fd: int,
        cancel_event: threading.Event | None,
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        _raise_if_cancelled(cancel_event)
        self._require_open()
        trust_lease = self._trusted_host.open_for_spawn(self._profile)
        try:
            known_hosts_file = trust_lease.__enter__()
        except Exception:
            _release_trust_lease(trust_lease)
            raise _KnownHostsSpawnFailure from None
        except BaseException:
            _release_trust_lease(trust_lease)
            raise
        try:
            trust_ownership = _KnownHostsLeaseOwnership(trust_lease)
        except BaseException:
            _release_trust_lease(trust_lease)
            raise
        try:
            argv = argv_factory(known_hosts_file)
            if self._uses_default_streaming_runner:
                completed = _run_streaming_subprocess(
                    argv,
                    timeout_seconds,
                    stdin_fd,
                    cancel_event,
                    trust_ownership=trust_ownership,
                    env=self._process_environment(),
                    agent_socket_source=self._agent_socket_source,
                    on_start=self._register_subprocess,
                    on_finish=self._unregister_subprocess,
                )
            else:
                _raise_if_cancelled(cancel_event)
                self._require_open()
                completed = self._streaming_runner(
                    argv,
                    timeout_seconds,
                    stdin_fd,
                    cancel_event,
                )
            _raise_if_cancelled(cancel_event)
            self._require_open()
            return completed, known_hosts_file
        finally:
            trust_ownership.release_if_caller_owned()

    def _process_environment(self) -> dict[str, str]:
        return dict(self._subprocess_environment)

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
            bound_command = self._bind_core_asset_consumption(command)
            effective_env = dict(env or {})
            if bound_command != command:
                effective_env = _core_runtime_proxy_env(self._profile) | effective_env
            remote_command = _remote_command(bound_command, cwd=cwd, env=effective_env)
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
            phase = "process"
            completed, known_hosts_file = self._run_trusted_subprocess(
                lambda known_hosts_file: self._ssh_argv(marked_command, known_hosts_file),
                timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            failure_code = SshTransportErrorCode.TIMEOUT
        except _KnownHostsSpawnFailure:
            failure_code = SshTransportErrorCode.HOST_KEY_VERIFICATION_FAILED
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
            phase = "process"
            completed, _known_hosts_file = self._run_trusted_subprocess(
                lambda known_hosts_file: self._rsync_argv(local, remote_path, known_hosts_file),
                300.0,
            )
        except subprocess.TimeoutExpired:
            failure_code = SshTransportErrorCode.TIMEOUT
        except _KnownHostsSpawnFailure:
            failure_code = SshTransportErrorCode.HOST_KEY_VERIFICATION_FAILED
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

    def stage_daemon_bundle(
        self,
        *,
        bundle_path: str,
        bundle_sha256: str,
        bundle_size: int,
        timeout_seconds: float = 300.0,
        cancel_event: threading.Event | None = None,
    ) -> StagedDaemonBundle:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 < timeout_seconds <= 300
            or (cancel_event is not None and not isinstance(cancel_event, threading.Event))
        ):
            raise SshTransportError(SshTransportErrorCode.INVALID_REQUEST)
        service_root = f"/home/{self._profile.user}/.openevo/daemon-bundles"
        try:
            snapshot = OpenedDaemonBundle.open(
                bundle_path,
                expected_sha256=bundle_sha256,
                expected_size=bundle_size,
            )
        except (OSError, DaemonBundleTransportContractError):
            _log_transport_failure(SshTransportErrorCode.INVALID_REQUEST)
            raise SshTransportError(SshTransportErrorCode.INVALID_REQUEST) from None

        transfer_id = secrets.token_hex(16)
        try:
            command = build_daemon_bundle_stage_command(
                service_root=service_root,
                sha256=snapshot.sha256,
                size=snapshot.size,
                transfer_id=transfer_id,
                host_profile=DOCKER_USER_CONTAINER_V1,
            )
            completion_marker = f"__OPENEVO_DAEMON_BUNDLE_COMPLETION_{secrets.token_hex(16)}__="
            marked_command = _with_completion_marker(command, completion_marker)
            completed: subprocess.CompletedProcess[str] | None = None
            known_hosts_file: Path | None = None
            failure_code: SshTransportErrorCode | None = None
            try:
                snapshot.rewind()
                completed, known_hosts_file = self._run_trusted_streaming_subprocess(
                    lambda known_hosts_file: self._ssh_argv(
                        marked_command,
                        known_hosts_file,
                    ),
                    float(timeout_seconds),
                    stdin_fd=snapshot.descriptor,
                    cancel_event=cancel_event,
                )
            except _SubprocessCancelled:
                failure_code = SshTransportErrorCode.CANCELLED
            except subprocess.TimeoutExpired:
                failure_code = SshTransportErrorCode.TIMEOUT
            except _KnownHostsSpawnFailure:
                failure_code = SshTransportErrorCode.HOST_KEY_VERIFICATION_FAILED
            except Exception:
                failure_code = SshTransportErrorCode.START_FAILED
            try:
                snapshot.verify_unchanged()
            except (OSError, DaemonBundleTransportContractError):
                failure_code = SshTransportErrorCode.DAEMON_BUNDLE_FAILED
            if failure_code is not None:
                _log_transport_failure(failure_code)
                raise SshTransportError(failure_code)
            assert completed is not None
            assert known_hosts_file is not None
            stderr, remote_return_code = _extract_remote_completion(
                completed.stderr or "",
                completion_marker,
            )
            if remote_return_code is None or completed.returncode == 255:
                _log_transport_failure(SshTransportErrorCode.CONNECTION_FAILED)
                raise SshTransportError(SshTransportErrorCode.CONNECTION_FAILED)
            if int(completed.returncode) != remote_return_code:
                _log_transport_failure(SshTransportErrorCode.CONNECTION_FAILED)
                raise SshTransportError(SshTransportErrorCode.CONNECTION_FAILED)
            if remote_return_code != 0:
                _redact_trust_paths(
                    stderr,
                    known_hosts_file,
                    self._trusted_host.known_hosts_file,
                )
                _log_transport_failure(SshTransportErrorCode.DAEMON_BUNDLE_FAILED)
                raise SshTransportError(SshTransportErrorCode.DAEMON_BUNDLE_FAILED)
            try:
                staged = parse_staged_daemon_bundle(completed.stdout or "")
                if (
                    staged.host_profile != DOCKER_USER_CONTAINER_V1.profile_id
                    or staged._service_root != service_root
                    or staged.sha256 != snapshot.sha256
                    or staged.size != snapshot.size
                ):
                    raise DaemonBundleTransportContractError(
                        "Daemon bundle staging receipt does not match the request."
                    )
                return staged
            except DaemonBundleTransportContractError:
                _log_transport_failure(SshTransportErrorCode.DAEMON_BUNDLE_FAILED)
                raise SshTransportError(SshTransportErrorCode.DAEMON_BUNDLE_FAILED) from None
        finally:
            snapshot.close()

    def daemon_bundle_identity(
        self,
        bundle: StagedDaemonBundle,
        *,
        timeout_seconds: float = 30.0,
        cancel_event: threading.Event | None = None,
    ) -> DaemonBundleIdentity:
        self._validate_daemon_bundle_control_request(
            bundle,
            timeout_seconds=timeout_seconds,
            cancel_event=cancel_event,
        )
        try:
            payload = self._run_secret_with_remote_failure(
                build_daemon_bundle_identity_command(bundle),
                timeout_seconds=float(timeout_seconds),
                remote_failure_code=SshTransportErrorCode.DAEMON_BUNDLE_FAILED,
                cancel_event=cancel_event,
            )
            identity = parse_daemon_bundle_identity(payload)
            if identity.bundle_sha256 != bundle.sha256 or identity.bundle_size != bundle.size:
                raise DaemonBundleTransportContractError(
                    "Daemon bundle identity does not match its staged receipt."
                )
            return identity
        except DaemonBundleTransportContractError:
            _log_transport_failure(SshTransportErrorCode.DAEMON_BUNDLE_FAILED)
            raise SshTransportError(SshTransportErrorCode.DAEMON_BUNDLE_FAILED) from None

    def ensure_daemon_bundle(
        self,
        bundle: StagedDaemonBundle,
        *,
        port: int = 0,
        timeout_seconds: float = 90.0,
        cancel_event: threading.Event | None = None,
    ) -> RemoteCoreControlAttachment:
        self._validate_daemon_bundle_control_request(
            bundle,
            timeout_seconds=timeout_seconds,
            cancel_event=cancel_event,
        )
        if type(port) is not int or not 0 <= port <= 65535:
            raise SshTransportError(SshTransportErrorCode.INVALID_REQUEST)
        deadline = time.monotonic() + float(timeout_seconds)
        identity = self.daemon_bundle_identity(
            bundle,
            timeout_seconds=_stage_remaining(deadline),
            cancel_event=cancel_event,
        )
        try:
            command = build_daemon_bundle_ensure_command(
                bundle,
                port=port,
                deadline_seconds=_stage_remaining(deadline),
            )
            payload = self._run_secret_with_remote_failure(
                command,
                timeout_seconds=_stage_remaining(deadline),
                remote_failure_code=SshTransportErrorCode.DAEMON_BUNDLE_FAILED,
                cancel_event=cancel_event,
            )
            from openevo.deployment.core_control import (
                CoreControlBootstrapError,
                parse_core_control_attachment,
            )

            try:
                attachment = parse_core_control_attachment(payload)
            except CoreControlBootstrapError:
                raise DaemonBundleTransportContractError(
                    "Daemon ensure attachment is invalid."
                ) from None
            if (
                attachment.release_identity != identity.release_identity
                or attachment.registry_digest != identity.registry_digest
                or attachment.source_commit != identity.source_commit
            ):
                raise DaemonBundleTransportContractError(
                    "Daemon attachment does not match the bundle identity."
                )
            return attachment
        except DaemonBundleTransportContractError:
            _log_transport_failure(SshTransportErrorCode.DAEMON_BUNDLE_FAILED)
            raise SshTransportError(SshTransportErrorCode.DAEMON_BUNDLE_FAILED) from None

    def ensure_managed_runtime_from_daemon(
        self,
        bundle: StagedDaemonBundle,
        *,
        archive_path: str,
        archive_sha256: str,
        archive_size: int,
        platform: str,
        config_id: str,
        oci_index_id: str,
        aliases: tuple[str, ...],
        timeout_seconds: float,
        cancel_event: threading.Event | None = None,
    ) -> ManagedRuntimeLoadReceipt:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 < timeout_seconds <= 900
            or (cancel_event is not None and not isinstance(cancel_event, threading.Event))
        ):
            raise SshTransportError(SshTransportErrorCode.INVALID_REQUEST)
        try:
            if not isinstance(bundle, StagedDaemonBundle):
                raise DaemonBundleTransportContractError("Daemon bundle receipt is invalid.")
            bundle.__post_init__()
            if (
                bundle.host_profile != DOCKER_USER_CONTAINER_V1.profile_id
                or bundle._service_root != f"/home/{self._profile.user}/.openevo/daemon-bundles"
            ):
                raise DaemonBundleTransportContractError(
                    "Daemon bundle receipt is not authoritative for this transport."
                )
            probe_command = build_daemon_managed_runtime_probe_command(
                bundle._executable_path,
                archive_sha256=archive_sha256,
                archive_size=archive_size,
                platform=platform,
                config_id=config_id,
                oci_index_id=oci_index_id,
                aliases=aliases,
            )
        except (DaemonBundleTransportContractError, TypeError, ValueError):
            raise SshTransportError(SshTransportErrorCode.INVALID_REQUEST) from None

        deadline = time.monotonic() + float(timeout_seconds)
        acquired = False
        while not acquired:
            try:
                _raise_if_cancelled(cancel_event)
            except _SubprocessCancelled:
                raise SshTransportError(SshTransportErrorCode.CANCELLED) from None
            acquired = self._managed_runtime_lock.acquire(
                timeout=min(0.05, _stage_remaining(deadline))
            )
        try:
            try:
                probe = self._run_secret_with_remote_failure(
                    probe_command,
                    timeout_seconds=_stage_remaining(deadline),
                    remote_failure_code=SshTransportErrorCode.MANAGED_RUNTIME_FAILED,
                    cancel_event=cancel_event,
                )
                ready = parse_managed_runtime_probe(probe)
                if ready is not None:
                    return ready
                with snapshot_managed_runtime_archive(
                    archive_path=archive_path,
                    archive_sha256=archive_sha256,
                    archive_size=archive_size,
                ) as snapshot:
                    with OpenedManagedRuntimeArchive.open(snapshot) as opened:
                        prepared = self._run_secret_with_remote_failure(
                            build_daemon_managed_runtime_prepare_command(
                                bundle._executable_path,
                                archive_sha256=archive_sha256,
                                archive_size=archive_size,
                            ),
                            timeout_seconds=_stage_remaining(deadline),
                            remote_failure_code=SshTransportErrorCode.MANAGED_RUNTIME_FAILED,
                            cancel_event=cancel_event,
                        )
                        transfer = parse_managed_runtime_prepare(prepared)
                        try:
                            self._stream_managed_runtime_to_daemon(
                                bundle,
                                opened,
                                transfer,
                                archive_sha256=archive_sha256,
                                archive_size=archive_size,
                                deadline=deadline,
                                cancel_event=cancel_event,
                            )
                            remaining = _stage_remaining(deadline)
                            finalized = self._run_secret_with_remote_failure(
                                build_daemon_managed_runtime_finalize_command(
                                    bundle._executable_path,
                                    transfer,
                                    archive_sha256=archive_sha256,
                                    archive_size=archive_size,
                                    platform=platform,
                                    config_id=config_id,
                                    oci_index_id=oci_index_id,
                                    aliases=aliases,
                                    load_timeout_seconds=max(
                                        1,
                                        min(900, int(remaining)),
                                    ),
                                ),
                                timeout_seconds=remaining,
                                remote_failure_code=SshTransportErrorCode.MANAGED_RUNTIME_FAILED,
                                cancel_event=cancel_event,
                            )
                            receipt = parse_managed_runtime_receipt(finalized)
                            if receipt.reused:
                                raise ValueError("managed runtime load receipt is inconsistent")
                            return receipt
                        except BaseException:
                            self._discard_daemon_managed_runtime_transfer(
                                bundle,
                                transfer,
                                archive_sha256=archive_sha256,
                                archive_size=archive_size,
                            )
                            raise
            except ManagedRuntimeArchiveSnapshotError:
                _log_transport_failure(SshTransportErrorCode.INVALID_REQUEST)
                raise SshTransportError(SshTransportErrorCode.INVALID_REQUEST) from None
            except SshTransportError:
                raise
            except (OSError, TypeError, ValueError):
                _log_transport_failure(SshTransportErrorCode.MANAGED_RUNTIME_FAILED)
                raise SshTransportError(SshTransportErrorCode.MANAGED_RUNTIME_FAILED) from None
        finally:
            self._managed_runtime_lock.release()

    def _stream_managed_runtime_to_daemon(
        self,
        bundle: StagedDaemonBundle,
        opened: OpenedManagedRuntimeArchive,
        transfer: ManagedRuntimeTransfer,
        *,
        archive_sha256: str,
        archive_size: int,
        deadline: float,
        cancel_event: threading.Event | None,
    ) -> None:
        command = build_daemon_managed_runtime_receive_command(
            bundle._executable_path,
            transfer,
            archive_sha256=archive_sha256,
            archive_size=archive_size,
        )
        completion_marker = f"__OPENEVO_MANAGED_RUNTIME_COMPLETION_{secrets.token_hex(16)}__="
        marked_command = _with_completion_marker(command, completion_marker)
        completed: subprocess.CompletedProcess[str] | None = None
        failure_code: SshTransportErrorCode | None = None
        try:
            opened.rewind()
            completed, _known_hosts_file = self._run_trusted_streaming_subprocess(
                lambda known_hosts_file: self._ssh_argv(marked_command, known_hosts_file),
                _stage_remaining(deadline),
                stdin_fd=opened.descriptor,
                cancel_event=cancel_event,
            )
        except _SubprocessCancelled:
            failure_code = SshTransportErrorCode.CANCELLED
        except subprocess.TimeoutExpired:
            failure_code = SshTransportErrorCode.TIMEOUT
        except _KnownHostsSpawnFailure:
            failure_code = SshTransportErrorCode.HOST_KEY_VERIFICATION_FAILED
        except Exception:
            failure_code = SshTransportErrorCode.START_FAILED
        try:
            opened.verify_unchanged()
        except ManagedRuntimeArchiveSnapshotError:
            failure_code = SshTransportErrorCode.MANAGED_RUNTIME_FAILED
        if failure_code is not None:
            _log_transport_failure(failure_code)
            raise SshTransportError(failure_code)
        assert completed is not None
        _stderr, remote_return_code = _extract_remote_completion(
            completed.stderr or "",
            completion_marker,
        )
        if remote_return_code is None or completed.returncode == 255:
            raise SshTransportError(SshTransportErrorCode.CONNECTION_FAILED)
        if int(completed.returncode) != remote_return_code:
            raise SshTransportError(SshTransportErrorCode.CONNECTION_FAILED)
        if remote_return_code != 0:
            raise SshTransportError(SshTransportErrorCode.MANAGED_RUNTIME_FAILED)
        try:
            parse_managed_runtime_receive(SecretStr(completed.stdout or ""))
        except (TypeError, ValueError):
            raise SshTransportError(SshTransportErrorCode.MANAGED_RUNTIME_FAILED) from None

    def _discard_daemon_managed_runtime_transfer(
        self,
        bundle: StagedDaemonBundle,
        transfer: ManagedRuntimeTransfer,
        *,
        archive_sha256: str,
        archive_size: int,
    ) -> None:
        try:
            response = self._run_secret_with_remote_failure(
                build_daemon_managed_runtime_discard_command(
                    bundle._executable_path,
                    transfer,
                    archive_sha256=archive_sha256,
                    archive_size=archive_size,
                ),
                timeout_seconds=_CORE_ASSET_CLEANUP_SECONDS,
                remote_failure_code=SshTransportErrorCode.MANAGED_RUNTIME_FAILED,
            )
            parse_managed_runtime_discard(response)
        except Exception:
            _log_transport_failure(SshTransportErrorCode.MANAGED_RUNTIME_FAILED)

    def inspect_daemon_bundle(
        self,
        bundle: StagedDaemonBundle,
        *,
        timeout_seconds: float = 30.0,
        cancel_event: threading.Event | None = None,
    ) -> DaemonBundleServiceStatus:
        self._validate_daemon_bundle_control_request(
            bundle,
            timeout_seconds=timeout_seconds,
            cancel_event=cancel_event,
        )
        try:
            payload = self._run_secret_with_remote_failure(
                build_daemon_bundle_inspect_command(bundle),
                timeout_seconds=float(timeout_seconds),
                remote_failure_code=SshTransportErrorCode.DAEMON_BUNDLE_FAILED,
                cancel_event=cancel_event,
            )
            return parse_daemon_bundle_service_status(payload)
        except DaemonBundleTransportContractError:
            _log_transport_failure(SshTransportErrorCode.DAEMON_BUNDLE_FAILED)
            raise SshTransportError(SshTransportErrorCode.DAEMON_BUNDLE_FAILED) from None

    def stop_daemon_bundle(
        self,
        bundle: StagedDaemonBundle,
        *,
        timeout_seconds: float = 30.0,
        cancel_event: threading.Event | None = None,
    ) -> DaemonBundleStopReceipt:
        self._validate_daemon_bundle_control_request(
            bundle,
            timeout_seconds=timeout_seconds,
            cancel_event=cancel_event,
        )
        try:
            command = build_daemon_bundle_stop_command(
                bundle,
                deadline_seconds=float(timeout_seconds),
            )
            payload = self._run_secret_with_remote_failure(
                command,
                timeout_seconds=float(timeout_seconds),
                remote_failure_code=SshTransportErrorCode.DAEMON_BUNDLE_FAILED,
                cancel_event=cancel_event,
            )
            return parse_daemon_bundle_stop_receipt(payload)
        except DaemonBundleTransportContractError:
            _log_transport_failure(SshTransportErrorCode.DAEMON_BUNDLE_FAILED)
            raise SshTransportError(SshTransportErrorCode.DAEMON_BUNDLE_FAILED) from None

    def _validate_daemon_bundle_control_request(
        self,
        bundle: StagedDaemonBundle,
        *,
        timeout_seconds: float,
        cancel_event: threading.Event | None,
    ) -> None:
        try:
            if not isinstance(bundle, StagedDaemonBundle):
                raise DaemonBundleTransportContractError("Daemon bundle receipt is invalid.")
            bundle.__post_init__()
            if (
                bundle.host_profile != DOCKER_USER_CONTAINER_V1.profile_id
                or bundle._service_root != f"/home/{self._profile.user}/.openevo/daemon-bundles"
                or isinstance(timeout_seconds, bool)
                or not isinstance(timeout_seconds, (int, float))
                or not 0 < timeout_seconds <= 300
                or (cancel_event is not None and not isinstance(cancel_event, threading.Event))
            ):
                raise DaemonBundleTransportContractError(
                    "Daemon bundle control request is invalid."
                )
        except DaemonBundleTransportContractError:
            raise SshTransportError(SshTransportErrorCode.INVALID_REQUEST) from None

    def stage_core_bootstrap_assets(
        self,
        *,
        runtime: CorePythonRuntimeAuthority,
        wheel_path: str,
        wheel_sha256: str,
        wheel_size: int,
        framework_lock_path: str,
        framework_lock_sha256: str,
        framework_lock_size: int,
        bundle_id: str,
        timeout_seconds: float,
    ) -> StagedCoreBootstrapAssets:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 < timeout_seconds <= 300
        ):
            raise SshTransportError(SshTransportErrorCode.INVALID_REQUEST)
        try:
            if not isinstance(runtime, CorePythonRuntimeAuthority):
                raise ValueError("Core Python runtime authority is invalid")
            runtime.__post_init__()
            prepare_command = build_core_asset_prepare_command(bundle_id, runtime)
        except (TypeError, ValueError):
            _log_transport_failure(SshTransportErrorCode.INVALID_REQUEST)
            raise SshTransportError(SshTransportErrorCode.INVALID_REQUEST) from None
        deadline = time.monotonic() + float(timeout_seconds)
        try:
            with snapshot_core_bootstrap_assets(
                wheel_path=wheel_path,
                wheel_sha256=wheel_sha256,
                wheel_size=wheel_size,
                framework_lock_path=framework_lock_path,
                framework_lock_sha256=framework_lock_sha256,
                framework_lock_size=framework_lock_size,
            ) as snapshot:
                self._retry_core_asset_transfer_cleanup(
                    deadline=time.monotonic() + _CORE_ASSET_CLEANUP_SECONDS
                )
                admission = _CoreAssetTransferAdmission()
                try:
                    self._admit_core_asset_transfer(admission)
                    prepare_payload = self._run_secret_with_remote_failure(
                        prepare_command,
                        timeout_seconds=_stage_remaining(deadline),
                        remote_failure_code=SshTransportErrorCode.CORE_ASSET_FAILED,
                    )
                    service_root, incoming_root, transfer_id = parse_core_asset_prepare(
                        prepare_payload,
                        bundle_id=bundle_id,
                    )
                except BaseException:
                    self._release_core_asset_admission(admission)
                    raise
                authority: _CoreAssetTransferAuthority | None = None
                try:
                    authority = _CoreAssetTransferAuthority(
                        runtime=runtime,
                        service_root=service_root,
                        bundle_id=bundle_id,
                        transfer_id=transfer_id,
                        wheel_filename=snapshot.wheel_filename,
                        wheel_sha256=wheel_sha256,
                        wheel_size=wheel_size,
                        framework_lock_sha256=framework_lock_sha256,
                        framework_lock_size=framework_lock_size,
                    )
                    self._bind_core_asset_admission(admission, authority)
                    self._remember_core_asset_transfer(
                        authority,
                        active=True,
                        consume_admission=True,
                    )
                except BaseException:
                    if authority is None:
                        authority = _CoreAssetTransferAuthority(
                            runtime=runtime,
                            service_root=service_root,
                            bundle_id=bundle_id,
                            transfer_id=transfer_id,
                            wheel_filename=snapshot.wheel_filename,
                            wheel_sha256=wheel_sha256,
                            wheel_size=wheel_size,
                            framework_lock_sha256=framework_lock_sha256,
                            framework_lock_size=framework_lock_size,
                        )
                    self._recover_core_asset_prepare_handoff(admission, authority)
                    raise
                try:
                    self._upload_core_asset_snapshot(
                        snapshot.root,
                        incoming_root,
                        authority=authority,
                        deadline=deadline,
                    )
                except BaseException:
                    self._mark_core_asset_transfer_inactive(authority)
                    self._discard_core_asset_transfer(
                        authority,
                        deadline=time.monotonic() + _CORE_ASSET_CLEANUP_SECONDS,
                    )
                    raise
                try:
                    authority = authority.with_finalize_started()
                    self._remember_core_asset_transfer(authority)
                    try:
                        staged = self._finalize_core_asset_transfer(
                            authority,
                            deadline=deadline,
                        )
                    except BaseException as failure:
                        reconciled = self._reconcile_core_asset_transfer(
                            authority,
                            deadline=time.monotonic() + _CORE_ASSET_CLEANUP_SECONDS,
                        )
                        if reconciled is not None and isinstance(failure, Exception):
                            return reconciled
                        raise
                    self._publish_core_asset_authority(authority, staged)
                    return staged
                finally:
                    self._mark_core_asset_transfer_inactive(authority)
        except CoreBootstrapAssetSnapshotError:
            _log_transport_failure(SshTransportErrorCode.INVALID_REQUEST)
            raise SshTransportError(SshTransportErrorCode.INVALID_REQUEST) from None
        except SshTransportError:
            raise
        except (OSError, TypeError, ValueError):
            _log_transport_failure(SshTransportErrorCode.CORE_ASSET_FAILED)
            raise SshTransportError(SshTransportErrorCode.CORE_ASSET_FAILED) from None

    def ensure_managed_runtime(
        self,
        *,
        runtime: CorePythonRuntimeAuthority,
        archive_path: str,
        archive_sha256: str,
        archive_size: int,
        platform: str,
        config_id: str,
        oci_index_id: str,
        aliases: tuple[str, ...],
        timeout_seconds: float,
        cancel_event: threading.Event | None = None,
    ) -> ManagedRuntimeLoadReceipt:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 < timeout_seconds <= 900
            or (cancel_event is not None and not isinstance(cancel_event, threading.Event))
        ):
            raise SshTransportError(SshTransportErrorCode.INVALID_REQUEST)
        try:
            if not isinstance(runtime, CorePythonRuntimeAuthority):
                raise ValueError("Core Python runtime authority is invalid")
            runtime.__post_init__()
            probe_command = build_managed_runtime_probe_command(
                runtime,
                archive_sha256=archive_sha256,
                archive_size=archive_size,
                platform=platform,
                config_id=config_id,
                oci_index_id=oci_index_id,
                aliases=aliases,
            )
        except SshTransportError:
            raise
        except (TypeError, ValueError):
            raise SshTransportError(SshTransportErrorCode.INVALID_REQUEST) from None
        deadline = time.monotonic() + float(timeout_seconds)
        acquired = False
        while not acquired:
            try:
                _raise_if_cancelled(cancel_event)
            except _SubprocessCancelled:
                raise SshTransportError(SshTransportErrorCode.CANCELLED) from None
            acquired = self._managed_runtime_lock.acquire(
                timeout=min(0.05, _stage_remaining(deadline))
            )
        try:
            try:
                probe = self._run_secret_with_remote_failure(
                    probe_command,
                    timeout_seconds=_stage_remaining(deadline),
                    remote_failure_code=SshTransportErrorCode.MANAGED_RUNTIME_FAILED,
                    cancel_event=cancel_event,
                )
                ready = parse_managed_runtime_probe(probe)
                if ready is not None:
                    return ready
                with snapshot_managed_runtime_archive(
                    archive_path=archive_path,
                    archive_sha256=archive_sha256,
                    archive_size=archive_size,
                ) as snapshot:
                    prepared = self._run_secret_with_remote_failure(
                        build_managed_runtime_prepare_command(
                            runtime,
                            archive_sha256=archive_sha256,
                            archive_size=archive_size,
                        ),
                        timeout_seconds=_stage_remaining(deadline),
                        remote_failure_code=SshTransportErrorCode.MANAGED_RUNTIME_FAILED,
                        cancel_event=cancel_event,
                    )
                    transfer = parse_managed_runtime_prepare(prepared)
                    try:
                        self._upload_managed_runtime_snapshot(
                            snapshot.root,
                            transfer,
                            archive_size=archive_size,
                            deadline=deadline,
                            cancel_event=cancel_event,
                        )
                        remaining = _stage_remaining(deadline)
                        finalized = self._run_secret_with_remote_failure(
                            build_managed_runtime_finalize_command(
                                runtime,
                                transfer,
                                archive_sha256=archive_sha256,
                                archive_size=archive_size,
                                platform=platform,
                                config_id=config_id,
                                oci_index_id=oci_index_id,
                                aliases=aliases,
                                load_timeout_seconds=max(1, min(900, int(remaining))),
                            ),
                            timeout_seconds=remaining,
                            remote_failure_code=SshTransportErrorCode.MANAGED_RUNTIME_FAILED,
                            cancel_event=cancel_event,
                        )
                        receipt = parse_managed_runtime_receipt(finalized)
                        if receipt.reused:
                            raise ValueError("managed runtime load receipt is inconsistent")
                        return receipt
                    except BaseException:
                        self._discard_managed_runtime_transfer(
                            runtime,
                            transfer,
                            archive_sha256=archive_sha256,
                            archive_size=archive_size,
                        )
                        raise
            except ManagedRuntimeArchiveSnapshotError:
                _log_transport_failure(SshTransportErrorCode.INVALID_REQUEST)
                raise SshTransportError(SshTransportErrorCode.INVALID_REQUEST) from None
            except SshTransportError:
                raise
            except (OSError, TypeError, ValueError):
                _log_transport_failure(SshTransportErrorCode.MANAGED_RUNTIME_FAILED)
                raise SshTransportError(SshTransportErrorCode.MANAGED_RUNTIME_FAILED) from None
        finally:
            self._managed_runtime_lock.release()

    def _discard_managed_runtime_transfer(
        self,
        runtime: CorePythonRuntimeAuthority,
        transfer: ManagedRuntimeTransfer,
        *,
        archive_sha256: str,
        archive_size: int,
    ) -> None:
        try:
            response = self._run_secret_with_remote_failure(
                build_managed_runtime_discard_command(
                    runtime,
                    transfer,
                    archive_sha256=archive_sha256,
                    archive_size=archive_size,
                ),
                timeout_seconds=_CORE_ASSET_CLEANUP_SECONDS,
                remote_failure_code=SshTransportErrorCode.MANAGED_RUNTIME_FAILED,
            )
            parse_managed_runtime_discard(response)
        except Exception:
            _log_transport_failure(SshTransportErrorCode.MANAGED_RUNTIME_FAILED)
            raise SshTransportError(SshTransportErrorCode.MANAGED_RUNTIME_FAILED) from None

    def _upload_managed_runtime_snapshot(
        self,
        local_root: Path,
        transfer: ManagedRuntimeTransfer,
        *,
        archive_size: int,
        deadline: float,
        cancel_event: threading.Event | None,
    ) -> None:
        failure_code: SshTransportErrorCode | None = None
        completed: subprocess.CompletedProcess[str] | None = None
        phase = "trust"
        try:
            phase = "process"
            completed, _known_hosts_file = self._run_trusted_subprocess(
                lambda known_hosts_file: self._managed_runtime_rsync_argv(
                    local_root,
                    transfer,
                    known_hosts_file,
                    archive_size=archive_size,
                ),
                _stage_remaining(deadline),
                cancel_event=cancel_event,
            )
        except _SubprocessCancelled:
            failure_code = SshTransportErrorCode.CANCELLED
        except subprocess.TimeoutExpired:
            failure_code = SshTransportErrorCode.TIMEOUT
        except _KnownHostsSpawnFailure:
            failure_code = SshTransportErrorCode.HOST_KEY_VERIFICATION_FAILED
        except SshTransportError:
            raise
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

    def select_core_python_runtime(
        self,
        *,
        timeout_seconds: float,
        cancel_event: threading.Event | None = None,
    ) -> CorePythonRuntimeAuthority:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 < timeout_seconds <= 300
        ):
            raise SshTransportError(SshTransportErrorCode.INVALID_REQUEST)
        payload = self._run_secret_with_remote_failure(
            build_core_supervisor_runtime_preflight_command(
                timeout_seconds=float(timeout_seconds)
            ),
            timeout_seconds=float(timeout_seconds),
            remote_failure_code=SshTransportErrorCode.CORE_RUNTIME_PREFLIGHT_FAILED,
            env=_core_runtime_proxy_env(self._profile),
            cancel_event=cancel_event,
        )
        try:
            selection = parse_core_supervisor_runtime_preflight(payload)
        except (TypeError, ValueError):
            _log_transport_failure(SshTransportErrorCode.CORE_RUNTIME_PREFLIGHT_FAILED)
            raise SshTransportError(SshTransportErrorCode.CORE_RUNTIME_PREFLIGHT_FAILED) from None
        if selection.authority is not None:
            return selection.authority
        reason_codes = {
            "no_supported_python": SshTransportErrorCode.CORE_PYTHON_UNAVAILABLE,
            "python_provision_failed": SshTransportErrorCode.CORE_PYTHON_PROVISION_FAILED,
            "kernel_syscall_unsupported": (SshTransportErrorCode.CORE_KERNEL_SYSCALL_UNSUPPORTED),
        }
        code = reason_codes.get(
            selection.reason,
            SshTransportErrorCode.CORE_RUNTIME_UNSUPPORTED,
        )
        _log_transport_failure(code)
        raise SshTransportError(code)

    def _remember_core_asset_transfer(
        self,
        authority: _CoreAssetTransferAuthority,
        *,
        active: bool = False,
        consume_admission: bool = False,
    ) -> None:
        with self._core_asset_authority_lock:
            admission = self._find_core_asset_admission_locked(authority.key)
            if admission is None or admission.authority is None:
                raise RuntimeError("Core asset admission ownership is missing")
            existing = admission.authority
            if consume_admission and existing != authority:
                raise SshTransportError(SshTransportErrorCode.CORE_ASSET_FAILED)
            if not consume_admission and authority not in (
                existing,
                existing.with_finalize_started(),
            ):
                raise SshTransportError(SshTransportErrorCode.CORE_ASSET_FAILED)
            admission.authority = authority
            if active:
                admission.active = True

    @property
    def _core_asset_cleanup_authorities(
        self,
    ) -> dict[tuple[str, str, str], _CoreAssetTransferAuthority]:
        with self._core_asset_authority_lock:
            return {
                admission.authority.key: admission.authority
                for admission in self._core_asset_transfer_ownerships
                if admission.authority is not None
            }

    @property
    def _core_asset_active_transfers(self) -> set[tuple[str, str, str]]:
        with self._core_asset_authority_lock:
            return {
                admission.authority.key
                for admission in self._core_asset_transfer_ownerships
                if admission.active and admission.authority is not None
            }

    @property
    def _core_asset_pending_admissions(self) -> int:
        with self._core_asset_authority_lock:
            return sum(
                admission.authority is None for admission in self._core_asset_transfer_ownerships
            )

    def _admit_core_asset_transfer(self, admission: _CoreAssetTransferAdmission) -> None:
        with self._core_asset_authority_lock:
            if len(self._core_asset_transfer_ownerships) >= _MAX_CORE_ASSET_CLEANUP_AUTHORITIES:
                raise SshTransportError(SshTransportErrorCode.CORE_ASSET_FAILED)
            if admission in self._core_asset_transfer_ownerships:
                raise RuntimeError("Core asset admission is already owned")
            self._core_asset_transfer_ownerships.add(admission)

    def _bind_core_asset_admission(
        self,
        admission: _CoreAssetTransferAdmission,
        authority: _CoreAssetTransferAuthority,
    ) -> None:
        with self._core_asset_authority_lock:
            if (
                admission not in self._core_asset_transfer_ownerships
                or admission.authority is not None
                or self._find_core_asset_admission_locked(authority.key) is not None
            ):
                raise SshTransportError(SshTransportErrorCode.CORE_ASSET_FAILED)
            admission.authority = authority

    def _release_core_asset_admission(
        self,
        admission: _CoreAssetTransferAdmission,
    ) -> None:
        with self._core_asset_authority_lock:
            if admission.authority is None:
                self._core_asset_transfer_ownerships.discard(admission)

    def _recover_core_asset_prepare_handoff(
        self,
        admission: _CoreAssetTransferAdmission,
        authority: _CoreAssetTransferAuthority,
    ) -> None:
        with self._core_asset_authority_lock:
            if admission not in self._core_asset_transfer_ownerships:
                raise SshTransportError(SshTransportErrorCode.CORE_ASSET_FAILED)
            admission.active = False
            existing = admission.authority
            if existing is not None and existing != authority:
                raise SshTransportError(SshTransportErrorCode.CORE_ASSET_FAILED)
            conflicting = self._find_core_asset_admission_locked(authority.key)
            if conflicting not in {None, admission}:
                raise SshTransportError(SshTransportErrorCode.CORE_ASSET_FAILED)
            admission.authority = authority

    def _find_core_asset_admission_locked(
        self,
        key: tuple[str, str, str],
    ) -> _CoreAssetTransferAdmission | None:
        return next(
            (
                admission
                for admission in self._core_asset_transfer_ownerships
                if admission.authority is not None and admission.authority.key == key
            ),
            None,
        )

    def _mark_core_asset_transfer_inactive(
        self,
        authority: _CoreAssetTransferAuthority,
    ) -> None:
        with self._core_asset_authority_lock:
            admission = self._find_core_asset_admission_locked(authority.key)
            if admission is not None:
                admission.active = False

    def _publish_core_asset_authority(
        self,
        authority: _CoreAssetTransferAuthority,
        staged: StagedCoreBootstrapAssets,
    ) -> None:
        final_root = str(Path(staged.wheel_path).parent)
        with self._core_asset_authority_lock:
            self._core_asset_authorities[final_root] = (staged, authority.runtime)
            admission = self._find_core_asset_admission_locked(authority.key)
            if admission is not None:
                admission.active = False
                self._core_asset_transfer_ownerships.discard(admission)

    def _finalize_core_asset_transfer(
        self,
        authority: _CoreAssetTransferAuthority,
        *,
        deadline: float,
    ) -> StagedCoreBootstrapAssets:
        finalize_payload = self._run_secret_with_remote_failure(
            build_core_asset_finalize_command(
                runtime=authority.runtime,
                service_root=authority.service_root,
                bundle_id=authority.bundle_id,
                transfer_id=authority.transfer_id,
                wheel_filename=authority.wheel_filename,
                wheel_sha256=authority.wheel_sha256,
                wheel_size=authority.wheel_size,
                framework_lock_sha256=authority.framework_lock_sha256,
                framework_lock_size=authority.framework_lock_size,
            ),
            timeout_seconds=_stage_remaining(deadline),
            remote_failure_code=SshTransportErrorCode.CORE_ASSET_FAILED,
        )
        return parse_staged_core_assets(
            finalize_payload,
            service_root=authority.service_root,
            bundle_id=authority.bundle_id,
            wheel_filename=authority.wheel_filename,
            wheel_sha256=authority.wheel_sha256,
            wheel_size=authority.wheel_size,
            framework_lock_sha256=authority.framework_lock_sha256,
            framework_lock_size=authority.framework_lock_size,
        )

    def _reconcile_core_asset_transfer(
        self,
        authority: _CoreAssetTransferAuthority,
        *,
        deadline: float,
    ) -> StagedCoreBootstrapAssets | None:
        try:
            staged = self._finalize_core_asset_transfer(authority, deadline=deadline)
        except SshTransportError as exc:
            if exc.code is SshTransportErrorCode.CORE_ASSET_FAILED:
                self._discard_core_asset_transfer(authority, deadline=deadline)
            return None
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            return None
        self._publish_core_asset_authority(authority, staged)
        return staged

    def _retry_core_asset_transfer_cleanup(self, *, deadline: float) -> None:
        with self._core_asset_authority_lock:
            authorities = tuple(
                admission.authority
                for admission in self._core_asset_transfer_ownerships
                if not admission.active and admission.authority is not None
            )
        for authority in authorities:
            if time.monotonic() >= deadline:
                return
            if authority.finalize_started:
                self._reconcile_core_asset_transfer(authority, deadline=deadline)
            else:
                self._discard_core_asset_transfer(authority, deadline=deadline)

    def _discard_core_asset_transfer(
        self,
        authority: _CoreAssetTransferAuthority,
        *,
        deadline: float,
    ) -> bool:
        try:
            timeout_seconds = _stage_remaining(deadline)
            self._run_secret_with_remote_failure(
                build_core_asset_discard_command(
                    runtime=authority.runtime,
                    service_root=authority.service_root,
                    bundle_id=authority.bundle_id,
                    transfer_id=authority.transfer_id,
                ),
                timeout_seconds=timeout_seconds,
                remote_failure_code=SshTransportErrorCode.CORE_ASSET_FAILED,
            )
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            return False
        with self._core_asset_authority_lock:
            admission = self._find_core_asset_admission_locked(authority.key)
            if admission is not None:
                admission.active = False
                self._core_asset_transfer_ownerships.discard(admission)
        return True

    def _upload_core_asset_snapshot(
        self,
        local_root: Path,
        remote_root: str,
        *,
        authority: _CoreAssetTransferAuthority,
        deadline: float,
    ) -> None:
        failure_code: SshTransportErrorCode | None = None
        completed: subprocess.CompletedProcess[str] | None = None
        phase = "trust"
        try:
            phase = "process"
            completed, _known_hosts_file = self._run_trusted_subprocess(
                lambda known_hosts_file: self._core_asset_rsync_argv(
                    local_root,
                    remote_root,
                    known_hosts_file,
                    authority=authority,
                ),
                _stage_remaining(deadline),
            )
        except subprocess.TimeoutExpired:
            failure_code = SshTransportErrorCode.TIMEOUT
        except _KnownHostsSpawnFailure:
            failure_code = SshTransportErrorCode.HOST_KEY_VERIFICATION_FAILED
        except SshTransportError:
            raise
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
        return self._run_secret_with_remote_failure(
            command,
            timeout_seconds=timeout_seconds,
            remote_failure_code=SshTransportErrorCode.CONNECTION_FAILED,
        )

    def _run_secret_with_remote_failure(
        self,
        command: str,
        *,
        timeout_seconds: float,
        remote_failure_code: SshTransportErrorCode,
        env: dict[str, str] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> SecretStr:
        try:
            bound_command = self._bind_core_asset_consumption(command)
            remote_command = _remote_command(bound_command, cwd=None, env=env)
        except Exception:
            raise SshTransportError(SshTransportErrorCode.INVALID_REQUEST) from None
        completion_marker = f"__OPENEVO_REMOTE_COMPLETION_{secrets.token_hex(16)}__="
        marked_command = _with_completion_marker(remote_command, completion_marker)
        completed: subprocess.CompletedProcess[str] | None = None
        failure_code: SshTransportErrorCode | None = None
        try:
            completed, _known_hosts_file = self._run_trusted_subprocess(
                lambda known_hosts_file: self._ssh_argv(marked_command, known_hosts_file),
                timeout_seconds,
                cancel_event=cancel_event,
            )
        except _SubprocessCancelled:
            failure_code = SshTransportErrorCode.CANCELLED
        except subprocess.TimeoutExpired:
            failure_code = SshTransportErrorCode.TIMEOUT
        except _KnownHostsSpawnFailure:
            failure_code = SshTransportErrorCode.HOST_KEY_VERIFICATION_FAILED
        except Exception:
            failure_code = SshTransportErrorCode.START_FAILED
        if failure_code is not None:
            _log_transport_failure(failure_code)
            raise SshTransportError(failure_code)
        assert completed is not None
        _stderr, remote_return_code = _extract_remote_completion(
            completed.stderr or "",
            completion_marker,
        )
        if remote_return_code is None or completed.returncode == 255:
            _log_transport_failure(SshTransportErrorCode.CONNECTION_FAILED)
            raise SshTransportError(SshTransportErrorCode.CONNECTION_FAILED)
        if int(completed.returncode) != remote_return_code:
            _log_transport_failure(SshTransportErrorCode.CONNECTION_FAILED)
            raise SshTransportError(SshTransportErrorCode.CONNECTION_FAILED)
        if remote_return_code != 0:
            _log_transport_failure(remote_failure_code)
            raise SshTransportError(remote_failure_code)
        return SecretStr(completed.stdout or "")

    def _bind_core_asset_consumption(self, command: str) -> str:
        with self._core_asset_authority_lock:
            matches = [
                authority
                for root, authority in self._core_asset_authorities.items()
                if root in command
            ]
        if not matches:
            return command
        if len(matches) != 1:
            raise ValueError("Core asset consumer command has ambiguous authority")
        assets, runtime = matches[0]
        return build_core_asset_consumer_command(command, assets, runtime)

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
        process_authority: _OwnedSubprocessAuthority | None = None
        trust_ownership: _KnownHostsLeaseOwnership | None = None
        try:
            argv = [
                *self._ssh_base_argv(known_hosts_file),
                "-o",
                "ExitOnForwardFailure=yes",
                "-N",
                "-L",
                forward_spec,
                "--",
                self._profile.host,
            ]
            if self._tunnel_starter is None:
                trust_ownership = _KnownHostsLeaseOwnership(trust_lease)
                process_authority = _OwnedSubprocessAuthority(trust_ownership=trust_ownership)
                process_authority.acquire()
                process_authority.spawn_tunnel(
                    argv,
                    env=self._process_environment(),
                    agent_socket_source=self._agent_socket_source,
                )
                process = process_authority.process
                if process is None:
                    raise RuntimeError("SSH tunnel child birth was not observed")
            else:
                process = self._tunnel_starter(argv)
        except BaseException as exc:
            if trust_ownership is None:
                _release_trust_lease(trust_lease)
            else:
                if process_authority is not None and not process_authority.released:
                    try:
                        process_authority.cleanup()
                    except BaseException:
                        process_authority.retain()
                        _log_transport_failure(SshTransportErrorCode.START_FAILED)
                trust_ownership.release_if_caller_owned()
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
                trust_lease=trust_lease if process_authority is None else None,
                trusted_host=self._trusted_host,
                process_authority=process_authority,
            )
        except Exception:
            tunnel_failed = True
            tunnel = None
        if tunnel_failed or tunnel is None:
            _log_transport_failure(SshTransportErrorCode.START_FAILED)
            raise SshTransportError(SshTransportErrorCode.START_FAILED)
        self._register_tunnel(tunnel)
        if wait_for_ready:
            ready_failure: SshTransportErrorCode | None = None
            try:
                _wait_for_local_port(
                    tunnel,
                    timeout_seconds=timeout_seconds,
                )
            except TimeoutError:
                ready_failure = SshTransportErrorCode.TIMEOUT
            except BaseException as exc:
                try:
                    tunnel.close()
                except BaseException:
                    tunnel._retain_orphan()
                    _log_transport_failure(SshTransportErrorCode.CONNECTION_FAILED)
                if not isinstance(exc, Exception):
                    raise
                ready_failure = SshTransportErrorCode.CONNECTION_FAILED
            if ready_failure is not None:
                try:
                    tunnel.close()
                except BaseException:
                    tunnel._retain_orphan()
                    _log_transport_failure(SshTransportErrorCode.CONNECTION_FAILED)
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
                agent_socket_source=self._agent_socket_source,
            )
        except BaseException as exc:
            if isinstance(exc, Exception):
                _log_transport_failure(SshTransportErrorCode.START_FAILED)
                raise SshTransportError(SshTransportErrorCode.START_FAILED) from None
            raise
        tunnel = SshCoreTunnel(endpoint)
        self._register_tunnel(tunnel)
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
        argv = [SSH_EXECUTABLE, "-F", "/dev/null", "-p", str(profile.port)]
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
        else:
            argv.extend(
                [
                    "-o",
                    "IdentityFile=none",
                    "-o",
                    "IdentitiesOnly=no",
                    "-o",
                    "IdentityAgent=SSH_AUTH_SOCK",
                ]
            )
        argv.extend(
            [
                "-o",
                "PasswordAuthentication=no",
                "-o",
                "KbdInteractiveAuthentication=no",
                "-o",
                "ChallengeResponseAuthentication=no",
                "-o",
                "GSSAPIAuthentication=no",
                "-o",
                "HostbasedAuthentication=no",
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
            RSYNC_EXECUTABLE,
            "-az",
            "--delete",
            "-e",
            ssh_command,
            _with_trailing_slash(str(local)),
            (f"{_rsync_host(self._profile.host)}:{_with_trailing_slash(remote_path)}"),
        ]

    def _core_asset_rsync_argv(
        self,
        local_root: Path,
        remote_root: str,
        known_hosts_file: Path,
        *,
        authority: _CoreAssetTransferAuthority,
    ) -> list[str]:
        _validate_remote_absolute_path(remote_root, "remote_root")
        ssh_command = " ".join(shlex.quote(part) for part in self._ssh_base_argv(known_hosts_file))
        return [
            RSYNC_EXECUTABLE,
            "--recursive",
            "--delete",
            f"--filter=protect /{CORE_ASSET_TRANSFER_LEASE}",
            "--chmod=F600,D700",
            "--no-owner",
            "--no-group",
            "--rsync-path",
            build_core_asset_rsync_path(
                service_root=authority.service_root,
                bundle_id=authority.bundle_id,
                transfer_id=authority.transfer_id,
            ),
            "-e",
            ssh_command,
            _with_trailing_slash(str(local_root)),
            f"{_rsync_host(self._profile.host)}:{_with_trailing_slash(remote_root)}",
        ]

    def _managed_runtime_rsync_argv(
        self,
        local_root: Path,
        transfer: ManagedRuntimeTransfer,
        known_hosts_file: Path,
        *,
        archive_size: int,
    ) -> list[str]:
        ssh_command = " ".join(shlex.quote(part) for part in self._ssh_base_argv(known_hosts_file))
        return [
            RSYNC_EXECUTABLE,
            "--recursive",
            "--inplace",
            "--delete",
            f"--max-size={archive_size}",
            f"--filter=protect /{MANAGED_RUNTIME_TRANSFER_LEASE}",
            "--chmod=F600,D700",
            "--no-owner",
            "--no-group",
            "--rsync-path",
            build_managed_runtime_rsync_path(
                transfer,
                archive_size=archive_size,
            ),
            "-e",
            ssh_command,
            _with_trailing_slash(str(local_root)),
            f"{_rsync_host(self._profile.host)}:{_with_trailing_slash(transfer.incoming_root)}",
        ]


class _KnownHostsLeaseOwnership:
    """Transfer one entered known-host lease to one spawned-process authority."""

    def __init__(self, trust_lease: AbstractContextManager[Path]) -> None:
        self._trust_lease: AbstractContextManager[Path] | None = trust_lease
        self._owner: _OwnedSubprocessAuthority | None = None
        self._guard = threading.Lock()

    def transfer_to(self, authority: _OwnedSubprocessAuthority) -> None:
        with self._guard:
            if self._trust_lease is None or self._owner is not None:
                raise RuntimeError("known-host lease ownership is unavailable")
            self._owner = authority

    def release_if_caller_owned(self) -> bool:
        with self._guard:
            if self._owner is not None or self._trust_lease is None:
                return True
            trust_lease = self._trust_lease
        if not _release_trust_lease(trust_lease):
            return False
        with self._guard:
            if self._owner is None and self._trust_lease is trust_lease:
                self._trust_lease = None
        return True

    def release_for(self, authority: _OwnedSubprocessAuthority) -> bool:
        with self._guard:
            if self._owner is not authority:
                return self._owner is None
            trust_lease = self._trust_lease
        if trust_lease is not None and not _release_trust_lease(
            trust_lease,
            retain_for_retry=False,
        ):
            return False
        with self._guard:
            if self._owner is authority:
                self._owner = None
                self._trust_lease = None
        return True


class _NonReapingPopen(subprocess.Popen):
    """Leave wait ownership with `_OwnedSubprocessAuthority`."""

    def poll(self) -> int | None:
        if self.returncode is None:
            raise RuntimeError("owned subprocess liveness requires its non-reaping observer")
        return self.returncode

    def __del__(self) -> None:
        return


_OWNED_SUBPROCESS_POPEN = _NonReapingPopen
_TUNNEL_SUBPROCESS_POPEN = _NonReapingPopen


class _RecoveredSubprocess:
    """Minimal wait handle rebuilt from the child-published birth record."""

    stdout: BinaryIO | None = None
    stderr: BinaryIO | None = None

    def __init__(self, process_id: int) -> None:
        self.pid = process_id
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is None:
            raise RuntimeError("owned subprocess liveness requires its non-reaping observer")
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is not None:
            return self.returncode
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            return_code = self._waitpid(os.WNOHANG if deadline is not None else 0)
            if return_code is not None:
                return return_code
            if deadline is None:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(self.pid, timeout)
            time.sleep(min(_SUBPROCESS_STATUS_INTERVAL_SECONDS, remaining))

    def _waitpid(self, options: int) -> int | None:
        try:
            waited_pid, status = os.waitpid(self.pid, options)
        except InterruptedError:
            return None
        except ChildProcessError as exc:
            raise RuntimeError("subprocess wait ownership was lost") from exc
        if waited_pid == 0:
            return None
        if waited_pid != self.pid:
            raise RuntimeError("subprocess wait returned an unexpected child")
        self.returncode = os.waitstatus_to_exitcode(status)
        return self.returncode


class _OwnedSubprocessAuthority:
    """Own one registry slot and lease before allowing subprocess birth."""

    def __init__(
        self,
        *,
        trust_ownership: _KnownHostsLeaseOwnership | None,
    ) -> None:
        self.process: _OwnedSubprocessProcess | None = None
        self.process_group_id: int | None = None
        self.exit_observer: _SubprocessExitObserver | None = None
        self._trust_ownership = trust_ownership
        self._birth_record: BinaryIO | None = None
        self._agent_proxy: SshAgentProxy | None = None
        self._spawn_outcome_unknown = False
        self._group_cleanup_confirmed = False
        self._slot_held = False
        self._retained = False
        self._cleanup_guard = threading.Lock()

    def acquire(self) -> None:
        if self._slot_held or self._birth_record is not None:
            raise RuntimeError("subprocess authority is already acquired")
        _retry_orphaned_subprocess_cleanup()
        try:
            self._reserve_slot()
        except BaseException:
            self._group_cleanup_confirmed = True
            try:
                self.release()
            except BaseException:
                self.retain()
            raise
        try:
            if self._trust_ownership is not None:
                self._trust_ownership.transfer_to(self)
        except BaseException:
            self._group_cleanup_confirmed = True
            self.release()
            raise

    @classmethod
    def spawn(
        cls,
        argv: list[str],
        *,
        trust_lease: AbstractContextManager[Path] | None = None,
        trust_ownership: _KnownHostsLeaseOwnership | None = None,
        env: dict[str, str] | None = None,
        agent_socket_source: SshAgentSocketSource | None = None,
        stdin_fd: int | None = None,
    ) -> _OwnedSubprocessAuthority:
        if trust_lease is not None and trust_ownership is not None:
            raise RuntimeError("known-host lease has multiple ownership sources")
        caller_ownership = (
            _KnownHostsLeaseOwnership(trust_lease) if trust_lease is not None else None
        )
        active_ownership = trust_ownership or caller_ownership
        authority = cls(trust_ownership=active_ownership)
        try:
            authority.acquire()
            authority._spawn(
                argv,
                env=env,
                agent_socket_source=agent_socket_source,
                stdin_fd=stdin_fd,
            )
            return authority
        except BaseException:
            try:
                authority.close_agent_proxy()
                authority.cleanup()
            except BaseException:
                authority.retain()
                _log_transport_failure(SshTransportErrorCode.START_FAILED)
            raise
        finally:
            if caller_ownership is not None:
                caller_ownership.release_if_caller_owned()

    def spawn_tunnel(
        self,
        argv: list[str],
        *,
        stream_fd: int | None = None,
        env: dict[str, str] | None = None,
        agent_socket_source: SshAgentSocketSource | None = None,
    ) -> None:
        try:
            self._spawn_tunnel(
                argv,
                stream_fd=stream_fd,
                env=env,
                agent_socket_source=agent_socket_source,
            )
            self.initialize_observer()
        except BaseException:
            try:
                self.close_agent_proxy()
                if self.process is None and self._spawn_outcome_unknown:
                    self._recover_spawned_process(
                        deadline=time.monotonic() + _SUBPROCESS_BIRTH_RECOVERY_SECONDS
                    )
                self.cleanup()
            except BaseException:
                self.retain()
                _log_transport_failure(SshTransportErrorCode.START_FAILED)
            raise

    def _reserve_slot(self) -> None:
        with _ORPHANED_SUBPROCESS_GUARD:
            if len(_ORPHANED_SUBPROCESSES) >= _MAX_OWNED_SUBPROCESSES:
                raise RuntimeError("subprocess ownership capacity is exhausted")
            self._slot_held = True
            _ORPHANED_SUBPROCESSES[id(self)] = self
            birth_record = tempfile.TemporaryFile(prefix="openevo-subprocess-birth-")
            self._birth_record = birth_record
            os.fchmod(birth_record.fileno(), 0o600)

    def _spawn(
        self,
        argv: list[str],
        *,
        env: dict[str, str] | None,
        agent_socket_source: SshAgentSocketSource | None,
        stdin_fd: int | None,
    ) -> None:
        birth_record = self._birth_record
        if birth_record is None:
            raise RuntimeError("subprocess birth authority is unavailable")
        birth_record_fd = birth_record.fileno()
        executable, nested, spawn_argv = _prepare_verified_spawn(argv)
        try:
            child_environment = _require_closed_child_environment(env)
            self._open_agent_proxy(agent_socket_source)
            agent_proxy = self._agent_proxy
            if agent_proxy is not None:
                child_environment["SSH_AUTH_SOCK"] = agent_proxy.socket_path
            descriptors = [
                birth_record_fd,
                executable.descriptor,
                *(item.descriptor for item in nested),
            ]
            if stdin_fd is not None:
                descriptors.append(stdin_fd)
            executable.verify_path_binding()
            for item in nested:
                item.verify_path_binding()
            if agent_proxy is not None:
                agent_proxy.verify_upstream_binding()
            self._spawn_outcome_unknown = True
            launcher = _owned_subprocess_launcher()
            process = _OWNED_SUBPROCESS_POPEN(
                _subprocess_birth_argv(
                    spawn_argv,
                    birth_record_fd,
                    executable.descriptor,
                ),
                executable=launcher,
                stdin=subprocess.DEVNULL if stdin_fd is None else stdin_fd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                start_new_session=True,
                close_fds=True,
                pass_fds=tuple(descriptors),
                env=child_environment,
            )
            self.process_group_id = process.pid
            self.process = process
            self._spawn_outcome_unknown = False
            executable.verify_path_binding()
            for item in nested:
                item.verify_path_binding()
            if agent_proxy is not None:
                agent_proxy.verify_upstream_binding()
                ssh_executable = nested[0] if nested else executable
                agent_proxy.bind_child(
                    session_id=process.pid,
                    process_group_id=process.pid,
                    executable_identity=ssh_executable.identity,
                )
        finally:
            for item in reversed(nested):
                item.close()
            executable.close()

    def _spawn_tunnel(
        self,
        argv: list[str],
        *,
        stream_fd: int | None,
        env: dict[str, str] | None,
        agent_socket_source: SshAgentSocketSource | None,
    ) -> None:
        birth_record = self._birth_record
        if birth_record is None:
            raise RuntimeError("subprocess birth authority is unavailable")
        birth_record_fd = birth_record.fileno()
        executable, nested, spawn_argv = _prepare_verified_spawn(argv)
        try:
            child_environment = _require_closed_child_environment(env)
            self._open_agent_proxy(agent_socket_source)
            agent_proxy = self._agent_proxy
            if agent_proxy is not None:
                child_environment["SSH_AUTH_SOCK"] = agent_proxy.socket_path
            descriptors = [
                birth_record_fd,
                executable.descriptor,
                *(item.descriptor for item in nested),
            ]
            if stream_fd is not None:
                descriptors.append(stream_fd)
            executable.verify_path_binding()
            for item in nested:
                item.verify_path_binding()
            if agent_proxy is not None:
                agent_proxy.verify_upstream_binding()
            self._spawn_outcome_unknown = True
            launcher = _owned_subprocess_launcher()
            process = _TUNNEL_SUBPROCESS_POPEN(
                _subprocess_birth_argv(
                    spawn_argv,
                    birth_record_fd,
                    executable.descriptor,
                ),
                executable=launcher,
                stdin=subprocess.DEVNULL if stream_fd is None else stream_fd,
                stdout=subprocess.DEVNULL if stream_fd is None else stream_fd,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                text=False,
                start_new_session=True,
                pass_fds=tuple(descriptors),
                env=child_environment,
            )
            self.process_group_id = process.pid
            self.process = process
            self._spawn_outcome_unknown = False
            executable.verify_path_binding()
            for item in nested:
                item.verify_path_binding()
            if agent_proxy is not None:
                agent_proxy.verify_upstream_binding()
                ssh_executable = nested[0] if nested else executable
                agent_proxy.bind_child(
                    session_id=process.pid,
                    process_group_id=process.pid,
                    executable_identity=ssh_executable.identity,
                )
        finally:
            for item in reversed(nested):
                item.close()
            executable.close()

    def _recover_spawned_process(self, *, deadline: float) -> bool:
        if self.process is not None:
            if self.process_group_id is None:
                self.process_group_id = self.process.pid
            return True
        while True:
            process_id = _read_subprocess_birth_record(self._birth_record)
            if process_id is not None:
                self.process = _RecoveredSubprocess(process_id)
                self.process_group_id = process_id
                self._spawn_outcome_unknown = False
                return True
            if self.process_group_id is not None:
                self.process = _RecoveredSubprocess(self.process_group_id)
                self._spawn_outcome_unknown = False
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.01, remaining))

    def _open_agent_proxy(self, source: SshAgentSocketSource | None) -> None:
        if self._agent_proxy is not None:
            raise RuntimeError("SSH agent proxy authority is already held")
        if source is not None:
            self._agent_proxy = source.open_proxy()

    def close_agent_proxy(self) -> None:
        proxy = self._agent_proxy
        if proxy is None:
            return
        proxy.close()
        self._agent_proxy = None

    def initialize_observer(self) -> None:
        process = self.process
        if process is None:
            raise RuntimeError("subprocess birth has not been observed")
        self.process_group_id = _owned_process_group_id(process)
        self.exit_observer = _SubprocessExitObserver(process)

    def cleanup(self) -> None:
        with self._cleanup_guard:
            if self.process is None and not self._spawn_outcome_unknown:
                self._group_cleanup_confirmed = True
                self.close_observer()
                if not self.release():
                    self.retain()
                return
            if self.process is None and not self._recover_spawned_process(
                deadline=time.monotonic() + _SUBPROCESS_BIRTH_RECOVERY_SECONDS
            ):
                self.retain()
                raise RuntimeError("subprocess birth outcome could not be confirmed")
            if self._group_cleanup_confirmed and self._is_reaped():
                self.close_observer()
                if not self.release():
                    self.retain()
                return
            process = self.process
            process_group_id = self.process_group_id
            if process is not None and process_group_id is None:
                process_group_id = process.pid
                self.process_group_id = process_group_id
            if process is None or process_group_id is None:
                self.retain()
                raise RuntimeError("subprocess cleanup authority is incomplete")
            failure: BaseException | None = None
            try:
                if process.returncode is not None:
                    _confirm_owned_process_group_disappeared(
                        process_group_id=process_group_id,
                    )
                    self.mark_group_cleanup_confirmed()
                else:
                    _terminate_and_reap_subprocess(
                        process,
                        process_group_id=process_group_id,
                        exit_observer=self.exit_observer,
                        on_group_cleanup_confirmed=self.mark_group_cleanup_confirmed,
                    )
            except BaseException as exc:
                failure = exc
            if self._group_cleanup_confirmed and self._is_reaped():
                self.close_observer()
                self.release()
            else:
                self.retain()
            if failure is not None:
                raise failure

    def close_observer(self) -> None:
        observer = self.exit_observer
        self.exit_observer = None
        if observer is not None:
            observer.close()

    @property
    def released(self) -> bool:
        with _ORPHANED_SUBPROCESS_GUARD:
            return not self._slot_held

    def request_group_termination(self) -> None:
        process = self.process
        process_group_id = self.process_group_id
        if process is None or process_group_id is None:
            self.retain()
            raise RuntimeError("subprocess cleanup authority is incomplete")
        _signal_owned_process_group(
            process,
            process_group_id=process_group_id,
            signal_number=signal.SIGTERM,
        )

    def leader_exited(self) -> bool:
        process = self.process
        observer = self.exit_observer
        if process is None or observer is None:
            raise RuntimeError("subprocess exit observer is unavailable")
        return observer.exited()

    def retain(self) -> None:
        with _ORPHANED_SUBPROCESS_GUARD:
            if not self._slot_held or self._retained:
                return
            _ORPHANED_SUBPROCESSES[id(self)] = self
            self._retained = True

    def mark_group_cleanup_confirmed(self) -> None:
        self._group_cleanup_confirmed = True

    def release(self) -> bool:
        if not self._group_cleanup_confirmed or not self._is_reaped():
            self.retain()
            return False
        birth_record = self._birth_record
        if not _discard_subprocess_birth_record(birth_record):
            self.retain()
            return False
        if self._birth_record is birth_record:
            self._birth_record = None
        agent_proxy = self._agent_proxy
        if agent_proxy is not None:
            try:
                self.close_agent_proxy()
            except BaseException:
                self.retain()
                return False
        trust_ownership = self._trust_ownership
        if trust_ownership is not None and not trust_ownership.release_for(self):
            self.retain()
            return False
        with _ORPHANED_SUBPROCESS_GUARD:
            if not self._slot_held:
                return True
            _ORPHANED_SUBPROCESSES.pop(id(self), None)
            self._retained = False
            self._slot_held = False
            self._trust_ownership = None
        return True

    def release_if_reaped(self) -> bool:
        if not self._group_cleanup_confirmed or not self._is_reaped():
            self.retain()
            return False
        return self.release()

    def _is_reaped(self) -> bool:
        process = self.process
        if process is None:
            return not self._spawn_outcome_unknown
        return process.returncode is not None


def _prepare_verified_spawn(
    argv: list[str],
) -> tuple[VerifiedSystemExecutable, list[VerifiedSystemExecutable], list[str]]:
    if not argv:
        raise RuntimeError("subprocess argv is empty")
    executable = VerifiedSystemExecutable.open(argv[0])
    nested: list[VerifiedSystemExecutable] = []
    spawn_argv = list(argv)
    try:
        if argv[0] == RSYNC_EXECUTABLE:
            try:
                command_index = spawn_argv.index("-e") + 1
                command = shlex.split(spawn_argv[command_index])
            except (ValueError, IndexError) as exc:
                raise RuntimeError("rsync SSH command is missing") from exc
            if not command or command[0] != SSH_EXECUTABLE:
                raise RuntimeError("rsync SSH executable is not fixed")
            ssh = VerifiedSystemExecutable.open(SSH_EXECUTABLE)
            nested.append(ssh)
            command[0] = ssh.execution_path
            spawn_argv[command_index] = " ".join(shlex.quote(part) for part in command)
        elif argv[0] != SSH_EXECUTABLE:
            raise RuntimeError("subprocess executable is not supported")
        return executable, nested, spawn_argv
    except BaseException:
        for item in reversed(nested):
            item.close()
        executable.close()
        raise


def _subprocess_birth_argv(
    argv: list[str],
    birth_record_fd: int,
    executable_fd: int,
) -> list[str]:
    if not argv:
        raise RuntimeError("subprocess argv is empty")
    return [
        _owned_subprocess_launcher(),
        "-I",
        "-c",
        _SUBPROCESS_BIRTH_LAUNCHER,
        OWNED_SUBPROCESS_BIRTH_ARGUMENT,
        str(birth_record_fd),
        str(executable_fd),
        *argv,
    ]


def _owned_subprocess_launcher() -> str:
    if sys.platform.startswith("linux") and getattr(sys, "frozen", False) is True:
        return "/proc/self/exe"
    return sys.executable


def _read_subprocess_birth_record(birth_record: BinaryIO | None) -> int | None:
    if birth_record is None:
        return None
    try:
        descriptor = birth_record.fileno()
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > 128
        ):
            raise RuntimeError("subprocess birth record identity is invalid")
        payload = os.pread(descriptor, 129, 0)
    except (OSError, ValueError) as exc:
        raise RuntimeError("subprocess birth record is unavailable") from exc
    if not payload.endswith(b"\n"):
        return None
    fields = payload[:-1].split(b" ")
    if len(fields) != 3 or any(not field.isdigit() for field in fields):
        raise RuntimeError("subprocess birth record is malformed")
    process_id, process_group_id, session_id = (int(field) for field in fields)
    if (
        process_id <= 1
        or process_id != process_group_id
        or process_id != session_id
        or process_group_id == os.getpgrp()
    ):
        raise RuntimeError("subprocess birth record ownership is invalid")
    return process_id


def _discard_subprocess_birth_record(birth_record: BinaryIO | None) -> bool:
    if birth_record is None:
        return True
    try:
        birth_record.close()
    except BaseException:
        if birth_record.closed:
            return True
        _log_transport_failure(SshTransportErrorCode.START_FAILED)
        return False
    return True


def _run_subprocess(
    argv: list[str],
    timeout_seconds: float,
    *,
    trust_lease: AbstractContextManager[Path] | None = None,
    trust_ownership: _KnownHostsLeaseOwnership | None = None,
    env: dict[str, str] | None = None,
    agent_socket_source: SshAgentSocketSource | None = None,
    on_start: Callable[[_OwnedSubprocessAuthority], None] | None = None,
    on_finish: Callable[[_OwnedSubprocessAuthority], None] | None = None,
    cancel_event: threading.Event | None = None,
    stdin_fd: int | None = None,
) -> subprocess.CompletedProcess[str]:
    if trust_lease is not None and trust_ownership is not None:
        raise RuntimeError("known-host lease has multiple ownership sources")
    caller_ownership = _KnownHostsLeaseOwnership(trust_lease) if trust_lease is not None else None
    active_ownership = trust_ownership or caller_ownership
    try:
        authority = _OwnedSubprocessAuthority.spawn(
            argv,
            trust_ownership=active_ownership,
            env=env,
            agent_socket_source=agent_socket_source,
            stdin_fd=stdin_fd,
        )
        process = authority.process
        if process is None:
            raise RuntimeError("subprocess birth has not been observed")
        stdout = b""
        stderr = b""
        registered = False
        try:
            if on_start is not None:
                on_start(authority)
                registered = True
            authority.initialize_observer()
            assert process.stdout is not None
            assert process.stderr is not None
            assert authority.exit_observer is not None
            stdout, stderr = _capture_subprocess_output(
                process,
                argv=argv,
                timeout_seconds=timeout_seconds,
                process_group_id=authority.process_group_id,
                exit_observer=authority.exit_observer,
                on_group_cleanup_confirmed=authority.mark_group_cleanup_confirmed,
                cancel_event=cancel_event,
            )
        except BaseException:
            try:
                authority.cleanup()
            except BaseException:
                authority.retain()
                _log_transport_failure(SshTransportErrorCode.START_FAILED)
            raise
        finally:
            if registered and on_finish is not None:
                on_finish(authority)
            try:
                authority.close_observer()
            except BaseException:
                _log_transport_failure(SshTransportErrorCode.START_FAILED)
            for stream in (process.stdout, process.stderr):
                if stream is None:
                    continue
                try:
                    stream.close()
                except BaseException:
                    _log_transport_failure(SshTransportErrorCode.START_FAILED)
        if not authority.release_if_reaped():
            raise RuntimeError("subprocess exit could not be confirmed")
        encoding = locale.getpreferredencoding(False)
        return subprocess.CompletedProcess(
            argv,
            process.returncode,
            stdout=stdout.decode(encoding),
            stderr=stderr.decode(encoding),
        )
    finally:
        if caller_ownership is not None:
            caller_ownership.release_if_caller_owned()


def _run_streaming_subprocess(
    argv: list[str],
    timeout_seconds: float,
    stdin_fd: int,
    cancel_event: threading.Event | None,
    *,
    trust_ownership: _KnownHostsLeaseOwnership,
    env: dict[str, str] | None,
    agent_socket_source: SshAgentSocketSource | None,
    on_start: Callable[[_OwnedSubprocessAuthority], None],
    on_finish: Callable[[_OwnedSubprocessAuthority], None],
) -> subprocess.CompletedProcess[str]:
    return _run_subprocess(
        argv,
        timeout_seconds,
        trust_ownership=trust_ownership,
        env=env,
        agent_socket_source=agent_socket_source,
        on_start=on_start,
        on_finish=on_finish,
        cancel_event=cancel_event,
        stdin_fd=stdin_fd,
    )


def _require_closed_child_environment(env: dict[str, str] | None) -> dict[str, str]:
    environment = {} if env is None else dict(env)
    if environment:
        raise RuntimeError("subprocess environment is not closed")
    return environment


class _SubprocessCaptureLimitExceeded(RuntimeError):
    """Internal signal translated to an existing renderer-safe transport error."""


class _SubprocessCancelled(RuntimeError):
    """Internal activation cancellation after owned process-group convergence."""


def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None:
        if not isinstance(cancel_event, threading.Event):
            raise TypeError("cancellation authority is invalid")
        if cancel_event.is_set():
            raise _SubprocessCancelled


class _KnownHostsSpawnFailure(RuntimeError):
    """Internal trust setup failure without retaining the original exception."""


def _capture_subprocess_output(
    process: _OwnedSubprocessProcess,
    *,
    argv: list[str],
    timeout_seconds: float,
    process_group_id: int,
    exit_observer: _SubprocessExitObserver,
    on_group_cleanup_confirmed: Callable[[], None],
    cancel_event: threading.Event | None = None,
) -> tuple[bytes, bytes]:
    assert process.stdout is not None
    assert process.stderr is not None
    deadline = time.monotonic() + timeout_seconds
    captured_bytes = 0
    chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    leader_exited = False
    descendant_pipe_deadline: float | None = None
    pipe_close_deadline: float | None = None
    group_killed = False
    try:
        while selector.get_map():
            _raise_if_cancelled(cancel_event)
            now = time.monotonic()
            if not leader_exited and exit_observer.exited():
                leader_exited = True
                descendant_pipe_deadline = now + _SUBPROCESS_DESCENDANT_PIPE_GRACE_SECONDS
            if not leader_exited and now >= deadline:
                raise subprocess.TimeoutExpired(argv, timeout_seconds)
            if (
                leader_exited
                and not group_killed
                and descendant_pipe_deadline is not None
                and now >= descendant_pipe_deadline
            ):
                _signal_owned_process_group(
                    process,
                    process_group_id=process_group_id,
                    signal_number=signal.SIGKILL,
                )
                group_killed = True
                pipe_close_deadline = now + _SUBPROCESS_TERMINATE_GRACE_SECONDS
            if group_killed and pipe_close_deadline is not None and now >= pipe_close_deadline:
                break
            wake_at = (
                pipe_close_deadline
                if group_killed
                else descendant_pipe_deadline
                if leader_exited
                else deadline
            )
            assert wake_at is not None
            wait_seconds = min(
                _SUBPROCESS_STATUS_INTERVAL_SECONDS,
                max(0.0, wake_at - now),
            )
            events = selector.select(wait_seconds)
            for key, _mask in events:
                read_size = min(
                    _SUBPROCESS_CAPTURE_CHUNK_BYTES,
                    _MAX_SUBPROCESS_CAPTURE_BYTES - captured_bytes + 1,
                )
                chunk = os.read(key.fd, read_size)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                captured_bytes += len(chunk)
                if captured_bytes > _MAX_SUBPROCESS_CAPTURE_BYTES:
                    raise _SubprocessCaptureLimitExceeded
                chunks[key.data].append(chunk)
        if not leader_exited and exit_observer.supports_nonreaping_observation:
            if not exit_observer.wait_until(deadline):
                raise subprocess.TimeoutExpired(argv, timeout_seconds)
        if not group_killed:
            _signal_owned_process_group(
                process,
                process_group_id=process_group_id,
                signal_number=signal.SIGKILL,
            )
        _confirm_owned_process_group_terminated(
            process,
            process_group_id=process_group_id,
        )
        process.wait(timeout=_SUBPROCESS_TERMINATE_GRACE_SECONDS)
        _confirm_owned_process_group_disappeared(
            process_group_id=process_group_id,
        )
        on_group_cleanup_confirmed()
    finally:
        selector.close()
    return b"".join(chunks["stdout"]), b"".join(chunks["stderr"])


def _owned_process_group_id(process: _OwnedSubprocessProcess) -> int:
    process_group_id = process.pid
    if process_group_id <= 1 or process_group_id == os.getpgrp():
        raise RuntimeError("subprocess did not receive an independent process group")
    try:
        observed_group_id = os.getpgid(process.pid)
    except ProcessLookupError:
        if sys.platform != "darwin" or process.returncode is not None:
            raise
        identity = _read_ps_process_group_states().get(process.pid)
        if identity is None:
            raise RuntimeError("subprocess process group is unavailable") from None
        observed_group_id, state = identity
        if observed_group_id != process_group_id or state not in {"X", "Z"}:
            raise RuntimeError("subprocess process group is not an unreaped leader") from None
    if observed_group_id != process_group_id:
        raise RuntimeError("subprocess did not receive an independent process group")
    return process_group_id


class _SubprocessExitObserver:
    """Observe one leader exit while retaining its status for the owning Popen."""

    def __init__(self, process: _OwnedSubprocessProcess) -> None:
        self._process = process
        waitid = getattr(os, "waitid", None)
        self._waitid = (
            waitid
            if callable(waitid)
            and all(hasattr(os, name) for name in ("P_PID", "WEXITED", "WNOHANG", "WNOWAIT"))
            else None
        )
        self._kqueue: select.kqueue | None = None
        proc_stat_path = f"/proc/{process.pid}/stat"
        self._proc_stat_path = proc_stat_path if os.path.isfile(proc_stat_path) else None
        self._ps_process_group_id = process.pid if self._proc_stat_path is None else None
        self._observed = False
        if callable(self._waitid):
            return
        if hasattr(select, "kqueue"):
            queue: select.kqueue | None = None
            try:
                queue = select.kqueue()
                event = select.kevent(
                    process.pid,
                    filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_ONESHOT,
                    fflags=select.KQ_NOTE_EXIT,
                )
                queue.control([event], 0, 0)
            except BaseException as exc:
                if queue is not None:
                    queue.close()
                if not isinstance(exc, OSError):
                    raise
            else:
                self._kqueue = queue
        if self._ps_process_group_id is not None:
            try:
                self._observed = self._ps_leader_exited()
            except BaseException:
                if self._kqueue is not None:
                    self._kqueue.close()
                    self._kqueue = None
                raise
            if self._kqueue is not None and not self._observed:
                # The snapshot closes the attach-before-exit gap. Future exits
                # are delivered by the registered one-shot kqueue event.
                self._ps_process_group_id = None

    @property
    def supports_nonreaping_observation(self) -> bool:
        return (
            callable(self._waitid)
            or self._kqueue is not None
            or self._proc_stat_path is not None
            or self._ps_process_group_id is not None
        )

    def exited(self) -> bool:
        if self._observed or self._process.returncode is not None:
            self._observed = True
            return True
        if callable(self._waitid):
            result = self._waitid(
                os.P_PID,
                self._process.pid,
                os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
            self._observed = result is not None
            return self._observed
        if self._kqueue is not None:
            self._observed = bool(self._kqueue.control(None, 1, 0))
            if self._observed:
                return True
        if self._proc_stat_path is not None:
            try:
                with open(self._proc_stat_path, "rb") as stream:
                    stat_line = stream.read(4097)
            except FileNotFoundError:
                self._observed = True
                return True
            if len(stat_line) > 4096:
                raise RuntimeError("subprocess proc status exceeds the observation limit")
            fields = stat_line.rsplit(b")", 1)
            if len(fields) != 2:
                raise RuntimeError("subprocess proc status is malformed")
            status_fields = fields[1].split()
            if not status_fields:
                raise RuntimeError("subprocess proc status is malformed")
            self._observed = status_fields[0] in {b"Z", b"X"}
            return self._observed
        if self._ps_process_group_id is not None:
            self._observed = self._ps_leader_exited()
            return self._observed
        return False

    def _ps_leader_exited(self) -> bool:
        process_group_id = self._ps_process_group_id
        if process_group_id is None:
            raise RuntimeError("subprocess ps observer is unavailable")
        identity = _read_ps_process_group_states().get(self._process.pid)
        if identity is None:
            return True
        observed_group_id, state = identity
        if observed_group_id != process_group_id:
            raise RuntimeError("subprocess process-group leader identity changed")
        return state in {"X", "Z"}

    def wait_until(self, deadline: float) -> bool:
        while True:
            if self.exited():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.01, remaining))

    def close(self) -> None:
        if self._kqueue is not None:
            self._kqueue.close()
            self._kqueue = None


def _signal_owned_process_group(
    process: _OwnedSubprocessProcess,
    *,
    process_group_id: int,
    signal_number: int,
) -> None:
    if (
        process.pid != process_group_id
        or process_group_id <= 1
        or process_group_id == os.getpgrp()
    ):
        raise RuntimeError("subprocess process-group ownership is invalid")
    states = _observe_owned_process_group_states(
        process,
        process_group_id=process_group_id,
    )
    if not any(state not in {"X", "Z"} for state in states.values()):
        return
    try:
        os.killpg(process_group_id, signal_number)
    except ProcessLookupError:
        pass
    except PermissionError:
        states = _observe_owned_process_group_states(
            process,
            process_group_id=process_group_id,
        )
        if any(state not in {"X", "Z"} for state in states.values()):
            raise


def _confirm_owned_process_group_terminated(
    process: _OwnedSubprocessProcess,
    *,
    process_group_id: int,
) -> None:
    deadline = time.monotonic() + _SUBPROCESS_TERMINATE_GRACE_SECONDS
    while True:
        states = _observe_owned_process_group_states(
            process,
            process_group_id=process_group_id,
        )
        live_members = tuple(
            process_id for process_id, state in states.items() if state not in {"X", "Z"}
        )
        if not live_members:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("subprocess process-group termination could not be confirmed")
        _signal_owned_process_group(
            process,
            process_group_id=process_group_id,
            signal_number=signal.SIGKILL,
        )
        time.sleep(min(_SUBPROCESS_STATUS_INTERVAL_SECONDS, remaining))


def _confirm_owned_process_group_disappeared(*, process_group_id: int) -> None:
    if process_group_id <= 1 or process_group_id == os.getpgrp():
        raise RuntimeError("subprocess process-group ownership is invalid")
    deadline = time.monotonic() + _SUBPROCESS_TERMINATE_GRACE_SECONDS
    while True:
        if os.path.isdir("/proc"):
            states = _read_proc_process_group_states(process_group_id)
        else:
            states = {
                process_id: state
                for process_id, (group_id, state) in _read_ps_process_group_states().items()
                if group_id == process_group_id
            }
        if not states:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("subprocess process-group disappearance could not be confirmed")
        time.sleep(min(_SUBPROCESS_STATUS_INTERVAL_SECONDS, remaining))


def _observe_owned_process_group_states(
    process: _OwnedSubprocessProcess,
    *,
    process_group_id: int,
) -> dict[int, str]:
    if (
        process.pid != process_group_id
        or process_group_id <= 1
        or process_group_id == os.getpgrp()
    ):
        raise RuntimeError("subprocess process-group ownership is invalid")
    proc_stat_path = f"/proc/{process.pid}/stat"
    if os.path.isfile(proc_stat_path):
        states = _read_proc_process_group_states(process_group_id)
    else:
        states = _read_ps_process_group_states()
        states = {
            process_id: state
            for process_id, (group_id, state) in states.items()
            if group_id == process_group_id
        }
    if process.pid not in states:
        raise RuntimeError("subprocess process-group leader observation is unavailable")
    return states


def _read_proc_process_group_states(process_group_id: int) -> dict[int, str]:
    states: dict[int, str] = {}
    entries = 0
    try:
        proc_entries = os.scandir("/proc")
    except OSError as exc:
        raise RuntimeError("subprocess process-group observation is unavailable") from exc
    with proc_entries:
        for entry in proc_entries:
            if not entry.name.isascii() or not entry.name.isdecimal():
                continue
            entries += 1
            if entries > _MAX_PROCESS_GROUP_SCAN_ENTRIES:
                raise RuntimeError("subprocess process-group observation capacity exceeded")
            try:
                with open(f"/proc/{entry.name}/stat", "rb") as stream:
                    stat_line = stream.read(4097)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise RuntimeError("subprocess process-group observation failed") from exc
            if len(stat_line) > 4096:
                raise RuntimeError("subprocess process-group status exceeds the observation limit")
            fields = stat_line.rsplit(b")", 1)
            if len(fields) != 2:
                raise RuntimeError("subprocess process-group status is malformed")
            status_fields = fields[1].split()
            if len(status_fields) < 3:
                raise RuntimeError("subprocess process-group status is malformed")
            try:
                group_id = int(status_fields[2])
                state = status_fields[0].decode("ascii")
            except (UnicodeDecodeError, ValueError) as exc:
                raise RuntimeError("subprocess process-group status is malformed") from exc
            if group_id == process_group_id:
                states[int(entry.name)] = state
    return states


def _read_ps_process_group_states() -> dict[int, tuple[int, str]]:
    ps_path = next(
        (path for path in ("/bin/ps", "/usr/bin/ps") if os.path.isfile(path)),
        None,
    )
    if ps_path is None:
        raise RuntimeError("subprocess process-group observer is unavailable")
    try:
        completed = subprocess.run(
            [ps_path, "-axo", "pid=,pgid=,stat="],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_PROCESS_GROUP_OBSERVER_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("subprocess process-group observation failed") from exc
    if completed.returncode != 0:
        raise RuntimeError("subprocess process-group observation failed")
    output = completed.stdout
    if len(output) > _MAX_PROCESS_GROUP_STATUS_BYTES:
        raise RuntimeError("subprocess process-group observation capacity exceeded")
    states: dict[int, tuple[int, str]] = {}
    for index, line in enumerate(output.splitlines(), start=1):
        if index > _MAX_PROCESS_GROUP_SCAN_ENTRIES:
            raise RuntimeError("subprocess process-group observation capacity exceeded")
        fields = line.split()
        if len(fields) != 3:
            raise RuntimeError("subprocess process-group status is malformed")
        try:
            process_id = int(fields[0])
            group_id = int(fields[1])
            state = fields[2][:1].decode("ascii")
        except (UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError("subprocess process-group status is malformed") from exc
        states[process_id] = (group_id, state)
    return states


def _terminate_and_reap_subprocess(
    process: _OwnedSubprocessProcess,
    *,
    process_group_id: int,
    exit_observer: _SubprocessExitObserver | None,
    on_group_cleanup_confirmed: Callable[[], None],
) -> None:
    failure: BaseException | None = None
    try:
        _signal_owned_process_group(
            process,
            process_group_id=process_group_id,
            signal_number=signal.SIGTERM,
        )
    except BaseException as exc:
        failure = exc
    try:
        if exit_observer is not None:
            exit_observer.wait_until(time.monotonic() + _SUBPROCESS_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    except BaseException as exc:
        if failure is None:
            failure = exc
    try:
        _signal_owned_process_group(
            process,
            process_group_id=process_group_id,
            signal_number=signal.SIGKILL,
        )
    except BaseException as exc:
        if failure is None:
            failure = exc
    if failure is not None:
        raise failure
    _confirm_owned_process_group_terminated(
        process,
        process_group_id=process_group_id,
    )
    process.wait(timeout=_SUBPROCESS_TERMINATE_GRACE_SECONDS)
    _confirm_owned_process_group_disappeared(
        process_group_id=process_group_id,
    )
    on_group_cleanup_confirmed()


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


def _terminate_tunnel_child(
    child: _TunnelChild,
) -> tuple[bool, BaseException | None]:
    authority = child.authority
    if authority is None:
        process = child.process
        if process is None:
            return False, RuntimeError("SSH tunnel child ownership is incomplete")
        return _terminate_tunnel_process(process)
    failure: BaseException | None = None
    try:
        authority.cleanup()
    except BaseException as exc:
        failure = exc
    return authority.released, failure


def _tunnel_child_has_exited(child: _TunnelChild) -> bool:
    process = child.process
    if process is None:
        raise RuntimeError("SSH tunnel child ownership is incomplete")
    if child.authority is not None:
        return child.authority.leader_exited()
    return process.poll() is not None


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
    _retry_orphaned_subprocess_cleanup()
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


def _retry_orphaned_subprocess_cleanup() -> None:
    with _ORPHANED_SUBPROCESS_GUARD:
        authorities = tuple(
            authority for authority in _ORPHANED_SUBPROCESSES.values() if authority._retained
        )[:_MAX_SUBPROCESS_ORPHAN_RETRIES]
    for authority in authorities:
        try:
            authority.cleanup()
        except BaseException as exc:
            authority.retain()
            _log_transport_failure(SshTransportErrorCode.START_FAILED)
            if not isinstance(exc, Exception):
                raise


def _release_trust_lease(
    trust_lease: AbstractContextManager[Path] | None,
    *,
    retain_for_retry: bool = True,
) -> bool:
    if trust_lease is None:
        return True
    try:
        trust_lease.__exit__(None, None, None)
    except BaseException:
        if retain_for_retry:
            with _ORPHANED_TUNNEL_GUARD:
                _ORPHANED_TRUST_LEASES[id(trust_lease)] = trust_lease
        _log_transport_failure(SshTransportErrorCode.HOST_KEY_VERIFICATION_FAILED)
        return False
    else:
        with _ORPHANED_TUNNEL_GUARD:
            _ORPHANED_TRUST_LEASES.pop(id(trust_lease), None)
        return True


def _log_transport_failure(
    code: SshTransportErrorCode,
) -> None:
    logger.warning(
        "ssh_transport_failure code=%s diagnostic_id=%s",
        code.value,
        secrets.token_hex(12),
    )


def _stage_remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        _log_transport_failure(SshTransportErrorCode.TIMEOUT)
        raise SshTransportError(SshTransportErrorCode.TIMEOUT)
    return remaining


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
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if tunnel._leader_exited():
            raise RuntimeError("SSH tunnel exited before it became ready.")
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


def _validate_supported_auth(
    profile: RemoteProfileConfig,
) -> None:
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


def _core_runtime_proxy_env(profile: RemoteProfileConfig) -> dict[str, str]:
    allowed = {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
    return {key: value for key, value in profile.proxy.to_env().items() if key in allowed}


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
