"""Strict active-project tunnel client for Core Control API v2.

The endpoint and bearer in this module are process-private capabilities.  A
client is bound to one profile connection generation and one Core project; it
never accepts a shared backend URL and never retries through SSH.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from functools import wraps
import hashlib
import json
import math
import queue
import re
import threading
import time
from typing import Any, Final, Literal, NoReturn, ParamSpec, TypeVar, cast
from urllib.parse import quote, urlsplit
import weakref

import httpx
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from desktop.sidecar.release_capabilities import (
    ReleaseAuthorityNegotiationError,
    V019_RELEASE_AUTHORITY_POLICY,
    negotiate_core_v2_mutation,
)
from openevo.backend.contracts.v2 import models as v2
from openevo.evolution.framework.profiles import execution_profile_for_release_mode


MAX_CORE_ERROR_RESPONSE_BYTES: Final = 64 * 1024
MAX_CORE_JSON_RESPONSE_BYTES: Final = 4 * 1024 * 1024
MAX_CORE_CAPABILITIES_RESPONSE_BYTES: Final = 8 * 1024 * 1024
MAX_CORE_LOG_RESPONSE_BYTES: Final = 2 * 1024 * 1024
MAX_CORE_ARTIFACT_RESPONSE_BYTES: Final = 4 * 1024 * 1024
MAX_CORE_REQUEST_BYTES: Final = v2.MAX_PROJECT_CONFIG_BYTES + 64 * 1024
MAX_CORE_WORKSPACE_CHUNK_REQUEST_BYTES: Final = v2.MAX_WORKSPACE_CHUNK_BYTES
MAX_CORE_SSE_FRAME_BYTES: Final = 4 * 1024 * 1024
MAX_CORE_SSE_RESPONSE_BYTES: Final = 64 * 1024 * 1024
MAX_CORE_SSE_EVENT_BINDINGS: Final = 10_000
MAX_CORE_CLOSE_WAIT_SECONDS: Final = 5.0
MAX_CORE_CLOSE_QUEUE_SIZE: Final = 256
CORE_CLOSE_WORKER_COUNT: Final = 4
CORE_BLOCKING_IO_WORKER_COUNT: Final = 8
MAX_CORE_REQUEST_DEADLINE_SECONDS: Final = 300.0
MAX_JSON_NESTING: Final = 64

CORE_OPENAPI_SHA256: Final = "f007726d8b092463a2515500e3cc0c496b52b45e9f24d1fc495b11df9a9a837b"
CORE_EVENTS_SCHEMA_SHA256: Final = (
    "464a52685dacaedc391fb17bb27516e64842e23d89d12d475679d7a41a0668df"
)

_BEARER = re.compile(r"[A-Za-z0-9._~+/\-]{43,510}={0,2}\Z", re.ASCII)
_ETAG = re.compile(r'"[0-9a-f]{64}"\Z', re.ASCII)
_HEADER_VALUE = re.compile(r"[^\x00-\x1f\x7f]{1,256}\Z", re.ASCII)
_OPAQUE_ID = TypeAdapter(v2.OpaqueId)
_CURSOR = TypeAdapter(v2.Cursor)
_EVENT_ADAPTER = TypeAdapter(v2.EventEnvelopeV2)

ResponseT = TypeVar("ResponseT")
MethodP = ParamSpec("MethodP")
ModelT = TypeVar("ModelT", bound=BaseModel)


class CoreClientLocalErrorCodeV2(StrEnum):
    CONNECTION_FAILED = "core_connection_failed"
    CLIENT_CLOSED = "core_client_closed"
    INVALID_CONNECTION = "invalid_core_tunnel_connection"
    INVALID_REQUEST = "invalid_core_request"
    NEGOTIATION_REQUIRED = "core_negotiation_required"
    RESPONSE_TOO_LARGE = "core_response_too_large"
    INVALID_RESPONSE = "invalid_core_response"
    INVALID_ERROR_RESPONSE = "invalid_core_error_response"
    REDIRECT_REJECTED = "core_redirect_rejected"
    ACTIVE_PROJECT_MISMATCH = "active_project_mismatch"
    AUTHORITY_DRIFT = "core_authority_drift"
    SSE_PROTOCOL_ERROR = "core_sse_protocol_error"
    SNAPSHOT_REFRESH_REQUIRED = "core_snapshot_refresh_required"


_LOCAL_ERROR_MESSAGES: dict[CoreClientLocalErrorCodeV2, str] = {
    CoreClientLocalErrorCodeV2.CONNECTION_FAILED: (
        "Desktop could not reach the active project tunnel."
    ),
    CoreClientLocalErrorCodeV2.CLIENT_CLOSED: "The Core v2 client is closed.",
    CoreClientLocalErrorCodeV2.INVALID_CONNECTION: (
        "The active project tunnel authority is invalid."
    ),
    CoreClientLocalErrorCodeV2.INVALID_REQUEST: (
        "The Core request does not satisfy the v2 contract."
    ),
    CoreClientLocalErrorCodeV2.NEGOTIATION_REQUIRED: (
        "Core v2 release negotiation is required before this operation."
    ),
    CoreClientLocalErrorCodeV2.RESPONSE_TOO_LARGE: (
        "Core returned a response above the allowed limit."
    ),
    CoreClientLocalErrorCodeV2.INVALID_RESPONSE: ("Core returned an invalid v2 response."),
    CoreClientLocalErrorCodeV2.INVALID_ERROR_RESPONSE: ("Core returned an invalid v2 error."),
    CoreClientLocalErrorCodeV2.REDIRECT_REJECTED: "Core redirects are not allowed.",
    CoreClientLocalErrorCodeV2.ACTIVE_PROJECT_MISMATCH: (
        "The Core resource does not belong to the active project."
    ),
    CoreClientLocalErrorCodeV2.AUTHORITY_DRIFT: (
        "The negotiated Core authority changed during this session."
    ),
    CoreClientLocalErrorCodeV2.SSE_PROTOCOL_ERROR: ("Core returned an invalid v2 event stream."),
    CoreClientLocalErrorCodeV2.SNAPSHOT_REFRESH_REQUIRED: (
        "Core event membership is unknown; reload snapshots before resuming events."
    ),
}


class CoreClientLocalErrorV2(BaseModel):
    """Closed and renderer-safe failures created by the client boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["2"] = "2"
    code: CoreClientLocalErrorCodeV2
    message: str
    retryable: bool


class CoreClientErrorV2(RuntimeError):
    """A typed Core error or a closed local transport error."""

    def __init__(
        self,
        status_code: int,
        error: v2.ApiErrorV2 | CoreClientLocalErrorV2,
    ) -> None:
        super().__init__("OpenEvo Core v2 request failed.")
        self.status_code = status_code
        self.error = error


class CoreMutationOutcomeUnknownV2(RuntimeError):
    """A mutation crossed the send boundary without a validated outcome."""

    code = "core_mutation_outcome_unknown"

    def __init__(self) -> None:
        super().__init__("OpenEvo Core v2 mutation outcome is unknown.")


@dataclass(frozen=True, slots=True)
class CoreTunnelConnectionV2:
    """One active project's private system-SSH tunnel capability."""

    endpoint: str
    bearer_token: str = field(repr=False)
    profile_id: str
    profile_connection_generation: int
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
        if invalid_url or (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or port is None
            or not 1 <= port <= 65_535
            or parsed.path not in {"", "/"}
            or bool(parsed.query)
            or bool(parsed.fragment)
        ):
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_CONNECTION, 400)
        if type(self.bearer_token) is not str or _BEARER.fullmatch(self.bearer_token) is None:
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_CONNECTION, 400)
        if len(set(self.bearer_token.rstrip("="))) < 8:
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_CONNECTION, 400)
        try:
            profile_id = _OPAQUE_ID.validate_python(self.profile_id, strict=True)
            project_id = _OPAQUE_ID.validate_python(self.project_id, strict=True)
            session_id = _OPAQUE_ID.validate_python(self.session_id, strict=True)
        except ValidationError:
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_CONNECTION, 400)
        if (
            type(self.profile_connection_generation) is not int
            or not 1 <= self.profile_connection_generation <= v2.MAX_JAVASCRIPT_SAFE_INTEGER
        ):
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_CONNECTION, 400)
        host = f"[{parsed.hostname}]" if parsed.hostname == "::1" else parsed.hostname
        origin = f"http://{host}:{port}"
        private = (self.bearer_token, origin, self.endpoint, session_id)
        _scan_private_strings(
            profile_id, private, CoreClientLocalErrorCodeV2.INVALID_CONNECTION, 400
        )
        _scan_private_strings(
            project_id, private, CoreClientLocalErrorCodeV2.INVALID_CONNECTION, 400
        )
        if profile_id == project_id or session_id in {profile_id, project_id}:
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_CONNECTION, 400)
        object.__setattr__(self, "profile_id", profile_id)
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "origin", origin)


@dataclass(frozen=True, slots=True)
class CoreBootstrapTunnelConnectionV2:
    """Private tunnel capability before Core issues a project identity."""

    endpoint: str
    bearer_token: str = field(repr=False)
    profile_id: str
    profile_connection_generation: int
    session_id: str
    origin: str = field(init=False)

    def __post_init__(self) -> None:
        temporary = self._temporary_binding()
        object.__setattr__(self, "endpoint", temporary.endpoint)
        object.__setattr__(self, "bearer_token", temporary.bearer_token)
        object.__setattr__(self, "profile_id", temporary.profile_id)
        object.__setattr__(
            self,
            "profile_connection_generation",
            temporary.profile_connection_generation,
        )
        object.__setattr__(self, "session_id", temporary.session_id)
        object.__setattr__(self, "origin", temporary.origin)

    def bind(self, project_id: str) -> CoreTunnelConnectionV2:
        return CoreTunnelConnectionV2(
            endpoint=self.endpoint,
            bearer_token=self.bearer_token,
            profile_id=self.profile_id,
            profile_connection_generation=self.profile_connection_generation,
            project_id=project_id,
            session_id=self.session_id,
        )

    def _temporary_binding(self) -> CoreTunnelConnectionV2:
        try:
            seed = (
                "openevo-core-bootstrap-v2\0"
                f"{self.endpoint}\0{self.profile_id}\0"
                f"{self.profile_connection_generation}\0{self.session_id}"
            ).encode("utf-8")
        except (AttributeError, UnicodeEncodeError):
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_CONNECTION, 400)
        return CoreTunnelConnectionV2(
            endpoint=self.endpoint,
            bearer_token=self.bearer_token,
            profile_id=self.profile_id,
            profile_connection_generation=self.profile_connection_generation,
            project_id=f"project-bootstrap-{hashlib.sha256(seed).hexdigest()[:32]}",
            session_id=self.session_id,
        )


@dataclass(frozen=True, slots=True)
class CoreProjectBootstrapResultV2:
    project: v2.ProjectV2
    connection: CoreTunnelConnectionV2

    def __post_init__(self) -> None:
        if type(self.project) is not v2.ProjectV2:
            raise TypeError("project must be an exact ProjectV2")
        if type(self.connection) is not CoreTunnelConnectionV2:
            raise TypeError("connection must be an exact CoreTunnelConnectionV2")
        if self.connection.project_id != self.project.project_id:
            raise ValueError("bootstrap connection must bind the created project")


@dataclass(slots=True)
class _GenerationLeaseToken:
    generation: int
    deadline: float
    owner: int
    released: bool = False


class _CloseReservation:
    def __init__(self, closer: _BoundedResourceCloser) -> None:
        self._closer = closer
        self._lock = threading.Lock()
        self._consumed = False

    def submit(self, action: Callable[[], None]) -> bool:
        with self._lock:
            if self._consumed or not self._closer._submit_reserved(action):
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
    """Process-global fixed-capacity owner for possibly blocking close calls."""

    def __init__(self, *, worker_count: int, capacity: int) -> None:
        self._queue: queue.Queue[Callable[[], None]] = queue.Queue(maxsize=capacity)
        self._capacity = capacity
        self._lock = threading.Lock()
        self._owned = 0
        for index in range(worker_count):
            threading.Thread(
                target=self._worker,
                name=f"openevo-core-v2-resource-closer-{index + 1}",
                daemon=True,
            ).start()

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

    def _submit_reserved(self, action: Callable[[], None]) -> bool:
        def owned_action() -> None:
            try:
                action()
            finally:
                self._release_reserved()

        try:
            self._queue.put_nowait(owned_action)
        except queue.Full:
            return False
        return True

    def _release_reserved(self) -> None:
        with self._lock:
            if self._owned <= 0:
                raise RuntimeError("Core closer reservation accounting underflow")
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


