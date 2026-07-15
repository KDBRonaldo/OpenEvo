"""Strict active-tunnel client for the Core Control API v1 contract."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from functools import wraps
import hashlib
import json
import math
import queue
import re
import threading
import time
from typing import Any, Literal, NoReturn, ParamSpec, TypeVar
from urllib.parse import quote, unquote, urlsplit
import weakref

import httpx
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from openevo.backend.contracts.v1 import models as v1
from openevo.evolution.framework.contracts import EvolutionExecutionProfile
from openevo.evolution.framework.profiles import execution_profile_for_release_mode


MAX_CORE_ERROR_RESPONSE_BYTES = 64 * 1024
MAX_CORE_JSON_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_CORE_CAPABILITIES_RESPONSE_BYTES = 8 * 1024 * 1024
# Artifact content and diff text are bounded by Core to 2 MiB of UTF-8. JSON can
# expand each input byte to six ASCII bytes (for example, NUL -> ``\u0000``),
# while bounded diff structure contributes additional fixed overhead.
MAX_CORE_ARTIFACT_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_CORE_LOG_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_CORE_REQUEST_BYTES = 2 * 1024 * 1024
MAX_CORE_WORKSPACE_CHUNK_REQUEST_BYTES = ((v1.MAX_WORKSPACE_CHUNK_BYTES + 2) // 3) * 4 + 4096
MAX_CORE_SSE_FRAME_BYTES = 4 * 1024 * 1024
MAX_CORE_SSE_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_CORE_SSE_EVENT_BINDINGS = 10_000
MAX_CORE_CLOSE_WAIT_SECONDS = 5.0
MAX_CORE_CLOSE_QUEUE_SIZE = 256
CORE_CLOSE_WORKER_COUNT = 4
CORE_BLOCKING_IO_WORKER_COUNT = 8
MAX_CORE_REQUEST_DEADLINE_SECONDS = 300.0
CORE_OPENAPI_SHA256 = "006fbe0ad33497329912280d9836bd1dce44f49f26fb018a9d9ba6bdf33b62ed"

_BEARER = re.compile(r"[A-Za-z0-9._~+/\-]{43,510}={0,2}\Z", re.ASCII)
_ETAG = re.compile(r'"[0-9a-f]{64}"\Z', re.ASCII)
_HEADER_VALUE = re.compile(r"[^\x00-\x1f\x7f]{1,256}\Z", re.ASCII)
_OPAQUE_ID = TypeAdapter(v1.OpaqueId)
_CURSOR = TypeAdapter(v1.Cursor)

ResponseT = TypeVar("ResponseT")
MethodP = ParamSpec("MethodP")


class CoreClientLocalErrorCodeV1(StrEnum):
    CONNECTION_FAILED = "core_connection_failed"
    CLIENT_CLOSED = "core_client_closed"
    INVALID_CONNECTION = "invalid_core_tunnel_connection"
    INVALID_REQUEST = "invalid_core_request"
    RESPONSE_TOO_LARGE = "core_response_too_large"
    INVALID_RESPONSE = "invalid_core_response"
    INVALID_ERROR_RESPONSE = "invalid_core_error_response"
    REDIRECT_REJECTED = "core_redirect_rejected"
    ACTIVE_PROJECT_MISMATCH = "active_project_mismatch"
    SSE_PROTOCOL_ERROR = "core_sse_protocol_error"
    SNAPSHOT_REFRESH_REQUIRED = "core_snapshot_refresh_required"


_LOCAL_ERROR_MESSAGES: dict[CoreClientLocalErrorCodeV1, str] = {
    CoreClientLocalErrorCodeV1.CONNECTION_FAILED: "Desktop could not reach the active Core tunnel.",
    CoreClientLocalErrorCodeV1.CLIENT_CLOSED: "The Core client is closed.",
    CoreClientLocalErrorCodeV1.INVALID_CONNECTION: "The active Core tunnel connection is invalid.",
    CoreClientLocalErrorCodeV1.INVALID_REQUEST: "The Core request does not satisfy the v1 contract.",
    CoreClientLocalErrorCodeV1.RESPONSE_TOO_LARGE: "Core returned a response above the allowed limit.",
    CoreClientLocalErrorCodeV1.INVALID_RESPONSE: "Core returned an invalid v1 response.",
    CoreClientLocalErrorCodeV1.INVALID_ERROR_RESPONSE: "Core returned an invalid v1 error.",
    CoreClientLocalErrorCodeV1.REDIRECT_REJECTED: "Core redirects are not allowed.",
    CoreClientLocalErrorCodeV1.ACTIVE_PROJECT_MISMATCH: (
        "The Core resource does not belong to the active project."
    ),
    CoreClientLocalErrorCodeV1.SSE_PROTOCOL_ERROR: "Core returned an invalid v1 event stream.",
    CoreClientLocalErrorCodeV1.SNAPSHOT_REFRESH_REQUIRED: (
        "Core event membership is unknown; reload snapshots before resuming events."
    ),
}


class CoreClientLocalErrorV1(BaseModel):
    """Closed, user-safe errors produced locally by the transport boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"] = "1"
    code: CoreClientLocalErrorCodeV1
    message: str
    retryable: bool


class CoreClientErrorV1(RuntimeError):
    """Raised with either the exact Core ApiErrorV1 or a closed local error."""

    def __init__(
        self,
        status_code: int,
        error: v1.ApiErrorV1 | CoreClientLocalErrorV1,
    ) -> None:
        super().__init__("OpenEvo Core request failed.")
        self.status_code = status_code
        self.error = error


@dataclass(frozen=True, slots=True)
class CoreTunnelConnectionV1:
    """One active project session's private loopback tunnel authority."""

    endpoint: str
    bearer_token: str = field(repr=False)
    project_id: str
    session_id: str
    origin: str = field(init=False)

    def __post_init__(self) -> None:
        invalid_url = False
        try:
            parsed = urlsplit(self.endpoint)
            port = parsed.port
        except (TypeError, ValueError):
            invalid_url = True
            parsed = urlsplit("http://127.0.0.1")
            port = None
        if invalid_url:
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_CONNECTION, 400)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or port is None
            or not 1 <= port <= 65535
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_CONNECTION, 400)
        if not isinstance(self.bearer_token, str) or not _BEARER.fullmatch(self.bearer_token):
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_CONNECTION, 400)
        if len(set(self.bearer_token.rstrip("="))) < 8:
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_CONNECTION, 400)
        invalid_identity = False
        try:
            project_id = _OPAQUE_ID.validate_python(self.project_id, strict=True)
            session_id = _OPAQUE_ID.validate_python(self.session_id, strict=True)
        except ValidationError:
            invalid_identity = True
            project_id = ""
            session_id = ""
        if invalid_identity:
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_CONNECTION, 400)
        host = f"[{parsed.hostname}]" if parsed.hostname == "::1" else parsed.hostname
        origin = f"http://{host}:{port}"
        _scan_private_strings(
            project_id,
            (self.bearer_token, origin, self.endpoint, session_id),
            CoreClientLocalErrorCodeV1.INVALID_CONNECTION,
            400,
        )
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "origin", origin)


@dataclass(frozen=True, slots=True)
class CoreBootstrapTunnelConnectionV1:
    """Private tunnel authority before Core has issued the project identity."""

    endpoint: str
    bearer_token: str = field(repr=False)
    session_id: str
    origin: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint, str) or not isinstance(self.session_id, str):
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_CONNECTION, 400)
        validated = self._temporary_binding()
        object.__setattr__(self, "endpoint", validated.endpoint)
        object.__setattr__(self, "bearer_token", validated.bearer_token)
        object.__setattr__(self, "session_id", validated.session_id)
        object.__setattr__(self, "origin", validated.origin)

    def bind(self, project_id: str) -> CoreTunnelConnectionV1:
        """Issue the ordinary project-bound authority after a verified create response."""

        return CoreTunnelConnectionV1(
            endpoint=self.endpoint,
            bearer_token=self.bearer_token,
            project_id=project_id,
            session_id=self.session_id,
        )

    def _temporary_binding(self) -> CoreTunnelConnectionV1:
        try:
            seed = f"openevo-core-bootstrap-v1\0{self.endpoint}\0{self.session_id}".encode("utf-8")
        except UnicodeEncodeError:
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_CONNECTION, 400)
        digest = hashlib.sha256(seed).hexdigest()
        return CoreTunnelConnectionV1(
            endpoint=self.endpoint,
            bearer_token=self.bearer_token,
            project_id=f"project-bootstrap-{digest[:32]}",
            session_id=self.session_id,
        )


@dataclass(frozen=True, slots=True)
class CoreProjectBootstrapResultV1:
    """A Core-created project and the exact authority for its bound client."""

    project: v1.ProjectV1
    connection: CoreTunnelConnectionV1

    def __post_init__(self) -> None:
        if not isinstance(self.project, v1.ProjectV1):
            raise TypeError("project must be ProjectV1")
        if not isinstance(self.connection, CoreTunnelConnectionV1):
            raise TypeError("connection must be CoreTunnelConnectionV1")
        if self.connection.project_id != self.project.id:
            raise ValueError("bootstrap connection must bind the created project")


class CoreSseStreamV1(Iterator[v1.SseFrameV1]):
    """Single-pass, bounded SSE adapter yielding validated contract envelopes."""

    def __init__(
        self,
        chunks: Iterator[bytes],
        *,
        linearize_frame_delivery: Callable[[v1.SseFrameV1], None],
        private_values: tuple[str, ...],
        declared_length: int | None,
        close_started: Callable[[], bool],
        session_guard: Callable[[], None],
        delivery_lease: Callable[[], AbstractContextManager[None]],
        deadline: float,
    ) -> None:
        self._chunks = chunks
        self._linearize_frame_delivery = linearize_frame_delivery
        self._private_values = private_values
        self._declared_length = declared_length
        self._close_started = close_started
        self._session_guard = session_guard
        self._delivery_lease = delivery_lease
        self._deadline = deadline
        self._frames = _iter_sse_frames(
            self._chunks,
            declared_length=self._declared_length,
            deadline=self._deadline,
        )

    def __iter__(self) -> CoreSseStreamV1:
        return self

    def __next__(self) -> v1.SseFrameV1:
        boundary_error = False
        try:
            _check_deadline(self._deadline)
            self._session_guard()
            frame = next(self._frames)
            with self._delivery_lease():
                _check_deadline(self._deadline)
                self._session_guard()
                validated_frame = _validate_sse_frame(frame, self._private_values)
                self._session_guard()
                self._linearize_frame_delivery(validated_frame)
                _check_deadline(self._deadline)
                self._session_guard()
                return validated_frame
        except StopIteration:
            raise
        except CoreClientErrorV1:
            raise
        except (
            httpx.HTTPError,
            OSError,
            TypeError,
            UnicodeError,
            RuntimeError,
            ValueError,
            ValidationError,
        ):
            boundary_error = True
        if boundary_error:
            closed = self._close_started()
            code = (
                CoreClientLocalErrorCodeV1.CLIENT_CLOSED
                if closed
                else CoreClientLocalErrorCodeV1.SSE_PROTOCOL_ERROR
            )
            _raise_local(code, 503 if closed else 502)


@dataclass(frozen=True, slots=True)
class _ResourceBinding:
    project_id: str
    parent_type: v1.ResourceChangeType | None = None
    parent_id: str | None = None


@dataclass(frozen=True, slots=True)
class _LogRefBinding:
    project_id: str
    parent_type: v1.ResourceChangeType
    parent_id: str


@dataclass(frozen=True, slots=True)
class _CapabilityAuthority:
    execution_mode: v1.ExecutionMode
    registry_digest: str
    evaluated_profile: EvolutionExecutionProfile


@dataclass(slots=True)
class _GenerationLeaseToken:
    generation: int
    deadline: float
    owner: int
    released: bool = False


def _generation_bound(method: Callable[MethodP, ResponseT]) -> Callable[MethodP, ResponseT]:
    @wraps(method)
    def wrapped(*args: MethodP.args, **kwargs: MethodP.kwargs) -> ResponseT:
        client = args[0]
        if not isinstance(client, CoreControlClientV1):
            raise TypeError("generation-bound method requires CoreControlClientV1")
        with client._generation_lease() as token:
            with client._registration_batch(delivery_token=token):
                result = method(*args, **kwargs)
                client._linearize_generation_result(token.generation, token.deadline)
                return result

    return wrapped


class _CloseReservation:
    """One globally bounded ownership slot for exactly one close action."""

    def __init__(self, closer: _BoundedResourceCloser) -> None:
        self._closer = closer
        self._lock = threading.Lock()
        self._consumed = False

    def submit(self, action: Callable[[], None]) -> bool:
        with self._lock:
            if self._consumed:
                return False
            if not self._closer._submit_reserved(action):
                return False
            self._consumed = True
            return True

    def release(self) -> bool:
        with self._lock:
            if self._consumed:
                return False
            self._consumed = True
        self._closer._release_reserved()
        return True


class _BoundedResourceCloser:
    """Process-shared fixed-size executor for close calls that may block indefinitely."""

    def __init__(
        self,
        *,
        worker_count: int = CORE_CLOSE_WORKER_COUNT,
        capacity: int = MAX_CORE_CLOSE_QUEUE_SIZE,
    ) -> None:
        if worker_count <= 0 or capacity <= 0:
            raise ValueError("closer worker count and capacity must be positive")
        self._queue: queue.Queue[Callable[[], None]] = queue.Queue(maxsize=capacity)
        self._capacity = capacity
        self._lock = threading.Lock()
        self._owned = 0
        self._workers = tuple(
            threading.Thread(
                target=self._worker,
                name=f"openevo-core-resource-closer-{index + 1}",
                daemon=True,
            )
            for index in range(worker_count)
        )
        for worker in self._workers:
            worker.start()

    @property
    def owned_count(self) -> int:
        with self._lock:
            return self._owned

    def reserve(self) -> _CloseReservation | None:
        with self._lock:
            if self._owned >= self._capacity:
                return None
            self._owned += 1
        return _CloseReservation(self)

    def submit(self, action: Callable[[], None]) -> bool:
        reservation = self.reserve()
        return reservation is not None and reservation.submit(action)

    def _submit_reserved(self, action: Callable[[], None]) -> bool:
        def owned_action() -> None:
            try:
                action()
            finally:
                self._release_reserved()

        with self._lock:
            try:
                self._queue.put_nowait(owned_action)
            except queue.Full:
                return False
        return True

    def _release_reserved(self) -> None:
        with self._lock:
            if self._owned <= 0:
                raise RuntimeError("closer reservation accounting underflow")
            self._owned -= 1

    def _worker(self) -> None:
        while True:
            action = self._queue.get()
            try:
                action()
            except BaseException:
                pass
            finally:
                self._queue.task_done()


