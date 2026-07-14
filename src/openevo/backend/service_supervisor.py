"""Core-owned lifecycle supervision for internal runtime services.

This module is deliberately independent from the frozen Core provider.  It owns
processes and private state; provider integration is a later dependency-injection
step.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import ctypes
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from openevo.backend.contracts.v1.models import (
    ApiErrorV1,
    ErrorCategory,
    ErrorSeverity,
    LogEntryV1,
    LogLevel,
    LogStream,
    ModelPreparationStatus,
    ModelPreparationV1,
    RepairAction,
    ServiceKind,
    ServiceStatus as ContractServiceStatus,
    ServiceSummaryV1,
)
from openevo.projects.science.compiler import MANAGED_RUNTIME_IMAGES


_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_SECRET_RE = re.compile(
    r"(?i)(authorization|proxy-authorization)\s*[:=]\s*[^\r\n]+"
    r"|bearer\s+[A-Za-z0-9._~+/=-]+"
    r"|(?:(?:token|password|passwd|secret|api[_-]?key)\s*[:=]\s*)[^\s,;]+"
)
_URL_CREDENTIAL_RE = re.compile(r"(?i)(https?://)([^/@\s]+)@")
_URL_QUERY_RE = re.compile(r"(?i)(https?://[^\s?#]+)[?#][^\s]+")
_ABSOLUTE_PATH_RE = re.compile(r"(?<![A-Za-z0-9_.-])/(?:[^\s/:]+/)+[^\s,:;]+")
_MAX_LEDGER_BYTES = 1 * 1024 * 1024
_MAX_FRAMEWORK_LOCK_BYTES = 4 * 1024 * 1024
_ROOT_MODE = 0o700
_FILE_MODE = 0o600


def _state_digest(value: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError("digest must be a lowercase SHA-256 digest")
    return value


class SupervisorError(RuntimeError):
    """Base class for internal supervisor failures."""


class SupervisorStateError(SupervisorError):
    """Private service state is unsafe, corrupt, or replaced."""


class SupervisorBusyError(SupervisorError):
    """Another Core daemon owns the host-global service root."""


class ServiceExecutionMode(StrEnum):
    CODEX_SUBSCRIPTION_TRANSCRIPT = "codex_subscription_transcript"
    SELF_DEPLOYED = "self-deployed"


class ServiceComponent(StrEnum):
    EVOLUTION_BACKEND = "evolution_backend"
    ROLLOUT = "rollout"
    GATEWAY = "gateway"
    EVOLUTION_WORKER = "evolution_worker"
    INFERENCE = "inference"


class ServiceStatus(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class HealthProbeKind(StrEnum):
    HTTP = "http"
    PROCESS = "process"


@dataclass(frozen=True, slots=True)
class ServiceReleaseIdentity:
    """Digests issued by the verified install/registry owner.

    The explicit value form is retained for tests and dependency injection.  A
    production owner should use :func:`release_identity_from_verified_registry`.
    """

    install_digest: str
    registry_digest: str

    def __post_init__(self) -> None:
        _require_digest(self.install_digest, "install_digest")
        _require_digest(self.registry_digest, "registry_digest")


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    birth_token: str

    def __post_init__(self) -> None:
        if self.pid <= 0:
            raise ValueError("pid must be positive")
        _require_digest(self.birth_token, "birth_token")


@dataclass(frozen=True, slots=True)
class ServiceHealthProbe:
    kind: HealthProbeKind
    url: str | None = None

    @classmethod
    def process(cls) -> ServiceHealthProbe:
        return cls(kind=HealthProbeKind.PROCESS)

    @classmethod
    def http(cls, url: str) -> ServiceHealthProbe:
        if not url.startswith("http://127.0.0.1:"):
            raise ValueError("service health URLs must use loopback HTTP")
        return cls(kind=HealthProbeKind.HTTP, url=url)


@dataclass(frozen=True, slots=True)
class ServiceProcessSpec:
    service_id: str
    display_name: str
    component: ServiceComponent
    argv: tuple[str, ...]
    env: Mapping[str, str]
    argv_digest: str
    env_digest: str
    identity_digest: str
    port: int | None
    health_probe: ServiceHealthProbe

    def __post_init__(self) -> None:
        if not self.service_id or not self.display_name or not self.argv:
            raise ValueError("service process identity fields must not be empty")
        if any(not isinstance(part, str) or not part for part in self.argv):
            raise ValueError("service argv must contain non-empty strings")
        for value, name in (
            (self.argv_digest, "argv_digest"),
            (self.env_digest, "env_digest"),
            (self.identity_digest, "identity_digest"),
        ):
            _require_digest(value, name)
        if self.port is not None and not 1 <= self.port <= 65535:
            raise ValueError("service port is outside the TCP range")


@dataclass(frozen=True, slots=True)
class HealthCheckResult:
    ready: bool
    message: str


@dataclass(frozen=True, slots=True)
class ManagedScienceRuntimeRequest:
    runtime_image: str
    codex_model: str

    def __post_init__(self) -> None:
        if self.runtime_image not in set(MANAGED_RUNTIME_IMAGES.values()):
            raise ValueError("subscription runtime_image is not a managed Science image")
        model = self.codex_model.strip()
        if not model or len(model) > 256 or any(ord(char) < 0x20 for char in model):
            raise ValueError("subscription codex_model is invalid")
        object.__setattr__(self, "codex_model", model)


@dataclass(frozen=True, slots=True)
class ManagedScienceRuntimeReadiness:
    ready: bool
    identity_digest: str | None
    message: str

    def __post_init__(self) -> None:
        if self.ready != (self.identity_digest is not None):
            raise ValueError("ready managed runtime evidence requires an identity digest")
        if self.identity_digest is not None:
            _require_digest(self.identity_digest, "managed runtime identity_digest")
        if not self.message.strip() or len(self.message) > 256:
            raise ValueError("managed runtime readiness message is invalid")


@dataclass(frozen=True, slots=True)
class ProbeCommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class ProbeCommandRunner(Protocol):
    def run(self, argv: tuple[str, ...], deadline: float) -> ProbeCommandResult: ...


class ManagedScienceRuntimeProbe(Protocol):
    def verify(
        self,
        request: ManagedScienceRuntimeRequest,
        deadline: float,
    ) -> ManagedScienceRuntimeReadiness: ...


class ProcessBackend(Protocol):
    def spawn(
        self,
        spec: ServiceProcessSpec,
        on_output: Callable[[bytes], None],
        on_exit: Callable[[int], None],
    ) -> ProcessIdentity: ...

    def is_alive(self, identity: ProcessIdentity) -> bool: ...

    def terminate(self, identity: ProcessIdentity) -> None: ...

    def kill(self, identity: ProcessIdentity) -> None: ...

    def wait(self, identity: ProcessIdentity, timeout: float | None) -> int | None: ...


class HealthChecker(Protocol):
    def wait_ready(
        self,
        spec: ServiceProcessSpec,
        identity: ProcessIdentity,
        process_backend: ProcessBackend,
        deadline: float,
    ) -> HealthCheckResult: ...


class PortProbe(Protocol):
    def is_available(self, host: str, port: int) -> bool: ...


class BoundedProbeCommandRunner:
    def __init__(self, *, max_output_bytes: int = 1 * 1024 * 1024) -> None:
        self._max_output_bytes = max_output_bytes

    def run(self, argv: tuple[str, ...], deadline: float) -> ProbeCommandResult:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ProbeCommandResult(124, b"", b"bootstrap probe deadline exceeded")
        try:
            completed = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_controlled_environment(),
                check=False,
                timeout=remaining,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ProbeCommandResult(
                124,
                b"",
                str(exc).encode("utf-8", errors="replace"),
            )
        stdout = completed.stdout[: self._max_output_bytes + 1]
        stderr = completed.stderr[: self._max_output_bytes + 1]
        if len(stdout) > self._max_output_bytes or len(stderr) > self._max_output_bytes:
            return ProbeCommandResult(125, b"", b"bootstrap probe output exceeded its limit")
        return ProbeCommandResult(completed.returncode, stdout, stderr)


class LocalManagedScienceRuntimeProbe:
    """Verify the managed image and Codex auth produced by existing bootstrap."""

    def __init__(
        self,
        *,
        command_runner: ProbeCommandRunner | None = None,
        codex_auth_path: Path | None = None,
    ) -> None:
        self._command_runner = command_runner or BoundedProbeCommandRunner()
        self._codex_auth_path = codex_auth_path or (Path.home() / ".codex" / "auth.json")

    def verify(
        self,
        request: ManagedScienceRuntimeRequest,
        deadline: float,
    ) -> ManagedScienceRuntimeReadiness:
        codex = self._command_runner.run(("codex", "--version"), deadline)
        if codex.returncode != 0:
            return ManagedScienceRuntimeReadiness(
                ready=False,
                identity_digest=None,
                message="Codex CLI is unavailable at the managed Science bootstrap boundary.",
            )
        docker = self._command_runner.run(
            ("docker", "image", "inspect", request.runtime_image),
            deadline,
        )
        if docker.returncode != 0:
            return ManagedScienceRuntimeReadiness(
                ready=False,
                identity_digest=None,
                message="Managed Science runtime image is not prepared.",
            )
        try:
            image_payload = json.loads(docker.stdout.decode("utf-8"))
            if not isinstance(image_payload, list) or len(image_payload) != 1:
                raise ValueError("Docker inspect returned an unexpected image set")
            image = image_payload[0]
            if not isinstance(image, dict):
                raise ValueError("Docker inspect image is not an object")
            image_id = image.get("Id")
            config = image.get("Config")
            labels = config.get("Labels") if isinstance(config, dict) else None
            if (
                not isinstance(image_id, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None
                or not isinstance(labels, dict)
                or labels.get("io.openevo.managed-runtime") != "true"
            ):
                raise ValueError("Docker image lacks managed runtime identity")
            auth_identity = _private_file_metadata_identity(self._codex_auth_path)
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            SupervisorStateError,
            ValueError,
        ) as exc:
            return ManagedScienceRuntimeReadiness(
                ready=False,
                identity_digest=None,
                message=_safe_message("Managed Science bootstrap evidence is invalid", exc),
            )
        return ManagedScienceRuntimeReadiness(
            ready=True,
            identity_digest=_digest_json(
                {
                    "auth_identity": auth_identity,
                    "codex_model": request.codex_model,
                    "codex_version_output_digest": hashlib.sha256(codex.stdout).hexdigest(),
                    "runtime_image": request.runtime_image,
                    "runtime_image_id": image_id,
                }
            ),
            message="Managed Science runtime bootstrap is verified.",
        )


@dataclass(frozen=True, slots=True)
class SupervisorLogEntry:
    id: str
    sequence: int
    occurred_at: str
    level: str
    message: str
    service_id: str
    content_sha256: str

    def to_contract(self) -> LogEntryV1:
        return LogEntryV1(
            id=self.id,
            sequence=self.sequence,
            occurred_at=self.occurred_at,
            stream=LogStream.SERVICE,
            level=LogLevel(self.level),
            message=self.message,
            service_id=self.service_id,
            content_sha256=self.content_sha256,
        )


@dataclass(frozen=True, slots=True)
class SupervisorModelPreparation:
    model_ref: str
    status: str
    updated_at: str
    next_interface: str


@dataclass(frozen=True, slots=True)
class SupervisorServiceSummary:
    id: str
    display_name: str
    component: ServiceComponent
    status: ServiceStatus
    restartable: bool
    status_message: str | None
    error_code: str | None
    updated_at: str
    observed_at: str
    identity_digest: str
    pid: int | None
    port: int | None
    etag: str
    model_preparation: SupervisorModelPreparation | None = None

    def to_contract(self) -> ServiceSummaryV1:
        error = None
        if self.status is ServiceStatus.FAILED:
            code = self.error_code or "service_failed"
            message = self.status_message or "The managed service failed."
            error = ApiErrorV1(
                request_id=f"{self.id}-service-error",
                code=code,
                http_status=503,
                message=message,
                severity=ErrorSeverity.BLOCKING,
                category=ErrorCategory.SERVICE,
                retryable=True,
                repair_action=RepairAction.OPENEVO_CAN_RETRY,
                next_action="Retry the managed service operation from OpenEvo Desktop.",
            )
        preparation = None
        if self.model_preparation is not None:
            preparation = ModelPreparationV1(
                model_ref=self.model_preparation.model_ref,
                status=ModelPreparationStatus(self.model_preparation.status),
                updated_at=self.model_preparation.updated_at,
            )
        return ServiceSummaryV1(
            id=self.id,
            display_name=self.display_name,
            kind=_contract_kind(self.component),
            status=ContractServiceStatus(self.status.value),
            restartable=self.restartable,
            status_message=self.status_message,
            error=error,
            model_preparation=preparation,
            updated_at=self.updated_at,
            observed_at=self.observed_at,
            etag=self.etag,
        )


@dataclass(frozen=True, slots=True)
class ServiceGroupSnapshot:
    execution_mode: ServiceExecutionMode
    ready: bool
    generation_digest: str
    services: tuple[SupervisorServiceSummary, ...]
    runtime_identity_digest: str | None
    status_message: str | None = None

    def service(self, service_id: str) -> SupervisorServiceSummary:
        for service in self.services:
            if service.id == service_id:
                return service
        raise KeyError(service_id)


class _StrictStateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_default=True)


class _LedgerRelease(_StrictStateModel):
    install_digest: str
    registry_digest: str
    framework_lock_digest: str

    _install = field_validator("install_digest")(_state_digest)
    _registry = field_validator("registry_digest")(_state_digest)
    _lock = field_validator("framework_lock_digest")(_state_digest)


class _LedgerLog(_StrictStateModel):
    id: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=0)
    occurred_at: str = Field(min_length=20, max_length=40)
    level: str
    message: str = Field(max_length=16_384)
    content_sha256: str

    _digest = field_validator("content_sha256")(_state_digest)

    @field_validator("level")
    @classmethod
    def _closed_level(cls, value: str) -> str:
        if value not in {"debug", "info", "warning", "error"}:
            raise ValueError("service log level is not supported")
        return value


class _LedgerService(_StrictStateModel):
    service_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=128)
    component: ServiceComponent
    status: ServiceStatus
    restartable: bool
    status_message: str | None = Field(default=None, max_length=256)
    error_code: str | None = Field(default=None, max_length=128)
    updated_at: str = Field(min_length=20, max_length=40)
    identity_digest: str
    argv_digest: str
    env_digest: str
    pid: int | None = Field(default=None, gt=0)
    birth_token: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    log_sequence: int = Field(default=0, ge=0)
    logs: list[_LedgerLog] = Field(default_factory=list, max_length=10_000)
    model_ref: str | None = Field(default=None, max_length=256)
    model_status: str | None = None
    model_updated_at: str | None = Field(default=None, max_length=40)
    model_next_interface: str | None = Field(default=None, max_length=64)

    _identity = field_validator("identity_digest")(_state_digest)
    _argv = field_validator("argv_digest")(_state_digest)
    _env = field_validator("env_digest")(_state_digest)

    @field_validator("component", mode="before")
    @classmethod
    def _component_from_canonical_text(cls, value: object) -> object:
        return ServiceComponent(value) if isinstance(value, str) else value

    @field_validator("status", mode="before")
    @classmethod
    def _status_from_canonical_text(cls, value: object) -> object:
        return ServiceStatus(value) if isinstance(value, str) else value


class _Ledger(_StrictStateModel):
    schema_version: int
    release: _LedgerRelease
    execution_mode: ServiceExecutionMode | None = None
    generation_digest: str | None = None
    runtime_identity_digest: str | None = None
    group_status_message: str | None = Field(default=None, max_length=256)
    services: list[_LedgerService] = Field(default_factory=list, max_length=16)

    @field_validator("schema_version")
    @classmethod
    def _schema_is_one(cls, value: int) -> int:
        if value != 1:
            raise ValueError("unsupported service ledger schema")
        return value

    @field_validator("generation_digest")
    @classmethod
    def _optional_digest(cls, value: str | None) -> str | None:
        return None if value is None else _state_digest(value)

    @field_validator("runtime_identity_digest")
    @classmethod
    def _optional_runtime_digest(cls, value: str | None) -> str | None:
        return None if value is None else _state_digest(value)

    @field_validator("execution_mode", mode="before")
    @classmethod
    def _mode_from_canonical_text(cls, value: object) -> object:
        return ServiceExecutionMode(value) if isinstance(value, str) else value


@dataclass(slots=True)
class _TrackedProcess:
    process: subprocess.Popen[bytes]
    identity: ProcessIdentity


class RealSubprocessBackend:
    """Controlled process-group launcher with Linux PID birth identity."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tracked: dict[str, _TrackedProcess] = {}

    def spawn(
        self,
        spec: ServiceProcessSpec,
        on_output: Callable[[bytes], None],
        on_exit: Callable[[int], None],
    ) -> ProcessIdentity:
        parent_pid = os.getpid()
        process = subprocess.Popen(
            spec.argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=dict(spec.env),
            close_fds=True,
            start_new_session=True,
            preexec_fn=_linux_parent_death_setup(parent_pid),
        )
        try:
            identity = ProcessIdentity(
                pid=process.pid,
                birth_token=_process_birth_token(process.pid),
            )
        except Exception:
            process.kill()
            process.wait(timeout=5)
            raise
        tracked = _TrackedProcess(process=process, identity=identity)
        with self._lock:
            self._tracked[identity.birth_token] = tracked
        threading.Thread(
            target=self._read_output,
            args=(tracked, on_output),
            name=f"openevo-log-{spec.service_id}",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._monitor,
            args=(tracked, on_exit),
            name=f"openevo-monitor-{spec.service_id}",
            daemon=True,
        ).start()
        return identity

    def is_alive(self, identity: ProcessIdentity) -> bool:
        tracked = self._owned(identity)
        return tracked is not None and tracked.process.poll() is None and _same_process(identity)

    def terminate(self, identity: ProcessIdentity) -> None:
        if self.is_alive(identity):
            os.killpg(identity.pid, signal.SIGTERM)

    def kill(self, identity: ProcessIdentity) -> None:
        if self.is_alive(identity):
            os.killpg(identity.pid, signal.SIGKILL)

    def wait(self, identity: ProcessIdentity, timeout: float | None) -> int | None:
        tracked = self._owned(identity)
        if tracked is None:
            return None
        try:
            return tracked.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def _owned(self, identity: ProcessIdentity) -> _TrackedProcess | None:
        with self._lock:
            tracked = self._tracked.get(identity.birth_token)
        if tracked is None or tracked.identity != identity:
            return None
        return tracked

    @staticmethod
    def _read_output(
        tracked: _TrackedProcess,
        on_output: Callable[[bytes], None],
    ) -> None:
        stream = tracked.process.stdout
        if stream is None:
            return
        try:
            while chunk := stream.readline(16_385):
                on_output(chunk)
        finally:
            stream.close()

    @staticmethod
    def _monitor(tracked: _TrackedProcess, on_exit: Callable[[int], None]) -> None:
        on_exit(tracked.process.wait())