class _BoundedBlockingIoExecutor:
    def __init__(self) -> None:
        self._queue: queue.Queue[tuple[Future[Any], Callable[[], Any]]] = queue.Queue(
            maxsize=MAX_CORE_CLOSE_QUEUE_SIZE
        )
        for index in range(CORE_BLOCKING_IO_WORKER_COUNT):
            threading.Thread(
                target=self._worker,
                name=f"openevo-core-v2-blocking-io-{index + 1}",
                daemon=True,
            ).start()

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


_PROCESS_RESOURCE_CLOSER = _BoundedResourceCloser(
    worker_count=CORE_CLOSE_WORKER_COUNT,
    capacity=MAX_CORE_CLOSE_QUEUE_SIZE,
)
_PROCESS_BLOCKING_IO = _BoundedBlockingIoExecutor()


def _finalize_reserved_close(
    close_action: Callable[[], None], reservation: _CloseReservation
) -> None:
    if not reservation.submit(close_action):
        reservation.release()


@dataclass(slots=True)
class _AuthorityCache:
    version: v2.VersionResponseV2 | None = None
    capabilities: dict[str, v2.CapabilitiesResponseV2] = field(default_factory=dict)
    project: v2.ProjectV2 | None = None
    uploads: dict[str, v2.WorkspaceUploadSessionV2] = field(default_factory=dict)
    heads: dict[str, v2.ProjectHeadRefV2] = field(default_factory=dict)
    transitions: dict[str, v2.SuccessorTransitionV2] = field(default_factory=dict)
    transition_ids: set[str] = field(default_factory=set)
    tasks: dict[str, v2.TaskV2] = field(default_factory=dict)
    task_ids: set[str] = field(default_factory=set)
    attempts: dict[str, v2.AttemptRefV2] = field(default_factory=dict)
    artifacts: dict[str, v2.ArtifactV2] = field(default_factory=dict)
    services: dict[str, v2.ServiceV2] = field(default_factory=dict)
    operations: dict[str, v2.OperationV2] = field(default_factory=dict)
    diagnostics: dict[str, v2.DiagnosticV2] = field(default_factory=dict)
    event_digests: dict[str, str] = field(default_factory=dict)
    event_sequences: dict[str, int] = field(default_factory=dict)
    event_order: deque[str] = field(default_factory=deque)
    maximum_event_sequence: int = 0

    def clone(self) -> _AuthorityCache:
        return _AuthorityCache(
            version=self.version,
            capabilities=self.capabilities.copy(),
            project=self.project,
            uploads=self.uploads.copy(),
            heads=self.heads.copy(),
            transitions=self.transitions.copy(),
            transition_ids=self.transition_ids.copy(),
            tasks=self.tasks.copy(),
            task_ids=self.task_ids.copy(),
            attempts=self.attempts.copy(),
            artifacts=self.artifacts.copy(),
            services=self.services.copy(),
            operations=self.operations.copy(),
            diagnostics=self.diagnostics.copy(),
            event_digests=self.event_digests.copy(),
            event_sequences=self.event_sequences.copy(),
            event_order=deque(self.event_order),
            maximum_event_sequence=self.maximum_event_sequence,
        )


def _generation_bound(
    method: Callable[MethodP, ResponseT],
) -> Callable[MethodP, ResponseT]:
    @wraps(method)
    def wrapped(*args: MethodP.args, **kwargs: MethodP.kwargs) -> ResponseT:
        client = args[0]
        if not isinstance(client, CoreControlClientV2):
            raise TypeError("generation-bound method requires CoreControlClientV2")
        with client._generation_lease() as token:
            with client._cache_transaction(token):
                result = method(*args, **kwargs)
                client._linearize_generation_result(token.generation, token.deadline)
                return result

    return wrapped


def _mutation_bound(
    method: Callable[MethodP, ResponseT],
) -> Callable[MethodP, ResponseT]:
    @wraps(method)
    def wrapped(*args: MethodP.args, **kwargs: MethodP.kwargs) -> ResponseT:
        client = args[0]
        if not isinstance(client, CoreControlClientV2):
            raise TypeError("mutation-bound method requires CoreControlClientV2")
        delivered = False
        try:
            with client._generation_lease() as token:
                with client._cache_transaction(token):
                    result = method(*args, **kwargs)
                    delivered = True
                    client._linearize_generation_result(token.generation, token.deadline)
                    return result
        except CoreClientErrorV2:
            if delivered:
                raise CoreMutationOutcomeUnknownV2 from None
            raise

    return wrapped


class CoreSseStreamV2(Iterator[v2.SseFrameV2]):
    """Single-pass bounded SSE adapter sealed to one client generation."""

    def __init__(
        self,
        *,
        client: CoreControlClientV2,
        chunks: Iterator[bytes],
        declared_length: int | None,
        generation: int,
        deadline: float,
    ) -> None:
        self._client = client
        self._generation = generation
        self._deadline = deadline
        self._frames = _iter_sse_frames(
            chunks,
            declared_length=declared_length,
            deadline=deadline,
        )

    def __iter__(self) -> CoreSseStreamV2:
        return self

    def __next__(self) -> v2.SseFrameV2:
        try:
            _check_deadline(self._deadline)
            self._client._ensure_session_generation(self._generation)
            raw = next(self._frames)
            frame = _validate_sse_frame(raw, self._client._private_values())
            self._client._deliver_sse_frame(
                frame,
                expected_generation=self._generation,
                deadline=self._deadline,
            )
            return frame
        except StopIteration:
            raise
        except CoreClientErrorV2:
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
            code = (
                CoreClientLocalErrorCodeV2.CLIENT_CLOSED
                if self._client._close_started()
                else CoreClientLocalErrorCodeV2.SSE_PROTOCOL_ERROR
            )
            _raise_local(code, 503 if code is CoreClientLocalErrorCodeV2.CLIENT_CLOSED else 502)


