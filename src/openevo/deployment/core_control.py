from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import hmac
import os
import re
import secrets
import shlex
import socket
import sys
import time
from typing import Literal, Protocol

from pydantic import SecretStr

from openevo.backend.runtime_identity import RuntimeIdentityError, load_bounded_json
from openevo.backend.service import (
    CoreServiceError,
    CoreServiceErrorCode,
    authenticate_core_service_endpoint,
)
from openevo.deployment.executor import RemoteExecutorTransport
from openevo.deployment.core_runtime import (
    CorePythonRuntimeAuthority,
    build_verified_python_command,
)
from openevo.deployment.preflight import RemoteCommandResult
from openevo.deployment.ssh import SshTransportError, SshTransportErrorCode


_BEARER_PATTERN = re.compile(r"[A-Za-z0-9_-]{64}\Z")
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_MAX_BOOTSTRAP_JSON_BYTES = 4096
_REMOTE_PATH_PATTERN = re.compile(r"/(?:[A-Za-z0-9._@%+=,-]+/)*[A-Za-z0-9._@%+=,-]+\Z")
_GENERATION_BOOTSTRAP = """
import os
from pathlib import Path
import subprocess
import stat
import sys
import venv

install_root = Path(sys.argv[1])
wheel_path = Path(sys.argv[2])
base_executable = sys.argv[3]
service_argv = sys.argv[4:]
# FD-bound invocation deliberately reports /proc/self/fd/N. Restore the verified
# canonical base path so venv copies and pyvenv.cfg bind the selected runtime.
sys.executable = base_executable
sys._base_executable = base_executable
parent = install_root.parent
service_root = parent.parent
service_root.mkdir(mode=0o700, parents=True, exist_ok=True)
service_metadata = service_root.lstat()
if (
    not stat.S_ISDIR(service_metadata.st_mode)
    or service_metadata.st_uid != os.geteuid()
    or service_metadata.st_mode & 0o777 != 0o700
):
    raise SystemExit(71)
parent.mkdir(mode=0o700, exist_ok=True)
parent_metadata = parent.lstat()
if (
    not stat.S_ISDIR(parent_metadata.st_mode)
    or parent_metadata.st_uid != os.geteuid()
    or parent_metadata.st_mode & 0o777 != 0o700
):
    raise SystemExit(72)
install_root.mkdir(mode=0o700)
venv.EnvBuilder(with_pip=True, symlinks=False).create(install_root)
interpreter = install_root / "bin" / "python"
environment = {
    key: value
    for key, value in os.environ.items()
    if key
    in {
        "HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LC_ALL",
        "NO_PROXY",
        "PATH",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
}
completed = subprocess.run(
    [
        str(interpreter),
        "-I",
        "-m",
        "pip",
        "install",
        "--isolated",
        "--disable-pip-version-check",
        "--only-binary=:all:",
        "--no-compile",
        str(wheel_path),
    ],
    env=environment,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    check=False,
)
if completed.returncode != 0:
    raise SystemExit(73)
os.execve(
    interpreter,
    [str(interpreter), "-I", "-m", "openevo.backend.service", *service_argv],
    environment,
)
""".strip()


class CoreControlBootstrapErrorCode(StrEnum):
    INVALID_PLAN = "core_bootstrap_plan_invalid"
    INSTALL_FAILED = "core_bootstrap_install_failed"
    VERIFICATION_FAILED = "core_bootstrap_verification_failed"
    SERVICE_FAILED = "core_bootstrap_service_failed"
    RESPONSE_INVALID = "core_bootstrap_response_invalid"
    DEADLINE_EXCEEDED = "core_bootstrap_deadline_exceeded"


