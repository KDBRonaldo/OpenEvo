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
import shutil
import socket
import stat
import subprocess
import sys
from tempfile import mkdtemp
import threading
import time
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from openevo.codex_models import validate_codex_model_ref
from openevo.backend.service_control import (
    CoreServiceControlError,
    ServiceRestartAttempt,
    ServiceRestartAttemptState,
)
from openevo.backend.workspace_handoff_v2 import WORKSPACE_HANDOFF_ROOT_ENV
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
from openevo.evolution.harness_service import CORE_GATEWAY_BASE_URL_ENV
from openevo.gateway.session_files import (
    CODEX_CREDENTIAL_AUTHORITY_FD_ENV,
    CODEX_CREDENTIAL_SNAPSHOT_FD_ENV,
    HeldCodexCredentialAuthority,
    PreparedCodexCredentialSnapshot,
    SessionFileSecurityError,
    capture_session_root_identity,
    remove_credential_tree,
    stage_codex_subscription_auth,
)
from openevo.runtime.managed import (
    MANAGED_CODEX_BINARY,
    MANAGED_CODEX_VERSION,
    require_immutable_managed_runtime_image,
    verified_managed_runtime_image_reference,
)
from openevo.runtime.self_deployed import (
    RELEASE_SELF_DEPLOYED_MODEL_PROFILES,
    SelfDeployedModelProfile,
    require_release_self_deployed_model_profile,
)
from openevo.runtime.self_deployed_cache import (
    SelfDeployedModelCacheError,
    prepare_release_model_snapshot,
)
from openevo.runtime.docker_host import (
    DOCKER_EXECUTABLE_PATH,
    DockerEngineAuthority,
    DockerExecutableAuthority,
    DockerHostPathError,
    DockerHostPathSpec,
    discover_docker_host_path,
    docker_cli_environment,
    docker_container_inspect_argv,
    docker_inspect_matches_current_container,
    docker_running_container_ids_argv,
    docker_self_inspect_argv,
    parse_docker_container_ids,
)
from openevo.internal_auth import (
    INTERNAL_CREDENTIAL_FD_ENV,
    INTERNAL_LISTEN_FD_ENV,
    INTERNAL_OWNERSHIP_ENV,
    CORE_RUN_ADMISSION_URL_ENV,
    InternalServiceIdentity,
)


