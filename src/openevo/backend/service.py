from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from enum import StrEnum
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
import subprocess
import sys
import time
from typing import Any, Sequence

from pydantic import SecretStr

from openevo.backend.runtime_identity import (
    CoreReleaseIdentity,
    HostServiceRoot,
    RuntimeIdentityError,
    canonical_json_bytes,
    compute_release_identity,
    default_core_service_root,
    load_bounded_json,
    load_or_create_core_bearer_token,
    require_host_global_service_root,
    rotate_core_bearer_token,
)
from openevo.evolution.framework import load_verified_framework_registry


_BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
_BOOT_ID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_MAX_READY_BYTES = 4096
_MAX_HTTP_BYTES = 64 * 1024
_LEDGER_NAME = "service.json"
_READY_NAME = "ready.json"
_PENDING_NAME = "pending.json"


class CoreServiceErrorCode(StrEnum):
    INVALID_ROOT = "core_service_root_invalid"
    IDENTITY_MISMATCH = "core_service_identity_mismatch"
    PORT_UNAVAILABLE = "core_service_port_unavailable"
    START_FAILED = "core_service_start_failed"
    STATUS_INVALID = "core_service_status_invalid"
    DEADLINE_EXCEEDED = "core_service_deadline_exceeded"
    STATE_INVALID = "core_service_state_invalid"


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
class CoreServiceAttachment:
    port: int
    release_identity: str
    registry_digest: str
    generation: str
    status_proof: str
    attached: bool
    _bearer: SecretStr = field(repr=False, compare=False)

    @property
    def bearer_token(self) -> str:
        return self._bearer.get_secret_value()


class LinuxProcessController:
    def __init__(self) -> None:
        if (
            sys.platform != "linux"
            or not hasattr(os, "pidfd_open")
            or not hasattr(signal, "pidfd_send_signal")
        ):
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
        probe = os.pidfd_open(os.getpid(), 0)
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

    def terminate(self, identity: ProcessIdentity, *, deadline: float) -> None:
        try:
            pid_fd = os.pidfd_open(identity.pid, 0)
        except ProcessLookupError:
            return
        try:
            try:
                if self.capture(identity.pid) != identity:
                    return
            except ProcessLookupError:
                return
            signal.pidfd_send_signal(pid_fd, signal.SIGTERM)
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


def ensure_core_service(
    *,
    service_root: str | Path,
    framework_lock: str | Path,
    source_commit: str,
    port: int = 0,
    deadline_seconds: float = 45.0,
    replace_mismatched: bool = False,
    process_controller: LinuxProcessController | None = None,
) -> CoreServiceAttachment:
    if not 0 <= port <= 65535 or deadline_seconds <= 0 or deadline_seconds > 300:
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
        registry = load_verified_framework_registry(framework_lock)
        release = compute_release_identity(
            framework_lock=framework_lock,
            registry=registry,
            source_commit=source_commit,
        )
        bearer = load_or_create_core_bearer_token(root)
        lock_fd = root.open_lock("lifecycle.lock")
        try:
            _flock_until(lock_fd, deadline)
            return _ensure_locked(
                root=root,
                framework_lock=Path(framework_lock),
                release=release,
                bearer=bearer,
                port=port,
                deadline=deadline,
                replace_mismatched=replace_mismatched,
                controller=controller,
            )
        finally:
            os.close(lock_fd)


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
        identity = _process_from_ledger(ledger)
        if not controller.is_alive(identity):
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
            deadline=time.monotonic() + 5.0,
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
) -> None:
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
        lock_fd = root.open_lock("lifecycle.lock")
        try:
            _flock_until(lock_fd, deadline)
            ledger = root.read_optional_json(_LEDGER_NAME)
            if ledger is not None:
                identity = _process_from_ledger(_require_ledger(ledger))
                if controller.is_alive(identity):
                    controller.terminate(identity, deadline=deadline)
            else:
                _recover_pending(root, controller=controller, deadline=deadline)
            root.unlink_regular(_LEDGER_NAME)
            root.unlink_regular(_READY_NAME)
            root.unlink_regular(_PENDING_NAME)
        finally:
            os.close(lock_fd)