_PROCESS_RESOURCE_CLOSER = _BoundedResourceCloser()


def _finalize_reserved_close(
    close_action: Callable[[], None],
    reservation: _CloseReservation,
) -> None:
    if not reservation.submit(close_action):
        reservation.release()


class _BoundedBlockingIoExecutor:
    """Process-shared fixed thread budget for synchronous transport calls."""

    def __init__(self) -> None:
        self._queue: queue.Queue[tuple[Future[Any], Callable[[], Any]]] = queue.Queue(
            maxsize=MAX_CORE_CLOSE_QUEUE_SIZE
        )
        self._workers = tuple(
            threading.Thread(
                target=self._worker,
                name=f"openevo-core-blocking-io-{index + 1}",
                daemon=True,
            )
            for index in range(CORE_BLOCKING_IO_WORKER_COUNT)
        )
        for worker in self._workers:
            worker.start()

    def submit(self, action: Callable[[], ResponseT]) -> Future[ResponseT] | None:
        future: Future[ResponseT] = Future()
        try:
            self._queue.put_nowait((future, action))
        except queue.Full:
            return None
        return future

    def _worker(self) -> None:
        while True:
            future, action = self._queue.get()
            try:
                if future.set_running_or_notify_cancel():
                    try:
                        future.set_result(action())
                    except BaseException as exc:
                        future.set_exception(exc)
            finally:
                self._queue.task_done()


_PROCESS_BLOCKING_IO = _BoundedBlockingIoExecutor()


