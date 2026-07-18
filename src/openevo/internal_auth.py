"""Ephemeral authentication for Core-owned internal service traffic."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import hmac
import json
import os
import re
import stat
from typing import Any, Protocol
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx


INTERNAL_CREDENTIAL_FD_ENV = "OPENEVO_INTERNAL_CREDENTIAL_FD"
INTERNAL_LISTEN_FD_ENV = "OPENEVO_INTERNAL_LISTEN_FD"
INTERNAL_OWNERSHIP_ENV = "OPENEVO_INTERNAL_OWNERSHIP_DIGEST"
INTERNAL_AUTHORIZATION_HEADER = "authorization"
INTERNAL_GENERATION_HEADER = "x-openevo-internal-generation"
INTERNAL_REGISTRY_HEADER = "x-openevo-internal-registry"
INTERNAL_SERVICE_HEADER = "x-openevo-internal-service"
CORE_RUN_ADMISSION_URL_ENV = "OPENEVO_CORE_RUN_ADMISSION_URL"
_MAX_CREDENTIAL_PAYLOAD_BYTES = 4096
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_SERVICE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


class InternalAuthError(RuntimeError):
    """The inherited internal-service identity is missing or malformed."""


class RunAdmissionOperation(StrEnum):
    ROLLOUT_TASK_SUBMIT = "rollout_task_submit"
    GATEWAY_SESSION_CREATE = "gateway_session_create"
    GATEWAY_SESSION_DISPATCH = "gateway_session_dispatch"


@dataclass(frozen=True, slots=True)
class GenerationBoundRunAdmissionCheck:
    """Closed identity passed to the future Core run-owner verifier."""

    operation: RunAdmissionOperation
    generation_digest: str
    registry_digest: str
    framework_lock_digest: str
    payload_sha256: str
    task_id: str | None
    session_id: str | None

    def __post_init__(self) -> None:
        for value, label in (
            (self.generation_digest, "generation_digest"),
            (self.registry_digest, "registry_digest"),
            (self.framework_lock_digest, "framework_lock_digest"),
            (self.payload_sha256, "payload_sha256"),
        ):
            if _DIGEST_RE.fullmatch(value) is None:
                raise ValueError(f"run admission {label} is invalid")
        for value, label in ((self.task_id, "task_id"), (self.session_id, "session_id")):
            if value is not None and (not value or len(value.encode("utf-8")) > 256):
                raise ValueError(f"run admission {label} is invalid")


class GenerationBoundRunAdmissionVerifier(Protocol):
    """Trusted run-owner interface; successful return authorizes one exact payload."""

    async def verify(self, check: GenerationBoundRunAdmissionCheck) -> None: ...


class CoreRunAdmissionHttpVerifier:
    """Ask the host-global Core run owner to authorize an exact service request."""

    def __init__(
        self,
        identity: InternalServiceIdentity,
        endpoint: str,
        *,
        timeout_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 30:
            raise ValueError("run admission timeout is outside the supported bounds")
        self._identity = identity
        self._endpoint = _validated_run_admission_endpoint(endpoint)
        self._timeout = timeout_seconds
        self._transport = transport

    async def verify(self, check: GenerationBoundRunAdmissionCheck) -> None:
        payload = {
            "framework_lock_digest": check.framework_lock_digest,
            "generation_digest": check.generation_digest,
            "operation": check.operation.value,
            "payload_sha256": check.payload_sha256,
            "registry_digest": check.registry_digest,
            "session_id": check.session_id,
            "task_id": check.task_id,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                trust_env=False,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    self._endpoint,
                    headers=self._identity.request_headers(),
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise RunAdmissionError(
                "run_admission_authority_unavailable",
                "Core run admission authority could not be reached.",
                status_code=503,
                retryable=True,
            ) from exc
        if response.status_code == 204:
            return
        raise RunAdmissionError(
            "run_admission_denied",
            "Core run admission authority rejected the service request.",
            status_code=(
                response.status_code if 400 <= response.status_code <= 599 else 503
            ),
            retryable=response.status_code >= 500,
        )


def configured_run_admission_verifier(
    identity: InternalServiceIdentity | None,
) -> GenerationBoundRunAdmissionVerifier | None:
    if identity is None:
        return None
    endpoint = os.environ.get(CORE_RUN_ADMISSION_URL_ENV)
    if endpoint is None:
        return None
    return CoreRunAdmissionHttpVerifier(identity, endpoint)


class RunAdmissionError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int,
        retryable: bool,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable


def _validated_run_admission_endpoint(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("run admission endpoint is invalid") from exc
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
        raise ValueError("run admission endpoint must be the Core loopback verifier")
    return f"http://127.0.0.1:{port}/internal/v1/run-admissions/verify"


@dataclass(frozen=True, slots=True)
class InternalServiceIdentity:
    """One generation-scoped service credential delivered through an inherited FD."""

    service_id: str
    generation_digest: str
    registry_digest: str
    framework_lock_digest: str
    credential: str = field(repr=False)

    def __post_init__(self) -> None:
        if _SERVICE_ID_RE.fullmatch(self.service_id) is None:
            raise ValueError("internal service_id is invalid")
        for value, label in (
            (self.generation_digest, "generation_digest"),
            (self.registry_digest, "registry_digest"),
            (self.framework_lock_digest, "framework_lock_digest"),
        ):
            if _DIGEST_RE.fullmatch(value) is None:
                raise ValueError(f"internal {label} is invalid")
        encoded = self.credential.encode("utf-8")
        if len(encoded) < 32 or len(encoded) > 512 or any(byte < 0x21 for byte in encoded):
            raise ValueError("internal credential is outside the closed byte policy")

    @property
    def auth_digest(self) -> str:
        return hashlib.sha256(self.credential.encode("utf-8")).hexdigest()

    def inherited_payload(self) -> bytes:
        return json.dumps(
            {
                "credential": self.credential,
                "generation_digest": self.generation_digest,
                "framework_lock_digest": self.framework_lock_digest,
                "registry_digest": self.registry_digest,
                "service_id": self.service_id,
            },
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def request_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.credential}",
            "X-OpenEvo-Internal-Generation": self.generation_digest,
            "X-OpenEvo-Internal-Registry": self.registry_digest,
            "X-OpenEvo-Internal-Service": self.service_id,
        }

    def health_identity(self) -> dict[str, str]:
        return {
            "auth_digest": self.auth_digest,
            "generation_digest": self.generation_digest,
            "framework_lock_digest": self.framework_lock_digest,
            "registry_digest": self.registry_digest,
            "service_id": self.service_id,
        }

    def authenticates(self, headers: Mapping[str, str]) -> bool:
        authorization = headers.get(INTERNAL_AUTHORIZATION_HEADER, "")
        expected = f"Bearer {self.credential}"
        generation = headers.get(INTERNAL_GENERATION_HEADER, "")
        registry = headers.get(INTERNAL_REGISTRY_HEADER, "")
        caller = headers.get(INTERNAL_SERVICE_HEADER, "")
        return (
            hmac.compare_digest(authorization.encode("utf-8"), expected.encode("utf-8"))
            and hmac.compare_digest(
                generation.encode("ascii", errors="ignore"),
                self.generation_digest.encode("ascii"),
            )
            and hmac.compare_digest(
                registry.encode("ascii", errors="ignore"),
                self.registry_digest.encode("ascii"),
            )
            and _SERVICE_ID_RE.fullmatch(caller) is not None
        )


def read_internal_service_identity(
    *,
    required: bool,
    expected_service_id: str | None = None,
    actual_registry_digest: str | None = None,
) -> InternalServiceIdentity | None:
    """Consume the inherited credential FD exactly once and close it."""

    raw_fd = os.environ.pop(INTERNAL_CREDENTIAL_FD_ENV, None)
    if raw_fd is None:
        if required:
            raise InternalAuthError("internal credential FD is required")
        return None
    try:
        fd = int(raw_fd, 10)
        if fd < 3:
            raise ValueError("credential FD must not alias a standard stream")
    except ValueError as exc:
        raise InternalAuthError("internal credential FD is invalid") from exc
    try:
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(fd, min(1024, _MAX_CREDENTIAL_PAYLOAD_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > _MAX_CREDENTIAL_PAYLOAD_BYTES:
                raise InternalAuthError("internal credential payload is too large")
    except OSError as exc:
        raise InternalAuthError("internal credential FD could not be read") from exc
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        payload = b"".join(chunks)
        decoded = payload.decode("utf-8")
        raw = json.loads(decoded)
        if not isinstance(raw, dict) or set(raw) != {
            "credential",
            "framework_lock_digest",
            "generation_digest",
            "registry_digest",
            "service_id",
        }:
            raise ValueError("credential payload is not a closed object")
        if any(not isinstance(value, str) for value in raw.values()):
            raise ValueError("credential payload values must be strings")
        identity = InternalServiceIdentity(**raw)
        if identity.inherited_payload() != payload:
            raise ValueError("credential payload is not canonical")
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise InternalAuthError("internal credential payload is invalid") from exc
    if expected_service_id is not None and not hmac.compare_digest(
        identity.service_id,
        expected_service_id,
    ):
        raise InternalAuthError("internal credential targets another service")
    if actual_registry_digest is not None and not hmac.compare_digest(
        identity.registry_digest,
        actual_registry_digest,
    ):
        raise InternalAuthError("loaded registry does not match the service generation")
    return identity


def install_internal_auth(
    app: FastAPI,
    identity_getter: Callable[[], InternalServiceIdentity | None],
    *,
    protected_path: Callable[[str], bool] | None = None,
) -> None:
    """Fail closed for release-owned routes whenever an identity is configured."""

    is_protected = protected_path or (lambda _path: True)

    @app.exception_handler(RunAdmissionError)
    async def run_admission_error_handler(_request: Request, exc: RunAdmissionError):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                    "retryable": exc.retryable,
                }
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.middleware("http")
    async def authenticate_internal_request(request: Request, call_next):
        identity = identity_getter()
        if identity is not None and is_protected(request.url.path):
            if not identity.authenticates(request.headers):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "internal authentication required"},
                    headers={"Cache-Control": "no-store"},
                )
        return await call_next(request)


async def require_generation_bound_run_admission(
    *,
    identity: InternalServiceIdentity | None,
    verifier: GenerationBoundRunAdmissionVerifier | None,
    operation: RunAdmissionOperation,
    payload: Mapping[str, object],
    task_id: str | None,
    session_id: str | None,
) -> None:
    """Authorize an exact release-owned payload without trusting request proof fields."""

    if identity is None:
        return
    if verifier is None:
        raise RunAdmissionError(
            "run_admission_authority_unavailable",
            "Core run admission authority is unavailable for this service generation.",
            status_code=503,
            retryable=True,
        )
    try:
        canonical_payload = json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        check = GenerationBoundRunAdmissionCheck(
            operation=operation,
            generation_digest=identity.generation_digest,
            registry_digest=identity.registry_digest,
            framework_lock_digest=identity.framework_lock_digest,
            payload_sha256=hashlib.sha256(canonical_payload).hexdigest(),
            task_id=task_id,
            session_id=session_id,
        )
    except (TypeError, ValueError) as exc:
        raise RunAdmissionError(
            "run_admission_request_invalid",
            "The release run request cannot be bound to a closed admission identity.",
            status_code=400,
            retryable=False,
        ) from exc
    try:
        await verifier.verify(check)
    except RunAdmissionError:
        raise
    except Exception as exc:
        raise RunAdmissionError(
            "run_admission_verification_failed",
            "Core run admission authority could not verify this request.",
            status_code=503,
            retryable=True,
        ) from exc


def inherited_listen_fd() -> int | None:
    raw = os.environ.get(INTERNAL_LISTEN_FD_ENV)
    if raw is None:
        return None
    try:
        fd = int(raw, 10)
    except ValueError as exc:
        raise InternalAuthError("internal listen FD is invalid") from exc
    if fd < 3:
        raise InternalAuthError("internal listen FD must not alias a standard stream")
    return fd


def health_identity_payload(identity: InternalServiceIdentity | None) -> dict[str, Any]:
    if identity is None:
        return {"internal_identity": None}
    return {"internal_identity": identity.health_identity()}


def verified_private_file_sha256(path: os.PathLike[str], *, max_bytes: int) -> str:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise InternalAuthError("internal identity file could not be opened safely") from exc
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) not in {0o400, 0o600}
            or before.st_size > max_bytes
        ):
            raise InternalAuthError("internal identity file is outside private-file policy")
        payload = os.pread(fd, before.st_size + 1, 0)
        after = os.fstat(fd)
        if (
            len(payload) != before.st_size
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise InternalAuthError("internal identity file changed while hashing")
        return hashlib.sha256(payload).hexdigest()
    finally:
        os.close(fd)


__all__ = [
    "INTERNAL_CREDENTIAL_FD_ENV",
    "INTERNAL_LISTEN_FD_ENV",
    "INTERNAL_OWNERSHIP_ENV",
    "INTERNAL_SERVICE_HEADER",
    "GenerationBoundRunAdmissionCheck",
    "GenerationBoundRunAdmissionVerifier",
    "InternalAuthError",
    "InternalServiceIdentity",
    "RunAdmissionError",
    "RunAdmissionOperation",
    "health_identity_payload",
    "inherited_listen_fd",
    "install_internal_auth",
    "read_internal_service_identity",
    "require_generation_bound_run_admission",
    "verified_private_file_sha256",
]