def _ensure_locked(
    *,
    root: HostServiceRoot,
    framework_lock: Path,
    release: CoreReleaseIdentity,
    bearer: str,
    port: int,
    deadline: float,
    replace_mismatched: bool,
    controller: LinuxProcessController,
) -> CoreServiceAttachment:
    existing_value = root.read_optional_json(_LEDGER_NAME)
    if existing_value is None:
        _recover_pending(root, controller=controller, deadline=deadline)
    if existing_value is not None:
        ledger = _require_ledger(existing_value)
        process = _process_from_ledger(ledger)
        alive = controller.is_alive(process)
        matching = ledger["release_identity"] == release.digest
        if alive and matching:
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
                deadline=deadline,
            )
            _verify_ready_ledger(root, ledger, proof)
            root.unlink_regular(_PENDING_NAME)
            return _attachment_from_ledger(
                ledger,
                bearer=bearer,
                status_proof=proof,
                attached=True,
            )
        if not matching:
            if alive and not replace_mismatched:
                raise CoreServiceError(
                    CoreServiceErrorCode.IDENTITY_MISMATCH,
                    "A different verified Core release is already running.",
                    retryable=False,
                )
            if alive:
                controller.terminate(process, deadline=deadline)
            bearer = rotate_core_bearer_token(root)
        root.unlink_regular(_LEDGER_NAME)
        root.unlink_regular(_READY_NAME)
        root.unlink_regular(_PENDING_NAME)

    _require_time(deadline)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    child: subprocess.Popen[bytes] | None = None
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
            "--expected-release-identity",
            release.digest,
            "--generation",
            generation,
        ]
        try:
            child = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=log_fd,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
                pass_fds=(listener.fileno(), ready_write),
            )
        finally:
            os.close(log_fd)
            os.close(ready_write)
        process = controller.capture(child.pid)
        if not controller.is_alive(process):
            raise CoreServiceError(
                CoreServiceErrorCode.START_FAILED,
                "Core service exited during startup.",
                retryable=True,
            )
        root.atomic_write_json(
            _PENDING_NAME,
            {
                "schema_version": 1,
                "release_identity": release.digest,
                "pid": process.pid,
                "boot_id": process.boot_id,
                "start_time_ticks": process.start_time_ticks,
                "port": actual_port,
                "generation": generation,
            },
            replace=False,
        )
        try:
            ready = _wait_ready(ready_read, child, deadline=deadline)
        finally:
            os.close(ready_read)
        if ready != {
            "schema_version": 1,
            "generation": generation,
            "release_identity": release.digest,
            "registry_digest": release.registry_digest,
        }:
            raise CoreServiceError(
                CoreServiceErrorCode.START_FAILED,
                "Core service returned an invalid readiness proof.",
                retryable=True,
            )
        if not controller.is_alive(process):
            raise CoreServiceError(
                CoreServiceErrorCode.START_FAILED,
                "Core service exited during startup.",
                retryable=True,
            )
        proof = _authenticated_status_proof(
            port=actual_port,
            bearer=bearer,
            release=release,
            deadline=deadline,
        )
        ready_ledger = {
            "schema_version": 1,
            "generation": generation,
            "release_identity": release.digest,
            "registry_digest": release.registry_digest,
            "status_proof": proof,
        }
        ready_digest = hashlib.sha256(canonical_json_bytes(ready_ledger)).hexdigest()
        ledger = {
            "schema_version": 1,
            "release_identity": release.digest,
            "registry_digest": release.registry_digest,
            "framework_lock_sha256": release.framework_lock_sha256,
            "source_commit": release.source_commit,
            "pid": process.pid,
            "boot_id": process.boot_id,
            "start_time_ticks": process.start_time_ticks,
            "port": actual_port,
            "generation": generation,
            "ready_sha256": ready_digest,
        }
        root.atomic_write_json(_READY_NAME, ready_ledger, replace=False)
        root.atomic_write_json(_LEDGER_NAME, ledger, replace=False)
        root.unlink_regular(_PENDING_NAME)
        if not controller.is_alive(process):
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
        if child is not None:
            _terminate_spawned_child(child)
        try:
            root.unlink_regular(_LEDGER_NAME)
            root.unlink_regular(_READY_NAME)
            root.unlink_regular(_PENDING_NAME)
        except Exception:
            pass
        raise
    finally:
        listener.close()