class DefaultHealthChecker:
    def __init__(self, *, poll_interval: float = 0.05) -> None:
        self._poll_interval = poll_interval

    def wait_ready(
        self,
        spec: ServiceProcessSpec,
        identity: ProcessIdentity,
        process_backend: ProcessBackend,
        deadline: float,
    ) -> HealthCheckResult:
        process_observations = 0
        last_message = "health check has not completed"
        while True:
            if not process_backend.is_alive(identity):
                return HealthCheckResult(False, "managed process exited before readiness")
            now = time.monotonic()
            if now >= deadline:
                return HealthCheckResult(False, last_message)
            if spec.health_probe.kind is HealthProbeKind.PROCESS:
                process_observations += 1
                if process_observations >= 2:
                    return HealthCheckResult(True, "managed process identity is live")
            else:
                ready, last_message = _probe_http(spec.health_probe.url, deadline - now)
                if ready:
                    return HealthCheckResult(True, last_message)
            time.sleep(min(self._poll_interval, max(0.0, deadline - time.monotonic())))


class SocketPortProbe:
    def is_available(self, host: str, port: int) -> bool:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as candidate:
            candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            try:
                candidate.bind((host, port))
            except OSError:
                return False
        return True


class _SecureServiceRoot:
    def __init__(self, path: Path) -> None:
        self.path = Path(os.path.abspath(os.fspath(path)))
        self._uid = os.getuid()
        self.owner_socket = self._claim_host_global_identity()
        try:
            self.fd = self._open_or_create_root()
        except Exception:
            if self.owner_socket is not None:
                self.owner_socket.close()
            raise
        self._identity = _fd_identity(self.fd)
        try:
            self.lock_fd = self._open_lock()
        except Exception:
            os.close(self.fd)
            if self.owner_socket is not None:
                self.owner_socket.close()
            raise

    def verify(self) -> None:
        held = os.fstat(self.fd)
        if _fd_identity_from_stat(held) != self._identity:
            raise SupervisorStateError("service root held inode binding changed")
        try:
            current = os.stat(self.path, follow_symlinks=False)
        except OSError as exc:
            raise SupervisorStateError("service root pathname binding is unavailable") from exc
        if stat.S_ISLNK(current.st_mode):
            raise SupervisorStateError("service root pathname became a symlink")
        if _fd_identity_from_stat(current) != self._identity:
            raise SupervisorStateError("service root pathname binding was replaced")
        _require_private_directory(current, self._uid, "service root")

    def read(self, name: str, *, max_bytes: int) -> bytes | None:
        self.verify()
        flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(name, flags, dir_fd=self.fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise SupervisorStateError(f"managed {name} is a symlink") from exc
            raise
        try:
            info = os.fstat(fd)
            _require_private_file(info, self._uid, name)
            if info.st_size > max_bytes:
                raise SupervisorStateError(f"managed {name} exceeds its byte limit")
            payload = bytearray()
            while len(payload) < info.st_size:
                chunk = os.read(fd, min(64 * 1024, info.st_size - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
            if len(payload) != info.st_size:
                raise SupervisorStateError(f"managed {name} changed while being read")
            after = os.fstat(fd)
            if _fd_identity_from_stat(after) != _fd_identity_from_stat(info):
                raise SupervisorStateError(f"managed {name} inode changed while being read")
            return bytes(payload)
        finally:
            os.close(fd)

    def atomic_write(self, name: str, payload: bytes) -> None:
        self.verify()
        temp_name = f".{name}.tmp-{secrets.token_hex(12)}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(temp_name, flags, _FILE_MODE, dir_fd=self.fd)
        published = False
        try:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise SupervisorStateError("managed state write made no progress")
                view = view[written:]
            os.fsync(fd)
            _require_private_file(os.fstat(fd), self._uid, temp_name)
            os.replace(temp_name, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            published = True
            os.fsync(self.fd)
            self.verify()
            verified = self.read(name, max_bytes=max(_MAX_LEDGER_BYTES, len(payload)))
            if verified != payload:
                raise SupervisorStateError(f"managed {name} readback mismatch")
        finally:
            os.close(fd)
            if not published:
                try:
                    os.unlink(temp_name, dir_fd=self.fd)
                except OSError:
                    pass

    def ensure_directory(self, name: str) -> None:
        if not name or "/" in name or name in {".", ".."}:
            raise ValueError("managed directory name is invalid")
        self.verify()
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(name, flags, dir_fd=self.fd)
        except FileNotFoundError:
            os.mkdir(name, _ROOT_MODE, dir_fd=self.fd)
            os.fsync(self.fd)
            fd = os.open(name, flags, dir_fd=self.fd)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise SupervisorStateError(
                    f"managed directory {name} is a symlink or non-directory"
                ) from exc
            raise
        try:
            _require_private_directory(os.fstat(fd), self._uid, name)
            current = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            if _fd_identity_from_stat(current) != _fd_identity(fd):
                raise SupervisorStateError(f"managed directory {name} binding changed")
        finally:
            os.close(fd)

    def close(self) -> None:
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(self.lock_fd)
            os.close(self.fd)
            if self.owner_socket is not None:
                self.owner_socket.close()

    def _claim_host_global_identity(self) -> socket.socket | None:
        if not sys.platform.startswith("linux"):
            return None
        owner = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        path_digest = hashlib.sha256(os.fsencode(self.path)).hexdigest()
        address = f"\0openevo-core-services-{self._uid}-{path_digest}"
        try:
            owner.bind(address)
            owner.listen(1)
        except OSError as exc:
            owner.close()
            if exc.errno == errno.EADDRINUSE:
                raise SupervisorBusyError(
                    "another Core daemon owns the host-global service identity"
                ) from exc
            raise SupervisorStateError(
                "host-global service owner identity could not be claimed"
            ) from exc
        return owner

    def _open_or_create_root(self) -> int:
        if not self.path.is_absolute():
            raise SupervisorStateError("service root must be absolute")
        parts = self.path.parts[1:]
        if not parts:
            raise SupervisorStateError("filesystem root cannot be a service root")
        current = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            for index, part in enumerate(parts):
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
                created = False
                try:
                    next_fd = os.open(part, flags, dir_fd=current)
                except FileNotFoundError:
                    os.mkdir(part, _ROOT_MODE, dir_fd=current)
                    os.fsync(current)
                    next_fd = os.open(part, flags, dir_fd=current)
                    created = True
                except OSError as exc:
                    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                        raise SupervisorStateError(
                            "service root path contains a symlink or non-directory component"
                        ) from exc
                    raise
                os.close(current)
                current = next_fd
                info = os.fstat(current)
                if created or index == len(parts) - 1:
                    _require_private_directory(info, self._uid, "service root")
                else:
                    _require_safe_ancestor(info)
            return current
        except Exception:
            os.close(current)
            raise

    def _open_lock(self) -> int:
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open("owner.lock", flags, _FILE_MODE, dir_fd=self.fd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise SupervisorStateError("service owner lock is a symlink") from exc
            raise
        try:
            _require_private_file(os.fstat(fd), self._uid, "owner lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SupervisorBusyError(
                    "another Core daemon owns the service supervisor root"
                ) from exc
            return fd
        except Exception:
            os.close(fd)
            raise


class CoreServiceSupervisor:
    """Recoverable owner for Core's evolution/rollout/gateway/worker group."""

    def __init__(
        self,
        *,
        service_root: Path,
        framework_lock: Path,
        release_identity: ServiceReleaseIdentity,
        python_executable: str | None = None,
        process_backend: ProcessBackend | None = None,
        health_checker: HealthChecker | None = None,
        port_probe: PortProbe | None = None,
        managed_runtime_probe: ManagedScienceRuntimeProbe | None = None,
        startup_timeout: float = 30.0,
        stop_timeout: float = 5.0,
        max_log_entries: int = 512,
        max_log_bytes: int = 512 * 1024,
    ) -> None:
        if startup_timeout <= 0 or stop_timeout <= 0:
            raise ValueError("supervisor timeouts must be positive")
        if not 1 <= max_log_entries <= 10_000 or not 1 <= max_log_bytes <= 1_048_576:
            raise ValueError("supervisor log limits are outside the supported bounds")
        self._mutex = threading.RLock()
        self._root = _SecureServiceRoot(service_root)
        self._closed = False
        try:
            self._framework_lock = Path(framework_lock)
            self._framework_lock_digest = _hash_private_regular_file(
                self._framework_lock,
                max_bytes=_MAX_FRAMEWORK_LOCK_BYTES,
            )
            self._release_identity = release_identity
            self._python = python_executable or sys.executable
            self._process_backend = process_backend or RealSubprocessBackend()
            self._health_checker = health_checker or DefaultHealthChecker()
            self._port_probe = port_probe or SocketPortProbe()
            self._managed_runtime_probe = (
                managed_runtime_probe or LocalManagedScienceRuntimeProbe()
            )
            self._startup_timeout = startup_timeout
            self._stop_timeout = stop_timeout
            self._max_log_entries = max_log_entries
            self._max_log_bytes = max_log_bytes
            self._handles: dict[str, ProcessIdentity] = {}
            self._specs: dict[str, ServiceProcessSpec] = {}
            self._restart_results: dict[tuple[str, str], SupervisorServiceSummary] = {}
            self._ledger = self._load_or_initialize_ledger()
            self._recover_prior_owner_state()
        except Exception:
            self._root.close()
            self._closed = True
            raise

    def ensure(
        self,
        execution_mode: ServiceExecutionMode,
        *,
        model_ref: str | None = None,
        codex_model: str | None = None,
        runtime_image: str | None = None,
        total_timeout: float | None = None,
    ) -> ServiceGroupSnapshot:
        with self._mutex:
            self._require_open()
            if total_timeout is not None and total_timeout <= 0:
                raise ValueError("total_timeout must be positive")
            deadline = time.monotonic() + (
                self._startup_timeout if total_timeout is None else total_timeout
            )
            if execution_mode is ServiceExecutionMode.SELF_DEPLOYED:
                return self._ensure_self_deployed_unavailable(model_ref, deadline)
            if codex_model is None or runtime_image is None:
                raise ValueError(
                    "subscription ensure requires codex_model and managed runtime_image"
                )
            runtime_request = ManagedScienceRuntimeRequest(
                runtime_image=runtime_image,
                codex_model=codex_model,
            )
            runtime = self._managed_runtime_probe.verify(runtime_request, deadline)
            plan_runtime_identity = runtime.identity_digest or _digest_json(
                {
                    "codex_model": runtime_request.codex_model,
                    "runtime_image": runtime_request.runtime_image,
                    "unverified": True,
                }
            )
            specs, topology = self._subscription_plan(
                runtime_request,
                plan_runtime_identity,
            )
            generation_digest = _digest_json([spec.identity_digest for spec in specs])
            self._specs = {spec.service_id: spec for spec in specs}
            if not runtime.ready:
                if not self._stop_all(deadline):
                    self._ledger.group_status_message = (
                        "Existing managed children could not be stopped; runtime change aborted."
                    )
                    self._persist()
                    return self._group_snapshot()
                self._install_planned_records(
                    execution_mode,
                    generation_digest,
                    specs,
                    runtime_identity_digest=None,
                    group_status_message=_sanitize(runtime.message),
                )
                for record in self._ledger.services:
                    record.status = ServiceStatus.UNAVAILABLE
                    record.status_message = _sanitize(runtime.message)
                    record.restartable = False
                self._specs = {}
                self._persist()
                return self._group_snapshot()
            if self._is_current_group_healthy(execution_mode, generation_digest, specs, deadline):
                return self._group_snapshot()
            if not self._stop_all(deadline):
                self._ledger.group_status_message = (
                    "Existing managed children could not be stopped; service start aborted."
                )
                self._persist()
                return self._group_snapshot()
            self._install_planned_records(
                execution_mode,
                generation_digest,
                specs,
                runtime_identity_digest=runtime.identity_digest,
                group_status_message=None,
            )
            conflict = next(
                (
                    spec
                    for spec in specs
                    if spec.port is not None
                    and not self._port_probe.is_available("127.0.0.1", spec.port)
                ),
                None,
            )
            if conflict is not None:
                self._fail_record(
                    conflict.service_id,
                    "service_port_conflict",
                    f"Loopback port {conflict.port} is already in use.",
                )
                self._persist()
                return self._group_snapshot()
            self._root.ensure_directory("evolution")
            self._root.ensure_directory("rollout")
            self._root.atomic_write("topology.json", _canonical_bytes(topology))
            started: list[str] = []
            for spec in specs:
                if time.monotonic() >= deadline:
                    self._fail_record(
                        spec.service_id,
                        "service_readiness_timeout",
                        "The service group startup deadline was exceeded.",
                    )
                    self._rollback(started, deadline)
                    return self._group_snapshot()
                self._set_starting(spec.service_id)
                try:
                    identity = self._process_backend.spawn(
                        spec,
                        lambda payload, service_id=spec.service_id: self._record_output(
                            service_id, payload
                        ),
                        lambda returncode, service_id=spec.service_id: self._record_exit(
                            service_id, returncode
                        ),
                    )
                except Exception as exc:
                    self._fail_record(
                        spec.service_id,
                        "service_spawn_failed",
                        _safe_message("Managed service could not be started", exc),
                    )
                    self._rollback(started, deadline)
                    return self._group_snapshot()
                self._handles[spec.service_id] = identity
                record = self._record(spec.service_id)
                record.pid = identity.pid
                record.birth_token = identity.birth_token
                self._persist()
                started.append(spec.service_id)
                health = self._health_checker.wait_ready(
                    spec,
                    identity,
                    self._process_backend,
                    deadline,
                )
                if not health.ready or not self._process_backend.is_alive(identity):
                    code = (
                        "service_readiness_timeout"
                        if time.monotonic() >= deadline
                        else "service_health_failed"
                    )
                    self._fail_record(spec.service_id, code, _sanitize(health.message))
                    self._rollback(started, deadline)
                    return self._group_snapshot()
                self._set_running(spec.service_id, health.message)
            return self._group_snapshot()

    def list(self) -> tuple[SupervisorServiceSummary, ...]:
        with self._mutex:
            self._require_open()
            self._refresh_process_state()
            return tuple(self._summary(record) for record in self._ledger.services)

    def get(self, service_id: str) -> SupervisorServiceSummary:
        with self._mutex:
            self._require_open()
            self._refresh_process_state()
            return self._summary(self._record(service_id))

    def restart(
        self,
        service_id: str,
        *,
        operation_id: str,
        total_timeout: float | None = None,
    ) -> SupervisorServiceSummary:
        if not operation_id or len(operation_id) > 128:
            raise ValueError("restart operation_id is invalid")
        with self._mutex:
            self._require_open()
            if total_timeout is not None and total_timeout <= 0:
                raise ValueError("total_timeout must be positive")
            key = (service_id, operation_id)
            if key in self._restart_results:
                return self._restart_results[key]
            record = self._record(service_id)
            if not record.restartable:
                raise SupervisorStateError("service is not restartable in its current state")
            spec = self._specs.get(service_id)
            if spec is None:
                raise KeyError(service_id)
            deadline = time.monotonic() + (
                self._startup_timeout if total_timeout is None else total_timeout
            )
            self._stop_service(service_id, deadline)
            if self._record(service_id).error_code == "service_stop_timeout":
                result = self._summary(self._record(service_id))
                self._restart_results[key] = result
                return result
            self._set_starting(service_id)
            try:
                identity = self._process_backend.spawn(
                    spec,
                    lambda payload: self._record_output(service_id, payload),
                    lambda returncode: self._record_exit(service_id, returncode),
                )
            except Exception as exc:
                self._fail_record(
                    service_id,
                    "service_spawn_failed",
                    _safe_message("Managed service could not be restarted", exc),
                )
                result = self._summary(self._record(service_id))
                self._restart_results[key] = result
                return result
            self._handles[service_id] = identity
            record = self._record(service_id)
            record.pid = identity.pid
            record.birth_token = identity.birth_token
            self._persist()
            health = self._health_checker.wait_ready(
                spec,
                identity,
                self._process_backend,
                deadline,
            )
            if not health.ready or not self._process_backend.is_alive(identity):
                self._fail_record(
                    service_id,
                    "service_health_failed",
                    _sanitize(health.message),
                )
                self._stop_service(service_id, deadline, preserve_failure=True)
            else:
                self._set_running(service_id, health.message)
            result = self._summary(self._record(service_id))
            self._restart_results[key] = result
            return result

    def logs(
        self,
        service_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> tuple[SupervisorLogEntry, ...]:
        if after_sequence < 0 or not 1 <= limit <= 100:
            raise ValueError("log snapshot bounds are invalid")
        with self._mutex:
            self._require_open()
            record = self._record(service_id)
            return tuple(
                SupervisorLogEntry(
                    id=item.id,
                    sequence=item.sequence,
                    occurred_at=item.occurred_at,
                    level=item.level,
                    message=item.message,
                    service_id=service_id,
                    content_sha256=item.content_sha256,
                )
                for item in record.logs
                if item.sequence > after_sequence
            )[:limit]

    def close(self, *, total_timeout: float | None = None) -> None:
        with self._mutex:
            if self._closed:
                return
            if total_timeout is not None and total_timeout <= 0:
                raise ValueError("total_timeout must be positive")
            deadline = time.monotonic() + (
                total_timeout
                if total_timeout is not None
                else self._stop_timeout * max(1, len(self._handles))
            )
            if not self._stop_all(deadline):
                raise SupervisorStateError(
                    "managed children remain live; supervisor ownership was retained"
                )
            self._closed = True
            self._root.close()

    def cancel(self, *, total_timeout: float | None = None) -> None:
        self.close(total_timeout=total_timeout)

    def __enter__(self) -> CoreServiceSupervisor:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _abandon_for_test(self) -> None:
        """Release only the owner lock; tests use this to emulate abrupt Core death."""
        with self._mutex:
            self._closed = True
            self._root.close()

    def _subscription_plan(
        self,
        runtime_request: ManagedScienceRuntimeRequest,
        runtime_identity_digest: str,
    ) -> tuple[tuple[ServiceProcessSpec, ...], dict[str, object]]:
        root = self._root.path
        topology_path = root / "topology.json"
        evolution_root = root / "evolution"
        topology: dict[str, object] = {
            "evolution": {
                "backend_url": "http://127.0.0.1:8200",
                "enabled": True,
                "context": {
                    "fail_open": True,
                    "target_dir": "/openevo/session/evolution",
                    "timeout_seconds": 10.0,
                },
                "event_export": {
                    "enabled": True,
                    "fail_open": True,
                    "timeout_seconds": 10.0,
                },
            },
            "gateway": {
                "heartbeat_interval_seconds": 30,
                "nodes": [
                    {
                        "host": "127.0.0.1",
                        "id": "core-gateway",
                        "inference": {
                            "base_url": "http://127.0.0.1:8000",
                            "engine": "vllm",
                        },
                        "model_served": runtime_request.codex_model,
                        "port": 8100,
                        "public_url": "http://127.0.0.1:8100",
                    }
                ],
                "rollout_server_url": "http://127.0.0.1:8080",
            },
            "rollout": {
                "host": "127.0.0.1",
                "port": 8080,
                "public_url": "http://127.0.0.1:8080",
                "save_dir": os.fspath(root / "rollout"),
            },
        }
        base_env = _controlled_environment()
        plans = (
            (
                "evolution-backend",
                "Evolution backend",
                ServiceComponent.EVOLUTION_BACKEND,
                (
                    self._python,
                    "-m",
                    "openevo.evolution.cli",
                    "serve",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8200",
                    "--db",
                    os.fspath(evolution_root / "evolution.db"),
                    "--artifact-root",
                    os.fspath(evolution_root / "artifacts"),
                    "--framework-lock",
                    os.fspath(self._framework_lock),
                ),
                8200,
                ServiceHealthProbe.http("http://127.0.0.1:8200/v1/health"),
            ),
            (
                "rollout",
                "Rollout service",
                ServiceComponent.ROLLOUT,
                (
                    self._python,
                    "-m",
                    "openevo.rollout.server",
                    "--config",
                    os.fspath(topology_path),
                    "--log-level",
                    "info",
                ),
                8080,
                ServiceHealthProbe.http("http://127.0.0.1:8080/health"),
            ),
            (
                "gateway",
                "Gateway service",
                ServiceComponent.GATEWAY,
                (
                    self._python,
                    "-m",
                    "openevo.gateway.server",
                    "--config",
                    os.fspath(topology_path),
                    "--node-id",
                    "core-gateway",
                    "--log-level",
                    "info",
                ),
                8100,
                ServiceHealthProbe.http("http://127.0.0.1:8100/health"),
            ),
            (
                "evolution-worker",
                "Evolution worker",
                ServiceComponent.EVOLUTION_WORKER,
                (
                    self._python,
                    "-m",
                    "openevo.evolution.cli",
                    "worker",
                    "--base-url",
                    "http://127.0.0.1:8200",
                    "--worker-id",
                    "core-reference-worker",
                    "--artifact-root",
                    os.fspath(evolution_root / "artifacts"),
                    "--framework-lock",
                    os.fspath(self._framework_lock),
                ),
                None,
                ServiceHealthProbe.process(),
            ),
        )
        topology_digest = _digest_json(topology)
        specs = []
        for service_id, display_name, component, argv, port, health_probe in plans:
            argv_digest = _digest_json(list(argv))
            env_digest = _digest_json(dict(sorted(base_env.items())))
            identity_digest = _digest_json(
                {
                    "argv_digest": argv_digest,
                    "component": component.value,
                    "env_digest": env_digest,
                    "framework_lock_digest": self._framework_lock_digest,
                    "install_digest": self._release_identity.install_digest,
                    "port": port,
                    "registry_digest": self._release_identity.registry_digest,
                    "runtime_identity_digest": runtime_identity_digest,
                    "runtime_image": runtime_request.runtime_image,
                    "service_id": service_id,
                    "topology_digest": topology_digest,
                }
            )
            specs.append(
                ServiceProcessSpec(
                    service_id=service_id,
                    display_name=display_name,
                    component=component,
                    argv=argv,
                    env=base_env,
                    argv_digest=argv_digest,
                    env_digest=env_digest,
                    identity_digest=identity_digest,
                    port=port,
                    health_probe=health_probe,
                )
            )
        return tuple(specs), topology

    def _ensure_self_deployed_unavailable(
        self,
        model_ref: str | None,
        deadline: float,
    ) -> ServiceGroupSnapshot:
        if model_ref is None or not model_ref.strip() or len(model_ref.strip()) > 256:
            raise ValueError("self-deployed execution requires a bounded model_ref")
        if not self._stop_all(deadline):
            self._ledger.group_status_message = (
                "Existing managed children could not be stopped; mode change aborted."
            )
            self._persist()
            return self._group_snapshot()
        now = _timestamp()
        identity = _digest_json(
            {
                "framework_lock_digest": self._framework_lock_digest,
                "install_digest": self._release_identity.install_digest,
                "model_ref": model_ref.strip(),
                "registry_digest": self._release_identity.registry_digest,
                "required_interface": "model_preparer_v1",
            }
        )
        self._ledger.execution_mode = ServiceExecutionMode.SELF_DEPLOYED
        self._ledger.generation_digest = identity
        self._ledger.runtime_identity_digest = None
        self._ledger.group_status_message = (
            "Self-deployed model preparation is unavailable in this release slice."
        )
        self._ledger.services = [
            _LedgerService(
                service_id="inference",
                display_name="Managed inference",
                component=ServiceComponent.INFERENCE,
                status=ServiceStatus.UNAVAILABLE,
                restartable=False,
                status_message=(
                    "Self-deployed model preparation requires model_preparer_v1; "
                    "dependency installation, Hugging Face download, proxy delivery, "
                    "vLLM launch, and served-model verification are not wired."
                ),
                updated_at=now,
                identity_digest=identity,
                argv_digest=_digest_json([]),
                env_digest=_digest_json({}),
                model_ref=model_ref.strip(),
                model_status="unresolved",
                model_updated_at=now,
                model_next_interface="model_preparer_v1",
            )
        ]
        self._specs = {}
        self._persist()
        return self._group_snapshot()

    def _is_current_group_healthy(
        self,
        execution_mode: ServiceExecutionMode,
        generation_digest: str,
        specs: Sequence[ServiceProcessSpec],
        deadline: float,
    ) -> bool:
        if (
            self._ledger.execution_mode is not execution_mode
            or self._ledger.generation_digest != generation_digest
            or [record.service_id for record in self._ledger.services]
            != [spec.service_id for spec in specs]
        ):
            return False
        for spec in specs:
            record = self._record(spec.service_id)
            identity = self._handles.get(spec.service_id)
            if (
                record.status is not ServiceStatus.RUNNING
                or identity is None
                or record.identity_digest != spec.identity_digest
                or record.pid != identity.pid
                or record.birth_token != identity.birth_token
                or not self._process_backend.is_alive(identity)
            ):
                return False
            health = self._health_checker.wait_ready(
                spec,
                identity,
                self._process_backend,
                deadline,
            )
            if not health.ready or not self._process_backend.is_alive(identity):
                self._fail_record(
                    spec.service_id,
                    "service_health_failed",
                    _sanitize(health.message),
                )
                return False
        return True

    def _install_planned_records(
        self,
        execution_mode: ServiceExecutionMode,
        generation_digest: str,
        specs: Sequence[ServiceProcessSpec],
        *,
        runtime_identity_digest: str | None,
        group_status_message: str | None,
    ) -> None:
        now = _timestamp()
        self._ledger.execution_mode = execution_mode
        self._ledger.generation_digest = generation_digest
        self._ledger.runtime_identity_digest = runtime_identity_digest
        self._ledger.group_status_message = group_status_message
        self._ledger.services = [
            _LedgerService(
                service_id=spec.service_id,
                display_name=spec.display_name,
                component=spec.component,
                status=ServiceStatus.STOPPED,
                restartable=True,
                updated_at=now,
                identity_digest=spec.identity_digest,
                argv_digest=spec.argv_digest,
                env_digest=spec.env_digest,
                port=spec.port,
            )
            for spec in specs
        ]
        self._persist()

    def _rollback(self, started: Sequence[str], deadline: float) -> None:
        failed = {
            record.service_id: (record.error_code, record.status_message)
            for record in self._ledger.services
            if record.status is ServiceStatus.FAILED
        }
        for service_id in reversed(started):
            self._stop_service(
                service_id,
                deadline,
                preserve_failure=service_id in failed,
            )
        for service_id, (code, message) in failed.items():
            record = self._record(service_id)
            record.status = ServiceStatus.FAILED
            record.error_code = code
            record.status_message = message
            record.updated_at = _timestamp()
        self._persist()

    def _stop_all(self, deadline: float) -> bool:
        stopped = True
        for record in reversed(self._ledger.services):
            self._stop_service(record.service_id, deadline)
            if self._record(record.service_id).error_code == "service_stop_timeout":
                stopped = False
        return stopped

    def _stop_service(
        self,
        service_id: str,
        deadline: float,
        *,
        preserve_failure: bool = False,
    ) -> None:
        record = self._record(service_id)
        identity = self._handles.pop(service_id, None)
        prior_error = (record.error_code, record.status_message)
        if identity is not None and self._process_backend.is_alive(identity):
            try:
                self._process_backend.terminate(identity)
            except Exception as exc:
                self._append_log(service_id, "warning", _safe_message("Terminate failed", exc))
            remaining = max(0.0, min(self._stop_timeout, deadline - time.monotonic()))
            exited = self._process_backend.wait(identity, remaining)
            if exited is None and self._process_backend.is_alive(identity):
                try:
                    self._process_backend.kill(identity)
                except Exception as exc:
                    self._append_log(service_id, "error", _safe_message("Kill failed", exc))
                self._process_backend.wait(identity, max(0.0, deadline - time.monotonic()))
        still_alive = identity is not None and self._process_backend.is_alive(identity)
        record.updated_at = _timestamp()
        if still_alive:
            self._handles[service_id] = identity
            record.pid = identity.pid
            record.birth_token = identity.birth_token
            record.status = ServiceStatus.FAILED
            record.error_code = "service_stop_timeout"
            record.status_message = "Managed child did not exit before the stop deadline."
            record.restartable = False
        elif preserve_failure:
            record.pid = None
            record.birth_token = None
            record.status = ServiceStatus.FAILED
            record.error_code, record.status_message = prior_error
        else:
            record.pid = None
            record.birth_token = None
            record.status = ServiceStatus.STOPPED
            record.error_code = None
            record.status_message = "Managed service is stopped."
        self._persist()

    def _record_output(self, service_id: str, payload: bytes) -> None:
        with self._mutex:
            if self._closed:
                return
            decoded = payload[:65_536].decode("utf-8", errors="replace")
            lines = decoded.splitlines() or [decoded]
            for line in lines:
                self._append_log(service_id, "info", line)
            self._persist()

    def _record_exit(self, service_id: str, returncode: int) -> None:
        with self._mutex:
            if self._closed:
                return
            record = self._record_or_none(service_id)
            if record is None or record.pid is None:
                return
            record.status = ServiceStatus.FAILED
            record.error_code = "service_process_exited"
            record.status_message = f"Managed process exited with status {returncode}."
            record.pid = None
            record.birth_token = None
            record.updated_at = _timestamp()
            self._handles.pop(service_id, None)
            self._append_log(service_id, "error", record.status_message)
            self._persist()

    def _append_log(self, service_id: str, level: str, message: str) -> None:
        record = self._record(service_id)
        safe = _sanitize(message).strip()
        if not safe:
            return
        encoded = safe.encode("utf-8")[:16_384]
        safe = encoded.decode("utf-8", errors="ignore")
        record.log_sequence += 1
        digest = hashlib.sha256(safe.encode("utf-8")).hexdigest()
        record.logs.append(
            _LedgerLog(
                id=f"{service_id}-log-{record.log_sequence}",
                sequence=record.log_sequence,
                occurred_at=_timestamp(),
                level=level,
                message=safe,
                content_sha256=digest,
            )
        )
        while self._log_entry_count() > self._max_log_entries or (
            self._log_byte_count() > self._max_log_bytes
        ):
            oldest = min(
                (candidate for candidate in self._ledger.services if candidate.logs),
                key=lambda candidate: (
                    candidate.logs[0].occurred_at,
                    candidate.service_id,
                    candidate.logs[0].sequence,
                ),
            )
            oldest.logs.pop(0)

    def _log_entry_count(self) -> int:
        return sum(len(record.logs) for record in self._ledger.services)

    def _log_byte_count(self) -> int:
        return sum(
            len(item.message.encode("utf-8"))
            for record in self._ledger.services
            for item in record.logs
        )

    def _set_starting(self, service_id: str) -> None:
        record = self._record(service_id)
        record.status = ServiceStatus.STARTING
        record.status_message = "Managed service is starting."
        record.error_code = None
        record.updated_at = _timestamp()
        self._persist()

    def _set_running(self, service_id: str, message: str) -> None:
        record = self._record(service_id)
        record.status = ServiceStatus.RUNNING
        record.status_message = _sanitize(message)[:256] or "Managed service is healthy."
        record.error_code = None
        record.updated_at = _timestamp()
        self._persist()

    def _fail_record(self, service_id: str, code: str, message: str) -> None:
        record = self._record(service_id)
        record.status = ServiceStatus.FAILED
        record.error_code = code
        record.status_message = (_sanitize(message) or "Managed service failed.")[:256]
        record.updated_at = _timestamp()
        self._append_log(service_id, "error", record.status_message)
        self._persist()

    def _refresh_process_state(self) -> None:
        changed = False
        for record in self._ledger.services:
            if record.status is not ServiceStatus.RUNNING:
                continue
            identity = self._handles.get(record.service_id)
            if identity is None or not self._process_backend.is_alive(identity):
                record.status = ServiceStatus.FAILED
                record.error_code = "service_process_identity_lost"
                record.status_message = "Managed process identity is no longer live."
                record.pid = None
                record.birth_token = None
                record.updated_at = _timestamp()
                self._handles.pop(record.service_id, None)
                changed = True
        if changed:
            self._persist()

    def _group_snapshot(self) -> ServiceGroupSnapshot:
        execution_mode = self._ledger.execution_mode
        generation = self._ledger.generation_digest
        if execution_mode is None or generation is None:
            raise SupervisorStateError("service group has no execution identity")
        services = tuple(self._summary(record) for record in self._ledger.services)
        ready = bool(services) and all(
            service.status is ServiceStatus.RUNNING for service in services
        )
        message = (
            None
            if ready
            else self._ledger.group_status_message
            or "One or more required Core services are not ready."
        )
        return ServiceGroupSnapshot(
            execution_mode=execution_mode,
            ready=ready,
            generation_digest=generation,
            services=services,
            runtime_identity_digest=self._ledger.runtime_identity_digest,
            status_message=message,
        )

    def _summary(self, record: _LedgerService) -> SupervisorServiceSummary:
        observed = _timestamp()
        preparation = None
        if record.component is ServiceComponent.INFERENCE:
            if not all(
                (
                    record.model_ref,
                    record.model_status,
                    record.model_updated_at,
                    record.model_next_interface,
                )
            ):
                raise SupervisorStateError("inference state lacks model preparation identity")
            preparation = SupervisorModelPreparation(
                model_ref=record.model_ref or "",
                status=record.model_status or "unresolved",
                updated_at=record.model_updated_at or record.updated_at,
                next_interface=record.model_next_interface or "model_preparer_v1",
            )
        etag_payload = {
            "component": record.component.value,
            "error_code": record.error_code,
            "identity_digest": record.identity_digest,
            "model_status": record.model_status,
            "pid": record.pid,
            "service_id": record.service_id,
            "status": record.status.value,
            "status_message": record.status_message,
            "updated_at": record.updated_at,
        }
        return SupervisorServiceSummary(
            id=record.service_id,
            display_name=record.display_name,
            component=record.component,
            status=record.status,
            restartable=record.restartable,
            status_message=record.status_message,
            error_code=record.error_code,
            updated_at=record.updated_at,
            observed_at=observed,
            identity_digest=record.identity_digest,
            pid=record.pid,
            port=record.port,
            etag=f'"{_digest_json(etag_payload)}"',
            model_preparation=preparation,
        )

    def _load_or_initialize_ledger(self) -> _Ledger:
        expected_release = _LedgerRelease(
            install_digest=self._release_identity.install_digest,
            registry_digest=self._release_identity.registry_digest,
            framework_lock_digest=self._framework_lock_digest,
        )
        payload = self._root.read("ledger.json", max_bytes=_MAX_LEDGER_BYTES)
        if payload is None:
            ledger = _Ledger(schema_version=1, release=expected_release)
            self._ledger = ledger
            self._persist()
            return ledger
        try:
            decoded = payload.decode("utf-8")
            raw = json.loads(decoded)
            if _canonical_bytes(raw) != payload:
                raise ValueError("ledger is not canonical JSON")
            ledger = _Ledger.model_validate(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise SupervisorStateError("service ledger is invalid") from exc
        if ledger.release != expected_release:
            raise SupervisorStateError("service ledger release identity does not match Core")
        if ledger.execution_mode is ServiceExecutionMode.SELF_DEPLOYED:
            if ledger.runtime_identity_digest is not None:
                raise SupervisorStateError("self-deployed unavailable state has runtime evidence")
        elif ledger.execution_mode is ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT:
            has_running_state = any(
                record.status
                in {
                    ServiceStatus.STARTING,
                    ServiceStatus.RUNNING,
                    ServiceStatus.DEGRADED,
                }
                for record in ledger.services
            )
            if has_running_state and ledger.runtime_identity_digest is None:
                raise SupervisorStateError("subscription service state lacks runtime evidence")
        elif ledger.runtime_identity_digest is not None:
            raise SupervisorStateError("unbound service ledger has runtime evidence")
        service_ids = [record.service_id for record in ledger.services]
        if len(service_ids) != len(set(service_ids)):
            raise SupervisorStateError("service ledger contains duplicate service identities")
        for record in ledger.services:
            if (record.pid is None) != (record.birth_token is None):
                raise SupervisorStateError("service ledger PID identity is incomplete")
            if record.birth_token is not None:
                _require_digest(record.birth_token, "birth_token")
            if (record.status is ServiceStatus.FAILED) != (record.error_code is not None):
                raise SupervisorStateError("service ledger failure state is inconsistent")
            previous_sequence = 0
            for item in record.logs:
                if item.sequence <= previous_sequence or item.sequence > record.log_sequence:
                    raise SupervisorStateError("service ledger log sequence is invalid")
                if item.id != f"{record.service_id}-log-{item.sequence}":
                    raise SupervisorStateError("service ledger log identity is invalid")
                if hashlib.sha256(item.message.encode("utf-8")).hexdigest() != item.content_sha256:
                    raise SupervisorStateError("service ledger log digest is invalid")
                previous_sequence = item.sequence
            model_fields = (
                record.model_ref,
                record.model_status,
                record.model_updated_at,
                record.model_next_interface,
            )
            if record.component is ServiceComponent.INFERENCE:
                if not all(model_fields) or record.model_status != "unresolved":
                    raise SupervisorStateError("inference model preparation state is invalid")
            elif any(value is not None for value in model_fields):
                raise SupervisorStateError("non-inference service has model preparation state")
        if (
            sum(len(record.logs) for record in ledger.services) > self._max_log_entries
            or sum(
                len(item.message.encode("utf-8"))
                for record in ledger.services
                for item in record.logs
            )
            > self._max_log_bytes
        ):
            raise SupervisorStateError("service ledger aggregate log budget is exceeded")
        return ledger

    def _recover_prior_owner_state(self) -> None:
        changed = False
        for record in self._ledger.services:
            if record.pid is not None or record.status in {
                ServiceStatus.STARTING,
                ServiceStatus.RUNNING,
                ServiceStatus.DEGRADED,
            }:
                record.status = ServiceStatus.FAILED
                record.error_code = "service_prior_owner_lost"
                record.status_message = (
                    "Prior Core ownership was lost; persisted PID identity was not reused."
                )
                record.pid = None
                record.birth_token = None
                record.updated_at = _timestamp()
                changed = True
        if changed:
            self._persist()

    def _persist(self) -> None:
        payload = _canonical_bytes(self._ledger.model_dump(mode="json"))
        if len(payload) > _MAX_LEDGER_BYTES:
            raise SupervisorStateError("service ledger exceeds its byte limit")
        self._root.atomic_write("ledger.json", payload)

    def _record(self, service_id: str) -> _LedgerService:
        record = self._record_or_none(service_id)
        if record is None:
            raise KeyError(service_id)
        return record

    def _record_or_none(self, service_id: str) -> _LedgerService | None:
        return next(
            (record for record in self._ledger.services if record.service_id == service_id),
            None,
        )

    def _require_open(self) -> None:
        if self._closed:
            raise SupervisorStateError("service supervisor is closed")
        self._root.verify()


def release_identity_from_verified_registry(registry: object) -> ServiceReleaseIdentity:
    """Derive service identity only from a loader-sealed executable registry."""

    from openevo.evolution.framework import require_verified_executable_registry

    verified = require_verified_executable_registry(registry)  # type: ignore[arg-type]
    installed = [
        {
            "distribution_digest": item.expectation.distribution_digest,
            "inventory_digest": item.inventory_digest,
        }
        for item in verified.distribution_attestations.values()
    ]
    installed.sort(key=lambda item: (item["distribution_digest"], item["inventory_digest"]))
    return ServiceReleaseIdentity(
        install_digest=_digest_json(installed),
        registry_digest=verified.snapshot.registry_digest,
    )


def _controlled_environment() -> dict[str, str]:
    allowed = ("HOME", "LANG", "LC_ALL", "PATH", "PYTHONPATH", "TMPDIR", "VIRTUAL_ENV")
    result = {key: os.environ[key] for key in allowed if key in os.environ}
    result.setdefault("PATH", os.defpath)
    result["PYTHONUNBUFFERED"] = "1"
    return dict(sorted(result.items()))


def _contract_kind(component: ServiceComponent) -> ServiceKind:
    if component is ServiceComponent.GATEWAY:
        return ServiceKind.GATEWAY
    if component is ServiceComponent.EVOLUTION_WORKER:
        return ServiceKind.EVOLUTION_WORKER
    if component is ServiceComponent.INFERENCE:
        return ServiceKind.INFERENCE
    return ServiceKind.CONTROL


def _private_file_metadata_identity(path: Path) -> str:
    fd = _open_absolute_nofollow_file(path, "Codex auth")
    try:
        info = os.fstat(fd)
        _require_private_file(info, os.getuid(), "Codex auth")
        return _digest_json(
            {
                "device": info.st_dev,
                "inode": info.st_ino,
                "mode": stat.S_IMODE(info.st_mode),
                "mtime_ns": info.st_mtime_ns,
                "size": info.st_size,
                "uid": info.st_uid,
            }
        )
    finally:
        os.close(fd)


def _hash_private_regular_file(path: Path, *, max_bytes: int) -> str:
    fd = _open_absolute_nofollow_file(path, "framework lock")
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise SupervisorStateError("framework lock is not a link-count-one regular file")
        if before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) & 0o022:
            raise SupervisorStateError("framework lock owner or mode is unsafe")
        if before.st_size > max_bytes:
            raise SupervisorStateError("framework lock exceeds its byte limit")
        digest = hashlib.sha256()
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                raise SupervisorStateError("framework lock changed while hashing")
            digest.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        if _fd_identity_from_stat(before) != _fd_identity_from_stat(after):
            raise SupervisorStateError("framework lock identity changed while hashing")
        return digest.hexdigest()
    finally:
        os.close(fd)


def _open_absolute_nofollow_file(path: Path, label: str) -> int:
    absolute = Path(os.path.abspath(os.fspath(path)))
    parts = absolute.parts[1:]
    if not parts:
        raise SupervisorStateError(f"{label} path does not name a file")
    current = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        directory_flags = (
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        )
        for part in parts[:-1]:
            try:
                next_fd = os.open(part, directory_flags, dir_fd=current)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise SupervisorStateError(f"{label} path contains a symlink") from exc
                raise SupervisorStateError(f"{label} parent path is unavailable") from exc
            os.close(current)
            current = next_fd
            _require_safe_ancestor(os.fstat(current))
        file_flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        try:
            return os.open(parts[-1], file_flags, dir_fd=current)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise SupervisorStateError(f"{label} path is a symlink") from exc
            raise SupervisorStateError(f"{label} cannot be opened safely") from exc
    finally:
        os.close(current)


def _probe_http(url: str | None, remaining: float) -> tuple[bool, str]:
    if url is None:
        return False, "health probe URL is missing"
    timeout = max(0.01, min(1.0, remaining))
    try:
        with urlopen(Request(url, method="GET"), timeout=timeout) as response:
            if 200 <= response.status < 300:
                response.read(4096)
                return True, "HTTP health probe succeeded"
            return False, f"HTTP health probe returned {response.status}"
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        return False, _safe_message("HTTP health probe failed", exc)


def _linux_parent_death_setup(parent_pid: int) -> Callable[[], None] | None:
    if not sys.platform.startswith("linux"):
        return None
    libc = ctypes.CDLL(None, use_errno=True)
    pr_set_pdeathsig = 1

    def setup() -> None:
        if libc.prctl(pr_set_pdeathsig, signal.SIGKILL, 0, 0, 0) != 0:
            os._exit(127)
        if os.getppid() != parent_pid:
            os.kill(os.getpid(), signal.SIGKILL)

    return setup


def _process_birth_token(pid: int) -> str:
    proc = Path("/proc") / str(pid)
    try:
        stat_text = (proc / "stat").read_text(encoding="ascii")
        end = stat_text.rfind(")")
        fields = stat_text[end + 2 :].split()
        start_ticks = fields[19]
        cmdline = (proc / "cmdline").read_bytes()
        uid = proc.stat().st_uid
    except (OSError, IndexError, UnicodeDecodeError) as exc:
        raise SupervisorStateError("could not establish child process birth identity") from exc
    return _digest_json(
        {
            "cmdline_sha256": hashlib.sha256(cmdline).hexdigest(),
            "pid": pid,
            "start_ticks": start_ticks,
            "uid": uid,
        }
    )


def _same_process(identity: ProcessIdentity) -> bool:
    try:
        return _process_birth_token(identity.pid) == identity.birth_token
    except SupervisorStateError:
        return False


def _require_private_directory(info: os.stat_result, uid: int, label: str) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise SupervisorStateError(f"{label} is not a directory")
    if info.st_uid != uid or stat.S_IMODE(info.st_mode) != _ROOT_MODE:
        raise SupervisorStateError(f"{label} must be owner-only mode 0700")


def _require_safe_ancestor(info: os.stat_result) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise SupervisorStateError("service root ancestor is not a directory")
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o022 and not mode & stat.S_ISVTX:
        raise SupervisorStateError("service root ancestor has unsafe writable mode")


def _require_private_file(info: os.stat_result, uid: int, label: str) -> None:
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise SupervisorStateError(f"{label} is not a link-count-one regular file")
    if info.st_uid != uid or stat.S_IMODE(info.st_mode) != _FILE_MODE:
        raise SupervisorStateError(f"{label} must be owner-only mode 0600")


def _fd_identity(fd: int) -> tuple[int, int, int]:
    return _fd_identity_from_stat(os.fstat(fd))


def _fd_identity_from_stat(info: os.stat_result) -> tuple[int, int, int]:
    return info.st_dev, info.st_ino, info.st_uid


def _sanitize(value: str) -> str:
    text = value.replace("\x00", "").replace("\r", " ").replace("\n", " ")
    text = _SECRET_RE.sub("<redacted>", text)
    text = _URL_CREDENTIAL_RE.sub(r"\1<redacted>@", text)
    text = _URL_QUERY_RE.sub(r"\1?<redacted>", text)
    text = _ABSOLUTE_PATH_RE.sub("<path>", text)
    return " ".join(text.split())[:16_384]


def _safe_message(prefix: str, exc: BaseException) -> str:
    detail = _sanitize(str(exc))
    return f"{prefix}: {detail}" if detail else prefix


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest_json(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _require_digest(value: str, name: str) -> None:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


__all__ = [
    "BoundedProbeCommandRunner",
    "CoreServiceSupervisor",
    "DefaultHealthChecker",
    "HealthCheckResult",
    "LocalManagedScienceRuntimeProbe",
    "ManagedScienceRuntimeReadiness",
    "ManagedScienceRuntimeRequest",
    "ProbeCommandResult",
    "ProcessIdentity",
    "RealSubprocessBackend",
    "ServiceComponent",
    "ServiceExecutionMode",
    "ServiceGroupSnapshot",
    "ServiceHealthProbe",
    "ServiceProcessSpec",
    "ServiceReleaseIdentity",
    "ServiceStatus",
    "SocketPortProbe",
    "SupervisorBusyError",
    "SupervisorError",
    "SupervisorLogEntry",
    "SupervisorServiceSummary",
    "SupervisorStateError",
    "release_identity_from_verified_registry",
]