class CoreControlClientV1:
    """Thread-safe strict client bound to one active project's SSH tunnel."""

    def __init__(
        self,
        connection: CoreTunnelConnectionV1,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float | httpx.Timeout = 30.0,
    ) -> None:
        if not isinstance(connection, CoreTunnelConnectionV1):
            raise TypeError("connection must be CoreTunnelConnectionV1")
        request_deadline_seconds = _finite_request_deadline(timeout)
        self._connection = connection
        self._state = threading.Condition(threading.RLock())
        self._delivery_lock = threading.RLock()
        self._membership_lock = threading.RLock()
        self._closing = False
        self._closed = False
        self._session_generation = 0
        self._generation_local = threading.local()
        self._close_tasks_pending = 0
        self._close_failed = False
        self._retained_close_actions: list[tuple[Callable[[], None], _CloseReservation]] = []
        self._leases = 0
        self._lease_owners: dict[int, int] = {}
        self._active_responses: dict[httpx.Response, _CloseReservation] = {}
        self._members: dict[tuple[v1.ResourceChangeType, str], _ResourceBinding] = {}
        self._log_refs: dict[str, _LogRefBinding] = {}
        self._workspace_uploads: dict[str, v1.WorkspaceUploadSessionV1] = {}
        self._workspace_etag_representations: dict[tuple[str, str], str] = {}
        self._workspace_representation_etags: dict[tuple[str, str], str] = {}
        self._project_state: v1.ProjectSummaryV1 | None = None
        self._runs: dict[str, v1.RunSummaryV1] = {}
        self._services: dict[str, v1.ServiceSummaryV1] = {}
        self._artifacts: dict[str, v1.ArtifactSummaryBaseV1] = {}
        self._operations: dict[str, v1.OperationV1] = {}
        self._diagnostics: dict[str, v1.DiagnosticV1] = {}
        self._sse_event_digests: dict[str, str] = {}
        self._capability_authority: _CapabilityAuthority | None = None
        self._version_authority: v1.VersionResponseV1 | None = None
        self._request_deadline_seconds = request_deadline_seconds
        transport_close_reservation = _PROCESS_RESOURCE_CLOSER.reserve()
        if transport_close_reservation is None:
            _raise_local(CoreClientLocalErrorCodeV1.CONNECTION_FAILED, 503)
        try:
            self._http = httpx.Client(
                base_url=f"{connection.origin}/",
                transport=transport,
                timeout=timeout,
                trust_env=False,
                follow_redirects=False,
                headers={
                    "Accept-Encoding": "identity",
                    "User-Agent": "OpenEvo-Desktop-CoreClient/1",
                },
            )
        except BaseException:
            transport_close_reservation.release()
            raise
        self._transport_close_reservation = transport_close_reservation
        self._transport_finalizer = weakref.finalize(
            self,
            _finalize_reserved_close,
            self._http.close,
            transport_close_reservation,
        )
        self._bind_resource(v1.ResourceChangeType.PROJECT, connection.project_id)

    def __enter__(self) -> CoreControlClientV1:
        self._ensure_open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Idempotently stop admission, cancel transports, and boundedly drain leases."""
        self._retry_retained_closes()
        deadline = time.monotonic() + MAX_CORE_CLOSE_WAIT_SECONDS
        close_actions: tuple[tuple[Callable[[], None], _CloseReservation], ...] = ()
        with self._delivery_lock:
            with self._state:
                if self._closed:
                    close_failed = self._close_failed
                    if close_failed:
                        raise _local_exception(
                            CoreClientLocalErrorCodeV1.CLIENT_CLOSED, 503
                        ) from None
                    return
                if self._closing:
                    while not self._closed:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            return
                        self._state.wait(remaining)
                    if self._close_failed:
                        raise _local_exception(
                            CoreClientLocalErrorCodeV1.CLIENT_CLOSED, 503
                        ) from None
                    return
                self._closing = True
                self._session_generation += 1
                self._transport_finalizer.detach()
                close_actions = tuple(
                    (response.close, reservation)
                    for response, reservation in self._active_responses.items()
                ) + ((self._http.close, self._transport_close_reservation),)
                self._active_responses.clear()

        for close_action, reservation in close_actions:
            self._schedule_close(close_action, reservation)

        with self._state:
            while self._close_tasks_pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._state.wait(remaining)
            self._closed = True
            self._active_responses.clear()
            self._state.notify_all()
            close_failed = self._close_failed
        if close_failed:
            raise _local_exception(CoreClientLocalErrorCodeV1.CLIENT_CLOSED, 503) from None

    def _retry_retained_closes(self) -> None:
        with self._state:
            retained = tuple(self._retained_close_actions)
            self._retained_close_actions.clear()
        for close_action, reservation in retained:
            self._schedule_close(close_action, reservation)

    def _schedule_close(
        self,
        close_action: Callable[[], None],
        reservation: _CloseReservation,
    ) -> bool:
        with self._state:
            self._close_tasks_pending += 1

        def tracked_close() -> None:
            failed = False
            try:
                close_action()
            except BaseException:
                failed = True
            finally:
                with self._state:
                    if failed:
                        self._close_failed = True
                    self._close_tasks_pending -= 1
                    self._state.notify_all()

        if not reservation.submit(tracked_close):
            with self._state:
                self._close_failed = True
                self._close_tasks_pending -= 1
                self._retained_close_actions.append((close_action, reservation))
                self._state.notify_all()
            return False
        return True

    @_generation_bound
    def version(self) -> v1.VersionResponseV1:
        result = self._json("GET", "/version", v1.VersionResponseV1, authenticated=False)
        if (
            result.openapi_sha256 != CORE_OPENAPI_SHA256
            or result.provider_kind is not v1.ProviderKind.OPENEVO_CORE
            or result.build_channel is not v1.BuildChannel.RELEASE
            or result.preferred_major != 1
            or 1 not in result.supported_majors
        ):
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
        with self._membership_lock:
            if self._version_authority is not None and self._version_authority != result:
                _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
            self._version_authority = result
        return result

    @_generation_bound
    def health(self) -> v1.HealthResponseV1:
        return self._json("GET", "/health", v1.HealthResponseV1, authenticated=False)

    @_generation_bound
    def status(self) -> v1.CoreStatusV1:
        result = self._json("GET", "/v1/status", v1.CoreStatusV1)
        with self._registration_batch():
            for service in result.services:
                self._register_service(service)
        return result

    @_generation_bound
    def environment_doctor(
        self,
        request: v1.EnvironmentDoctorRequestV1,
        *,
        idempotency_key: str,
    ) -> v1.EnvironmentDoctorResponseV1:
        return self._mutation(
            "POST",
            "/v1/environment/doctor",
            request,
            v1.EnvironmentDoctorRequestV1,
            v1.EnvironmentDoctorResponseV1,
            idempotency_key=idempotency_key,
        )

    @_generation_bound
    def environment_repair(
        self,
        request: v1.EnvironmentRepairRequestV1,
        *,
        idempotency_key: str,
    ) -> v1.OperationV1:
        result = self._mutation(
            "POST",
            "/v1/environment/repair",
            request,
            v1.EnvironmentRepairRequestV1,
            v1.OperationV1,
            idempotency_key=idempotency_key,
            expected_status=202,
        )
        if not isinstance(result.request, v1.EnvironmentRepairOperationRequestV1) or (
            result.request.request != request
        ):
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
        self._register_operation(result)
        return result

    @_generation_bound
    def capabilities(self, execution_mode: v1.ExecutionMode) -> v1.CapabilitiesResponseV1:
        mode = _enum_query(execution_mode, v1.ExecutionMode)
        result = self._json(
            "GET",
            "/v1/capabilities",
            v1.CapabilitiesResponseV1,
            params={"execution_mode": mode},
            max_response_bytes=MAX_CORE_CAPABILITIES_RESPONSE_BYTES,
        )
        expected_profile = execution_profile_for_release_mode(execution_mode)
        if result.evaluated_profile != expected_profile:
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
        authority = _CapabilityAuthority(
            execution_mode=execution_mode,
            registry_digest=result.registry_digest,
            evaluated_profile=result.evaluated_profile,
        )
        with self._membership_lock:
            if self._project_state is not None and (
                self._project_state.execution_mode is not execution_mode
                or self._project_state.registry_digest != result.registry_digest
            ):
                _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
            if self._capability_authority is not None and self._capability_authority != authority:
                _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
            self._capability_authority = authority
        return result

    @_generation_bound
    def list_projects(
        self,
        *,
        limit: int = 50,
        after: str | None = None,
        sort: Literal["created_at", "updated_at", "name"] = "updated_at",
        direction: Literal["asc", "desc"] = "desc",
    ) -> v1.ProjectPageV1:
        result = self._json(
            "GET",
            "/v1/projects",
            v1.ProjectPageV1,
            params=_page_query(
                limit, after, sort, direction, {"created_at", "updated_at", "name"}
            ),
        )
        with self._registration_batch():
            for project in result.items:
                self._register_project(project)
        return result

    @_generation_bound
    def create_project(
        self,
        request: v1.ProjectCreateV1,
        *,
        idempotency_key: str,
    ) -> v1.ProjectV1:
        del request, idempotency_key
        # Core owns new project IDs. A project-bound client cannot predict that
        # identity and must never create an orphan before rejecting the response.
        _raise_local(CoreClientLocalErrorCodeV1.INVALID_REQUEST, 400)

    @_generation_bound
    def get_project(self, project_id: str | None = None) -> v1.ProjectV1:
        project_id = self._active_project(project_id)
        result = self._json("GET", f"/v1/projects/{_segment(project_id)}", v1.ProjectV1)
        self._register_project(result, expected_id=project_id)
        return result

    @_generation_bound
    def patch_project(
        self,
        request: v1.ProjectPatchV1,
        *,
        if_match: str,
        idempotency_key: str,
        project_id: str | None = None,
    ) -> v1.ProjectV1:
        project_id = self._active_project(project_id)
        result = self._mutation(
            "PATCH",
            f"/v1/projects/{_segment(project_id)}",
            request,
            v1.ProjectPatchV1,
            v1.ProjectV1,
            if_match=if_match,
            idempotency_key=idempotency_key,
        )
        self._register_project(result, expected_id=project_id)
        return result

    @_generation_bound
    def delete_project(
        self,
        *,
        if_match: str,
        idempotency_key: str,
        project_id: str | None = None,
    ) -> None:
        project_id = self._active_project(project_id)
        self._no_content_mutation(
            "DELETE",
            f"/v1/projects/{_segment(project_id)}",
            if_match=if_match,
            idempotency_key=idempotency_key,
        )

    @_generation_bound
    def list_revisions(
        self,
        *,
        limit: int = 50,
        after: str | None = None,
        sort: Literal["generation", "created_at", "updated_at"] = "generation",
        direction: Literal["asc", "desc"] = "desc",
        project_id: str | None = None,
    ) -> v1.RevisionPageV1:
        project_id = self._active_project(project_id)
        result = self._json(
            "GET",
            f"/v1/projects/{_segment(project_id)}/revisions",
            v1.RevisionPageV1,
            params=_page_query(
                limit,
                after,
                sort,
                direction,
                {"generation", "created_at", "updated_at"},
            ),
        )
        for revision in result.items:
            self._ensure_active_project(revision.revision.project_id)
        return result

    @_generation_bound
    def revision_head(self, project_id: str | None = None) -> v1.RevisionHeadV1:
        project_id = self._active_project(project_id)
        result = self._json(
            "GET", f"/v1/projects/{_segment(project_id)}/revisions/head", v1.RevisionHeadV1
        )
        self._ensure_active_project(result.project_id)
        if result.project_id != project_id:
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
        return result

    @_generation_bound
    def get_revision(self, revision_id: str, *, project_id: str) -> v1.RevisionV1:
        self._ensure_active_project(project_id)
        result = self._json("GET", f"/v1/revisions/{_segment(revision_id)}", v1.RevisionV1)
        self._ensure_active_project(result.revision.project_id)
        if result.revision.id != revision_id or result.revision.project_id != project_id:
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
        return result

    @_generation_bound
    def create_workspace_upload(
        self,
        request: v1.WorkspaceUploadCreateV1,
        *,
        if_match: str,
        idempotency_key: str,
        project_id: str | None = None,
    ) -> v1.WorkspaceUploadSessionV1:
        project_id = self._active_project(project_id)
        if_match = _etag(if_match)
        with self._membership_lock:
            project = self._project_state
        if (
            project is None
            or request.project_snapshot != project.current_project_snapshot
            or if_match != project.etag
        ):
            _raise_local(CoreClientLocalErrorCodeV1.ACTIVE_PROJECT_MISMATCH, 409)
        result = self._mutation(
            "POST",
            f"/v1/projects/{_segment(project_id)}/workspace-uploads",
            request,
            v1.WorkspaceUploadCreateV1,
            v1.WorkspaceUploadSessionV1,
            if_match=if_match,
            idempotency_key=idempotency_key,
            expected_status=201,
        )
        self._validate_new_workspace_upload(result, request, project_id, if_match)
        self._register_workspace_upload(
            result,
            create_project_etag=project.etag,
            exact_replay=True,
        )
        return result

    @_generation_bound
    def get_workspace_upload(
        self,
        upload_id: str,
        *,
        project_id: str | None = None,
    ) -> v1.WorkspaceUploadSessionV1:
        project_id = self._active_project(project_id)
        result = self._json(
            "GET",
            f"/v1/projects/{_segment(project_id)}/workspace-uploads/{_segment(upload_id)}",
            v1.WorkspaceUploadSessionV1,
        )
        self._validate_workspace_upload_identity(result, upload_id, project_id)
        self._register_workspace_upload(result)
        return result

    @_generation_bound
    def put_workspace_upload_chunk(
        self,
        upload_id: str,
        request: v1.WorkspaceUploadChunkV1,
        *,
        if_match: str,
        idempotency_key: str,
        project_id: str | None = None,
    ) -> v1.WorkspaceUploadSessionV1:
        project_id = self._active_project(project_id)
        if_match = _etag(if_match)
        upload = self._require_workspace_upload(upload_id, project_id)
        decoded_length = _decoded_chunk_length(request)
        if (
            upload.status is not v1.WorkspaceUploadStatus.OPEN
            or request.offset != upload.accepted_offset
            or request.offset + decoded_length > upload.archive.byte_size
            or if_match != upload.etag
        ):
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_REQUEST, 400)
        result = self._mutation(
            "PUT",
            f"/v1/projects/{_segment(project_id)}/workspace-uploads/{_segment(upload_id)}/chunk",
            request,
            v1.WorkspaceUploadChunkV1,
            v1.WorkspaceUploadSessionV1,
            if_match=if_match,
            idempotency_key=idempotency_key,
            max_request_bytes=MAX_CORE_WORKSPACE_CHUNK_REQUEST_BYTES,
        )
        self._validate_workspace_upload_identity(result, upload_id, project_id)
        _ensure_upload_stable(upload, result)
        if (
            result.status is not v1.WorkspaceUploadStatus.OPEN
            or result.publication is not None
            or result.accepted_offset != request.offset + decoded_length
        ):
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
        self._register_workspace_upload(result, expected_previous=upload)
        return result

    @_generation_bound
    def finalize_workspace_upload(
        self,
        upload_id: str,
        request: v1.WorkspaceUploadFinalizeV1,
        *,
        if_match: str,
        if_project_match: str,
        idempotency_key: str,
        project_id: str | None = None,
    ) -> v1.WorkspaceUploadFinalizeResponseV1:
        project_id = self._active_project(project_id)
        if_match = _etag(if_match)
        if_project_match = _etag(if_project_match)
        upload = self._require_workspace_upload(upload_id, project_id)
        if (
            upload.status is not v1.WorkspaceUploadStatus.OPEN
            or upload.accepted_offset != upload.archive.byte_size
            or request.content_sha256 != upload.archive.content_sha256
            or if_match != upload.etag
            or if_project_match != upload.project_etag
        ):
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_REQUEST, 400)
        result = self._mutation(
            "POST",
            f"/v1/projects/{_segment(project_id)}/workspace-uploads/{_segment(upload_id)}/finalize",
            request,
            v1.WorkspaceUploadFinalizeV1,
            v1.WorkspaceUploadFinalizeResponseV1,
            if_match=if_match,
            if_project_match=if_project_match,
            idempotency_key=idempotency_key,
            expected_status=201,
        )
        self._ensure_active_project(result.project_id)
        self._validate_workspace_upload_identity(result.upload, upload_id, project_id)
        _ensure_upload_stable(upload, result.upload)
        if (
            result.project_id != project_id
            or result.upload.status is not v1.WorkspaceUploadStatus.FINALIZED
            or result.upload.accepted_offset != upload.archive.byte_size
            or result.upload.publication != result.publication
            or result.publication.archive != upload.archive
            or result.publication.content_ref.sha256 != request.content_sha256
            or result.project.id != project_id
            or result.project.current_project_snapshot == upload.project_snapshot
            or result.project.current_workspace_snapshot != result.publication.workspace_snapshot
        ):
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
        with self._registration_batch():
            self._register_workspace_upload(result.upload, expected_previous=upload)
            self._register_project(result.project, expected_id=project_id)
        return result

    @_generation_bound
    def abort_workspace_upload(
        self,
        upload_id: str,
        request: v1.WorkspaceUploadAbortV1,
        *,
        if_match: str,
        idempotency_key: str,
        project_id: str | None = None,
    ) -> v1.WorkspaceUploadSessionV1:
        project_id = self._active_project(project_id)
        if_match = _etag(if_match)
        upload = self._require_workspace_upload(upload_id, project_id)
        if upload.status is not v1.WorkspaceUploadStatus.OPEN or if_match != upload.etag:
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_REQUEST, 400)
        return self._abort_workspace_upload_mutation(
            upload,
            request,
            if_match=if_match,
            idempotency_key=idempotency_key,
            project_id=project_id,
        )

    @_generation_bound
    def abort_persisted_workspace_upload(
        self,
        upload: v1.WorkspaceUploadSessionV1,
        request: v1.WorkspaceUploadAbortV1,
        *,
        if_match: str,
        idempotency_key: str,
        project_id: str | None = None,
    ) -> v1.WorkspaceUploadSessionV1:
        """Restore and abort one exact durable open-upload representation."""

        project_id = self._active_project(project_id)
        if_match = _etag(if_match)
        self._validate_workspace_upload_identity(upload, upload.id, project_id)
        if (
            upload.status is not v1.WorkspaceUploadStatus.OPEN
            or upload.publication is not None
            or if_match != upload.etag
        ):
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_REQUEST, 400)
        # Validate all caller-supplied mutation authority before introducing the
        # persisted representation into this generation's copy-on-write cache.
        self._mutation_headers(idempotency_key=idempotency_key, if_match=if_match)
        self._register_workspace_upload(upload, exact_replay=True)
        return self._abort_workspace_upload_mutation(
            upload,
            request,
            if_match=if_match,
            idempotency_key=idempotency_key,
            project_id=project_id,
        )

    def _abort_workspace_upload_mutation(
        self,
        upload: v1.WorkspaceUploadSessionV1,
        request: v1.WorkspaceUploadAbortV1,
        *,
        if_match: str,
        idempotency_key: str,
        project_id: str,
    ) -> v1.WorkspaceUploadSessionV1:
        result = self._mutation(
            "POST",
            f"/v1/projects/{_segment(project_id)}/workspace-uploads/{_segment(upload.id)}/abort",
            request,
            v1.WorkspaceUploadAbortV1,
            v1.WorkspaceUploadSessionV1,
            if_match=if_match,
            idempotency_key=idempotency_key,
        )
        self._validate_workspace_upload_identity(result, upload.id, project_id)
        _ensure_upload_stable(upload, result)
        if (
            result.status is not v1.WorkspaceUploadStatus.ABORTED
            or result.accepted_offset != upload.accepted_offset
            or result.publication is not None
        ):
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
        self._register_workspace_upload(result, expected_previous=upload)
        return result

    @_generation_bound
    def validate_project(
        self,
        request: v1.ProjectValidationRequestV1,
        *,
        idempotency_key: str,
        project_id: str | None = None,
    ) -> v1.ProjectValidationResponseV1:
        project_id = self._active_project(project_id)
        authority = self._require_capability_authority(
            request.expected_registry_digest,
            request_error=True,
        )
        result = self._mutation(
            "POST",
            f"/v1/projects/{_segment(project_id)}/validate",
            request,
            v1.ProjectValidationRequestV1,
            v1.ProjectValidationResponseV1,
            idempotency_key=idempotency_key,
        )
        if result.registry_digest != authority.registry_digest:
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
        return result

    @_generation_bound
    def list_runs(
        self,
        *,
        limit: int = 50,
        after: str | None = None,
        sort: Literal["created_at", "started_at", "finished_at"] = "created_at",
        direction: Literal["asc", "desc"] = "desc",
        status: v1.RunStatus | None = None,
    ) -> v1.RunPageV1:
        params = _page_query(
            limit,
            after,
            sort,
            direction,
            {"created_at", "started_at", "finished_at"},
        )
        params["project_id"] = self._connection.project_id
        if status is not None:
            params["status"] = _enum_query(status, v1.RunStatus)
        result = self._json("GET", "/v1/runs", v1.RunPageV1, params=params)
        with self._registration_batch():
            for run in result.items:
                self._register_run(run)
        return result

    @_generation_bound
    def create_run(
        self,
        request: v1.RunCreateV1,
        *,
        idempotency_key: str,
    ) -> v1.RunV1:
        self._ensure_active_project(request.project_id)
        authority = self._require_capability_authority(
            request.expected_registry_digest,
            request_error=True,
        )
        result = self._mutation(
            "POST",
            "/v1/runs",
            request,
            v1.RunCreateV1,
            v1.RunV1,
            idempotency_key=idempotency_key,
            expected_status=202,
        )
        if (
            result.project_snapshot != request.project_snapshot
            or result.task_snapshot != request.task_snapshot
            or result.workspace_snapshot != request.workspace_snapshot
            or result.registry_digest != authority.registry_digest
            or result.execution_mode is not authority.execution_mode
            or result.capture_mode.value != authority.evaluated_profile.capture_mode.value
            or result.required_revision != request.required_revision
        ):
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
        self._register_run(result)
        return result

    @_generation_bound
    def get_run(self, run_id: str, *, project_id: str) -> v1.RunV1:
        self._ensure_active_project(project_id)
        result = self._json("GET", f"/v1/runs/{_segment(run_id)}", v1.RunV1)
        self._register_run(result, expected_id=run_id)
        return result

    @_generation_bound
    def delete_run(
        self,
        run_id: str,
        *,
        project_id: str,
        if_match: str,
        idempotency_key: str,
    ) -> None:
        self._ensure_active_project(project_id)
        self._require_member(v1.ResourceChangeType.RUN, run_id)
        self._no_content_mutation(
            "DELETE",
            f"/v1/runs/{_segment(run_id)}",
            if_match=if_match,
            idempotency_key=idempotency_key,
        )

    @_generation_bound
    def cancel_run(
        self,
        run_id: str,
        request: v1.RunCancelRequestV1,
        *,
        project_id: str,
        if_match: str,
        idempotency_key: str,
    ) -> v1.RunV1:
        self._ensure_active_project(project_id)
        self._require_member(v1.ResourceChangeType.RUN, run_id)
        result = self._mutation(
            "POST",
            f"/v1/runs/{_segment(run_id)}/cancel",
            request,
            v1.RunCancelRequestV1,
            v1.RunV1,
            if_match=if_match,
            idempotency_key=idempotency_key,
            expected_status=202,
        )
        self._register_run(result, expected_id=run_id)
        return result

    @_generation_bound
    def retry_run(
        self,
        run_id: str,
        request: v1.RunRetryRequestV1,
        *,
        project_id: str,
        if_match: str,
        idempotency_key: str,
    ) -> v1.RunV1:
        self._ensure_active_project(project_id)
        self._require_member(v1.ResourceChangeType.RUN, run_id)
        result = self._mutation(
            "POST",
            f"/v1/runs/{_segment(run_id)}/retry",
            request,
            v1.RunRetryRequestV1,
            v1.RunV1,
            if_match=if_match,
            idempotency_key=idempotency_key,
            expected_status=202,
        )
        self._register_run(result, expected_id=run_id)
        return result

    @_generation_bound
    def run_timeline(
        self,
        run_id: str,
        *,
        project_id: str,
        limit: int = 50,
        after: str | None = None,
        sort: Literal["sequence", "occurred_at"] = "sequence",
        direction: Literal["asc", "desc"] = "asc",
    ) -> v1.RunTimelinePageV1:
        self._ensure_active_project(project_id)
        self._require_member(v1.ResourceChangeType.RUN, run_id)
        result = self._json(
            "GET",
            f"/v1/runs/{_segment(run_id)}/timeline",
            v1.RunTimelinePageV1,
            params=_page_query(limit, after, sort, direction, {"sequence", "occurred_at"}),
        )
        for entry in result.items:
            if entry.run_id != run_id:
                _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
        return result

    @_generation_bound
    def run_logs(
        self,
        run_id: str,
        *,
        project_id: str,
        limit: int = 100,
        after: str | None = None,
        sort: Literal["sequence", "occurred_at"] = "sequence",
        direction: Literal["asc", "desc"] = "asc",
        stream: v1.LogStream | None = None,
    ) -> v1.LogPageV1:
        self._ensure_active_project(project_id)
        self._require_member(v1.ResourceChangeType.RUN, run_id)
        params = _page_query(limit, after, sort, direction, {"sequence", "occurred_at"})
        if stream is not None:
            params["stream"] = _enum_query(stream, v1.LogStream)
        result = self._json(
            "GET",
            f"/v1/runs/{_segment(run_id)}/logs",
            v1.LogPageV1,
            params=params,
            max_response_bytes=MAX_CORE_LOG_RESPONSE_BYTES,
        )
        for entry in result.items:
            if entry.run_id != run_id:
                _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
        return result

    @_generation_bound
    def run_context(self, run_id: str, *, project_id: str) -> v1.RunContextV1:
        self._ensure_active_project(project_id)
        self._require_member(v1.ResourceChangeType.RUN, run_id)
        result = self._json("GET", f"/v1/runs/{_segment(run_id)}/context", v1.RunContextV1)
        self._ensure_active_project(result.project_id)
        if result.run_id != run_id:
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
        with self._membership_lock:
            run = self._runs.get(run_id)
        if run is None or any(
            (
                result.project_snapshot != run.project_snapshot,
                result.task_snapshot != run.task_snapshot,
                result.workspace_snapshot != run.workspace_snapshot,
                result.required_revision != run.required_revision,
                result.registry_digest != run.registry_digest,
                result.execution_mode is not run.execution_mode,
                result.capture_mode is not run.capture_mode,
            )
        ):
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
        return result

    @_generation_bound
    def run_artifacts(
        self,
        run_id: str,
        *,
        project_id: str,
        limit: int = 50,
        after: str | None = None,
        sort: Literal["created_at", "title", "artifact_type"] = "created_at",
        direction: Literal["asc", "desc"] = "asc",
        artifact_type: v1.ArtifactType | None = None,
    ) -> v1.ArtifactPageV1:
        self._ensure_active_project(project_id)
        self._require_member(v1.ResourceChangeType.RUN, run_id)
        params = _page_query(
            limit,
            after,
            sort,
            direction,
            {"created_at", "title", "artifact_type"},
        )
        if artifact_type is not None:
            params["artifact_type"] = _enum_query(artifact_type, v1.ArtifactType)
        result = self._json(
            "GET", f"/v1/runs/{_segment(run_id)}/artifacts", v1.ArtifactPageV1, params=params
        )
        with self._registration_batch():
            for artifact in result.items:
                if artifact.run_id != run_id:
                    _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
                self._register_artifact(artifact)
        return result

    @_generation_bound
    def get_artifact(self, artifact_id: str, *, project_id: str) -> v1.ArtifactSummaryV1:
        self._ensure_active_project(project_id)
        result = self._json(
            "GET",
            f"/v1/projects/{_segment(project_id)}/artifacts/{_segment(artifact_id)}",
            v1.ArtifactSummaryV1,
        )
        self._register_artifact(result, expected_id=artifact_id)
        return result

    @_generation_bound
    def artifact_content(self, artifact_id: str, *, project_id: str) -> v1.ArtifactContentV1:
        self._ensure_active_project(project_id)
        self._require_member(v1.ResourceChangeType.ARTIFACT, artifact_id)
        result = self._json(
            "GET",
            f"/v1/projects/{_segment(project_id)}/artifacts/{_segment(artifact_id)}/content",
            v1.ArtifactContentV1,
            max_response_bytes=MAX_CORE_ARTIFACT_RESPONSE_BYTES,
        )
        if result.artifact_id != artifact_id:
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
        with self._membership_lock:
            artifact = self._artifacts.get(artifact_id)
        if artifact is None or result.artifact_type is not artifact.artifact_type:
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
        return result

    @_generation_bound
    def artifact_diff(
        self,
        artifact_id: str,
        *,
        project_id: str,
        previous_artifact_id: str | None = None,
    ) -> v1.ArtifactDiffV1:
        self._ensure_active_project(project_id)
        self._require_member(v1.ResourceChangeType.ARTIFACT, artifact_id)
        params = None
        if previous_artifact_id is not None:
            params = {"previous_artifact_id": _opaque_request(previous_artifact_id)}
        result = self._json(
            "GET",
            f"/v1/projects/{_segment(project_id)}/artifacts/{_segment(artifact_id)}/diff",
            v1.ArtifactDiffV1,
            params=params,
            max_response_bytes=MAX_CORE_ARTIFACT_RESPONSE_BYTES,
        )
        if result.artifact_id != artifact_id or (
            previous_artifact_id is not None
            and result.previous_artifact_id != previous_artifact_id
        ):
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
        with self._membership_lock:
            current = self._artifacts.get(artifact_id)
            previous = self._artifacts.get(result.previous_artifact_id)
        if (
            current is None
            or result.artifact_content_sha256 != current.content_sha256
            or result.previous_artifact_id not in current.lineage.source_artifact_ids
            or (
                previous is not None
                and (
                    previous.project_id != current.project_id
                    or previous.target_id != current.target_id
                    or previous.artifact_type is not current.artifact_type
                    or result.previous_artifact_content_sha256 != previous.content_sha256
                )
            )
        ):
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
        return result

    @_generation_bound
    def list_services(
        self,
        *,
        limit: int = 50,
        after: str | None = None,
        sort: Literal["kind", "status", "updated_at"] = "kind",
        direction: Literal["asc", "desc"] = "asc",
    ) -> v1.ServicePageV1:
        result = self._json(
            "GET",
            "/v1/services",
            v1.ServicePageV1,
            params=_page_query(limit, after, sort, direction, {"kind", "status", "updated_at"}),
        )
        with self._registration_batch():
            for service in result.items:
                self._register_service(service)
        return result

    @_generation_bound
    def get_service(self, service_id: str) -> v1.ServiceSummaryV1:
        result = self._json("GET", f"/v1/services/{_segment(service_id)}", v1.ServiceSummaryV1)
        self._register_service(result, expected_id=service_id)
        return result

    @_generation_bound
    def restart_service(
        self,
        service_id: str,
        request: v1.ServiceRestartRequestV1,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> v1.OperationV1:
        self._require_member(v1.ResourceChangeType.SERVICE, service_id)
        result = self._mutation(
            "POST",
            f"/v1/services/{_segment(service_id)}/restart",
            request,
            v1.ServiceRestartRequestV1,
            v1.OperationV1,
            if_match=if_match,
            idempotency_key=idempotency_key,
            expected_status=202,
        )
        if not isinstance(result.request, v1.ServiceRestartOperationRequestV1) or (
            result.request.service_id != service_id or result.request.request != request
        ):
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
        self._register_operation(result)
        return result

    @_generation_bound
    def service_logs(
        self,
        service_id: str,
        *,
        limit: int = 100,
        after: str | None = None,
        sort: Literal["sequence", "occurred_at"] = "sequence",
        direction: Literal["asc", "desc"] = "asc",
    ) -> v1.LogPageV1:
        self._require_member(v1.ResourceChangeType.SERVICE, service_id)
        result = self._json(
            "GET",
            f"/v1/services/{_segment(service_id)}/logs",
            v1.LogPageV1,
            params=_page_query(limit, after, sort, direction, {"sequence", "occurred_at"}),
            max_response_bytes=MAX_CORE_LOG_RESPONSE_BYTES,
        )
        for entry in result.items:
            if entry.service_id != service_id:
                _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
            if entry.run_id is not None:
                self._require_member(v1.ResourceChangeType.RUN, entry.run_id)
        return result

    @_generation_bound
    def get_operation(self, operation_id: str) -> v1.OperationV1:
        result = self._json("GET", f"/v1/operations/{_segment(operation_id)}", v1.OperationV1)
        self._register_operation(result, expected_id=operation_id)
        return result

    @_generation_bound
    def cancel_operation(
        self,
        operation_id: str,
        request: v1.OperationCancelRequestV1,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> v1.OperationV1:
        self._require_member(v1.ResourceChangeType.OPERATION, operation_id)
        result = self._mutation(
            "POST",
            f"/v1/operations/{_segment(operation_id)}/cancel",
            request,
            v1.OperationCancelRequestV1,
            v1.OperationV1,
            if_match=if_match,
            idempotency_key=idempotency_key,
            expected_status=202,
        )
        self._register_operation(result, expected_id=operation_id)
        return result

    @_generation_bound
    def logs_by_ref(
        self,
        logs_ref: str,
        *,
        limit: int = 100,
        after: str | None = None,
        sort: Literal["sequence", "occurred_at"] = "sequence",
        direction: Literal["asc", "desc"] = "asc",
    ) -> v1.ReferencedLogPageV1:
        logs_ref = _opaque_request(logs_ref)
        with self._membership_lock:
            binding = self._log_refs.get(logs_ref)
        if binding is None or binding.project_id != self._connection.project_id:
            _raise_local(CoreClientLocalErrorCodeV1.ACTIVE_PROJECT_MISMATCH, 409)
        result = self._json(
            "GET",
            f"/v1/logs/{_segment(logs_ref)}",
            v1.ReferencedLogPageV1,
            params=_page_query(limit, after, sort, direction, {"sequence", "occurred_at"}),
            max_response_bytes=MAX_CORE_LOG_RESPONSE_BYTES,
        )
        if result.logs_ref != logs_ref:
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
        for entry in result.items:
            self._require_member(v1.ResourceChangeType.SERVICE, entry.service_id)
            if entry.run_id is not None:
                self._require_member(v1.ResourceChangeType.RUN, entry.run_id)
        return result

    @_generation_bound
    def create_diagnostic(
        self,
        request: v1.DiagnosticsRequestV1,
        *,
        idempotency_key: str,
    ) -> v1.DiagnosticV1:
        _ensure_diagnostic_request_project(request, self._connection.project_id)
        if isinstance(request.target, v1.RunDiagnosticTargetV1):
            self._require_member(v1.ResourceChangeType.RUN, request.target.run_id)
        result = self._mutation(
            "POST",
            "/v1/diagnostics",
            request,
            v1.DiagnosticsRequestV1,
            v1.DiagnosticV1,
            idempotency_key=idempotency_key,
            expected_status=202,
        )
        if result.scopes != request.scopes or result.target != request.target:
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
        self._register_diagnostic(result)
        return result

    @_generation_bound
    def get_diagnostic(self, diagnostic_id: str) -> v1.DiagnosticV1:
        result = self._json("GET", f"/v1/diagnostics/{_segment(diagnostic_id)}", v1.DiagnosticV1)
        self._register_diagnostic(result, expected_id=diagnostic_id)
        return result

    @_generation_bound
    def delete_diagnostic(
        self,
        diagnostic_id: str,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> None:
        self._require_member(v1.ResourceChangeType.DIAGNOSTIC, diagnostic_id)
        self._no_content_mutation(
            "DELETE",
            f"/v1/diagnostics/{_segment(diagnostic_id)}",
            if_match=if_match,
            idempotency_key=idempotency_key,
        )

    @_generation_bound
    def cache_cleanup(
        self,
        request: v1.CacheCleanupRequestV1,
        *,
        idempotency_key: str,
    ) -> v1.OperationV1:
        result = self._mutation(
            "POST",
            "/v1/maintenance/cache-cleanup",
            request,
            v1.CacheCleanupRequestV1,
            v1.OperationV1,
            idempotency_key=idempotency_key,
            expected_status=202,
        )
        if not isinstance(result.request, v1.CacheCleanupOperationRequestV1) or (
            result.request.request != request
        ):
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
        self._register_operation(result)
        return result

    @contextmanager
    def events(self, *, last_event_id: str | None = None) -> Iterator[CoreSseStreamV1]:
        """Open the authenticated event stream; closing the context closes the response."""
        self._require_version_authority()
        headers = self._headers(authenticated=True, accept="text/event-stream")
        if last_event_id is not None:
            cursor = _visible_ascii_header(_cursor(last_event_id))
            _scan_private_strings(
                cursor,
                self._private_values(),
                CoreClientLocalErrorCodeV1.INVALID_REQUEST,
                400,
            )
            headers["Last-Event-ID"] = cursor
        with self._lease() as session_generation:
            deadline = time.monotonic() + self._request_deadline_seconds
            transport_error = False
            release_failed = False
            response: httpx.Response | None = None
            registered = False
            try:
                _check_deadline(deadline)
                request = self._http.build_request("GET", "/v1/events", headers=headers)
                response_reservation = _PROCESS_RESOURCE_CLOSER.reserve()
                if response_reservation is None:
                    _raise_local(CoreClientLocalErrorCodeV1.CONNECTION_FAILED, 503)
                response = _send_before_deadline(
                    lambda: self._http.send(request, stream=True, follow_redirects=False),
                    deadline,
                    response_reservation,
                    late_dispose=lambda late_response, late_reservation: self._schedule_close(
                        late_response.close, late_reservation
                    ),
                )
                self._register_response(response, session_generation, response_reservation)
                registered = True
                _check_deadline(deadline)
                self._ensure_session_generation(session_generation)
                self._ensure_response_origin(response)
                if 300 <= response.status_code < 400:
                    self._ensure_session_generation(session_generation)
                    _raise_local(CoreClientLocalErrorCodeV1.REDIRECT_REJECTED, 502)
                if response.status_code != 200:
                    body = _read_bounded(
                        response,
                        MAX_CORE_ERROR_RESPONSE_BYTES,
                        deadline=deadline,
                    )
                    self._ensure_session_generation(session_generation)
                    _require_content_type(response, "application/json", error_response=True)
                    self._raise_http_error(
                        response.status_code, body, session_generation=session_generation
                    )
                _require_content_type(response, "text/event-stream")
                if response.headers.get("content-encoding", "identity").lower() not in {
                    "",
                    "identity",
                }:
                    _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
                declared_length = _bounded_content_length(
                    response,
                    MAX_CORE_SSE_RESPONSE_BYTES,
                    invalid_code=CoreClientLocalErrorCodeV1.SSE_PROTOCOL_ERROR,
                )
                self._ensure_session_generation(session_generation)
                yield CoreSseStreamV1(
                    _iter_response_bytes(
                        response,
                        deadline,
                        invalid_code=CoreClientLocalErrorCodeV1.SSE_PROTOCOL_ERROR,
                    ),
                    linearize_frame_delivery=lambda frame: self._linearize_sse_frame_delivery(
                        frame, session_generation
                    ),
                    private_values=self._private_values(),
                    declared_length=declared_length,
                    close_started=self._close_started,
                    session_guard=lambda: self._linearize_generation_result(
                        session_generation, deadline
                    ),
                    delivery_lease=lambda: self._sse_delivery_lease(session_generation, deadline),
                    deadline=deadline,
                )
            except CoreClientErrorV1:
                raise
            except (
                httpx.HTTPError,
                OSError,
                TypeError,
                UnicodeError,
                RuntimeError,
                ValueError,
            ):
                transport_error = True
            finally:
                if registered and response is not None:
                    release_failed = not self._release_response(response)
            if transport_error:
                self._raise_transport_error()
            if release_failed:
                self._raise_transport_error()

    def _mutation(
        self,
        method: str,
        path: str,
        request: BaseModel,
        request_model: type[BaseModel],
        response_model: Any,
        *,
        idempotency_key: str,
        if_match: str | None = None,
        if_project_match: str | None = None,
        expected_status: int = 200,
        max_request_bytes: int = MAX_CORE_REQUEST_BYTES,
    ) -> Any:
        body = _encode_request(request, request_model, max_request_bytes)
        _scan_private_strings(
            request.model_dump(mode="json"),
            self._private_values(),
            CoreClientLocalErrorCodeV1.INVALID_REQUEST,
            400,
        )
        headers = self._mutation_headers(
            idempotency_key=idempotency_key,
            if_match=if_match,
            if_project_match=if_project_match,
        )
        headers["Content-Type"] = "application/json"
        return self._json(
            method,
            path,
            response_model,
            content=body,
            headers=headers,
            expected_status=expected_status,
        )

    def _no_content_mutation(
        self,
        method: str,
        path: str,
        *,
        idempotency_key: str,
        if_match: str | None = None,
    ) -> None:
        headers = self._mutation_headers(
            idempotency_key=idempotency_key,
            if_match=if_match,
        )
        self._json(method, path, None, headers=headers, expected_status=204)

    def _json(
        self,
        method: str,
        path: str,
        response_model: Any,
        *,
        authenticated: bool = True,
        params: Mapping[str, str] | None = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        expected_status: int = 200,
        max_response_bytes: int = MAX_CORE_JSON_RESPONSE_BYTES,
    ) -> Any:
        if authenticated:
            self._require_version_authority()
        private_values = self._private_values()
        _scan_private_strings(
            (unquote(path), params or {}, headers or {}),
            private_values,
            CoreClientLocalErrorCodeV1.INVALID_REQUEST,
            400,
        )
        if content is not None:
            request_value = _decode_request_json(content)
            _scan_private_strings(
                request_value,
                private_values,
                CoreClientLocalErrorCodeV1.INVALID_REQUEST,
                400,
            )
        request_headers = self._headers(authenticated=authenticated, accept="application/json")
        if headers:
            request_headers.update(headers)
        with self._json_generation_lease() as (session_generation, deadline):
            transport_error = False
            release_failed = False
            response: httpx.Response | None = None
            registered = False
            try:
                _check_deadline(deadline)
                request = self._http.build_request(
                    method,
                    path,
                    params=params,
                    content=content,
                    headers=request_headers,
                )
                response_reservation = _PROCESS_RESOURCE_CLOSER.reserve()
                if response_reservation is None:
                    _raise_local(CoreClientLocalErrorCodeV1.CONNECTION_FAILED, 503)
                response = _send_before_deadline(
                    lambda: self._http.send(request, stream=True, follow_redirects=False),
                    deadline,
                    response_reservation,
                    late_dispose=lambda late_response, late_reservation: self._schedule_close(
                        late_response.close, late_reservation
                    ),
                )
                self._register_response(response, session_generation, response_reservation)
                registered = True
                _check_deadline(deadline)
                self._ensure_session_generation(session_generation)
                self._ensure_response_origin(response)
                if 300 <= response.status_code < 400:
                    _raise_local(CoreClientLocalErrorCodeV1.REDIRECT_REJECTED, 502)
                limit = (
                    max_response_bytes
                    if response.status_code == expected_status
                    else MAX_CORE_ERROR_RESPONSE_BYTES
                )
                body = _read_bounded(response, limit, deadline=deadline)
                self._ensure_session_generation(session_generation)
                status_code = response.status_code
                content_type = response.headers.get("content-type")
            except CoreClientErrorV1:
                raise
            except (
                httpx.HTTPError,
                OSError,
                TypeError,
                UnicodeError,
                RuntimeError,
                ValueError,
            ):
                transport_error = True
            finally:
                if registered and response is not None:
                    release_failed = not self._release_response(response)
            if transport_error:
                self._raise_transport_error()
            if release_failed:
                self._raise_transport_error()
            self._ensure_session_generation(session_generation)
            if status_code != expected_status:
                if content_type is None or (
                    content_type.split(";", 1)[0].strip().lower() != "application/json"
                ):
                    _raise_local(CoreClientLocalErrorCodeV1.INVALID_ERROR_RESPONSE, 502)
                self._raise_http_error(status_code, body, session_generation=session_generation)
            if expected_status == 204:
                if body:
                    _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
                self._ensure_session_generation(session_generation)
                return None
            if (
                content_type is None
                or content_type.split(";", 1)[0].strip().lower() != "application/json"
            ):
                _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
            decoded = _decode_json_checked(
                body,
                self._private_values(),
                CoreClientLocalErrorCodeV1.INVALID_RESPONSE,
            )
            try:
                adapter = TypeAdapter(response_model)
                if not _json_matches_schema_types(decoded, adapter.json_schema(mode="validation")):
                    raise ValueError("response scalar does not match the contract schema")
                result = adapter.validate_json(body)
            except (ValidationError, ValueError, TypeError, RecursionError):
                self._ensure_session_generation(session_generation)
                _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
            self._ensure_session_generation(session_generation)
            return result

    def _raise_http_error(
        self, status_code: int, body: bytes, *, session_generation: int
    ) -> NoReturn:
        decoded = _decode_json_checked(
            body,
            self._private_values(),
            CoreClientLocalErrorCodeV1.INVALID_ERROR_RESPONSE,
        )
        validation_failed = False
        try:
            adapter = TypeAdapter(v1.ApiErrorV1)
            if not _json_matches_schema_types(decoded, adapter.json_schema(mode="validation")):
                raise ValueError("error scalar does not match the contract schema")
            error = adapter.validate_json(body)
        except (ValidationError, ValueError, TypeError, RecursionError):
            self._ensure_session_generation(session_generation)
            validation_failed = True
            error = None
        if validation_failed or error is None:
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_ERROR_RESPONSE, 502)
        if error.http_status != status_code:
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_ERROR_RESPONSE, 502)
        self._ensure_session_generation(session_generation)
        raise CoreClientErrorV1(status_code, error) from None

    def _headers(self, *, authenticated: bool, accept: str) -> dict[str, str]:
        headers = {"Accept": accept}
        if authenticated:
            headers["Authorization"] = f"Bearer {self._connection.bearer_token}"
        return headers

    def _mutation_headers(
        self,
        *,
        idempotency_key: str,
        if_match: str | None = None,
        if_project_match: str | None = None,
    ) -> dict[str, str]:
        key = _visible_ascii_header(idempotency_key)
        _scan_private_strings(
            key,
            self._private_values(),
            CoreClientLocalErrorCodeV1.INVALID_REQUEST,
            400,
        )
        headers = {"Idempotency-Key": key}
        if if_match is not None:
            headers["If-Match"] = _etag(if_match)
        if if_project_match is not None:
            headers["If-Project-Match"] = _etag(if_project_match)
        return headers

    @contextmanager
    def _lease(
        self,
        *,
        expected_generation: int | None = None,
    ) -> Iterator[int]:
        owner = threading.get_ident()
        with self._state:
            if self._closing or self._closed or self._close_failed:
                _raise_local(CoreClientLocalErrorCodeV1.CLIENT_CLOSED, 503)
            if expected_generation is not None and expected_generation != self._session_generation:
                _raise_local(CoreClientLocalErrorCodeV1.CLIENT_CLOSED, 503)
            if self._close_tasks_pending + self._leases >= MAX_CORE_CLOSE_QUEUE_SIZE - 1:
                _raise_local(CoreClientLocalErrorCodeV1.CONNECTION_FAILED, 503)
            self._leases += 1
            self._lease_owners[owner] = self._lease_owners.get(owner, 0) + 1
            session_generation = self._session_generation
        try:
            yield session_generation
        finally:
            with self._state:
                self._release_lease_locked(owner)

    @contextmanager
    def _generation_lease(
        self,
        *,
        expected_generation: int | None = None,
        deadline: float | None = None,
    ) -> Iterator[_GenerationLeaseToken]:
        owner = threading.get_ident()
        with self._state:
            if self._closing or self._closed or self._close_failed:
                _raise_local(CoreClientLocalErrorCodeV1.CLIENT_CLOSED, 503)
            if expected_generation is not None and expected_generation != self._session_generation:
                _raise_local(CoreClientLocalErrorCodeV1.CLIENT_CLOSED, 503)
            if self._close_tasks_pending + self._leases >= MAX_CORE_CLOSE_QUEUE_SIZE - 1:
                _raise_local(CoreClientLocalErrorCodeV1.CONNECTION_FAILED, 503)
            self._leases += 1
            self._lease_owners[owner] = self._lease_owners.get(owner, 0) + 1
            session_generation = self._session_generation
        stack = getattr(self._generation_local, "stack", None)
        if stack is None:
            stack = []
            self._generation_local.stack = stack
        lease_deadline = time.monotonic() + self._request_deadline_seconds
        if deadline is not None:
            lease_deadline = min(lease_deadline, deadline)
        if stack:
            lease_deadline = min(lease_deadline, stack[-1].deadline)
        token = _GenerationLeaseToken(session_generation, lease_deadline, owner)
        stack.append(token)
        try:
            yield token
        finally:
            popped = stack.pop()
            if popped is not token:
                raise RuntimeError("generation lease stack corrupted")
            if not token.released:
                with self._state:
                    self._release_generation_token_locked(token)

    @contextmanager
    def _sse_delivery_lease(
        self,
        session_generation: int,
        deadline: float,
    ) -> Iterator[None]:
        _check_deadline(deadline)
        with self._generation_lease(
            expected_generation=session_generation,
            deadline=deadline,
        ) as token:
            with self._registration_batch(delivery_token=token):
                yield

    def _release_lease_locked(self, owner: int) -> None:
        self._leases -= 1
        remaining = self._lease_owners[owner] - 1
        if remaining:
            self._lease_owners[owner] = remaining
        else:
            del self._lease_owners[owner]
        self._state.notify_all()

    def _release_generation_token_locked(self, token: _GenerationLeaseToken) -> None:
        if token.released:
            return
        self._release_lease_locked(token.owner)
        token.released = True

    @contextmanager
    def _json_generation_lease(self) -> Iterator[tuple[int, float]]:
        token = self._current_generation_token()
        _check_deadline(token.deadline)
        self._ensure_session_generation(token.generation)
        yield token.generation, token.deadline

    def _current_generation_token(self) -> _GenerationLeaseToken:
        stack = getattr(self._generation_local, "stack", None)
        if not stack:
            raise RuntimeError("JSON request requires a public generation lease")
        return stack[-1]

    def _linearize_generation_result(
        self, session_generation: int, deadline: float | None = None
    ) -> None:
        if deadline is not None:
            _check_deadline(deadline)
        with self._delivery_lock:
            if deadline is not None:
                _check_deadline(deadline)
            with self._state:
                if (
                    self._closing
                    or self._closed
                    or self._close_failed
                    or session_generation != self._session_generation
                ):
                    _raise_local(CoreClientLocalErrorCodeV1.CLIENT_CLOSED, 503)

    def _register_response(
        self,
        response: httpx.Response,
        session_generation: int,
        reservation: _CloseReservation,
    ) -> None:
        close_late_response = False
        with self._state:
            if (
                self._closing
                or self._closed
                or self._close_failed
                or session_generation != self._session_generation
            ):
                close_late_response = True
            else:
                self._active_responses[response] = reservation
        if close_late_response:
            self._schedule_close(response.close, reservation)
            _raise_local(CoreClientLocalErrorCodeV1.CLIENT_CLOSED, 503)

    def _release_response(self, response: httpx.Response) -> bool:
        reservation: _CloseReservation | None = None
        with self._state:
            reservation = self._active_responses.pop(response, None)
        if reservation is not None:
            return self._schedule_close(response.close, reservation)
        return True

    def _close_started(self) -> bool:
        with self._state:
            return self._closing or self._closed or self._close_failed

    def _ensure_session_generation(self, session_generation: int) -> None:
        with self._state:
            if (
                self._closing
                or self._closed
                or self._close_failed
                or session_generation != self._session_generation
            ):
                _raise_local(CoreClientLocalErrorCodeV1.CLIENT_CLOSED, 503)

    def _raise_transport_error(self) -> None:
        if self._close_started():
            _raise_local(CoreClientLocalErrorCodeV1.CLIENT_CLOSED, 503)
        _raise_local(CoreClientLocalErrorCodeV1.CONNECTION_FAILED, 503)

    def _private_values(self) -> tuple[str, ...]:
        return (
            self._connection.bearer_token,
            self._connection.origin,
            self._connection.endpoint,
            self._connection.session_id,
        )

    @contextmanager
    def _registration_batch(
        self,
        *,
        delivery_token: _GenerationLeaseToken | None = None,
    ) -> Iterator[None]:
        with self._membership_lock:
            original = (
                self._members,
                self._log_refs,
                self._workspace_uploads,
                self._workspace_etag_representations,
                self._workspace_representation_etags,
                self._project_state,
                self._runs,
                self._services,
                self._artifacts,
                self._operations,
                self._diagnostics,
                self._sse_event_digests,
                self._capability_authority,
                self._version_authority,
            )
            self._members = self._members.copy()
            self._log_refs = self._log_refs.copy()
            self._workspace_uploads = self._workspace_uploads.copy()
            self._workspace_etag_representations = self._workspace_etag_representations.copy()
            self._workspace_representation_etags = self._workspace_representation_etags.copy()
            self._runs = self._runs.copy()
            self._services = self._services.copy()
            self._artifacts = self._artifacts.copy()
            self._operations = self._operations.copy()
            self._diagnostics = self._diagnostics.copy()
            self._sse_event_digests = self._sse_event_digests.copy()
            succeeded = False
            try:
                yield
                succeeded = True
            finally:
                delivery_error: CoreClientErrorV1 | None = None
                if succeeded and delivery_token is not None:
                    with self._delivery_lock:
                        try:
                            _check_deadline(delivery_token.deadline)
                        except CoreClientErrorV1 as exc:
                            delivery_error = exc
                        with self._state:
                            delivery_rejected = (
                                self._closing
                                or self._closed
                                or self._close_failed
                                or delivery_token.generation != self._session_generation
                            )
                            if delivery_error is not None or delivery_rejected:
                                (
                                    self._members,
                                    self._log_refs,
                                    self._workspace_uploads,
                                    self._workspace_etag_representations,
                                    self._workspace_representation_etags,
                                    self._project_state,
                                    self._runs,
                                    self._services,
                                    self._artifacts,
                                    self._operations,
                                    self._diagnostics,
                                    self._sse_event_digests,
                                    self._capability_authority,
                                    self._version_authority,
                                ) = original
                            self._release_generation_token_locked(delivery_token)
                        if delivery_rejected:
                            _raise_local(CoreClientLocalErrorCodeV1.CLIENT_CLOSED, 503)
                        if delivery_error is not None:
                            raise delivery_error
                elif not succeeded:
                    (
                        self._members,
                        self._log_refs,
                        self._workspace_uploads,
                        self._workspace_etag_representations,
                        self._workspace_representation_etags,
                        self._project_state,
                        self._runs,
                        self._services,
                        self._artifacts,
                        self._operations,
                        self._diagnostics,
                        self._sse_event_digests,
                        self._capability_authority,
                        self._version_authority,
                    ) = original

    def _bind_resource(
        self,
        resource_type: v1.ResourceChangeType,
        resource_id: str,
        *,
        parent_type: v1.ResourceChangeType | None = None,
        parent_id: str | None = None,
    ) -> None:
        with self._membership_lock:
            key, binding = self._validated_resource_binding_locked(
                resource_type,
                resource_id,
                parent_type=parent_type,
                parent_id=parent_id,
            )
            self._members[key] = binding

    def _validated_resource_binding_locked(
        self,
        resource_type: v1.ResourceChangeType,
        resource_id: str,
        *,
        parent_type: v1.ResourceChangeType | None = None,
        parent_id: str | None = None,
    ) -> tuple[tuple[v1.ResourceChangeType, str], _ResourceBinding]:
        resource_id = _opaque_or_response_error(resource_id)
        if (parent_type is None) != (parent_id is None):
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
        if parent_type is not None and parent_id is not None:
            parent_id = _opaque_or_response_error(parent_id)
            parent = self._members.get((parent_type, parent_id))
            if parent is None or parent.project_id != self._connection.project_id:
                _raise_local(CoreClientLocalErrorCodeV1.ACTIVE_PROJECT_MISMATCH, 409)
        binding = _ResourceBinding(
            project_id=self._connection.project_id,
            parent_type=parent_type,
            parent_id=parent_id,
        )
        key = (resource_type, resource_id)
        existing = self._members.get(key)
        if existing is not None and existing != binding:
            _raise_local(CoreClientLocalErrorCodeV1.ACTIVE_PROJECT_MISMATCH, 409)
        return key, binding

    def _require_member(self, resource_type: v1.ResourceChangeType, resource_id: str) -> None:
        resource_id = _opaque_request(resource_id)
        with self._membership_lock:
            binding = self._members.get((resource_type, resource_id))
        if binding is None or binding.project_id != self._connection.project_id:
            _raise_local(CoreClientLocalErrorCodeV1.ACTIVE_PROJECT_MISMATCH, 409)

    def _bind_log_ref(
        self,
        logs_ref: str,
        parent_type: v1.ResourceChangeType,
        parent_id: str,
    ) -> None:
        with self._membership_lock:
            logs_ref, binding = self._validated_log_ref_binding_locked(
                logs_ref, parent_type, parent_id
            )
            self._log_refs[logs_ref] = binding

    def _validated_log_ref_binding_locked(
        self,
        logs_ref: str,
        parent_type: v1.ResourceChangeType,
        parent_id: str,
    ) -> tuple[str, _LogRefBinding]:
        logs_ref = _opaque_or_response_error(logs_ref)
        parent_id = _opaque_or_response_error(parent_id)
        parent = self._members.get((parent_type, parent_id))
        if parent is None or parent.project_id != self._connection.project_id:
            _raise_local(CoreClientLocalErrorCodeV1.ACTIVE_PROJECT_MISMATCH, 409)
        binding = _LogRefBinding(self._connection.project_id, parent_type, parent_id)
        existing = self._log_refs.get(logs_ref)
        if existing is not None and existing != binding:
            _raise_local(CoreClientLocalErrorCodeV1.ACTIVE_PROJECT_MISMATCH, 409)
        return logs_ref, binding

    def _register_run(self, run: v1.RunSummaryV1, *, expected_id: str | None = None) -> None:
        self._ensure_active_project(run.project_id)
        if expected_id is not None and run.id != expected_id:
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
        with self._membership_lock:
            authority = self._capability_authority
            if authority is not None and (
                run.registry_digest != authority.registry_digest
                or run.execution_mode is not authority.execution_mode
                or run.capture_mode.value != authority.evaluated_profile.capture_mode.value
            ):
                _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
            member_key, member_binding = self._validated_resource_binding_locked(
                v1.ResourceChangeType.RUN,
                run.id,
                parent_type=v1.ResourceChangeType.PROJECT,
                parent_id=run.project_id,
            )
            previous = self._runs.get(run.id)
            if previous is not None and (
                previous.project_id != run.project_id
                or previous.project_snapshot != run.project_snapshot
                or previous.task_snapshot != run.task_snapshot
                or previous.workspace_snapshot != run.workspace_snapshot
                or previous.registry_digest != run.registry_digest
                or previous.execution_mode is not run.execution_mode
                or previous.capture_mode is not run.capture_mode
                or previous.required_revision != run.required_revision
                or previous.created_at != run.created_at
            ):
                _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
            self._members[member_key] = member_binding
            self._runs[run.id] = run

    def _register_project(
        self, project: v1.ProjectSummaryV1, *, expected_id: str | None = None
    ) -> None:
        self._ensure_active_project(project.id)
        if expected_id is not None and project.id != expected_id:
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
        with self._membership_lock:
            if self._capability_authority is not None and (
                project.execution_mode is not self._capability_authority.execution_mode
                or project.registry_digest != self._capability_authority.registry_digest
            ):
                _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
            self._project_state = project

    def _register_service(
        self, service: v1.ServiceSummaryV1, *, expected_id: str | None = None
    ) -> None:
        if expected_id is not None and service.id != expected_id:
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
        with self._membership_lock:
            member_key, member_binding = self._validated_resource_binding_locked(
                v1.ResourceChangeType.SERVICE,
                service.id,
                parent_type=v1.ResourceChangeType.PROJECT,
                parent_id=self._connection.project_id,
            )
            previous = self._services.get(service.id)
            if previous is not None and previous.kind is not service.kind:
                _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
            self._members[member_key] = member_binding
            self._services[service.id] = service

    def _register_artifact(
        self, artifact: v1.ArtifactSummaryBaseV1, *, expected_id: str | None = None
    ) -> None:
        self._ensure_active_project(artifact.project_id)
        if expected_id is not None and artifact.id != expected_id:
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
        with self._membership_lock:
            member_key, member_binding = self._validated_resource_binding_locked(
                v1.ResourceChangeType.ARTIFACT,
                artifact.id,
                parent_type=v1.ResourceChangeType.PROJECT,
                parent_id=artifact.project_id,
            )
            previous = self._artifacts.get(artifact.id)
            if previous is not None and (
                previous.project_id != artifact.project_id
                or previous.run_id != artifact.run_id
                or previous.artifact_type is not artifact.artifact_type
                or previous.content_sha256 != artifact.content_sha256
                or previous.created_at != artifact.created_at
            ):
                _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
            self._members[member_key] = member_binding
            self._artifacts[artifact.id] = artifact

    def _register_operation(
        self, operation: v1.OperationV1, *, expected_id: str | None = None
    ) -> None:
        if expected_id is not None and operation.id != expected_id:
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
        parent_type = v1.ResourceChangeType.PROJECT
        parent_id = self._connection.project_id
        if isinstance(operation.request, v1.ServiceRestartOperationRequestV1):
            parent_type = v1.ResourceChangeType.SERVICE
            parent_id = operation.request.service_id
        with self._membership_lock:
            member_key, member_binding = self._validated_resource_binding_locked(
                v1.ResourceChangeType.OPERATION,
                operation.id,
                parent_type=parent_type,
                parent_id=parent_id,
            )
            logs_ref = _opaque_or_response_error(operation.logs_ref)
            log_binding = _LogRefBinding(
                self._connection.project_id,
                v1.ResourceChangeType.OPERATION,
                operation.id,
            )
            existing_log = self._log_refs.get(logs_ref)
            if existing_log is not None and existing_log != log_binding:
                _raise_local(CoreClientLocalErrorCodeV1.ACTIVE_PROJECT_MISMATCH, 409)
            previous = self._operations.get(operation.id)
            if previous is not None and (
                previous.kind is not operation.kind
                or previous.descriptor != operation.descriptor
                or previous.request != operation.request
                or previous.logs_ref != operation.logs_ref
                or previous.created_at != operation.created_at
            ):
                _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
            self._members[member_key] = member_binding
            self._log_refs[logs_ref] = log_binding
            self._operations[operation.id] = operation

    def _register_diagnostic(
        self, diagnostic: v1.DiagnosticV1, *, expected_id: str | None = None
    ) -> None:
        if expected_id is not None and diagnostic.id != expected_id:
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
        _ensure_diagnostic_project(diagnostic, self._connection.project_id)
        target = diagnostic.target
        with self._membership_lock:
            if isinstance(target, v1.RunDiagnosticTargetV1):
                target_id = _opaque_or_response_error(target.run_id)
                target_binding = self._members.get((v1.ResourceChangeType.RUN, target_id))
                if (
                    target_binding is None
                    or target_binding.project_id != self._connection.project_id
                ):
                    _raise_local(CoreClientLocalErrorCodeV1.ACTIVE_PROJECT_MISMATCH, 409)
            member_key, member_binding = self._validated_resource_binding_locked(
                v1.ResourceChangeType.DIAGNOSTIC,
                diagnostic.id,
                parent_type=v1.ResourceChangeType.PROJECT,
                parent_id=self._connection.project_id,
            )
            log_bindings: list[tuple[str, _LogRefBinding]] = []
            for check in diagnostic.checks:
                if check.logs_ref is None:
                    continue
                logs_ref = _opaque_or_response_error(check.logs_ref)
                log_binding = _LogRefBinding(
                    self._connection.project_id,
                    v1.ResourceChangeType.DIAGNOSTIC,
                    diagnostic.id,
                )
                existing_log = self._log_refs.get(logs_ref)
                if existing_log is not None and existing_log != log_binding:
                    _raise_local(CoreClientLocalErrorCodeV1.ACTIVE_PROJECT_MISMATCH, 409)
                log_bindings.append((logs_ref, log_binding))
            previous = self._diagnostics.get(diagnostic.id)
            if previous is not None and (
                previous.scopes != diagnostic.scopes
                or previous.target != diagnostic.target
                or previous.created_at != diagnostic.created_at
            ):
                _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
            self._members[member_key] = member_binding
            self._log_refs.update(log_bindings)
            self._diagnostics[diagnostic.id] = diagnostic

    def _validate_new_workspace_upload(
        self,
        upload: v1.WorkspaceUploadSessionV1,
        request: v1.WorkspaceUploadCreateV1,
        project_id: str,
        project_etag: str,
    ) -> None:
        self._validate_workspace_upload_identity(upload, upload.id, project_id)
        if (
            upload.status is not v1.WorkspaceUploadStatus.OPEN
            or upload.accepted_offset != 0
            or upload.project_snapshot != request.project_snapshot
            or upload.project_etag != project_etag
            or upload.archive != request.archive
            or upload.base_workspace_snapshot != request.base_workspace_snapshot
            or upload.publication is not None
        ):
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)

    def _validate_workspace_upload_identity(
        self, upload: v1.WorkspaceUploadSessionV1, upload_id: str, project_id: str
    ) -> None:
        self._ensure_active_project(upload.project_id)
        if upload.id != upload_id or upload.project_id != project_id:
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)

    def _register_workspace_upload(
        self,
        upload: v1.WorkspaceUploadSessionV1,
        *,
        expected_previous: v1.WorkspaceUploadSessionV1 | None = None,
        create_project_etag: str | None = None,
        exact_replay: bool = False,
    ) -> None:
        with self._membership_lock:
            previous = self._workspace_uploads.get(upload.id)
            if expected_previous is not None and previous != expected_previous:
                _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
            if previous is None:
                if create_project_etag is not None and upload.etag == create_project_etag:
                    _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
            else:
                _ensure_upload_stable(previous, upload)
                if exact_replay and previous != upload:
                    _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
                _ensure_upload_state_progress(previous, upload)
                _ensure_upload_etag_transition(previous, upload)

            representation = _workspace_upload_representation_digest(upload)
            etag_key = (upload.id, upload.etag)
            representation_key = (upload.id, representation)
            previous_representation = self._workspace_etag_representations.get(etag_key)
            previous_etag = self._workspace_representation_etags.get(representation_key)
            if (
                previous_representation is not None and previous_representation != representation
            ) or (previous_etag is not None and previous_etag != upload.etag):
                _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)

            self._workspace_uploads[upload.id] = upload
            self._workspace_etag_representations[etag_key] = representation
            self._workspace_representation_etags[representation_key] = upload.etag

    def _require_workspace_upload(
        self, upload_id: str, project_id: str
    ) -> v1.WorkspaceUploadSessionV1:
        upload_id = _opaque_request(upload_id)
        with self._membership_lock:
            upload = self._workspace_uploads.get(upload_id)
        if upload is None or upload.project_id != project_id:
            _raise_local(CoreClientLocalErrorCodeV1.ACTIVE_PROJECT_MISMATCH, 409)
        return upload

    def _has_member(self, resource_type: v1.ResourceChangeType, resource_id: str) -> bool:
        with self._membership_lock:
            binding = self._members.get((resource_type, resource_id))
        return binding is not None and binding.project_id == self._connection.project_id

    def _require_event_member(
        self, resource_type: v1.ResourceChangeType, resource_id: str
    ) -> None:
        if not self._has_member(resource_type, resource_id):
            _raise_local(CoreClientLocalErrorCodeV1.SNAPSHOT_REFRESH_REQUIRED, 409)

    def _validate_event_membership(self, envelope: v1.EventEnvelopeV1) -> None:
        event = envelope.root
        if isinstance(event, v1.HeartbeatEventV1):
            return
        if isinstance(event, v1.ProjectUpdatedEventV1):
            self._register_project(event.payload, expected_id=self._connection.project_id)
            return
        if isinstance(event, v1.RunUpdatedEventV1):
            self._register_run(event.payload, expected_id=event.change.resource_id)
            return
        if isinstance(event, v1.ArtifactUpdatedEventV1):
            self._register_artifact(event.payload, expected_id=event.change.resource_id)
            return
        if isinstance(
            event,
            (v1.RevisionSuccessorTransitionUpdatedEventV1, v1.RevisionActivatedEventV1),
        ):
            _ensure_event_project(envelope, self._connection.project_id)
            return
        if isinstance(event, v1.RunTimelineAppendedEventV1):
            self._require_event_member(v1.ResourceChangeType.RUN, event.payload.run_id)
            return
        if isinstance(event, v1.LogAppendedEventV1):
            parent_type = (
                v1.ResourceChangeType.RUN
                if event.payload.run_id is not None
                else v1.ResourceChangeType.SERVICE
            )
            parent_id = event.payload.run_id or event.payload.service_id
            self._require_event_member(parent_type, parent_id)
            return
        if isinstance(event, v1.ServiceUpdatedEventV1):
            self._require_event_member(v1.ResourceChangeType.SERVICE, event.payload.id)
            return
        if isinstance(event, v1.DiagnosticUpdatedEventV1):
            target = event.payload.target
            if isinstance(target, v1.ProjectDiagnosticTargetV1):
                self._ensure_active_project(target.project_id)
            elif isinstance(target, v1.RunDiagnosticTargetV1):
                self._ensure_active_project(target.project_id)
                self._require_event_member(v1.ResourceChangeType.RUN, target.run_id)
            else:
                self._require_event_member(v1.ResourceChangeType.DIAGNOSTIC, event.payload.id)
            self._register_diagnostic(event.payload, expected_id=event.change.resource_id)
            return
        if isinstance(event, v1.OperationUpdatedEventV1):
            request = event.payload.request
            if isinstance(request, v1.ServiceRestartOperationRequestV1):
                self._require_event_member(v1.ResourceChangeType.SERVICE, request.service_id)
            else:
                self._require_event_member(v1.ResourceChangeType.OPERATION, event.payload.id)
            self._register_operation(event.payload, expected_id=event.change.resource_id)
            return
        _raise_local(CoreClientLocalErrorCodeV1.SSE_PROTOCOL_ERROR, 502)

    def _linearize_sse_frame_delivery(self, frame: v1.SseFrameV1, session_generation: int) -> None:
        canonical_event = json.dumps(
            frame.data.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        event_digest = hashlib.sha256(canonical_event).hexdigest()
        with self._registration_batch():
            with self._membership_lock:
                previous_digest = self._sse_event_digests.get(frame.id)
                if previous_digest is not None:
                    if previous_digest != event_digest:
                        _raise_local(CoreClientLocalErrorCodeV1.SSE_PROTOCOL_ERROR, 502)
                else:
                    if len(self._sse_event_digests) >= MAX_CORE_SSE_EVENT_BINDINGS:
                        _raise_local(CoreClientLocalErrorCodeV1.SSE_PROTOCOL_ERROR, 502)
                    self._validate_event_membership(frame.data)
                    self._sse_event_digests[frame.id] = event_digest
            self._linearize_generation_result(session_generation)

    def _active_project(self, project_id: str | None) -> str:
        if project_id is None:
            return self._connection.project_id
        self._ensure_active_project(project_id)
        return project_id

    def _require_capability_authority(
        self,
        registry_digest: str,
        *,
        request_error: bool,
    ) -> _CapabilityAuthority:
        with self._membership_lock:
            authority = self._capability_authority
        if authority is None or authority.registry_digest != registry_digest:
            code = (
                CoreClientLocalErrorCodeV1.INVALID_REQUEST
                if request_error
                else CoreClientLocalErrorCodeV1.INVALID_RESPONSE
            )
            _raise_local(code, 400 if request_error else 502)
        return authority

    def _require_version_authority(self) -> v1.VersionResponseV1:
        with self._membership_lock:
            authority = self._version_authority
        if authority is None:
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_CONNECTION, 503)
        return authority

    def _ensure_active_project(self, project_id: str) -> None:
        invalid = False
        try:
            candidate = _opaque(project_id)
        except ValueError:
            invalid = True
            candidate = ""
        if invalid:
            _raise_local(CoreClientLocalErrorCodeV1.ACTIVE_PROJECT_MISMATCH, 409)
        if candidate != self._connection.project_id:
            _raise_local(CoreClientLocalErrorCodeV1.ACTIVE_PROJECT_MISMATCH, 409)

    def _ensure_open(self) -> None:
        with self._state:
            if self._closing or self._closed or self._close_failed:
                _raise_local(CoreClientLocalErrorCodeV1.CLIENT_CLOSED, 503)

    def _ensure_response_origin(self, response: httpx.Response) -> None:
        url = response.request.url
        origin = f"{url.scheme}://{url.host}:{url.port}"
        expected = self._connection.origin.replace("[::1]", "::1")
        if origin != expected:
            _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)


class CoreProjectBootstrapClientV1:
    """One-shot project creation client for a negotiated private Core tunnel.

    The ordinary client is project-bound. This narrow bootstrap surface exists
    only because Core, not Desktop, issues a new project's identity.
    """

    def __init__(
        self,
        connection: CoreBootstrapTunnelConnectionV1,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float | httpx.Timeout = 30.0,
    ) -> None:
        if not isinstance(connection, CoreBootstrapTunnelConnectionV1):
            raise TypeError("connection must be CoreBootstrapTunnelConnectionV1")
        self._connection = connection
        self._client = CoreControlClientV1(
            connection._temporary_binding(),
            transport=transport,
            timeout=timeout,
        )
        self._create_lock = threading.Lock()
        self._submitted_request: v1.ProjectCreateV1 | None = None
        self._submitted_idempotency_key: str | None = None
        self._delivered_result: CoreProjectBootstrapResultV1 | None = None

    def __enter__(self) -> CoreProjectBootstrapClientV1:
        self._client._ensure_open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def version(self) -> v1.VersionResponseV1:
        return self._client.version()

    def health(self) -> v1.HealthResponseV1:
        return self._client.health()

    def status(self) -> v1.CoreStatusV1:
        return self._client.status()

    def capabilities(self, execution_mode: v1.ExecutionMode) -> v1.CapabilitiesResponseV1:
        return self._client.capabilities(execution_mode)

    def create_project(
        self,
        request: v1.ProjectCreateV1,
        *,
        idempotency_key: str,
    ) -> CoreProjectBootstrapResultV1:
        deadline = time.monotonic() + self._client._request_deadline_seconds
        _encode_request(request, v1.ProjectCreateV1, MAX_CORE_REQUEST_BYTES)
        # Validate the header even for a local exact-success replay or rejected retry.
        normalized_key = self._client._mutation_headers(idempotency_key=idempotency_key)[
            "Idempotency-Key"
        ]
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._create_lock.acquire(timeout=remaining):
            _raise_local(CoreClientLocalErrorCodeV1.CONNECTION_FAILED, 503)
        try:
            with self._client._generation_lease(deadline=deadline) as token:
                self._client._require_version_authority()
                if self._submitted_request is not None and (
                    request != self._submitted_request
                    or normalized_key != self._submitted_idempotency_key
                ):
                    _raise_local(CoreClientLocalErrorCodeV1.INVALID_REQUEST, 400)
                if self._delivered_result is not None:
                    return self._deliver(token, self._delivered_result, commit=False)

                # Freeze identity before the first transport submission. An unknown
                # outcome may be retried only with this exact request and key.
                self._submitted_request = request
                self._submitted_idempotency_key = normalized_key
                project = self._client._mutation(
                    "POST",
                    "/v1/projects",
                    request,
                    v1.ProjectCreateV1,
                    v1.ProjectV1,
                    idempotency_key=normalized_key,
                    expected_status=201,
                )
                _ensure_project_create_response(request, project)
                result = CoreProjectBootstrapResultV1(
                    project=project,
                    connection=self._connection.bind(project.id),
                )
                return self._deliver(token, result, commit=True)
        finally:
            self._create_lock.release()

    def _deliver(
        self,
        token: _GenerationLeaseToken,
        result: CoreProjectBootstrapResultV1,
        *,
        commit: bool,
    ) -> CoreProjectBootstrapResultV1:
        with self._client._delivery_lock:
            _check_deadline(token.deadline)
            with self._client._state:
                if (
                    self._client._closing
                    or self._client._closed
                    or self._client._close_failed
                    or token.generation != self._client._session_generation
                ):
                    _raise_local(CoreClientLocalErrorCodeV1.CLIENT_CLOSED, 503)
                if commit:
                    self._delivered_result = result
                self._client._release_generation_token_locked(token)
        return result


def _ensure_project_create_response(
    request: v1.ProjectCreateV1,
    project: v1.ProjectV1,
) -> None:
    if (
        project.name != request.name
        or project.description != request.description
        or project.spec != request.spec
        or project.task != request.task
        or project.workspace != request.workspace
        or project.status is not v1.ProjectStatus.DRAFT
        or project.active_revision is not None
        or project.workspace_publication is not None
        or (
            isinstance(request.workspace, v1.ScratchWorkspaceSpecV1)
            and project.current_workspace_snapshot is None
        )
        or (
            isinstance(request.workspace, v1.ImportedWorkspaceSpecV1)
            and project.current_workspace_snapshot is not None
        )
    ):
        _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)


def _encode_request(request: BaseModel, request_model: type[BaseModel], limit: int) -> bytes:
    if type(request) is not request_model:
        _raise_local(CoreClientLocalErrorCodeV1.INVALID_REQUEST, 400)
    encoding_failed = False
    try:
        body = request.model_dump_json().encode("utf-8")
    except (UnicodeError, ValueError, TypeError, RecursionError):
        encoding_failed = True
        body = b""
    if encoding_failed:
        _raise_local(CoreClientLocalErrorCodeV1.INVALID_REQUEST, 400)
    if len(body) > limit:
        _raise_local(CoreClientLocalErrorCodeV1.INVALID_REQUEST, 400)
    return body


def _decode_request_json(body: bytes) -> object:
    try:
        return json.loads(
            body.decode("utf-8", errors="strict"),
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeError, ValueError, TypeError, RecursionError):
        _raise_local(CoreClientLocalErrorCodeV1.INVALID_REQUEST, 400)


def _decode_json_checked(
    body: bytes,
    private_values: tuple[str, ...],
    code: CoreClientLocalErrorCodeV1,
) -> object:
    decoding_failed = False
    try:
        value = json.loads(
            body.decode("utf-8", errors="strict"),
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeError, ValueError, TypeError, RecursionError):
        decoding_failed = True
        value = None
    if decoding_failed:
        _raise_local(code, 502)
    _scan_private_strings(value, private_values, code, 502)
    return value


def _json_matches_schema_types(
    value: object,
    schema: Mapping[str, object],
    root_schema: Mapping[str, object] | None = None,
) -> bool:
    """Check JSON-native scalar/container types without rejecting arrays for tuples."""
    root = schema if root_schema is None else root_schema
    reference = schema.get("$ref")
    if isinstance(reference, str):
        prefix = "#/$defs/"
        if not reference.startswith(prefix):
            return False
        name = reference.removeprefix(prefix).replace("~1", "/").replace("~0", "~")
        definitions = root.get("$defs")
        if not isinstance(definitions, Mapping):
            return False
        target = definitions.get(name)
        return isinstance(target, Mapping) and _json_matches_schema_types(value, target, root)

    for keyword in ("anyOf", "oneOf"):
        choices = schema.get(keyword)
        if isinstance(choices, list):
            return any(
                isinstance(choice, Mapping) and _json_matches_schema_types(value, choice, root)
                for choice in choices
            )
    all_of = schema.get("allOf")
    if isinstance(all_of, list) and not all(
        isinstance(choice, Mapping) and _json_matches_schema_types(value, choice, root)
        for choice in all_of
    ):
        return False

    expected = schema.get("type")
    if isinstance(expected, list):
        return any(
            _json_matches_schema_types(value, {**schema, "type": candidate}, root)
            for candidate in expected
            if isinstance(candidate, str)
        )
    if expected == "null":
        return value is None
    if expected == "boolean":
        return type(value) is bool
    if expected == "integer":
        return type(value) is int
    if expected == "number":
        return type(value) in {int, float}
    if expected == "string":
        return type(value) is str
    if expected == "array":
        if type(value) is not list:
            return False
        prefix_items = schema.get("prefixItems")
        if isinstance(prefix_items, list):
            if len(value) < len(prefix_items):
                return False
            for item, item_schema in zip(value, prefix_items, strict=False):
                if not isinstance(item_schema, Mapping) or not _json_matches_schema_types(
                    item, item_schema, root
                ):
                    return False
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            start = len(prefix_items) if isinstance(prefix_items, list) else 0
            return all(
                _json_matches_schema_types(item, item_schema, root) for item in value[start:]
            )
        return item_schema is not False or not value
    if expected == "object":
        if type(value) is not dict:
            return False
        properties = schema.get("properties")
        known = properties if isinstance(properties, Mapping) else {}
        for key, item in value.items():
            property_schema = known.get(key)
            if isinstance(property_schema, Mapping):
                if not _json_matches_schema_types(item, property_schema, root):
                    return False
                continue
            additional = schema.get("additionalProperties", True)
            if additional is False:
                return False
            if isinstance(additional, Mapping) and not _json_matches_schema_types(
                item, additional, root
            ):
                return False
        return True
    return True


def _scan_private_strings(
    value: object,
    private_values: tuple[str, ...],
    code: CoreClientLocalErrorCodeV1,
    status_code: int,
) -> None:
    bearer, origin, endpoint, session_id = private_values
    folded_urls = (origin.casefold(), endpoint.casefold())
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            if (
                bearer in current
                or any(url in current.casefold() for url in folded_urls)
                or session_id in current
            ):
                _raise_local(code, status_code)
        elif isinstance(current, Mapping):
            stack.extend(current.keys())
            stack.extend(current.values())
        elif isinstance(current, (list, tuple)):
            stack.extend(current)


def _decoded_chunk_length(request: v1.WorkspaceUploadChunkV1) -> int:
    decoding_failed = False
    try:
        decoded = base64.b64decode(request.content_base64, validate=True)
    except (binascii.Error, ValueError):
        decoding_failed = True
        decoded = b""
    if decoding_failed:
        _raise_local(CoreClientLocalErrorCodeV1.INVALID_REQUEST, 400)
    return len(decoded)


def _ensure_upload_stable(
    previous: v1.WorkspaceUploadSessionV1,
    current: v1.WorkspaceUploadSessionV1,
) -> None:
    if (
        current.id != previous.id
        or current.project_id != previous.project_id
        or current.project_snapshot != previous.project_snapshot
        or current.project_etag != previous.project_etag
        or current.archive != previous.archive
        or current.base_workspace_snapshot != previous.base_workspace_snapshot
        or current.created_at != previous.created_at
    ):
        _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)


def _ensure_upload_etag_transition(
    previous: v1.WorkspaceUploadSessionV1,
    current: v1.WorkspaceUploadSessionV1,
) -> None:
    _ensure_upload_stable(previous, current)
    previous_representation = previous.model_dump(mode="json", exclude={"etag"})
    current_representation = current.model_dump(mode="json", exclude={"etag"})
    if previous_representation != current_representation and current.etag == previous.etag:
        _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)


def _ensure_upload_state_progress(
    previous: v1.WorkspaceUploadSessionV1,
    current: v1.WorkspaceUploadSessionV1,
) -> None:
    if current.accepted_offset < previous.accepted_offset or _utc_timestamp(
        current.updated_at
    ) < _utc_timestamp(previous.updated_at):
        _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
    allowed_statuses = {
        v1.WorkspaceUploadStatus.OPEN: {
            v1.WorkspaceUploadStatus.OPEN,
            v1.WorkspaceUploadStatus.FINALIZED,
            v1.WorkspaceUploadStatus.ABORTED,
        },
        v1.WorkspaceUploadStatus.FINALIZED: {v1.WorkspaceUploadStatus.FINALIZED},
        v1.WorkspaceUploadStatus.ABORTED: {v1.WorkspaceUploadStatus.ABORTED},
    }
    if current.status not in allowed_statuses[previous.status]:
        _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
    if previous.status is not v1.WorkspaceUploadStatus.OPEN and current != previous:
        _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)


def _workspace_upload_representation_digest(
    upload: v1.WorkspaceUploadSessionV1,
) -> str:
    canonical = json.dumps(
        upload.model_dump(mode="json", exclude={"etag"}),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _utc_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.removesuffix("Z") + "+00:00")


def _read_bounded(response: httpx.Response, limit: int, *, deadline: float) -> bytes:
    _check_deadline(deadline)
    if response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
        _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
    declared = _bounded_content_length(response, limit)
    chunks: list[bytes] = []
    total = 0
    for chunk in _iter_response_bytes(response, deadline):
        _check_deadline(deadline)
        total += len(chunk)
        if total > limit:
            _raise_local(CoreClientLocalErrorCodeV1.RESPONSE_TOO_LARGE, 502)
        chunks.append(chunk)
    _check_deadline(deadline)
    if declared is not None and total != declared:
        _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
    return b"".join(chunks)


def _bounded_content_length(
    response: httpx.Response,
    limit: int,
    *,
    invalid_code: CoreClientLocalErrorCodeV1 = CoreClientLocalErrorCodeV1.INVALID_RESPONSE,
) -> int | None:
    content_length = response.headers.get("content-length")
    if content_length is None:
        return None
    invalid = not re.fullmatch(r"0|[1-9][0-9]*", content_length)
    if invalid:
        _raise_local(invalid_code, 502)
    try:
        declared = int(content_length)
    except (ValueError, UnicodeError):
        declared = -1
    if declared < 0:
        _raise_local(invalid_code, 502)
    if declared > limit:
        _raise_local(CoreClientLocalErrorCodeV1.RESPONSE_TOO_LARGE, 502)
    return declared


def _page_query(
    limit: int,
    after: str | None,
    sort: str,
    direction: str,
    allowed_sorts: set[str],
) -> dict[str, str]:
    if type(limit) is not int or not 1 <= limit <= 100:
        _raise_local(CoreClientLocalErrorCodeV1.INVALID_REQUEST, 400)
    if sort not in allowed_sorts or direction not in {"asc", "desc"}:
        _raise_local(CoreClientLocalErrorCodeV1.INVALID_REQUEST, 400)
    params = {"limit": str(limit), "sort": sort, "direction": direction}
    if after is not None:
        params["after"] = _cursor(after)
    return params


def _enum_query(value: object, enum_type: type[StrEnum]) -> str:
    if not isinstance(value, enum_type):
        _raise_local(CoreClientLocalErrorCodeV1.INVALID_REQUEST, 400)
    return value.value


def _opaque(value: str) -> str:
    invalid = False
    try:
        result = _OPAQUE_ID.validate_python(value, strict=True)
    except ValidationError:
        invalid = True
        result = ""
    if invalid:
        raise ValueError("invalid opaque identity") from None
    return result


def _opaque_or_response_error(value: str) -> str:
    invalid = False
    try:
        result = _opaque(value)
    except ValueError:
        invalid = True
        result = ""
    if invalid:
        _raise_local(CoreClientLocalErrorCodeV1.INVALID_RESPONSE, 502)
    return result


def _opaque_request(value: str) -> str:
    invalid = False
    try:
        result = _opaque(value)
    except ValueError:
        invalid = True
        result = ""
    if invalid:
        _raise_local(CoreClientLocalErrorCodeV1.INVALID_REQUEST, 400)
    return result


def _cursor(value: str) -> str:
    invalid = False
    try:
        result = _CURSOR.validate_python(value, strict=True)
    except ValidationError:
        invalid = True
        result = ""
    if invalid:
        _raise_local(CoreClientLocalErrorCodeV1.INVALID_REQUEST, 400)
    return result


def _segment(value: str) -> str:
    invalid = False
    try:
        result = quote(_opaque(value), safe="")
    except ValueError:
        invalid = True
        result = ""
    if invalid:
        _raise_local(CoreClientLocalErrorCodeV1.INVALID_REQUEST, 400)
    return result


def _visible_ascii_header(value: str) -> str:
    if (
        not isinstance(value, str)
        or not _HEADER_VALUE.fullmatch(value)
        or any(not 0x21 <= ord(char) <= 0x7E for char in value)
    ):
        _raise_local(CoreClientLocalErrorCodeV1.INVALID_REQUEST, 400)
    return value


def _etag(value: str) -> str:
    if not isinstance(value, str) or not _ETAG.fullmatch(value):
        _raise_local(CoreClientLocalErrorCodeV1.INVALID_REQUEST, 400)
    return value


def _require_content_type(
    response: httpx.Response,
    expected: str,
    *,
    error_response: bool = False,
) -> None:
    actual = response.headers.get("content-type")
    if actual is None or actual.split(";", 1)[0].strip().lower() != expected:
        code = (
            CoreClientLocalErrorCodeV1.INVALID_ERROR_RESPONSE
            if error_response
            else CoreClientLocalErrorCodeV1.INVALID_RESPONSE
        )
        _raise_local(code, 502)


def _iter_sse_frames(
    chunks: Iterator[bytes],
    *,
    declared_length: int | None,
    deadline: float,
) -> Iterator[bytes]:
    buffer = bytearray()
    lines: list[bytes] = []
    frame_bytes = 0
    total_bytes = 0
    for chunk in chunks:
        _check_deadline(deadline)
        if not chunk:
            continue
        total_bytes += len(chunk)
        if total_bytes > MAX_CORE_SSE_RESPONSE_BYTES:
            _raise_local(CoreClientLocalErrorCodeV1.RESPONSE_TOO_LARGE, 502)
        buffer.extend(chunk)
        if len(buffer) > MAX_CORE_SSE_FRAME_BYTES:
            raise ValueError("SSE line exceeds limit")
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                break
            line = bytes(buffer[:newline])
            del buffer[: newline + 1]
            if line.endswith(b"\r"):
                line = line[:-1]
            if not line:
                if lines:
                    _check_deadline(deadline)
                    yield b"\n".join(lines)
                    lines = []
                    frame_bytes = 0
                continue
            if line.startswith(b":"):
                continue
            frame_bytes += len(line) + 1
            if frame_bytes > MAX_CORE_SSE_FRAME_BYTES:
                raise ValueError("SSE frame exceeds limit")
            lines.append(line)
    if buffer or lines:
        raise ValueError("SSE stream ended with an incomplete frame")
    if declared_length is not None and total_bytes != declared_length:
        raise ValueError("SSE stream length differs from Content-Length")


def _finite_request_deadline(timeout: float | httpx.Timeout) -> float:
    values: tuple[object, ...]
    if isinstance(timeout, httpx.Timeout):
        values = (timeout.connect, timeout.read, timeout.write, timeout.pool)
    else:
        values = (timeout,)
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
        or value > MAX_CORE_REQUEST_DEADLINE_SECONDS
        for value in values
    ):
        _raise_local(CoreClientLocalErrorCodeV1.INVALID_CONNECTION, 400)
    return float(max(values))


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        _raise_local(CoreClientLocalErrorCodeV1.CONNECTION_FAILED, 503)


def _call_before_deadline(
    action: Callable[[], ResponseT],
    deadline: float,
    *,
    late_dispose: Callable[[ResponseT], object] | None = None,
) -> ResponseT:
    _check_deadline(deadline)
    future = _PROCESS_BLOCKING_IO.submit(action)
    if future is None:
        _raise_local(CoreClientLocalErrorCodeV1.CONNECTION_FAILED, 503)
    try:
        return future.result(timeout=max(0.0, deadline - time.monotonic()))
    except FutureTimeoutError:
        cancelled = future.cancel()
        if not cancelled and late_dispose is not None:

            def dispose_completed(completed: Future[ResponseT]) -> None:
                try:
                    value = completed.result()
                except BaseException:
                    return
                try:
                    late_dispose(value)
                except BaseException:
                    pass

            future.add_done_callback(dispose_completed)
        _raise_local(CoreClientLocalErrorCodeV1.CONNECTION_FAILED, 503)


def _send_before_deadline(
    action: Callable[[], ResponseT],
    deadline: float,
    reservation: _CloseReservation,
    *,
    late_dispose: Callable[[ResponseT, _CloseReservation], object],
) -> ResponseT:
    _check_deadline(deadline)
    future = _PROCESS_BLOCKING_IO.submit(action)
    if future is None:
        reservation.release()
        _raise_local(CoreClientLocalErrorCodeV1.CONNECTION_FAILED, 503)
    try:
        return future.result(timeout=max(0.0, deadline - time.monotonic()))
    except FutureTimeoutError:
        if future.cancel():
            reservation.release()
        else:

            def dispose_completed(completed: Future[ResponseT]) -> None:
                try:
                    value = completed.result()
                except BaseException:
                    reservation.release()
                    return
                try:
                    late_dispose(value, reservation)
                except BaseException:
                    reservation.release()

            future.add_done_callback(dispose_completed)
        _raise_local(CoreClientLocalErrorCodeV1.CONNECTION_FAILED, 503)
    except BaseException:
        reservation.release()
        raise


_END_OF_RESPONSE = object()


def _iter_response_bytes(
    response: httpx.Response,
    deadline: float,
    *,
    invalid_code: CoreClientLocalErrorCodeV1 = CoreClientLocalErrorCodeV1.INVALID_RESPONSE,
) -> Iterator[bytes]:
    chunks = response.iter_bytes()
    while True:
        chunk = _call_before_deadline(lambda: next(chunks, _END_OF_RESPONSE), deadline)
        if chunk is _END_OF_RESPONSE:
            return
        if not isinstance(chunk, bytes):
            _raise_local(invalid_code, 502)
        yield chunk


def _validate_sse_frame(frame: bytes, private_values: tuple[str, ...]) -> v1.SseFrameV1:
    fields: dict[str, bytes] = {}
    for line in frame.split(b"\n"):
        name, separator, raw_value = line.partition(b":")
        if not separator or name not in {b"id", b"event", b"data"}:
            raise ValueError("unexpected SSE field")
        key = name.decode("ascii")
        if key in fields:
            raise ValueError("duplicate SSE field")
        fields[key] = raw_value[1:] if raw_value.startswith(b" ") else raw_value
    if set(fields) != {"id", "event", "data"}:
        raise ValueError("incomplete SSE frame")
    event_id = fields["id"].decode("utf-8", errors="strict")
    event_name = fields["event"].decode("utf-8", errors="strict")
    _visible_ascii_sse_id(event_id)
    _scan_private_strings(
        (event_id, event_name),
        private_values,
        CoreClientLocalErrorCodeV1.SSE_PROTOCOL_ERROR,
        502,
    )
    _decode_json_checked(
        fields["data"], private_values, CoreClientLocalErrorCodeV1.SSE_PROTOCOL_ERROR
    )
    envelope = v1.EventEnvelopeV1.model_validate_json(fields["data"])
    return v1.SseFrameV1.model_validate(
        {"id": event_id, "event": event_name, "data": envelope},
        strict=True,
    )


def _visible_ascii_sse_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or any(not 0x21 <= ord(char) <= 0x7E for char in value)
    ):
        _raise_local(CoreClientLocalErrorCodeV1.SSE_PROTOCOL_ERROR, 502)
    return value


def _ensure_diagnostic_request_project(
    request: v1.DiagnosticsRequestV1,
    active_project_id: str,
) -> None:
    target = request.target
    if isinstance(target, (v1.ProjectDiagnosticTargetV1, v1.RunDiagnosticTargetV1)):
        if target.project_id != active_project_id:
            _raise_local(CoreClientLocalErrorCodeV1.ACTIVE_PROJECT_MISMATCH, 409)


def _ensure_diagnostic_project(value: v1.DiagnosticV1, active_project_id: str) -> None:
    target = value.target
    if isinstance(target, (v1.ProjectDiagnosticTargetV1, v1.RunDiagnosticTargetV1)):
        if target.project_id != active_project_id:
            _raise_local(CoreClientLocalErrorCodeV1.ACTIVE_PROJECT_MISMATCH, 409)


def _ensure_event_project(envelope: v1.EventEnvelopeV1, active_project_id: str) -> None:
    event = envelope.root
    project_id: str | None = None
    payload = getattr(event, "payload", None)
    if isinstance(payload, (v1.ProjectSummaryV1, v1.ProjectV1)):
        project_id = payload.id
    elif hasattr(payload, "project_id"):
        project_id = payload.project_id
    elif isinstance(payload, v1.RevisionV1):
        project_id = payload.revision.project_id
    elif isinstance(payload, v1.RevisionHeadV1):
        project_id = payload.project_id
    elif isinstance(payload, v1.DiagnosticV1):
        target = payload.target
        if isinstance(target, (v1.ProjectDiagnosticTargetV1, v1.RunDiagnosticTargetV1)):
            project_id = target.project_id
    if project_id is not None and project_id != active_project_id:
        _raise_local(CoreClientLocalErrorCodeV1.ACTIVE_PROJECT_MISMATCH, 409)


def _local_exception(
    code: CoreClientLocalErrorCodeV1,
    status_code: int,
) -> CoreClientErrorV1:
    return CoreClientErrorV1(
        status_code,
        CoreClientLocalErrorV1(
            code=code,
            message=_LOCAL_ERROR_MESSAGES[code],
            retryable=code
            in {
                CoreClientLocalErrorCodeV1.CONNECTION_FAILED,
                CoreClientLocalErrorCodeV1.CLIENT_CLOSED,
                CoreClientLocalErrorCodeV1.SNAPSHOT_REFRESH_REQUIRED,
            },
        ),
    )


def _raise_local(code: CoreClientLocalErrorCodeV1, status_code: int) -> NoReturn:
    raise _local_exception(code, status_code) from None


__all__ = [
    "CORE_OPENAPI_SHA256",
    "CoreBootstrapTunnelConnectionV1",
    "CoreClientErrorV1",
    "CoreClientLocalErrorCodeV1",
    "CoreClientLocalErrorV1",
    "CoreControlClientV1",
    "CoreProjectBootstrapClientV1",
    "CoreProjectBootstrapResultV1",
    "CoreSseStreamV1",
    "CoreTunnelConnectionV1",
]