class CoreControlBootstrapError(RuntimeError):
    def __init__(
        self,
        code: CoreControlBootstrapErrorCode,
        message: str,
        *,
        retryable: bool,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class CoreControlBootstrapPlan:
    source_commit: str
    port: int = 0
    deadline_seconds: float = 90.0
    replace_mismatched: bool = False
    _wheel_path: str = field(repr=False, compare=False, default="")
    _framework_lock: str = field(repr=False, compare=False, default="")
    _service_root: str = field(repr=False, compare=False, default="")
    _runtime: CorePythonRuntimeAuthority | None = field(
        repr=False,
        compare=False,
        default=None,
    )

    def __post_init__(self) -> None:
        if (
            _SOURCE_COMMIT_PATTERN.fullmatch(self.source_commit) is None
            or not 0 <= self.port <= 65535
            or not 1 <= self.deadline_seconds <= 300
            or not _is_remote_absolute_path(self._wheel_path)
            or not _is_remote_absolute_path(self._framework_lock)
            or not _is_remote_absolute_path(self._service_root)
            or not isinstance(self._runtime, CorePythonRuntimeAuthority)
        ):
            raise CoreControlBootstrapError(
                CoreControlBootstrapErrorCode.INVALID_PLAN,
                "Core bootstrap settings are invalid.",
                retryable=False,
            )
        assert self._runtime is not None
        try:
            self._runtime.__post_init__()
        except ValueError:
            raise CoreControlBootstrapError(
                CoreControlBootstrapErrorCode.INVALID_PLAN,
                "Core bootstrap settings are invalid.",
                retryable=False,
            ) from None


@dataclass(frozen=True, slots=True)
class RemoteCoreControlAttachment:
    remote_host: str
    remote_port: int
    execution_mode: Literal["subscription"]
    capture_mode: Literal["transcript"]
    release_identity: str
    registry_digest: str
    source_commit: str
    generation: str
    status_proof: str
    attached: bool
    _bearer: SecretStr = field(repr=False, compare=False)

    @property
    def bearer_token(self) -> str:
        return self._bearer.get_secret_value()


class CoreTunnelHandle(Protocol):
    @property
    def base_url(self) -> str: ...

    def verify_authority(self) -> None: ...

    def open_verified_socket(self, *, timeout_seconds: float) -> socket.socket: ...

    def close(self) -> None: ...


class CoreBootstrapTransport(RemoteExecutorTransport, Protocol):
    def run_secret(
        self,
        command: str,
        *,
        timeout_seconds: float = 30.0,
    ) -> SecretStr: ...


class CoreTunnelTransport(Protocol):
    def open_core_tunnel(
        self,
        *,
        remote_port: int,
        remote_host: str = "127.0.0.1",
        wait_for_ready: bool = True,
        timeout_seconds: float = 10.0,
    ) -> CoreTunnelHandle: ...


@dataclass(frozen=True, slots=True)
class VerifiedCoreControlTunnel:
    base_url: str
    release_identity: str
    registry_digest: str
    source_commit: str
    generation: str
    status_proof: str
    _tunnel: CoreTunnelHandle = field(repr=False, compare=False)
    _bearer: SecretStr = field(repr=False, compare=False)

    @property
    def bearer_token(self) -> str:
        return self._bearer.get_secret_value()

    def verify_authority(self) -> None:
        self._tunnel.verify_authority()

    def open_verified_socket(self, *, timeout_seconds: float) -> socket.socket:
        return self._tunnel.open_verified_socket(timeout_seconds=timeout_seconds)

    def close(self) -> None:
        self._tunnel.close()

    def __enter__(self) -> VerifiedCoreControlTunnel:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()


def build_core_control_bootstrap_plan(
    *,
    runtime: CorePythonRuntimeAuthority,
    wheel_path: str,
    framework_lock: str,
    service_root: str,
    source_commit: str,
    port: int = 0,
    deadline_seconds: float = 90.0,
    replace_mismatched: bool = False,
) -> CoreControlBootstrapPlan:
    if (
        not _is_remote_absolute_path(wheel_path)
        or not _is_remote_absolute_path(framework_lock)
        or not _is_remote_absolute_path(service_root)
        or _SOURCE_COMMIT_PATTERN.fullmatch(source_commit) is None
        or not 0 <= port <= 65535
        or not 1 <= deadline_seconds <= 300
        or not isinstance(runtime, CorePythonRuntimeAuthority)
    ):
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.INVALID_PLAN,
            "Core bootstrap settings are invalid.",
            retryable=False,
        )
    try:
        runtime.__post_init__()
    except ValueError:
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.INVALID_PLAN,
            "Core bootstrap settings are invalid.",
            retryable=False,
        ) from None
    return CoreControlBootstrapPlan(
        source_commit=source_commit,
        port=port,
        deadline_seconds=deadline_seconds,
        replace_mismatched=replace_mismatched,
        _wheel_path=wheel_path,
        _framework_lock=framework_lock,
        _service_root=service_root,
        _runtime=runtime,
    )


