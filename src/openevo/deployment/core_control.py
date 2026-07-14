from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import re
import shlex
import time
from typing import Literal, Protocol

from pydantic import SecretStr

from openevo.backend.runtime_identity import RuntimeIdentityError, load_bounded_json
from openevo.deployment.executor import RemoteExecutorTransport
from openevo.deployment.preflight import RemoteCommandResult


_BEARER_PATTERN = re.compile(r"[A-Za-z0-9_-]{64}\Z")
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_MAX_BOOTSTRAP_JSON_BYTES = 4096


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

    def __post_init__(self) -> None:
        if (
            _SOURCE_COMMIT_PATTERN.fullmatch(self.source_commit) is None
            or not 0 <= self.port <= 65535
            or not 1 <= self.deadline_seconds <= 300
            or not _is_remote_absolute_path(self._wheel_path)
            or not _is_remote_absolute_path(self._framework_lock)
            or not _is_remote_absolute_path(self._service_root)
        ):
            raise CoreControlBootstrapError(
                CoreControlBootstrapErrorCode.INVALID_PLAN,
                "Core bootstrap settings are invalid.",
                retryable=False,
            )


@dataclass(frozen=True, slots=True)
class RemoteCoreControlAttachment:
    remote_host: str
    remote_port: int
    execution_mode: Literal["subscription"]
    capture_mode: Literal["transcript"]
    release_identity: str
    registry_digest: str
    generation: str
    status_proof: str
    attached: bool
    _bearer: SecretStr = field(repr=False, compare=False)

    @property
    def bearer_token(self) -> str:
        return self._bearer.get_secret_value()


class CoreTunnelTransport(Protocol):
    def open_tunnel(
        self,
        *,
        remote_port: int,
        remote_host: str = "127.0.0.1",
        wait_for_ready: bool = True,
        timeout_seconds: float = 10.0,
    ) -> object: ...


def build_core_control_bootstrap_plan(
    *,
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
    ):
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.INVALID_PLAN,
            "Core bootstrap settings are invalid.",
            retryable=False,
        )
    return CoreControlBootstrapPlan(
        source_commit=source_commit,
        port=port,
        deadline_seconds=deadline_seconds,
        replace_mismatched=replace_mismatched,
        _wheel_path=wheel_path,
        _framework_lock=framework_lock,
        _service_root=service_root,
    )


def execute_core_control_bootstrap(
    plan: CoreControlBootstrapPlan,
    transport: RemoteExecutorTransport,
) -> RemoteCoreControlAttachment:
    deadline = time.monotonic() + plan.deadline_seconds
    verification_command = (
        "python3 -c "
        + shlex.quote(
            "from openevo.evolution.framework import "
            "load_verified_framework_registry as load; "
            "import sys; load(sys.argv[1])"
        )
        + " "
        + shlex.quote(plan._framework_lock)
    )
    verification = _run_bootstrap_command(
        transport,
        verification_command,
        deadline=deadline,
        code=CoreControlBootstrapErrorCode.VERIFICATION_FAILED,
        message="The installed Core release could not be verified.",
    )
    if not verification.ok:
        install = _run_bootstrap_command(
            transport,
            "python3 -m pip install --user --no-deps --force-reinstall "
            + shlex.quote(plan._wheel_path),
            deadline=deadline,
            code=CoreControlBootstrapErrorCode.INSTALL_FAILED,
            message="The verified Core wheel could not be installed.",
        )
        if not install.ok:
            raise CoreControlBootstrapError(
                CoreControlBootstrapErrorCode.INSTALL_FAILED,
                "The verified Core wheel could not be installed.",
                retryable=True,
            )
        verification = _run_bootstrap_command(
            transport,
            verification_command,
            deadline=deadline,
            code=CoreControlBootstrapErrorCode.VERIFICATION_FAILED,
            message="The installed Core release could not be verified.",
        )
        if not verification.ok:
            raise CoreControlBootstrapError(
                CoreControlBootstrapErrorCode.VERIFICATION_FAILED,
                "The installed Core release did not match its framework lock.",
                retryable=False,
            )
    service_command = (
        "python3 -m openevo.backend.service ensure --bootstrap-json"
        f" --service-root {shlex.quote(plan._service_root)}"
        f" --framework-lock {shlex.quote(plan._framework_lock)}"
        f" --source-commit {plan.source_commit}"
        f" --port {plan.port}"
        f" --deadline-seconds {max(1.0, plan.deadline_seconds)}"
        + (" --replace-mismatched" if plan.replace_mismatched else "")
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
    return parse_core_control_attachment(service.stdout)


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
    except Exception:
        raise CoreControlBootstrapError(code, message, retryable=True) from None


def parse_core_control_attachment(payload: str) -> RemoteCoreControlAttachment:
    if len(payload) > _MAX_BOOTSTRAP_JSON_BYTES:
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.RESPONSE_INVALID,
            "Core bootstrap returned an invalid attachment.",
            retryable=False,
        )
    try:
        encoded = payload.encode("utf-8")
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
) -> object:
    if timeout_seconds <= 0 or timeout_seconds > 60:
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.INVALID_PLAN,
            "Core tunnel settings are invalid.",
            retryable=False,
        )
    try:
        return transport.open_tunnel(
            remote_port=attachment.remote_port,
            remote_host="127.0.0.1",
            wait_for_ready=True,
            timeout_seconds=timeout_seconds,
        )
    except Exception:
        raise CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.SERVICE_FAILED,
            "The Core Control tunnel could not be opened.",
            retryable=True,
        ) from None


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST_PATTERN.fullmatch(value) is not None


def _is_remote_absolute_path(value: str) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("/")
        and "\0" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/")[1:])
    )


__all__ = [
    "CoreControlBootstrapError",
    "CoreControlBootstrapErrorCode",
    "CoreControlBootstrapPlan",
    "RemoteCoreControlAttachment",
    "build_core_control_bootstrap_plan",
    "execute_core_control_bootstrap",
    "open_core_control_tunnel",
    "parse_core_control_attachment",
]
