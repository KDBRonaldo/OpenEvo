from __future__ import annotations

import argparse
import ctypes
from dataclasses import dataclass, field
from enum import StrEnum
import errno
import fcntl
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import select
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Protocol, Sequence

from pydantic import SecretStr, ValidationError

from openevo import __version__
from openevo.backend.contracts.v2 import models as core_v2_models
from openevo.backend.contracts.v2.provider import RELEASE_DAEMON_FEATURE_FLAGS_V2
from openevo.backend.contracts.v2.snapshots import (
    events_schema_sha256 as core_v2_events_schema_sha256,
    openapi_sha256 as core_v2_openapi_sha256,
)

from openevo.backend.runtime_identity import (
    CoreReleaseIdentity,
    HostServiceRoot,
    RuntimeIdentityError,
    canonical_json_bytes,
    compute_release_identity,
    default_core_service_root,
    load_bounded_json,
    load_or_create_core_bearer_token,
    release_runtime_contract_sha256,
    require_host_global_service_root,
    rotate_core_bearer_token,
)
from openevo.evolution.framework import (
    load_framework_distribution_lock,
    load_verified_framework_registry,
)


_BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
_BOOT_ID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_MAX_READY_BYTES = 4096
_MAX_HTTP_BYTES = 64 * 1024
_LOCAL_HTTP_ATTEMPT_SECONDS = 1.0
_TUNNEL_HTTP_ATTEMPT_SECONDS = 5.0
_LEDGER_NAME = "service.json"
_READY_NAME = "ready.json"
_PENDING_NAME = "pending.json"
_SPAWN_LOCK_NAME = "spawn.lock"
_SERVICE_GENERATION_HEADER = "X-OpenEvo-Core-Generation"
_RELEASE_IDENTITY_HEADER = "X-OpenEvo-Core-Release-Identity"
_PROCESS_GROUP_LIFECYCLE_COMPATIBILITY = 3
_PRODUCTION_V2_LIFECYCLE_COMPATIBILITY = 10
V2_DAEMON_LIFECYCLE_COMPATIBILITY = 13
_ONEFILE_LAUNCHER_CLEANUP_SECONDS = 10.0
_ORPHANED_SERVICE_CHILDREN_GUARD = threading.Lock()
_ORPHANED_SERVICE_CHILDREN: dict[int, subprocess.Popen[bytes]] = {}
_PIDFD_SYSCALL_NUMBERS = {
    "aarch64": (434, 424),
    "arm64": (434, 424),
    "amd64": (434, 424),
    "x86_64": (434, 424),
}


class CoreServiceErrorCode(StrEnum):
    INVALID_ROOT = "core_service_root_invalid"
    IDENTITY_MISMATCH = "core_service_identity_mismatch"
    PREDECESSOR_MISMATCH = "core_service_predecessor_mismatch"
    UPDATE_REQUIRED = "core_service_update_required"
    PORT_UNAVAILABLE = "core_service_port_unavailable"
    START_FAILED = "core_service_start_failed"
    STATUS_INVALID = "core_service_status_invalid"
    DEADLINE_EXCEEDED = "core_service_deadline_exceeded"
    STATE_INVALID = "core_service_state_invalid"
    INSTALL_FAILED = "core_service_install_failed"
    VERIFICATION_FAILED = "core_service_verification_failed"