class CoreControlClientV2:
    """Thread-safe strict client bound to one active Core v2 project."""

    def __init__(
        self,
        connection: CoreTunnelConnectionV2,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float | httpx.Timeout = 30.0,
    ) -> None:
        if type(connection) is not CoreTunnelConnectionV2:
            raise TypeError("connection must be an exact CoreTunnelConnectionV2")
        self._request_deadline_seconds = _finite_request_deadline(timeout)
        self._connection = connection
        self._state = threading.Condition(threading.RLock())
        self._delivery_lock = threading.RLock()
        self._cache_lock = threading.RLock()
        self._cache = _AuthorityCache()
        self._generation_local = threading.local()
        self._closing = False
        self._closed = False
        self._close_failed = False
        self._session_generation = 0
        self._leases = 0
        self._lease_owners: dict[int, int] = {}
        self._close_tasks_pending = 0
        self._retained_closes: list[tuple[Callable[[], None], _CloseReservation]] = []
        self._active_responses: dict[httpx.Response, _CloseReservation] = {}
        transport_reservation = _PROCESS_RESOURCE_CLOSER.reserve()
        if transport_reservation is None:
            _raise_local(CoreClientLocalErrorCodeV2.CONNECTION_FAILED, 503)
        try:
            self._http = httpx.Client(
                base_url=f"{connection.origin}/",
                transport=transport,
                timeout=timeout,
                trust_env=False,
                follow_redirects=False,
                headers={
                    "Accept-Encoding": "identity",
                    "User-Agent": "OpenEvo-Desktop-CoreClient/2",
                },
            )
        except BaseException:
            transport_reservation.release()
            raise
        self._transport_reservation = transport_reservation
        self._transport_finalizer = weakref.finalize(
            self,
            _finalize_reserved_close,
            self._http.close,
            transport_reservation,
        )

    def __enter__(self) -> CoreControlClientV2:
        self._ensure_open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return "CoreControlClientV2(<private>)"

    @property
    def connection(self) -> CoreTunnelConnectionV2:
        return self._connection

    @property
    def cached_project(self) -> v2.ProjectV2 | None:
        with self._cache_lock:
            return self._cache.project

    @property
    def negotiated_version(self) -> v2.VersionResponseV2 | None:
        with self._cache_lock:
            return self._cache.version

    def close(self) -> None:
        """Seal delivery, cancel owned responses, and boundedly close transport."""

        self._retry_retained_closes()
        deadline = time.monotonic() + MAX_CORE_CLOSE_WAIT_SECONDS
        close_actions: tuple[tuple[Callable[[], None], _CloseReservation], ...] = ()
        with self._delivery_lock:
            with self._state:
                if self._closed:
                    if self._close_failed:
                        raise _local_exception(
                            CoreClientLocalErrorCodeV2.CLIENT_CLOSED, 503
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
                            CoreClientLocalErrorCodeV2.CLIENT_CLOSED, 503
                        ) from None
                    return
                self._closing = True
                self._session_generation += 1
                self._transport_finalizer.detach()
                close_actions = tuple(
                    (response.close, reservation)
                    for response, reservation in self._active_responses.items()
                ) + ((self._http.close, self._transport_reservation),)
                self._active_responses.clear()

        for action, reservation in close_actions:
            self._schedule_close(action, reservation)

        with self._state:
            while self._close_tasks_pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._state.wait(remaining)
            self._closed = True
            self._state.notify_all()
            failed = self._close_failed
        if failed:
            raise _local_exception(CoreClientLocalErrorCodeV2.CLIENT_CLOSED, 503) from None

    def _retry_retained_closes(self) -> None:
        with self._state:
            retained = tuple(self._retained_closes)
            self._retained_closes.clear()
        for action, reservation in retained:
            self._schedule_close(action, reservation)

    def _schedule_close(
        self,
        action: Callable[[], None],
        reservation: _CloseReservation,
    ) -> bool:
        with self._state:
            self._close_tasks_pending += 1

        def tracked() -> None:
            failed = False
            try:
                action()
            except BaseException:
                failed = True
            finally:
                with self._state:
                    if failed:
                        self._close_failed = True
                    self._close_tasks_pending -= 1
                    self._state.notify_all()

        if reservation.submit(tracked):
            return True
        with self._state:
            self._close_tasks_pending -= 1
            self._close_failed = True
            self._retained_closes.append((action, reservation))
            self._state.notify_all()
        return False

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
                _raise_local(CoreClientLocalErrorCodeV2.CLIENT_CLOSED, 503)
            if expected_generation is not None and expected_generation != self._session_generation:
                _raise_local(CoreClientLocalErrorCodeV2.CLIENT_CLOSED, 503)
            if self._leases + self._close_tasks_pending >= MAX_CORE_CLOSE_QUEUE_SIZE - 1:
                _raise_local(CoreClientLocalErrorCodeV2.CONNECTION_FAILED, 503)
            self._leases += 1
            self._lease_owners[owner] = self._lease_owners.get(owner, 0) + 1
            generation = self._session_generation
        stack = getattr(self._generation_local, "stack", None)
        if stack is None:
            stack = []
            self._generation_local.stack = stack
        lease_deadline = time.monotonic() + self._request_deadline_seconds
        if deadline is not None:
            lease_deadline = min(lease_deadline, deadline)
        if stack:
            lease_deadline = min(lease_deadline, stack[-1].deadline)
        token = _GenerationLeaseToken(generation, lease_deadline, owner)
        stack.append(token)
        try:
            yield token
        finally:
            popped = stack.pop()
            if popped is not token:
                raise RuntimeError("Core v2 generation lease stack is corrupt")
            if not token.released:
                with self._state:
                    self._release_generation_token_locked(token)

    def _release_generation_token_locked(self, token: _GenerationLeaseToken) -> None:
        if token.released:
            return
        self._leases -= 1
        remaining = self._lease_owners[token.owner] - 1
        if remaining:
            self._lease_owners[token.owner] = remaining
        else:
            del self._lease_owners[token.owner]
        token.released = True
        self._state.notify_all()

    def _current_generation_token(self) -> _GenerationLeaseToken:
        stack = getattr(self._generation_local, "stack", None)
        if not stack:
            raise RuntimeError("Core v2 request requires a public generation lease")
        return cast(_GenerationLeaseToken, stack[-1])

    @contextmanager
    def _cache_transaction(self, token: _GenerationLeaseToken) -> Iterator[None]:
        with self._cache_lock:
            original = self._cache
            self._cache = original.clone()
            succeeded = False
            try:
                yield
                succeeded = True
            finally:
                delivery_error: CoreClientErrorV2 | None = None
                if succeeded:
                    with self._delivery_lock:
                        try:
                            _check_deadline(token.deadline)
                        except CoreClientErrorV2 as exc:
                            delivery_error = exc
                        with self._state:
                            rejected = (
                                self._closing
                                or self._closed
                                or self._close_failed
                                or token.generation != self._session_generation
                            )
                            if rejected or delivery_error is not None:
                                self._cache = original
                            self._release_generation_token_locked(token)
                        if rejected:
                            _raise_local(CoreClientLocalErrorCodeV2.CLIENT_CLOSED, 503)
                        if delivery_error is not None:
                            raise delivery_error
                else:
                    self._cache = original

    def _linearize_generation_result(self, generation: int, deadline: float) -> None:
        _check_deadline(deadline)
        with self._delivery_lock:
            _check_deadline(deadline)
            self._ensure_session_generation(generation)

    def _ensure_session_generation(self, generation: int) -> None:
        with self._state:
            if (
                self._closing
                or self._closed
                or self._close_failed
                or generation != self._session_generation
            ):
                _raise_local(CoreClientLocalErrorCodeV2.CLIENT_CLOSED, 503)

    def _close_started(self) -> bool:
        with self._state:
            return self._closing or self._closed or self._close_failed

    def _ensure_open(self) -> None:
        with self._state:
            if self._closing or self._closed or self._close_failed:
                _raise_local(CoreClientLocalErrorCodeV2.CLIENT_CLOSED, 503)

    def _private_values(self) -> tuple[str, ...]:
        return (
            self._connection.bearer_token,
            self._connection.endpoint,
            self._connection.origin,
            self._connection.session_id,
        )

    @_generation_bound
    def version(self) -> v2.VersionResponseV2:
        result = self._json(
            "GET",
            "/version",
            v2.VersionResponseV2,
            authenticated=False,
            expected_status=200,
        )
        try:
            negotiated = negotiate_core_v2_mutation(result.model_dump(mode="json"))
        except ReleaseAuthorityNegotiationError:
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_RESPONSE, 502)
        if (
            negotiated != result
            or CORE_OPENAPI_SHA256 != V019_RELEASE_AUTHORITY_POLICY.core_openapi_sha256
            or CORE_EVENTS_SCHEMA_SHA256 != V019_RELEASE_AUTHORITY_POLICY.core_event_schema_sha256
        ):
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_RESPONSE, 502)
        if self._cache.version is not None and self._cache.version != result:
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_RESPONSE, 502)
        self._cache.version = result
        self._validate_cached_authority()
        return result

    @_generation_bound
    def health(self) -> v2.HealthResponseV2:
        return self._json(
            "GET",
            "/health",
            v2.HealthResponseV2,
            authenticated=False,
            expected_status=200,
        )

    @_generation_bound
    def system_status(self) -> v2.SystemStatusV2:
        result = self._json(
            "GET",
            "/v2/system/status",
            v2.SystemStatusV2,
            expected_status=200,
        )
        version = self._require_version_authority()
        if (
            result.release_version != version.release_version
            or result.source_commit != version.source_commit
            or result.registry_sha256 != version.registry_sha256
        ):
            _raise_local(CoreClientLocalErrorCodeV2.AUTHORITY_DRIFT, 502)
        return result

    @_generation_bound
    def capabilities(
        self,
        execution_mode: v2.ExecutionModeV2,
    ) -> v2.CapabilitiesResponseV2:
        mode = _execution_mode(execution_mode)
        result = self._json(
            "GET",
            "/v2/capabilities",
            v2.CapabilitiesResponseV2,
            params=(("execution_mode", mode),),
            limit=MAX_CORE_CAPABILITIES_RESPONSE_BYTES,
            expected_status=200,
        )
        version = self._require_version_authority()
        expected_profile = execution_profile_for_release_mode(
            "codex_subscription_transcript"
            if mode == "codex_subscription_transcript"
            else "self-deployed"
        )
        if (
            result.registry_digest != version.registry_sha256
            or result.evaluated_profile != expected_profile
            or result.core_version != version.release_version
        ):
            _raise_local(CoreClientLocalErrorCodeV2.AUTHORITY_DRIFT, 502)
        previous = self._cache.capabilities.get(mode)
        if previous is not None and previous != result:
            _raise_local(CoreClientLocalErrorCodeV2.AUTHORITY_DRIFT, 502)
        self._cache.capabilities[mode] = result
        return result

    @_generation_bound
    def list_projects(
        self,
        *,
        limit: int = 50,
        after: str | None = None,
        direction: Literal["asc", "desc"] = "desc",
    ) -> v2.ProjectPageV2:
        page = self._json(
            "GET",
            "/v2/projects",
            v2.ProjectPageV2,
            params=_page_query(limit, after, direction=direction),
            expected_status=200,
        )
        for project in page.items:
            self._register_project(project)
        return page

    def create_project(
        self,
        request: v2.ProjectCreateV2,
        *,
        idempotency_key: str,
    ) -> NoReturn:
        del request, idempotency_key
        _raise_local(CoreClientLocalErrorCodeV2.INVALID_REQUEST, 400)

    @_generation_bound
    def get_project(self, project_id: str | None = None) -> v2.ProjectV2:
        active = self._active_project(project_id)
        project = self._json(
            "GET",
            f"/v2/projects/{_segment(active, self._private_values())}",
            v2.ProjectV2,
            expected_status=200,
        )
        self._register_project(project)
        return project

    @_mutation_bound
    def update_project(
        self,
        request: v2.ProjectUpdateV2,
        *,
        if_match: str,
        idempotency_key: str,
        project_id: str | None = None,
    ) -> v2.ProjectV2:
        active = self._active_project(project_id)
        result = self._mutation(
            "PATCH",
            f"/v2/projects/{_segment(active, self._private_values())}",
            request,
            v2.ProjectUpdateV2,
            v2.ProjectV2,
            if_match=if_match,
            idempotency_key=idempotency_key,
            expected_status=200,
        )
        self._register_project(result)
        return result

    @_mutation_bound
    def create_workspace_upload(
        self,
        request: v2.WorkspaceUploadCreateV2,
        *,
        if_match: str,
        idempotency_key: str,
        project_id: str | None = None,
    ) -> v2.WorkspaceUploadSessionV2:
        active = self._active_project(project_id)
        result = self._mutation(
            "POST",
            f"/v2/projects/{_segment(active, self._private_values())}/workspace-uploads",
            request,
            v2.WorkspaceUploadCreateV2,
            v2.WorkspaceUploadSessionV2,
            if_match=if_match,
            idempotency_key=idempotency_key,
            expected_status=201,
        )
        self._register_upload(result, expected_id=None, previous=None)
        return result

    @_generation_bound
    def get_workspace_upload(
        self,
        upload_id: str,
        *,
        project_id: str | None = None,
    ) -> v2.WorkspaceUploadSessionV2:
        active = self._active_project(project_id)
        upload = self._json(
            "GET",
            f"/v2/projects/{_segment(active, self._private_values())}"
            f"/workspace-uploads/{_segment(upload_id, self._private_values())}",
            v2.WorkspaceUploadSessionV2,
            expected_status=200,
        )
        self._register_upload(upload, expected_id=upload_id, previous=None)
        return upload

    @_mutation_bound
    def put_workspace_upload_chunk(
        self,
        upload_id: str,
        chunk_index: int,
        chunk: bytes,
        *,
        chunk_sha256: str,
        if_match: str,
        idempotency_key: str,
        project_id: str | None = None,
    ) -> v2.WorkspaceUploadSessionV2:
        active = self._active_project(project_id)
        previous = self._require_upload(upload_id)
        if (
            type(chunk_index) is not int
            or not 0 <= chunk_index < v2.MAX_WORKSPACE_CHUNKS
            or type(chunk) is not bytes
            or not 1 <= len(chunk) <= v2.MAX_WORKSPACE_CHUNK_BYTES
            or hashlib.sha256(chunk).hexdigest() != chunk_sha256
            or chunk_index != previous.next_chunk_index
        ):
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_REQUEST, 400)
        result = self._binary_mutation(
            "PUT",
            f"/v2/projects/{_segment(active, self._private_values())}"
            f"/workspace-uploads/{_segment(upload_id, self._private_values())}"
            f"/chunks/{chunk_index}",
            chunk,
            v2.WorkspaceUploadSessionV2,
            headers={
                "X-OpenEvo-Chunk-SHA256": chunk_sha256,
                "X-OpenEvo-Chunk-Byte-Size": str(len(chunk)),
            },
            if_match=if_match,
            idempotency_key=idempotency_key,
            expected_status=200,
        )
        self._register_upload(result, expected_id=upload_id, previous=previous)
        return result

    @_mutation_bound
    def finalize_workspace_upload(
        self,
        upload_id: str,
        request: v2.WorkspaceUploadFinalizeV2,
        *,
        if_match: str,
        idempotency_key: str,
        project_id: str | None = None,
    ) -> v2.WorkspaceUploadSessionV2:
        active = self._active_project(project_id)
        previous = self._require_upload(upload_id)
        result = self._mutation(
            "POST",
            f"/v2/projects/{_segment(active, self._private_values())}"
            f"/workspace-uploads/{_segment(upload_id, self._private_values())}/finalize",
            request,
            v2.WorkspaceUploadFinalizeV2,
            v2.WorkspaceUploadSessionV2,
            if_match=if_match,
            idempotency_key=idempotency_key,
            expected_status=201,
        )
        self._register_upload(result, expected_id=upload_id, previous=previous)
        return result

    @_mutation_bound
    def abort_workspace_upload(
        self,
        upload_id: str,
        request: v2.WorkspaceUploadAbortV2,
        *,
        if_match: str,
        idempotency_key: str,
        project_id: str | None = None,
    ) -> v2.WorkspaceUploadSessionV2:
        active = self._active_project(project_id)
        previous = self._require_upload(upload_id)
        result = self._mutation(
            "POST",
            f"/v2/projects/{_segment(active, self._private_values())}"
            f"/workspace-uploads/{_segment(upload_id, self._private_values())}/abort",
            request,
            v2.WorkspaceUploadAbortV2,
            v2.WorkspaceUploadSessionV2,
            if_match=if_match,
            idempotency_key=idempotency_key,
            expected_status=200,
        )
        self._register_upload(result, expected_id=upload_id, previous=previous)
        return result

    @_mutation_bound
    def validate_project(
        self,
        request: v2.ProjectValidationRequestV2,
        *,
        idempotency_key: str,
        project_id: str | None = None,
    ) -> v2.ProjectValidationResponseV2:
        active = self._active_project(project_id)
        version = self._require_version_authority()
        if request.expected_registry_sha256 != version.registry_sha256:
            _raise_local(CoreClientLocalErrorCodeV2.AUTHORITY_DRIFT, 409)
        result = self._mutation(
            "POST",
            f"/v2/projects/{_segment(active, self._private_values())}/validate",
            request,
            v2.ProjectValidationRequestV2,
            v2.ProjectValidationResponseV2,
            idempotency_key=idempotency_key,
            expected_status=200,
        )
        if result.project_id != active or result.registry_sha256 != version.registry_sha256:
            _raise_local(CoreClientLocalErrorCodeV2.ACTIVE_PROJECT_MISMATCH, 502)
        return result

    @_generation_bound
    def list_project_heads(
        self,
        *,
        limit: int = 50,
        after: str | None = None,
        direction: Literal["asc", "desc"] = "desc",
        project_id: str | None = None,
    ) -> v2.ProjectHeadPageV2:
        active = self._active_project(project_id)
        page = self._json(
            "GET",
            f"/v2/projects/{_segment(active, self._private_values())}/heads",
            v2.ProjectHeadPageV2,
            params=_page_query(limit, after, direction=direction),
            expected_status=200,
        )
        for head in page.items:
            self._register_head(head)
        return page

    @_generation_bound
    def get_active_project_head(self, project_id: str | None = None) -> v2.ProjectHeadRefV2:
        active = self._active_project(project_id)
        head = self._json(
            "GET",
            f"/v2/projects/{_segment(active, self._private_values())}/heads/active",
            v2.ProjectHeadRefV2,
            expected_status=200,
        )
        self._register_head(head)
        return head

    @_generation_bound
    def get_project_head(self, project_head_id: str) -> v2.ProjectHeadRefV2:
        head = self._json(
            "GET",
            f"/v2/project-heads/{_segment(project_head_id, self._private_values())}",
            v2.ProjectHeadRefV2,
            expected_status=200,
        )
        if head.project_head_id != project_head_id:
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_RESPONSE, 502)
        self._register_head(head)
        return head

    @_generation_bound
    def list_transitions(
        self,
        *,
        limit: int = 50,
        after: str | None = None,
        direction: Literal["asc", "desc"] = "desc",
        project_id: str | None = None,
    ) -> v2.SuccessorTransitionPageV2:
        active = self._active_project(project_id)
        page = self._json(
            "GET",
            f"/v2/projects/{_segment(active, self._private_values())}/transitions",
            v2.SuccessorTransitionPageV2,
            params=_page_query(limit, after, direction=direction),
            expected_status=200,
        )
        for transition in page.items:
            self._register_transition(transition)
        return page

    @_generation_bound
    def get_transition(self, successor_transition_id: str) -> v2.SuccessorTransitionV2:
        transition_id = _opaque_request(
            successor_transition_id,
            self._private_values(),
        )
        transition = self._json(
            "GET",
            f"/v2/transitions/{quote(transition_id, safe='')}",
            v2.SuccessorTransitionV2,
            expected_status=200,
        )
        if transition.transition.successor_transition_id != transition_id:
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_RESPONSE, 502)
        self._register_transition(transition)
        return transition

    @_mutation_bound
    def retry_transition(
        self,
        successor_transition_id: str,
        request: v2.ActionRequestV2,
        *,
        idempotency_key: str,
    ) -> v2.OperationV2:
        return self._transition_action(
            successor_transition_id,
            "retry",
            request,
            idempotency_key=idempotency_key,
        )

    @_mutation_bound
    def abandon_transition(
        self,
        successor_transition_id: str,
        request: v2.ActionRequestV2,
        *,
        idempotency_key: str,
    ) -> v2.OperationV2:
        return self._transition_action(
            successor_transition_id,
            "abandon",
            request,
            idempotency_key=idempotency_key,
        )

    def _transition_action(
        self,
        successor_transition_id: str,
        action: Literal["retry", "abandon"],
        request: v2.ActionRequestV2,
        *,
        idempotency_key: str,
    ) -> v2.OperationV2:
        transition_id = _opaque_request(successor_transition_id, self._private_values())
        self._require_transition(transition_id)
        result = self._mutation(
            "POST",
            f"/v2/transitions/{quote(transition_id, safe='')}/{action}",
            request,
            v2.ActionRequestV2,
            v2.OperationV2,
            idempotency_key=idempotency_key,
            expected_status=202,
        )
        self._register_operation(result)
        return result

    @_generation_bound
    def list_tasks(
        self,
        *,
        limit: int = 50,
        after: str | None = None,
        direction: Literal["asc", "desc"] = "desc",
        project_id: str | None = None,
    ) -> v2.TaskPageV2:
        active = self._active_project(project_id)
        params = list(_page_query(limit, after, direction=direction))
        params.append(("project_id", active))
        page = self._json(
            "GET",
            "/v2/tasks",
            v2.TaskPageV2,
            params=tuple(params),
            expected_status=200,
        )
        for task in page.items:
            self._register_task(task)
        return page

    @_mutation_bound
    def submit_task(
        self,
        request: v2.TaskSubmitRequestV2,
        *,
        idempotency_key: str,
    ) -> v2.TaskV2:
        self._ensure_active_project(request.project_id)
        project = self._cache.project
        if project is not None:
            head = project.active_project_head
            if (
                head is None
                or project.admission_etag != request.expected_project_admission_etag
                or head.project_head_id != request.expected_project_head_id
                or head.manifest_sha256 != request.expected_project_head_manifest_sha256
                or project.project_config_sha256 != request.expected_project_config_sha256
            ):
                _raise_local(CoreClientLocalErrorCodeV2.AUTHORITY_DRIFT, 409)
        task = self._mutation(
            "POST",
            "/v2/tasks",
            request,
            v2.TaskSubmitRequestV2,
            v2.TaskV2,
            idempotency_key=idempotency_key,
            expected_status=202,
        )
        self._register_task(task)
        return task

    @_generation_bound
    def get_task(self, task_id: str) -> v2.TaskV2:
        requested = _opaque_request(task_id, self._private_values())
        task = self._json(
            "GET",
            f"/v2/tasks/{quote(requested, safe='')}",
            v2.TaskV2,
            expected_status=200,
        )
        if task.task_id != requested:
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_RESPONSE, 502)
        self._register_task(task)
        return task

    @_generation_bound
    def get_task_admission(self, task_id: str) -> v2.TaskAdmissionRefV2:
        requested = _opaque_request(task_id, self._private_values())
        admission = self._json(
            "GET",
            f"/v2/tasks/{quote(requested, safe='')}/admission",
            v2.TaskAdmissionRefV2,
            expected_status=200,
        )
        if admission.task_id != requested:
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_RESPONSE, 502)
        self._register_admission(admission)
        return admission

    @_generation_bound
    def list_task_attempts(
        self,
        task_id: str,
        *,
        limit: int = 50,
        after: str | None = None,
    ) -> v2.AttemptPageV2:
        requested = _opaque_request(task_id, self._private_values())
        page = self._json(
            "GET",
            f"/v2/tasks/{quote(requested, safe='')}/attempts",
            v2.AttemptPageV2,
            params=_page_query(limit, after),
            expected_status=200,
        )
        for attempt in page.items:
            if attempt.task_id != requested:
                _raise_local(CoreClientLocalErrorCodeV2.INVALID_RESPONSE, 502)
            self._register_attempt(attempt)
        return page

    @_mutation_bound
    def append_task_attempt(
        self,
        task_id: str,
        request: v2.AttemptAppendRequestV2,
        *,
        idempotency_key: str,
    ) -> v2.AttemptRefV2:
        requested = _opaque_request(task_id, self._private_values())
        attempt = self._mutation(
            "POST",
            f"/v2/tasks/{quote(requested, safe='')}/attempts",
            request,
            v2.AttemptAppendRequestV2,
            v2.AttemptRefV2,
            idempotency_key=idempotency_key,
            expected_status=202,
        )
        if attempt.task_id != requested:
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_RESPONSE, 502)
        self._register_attempt(attempt)
        return attempt

    @_generation_bound
    def get_task_attempt(self, task_id: str, attempt_id: str) -> v2.AttemptRefV2:
        task = _opaque_request(task_id, self._private_values())
        attempt_requested = _opaque_request(attempt_id, self._private_values())
        attempt = self._json(
            "GET",
            f"/v2/tasks/{quote(task, safe='')}/attempts/{quote(attempt_requested, safe='')}",
            v2.AttemptRefV2,
            expected_status=200,
        )
        if attempt.task_id != task or attempt.attempt_id != attempt_requested:
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_RESPONSE, 502)
        self._register_attempt(attempt)
        return attempt

    @_mutation_bound
    def cancel_task_attempt(
        self,
        task_id: str,
        attempt_id: str,
        request: v2.TaskActionRequestV2,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> v2.OperationV2:
        task = _opaque_request(task_id, self._private_values())
        attempt = _opaque_request(attempt_id, self._private_values())
        result = self._mutation(
            "POST",
            f"/v2/tasks/{quote(task, safe='')}/attempts/{quote(attempt, safe='')}/cancel",
            request,
            v2.TaskActionRequestV2,
            v2.OperationV2,
            if_match=if_match,
            idempotency_key=idempotency_key,
            expected_status=202,
        )
        self._register_operation(result)
        return result

    @_mutation_bound
    def close_task(
        self,
        task_id: str,
        request: v2.TaskActionRequestV2,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> v2.OperationV2:
        task = _opaque_request(task_id, self._private_values())
        result = self._mutation(
            "POST",
            f"/v2/tasks/{quote(task, safe='')}/close",
            request,
            v2.TaskActionRequestV2,
            v2.OperationV2,
            if_match=if_match,
            idempotency_key=idempotency_key,
            expected_status=202,
        )
        self._register_operation(result)
        return result

    @_generation_bound
    def task_timeline(
        self,
        task_id: str,
        *,
        limit: int = 50,
        after: str | None = None,
    ) -> v2.TimelinePageV2:
        task = _opaque_request(task_id, self._private_values())
        page = self._json(
            "GET",
            f"/v2/tasks/{quote(task, safe='')}/timeline",
            v2.TimelinePageV2,
            params=_page_query(limit, after),
            expected_status=200,
        )
        for event in page.items:
            self._register_event(event, from_sse=False)
        return page

    @_generation_bound
    def task_logs(
        self,
        task_id: str,
        *,
        limit: int = 100,
        after: str | None = None,
    ) -> v2.LogPageV2:
        task = _opaque_request(task_id, self._private_values())
        return self._json(
            "GET",
            f"/v2/tasks/{quote(task, safe='')}/logs",
            v2.LogPageV2,
            params=_page_query(limit, after),
            limit=MAX_CORE_LOG_RESPONSE_BYTES,
            expected_status=200,
        )

    @_generation_bound
    def task_context(self, task_id: str) -> v2.TaskContextV2:
        task = _opaque_request(task_id, self._private_values())
        context = self._json(
            "GET",
            f"/v2/tasks/{quote(task, safe='')}/context",
            v2.TaskContextV2,
            expected_status=200,
        )
        if context.task_id != task:
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_RESPONSE, 502)
        self._register_head(context.project_head)
        self._ensure_active_project(context.workspace_snapshot.project_id)
        return context

    @_generation_bound
    def task_artifacts(
        self,
        task_id: str,
        *,
        limit: int = 50,
        after: str | None = None,
    ) -> v2.ArtifactPageV2:
        task = _opaque_request(task_id, self._private_values())
        page = self._json(
            "GET",
            f"/v2/tasks/{quote(task, safe='')}/artifacts",
            v2.ArtifactPageV2,
            params=_page_query(limit, after),
            expected_status=200,
        )
        for artifact in page.items:
            self._register_artifact(artifact)
        return page

    @_generation_bound
    def get_artifact(
        self,
        artifact_id: str,
        *,
        project_id: str | None = None,
    ) -> v2.ArtifactV2:
        active = self._active_project(project_id)
        requested = _opaque_request(artifact_id, self._private_values())
        artifact = self._json(
            "GET",
            f"/v2/projects/{_segment(active, self._private_values())}"
            f"/artifacts/{quote(requested, safe='')}",
            v2.ArtifactV2,
            expected_status=200,
        )
        if artifact.artifact_id != requested:
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_RESPONSE, 502)
        self._register_artifact(artifact)
        return artifact

    @_generation_bound
    def artifact_content(
        self,
        artifact_id: str,
        *,
        project_id: str | None = None,
    ) -> v2.ArtifactContentV2:
        active = self._active_project(project_id)
        requested = _opaque_request(artifact_id, self._private_values())
        content = self._json(
            "GET",
            f"/v2/projects/{_segment(active, self._private_values())}"
            f"/artifacts/{quote(requested, safe='')}/content",
            v2.ArtifactContentV2,
            limit=MAX_CORE_ARTIFACT_RESPONSE_BYTES,
            expected_status=200,
        )
        if content.artifact.artifact_id != requested:
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_RESPONSE, 502)
        self._register_artifact(content.artifact)
        return content

    @_generation_bound
    def list_services(
        self,
        *,
        limit: int = 50,
        after: str | None = None,
    ) -> v2.ServicePageV2:
        page = self._json(
            "GET",
            "/v2/services",
            v2.ServicePageV2,
            params=_page_query(limit, after),
            expected_status=200,
        )
        for service in page.items:
            self._register_service(service)
        return page

    @_generation_bound
    def get_service(self, service_id: str) -> v2.ServiceV2:
        requested = _opaque_request(service_id, self._private_values())
        service = self._json(
            "GET",
            f"/v2/services/{quote(requested, safe='')}",
            v2.ServiceV2,
            expected_status=200,
        )
        if service.service_id != requested:
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_RESPONSE, 502)
        self._register_service(service)
        return service

    @_mutation_bound
    def restart_service(
        self,
        service_id: str,
        request: v2.ActionRequestV2,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> v2.OperationV2:
        requested = _opaque_request(service_id, self._private_values())
        if requested not in self._cache.services:
            _raise_local(CoreClientLocalErrorCodeV2.SNAPSHOT_REFRESH_REQUIRED, 409)
        result = self._mutation(
            "POST",
            f"/v2/services/{quote(requested, safe='')}/restart",
            request,
            v2.ActionRequestV2,
            v2.OperationV2,
            if_match=if_match,
            idempotency_key=idempotency_key,
            expected_status=202,
        )
        self._register_operation(result)
        return result

    @_generation_bound
    def service_logs(
        self,
        service_id: str,
        *,
        limit: int = 100,
        after: str | None = None,
    ) -> v2.LogPageV2:
        requested = _opaque_request(service_id, self._private_values())
        if requested not in self._cache.services:
            _raise_local(CoreClientLocalErrorCodeV2.SNAPSHOT_REFRESH_REQUIRED, 409)
        return self._json(
            "GET",
            f"/v2/services/{quote(requested, safe='')}/logs",
            v2.LogPageV2,
            params=_page_query(limit, after),
            limit=MAX_CORE_LOG_RESPONSE_BYTES,
            expected_status=200,
        )

    @_generation_bound
    def get_operation(self, operation_id: str) -> v2.OperationV2:
        requested = _opaque_request(operation_id, self._private_values())
        if requested not in self._cache.operations:
            _raise_local(CoreClientLocalErrorCodeV2.SNAPSHOT_REFRESH_REQUIRED, 409)
        operation = self._json(
            "GET",
            f"/v2/operations/{quote(requested, safe='')}",
            v2.OperationV2,
            expected_status=200,
        )
        if operation.operation_id != requested:
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_RESPONSE, 502)
        self._register_operation(operation)
        return operation

    @_mutation_bound
    def cancel_operation(
        self,
        operation_id: str,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> v2.OperationV2:
        requested = _opaque_request(operation_id, self._private_values())
        if requested not in self._cache.operations:
            _raise_local(CoreClientLocalErrorCodeV2.SNAPSHOT_REFRESH_REQUIRED, 409)
        operation = self._no_body_mutation(
            "POST",
            f"/v2/operations/{quote(requested, safe='')}/cancel",
            v2.OperationV2,
            if_match=if_match,
            idempotency_key=idempotency_key,
            expected_status=202,
        )
        if operation.operation_id != requested:
            raise CoreMutationOutcomeUnknownV2
        self._register_operation(operation)
        return operation

    @_mutation_bound
    def create_diagnostic(
        self,
        request: v2.DiagnosticRequestV2,
        *,
        idempotency_key: str,
    ) -> v2.DiagnosticV2:
        if request.scope != "system" and request.resource_id is None:
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_REQUEST, 400)
        diagnostic = self._mutation(
            "POST",
            "/v2/diagnostics",
            request,
            v2.DiagnosticRequestV2,
            v2.DiagnosticV2,
            idempotency_key=idempotency_key,
            expected_status=202,
        )
        self._register_diagnostic(diagnostic)
        return diagnostic

    @_generation_bound
    def get_diagnostic(self, diagnostic_id: str) -> v2.DiagnosticV2:
        requested = _opaque_request(diagnostic_id, self._private_values())
        if requested not in self._cache.diagnostics:
            _raise_local(CoreClientLocalErrorCodeV2.SNAPSHOT_REFRESH_REQUIRED, 409)
        diagnostic = self._json(
            "GET",
            f"/v2/diagnostics/{quote(requested, safe='')}",
            v2.DiagnosticV2,
            expected_status=200,
        )
        if diagnostic.diagnostic_id != requested:
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_RESPONSE, 502)
        self._register_diagnostic(diagnostic)
        return diagnostic

    @_mutation_bound
    def delete_diagnostic(
        self,
        diagnostic_id: str,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> None:
        requested = _opaque_request(diagnostic_id, self._private_values())
        if requested not in self._cache.diagnostics:
            _raise_local(CoreClientLocalErrorCodeV2.SNAPSHOT_REFRESH_REQUIRED, 409)
        self._empty_mutation(
            "DELETE",
            f"/v2/diagnostics/{quote(requested, safe='')}",
            if_match=if_match,
            idempotency_key=idempotency_key,
            expected_status=204,
        )
        self._cache.diagnostics.pop(requested, None)

    @_mutation_bound
    def cache_cleanup(
        self,
        request: v2.CacheCleanupRequestV2,
        *,
        idempotency_key: str,
    ) -> v2.OperationV2:
        operation = self._mutation(
            "POST",
            "/v2/maintenance/cache-cleanup",
            request,
            v2.CacheCleanupRequestV2,
            v2.OperationV2,
            idempotency_key=idempotency_key,
            expected_status=202,
        )
        self._register_operation(operation)
        return operation

    @contextmanager
    def events(
        self,
        *,
        last_event_id: str | None = None,
    ) -> Iterator[CoreSseStreamV2]:
        response: httpx.Response | None = None
        generation = -1
        deadline = 0.0
        try:
            with self._generation_lease() as token:
                generation = token.generation
                deadline = token.deadline
                self._require_version_authority()
                headers = self._headers(authenticated=True, accept="text/event-stream")
                if last_event_id is not None:
                    headers["Last-Event-ID"] = _visible_ascii_sse_id(last_event_id)
                response = self._send(
                    "GET",
                    "/v2/events",
                    headers=headers,
                    deadline=deadline,
                )
                if response.is_redirect:
                    _raise_local(CoreClientLocalErrorCodeV2.REDIRECT_REJECTED, 502)
                if response.status_code != 200:
                    self._raise_http_error(response, deadline=deadline)
                _require_content_type(response, "text/event-stream")
                declared = _bounded_content_length(response, MAX_CORE_SSE_RESPONSE_BYTES)
                self._linearize_generation_result(generation, deadline)
            assert response is not None
            yield CoreSseStreamV2(
                client=self,
                chunks=_iter_response_bytes(response, deadline=deadline),
                declared_length=declared,
                generation=generation,
                deadline=deadline,
            )
        except CoreClientErrorV2:
            raise
        except (httpx.HTTPError, OSError, RuntimeError, ValueError):
            if self._close_started():
                _raise_local(CoreClientLocalErrorCodeV2.CLIENT_CLOSED, 503)
            _raise_local(CoreClientLocalErrorCodeV2.CONNECTION_FAILED, 503)
        finally:
            if response is not None:
                self._release_response(response)

    def _mutation(
        self,
        method: str,
        path: str,
        request: BaseModel,
        request_model: type[ModelT],
        response_model: type[ResponseT],
        *,
        idempotency_key: str,
        expected_status: int,
        if_match: str | None = None,
        limit: int = MAX_CORE_JSON_RESPONSE_BYTES,
    ) -> ResponseT:
        body = _encode_request(
            request,
            request_model,
            MAX_CORE_REQUEST_BYTES,
            self._private_values(),
        )
        headers = self._mutation_headers(
            idempotency_key=idempotency_key,
            if_match=if_match,
        )
        headers["Content-Type"] = "application/json"
        return self._request_model(
            method,
            path,
            response_model,
            headers=headers,
            content=body,
            expected_status=expected_status,
            limit=limit,
            mutation=True,
        )

    def _binary_mutation(
        self,
        method: str,
        path: str,
        body: bytes,
        response_model: type[ResponseT],
        *,
        headers: Mapping[str, str],
        if_match: str,
        idempotency_key: str,
        expected_status: int,
    ) -> ResponseT:
        if type(body) is not bytes or len(body) > MAX_CORE_WORKSPACE_CHUNK_REQUEST_BYTES:
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_REQUEST, 400)
        request_headers = self._mutation_headers(
            idempotency_key=idempotency_key,
            if_match=if_match,
        )
        request_headers.update(_safe_extra_headers(headers, self._private_values()))
        request_headers["Content-Type"] = "application/octet-stream"
        return self._request_model(
            method,
            path,
            response_model,
            headers=request_headers,
            content=body,
            expected_status=expected_status,
            limit=MAX_CORE_JSON_RESPONSE_BYTES,
            mutation=True,
        )

    def _no_body_mutation(
        self,
        method: str,
        path: str,
        response_model: type[ResponseT],
        *,
        if_match: str | None,
        idempotency_key: str,
        expected_status: int,
    ) -> ResponseT:
        return self._request_model(
            method,
            path,
            response_model,
            headers=self._mutation_headers(
                idempotency_key=idempotency_key,
                if_match=if_match,
            ),
            content=None,
            expected_status=expected_status,
            limit=MAX_CORE_JSON_RESPONSE_BYTES,
            mutation=True,
        )

    def _empty_mutation(
        self,
        method: str,
        path: str,
        *,
        if_match: str | None,
        idempotency_key: str,
        expected_status: int,
    ) -> None:
        token = self._current_generation_token()
        self._require_version_authority()
        response: httpx.Response | None = None
        sent = False
        try:

            def mark_sent() -> None:
                nonlocal sent
                sent = True

            response = self._send(
                method,
                path,
                headers=self._headers(authenticated=True, accept="application/json")
                | self._mutation_headers(
                    idempotency_key=idempotency_key,
                    if_match=if_match,
                ),
                deadline=token.deadline,
                on_send_started=mark_sent,
            )
            if response.is_redirect:
                _raise_local(CoreClientLocalErrorCodeV2.REDIRECT_REJECTED, 502)
            if response.status_code != expected_status:
                self._raise_http_error(response, deadline=token.deadline)
            declared = _bounded_content_length(response, 0)
            body = _read_bounded(response, 0, deadline=token.deadline)
            if declared not in {None, 0} or body:
                raise CoreMutationOutcomeUnknownV2
        except CoreMutationOutcomeUnknownV2:
            raise
        except CoreClientErrorV2:
            raise
        except (httpx.HTTPError, OSError, RuntimeError, ValueError):
            if sent:
                raise CoreMutationOutcomeUnknownV2 from None
            self._raise_transport_error()
        finally:
            if response is not None:
                self._release_response(response)

    def _json(
        self,
        method: str,
        path: str,
        response_model: type[ResponseT],
        *,
        authenticated: bool = True,
        params: tuple[tuple[str, str], ...] = (),
        limit: int = MAX_CORE_JSON_RESPONSE_BYTES,
        expected_status: int,
    ) -> ResponseT:
        return self._request_model(
            method,
            path,
            response_model,
            headers=self._headers(
                authenticated=authenticated,
                accept="application/json",
            ),
            params=params,
            content=None,
            expected_status=expected_status,
            limit=limit,
            mutation=False,
            authenticated=authenticated,
        )

    def _request_model(
        self,
        method: str,
        path: str,
        response_model: type[ResponseT],
        *,
        headers: Mapping[str, str],
        content: bytes | None,
        expected_status: int,
        limit: int,
        mutation: bool,
        params: tuple[tuple[str, str], ...] = (),
        authenticated: bool = True,
    ) -> ResponseT:
        token = self._current_generation_token()
        if authenticated:
            self._require_version_authority()
        response: httpx.Response | None = None
        sent = False
        try:

            def mark_sent() -> None:
                nonlocal sent
                sent = True

            response = self._send(
                method,
                path,
                headers=headers,
                params=params,
                content=content,
                deadline=token.deadline,
                on_send_started=mark_sent,
            )
            if response.is_redirect:
                _raise_local(CoreClientLocalErrorCodeV2.REDIRECT_REJECTED, 502)
            if response.status_code != expected_status:
                self._raise_http_error(response, deadline=token.deadline)
            result = self._parse_json_response(
                response,
                response_model,
                limit=limit,
                deadline=token.deadline,
            )
            self._ensure_session_generation(token.generation)
            return result
        except CoreMutationOutcomeUnknownV2:
            raise
        except CoreClientErrorV2 as exc:
            if (
                mutation
                and sent
                and (
                    response is None
                    or 200 <= response.status_code < 300
                )
            ):
                raise CoreMutationOutcomeUnknownV2 from None
            raise exc
        except (httpx.HTTPError, OSError, RuntimeError, TypeError, UnicodeError, ValueError):
            if mutation and sent:
                raise CoreMutationOutcomeUnknownV2 from None
            self._raise_transport_error()
        finally:
            if response is not None:
                self._release_response(response)

    def _parse_json_response(
        self,
        response: httpx.Response,
        response_model: type[ResponseT],
        *,
        limit: int,
        deadline: float,
    ) -> ResponseT:
        _require_content_type(response, "application/json")
        body = _read_bounded(response, limit, deadline=deadline)
        value = _decode_json_document(
            body,
            private_values=self._private_values(),
            error_code=CoreClientLocalErrorCodeV2.INVALID_RESPONSE,
        )
        try:
            adapter = TypeAdapter(response_model)
            if not _json_matches_schema_types(
                value, adapter.json_schema(mode="validation")
            ):
                raise ValueError("Core response JSON types differ from the contract")
            # JSON arrays are the canonical wire form of tuple-backed framework
            # contracts. Scalar/container strictness is enforced above before
            # allowing that one representation conversion.
            result = adapter.validate_json(body)
        except (TypeError, ValidationError, ValueError, RecursionError):
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_RESPONSE, 502)
        del value
        return cast(ResponseT, result)

    def _raise_http_error(self, response: httpx.Response, *, deadline: float) -> NoReturn:
        if response.is_redirect:
            _raise_local(CoreClientLocalErrorCodeV2.REDIRECT_REJECTED, 502)
        try:
            _require_content_type(
                response,
                "application/json",
                error_code=CoreClientLocalErrorCodeV2.INVALID_ERROR_RESPONSE,
            )
            body = _read_bounded(
                response,
                MAX_CORE_ERROR_RESPONSE_BYTES,
                deadline=deadline,
            )
            value = _decode_json_document(
                body,
                private_values=self._private_values(),
                error_code=CoreClientLocalErrorCodeV2.INVALID_ERROR_RESPONSE,
            )
            adapter = TypeAdapter(v2.ApiErrorV2)
            if not _json_matches_schema_types(
                value, adapter.json_schema(mode="validation")
            ):
                raise ValueError("Core error JSON types differ from the contract")
            error = adapter.validate_json(body)
            if error.http_status != response.status_code:
                raise ValueError("Core error status mismatch")
        except CoreClientErrorV2:
            raise
        except (TypeError, ValidationError, ValueError):
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_ERROR_RESPONSE, 502)
        raise CoreClientErrorV2(response.status_code, error)

    def _send(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        deadline: float,
        params: tuple[tuple[str, str], ...] = (),
        content: bytes | None = None,
        on_send_started: Callable[[], None] | None = None,
    ) -> httpx.Response:
        token = self._current_generation_token()
        _check_deadline(deadline)
        _validate_request_target(path, params, headers, self._private_values())
        try:
            request = self._http.build_request(
                method,
                path,
                headers=dict(headers),
                params=params,
                content=content,
            )
        except (httpx.HTTPError, TypeError, ValueError):
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_REQUEST, 400)
        reservation = _PROCESS_RESOURCE_CLOSER.reserve()
        if reservation is None:
            _raise_local(CoreClientLocalErrorCodeV2.CONNECTION_FAILED, 503)
        future = _PROCESS_BLOCKING_IO.submit(lambda: self._http.send(request, stream=True))
        if future is None:
            reservation.release()
            _raise_local(CoreClientLocalErrorCodeV2.CONNECTION_FAILED, 503)
        if on_send_started is not None:
            on_send_started()
        try:
            response = future.result(timeout=_remaining_deadline(deadline))
        except FutureTimeoutError:

            def close_late(completed: Future[httpx.Response]) -> None:
                try:
                    late = completed.result()
                except BaseException:
                    reservation.release()
                else:
                    self._schedule_close(late.close, reservation)

            future.add_done_callback(close_late)
            _raise_local(CoreClientLocalErrorCodeV2.CONNECTION_FAILED, 503)
        except BaseException as exc:
            reservation.release()
            if not isinstance(exc, Exception):
                raise
            self._raise_transport_error()
        self._register_response(response, token.generation, reservation)
        try:
            self._ensure_response_origin(response)
        except BaseException:
            self._release_response(response)
            raise
        return response

    def _register_response(
        self,
        response: httpx.Response,
        generation: int,
        reservation: _CloseReservation,
    ) -> None:
        close_late = False
        with self._state:
            if (
                self._closing
                or self._closed
                or self._close_failed
                or generation != self._session_generation
            ):
                close_late = True
            else:
                self._active_responses[response] = reservation
        if close_late:
            self._schedule_close(response.close, reservation)
            _raise_local(CoreClientLocalErrorCodeV2.CLIENT_CLOSED, 503)

    def _release_response(self, response: httpx.Response) -> bool:
        with self._state:
            reservation = self._active_responses.pop(response, None)
        return reservation is None or self._schedule_close(response.close, reservation)

    def _ensure_response_origin(self, response: httpx.Response) -> None:
        try:
            origin = f"{response.url.scheme}://{response.url.host}:{response.url.port}"
        except (AttributeError, TypeError, ValueError):
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_RESPONSE, 502)
        if origin != self._connection.origin:
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_RESPONSE, 502)
        encoding = response.headers.get("content-encoding")
        if encoding is not None and encoding.strip().lower() != "identity":
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_RESPONSE, 502)

    def _headers(self, *, authenticated: bool, accept: str) -> dict[str, str]:
        headers = {"Accept": accept}
        if authenticated:
            headers["Authorization"] = f"Bearer {self._connection.bearer_token}"
        return headers

    def _mutation_headers(
        self,
        *,
        idempotency_key: str,
        if_match: str | None,
    ) -> dict[str, str]:
        key = _visible_ascii_header(idempotency_key)
        _scan_private_strings(
            key,
            self._private_values(),
            CoreClientLocalErrorCodeV2.INVALID_REQUEST,
            400,
        )
        headers = self._headers(authenticated=True, accept="application/json")
        headers["Idempotency-Key"] = key
        if if_match is not None:
            headers["If-Match"] = _etag(if_match)
        return headers

    def _raise_transport_error(self) -> NoReturn:
        if self._close_started():
            _raise_local(CoreClientLocalErrorCodeV2.CLIENT_CLOSED, 503)
        _raise_local(CoreClientLocalErrorCodeV2.CONNECTION_FAILED, 503)

    def _require_version_authority(self) -> v2.VersionResponseV2:
        version = self._cache.version
        if version is None:
            _raise_local(CoreClientLocalErrorCodeV2.NEGOTIATION_REQUIRED, 409)
        return version

    def _validate_cached_authority(self) -> None:
        version = self._require_version_authority()
        project = self._cache.project
        if project is not None:
            self._validate_project_authority(project, version)
        for head in self._cache.heads.values():
            self._validate_head_authority(head, version)
        for capability in self._cache.capabilities.values():
            if capability.registry_digest != version.registry_sha256:
                _raise_local(CoreClientLocalErrorCodeV2.AUTHORITY_DRIFT, 502)

    def _active_project(self, project_id: str | None) -> str:
        if project_id is None:
            return self._connection.project_id
        requested = _opaque_request(project_id, self._private_values())
        self._ensure_active_project(requested)
        return requested

    def _ensure_active_project(self, project_id: str) -> None:
        if project_id != self._connection.project_id:
            _raise_local(CoreClientLocalErrorCodeV2.ACTIVE_PROJECT_MISMATCH, 409)

    def _validate_head_authority(
        self,
        head: v2.ProjectHeadRefV2,
        version: v2.VersionResponseV2 | None = None,
    ) -> None:
        self._ensure_active_project(head.project_id)
        negotiated = version or self._require_version_authority()
        if (
            head.registry_sha256 != negotiated.registry_sha256
            or head.runtime_context_snapshot.runtime_contract_sha256
            != negotiated.runtime_contract_sha256
        ):
            _raise_local(CoreClientLocalErrorCodeV2.AUTHORITY_DRIFT, 502)

    def _validate_project_authority(
        self,
        project: v2.ProjectV2,
        version: v2.VersionResponseV2 | None = None,
    ) -> None:
        self._ensure_active_project(project.project_id)
        if project.active_project_head is not None:
            self._validate_head_authority(project.active_project_head, version)

    def _register_project(self, project: v2.ProjectV2) -> None:
        if type(project) is not v2.ProjectV2:
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_RESPONSE, 502)
        self._validate_project_authority(project)
        previous = self._cache.project
        if previous is not None:
            if previous.created_at != project.created_at:
                _raise_local(CoreClientLocalErrorCodeV2.AUTHORITY_DRIFT, 502)
            if previous.etag == project.etag and previous != project:
                _raise_local(CoreClientLocalErrorCodeV2.AUTHORITY_DRIFT, 502)
            old_head = previous.active_project_head
            new_head = project.active_project_head
            if old_head is not None and new_head is not None:
                if new_head.generation < old_head.generation:
                    _raise_local(CoreClientLocalErrorCodeV2.AUTHORITY_DRIFT, 502)
                if new_head.generation == old_head.generation and new_head != old_head:
                    _raise_local(CoreClientLocalErrorCodeV2.AUTHORITY_DRIFT, 502)
                if new_head.generation == old_head.generation + 1 and (
                    new_head.predecessor_project_head_id != old_head.project_head_id
                ):
                    _raise_local(CoreClientLocalErrorCodeV2.AUTHORITY_DRIFT, 502)
                if new_head.generation > old_head.generation + 1:
                    _raise_local(CoreClientLocalErrorCodeV2.SNAPSHOT_REFRESH_REQUIRED, 409)
        self._cache.project = project
        if project.active_project_head is not None:
            self._register_head(project.active_project_head)

    def _register_head(self, head: v2.ProjectHeadRefV2) -> None:
        if type(head) is not v2.ProjectHeadRefV2:
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_RESPONSE, 502)
        self._validate_head_authority(head)
        existing = self._cache.heads.get(head.project_head_id)
        if existing is not None and existing != head:
            _raise_local(CoreClientLocalErrorCodeV2.AUTHORITY_DRIFT, 502)
        same_generation = [
            item
            for item in self._cache.heads.values()
            if item.generation == head.generation and item.project_head_id != head.project_head_id
        ]
        if same_generation:
            _raise_local(CoreClientLocalErrorCodeV2.AUTHORITY_DRIFT, 502)
        predecessor = (
            None
            if head.predecessor_project_head_id is None
            else self._cache.heads.get(head.predecessor_project_head_id)
        )
        if predecessor is not None and predecessor.generation + 1 != head.generation:
            _raise_local(CoreClientLocalErrorCodeV2.AUTHORITY_DRIFT, 502)
        self._cache.heads[head.project_head_id] = head

    def _register_upload(
        self,
        upload: v2.WorkspaceUploadSessionV2,
        *,
        expected_id: str | None,
        previous: v2.WorkspaceUploadSessionV2 | None,
    ) -> None:
        self._ensure_active_project(upload.project_id)
        if expected_id is not None and upload.upload_id != expected_id:
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_RESPONSE, 502)
        cached = self._cache.uploads.get(upload.upload_id)
        prior = previous or cached
        if prior is not None:
            stable = (
                prior.upload_id,
                prior.project_id,
                prior.expected_project_head_id,
                prior.expected_project_head_manifest_sha256,
                prior.expected_project_config_sha256,
                prior.archive,
                prior.chunk_byte_size,
                prior.chunk_count,
                prior.created_at,
            )
            current = (
                upload.upload_id,
                upload.project_id,
                upload.expected_project_head_id,
                upload.expected_project_head_manifest_sha256,
                upload.expected_project_config_sha256,
                upload.archive,
                upload.chunk_byte_size,
                upload.chunk_count,
                upload.created_at,
            )
            if stable != current:
                _raise_local(CoreClientLocalErrorCodeV2.AUTHORITY_DRIFT, 502)
            if upload.next_chunk_index < prior.next_chunk_index:
                _raise_local(CoreClientLocalErrorCodeV2.AUTHORITY_DRIFT, 502)
            if upload.etag == prior.etag and upload != prior:
                _raise_local(CoreClientLocalErrorCodeV2.AUTHORITY_DRIFT, 502)
        self._cache.uploads[upload.upload_id] = upload

    def _require_upload(self, upload_id: str) -> v2.WorkspaceUploadSessionV2:
        requested = _opaque_request(upload_id, self._private_values())
        upload = self._cache.uploads.get(requested)
        if upload is None:
            _raise_local(CoreClientLocalErrorCodeV2.SNAPSHOT_REFRESH_REQUIRED, 409)
        return upload

    def _register_transition(self, transition: v2.SuccessorTransitionV2) -> None:
        ref = transition.transition
        self._ensure_active_project(ref.project_id)
        self._validate_head_authority(ref.predecessor_project_head)
        if ref.successor_project_head is not None:
            self._register_head(ref.successor_project_head)
        existing = self._cache.transitions.get(ref.successor_transition_id)
        if existing is not None:
            if existing.transition != ref or existing.created_at != transition.created_at:
                _raise_local(CoreClientLocalErrorCodeV2.AUTHORITY_DRIFT, 502)
            if transition.updated_at < existing.updated_at:
                _raise_local(CoreClientLocalErrorCodeV2.AUTHORITY_DRIFT, 502)
        self._cache.transitions[ref.successor_transition_id] = transition
        self._cache.transition_ids.add(ref.successor_transition_id)

    def _require_transition(self, transition_id: str) -> None:
        if transition_id not in self._cache.transition_ids:
            _raise_local(CoreClientLocalErrorCodeV2.SNAPSHOT_REFRESH_REQUIRED, 409)

    def _register_task(self, task: v2.TaskV2) -> None:
        self._ensure_active_project(task.project_id)
        self._register_admission(task.admission)
        for attempt in task.attempts:
            self._register_attempt(attempt)
        if task.successor_transition is not None:
            self._cache.transition_ids.add(task.successor_transition.successor_transition_id)
            self._validate_head_authority(task.successor_transition.predecessor_project_head)
        existing = self._cache.tasks.get(task.task_id)
        if existing is not None:
            if existing.admission != task.admission or existing.created_at != task.created_at:
                _raise_local(CoreClientLocalErrorCodeV2.AUTHORITY_DRIFT, 502)
            if task.attempts[: len(existing.attempts)] != existing.attempts:
                _raise_local(CoreClientLocalErrorCodeV2.AUTHORITY_DRIFT, 502)
            if task.etag == existing.etag and task != existing:
                _raise_local(CoreClientLocalErrorCodeV2.AUTHORITY_DRIFT, 502)
        self._cache.tasks[task.task_id] = task
        self._cache.task_ids.add(task.task_id)

    def _register_admission(self, admission: v2.TaskAdmissionRefV2) -> None:
        self._ensure_active_project(admission.project_id)
        self._validate_head_authority(admission.predecessor_project_head)
        self._cache.task_ids.add(admission.task_id)

    def _register_attempt(self, attempt: v2.AttemptRefV2) -> None:
        self._ensure_active_project(attempt.project_id)
        existing = self._cache.attempts.get(attempt.attempt_id)
        if existing is not None and existing != attempt:
            _raise_local(CoreClientLocalErrorCodeV2.AUTHORITY_DRIFT, 502)
        self._cache.attempts[attempt.attempt_id] = attempt
        self._cache.task_ids.add(attempt.task_id)

    def _register_artifact(self, artifact: v2.ArtifactV2) -> None:
        self._ensure_active_project(artifact.project_id)
        existing = self._cache.artifacts.get(artifact.artifact_id)
        if existing is not None and existing != artifact:
            _raise_local(CoreClientLocalErrorCodeV2.AUTHORITY_DRIFT, 502)
        self._cache.artifacts[artifact.artifact_id] = artifact

    def _register_service(self, service: v2.ServiceV2) -> None:
        existing = self._cache.services.get(service.service_id)
        if existing is not None:
            if service.etag == existing.etag and service != existing:
                _raise_local(CoreClientLocalErrorCodeV2.AUTHORITY_DRIFT, 502)
            if service.updated_at < existing.updated_at:
                _raise_local(CoreClientLocalErrorCodeV2.AUTHORITY_DRIFT, 502)
        self._cache.services[service.service_id] = service

    def _register_operation(self, operation: v2.OperationV2) -> None:
        existing = self._cache.operations.get(operation.operation_id)
        if existing is not None:
            if existing.kind != operation.kind or existing.created_at != operation.created_at:
                _raise_local(CoreClientLocalErrorCodeV2.AUTHORITY_DRIFT, 502)
            if operation.etag == existing.etag and operation != existing:
                _raise_local(CoreClientLocalErrorCodeV2.AUTHORITY_DRIFT, 502)
        self._cache.operations[operation.operation_id] = operation

    def _register_diagnostic(self, diagnostic: v2.DiagnosticV2) -> None:
        existing = self._cache.diagnostics.get(diagnostic.diagnostic_id)
        if existing is not None:
            if (
                existing.scope != diagnostic.scope
                or existing.resource_id != diagnostic.resource_id
                or existing.created_at != diagnostic.created_at
                or (existing.etag == diagnostic.etag and existing != diagnostic)
            ):
                _raise_local(CoreClientLocalErrorCodeV2.AUTHORITY_DRIFT, 502)
        self._cache.diagnostics[diagnostic.diagnostic_id] = diagnostic

    def _deliver_sse_frame(
        self,
        frame: v2.SseFrameV2,
        *,
        expected_generation: int,
        deadline: float,
    ) -> None:
        with self._generation_lease(
            expected_generation=expected_generation,
            deadline=deadline,
        ) as token:
            with self._cache_transaction(token):
                self._register_event(frame.data, from_sse=True)
                self._linearize_generation_result(token.generation, token.deadline)

    def _register_event(
        self,
        event: v2.EventEnvelopeV2,
        *,
        from_sse: bool,
    ) -> None:
        self._ensure_active_project(event.project_id)
        digest: str | None = None
        if from_sse:
            digest = hashlib.sha256(_canonical_model_bytes(event)).hexdigest()
            previous_digest = self._cache.event_digests.get(event.event_id)
            if previous_digest is not None:
                if previous_digest != digest:
                    _raise_local(CoreClientLocalErrorCodeV2.SSE_PROTOCOL_ERROR, 502)
                return
            if len(self._cache.event_digests) >= MAX_CORE_SSE_EVENT_BINDINGS:
                _raise_local(CoreClientLocalErrorCodeV2.SSE_PROTOCOL_ERROR, 502)
            if event.sequence <= self._cache.maximum_event_sequence:
                _raise_local(CoreClientLocalErrorCodeV2.SSE_PROTOCOL_ERROR, 502)
        if isinstance(event, v2.TaskAdmittedEventV2):
            self._register_admission(event.admission)
        elif isinstance(event, v2.AttemptAppendedEventV2):
            self._register_attempt(event.attempt)
        elif isinstance(event, v2.TransitionChangedEventV2):
            ref = event.transition
            self._validate_head_authority(ref.predecessor_project_head)
            self._cache.transition_ids.add(ref.successor_transition_id)
        elif isinstance(event, v2.EvolutionRevisionCommittedEventV2):
            self._ensure_active_project(event.evolution_revision.project_id)
        elif isinstance(event, v2.RuntimeContextCommittedEventV2):
            context = event.runtime_context_snapshot
            self._ensure_active_project(context.project_id)
            version = self._require_version_authority()
            if (
                context.registry_sha256 != version.registry_sha256
                or context.runtime_contract_sha256 != version.runtime_contract_sha256
            ):
                _raise_local(CoreClientLocalErrorCodeV2.AUTHORITY_DRIFT, 502)
        elif isinstance(event, v2.ProjectHeadActivatedEventV2):
            self._register_head(event.project_head)
        if from_sse:
            assert digest is not None
            self._cache.event_digests[event.event_id] = digest
            self._cache.event_sequences[event.event_id] = event.sequence
            self._cache.event_order.append(event.event_id)
            self._cache.maximum_event_sequence = event.sequence


