"""Core-owned lifecycle supervision for internal runtime services.

This module is deliberately independent from the frozen Core provider.  It owns
processes and private state; provider integration is a later dependency-injection
step.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
import ctypes
from dataclasses import dataclass, field
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
import selectors
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from openevo.backend.service_control import CoreServiceControlError
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
from openevo.gateway.session_files import (
    CODEX_CREDENTIAL_AUTHORITY_FD_ENV,
    HeldCodexCredentialAuthority,
    SessionFileSecurityError,
)
from openevo.runtime.managed import verified_managed_runtime_image_reference
from openevo.internal_auth import (
    INTERNAL_CREDENTIAL_FD_ENV,
    INTERNAL_LISTEN_FD_ENV,
    INTERNAL_OWNERSHIP_ENV,
    CORE_RUN_ADMISSION_URL_ENV,
    InternalServiceIdentity,
)


_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_SECRET_KEY_PATTERN = (
    r"(?:authorization|proxy[-_]?authorization|cookie|set[-_]?cookie|credential|"
    r"api[-_]?key|access[-_]?token|refresh[-_]?token|token|password|passwd|secret|"
    r"private[-_ ]?key|aws[-_]?secret[-_]?access[-_]?key|aws[-_]?session[-_]?token)"
)
_SECRET_RE = re.compile(
    rf"(?i)(?:{_SECRET_KEY_PATTERN})\s*[:=]\s*.*$"
    r"|bearer\s+[A-Za-z0-9._~+/=-]+"
)
_SECRET_KEY_RE = re.compile(rf"(?i)^{_SECRET_KEY_PATTERN}$")
_SPACE_SEPARATED_OPTION_SECRET_PREFIX_RE = re.compile(
    rf"(?i)(?<![A-Za-z0-9_-])--(?:{_SECRET_KEY_PATTERN})\s+(?=\S)"
)
_SPACE_SEPARATED_ENV_SECRET_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:[A-Z][A-Z0-9]*_)*(?:"
    r"AUTHORIZATION|COOKIE|CREDENTIAL|API_KEY|ACCESS_TOKEN|REFRESH_TOKEN|TOKEN|"
    r"PASSWORD|PASSWD|SECRET|PRIVATE_KEY|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN"
    r")\s+(?=\S)"
)
_SAFE_STRUCTURED_LOG_KEYS = frozenset(
    {
        "code",
        "component",
        "event",
        "level",
        "logger",
        "message",
        "msg",
        "name",
        "pid",
        "service",
        "service_id",
        "status",
        "time",
        "timestamp",
    }
)
_URI_RE = re.compile(
    r"(?<![A-Za-z0-9+.-])(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)"
    r"(?P<authority>[^\s/?#<>{}\"]*)"
    r"(?P<path>/[^\s?#<>{}\"]*)?"
    r"(?P<query>\?(?:<redacted>|[^\s#<>{}\"]*))?"
    r"(?P<fragment>#(?:<redacted>|[^\s<>{}\"]*))?"
)
_ABSOLUTE_PATH_RE = re.compile(r"(?<![A-Za-z0-9_.:/-])/(?:[^\s/:]+/)+[^\s,:;]+")
_MAX_LEDGER_BYTES = 1 * 1024 * 1024
_MAX_FRAMEWORK_LOCK_BYTES = 4 * 1024 * 1024
_ROOT_MODE = 0o700
_FILE_MODE = 0o600
_MAX_LOG_LINE_BYTES = 16_384
_MIN_SENSITIVE_CREDENTIAL_PREFIX_BYTES = 8


def _state_digest(value: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError("digest must be a lowercase SHA-256 digest")
    return value


class SupervisorError(CoreServiceControlError):
    """Base class for internal supervisor failures."""


class SupervisorStateError(SupervisorError):
    """Private service state is unsafe, corrupt, or replaced."""


class SupervisorBusyError(SupervisorError):
    """Another Core daemon owns the host-global service root."""


class ServiceExecutionMode(StrEnum):
    CODEX_SUBSCRIPTION_TRANSCRIPT = "codex_subscription_transcript"
    SELF_DEPLOYED = "self-deployed"


class ServiceRunReadinessCode(StrEnum):
    READY = "ready"
    CODEX_CLI_UNAVAILABLE = "codex_cli_unavailable"
    CODEX_SUBSCRIPTION_AUTH_UNAVAILABLE = "codex_subscription_auth_unavailable"
    RUNTIME_EXECUTABLE_UNAVAILABLE = "runtime_executable_unavailable"
    RUNTIME_IMAGE_UNAVAILABLE = "runtime_image_unavailable"
    RUNTIME_EVIDENCE_INVALID = "runtime_evidence_invalid"
    SERVICE_GROUP_UNAVAILABLE = "service_group_unavailable"
    RUN_ADMISSION_UNAVAILABLE = "run_admission_unavailable"
    SELF_DEPLOYED_UNAVAILABLE = "self_deployed_unavailable"


class ServiceLaunchMode(StrEnum):
    RELEASE = "release"
    DEVELOPMENT_TEST = "development_test"


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
    session_id: int
    process_group_id: int
    ownership_digest: str

    def __post_init__(self) -> None:
        if self.pid <= 0:
            raise ValueError("pid must be positive")
        _require_digest(self.birth_token, "birth_token")
        if self.session_id <= 0 or self.process_group_id <= 0:
            raise ValueError("process session/group identity must be positive")
        _require_digest(self.ownership_digest, "ownership_digest")


@dataclass(frozen=True, slots=True)
class ServiceHealthProbe:
    kind: HealthProbeKind
    url: str | None = None
    expected_service_id: str | None = None
    required_worker_id: str | None = None
    expected_gateway_url: str | None = None

    @classmethod
    def process(cls) -> ServiceHealthProbe:
        return cls(kind=HealthProbeKind.PROCESS)

    @classmethod
    def http(
        cls,
        url: str,
        *,
        expected_service_id: str,
        required_worker_id: str | None = None,
        expected_gateway_url: str | None = None,
    ) -> ServiceHealthProbe:
        if not url.startswith("http://127.0.0.1:"):
            raise ValueError("service health URLs must use loopback HTTP")
        return cls(
            kind=HealthProbeKind.HTTP,
            url=url,
            expected_service_id=expected_service_id,
            required_worker_id=required_worker_id,
            expected_gateway_url=expected_gateway_url,
        )


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
    cwd: str = "/"
    internal_identity: InternalServiceIdentity | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    listen_fd: int | None = field(default=None, repr=False, compare=False)
    codex_credential_authority: HeldCodexCredentialAuthority | None = field(
        default=None,
        repr=False,
        compare=False,
    )

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
        if not os.path.isabs(self.cwd) or any(ord(char) < 0x20 for char in self.cwd):
            raise ValueError("service cwd must be an absolute safe path")


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
    code: ServiceRunReadinessCode
    identity_digest: str | None
    message: str
    credential_authority: HeldCodexCredentialAuthority | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if self.ready != (self.code is ServiceRunReadinessCode.READY):
            raise ValueError("managed runtime readiness code does not match ready state")
        if self.ready != (self.identity_digest is not None):
            raise ValueError("ready managed runtime evidence requires an identity digest")
        if self.ready != (self.credential_authority is not None):
            raise ValueError("ready managed runtime evidence requires held credential authority")
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
    def run(
        self,
        argv: tuple[str, ...],
        deadline: float,
        cancellation: threading.Event | None = None,
    ) -> ProbeCommandResult: ...


class ManagedScienceRuntimeProbe(Protocol):
    def verify(
        self,
        request: ManagedScienceRuntimeRequest,
        deadline: float,
        cancellation: threading.Event | None = None,
    ) -> ManagedScienceRuntimeReadiness: ...


class ProcessBackend(Protocol):
    def spawn(
        self,
        spec: ServiceProcessSpec,
        on_output: Callable[[ProcessIdentity, bytes], None],
        on_exit: Callable[[ProcessIdentity, int], None],
    ) -> ProcessIdentity: ...

    def is_alive(self, identity: ProcessIdentity) -> bool: ...

    def terminate(self, identity: ProcessIdentity) -> None: ...

    def kill(self, identity: ProcessIdentity) -> None: ...

    def wait(self, identity: ProcessIdentity, timeout: float | None) -> int | None: ...

    def recover_stale_group(self, identity: ProcessIdentity, deadline: float) -> bool: ...


class HealthChecker(Protocol):
    def wait_ready(
        self,
        spec: ServiceProcessSpec,
        identity: ProcessIdentity,
        process_backend: ProcessBackend,
        deadline: float,
        cancellation: threading.Event | None = None,
    ) -> HealthCheckResult: ...


class PortProbe(Protocol):
    def reserve(self, host: str) -> socket.socket: ...


class BoundedProbeCommandRunner:
    def __init__(self, *, max_output_bytes: int = 1 * 1024 * 1024) -> None:
        if not 1 <= max_output_bytes <= 16 * 1024 * 1024:
            raise ValueError("probe output limit is outside the supported bounds")
        self._max_output_bytes = max_output_bytes

    def run(
        self,
        argv: tuple[str, ...],
        deadline: float,
        cancellation: threading.Event | None = None,
    ) -> ProbeCommandResult:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ProbeCommandResult(124, b"", b"bootstrap probe deadline exceeded")
        process: subprocess.Popen[bytes] | None = None
        selector: selectors.BaseSelector | None = None
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_controlled_environment(),
                cwd="/",
                start_new_session=True,
            )
            stdout = process.stdout
            stderr = process.stderr
            if stdout is None or stderr is None:
                raise OSError("bootstrap probe pipes are unavailable")
            output = {"stdout": bytearray(), "stderr": bytearray()}
            aggregate = 0
            selector = selectors.DefaultSelector()
            for name, stream in (("stdout", stdout), ("stderr", stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map():
                if cancellation is not None and cancellation.is_set():
                    self._kill_group_and_reap(process)
                    return ProbeCommandResult(
                        130, bytes(output["stdout"]), bytes(output["stderr"])
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._kill_group_and_reap(process)
                    return ProbeCommandResult(
                        124, bytes(output["stdout"]), bytes(output["stderr"])
                    )
                for key, _events in selector.select(min(0.05, remaining)):
                    stream = key.fileobj
                    try:
                        chunk = os.read(
                            stream.fileno(),
                            min(64 * 1024, self._max_output_bytes - aggregate + 1),
                        )
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(stream)
                        stream.close()
                        continue
                    aggregate += len(chunk)
                    if aggregate > self._max_output_bytes:
                        self._kill_group_and_reap(process)
                        return ProbeCommandResult(
                            125,
                            b"",
                            b"bootstrap probe output exceeded its aggregate limit",
                        )
                    output[key.data].extend(chunk)
            while True:
                if cancellation is not None and cancellation.is_set():
                    self._kill_group_and_reap(process)
                    return ProbeCommandResult(
                        130, bytes(output["stdout"]), bytes(output["stderr"])
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._kill_group_and_reap(process)
                    return ProbeCommandResult(
                        124, bytes(output["stdout"]), bytes(output["stderr"])
                    )
                try:
                    returncode = process.wait(timeout=min(0.05, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
            return ProbeCommandResult(returncode, bytes(output["stdout"]), bytes(output["stderr"]))
        except OSError as exc:
            if process is not None and process.poll() is None:
                self._kill_group_and_reap(process)
            return ProbeCommandResult(
                124,
                b"",
                str(exc).encode("utf-8", errors="replace"),
            )
        finally:
            if selector is not None:
                selector.close()
            if process is not None:
                for stream in (process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

    @staticmethod
    def _kill_group_and_reap(process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=0.75)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass


def _is_chatgpt_subscription_status(stdout: bytes, stderr: bytes) -> bool:
    try:
        statuses = [
            decoded for payload in (stdout, stderr) if (decoded := payload.decode("utf-8").strip())
        ]
    except UnicodeDecodeError:
        return False
    return statuses == ["Logged in using ChatGPT"]


class LocalManagedScienceRuntimeProbe:
    """Verify the runtime, managed image, and Codex subscription bootstrap."""

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
        cancellation: threading.Event | None = None,
    ) -> ManagedScienceRuntimeReadiness:
        codex = self._command_runner.run(("codex", "--version"), deadline, cancellation)
        if codex.returncode != 0:
            return ManagedScienceRuntimeReadiness(
                ready=False,
                code=ServiceRunReadinessCode.CODEX_CLI_UNAVAILABLE,
                identity_digest=None,
                message="Codex CLI is unavailable at the managed Science bootstrap boundary.",
            )
        auth = self._command_runner.run(("codex", "login", "status"), deadline, cancellation)
        if auth.returncode != 0 or not _is_chatgpt_subscription_status(auth.stdout, auth.stderr):
            return ManagedScienceRuntimeReadiness(
                ready=False,
                code=ServiceRunReadinessCode.CODEX_SUBSCRIPTION_AUTH_UNAVAILABLE,
                identity_digest=None,
                message="Codex subscription login is unavailable on the remote Core host.",
            )
        try:
            credential_authority = HeldCodexCredentialAuthority.open(self._codex_auth_path)
        except (OSError, SessionFileSecurityError, ValueError):
            return ManagedScienceRuntimeReadiness(
                ready=False,
                code=ServiceRunReadinessCode.CODEX_SUBSCRIPTION_AUTH_UNAVAILABLE,
                identity_digest=None,
                message="Codex subscription login evidence is invalid on the remote Core host.",
            )
        try:
            runtime = self._command_runner.run(("docker", "--version"), deadline, cancellation)
        except BaseException:
            credential_authority.close()
            raise
        if runtime.returncode != 0:
            credential_authority.close()
            return ManagedScienceRuntimeReadiness(
                ready=False,
                code=ServiceRunReadinessCode.RUNTIME_EXECUTABLE_UNAVAILABLE,
                identity_digest=None,
                message="The managed Science runtime executable is unavailable.",
            )
        try:
            image_result = self._command_runner.run(
                ("docker", "image", "inspect", request.runtime_image),
                deadline,
                cancellation,
            )
        except BaseException:
            credential_authority.close()
            raise
        if image_result.returncode != 0:
            credential_authority.close()
            return ManagedScienceRuntimeReadiness(
                ready=False,
                code=ServiceRunReadinessCode.RUNTIME_IMAGE_UNAVAILABLE,
                identity_digest=None,
                message="Managed Science runtime image is not prepared.",
            )
        try:
            image_payload = json.loads(image_result.stdout.decode("utf-8"))
            if not isinstance(image_payload, list) or len(image_payload) != 1:
                raise ValueError("Docker inspect returned an unexpected image set")
            image = image_payload[0]
            if not isinstance(image, dict):
                raise ValueError("Docker inspect image is not an object")
            image_id = image.get("Id")
            repo_digests = image.get("RepoDigests")
            config = image.get("Config")
            labels = config.get("Labels") if isinstance(config, dict) else None
            immutable_image = verified_managed_runtime_image_reference(
                profile="managed_science",
                image=request.runtime_image,
                image_id=image_id,
                repo_digests=repo_digests,
                labels=labels,
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
        ):
            credential_authority.close()
            return ManagedScienceRuntimeReadiness(
                ready=False,
                code=ServiceRunReadinessCode.RUNTIME_EVIDENCE_INVALID,
                identity_digest=None,
                message="Managed Science bootstrap evidence is invalid.",
            )
        return ManagedScienceRuntimeReadiness(
            ready=True,
            code=ServiceRunReadinessCode.READY,
            identity_digest=_digest_json(
                {
                    "auth_content_sha256": credential_authority.content_sha256,
                    "auth_identity": credential_authority.identity,
                    "codex_model": request.codex_model,
                    "codex_version_output_digest": hashlib.sha256(codex.stdout).hexdigest(),
                    "runtime_version_output_digest": hashlib.sha256(runtime.stdout).hexdigest(),
                    "runtime_image": request.runtime_image,
                    "runtime_image_id": image_id,
                    "runtime_image_immutable_reference": immutable_image,
                }
            ),
            message="Managed Science runtime bootstrap is verified.",
            credential_authority=credential_authority,
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
    services_available: bool
    run_ready: bool
    run_readiness_code: ServiceRunReadinessCode
    generation_digest: str
    services: tuple[SupervisorServiceSummary, ...]
    runtime_image: str | None
    runtime_identity_digest: str | None
    status_message: str | None = None

    def __post_init__(self) -> None:
        if self.run_ready != (self.run_readiness_code is ServiceRunReadinessCode.READY):
            raise ValueError("service run readiness code does not match ready state")
        if self.run_ready and (
            not self.services_available
            or self.runtime_image not in set(MANAGED_RUNTIME_IMAGES.values())
            or self.runtime_identity_digest is None
        ):
            raise ValueError("run-ready service group lacks runtime evidence")

    def service(self, service_id: str) -> SupervisorServiceSummary:
        for service in self.services:
            if service.id == service_id:
                return service
        raise KeyError(service_id)


@dataclass(frozen=True, slots=True)
class ServiceRunBinding:
    """Ephemeral trusted connection from the run owner to one service generation."""

    execution_mode: ServiceExecutionMode
    runtime_image: str
    runtime_identity_digest: str
    generation_digest: str
    registry_digest: str
    framework_lock_digest: str
    rollout_url: str
    evolution_backend_url: str
    gateway_url: str
    _identity: InternalServiceIdentity = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.execution_mode, ServiceExecutionMode):
            raise ValueError("service run binding execution mode is invalid")
        if self.runtime_image not in set(MANAGED_RUNTIME_IMAGES.values()):
            raise ValueError("service run binding image is not Core-managed")
        _require_digest(self.runtime_identity_digest, "runtime_identity_digest")

    def request_headers(self) -> dict[str, str]:
        return self._identity.request_headers()


@dataclass(slots=True)
class ServiceRunLease:
    """Process-local lease preventing replacement of one bound run generation."""

    binding: ServiceRunBinding
    _release: Callable[[], None] = field(repr=False, compare=False)
    _closed: bool = field(default=False, init=False, repr=False, compare=False)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._release()


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
    session_id: int | None = Field(default=None, gt=0)
    process_group_id: int | None = Field(default=None, gt=0)
    ownership_digest: str | None = None
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

    @field_validator("ownership_digest")
    @classmethod
    def _optional_ownership_digest(cls, value: str | None) -> str | None:
        return None if value is None else _state_digest(value)

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
    runtime_readiness_code: ServiceRunReadinessCode | None = None
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

    @field_validator("runtime_readiness_code", mode="before")
    @classmethod
    def _readiness_from_canonical_text(cls, value: object) -> object:
        return ServiceRunReadinessCode(value) if isinstance(value, str) else value


@dataclass(slots=True)
class _TrackedProcess:
    process: subprocess.Popen[bytes]
    identity: ProcessIdentity
    callback_complete: bool = False


class _BoundedLogStreamRedactor:
    """Sanitize complete log lines while retaining a strictly bounded carry."""

    def __init__(self, credential: str, *, max_line_bytes: int = _MAX_LOG_LINE_BYTES) -> None:
        if not 1 <= max_line_bytes <= _MAX_LOG_LINE_BYTES:
            raise ValueError("log line limit is outside the supported bounds")
        self._credential = credential.encode("utf-8")
        self._max_line_bytes = max_line_bytes
        self._carry = bytearray()
        self._dropping_oversize = False

    @property
    def buffered_bytes(self) -> int:
        return len(self._carry)

    def feed(self, payload: bytes) -> bytes:
        output = bytearray()
        offset = 0
        while offset < len(payload):
            newline = payload.find(b"\n", offset)
            if self._dropping_oversize:
                if newline < 0:
                    return bytes(output)
                output.extend(b"<redacted-oversize-line>\n")
                self._dropping_oversize = False
                offset = newline + 1
                continue
            segment_end = len(payload) if newline < 0 else newline
            segment = payload[offset:segment_end]
            if len(self._carry) + len(segment) > self._max_line_bytes:
                self._carry.clear()
                if newline < 0:
                    self._dropping_oversize = True
                    return bytes(output)
                output.extend(b"<redacted-oversize-line>\n")
                offset = newline + 1
                continue
            self._carry.extend(segment)
            if newline < 0:
                break
            output.extend(self._sanitize_line(bytes(self._carry), eof=False))
            self._carry.clear()
            offset = newline + 1
        return bytes(output)

    def flush(self) -> bytes:
        if self._dropping_oversize:
            self._dropping_oversize = False
            self._carry.clear()
            return b"<redacted-oversize-line>\n"
        if not self._carry:
            return b""
        line = bytes(self._carry)
        self._carry.clear()
        return self._sanitize_line(line, eof=True)

    def _sanitize_line(self, line: bytes, *, eof: bool) -> bytes:
        line = line.replace(self._credential, b"<redacted>")
        if eof and len(self._credential) > _MIN_SENSITIVE_CREDENTIAL_PREFIX_BYTES:
            prefix_size = min(len(line), len(self._credential) - 1)
            while (
                prefix_size >= _MIN_SENSITIVE_CREDENTIAL_PREFIX_BYTES
                and not self._credential.startswith(line[-prefix_size:])
            ):
                prefix_size -= 1
            if prefix_size >= _MIN_SENSITIVE_CREDENTIAL_PREFIX_BYTES:
                line = line[:-prefix_size] + b"<redacted>"
        safe = _sanitize(line.decode("utf-8", errors="replace"))
        return (safe.encode("utf-8") + b"\n") if safe else b""


class RealSubprocessBackend:
    """Controlled process-group launcher with Linux PID birth identity."""

    def __init__(self, *, max_tracked_processes: int = 64) -> None:
        if not 1 <= max_tracked_processes <= 1024:
            raise ValueError("tracked process limit is outside the supported bounds")
        self._lock = threading.RLock()
        self._tracked: dict[str, _TrackedProcess] = {}
        self._completed: OrderedDict[str, tuple[ProcessIdentity, int]] = OrderedDict()
        self._max_tracked_processes = max_tracked_processes
        self._spawn_reservations = 0

    def spawn(
        self,
        spec: ServiceProcessSpec,
        on_output: Callable[[ProcessIdentity, bytes], None],
        on_exit: Callable[[ProcessIdentity, int], None],
    ) -> ProcessIdentity:
        if not sys.platform.startswith("linux"):
            raise SupervisorStateError("release process-group supervision requires Linux")
        self._reserve_spawn_slot()
        parent_pid = os.getpid()
        child_env = dict(spec.env)
        child_env[INTERNAL_OWNERSHIP_ENV] = spec.identity_digest
        pass_fds: list[int] = []
        credential_read_fd: int | None = None
        cwd_fd: int | None = None
        process: subprocess.Popen[bytes] | None = None
        try:
            cwd_fd = _open_absolute_nofollow_directory(Path(spec.cwd), "service cwd")
            pass_fds.append(cwd_fd)
            if spec.internal_identity is not None:
                credential_read_fd, credential_write_fd = os.pipe2(os.O_CLOEXEC)
                try:
                    os.write(credential_write_fd, spec.internal_identity.inherited_payload())
                finally:
                    os.close(credential_write_fd)
                child_env[INTERNAL_CREDENTIAL_FD_ENV] = str(credential_read_fd)
                pass_fds.append(credential_read_fd)
            if spec.codex_credential_authority is not None:
                authority_fd = spec.codex_credential_authority.inheritance_descriptor()
                child_env[CODEX_CREDENTIAL_AUTHORITY_FD_ENV] = str(authority_fd)
                pass_fds.append(authority_fd)
            if spec.listen_fd is not None:
                child_env[INTERNAL_LISTEN_FD_ENV] = str(spec.listen_fd)
                pass_fds.append(spec.listen_fd)
            try:
                process = subprocess.Popen(
                    spec.argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    env=child_env,
                    close_fds=True,
                    pass_fds=tuple(pass_fds),
                    start_new_session=True,
                    preexec_fn=_linux_child_setup(parent_pid, cwd_fd),
                )
            finally:
                if credential_read_fd is not None:
                    os.close(credential_read_fd)
                if cwd_fd is not None:
                    os.close(cwd_fd)
        except Exception:
            if process is not None:
                self._kill_spawned_process_group(process)
            self._release_spawn_slot()
            raise
        try:
            assert process is not None
            identity = ProcessIdentity(
                pid=process.pid,
                birth_token=_process_birth_token(process.pid),
                session_id=os.getsid(process.pid),
                process_group_id=os.getpgid(process.pid),
                ownership_digest=spec.identity_digest,
            )
            if identity.session_id != process.pid or identity.process_group_id != process.pid:
                raise SupervisorStateError("child did not establish a dedicated process group")
        except Exception:
            if process is not None:
                self._kill_spawned_process_group(process)
            self._release_spawn_slot()
            raise
        tracked = _TrackedProcess(process=process, identity=identity)
        with self._lock:
            self._tracked[identity.birth_token] = tracked
            self._spawn_reservations -= 1
        output_thread = threading.Thread(
            target=self._read_output,
            args=(tracked, on_output),
            name=f"openevo-log-{spec.service_id}",
            daemon=True,
        )
        monitor_thread = threading.Thread(
            target=self._monitor,
            args=(tracked, output_thread, on_exit),
            name=f"openevo-monitor-{spec.service_id}",
            daemon=True,
        )
        output_started = False
        try:
            output_thread.start()
            output_started = True
            monitor_thread.start()
        except Exception:
            self._kill_spawned_process_group(process)
            if output_started:
                output_thread.join(timeout=0.5)
            with self._lock:
                tracked.callback_complete = True
            self._retire_if_complete(identity)
            raise
        return identity

    def is_alive(self, identity: ProcessIdentity) -> bool:
        tracked = self._owned(identity)
        if tracked is None:
            return False
        members = _owned_process_group_members(identity)
        if members is None:
            raise SupervisorStateError("managed process-group identity could not be verified")
        alive = bool(members)
        if not alive:
            self._retire_if_complete(identity)
        return alive

    def terminate(self, identity: ProcessIdentity) -> None:
        if self.is_alive(identity):
            os.killpg(identity.process_group_id, signal.SIGTERM)

    def kill(self, identity: ProcessIdentity) -> None:
        if self.is_alive(identity):
            os.killpg(identity.process_group_id, signal.SIGKILL)

    def wait(self, identity: ProcessIdentity, timeout: float | None) -> int | None:
        completed_returncode = self._completed_returncode(identity)
        if completed_returncode is not None:
            return completed_returncode
        tracked = self._owned(identity)
        if tracked is None:
            return self._completed_returncode(identity)
        deadline = None if timeout is None else time.monotonic() + timeout
        returncode: int | None = tracked.process.poll()
        while True:
            if returncode is None:
                returncode = tracked.process.poll()
            members = _owned_process_group_members(identity)
            if members == ():
                result = returncode if returncode is not None else 0
                self._retire_if_complete(identity)
                return result
            if members is None:
                completed_returncode = self._completed_returncode(identity)
                if completed_returncode is not None:
                    return completed_returncode
                if deadline is None or time.monotonic() >= deadline:
                    return None
                time.sleep(0.01)
                continue
            if deadline is not None and time.monotonic() >= deadline:
                return None
            time.sleep(0.01)

    def recover_stale_group(self, identity: ProcessIdentity, deadline: float) -> bool:
        members = _owned_process_group_members(identity)
        if members == ():
            return True
        if members is None:
            return False
        try:
            os.killpg(identity.process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            return True
        while time.monotonic() < deadline:
            members = _owned_process_group_members(identity)
            if members == ():
                return True
            if members is None:
                return False
            time.sleep(0.02)
        members = _owned_process_group_members(identity)
        if members is None:
            return False
        if members:
            os.killpg(identity.process_group_id, signal.SIGKILL)
        while time.monotonic() < deadline:
            members = _owned_process_group_members(identity)
            if members == ():
                return True
            if members is None:
                return False
            time.sleep(0.01)
        return _owned_process_group_members(identity) == ()

    def _owned(self, identity: ProcessIdentity) -> _TrackedProcess | None:
        with self._lock:
            tracked = self._tracked.get(identity.birth_token)
        if tracked is None or tracked.identity != identity:
            return None
        return tracked

    def _reserve_spawn_slot(self) -> None:
        with self._lock:
            self._reclaim_completed_locked()
            if len(self._tracked) + self._spawn_reservations >= self._max_tracked_processes:
                raise SupervisorStateError("tracked process capacity is exhausted")
            self._spawn_reservations += 1

    def _release_spawn_slot(self) -> None:
        with self._lock:
            if self._spawn_reservations > 0:
                self._spawn_reservations -= 1

    def _reclaim_completed_locked(self) -> None:
        for birth_token, tracked in tuple(self._tracked.items()):
            if (
                tracked.callback_complete
                and tracked.process.poll() is not None
                and _owned_process_group_members(tracked.identity) == ()
            ):
                self._tracked.pop(birth_token, None)
                self._remember_completed_locked(tracked)

    def _retire_if_complete(self, identity: ProcessIdentity) -> None:
        with self._lock:
            tracked = self._tracked.get(identity.birth_token)
            if (
                tracked is not None
                and tracked.identity == identity
                and tracked.callback_complete
                and tracked.process.poll() is not None
                and _owned_process_group_members(identity) == ()
            ):
                self._tracked.pop(identity.birth_token, None)
                self._remember_completed_locked(tracked)

    def _remember_completed_locked(self, tracked: _TrackedProcess) -> None:
        returncode = tracked.process.poll()
        if returncode is None:
            return
        self._completed[tracked.identity.birth_token] = (tracked.identity, returncode)
        self._completed.move_to_end(tracked.identity.birth_token)
        while len(self._completed) > self._max_tracked_processes:
            self._completed.popitem(last=False)

    def _completed_returncode(self, identity: ProcessIdentity) -> int | None:
        with self._lock:
            completed = self._completed.get(identity.birth_token)
            if completed is None or completed[0] != identity:
                return None
            self._completed.move_to_end(identity.birth_token)
            return completed[1]

    @staticmethod
    def _kill_spawned_process_group(process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass

    @staticmethod
    def _read_output(
        tracked: _TrackedProcess,
        on_output: Callable[[ProcessIdentity, bytes], None],
    ) -> None:
        stream = tracked.process.stdout
        if stream is None:
            return
        try:
            while chunk := stream.readline(16_385):
                on_output(tracked.identity, chunk)
        finally:
            stream.close()

    def _monitor(
        self,
        tracked: _TrackedProcess,
        output_thread: threading.Thread,
        on_exit: Callable[[ProcessIdentity, int], None],
    ) -> None:
        returncode = tracked.process.wait()
        output_thread.join()
        try:
            on_exit(tracked.identity, returncode)
        finally:
            with self._lock:
                current = self._tracked.get(tracked.identity.birth_token)
                if current is tracked:
                    tracked.callback_complete = True
            self._retire_if_complete(tracked.identity)


class DefaultHealthChecker:
    def __init__(self, *, poll_interval: float = 0.05) -> None:
        self._poll_interval = poll_interval

    def wait_ready(
        self,
        spec: ServiceProcessSpec,
        identity: ProcessIdentity,
        process_backend: ProcessBackend,
        deadline: float,
        cancellation: threading.Event | None = None,
    ) -> HealthCheckResult:
        process_observations = 0
        last_message = "health check has not completed"
        while True:
            if cancellation is not None and cancellation.is_set():
                return HealthCheckResult(False, "service readiness was cancelled")
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
                ready, last_message = _probe_http(spec, deadline - now)
                if ready:
                    return HealthCheckResult(True, last_message)
            time.sleep(min(self._poll_interval, max(0.0, deadline - time.monotonic())))


class SocketPortProbe:
    def reserve(self, host: str) -> socket.socket:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        candidate = socket.socket(family, socket.SOCK_STREAM)
        try:
            candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            candidate.bind((host, 0))
            candidate.listen(2048)
            return candidate
        except Exception:
            candidate.close()
            raise


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


class _VerifiedFrameworkLock:
    """Hold and re-hash the exact private framework lock across generations."""

    def __init__(self, path: Path) -> None:
        self.path = Path(os.path.abspath(os.fspath(path)))
        self.fd = _open_absolute_nofollow_file(self.path, "framework lock")
        try:
            info = os.fstat(self.fd)
            _require_private_file(info, os.getuid(), "framework lock")
            if info.st_size > _MAX_FRAMEWORK_LOCK_BYTES:
                raise SupervisorStateError("framework lock exceeds its byte limit")
            self.identity = _fd_identity(self.fd)
            self.payload = os.pread(self.fd, info.st_size + 1, 0)
            if len(self.payload) != info.st_size:
                raise SupervisorStateError("framework lock changed while it was read")
            self.digest = hashlib.sha256(self.payload).hexdigest()
        except Exception:
            os.close(self.fd)
            raise

    def verified_payload(self) -> bytes:
        current_fd = _open_absolute_nofollow_file(self.path, "framework lock")
        try:
            if _fd_identity(current_fd) != self.identity:
                raise SupervisorStateError("framework lock pathname binding changed")
            info = os.fstat(current_fd)
            _require_private_file(info, os.getuid(), "framework lock")
            if info.st_size > _MAX_FRAMEWORK_LOCK_BYTES:
                raise SupervisorStateError("framework lock exceeds its byte limit")
            payload = os.pread(current_fd, info.st_size + 1, 0)
            if len(payload) != info.st_size or hashlib.sha256(payload).hexdigest() != self.digest:
                raise SupervisorStateError("framework lock content changed")
            held_info = os.fstat(self.fd)
            held_payload = os.pread(self.fd, held_info.st_size + 1, 0)
            if held_payload != payload:
                raise SupervisorStateError("held framework lock no longer matches its pathname")
            return payload
        finally:
            os.close(current_fd)

    def close(self) -> None:
        os.close(self.fd)


class CoreServiceSupervisor:
    """Recoverable owner for Core's evolution/rollout/gateway/worker group."""

    def __init__(
        self,
        *,
        launch_mode: ServiceLaunchMode,
        service_root: Path,
        framework_lock: Path,
        release_identity: ServiceReleaseIdentity | None = None,
        verified_registry: object | None = None,
        run_admission_url: str | None = None,
        python_executable: str | None = None,
        process_backend: ProcessBackend | None = None,
        health_checker: HealthChecker | None = None,
        port_probe: PortProbe | None = None,
        managed_runtime_probe: ManagedScienceRuntimeProbe | None = None,
        startup_timeout: float = 30.0,
        stop_timeout: float = 5.0,
        max_log_entries: int = 512,
        max_log_bytes: int = 512 * 1024,
        max_restart_operations: int = 256,
    ) -> None:
        if startup_timeout <= 0 or stop_timeout <= 0:
            raise ValueError("supervisor timeouts must be positive")
        if not 1 <= max_log_entries <= 10_000 or not 1 <= max_log_bytes <= 1_048_576:
            raise ValueError("supervisor log limits are outside the supported bounds")
        if not 1 <= max_restart_operations <= 4096:
            raise ValueError("restart operation limit is outside the supported bounds")
        launch_mode = ServiceLaunchMode(launch_mode)
        injected_runtime_dependencies = any(
            dependency is not None
            for dependency in (
                python_executable,
                process_backend,
                health_checker,
                port_probe,
                managed_runtime_probe,
            )
        )
        if launch_mode is ServiceLaunchMode.RELEASE:
            if verified_registry is None or release_identity is not None:
                raise ValueError("release mode requires only a verified registry identity source")
            if injected_runtime_dependencies:
                raise ValueError("release mode rejects development runtime dependency injection")
            resolved_release_identity = release_identity_from_verified_registry(verified_registry)
        else:
            if release_identity is None or verified_registry is not None:
                raise ValueError(
                    "development/test mode requires an explicit release identity and no registry"
                )
            resolved_release_identity = release_identity
        self._mutex = threading.RLock()
        self._root = _SecureServiceRoot(service_root)
        self._closed = False
        try:
            self._framework_lock_source = _VerifiedFrameworkLock(Path(framework_lock))
            self._framework_lock_digest = self._framework_lock_source.digest
            self._managed_framework_lock = self._root.path / "framework-lock.json"
            self._launch_mode = launch_mode
            self._verified_registry = verified_registry
            self._run_admission_url = (
                _validated_run_admission_url(run_admission_url)
                if run_admission_url is not None
                else None
            )
            self._release_identity = resolved_release_identity
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
            self._max_restart_operations = max_restart_operations
            self._root.ensure_directory("child-cwd")
            self._child_cwd = self._root.path / "child-cwd"
            self._handles: dict[str, ProcessIdentity] = {}
            self._specs: dict[str, ServiceProcessSpec] = {}
            self._output_redactors: dict[
                tuple[str, str, ProcessIdentity], _BoundedLogStreamRedactor
            ] = {}
            self._restart_results: dict[str, tuple[str, SupervisorServiceSummary]] = {}
            self._active_plan_key: str | None = None
            self._active_credential: str | None = None
            self._active_runtime_request: ManagedScienceRuntimeRequest | None = None
            self._active_credential_authority: HeldCodexCredentialAuthority | None = None
            self._active_run_lease: object | None = None
            self._active_cancellation: threading.Event | None = None
            self._ledger = self._load_or_initialize_ledger()
            self._recover_prior_owner_state()
        except Exception:
            source = getattr(self, "_framework_lock_source", None)
            if source is not None:
                source.close()
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
        return self._ensure_locked(
            execution_mode,
            model_ref=model_ref,
            codex_model=codex_model,
            runtime_image=runtime_image,
            total_timeout=total_timeout,
            force_restart=False,
        )

    def ensure_run_binding(
        self,
        execution_mode: ServiceExecutionMode,
        *,
        model_ref: str | None = None,
        codex_model: str | None = None,
        runtime_image: str | None = None,
        total_timeout: float | None = None,
    ) -> tuple[ServiceGroupSnapshot, ServiceRunLease | None]:
        """Ensure readiness and issue its exact binding under one lifecycle lock."""

        with self._mutex:
            snapshot = self._ensure_locked(
                execution_mode,
                model_ref=model_ref,
                codex_model=codex_model,
                runtime_image=runtime_image,
                total_timeout=total_timeout,
                force_restart=False,
            )
            if not snapshot.run_ready:
                return snapshot, None
            if self._active_run_lease is not None:
                raise SupervisorStateError("managed service generation already has a run lease")
            binding = self._run_binding_locked()
            if not _binding_matches_snapshot(snapshot, binding):
                raise SupervisorStateError(
                    "managed service generation changed while issuing a run binding"
                )
            token = object()
            self._active_run_lease = token
            return snapshot, ServiceRunLease(
                binding=binding,
                _release=lambda: self._release_run_lease(token),
            )

    def _ensure_locked(
        self,
        execution_mode: ServiceExecutionMode,
        *,
        model_ref: str | None = None,
        codex_model: str | None = None,
        runtime_image: str | None = None,
        total_timeout: float | None = None,
        force_restart: bool,
    ) -> ServiceGroupSnapshot:
        with self._mutex:
            self._require_open()
            self._verify_release_installation()
            cancellation = threading.Event()
            self._active_cancellation = cancellation
            if total_timeout is not None and total_timeout <= 0:
                raise ValueError("total_timeout must be positive")
            deadline = time.monotonic() + (
                self._startup_timeout if total_timeout is None else total_timeout
            )
            lock_payload = self._framework_lock_source.verified_payload()
            self._root.atomic_write("framework-lock.json", lock_payload)
            if execution_mode is ServiceExecutionMode.SELF_DEPLOYED:
                if self._active_run_lease is not None:
                    raise SupervisorStateError(
                        "managed service generation is leased to an active run"
                    )
                return self._ensure_self_deployed_unavailable(model_ref, deadline)
            if codex_model is None or runtime_image is None:
                raise ValueError(
                    "subscription ensure requires codex_model and managed runtime_image"
                )
            runtime_request = ManagedScienceRuntimeRequest(
                runtime_image=runtime_image,
                codex_model=codex_model,
            )
            runtime = self._managed_runtime_probe.verify(
                runtime_request,
                deadline,
                cancellation,
            )
            candidate_authority = runtime.credential_authority
            if cancellation.is_set():
                if candidate_authority is not None:
                    candidate_authority.close()
                self._raise_if_cancelled(cancellation)
            plan_runtime_identity = runtime.identity_digest or _digest_json(
                {
                    "codex_model": runtime_request.codex_model,
                    "runtime_image": runtime_request.runtime_image,
                    "unverified": True,
                }
            )
            plan_key = _digest_json(
                {
                    "codex_model": runtime_request.codex_model,
                    "framework_lock_digest": self._framework_lock_digest,
                    "install_digest": self._release_identity.install_digest,
                    "registry_digest": self._release_identity.registry_digest,
                    "runtime_identity_digest": plan_runtime_identity,
                    "runtime_image": runtime_request.runtime_image,
                }
            )
            if self._active_run_lease is not None:
                if (
                    force_restart
                    or not runtime.ready
                    or self._active_plan_key != plan_key
                    or self._active_credential is None
                    or candidate_authority is None
                    or not self._active_credential_authority_matches(candidate_authority)
                    or not self._specs
                    or self._ledger.generation_digest is None
                    or not self._is_current_group_healthy_with_candidate(
                        execution_mode,
                        self._ledger.generation_digest,
                        tuple(self._specs.values()),
                        deadline,
                        cancellation,
                        candidate_authority,
                    )
                ):
                    if candidate_authority is not None:
                        candidate_authority.close()
                    raise SupervisorStateError(
                        "managed service generation is leased to an active run"
                    )
                candidate_authority.close()
                return self._group_snapshot()
            if (
                not force_restart
                and runtime.ready
                and self._active_plan_key == plan_key
                and self._active_credential is not None
                and candidate_authority is not None
                and self._active_credential_authority_matches(candidate_authority)
                and self._specs
                and self._ledger.generation_digest is not None
                and self._is_current_group_healthy_with_candidate(
                    execution_mode,
                    self._ledger.generation_digest,
                    tuple(self._specs.values()),
                    deadline,
                    cancellation,
                    candidate_authority,
                )
            ):
                if cancellation.is_set():
                    candidate_authority.close()
                    self._raise_if_cancelled(cancellation)
                candidate_authority.close()
                return self._group_snapshot()
            try:
                stopped = self._stop_all(deadline)
            except BaseException:
                if candidate_authority is not None:
                    candidate_authority.close()
                raise
            if not stopped:
                if candidate_authority is not None:
                    candidate_authority.close()
                self._ledger.group_status_message = (
                    "Existing managed children could not be stopped; service start aborted."
                )
                self._persist()
                return self._group_snapshot()
            self._release_active_credential_authority()
            listeners: dict[str, socket.socket] = {}
            try:
                for service_id in ("evolution-backend", "rollout", "gateway"):
                    self._raise_if_cancelled(cancellation)
                    listeners[service_id] = self._port_probe.reserve("127.0.0.1")
            except SupervisorStateError:
                for listener in listeners.values():
                    listener.close()
                if candidate_authority is not None:
                    candidate_authority.close()
                raise
            except Exception as exc:
                for listener in listeners.values():
                    listener.close()
                if candidate_authority is not None:
                    candidate_authority.close()
                raise SupervisorStateError("internal listener reservation failed") from exc
            credential = secrets.token_urlsafe(48)
            generation_digest = _digest_json(
                {
                    "auth_digest": hashlib.sha256(credential.encode("utf-8")).hexdigest(),
                    "plan_key": plan_key,
                }
            )
            try:
                specs, topology = self._subscription_plan(
                    runtime_request,
                    plan_runtime_identity,
                    generation_digest,
                    credential,
                    listeners,
                    candidate_authority,
                )
            except Exception:
                for listener in listeners.values():
                    listener.close()
                if candidate_authority is not None:
                    candidate_authority.close()
                raise
            self._specs = {spec.service_id: spec for spec in specs}
            self._active_plan_key = plan_key
            self._active_credential = credential
            self._active_runtime_request = runtime_request
            self._active_credential_authority = candidate_authority
            if not runtime.ready:
                try:
                    self._install_planned_records(
                        execution_mode,
                        generation_digest,
                        specs,
                        runtime_identity_digest=None,
                        runtime_readiness_code=runtime.code,
                        group_status_message=_sanitize(runtime.message),
                    )
                    for record in self._ledger.services:
                        record.status = ServiceStatus.UNAVAILABLE
                        record.status_message = _sanitize(runtime.message)
                        record.restartable = False
                    self._specs = {}
                    self._persist()
                    return self._group_snapshot()
                finally:
                    for listener in listeners.values():
                        listener.close()
            self._install_planned_records(
                execution_mode,
                generation_digest,
                specs,
                runtime_identity_digest=runtime.identity_digest,
                runtime_readiness_code=runtime.code,
                group_status_message=None,
            )
            self._root.ensure_directory("evolution")
            self._root.ensure_directory("rollout")
            self._root.atomic_write("topology.json", _canonical_bytes(topology))
            started: list[str] = []
            try:
                for spec in specs:
                    self._raise_if_cancelled(cancellation)
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
                            lambda process_identity, payload, service_id=spec.service_id: (
                                self._record_output(
                                    service_id,
                                    generation_digest,
                                    process_identity,
                                    payload,
                                )
                            ),
                            lambda process_identity, returncode, service_id=spec.service_id: (
                                self._record_exit(
                                    service_id,
                                    generation_digest,
                                    process_identity,
                                    returncode,
                                )
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
                    if spec.service_id in listeners:
                        listeners[spec.service_id].close()
                    self._handles[spec.service_id] = identity
                    self._write_process_identity(self._record(spec.service_id), identity)
                    self._persist()
                    started.append(spec.service_id)
                    health = self._health_checker.wait_ready(
                        spec,
                        identity,
                        self._process_backend,
                        deadline,
                        cancellation,
                    )
                    self._raise_if_cancelled(cancellation)
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
                if isinstance(self._health_checker, DefaultHealthChecker):
                    rollout_spec = self._specs["rollout"]
                    graph_ready, graph_message = _probe_rollout_registration(
                        rollout_spec,
                        deadline - time.monotonic(),
                    )
                    if not graph_ready:
                        self._fail_record("rollout", "service_graph_not_ready", graph_message)
                        self._rollback(started, deadline)
                return self._group_snapshot()
            except SupervisorStateError:
                if cancellation.is_set():
                    self._rollback(
                        started,
                        min(
                            deadline, time.monotonic() + self._stop_timeout * max(1, len(started))
                        ),
                    )
                raise
            finally:
                for listener in listeners.values():
                    listener.close()

    def list(self) -> tuple[SupervisorServiceSummary, ...]:
        with self._mutex:
            self._require_open()
            self._refresh_process_state()
            return tuple(self._summary(record) for record in self._ledger.services)

    def run_binding(self) -> ServiceRunBinding:
        with self._mutex:
            return self._run_binding_locked()

    def _run_binding_locked(self) -> ServiceRunBinding:
        self._require_open()
        self._verify_release_installation()
        self._require_active_credential_authority()
        self._refresh_process_state()
        snapshot = self._group_snapshot()
        credential = self._active_credential
        runtime_request = self._active_runtime_request
        runtime_identity = snapshot.runtime_identity_digest
        if (
            not snapshot.run_ready
            or credential is None
            or runtime_request is None
            or runtime_identity is None
        ):
            raise SupervisorStateError("managed service group is not ready for a run")
        specs = self._specs
        required = {"evolution-backend", "rollout", "gateway"}
        if not required.issubset(specs):
            raise SupervisorStateError("managed run service endpoints are unavailable")
        ports = {service_id: specs[service_id].port for service_id in required}
        if any(port is None for port in ports.values()):
            raise SupervisorStateError("managed run service ports are unavailable")
        identity = InternalServiceIdentity(
            service_id="core-control",
            generation_digest=snapshot.generation_digest,
            registry_digest=self._release_identity.registry_digest,
            framework_lock_digest=self._framework_lock_digest,
            credential=credential,
        )
        return ServiceRunBinding(
            execution_mode=snapshot.execution_mode,
            runtime_image=runtime_request.runtime_image,
            runtime_identity_digest=runtime_identity,
            generation_digest=snapshot.generation_digest,
            registry_digest=self._release_identity.registry_digest,
            framework_lock_digest=self._framework_lock_digest,
            rollout_url=f"http://127.0.0.1:{ports['rollout']}",
            evolution_backend_url=f"http://127.0.0.1:{ports['evolution-backend']}",
            gateway_url=f"http://127.0.0.1:{ports['gateway']}",
            _identity=identity,
        )

    def _release_run_lease(self, token: object) -> None:
        with self._mutex:
            if self._active_run_lease is token:
                self._active_run_lease = None

    def authenticates_run_service(self, headers: Mapping[str, str]) -> bool:
        with self._mutex:
            if self._closed:
                return False
            try:
                self._require_active_credential_authority()
            except SupervisorStateError:
                return False
            credential = self._active_credential
            generation = self._ledger.generation_digest
            if credential is None or generation is None:
                return False
            identity = InternalServiceIdentity(
                service_id="core-control",
                generation_digest=generation,
                registry_digest=self._release_identity.registry_digest,
                framework_lock_digest=self._framework_lock_digest,
                credential=credential,
            )
            normalized = {str(key).lower(): str(value) for key, value in headers.items()}
            return identity.authenticates(normalized)

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
            self._verify_release_installation()
            if total_timeout is not None and total_timeout <= 0:
                raise ValueError("total_timeout must be positive")
            prior = self._restart_results.get(operation_id)
            if prior is not None:
                prior_service_id, prior_result = prior
                if prior_service_id != service_id:
                    raise SupervisorStateError(
                        "restart operation identity was reused for a different request"
                    )
                return prior_result
            record = self._record(service_id)
            if not record.restartable:
                raise SupervisorStateError("service is not restartable in its current state")
            if len(self._restart_results) >= self._max_restart_operations:
                raise SupervisorBusyError("restart operation replay capacity is exhausted")
            runtime_request = self._active_runtime_request
            if runtime_request is None:
                raise SupervisorStateError("subscription runtime request is unavailable")
            snapshot = self._ensure_locked(
                ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT,
                codex_model=runtime_request.codex_model,
                runtime_image=runtime_request.runtime_image,
                total_timeout=total_timeout,
                force_restart=True,
            )
            result = snapshot.service(service_id)
            self._restart_results[operation_id] = (service_id, result)
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
            self._release_active_credential_authority()
            self._restart_results.clear()
            self._closed = True
            self._framework_lock_source.close()
            self._root.close()

    def cancel(self, *, total_timeout: float | None = None) -> None:
        cancellation = self._active_cancellation
        if cancellation is not None:
            cancellation.set()
        self.close(total_timeout=total_timeout)

    def __enter__(self) -> CoreServiceSupervisor:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _abandon_for_test(self) -> None:
        """Release only the owner lock; tests use this to emulate abrupt Core death."""
        with self._mutex:
            self._release_active_credential_authority()
            self._closed = True
            self._framework_lock_source.close()
            self._root.close()

    def _subscription_plan(
        self,
        runtime_request: ManagedScienceRuntimeRequest,
        runtime_identity_digest: str,
        generation_digest: str,
        credential: str,
        listeners: Mapping[str, socket.socket],
        credential_authority: HeldCodexCredentialAuthority | None,
    ) -> tuple[tuple[ServiceProcessSpec, ...], dict[str, object]]:
        root = self._root.path
        topology_path = root / "topology.json"
        evolution_root = root / "evolution"
        ports = {
            service_id: int(listener.getsockname()[1])
            for service_id, listener in listeners.items()
        }
        evolution_url = f"http://127.0.0.1:{ports['evolution-backend']}"
        rollout_url = f"http://127.0.0.1:{ports['rollout']}"
        gateway_url = f"http://127.0.0.1:{ports['gateway']}"
        topology: dict[str, object] = {
            "gateway": {
                "heartbeat_interval_seconds": 2,
                "nodes": [
                    {
                        "host": "127.0.0.1",
                        "id": "core-gateway",
                        "inference": {
                            "base_url": "http://127.0.0.1:1",
                            "engine": "vllm",
                        },
                        "model_served": runtime_request.codex_model,
                        "port": ports["gateway"],
                        "public_url": gateway_url,
                    }
                ],
                "rollout_server_url": rollout_url,
            },
            "rollout": {
                "host": "127.0.0.1",
                "port": ports["rollout"],
                "public_url": rollout_url,
                "save_dir": os.fspath(root / "rollout"),
            },
        }
        base_env = _controlled_environment()
        if self._run_admission_url is not None:
            base_env[CORE_RUN_ADMISSION_URL_ENV] = self._run_admission_url
        plans = (
            (
                "evolution-backend",
                "Evolution backend",
                ServiceComponent.EVOLUTION_BACKEND,
                (
                    self._python,
                    "-I",
                    "-m",
                    "openevo.evolution.cli",
                    "serve",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(ports["evolution-backend"]),
                    "--db",
                    os.fspath(evolution_root / "evolution.db"),
                    "--artifact-root",
                    os.fspath(evolution_root / "artifacts"),
                    "--framework-lock",
                    os.fspath(self._managed_framework_lock),
                ),
                ports["evolution-backend"],
                ServiceHealthProbe.http(
                    f"{evolution_url}/v1/health",
                    expected_service_id="evolution-backend",
                ),
            ),
            (
                "rollout",
                "Rollout service",
                ServiceComponent.ROLLOUT,
                (
                    self._python,
                    "-I",
                    "-m",
                    "openevo.rollout.server",
                    "--config",
                    os.fspath(topology_path),
                    "--log-level",
                    "info",
                ),
                ports["rollout"],
                ServiceHealthProbe.http(
                    f"{rollout_url}/health",
                    expected_service_id="rollout",
                    expected_gateway_url=gateway_url,
                ),
            ),
            (
                "gateway",
                "Gateway service",
                ServiceComponent.GATEWAY,
                (
                    self._python,
                    "-I",
                    "-m",
                    "openevo.gateway.server",
                    "--config",
                    os.fspath(topology_path),
                    "--node-id",
                    "core-gateway",
                    "--log-level",
                    "info",
                ),
                ports["gateway"],
                ServiceHealthProbe.http(
                    f"{gateway_url}/health",
                    expected_service_id="gateway",
                ),
            ),
            (
                "evolution-worker",
                "Evolution worker",
                ServiceComponent.EVOLUTION_WORKER,
                (
                    self._python,
                    "-I",
                    "-m",
                    "openevo.evolution.cli",
                    "worker",
                    "--base-url",
                    evolution_url,
                    "--worker-id",
                    "core-reference-worker",
                    "--artifact-root",
                    os.fspath(evolution_root / "artifacts"),
                    "--framework-lock",
                    os.fspath(self._managed_framework_lock),
                ),
                None,
                ServiceHealthProbe.http(
                    f"{evolution_url}/v1/health",
                    expected_service_id="evolution-backend",
                    required_worker_id="core-reference-worker",
                ),
            ),
        )
        topology_digest = _digest_json(topology)
        specs = []
        for service_id, display_name, component, argv, port, health_probe in plans:
            internal_identity = InternalServiceIdentity(
                service_id=service_id,
                generation_digest=generation_digest,
                registry_digest=self._release_identity.registry_digest,
                framework_lock_digest=self._framework_lock_digest,
                credential=credential,
            )
            argv_digest = _digest_json(list(argv))
            env_digest = _digest_json(dict(sorted(base_env.items())))
            identity_digest = _digest_json(
                {
                    "argv_digest": argv_digest,
                    "component": component.value,
                    "cwd": os.fspath(self._child_cwd),
                    "env_digest": env_digest,
                    "framework_lock_digest": self._framework_lock_digest,
                    "install_digest": self._release_identity.install_digest,
                    "port": port,
                    "registry_digest": self._release_identity.registry_digest,
                    "runtime_identity_digest": runtime_identity_digest,
                    "runtime_image": runtime_request.runtime_image,
                    "service_id": service_id,
                    "topology_digest": topology_digest,
                    "generation_digest": generation_digest,
                    "auth_digest": internal_identity.auth_digest,
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
                    cwd=os.fspath(self._child_cwd),
                    internal_identity=internal_identity,
                    listen_fd=(
                        listeners[service_id].fileno() if service_id in listeners else None
                    ),
                    codex_credential_authority=(
                        credential_authority if service_id == "gateway" else None
                    ),
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
        self._release_active_credential_authority()
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
        self._ledger.runtime_readiness_code = ServiceRunReadinessCode.SELF_DEPLOYED_UNAVAILABLE
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
        cancellation: threading.Event,
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
                cancellation,
            )
            self._raise_if_cancelled(cancellation)
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
        runtime_readiness_code: ServiceRunReadinessCode,
        group_status_message: str | None,
    ) -> None:
        now = _timestamp()
        self._ledger.execution_mode = execution_mode
        self._ledger.generation_digest = generation_digest
        self._ledger.runtime_identity_digest = runtime_identity_digest
        self._ledger.runtime_readiness_code = runtime_readiness_code
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

    def _active_credential_authority_matches(
        self,
        candidate: HeldCodexCredentialAuthority,
    ) -> bool:
        active = self._active_credential_authority
        if (
            active is None
            or active.identity != candidate.identity
            or active.content_sha256 != candidate.content_sha256
        ):
            return False
        try:
            active.verify()
            candidate.verify()
        except SessionFileSecurityError:
            return False
        return True

    def _is_current_group_healthy_with_candidate(
        self,
        execution_mode: ServiceExecutionMode,
        generation_digest: str,
        specs: tuple[ServiceProcessSpec, ...],
        deadline: float,
        cancellation: threading.Event,
        candidate: HeldCodexCredentialAuthority,
    ) -> bool:
        try:
            return self._is_current_group_healthy(
                execution_mode,
                generation_digest,
                specs,
                deadline,
                cancellation,
            )
        except BaseException:
            candidate.close()
            raise

    def _require_active_credential_authority(self) -> None:
        authority = self._active_credential_authority
        if authority is None:
            raise SupervisorStateError("managed credential authority is unavailable")
        try:
            authority.verify()
        except SessionFileSecurityError as exc:
            raise SupervisorStateError("managed credential authority changed") from exc

    def _release_active_credential_authority(self) -> None:
        authority = self._active_credential_authority
        self._active_credential_authority = None
        if authority is not None:
            authority.close()

    def _stop_service(
        self,
        service_id: str,
        deadline: float,
        *,
        preserve_failure: bool = False,
    ) -> None:
        record = self._record(service_id)
        identity = self._handles.get(service_id)
        if identity is not None and self._ledger.generation_digest is not None:
            self._flush_output(service_id, self._ledger.generation_digest, identity)
        self._handles.pop(service_id, None)
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
            self._write_process_identity(record, identity)
            record.status = ServiceStatus.FAILED
            record.error_code = "service_stop_timeout"
            record.status_message = "Managed child did not exit before the stop deadline."
            record.restartable = False
        elif preserve_failure:
            self._clear_process_identity(record)
            record.status = ServiceStatus.FAILED
            record.error_code, record.status_message = prior_error
        else:
            self._clear_process_identity(record)
            record.status = ServiceStatus.STOPPED
            record.error_code = None
            record.status_message = "Managed service is stopped."
        self._persist()

    def _record_output(
        self,
        service_id: str,
        generation_digest: str,
        identity: ProcessIdentity,
        payload: bytes,
    ) -> None:
        with self._mutex:
            if (
                self._closed
                or self._ledger.generation_digest != generation_digest
                or self._handles.get(service_id) != identity
            ):
                return
            credential = self._active_credential
            if credential is None:
                return
            key = (service_id, generation_digest, identity)
            redactor = self._output_redactors.get(key)
            if redactor is None:
                redactor = _BoundedLogStreamRedactor(credential)
                self._output_redactors[key] = redactor
            redacted = redactor.feed(payload)
            self._append_output_payload(service_id, redacted)
            self._persist()

    def _append_output_payload(self, service_id: str, payload: bytes) -> None:
        if not payload:
            return
        decoded = payload.decode("utf-8", errors="replace")
        lines = decoded.splitlines() or [decoded]
        for line in lines:
            self._append_log(service_id, "info", line)

    def _flush_output(
        self,
        service_id: str,
        generation_digest: str,
        identity: ProcessIdentity,
    ) -> None:
        redactor = self._output_redactors.pop(
            (service_id, generation_digest, identity),
            None,
        )
        if redactor is not None:
            self._append_output_payload(service_id, redactor.flush())

    def _record_exit(
        self,
        service_id: str,
        generation_digest: str,
        identity: ProcessIdentity,
        returncode: int,
    ) -> None:
        with self._mutex:
            if (
                self._closed
                or self._ledger.generation_digest != generation_digest
                or self._handles.get(service_id) != identity
            ):
                return
            record = self._record_or_none(service_id)
            if record is None or not self._record_matches_identity(record, identity):
                return
            self._flush_output(service_id, generation_digest, identity)
            record.status = ServiceStatus.FAILED
            record.error_code = "service_process_exited"
            record.status_message = f"Managed process exited with status {returncode}."
            record.updated_at = _timestamp()
            try:
                group_alive = self._process_backend.is_alive(identity)
            except SupervisorStateError:
                group_alive = True
                record.error_code = "service_process_group_identity_unverified"
                record.status_message = (
                    "Managed leader exited and its process group could not be verified."
                )
            if not group_alive:
                self._clear_process_identity(record)
                if self._handles.get(service_id) == identity:
                    self._handles.pop(service_id, None)
            self._append_log(service_id, "error", record.status_message)
            self._persist()

    def _append_log(self, service_id: str, level: str, message: str) -> None:
        record = self._record(service_id)
        if self._active_credential is not None:
            message = message.replace(self._active_credential, "<redacted>")
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
                self._clear_process_identity(record)
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
        services_available = bool(services) and all(
            service.status is ServiceStatus.RUNNING for service in services
        )
        runtime_code = self._ledger.runtime_readiness_code
        if (
            services_available
            and self._ledger.runtime_identity_digest is not None
            and runtime_code is ServiceRunReadinessCode.READY
            and self._run_admission_url is not None
        ):
            run_ready = True
            readiness_code = ServiceRunReadinessCode.READY
            message = None
        elif runtime_code not in {None, ServiceRunReadinessCode.READY}:
            run_ready = False
            readiness_code = runtime_code
            message = self._ledger.group_status_message or (
                "Managed Science runtime prerequisites are unavailable."
            )
        elif services_available and self._run_admission_url is None:
            run_ready = False
            readiness_code = ServiceRunReadinessCode.RUN_ADMISSION_UNAVAILABLE
            message = "The generation-bound science run admission owner is unavailable."
        else:
            run_ready = False
            readiness_code = ServiceRunReadinessCode.SERVICE_GROUP_UNAVAILABLE
            message = self._ledger.group_status_message or (
                "One or more required Core services are not ready."
            )
        return ServiceGroupSnapshot(
            execution_mode=execution_mode,
            services_available=services_available,
            run_ready=run_ready,
            run_readiness_code=readiness_code,
            generation_digest=generation,
            services=services,
            runtime_image=(
                self._active_runtime_request.runtime_image
                if execution_mode is ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT
                and self._active_runtime_request is not None
                else None
            ),
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
            if ledger.runtime_identity_digest is not None or ledger.runtime_readiness_code not in {
                None,
                ServiceRunReadinessCode.SELF_DEPLOYED_UNAVAILABLE,
            }:
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
            if (
                ledger.runtime_identity_digest is not None
                and ledger.runtime_readiness_code not in {None, ServiceRunReadinessCode.READY}
            ) or (
                ledger.runtime_identity_digest is None
                and ledger.runtime_readiness_code is ServiceRunReadinessCode.READY
            ):
                raise SupervisorStateError(
                    "subscription runtime readiness evidence is inconsistent"
                )
        elif (
            ledger.runtime_identity_digest is not None or ledger.runtime_readiness_code is not None
        ):
            raise SupervisorStateError("unbound service ledger has runtime evidence")
        service_ids = [record.service_id for record in ledger.services]
        if len(service_ids) != len(set(service_ids)):
            raise SupervisorStateError("service ledger contains duplicate service identities")
        for record in ledger.services:
            process_fields = (
                record.pid,
                record.birth_token,
                record.session_id,
                record.process_group_id,
                record.ownership_digest,
            )
            if any(value is not None for value in process_fields) and not all(
                value is not None for value in process_fields
            ):
                raise SupervisorStateError("service ledger process identity is incomplete")
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
                if record.pid is not None:
                    identity = ProcessIdentity(
                        pid=record.pid,
                        birth_token=record.birth_token or "",
                        session_id=record.session_id or 0,
                        process_group_id=record.process_group_id or 0,
                        ownership_digest=record.ownership_digest or "",
                    )
                    recovered = self._process_backend.recover_stale_group(
                        identity,
                        time.monotonic() + self._stop_timeout,
                    )
                    if not recovered:
                        raise SupervisorStateError(
                            "persisted process group could not be verified and recovered"
                        )
                record.status = ServiceStatus.FAILED
                record.error_code = "service_prior_owner_lost"
                record.status_message = (
                    "Prior Core ownership was lost; its verified stale process group was reaped."
                )
                self._clear_process_identity(record)
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

    @staticmethod
    def _write_process_identity(record: _LedgerService, identity: ProcessIdentity) -> None:
        record.pid = identity.pid
        record.birth_token = identity.birth_token
        record.session_id = identity.session_id
        record.process_group_id = identity.process_group_id
        record.ownership_digest = identity.ownership_digest

    @staticmethod
    def _clear_process_identity(record: _LedgerService) -> None:
        record.pid = None
        record.birth_token = None
        record.session_id = None
        record.process_group_id = None
        record.ownership_digest = None

    @staticmethod
    def _record_matches_identity(
        record: _LedgerService,
        identity: ProcessIdentity,
    ) -> bool:
        return (
            record.pid == identity.pid
            and record.birth_token == identity.birth_token
            and record.session_id == identity.session_id
            and record.process_group_id == identity.process_group_id
            and record.ownership_digest == identity.ownership_digest
        )

    def _require_open(self) -> None:
        if self._closed:
            raise SupervisorStateError("service supervisor is closed")
        self._root.verify()

    def _verify_release_installation(self) -> None:
        if self._launch_mode is not ServiceLaunchMode.RELEASE:
            return
        try:
            current = release_identity_from_verified_registry(self._verified_registry)
        except Exception as exc:
            raise SupervisorStateError(
                "verified release installation could not be revalidated"
            ) from exc
        if current != self._release_identity:
            raise SupervisorStateError("verified release installation identity changed")

    @staticmethod
    def _raise_if_cancelled(cancellation: threading.Event) -> None:
        if cancellation.is_set():
            raise SupervisorStateError("service operation was cancelled")


def release_identity_from_verified_registry(registry: object) -> ServiceReleaseIdentity:
    """Derive service identity only from a loader-sealed executable registry."""

    from openevo.evolution.framework.builtins import require_verified_executable_registry
    from openevo.evolution.framework.loading import _reverify_distribution_inventory

    verified = require_verified_executable_registry(registry)  # type: ignore[arg-type]
    for attestation in verified.distribution_attestations.values():
        _reverify_distribution_inventory(attestation)
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
    allowed = ("HOME", "LANG", "LC_ALL", "PATH", "TMPDIR")
    result = {key: os.environ[key] for key in allowed if key in os.environ}
    result.setdefault("PATH", os.defpath)
    result["PYTHONNOUSERSITE"] = "1"
    result["PYTHONSAFEPATH"] = "1"
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


def _validated_run_admission_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("run admission URL is invalid") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != "/internal/v1/run-admissions/verify"
    ):
        raise ValueError("run admission URL must be the Core loopback verifier")
    return f"http://127.0.0.1:{port}/internal/v1/run-admissions/verify"


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


def _open_absolute_nofollow_directory(path: Path, label: str) -> int:
    absolute = Path(os.path.abspath(os.fspath(path)))
    parts = absolute.parts[1:]
    current = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    if not parts:
        return current
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        for index, part in enumerate(parts):
            try:
                next_fd = os.open(part, flags, dir_fd=current)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise SupervisorStateError(f"{label} path contains a symlink") from exc
                raise SupervisorStateError(f"{label} path is unavailable") from exc
            os.close(current)
            current = next_fd
            if index < len(parts) - 1:
                _require_safe_ancestor(os.fstat(current))
        _require_private_directory(os.fstat(current), os.getuid(), label)
        return current
    except Exception:
        os.close(current)
        raise


def _probe_http(spec: ServiceProcessSpec, remaining: float) -> tuple[bool, str]:
    probe = spec.health_probe
    url = probe.url
    identity = spec.internal_identity
    if url is None or identity is None or probe.expected_service_id is None:
        return False, "health probe URL is missing"
    timeout = max(0.01, min(1.0, remaining))
    try:
        with urlopen(
            Request(url, method="GET", headers=identity.request_headers()),
            timeout=timeout,
        ) as response:
            if 200 <= response.status < 300:
                raw = response.read(65_537)
                if len(raw) > 65_536:
                    return False, "HTTP health response exceeded its limit"
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, dict):
                    return False, "HTTP health response is not an object"
                actual = payload.get("internal_identity")
                expected = identity.health_identity()
                expected["service_id"] = probe.expected_service_id
                if actual != expected:
                    return False, "HTTP health identity mismatch"
                if (
                    probe.expected_service_id == "evolution-backend"
                    and payload.get("registry_digest") != identity.registry_digest
                ):
                    return False, "HTTP health registry mismatch"
                if probe.expected_service_id == "gateway" and (
                    payload.get("rollout_connected") is not True
                    or payload.get("capture_mode") != "transcript"
                    or payload.get("token_level_metrics_available") is not False
                    or payload.get("direct_model_api") is not False
                ):
                    return False, "gateway is not connected in transcript-only mode"
                if probe.required_worker_id is not None:
                    workers = payload.get("workers")
                    expected_worker = {
                        "framework_lock_digest": identity.framework_lock_digest,
                        "generation_digest": identity.generation_digest,
                        "registry_digest": identity.registry_digest,
                        "worker_id": probe.required_worker_id,
                    }
                    if not isinstance(workers, list) or expected_worker not in workers:
                        return False, "evolution worker is not registered"
                return True, "authenticated service identity is healthy"
            return False, f"HTTP health probe returned {response.status}"
    except (
        HTTPError,
        URLError,
        TimeoutError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        return False, _safe_message("HTTP health probe failed", exc)


def _probe_rollout_registration(
    spec: ServiceProcessSpec,
    remaining: float,
) -> tuple[bool, str]:
    ready, message = _probe_http(spec, remaining)
    if not ready:
        return ready, message
    if spec.health_probe.url is None or spec.internal_identity is None:
        return False, "rollout registration probe is incomplete"
    try:
        with urlopen(
            Request(
                spec.health_probe.url,
                method="GET",
                headers=spec.internal_identity.request_headers(),
            ),
            timeout=max(0.01, min(1.0, remaining)),
        ) as response:
            payload = json.loads(response.read(65_537).decode("utf-8"))
        registration = payload.get("gateway_registration")
        if registration != {
            "gateway_url": spec.health_probe.expected_gateway_url,
            "node_id": "core-gateway",
            "registered": True,
            "schedulable": True,
        }:
            return False, "rollout does not expose a schedulable registered gateway"
        return True, "rollout gateway registration is schedulable"
    except (
        HTTPError,
        URLError,
        TimeoutError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        AttributeError,
    ) as exc:
        return False, _safe_message("rollout registration probe failed", exc)


def _linux_child_setup(parent_pid: int, cwd_fd: int) -> Callable[[], None] | None:
    if not sys.platform.startswith("linux"):
        return None
    libc = ctypes.CDLL(None, use_errno=True)
    pr_set_pdeathsig = 1

    def setup() -> None:
        try:
            os.fchdir(cwd_fd)
            os.close(cwd_fd)
        except OSError:
            os._exit(127)
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


def _owned_process_group_members(identity: ProcessIdentity) -> tuple[int, ...] | None:
    """Return owned live group members, empty for absent, or None when unverified."""

    if not sys.platform.startswith("linux"):
        return None
    members: list[int] = []
    proc_root = Path("/proc")
    try:
        candidates = tuple(proc_root.iterdir())
    except OSError:
        return None
    for candidate in candidates:
        if not candidate.name.isdecimal():
            continue
        try:
            stat_text = (candidate / "stat").read_text(encoding="ascii")
            end = stat_text.rfind(")")
            fields = stat_text[end + 2 :].split()
            state = fields[0]
            process_group_id = int(fields[2])
            session_id = int(fields[3])
        except (OSError, UnicodeDecodeError, ValueError, IndexError):
            continue
        if state == "Z" or (
            process_group_id != identity.process_group_id or session_id != identity.session_id
        ):
            continue
        pid = int(candidate.name)
        try:
            if candidate.stat().st_uid != os.getuid():
                return None
            environment = (candidate / "environ").read_bytes().split(b"\0")
        except OSError:
            return None
        expected = f"{INTERNAL_OWNERSHIP_ENV}={identity.ownership_digest}".encode("ascii")
        if expected not in environment:
            return None
        if pid == identity.pid and not _same_process(identity):
            return None
        members.append(pid)
    return tuple(sorted(members))


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
    text = value.replace("\x00", "").strip()
    if text.startswith(("{", "[")):
        try:
            structured = json.loads(text)
        except json.JSONDecodeError:
            return "<redacted-structured-log>"
        serialized = json.dumps(
            _sanitize_structured_log(structured),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return serialized if len(serialized) <= 16_384 else "<redacted-oversize-structured-log>"
    return _sanitize_scalar_text(text, max_chars=16_384)


def _sanitize_scalar_text(value: str, *, max_chars: int) -> str:
    text = value.replace("\x00", "").replace("\r", " ").replace("\n", " ")
    text = _URI_RE.sub(_sanitize_uri_match, text)
    text = _redact_space_separated_secrets(text)
    text = _SECRET_RE.sub("<redacted>", text)
    text = _ABSOLUTE_PATH_RE.sub("<path>", text)
    return " ".join(text.split())[:max_chars]


def _redact_space_separated_secrets(value: str) -> str:
    output: list[str] = []
    cursor = 0
    while cursor < len(value):
        matches = tuple(
            match
            for pattern in (
                _SPACE_SEPARATED_OPTION_SECRET_PREFIX_RE,
                _SPACE_SEPARATED_ENV_SECRET_PREFIX_RE,
            )
            if (match := pattern.search(value, cursor)) is not None
        )
        if not matches:
            output.append(value[cursor:])
            break
        match = min(matches, key=lambda candidate: (candidate.start(), candidate.end()))
        output.append(value[cursor : match.end()])
        output.append("<redacted>")
        secret_start = match.end()
        quote = value[secret_start]
        if quote not in {"'", '"'}:
            secret_end = secret_start + 1
            while secret_end < len(value) and not value[secret_end].isspace():
                secret_end += 1
            cursor = secret_end
            continue
        secret_end = secret_start + 1
        while secret_end < len(value):
            character = value[secret_end]
            if character == "\\":
                secret_end += 2
                continue
            secret_end += 1
            if character == quote:
                while secret_end < len(value) and not value[secret_end].isspace():
                    secret_end += 1
                break
        else:
            cursor = len(value)
            continue
        cursor = secret_end
    return "".join(output)


def _sanitize_uri_match(match: re.Match[str]) -> str:
    authority = match.group("authority")
    if "@" in authority:
        _userinfo, host = authority.rsplit("@", 1)
        authority = f"<redacted>@{host}"
    suffix = ""
    if match.group("query") is not None:
        suffix = "?<redacted>"
    elif match.group("fragment") is not None:
        suffix = "#<redacted>"
    return f"{match.group('scheme')}{authority}{match.group('path') or ''}{suffix}"


def _sanitize_structured_log(value: object, *, key: str | None = None) -> object:
    if key is not None and _SECRET_KEY_RE.fullmatch(key) is not None:
        return "<redacted>"
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for raw_key, item in value.items():
            field_name = str(raw_key)
            safe_field_name = _sanitize_scalar_text(field_name, max_chars=256)
            if not safe_field_name:
                safe_field_name = "<redacted-field>"
            if _SECRET_KEY_RE.fullmatch(field_name) is not None:
                result[safe_field_name] = "<redacted>"
            elif field_name in _SAFE_STRUCTURED_LOG_KEYS:
                result[safe_field_name] = _sanitize_structured_log(item, key=field_name)
            else:
                result[safe_field_name] = "<redacted>"
        return result
    if isinstance(value, list):
        return ["<redacted>" for _item in value[:128]]
    if isinstance(value, str):
        return _sanitize_scalar_text(value, max_chars=4096)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return "<redacted>"


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


def _binding_matches_snapshot(
    snapshot: ServiceGroupSnapshot,
    binding: ServiceRunBinding,
) -> bool:
    return (
        snapshot.run_ready
        and binding.execution_mode is snapshot.execution_mode
        and binding.runtime_image == snapshot.runtime_image
        and binding.runtime_identity_digest == snapshot.runtime_identity_digest
        and binding.generation_digest == snapshot.generation_digest
    )


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
    "ServiceLaunchMode",
    "ServiceGroupSnapshot",
    "ServiceRunReadinessCode",
    "ServiceRunBinding",
    "ServiceRunLease",
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