class CoreServiceError(RuntimeError):
    def __init__(
        self,
        code: CoreServiceErrorCode,
        message: str,
        *,
        retryable: bool,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    boot_id: str
    start_time_ticks: int


@dataclass(frozen=True, slots=True)
class _ServiceProcessGroup:
    launcher: ProcessIdentity
    application: ProcessIdentity

    @property
    def identities(self) -> tuple[ProcessIdentity, ...]:
        if self.launcher == self.application:
            return (self.launcher,)
        return (self.application, self.launcher)


@dataclass(frozen=True, slots=True)
class LockIdentity:
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class CoreServiceAttachment:
    port: int
    release_identity: str
    registry_digest: str
    source_commit: str
    generation: str
    status_proof: str
    attached: bool
    _bearer: SecretStr = field(repr=False, compare=False)
    bundle_sha256: str | None = None
    canonical_manifest_sha256: str | None = None
    lifecycle_compatibility: int | None = None

    @property
    def bearer_token(self) -> str:
        return self._bearer.get_secret_value()


@dataclass(frozen=True, slots=True)
class CoreServicePredecessor:
    state: str
    generation: str | None
    release_identity: str | None
    bundle_sha256: str | None
    canonical_manifest_sha256: str | None
    lifecycle_compatibility: int | None

    def __post_init__(self) -> None:
        absent = (
            self.state == "absent"
            and self.generation is None
            and self.release_identity is None
            and self.bundle_sha256 is None
            and self.canonical_manifest_sha256 is None
            and self.lifecycle_compatibility is None
        )
        legacy = (
            self.state == "legacy"
            and isinstance(self.generation, str)
            and re.fullmatch(r"[0-9a-f]{32}", self.generation) is not None
            and isinstance(self.release_identity, str)
            and re.fullmatch(r"[0-9a-f]{64}", self.release_identity) is not None
            and self.bundle_sha256 is None
            and self.canonical_manifest_sha256 is None
            and self.lifecycle_compatibility == 1
        )
        running = (
            self.state == "running"
            and isinstance(self.generation, str)
            and re.fullmatch(r"[0-9a-f]{32}", self.generation) is not None
            and isinstance(self.release_identity, str)
            and re.fullmatch(r"[0-9a-f]{64}", self.release_identity) is not None
            and isinstance(self.bundle_sha256, str)
            and re.fullmatch(r"[0-9a-f]{64}", self.bundle_sha256) is not None
            and isinstance(self.canonical_manifest_sha256, str)
            and re.fullmatch(r"[0-9a-f]{64}", self.canonical_manifest_sha256) is not None
            and type(self.lifecycle_compatibility) is int
            and self.lifecycle_compatibility >= 2
        )
        if not absent and not legacy and not running:
            raise ValueError("Core service predecessor identity is invalid.")

    @classmethod
    def absent(cls) -> CoreServicePredecessor:
        return cls(
            state="absent",
            generation=None,
            release_identity=None,
            bundle_sha256=None,
            canonical_manifest_sha256=None,
            lifecycle_compatibility=None,
        )

    @classmethod
    def legacy(
        cls,
        *,
        generation: str,
        release_identity: str,
    ) -> CoreServicePredecessor:
        return cls(
            state="legacy",
            generation=generation,
            release_identity=release_identity,
            bundle_sha256=None,
            canonical_manifest_sha256=None,
            lifecycle_compatibility=1,
        )

    @classmethod
    def running(
        cls,
        *,
        generation: str,
        release_identity: str,
        bundle_sha256: str | None = None,
        canonical_manifest_sha256: str | None = None,
        lifecycle_compatibility: int | None = None,
    ) -> CoreServicePredecessor:
        if (
            bundle_sha256 is None
            and canonical_manifest_sha256 is None
            and lifecycle_compatibility is None
        ):
            return cls.legacy(
                generation=generation,
                release_identity=release_identity,
            )
        return cls(
            state="running",
            generation=generation,
            release_identity=release_identity,
            bundle_sha256=bundle_sha256,
            canonical_manifest_sha256=canonical_manifest_sha256,
            lifecycle_compatibility=lifecycle_compatibility,
        )


@dataclass(frozen=True, slots=True)
class CoreDaemonBundleIdentity:
    bundle_sha256: str
    canonical_manifest_sha256: str
    lifecycle_compatibility: int

    def __post_init__(self) -> None:
        if (
            re.fullmatch(r"[0-9a-f]{64}", self.bundle_sha256) is None
            or re.fullmatch(r"[0-9a-f]{64}", self.canonical_manifest_sha256) is None
            or type(self.lifecycle_compatibility) is not int
            or self.lifecycle_compatibility < 2
        ):
            raise ValueError("Core Daemon bundle identity is invalid.")


class CoreServiceEndpoint(Protocol):
    def open_verified_socket(self, *, timeout_seconds: float) -> socket.socket: ...

    def verify_authority(self) -> None: ...


class _EndpointHTTPConnection(http.client.HTTPConnection):
    def __init__(
        self,
        endpoint: CoreServiceEndpoint,
        *,
        timeout: float,
    ) -> None:
        super().__init__("openevo-core.local", timeout=timeout)
        self._endpoint = endpoint

    def connect(self) -> None:
        timeout = self.timeout if isinstance(self.timeout, (int, float)) else 1.0
        self.sock = self._endpoint.open_verified_socket(timeout_seconds=timeout)


def _linux_syscall(number: int, *arguments: object) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    syscall = libc.syscall
    syscall.restype = ctypes.c_long
    ctypes.set_errno(0)
    result = syscall(ctypes.c_long(number), *arguments)
    if result == -1:
        error = ctypes.get_errno() or errno.EIO
        raise OSError(error, os.strerror(error))
    return int(result)


def _pidfd_syscall_numbers() -> tuple[int, int]:
    try:
        machine = os.uname().machine.lower()
    except (AttributeError, OSError) as exc:
        raise OSError(errno.ENOSYS, "Linux architecture is unavailable") from exc
    numbers = _PIDFD_SYSCALL_NUMBERS.get(machine)
    if numbers is None:
        raise OSError(errno.ENOSYS, "Linux pidfd syscall ABI is unsupported")
    return numbers


def _pidfd_open_via_syscall(pid: int, flags: int) -> int:
    open_number, _ = _pidfd_syscall_numbers()
    return _linux_syscall(open_number, ctypes.c_int(pid), ctypes.c_uint(flags))


def _pidfd_send_signal_via_syscall(pid_fd: int, sig: int, flags: int) -> None:
    _, send_signal_number = _pidfd_syscall_numbers()
    _linux_syscall(
        send_signal_number,
        ctypes.c_int(pid_fd),
        ctypes.c_int(sig),
        ctypes.c_void_p(),
        ctypes.c_uint(flags),
    )


def _pidfd_open(pid: int, flags: int) -> int:
    native = getattr(os, "pidfd_open", None)
    if callable(native):
        return native(pid, flags)
    return _pidfd_open_via_syscall(pid, flags)


def _pidfd_send_signal(pid_fd: int, sig: int, flags: int) -> None:
    native = getattr(signal, "pidfd_send_signal", None)
    if callable(native):
        native(pid_fd, sig, None, flags)
        return
    _pidfd_send_signal_via_syscall(pid_fd, sig, flags)


class LinuxProcessController:
    def __init__(self) -> None:
        if sys.platform != "linux":
            raise CoreServiceError(
                CoreServiceErrorCode.START_FAILED,
                "Core service supervision requires Linux pidfd support.",
                retryable=False,
            )
        try:
            boot_id = _BOOT_ID_PATH.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as exc:
            raise CoreServiceError(
                CoreServiceErrorCode.START_FAILED,
                "Core service process identity is unavailable.",
                retryable=False,
            ) from exc
        if _BOOT_ID_PATTERN.fullmatch(boot_id) is None:
            raise CoreServiceError(
                CoreServiceErrorCode.START_FAILED,
                "Core service process identity is invalid.",
                retryable=False,
            )
        self.boot_id = boot_id
        try:
            probe = _pidfd_open(os.getpid(), 0)
        except OSError as exc:
            raise CoreServiceError(
                CoreServiceErrorCode.START_FAILED,
                "Core service supervision requires Linux pidfd support.",
                retryable=False,
            ) from exc
        os.close(probe)

    def capture(self, pid: int) -> ProcessIdentity:
        if type(pid) is not int or pid <= 0:
            raise CoreServiceError(
                CoreServiceErrorCode.STATE_INVALID,
                "Core service process state is invalid.",
                retryable=False,
            )
        try:
            payload = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii")
        except FileNotFoundError as exc:
            raise ProcessLookupError(pid) from exc
        except (OSError, UnicodeError) as exc:
            raise CoreServiceError(
                CoreServiceErrorCode.STATE_INVALID,
                "Core service process identity cannot be read.",
                retryable=True,
            ) from exc
        end = payload.rfind(")")
        fields = payload[end + 1 :].split() if end >= 0 else []
        try:
            start_time = int(fields[19])
        except (IndexError, ValueError) as exc:
            raise CoreServiceError(
                CoreServiceErrorCode.STATE_INVALID,
                "Core service process identity is malformed.",
                retryable=False,
            ) from exc
        if start_time <= 0:
            raise CoreServiceError(
                CoreServiceErrorCode.STATE_INVALID,
                "Core service process identity is malformed.",
                retryable=False,
            )
        return ProcessIdentity(pid=pid, boot_id=self.boot_id, start_time_ticks=start_time)

    def is_alive(self, identity: ProcessIdentity) -> bool:
        try:
            current = self.capture(identity.pid)
            state_payload = (Path("/proc") / str(identity.pid) / "stat").read_text(
                encoding="ascii"
            )
            end = state_payload.rfind(")")
            state = state_payload[end + 1 :].split()[0]
        except (ProcessLookupError, FileNotFoundError):
            return False
        return current == identity and state not in {"X", "Z"}

    def owns_service_process(
        self,
        launcher: ProcessIdentity,
        claimed: ProcessIdentity,
    ) -> bool:
        """Verify the source or PyInstaller onefile service process topology."""

        if launcher.boot_id != self.boot_id or claimed.boot_id != self.boot_id:
            return False
        if launcher == claimed:
            return self.is_alive(launcher)
        try:
            launcher_before = self.capture(launcher.pid)
            claimed_before = self.capture(claimed.pid)
            payload = (Path("/proc") / str(claimed.pid) / "stat").read_text(encoding="ascii")
            end = payload.rfind(")")
            fields = payload[end + 1 :].split() if end >= 0 else []
            parent_pid = int(fields[1])
            process_group_id = int(fields[2])
            session_id = int(fields[3])
            launcher_after = self.capture(launcher.pid)
            claimed_after = self.capture(claimed.pid)
        except (ProcessLookupError, FileNotFoundError):
            return False
        except (IndexError, OSError, UnicodeError, ValueError) as exc:
            raise CoreServiceError(
                CoreServiceErrorCode.STATE_INVALID,
                "Core service child topology cannot be verified.",
                retryable=False,
            ) from exc
        return (
            launcher_before == launcher_after == launcher
            and claimed_before == claimed_after == claimed
            and parent_pid == launcher.pid
            and process_group_id == launcher.pid
            and session_id == launcher.pid
            and self.is_alive(launcher)
            and self.is_alive(claimed)
        )

    def wait_for_exit(self, identity: ProcessIdentity, *, deadline: float) -> bool:
        try:
            pid_fd = _pidfd_open(identity.pid, 0)
        except ProcessLookupError:
            return True
        try:
            try:
                if self.capture(identity.pid) != identity:
                    return True
            except ProcessLookupError:
                return True
            poller = select.poll()
            poller.register(pid_fd, select.POLLIN | select.POLLHUP | select.POLLERR)
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            if poller.poll(remaining_ms):
                return True
            return not self.is_alive(identity)
        finally:
            os.close(pid_fd)

    def find_lock_holders(self, lock: LockIdentity) -> tuple[ProcessIdentity, ...]:
        holders: dict[int, ProcessIdentity] = {}
        try:
            process_entries = os.scandir("/proc")
        except OSError as exc:
            raise CoreServiceError(
                CoreServiceErrorCode.STATE_INVALID,
                "Core service process inventory is unavailable.",
                retryable=True,
            ) from exc
        with process_entries:
            for process_entry in process_entries:
                if not process_entry.name.isdecimal():
                    continue
                pid = int(process_entry.name)
                if pid == os.getpid():
                    continue
                try:
                    fd_entries = os.scandir(f"/proc/{pid}/fd")
                except (FileNotFoundError, PermissionError, ProcessLookupError):
                    continue
                except OSError as exc:
                    raise CoreServiceError(
                        CoreServiceErrorCode.STATE_INVALID,
                        "Core service process inventory cannot be inspected.",
                        retryable=True,
                    ) from exc
                found = False
                with fd_entries:
                    for fd_entry in fd_entries:
                        try:
                            metadata = os.stat(fd_entry.path)
                        except (FileNotFoundError, PermissionError, ProcessLookupError):
                            continue
                        if (metadata.st_dev, metadata.st_ino) == (
                            lock.device,
                            lock.inode,
                        ):
                            found = True
                            break
                if found:
                    try:
                        identity = self.capture(pid)
                    except ProcessLookupError:
                        continue
                    if self.is_alive(identity):
                        holders[pid] = identity
        return tuple(holders[pid] for pid in sorted(holders))

    def terminate(self, identity: ProcessIdentity, *, deadline: float) -> None:
        try:
            pid_fd = _pidfd_open(identity.pid, 0)
        except ProcessLookupError:
            return
        try:
            try:
                if self.capture(identity.pid) != identity:
                    return
            except ProcessLookupError:
                return
            try:
                _pidfd_send_signal(pid_fd, signal.SIGTERM, 0)
            except OSError as exc:
                if exc.errno == errno.ESRCH:
                    return
                raise
            poller = select.poll()
            poller.register(pid_fd, select.POLLIN | select.POLLHUP | select.POLLERR)
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            if not poller.poll(remaining_ms):
                raise CoreServiceError(
                    CoreServiceErrorCode.DEADLINE_EXCEEDED,
                    "Core service replacement did not stop before the deadline.",
                    retryable=True,
                )
        finally:
            os.close(pid_fd)


def _frozen_service_environment(
    *,
    reuse_parent_extraction_for_bounded_smoke: bool,
) -> dict[str, str] | None:
    """Select independent long-lived or parent-owned bounded onefile state."""

    if not (getattr(sys, "frozen", False) and isinstance(getattr(sys, "_MEIPASS", None), str)):
        return None
    environment = dict(os.environ)
    if reuse_parent_extraction_for_bounded_smoke:
        environment.pop("PYINSTALLER_RESET_ENVIRONMENT", None)
        return environment
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return environment


def ensure_core_service(
    *,
    service_root: str | Path,
    framework_lock: str | Path,
    source_commit: str,
    port: int = 0,
    deadline_seconds: float = 45.0,
    replace_mismatched: bool = False,
    expected_predecessor: CoreServicePredecessor | None = None,
    daemon_bundle_identity: CoreDaemonBundleIdentity | None = None,
    process_controller: LinuxProcessController | None = None,
    _fault_injector: Callable[[str, int], None] | None = None,
    _bootstrap_lock_fd: int | None = None,
    _reuse_frozen_extraction_for_bounded_smoke: bool = False,
) -> CoreServiceAttachment:
    _retry_orphaned_service_children()
    if (
        not 0 <= port <= 65535
        or deadline_seconds <= 0
        or deadline_seconds > 300
        or (
            expected_predecessor is not None
            and not isinstance(expected_predecessor, CoreServicePredecessor)
        )
        or (
            daemon_bundle_identity is not None
            and (
                not isinstance(daemon_bundle_identity, CoreDaemonBundleIdentity)
                or daemon_bundle_identity.lifecycle_compatibility
                < _PROCESS_GROUP_LIFECYCLE_COMPATIBILITY
            )
        )
        or type(_reuse_frozen_extraction_for_bounded_smoke) is not bool
        or (
            _reuse_frozen_extraction_for_bounded_smoke
            and (
                daemon_bundle_identity is not None
                or not getattr(sys, "frozen", False)
                or not isinstance(getattr(sys, "_MEIPASS", None), str)
            )
        )
    ):
        raise CoreServiceError(
            CoreServiceErrorCode.START_FAILED,
            "Core service startup settings are invalid.",
            retryable=False,
        )
    deadline = time.monotonic() + deadline_seconds
    try:
        require_host_global_service_root(service_root)
        root = HostServiceRoot(service_root)
    except (OSError, RuntimeIdentityError) as exc:
        raise CoreServiceError(
            CoreServiceErrorCode.INVALID_ROOT,
            "Core service root failed private ownership validation.",
            retryable=False,
        ) from exc
    with root:
        root.ensure_directory("state")
        controller = process_controller or LinuxProcessController()
        bootstrap_lock_fd = (
            root.open_lock("bootstrap.lock") if _bootstrap_lock_fd is None else _bootstrap_lock_fd
        )
        try:
            if _bootstrap_lock_fd is None:
                _flock_until(bootstrap_lock_fd, deadline)
            else:
                _require_inherited_lock(root, "bootstrap.lock", bootstrap_lock_fd)
            lifecycle_lock_fd = root.open_lock("lifecycle.lock")
            try:
                _flock_until(lifecycle_lock_fd, deadline)
                registry = load_verified_framework_registry(framework_lock)
                release = compute_release_identity(
                    framework_lock=framework_lock,
                    registry=registry,
                    source_commit=source_commit,
                )
                return _ensure_locked(
                    root=root,
                    framework_lock=Path(framework_lock),
                    release=release,
                    port=port,
                    deadline=deadline,
                    replace_mismatched=replace_mismatched,
                    expected_predecessor=expected_predecessor,
                    daemon_bundle_identity=daemon_bundle_identity,
                    controller=controller,
                    fault_injector=_fault_injector,
                    reuse_frozen_extraction_for_bounded_smoke=(
                        _reuse_frozen_extraction_for_bounded_smoke
                    ),
                )
            finally:
                os.close(lifecycle_lock_fd)
        finally:
            os.close(bootstrap_lock_fd)


def observe_core_service_predecessor(
    *,
    service_root: str | Path,
    deadline_seconds: float = 15.0,
    process_controller: LinuxProcessController | None = None,
) -> CoreServicePredecessor:
    if not 0 < deadline_seconds <= 300:
        raise CoreServiceError(
            CoreServiceErrorCode.START_FAILED,
            "Core service observation settings are invalid.",
            retryable=False,
        )
    deadline = time.monotonic() + deadline_seconds
    try:
        require_host_global_service_root(service_root)
        root = HostServiceRoot(service_root)
    except (OSError, RuntimeIdentityError) as exc:
        raise CoreServiceError(
            CoreServiceErrorCode.INVALID_ROOT,
            "Core service root failed private ownership validation.",
            retryable=False,
        ) from exc
    with root:
        root.ensure_directory("state")
        controller = process_controller or LinuxProcessController()
        bootstrap_lock_fd = root.open_lock("bootstrap.lock")
        try:
            _flock_until(bootstrap_lock_fd, deadline)
            lifecycle_lock_fd = root.open_lock("lifecycle.lock")
            try:
                _flock_until(lifecycle_lock_fd, deadline)
                value = root.read_optional_json(_LEDGER_NAME)
                if value is None:
                    _recover_pending(root, controller=controller, deadline=deadline)
                    return CoreServicePredecessor.absent()
                state = _require_service_state(value)
                if state.get("state") == "stopped":
                    return CoreServicePredecessor.absent()
                ledger = state
                process_group = _service_process_group_from_ledger(ledger)
                if not _service_process_group_is_alive(process_group, controller):
                    _terminate_service_process_group(
                        process_group,
                        controller,
                        deadline=deadline,
                    )
                    if _is_exact_daemon_ledger(ledger):
                        root.atomic_write_json(
                            _LEDGER_NAME,
                            _floor_from_ledger(ledger),
                            replace=True,
                        )
                    else:
                        root.unlink_regular(_LEDGER_NAME)
                    root.unlink_regular(_READY_NAME)
                    root.unlink_regular(_PENDING_NAME)
                    return CoreServicePredecessor.absent()
                bearer = load_or_create_core_bearer_token(root)
                proof = _authenticated_status_proof(
                    port=ledger["port"],
                    bearer=bearer,
                    release=_release_from_ledger(ledger),
                    generation=ledger["generation"],
                    deadline=deadline,
                    require_production_v2=_ledger_requires_production_v2(ledger),
                )
                _verify_ready_ledger(root, ledger, proof)
                return _predecessor_from_ledger(ledger)
            finally:
                os.close(lifecycle_lock_fd)
        finally:
            os.close(bootstrap_lock_fd)


def inspect_core_service(
    *,
    service_root: str | Path,
    process_controller: LinuxProcessController | None = None,
) -> CoreServiceAttachment:
    try:
        require_host_global_service_root(service_root)
        root = HostServiceRoot(service_root, create=False)
    except (OSError, RuntimeIdentityError) as exc:
        raise CoreServiceError(
            CoreServiceErrorCode.INVALID_ROOT,
            "Core service root failed private ownership validation.",
            retryable=False,
        ) from exc
    with root:
        controller = process_controller or LinuxProcessController()
        ledger = _read_ledger(root)
        process_group = _service_process_group_from_ledger(ledger)
        if not _service_process_group_is_alive(process_group, controller):
            raise CoreServiceError(
                CoreServiceErrorCode.STATUS_INVALID,
                "Core service is not running.",
                retryable=True,
            )
        bearer = load_or_create_core_bearer_token(root)
        release = _release_from_ledger(ledger)
        proof = _authenticated_status_proof(
            port=_required_int(ledger, "port", minimum=1, maximum=65535),
            bearer=bearer,
            release=release,
            generation=ledger["generation"],
            deadline=time.monotonic() + 5.0,
            require_production_v2=_ledger_requires_production_v2(ledger),
        )
        return _attachment_from_ledger(
            ledger,
            bearer="",
            status_proof=proof,
            attached=True,
        )


def stop_core_service(
    *,
    service_root: str | Path,
    deadline_seconds: float = 15.0,
    process_controller: LinuxProcessController | None = None,
    preserve_compatibility_floor: bool = False,
) -> None:
    if not 0 < deadline_seconds <= 300 or type(preserve_compatibility_floor) is not bool:
        raise CoreServiceError(
            CoreServiceErrorCode.START_FAILED,
            "Core service stop settings are invalid.",
            retryable=False,
        )
    deadline = time.monotonic() + deadline_seconds
    try:
        require_host_global_service_root(service_root)
        root = HostServiceRoot(service_root, create=False)
    except (OSError, RuntimeIdentityError) as exc:
        raise CoreServiceError(
            CoreServiceErrorCode.INVALID_ROOT,
            "Core service root failed private ownership validation.",
            retryable=False,
        ) from exc
    with root:
        controller = process_controller or LinuxProcessController()
        bootstrap_lock_fd = root.open_lock("bootstrap.lock")
        try:
            _flock_until(bootstrap_lock_fd, deadline)
            lifecycle_lock_fd = root.open_lock("lifecycle.lock")
            try:
                _flock_until(lifecycle_lock_fd, deadline)
                ledger = root.read_optional_json(_LEDGER_NAME)
                if ledger is not None:
                    state = _require_service_state(ledger)
                    if state.get("state", "running") == "running":
                        _terminate_service_process_group(
                            _service_process_group_from_ledger(state),
                            controller,
                            deadline=deadline,
                        )
                        if preserve_compatibility_floor or _is_exact_daemon_ledger(state):
                            root.atomic_write_json(
                                _LEDGER_NAME,
                                _floor_from_ledger(state),
                                replace=True,
                            )
                        else:
                            root.unlink_regular(_LEDGER_NAME)
                else:
                    _recover_pending(root, controller=controller, deadline=deadline)
                root.unlink_regular(_READY_NAME)
                root.unlink_regular(_PENDING_NAME)
            finally:
                os.close(lifecycle_lock_fd)
        finally:
            os.close(bootstrap_lock_fd)


def stop_core_service_if_generation(
    *,
    service_root: str | Path,
    expected_generation: str,
    expected_release_identity: str,
    deadline_seconds: float = 15.0,
    process_controller: LinuxProcessController | None = None,
) -> bool:
    """Stop only the exact service generation named by a prior attachment."""

    if (
        re.fullmatch(r"[0-9a-f]{32}", expected_generation) is None
        or re.fullmatch(r"[0-9a-f]{64}", expected_release_identity) is None
        or not 0 < deadline_seconds <= 300
    ):
        raise CoreServiceError(
            CoreServiceErrorCode.START_FAILED,
            "Core service conditional stop settings are invalid.",
            retryable=False,
        )
    deadline = time.monotonic() + deadline_seconds
    try:
        require_host_global_service_root(service_root)
        root = HostServiceRoot(service_root, create=False)
    except (OSError, RuntimeIdentityError) as exc:
        raise CoreServiceError(
            CoreServiceErrorCode.INVALID_ROOT,
            "Core service root failed private ownership validation.",
            retryable=False,
        ) from exc
    with root:
        controller = process_controller or LinuxProcessController()
        bootstrap_lock_fd = root.open_lock("bootstrap.lock")
        try:
            _flock_until(bootstrap_lock_fd, deadline)
            lifecycle_lock_fd = root.open_lock("lifecycle.lock")
            try:
                _flock_until(lifecycle_lock_fd, deadline)
                value = root.read_optional_json(_LEDGER_NAME)
                if value is None:
                    return False
                state = _require_service_state(value)
                if state.get("state") == "stopped":
                    return False
                ledger = state
                if (
                    ledger["generation"] != expected_generation
                    or ledger["release_identity"] != expected_release_identity
                ):
                    return False
                _terminate_service_process_group(
                    _service_process_group_from_ledger(ledger),
                    controller,
                    deadline=deadline,
                )
                if _is_exact_daemon_ledger(ledger):
                    root.atomic_write_json(
                        _LEDGER_NAME,
                        _floor_from_ledger(ledger),
                        replace=True,
                    )
                else:
                    root.unlink_regular(_LEDGER_NAME)
                root.unlink_regular(_READY_NAME)
                root.unlink_regular(_PENDING_NAME)
                return True
            finally:
                os.close(lifecycle_lock_fd)
        finally:
            os.close(bootstrap_lock_fd)


def bootstrap_core_service(
    *,
    service_root: str | Path,
    wheel_path: str | Path,
    framework_lock: str | Path,
    source_commit: str,
    attachment_name: str,
    install_generation: str,
    port: int = 0,
    deadline_seconds: float = 90.0,
    replace_mismatched: bool = False,
) -> None:
    if (
        not Path(wheel_path).is_absolute()
        or not Path(framework_lock).is_absolute()
        or re.fullmatch(r"[0-9a-f]{40}", source_commit) is None
        or re.fullmatch(r"[0-9a-f]{32}", install_generation) is None
        or not 0 <= port <= 65535
        or not 1 <= deadline_seconds <= 300
    ):
        raise CoreServiceError(
            CoreServiceErrorCode.START_FAILED,
            "Core bootstrap settings are invalid.",
            retryable=False,
        )
    deadline = time.monotonic() + deadline_seconds
    _validate_attachment_name(attachment_name)
    try:
        require_host_global_service_root(service_root)
        root = HostServiceRoot(service_root)
    except (OSError, RuntimeIdentityError) as exc:
        raise CoreServiceError(
            CoreServiceErrorCode.INVALID_ROOT,
            "Core service root failed private ownership validation.",
            retryable=False,
        ) from exc
    with root:
        bootstrap_lock_fd = root.open_lock("bootstrap.lock")
        try:
            _flock_until(bootstrap_lock_fd, deadline)
            root.unlink_regular(attachment_name)
            _verify_generation_install(
                service_root=Path(service_root),
                wheel_path=Path(wheel_path),
                framework_lock=Path(framework_lock),
                source_commit=source_commit,
                install_generation=install_generation,
            )
            ensure_argv = [
                sys.executable,
                "-I",
                "-m",
                "openevo.backend.service",
                "ensure",
                "--service-root",
                str(service_root),
                "--framework-lock",
                str(framework_lock),
                "--source-commit",
                source_commit,
                "--port",
                str(port),
                "--deadline-seconds",
                str(max(1.0, deadline - time.monotonic())),
                "--attachment-name",
                attachment_name,
                "--bootstrap-lock-fd",
                str(bootstrap_lock_fd),
            ]
            if replace_mismatched:
                ensure_argv.append("--replace-mismatched")
            if (
                _run_private_command(
                    ensure_argv,
                    deadline=deadline,
                    pass_fds=(bootstrap_lock_fd,),
                )
                != 0
            ):
                raise CoreServiceError(
                    CoreServiceErrorCode.START_FAILED,
                    "Core Control could not be attached or started.",
                    retryable=True,
                )
        finally:
            os.close(bootstrap_lock_fd)


def _verify_generation_install(
    *,
    service_root: Path,
    wheel_path: Path,
    framework_lock: Path,
    source_commit: str,
    install_generation: str,
) -> None:
    expected_root = service_root / "releases" / install_generation
    executable = Path(sys.executable)
    module_path = Path(__file__)
    try:
        root_metadata = os.lstat(expected_root)
        executable_metadata = os.lstat(executable)
        _lock, locked_wheel = load_framework_distribution_lock(framework_lock)
        wheel_matches = os.path.samefile(wheel_path, locked_wheel)
    except (OSError, ValueError) as exc:
        raise CoreServiceError(
            CoreServiceErrorCode.VERIFICATION_FAILED,
            "The isolated Core generation could not be verified.",
            retryable=False,
        ) from exc
    if (
        re.fullmatch(r"[0-9a-f]{40}", source_commit) is None
        or re.fullmatch(r"[0-9a-f]{32}", install_generation) is None
        or Path(sys.prefix) != expected_root
        or executable.parent != expected_root / "bin"
        or not module_path.is_relative_to(expected_root)
        or not stat_is_regular(executable_metadata.st_mode)
        or executable_metadata.st_uid != os.geteuid()
        or executable_metadata.st_nlink != 1
        or not stat.S_ISDIR(root_metadata.st_mode)
        or root_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(root_metadata.st_mode) != 0o700
        or not wheel_matches
    ):
        raise CoreServiceError(
            CoreServiceErrorCode.VERIFICATION_FAILED,
            "The isolated Core generation did not match its release inputs.",
            retryable=False,
        )
    try:
        load_verified_framework_registry(framework_lock)
    except Exception as exc:
        raise CoreServiceError(
            CoreServiceErrorCode.VERIFICATION_FAILED,
            "The isolated Core generation did not match its framework lock.",
            retryable=False,
        ) from exc


def consume_core_service_attachment(
    *,
    service_root: str | Path,
    attachment_name: str,
) -> bytes:
    _validate_attachment_name(attachment_name)
    try:
        require_host_global_service_root(service_root)
        with HostServiceRoot(service_root, create=False) as root:
            payload = root.read_bytes(attachment_name, max_bytes=_MAX_READY_BYTES)
            root.unlink_regular(attachment_name)
            return payload
    except (OSError, RuntimeIdentityError) as exc:
        raise CoreServiceError(
            CoreServiceErrorCode.STATE_INVALID,
            "Core bootstrap attachment is unavailable.",
            retryable=False,
        ) from exc


def _ensure_locked(
    *,
    root: HostServiceRoot,
    framework_lock: Path,
    release: CoreReleaseIdentity,
    port: int,
    deadline: float,
    replace_mismatched: bool,
    expected_predecessor: CoreServicePredecessor | None,
    daemon_bundle_identity: CoreDaemonBundleIdentity | None,
    controller: LinuxProcessController,
    fault_injector: Callable[[str, int], None] | None = None,
    reuse_frozen_extraction_for_bounded_smoke: bool = False,
) -> CoreServiceAttachment:
    bearer = load_or_create_core_bearer_token(root)
    existing_value = root.read_optional_json(_LEDGER_NAME)
    floor: dict[str, Any] | None = None
    predecessor_consumed = False
    if existing_value is None:
        _recover_pending(root, controller=controller, deadline=deadline)
    if existing_value is not None:
        state = _require_service_state(existing_value)
        if state.get("state") == "stopped":
            floor = state
            if expected_predecessor is not None:
                _require_predecessor_match(
                    expected=expected_predecessor,
                    actual=CoreServicePredecessor.absent(),
                )
            root.unlink_regular(_READY_NAME)
            root.unlink_regular(_PENDING_NAME)
        else:
            ledger = state
            process_group = _service_process_group_from_ledger(ledger)
            alive = _service_process_group_is_alive(process_group, controller)
            if not alive:
                _terminate_service_process_group(
                    process_group,
                    controller,
                    deadline=deadline,
                )
            if not alive and _is_exact_daemon_ledger(ledger):
                floor = _floor_from_ledger(ledger)
                root.atomic_write_json(_LEDGER_NAME, floor, replace=True)
                root.unlink_regular(_READY_NAME)
                root.unlink_regular(_PENDING_NAME)
            elif not alive:
                root.unlink_regular(_LEDGER_NAME)
                root.unlink_regular(_READY_NAME)
                root.unlink_regular(_PENDING_NAME)
            if alive:
                actual_predecessor = _predecessor_from_ledger(ledger)
                if daemon_bundle_identity is not None:
                    _require_predecessor_match(
                        expected=expected_predecessor,
                        actual=actual_predecessor,
                    )
                matching = _service_identity_matches(
                    ledger,
                    release=release,
                    daemon_bundle_identity=daemon_bundle_identity,
                )
                if matching:
                    if port not in {0, ledger["port"]}:
                        raise CoreServiceError(
                            CoreServiceErrorCode.PORT_UNAVAILABLE,
                            "Core service already owns a different loopback port.",
                            retryable=False,
                        )
                    proof = _authenticated_status_proof(
                        port=ledger["port"],
                        bearer=bearer,
                        release=release,
                        generation=ledger["generation"],
                        deadline=deadline,
                        require_production_v2=_ledger_requires_production_v2(ledger),
                    )
                    _verify_ready_ledger(root, ledger, proof)
                    root.unlink_regular(_PENDING_NAME)
                    return _attachment_from_ledger(
                        ledger,
                        bearer=bearer,
                        status_proof=proof,
                        attached=True,
                    )
                if daemon_bundle_identity is not None:
                    current_compatibility = actual_predecessor.lifecycle_compatibility
                    if (
                        not replace_mismatched
                        or current_compatibility is None
                        or daemon_bundle_identity.lifecycle_compatibility <= current_compatibility
                    ):
                        raise CoreServiceError(
                            CoreServiceErrorCode.UPDATE_REQUIRED,
                            "The active OpenEvo Daemon cannot be replaced by this lifecycle "
                            "compatibility.",
                            retryable=False,
                        )
                    _terminate_service_process_group(
                        process_group,
                        controller,
                        deadline=deadline,
                    )
                    floor = _floor_from_ledger(ledger)
                    root.atomic_write_json(_LEDGER_NAME, floor, replace=True)
                    root.unlink_regular(_READY_NAME)
                    root.unlink_regular(_PENDING_NAME)
                    predecessor_consumed = True
                else:
                    if not replace_mismatched:
                        raise CoreServiceError(
                            CoreServiceErrorCode.IDENTITY_MISMATCH,
                            "A different verified Core release is already running.",
                            retryable=False,
                        )
                    _require_predecessor_match(
                        expected=expected_predecessor,
                        actual=actual_predecessor,
                    )
                    _terminate_service_process_group(
                        process_group,
                        controller,
                        deadline=deadline,
                    )
                    root.unlink_regular(_LEDGER_NAME)
                    root.unlink_regular(_READY_NAME)
                    root.unlink_regular(_PENDING_NAME)
        if floor is not None:
            _require_floor_compatibility(floor, daemon_bundle_identity)
            if expected_predecessor is not None and not predecessor_consumed:
                _require_predecessor_match(
                    expected=expected_predecessor,
                    actual=CoreServicePredecessor.absent(),
                )
    elif expected_predecessor is not None:
        _require_predecessor_match(
            expected=expected_predecessor,
            actual=CoreServicePredecessor.absent(),
        )

    _require_time(deadline)
    bearer = rotate_core_bearer_token(root)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    child: subprocess.Popen[bytes] | None = None
    process_group: _ServiceProcessGroup | None = None
    ready_read = -1
    ready_write = -1
    spawn_lock_fd = -1
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        try:
            listener.bind(("127.0.0.1", port))
            listener.listen(128)
        except OSError as exc:
            raise CoreServiceError(
                CoreServiceErrorCode.PORT_UNAVAILABLE,
                "Core service loopback port is unavailable.",
                retryable=True,
            ) from exc
        actual_port = int(listener.getsockname()[1])
        ready_read, ready_write = os.pipe2(os.O_CLOEXEC)
        log_fd = _open_log(root)
        generation = os.urandom(16).hex()
        spawn_lock_fd = root.open_lock(_SPAWN_LOCK_NAME)
        _flock_until(spawn_lock_fd, deadline)
        spawn_lock_metadata = os.fstat(spawn_lock_fd)
        root.atomic_write_json(
            _PENDING_NAME,
            {
                "schema_version": 3,
                "phase": "spawn_intent",
                "release_identity": release.digest,
                "port": actual_port,
                "generation": generation,
                "spawn_lock_device": spawn_lock_metadata.st_dev,
                "spawn_lock_inode": spawn_lock_metadata.st_ino,
            },
            replace=False,
        )
        argv = [
            sys.executable,
            "-m",
            "openevo.backend.launcher",
            "serve",
            "--service-root",
            str(root.path),
            "--framework-lock",
            str(framework_lock),
            "--source-commit",
            release.source_commit,
            "--socket-fd",
            str(listener.fileno()),
            "--ready-fd",
            str(ready_write),
            "--spawn-lock-fd",
            str(spawn_lock_fd),
            "--expected-release-identity",
            release.digest,
            "--generation",
            generation,
        ]
        child_environment = _frozen_service_environment(
            reuse_parent_extraction_for_bounded_smoke=(reuse_frozen_extraction_for_bounded_smoke)
        )
        try:
            child = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=log_fd,
                stderr=subprocess.STDOUT,
                env=child_environment,
                start_new_session=True,
                close_fds=True,
                pass_fds=(listener.fileno(), ready_write, spawn_lock_fd),
            )
        finally:
            os.close(log_fd)
            os.close(ready_write)
            ready_write = -1
            os.close(spawn_lock_fd)
            spawn_lock_fd = -1
        launcher_process = controller.capture(child.pid)
        if fault_injector is not None:
            fault_injector("after_spawn", child.pid)
        try:
            ready = _wait_ready(ready_read, child, deadline=deadline)
        finally:
            os.close(ready_read)
            ready_read = -1
        require_production_v2 = _identity_requires_production_v2(daemon_bundle_identity)
        if not _launcher_ready_matches(
            ready,
            release=release,
            generation=generation,
            require_production_v2=require_production_v2,
        ):
            raise CoreServiceError(
                CoreServiceErrorCode.START_FAILED,
                "Core service returned an invalid readiness proof.",
                retryable=True,
            )
        process_group = _claimed_process(
            root,
            expected_launcher=launcher_process,
            process_controller=controller,
            release_identity=release.digest,
            port=actual_port,
            generation=generation,
        )
        if daemon_bundle_identity is None and (
            process_group.launcher != process_group.application
        ):
            raise CoreServiceError(
                CoreServiceErrorCode.START_FAILED,
                "A frozen Core service requires a versioned Daemon lifecycle identity.",
                retryable=False,
            )
        if not _service_process_group_is_alive(process_group, controller):
            raise CoreServiceError(
                CoreServiceErrorCode.START_FAILED,
                "Core service exited during startup.",
                retryable=True,
            )
        proof = _authenticated_status_proof(
            port=actual_port,
            bearer=bearer,
            release=release,
            generation=generation,
            deadline=deadline,
            require_production_v2=require_production_v2,
        )
        ready_ledger: dict[str, object] = {
            "schema_version": 1 if daemon_bundle_identity is None else 2,
            "generation": generation,
            "release_identity": release.digest,
            "registry_digest": release.registry_digest,
            "status_proof": proof,
        }
        if daemon_bundle_identity is not None:
            ready_ledger.update(
                {
                    "bundle_sha256": daemon_bundle_identity.bundle_sha256,
                    "canonical_manifest_sha256": (
                        daemon_bundle_identity.canonical_manifest_sha256
                    ),
                    "lifecycle_compatibility": (daemon_bundle_identity.lifecycle_compatibility),
                }
            )
        ready_digest = hashlib.sha256(canonical_json_bytes(ready_ledger)).hexdigest()
        persisted_process = (
            process_group.application if daemon_bundle_identity is None else process_group.launcher
        )
        ledger: dict[str, object] = {
            "schema_version": 2 if daemon_bundle_identity is None else 5,
            "state": "running" if daemon_bundle_identity is not None else None,
            "release_identity": release.digest,
            "registry_digest": release.registry_digest,
            "framework_lock_sha256": release.framework_lock_sha256,
            "source_commit": release.source_commit,
            "pid": persisted_process.pid,
            "boot_id": persisted_process.boot_id,
            "start_time_ticks": persisted_process.start_time_ticks,
            "port": actual_port,
            "generation": generation,
            "ready_sha256": ready_digest,
        }
        if daemon_bundle_identity is None:
            ledger.pop("state")
        else:
            ledger.update(
                {
                    "application_pid": process_group.application.pid,
                    "application_boot_id": process_group.application.boot_id,
                    "application_start_time_ticks": (process_group.application.start_time_ticks),
                    "bundle_sha256": daemon_bundle_identity.bundle_sha256,
                    "canonical_manifest_sha256": (
                        daemon_bundle_identity.canonical_manifest_sha256
                    ),
                    "lifecycle_compatibility": (daemon_bundle_identity.lifecycle_compatibility),
                }
            )
        root.atomic_write_json(_READY_NAME, ready_ledger, replace=False)
        root.atomic_write_json(_LEDGER_NAME, ledger, replace=floor is not None)
        root.unlink_regular(_PENDING_NAME)
        if not _service_process_group_is_alive(process_group, controller):
            raise CoreServiceError(
                CoreServiceErrorCode.START_FAILED,
                "Core service exited during state publication.",
                retryable=True,
            )
        return _attachment_from_ledger(
            ledger,
            bearer=bearer,
            status_proof=proof,
            attached=False,
        )
    except BaseException:
        cleanup_deadline = max(deadline, time.monotonic() + 10.0)
        pending_cleanup_confirmed = True
        if process_group is not None:
            try:
                _terminate_service_process_group(
                    process_group,
                    controller,
                    deadline=cleanup_deadline,
                )
            except BaseException:
                pending_cleanup_confirmed = False
        try:
            if root.read_optional_json(_PENDING_NAME, max_bytes=_MAX_READY_BYTES) is not None:
                _recover_pending(
                    root,
                    controller=controller,
                    deadline=cleanup_deadline,
                    clear_state=False,
                )
        except BaseException:
            pending_cleanup_confirmed = False
        if child is not None:
            child_exit_confirmed = _terminate_spawned_child(child)
            if not child_exit_confirmed:
                _retain_orphaned_service_child(child)
        else:
            child_exit_confirmed = True
        if process_group is not None:
            try:
                if _service_process_group_is_alive(process_group, controller):
                    pending_cleanup_confirmed = False
            except BaseException:
                pending_cleanup_confirmed = False
        if child_exit_confirmed and pending_cleanup_confirmed:
            try:
                if floor is None:
                    root.unlink_regular(_LEDGER_NAME)
                else:
                    root.atomic_write_json(_LEDGER_NAME, floor, replace=True)
                root.unlink_regular(_READY_NAME)
                root.unlink_regular(_PENDING_NAME)
            except BaseException:
                pass
        raise
    finally:
        for descriptor in (ready_read, ready_write, spawn_lock_fd):
            if descriptor >= 0:
                os.close(descriptor)
        listener.close()