class CoreProjectBootstrapClientV2:
    """One-shot negotiated project creation surface for a private tunnel."""

    def __init__(
        self,
        connection: CoreBootstrapTunnelConnectionV2,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float | httpx.Timeout = 30.0,
    ) -> None:
        if type(connection) is not CoreBootstrapTunnelConnectionV2:
            raise TypeError("connection must be an exact CoreBootstrapTunnelConnectionV2")
        self._connection = connection
        self._client = CoreControlClientV2(
            connection._temporary_binding(),
            transport=transport,
            timeout=timeout,
        )
        self._create_lock = threading.Lock()
        self._submitted_request: v2.ProjectCreateV2 | None = None
        self._submitted_idempotency_key: str | None = None
        self._delivered_result: CoreProjectBootstrapResultV2 | None = None

    def __enter__(self) -> CoreProjectBootstrapClientV2:
        self._client._ensure_open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return "CoreProjectBootstrapClientV2(<private>)"

    def close(self) -> None:
        self._client.close()

    def version(self) -> v2.VersionResponseV2:
        return self._client.version()

    def health(self) -> v2.HealthResponseV2:
        return self._client.health()

    def system_status(self) -> v2.SystemStatusV2:
        return self._client.system_status()

    def capabilities(self, execution_mode: v2.ExecutionModeV2) -> v2.CapabilitiesResponseV2:
        return self._client.capabilities(execution_mode)

    def create_project(
        self,
        request: v2.ProjectCreateV2,
        *,
        idempotency_key: str,
    ) -> CoreProjectBootstrapResultV2:
        deadline = time.monotonic() + self._client._request_deadline_seconds
        _encode_request(
            request,
            v2.ProjectCreateV2,
            MAX_CORE_REQUEST_BYTES,
            self._client._private_values(),
        )
        normalized_key = self._client._mutation_headers(
            idempotency_key=idempotency_key,
            if_match=None,
        )["Idempotency-Key"]
        remaining = _remaining_deadline(deadline)
        if not self._create_lock.acquire(timeout=remaining):
            _raise_local(CoreClientLocalErrorCodeV2.CONNECTION_FAILED, 503)
        try:
            with self._client._generation_lease(deadline=deadline) as token:
                self._client._require_version_authority()
                if self._submitted_request is not None and (
                    request != self._submitted_request
                    or normalized_key != self._submitted_idempotency_key
                ):
                    _raise_local(CoreClientLocalErrorCodeV2.INVALID_REQUEST, 400)
                if self._delivered_result is not None:
                    return self._deliver(token, self._delivered_result, commit=False)
                self._submitted_request = request
                self._submitted_idempotency_key = normalized_key
                project = self._client._mutation(
                    "POST",
                    "/v2/projects",
                    request,
                    v2.ProjectCreateV2,
                    v2.ProjectV2,
                    idempotency_key=normalized_key,
                    expected_status=201,
                )
                _ensure_project_create_response(
                    request,
                    project,
                    self._client._require_version_authority(),
                )
                result = CoreProjectBootstrapResultV2(
                    project=project,
                    connection=self._connection.bind(project.project_id),
                )
                return self._deliver(token, result, commit=True)
        finally:
            self._create_lock.release()

    def _deliver(
        self,
        token: _GenerationLeaseToken,
        result: CoreProjectBootstrapResultV2,
        *,
        commit: bool,
    ) -> CoreProjectBootstrapResultV2:
        with self._client._delivery_lock:
            _check_deadline(token.deadline)
            with self._client._state:
                if (
                    self._client._closing
                    or self._client._closed
                    or self._client._close_failed
                    or token.generation != self._client._session_generation
                ):
                    _raise_local(CoreClientLocalErrorCodeV2.CLIENT_CLOSED, 503)
                if commit:
                    self._delivered_result = result
                self._client._release_generation_token_locked(token)
        return result