def _authenticated_status_proof(
    *,
    port: int,
    bearer: str,
    release: CoreReleaseIdentity,
    deadline: float,
) -> str:
    version = _fetch_json(port, "/version", bearer=None, deadline=deadline)
    status = _fetch_json(port, "/v1/status", bearer=bearer, deadline=deadline)
    if (
        not isinstance(version, dict)
        or version.get("provider_kind") != "openevo_core"
        or version.get("build_channel") != "release"
        or version.get("source_commit") != release.source_commit
        or not isinstance(status, dict)
        or status.get("registry_status") != "verified"
        or status.get("registry_digest") != release.registry_digest
    ):
        raise CoreServiceError(
            CoreServiceErrorCode.STATUS_INVALID,
            "Core service identity proof did not match the verified release.",
            retryable=False,
        )
    material = canonical_json_bytes(
        {
            "schema_version": 1,
            "release_identity": release.digest,
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
    port: int,
    path: str,
    *,
    bearer: str | None,
    deadline: float,
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
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            port,
            timeout=min(1.0, remaining),
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
                return load_bounded_json(payload, max_bytes=_MAX_HTTP_BYTES)
            except RuntimeIdentityError as exc:
                raise CoreServiceError(
                    CoreServiceErrorCode.STATUS_INVALID,
                    "Core service status response is invalid.",
                    retryable=False,
                ) from exc
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
    if not isinstance(ready, dict) or set(ready) != {
        "schema_version",
        "generation",
        "release_identity",
        "registry_digest",
        "status_proof",
    }:
        raise CoreServiceError(
            CoreServiceErrorCode.STATE_INVALID,
            "Core service readiness state is invalid.",
            retryable=False,
        )
    digest = hashlib.sha256(canonical_json_bytes(ready)).hexdigest()
    if (
        ready.get("schema_version") != 1
        or ready.get("generation") != ledger["generation"]
        or ready.get("release_identity") != ledger["release_identity"]
        or ready.get("registry_digest") != ledger["registry_digest"]
        or ready.get("status_proof") != proof
        or digest != ledger["ready_sha256"]
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
) -> None:
    value = root.read_optional_json(_PENDING_NAME, max_bytes=_MAX_READY_BYTES)
    if value is None:
        root.unlink_regular(_READY_NAME)
        return
    expected = {
        "schema_version",
        "release_identity",
        "pid",
        "boot_id",
        "start_time_ticks",
        "port",
        "generation",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or value.get("schema_version") != 1
        or not isinstance(value.get("release_identity"), str)
        or re.fullmatch(r"[0-9a-f]{64}", value["release_identity"]) is None
        or not isinstance(value.get("boot_id"), str)
        or _BOOT_ID_PATTERN.fullmatch(value["boot_id"]) is None
        or not isinstance(value.get("generation"), str)
        or re.fullmatch(r"[0-9a-f]{32}", value["generation"]) is None
    ):
        raise CoreServiceError(
            CoreServiceErrorCode.STATE_INVALID,
            "Core service pending state is invalid.",
            retryable=False,
        )
    _required_int(value, "pid", minimum=1)
    _required_int(value, "start_time_ticks", minimum=1)
    _required_int(value, "port", minimum=1, maximum=65535)
    identity = ProcessIdentity(
        pid=value["pid"],
        boot_id=value["boot_id"],
        start_time_ticks=value["start_time_ticks"],
    )
    if controller.is_alive(identity):
        controller.terminate(identity, deadline=deadline)
    root.unlink_regular(_PENDING_NAME)
    root.unlink_regular(_READY_NAME)


def _read_ledger(root: HostServiceRoot) -> dict[str, Any]:
    try:
        return _require_ledger(root.read_json(_LEDGER_NAME))
    except FileNotFoundError as exc:
        raise CoreServiceError(
            CoreServiceErrorCode.STATUS_INVALID,
            "Core service is not running.",
            retryable=True,
        ) from exc


def _require_ledger(value: Any) -> dict[str, Any]:
    keys = {
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
    if not isinstance(value, dict) or set(value) != keys or value.get("schema_version") != 1:
        raise CoreServiceError(
            CoreServiceErrorCode.STATE_INVALID,
            "Core service lifecycle state is invalid.",
            retryable=False,
        )
    for key in (
        "release_identity",
        "registry_digest",
        "framework_lock_sha256",
        "ready_sha256",
    ):
        if not isinstance(value[key], str) or re.fullmatch(r"[0-9a-f]{64}", value[key]) is None:
            raise CoreServiceError(
                CoreServiceErrorCode.STATE_INVALID,
                "Core service lifecycle state is invalid.",
                retryable=False,
            )
    if (
        not isinstance(value["source_commit"], str)
        or re.fullmatch(r"[0-9a-f]{40}", value["source_commit"]) is None
        or not isinstance(value["boot_id"], str)
        or _BOOT_ID_PATTERN.fullmatch(value["boot_id"]) is None
        or not isinstance(value["generation"], str)
        or re.fullmatch(r"[0-9a-f]{32}", value["generation"]) is None
    ):
        raise CoreServiceError(
            CoreServiceErrorCode.STATE_INVALID,
            "Core service lifecycle state is invalid.",
            retryable=False,
        )
    _required_int(value, "pid", minimum=1)
    _required_int(value, "start_time_ticks", minimum=1)
    _required_int(value, "port", minimum=1, maximum=65535)
    return value


def _process_from_ledger(ledger: dict[str, Any]) -> ProcessIdentity:
    return ProcessIdentity(
        pid=ledger["pid"],
        boot_id=ledger["boot_id"],
        start_time_ticks=ledger["start_time_ticks"],
    )


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
        generation=ledger["generation"],
        status_proof=status_proof,
        attached=attached,
        _bearer=SecretStr(bearer),
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


def _terminate_spawned_child(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)


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
    ensure.add_argument("--bootstrap-json", action="store_true")
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
            )
            payload = (
                _bootstrap_payload(attachment)
                if args.bootstrap_json
                else _attachment_metadata(attachment)
            )
            print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
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
    "CoreServiceAttachment",
    "CoreServiceError",
    "CoreServiceErrorCode",
    "LinuxProcessController",
    "ProcessIdentity",
    "ensure_core_service",
    "inspect_core_service",
    "stop_core_service",
]
