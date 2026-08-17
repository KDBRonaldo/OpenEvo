"""Active-project bridge from Desktop v2 intent to Core Control API v2."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from enum import StrEnum
import hashlib
import json
import re
import secrets
import threading
import time
from typing import Any, Protocol

import httpx
from pydantic import TypeAdapter, ValidationError

from desktop.sidecar.contracts.v2 import models as local_v2
from desktop.sidecar.core_client_v2 import (
    CoreBootstrapTunnelConnectionV2,
    CoreClientErrorV2,
    CoreControlClientV2,
    CoreMutationOutcomeUnknownV2,
    CoreProjectBootstrapClientV2,
    CoreProjectBootstrapResultV2,
    CoreTunnelConnectionV2,
)
from desktop.sidecar.lifecycle_logs_v2 import LifecycleRawOutputObserverV2
from desktop.sidecar.release_capabilities import (
    ReleaseAuthorityNegotiationError,
    V0110_RELEASE_AUTHORITY_POLICY,
    validate_persisted_core_v2_authority,
)
from openevo.backend.contracts.v2 import models as core_v2


DEFAULT_BRIDGE_TIMEOUT_SECONDS = 60.0
MAX_BRIDGE_TIMEOUT_SECONDS = 300.0
MAX_ACTIVATION_TIMEOUT_SECONDS = 7200.0
MAX_MAPPING_HISTORY_PROOF_GENERATIONS = 256

_OPAQUE_ID = TypeAdapter(core_v2.OpaqueId)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_ETAG = re.compile(r'"[0-9a-f]{64}"\Z', re.ASCII)
_SOURCE_COMMIT = re.compile(r"[0-9a-f]{7,64}\Z", re.ASCII)
_HEADER = re.compile(r"[\x21-\x7e]{16,256}\Z", re.ASCII)
_MODEL_DOWNLOAD_PROGRESS = re.compile(
    r"Model download progress: [0-9]{1,3}% \(([0-9]+)/([0-9]+) bytes\)\.\Z"
)


class CoreBridgeMutationStateV2(StrEnum):
    PREPARED = "prepared"
    UNKNOWN = "unknown"
    APPLIED = "applied"


@dataclass(frozen=True, slots=True)
class CoreBridgeMutationV2:
    """Durable replay identity for one Core mutation.

    It intentionally stores neither request bytes nor a URL, token, command, or
    host path.  The bridge must reconstruct the exact typed request and prove
    its digest before replay.
    """

    desktop_project_id: str
    profile_id: str
    profile_connection_generation: int
    operation: str
    resource_scope: str
    idempotency_key: str
    request_sha256: str
    state: CoreBridgeMutationStateV2
    response_sha256: str | None
    response_resource_id: str | None

    def __post_init__(self) -> None:
        for value in (
            self.desktop_project_id,
            self.profile_id,
            self.operation,
            self.resource_scope,
        ):
            _require_opaque(value)
        if (
            type(self.profile_connection_generation) is not int
            or not 1 <= self.profile_connection_generation <= core_v2.MAX_JAVASCRIPT_SAFE_INTEGER
            or type(self.idempotency_key) is not str
            or _HEADER.fullmatch(self.idempotency_key) is None
            or _DIGEST.fullmatch(self.request_sha256) is None
            or type(self.state) is not CoreBridgeMutationStateV2
        ):
            raise ValueError("Core bridge mutation identity is invalid")
        applied = self.state is CoreBridgeMutationStateV2.APPLIED
        if applied != (self.response_sha256 is not None and self.response_resource_id is not None):
            raise ValueError("only an applied Core mutation has response authority")
        if self.response_sha256 is not None and _DIGEST.fullmatch(self.response_sha256) is None:
            raise ValueError("Core mutation response digest is invalid")
        if self.response_resource_id is not None:
            _require_opaque(self.response_resource_id)


@dataclass(frozen=True, slots=True)
class CoreProjectMappingV2:
    """Exact durable Desktop/profile/Core/head authority mapping.

    Distinct v2 identities remain distinct.  There is deliberately no generic
    ``revision`` field or compatibility projection.
    """

    desktop_project_id: str
    profile_id: str
    profile_connection_generation: int
    core_project_id: str
    project_config_sha256: str
    project_etag: str
    project_admission_etag: str | None
    active_project_head: core_v2.ProjectHeadRefV2 | None
    project_head_successor_proof: tuple[core_v2.ProjectHeadRefV2, ...]
    daemon_release_version: str
    daemon_build_id: str
    daemon_source_commit: str
    daemon_openapi_sha256: str
    daemon_event_schema_sha256: str
    daemon_registry_sha256: str
    daemon_runtime_contract_sha256: str
    core_project: core_v2.ProjectV2
    core_version: core_v2.VersionResponseV2
    mapping_generation: int
    predecessor_mapping_sha256: str | None
    last_core_event_id: str | None
    last_core_event_sequence: int | None
    last_core_event_payload_sha256: str | None

    def __post_init__(self) -> None:
        for identity in (
            self.desktop_project_id,
            self.profile_id,
            self.core_project_id,
        ):
            _require_opaque(identity)
        if (
            type(self.profile_connection_generation) is not int
            or not 1 <= self.profile_connection_generation <= core_v2.MAX_JAVASCRIPT_SAFE_INTEGER
            or type(self.mapping_generation) is not int
            or not 1 <= self.mapping_generation <= core_v2.MAX_JAVASCRIPT_SAFE_INTEGER
            or _DIGEST.fullmatch(self.project_config_sha256) is None
            or _ETAG.fullmatch(self.project_etag) is None
            or (
                self.project_admission_etag is not None
                and _ETAG.fullmatch(self.project_admission_etag) is None
            )
            or _DIGEST.fullmatch(self.daemon_build_id) is None
            or _SOURCE_COMMIT.fullmatch(self.daemon_source_commit) is None
            or any(
                _DIGEST.fullmatch(value) is None
                for value in (
                    self.daemon_openapi_sha256,
                    self.daemon_event_schema_sha256,
                    self.daemon_registry_sha256,
                    self.daemon_runtime_contract_sha256,
                )
            )
            or type(self.core_project) is not core_v2.ProjectV2
            or type(self.core_version) is not core_v2.VersionResponseV2
            or type(self.project_head_successor_proof) is not tuple
            or any(
                type(head) is not core_v2.ProjectHeadRefV2
                for head in self.project_head_successor_proof
            )
        ):
            raise ValueError("Core project mapping identity is invalid")
        if (self.mapping_generation == 1) != (self.predecessor_mapping_sha256 is None):
            raise ValueError("only the first mapping has no predecessor digest")
        if (
            self.predecessor_mapping_sha256 is not None
            and _DIGEST.fullmatch(self.predecessor_mapping_sha256) is None
        ):
            raise ValueError("mapping predecessor digest is invalid")
        event_values = (
            self.last_core_event_id,
            self.last_core_event_sequence,
            self.last_core_event_payload_sha256,
        )
        if any(value is None for value in event_values) != all(
            value is None for value in event_values
        ):
            raise ValueError("Core event cursor authority is incomplete")
        if self.last_core_event_id is not None:
            _require_opaque(self.last_core_event_id)
            if (
                type(self.last_core_event_sequence) is not int
                or not 1 <= self.last_core_event_sequence <= core_v2.MAX_JAVASCRIPT_SAFE_INTEGER
                or self.last_core_event_payload_sha256 is None
                or _DIGEST.fullmatch(self.last_core_event_payload_sha256) is None
            ):
                raise ValueError("Core event cursor authority is invalid")
        self._validate_remote_authority()
        self._validate_head_proof()

    def _validate_head_proof(self) -> None:
        proof = self.project_head_successor_proof
        if len(proof) > MAX_MAPPING_HISTORY_PROOF_GENERATIONS:
            raise ValueError("Core project-head successor proof is too long")
        if not proof:
            return
        if proof[-1] != self.active_project_head:
            raise ValueError("Core project-head proof does not end at the active head")
        previous: core_v2.ProjectHeadRefV2 | None = None
        for head in proof:
            if head.project_id != self.core_project_id:
                raise ValueError("Core project-head proof belongs to another project")
            if previous is not None and (
                head.generation != previous.generation + 1
                or head.predecessor_project_head_id != previous.project_head_id
            ):
                raise ValueError("Core project-head proof is not contiguous")
            previous = head

    def _validate_remote_authority(self) -> None:
        project = self.core_project
        version = self.core_version
        try:
            negotiated = validate_persisted_core_v2_authority(version.model_dump(mode="json"))
        except ReleaseAuthorityNegotiationError as exc:
            raise ValueError("Core mapping version is not release authority") from exc
        if negotiated != version:
            raise ValueError("Core mapping version normalization changed")
        v2_offer = next(
            (offer for offer in version.contracts if offer.api_major == 2),
            None,
        )
        if (
            project.project_id != self.core_project_id
            or project.project_config_sha256 != self.project_config_sha256
            or project.etag != self.project_etag
            or project.admission_etag != self.project_admission_etag
            or project.active_project_head != self.active_project_head
            or version.release_version != self.daemon_release_version
            or version.build_id != self.daemon_build_id
            or version.source_commit != self.daemon_source_commit
            or v2_offer is None
            or v2_offer.openapi_sha256 != self.daemon_openapi_sha256
            or v2_offer.event_schema_sha256 != self.daemon_event_schema_sha256
            or version.registry_sha256 != self.daemon_registry_sha256
            or version.runtime_contract_sha256 != self.daemon_runtime_contract_sha256
        ):
            raise ValueError("Core mapping flattened authority differs from its models")
        head = self.active_project_head
        if head is not None:
            head_authority = (
                head.registry_sha256,
                head.runtime_context_snapshot.runtime_contract_sha256,
            )
            allowed_head_authorities = {
                (
                    self.daemon_registry_sha256,
                    self.daemon_runtime_contract_sha256,
                ),
                *(
                    (
                        authority.registry_sha256,
                        authority.runtime_contract_sha256,
                    )
                    for authority in V0110_RELEASE_AUTHORITY_POLICY.retained_core_authorities
                ),
            }
            if head.project_id != self.core_project_id or head_authority not in (
                allowed_head_authorities
            ):
                raise ValueError("Core project head differs from negotiated authority")


def core_project_mapping_document_v2(mapping: CoreProjectMappingV2) -> dict[str, object]:
    if type(mapping) is not CoreProjectMappingV2:
        raise TypeError("mapping must be an exact CoreProjectMappingV2")
    return {
        "schema_version": "2",
        "record_type": "CoreProjectMappingV2",
        "desktop_project_id": mapping.desktop_project_id,
        "profile_id": mapping.profile_id,
        "profile_connection_generation": mapping.profile_connection_generation,
        "core_project_id": mapping.core_project_id,
        "project_config_sha256": mapping.project_config_sha256,
        "project_etag": mapping.project_etag,
        "project_admission_etag": mapping.project_admission_etag,
        "active_project_head": (
            None
            if mapping.active_project_head is None
            else mapping.active_project_head.model_dump(mode="json")
        ),
        "project_head_successor_proof": [
            head.model_dump(mode="json") for head in mapping.project_head_successor_proof
        ],
        "daemon_release_version": mapping.daemon_release_version,
        "daemon_build_id": mapping.daemon_build_id,
        "daemon_source_commit": mapping.daemon_source_commit,
        "daemon_openapi_sha256": mapping.daemon_openapi_sha256,
        "daemon_event_schema_sha256": mapping.daemon_event_schema_sha256,
        "daemon_registry_sha256": mapping.daemon_registry_sha256,
        "daemon_runtime_contract_sha256": mapping.daemon_runtime_contract_sha256,
        "core_project": mapping.core_project.model_dump(mode="json"),
        "core_version": mapping.core_version.model_dump(mode="json"),
        "mapping_generation": mapping.mapping_generation,
        "predecessor_mapping_sha256": mapping.predecessor_mapping_sha256,
        "last_core_event_id": mapping.last_core_event_id,
        "last_core_event_sequence": mapping.last_core_event_sequence,
        "last_core_event_payload_sha256": mapping.last_core_event_payload_sha256,
    }


def core_project_mapping_sha256_v2(mapping: CoreProjectMappingV2) -> str:
    return hashlib.sha256(
        _canonical_json_bytes(core_project_mapping_document_v2(mapping))
    ).hexdigest()


def core_bridge_mutation_document_v2(
    mutation: CoreBridgeMutationV2,
) -> dict[str, object]:
    if type(mutation) is not CoreBridgeMutationV2:
        raise TypeError("mutation must be an exact CoreBridgeMutationV2")
    return {
        "schema_version": "2",
        "record_type": "CoreBridgeMutationV2",
        "desktop_project_id": mutation.desktop_project_id,
        "profile_id": mutation.profile_id,
        "profile_connection_generation": mutation.profile_connection_generation,
        "operation": mutation.operation,
        "resource_scope": mutation.resource_scope,
        "idempotency_key": mutation.idempotency_key,
        "request_sha256": mutation.request_sha256,
        "state": mutation.state.value,
        "response_sha256": mutation.response_sha256,
        "response_resource_id": mutation.response_resource_id,
    }


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _require_opaque(value: object) -> str:
    try:
        return _OPAQUE_ID.validate_python(value, strict=True)
    except ValidationError as exc:
        raise ValueError("Core bridge opaque identity is invalid") from exc


class DesktopCoreBridgeErrorV2(RuntimeError):
    def __init__(self, status_code: int, error: local_v2.DesktopErrorV2) -> None:
        super().__init__(error.summary)
        self.status_code = status_code
        self.error = error


class _MutationReceiptV2(core_v2.ContractModel):
    operation: core_v2.OpaqueId
    resource_id: core_v2.OpaqueId


@dataclass(frozen=True, slots=True, repr=False)
class CoreHostAttachmentV2:
    """Host-global Daemon authority without a renderer-visible URL or secret."""

    profile_id: str
    profile_connection_generation: int
    remote_port: int
    bearer_token: str = field(repr=False)
    bearer_identity: str

    def __post_init__(self) -> None:
        _require_opaque(self.profile_id)
        if (
            type(self.profile_connection_generation) is not int
            or not 1 <= self.profile_connection_generation <= core_v2.MAX_JAVASCRIPT_SAFE_INTEGER
            or type(self.remote_port) is not int
            or not 1 <= self.remote_port <= 65_535
            or type(self.bearer_token) is not str
            or len(self.bearer_token) < 43
            or _DIGEST.fullmatch(self.bearer_identity) is None
            or self.bearer_token == self.bearer_identity
        ):
            raise ValueError("Core host attachment authority is invalid")


class CoreHostServiceV2(Protocol):
    def ensure_core(
        self,
        profile_id: str,
        profile_connection_generation: int,
        *,
        deadline: float,
        cancel_event: threading.Event,
    ) -> CoreHostAttachmentV2: ...


_TUNNEL_CLOSE_EXECUTOR = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="openevo-core-v2-tunnel-close",
)
_TUNNEL_CLOSE_CAPACITY = threading.BoundedSemaphore(32)
_PROJECT_CREATE_CAPACITY = threading.BoundedSemaphore(8)
_PROJECT_CREATE_PROGRESS_POLL_SECONDS = 0.25


class CoreTunnelHandleV2:
    """One private loopback tunnel owned by one active project generation."""

    def __init__(
        self,
        *,
        endpoint: str,
        profile_id: str,
        profile_connection_generation: int,
        session_id: str,
        close_callback: Callable[[], None],
    ) -> None:
        if not callable(close_callback):
            raise TypeError("Core tunnel close callback must be callable")
        # Reuse the strict client connection validator without retaining a
        # token.  The placeholder is process-local and never leaves this call.
        reserved_identities = {profile_id, session_id}
        validation_project = next(
            candidate
            for candidate in (
                "core-tunnel-validation-project",
                "core-tunnel-validation-project-alternate",
                "core-tunnel-validation-project-final",
            )
            if candidate not in reserved_identities
        )
        CoreTunnelConnectionV2(
            endpoint=endpoint,
            bearer_token="abcdefghijklmnopqrstuvwxyzABCDEFGH0123456789._-abcdefghijklmnop",
            profile_id=profile_id,
            profile_connection_generation=profile_connection_generation,
            project_id=validation_project,
            session_id=session_id,
        )
        self.endpoint = endpoint
        self.profile_id = profile_id
        self.profile_connection_generation = profile_connection_generation
        self.session_id = session_id
        self._close_callback = close_callback
        self._lock = threading.Lock()
        self._closed = False
        self._close_future: Future[None] | None = None

    def __repr__(self) -> str:
        return "CoreTunnelHandleV2(<private>)"

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def close(self, *, deadline: float) -> None:
        with self._lock:
            if self._closed:
                return
            future = self._close_future
            if future is None:
                if not _TUNNEL_CLOSE_CAPACITY.acquire(blocking=False):
                    raise _bridge_error(
                        "core_tunnel_close_unavailable",
                        "Desktop cannot schedule another Core tunnel close operation.",
                        status=503,
                        retryable=True,
                        action="retry",
                    )

                def close_owned() -> None:
                    try:
                        self._close_callback()
                    finally:
                        _TUNNEL_CLOSE_CAPACITY.release()

                try:
                    future = _TUNNEL_CLOSE_EXECUTOR.submit(close_owned)
                except Exception:
                    _TUNNEL_CLOSE_CAPACITY.release()
                    raise
                self._close_future = future
        try:
            future.result(timeout=_remaining(deadline))
        except FutureTimeoutError:
            raise _bridge_error(
                "core_tunnel_close_timeout",
                "The active Core tunnel did not close before its deadline.",
                status=503,
                retryable=True,
                action="retry",
            ) from None
        except DesktopCoreBridgeErrorV2:
            raise
        except Exception:
            with self._lock:
                if self._close_future is future:
                    self._close_future = None
            raise _bridge_error(
                "core_tunnel_close_failed",
                "The active Core tunnel could not be closed safely.",
                status=503,
                retryable=True,
                action="retry",
            ) from None
        with self._lock:
            self._closed = True
            self._close_future = None


class CoreTunnelFactoryV2(Protocol):
    def open_tunnel(
        self,
        *,
        profile_id: str,
        profile_connection_generation: int,
        remote_port: int,
        session_id: str,
        deadline: float,
    ) -> CoreTunnelHandleV2: ...


class DesktopCoreBridgePersistenceV2(Protocol):
    def load_mapping(self, desktop_project_id: str) -> CoreProjectMappingV2 | None: ...

    def load_mapping_history(
        self, desktop_project_id: str
    ) -> tuple[CoreProjectMappingV2, ...]: ...

    def commit_mapping(
        self,
        mapping: CoreProjectMappingV2,
        *,
        expected_previous: CoreProjectMappingV2 | None,
    ) -> None: ...

    def load_mutation(
        self,
        desktop_project_id: str,
        operation: str,
        idempotency_key: str,
    ) -> CoreBridgeMutationV2 | None: ...

    def reserve_mutation(self, mutation: CoreBridgeMutationV2) -> CoreBridgeMutationV2: ...

    def mark_mutation_unknown(self, mutation: CoreBridgeMutationV2) -> CoreBridgeMutationV2: ...

    def mark_mutation_applied(
        self,
        mutation: CoreBridgeMutationV2,
        *,
        response_sha256: str,
        response_resource_id: str,
    ) -> CoreBridgeMutationV2: ...


class DesktopEventPublisherV2(Protocol):
    def publish(
        self, payload: local_v2.DesktopEventPayloadV2
    ) -> local_v2.DesktopEventEnvelopeV2 | None: ...


@dataclass(frozen=True, slots=True)
class CoreActivationV2:
    desktop_project_id: str
    profile_id: str
    profile_connection_generation: int
    bridge_generation: int
    project: core_v2.ProjectV2
    version: core_v2.VersionResponseV2
    capabilities: core_v2.CapabilitiesResponseV2
    mapping: CoreProjectMappingV2

    def __post_init__(self) -> None:
        _require_opaque(self.desktop_project_id)
        _require_opaque(self.profile_id)
        if (
            type(self.profile_connection_generation) is not int
            or type(self.bridge_generation) is not int
            or self.profile_connection_generation < 1
            or self.bridge_generation < 1
            or type(self.project) is not core_v2.ProjectV2
            or type(self.version) is not core_v2.VersionResponseV2
            or type(self.capabilities) is not core_v2.CapabilitiesResponseV2
            or type(self.mapping) is not CoreProjectMappingV2
            or self.mapping.desktop_project_id != self.desktop_project_id
            or self.mapping.profile_id != self.profile_id
            or self.mapping.profile_connection_generation != self.profile_connection_generation
            or self.mapping.core_project != self.project
            or self.mapping.core_version != self.version
        ):
            raise ValueError("Core activation authority is inconsistent")


@dataclass(slots=True)
class _ActiveSessionV2:
    activation: CoreActivationV2
    attachment: CoreHostAttachmentV2
    tunnel: CoreTunnelHandleV2
    client: CoreControlClientV2


class DesktopCoreBridgeV2:
    """Generation-sealed bridge using only one active system-SSH tunnel."""

    def __init__(
        self,
        *,
        host_service: CoreHostServiceV2,
        tunnel_factory: CoreTunnelFactoryV2,
        persistence: DesktopCoreBridgePersistenceV2,
        transport_factory: Callable[[], httpx.BaseTransport] | None = None,
        event_publisher: DesktopEventPublisherV2 | None = None,
        progress_observer: Callable[
            [local_v2.LifecyclePhaseV2, local_v2.LifecycleProgressV2 | None, bool],
            None,
        ]
        | None = None,
        output_observer: LifecycleRawOutputObserverV2 | None = None,
        timeout: float = DEFAULT_BRIDGE_TIMEOUT_SECONDS,
        activation_timeout: float | None = None,
    ) -> None:
        resolved_activation = timeout if activation_timeout is None else activation_timeout
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0 < timeout <= MAX_BRIDGE_TIMEOUT_SECONDS
            or isinstance(resolved_activation, bool)
            or not isinstance(resolved_activation, (int, float))
            or not 0 < resolved_activation <= MAX_ACTIVATION_TIMEOUT_SECONDS
        ):
            raise ValueError("Core bridge timeouts are outside their finite bounds")
        if progress_observer is not None and not callable(progress_observer):
            raise TypeError("Core bridge lifecycle progress observer is invalid")
        if output_observer is not None and not callable(output_observer):
            raise TypeError("Core bridge lifecycle output observer is invalid")
        self._host_service = host_service
        self._tunnel_factory = tunnel_factory
        self._persistence = persistence
        self._transport_factory = transport_factory
        self._event_publisher = event_publisher
        self._progress_observer = progress_observer
        self._output_observer = output_observer
        self._timeout = float(timeout)
        self._activation_timeout = float(resolved_activation)
        self._lock = threading.RLock()
        self._transition_lock = threading.Lock()
        self._active: _ActiveSessionV2 | None = None
        self._retained: list[_ActiveSessionV2] = []
        self._generation = 0
        self._closed = False

    @property
    def active_activation(self) -> CoreActivationV2 | None:
        with self._lock:
            return None if self._active is None else self._active.activation

    def __enter__(self) -> DesktopCoreBridgeV2:
        with self._lock:
            if self._closed:
                raise _bridge_error(
                    "core_bridge_closed",
                    "The Desktop Core bridge is closed.",
                    status=503,
                    retryable=True,
                    action="reconnect",
                )
        return self

    def set_progress_observer(
        self,
        observer: Callable[
            [local_v2.LifecyclePhaseV2, local_v2.LifecycleProgressV2 | None, bool],
            None,
        ],
    ) -> None:
        if not callable(observer):
            raise TypeError("Core bridge lifecycle progress observer is invalid")
        with self._lock:
            if self._active is not None or self._progress_observer is not None:
                raise RuntimeError("Core bridge lifecycle progress observer cannot be changed")
            set_host_progress = getattr(self._host_service, "set_progress_observer", None)
            if callable(set_host_progress):
                set_host_progress(observer)
            self._progress_observer = observer

    def set_output_observer(self, observer: LifecycleRawOutputObserverV2) -> None:
        if not callable(observer):
            raise TypeError("Core bridge lifecycle output observer is invalid")
        with self._lock:
            if self._active is not None or self._output_observer is not None:
                raise RuntimeError("Core bridge lifecycle output observer cannot be changed")
            self._output_observer = observer

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        deadline = time.monotonic() + self._timeout
        self._acquire_transition(deadline)
        try:
            with self._lock:
                if not self._closed:
                    self._closed = True
                    self._generation += 1
                    if self._active is not None:
                        self._retained.append(self._active)
                        self._active = None
                retained = tuple(self._retained)
                self._retained.clear()
            for index, session in enumerate(retained):
                try:
                    self._retire(session, deadline=deadline)
                except BaseException:
                    with self._lock:
                        self._retained[0:0] = retained[index:]
                    raise
        finally:
            self._transition_lock.release()

    def activate_project(
        self,
        desktop_project_id: str,
        request: local_v2.ProjectCreateV2,
        *,
        idempotency_key: str,
        cancel_event: threading.Event | None = None,
    ) -> CoreActivationV2:
        _require_opaque(desktop_project_id)
        if type(request) is not local_v2.ProjectCreateV2:
            raise TypeError("activation requires an exact Desktop ProjectCreateV2")
        _require_idempotency_key(idempotency_key)
        if cancel_event is not None and not isinstance(cancel_event, threading.Event):
            raise TypeError("activation cancellation authority is invalid")
        activation_cancel = cancel_event or threading.Event()
        deadline = time.monotonic() + self._activation_timeout
        self._acquire_transition(deadline, cancel_event=activation_cancel)
        tunnel: CoreTunnelHandleV2 | None = None
        client: CoreControlClientV2 | None = None
        published = False
        try:
            with self._lock:
                if self._closed:
                    raise _bridge_error(
                        "core_bridge_closed",
                        "The Desktop Core bridge is closed.",
                        status=503,
                        retryable=True,
                        action="reconnect",
                    )
                candidate_generation = self._generation + 1
            attachment = self._call_adapter(
                lambda: self._host_service.ensure_core(
                    request.profile_id,
                    request.profile_connection_generation,
                    deadline=deadline,
                    cancel_event=activation_cancel,
                ),
                failure_code="core_host_unavailable",
                failure_summary="Desktop could not attach the compatible OpenEvo Daemon.",
            )
            if type(attachment) is not CoreHostAttachmentV2 or (
                attachment.profile_id != request.profile_id
                or attachment.profile_connection_generation
                != request.profile_connection_generation
            ):
                raise _bridge_error(
                    "core_host_authority_mismatch",
                    "The OpenEvo Daemon belongs to another profile generation.",
                    status=409,
                    action="reconnect",
                )
            session_id = f"core-session-{secrets.token_hex(20)}"
            self._observe_lifecycle_progress("opening_project_tunnel", cancellable=True)
            tunnel = self._call_adapter(
                lambda: self._tunnel_factory.open_tunnel(
                    profile_id=request.profile_id,
                    profile_connection_generation=request.profile_connection_generation,
                    remote_port=attachment.remote_port,
                    session_id=session_id,
                    deadline=deadline,
                ),
                failure_code="core_tunnel_open_failed",
                failure_summary="Desktop could not open the active project tunnel.",
            )
            self._validate_tunnel(tunnel, request, session_id)
            self._observe_lifecycle_progress("negotiating_core", cancellable=True)
            previous = self._call_adapter(
                lambda: self._persistence.load_mapping(desktop_project_id),
                failure_code="core_mapping_unavailable",
                failure_summary="Desktop could not read the durable Core project mapping.",
            )
            create = core_v2.ProjectCreateV2(
                display_name=request.display_name,
                config=request.config,
            )
            self._observe_lifecycle_progress("creating_remote_project", cancellable=False)
            bootstrap_version: core_v2.VersionResponseV2 | None
            if previous is None:
                connection, bootstrap_version = self._bootstrap_project(
                    desktop_project_id=desktop_project_id,
                    request=request,
                    create=create,
                    idempotency_key=idempotency_key,
                    attachment=attachment,
                    tunnel=tunnel,
                    deadline=deadline,
                )
            else:
                self._validate_existing_mapping(previous, desktop_project_id, request)
                connection = CoreTunnelConnectionV2(
                    endpoint=tunnel.endpoint,
                    bearer_token=attachment.bearer_token,
                    profile_id=request.profile_id,
                    profile_connection_generation=request.profile_connection_generation,
                    project_id=previous.core_project_id,
                    session_id=tunnel.session_id,
                )
                bootstrap_version = None
            client = self._new_client(connection, deadline)
            native_workspace = request.config.workspace.kind == "native_folder_snapshot"
            if not native_workspace:
                self._observe_lifecycle_progress("verifying_project", cancellable=False)
            version = self._call_core(client.version)
            if bootstrap_version is not None and version != bootstrap_version:
                raise _bridge_error(
                    "core_authority_changed",
                    "The negotiated Core authority changed during project activation.",
                    status=502,
                    action="install_repair_daemon",
                )
            status = self._call_core(client.system_status)
            if status.status != "ready":
                raise _bridge_error(
                    "core_not_ready",
                    "The OpenEvo Daemon is not ready for project operations.",
                    status=503,
                    retryable=True,
                    action="install_repair_daemon",
                )
            project = self._call_core(client.get_project)
            self._validate_project_intent(project, create)
            capabilities = self._call_core(
                lambda: client.capabilities(request.config.execution.mode)
            )
            proof = self._head_successor_proof(
                client=client,
                previous=None if previous is None else previous.active_project_head,
                current=project.active_project_head,
            )
            mapping = self._mapping_from_authority(
                desktop_project_id=desktop_project_id,
                request=request,
                project=project,
                version=version,
                previous=previous,
                head_proof=proof,
            )
            if previous is not None and self._same_mapping_authority(previous, mapping):
                mapping = previous
            else:
                if not native_workspace:
                    self._observe_lifecycle_progress("activating", cancellable=False)
                self._call_adapter(
                    lambda: self._persistence.commit_mapping(
                        mapping,
                        expected_previous=previous,
                    ),
                    failure_code="core_mapping_commit_failed",
                    failure_summary="Desktop could not commit the Core project mapping.",
                )
            if previous is not None and self._same_mapping_authority(previous, mapping):
                if not native_workspace:
                    self._observe_lifecycle_progress("activating", cancellable=False)
            activation = CoreActivationV2(
                desktop_project_id=desktop_project_id,
                profile_id=request.profile_id,
                profile_connection_generation=request.profile_connection_generation,
                bridge_generation=candidate_generation,
                project=project,
                version=version,
                capabilities=capabilities,
                mapping=mapping,
            )
            candidate = _ActiveSessionV2(
                activation=activation,
                attachment=attachment,
                tunnel=tunnel,
                client=client,
            )
            with self._lock:
                if self._closed:
                    raise _bridge_error(
                        "core_bridge_closed",
                        "The Desktop Core bridge closed during activation.",
                        status=503,
                        retryable=True,
                        action="reconnect",
                    )
                old = self._active
                self._active = candidate
                self._generation = candidate_generation
                published = True
            if old is not None:
                self._retire_or_retain(old, deadline=deadline, suppress_errors=True)
            if not native_workspace:
                self._observe_lifecycle_progress("activating", cancellable=False)
            return activation
        except DesktopCoreBridgeErrorV2:
            raise
        except CoreMutationOutcomeUnknownV2:
            raise _mutation_unknown_error(desktop_project_id) from None
        except CoreClientErrorV2 as exc:
            raise _bridge_client_error(exc, desktop_project_id) from None
        except (TypeError, ValueError, ValidationError):
            raise _bridge_error(
                "core_authority_invalid",
                "The OpenEvo Daemon returned inconsistent v2 authority.",
                status=502,
                action="install_repair_daemon",
                affected_resource_id=desktop_project_id,
            ) from None
        finally:
            if not published:
                if client is not None:
                    self._close_client(client, suppress_errors=True)
                if tunnel is not None:
                    self._close_tunnel(tunnel, deadline=deadline, suppress_errors=True)
            self._transition_lock.release()

    def _observe_lifecycle_progress(
        self,
        phase: local_v2.LifecyclePhaseV2,
        *,
        cancellable: bool,
        progress: local_v2.LifecycleProgressV2 | None = None,
    ) -> None:
        observer = self._progress_observer
        if observer is not None:
            observer(
                phase,
                progress
                if progress is not None
                else local_v2.LifecycleProgressIndeterminateV2(kind="indeterminate"),
                cancellable,
            )

    def deactivate_project(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
    ) -> None:
        deadline = time.monotonic() + self._timeout
        self._acquire_transition(deadline)
        try:
            with self._lock:
                session, was_active = self._deactivation_session_locked(
                    desktop_project_id, profile_connection_generation
                )
                if was_active:
                    self._active = None
                    self._generation += 1
            self._retire_or_retain(session, deadline=deadline, suppress_errors=False)
        finally:
            self._transition_lock.release()

    def get_project(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
    ) -> core_v2.ProjectV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        project = self._call_core(session.client.get_project, desktop_project_id)
        self._refresh_mapping(session, project=project)
        return project

    def list_projects(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        *,
        limit: int = 50,
        after: str | None = None,
    ) -> core_v2.ProjectPageV2:
        del limit
        if after is not None:
            raise _bridge_error(
                "project_page_cursor_invalid",
                "The active project inventory has no continuation page.",
                status=400,
                affected_resource_id=desktop_project_id,
            )
        project = self.get_project(
            desktop_project_id,
            profile_connection_generation,
        )
        return core_v2.ProjectPageV2(
            items=[project],
            has_more=False,
            next_cursor=None,
        )

    def capabilities(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        execution_mode: core_v2.ExecutionModeV2,
    ) -> core_v2.CapabilitiesResponseV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        return self._session_core(
            session, lambda: session.client.capabilities(execution_mode), desktop_project_id
        )

    def validate_project(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        request: core_v2.ProjectValidationRequestV2,
        *,
        idempotency_key: str,
    ) -> core_v2.ProjectValidationResponseV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        _require_exact_request(request, core_v2.ProjectValidationRequestV2)
        return self._durable_mutation(
            session=session,
            operation="validate_project_v2",
            resource_scope=session.activation.mapping.core_project_id,
            request_sha256=_model_sha256(request),
            idempotency_key=idempotency_key,
            invoke=lambda: session.client.validate_project(
                request, idempotency_key=idempotency_key
            ),
            resource_id=lambda response: response.project_id,
            recover=lambda _project_id: session.client.validate_project(
                request, idempotency_key=idempotency_key
            ),
            response_type=core_v2.ProjectValidationResponseV2,
        )

    def update_project(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        request: core_v2.ProjectUpdateV2,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> core_v2.ProjectV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        _require_exact_request(request, core_v2.ProjectUpdateV2)
        project = self._durable_mutation(
            session=session,
            operation="update_project_v2",
            resource_scope=session.activation.mapping.core_project_id,
            request_sha256=_mutation_request_sha256(request, if_match=if_match),
            idempotency_key=idempotency_key,
            invoke=lambda: session.client.update_project(
                request,
                if_match=if_match,
                idempotency_key=idempotency_key,
            ),
            resource_id=lambda response: response.project_id,
            recover=lambda _project_id: session.client.update_project(
                request,
                if_match=if_match,
                idempotency_key=idempotency_key,
            ),
            response_type=core_v2.ProjectV2,
        )
        self._refresh_mapping(session, project=project)
        return project

    def create_workspace_upload(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        request: core_v2.WorkspaceUploadCreateV2,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> core_v2.WorkspaceUploadSessionV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        _require_exact_request(request, core_v2.WorkspaceUploadCreateV2)
        return self._durable_mutation(
            session=session,
            operation="create_workspace_upload_v2",
            resource_scope=session.activation.mapping.core_project_id,
            request_sha256=_mutation_request_sha256(request, if_match=if_match),
            idempotency_key=idempotency_key,
            invoke=lambda: session.client.create_workspace_upload(
                request,
                if_match=if_match,
                idempotency_key=idempotency_key,
            ),
            resource_id=lambda response: response.upload_id,
            recover=lambda upload_id: session.client.get_workspace_upload(upload_id),
            response_type=core_v2.WorkspaceUploadSessionV2,
        )

    def get_workspace_upload(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        upload_id: str,
    ) -> core_v2.WorkspaceUploadSessionV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        return self._session_core(
            session,
            lambda: session.client.get_workspace_upload(upload_id),
            desktop_project_id,
        )

    def put_workspace_upload_chunk(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        upload_id: str,
        chunk_index: int,
        chunk: bytes,
        *,
        chunk_sha256: str,
        if_match: str,
        idempotency_key: str,
    ) -> core_v2.WorkspaceUploadSessionV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        _require_opaque(upload_id)
        _require_etag(if_match)
        if (
            type(chunk) is not bytes
            or not 1 <= len(chunk) <= core_v2.MAX_WORKSPACE_CHUNK_BYTES
            or type(chunk_index) is not int
            or not 0 <= chunk_index < core_v2.MAX_WORKSPACE_CHUNKS
            or type(chunk_sha256) is not str
            or _DIGEST.fullmatch(chunk_sha256) is None
            or hashlib.sha256(chunk).hexdigest() != chunk_sha256
        ):
            raise ValueError("workspace chunk identity is invalid")
        digest = _canonical_value_sha256(
            {
                "schema_version": "2",
                "operation": "put_workspace_upload_chunk_v2",
                "upload_id": upload_id,
                "chunk_index": chunk_index,
                "chunk_sha256": chunk_sha256,
                "chunk_byte_size": len(chunk),
                "if_match": if_match,
            }
        )
        return self._durable_mutation(
            session=session,
            operation="put_workspace_chunk_v2",
            resource_scope=upload_id,
            request_sha256=digest,
            idempotency_key=idempotency_key,
            invoke=lambda: session.client.put_workspace_upload_chunk(
                upload_id,
                chunk_index,
                chunk,
                chunk_sha256=chunk_sha256,
                if_match=if_match,
                idempotency_key=idempotency_key,
            ),
            resource_id=lambda response: response.upload_id,
            recover=lambda current_upload_id: session.client.get_workspace_upload(
                current_upload_id
            ),
            response_type=core_v2.WorkspaceUploadSessionV2,
        )

    def finalize_workspace_upload(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        upload_id: str,
        request: core_v2.WorkspaceUploadFinalizeV2,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> core_v2.WorkspaceUploadSessionV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        _require_exact_request(request, core_v2.WorkspaceUploadFinalizeV2)
        return self._durable_mutation(
            session=session,
            operation="finalize_workspace_upload_v2",
            resource_scope=upload_id,
            request_sha256=_mutation_request_sha256(
                request, resource_id=upload_id, if_match=if_match
            ),
            idempotency_key=idempotency_key,
            invoke=lambda: session.client.finalize_workspace_upload(
                upload_id,
                request,
                if_match=if_match,
                idempotency_key=idempotency_key,
            ),
            resource_id=lambda response: response.upload_id,
            recover=lambda current_upload_id: session.client.get_workspace_upload(
                current_upload_id
            ),
            response_type=core_v2.WorkspaceUploadSessionV2,
        )

    def abort_workspace_upload(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        upload_id: str,
        request: core_v2.WorkspaceUploadAbortV2,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> core_v2.WorkspaceUploadSessionV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        _require_exact_request(request, core_v2.WorkspaceUploadAbortV2)
        return self._durable_mutation(
            session=session,
            operation="abort_workspace_upload_v2",
            resource_scope=upload_id,
            request_sha256=_mutation_request_sha256(
                request, resource_id=upload_id, if_match=if_match
            ),
            idempotency_key=idempotency_key,
            invoke=lambda: session.client.abort_workspace_upload(
                upload_id,
                request,
                if_match=if_match,
                idempotency_key=idempotency_key,
            ),
            resource_id=lambda response: response.upload_id,
            recover=lambda current_upload_id: session.client.get_workspace_upload(
                current_upload_id
            ),
            response_type=core_v2.WorkspaceUploadSessionV2,
        )

    def list_project_heads(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        *,
        limit: int = 50,
        after: str | None = None,
    ) -> core_v2.ProjectHeadPageV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        return self._session_core(
            session,
            lambda: session.client.list_project_heads(limit=limit, after=after),
            desktop_project_id,
        )

    def get_project_head(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        project_head_id: str,
    ) -> core_v2.ProjectHeadRefV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        return self._session_core(
            session,
            lambda: session.client.get_project_head(project_head_id),
            desktop_project_id,
        )

    def list_transitions(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        *,
        limit: int = 50,
        after: str | None = None,
    ) -> core_v2.SuccessorTransitionPageV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        return self._session_core(
            session,
            lambda: session.client.list_transitions(limit=limit, after=after),
            desktop_project_id,
        )

    def get_transition(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        successor_transition_id: str,
    ) -> core_v2.SuccessorTransitionV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        return self._session_core(
            session,
            lambda: session.client.get_transition(successor_transition_id),
            desktop_project_id,
        )

    def retry_transition(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        successor_transition_id: str,
        request: core_v2.ActionRequestV2,
        *,
        idempotency_key: str,
    ) -> core_v2.OperationV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        _require_exact_request(request, core_v2.ActionRequestV2)
        return self._durable_mutation(
            session=session,
            operation="retry_transition_v2",
            resource_scope=successor_transition_id,
            request_sha256=_mutation_request_sha256(request, resource_id=successor_transition_id),
            idempotency_key=idempotency_key,
            invoke=lambda: session.client.retry_transition(
                successor_transition_id,
                request,
                idempotency_key=idempotency_key,
            ),
            resource_id=lambda response: response.operation_id,
            recover=lambda _operation_id: session.client.retry_transition(
                successor_transition_id,
                request,
                idempotency_key=idempotency_key,
            ),
            response_type=core_v2.OperationV2,
        )

    def abandon_transition(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        successor_transition_id: str,
        request: core_v2.ActionRequestV2,
        *,
        idempotency_key: str,
    ) -> core_v2.OperationV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        _require_exact_request(request, core_v2.ActionRequestV2)
        return self._durable_mutation(
            session=session,
            operation="abandon_transition_v2",
            resource_scope=successor_transition_id,
            request_sha256=_mutation_request_sha256(request, resource_id=successor_transition_id),
            idempotency_key=idempotency_key,
            invoke=lambda: session.client.abandon_transition(
                successor_transition_id,
                request,
                idempotency_key=idempotency_key,
            ),
            resource_id=lambda response: response.operation_id,
            recover=lambda _operation_id: session.client.abandon_transition(
                successor_transition_id,
                request,
                idempotency_key=idempotency_key,
            ),
            response_type=core_v2.OperationV2,
        )

    def submit_task(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        request: core_v2.TaskSubmitRequestV2,
        *,
        idempotency_key: str,
    ) -> core_v2.TaskV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        if type(request) is not core_v2.TaskSubmitRequestV2:
            raise TypeError("task submission requires an exact Core v2 request")
        return self._durable_mutation(
            session=session,
            operation="submit_task_v2",
            resource_scope=session.activation.mapping.core_project_id,
            request_sha256=_model_sha256(request),
            idempotency_key=idempotency_key,
            invoke=lambda: session.client.submit_task(request, idempotency_key=idempotency_key),
            resource_id=lambda task: task.task_id,
            recover=lambda task_id: session.client.get_task(task_id),
            response_type=core_v2.TaskV2,
        )

    def list_tasks(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        *,
        limit: int = 50,
        after: str | None = None,
    ) -> core_v2.TaskPageV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        return self._session_core(
            session,
            lambda: session.client.list_tasks(limit=limit, after=after),
            desktop_project_id,
        )

    def get_task(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        task_id: str,
    ) -> core_v2.TaskV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        return self._session_core(
            session,
            lambda: session.client.get_task(task_id),
            desktop_project_id,
        )

    def get_task_admission(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        task_id: str,
    ) -> core_v2.TaskAdmissionRefV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        return self._session_core(
            session,
            lambda: session.client.get_task_admission(task_id),
            desktop_project_id,
        )

    def list_task_attempts(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        task_id: str,
        *,
        limit: int = 50,
        after: str | None = None,
    ) -> core_v2.AttemptPageV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        return self._session_core(
            session,
            lambda: session.client.list_task_attempts(task_id, limit=limit, after=after),
            desktop_project_id,
        )

    def get_task_attempt(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        task_id: str,
        attempt_id: str,
    ) -> core_v2.AttemptRefV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        return self._session_core(
            session,
            lambda: session.client.get_task_attempt(task_id, attempt_id),
            desktop_project_id,
        )

    def append_task_attempt(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        task_id: str,
        request: core_v2.AttemptAppendRequestV2,
        *,
        idempotency_key: str,
    ) -> core_v2.AttemptRefV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        _require_exact_request(request, core_v2.AttemptAppendRequestV2)
        return self._durable_mutation(
            session=session,
            operation="append_task_attempt_v2",
            resource_scope=task_id,
            request_sha256=_mutation_request_sha256(request, resource_id=task_id),
            idempotency_key=idempotency_key,
            invoke=lambda: session.client.append_task_attempt(
                task_id, request, idempotency_key=idempotency_key
            ),
            resource_id=lambda response: response.attempt_id,
            recover=lambda attempt_id: session.client.get_task_attempt(task_id, attempt_id),
            response_type=core_v2.AttemptRefV2,
        )

    def cancel_task_attempt(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        task_id: str,
        attempt_id: str,
        request: core_v2.TaskActionRequestV2,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> core_v2.OperationV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        _require_exact_request(request, core_v2.TaskActionRequestV2)
        scope = _scope_id("task-attempt", task_id, attempt_id)
        return self._durable_mutation(
            session=session,
            operation="cancel_task_attempt_v2",
            resource_scope=scope,
            request_sha256=_mutation_request_sha256(request, resource_id=scope, if_match=if_match),
            idempotency_key=idempotency_key,
            invoke=lambda: session.client.cancel_task_attempt(
                task_id,
                attempt_id,
                request,
                if_match=if_match,
                idempotency_key=idempotency_key,
            ),
            resource_id=lambda response: response.operation_id,
            recover=lambda _operation_id: session.client.cancel_task_attempt(
                task_id,
                attempt_id,
                request,
                if_match=if_match,
                idempotency_key=idempotency_key,
            ),
            response_type=core_v2.OperationV2,
        )

    def close_task(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        task_id: str,
        request: core_v2.TaskActionRequestV2,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> core_v2.OperationV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        _require_exact_request(request, core_v2.TaskActionRequestV2)
        return self._durable_mutation(
            session=session,
            operation="close_task_v2",
            resource_scope=task_id,
            request_sha256=_mutation_request_sha256(
                request, resource_id=task_id, if_match=if_match
            ),
            idempotency_key=idempotency_key,
            invoke=lambda: session.client.close_task(
                task_id,
                request,
                if_match=if_match,
                idempotency_key=idempotency_key,
            ),
            resource_id=lambda response: response.operation_id,
            recover=lambda _operation_id: session.client.close_task(
                task_id,
                request,
                if_match=if_match,
                idempotency_key=idempotency_key,
            ),
            response_type=core_v2.OperationV2,
        )

    def task_timeline(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        task_id: str,
        *,
        limit: int = 50,
        after: str | None = None,
    ) -> core_v2.TimelinePageV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        return self._session_core(
            session,
            lambda: session.client.task_timeline(task_id, limit=limit, after=after),
            desktop_project_id,
        )

    def task_logs(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        task_id: str,
        *,
        limit: int = 100,
        after: str | None = None,
    ) -> core_v2.LogPageV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        return self._session_core(
            session,
            lambda: session.client.task_logs(task_id, limit=limit, after=after),
            desktop_project_id,
        )

    def task_context(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        task_id: str,
    ) -> core_v2.TaskContextV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        return self._session_core(
            session,
            lambda: session.client.task_context(task_id),
            desktop_project_id,
        )

    def task_artifacts(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        task_id: str,
        *,
        limit: int = 50,
        after: str | None = None,
    ) -> core_v2.ArtifactPageV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        return self._session_core(
            session,
            lambda: session.client.task_artifacts(task_id, limit=limit, after=after),
            desktop_project_id,
        )

    def get_artifact(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        artifact_id: str,
    ) -> core_v2.ArtifactV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        return self._session_core(
            session,
            lambda: session.client.get_artifact(artifact_id),
            desktop_project_id,
        )

    def artifact_content(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        artifact_id: str,
    ) -> core_v2.ArtifactContentV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        return self._session_core(
            session,
            lambda: session.client.artifact_content(artifact_id),
            desktop_project_id,
        )

    def list_services(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        *,
        limit: int = 50,
        after: str | None = None,
    ) -> core_v2.ServicePageV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        return self._session_core(
            session,
            lambda: session.client.list_services(limit=limit, after=after),
            desktop_project_id,
        )

    def get_service(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        service_id: str,
    ) -> core_v2.ServiceV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        return self._session_core(
            session,
            lambda: session.client.get_service(service_id),
            desktop_project_id,
        )

    def restart_service(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        service_id: str,
        request: core_v2.ActionRequestV2,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> core_v2.OperationV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        _require_exact_request(request, core_v2.ActionRequestV2)
        return self._durable_mutation(
            session=session,
            operation="restart_service_v2",
            resource_scope=service_id,
            request_sha256=_mutation_request_sha256(
                request, resource_id=service_id, if_match=if_match
            ),
            idempotency_key=idempotency_key,
            invoke=lambda: session.client.restart_service(
                service_id,
                request,
                if_match=if_match,
                idempotency_key=idempotency_key,
            ),
            resource_id=lambda response: response.operation_id,
            recover=lambda _operation_id: session.client.restart_service(
                service_id,
                request,
                if_match=if_match,
                idempotency_key=idempotency_key,
            ),
            response_type=core_v2.OperationV2,
        )

    def service_logs(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        service_id: str,
        *,
        limit: int = 100,
        after: str | None = None,
    ) -> core_v2.LogPageV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        return self._session_core(
            session,
            lambda: session.client.service_logs(service_id, limit=limit, after=after),
            desktop_project_id,
        )

    def get_operation(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        operation_id: str,
    ) -> core_v2.OperationV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        return self._session_core(
            session,
            lambda: session.client.get_operation(operation_id),
            desktop_project_id,
        )

    def cancel_operation(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        operation_id: str,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> core_v2.OperationV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        _require_opaque(operation_id)
        _require_etag(if_match)
        request_digest = _canonical_value_sha256(
            {
                "schema_version": "2",
                "operation": "cancel_operation_v2",
                "operation_id": operation_id,
                "if_match": if_match,
            }
        )
        return self._durable_mutation(
            session=session,
            operation="cancel_operation_v2",
            resource_scope=operation_id,
            request_sha256=request_digest,
            idempotency_key=idempotency_key,
            invoke=lambda: session.client.cancel_operation(
                operation_id,
                if_match=if_match,
                idempotency_key=idempotency_key,
            ),
            resource_id=lambda response: response.operation_id,
            recover=lambda _resource_id: session.client.cancel_operation(
                operation_id,
                if_match=if_match,
                idempotency_key=idempotency_key,
            ),
            response_type=core_v2.OperationV2,
        )

    def create_diagnostic(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        request: core_v2.DiagnosticRequestV2,
        *,
        idempotency_key: str,
    ) -> core_v2.DiagnosticV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        _require_exact_request(request, core_v2.DiagnosticRequestV2)
        return self._durable_mutation(
            session=session,
            operation="create_diagnostic_v2",
            resource_scope=session.activation.mapping.core_project_id,
            request_sha256=_model_sha256(request),
            idempotency_key=idempotency_key,
            invoke=lambda: session.client.create_diagnostic(
                request, idempotency_key=idempotency_key
            ),
            resource_id=lambda response: response.diagnostic_id,
            recover=lambda _diagnostic_id: session.client.create_diagnostic(
                request, idempotency_key=idempotency_key
            ),
            response_type=core_v2.DiagnosticV2,
        )

    def get_diagnostic(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        diagnostic_id: str,
    ) -> core_v2.DiagnosticV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        return self._session_core(
            session,
            lambda: session.client.get_diagnostic(diagnostic_id),
            desktop_project_id,
        )

    def delete_diagnostic(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        diagnostic_id: str,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> None:
        session = self._session(desktop_project_id, profile_connection_generation)
        _require_opaque(diagnostic_id)
        _require_etag(if_match)
        self._durable_empty_mutation(
            session=session,
            operation="delete_diagnostic_v2",
            resource_scope=diagnostic_id,
            request_sha256=_canonical_value_sha256(
                {
                    "schema_version": "2",
                    "operation": "delete_diagnostic_v2",
                    "diagnostic_id": diagnostic_id,
                    "if_match": if_match,
                }
            ),
            idempotency_key=idempotency_key,
            invoke=lambda: session.client.delete_diagnostic(
                diagnostic_id,
                if_match=if_match,
                idempotency_key=idempotency_key,
            ),
        )

    def cache_cleanup(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        request: core_v2.CacheCleanupRequestV2,
        *,
        idempotency_key: str,
    ) -> core_v2.OperationV2:
        session = self._session(desktop_project_id, profile_connection_generation)
        _require_exact_request(request, core_v2.CacheCleanupRequestV2)
        return self._durable_mutation(
            session=session,
            operation="cache_cleanup_v2",
            resource_scope=session.activation.mapping.core_project_id,
            request_sha256=_model_sha256(request),
            idempotency_key=idempotency_key,
            invoke=lambda: session.client.cache_cleanup(request, idempotency_key=idempotency_key),
            resource_id=lambda response: response.operation_id,
            recover=lambda _operation_id: session.client.cache_cleanup(
                request, idempotency_key=idempotency_key
            ),
            response_type=core_v2.OperationV2,
        )

    def events(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        *,
        last_event_id: str | None = None,
    ) -> AbstractContextManager[Iterator[core_v2.SseFrameV2]]:
        session = self._session(desktop_project_id, profile_connection_generation)
        mapping_cursor = session.activation.mapping.last_core_event_id
        if (
            last_event_id is not None
            and mapping_cursor is not None
            and last_event_id != mapping_cursor
        ):
            raise _bridge_error(
                "core_event_cursor_mismatch",
                "The requested Core event cursor differs from durable Desktop authority.",
                status=409,
                action="reconnect",
                affected_resource_id=desktop_project_id,
            )
        return _BridgeEventContextV2(
            bridge=self,
            session=session,
            last_event_id=last_event_id or mapping_cursor,
        )

    def _bootstrap_project(
        self,
        *,
        desktop_project_id: str,
        request: local_v2.ProjectCreateV2,
        create: core_v2.ProjectCreateV2,
        idempotency_key: str,
        attachment: CoreHostAttachmentV2,
        tunnel: CoreTunnelHandleV2,
        deadline: float,
    ) -> tuple[CoreTunnelConnectionV2, core_v2.VersionResponseV2]:
        replay = self._load_or_reserve_mutation(
            desktop_project_id=desktop_project_id,
            profile_id=request.profile_id,
            profile_connection_generation=request.profile_connection_generation,
            operation="create_project_v2",
            resource_scope=desktop_project_id,
            idempotency_key=idempotency_key,
            request_sha256=_model_sha256(create),
        )
        bootstrap_connection = CoreBootstrapTunnelConnectionV2(
            endpoint=tunnel.endpoint,
            bearer_token=attachment.bearer_token,
            profile_id=request.profile_id,
            profile_connection_generation=request.profile_connection_generation,
            session_id=tunnel.session_id,
        )
        observed_sequences: dict[str, int] = {}
        observed_cursors: dict[str, str] = {}
        if replay.state is CoreBridgeMutationStateV2.APPLIED:
            assert replay.response_resource_id is not None
            connection = bootstrap_connection.bind(replay.response_resource_id)
            probe = self._new_project_create_client(connection, deadline)
            try:
                version = self._call_core(probe.version, desktop_project_id)
                project = self._call_core(probe.get_project, desktop_project_id)
                if project.project_id != replay.response_resource_id:
                    raise _bridge_error(
                        "core_mutation_replay_drift",
                        "The recovered Core project differs from its durable mutation result.",
                        status=409,
                        action="install_repair_daemon",
                        affected_resource_id=desktop_project_id,
                    )
                self._wait_for_scratch_project_ready(
                    probe,
                    request=create,
                    initial=project,
                    expected_version=version,
                    deadline=deadline,
                    affected_resource_id=desktop_project_id,
                    observed_sequences=observed_sequences,
                    observed_cursors=observed_cursors,
                )
            finally:
                self._close_client(probe, suppress_errors=True)
            return connection, version
        bootstrap = self._new_bootstrap_client(bootstrap_connection, deadline)
        try:
            version = self._call_core(bootstrap.version, desktop_project_id)
            status = self._call_core(bootstrap.system_status, desktop_project_id)
            if status.status != "ready":
                raise _bridge_error(
                    "core_not_ready",
                    "The OpenEvo Daemon is not ready to create a project.",
                    status=503,
                    retryable=True,
                    action="install_repair_daemon",
                    affected_resource_id=desktop_project_id,
                )
            try:
                result = self._create_project_with_progress(
                    bootstrap,
                    create,
                    idempotency_key=idempotency_key,
                    deadline=deadline,
                    observed_sequences=observed_sequences,
                    observed_cursors=observed_cursors,
                )
            except CoreMutationOutcomeUnknownV2:
                self._mark_unknown(replay)
                raise
            self._mark_applied(
                replay,
                response=result.project,
                response_resource_id=result.project.project_id,
            )
            if (
                create.config.workspace.kind != "native_folder_snapshot"
                and result.project.state != "ready"
            ):
                probe = self._new_project_create_client(result.connection, deadline)
                try:
                    self._wait_for_scratch_project_ready(
                        probe,
                        request=create,
                        initial=result.project,
                        expected_version=version,
                        deadline=deadline,
                        affected_resource_id=desktop_project_id,
                        observed_sequences=observed_sequences,
                        observed_cursors=observed_cursors,
                    )
                finally:
                    self._close_client(probe, suppress_errors=True)
            return result.connection, version
        finally:
            self._close_bootstrap(bootstrap, suppress_errors=True)

    def _create_project_with_progress(
        self,
        bootstrap: CoreProjectBootstrapClientV2,
        create: core_v2.ProjectCreateV2,
        *,
        idempotency_key: str,
        deadline: float,
        observed_sequences: dict[str, int],
        observed_cursors: dict[str, str],
    ) -> CoreProjectBootstrapResultV2:
        if not _PROJECT_CREATE_CAPACITY.acquire(blocking=False):
            raise _bridge_error(
                "core_project_create_capacity_exhausted",
                "Desktop cannot schedule another remote project creation.",
                status=503,
                retryable=True,
                action="retry",
            )

        def create_owned() -> CoreProjectBootstrapResultV2:
            return bootstrap.create_project(create, idempotency_key=idempotency_key)

        future: Future[CoreProjectBootstrapResultV2] = Future()

        def run_owned() -> None:
            try:
                if not future.set_running_or_notify_cancel():
                    return
                try:
                    result = create_owned()
                except BaseException as exc:
                    future.set_exception(exc)
                else:
                    future.set_result(result)
            finally:
                _PROJECT_CREATE_CAPACITY.release()

        try:
            worker = threading.Thread(
                target=run_owned,
                name="openevo-core-v2-project-create",
                daemon=True,
            )
            worker.start()
        except BaseException:
            _PROJECT_CREATE_CAPACITY.release()
            raise
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                future.cancel()
                raise CoreMutationOutcomeUnknownV2
            try:
                return future.result(timeout=min(_PROJECT_CREATE_PROGRESS_POLL_SECONDS, remaining))
            except FutureTimeoutError:
                self._observe_project_create_services(
                    bootstrap,
                    observed_sequences,
                    observed_cursors,
                )

    def _wait_for_scratch_project_ready(
        self,
        client: CoreControlClientV2,
        *,
        request: core_v2.ProjectCreateV2,
        initial: core_v2.ProjectV2,
        expected_version: core_v2.VersionResponseV2,
        deadline: float,
        affected_resource_id: str,
        observed_sequences: dict[str, int],
        observed_cursors: dict[str, str],
    ) -> core_v2.ProjectV2:
        """Finish a durable scratch create after Core acknowledges it as not ready."""

        if request.config.workspace.kind == "native_folder_snapshot":
            return initial
        negotiated = self._call_core(client.version, affected_resource_id)
        if negotiated != expected_version:
            raise _bridge_error(
                "core_authority_changed",
                "The negotiated Core authority changed while creating the project.",
                status=502,
                action="install_repair_daemon",
                affected_resource_id=affected_resource_id,
            )
        project = initial
        while True:
            self._validate_project_intent(
                project,
                request,
                allow_pending_scratch=True,
            )
            if project.state == "ready":
                return project
            if (
                project.state != "not_ready"
                or project.active_project_head is not None
                or project.admission_etag is not None
            ):
                raise _bridge_error(
                    "core_project_readiness_invalid",
                    "The Core project entered an invalid creation readiness state.",
                    status=502,
                    action="install_repair_daemon",
                    affected_resource_id=affected_resource_id,
                )
            services = self._observe_project_create_services(
                client,
                observed_sequences,
                observed_cursors,
            )
            unavailable = (
                []
                if services is None
                else [
                    service.service_id
                    for service in services.items
                    if service.status == "unavailable"
                ]
            )
            if unavailable:
                if self._output_observer is not None:
                    names = ", ".join(unavailable)
                    self._output_observer(
                        "daemon_stderr",
                        (
                            "[desktop] Required remote services are unavailable: "
                            f"{names}.\n"
                        ).encode("utf-8"),
                    )
                raise _bridge_error(
                    "core_project_services_unavailable",
                    "The remote services required to create this project are unavailable.",
                    status=503,
                    action="install_repair_daemon",
                    affected_resource_id=affected_resource_id,
                )
            remaining = _remaining(deadline)
            time.sleep(min(_PROJECT_CREATE_PROGRESS_POLL_SECONDS, remaining))
            project = self._call_core(client.get_project, affected_resource_id)

    def _observe_project_create_services(
        self,
        bootstrap: CoreControlClientV2 | CoreProjectBootstrapClientV2,
        observed_sequences: dict[str, int],
        observed_cursors: dict[str, str],
    ) -> core_v2.ServicePageV2 | None:
        try:
            services = bootstrap.list_services(limit=100)
            if self._output_observer is None and self._progress_observer is None:
                return services
            for service in services.items:
                after: str | None = observed_cursors.get(service.service_id)
                pages = 0
                while pages < 8:
                    page = bootstrap.service_logs(
                        service.service_id,
                        limit=100,
                        after=after,
                    )
                    pages += 1
                    for entry in page.items:
                        if entry.sequence <= observed_sequences.get(service.service_id, 0):
                            continue
                        observed_sequences[service.service_id] = entry.sequence
                        self._observe_project_create_log(service.service_id, entry)
                    if not page.has_more:
                        break
                    if page.next_cursor is None or page.next_cursor == after:
                        break
                    after = page.next_cursor
                    observed_cursors[service.service_id] = after
            return services
        except (CoreClientErrorV2, CoreMutationOutcomeUnknownV2):
            return

    def _observe_project_create_log(
        self,
        service_id: str,
        entry: core_v2.LogEntryV2,
    ) -> None:
        output = self._output_observer
        if output is not None:
            source = "daemon_stderr" if entry.stream == "stderr" else "daemon_stdout"
            output(source, f"[{service_id}] {entry.message}\n".encode("utf-8"))
        match = _MODEL_DOWNLOAD_PROGRESS.fullmatch(entry.message)
        if match is None:
            return
        completed = int(match.group(1))
        total = int(match.group(2))
        if not 0 <= completed <= total or total <= 0:
            return
        self._observe_lifecycle_progress(
            "creating_remote_project",
            cancellable=False,
            progress=local_v2.LifecycleProgressBytesV2(
                kind="bytes",
                completed=completed,
                total=total,
            ),
        )

    def _load_or_reserve_mutation(
        self,
        *,
        desktop_project_id: str,
        profile_id: str,
        profile_connection_generation: int,
        operation: str,
        resource_scope: str,
        idempotency_key: str,
        request_sha256: str,
    ) -> CoreBridgeMutationV2:
        _require_idempotency_key(idempotency_key)
        loaded = self._call_adapter(
            lambda: self._persistence.load_mutation(
                desktop_project_id, operation, idempotency_key
            ),
            failure_code="core_mutation_ledger_unavailable",
            failure_summary="Desktop could not read the Core mutation replay ledger.",
        )
        if loaded is not None:
            if (
                loaded.profile_id != profile_id
                or loaded.profile_connection_generation > profile_connection_generation
                or loaded.resource_scope != resource_scope
                or loaded.request_sha256 != request_sha256
            ):
                raise _bridge_error(
                    "core_mutation_identity_conflict",
                    "The Core mutation idempotency identity was reused.",
                    status=409,
                    action="correct_project",
                    affected_resource_id=desktop_project_id,
                )
            return loaded
        prepared = CoreBridgeMutationV2(
            desktop_project_id=desktop_project_id,
            profile_id=profile_id,
            profile_connection_generation=profile_connection_generation,
            operation=operation,
            resource_scope=resource_scope,
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
            state=CoreBridgeMutationStateV2.PREPARED,
            response_sha256=None,
            response_resource_id=None,
        )
        return self._call_adapter(
            lambda: self._persistence.reserve_mutation(prepared),
            failure_code="core_mutation_ledger_unavailable",
            failure_summary="Desktop could not reserve the Core mutation replay identity.",
        )

    def _durable_mutation(
        self,
        *,
        session: _ActiveSessionV2,
        operation: str,
        resource_scope: str,
        request_sha256: str,
        idempotency_key: str,
        invoke: Callable[[], Any],
        resource_id: Callable[[Any], str],
        recover: Callable[[str], Any],
        response_type: type[Any],
    ) -> Any:
        replay = self._load_or_reserve_mutation(
            desktop_project_id=session.activation.desktop_project_id,
            profile_id=session.activation.profile_id,
            profile_connection_generation=session.activation.profile_connection_generation,
            operation=operation,
            resource_scope=resource_scope,
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
        )
        if replay.state is CoreBridgeMutationStateV2.APPLIED:
            assert replay.response_resource_id is not None
            response = self._call_core(
                lambda: recover(replay.response_resource_id),
                session.activation.desktop_project_id,
            )
            if (
                type(response) is not response_type
                or resource_id(response) != replay.response_resource_id
            ):
                raise _bridge_error(
                    "core_mutation_replay_drift",
                    "The Core mutation replay result changed.",
                    status=409,
                    action="install_repair_daemon",
                    affected_resource_id=session.activation.desktop_project_id,
                )
            with self._lock:
                self._ensure_current_locked(session)
            return response
        try:
            response = invoke()
        except CoreMutationOutcomeUnknownV2:
            self._mark_unknown(replay)
            raise _mutation_unknown_error(session.activation.desktop_project_id) from None
        except CoreClientErrorV2 as exc:
            raise _bridge_client_error(exc, session.activation.desktop_project_id) from None
        if type(response) is not response_type:
            raise _bridge_error(
                "core_authority_invalid",
                "Core returned an unexpected mutation result type.",
                status=502,
                action="install_repair_daemon",
                affected_resource_id=session.activation.desktop_project_id,
            )
        self._mark_applied(
            replay,
            response=response,
            response_resource_id=resource_id(response),
        )
        with self._lock:
            self._ensure_current_locked(session)
        return response

    def _mark_unknown(self, replay: CoreBridgeMutationV2) -> None:
        self._call_adapter(
            lambda: self._persistence.mark_mutation_unknown(replay),
            failure_code="core_mutation_ledger_unavailable",
            failure_summary="Desktop could not record the unknown Core mutation outcome.",
        )

    def _durable_empty_mutation(
        self,
        *,
        session: _ActiveSessionV2,
        operation: str,
        resource_scope: str,
        request_sha256: str,
        idempotency_key: str,
        invoke: Callable[[], None],
    ) -> None:
        replay = self._load_or_reserve_mutation(
            desktop_project_id=session.activation.desktop_project_id,
            profile_id=session.activation.profile_id,
            profile_connection_generation=session.activation.profile_connection_generation,
            operation=operation,
            resource_scope=resource_scope,
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
        )
        if replay.state is CoreBridgeMutationStateV2.APPLIED:
            with self._lock:
                self._ensure_current_locked(session)
            return
        try:
            invoke()
        except CoreMutationOutcomeUnknownV2:
            self._mark_unknown(replay)
            raise _mutation_unknown_error(session.activation.desktop_project_id) from None
        except CoreClientErrorV2 as exc:
            raise _bridge_client_error(exc, session.activation.desktop_project_id) from None
        receipt = _MutationReceiptV2(
            operation=operation,
            resource_id=resource_scope,
        )
        self._mark_applied(
            replay,
            response=receipt,
            response_resource_id=resource_scope,
        )
        with self._lock:
            self._ensure_current_locked(session)

    def _mark_applied(
        self,
        replay: CoreBridgeMutationV2,
        *,
        response: core_v2.ContractModel,
        response_resource_id: str,
    ) -> None:
        self._call_adapter(
            lambda: self._persistence.mark_mutation_applied(
                replay,
                response_sha256=_model_sha256(response),
                response_resource_id=response_resource_id,
            ),
            failure_code="core_mutation_ledger_unavailable",
            failure_summary="Desktop could not commit the Core mutation outcome.",
        )

    def _refresh_mapping(
        self,
        session: _ActiveSessionV2,
        *,
        project: core_v2.ProjectV2,
        event: core_v2.EventEnvelopeV2 | None = None,
    ) -> CoreProjectMappingV2:
        with self._lock:
            self._ensure_current_locked(session)
            previous = session.activation.mapping
            proof = self._head_successor_proof(
                client=session.client,
                previous=previous.active_project_head,
                current=project.active_project_head,
            )
            event_id = previous.last_core_event_id
            event_sequence = previous.last_core_event_sequence
            event_digest = previous.last_core_event_payload_sha256
            if event is not None:
                digest = _model_sha256(event)
                if event_sequence is not None and event.sequence <= event_sequence:
                    if (
                        event.sequence == event_sequence
                        and event.event_id == event_id
                        and digest == event_digest
                    ):
                        return previous
                    raise _bridge_error(
                        "core_event_cursor_conflict",
                        "Core event replay differs from durable Desktop authority.",
                        status=409,
                        action="reconnect",
                        affected_resource_id=session.activation.desktop_project_id,
                    )
                event_id = event.event_id
                event_sequence = event.sequence
                event_digest = digest
            mapping = self._mapping_from_authority(
                desktop_project_id=session.activation.desktop_project_id,
                request=local_v2.ProjectCreateV2(
                    profile_id=session.activation.profile_id,
                    profile_connection_generation=session.activation.profile_connection_generation,
                    display_name=project.display_name,
                    config=project.config,
                ),
                project=project,
                version=session.activation.version,
                previous=previous,
                head_proof=proof,
                last_event_id=event_id,
                last_event_sequence=event_sequence,
                last_event_payload_sha256=event_digest,
            )
            if self._same_mapping_authority(previous, mapping):
                return previous
            self._call_adapter(
                lambda: self._persistence.commit_mapping(mapping, expected_previous=previous),
                failure_code="core_mapping_commit_failed",
                failure_summary="Desktop could not commit refreshed Core authority.",
            )
            session.activation = replace(
                session.activation,
                project=project,
                mapping=mapping,
            )
            return mapping

    def _record_event(
        self,
        session: _ActiveSessionV2,
        frame: core_v2.SseFrameV2,
    ) -> None:
        project = session.activation.project
        if isinstance(frame.data, core_v2.ProjectHeadActivatedEventV2):
            project = self._call_core(
                session.client.get_project,
                session.activation.desktop_project_id,
            )
            if project.active_project_head != frame.data.project_head:
                raise _bridge_error(
                    "core_event_project_mismatch",
                    "The Core project snapshot does not include the activated head event.",
                    status=409,
                    retryable=True,
                    action="retry",
                    affected_resource_id=session.activation.desktop_project_id,
                )
        mapping = self._refresh_mapping(session, project=project, event=frame.data)
        if self._event_publisher is not None:
            payload = local_v2.CoreAuthorityEventPayloadV2(
                payload_kind="core_authority_changed",
                profile_id=session.activation.profile_id,
                project_id=session.activation.desktop_project_id,
                core_event_id=frame.data.event_id,
                core_event_sequence=frame.data.sequence,
                core_event_type=frame.data.event_type,
                core_payload_sha256=cast_str(mapping.last_core_event_payload_sha256),
            )
            try:
                self._event_publisher.publish(payload)
            except Exception:
                raise _bridge_error(
                    "desktop_event_publication_failed",
                    "Desktop could not publish the committed Core authority event.",
                    status=503,
                    retryable=True,
                    action="retry",
                    affected_resource_id=session.activation.desktop_project_id,
                ) from None

    def _mapping_from_authority(
        self,
        *,
        desktop_project_id: str,
        request: local_v2.ProjectCreateV2,
        project: core_v2.ProjectV2,
        version: core_v2.VersionResponseV2,
        previous: CoreProjectMappingV2 | None,
        head_proof: tuple[core_v2.ProjectHeadRefV2, ...],
        last_event_id: str | None = None,
        last_event_sequence: int | None = None,
        last_event_payload_sha256: str | None = None,
    ) -> CoreProjectMappingV2:
        offer = next((item for item in version.contracts if item.api_major == 2), None)
        if offer is None:
            raise ValueError("negotiated v2 contract offer is absent")
        return CoreProjectMappingV2(
            desktop_project_id=desktop_project_id,
            profile_id=request.profile_id,
            profile_connection_generation=request.profile_connection_generation,
            core_project_id=project.project_id,
            project_config_sha256=project.project_config_sha256,
            project_etag=project.etag,
            project_admission_etag=project.admission_etag,
            active_project_head=project.active_project_head,
            project_head_successor_proof=head_proof,
            daemon_release_version=version.release_version,
            daemon_build_id=version.build_id,
            daemon_source_commit=version.source_commit,
            daemon_openapi_sha256=offer.openapi_sha256,
            daemon_event_schema_sha256=offer.event_schema_sha256,
            daemon_registry_sha256=version.registry_sha256,
            daemon_runtime_contract_sha256=version.runtime_contract_sha256,
            core_project=project,
            core_version=version,
            mapping_generation=1 if previous is None else previous.mapping_generation + 1,
            predecessor_mapping_sha256=(
                None if previous is None else core_project_mapping_sha256_v2(previous)
            ),
            last_core_event_id=(
                last_event_id
                if last_event_id is not None
                else None
                if previous is None
                else previous.last_core_event_id
            ),
            last_core_event_sequence=(
                last_event_sequence
                if last_event_sequence is not None
                else None
                if previous is None
                else previous.last_core_event_sequence
            ),
            last_core_event_payload_sha256=(
                last_event_payload_sha256
                if last_event_payload_sha256 is not None
                else None
                if previous is None
                else previous.last_core_event_payload_sha256
            ),
        )

    @staticmethod
    def _same_mapping_authority(
        previous: CoreProjectMappingV2,
        candidate: CoreProjectMappingV2,
    ) -> bool:
        previous_document = core_project_mapping_document_v2(previous)
        candidate_document = core_project_mapping_document_v2(candidate)
        for key in (
            "mapping_generation",
            "predecessor_mapping_sha256",
            "project_head_successor_proof",
        ):
            previous_document.pop(key)
            candidate_document.pop(key)
        return previous_document == candidate_document

    def _head_successor_proof(
        self,
        *,
        client: CoreControlClientV2,
        previous: core_v2.ProjectHeadRefV2 | None,
        current: core_v2.ProjectHeadRefV2 | None,
    ) -> tuple[core_v2.ProjectHeadRefV2, ...]:
        if previous == current:
            return ()
        if current is None:
            if previous is None:
                return ()
            raise _bridge_error(
                "core_project_head_regressed",
                "Core removed the active project head.",
                status=409,
                action="install_repair_daemon",
            )
        if previous is None:
            if current.generation != 0 or current.predecessor_project_head_id is not None:
                raise _bridge_error(
                    "core_project_head_invalid",
                    "The first Core project head is not generation zero.",
                    status=409,
                    action="install_repair_daemon",
                )
            return (current,)
        if current.generation <= previous.generation:
            raise _bridge_error(
                "core_project_head_regressed",
                "Core returned an older active project head.",
                status=409,
                action="install_repair_daemon",
            )
        reverse: list[core_v2.ProjectHeadRefV2] = []
        cursor = current
        while cursor.project_head_id != previous.project_head_id:
            if len(reverse) >= MAX_MAPPING_HISTORY_PROOF_GENERATIONS:
                raise _bridge_error(
                    "core_project_head_proof_too_long",
                    "The Core project-head successor proof exceeds its bound.",
                    status=409,
                    action="install_repair_daemon",
                )
            reverse.append(cursor)
            predecessor_id = cursor.predecessor_project_head_id
            if predecessor_id is None:
                raise _bridge_error(
                    "core_project_head_proof_broken",
                    "The Core project-head successor chain is incomplete.",
                    status=409,
                    action="install_repair_daemon",
                )
            if predecessor_id == previous.project_head_id:
                cursor = previous
            else:
                cursor = self._call_core(
                    lambda predecessor_id=predecessor_id: client.get_project_head(predecessor_id)
                )
        reverse.reverse()
        predecessor = previous
        for head in reverse:
            if (
                head.generation != predecessor.generation + 1
                or head.predecessor_project_head_id != predecessor.project_head_id
            ):
                raise _bridge_error(
                    "core_project_head_proof_broken",
                    "The Core project-head successor chain is not contiguous.",
                    status=409,
                    action="install_repair_daemon",
                )
            predecessor = head
        return tuple(reverse)

    @staticmethod
    def _validate_project_intent(
        project: core_v2.ProjectV2,
        request: core_v2.ProjectCreateV2,
        *,
        allow_pending_scratch: bool = False,
    ) -> None:
        common_mismatch = (
            project.display_name != request.display_name
            or project.config != request.config
            or project.project_config_sha256 != core_v2.project_config_sha256_for(request.config)
        )
        if request.config.workspace.kind == "native_folder_snapshot":
            if project.active_project_head is None:
                invalid_authority = (
                    project.state != "not_ready" or project.admission_etag is not None
                )
            else:
                invalid_authority = project.admission_etag is None
        elif allow_pending_scratch and project.active_project_head is None:
            invalid_authority = project.state != "not_ready" or project.admission_etag is not None
        else:
            invalid_authority = (
                project.active_project_head is None or project.admission_etag is None
            )
        if common_mismatch or invalid_authority:
            raise _bridge_error(
                "core_project_intent_mismatch",
                "The Core project differs from the saved Desktop project intent.",
                status=409,
                action="correct_project",
            )

    @staticmethod
    def _validate_existing_mapping(
        mapping: CoreProjectMappingV2,
        desktop_project_id: str,
        request: local_v2.ProjectCreateV2,
    ) -> None:
        if (
            mapping.desktop_project_id != desktop_project_id
            or mapping.profile_id != request.profile_id
            or mapping.profile_connection_generation > request.profile_connection_generation
            or mapping.core_project.display_name != request.display_name
            or mapping.core_project.config != request.config
            or mapping.project_config_sha256 != core_v2.project_config_sha256_for(request.config)
        ):
            raise _bridge_error(
                "core_project_mapping_conflict",
                "The durable Core project mapping differs from the Desktop project.",
                status=409,
                action="correct_project",
                affected_resource_id=desktop_project_id,
            )

    @staticmethod
    def _validate_tunnel(
        tunnel: CoreTunnelHandleV2,
        request: local_v2.ProjectCreateV2,
        session_id: str,
    ) -> None:
        if type(tunnel) is not CoreTunnelHandleV2 or (
            tunnel.profile_id != request.profile_id
            or tunnel.profile_connection_generation != request.profile_connection_generation
            or tunnel.session_id != session_id
            or tunnel.closed
        ):
            raise _bridge_error(
                "core_tunnel_authority_mismatch",
                "The active project tunnel belongs to another profile generation.",
                status=409,
                action="reconnect",
            )

    def _session(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
    ) -> _ActiveSessionV2:
        with self._lock:
            return self._active_session_locked(desktop_project_id, profile_connection_generation)

    def _active_session_locked(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
    ) -> _ActiveSessionV2:
        _require_opaque(desktop_project_id)
        if type(profile_connection_generation) is not int:
            raise TypeError("profile connection generation must be an exact integer")
        session = self._active
        if self._closed or session is None:
            raise _bridge_error(
                "active_core_project_required",
                "Connect and activate this remote project before continuing.",
                status=409,
                retryable=True,
                action="reconnect",
                affected_resource_id=desktop_project_id,
            )
        activation = session.activation
        if (
            activation.desktop_project_id != desktop_project_id
            or activation.profile_connection_generation != profile_connection_generation
            or activation.bridge_generation != self._generation
        ):
            raise _bridge_error(
                "active_core_project_mismatch",
                "A newer profile or project generation owns the active Core tunnel.",
                status=409,
                retryable=True,
                action="reconnect",
                affected_resource_id=desktop_project_id,
            )
        return session

    def _deactivation_session_locked(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
    ) -> tuple[_ActiveSessionV2, bool]:
        """Resolve active or retained cleanup authority for an exact retry."""

        _require_opaque(desktop_project_id)
        if type(profile_connection_generation) is not int:
            raise TypeError("profile connection generation must be an exact integer")
        if self._closed:
            raise _bridge_error(
                "active_core_project_required",
                "Connect and activate this remote project before continuing.",
                status=409,
                retryable=True,
                action="reconnect",
                affected_resource_id=desktop_project_id,
            )
        active = self._active
        if active is not None:
            activation = active.activation
            if (
                activation.desktop_project_id == desktop_project_id
                and activation.profile_connection_generation == profile_connection_generation
                and activation.bridge_generation == self._generation
            ):
                return active, True
        retained = tuple(
            session
            for session in self._retained
            if session.activation.desktop_project_id == desktop_project_id
            and session.activation.profile_connection_generation == profile_connection_generation
        )
        if len(retained) == 1:
            return retained[0], False
        if active is None:
            raise _bridge_error(
                "active_core_project_required",
                "Connect and activate this remote project before continuing.",
                status=409,
                retryable=True,
                action="reconnect",
                affected_resource_id=desktop_project_id,
            )
        raise _bridge_error(
            "active_core_project_mismatch",
            "A newer profile or project generation owns the active Core tunnel.",
            status=409,
            retryable=True,
            action="reconnect",
            affected_resource_id=desktop_project_id,
        )

    def _ensure_current_locked(self, session: _ActiveSessionV2) -> None:
        if (
            self._closed
            or self._active is not session
            or session.activation.bridge_generation != self._generation
        ):
            raise _bridge_error(
                "active_core_project_superseded",
                "A newer active project generation superseded this Core result.",
                status=409,
                retryable=True,
                action="reconnect",
                affected_resource_id=session.activation.desktop_project_id,
            )

    def _new_client(
        self, connection: CoreTunnelConnectionV2, deadline: float
    ) -> CoreControlClientV2:
        return CoreControlClientV2(
            connection,
            transport=self._new_transport(),
            timeout=min(self._timeout, _remaining(deadline)),
        )

    def _new_project_create_client(
        self,
        connection: CoreTunnelConnectionV2,
        deadline: float,
    ) -> CoreControlClientV2:
        return CoreControlClientV2(
            connection,
            transport=self._new_transport(),
            timeout=_remaining(deadline),
        )

    def _new_bootstrap_client(
        self, connection: CoreBootstrapTunnelConnectionV2, deadline: float
    ) -> CoreProjectBootstrapClientV2:
        return CoreProjectBootstrapClientV2(
            connection,
            transport=self._new_transport(),
            timeout=_remaining(deadline),
        )

    def _new_transport(self) -> httpx.BaseTransport | None:
        return None if self._transport_factory is None else self._transport_factory()

    def _call_core(
        self,
        action: Callable[[], Any],
        affected_resource_id: str | None = None,
    ) -> Any:
        try:
            return action()
        except DesktopCoreBridgeErrorV2:
            raise
        except CoreMutationOutcomeUnknownV2:
            raise
        except CoreClientErrorV2 as exc:
            raise _bridge_client_error(exc, affected_resource_id) from None

    def _session_core(
        self,
        session: _ActiveSessionV2,
        action: Callable[[], Any],
        affected_resource_id: str | None = None,
    ) -> Any:
        result = self._call_core(action, affected_resource_id)
        with self._lock:
            self._ensure_current_locked(session)
        return result

    @staticmethod
    def _call_adapter(
        action: Callable[[], Any],
        *,
        failure_code: str,
        failure_summary: str,
    ) -> Any:
        try:
            return action()
        except DesktopCoreBridgeErrorV2:
            raise
        except Exception:
            raise _bridge_error(
                failure_code,
                failure_summary,
                status=503,
                retryable=True,
                action="retry",
            ) from None

    def _acquire_transition(
        self,
        deadline: float,
        *,
        cancel_event: threading.Event | None = None,
    ) -> None:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise _bridge_error(
                    "core_activation_cancelled",
                    "The OpenEvo Daemon activation was cancelled before publication.",
                    status=409,
                    retryable=True,
                    action="retry",
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _bridge_error(
                    "core_bridge_busy",
                    "Another Core project transition is still in progress.",
                    status=409,
                    retryable=True,
                    action="retry",
                )
            if self._transition_lock.acquire(timeout=min(remaining, 0.05)):
                if cancel_event is not None and cancel_event.is_set():
                    self._transition_lock.release()
                    raise _bridge_error(
                        "core_activation_cancelled",
                        "The OpenEvo Daemon activation was cancelled before publication.",
                        status=409,
                        retryable=True,
                        action="retry",
                    )
                return

    def _retire(
        self,
        session: _ActiveSessionV2,
        *,
        deadline: float,
    ) -> None:
        client_error: Exception | None = None
        try:
            session.client.close()
        except Exception as exc:
            client_error = exc
        try:
            session.tunnel.close(deadline=deadline)
        except Exception:
            raise
        if client_error is not None:
            raise _bridge_error(
                "core_client_close_failed",
                "The active Core client could not be closed safely.",
                status=503,
                retryable=True,
                action="retry",
            ) from None

    def _retire_or_retain(
        self,
        session: _ActiveSessionV2,
        *,
        deadline: float,
        suppress_errors: bool,
    ) -> None:
        try:
            self._retire(session, deadline=deadline)
        except BaseException as exc:
            with self._lock:
                if all(retained is not session for retained in self._retained):
                    self._retained.append(session)
            if not suppress_errors or not isinstance(exc, Exception):
                raise
        else:
            with self._lock:
                self._retained[:] = [
                    retained for retained in self._retained if retained is not session
                ]

    @staticmethod
    def _close_client(client: CoreControlClientV2, *, suppress_errors: bool) -> None:
        try:
            client.close()
        except Exception:
            if not suppress_errors:
                raise

    @staticmethod
    def _close_bootstrap(client: CoreProjectBootstrapClientV2, *, suppress_errors: bool) -> None:
        try:
            client.close()
        except Exception:
            if not suppress_errors:
                raise

    @staticmethod
    def _close_tunnel(
        tunnel: CoreTunnelHandleV2,
        *,
        deadline: float,
        suppress_errors: bool,
    ) -> None:
        try:
            tunnel.close(deadline=deadline)
        except Exception:
            if not suppress_errors:
                raise


class _BridgeEventContextV2:
    def __init__(
        self,
        *,
        bridge: DesktopCoreBridgeV2,
        session: _ActiveSessionV2,
        last_event_id: str | None,
    ) -> None:
        self._bridge = bridge
        self._session = session
        self._last_event_id = last_event_id
        self._context: AbstractContextManager[Any] | None = None

    def __enter__(self) -> Iterator[core_v2.SseFrameV2]:
        with self._bridge._lock:
            self._bridge._ensure_current_locked(self._session)
        try:
            self._context = self._session.client.events(last_event_id=self._last_event_id)
            stream = self._context.__enter__()
        except CoreClientErrorV2 as exc:
            raise _bridge_client_error(exc, self._session.activation.desktop_project_id) from None
        return _BridgeEventIteratorV2(
            bridge=self._bridge,
            session=self._session,
            stream=iter(stream),
        )

    def __exit__(self, *exc: object) -> None:
        if self._context is None:
            return
        try:
            self._context.__exit__(*exc)
        except CoreClientErrorV2 as error:
            raise _bridge_client_error(
                error, self._session.activation.desktop_project_id
            ) from None


class _BridgeEventIteratorV2(Iterator[core_v2.SseFrameV2]):
    def __init__(
        self,
        *,
        bridge: DesktopCoreBridgeV2,
        session: _ActiveSessionV2,
        stream: Iterator[core_v2.SseFrameV2],
    ) -> None:
        self._bridge = bridge
        self._session = session
        self._stream = stream

    def __iter__(self) -> _BridgeEventIteratorV2:
        return self

    def __next__(self) -> core_v2.SseFrameV2:
        with self._bridge._lock:
            self._bridge._ensure_current_locked(self._session)
        try:
            frame = next(self._stream)
        except CoreClientErrorV2 as exc:
            raise _bridge_client_error(exc, self._session.activation.desktop_project_id) from None
        self._bridge._record_event(self._session, frame)
        return frame


def _model_sha256(model: core_v2.ContractModel) -> str:
    if not isinstance(model, core_v2.ContractModel):
        raise TypeError("Core authority digest requires a v2 contract model")
    return hashlib.sha256(_canonical_json_bytes(model.model_dump(mode="json"))).hexdigest()


def _mutation_request_sha256(
    request: core_v2.ContractModel,
    *,
    resource_id: str | None = None,
    if_match: str | None = None,
) -> str:
    if not isinstance(request, core_v2.ContractModel):
        raise TypeError("Core mutation digest requires a v2 contract model")
    if resource_id is not None:
        _require_opaque(resource_id)
    if if_match is not None:
        _require_etag(if_match)
    return _canonical_value_sha256(
        {
            "request": request.model_dump(mode="json"),
            "resource_id": resource_id,
            "if_match": if_match,
        }
    )


def _canonical_value_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _scope_id(domain: str, *values: str) -> str:
    _require_opaque(domain)
    for value in values:
        _require_opaque(value)
    encoded = _canonical_json_bytes([domain, *values])
    return f"{domain}-{hashlib.sha256(encoded).hexdigest()}"


def _require_exact_request(
    request: object,
    request_type: type[core_v2.ContractModel],
) -> None:
    if type(request) is not request_type:
        raise TypeError("Core bridge mutation requires its exact v2 request model")


def _require_etag(value: object) -> str:
    if type(value) is not str or _ETAG.fullmatch(value) is None:
        raise ValueError("Core bridge mutation ETag is invalid")
    return value


def _require_idempotency_key(value: str) -> None:
    if type(value) is not str or _HEADER.fullmatch(value) is None:
        raise ValueError("Core bridge idempotency key is invalid")


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _bridge_error(
            "core_bridge_timeout",
            "The Core bridge operation exceeded its deadline.",
            status=504,
            retryable=True,
            action="retry",
        )
    return remaining


def cast_str(value: str | None) -> str:
    if value is None:
        raise ValueError("required Core digest is absent")
    return value


def _bridge_client_error(
    error: CoreClientErrorV2,
    affected_resource_id: str | None,
) -> DesktopCoreBridgeErrorV2:
    local_code = getattr(error.error, "code", "core_request_failed")
    retryable = bool(getattr(error.error, "retryable", False))
    if isinstance(local_code, StrEnum):
        local_code = local_code.value
    transport_codes = {
        "core_connection_failed",
        "core_client_closed",
    }
    authority_codes = {
        "invalid_core_tunnel_connection",
        "core_negotiation_required",
        "core_response_too_large",
        "invalid_core_response",
        "invalid_core_error_response",
        "core_redirect_rejected",
        "core_authority_drift",
        "core_sse_protocol_error",
    }
    if local_code in transport_codes:
        return _bridge_error(
            "core_connection_failed",
            "Desktop could not reach the active project tunnel.",
            status=503,
            retryable=True,
            action="reconnect",
            affected_resource_id=affected_resource_id,
        )
    if local_code in authority_codes:
        return _bridge_error(
            "core_authority_invalid",
            "The OpenEvo Daemon did not satisfy the v2 release authority.",
            status=502,
            action="install_repair_daemon",
            affected_resource_id=affected_resource_id,
        )
    if local_code == "core_snapshot_refresh_required":
        return _bridge_error(
            "core_snapshot_refresh_required",
            "Reload Core snapshots before retrying this action.",
            status=409,
            retryable=True,
            action="retry",
            affected_resource_id=affected_resource_id,
        )
    repair_action = getattr(error.error, "repair_action", None)
    action: local_v2.DesktopActionV2 = {
        "retry": "retry",
        "repair": "install_repair_daemon",
        "reconfigure": "correct_project",
        "user_action_required": "administrator_action",
        "unsupported": "none",
    }.get(repair_action, "retry" if retryable else "correct_project")
    category = getattr(error.error, "category", "system")
    safe_code = (
        f"core_{category}_request_failed"
        if category
        in {
            "system",
            "project",
            "task",
            "transition",
            "artifact",
            "service",
            "authentication",
            "contract",
            "internal",
        }
        else "core_request_failed"
    )
    return _bridge_error(
        safe_code,
        "The OpenEvo Daemon rejected the v2 operation.",
        status=error.status_code,
        retryable=retryable,
        action=action,
        affected_resource_id=affected_resource_id,
    )


def _mutation_unknown_error(
    affected_resource_id: str | None,
) -> DesktopCoreBridgeErrorV2:
    return _bridge_error(
        "core_mutation_outcome_unknown",
        "Core may have accepted this action; retry with the same action identity.",
        status=503,
        retryable=True,
        action="retry",
        affected_resource_id=affected_resource_id,
    )


def _bridge_error(
    code: str,
    summary: str,
    *,
    status: int = 400,
    retryable: bool = False,
    action: local_v2.DesktopActionV2 = "none",
    affected_resource_id: str | None = None,
) -> DesktopCoreBridgeErrorV2:
    try:
        error = local_v2.DesktopErrorV2(
            code=code,
            summary=summary,
            retryable=retryable,
            action=action,
            affected_resource_id=affected_resource_id,
        )
    except ValidationError:
        error = local_v2.DesktopErrorV2(
            code="core_bridge_failed",
            summary="The Desktop Core bridge failed closed.",
            retryable=False,
            action="none",
            affected_resource_id=None,
        )
        status = 500
    return DesktopCoreBridgeErrorV2(min(599, max(400, status)), error)


__all__ = (
    "CoreBridgeMutationStateV2",
    "CoreBridgeMutationV2",
    "CoreActivationV2",
    "CoreHostAttachmentV2",
    "CoreHostServiceV2",
    "CoreProjectMappingV2",
    "CoreTunnelFactoryV2",
    "CoreTunnelHandleV2",
    "DesktopCoreBridgePersistenceV2",
    "DesktopCoreBridgeErrorV2",
    "DesktopCoreBridgeV2",
    "DesktopEventPublisherV2",
    "core_bridge_mutation_document_v2",
    "core_project_mapping_document_v2",
    "core_project_mapping_sha256_v2",
)