def _ensure_project_create_response(
    request: v2.ProjectCreateV2,
    project: v2.ProjectV2,
    version: v2.VersionResponseV2,
) -> None:
    head = project.active_project_head
    if (
        project.display_name != request.display_name
        or project.config != request.config
        or project.project_config_sha256 != v2.project_config_sha256_for(request.config)
        or head is None
        or project.admission_etag is None
        or project.state != "ready"
        or head.project_id != project.project_id
        or head.generation != 0
        or head.predecessor_project_head_id is not None
        or head.registry_sha256 != version.registry_sha256
        or head.runtime_context_snapshot.runtime_contract_sha256 != version.runtime_contract_sha256
    ):
        _raise_local(CoreClientLocalErrorCodeV2.INVALID_RESPONSE, 502)


def _encode_request(
    request: BaseModel,
    request_model: type[BaseModel],
    limit: int,
    private_values: tuple[str, ...],
) -> bytes:
    if type(request) is not request_model:
        _raise_local(CoreClientLocalErrorCodeV2.INVALID_REQUEST, 400)
    try:
        value = request.model_dump(mode="json")
        _scan_private_strings(
            value,
            private_values,
            CoreClientLocalErrorCodeV2.INVALID_REQUEST,
            400,
        )
        body = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, UnicodeError, ValueError, RecursionError):
        _raise_local(CoreClientLocalErrorCodeV2.INVALID_REQUEST, 400)
    if len(body) > limit:
        _raise_local(CoreClientLocalErrorCodeV2.INVALID_REQUEST, 400)
    return body