def execute_core_control_bootstrap(
    plan: CoreControlBootstrapPlan,
    transport: CoreBootstrapTransport,
) -> RemoteCoreControlAttachment:
    deadline = time.monotonic() + plan.deadline_seconds
    if plan._runtime is None:
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.INVALID_PLAN,
            "Core bootstrap settings are invalid.",
            retryable=False,
        )
    attachment_name = f"bootstrap-{secrets.token_hex(16)}.json"
    install_generation = secrets.token_hex(16)
    install_root = f"{plan._service_root}/releases/{install_generation}"
    service_arguments = (
        "bootstrap"
        f" --service-root {shlex.quote(plan._service_root)}"
        f" --wheel-path {shlex.quote(plan._wheel_path)}"
        f" --framework-lock {shlex.quote(plan._framework_lock)}"
        f" --source-commit {plan.source_commit}"
        f" --attachment-name {attachment_name}"
        f" --install-generation {install_generation}"
        f" --port {plan.port}"
        f" --deadline-seconds {max(1.0, plan.deadline_seconds)}"
        + (" --replace-mismatched" if plan.replace_mismatched else "")
    )
    service_command = build_verified_python_command(
        plan._runtime,
        _GENERATION_BOOTSTRAP,
        install_root,
        plan._wheel_path,
        plan._runtime.executable_path,
        *shlex.split(service_arguments),
    )
    service = _run_bootstrap_command(
        transport,
        service_command,
        deadline=deadline,
        code=CoreControlBootstrapErrorCode.SERVICE_FAILED,
        message="Core Control could not be attached or started.",
    )
    if not service.ok:
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.SERVICE_FAILED,
            "Core Control could not be attached or started.",
            retryable=True,
        )
    consume_command = (
        f"{shlex.quote(install_root + '/bin/python')} -I "
        "-m openevo.backend.service consume-attachment"
        f" --service-root {shlex.quote(plan._service_root)}"
        f" --attachment-name {attachment_name}"
    )
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.DEADLINE_EXCEEDED,
            "Core bootstrap exceeded its total deadline.",
            retryable=True,
        )
    try:
        payload = transport.run_secret(consume_command, timeout_seconds=remaining)
    except SshTransportError as exc:
        if exc.code is SshTransportErrorCode.TIMEOUT:
            raise CoreControlBootstrapError(
                CoreControlBootstrapErrorCode.DEADLINE_EXCEEDED,
                "Core bootstrap exceeded its total deadline.",
                retryable=True,
            ) from None
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.RESPONSE_INVALID,
            "Core bootstrap attachment could not be read securely.",
            retryable=True,
        ) from None
    except Exception:
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.RESPONSE_INVALID,
            "Core bootstrap attachment could not be read securely.",
            retryable=True,
        ) from None
    return parse_core_control_attachment(payload)