_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_STRONG_ETAG_RE = re.compile(r'^"[0-9a-f]{64}"$')
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
_CODEX_VERSION_MAX_BYTES = 4096
_SEMVER_NUMBER = r"(?:0|[1-9][0-9]*)"
_SEMVER_PRERELEASE_IDENTIFIER = r"(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
_SEMVER_BUILD_IDENTIFIER = r"[0-9A-Za-z-]+"
_PROBE_EXECUTABLE_MAX_BYTES = 512 * 1024 * 1024
_CODEX_VERSION_RE = re.compile(
    rf"^codex(?:-cli)? {_SEMVER_NUMBER}\.{_SEMVER_NUMBER}\.{_SEMVER_NUMBER}"
    rf"(?:-{_SEMVER_PRERELEASE_IDENTIFIER}"
    rf"(?:\.{_SEMVER_PRERELEASE_IDENTIFIER})*)?"
    rf"(?:\+{_SEMVER_BUILD_IDENTIFIER}(?:\.{_SEMVER_BUILD_IDENTIFIER})*)?$"
)


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
    expected_model_id: str | None = None

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
        expected_model_id: str | None = None,
    ) -> ServiceHealthProbe:
        if not url.startswith("http://127.0.0.1:"):
            raise ValueError("service health URLs must use loopback HTTP")
        return cls(
            kind=HealthProbeKind.HTTP,
            url=url,
            expected_service_id=expected_service_id,
            required_worker_id=required_worker_id,
            expected_gateway_url=expected_gateway_url,
            expected_model_id=expected_model_id,
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
    codex_credential_authority: (
        HeldCodexCredentialAuthority | PreparedCodexCredentialSnapshot | None
    ) = field(
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
        model = validate_codex_model_ref(
            self.codex_model,
            field_name="subscription codex_model",
        )
        object.__setattr__(self, "codex_model", model)


@dataclass(frozen=True, slots=True)
class ManagedScienceRuntimeReadiness:
    ready: bool
    code: ServiceRunReadinessCode
    identity_digest: str | None
    runtime_image_immutable_reference: str | None
    message: str
    docker_host_path: DockerHostPathSpec | None = None
    credential_authority: HeldCodexCredentialAuthority | PreparedCodexCredentialSnapshot | None = (
        field(
            default=None,
            repr=False,
            compare=False,
        )
    )

    def __post_init__(self) -> None:
        if self.ready != (self.code is ServiceRunReadinessCode.READY):
            raise ValueError("managed runtime readiness code does not match ready state")
        if self.ready != (self.identity_digest is not None):
            raise ValueError("ready managed runtime evidence requires an identity digest")
        if self.ready != (self.runtime_image_immutable_reference is not None):
            raise ValueError("ready managed runtime evidence requires an immutable image")
        if self.runtime_image_immutable_reference is not None:
            require_immutable_managed_runtime_image(
                profile="managed_science",
                image=self.runtime_image_immutable_reference,
            )
        if self.ready != (self.credential_authority is not None):
            raise ValueError("ready managed runtime evidence requires held credential authority")
        if self.identity_digest is not None:
            _require_digest(self.identity_digest, "managed runtime identity_digest")
        if not self.message.strip() or len(self.message) > 256:
            raise ValueError("managed runtime readiness message is invalid")


@dataclass(frozen=True, slots=True)
class SelfDeployedRuntimeRequest:
    """Closed release profile selected for one managed local inference group."""

    profile_id: str
    runtime_image: str

    def __post_init__(self) -> None:
        require_release_self_deployed_model_profile(self.profile_id)
        if self.runtime_image not in set(MANAGED_RUNTIME_IMAGES.values()):
            raise ValueError("Self-Deployed runtime_image is not a managed Science image")


@dataclass(frozen=True, slots=True)
class SelfDeployedRuntimeReadiness:
    """Verified, non-secret host/model/serving inputs for Self-Deployed startup."""

    ready: bool
    code: ServiceRunReadinessCode
    identity_digest: str | None
    runtime_image_immutable_reference: str | None
    profile: SelfDeployedModelProfile | None
    model_cache_container_path: Path | None
    model_cache_daemon_path: Path | None
    daemon_container_id: str | None
    gpu_device_id: str | None
    message: str
    docker_host_path: DockerHostPathSpec | None = None

    def __post_init__(self) -> None:
        if self.ready != (self.code is ServiceRunReadinessCode.READY):
            raise ValueError("Self-Deployed readiness code does not match ready state")
        evidence = (
            self.identity_digest,
            self.runtime_image_immutable_reference,
            self.profile,
            self.model_cache_container_path,
            self.model_cache_daemon_path,
            self.daemon_container_id,
            self.gpu_device_id,
            self.docker_host_path,
        )
        if self.ready != all(value is not None for value in evidence):
            raise ValueError("ready Self-Deployed evidence is incomplete")
        if self.identity_digest is not None:
            _require_digest(self.identity_digest, "Self-Deployed runtime identity_digest")
        if self.runtime_image_immutable_reference is not None:
            require_immutable_managed_runtime_image(
                profile="managed_science",
                image=self.runtime_image_immutable_reference,
            )
        if self.profile is not None:
            self.profile.__post_init__()
        if self.docker_host_path is not None:
            if self.model_cache_container_path is None or self.model_cache_daemon_path is None:
                raise ValueError("Self-Deployed model cache mapping is incomplete")
            translated = self.docker_host_path.translate(self.model_cache_container_path)
            if translated != self.model_cache_daemon_path:
                raise ValueError("Self-Deployed model cache mapping is inconsistent")
        if (
            self.daemon_container_id is not None
            and re.fullmatch(r"[0-9a-f]{64}", self.daemon_container_id) is None
        ):
            raise ValueError("Self-Deployed Daemon container identity is invalid")
        if (
            self.gpu_device_id is not None
            and re.fullmatch(r"GPU-[0-9A-Fa-f-]{16,64}", self.gpu_device_id) is None
        ):
            raise ValueError("Self-Deployed GPU identity is invalid")
        if not self.message.strip() or len(self.message) > 256:
            raise ValueError("Self-Deployed runtime readiness message is invalid")


@dataclass(frozen=True, slots=True)
class ProbeCommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class ProbeCommandRunner(Protocol):
    def hold_executable(self, name: str) -> ProbeExecutableAuthority: ...

    def run(
        self,
        argv: tuple[str, ...],
        deadline: float,
        cancellation: threading.Event | None = None,
        *,
        env: Mapping[str, str] | None = None,
        pass_fds: tuple[int, ...] = (),
    ) -> ProbeCommandResult: ...


def _discover_docker_user_container_path(
    run_docker: Callable[
        [DockerEngineAuthority, tuple[str, ...], float, threading.Event | None],
        ProbeCommandResult,
    ],
    docker_engine: DockerEngineAuthority,
    *,
    namespace: str,
    deadline: float,
    cancellation: threading.Event | None,
    minimum_available_bytes: int = 512 * 1024 * 1024,
    preferred_container_path: Path | None = None,
) -> DockerHostPathSpec:
    """Pin this Daemon container, including uniquely matched custom hostnames."""

    current_hostname = socket.gethostname()
    try:
        self_inspect = docker_self_inspect_argv(current_hostname)
        result = run_docker(
            docker_engine,
            self_inspect[1:],
            deadline,
            cancellation,
        )
        if result.returncode != 0:
            raise DockerHostPathError("Docker could not inspect the Daemon user container")
        return discover_docker_host_path(
            result.stdout,
            namespace=namespace,
            hostname=current_hostname,
            minimum_available_bytes=minimum_available_bytes,
            preferred_container_path=preferred_container_path,
        )
    except DockerHostPathError:
        pass

    inventory_command = docker_running_container_ids_argv()
    inventory = run_docker(
        docker_engine,
        inventory_command[1:],
        deadline,
        cancellation,
    )
    if inventory.returncode != 0:
        raise DockerHostPathError("Docker could not enumerate running containers")

    matching_payloads: list[bytes] = []
    for container_id in parse_docker_container_ids(inventory.stdout):
        inspect_command = docker_container_inspect_argv(container_id)
        result = run_docker(
            docker_engine,
            inspect_command[1:],
            deadline,
            cancellation,
        )
        if result.returncode != 0:
            raise DockerHostPathError("Docker candidate inspection failed")
        if docker_inspect_matches_current_container(
            result.stdout,
            container_id=container_id,
            hostname=current_hostname,
        ):
            matching_payloads.append(result.stdout)
    if len(matching_payloads) != 1:
        raise DockerHostPathError(
            "Docker custom-hostname self-container evidence is missing or ambiguous"
        )
    return discover_docker_host_path(
        matching_payloads[0],
        namespace=namespace,
        hostname=current_hostname,
        minimum_available_bytes=minimum_available_bytes,
        allow_custom_hostname=True,
        preferred_container_path=preferred_container_path,
    )


class ProbeExecutableAuthority(Protocol):
    @property
    def identity_digest(self) -> str: ...

    def run(
        self,
        argv: tuple[str, ...],
        deadline: float,
        cancellation: threading.Event | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> ProbeCommandResult: ...

    def close(self) -> None: ...


class ManagedScienceRuntimeProbe(Protocol):
    def verify(
        self,
        request: ManagedScienceRuntimeRequest,
        deadline: float,
        cancellation: threading.Event | None = None,
    ) -> ManagedScienceRuntimeReadiness: ...


class SelfDeployedRuntimeProbe(Protocol):
    def verify(
        self,
        request: SelfDeployedRuntimeRequest,
        deadline: float,
        cancellation: threading.Event | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> SelfDeployedRuntimeReadiness: ...

    def remove_managed_container(self, generation_digest: str, deadline: float) -> bool: ...


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

    def hold_executable(self, name: str) -> ProbeExecutableAuthority:
        return _HeldProbeExecutable.open(name, self)

    def run(
        self,
        argv: tuple[str, ...],
        deadline: float,
        cancellation: threading.Event | None = None,
        *,
        env: Mapping[str, str] | None = None,
        pass_fds: tuple[int, ...] = (),
    ) -> ProbeCommandResult:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ProbeCommandResult(124, b"", b"bootstrap probe deadline exceeded")
        process: subprocess.Popen[bytes] | None = None
        selector: selectors.BaseSelector | None = None
        try:
            requested_env = dict(env or {})
            if requested_env == docker_cli_environment():
                process_env = requested_env
            else:
                process_env = _controlled_environment()
                for key, value in requested_env.items():
                    if key != "CODEX_HOME" or not os.path.isabs(value):
                        raise OSError("probe environment override is invalid")
                    if not value or any(ord(char) < 0x20 for char in value):
                        raise OSError("probe environment override is invalid")
                    process_env[key] = value
            if any(fd < 3 for fd in pass_fds) or len(set(pass_fds)) != len(pass_fds):
                raise OSError("probe inherited descriptor set is invalid")
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=process_env,
                cwd="/",
                close_fds=True,
                pass_fds=pass_fds,
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


class _HeldProbeExecutable:
    """One no-follow executable inode used by every command in a probe."""

    def __init__(
        self,
        *,
        descriptor: int,
        identity: tuple[int, int, int, int, int, int, int, int],
        content_sha256: str,
        runner: BoundedProbeCommandRunner,
    ) -> None:
        self._descriptor = descriptor
        self._identity = identity
        self._content_sha256 = content_sha256
        self._runner = runner
        self._closed = False
        self._identity_digest = _digest_json(
            {
                "content_sha256": content_sha256,
                "identity": identity,
            }
        )

    @classmethod
    def open(
        cls,
        name: str,
        runner: BoundedProbeCommandRunner,
    ) -> _HeldProbeExecutable:
        candidate = shutil.which(name, path=_controlled_environment().get("PATH"))
        if candidate is None:
            raise OSError(f"{name} executable is unavailable")
        resolved = Path(candidate).resolve(strict=True)
        descriptor = os.open(
            resolved,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
        try:
            opened = os.fstat(descriptor)
            identity = _probe_executable_identity(opened)
            content_sha256 = _digest_probe_executable(descriptor, opened.st_size)
            authority = cls(
                descriptor=descriptor,
                identity=identity,
                content_sha256=content_sha256,
                runner=runner,
            )
            authority._verify()
            descriptor = -1
            return authority
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    @property
    def identity_digest(self) -> str:
        return self._identity_digest

    def run(
        self,
        argv: tuple[str, ...],
        deadline: float,
        cancellation: threading.Event | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> ProbeCommandResult:
        self._verify()
        try:
            return self._runner.run(
                (f"/proc/self/fd/{self._descriptor}", *argv),
                deadline,
                cancellation,
                env=env,
                pass_fds=(self._descriptor,),
            )
        finally:
            self._verify()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        os.close(self._descriptor)

    def _verify(self) -> None:
        if self._closed:
            raise OSError("held probe executable is closed")
        opened = os.fstat(self._descriptor)
        if _probe_executable_identity(opened) != self._identity:
            raise OSError("held probe executable identity changed")
        if _digest_probe_executable(self._descriptor, opened.st_size) != self._content_sha256:
            raise OSError("held probe executable content changed")


def _probe_executable_identity(
    opened: os.stat_result,
) -> tuple[int, int, int, int, int, int, int, int]:
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_uid not in {0, os.geteuid()}
        or opened.st_nlink < 1
        or not (stat.S_IMODE(opened.st_mode) & 0o111)
        or opened.st_size <= 0
        or opened.st_size > _PROBE_EXECUTABLE_MAX_BYTES
    ):
        raise OSError("probe executable identity is invalid")
    return (
        opened.st_dev,
        opened.st_ino,
        opened.st_mode,
        opened.st_uid,
        opened.st_nlink,
        opened.st_size,
        opened.st_mtime_ns,
        opened.st_ctime_ns,
    )


def _digest_probe_executable(descriptor: int, expected_size: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while offset < expected_size:
        chunk = os.pread(descriptor, min(1024 * 1024, expected_size - offset), offset)
        if not chunk:
            raise OSError("probe executable digest ended early")
        digest.update(chunk)
        offset += len(chunk)
    if os.pread(descriptor, 1, expected_size):
        raise OSError("probe executable grew during digest")
    return digest.hexdigest()


def _valid_codex_version(
    result: ProbeCommandResult,
    *,
    expected_version: str | None = None,
) -> bool:
    if (
        result.returncode != 0
        or len(result.stdout) + len(result.stderr) > _CODEX_VERSION_MAX_BYTES
    ):
        return False
    try:
        stdout = result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return False
    line = stdout.removesuffix("\r\n").removesuffix("\n")
    if stdout not in {line, f"{line}\n", f"{line}\r\n"}:
        return False
    if not line or _CODEX_VERSION_RE.fullmatch(line) is None:
        return False
    if not line.startswith("codex-cli "):
        return False
    version = line.removeprefix("codex-cli ")
    if "-" in version or "+" in version:
        return False
    return expected_version is None or version == expected_version


def _command_evidence(result: ProbeCommandResult) -> dict[str, object]:
    return {
        "returncode": result.returncode,
        "stderr_hex": result.stderr.hex(),
        "stdout_hex": result.stdout.hex(),
    }


class LocalManagedScienceRuntimeProbe:
    """Verify the runtime, managed image, and Codex subscription bootstrap."""

    def __init__(
        self,
        *,
        command_runner: ProbeCommandRunner | None = None,
        codex_auth_path: Path | None = None,
        credential_probe_root: Path | None = None,
        runtime_namespace: str | None = None,
        require_docker_user_container: bool = False,
        preferred_container_path: Path | None = None,
    ) -> None:
        self._command_runner = command_runner or BoundedProbeCommandRunner()
        self._codex_auth_path = codex_auth_path or (Path.home() / ".codex" / "auth.json")
        self._credential_probe_root = credential_probe_root
        self._runtime_namespace = runtime_namespace
        self._require_docker_user_container = require_docker_user_container
        self._preferred_container_path = preferred_container_path

    def verify(
        self,
        request: ManagedScienceRuntimeRequest,
        deadline: float,
        cancellation: threading.Event | None = None,
    ) -> ManagedScienceRuntimeReadiness:
        try:
            codex_executable = self._command_runner.hold_executable("codex")
        except (OSError, ValueError):
            return _runtime_not_ready(
                ServiceRunReadinessCode.CODEX_CLI_UNAVAILABLE,
                "Codex CLI is unavailable at the managed Science bootstrap boundary.",
            )
        credential_snapshot: PreparedCodexCredentialSnapshot | None = None
        try:
            try:
                codex = codex_executable.run(("--version",), deadline, cancellation)
            except (OSError, ValueError):
                return _runtime_not_ready(
                    ServiceRunReadinessCode.CODEX_CLI_UNAVAILABLE,
                    "Codex CLI is unavailable at the managed Science bootstrap boundary.",
                )
            # The host CLI is only an authentication-status client. The exact
            # task-execution CLI is supplied by the immutable managed image and
            # is proved independently below. Host package managers may advance
            # this helper without changing the release execution authority.
            if not _valid_codex_version(codex):
                return _runtime_not_ready(
                    ServiceRunReadinessCode.CODEX_CLI_UNAVAILABLE,
                    "Codex CLI version evidence is invalid at the managed Science bootstrap boundary.",
                )

            credential_snapshot = self._prepare_login_snapshot()
            if credential_snapshot is None:
                return _runtime_not_ready(
                    ServiceRunReadinessCode.CODEX_SUBSCRIPTION_AUTH_UNAVAILABLE,
                    "Codex subscription login evidence is invalid on the remote Core host.",
                )
            auth: ProbeCommandResult | None = None
            login_root: Path | None = None
            login_root_identity: tuple[int, int, int] | None = None
            login_auth_identity = None
            cleanup_failed = False
            try:
                login_root = Path(
                    mkdtemp(
                        prefix="openevo-codex-login-",
                        dir=self._credential_probe_root,
                    )
                )
                login_root_identity = capture_session_root_identity(login_root)
                os.chmod(login_root, 0o700)
                staged = stage_codex_subscription_auth(
                    source=self._codex_auth_path,
                    prepared_snapshot=credential_snapshot,
                    session_dir=login_root,
                    session_identity=login_root_identity,
                    target_home_parts=(),
                )
                login_auth_identity = staged.auth_identity
                auth = codex_executable.run(
                    ("login", "status"),
                    deadline,
                    cancellation,
                    env={"CODEX_HOME": os.fspath(login_root)},
                )
            except (OSError, ValueError, SessionFileSecurityError):
                auth = None
            finally:
                if login_root is not None and login_root_identity is not None:
                    try:
                        remove_credential_tree(
                            login_root,
                            login_root_identity,
                            login_auth_identity,
                        )
                    except (OSError, SessionFileSecurityError):
                        cleanup_failed = True
            if (
                cleanup_failed
                or auth is None
                or auth.returncode != 0
                or not _is_chatgpt_subscription_status(auth.stdout, auth.stderr)
            ):
                credential_snapshot.close()
                return _runtime_not_ready(
                    ServiceRunReadinessCode.CODEX_SUBSCRIPTION_AUTH_UNAVAILABLE,
                    "Codex subscription login is unavailable on the remote Core host.",
                )

            try:
                docker_executable = DockerExecutableAuthority.open()
            except Exception:
                credential_snapshot.close()
                return _runtime_not_ready(
                    ServiceRunReadinessCode.RUNTIME_EXECUTABLE_UNAVAILABLE,
                    "The managed Science runtime executable is unavailable.",
                )
            try:
                docker_engine = DockerEngineAuthority.open()
                if docker_engine.executable.identity != docker_executable.identity:
                    raise DockerHostPathError("the release Docker executable authority changed")
            except Exception:
                credential_snapshot.close()
                return _runtime_not_ready(
                    ServiceRunReadinessCode.RUNTIME_EVIDENCE_INVALID,
                    "The local Docker Engine authority is unavailable.",
                )
            try:
                runtime = self._run_docker(
                    docker_engine,
                    ("--version",),
                    deadline,
                    cancellation,
                )
            except Exception:
                credential_snapshot.close()
                return _runtime_not_ready(
                    ServiceRunReadinessCode.RUNTIME_EXECUTABLE_UNAVAILABLE,
                    "The managed Science runtime executable is unavailable.",
                )
            if runtime.returncode != 0:
                credential_snapshot.close()
                return _runtime_not_ready(
                    ServiceRunReadinessCode.RUNTIME_EXECUTABLE_UNAVAILABLE,
                    "The managed Science runtime executable is unavailable.",
                )
            try:
                image_result = self._run_docker(
                    docker_engine,
                    ("image", "inspect", request.runtime_image),
                    deadline,
                    cancellation,
                )
            except Exception:
                credential_snapshot.close()
                return _runtime_not_ready(
                    ServiceRunReadinessCode.RUNTIME_EVIDENCE_INVALID,
                    "Managed Science runtime image evidence is unavailable.",
                )
            if image_result.returncode != 0:
                credential_snapshot.close()
                return _runtime_not_ready(
                    ServiceRunReadinessCode.RUNTIME_IMAGE_UNAVAILABLE,
                    "Managed Science runtime image is not prepared.",
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
                RecursionError,
                ValueError,
            ):
                credential_snapshot.close()
                return _runtime_not_ready(
                    ServiceRunReadinessCode.RUNTIME_EVIDENCE_INVALID,
                    "Managed Science bootstrap evidence is invalid.",
                )
            try:
                runtime_codex = self._run_docker(
                    docker_engine,
                    (
                        "run",
                        "--rm",
                        "--network=none",
                        "--read-only",
                        "--cap-drop=ALL",
                        "--security-opt=no-new-privileges",
                        "--entrypoint",
                        MANAGED_CODEX_BINARY,
                        image_id,
                        "--version",
                    ),
                    deadline,
                    cancellation,
                )
            except Exception:
                credential_snapshot.close()
                return _runtime_not_ready(
                    ServiceRunReadinessCode.RUNTIME_EVIDENCE_INVALID,
                    "Managed Science runtime Codex CLI evidence is unavailable.",
                )
            if not _valid_codex_version(
                runtime_codex,
                expected_version=MANAGED_CODEX_VERSION,
            ):
                credential_snapshot.close()
                return _runtime_not_ready(
                    ServiceRunReadinessCode.RUNTIME_EVIDENCE_INVALID,
                    "Managed Science runtime Codex CLI evidence is invalid.",
                )
            docker_host_path: DockerHostPathSpec | None = None
            if self._runtime_namespace is not None:
                try:
                    docker_host_path = _discover_docker_user_container_path(
                        self._run_docker,
                        docker_engine,
                        namespace=self._runtime_namespace,
                        deadline=deadline,
                        cancellation=cancellation,
                        preferred_container_path=self._preferred_container_path,
                    )
                except Exception:
                    if self._require_docker_user_container:
                        credential_snapshot.close()
                        return _runtime_not_ready(
                            ServiceRunReadinessCode.RUNTIME_EVIDENCE_INVALID,
                            "The Docker user-container data-root mapping is unavailable.",
                        )
            readiness = ManagedScienceRuntimeReadiness(
                ready=True,
                code=ServiceRunReadinessCode.READY,
                identity_digest=_digest_json(
                    {
                        "auth_content_sha256": credential_snapshot.content_sha256,
                        "auth_identity": credential_snapshot.identity,
                        "host_codex_auth_client_identity_digest": (
                            codex_executable.identity_digest
                        ),
                        "host_codex_auth_client_version_evidence": _command_evidence(codex),
                        "codex_model": request.codex_model,
                        "codex_version_evidence": _command_evidence(runtime_codex),
                        "runtime_version_evidence": _command_evidence(runtime),
                        "runtime_executable_identity_digest": (docker_executable.identity_digest),
                        "runtime_engine_identity_digest": (docker_engine.identity_digest),
                        "runtime_image": request.runtime_image,
                        "runtime_image_id": image_id,
                        "runtime_image_immutable_reference": immutable_image,
                        "docker_host_path_identity": (
                            None if docker_host_path is None else docker_host_path.identity_digest
                        ),
                    }
                ),
                runtime_image_immutable_reference=immutable_image,
                message="Managed Science runtime bootstrap is verified.",
                docker_host_path=docker_host_path,
                credential_authority=credential_snapshot,
            )
            credential_snapshot = None
            return readiness
        finally:
            if credential_snapshot is not None:
                credential_snapshot.close()
            codex_executable.close()

    def _run_docker(
        self,
        authority: DockerEngineAuthority,
        arguments: tuple[str, ...],
        deadline: float,
        cancellation: threading.Event | None,
    ) -> ProbeCommandResult:
        argv = authority.argv(*arguments)
        try:
            return self._command_runner.run(
                argv,
                deadline,
                cancellation,
                env=authority.environment(),
            )
        finally:
            authority.verify()

    def _prepare_login_snapshot(self) -> PreparedCodexCredentialSnapshot | None:
        authority: HeldCodexCredentialAuthority | None = None
        try:
            authority = HeldCodexCredentialAuthority.open(self._codex_auth_path)
            # prepare_snapshot performs the source FD/path checks before and after
            # copying. Its sealed memfd commit is the point after which source-path
            # replacement is irrelevant to this readiness generation.
            return authority.prepare_snapshot()
        except (OSError, SessionFileSecurityError, ValueError):
            return None
        finally:
            if authority is not None:
                authority.close()


class LocalSelfDeployedRuntimeProbe:
    """Prepare and verify the one release-owned Self-Deployed profile."""

    def __init__(
        self,
        *,
        command_runner: ProbeCommandRunner | None = None,
        runtime_namespace: str | None = None,
        require_docker_user_container: bool = False,
        preferred_container_path: Path | None = None,
    ) -> None:
        self._command_runner = command_runner or BoundedProbeCommandRunner()
        self._runtime_namespace = runtime_namespace
        self._require_docker_user_container = require_docker_user_container
        self._preferred_container_path = preferred_container_path

    def verify(
        self,
        request: SelfDeployedRuntimeRequest,
        deadline: float,
        cancellation: threading.Event | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> SelfDeployedRuntimeReadiness:
        profile = require_release_self_deployed_model_profile(request.profile_id)
        _progress(progress, "Checking Docker Engine and managed Science runtime image.")
        try:
            docker_engine = DockerEngineAuthority.open()
            runtime = self._run_docker(
                docker_engine,
                ("--version",),
                deadline,
                cancellation,
            )
        except Exception:
            return _self_deployed_not_ready(
                ServiceRunReadinessCode.RUNTIME_EXECUTABLE_UNAVAILABLE,
                "The Docker Engine required for Self-Deployed execution is unavailable.",
            )
        if runtime.returncode != 0:
            return _self_deployed_not_ready(
                ServiceRunReadinessCode.RUNTIME_EXECUTABLE_UNAVAILABLE,
                "The Docker Engine required for Self-Deployed execution is unavailable.",
            )
        try:
            managed_image = self._run_docker(
                docker_engine,
                ("image", "inspect", request.runtime_image),
                deadline,
                cancellation,
            )
            immutable_runtime_image, managed_image_id = _verified_managed_image_inspect(
                managed_image,
                request.runtime_image,
            )
        except Exception:
            return _self_deployed_not_ready(
                ServiceRunReadinessCode.RUNTIME_IMAGE_UNAVAILABLE,
                "The managed Science runtime image is not prepared.",
            )

        if self._runtime_namespace is None:
            return _self_deployed_not_ready(
                ServiceRunReadinessCode.RUNTIME_EVIDENCE_INVALID,
                "The Docker user-container data-root mapping is unavailable.",
            )
        try:
            docker_host_path = _discover_docker_user_container_path(
                self._run_docker,
                docker_engine,
                namespace=self._runtime_namespace,
                deadline=deadline,
                cancellation=cancellation,
                minimum_available_bytes=profile.minimum_free_disk_bytes,
                preferred_container_path=self._preferred_container_path,
            )
        except Exception:
            if self._require_docker_user_container:
                return _self_deployed_not_ready(
                    ServiceRunReadinessCode.RUNTIME_EVIDENCE_INVALID,
                    "No verified Docker bind root has enough free space for this model profile.",
                )
            return _self_deployed_not_ready(
                ServiceRunReadinessCode.RUNTIME_EVIDENCE_INVALID,
                "The Docker data-root mapping is unavailable.",
            )

        _progress(progress, "Checking NVIDIA GPU capacity for managed inference.")
        gpu_authority: ProbeExecutableAuthority | None = None
        try:
            gpu_authority = self._command_runner.hold_executable("nvidia-smi")
            gpu_result = gpu_authority.run(
                (
                    "--query-gpu=index,uuid,name,memory.free,memory.total,driver_version",
                    "--format=csv,noheader,nounits",
                ),
                deadline,
                cancellation,
            )
            gpu_device_id, gpu_inventory_digest = _select_self_deployed_gpu(
                gpu_result,
                minimum_free_vram_bytes=profile.minimum_free_vram_bytes,
            )
        except Exception:
            return _self_deployed_not_ready(
                ServiceRunReadinessCode.SELF_DEPLOYED_UNAVAILABLE,
                "No NVIDIA GPU currently satisfies the release model profile.",
            )
        finally:
            if gpu_authority is not None:
                gpu_authority.close()
        try:
            system_memory_bytes = _linux_system_memory_bytes()
            if system_memory_bytes < profile.minimum_system_memory_bytes:
                raise ValueError("system memory is below the profile floor")
        except Exception:
            return _self_deployed_not_ready(
                ServiceRunReadinessCode.SELF_DEPLOYED_UNAVAILABLE,
                "System memory is below the release model profile requirement.",
            )

        _progress(progress, "Checking NVIDIA Container Toolkit GPU injection.")
        try:
            toolkit = self._run_docker(
                docker_engine,
                (
                    "run",
                    "--rm",
                    "--platform",
                    "linux/amd64",
                    "--network",
                    "none",
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges=true",
                    "--pids-limit",
                    "64",
                    "--gpus",
                    f"device={gpu_device_id}",
                    "--user",
                    f"{os.geteuid()}:{os.getegid()}",
                    "--entrypoint",
                    "nvidia-smi",
                    immutable_runtime_image,
                    "--query-gpu=uuid",
                    "--format=csv,noheader",
                ),
                deadline,
                cancellation,
            )
            _verify_nvidia_toolkit_probe(toolkit, gpu_device_id)
        except Exception:
            return _self_deployed_not_ready(
                ServiceRunReadinessCode.SELF_DEPLOYED_UNAVAILABLE,
                "NVIDIA Container Toolkit could not inject the selected GPU.",
            )

        _progress(progress, "Checking the immutable vLLM serving image.")
        try:
            vllm_inspect = self._run_docker(
                docker_engine,
                ("image", "inspect", profile.vllm_image),
                deadline,
                cancellation,
            )
            if vllm_inspect.returncode != 0:
                _progress(
                    progress,
                    f"Pulling immutable vLLM {profile.vllm_version} image; this can take several minutes.",
                )
                pulled = self._run_docker(
                    docker_engine,
                    ("pull", "--platform", "linux/amd64", profile.vllm_image),
                    deadline,
                    cancellation,
                )
                if pulled.returncode != 0:
                    raise ValueError("immutable vLLM image pull failed")
                vllm_inspect = self._run_docker(
                    docker_engine,
                    ("image", "inspect", profile.vllm_image),
                    deadline,
                    cancellation,
                )
            vllm_image_id = _verified_vllm_image_inspect(vllm_inspect, profile)
        except Exception:
            return _self_deployed_not_ready(
                ServiceRunReadinessCode.SELF_DEPLOYED_UNAVAILABLE,
                "The immutable vLLM serving image could not be prepared or verified.",
            )

        cache_root = Path(docker_host_path.runtime_container_root) / "models"
        try:
            cache_root.mkdir(mode=0o700, exist_ok=True)
            if stat.S_IMODE(os.lstat(cache_root).st_mode) != 0o700:
                raise ValueError("model cache root is not private")
            _progress(
                progress,
                "Preparing the exact Hugging Face model snapshot; progress will be reported by bytes.",
            )
            model_path = prepare_release_model_snapshot(
                cache_root=cache_root,
                profile=profile,
                deadline=deadline,
                cancellation=cancellation,
                progress=progress,
            )
            daemon_model_path = docker_host_path.translate(model_path)
        except (OSError, ValueError, SelfDeployedModelCacheError):
            return _self_deployed_not_ready(
                ServiceRunReadinessCode.SELF_DEPLOYED_UNAVAILABLE,
                "The release model snapshot could not be downloaded and verified.",
            )

        identity_digest = _digest_json(
            {
                "docker_engine_identity_digest": docker_engine.identity_digest,
                "docker_host_path_identity": docker_host_path.identity_digest,
                "gpu_device_id": gpu_device_id,
                "gpu_inventory_digest": gpu_inventory_digest,
                "managed_runtime_image_id": managed_image_id,
                "managed_runtime_image_immutable_reference": immutable_runtime_image,
                "model_profile_sha256": profile.profile_sha256,
                "model_snapshot_manifest_sha256": (profile.model_snapshot_manifest_sha256),
                "runtime_version_evidence": _command_evidence(runtime),
                "system_memory_bytes": system_memory_bytes,
                "toolkit_probe_evidence": _command_evidence(toolkit),
                "vllm_image": profile.vllm_image,
                "vllm_image_id": vllm_image_id,
            }
        )
        _progress(progress, "Self-Deployed model and serving prerequisites are verified.")
        return SelfDeployedRuntimeReadiness(
            ready=True,
            code=ServiceRunReadinessCode.READY,
            identity_digest=identity_digest,
            runtime_image_immutable_reference=immutable_runtime_image,
            profile=profile,
            model_cache_container_path=model_path,
            model_cache_daemon_path=daemon_model_path,
            daemon_container_id=docker_host_path.container_id,
            gpu_device_id=gpu_device_id,
            message="Self-Deployed runtime bootstrap is verified.",
            docker_host_path=docker_host_path,
        )

    def _run_docker(
        self,
        authority: DockerEngineAuthority,
        arguments: tuple[str, ...],
        deadline: float,
        cancellation: threading.Event | None,
    ) -> ProbeCommandResult:
        try:
            return self._command_runner.run(
                authority.argv(*arguments),
                deadline,
                cancellation,
                env=authority.environment(),
            )
        finally:
            authority.verify()

    def remove_managed_container(self, generation_digest: str, deadline: float) -> bool:
        """Idempotently reap only the exactly labelled inference container."""

        try:
            _require_digest(generation_digest, "Self-Deployed generation digest")
            authority = DockerEngineAuthority.open()
            name = _self_deployed_container_name(generation_digest)
            selected = self._run_docker(
                authority,
                (
                    "container",
                    "ls",
                    "--all",
                    "--quiet",
                    "--no-trunc",
                    "--filter",
                    f"name=^/{name}$",
                ),
                deadline,
                None,
            )
            if selected.returncode != 0:
                return False
            identities = tuple(
                line.strip()
                for line in selected.stdout.decode("ascii").splitlines()
                if line.strip()
            )
            if not identities:
                return True
            if len(identities) != 1 or re.fullmatch(r"[0-9a-f]{64}", identities[0]) is None:
                return False
            identity = identities[0]
            inspected = self._run_docker(
                authority,
                ("container", "inspect", identity),
                deadline,
                None,
            )
            if not _managed_inference_container_matches(
                inspected,
                container_id=identity,
                container_name=name,
                generation_digest=generation_digest,
            ):
                return False
            removed = self._run_docker(
                authority,
                ("container", "rm", "--force", identity),
                deadline,
                None,
            )
            if removed.returncode != 0:
                return False
            confirmed = self._run_docker(
                authority,
                (
                    "container",
                    "ls",
                    "--all",
                    "--quiet",
                    "--no-trunc",
                    "--filter",
                    f"name=^/{name}$",
                ),
                deadline,
                None,
            )
            return confirmed.returncode == 0 and not confirmed.stdout.strip()
        except Exception:
            return False


def _self_deployed_not_ready(
    code: ServiceRunReadinessCode,
    message: str,
) -> SelfDeployedRuntimeReadiness:
    return SelfDeployedRuntimeReadiness(
        ready=False,
        code=code,
        identity_digest=None,
        runtime_image_immutable_reference=None,
        profile=None,
        model_cache_container_path=None,
        model_cache_daemon_path=None,
        daemon_container_id=None,
        gpu_device_id=None,
        message=message,
        docker_host_path=None,
    )


def _verified_managed_image_inspect(
    result: ProbeCommandResult,
    runtime_image: str,
) -> tuple[str, str]:
    if result.returncode != 0:
        raise ValueError("managed runtime image is unavailable")
    payload = json.loads(result.stdout.decode("utf-8"))
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise ValueError("managed runtime inspect payload is invalid")
    image = payload[0]
    config = image.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    immutable = verified_managed_runtime_image_reference(
        profile="managed_science",
        image=runtime_image,
        image_id=image.get("Id"),
        repo_digests=image.get("RepoDigests"),
        labels=labels,
    )
    image_id = image.get("Id")
    if not isinstance(image_id, str):
        raise ValueError("managed runtime image ID is unavailable")
    return immutable, image_id


def _verified_vllm_image_inspect(
    result: ProbeCommandResult,
    profile: SelfDeployedModelProfile,
) -> str:
    if result.returncode != 0:
        raise ValueError("vLLM image is unavailable")
    payload = json.loads(result.stdout.decode("utf-8"))
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise ValueError("vLLM image inspect payload is invalid")
    image = payload[0]
    image_id = image.get("Id")
    repo_digests = image.get("RepoDigests")
    expected_digest = profile.vllm_image.split("@", 1)[1]
    canonical_repo_digest = profile.vllm_image.removeprefix("docker.io/")
    if (
        image_id not in {profile.vllm_image_config_digest, expected_digest}
        or not isinstance(repo_digests, list)
        or not any(
            isinstance(item, str) and item in {profile.vllm_image, canonical_repo_digest}
            for item in repo_digests
        )
    ):
        raise ValueError("vLLM image identity differs from the release profile")
    if image.get("Architecture") != "amd64" or image.get("Os") != "linux":
        raise ValueError("vLLM image platform differs from the release profile")
    return image_id


def _self_deployed_container_name(generation_digest: str) -> str:
    _require_digest(generation_digest, "Self-Deployed generation digest")
    return f"openevo-vllm-{generation_digest[:24]}"


def _managed_inference_container_matches(
    result: ProbeCommandResult,
    *,
    container_id: str,
    container_name: str,
    generation_digest: str,
) -> bool:
    if result.returncode != 0:
        return False
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        return False
    item = payload[0]
    config = item.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    return (
        item.get("Id") == container_id
        and item.get("Name") == f"/{container_name}"
        and isinstance(labels, dict)
        and labels.get("io.openevo.generation") == generation_digest
        and labels.get("io.openevo.managed-service") == "true"
    )


def _select_self_deployed_gpu(
    result: ProbeCommandResult,
    *,
    minimum_free_vram_bytes: int,
) -> tuple[str, str]:
    if result.returncode != 0:
        raise ValueError("NVIDIA GPU inventory is unavailable")
    try:
        text = result.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("NVIDIA GPU inventory is not UTF-8") from exc
    inventory: list[dict[str, object]] = []
    for line in text.splitlines():
        fields = tuple(value.strip() for value in line.split(","))
        if len(fields) != 6:
            raise ValueError("NVIDIA GPU inventory shape is invalid")
        index_text, uuid, name, free_text, total_text, driver = fields
        if (
            not index_text.isdecimal()
            or re.fullmatch(r"GPU-[0-9A-Fa-f-]{16,64}", uuid) is None
            or not name
            or not free_text.isdecimal()
            or not total_text.isdecimal()
            or re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", driver) is None
        ):
            raise ValueError("NVIDIA GPU inventory value is invalid")
        inventory.append(
            {
                "index": int(index_text),
                "uuid": uuid,
                "name": name,
                "free_bytes": int(free_text) * 1024 * 1024,
                "total_bytes": int(total_text) * 1024 * 1024,
                "driver_version": driver,
            }
        )
    candidates = [item for item in inventory if int(item["free_bytes"]) >= minimum_free_vram_bytes]
    if not candidates:
        raise ValueError("no NVIDIA GPU satisfies the free VRAM floor")
    selected = max(candidates, key=lambda item: (int(item["free_bytes"]), str(item["uuid"])))
    stable_inventory = [
        {
            "index": item["index"],
            "uuid": item["uuid"],
            "name": item["name"],
            "total_bytes": item["total_bytes"],
            "driver_version": item["driver_version"],
        }
        for item in sorted(inventory, key=lambda item: int(item["index"]))
    ]
    return str(selected["uuid"]), _digest_json(stable_inventory)


def _verify_nvidia_toolkit_probe(
    result: ProbeCommandResult,
    expected_gpu_uuid: str,
) -> None:
    if result.returncode != 0:
        raise ValueError("NVIDIA Container Toolkit probe failed")
    try:
        lines = tuple(line.strip() for line in result.stdout.decode("ascii").splitlines())
    except UnicodeDecodeError as exc:
        raise ValueError("NVIDIA Container Toolkit probe is not ASCII") from exc
    if lines != (expected_gpu_uuid,):
        raise ValueError("NVIDIA Container Toolkit exposed a different GPU")


def _linux_system_memory_bytes() -> int:
    payload = Path("/proc/meminfo").read_text(encoding="ascii")
    matches = [line for line in payload.splitlines() if line.startswith("MemTotal:")]
    if len(matches) != 1:
        raise ValueError("Linux system memory evidence is unavailable")
    fields = matches[0].split()
    if len(fields) != 3 or not fields[1].isdecimal() or fields[2] != "kB":
        raise ValueError("Linux system memory evidence is invalid")
    return int(fields[1]) * 1024


def _progress(callback: Callable[[str], None] | None, message: str) -> None:
    if callback is not None:
        callback(message)


def _runtime_not_ready(
    code: ServiceRunReadinessCode,
    message: str,
) -> ManagedScienceRuntimeReadiness:
    return ManagedScienceRuntimeReadiness(
        ready=False,
        code=code,
        identity_digest=None,
        runtime_image_immutable_reference=None,
        message=message,
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


@dataclass(slots=True)
class _SelfDeployedPreparationState:
    token: object
    model_ref: str
    identity_digest: str
    started_at: str
    updated_at: str
    log_sequence: int = 0
    logs: list[SupervisorLogEntry] = field(default_factory=list)


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
    runtime_image_immutable_reference: str | None
    runtime_identity_digest: str | None
    status_message: str | None = None

    def __post_init__(self) -> None:
        if self.run_ready != (self.run_readiness_code is ServiceRunReadinessCode.READY):
            raise ValueError("service run readiness code does not match ready state")
        if self.run_ready and (
            not self.services_available
            or self.runtime_image not in set(MANAGED_RUNTIME_IMAGES.values())
            or self.runtime_image_immutable_reference is None
            or self.runtime_identity_digest is None
        ):
            raise ValueError("run-ready service group lacks runtime evidence")
        if self.runtime_image_immutable_reference is not None:
            release = require_immutable_managed_runtime_image(
                profile="managed_science",
                image=self.runtime_image_immutable_reference,
            )
            if self.runtime_image is not None and release.image != self.runtime_image:
                raise ValueError("service group immutable image does not match its alias")

    def service(self, service_id: str) -> SupervisorServiceSummary:
        for service in self.services:
            if service.id == service_id:
                return service
        raise KeyError(service_id)


@dataclass(frozen=True, slots=True)
class ServiceRunBinding:
    """Ephemeral trusted connection from the run owner to one service generation."""

    execution_mode: ServiceExecutionMode
    codex_model: str
    runtime_image: str
    runtime_image_immutable_reference: str
    runtime_identity_digest: str
    generation_digest: str
    registry_digest: str
    framework_lock_digest: str
    rollout_url: str
    evolution_backend_url: str
    gateway_url: str
    _identity: InternalServiceIdentity = field(repr=False, compare=False)
    self_deployed_profile_id: str | None = None
    self_deployed_profile_sha256: str | None = None
    self_deployed_model_revision: str | None = None
    self_deployed_model_snapshot_sha256: str | None = None
    self_deployed_vllm_image: str | None = None
    self_deployed_vllm_image_config_digest: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.execution_mode, ServiceExecutionMode):
            raise ValueError("service run binding execution mode is invalid")
        if self.runtime_image not in set(MANAGED_RUNTIME_IMAGES.values()):
            raise ValueError("service run binding image is not Core-managed")
        model = validate_codex_model_ref(
            self.codex_model,
            field_name="service run binding Codex model",
        )
        if model != self.codex_model:
            raise ValueError("service run binding Codex model is not canonical")
        release = require_immutable_managed_runtime_image(
            profile="managed_science",
            image=self.runtime_image_immutable_reference,
        )
        if release.image != self.runtime_image:
            raise ValueError("service run binding immutable image does not match its alias")
        self_deployed_values = (
            self.self_deployed_profile_id,
            self.self_deployed_profile_sha256,
            self.self_deployed_model_revision,
            self.self_deployed_model_snapshot_sha256,
            self.self_deployed_vllm_image,
            self.self_deployed_vllm_image_config_digest,
        )
        if self.execution_mode is ServiceExecutionMode.SELF_DEPLOYED:
            if not all(value is not None for value in self_deployed_values):
                raise ValueError("Self-Deployed service binding evidence is incomplete")
            profile = require_release_self_deployed_model_profile(
                self.self_deployed_profile_id or ""
            )
            if (
                self.codex_model != profile.model_id
                or self.self_deployed_profile_sha256 != profile.profile_sha256
                or self.self_deployed_model_revision != profile.model_revision
                or self.self_deployed_model_snapshot_sha256
                != profile.model_snapshot_manifest_sha256
                or self.self_deployed_vllm_image != profile.vllm_image
                or self.self_deployed_vllm_image_config_digest != profile.vllm_image_config_digest
            ):
                raise ValueError("Self-Deployed service binding differs from its release profile")
        elif any(value is not None for value in self_deployed_values):
            raise ValueError("Subscription service binding contains Self-Deployed evidence")
        for value, label in (
            (self.runtime_identity_digest, "runtime_identity_digest"),
            (self.generation_digest, "generation_digest"),
            (self.registry_digest, "registry_digest"),
            (self.framework_lock_digest, "framework_lock_digest"),
        ):
            _require_digest(value, label)
        if (
            type(self._identity) is not InternalServiceIdentity
            or self._identity.service_id != "core-control"
            or self._identity.generation_digest != self.generation_digest
            or self._identity.registry_digest != self.registry_digest
            or self._identity.framework_lock_digest != self.framework_lock_digest
        ):
            raise ValueError("service run binding internal identity is inconsistent")

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


class _LedgerRestartModelPreparation(_StrictStateModel):
    model_ref: str = Field(min_length=1, max_length=256)
    status: str = Field(min_length=1, max_length=64)
    updated_at: str = Field(min_length=20, max_length=40)
    next_interface: str = Field(min_length=1, max_length=64)


class _LedgerRestartService(_StrictStateModel):
    id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=128)
    component: ServiceComponent
    status: ServiceStatus
    restartable: bool
    status_message: str | None = Field(default=None, max_length=256)
    error_code: str | None = Field(default=None, max_length=128)
    updated_at: str = Field(min_length=20, max_length=40)
    observed_at: str = Field(min_length=20, max_length=40)
    identity_digest: str
    pid: int | None = Field(default=None, gt=0)
    port: int | None = Field(default=None, ge=1, le=65535)
    etag: str = Field(min_length=66, max_length=66)
    model_preparation: _LedgerRestartModelPreparation | None = None

    _identity = field_validator("identity_digest")(_state_digest)

    @field_validator("component", mode="before")
    @classmethod
    def _component_from_canonical_text(cls, value: object) -> object:
        return ServiceComponent(value) if isinstance(value, str) else value

    @field_validator("status", mode="before")
    @classmethod
    def _status_from_canonical_text(cls, value: object) -> object:
        return ServiceStatus(value) if isinstance(value, str) else value

    @field_validator("etag")
    @classmethod
    def _strong_etag(cls, value: str) -> str:
        if _STRONG_ETAG_RE.fullmatch(value) is None:
            raise ValueError("restart service etag is invalid")
        return value


class _LedgerRestartAttempt(_StrictStateModel):
    operation_id: str = Field(min_length=1, max_length=128)
    service_id: str = Field(min_length=1, max_length=128)
    expected_service_etag: str = Field(min_length=66, max_length=66)
    state: ServiceRestartAttemptState
    service: _LedgerRestartService | None = None

    @field_validator("expected_service_etag")
    @classmethod
    def _strong_expected_etag(cls, value: str) -> str:
        if _STRONG_ETAG_RE.fullmatch(value) is None:
            raise ValueError("restart expected service etag is invalid")
        return value

    @field_validator("state", mode="before")
    @classmethod
    def _state_from_canonical_text(cls, value: object) -> object:
        return ServiceRestartAttemptState(value) if isinstance(value, str) else value


class _LedgerBase(_StrictStateModel):
    schema_version: int
    release: _LedgerRelease
    execution_mode: ServiceExecutionMode | None = None
    generation_digest: str | None = None
    runtime_identity_digest: str | None = None
    runtime_readiness_code: ServiceRunReadinessCode | None = None
    group_status_message: str | None = Field(default=None, max_length=256)
    services: list[_LedgerService] = Field(default_factory=list, max_length=16)

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


class _LedgerV1(_LedgerBase):
    @field_validator("schema_version")
    @classmethod
    def _schema_is_one(cls, value: int) -> int:
        if value != 1:
            raise ValueError("unsupported service ledger schema")
        return value


class _Ledger(_LedgerBase):
    restart_attempts: list[_LedgerRestartAttempt] = Field(default_factory=list, max_length=4096)

    @field_validator("schema_version")
    @classmethod
    def _schema_is_two(cls, value: int) -> int:
        if value != 2:
            raise ValueError("unsupported service ledger schema")
        return value


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


class _StartupLogCapture:
    """Capture sanitized child output without taking the lifecycle mutex."""

    def __init__(self, credential: str, *, max_bytes: int) -> None:
        if max_bytes < 1:
            raise ValueError("startup log byte limit must be positive")
        self._mutex = threading.Lock()
        self._redactor = _BoundedLogStreamRedactor(credential)
        self._max_bytes = max_bytes
        self._payload = bytearray()
        self._accepting = True

    def capture(self, payload: bytes) -> bool:
        with self._mutex:
            if not self._accepting:
                return False
            self._append(self._redactor.feed(payload))
            return True

    def promote(self) -> tuple[bytes, _BoundedLogStreamRedactor]:
        """Stop capture while preserving redactor carry for steady-state output."""

        with self._mutex:
            if not self._accepting:
                raise SupervisorStateError("startup log capture was already completed")
            self._accepting = False
            payload = bytes(self._payload)
            self._payload.clear()
            return payload, self._redactor

    def finish(self) -> bytes:
        """Stop terminal capture and flush any bounded partial log line."""

        with self._mutex:
            if not self._accepting:
                return b""
            self._accepting = False
            self._append(self._redactor.flush())
            payload = bytes(self._payload)
            self._payload.clear()
            return payload

    def _append(self, payload: bytes) -> None:
        remaining = self._max_bytes - len(self._payload)
        if remaining > 0:
            self._payload.extend(payload[:remaining])


class RealSubprocessBackend:
    """Controlled process-group launcher with Linux PID birth identity."""

    def __init__(self, *, max_tracked_processes: int = 64) -> None:
        if not 1 <= max_tracked_processes <= 1024:
            raise ValueError("tracked process limit is outside the supported bounds")
        self._lock = threading.RLock()
        self._tracked: dict[str, _TrackedProcess] = {}
        self._completed: OrderedDict[str, tuple[ProcessIdentity, int]] = OrderedDict()
        self._max_tracked_processes = max_tracked_processes
        self._max_tracked_records = max_tracked_processes + 1
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
                authority_env = (
                    CODEX_CREDENTIAL_SNAPSHOT_FD_ENV
                    if isinstance(
                        spec.codex_credential_authority,
                        PreparedCodexCredentialSnapshot,
                    )
                    else CODEX_CREDENTIAL_AUTHORITY_FD_ENV
                )
                child_env[authority_env] = str(authority_fd)
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
                if returncode is None:
                    if deadline is not None and time.monotonic() >= deadline:
                        return None
                    time.sleep(0.001)
                    continue
                self._retire_if_complete(identity)
                return returncode
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
            if (
                len(self._tracked) + self._spawn_reservations >= self._max_tracked_records
                or self._active_process_slots_locked() + self._spawn_reservations
                >= self._max_tracked_processes
            ):
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

    def _active_process_slots_locked(self) -> int:
        active = 0
        for tracked in self._tracked.values():
            if tracked.process.poll() is None:
                active += 1
                continue
            # A reaped group no longer consumes process capacity even while its
            # bounded exit callback record is retained. Unknown ownership still
            # consumes capacity so process identity failures remain fail closed.
            if _owned_process_group_members(tracked.identity) != ():
                active += 1
        return active

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
            _require_private_framework_lock(info, os.getuid())
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
            _require_private_framework_lock(info, os.getuid())
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
        self_deployed_runtime_probe: SelfDeployedRuntimeProbe | None = None,
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
                self_deployed_runtime_probe,
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
        self._preparation_mutex = threading.RLock()
        self._self_deployed_preparation: _SelfDeployedPreparationState | None = None
        self._root = _SecureServiceRoot(service_root)
        self._closed = False
        try:
            self._framework_lock_source = _VerifiedFrameworkLock(Path(framework_lock))
            self._framework_lock_digest = self._framework_lock_source.digest
            self._framework_lock_path = self._framework_lock_source.path
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
            self._root.ensure_directory("credential-probes")
            self._credential_probe_root = self._root.path / "credential-probes"
            self._managed_runtime_probe = managed_runtime_probe or LocalManagedScienceRuntimeProbe(
                credential_probe_root=self._credential_probe_root,
                runtime_namespace=(
                    "core-"
                    + hashlib.sha256(
                        os.fsencode(os.fspath(self._root.path.absolute()))
                    ).hexdigest()[:24]
                ),
                require_docker_user_container=(launch_mode is ServiceLaunchMode.RELEASE),
                preferred_container_path=self._root.path,
            )
            self._self_deployed_runtime_probe = (
                self_deployed_runtime_probe
                or LocalSelfDeployedRuntimeProbe(
                    runtime_namespace=(
                        "core-"
                        + hashlib.sha256(
                            os.fsencode(os.fspath(self._root.path.absolute()))
                        ).hexdigest()[:24]
                    ),
                    require_docker_user_container=(launch_mode is ServiceLaunchMode.RELEASE),
                    preferred_container_path=self._root.path,
                )
            )
            self._startup_timeout = startup_timeout
            self._stop_timeout = stop_timeout
            self._max_log_entries = max_log_entries
            self._max_log_bytes = max_log_bytes
            self._max_restart_operations = max_restart_operations
            self._root.ensure_directory("child-cwd")
            self._child_cwd = self._root.path / "child-cwd"
            self._root.ensure_directory("tmp")
            self._service_tmp = self._root.path / "tmp"
            self._root.ensure_directory("workspace-handoffs")
            self._workspace_handoff_root = self._root.path / "workspace-handoffs"
            self._handles: dict[str, ProcessIdentity] = {}
            self._specs: dict[str, ServiceProcessSpec] = {}
            self._output_redactors: dict[
                tuple[str, str, ProcessIdentity], _BoundedLogStreamRedactor
            ] = {}
            self._restart_results: dict[str, tuple[str, SupervisorServiceSummary]] = {}
            self._active_plan_key: str | None = None
            self._active_credential: str | None = None
            self._active_runtime_request: ManagedScienceRuntimeRequest | None = None
            self._active_self_deployed_request: SelfDeployedRuntimeRequest | None = None
            self._active_self_deployed_runtime: SelfDeployedRuntimeReadiness | None = None
            self._active_runtime_image_immutable_reference: str | None = None
            self._active_credential_authority: (
                HeldCodexCredentialAuthority | PreparedCodexCredentialSnapshot | None
            ) = None
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
        preparation_token = self._begin_public_self_deployed_preparation(
            execution_mode,
            model_ref=model_ref,
            runtime_image=runtime_image,
        )
        with self._mutex:
            try:
                return self._ensure_locked(
                    execution_mode,
                    model_ref=model_ref,
                    codex_model=codex_model,
                    runtime_image=runtime_image,
                    total_timeout=total_timeout,
                    force_restart=False,
                    preparation_token=preparation_token,
                )
            finally:
                if preparation_token is not None:
                    self._finish_self_deployed_preparation(preparation_token)

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

        preparation_token = self._begin_public_self_deployed_preparation(
            execution_mode,
            model_ref=model_ref,
            runtime_image=runtime_image,
        )
        with self._mutex:
            try:
                snapshot = self._ensure_locked(
                    execution_mode,
                    model_ref=model_ref,
                    codex_model=codex_model,
                    runtime_image=runtime_image,
                    total_timeout=total_timeout,
                    force_restart=False,
                    preparation_token=preparation_token,
                )
                if not snapshot.run_ready:
                    return snapshot, None
                if self._active_run_lease is not None:
                    raise SupervisorStateError(
                        "managed service generation already has a run lease"
                    )
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
            finally:
                if preparation_token is not None:
                    self._finish_self_deployed_preparation(preparation_token)

    def _ensure_locked(
        self,
        execution_mode: ServiceExecutionMode,
        *,
        model_ref: str | None = None,
        codex_model: str | None = None,
        runtime_image: str | None = None,
        total_timeout: float | None = None,
        force_restart: bool,
        preparation_token: object | None = None,
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
            self._framework_lock_source.verified_payload()
            self_deployed = execution_mode is ServiceExecutionMode.SELF_DEPLOYED
            preparation_messages: list[str] = []
            active_preparation_token = preparation_token
            pre_stopped = False
            if runtime_image is None:
                raise ValueError("service ensure requires a managed runtime_image")
            self_deployed_request: SelfDeployedRuntimeRequest | None = None
            self_deployed_runtime: SelfDeployedRuntimeReadiness | None = None
            if self_deployed:
                if model_ref is None:
                    raise ValueError("Self-Deployed ensure requires a release model profile ID")
                self_deployed_request = SelfDeployedRuntimeRequest(
                    profile_id=model_ref,
                    runtime_image=runtime_image,
                )
                requested_profile = require_release_self_deployed_model_profile(model_ref)
                runtime_request = ManagedScienceRuntimeRequest(
                    runtime_image=runtime_image,
                    codex_model=requested_profile.model_id,
                )
                if (
                    not force_restart
                    and self._active_self_deployed_request == self_deployed_request
                    and self._active_runtime_request == runtime_request
                    and self._active_self_deployed_runtime is not None
                    and self._active_self_deployed_runtime.ready
                    and self._active_plan_key is not None
                    and self._active_credential is not None
                    and self._specs
                    and self._ledger.generation_digest is not None
                    and self._is_current_group_healthy(
                        execution_mode,
                        self._ledger.generation_digest,
                        tuple(self._specs.values()),
                        deadline,
                        cancellation,
                    )
                ):
                    return self._group_snapshot()
                if self._ledger.execution_mode is ServiceExecutionMode.SELF_DEPLOYED and (
                    self._specs or self._handles
                ):
                    if self._active_run_lease is not None:
                        raise SupervisorStateError(
                            "managed service generation is leased to an active run"
                        )
                    pre_stopped = self._stop_all(deadline)
                    if not pre_stopped:
                        self._ledger.group_status_message = "Existing managed children could not be stopped; service start aborted."
                        self._persist()
                        return self._group_snapshot()
                    self._release_active_credential_authority()
                if active_preparation_token is None:
                    active_preparation_token = self._begin_self_deployed_preparation(
                        self_deployed_request
                    )
                try:
                    self_deployed_runtime = self._self_deployed_runtime_probe.verify(
                        self_deployed_request,
                        deadline,
                        cancellation,
                        progress=lambda message: self._append_preparation_log(
                            active_preparation_token,
                            message,
                        ),
                    )
                finally:
                    if preparation_token is None:
                        preparation_messages = self._finish_self_deployed_preparation(
                            active_preparation_token
                        )
                    else:
                        preparation_messages = self._self_deployed_preparation_messages(
                            active_preparation_token
                        )
                runtime = self_deployed_runtime
                candidate_authority = None
            else:
                if codex_model is None:
                    raise ValueError("subscription ensure requires codex_model")
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
                    "runtime_image_immutable_reference": (
                        runtime.runtime_image_immutable_reference
                    ),
                    "docker_host_path_identity": (
                        None
                        if runtime.docker_host_path is None
                        else runtime.docker_host_path.identity_digest
                    ),
                    "self_deployed_profile_sha256": (
                        None
                        if self_deployed_runtime is None or self_deployed_runtime.profile is None
                        else self_deployed_runtime.profile.profile_sha256
                    ),
                }
            )

            def credential_authority_matches() -> bool:
                if self_deployed:
                    return self._active_credential_authority is None
                return (
                    candidate_authority is not None
                    and self._active_credential_authority_matches(candidate_authority)
                )

            def current_group_is_healthy() -> bool:
                if self._ledger.generation_digest is None:
                    return False
                if self_deployed:
                    return self._is_current_group_healthy(
                        execution_mode,
                        self._ledger.generation_digest,
                        tuple(self._specs.values()),
                        deadline,
                        cancellation,
                    )
                if candidate_authority is None:
                    return False
                return self._is_current_group_healthy_with_candidate(
                    execution_mode,
                    self._ledger.generation_digest,
                    tuple(self._specs.values()),
                    deadline,
                    cancellation,
                    candidate_authority,
                )

            if self._active_run_lease is not None:
                if (
                    force_restart
                    or not runtime.ready
                    or self._active_plan_key != plan_key
                    or self._active_credential is None
                    or not credential_authority_matches()
                    or not self._specs
                    or self._ledger.generation_digest is None
                    or not current_group_is_healthy()
                ):
                    if candidate_authority is not None:
                        candidate_authority.close()
                    raise SupervisorStateError(
                        "managed service generation is leased to an active run"
                    )
                if candidate_authority is not None:
                    candidate_authority.close()
                return self._group_snapshot()
            if (
                not force_restart
                and runtime.ready
                and self._active_plan_key == plan_key
                and self._active_credential is not None
                and credential_authority_matches()
                and self._specs
                and self._ledger.generation_digest is not None
                and current_group_is_healthy()
            ):
                if cancellation.is_set():
                    if candidate_authority is not None:
                        candidate_authority.close()
                    self._raise_if_cancelled(cancellation)
                if candidate_authority is not None:
                    candidate_authority.close()
                return self._group_snapshot()
            try:
                stopped = pre_stopped or self._stop_all(deadline)
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
            if self_deployed and not runtime.ready:
                assert self_deployed_request is not None
                generation_digest = _digest_json(
                    {
                        "plan_key": plan_key,
                        "readiness_code": runtime.code.value,
                    }
                )
                self._active_plan_key = plan_key
                self._active_credential = None
                self._active_runtime_request = runtime_request
                self._active_self_deployed_request = self_deployed_request
                self._active_self_deployed_runtime = self_deployed_runtime
                self._active_runtime_image_immutable_reference = None
                snapshot = self._install_self_deployed_unavailable(
                    request=self_deployed_request,
                    runtime=runtime,
                    generation_digest=generation_digest,
                )
                for message in preparation_messages:
                    self._append_log("inference", "info", message)
                self._persist()
                return snapshot
            listeners: dict[str, socket.socket] = {}
            try:
                service_ids = ["evolution-backend", "rollout", "gateway"]
                if self_deployed:
                    service_ids.insert(0, "inference")
                for service_id in service_ids:
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
                specs, topology = self._service_plan(
                    runtime_request,
                    plan_runtime_identity,
                    generation_digest,
                    credential,
                    listeners,
                    candidate_authority,
                    runtime.docker_host_path,
                    self_deployed_runtime=self_deployed_runtime,
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
            self._active_self_deployed_request = self_deployed_request
            self._active_self_deployed_runtime = self_deployed_runtime
            self._active_runtime_image_immutable_reference = (
                runtime.runtime_image_immutable_reference
            )
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
            inference_profile: SelfDeployedModelProfile | None = None
            if self_deployed_runtime is not None:
                inference_profile = self_deployed_runtime.profile
                if inference_profile is None:
                    raise SupervisorStateError(
                        "Self-Deployed runtime profile disappeared after planning"
                    )
            self._install_planned_records(
                execution_mode,
                generation_digest,
                specs,
                runtime_identity_digest=runtime.identity_digest,
                runtime_readiness_code=runtime.code,
                group_status_message=None,
                inference_profile=inference_profile,
            )
            if inference_profile is not None:
                for message in preparation_messages:
                    self._append_log("inference", "info", message)
                self._persist()
            self._root.ensure_directory("evolution")
            self._root.ensure_directory("rollout")
            self._root.atomic_write("topology.json", _canonical_bytes(topology))
            started: list[str] = []
            startup_captures: dict[str, _StartupLogCapture] = {}

            def finish_startup_capture(service_id: str) -> None:
                capture = startup_captures.pop(service_id, None)
                if capture is not None:
                    self._append_output_payload(service_id, capture.finish())

            def promote_startup_capture(
                service_id: str,
                identity: ProcessIdentity,
            ) -> None:
                capture = startup_captures.pop(service_id)
                payload, redactor = capture.promote()
                self._output_redactors[(service_id, generation_digest, identity)] = redactor
                self._append_output_payload(service_id, payload)

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
                    startup_message = f"Starting {spec.display_name}."
                    if active_preparation_token is not None:
                        self._append_preparation_log(
                            active_preparation_token,
                            startup_message,
                        )
                        self._append_log("inference", "info", startup_message)
                    if spec.component is ServiceComponent.INFERENCE:
                        listener = listeners.pop(spec.service_id, None)
                        if listener is not None:
                            listener.close()
                    startup_capture = _StartupLogCapture(
                        credential,
                        max_bytes=self._max_log_bytes,
                    )
                    startup_captures[spec.service_id] = startup_capture
                    try:
                        identity = self._process_backend.spawn(
                            spec,
                            lambda process_identity, payload, service_id=spec.service_id, capture=startup_capture: (
                                self._capture_startup_output(
                                    capture,
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
                        finish_startup_capture(spec.service_id)
                        self._fail_record(
                            spec.service_id,
                            "service_spawn_failed",
                            _safe_message("Managed service could not be started", exc),
                        )
                        self._rollback(started, deadline)
                        return self._group_snapshot()
                    if spec.service_id in listeners:
                        listeners.pop(spec.service_id).close()
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
                        if active_preparation_token is not None:
                            self._append_preparation_log(
                                active_preparation_token,
                                f"{spec.display_name} failed its readiness check.",
                            )
                        self._rollback(started, deadline)
                        finish_startup_capture(spec.service_id)
                        self._persist()
                        return self._group_snapshot()
                    promote_startup_capture(spec.service_id, identity)
                    self._set_running(spec.service_id, health.message)
                    ready_message = f"{spec.display_name} is ready."
                    if active_preparation_token is not None:
                        self._append_preparation_log(
                            active_preparation_token,
                            ready_message,
                        )
                        self._append_log("inference", "info", ready_message)
                        self._persist()
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
                captured_output = False
                for service_id in tuple(startup_captures):
                    capture = startup_captures.pop(service_id)
                    payload = capture.finish()
                    if payload:
                        self._append_output_payload(service_id, payload)
                        captured_output = True
                if captured_output:
                    self._persist()
                for listener in listeners.values():
                    listener.close()

    def list(self) -> tuple[SupervisorServiceSummary, ...]:
        preparation = self._preparation_summary()
        if preparation is not None:
            return (preparation,)
        with self._mutex:
            self._require_open()
            self._refresh_process_state()
            return tuple(self._summary(record) for record in self._ledger.services)

    def run_binding(self) -> ServiceRunBinding:
        with self._mutex:
            return self._run_binding_locked()

    @property
    def workspace_handoff_root(self) -> Path:
        with self._mutex:
            self._require_open()
            self._root.verify()
            return self._workspace_handoff_root

    def _run_binding_locked(self) -> ServiceRunBinding:
        self._require_open()
        self._verify_release_installation()
        if self._ledger.execution_mode is ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT:
            self._require_active_credential_authority()
        self._refresh_process_state()
        snapshot = self._group_snapshot()
        credential = self._active_credential
        runtime_request = self._active_runtime_request
        immutable_runtime_image = self._active_runtime_image_immutable_reference
        runtime_identity = snapshot.runtime_identity_digest
        self_deployed_runtime = self._active_self_deployed_runtime
        if (
            not snapshot.run_ready
            or credential is None
            or runtime_request is None
            or immutable_runtime_image is None
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
            codex_model=runtime_request.codex_model,
            runtime_image=runtime_request.runtime_image,
            runtime_image_immutable_reference=immutable_runtime_image,
            runtime_identity_digest=runtime_identity,
            generation_digest=snapshot.generation_digest,
            registry_digest=self._release_identity.registry_digest,
            framework_lock_digest=self._framework_lock_digest,
            rollout_url=f"http://127.0.0.1:{ports['rollout']}",
            evolution_backend_url=f"http://127.0.0.1:{ports['evolution-backend']}",
            gateway_url=f"http://127.0.0.1:{ports['gateway']}",
            self_deployed_profile_id=(
                None
                if self_deployed_runtime is None or self_deployed_runtime.profile is None
                else self_deployed_runtime.profile.profile_id
            ),
            self_deployed_profile_sha256=(
                None
                if self_deployed_runtime is None or self_deployed_runtime.profile is None
                else self_deployed_runtime.profile.profile_sha256
            ),
            self_deployed_model_revision=(
                None
                if self_deployed_runtime is None or self_deployed_runtime.profile is None
                else self_deployed_runtime.profile.model_revision
            ),
            self_deployed_model_snapshot_sha256=(
                None
                if self_deployed_runtime is None or self_deployed_runtime.profile is None
                else self_deployed_runtime.profile.model_snapshot_manifest_sha256
            ),
            self_deployed_vllm_image=(
                None
                if self_deployed_runtime is None or self_deployed_runtime.profile is None
                else self_deployed_runtime.profile.vllm_image
            ),
            self_deployed_vllm_image_config_digest=(
                None
                if self_deployed_runtime is None or self_deployed_runtime.profile is None
                else self_deployed_runtime.profile.vllm_image_config_digest
            ),
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
            if self._ledger.execution_mode is ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT:
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
        preparation = self._preparation_summary()
        if preparation is not None and service_id == preparation.id:
            return preparation
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
                raise SupervisorStateError("managed runtime request is unavailable")
            snapshot = self._restart_active_group_locked(
                runtime_request,
                total_timeout=total_timeout,
            )
            result = snapshot.service(service_id)
            self._restart_results[operation_id] = (service_id, result)
            return result

    def restart_once(
        self,
        service_id: str,
        *,
        operation_id: str,
        expected_service_etag: str,
        total_timeout: float | None = None,
    ) -> SupervisorServiceSummary:
        _validate_restart_identity(operation_id, expected_service_etag)
        with self._mutex:
            self._require_open()
            self._verify_release_installation()
            if total_timeout is not None and total_timeout <= 0:
                raise ValueError("total_timeout must be positive")
            prior = self._restart_attempt_or_none(operation_id)
            if prior is not None:
                self._require_matching_restart_attempt(
                    prior,
                    service_id=service_id,
                    expected_service_etag=expected_service_etag,
                )
                if prior.state is ServiceRestartAttemptState.STARTED:
                    raise SupervisorStateError(
                        "restart attempt was already started and cannot be executed again"
                    )
                if prior.service is None:
                    raise SupervisorStateError(
                        "completed restart attempt lacks its service result"
                    )
                return self._restart_service_summary(prior.service)

            record = self._record(service_id)
            current = self._summary(record)
            if current.etag != expected_service_etag:
                raise SupervisorStateError("restart expected service etag does not match")
            if not record.restartable:
                raise SupervisorStateError("service is not restartable in its current state")
            if len(self._ledger.restart_attempts) >= self._max_restart_operations:
                raise SupervisorBusyError("restart attempt receipt capacity is exhausted")
            runtime_request = self._active_runtime_request
            if runtime_request is None:
                raise SupervisorStateError("managed runtime request is unavailable")

            attempt = _LedgerRestartAttempt(
                operation_id=operation_id,
                service_id=service_id,
                expected_service_etag=expected_service_etag,
                state=ServiceRestartAttemptState.STARTED,
            )
            self._ledger.restart_attempts.append(attempt)
            self._persist()

            snapshot = self._restart_active_group_locked(
                runtime_request,
                total_timeout=total_timeout,
            )
            result = snapshot.service(service_id)
            frozen = self._ledger_restart_service(result)
            attempt.state = ServiceRestartAttemptState.COMPLETED
            attempt.service = frozen
            try:
                self._persist()
            except BaseException as original_error:
                self._resync_ledger_after_persist_failure()
                persisted = self._restart_attempt_or_none(operation_id)
                if (
                    persisted is not None
                    and persisted.service_id == service_id
                    and persisted.expected_service_etag == expected_service_etag
                    and persisted.state is ServiceRestartAttemptState.COMPLETED
                    and persisted.service is not None
                ):
                    return self._restart_service_summary(persisted.service)
                raise original_error.with_traceback(original_error.__traceback__)
            return result

    def _restart_active_group_locked(
        self,
        runtime_request: ManagedScienceRuntimeRequest,
        *,
        total_timeout: float | None,
    ) -> ServiceGroupSnapshot:
        execution_mode = self._ledger.execution_mode
        if execution_mode is ServiceExecutionMode.SELF_DEPLOYED:
            request = self._active_self_deployed_request
            if request is None or request.runtime_image != runtime_request.runtime_image:
                raise SupervisorStateError("Self-Deployed runtime request is unavailable")
            return self._ensure_locked(
                execution_mode,
                model_ref=request.profile_id,
                runtime_image=runtime_request.runtime_image,
                total_timeout=total_timeout,
                force_restart=True,
            )
        if execution_mode is ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT:
            return self._ensure_locked(
                execution_mode,
                codex_model=runtime_request.codex_model,
                runtime_image=runtime_request.runtime_image,
                total_timeout=total_timeout,
                force_restart=True,
            )
        raise SupervisorStateError("managed service execution mode is unavailable")

    def list_restart_attempts(self) -> tuple[ServiceRestartAttempt, ...]:
        with self._mutex:
            self._require_open()
            return tuple(self._restart_attempt(item) for item in self._ledger.restart_attempts)

    def acknowledge_restart_attempt(
        self,
        operation_id: str,
        *,
        service_id: str,
        expected_service_etag: str,
    ) -> None:
        _validate_restart_identity(operation_id, expected_service_etag)
        with self._mutex:
            self._require_open()
            self._verify_release_installation()
            attempt = self._restart_attempt_or_none(operation_id)
            if attempt is None:
                raise SupervisorStateError("restart attempt receipt does not exist")
            self._require_matching_restart_attempt(
                attempt,
                service_id=service_id,
                expected_service_etag=expected_service_etag,
            )
            index = self._ledger.restart_attempts.index(attempt)
            del self._ledger.restart_attempts[index]
            try:
                self._persist()
            except BaseException as original_error:
                self._resync_ledger_after_persist_failure()
                persisted = self._restart_attempt_or_none(operation_id)
                if persisted is None:
                    return
                self._require_matching_restart_attempt(
                    persisted,
                    service_id=service_id,
                    expected_service_etag=expected_service_etag,
                )
                raise original_error.with_traceback(original_error.__traceback__)

    def logs(
        self,
        service_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> tuple[SupervisorLogEntry, ...]:
        if after_sequence < 0 or not 1 <= limit <= 100:
            raise ValueError("log snapshot bounds are invalid")
        preparation = self._preparation_logs(
            service_id,
            after_sequence=after_sequence,
            limit=limit,
        )
        if preparation is not None:
            return preparation
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
            if self._active_run_lease is not None:
                raise SupervisorStateError("managed service generation is leased to an active run")
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

    def _begin_public_self_deployed_preparation(
        self,
        execution_mode: ServiceExecutionMode,
        *,
        model_ref: str | None,
        runtime_image: str | None,
    ) -> object | None:
        if (
            execution_mode is not ServiceExecutionMode.SELF_DEPLOYED
            or model_ref is None
            or runtime_image is None
        ):
            return None
        request = SelfDeployedRuntimeRequest(
            profile_id=model_ref,
            runtime_image=runtime_image,
        )
        try:
            return self._begin_self_deployed_preparation(request)
        except SupervisorBusyError:
            # A concurrent ensure already owns the serialized lifecycle operation.
            # This caller will wait on the lifecycle mutex and replay its exact request.
            return None

    def _begin_self_deployed_preparation(
        self,
        request: SelfDeployedRuntimeRequest,
    ) -> object:
        profile = require_release_self_deployed_model_profile(request.profile_id)
        token = object()
        now = _timestamp()
        initial_message = "Preparing the verified Self-Deployed runtime."
        state = _SelfDeployedPreparationState(
            token=token,
            model_ref=profile.model_id,
            identity_digest=_digest_json(
                {
                    "install_digest": self._release_identity.install_digest,
                    "profile_sha256": profile.profile_sha256,
                    "registry_digest": self._release_identity.registry_digest,
                    "runtime_image": request.runtime_image,
                    "state": "preparing",
                }
            ),
            started_at=now,
            updated_at=now,
            log_sequence=1,
            logs=[
                SupervisorLogEntry(
                    id="inference-log-1",
                    sequence=1,
                    occurred_at=now,
                    level="info",
                    message=initial_message,
                    service_id="inference",
                    content_sha256=hashlib.sha256(initial_message.encode("utf-8")).hexdigest(),
                )
            ],
        )
        with self._preparation_mutex:
            if self._self_deployed_preparation is not None:
                raise SupervisorBusyError(
                    "another Self-Deployed runtime preparation is already active"
                )
            self._self_deployed_preparation = state
        return token

    def _append_preparation_log(self, token: object, message: str) -> None:
        safe = _sanitize(message)
        with self._preparation_mutex:
            state = self._self_deployed_preparation
            if state is None or state.token is not token:
                return
            state.log_sequence += 1
            now = _timestamp()
            entry = SupervisorLogEntry(
                id=f"inference-log-{state.log_sequence}",
                sequence=state.log_sequence,
                occurred_at=now,
                level="info",
                message=safe,
                service_id="inference",
                content_sha256=hashlib.sha256(safe.encode("utf-8")).hexdigest(),
            )
            state.logs.append(entry)
            state.updated_at = now
            while len(state.logs) > self._max_log_entries:
                state.logs.pop(0)
            while (
                state.logs
                and sum(len(item.message.encode("utf-8")) for item in state.logs)
                > self._max_log_bytes
            ):
                state.logs.pop(0)

    def _finish_self_deployed_preparation(self, token: object) -> list[str]:
        with self._preparation_mutex:
            state = self._self_deployed_preparation
            if state is None or state.token is not token:
                return []
            messages = [item.message for item in state.logs]
            self._self_deployed_preparation = None
            return messages

    def _self_deployed_preparation_messages(self, token: object) -> list[str]:
        with self._preparation_mutex:
            state = self._self_deployed_preparation
            if state is None or state.token is not token:
                return []
            return [item.message for item in state.logs]

    def _preparation_summary(self) -> SupervisorServiceSummary | None:
        with self._preparation_mutex:
            state = self._self_deployed_preparation
            if state is None:
                return None
            status_message = (
                state.logs[-1].message
                if state.logs
                else "Preparing the verified Self-Deployed runtime."
            )
            return SupervisorServiceSummary(
                id="inference",
                display_name="Managed inference",
                component=ServiceComponent.INFERENCE,
                status=ServiceStatus.STARTING,
                restartable=False,
                status_message=status_message,
                error_code=None,
                updated_at=state.updated_at,
                observed_at=_timestamp(),
                identity_digest=state.identity_digest,
                pid=None,
                port=None,
                etag=_service_summary_etag(
                    component=ServiceComponent.INFERENCE,
                    error_code=None,
                    identity_digest=state.identity_digest,
                    model_status="downloading",
                    pid=None,
                    service_id="inference",
                    status=ServiceStatus.STARTING,
                    status_message=status_message,
                    updated_at=state.updated_at,
                ),
                model_preparation=SupervisorModelPreparation(
                    model_ref=state.model_ref,
                    status="downloading",
                    updated_at=state.updated_at,
                    next_interface="retry_service_ensure",
                ),
            )

    def _preparation_logs(
        self,
        service_id: str,
        *,
        after_sequence: int,
        limit: int,
    ) -> tuple[SupervisorLogEntry, ...] | None:
        with self._preparation_mutex:
            state = self._self_deployed_preparation
            if state is None or service_id != "inference":
                return None
            return tuple(item for item in state.logs if item.sequence > after_sequence)[:limit]

    def _service_plan(
        self,
        runtime_request: ManagedScienceRuntimeRequest,
        runtime_identity_digest: str,
        generation_digest: str,
        credential: str,
        listeners: Mapping[str, socket.socket],
        credential_authority: (
            HeldCodexCredentialAuthority | PreparedCodexCredentialSnapshot | None
        ),
        docker_host_path: DockerHostPathSpec | None,
        *,
        self_deployed_runtime: SelfDeployedRuntimeReadiness | None,
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
        self_deployed = self_deployed_runtime is not None
        if self_deployed and not self_deployed_runtime.ready:
            raise ValueError("Self-Deployed service planning requires ready runtime evidence")
        inference_url = (
            f"http://127.0.0.1:{ports['inference']}" if self_deployed else "http://127.0.0.1:1"
        )
        topology: dict[str, object] = {
            "gateway": {
                "heartbeat_interval_seconds": 2,
                "nodes": [
                    {
                        "host": "127.0.0.1",
                        "id": "core-gateway",
                        "inference": {
                            "base_url": inference_url,
                            "engine": "vllm",
                        },
                        "model_served": runtime_request.codex_model,
                        "port": ports["gateway"],
                        "public_url": gateway_url,
                        **(
                            {}
                            if docker_host_path is None
                            else {"docker_host_path": docker_host_path.model_dump(mode="json")}
                        ),
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
            "evolution": {
                "enabled": True,
                "backend_url": evolution_url,
                "context": {
                    "target_dir": "/openevo/session/evolution",
                    "timeout_seconds": 10,
                    "fail_open": False,
                },
                "event_export": {
                    "enabled": True,
                    "timeout_seconds": 10,
                    "fail_open": False,
                },
            },
        }
        self._root.ensure_directory("tmp")
        base_env = _controlled_environment()
        base_env["TMPDIR"] = os.fspath(self._service_tmp)
        if self._run_admission_url is not None:
            base_env[CORE_RUN_ADMISSION_URL_ENV] = self._run_admission_url
        common_plans = (
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
                    os.fspath(self._framework_lock_path),
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
                    "--managed-execution-mode",
                    (
                        ServiceExecutionMode.SELF_DEPLOYED.value
                        if self_deployed
                        else ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT.value
                    ),
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
                    os.fspath(self._framework_lock_path),
                ),
                None,
                ServiceHealthProbe.http(
                    f"{evolution_url}/v1/health",
                    expected_service_id="evolution-backend",
                    required_worker_id="core-reference-worker",
                ),
            ),
        )
        inference_plans: tuple[
            tuple[
                str,
                str,
                ServiceComponent,
                tuple[str, ...],
                int | None,
                ServiceHealthProbe,
            ],
            ...,
        ] = ()
        if self_deployed:
            assert self_deployed_runtime is not None
            profile = self_deployed_runtime.profile
            model_path = self_deployed_runtime.model_cache_daemon_path
            daemon_container_id = self_deployed_runtime.daemon_container_id
            gpu_device_id = self_deployed_runtime.gpu_device_id
            if (
                profile is None
                or model_path is None
                or daemon_container_id is None
                or gpu_device_id is None
            ):
                raise ValueError("Self-Deployed runtime evidence is incomplete")
            container_name = _self_deployed_container_name(generation_digest)
            inference_argv = (
                DOCKER_EXECUTABLE_PATH,
                "run",
                "--platform",
                "linux/amd64",
                "--rm",
                "--init",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges=true",
                "--pids-limit",
                "4096",
                "--shm-size",
                "4294967296",
                "--stop-timeout",
                "10",
                "--name",
                container_name,
                "--label",
                "io.openevo.managed-service=true",
                "--label",
                f"io.openevo.generation={generation_digest}",
                "--network",
                f"container:{daemon_container_id}",
                "--gpus",
                f"device={gpu_device_id}",
                "--user",
                f"{os.geteuid()}:{os.getegid()}",
                "--env",
                "HF_HUB_OFFLINE=1",
                "--env",
                "TRANSFORMERS_OFFLINE=1",
                "--env",
                "VLLM_NO_USAGE_STATS=1",
                "--env",
                "HOME=/tmp",
                # The serving image cannot enumerate arbitrary host UIDs. These
                # non-secret names keep getpass/cache initialization independent
                # of /etc/passwd while retaining owner-only model file access.
                "--env",
                "USER=openevo",
                "--env",
                "LOGNAME=openevo",
                "--env",
                "HF_HOME=/tmp/huggingface",
                "--env",
                "XDG_CACHE_HOME=/tmp/cache",
                "--env",
                "TORCHINDUCTOR_CACHE_DIR=/tmp/torchinductor",
                "--env",
                "VLLM_CACHE_ROOT=/tmp/vllm-cache",
                "--tmpfs",
                "/tmp:rw,exec,nosuid,size=4294967296",
                "--mount",
                f"type=bind,src={model_path},dst=/openevo/model,readonly",
                "--entrypoint",
                "python3",
                profile.vllm_image,
                "-m",
                "vllm.entrypoints.openai.api_server",
                "--model",
                "/openevo/model",
                "--served-model-name",
                profile.model_id,
                "--host",
                "127.0.0.1",
                "--port",
                str(ports["inference"]),
                *profile.serving_arguments,
            )
            inference_plans = (
                (
                    "inference",
                    "Managed inference",
                    ServiceComponent.INFERENCE,
                    inference_argv,
                    ports["inference"],
                    ServiceHealthProbe.http(
                        f"{inference_url}/v1/models",
                        expected_service_id="inference",
                        expected_model_id=profile.model_id,
                    ),
                ),
            )
        plans = (*inference_plans, *common_plans)
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
            service_env = (
                docker_cli_environment()
                if component is ServiceComponent.INFERENCE
                else dict(base_env)
            )
            if service_id == "gateway":
                service_env[WORKSPACE_HANDOFF_ROOT_ENV] = os.fspath(self._workspace_handoff_root)
            if service_id == "evolution-worker" and self_deployed:
                service_env[CORE_GATEWAY_BASE_URL_ENV] = f"{gateway_url}/v1"
            service_env = dict(sorted(service_env.items()))
            env_digest = _digest_json(service_env)
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
                    "self_deployed_profile_sha256": (
                        None
                        if self_deployed_runtime is None or self_deployed_runtime.profile is None
                        else self_deployed_runtime.profile.profile_sha256
                    ),
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
                    env=service_env,
                    argv_digest=argv_digest,
                    env_digest=env_digest,
                    identity_digest=identity_digest,
                    port=port,
                    health_probe=health_probe,
                    cwd=os.fspath(self._child_cwd),
                    internal_identity=internal_identity,
                    listen_fd=(
                        listeners[service_id].fileno()
                        if service_id in listeners and component is not ServiceComponent.INFERENCE
                        else None
                    ),
                    codex_credential_authority=(
                        credential_authority if service_id == "gateway" else None
                    ),
                )
            )
        return tuple(specs), topology

    def _install_self_deployed_unavailable(
        self,
        *,
        request: SelfDeployedRuntimeRequest,
        runtime: SelfDeployedRuntimeReadiness,
        generation_digest: str,
    ) -> ServiceGroupSnapshot:
        if runtime.ready or runtime.code is ServiceRunReadinessCode.READY:
            raise ValueError("ready Self-Deployed runtime cannot be installed as unavailable")
        profile = require_release_self_deployed_model_profile(request.profile_id)
        now = _timestamp()
        self._ledger.execution_mode = ServiceExecutionMode.SELF_DEPLOYED
        self._ledger.generation_digest = generation_digest
        self._ledger.runtime_identity_digest = None
        self._ledger.runtime_readiness_code = runtime.code
        self._ledger.group_status_message = _sanitize(runtime.message)
        self._ledger.services = [
            _LedgerService(
                service_id="inference",
                display_name="Managed inference",
                component=ServiceComponent.INFERENCE,
                status=ServiceStatus.UNAVAILABLE,
                restartable=False,
                status_message=_sanitize(runtime.message),
                updated_at=now,
                identity_digest=generation_digest,
                argv_digest=_digest_json([]),
                env_digest=_digest_json({}),
                model_ref=profile.model_id,
                model_status="failed",
                model_updated_at=now,
                model_next_interface="retry_service_ensure",
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
        inference_profile: SelfDeployedModelProfile | None = None,
    ) -> None:
        now = _timestamp()
        has_inference = any(spec.component is ServiceComponent.INFERENCE for spec in specs)
        if has_inference != (inference_profile is not None) or (
            has_inference and execution_mode is not ServiceExecutionMode.SELF_DEPLOYED
        ):
            raise SupervisorStateError(
                "planned inference service lacks its exact Self-Deployed profile"
            )
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
                model_ref=(
                    inference_profile.model_id
                    if spec.component is ServiceComponent.INFERENCE
                    and inference_profile is not None
                    else None
                ),
                model_status=("ready" if spec.component is ServiceComponent.INFERENCE else None),
                model_updated_at=(now if spec.component is ServiceComponent.INFERENCE else None),
                model_next_interface=(
                    "run_task" if spec.component is ServiceComponent.INFERENCE else None
                ),
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
            if self._record(record.service_id).error_code in {
                "service_stop_timeout",
                "managed_container_cleanup_failed",
            }:
                stopped = False
        return stopped

    def _active_credential_authority_matches(
        self,
        candidate: HeldCodexCredentialAuthority | PreparedCodexCredentialSnapshot,
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
        candidate: HeldCodexCredentialAuthority | PreparedCodexCredentialSnapshot,
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
        self._active_runtime_image_immutable_reference = None
        self._active_self_deployed_request = None
        self._active_self_deployed_runtime = None
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
        container_cleanup_failed = False
        if (
            record.component is ServiceComponent.INFERENCE
            and self._ledger.execution_mode is ServiceExecutionMode.SELF_DEPLOYED
            and self._ledger.runtime_identity_digest is not None
            and self._ledger.generation_digest is not None
        ):
            removed = self._self_deployed_runtime_probe.remove_managed_container(
                self._ledger.generation_digest,
                deadline,
            )
            if not removed:
                container_cleanup_failed = True
                self._append_log(
                    service_id,
                    "error",
                    "The managed inference container could not be verified and removed.",
                )
            elif identity is not None and self._process_backend.is_alive(identity):
                self._process_backend.wait(
                    identity,
                    max(0.0, deadline - time.monotonic()),
                )
        still_alive = identity is not None and self._process_backend.is_alive(identity)
        record.updated_at = _timestamp()
        if still_alive or container_cleanup_failed:
            if identity is not None and still_alive:
                self._handles[service_id] = identity
                self._write_process_identity(record, identity)
            else:
                self._handles.pop(service_id, None)
                self._clear_process_identity(record)
            record.status = ServiceStatus.FAILED
            record.error_code = (
                "service_stop_timeout" if still_alive else "managed_container_cleanup_failed"
            )
            record.status_message = (
                "Managed child did not exit before the stop deadline."
                if still_alive
                else "Managed inference container cleanup could not be verified."
            )
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

    def _capture_startup_output(
        self,
        capture: _StartupLogCapture,
        service_id: str,
        generation_digest: str,
        identity: ProcessIdentity,
        payload: bytes,
    ) -> None:
        if capture.capture(payload):
            return
        self._record_output(service_id, generation_digest, identity, payload)

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
                if self._active_runtime_request is not None
                else None
            ),
            runtime_image_immutable_reference=(self._active_runtime_image_immutable_reference),
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
            etag=_service_summary_etag(
                component=record.component,
                error_code=record.error_code,
                identity_digest=record.identity_digest,
                model_status=record.model_status,
                pid=record.pid,
                service_id=record.service_id,
                status=record.status,
                status_message=record.status_message,
                updated_at=record.updated_at,
            ),
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
            ledger = _Ledger(schema_version=2, release=expected_release)
            self._ledger = ledger
            self._persist()
            return ledger
        migrated = False
        try:
            decoded = payload.decode("utf-8")
            raw = json.loads(decoded)
            if _canonical_bytes(raw) != payload:
                raise ValueError("ledger is not canonical JSON")
            schema_version = raw.get("schema_version")
            if schema_version == 1:
                legacy = _LedgerV1.model_validate(raw)
                self._validate_loaded_ledger(legacy)
                ledger = _Ledger.model_validate(
                    {
                        **legacy.model_dump(mode="json"),
                        "schema_version": 2,
                        "restart_attempts": [],
                    }
                )
                migrated = True
            else:
                ledger = _Ledger.model_validate(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise SupervisorStateError("service ledger is invalid") from exc
        self._validate_loaded_ledger(ledger)
        if ledger.release != expected_release:
            if ledger.restart_attempts:
                raise SupervisorStateError(
                    "service ledger release identity does not match Core with pending restart receipts"
                )
            if not self._ledger_is_quiescent(ledger):
                raise SupervisorStateError("service ledger release identity does not match Core")
            ledger = _Ledger(schema_version=2, release=expected_release)
            self._ledger = ledger
            self._persist()
            return ledger
        if migrated:
            self._ledger = ledger
            self._persist()
        return ledger

    def _validate_loaded_ledger(self, ledger: _LedgerBase) -> None:
        if ledger.execution_mode is ServiceExecutionMode.SELF_DEPLOYED:
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
                raise SupervisorStateError("Self-Deployed service state lacks runtime evidence")
            if (
                ledger.runtime_identity_digest is not None
                and ledger.runtime_readiness_code
                not in {
                    None,
                    ServiceRunReadinessCode.READY,
                }
            ) or (
                ledger.runtime_identity_digest is None
                and ledger.runtime_readiness_code is ServiceRunReadinessCode.READY
            ):
                raise SupervisorStateError(
                    "Self-Deployed runtime readiness evidence is inconsistent"
                )
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
                if ledger.execution_mode is not ServiceExecutionMode.SELF_DEPLOYED or not all(
                    model_fields
                ):
                    raise SupervisorStateError("inference model preparation state is invalid")
                model_status = record.model_status
                if model_status == "unresolved":
                    valid_model_state = (
                        record.model_next_interface == "model_preparer_v1"
                        and record.status in {ServiceStatus.UNAVAILABLE, ServiceStatus.STOPPED}
                        and ledger.runtime_identity_digest is None
                        and ledger.runtime_readiness_code is None
                    )
                else:
                    release_model = any(
                        profile.model_id == record.model_ref
                        for profile in RELEASE_SELF_DEPLOYED_MODEL_PROFILES.values()
                    )
                    if model_status == "ready":
                        valid_model_state = (
                            release_model
                            and record.model_next_interface == "run_task"
                            and ledger.runtime_identity_digest is not None
                            and ledger.runtime_readiness_code
                            in {None, ServiceRunReadinessCode.READY}
                        )
                    elif model_status == "failed":
                        valid_model_state = (
                            release_model
                            and record.model_next_interface == "retry_service_ensure"
                            and record.status in {ServiceStatus.UNAVAILABLE, ServiceStatus.STOPPED}
                            and ledger.runtime_identity_digest is None
                            and ledger.runtime_readiness_code
                            not in {None, ServiceRunReadinessCode.READY}
                        )
                    else:
                        valid_model_state = False
                if not valid_model_state:
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
        restart_attempts = ledger.restart_attempts if isinstance(ledger, _Ledger) else ()
        if len(restart_attempts) > self._max_restart_operations:
            raise SupervisorStateError("restart attempt receipt capacity is exceeded")
        operation_ids: set[str] = set()
        for attempt in restart_attempts:
            if attempt.operation_id in operation_ids:
                raise SupervisorStateError(
                    "restart attempt ledger contains duplicate operation IDs"
                )
            operation_ids.add(attempt.operation_id)
            if attempt.state is ServiceRestartAttemptState.STARTED:
                if attempt.service is not None:
                    raise SupervisorStateError("started restart attempt contains a service result")
            elif attempt.service is None:
                raise SupervisorStateError("completed restart attempt lacks a service result")
            if attempt.service is not None:
                if attempt.service.id != attempt.service_id:
                    raise SupervisorStateError(
                        "restart attempt service result has a different service identity"
                    )
                service = attempt.service
                if service.etag != _service_summary_etag(
                    component=service.component,
                    error_code=service.error_code,
                    identity_digest=service.identity_digest,
                    model_status=(
                        None
                        if service.model_preparation is None
                        else service.model_preparation.status
                    ),
                    pid=service.pid,
                    service_id=service.id,
                    status=service.status,
                    status_message=service.status_message,
                    updated_at=service.updated_at,
                ):
                    raise SupervisorStateError(
                        "restart attempt service result etag is inconsistent"
                    )
                if (service.status is ServiceStatus.FAILED) != (service.error_code is not None):
                    raise SupervisorStateError(
                        "restart attempt service result failure state is inconsistent"
                    )

    @staticmethod
    def _ledger_is_quiescent(ledger: _Ledger) -> bool:
        return all(
            record.status is ServiceStatus.STOPPED
            and record.pid is None
            and record.birth_token is None
            and record.session_id is None
            and record.process_group_id is None
            and record.ownership_digest is None
            for record in ledger.services
        )

    def _recover_prior_owner_state(self) -> None:
        changed = False
        for record in self._ledger.services:
            if record.pid is not None or record.status in {
                ServiceStatus.STARTING,
                ServiceStatus.RUNNING,
                ServiceStatus.DEGRADED,
            }:
                recovered = True
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
        if (
            self._ledger.execution_mode is ServiceExecutionMode.SELF_DEPLOYED
            and self._ledger.runtime_identity_digest is not None
            and self._ledger.generation_digest is not None
            and not self._self_deployed_runtime_probe.remove_managed_container(
                self._ledger.generation_digest,
                time.monotonic() + self._stop_timeout,
            )
        ):
            raise SupervisorStateError(
                "persisted managed inference container could not be verified and recovered"
            )
        if changed:
            self._persist()

    def _persist(self) -> None:
        payload = _canonical_bytes(self._ledger.model_dump(mode="json"))
        if len(payload) > _MAX_LEDGER_BYTES:
            raise SupervisorStateError("service ledger exceeds its byte limit")
        self._root.atomic_write("ledger.json", payload)

    def _resync_ledger_after_persist_failure(self) -> None:
        """Adopt only a fully validated ledger after an ambiguous atomic write."""
        payload = self._root.read("ledger.json", max_bytes=_MAX_LEDGER_BYTES)
        if payload is None:
            raise SupervisorStateError(
                "service ledger disappeared after an ambiguous state publication"
            )
        try:
            raw = json.loads(payload.decode("utf-8"))
            if _canonical_bytes(raw) != payload:
                raise ValueError("ledger is not canonical JSON")
            ledger = _Ledger.model_validate(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise SupervisorStateError(
                "service ledger cannot be trusted after an ambiguous state publication"
            ) from exc
        self._validate_loaded_ledger(ledger)
        expected_release = _LedgerRelease(
            install_digest=self._release_identity.install_digest,
            registry_digest=self._release_identity.registry_digest,
            framework_lock_digest=self._framework_lock_digest,
        )
        if ledger.release != expected_release:
            raise SupervisorStateError(
                "service ledger release identity changed after an ambiguous state publication"
            )
        self._ledger = ledger

    @staticmethod
    def _ledger_restart_service(
        service: SupervisorServiceSummary,
    ) -> _LedgerRestartService:
        return _LedgerRestartService.model_validate(
            service.__dict__
            if hasattr(service, "__dict__")
            else {
                "id": service.id,
                "display_name": service.display_name,
                "component": service.component.value,
                "status": service.status.value,
                "restartable": service.restartable,
                "status_message": service.status_message,
                "error_code": service.error_code,
                "updated_at": service.updated_at,
                "observed_at": service.observed_at,
                "identity_digest": service.identity_digest,
                "pid": service.pid,
                "port": service.port,
                "etag": service.etag,
                "model_preparation": (
                    None
                    if service.model_preparation is None
                    else {
                        "model_ref": service.model_preparation.model_ref,
                        "status": service.model_preparation.status,
                        "updated_at": service.model_preparation.updated_at,
                        "next_interface": service.model_preparation.next_interface,
                    }
                ),
            }
        )

    @staticmethod
    def _restart_service_summary(
        service: _LedgerRestartService,
    ) -> SupervisorServiceSummary:
        preparation = service.model_preparation
        return SupervisorServiceSummary(
            id=service.id,
            display_name=service.display_name,
            component=service.component,
            status=service.status,
            restartable=service.restartable,
            status_message=service.status_message,
            error_code=service.error_code,
            updated_at=service.updated_at,
            observed_at=service.observed_at,
            identity_digest=service.identity_digest,
            pid=service.pid,
            port=service.port,
            etag=service.etag,
            model_preparation=(
                None
                if preparation is None
                else SupervisorModelPreparation(
                    model_ref=preparation.model_ref,
                    status=preparation.status,
                    updated_at=preparation.updated_at,
                    next_interface=preparation.next_interface,
                )
            ),
        )

    @staticmethod
    def _restart_attempt(attempt: _LedgerRestartAttempt) -> ServiceRestartAttempt:
        return ServiceRestartAttempt(
            operation_id=attempt.operation_id,
            service_id=attempt.service_id,
            expected_service_etag=attempt.expected_service_etag,
            state=attempt.state,
            service=(
                None
                if attempt.service is None
                else CoreServiceSupervisor._restart_service_summary(attempt.service)
            ),
        )

    def _restart_attempt_or_none(self, operation_id: str) -> _LedgerRestartAttempt | None:
        return next(
            (
                attempt
                for attempt in self._ledger.restart_attempts
                if attempt.operation_id == operation_id
            ),
            None,
        )

    @staticmethod
    def _require_matching_restart_attempt(
        attempt: _LedgerRestartAttempt,
        *,
        service_id: str,
        expected_service_etag: str,
    ) -> None:
        if (
            attempt.service_id != service_id
            or attempt.expected_service_etag != expected_service_etag
        ):
            raise SupervisorStateError(
                "restart operation identity was reused for a different request"
            )

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
    allowed = (
        "CUDA_VISIBLE_DEVICES",
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "TMPDIR",
    )
    result = {key: os.environ[key] for key in allowed if key in os.environ}
    search_path = result.get("PATH", os.defpath)
    home = result.get("HOME")
    if home and os.path.isabs(home):
        # Codex' supported device-auth installer places the stable launcher in
        # ~/.local/bin.  Formal Daemons are normally started by a non-login SSH
        # command, so that directory is not guaranteed to be present in PATH
        # even though the same remote user can run `codex` interactively.
        #
        # This only extends discovery.  _HeldProbeExecutable still resolves the
        # candidate, opens the final executable no-follow, validates its owner,
        # mode, inode and size, and hashes the held bytes before executing it.
        user_bin = os.fspath(Path(home) / ".local" / "bin")
        search_entries = search_path.split(os.pathsep)
        if user_bin not in search_entries:
            search_path = os.pathsep.join((user_bin, *search_entries))
    result["PATH"] = search_path
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
                if probe.expected_service_id == "inference":
                    data = payload.get("data")
                    expected_model = probe.expected_model_id
                    if (
                        expected_model is None
                        or not isinstance(data, list)
                        or len(data) != 1
                        or not isinstance(data[0], dict)
                        or data[0].get("id") != expected_model
                    ):
                        return False, "managed inference served-model identity mismatch"
                    return True, "managed inference served-model identity is healthy"
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
        except OSError as exc:
            if _process_group_candidate_no_longer_matches(candidate, identity, exc):
                continue
            return None
        expected = f"{INTERNAL_OWNERSHIP_ENV}={identity.ownership_digest}".encode("ascii")
        if expected not in environment:
            return None
        if pid == identity.pid and not _same_process(identity):
            return None
        members.append(pid)
    return tuple(sorted(members))


def _process_group_candidate_no_longer_matches(
    candidate: Path,
    identity: ProcessIdentity,
    error: OSError,
) -> bool:
    """Confirm that a /proc member vanished or left the owned group after enumeration."""

    if error.errno not in {errno.ENOENT, errno.ESRCH}:
        return False
    try:
        stat_text = (candidate / "stat").read_text(encoding="ascii")
    except OSError as exc:
        return exc.errno in {errno.ENOENT, errno.ESRCH}
    try:
        end = stat_text.rfind(")")
        fields = stat_text[end + 2 :].split()
        state = fields[0]
        process_group_id = int(fields[2])
        session_id = int(fields[3])
    except (UnicodeDecodeError, ValueError, IndexError):
        return False
    return state == "Z" or (
        process_group_id != identity.process_group_id or session_id != identity.session_id
    )


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


def _require_private_framework_lock(info: os.stat_result, uid: int) -> None:
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise SupervisorStateError("framework lock is not a link-count-one regular file")
    if info.st_uid != uid or stat.S_IMODE(info.st_mode) not in {0o400, _FILE_MODE}:
        raise SupervisorStateError("framework lock must be owner-only mode 0600 or immutable 0400")


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


def _service_summary_etag(
    *,
    component: ServiceComponent,
    error_code: str | None,
    identity_digest: str,
    model_status: str | None,
    pid: int | None,
    service_id: str,
    status: ServiceStatus,
    status_message: str | None,
    updated_at: str,
) -> str:
    payload = {
        "component": component.value,
        "error_code": error_code,
        "identity_digest": identity_digest,
        "model_status": model_status,
        "pid": pid,
        "service_id": service_id,
        "status": status.value,
        "status_message": status_message,
        "updated_at": updated_at,
    }
    return f'"{_digest_json(payload)}"'


def _validate_restart_identity(operation_id: str, expected_service_etag: str) -> None:
    if (
        not isinstance(operation_id, str)
        or not operation_id
        or len(operation_id) > 128
        or any(ord(char) < 0x20 for char in operation_id)
    ):
        raise ValueError("restart operation_id is invalid")
    if (
        not isinstance(expected_service_etag, str)
        or _STRONG_ETAG_RE.fullmatch(expected_service_etag) is None
    ):
        raise ValueError("restart expected_service_etag is invalid")


def _binding_matches_snapshot(
    snapshot: ServiceGroupSnapshot,
    binding: ServiceRunBinding,
) -> bool:
    return (
        snapshot.run_ready
        and binding.execution_mode is snapshot.execution_mode
        and binding.runtime_image == snapshot.runtime_image
        and binding.runtime_image_immutable_reference == snapshot.runtime_image_immutable_reference
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