def _decode_json_document(
    body: bytes,
    *,
    private_values: tuple[str, ...],
    error_code: CoreClientLocalErrorCodeV2,
) -> object:
    if not body or _json_nesting_exceeds(body, MAX_JSON_NESTING):
        _raise_local(error_code, 502)

    def exact_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON object member")
            value[key] = item
        return value

    try:
        value = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=exact_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (TypeError, UnicodeError, ValueError, RecursionError):
        _raise_local(error_code, 502)
    _scan_private_strings(value, private_values, error_code, 502)
    return value


def _canonical_model_bytes(value: BaseModel) -> bytes:
    return json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _scan_private_strings(
    value: object,
    private_values: tuple[str, ...],
    code: CoreClientLocalErrorCodeV2,
    status_code: int,
) -> None:
    folded = tuple(item.casefold() for item in private_values if item)
    stack = [value]
    visited = 0
    while stack:
        current = stack.pop()
        visited += 1
        if visited > 262_144:
            _raise_local(code, status_code)
        if type(current) is str:
            candidate = current.casefold()
            if any(item in candidate for item in folded):
                _raise_local(code, status_code)
        elif isinstance(current, Mapping):
            stack.extend(current.keys())
            stack.extend(current.values())
        elif isinstance(current, (list, tuple)):
            stack.extend(current)