def _run_bootstrap_command(
    transport: RemoteExecutorTransport,
    command: str,
    *,
    deadline: float,
    code: CoreControlBootstrapErrorCode,
    message: str,
) -> RemoteCommandResult:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.DEADLINE_EXCEEDED,
            "Core bootstrap exceeded its total deadline.",
            retryable=True,
        )
    try:
        return transport.run(command, timeout_seconds=remaining)
    except SshTransportError as exc:
        if exc.code is SshTransportErrorCode.TIMEOUT:
            raise CoreControlBootstrapError(
                CoreControlBootstrapErrorCode.DEADLINE_EXCEEDED,
                "Core bootstrap exceeded its total deadline.",
                retryable=True,
            ) from None
        raise CoreControlBootstrapError(code, message, retryable=True) from None
    except Exception:
        raise CoreControlBootstrapError(code, message, retryable=True) from None


def parse_core_control_attachment(payload: SecretStr) -> RemoteCoreControlAttachment:
    if not isinstance(payload, SecretStr):
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.RESPONSE_INVALID,
            "Core bootstrap returned an invalid attachment.",
            retryable=False,
        )
    payload_value = payload.get_secret_value()
    if len(payload_value) > _MAX_BOOTSTRAP_JSON_BYTES:
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.RESPONSE_INVALID,
            "Core bootstrap returned an invalid attachment.",
            retryable=False,
        )
    try:
        encoded = payload_value.encode("utf-8")
    except UnicodeError:
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.RESPONSE_INVALID,
            "Core bootstrap returned an invalid attachment.",
            retryable=False,
        ) from None
    try:
        value = load_bounded_json(encoded, max_bytes=_MAX_BOOTSTRAP_JSON_BYTES)
    except RuntimeIdentityError:
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.RESPONSE_INVALID,
            "Core bootstrap returned an invalid attachment.",
            retryable=False,
        ) from None
    expected = {
        "schema_version",
        "host",
        "port",
        "release_identity",
        "registry_digest",
        "source_commit",
        "generation",
        "status_proof",
        "attached",
        "bearer_token",
        "execution_mode",
        "capture_mode",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.RESPONSE_INVALID,
            "Core bootstrap returned an invalid attachment.",
            retryable=False,
        )
    port = value.get("port")
    attached = value.get("attached")
    bearer = value.get("bearer_token")
    if (
        value.get("schema_version") != 1
        or value.get("host") != "127.0.0.1"
        or type(port) is not int
        or not 1 <= port <= 65535
        or type(attached) is not bool
        or value.get("execution_mode") != "subscription"
        or value.get("capture_mode") != "transcript"
        or not isinstance(bearer, str)
        or _BEARER_PATTERN.fullmatch(bearer) is None
        or not _valid_digest(value.get("release_identity"))
        or not _valid_digest(value.get("registry_digest"))
        or not isinstance(value.get("source_commit"), str)
        or _SOURCE_COMMIT_PATTERN.fullmatch(value["source_commit"]) is None
        or not _valid_digest(value.get("status_proof"))
        or not isinstance(value.get("generation"), str)
        or re.fullmatch(r"[0-9a-f]{32}", value["generation"]) is None
    ):
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.RESPONSE_INVALID,
            "Core bootstrap returned an invalid attachment.",
            retryable=False,
        )
    return RemoteCoreControlAttachment(
        remote_host="127.0.0.1",
        remote_port=port,
        execution_mode="subscription",
        capture_mode="transcript",
        release_identity=value["release_identity"],
        registry_digest=value["registry_digest"],
        source_commit=value["source_commit"],
        generation=value["generation"],
        status_proof=value["status_proof"],
        attached=attached,
        _bearer=SecretStr(bearer),
    )