def _authenticated_status_proof(
    *,
    port: int,
    bearer: str,
    release: CoreReleaseIdentity,
    generation: str,
    deadline: float,
    host: str = "127.0.0.1",
    require_production_v2: bool = False,
) -> str:
    return authenticate_core_service_endpoint(
        host=host,
        port=port,
        bearer=bearer,
        release_identity=release.digest,
        registry_digest=release.registry_digest,
        source_commit=release.source_commit,
        generation=generation,
        deadline=deadline,
        require_production_v2=require_production_v2,
    )


def _identity_requires_production_v2(
    identity: CoreDaemonBundleIdentity | None,
) -> bool:
    return (
        identity is not None
        and identity.lifecycle_compatibility >= _PRODUCTION_V2_LIFECYCLE_COMPATIBILITY
    )


def _ledger_requires_production_v2(ledger: dict[str, Any]) -> bool:
    return bool(
        _is_exact_daemon_ledger(ledger)
        and ledger["lifecycle_compatibility"] >= _PRODUCTION_V2_LIFECYCLE_COMPATIBILITY
    )


def _production_v2_feature_set_sha256() -> str:
    payload = json.dumps(
        list(RELEASE_DAEMON_FEATURE_FLAGS_V2),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _is_exact_production_v2_discovery(
    version: core_v2_models.VersionResponseV2,
) -> bool:
    if (
        version.preferred_major != 2
        or version.supported_majors != [2]
        or version.mutation_major != 2
        or version.release_version != __version__
        or tuple(version.feature_flags) != RELEASE_DAEMON_FEATURE_FLAGS_V2
        or version.feature_set_sha256 != _production_v2_feature_set_sha256()
        or version.runtime_contract_sha256 != release_runtime_contract_sha256()
        or len(version.contracts) != 1
    ):
        return False
    offer = version.contracts[0]
    return (
        offer.api_major == 2
        and offer.access == "mutation"
        and offer.mutation_compatible is True
        and offer.openapi_sha256 == core_v2_openapi_sha256()
        and offer.event_schema_sha256 == core_v2_events_schema_sha256()
    )


def _launcher_ready_matches(
    value: object,
    *,
    release: CoreReleaseIdentity,
    generation: str,
    require_production_v2: bool,
) -> bool:
    if not isinstance(value, dict):
        return False
    v2_keys = {
        "api_major",
        "build_id",
        "event_schema_sha256",
        "feature_set_sha256",
        "generation",
        "openapi_sha256",
        "provider_kind",
        "registry_digest",
        "release_identity",
        "release_version",
        "runtime_contract_sha256",
        "schema_version",
        "source_commit",
    }
    if set(value) == v2_keys:
        return bool(
            value.get("schema_version") == 2
            and value.get("generation") == generation
            and value.get("release_identity") == release.digest
            and value.get("api_major") == 2
            and value.get("openapi_sha256") == core_v2_openapi_sha256()
            and value.get("event_schema_sha256") == core_v2_events_schema_sha256()
            and value.get("release_version") == __version__
            and isinstance(value.get("build_id"), str)
            and re.fullmatch(r"[0-9a-f]{64}", value["build_id"]) is not None
            and value.get("source_commit") == release.source_commit
            and value.get("provider_kind") == "openevo_daemon"
            and value.get("feature_set_sha256") == _production_v2_feature_set_sha256()
            and value.get("registry_digest") == release.registry_digest
            and value.get("runtime_contract_sha256") == release_runtime_contract_sha256()
        )
    if require_production_v2:
        return False
    return value == {
        "schema_version": 1,
        "generation": generation,
        "release_identity": release.digest,
        "registry_digest": release.registry_digest,
    }


def authenticate_core_service_endpoint(
    *,
    host: str | None,
    port: int | None,
    bearer: str,
    release_identity: str,
    registry_digest: str,
    source_commit: str,
    generation: str,
    deadline: float,
    endpoint: CoreServiceEndpoint | None = None,
    require_production_v2: bool = False,
) -> str:
    if (
        ((endpoint is None) != (host == "127.0.0.1" and type(port) is int and 1 <= port <= 65535))
        or re.fullmatch(r"[A-Za-z0-9_-]{64}", bearer) is None
        or re.fullmatch(r"[0-9a-f]{64}", release_identity) is None
        or re.fullmatch(r"[0-9a-f]{64}", registry_digest) is None
        or re.fullmatch(r"[0-9a-f]{40}", source_commit) is None
        or re.fullmatch(r"[0-9a-f]{32}", generation) is None
        or not isinstance(require_production_v2, bool)
    ):
        raise CoreServiceError(
            CoreServiceErrorCode.STATUS_INVALID,
            "Core service endpoint identity is invalid.",
            retryable=False,
        )
    expected_headers = {
        _SERVICE_GENERATION_HEADER: generation,
        _RELEASE_IDENTITY_HEADER: release_identity,
    }
    version = _fetch_json(
        host,
        port,
        "/version",
        bearer=bearer,
        deadline=deadline,
        expected_headers=expected_headers,
        endpoint=endpoint,
    )
    is_v2 = isinstance(version, dict) and (
        version.get("preferred_major") == 2 or version.get("provider_kind") == "openevo_daemon"
    )
    if require_production_v2 and not is_v2:
        raise CoreServiceError(
            CoreServiceErrorCode.STATUS_INVALID,
            "Core service identity proof did not match the verified release.",
            retryable=False,
        )
    status_path = "/v2/system/status" if is_v2 else "/v1/status"
    status = _fetch_json(
        host,
        port,
        status_path,
        bearer=bearer,
        deadline=deadline,
        expected_headers=expected_headers,
        endpoint=endpoint,
    )
    if is_v2:
        try:
            version_v2 = core_v2_models.VersionResponseV2.model_validate(version)
            status_v2 = core_v2_models.SystemStatusV2.model_validate(status)
        except ValidationError as exc:
            raise CoreServiceError(
                CoreServiceErrorCode.STATUS_INVALID,
                "Core service identity proof did not match the verified release.",
                retryable=False,
            ) from exc
        if (
            version_v2.provider_kind != "openevo_daemon"
            or version_v2.build_channel != "release"
            or version_v2.source_commit != source_commit
            or version_v2.registry_sha256 != registry_digest
            or not version_v2.mutation_compatible
            or status_v2.status != "ready"
            or status_v2.release_version != version_v2.release_version
            or status_v2.source_commit != source_commit
            or status_v2.registry_sha256 != registry_digest
            or (require_production_v2 and not _is_exact_production_v2_discovery(version_v2))
        ):
            raise CoreServiceError(
                CoreServiceErrorCode.STATUS_INVALID,
                "Core service identity proof did not match the verified release.",
                retryable=False,
            )
        mutation_offer = next(
            (
                offer
                for offer in version_v2.contracts
                if offer.api_major == version_v2.mutation_major
            ),
            None,
        )
        if mutation_offer is None:
            raise CoreServiceError(
                CoreServiceErrorCode.STATUS_INVALID,
                "Core service identity proof did not match the verified release.",
                retryable=False,
            )
        material = canonical_json_bytes(
            {
                "schema_version": 2,
                "generation": generation,
                "release_identity": release_identity,
                "api_major": version_v2.mutation_major,
                "openapi_sha256": mutation_offer.openapi_sha256,
                "event_schema_sha256": mutation_offer.event_schema_sha256,
                "release_version": version_v2.release_version,
                "build_id": version_v2.build_id,
                "source_commit": version_v2.source_commit,
                "provider_kind": version_v2.provider_kind,
                "build_channel": version_v2.build_channel,
                "feature_set_sha256": version_v2.feature_set_sha256,
                "registry_sha256": version_v2.registry_sha256,
                "runtime_contract_sha256": version_v2.runtime_contract_sha256,
            }
        )
    else:
        if (
            not isinstance(version, dict)
            or version.get("provider_kind") != "openevo_core"
            or version.get("build_channel") != "release"
            or version.get("source_commit") != source_commit
            or not isinstance(version.get("openapi_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", version["openapi_sha256"]) is None
            or not isinstance(version.get("build_version"), str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,127}", version["build_version"]) is None
            or not isinstance(status, dict)
            or status.get("registry_status") != "verified"
            or status.get("registry_digest") != registry_digest
        ):
            raise CoreServiceError(
                CoreServiceErrorCode.STATUS_INVALID,
                "Core service identity proof did not match the verified release.",
                retryable=False,
            )
        material = canonical_json_bytes(
            {
                "schema_version": 1,
                "generation": generation,
                "release_identity": release_identity,
                "openapi_sha256": version.get("openapi_sha256"),
                "build_version": version.get("build_version"),
                "source_commit": version.get("source_commit"),
                "provider_kind": version.get("provider_kind"),
                "build_channel": version.get("build_channel"),
                "registry_status": status.get("registry_status"),
                "registry_digest": status.get("registry_digest"),
            }
        )
    return hmac.new(bearer.encode("ascii"), material, hashlib.sha256).hexdigest()


def _fetch_json(
    host: str | None,
    port: int | None,
    path: str,
    *,
    bearer: str | None,
    deadline: float,
    expected_headers: dict[str, str] | None = None,
    endpoint: CoreServiceEndpoint | None = None,
) -> Any:
    last_error: Exception | None = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CoreServiceError(
                CoreServiceErrorCode.DEADLINE_EXCEEDED,
                "Core service did not become ready before the deadline.",
                retryable=True,
            ) from last_error
        headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
        if endpoint is None:
            assert host is not None and port is not None
            connection: http.client.HTTPConnection = http.client.HTTPConnection(
                host,
                port,
                timeout=min(_LOCAL_HTTP_ATTEMPT_SECONDS, remaining),
            )
        else:
            connection = _EndpointHTTPConnection(
                endpoint,
                timeout=min(_TUNNEL_HTTP_ATTEMPT_SECONDS, remaining),
            )
        try:
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            if response.status != 200:
                raise CoreServiceError(
                    CoreServiceErrorCode.STATUS_INVALID,
                    "Core service status endpoint rejected identity verification.",
                    retryable=False,
                )
            if expected_headers is not None and any(
                not hmac.compare_digest(response.headers.get(name, ""), expected)
                for name, expected in expected_headers.items()
            ):
                raise CoreServiceError(
                    CoreServiceErrorCode.STATUS_INVALID,
                    "Core service endpoint generation did not match the attachment.",
                    retryable=False,
                )
            content_type = response.headers.get_content_type()
            length = response.headers.get("Content-Length")
            try:
                content_length = int(length) if length is not None else None
            except ValueError as exc:
                raise CoreServiceError(
                    CoreServiceErrorCode.STATUS_INVALID,
                    "Core service status response metadata is invalid.",
                    retryable=False,
                ) from exc
            if content_type != "application/json" or (
                content_length is not None and not 0 <= content_length <= _MAX_HTTP_BYTES
            ):
                raise CoreServiceError(
                    CoreServiceErrorCode.STATUS_INVALID,
                    "Core service status response metadata is invalid.",
                    retryable=False,
                )
            payload = response.read(_MAX_HTTP_BYTES + 1)
            if len(payload) > _MAX_HTTP_BYTES:
                raise CoreServiceError(
                    CoreServiceErrorCode.STATUS_INVALID,
                    "Core service status response exceeded its limit.",
                    retryable=False,
                )
            try:
                value = load_bounded_json(payload, max_bytes=_MAX_HTTP_BYTES)
            except RuntimeIdentityError as exc:
                raise CoreServiceError(
                    CoreServiceErrorCode.STATUS_INVALID,
                    "Core service status response is invalid.",
                    retryable=False,
                ) from exc
            if endpoint is not None:
                endpoint.verify_authority()
            return value
        except CoreServiceError:
            raise
        except (OSError, ValueError, http.client.HTTPException) as exc:
            last_error = exc
            time.sleep(min(0.05, max(0.0, remaining)))
        finally:
            connection.close()


def _wait_ready(
    ready_fd: int,
    child: subprocess.Popen[bytes],
    *,
    deadline: float,
) -> Any:
    payload = bytearray()
    while True:
        _require_time(deadline)
        readable, _, _ = select.select([ready_fd], [], [], min(0.1, deadline - time.monotonic()))
        if readable:
            chunk = os.read(ready_fd, _MAX_READY_BYTES + 1 - len(payload))
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > _MAX_READY_BYTES:
                raise CoreServiceError(
                    CoreServiceErrorCode.START_FAILED,
                    "Core service readiness response exceeded its limit.",
                    retryable=False,
                )
            if payload.endswith(b"\n"):
                break
        return_code = child.poll()
        if return_code is not None:
            raise CoreServiceError(
                CoreServiceErrorCode.START_FAILED,
                "Core service exited before readiness.",
                retryable=True,
            )
    try:
        return load_bounded_json(bytes(payload), max_bytes=_MAX_READY_BYTES)
    except RuntimeIdentityError as exc:
        raise CoreServiceError(
            CoreServiceErrorCode.START_FAILED,
            "Core service readiness response was invalid.",
            retryable=False,
        ) from exc


def _verify_ready_ledger(
    root: HostServiceRoot,
    ledger: dict[str, Any],
    proof: str,
) -> None:
    ready = root.read_json(_READY_NAME, max_bytes=_MAX_READY_BYTES)
    legacy_keys = {
        "schema_version",
        "generation",
        "release_identity",
        "registry_digest",
        "status_proof",
    }
    exact_keys = legacy_keys | {
        "bundle_sha256",
        "canonical_manifest_sha256",
        "lifecycle_compatibility",
    }
    expected_keys = exact_keys if _is_exact_daemon_ledger(ledger) else legacy_keys
    if not isinstance(ready, dict) or set(ready) != expected_keys:
        raise CoreServiceError(
            CoreServiceErrorCode.STATE_INVALID,
            "Core service readiness state is invalid.",
            retryable=False,
        )
    digest = hashlib.sha256(canonical_json_bytes(ready)).hexdigest()
    if (
        ready.get("schema_version") != (2 if _is_exact_daemon_ledger(ledger) else 1)
        or ready.get("generation") != ledger["generation"]
        or ready.get("release_identity") != ledger["release_identity"]
        or ready.get("registry_digest") != ledger["registry_digest"]
        or ready.get("status_proof") != proof
        or digest != ledger["ready_sha256"]
        or (
            _is_exact_daemon_ledger(ledger)
            and (
                ready.get("bundle_sha256") != ledger["bundle_sha256"]
                or ready.get("canonical_manifest_sha256") != ledger["canonical_manifest_sha256"]
                or ready.get("lifecycle_compatibility") != ledger["lifecycle_compatibility"]
            )
        )
    ):
        raise CoreServiceError(
            CoreServiceErrorCode.STATE_INVALID,
            "Core service readiness state does not match the running process.",
            retryable=False,
        )


def _recover_pending(
    root: HostServiceRoot,
    *,
    controller: LinuxProcessController,
    deadline: float,
    clear_state: bool = True,
) -> None:
    value = root.read_optional_json(_PENDING_NAME, max_bytes=_MAX_READY_BYTES)
    if value is None:
        if clear_state:
            root.unlink_regular(_READY_NAME)
        return
    pending = _require_pending(value)
    original_identity = (
        pending["release_identity"],
        pending["port"],
        pending["generation"],
    )
    lock_identity: LockIdentity | None = None
    if "spawn_lock_device" in pending:
        lock_identity = LockIdentity(
            device=pending["spawn_lock_device"],
            inode=pending["spawn_lock_inode"],
        )
    claimed_identities: list[ProcessIdentity] = []

    def terminate_claimed(value: dict[str, Any]) -> None:
        if value["phase"] != "spawn_claimed":
            return
        identity = ProcessIdentity(
            pid=value["pid"],
            boot_id=value["boot_id"],
            start_time_ticks=value["start_time_ticks"],
        )
        if identity not in claimed_identities:
            claimed_identities.append(identity)
        if controller.is_alive(identity):
            controller.terminate(identity, deadline=deadline)

    terminate_claimed(pending)
    if lock_identity is not None:
        for holder in controller.find_lock_holders(lock_identity):
            if controller.is_alive(holder):
                claimed_was_present = bool(claimed_identities)
                if not claimed_was_present or not _wait_for_natural_launcher_exit(
                    holder,
                    controller,
                    deadline=deadline,
                ):
                    controller.terminate(holder, deadline=deadline)
        spawn_lock_fd = root.open_lock(_SPAWN_LOCK_NAME)
        try:
            metadata = os.fstat(spawn_lock_fd)
            if (metadata.st_dev, metadata.st_ino) != (
                lock_identity.device,
                lock_identity.inode,
            ):
                raise CoreServiceError(
                    CoreServiceErrorCode.STATE_INVALID,
                    "Core service spawn lock identity changed.",
                    retryable=False,
                )
            _flock_until(spawn_lock_fd, deadline)
            current = root.read_optional_json(_PENDING_NAME, max_bytes=_MAX_READY_BYTES)
            if current is not None:
                pending = _require_pending(current)
                if (
                    pending["release_identity"],
                    pending["port"],
                    pending["generation"],
                ) != original_identity:
                    raise CoreServiceError(
                        CoreServiceErrorCode.STATE_INVALID,
                        "Core service pending identity changed during recovery.",
                        retryable=False,
                    )
                terminate_claimed(pending)
        finally:
            os.close(spawn_lock_fd)
        for holder in controller.find_lock_holders(lock_identity):
            if controller.is_alive(holder):
                raise CoreServiceError(
                    CoreServiceErrorCode.DEADLINE_EXCEEDED,
                    "Core service launcher remained alive after pending recovery.",
                    retryable=True,
                )
    if any(controller.is_alive(identity) for identity in claimed_identities):
        raise CoreServiceError(
            CoreServiceErrorCode.DEADLINE_EXCEEDED,
            "Core service application remained alive after pending recovery.",
            retryable=True,
        )
    if clear_state:
        root.unlink_regular(_PENDING_NAME)
        root.unlink_regular(_READY_NAME)


def _require_pending(value: Any) -> dict[str, Any]:
    common = {
        "schema_version",
        "phase",
        "release_identity",
        "port",
        "generation",
    }
    intent_keys = common | {"spawn_lock_device", "spawn_lock_inode"}
    claimed_v2_keys = common | {"pid", "boot_id", "start_time_ticks"}
    claimed_v3_keys = claimed_v2_keys | {"spawn_lock_device", "spawn_lock_inode"}
    schema_version = value.get("schema_version") if isinstance(value, dict) else None
    phase = value.get("phase") if isinstance(value, dict) else None
    expected_keys = (
        intent_keys
        if phase == "spawn_intent"
        else claimed_v3_keys
        if schema_version == 3
        else claimed_v2_keys
    )
    if (
        not isinstance(value, dict)
        or schema_version not in {2, 3}
        or phase not in {"spawn_intent", "spawn_claimed"}
        or set(value) != expected_keys
        or not isinstance(value.get("release_identity"), str)
        or re.fullmatch(r"[0-9a-f]{64}", value["release_identity"]) is None
        or not isinstance(value.get("generation"), str)
        or re.fullmatch(r"[0-9a-f]{32}", value["generation"]) is None
    ):
        raise CoreServiceError(
            CoreServiceErrorCode.STATE_INVALID,
            "Core service pending state is invalid.",
            retryable=False,
        )
    _required_int(value, "port", minimum=1, maximum=65535)
    if value["phase"] == "spawn_intent" or schema_version == 3:
        _required_int(value, "spawn_lock_device", minimum=1)
        _required_int(value, "spawn_lock_inode", minimum=1)
    if value["phase"] == "spawn_claimed":
        _required_int(value, "pid", minimum=1)
        _required_int(value, "start_time_ticks", minimum=1)
        if (
            not isinstance(value.get("boot_id"), str)
            or _BOOT_ID_PATTERN.fullmatch(value["boot_id"]) is None
        ):
            raise CoreServiceError(
                CoreServiceErrorCode.STATE_INVALID,
                "Core service pending state is invalid.",
                retryable=False,
            )
    return value


def _claimed_process(
    root: HostServiceRoot,
    *,
    expected_launcher: ProcessIdentity,
    process_controller: LinuxProcessController,
    release_identity: str,
    port: int,
    generation: str,
) -> _ServiceProcessGroup:
    pending = _require_pending(root.read_json(_PENDING_NAME, max_bytes=_MAX_READY_BYTES))
    if (
        pending["phase"] != "spawn_claimed"
        or pending["release_identity"] != release_identity
        or pending["port"] != port
        or pending["generation"] != generation
    ):
        raise CoreServiceError(
            CoreServiceErrorCode.STATE_INVALID,
            "Core service child claim does not match the spawn intent.",
            retryable=False,
        )
    claimed = ProcessIdentity(
        pid=pending["pid"],
        boot_id=pending["boot_id"],
        start_time_ticks=pending["start_time_ticks"],
    )
    if not process_controller.owns_service_process(expected_launcher, claimed):
        raise CoreServiceError(
            CoreServiceErrorCode.STATE_INVALID,
            "Core service child claim does not match the spawn topology.",
            retryable=False,
        )
    return _ServiceProcessGroup(
        launcher=expected_launcher,
        application=claimed,
    )


def claim_core_service_spawn(
    *,
    service_root: str | Path,
    spawn_lock_fd: int,
    release_identity: str,
    port: int,
    generation: str,
) -> ProcessIdentity:
    try:
        require_host_global_service_root(service_root)
        if spawn_lock_fd < 3:
            raise RuntimeIdentityError("Core spawn lock descriptor is invalid")
        with HostServiceRoot(service_root, create=False) as root:
            lock_metadata = os.fstat(spawn_lock_fd)
            pathname_metadata = os.stat(
                _SPAWN_LOCK_NAME,
                dir_fd=root.fd,
                follow_symlinks=False,
            )
            if (
                not stat_is_regular(lock_metadata.st_mode)
                or lock_metadata.st_uid != os.geteuid()
                or (lock_metadata.st_mode & 0o777) != 0o600
                or (lock_metadata.st_dev, lock_metadata.st_ino)
                != (pathname_metadata.st_dev, pathname_metadata.st_ino)
            ):
                raise RuntimeIdentityError("Core spawn lock identity is invalid")
            probe_fd = root.open_lock(_SPAWN_LOCK_NAME)
            try:
                try:
                    fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    raise RuntimeIdentityError("Core spawn lock is not held")
            finally:
                os.close(probe_fd)
            pending = _require_pending(root.read_json(_PENDING_NAME, max_bytes=_MAX_READY_BYTES))
            if (
                pending["phase"] != "spawn_intent"
                or pending["release_identity"] != release_identity
                or pending["port"] != port
                or pending["generation"] != generation
                or pending["spawn_lock_device"] != lock_metadata.st_dev
                or pending["spawn_lock_inode"] != lock_metadata.st_ino
            ):
                raise RuntimeIdentityError("Core spawn intent does not match the child")
            identity = LinuxProcessController().capture(os.getpid())
            root.atomic_write_json(
                _PENDING_NAME,
                {
                    "schema_version": 3,
                    "phase": "spawn_claimed",
                    "release_identity": release_identity,
                    "pid": identity.pid,
                    "boot_id": identity.boot_id,
                    "start_time_ticks": identity.start_time_ticks,
                    "port": port,
                    "generation": generation,
                    "spawn_lock_device": pending["spawn_lock_device"],
                    "spawn_lock_inode": pending["spawn_lock_inode"],
                },
                replace=True,
            )
            return identity
    except (OSError, RuntimeIdentityError) as exc:
        raise CoreServiceError(
            CoreServiceErrorCode.STATE_INVALID,
            "Core service child could not claim its spawn intent.",
            retryable=False,
        ) from exc
    finally:
        if spawn_lock_fd >= 3:
            os.close(spawn_lock_fd)


def _read_ledger(root: HostServiceRoot) -> dict[str, Any]:
    try:
        state = _require_service_state(root.read_json(_LEDGER_NAME))
    except FileNotFoundError as exc:
        raise CoreServiceError(
            CoreServiceErrorCode.STATUS_INVALID,
            "Core service is not running.",
            retryable=True,
        ) from exc
    if state.get("state") == "stopped":
        raise CoreServiceError(
            CoreServiceErrorCode.STATUS_INVALID,
            "Core service is not running.",
            retryable=True,
        )
    return state


def _predecessor_from_ledger(ledger: dict[str, Any]) -> CoreServicePredecessor:
    if not _is_exact_daemon_ledger(ledger):
        return CoreServicePredecessor.legacy(
            generation=ledger["generation"],
            release_identity=ledger["release_identity"],
        )
    return CoreServicePredecessor.running(
        generation=ledger["generation"],
        release_identity=ledger["release_identity"],
        bundle_sha256=ledger["bundle_sha256"],
        canonical_manifest_sha256=ledger["canonical_manifest_sha256"],
        lifecycle_compatibility=ledger["lifecycle_compatibility"],
    )


def _require_predecessor_match(
    *,
    expected: CoreServicePredecessor | None,
    actual: CoreServicePredecessor,
) -> None:
    if expected != actual:
        raise CoreServiceError(
            CoreServiceErrorCode.PREDECESSOR_MISMATCH,
            "Core service changed after activation observed its predecessor.",
            retryable=True,
        )


def _require_service_state(value: Any) -> dict[str, Any]:
    if (
        isinstance(value, dict)
        and value.get("schema_version") == 3
        and value.get("state") == "stopped"
    ):
        keys = {
            "schema_version",
            "state",
            "release_identity",
            "bundle_sha256",
            "canonical_manifest_sha256",
            "lifecycle_compatibility",
        }
        if set(value) != keys:
            _raise_invalid_service_state()
        for key in ("release_identity", "bundle_sha256", "canonical_manifest_sha256"):
            item = value.get(key)
            if item is not None and (
                not isinstance(item, str) or re.fullmatch(r"[0-9a-f]{64}", item) is None
            ):
                _raise_invalid_service_state()
        compatibility = value.get("lifecycle_compatibility")
        if type(compatibility) is not int or not 1 <= compatibility <= 2**31 - 1:
            _raise_invalid_service_state()
        if compatibility == 1:
            if (
                value["bundle_sha256"] is not None
                or value["canonical_manifest_sha256"] is not None
            ):
                _raise_invalid_service_state()
        elif value["bundle_sha256"] is None or value["canonical_manifest_sha256"] is None:
            _raise_invalid_service_state()
        return value
    return _require_ledger(value)


def _require_ledger(value: Any) -> dict[str, Any]:
    legacy_keys = {
        "schema_version",
        "release_identity",
        "registry_digest",
        "framework_lock_sha256",
        "source_commit",
        "pid",
        "boot_id",
        "start_time_ticks",
        "port",
        "generation",
        "ready_sha256",
    }
    exact_keys = legacy_keys | {
        "state",
        "bundle_sha256",
        "canonical_manifest_sha256",
        "lifecycle_compatibility",
    }
    process_group_keys = {
        "application_pid",
        "application_boot_id",
        "application_start_time_ticks",
    }
    schema_version = value.get("schema_version") if isinstance(value, dict) else None
    if schema_version == 5:
        expected_keys = exact_keys | process_group_keys
    elif schema_version == 3:
        expected_keys = exact_keys
    else:
        expected_keys = legacy_keys
    if (
        not isinstance(value, dict)
        or set(value) != expected_keys
        or schema_version not in {1, 2, 3, 5}
        or (schema_version in {3, 5} and value.get("state") != "running")
    ):
        _raise_invalid_service_state()
    for key in (
        "release_identity",
        "registry_digest",
        "framework_lock_sha256",
        "ready_sha256",
    ):
        if not isinstance(value[key], str) or re.fullmatch(r"[0-9a-f]{64}", value[key]) is None:
            _raise_invalid_service_state()
    if schema_version in {3, 5}:
        for key in ("bundle_sha256", "canonical_manifest_sha256"):
            if (
                not isinstance(value.get(key), str)
                or re.fullmatch(r"[0-9a-f]{64}", value[key]) is None
            ):
                _raise_invalid_service_state()
        compatibility = value.get("lifecycle_compatibility")
        minimum_compatibility = 3 if schema_version == 5 else 2
        if (
            type(compatibility) is not int
            or not minimum_compatibility <= compatibility <= 2**31 - 1
        ):
            _raise_invalid_service_state()
    if (
        not isinstance(value["source_commit"], str)
        or re.fullmatch(r"[0-9a-f]{40}", value["source_commit"]) is None
        or not isinstance(value["boot_id"], str)
        or _BOOT_ID_PATTERN.fullmatch(value["boot_id"]) is None
        or not isinstance(value["generation"], str)
        or re.fullmatch(r"[0-9a-f]{32}", value["generation"]) is None
    ):
        _raise_invalid_service_state()
    _required_int(value, "pid", minimum=1)
    _required_int(value, "start_time_ticks", minimum=1)
    if schema_version == 5:
        if (
            not isinstance(value["application_boot_id"], str)
            or _BOOT_ID_PATTERN.fullmatch(value["application_boot_id"]) is None
        ):
            _raise_invalid_service_state()
        _required_int(value, "application_pid", minimum=1)
        _required_int(value, "application_start_time_ticks", minimum=1)
    _required_int(value, "port", minimum=1, maximum=65535)
    return value


def _raise_invalid_service_state() -> None:
    raise CoreServiceError(
        CoreServiceErrorCode.STATE_INVALID,
        "Core service lifecycle state is invalid.",
        retryable=False,
    )


def _is_exact_daemon_ledger(ledger: dict[str, Any]) -> bool:
    return ledger.get("schema_version") in {3, 5} and ledger.get("state") == "running"


def _floor_from_ledger(ledger: dict[str, Any]) -> dict[str, Any]:
    if _is_exact_daemon_ledger(ledger):
        return {
            "schema_version": 3,
            "state": "stopped",
            "release_identity": ledger["release_identity"],
            "bundle_sha256": ledger["bundle_sha256"],
            "canonical_manifest_sha256": ledger["canonical_manifest_sha256"],
            "lifecycle_compatibility": ledger["lifecycle_compatibility"],
        }
    return {
        "schema_version": 3,
        "state": "stopped",
        "release_identity": ledger["release_identity"],
        "bundle_sha256": None,
        "canonical_manifest_sha256": None,
        "lifecycle_compatibility": 1,
    }


def _require_floor_compatibility(
    floor: dict[str, Any],
    candidate: CoreDaemonBundleIdentity | None,
) -> None:
    if candidate is None:
        raise CoreServiceError(
            CoreServiceErrorCode.UPDATE_REQUIRED,
            "This service root requires a compatible OpenEvo Daemon bundle.",
            retryable=False,
        )
    floor_compatibility = floor["lifecycle_compatibility"]
    exact = (
        candidate.lifecycle_compatibility == floor_compatibility
        and candidate.bundle_sha256 == floor["bundle_sha256"]
        and candidate.canonical_manifest_sha256 == floor["canonical_manifest_sha256"]
    )
    if candidate.lifecycle_compatibility <= floor_compatibility and not exact:
        raise CoreServiceError(
            CoreServiceErrorCode.UPDATE_REQUIRED,
            "The OpenEvo Daemon bundle does not satisfy the persisted no-downgrade floor.",
            retryable=False,
        )


def _service_identity_matches(
    ledger: dict[str, Any],
    *,
    release: CoreReleaseIdentity,
    daemon_bundle_identity: CoreDaemonBundleIdentity | None,
) -> bool:
    if ledger["release_identity"] != release.digest:
        return False
    if daemon_bundle_identity is None:
        return not _is_exact_daemon_ledger(ledger)
    return (
        _is_exact_daemon_ledger(ledger)
        and ledger["bundle_sha256"] == daemon_bundle_identity.bundle_sha256
        and ledger["canonical_manifest_sha256"] == daemon_bundle_identity.canonical_manifest_sha256
        and ledger["lifecycle_compatibility"] == daemon_bundle_identity.lifecycle_compatibility
    )


def _process_from_ledger(ledger: dict[str, Any]) -> ProcessIdentity:
    return ProcessIdentity(
        pid=ledger["pid"],
        boot_id=ledger["boot_id"],
        start_time_ticks=ledger["start_time_ticks"],
    )


def _service_process_group_from_ledger(
    ledger: dict[str, Any],
) -> _ServiceProcessGroup:
    launcher = _process_from_ledger(ledger)
    if ledger["schema_version"] != 5:
        return _ServiceProcessGroup(
            launcher=launcher,
            application=launcher,
        )
    return _ServiceProcessGroup(
        launcher=launcher,
        application=ProcessIdentity(
            pid=ledger["application_pid"],
            boot_id=ledger["application_boot_id"],
            start_time_ticks=ledger["application_start_time_ticks"],
        ),
    )


def _service_process_group_is_alive(
    process_group: _ServiceProcessGroup,
    controller: LinuxProcessController,
) -> bool:
    return all(controller.is_alive(identity) for identity in process_group.identities)


def _terminate_service_process_group(
    process_group: _ServiceProcessGroup,
    controller: LinuxProcessController,
    *,
    deadline: float,
) -> None:
    if controller.is_alive(process_group.application):
        controller.terminate(process_group.application, deadline=deadline)
    if (
        process_group.launcher != process_group.application
        and controller.is_alive(process_group.launcher)
        and not _wait_for_natural_launcher_exit(
            process_group.launcher,
            controller,
            deadline=deadline,
        )
    ):
        controller.terminate(process_group.launcher, deadline=deadline)
    if any(controller.is_alive(identity) for identity in process_group.identities):
        raise CoreServiceError(
            CoreServiceErrorCode.DEADLINE_EXCEEDED,
            "Core service process group did not stop before the deadline.",
            retryable=True,
        )


def _wait_for_natural_launcher_exit(
    launcher: ProcessIdentity,
    controller: LinuxProcessController,
    *,
    deadline: float,
) -> bool:
    cleanup_deadline = min(
        deadline,
        time.monotonic() + _ONEFILE_LAUNCHER_CLEANUP_SECONDS,
    )
    return controller.wait_for_exit(launcher, deadline=cleanup_deadline)


def _release_from_ledger(ledger: dict[str, Any]) -> CoreReleaseIdentity:
    return CoreReleaseIdentity(
        digest=ledger["release_identity"],
        registry_digest=ledger["registry_digest"],
        framework_lock_sha256=ledger["framework_lock_sha256"],
        source_commit=ledger["source_commit"],
    )


def _attachment_from_ledger(
    ledger: dict[str, Any],
    *,
    bearer: str,
    status_proof: str,
    attached: bool,
) -> CoreServiceAttachment:
    return CoreServiceAttachment(
        port=ledger["port"],
        release_identity=ledger["release_identity"],
        registry_digest=ledger["registry_digest"],
        source_commit=ledger["source_commit"],
        generation=ledger["generation"],
        status_proof=status_proof,
        attached=attached,
        _bearer=SecretStr(bearer),
        bundle_sha256=ledger.get("bundle_sha256"),
        canonical_manifest_sha256=ledger.get("canonical_manifest_sha256"),
        lifecycle_compatibility=ledger.get("lifecycle_compatibility"),
    )


def _required_int(
    value: dict[str, Any],
    key: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    item = value.get(key)
    if type(item) is not int or item < minimum or (maximum is not None and item > maximum):
        raise CoreServiceError(
            CoreServiceErrorCode.STATE_INVALID,
            "Core service lifecycle state is invalid.",
            retryable=False,
        )
    return item


def _open_log(root: HostServiceRoot) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
    fd = os.open("service.log", flags, 0o600, dir_fd=root.fd)
    metadata = os.fstat(fd)
    if (
        not stat_is_regular(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or (metadata.st_mode & 0o777) != 0o600
    ):
        os.close(fd)
        raise CoreServiceError(
            CoreServiceErrorCode.STATE_INVALID,
            "Core service log metadata is invalid.",
            retryable=False,
        )
    pathname = os.stat("service.log", dir_fd=root.fd, follow_symlinks=False)
    if (pathname.st_dev, pathname.st_ino) != (metadata.st_dev, metadata.st_ino):
        os.close(fd)
        raise CoreServiceError(
            CoreServiceErrorCode.STATE_INVALID,
            "Core service log pathname binding is invalid.",
            retryable=False,
        )
    os.ftruncate(fd, 0)
    os.fsync(fd)
    return fd


def stat_is_regular(mode: int) -> bool:
    return (mode & 0o170000) == 0o100000


def _flock_until(fd: int, deadline: float) -> None:
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            _require_time(deadline)
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


def _require_time(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise CoreServiceError(
            CoreServiceErrorCode.DEADLINE_EXCEEDED,
            "Core service operation exceeded its deadline.",
            retryable=True,
        )


def _run_private_command(
    argv: list[str],
    *,
    deadline: float,
    pass_fds: tuple[int, ...] = (),
) -> int:
    _require_time(deadline)
    child_environment = os.environ.copy()
    child_environment.pop("PYTHONPATH", None)
    try:
        completed = subprocess.run(
            argv,
            env=child_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            close_fds=True,
            pass_fds=pass_fds,
            timeout=max(0.001, deadline - time.monotonic()),
        )
    except subprocess.TimeoutExpired as exc:
        raise CoreServiceError(
            CoreServiceErrorCode.DEADLINE_EXCEEDED,
            "Core bootstrap exceeded its total deadline.",
            retryable=True,
        ) from exc
    except OSError as exc:
        raise CoreServiceError(
            CoreServiceErrorCode.START_FAILED,
            "Core bootstrap process could not be started.",
            retryable=True,
        ) from exc
    return completed.returncode


def _require_inherited_lock(root: HostServiceRoot, name: str, lock_fd: int) -> None:
    if lock_fd < 3:
        raise CoreServiceError(
            CoreServiceErrorCode.STATE_INVALID,
            "Core host lock descriptor is invalid.",
            retryable=False,
        )
    try:
        lock_metadata = os.fstat(lock_fd)
        pathname_metadata = os.stat(name, dir_fd=root.fd, follow_symlinks=False)
        if (
            not stat_is_regular(lock_metadata.st_mode)
            or lock_metadata.st_uid != os.geteuid()
            or (lock_metadata.st_mode & 0o777) != 0o600
            or (lock_metadata.st_dev, lock_metadata.st_ino)
            != (pathname_metadata.st_dev, pathname_metadata.st_ino)
        ):
            raise OSError("lock identity mismatch")
        probe_fd = root.open_lock(name)
        try:
            try:
                fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            raise OSError("lock is not held")
        finally:
            os.close(probe_fd)
    except OSError as exc:
        raise CoreServiceError(
            CoreServiceErrorCode.STATE_INVALID,
            "Core inherited host lock is invalid.",
            retryable=False,
        ) from exc


def _validate_attachment_name(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"bootstrap-[0-9a-f]{32}\.json", value) is None:
        raise CoreServiceError(
            CoreServiceErrorCode.STATE_INVALID,
            "Core bootstrap attachment name is invalid.",
            retryable=False,
        )


def _terminate_spawned_child(child: subprocess.Popen[bytes]) -> bool:
    try:
        if child.poll() is not None:
            return True
    except BaseException:
        pass
    try:
        child.terminate()
    except BaseException:
        pass
    try:
        child.wait(timeout=5)
        return True
    except BaseException:
        pass
    try:
        child.kill()
    except BaseException:
        pass
    try:
        child.wait(timeout=5)
        return True
    except BaseException:
        try:
            return child.poll() is not None
        except BaseException:
            return False


def _retain_orphaned_service_child(child: subprocess.Popen[bytes]) -> None:
    with _ORPHANED_SERVICE_CHILDREN_GUARD:
        _ORPHANED_SERVICE_CHILDREN[id(child)] = child


def _retry_orphaned_service_children() -> None:
    with _ORPHANED_SERVICE_CHILDREN_GUARD:
        children = tuple(_ORPHANED_SERVICE_CHILDREN.values())
    for child in children:
        if _terminate_spawned_child(child):
            with _ORPHANED_SERVICE_CHILDREN_GUARD:
                _ORPHANED_SERVICE_CHILDREN.pop(id(child), None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openevo-core-service")
    subparsers = parser.add_subparsers(dest="command", required=True)
    ensure = subparsers.add_parser("ensure")
    ensure.add_argument("--service-root", type=Path, default=default_core_service_root())
    ensure.add_argument("--framework-lock", type=Path, required=True)
    ensure.add_argument("--source-commit", required=True)
    ensure.add_argument("--port", type=int, default=0)
    ensure.add_argument("--deadline-seconds", type=float, default=45.0)
    ensure.add_argument("--replace-mismatched", action="store_true")
    ensure.add_argument("--attachment-name")
    ensure.add_argument("--bootstrap-lock-fd", type=int)
    bootstrap = subparsers.add_parser("bootstrap")
    bootstrap.add_argument("--service-root", type=Path, default=default_core_service_root())
    bootstrap.add_argument("--wheel-path", type=Path, required=True)
    bootstrap.add_argument("--framework-lock", type=Path, required=True)
    bootstrap.add_argument("--source-commit", required=True)
    bootstrap.add_argument("--attachment-name", required=True)
    bootstrap.add_argument("--install-generation", required=True)
    bootstrap.add_argument("--port", type=int, default=0)
    bootstrap.add_argument("--deadline-seconds", type=float, default=90.0)
    bootstrap.add_argument("--replace-mismatched", action="store_true")
    consume = subparsers.add_parser("consume-attachment")
    consume.add_argument("--service-root", type=Path, default=default_core_service_root())
    consume.add_argument("--attachment-name", required=True)
    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("--service-root", type=Path, default=default_core_service_root())
    stop = subparsers.add_parser("stop")
    stop.add_argument("--service-root", type=Path, default=default_core_service_root())
    stop.add_argument("--deadline-seconds", type=float, default=15.0)
    return parser


def _attachment_metadata(attachment: CoreServiceAttachment) -> dict[str, object]:
    return {
        "schema_version": 1,
        "host": "127.0.0.1",
        "port": attachment.port,
        "release_identity": attachment.release_identity,
        "registry_digest": attachment.registry_digest,
        "source_commit": attachment.source_commit,
        "generation": attachment.generation,
        "status_proof": attachment.status_proof,
        "attached": attachment.attached,
    }


def _bootstrap_payload(attachment: CoreServiceAttachment) -> dict[str, object]:
    payload = _attachment_metadata(attachment)
    payload["execution_mode"] = "subscription"
    payload["capture_mode"] = "transcript"
    payload["bearer_token"] = attachment.bearer_token
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "ensure":
            attachment = ensure_core_service(
                service_root=args.service_root,
                framework_lock=args.framework_lock,
                source_commit=args.source_commit,
                port=args.port,
                deadline_seconds=args.deadline_seconds,
                replace_mismatched=args.replace_mismatched,
                _bootstrap_lock_fd=args.bootstrap_lock_fd,
            )
            if args.attachment_name is not None:
                _validate_attachment_name(args.attachment_name)
                with HostServiceRoot(args.service_root, create=False) as root:
                    root.atomic_write_json(
                        args.attachment_name,
                        _bootstrap_payload(attachment),
                        replace=False,
                    )
            print(
                json.dumps(
                    _attachment_metadata(attachment),
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "bootstrap":
            bootstrap_core_service(
                service_root=args.service_root,
                wheel_path=args.wheel_path,
                framework_lock=args.framework_lock,
                source_commit=args.source_commit,
                attachment_name=args.attachment_name,
                install_generation=args.install_generation,
                port=args.port,
                deadline_seconds=args.deadline_seconds,
                replace_mismatched=args.replace_mismatched,
            )
            print('{"schema_version":1,"bootstrapped":true}')
            return 0
        if args.command == "consume-attachment":
            payload = consume_core_service_attachment(
                service_root=args.service_root,
                attachment_name=args.attachment_name,
            )
            sys.stdout.buffer.write(payload)
            return 0
        if args.command == "inspect":
            attachment = inspect_core_service(service_root=args.service_root)
            print(
                json.dumps(
                    _attachment_metadata(attachment),
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "stop":
            stop_core_service(
                service_root=args.service_root,
                deadline_seconds=args.deadline_seconds,
            )
            print('{"schema_version":1,"stopped":true}')
            return 0
    except CoreServiceError as exc:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "code": exc.code.value,
                    "message": str(exc),
                    "retryable": exc.retryable,
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except Exception:
        print(
            '{"code":"core_service_internal_error","message":"Core service operation '
            'failed.","retryable":true,"schema_version":1}',
            file=sys.stderr,
        )
        return 2
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "authenticate_core_service_endpoint",
    "bootstrap_core_service",
    "claim_core_service_spawn",
    "consume_core_service_attachment",
    "CoreServiceAttachment",
    "CoreDaemonBundleIdentity",
    "CoreServiceError",
    "CoreServiceErrorCode",
    "CoreServicePredecessor",
    "LinuxProcessController",
    "LockIdentity",
    "ProcessIdentity",
    "V2_DAEMON_LIFECYCLE_COMPATIBILITY",
    "ensure_core_service",
    "inspect_core_service",
    "observe_core_service_predecessor",
    "stop_core_service",
    "stop_core_service_if_generation",
]