def _safe_extra_headers(
    headers: Mapping[str, str], private_values: tuple[str, ...]
) -> dict[str, str]:
    if type(headers) is not dict:
        _raise_local(CoreClientLocalErrorCodeV2.INVALID_REQUEST, 400)
    result: dict[str, str] = {}
    for name, value in headers.items():
        if type(name) is not str or type(value) is not str:
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_REQUEST, 400)
        normalized = name.lower()
        if normalized not in {
            "x-openevo-chunk-sha256",
            "x-openevo-chunk-byte-size",
        }:
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_REQUEST, 400)
        safe = _visible_ascii_header(value)
        _scan_private_strings(
            safe,
            private_values,
            CoreClientLocalErrorCodeV2.INVALID_REQUEST,
            400,
        )
        result[name] = safe
    return result


def _validate_request_target(
    path: str,
    params: tuple[tuple[str, str], ...],
    headers: Mapping[str, str],
    private_values: tuple[str, ...],
) -> None:
    if (
        type(path) is not str
        or not path.startswith("/")
        or path.startswith("//")
        or "?" in path
        or "#" in path
        or "\\" in path
    ):
        _raise_local(CoreClientLocalErrorCodeV2.INVALID_REQUEST, 400)
    _scan_private_strings(
        path,
        private_values,
        CoreClientLocalErrorCodeV2.INVALID_REQUEST,
        400,
    )
    if type(params) is not tuple:
        _raise_local(CoreClientLocalErrorCodeV2.INVALID_REQUEST, 400)
    for pair in params:
        if (
            type(pair) is not tuple
            or len(pair) != 2
            or type(pair[0]) is not str
            or type(pair[1]) is not str
        ):
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_REQUEST, 400)
        _scan_private_strings(
            pair,
            private_values,
            CoreClientLocalErrorCodeV2.INVALID_REQUEST,
            400,
        )
    for name, value in headers.items():
        if type(name) is not str or type(value) is not str:
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_REQUEST, 400)
        if name.lower() == "authorization":
            continue
        _scan_private_strings(
            (name, value),
            private_values,
            CoreClientLocalErrorCodeV2.INVALID_REQUEST,
            400,
        )