def open_core_control_tunnel(
    attachment: RemoteCoreControlAttachment,
    transport: CoreTunnelTransport,
    *,
    timeout_seconds: float = 10.0,
) -> VerifiedCoreControlTunnel:
    if timeout_seconds <= 0 or timeout_seconds > 60:
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.INVALID_PLAN,
            "Core tunnel settings are invalid.",
            retryable=False,
        )
    try:
        tunnel = transport.open_core_tunnel(
            remote_port=attachment.remote_port,
            remote_host="127.0.0.1",
            wait_for_ready=True,
            timeout_seconds=timeout_seconds,
        )
    except SshTransportError as exc:
        code = (
            CoreControlBootstrapErrorCode.DEADLINE_EXCEEDED
            if exc.code is SshTransportErrorCode.TIMEOUT
            else CoreControlBootstrapErrorCode.SERVICE_FAILED
        )
        message = (
            "Core Control tunnel opening exceeded its deadline."
            if code is CoreControlBootstrapErrorCode.DEADLINE_EXCEEDED
            else "The Core Control tunnel could not be opened."
        )
        raise CoreControlBootstrapError(code, message, retryable=True) from None
    except Exception:
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.SERVICE_FAILED,
            "The Core Control tunnel could not be opened.",
            retryable=True,
        ) from None
    verified: VerifiedCoreControlTunnel | None = None
    try:
        expected_base_url = "http://openevo-core.local"
        if tunnel.base_url != expected_base_url:
            raise CoreControlBootstrapError(
                CoreControlBootstrapErrorCode.RESPONSE_INVALID,
                "The Core Control tunnel endpoint is invalid.",
                retryable=False,
            )
        proof = authenticate_core_service_endpoint(
            host=None,
            port=None,
            bearer=attachment.bearer_token,
            release_identity=attachment.release_identity,
            registry_digest=attachment.registry_digest,
            source_commit=attachment.source_commit,
            generation=attachment.generation,
            deadline=time.monotonic() + timeout_seconds,
            endpoint=tunnel,
        )
        if not hmac.compare_digest(proof, attachment.status_proof):
            raise CoreControlBootstrapError(
                CoreControlBootstrapErrorCode.RESPONSE_INVALID,
                "The Core Control tunnel identity did not match its attachment.",
                retryable=False,
            )
        tunnel.verify_authority()
        verified = VerifiedCoreControlTunnel(
            base_url=expected_base_url,
            release_identity=attachment.release_identity,
            registry_digest=attachment.registry_digest,
            source_commit=attachment.source_commit,
            generation=attachment.generation,
            status_proof=proof,
            _tunnel=tunnel,
            _bearer=SecretStr(attachment.bearer_token),
        )
        return verified
    except CoreControlBootstrapError:
        raise
    except CoreServiceError as exc:
        if exc.code is CoreServiceErrorCode.DEADLINE_EXCEEDED:
            code = CoreControlBootstrapErrorCode.DEADLINE_EXCEEDED
            message = "Core Control tunnel authentication exceeded its deadline."
        elif exc.code is CoreServiceErrorCode.STATUS_INVALID:
            code = CoreControlBootstrapErrorCode.RESPONSE_INVALID
            message = "The Core Control tunnel identity response was invalid."
        else:
            code = CoreControlBootstrapErrorCode.SERVICE_FAILED
            message = "The Core Control tunnel could not reach the remote daemon."
        raise CoreControlBootstrapError(
            code,
            message,
            retryable=exc.retryable,
        ) from None
    except Exception:
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.SERVICE_FAILED,
            "The Core Control tunnel could not reach the remote daemon.",
            retryable=True,
        ) from None
    finally:
        if verified is None or sys.exc_info()[0] is not None:
            try:
                tunnel.close()
            except BaseException:
                pass


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST_PATTERN.fullmatch(value) is not None


def _is_remote_absolute_path(value: str) -> bool:
    return (
        isinstance(value, str)
        and _REMOTE_PATH_PATTERN.fullmatch(value) is not None
        and os.pathsep not in value
        and "\0" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/")[1:])
    )


__all__ = [
    "CoreBootstrapTransport",
    "CoreControlBootstrapError",
    "CoreControlBootstrapErrorCode",
    "CoreControlBootstrapPlan",
    "RemoteCoreControlAttachment",
    "VerifiedCoreControlTunnel",
    "build_core_control_bootstrap_plan",
    "execute_core_control_bootstrap",
    "open_core_control_tunnel",
    "parse_core_control_attachment",
]