def _read_bounded(response: httpx.Response, limit: int, *, deadline: float) -> bytes:
    _check_deadline(deadline)
    declared = _bounded_content_length(response, limit)
    chunks: list[bytes] = []
    total = 0
    for chunk in _iter_response_bytes(response, deadline=deadline):
        total += len(chunk)
        if total > limit:
            _raise_local(CoreClientLocalErrorCodeV2.RESPONSE_TOO_LARGE, 502)
        chunks.append(chunk)
    if declared is not None and total != declared:
        _raise_local(CoreClientLocalErrorCodeV2.INVALID_RESPONSE, 502)
    return b"".join(chunks)


def _bounded_content_length(response: httpx.Response, limit: int) -> int | None:
    values = response.headers.get_list("content-length")
    if not values:
        return None
    if len(values) != 1 or re.fullmatch(r"0|[1-9][0-9]*", values[0]) is None:
        _raise_local(CoreClientLocalErrorCodeV2.INVALID_RESPONSE, 502)
    declared = int(values[0])
    if declared > limit:
        _raise_local(CoreClientLocalErrorCodeV2.RESPONSE_TOO_LARGE, 502)
    return declared


_END_OF_RESPONSE = object()


def _call_before_deadline(action: Callable[[], ResponseT], deadline: float) -> ResponseT:
    _check_deadline(deadline)
    future = _PROCESS_BLOCKING_IO.submit(action)
    if future is None:
        _raise_local(CoreClientLocalErrorCodeV2.CONNECTION_FAILED, 503)
    try:
        return future.result(timeout=_remaining_deadline(deadline))
    except FutureTimeoutError:
        future.cancel()
        _raise_local(CoreClientLocalErrorCodeV2.CONNECTION_FAILED, 503)


def _iter_response_bytes(
    response: httpx.Response,
    *,
    deadline: float,
) -> Iterator[bytes]:
    chunks = response.iter_bytes()
    while True:
        chunk = _call_before_deadline(lambda: next(chunks, _END_OF_RESPONSE), deadline)
        if chunk is _END_OF_RESPONSE:
            return
        if type(chunk) is not bytes:
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_RESPONSE, 502)
        yield chunk


def _require_content_type(
    response: httpx.Response,
    expected: str,
    *,
    error_code: CoreClientLocalErrorCodeV2 = CoreClientLocalErrorCodeV2.INVALID_RESPONSE,
) -> None:
    values = response.headers.get_list("content-type")
    if len(values) != 1 or values[0].split(";", 1)[0].strip().lower() != expected:
        _raise_local(error_code, 502)


def _page_query(
    limit: int,
    after: str | None,
    *,
    direction: Literal["asc", "desc"] | None = None,
) -> tuple[tuple[str, str], ...]:
    if type(limit) is not int or not 1 <= limit <= 100:
        _raise_local(CoreClientLocalErrorCodeV2.INVALID_REQUEST, 400)
    result: list[tuple[str, str]] = [("limit", str(limit))]
    if after is not None:
        result.append(("after", _cursor(after)))
    if direction is not None:
        if direction not in {"asc", "desc"}:
            _raise_local(CoreClientLocalErrorCodeV2.INVALID_REQUEST, 400)
        result.append(("direction", direction))
    return tuple(result)


def _execution_mode(value: object) -> str:
    if value not in {"codex_subscription_transcript", "self_deployed"}:
        _raise_local(CoreClientLocalErrorCodeV2.INVALID_REQUEST, 400)
    return cast(str, value)


def _opaque_request(value: str, private_values: tuple[str, ...]) -> str:
    try:
        result = _OPAQUE_ID.validate_python(value, strict=True)
    except ValidationError:
        _raise_local(CoreClientLocalErrorCodeV2.INVALID_REQUEST, 400)
    _scan_private_strings(
        result,
        private_values,
        CoreClientLocalErrorCodeV2.INVALID_REQUEST,
        400,
    )
    return result


def _segment(value: str, private_values: tuple[str, ...]) -> str:
    return quote(_opaque_request(value, private_values), safe="")


def _cursor(value: str) -> str:
    try:
        return _CURSOR.validate_python(value, strict=True)
    except ValidationError:
        _raise_local(CoreClientLocalErrorCodeV2.INVALID_REQUEST, 400)


def _visible_ascii_header(value: str) -> str:
    if (
        type(value) is not str
        or _HEADER_VALUE.fullmatch(value) is None
        or any(not 0x21 <= ord(character) <= 0x7E for character in value)
    ):
        _raise_local(CoreClientLocalErrorCodeV2.INVALID_REQUEST, 400)
    return value


def _visible_ascii_sse_id(value: str) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 128
        or any(not 0x21 <= ord(character) <= 0x7E for character in value)
    ):
        _raise_local(CoreClientLocalErrorCodeV2.SSE_PROTOCOL_ERROR, 502)
    return value


def _etag(value: str) -> str:
    if type(value) is not str or _ETAG.fullmatch(value) is None:
        _raise_local(CoreClientLocalErrorCodeV2.INVALID_REQUEST, 400)
    return value


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
        _raise_local(CoreClientLocalErrorCodeV2.INVALID_CONNECTION, 400)
    return float(max(cast(tuple[float, ...], values)))


def _remaining_deadline(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        _raise_local(CoreClientLocalErrorCodeV2.CONNECTION_FAILED, 503)
    return remaining


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        _raise_local(CoreClientLocalErrorCodeV2.CONNECTION_FAILED, 503)


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
            _raise_local(CoreClientLocalErrorCodeV2.RESPONSE_TOO_LARGE, 502)
        buffer.extend(chunk)
        if len(buffer) > MAX_CORE_SSE_FRAME_BYTES:
            raise ValueError("Core SSE line exceeds its byte bound")
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
                    yield b"\n".join(lines)
                    lines = []
                    frame_bytes = 0
                continue
            if line.startswith(b":"):
                continue
            frame_bytes += len(line) + 1
            if frame_bytes > MAX_CORE_SSE_FRAME_BYTES:
                raise ValueError("Core SSE frame exceeds its byte bound")
            lines.append(line)
    if buffer or lines:
        raise ValueError("Core SSE response ended with an incomplete frame")
    if declared_length is not None and declared_length != total_bytes:
        raise ValueError("Core SSE response length differs from Content-Length")


def _validate_sse_frame(
    frame: bytes,
    private_values: tuple[str, ...],
) -> v2.SseFrameV2:
    fields: dict[str, bytes] = {}
    for line in frame.split(b"\n"):
        name, separator, raw = line.partition(b":")
        if not separator or name not in {b"id", b"event", b"data", b"retry"}:
            raise ValueError("unexpected Core SSE field")
        key = name.decode("ascii")
        if key in fields:
            raise ValueError("duplicate Core SSE field")
        fields[key] = raw[1:] if raw.startswith(b" ") else raw
    if not {"id", "event", "data"}.issubset(fields) or set(fields) - {
        "id",
        "event",
        "data",
        "retry",
    }:
        raise ValueError("incomplete Core SSE frame")
    event_id = fields["id"].decode("utf-8", errors="strict")
    event_name = fields["event"].decode("utf-8", errors="strict")
    _visible_ascii_sse_id(event_id)
    _scan_private_strings(
        (event_id, event_name),
        private_values,
        CoreClientLocalErrorCodeV2.SSE_PROTOCOL_ERROR,
        502,
    )
    decoded = _decode_json_document(
        fields["data"],
        private_values=private_values,
        error_code=CoreClientLocalErrorCodeV2.SSE_PROTOCOL_ERROR,
    )
    try:
        envelope = _EVENT_ADAPTER.validate_json(fields["data"], strict=True)
        payload: dict[str, object] = {
            "id": event_id,
            "event": event_name,
            "data": envelope,
        }
        if "retry" in fields:
            retry_text = fields["retry"].decode("ascii", errors="strict")
            if re.fullmatch(r"[1-9][0-9]*", retry_text) is None:
                raise ValueError("invalid Core SSE retry")
            payload["retry"] = int(retry_text)
        result = v2.SseFrameV2.model_validate(payload, strict=True)
    except (TypeError, UnicodeError, ValidationError, ValueError):
        _raise_local(CoreClientLocalErrorCodeV2.SSE_PROTOCOL_ERROR, 502)
    del decoded
    return result


def _json_nesting_exceeds(body: bytes, maximum: int) -> bool:
    depth = 0
    in_string = False
    escaped = False
    for byte in body:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in {0x5B, 0x7B}:
            depth += 1
            if depth > maximum:
                return True
        elif byte in {0x5D, 0x7D}:
            depth -= 1
            if depth < 0:
                return True
    return in_string or depth != 0


def _json_matches_schema_types(
    value: object,
    schema: Mapping[str, object],
    root_schema: Mapping[str, object] | None = None,
) -> bool:
    """Reject scalar coercion while accepting JSON arrays for tuple contracts."""

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
        return isinstance(target, Mapping) and _json_matches_schema_types(
            value, target, root
        )

    for keyword in ("anyOf", "oneOf"):
        choices = schema.get(keyword)
        if isinstance(choices, list):
            return any(
                isinstance(choice, Mapping)
                and _json_matches_schema_types(value, choice, root)
                for choice in choices
            )
    all_of = schema.get("allOf")
    if isinstance(all_of, list) and not all(
        isinstance(choice, Mapping)
        and _json_matches_schema_types(value, choice, root)
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
                _json_matches_schema_types(item, item_schema, root)
                for item in value[start:]
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


def _local_exception(
    code: CoreClientLocalErrorCodeV2,
    status_code: int,
) -> CoreClientErrorV2:
    return CoreClientErrorV2(
        status_code,
        CoreClientLocalErrorV2(
            code=code,
            message=_LOCAL_ERROR_MESSAGES[code],
            retryable=code
            in {
                CoreClientLocalErrorCodeV2.CONNECTION_FAILED,
                CoreClientLocalErrorCodeV2.CLIENT_CLOSED,
                CoreClientLocalErrorCodeV2.NEGOTIATION_REQUIRED,
                CoreClientLocalErrorCodeV2.SNAPSHOT_REFRESH_REQUIRED,
            },
        ),
    )


def _raise_local(code: CoreClientLocalErrorCodeV2, status_code: int) -> NoReturn:
    raise _local_exception(code, status_code) from None


__all__ = (
    "CORE_EVENTS_SCHEMA_SHA256",
    "CORE_OPENAPI_SHA256",
    "CoreBootstrapTunnelConnectionV2",
    "CoreClientErrorV2",
    "CoreClientLocalErrorCodeV2",
    "CoreClientLocalErrorV2",
    "CoreControlClientV2",
    "CoreMutationOutcomeUnknownV2",
    "CoreProjectBootstrapClientV2",
    "CoreProjectBootstrapResultV2",
    "CoreSseStreamV2",
    "CoreTunnelConnectionV2",
)
