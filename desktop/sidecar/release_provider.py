from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import logging
import re
import sqlite3
from threading import BoundedSemaphore, Event, Lock, RLock
from typing import Literal, NoReturn, Protocol, cast

from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from desktop.sidecar.contracts.v1 import models as local_v1
from desktop.sidecar.contracts.v1.canonical import DESKTOP_OPENAPI_SHA256
from desktop.sidecar.contracts.v1.models import (
    ActiveProjectStateV1,
    ApiErrorV1,
    ConnectionFailureV1,
    ContractNegotiationV1,
    CoreConnectionStateV1,
    DesktopStateV1,
    ExecutionModeCapabilitiesV1,
    ExecutionModeCapabilityV1,
    ExecutionModeV1,
    HealthV1,
    HostKeyAcceptV1,
    HostKeyReviewV1,
    LocalOperationV1,
    ProjectCreateV1,
    ProjectPatchV1,
    ProjectSourceV1,
    ProjectV1,
    RemoteProfileCreateV1,
    RemoteProfilePatchV1,
    RemoteProfileV1,
    StateEventV1,
    VersionV1,
    WorkspaceImportRefV1,
)
from desktop.sidecar.core_bridge_v1 import (
    CoreActivationV1,
    DesktopCoreBridgeErrorV1,
    DesktopCoreBridgeV1,
)
from desktop.sidecar.core_client_v1 import CORE_OPENAPI_SHA256
from desktop.sidecar.event_broker_v1 import DesktopEventBrokerError, DesktopEventBrokerV1
from desktop.sidecar.provider_store import (
    DesktopProviderStore,
    ETagConflictError,
    ProfileRuntimeActionReservation,
    ProjectRuntimeActionReservation,
    ProviderStoreError,
    ResourceNotFoundError,
    ResourceInUseError,
)
from desktop.sidecar.release_runtime import CoreRuntimeSessionBinding
from desktop.sidecar.remote_lifecycle import (
    DesktopRemoteLifecycle,
    RemoteConnectionFailedError,
    RemoteCredentialUnavailableError,
    RemoteLifecycleError,
    RemoteLifecycleSupersededError,
)
from openevo.backend.contracts.v1.models import ErrorCategory, ErrorSeverity, RepairAction
from desktop.sidecar.workspace_identity import ownership_for_native_import
from desktop.sidecar.workspace_imports import (
    WorkspaceImportError,
    WorkspaceImportOwnership,
    WorkspaceImportStore,
)


NATIVE_SIDECAR_PROTOCOL = "openevo-native-sidecar-v1"
_LOGGER = logging.getLogger(__name__)
_LOCAL_CORE_SESSION_LOSS_CODES = frozenset(
    {
        "active_project_session_superseded",
        "active_project_session_unavailable",
        "core_client_closed",
        "desktop_core_bridge_closed",
    }
)


class ProviderCapabilityUnavailableError(Exception):
    """The release provider has no verified implementation for an operation."""

    def __init__(self, operation_id: str) -> None:
        super().__init__("required provider capability is unavailable")
        self.operation_id = operation_id


class ExecutionModeReleaseUnavailableError(Exception):
    """A valid project mode is not usable in the exact Desktop release composition."""

    def __init__(self, operation_id: str, capability: ExecutionModeCapabilityV1) -> None:
        super().__init__("execution mode is unavailable in this Desktop release")
        self.operation_id = operation_id
        self.capability = capability


class ActiveProjectMismatchError(Exception):
    """A mutation names a project other than the active Desktop session."""

    def __init__(self, operation_id: str) -> None:
        super().__init__("requested project does not own the active Desktop session")
        self.operation_id = operation_id


class EvolutionConfigurationPendingError(Exception):
    """The active project has not completed the explicit evolution setup stage."""


class InvalidNativeChallengeError(Exception):
    """The native readiness challenge is missing or malformed."""


OperationHandler = Callable[[Mapping[str, object]], object]
ProfileRuntimeState = Literal["connected", "disconnected", "host_key_required"]


@dataclass(frozen=True)
class _ConnectionOutcome:
    state: ProfileRuntimeState
    host_key_fingerprint: str | None
    host_key_review: HostKeyReviewV1 | None = None


ConnectionAction = Callable[[RemoteProfileV1], _ConnectionOutcome]


def _collect_cleanup_failure(
    cleanup: Callable[[], None],
    first_failure: BaseException | None,
) -> BaseException | None:
    try:
        cleanup()
    except BaseException as exc:
        if first_failure is None:
            return exc
    return first_failure


class DesktopCoreRuntimeOwnerV1(Protocol):
    core_bridge: DesktopCoreBridgeV1
    event_broker: DesktopEventBrokerV1

    def start(
        self,
        *,
        active_project: Callable[[], CoreRuntimeSessionBinding | None],
        publish: Callable[[], None],
        session_lost: Callable[[CoreRuntimeSessionBinding, DesktopCoreBridgeErrorV1], None],
    ) -> None: ...

    def stop(self) -> None: ...

    def close(self) -> None: ...


@dataclass
class _ConnectionActionGate:
    lock: Lock
    users: int = 0


@dataclass(frozen=True, slots=True)
class _ProjectActivationWork:
    reservation: ProjectRuntimeActionReservation
    route: str
    project_id: str
    key: str
    if_match: str


@dataclass(slots=True)
class _ProjectExecution:
    operation_id: str
    cancel_event: Event
    start: Event
    run: Event
    running: Event
    finished: Event
    interrupt_started: Event
    interrupt: Callable[[], None]
    future: Future[None] | None = None


class _BoundedProjectExecutor:
    """One serialized project authority queue with a hard admission bound."""

    def __init__(self, *, max_pending: int = 16) -> None:
        self._slots = BoundedSemaphore(max_pending)
        self._lock = Lock()
        self._closed = False
        self._entries: dict[str, _ProjectExecution] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="openevo-project-runtime",
        )

    def submit(
        self,
        operation_id: str,
        action: Callable[[Event], None],
        *,
        accepted: Callable[[], None],
        interrupt: Callable[[], None],
    ) -> bool:
        if not self._slots.acquire(blocking=False):
            return False
        entry = _ProjectExecution(
            operation_id=operation_id,
            cancel_event=Event(),
            start=Event(),
            run=Event(),
            running=Event(),
            finished=Event(),
            interrupt_started=Event(),
            interrupt=interrupt,
        )
        with self._lock:
            if self._closed or operation_id in self._entries:
                self._slots.release()
                return False
            try:
                future = self._executor.submit(self._run, entry, action)
            except RuntimeError:
                self._slots.release()
                return False
            entry.future = future
            self._entries[operation_id] = entry
            future.add_done_callback(lambda _completed: self._finish(entry))
        try:
            accepted()
            entry.run.set()
        finally:
            entry.start.set()
        return True

    def cancel(self, operation_id: str, *, wait_seconds: float = 2.0) -> bool:
        with self._lock:
            entry = self._entries.get(operation_id)
        if entry is None:
            return False
        self._cancel_entry(entry)
        entry.finished.wait(timeout=wait_seconds)
        return True

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            entries = tuple(self._entries.values())
        for entry in entries:
            self._cancel_entry(entry)
        futures = tuple(entry.future for entry in entries if entry.future is not None)
        if futures:
            wait(futures, timeout=2.0)
        self._executor.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def _run(entry: _ProjectExecution, action: Callable[[Event], None]) -> None:
        try:
            entry.start.wait()
            if not entry.run.is_set() or entry.cancel_event.is_set():
                return
            entry.running.set()
            if entry.cancel_event.is_set():
                return
            action(entry.cancel_event)
        finally:
            entry.finished.set()

    def _cancel_entry(self, entry: _ProjectExecution) -> None:
        entry.cancel_event.set()
        future = entry.future
        if future is not None:
            future.cancel()
        with self._lock:
            should_interrupt = (
                entry.running.is_set()
                and not entry.finished.is_set()
                and not entry.interrupt_started.is_set()
            )
            if should_interrupt:
                entry.interrupt_started.set()
        if should_interrupt:
            try:
                entry.interrupt()
            except Exception:
                _LOGGER.warning(
                    "project operation interrupt failed",
                    extra={"operation_id": entry.operation_id},
                )

    def _finish(self, entry: _ProjectExecution) -> None:
        entry.finished.set()
        with self._lock:
            if self._entries.get(entry.operation_id) is entry:
                self._entries.pop(entry.operation_id, None)
        self._slots.release()


class DesktopReleaseProvider:
    """First release provider slice backed by ``DesktopProviderStore``."""

    def __init__(
        self,
        store: DesktopProviderStore,
        workspace_import_store: WorkspaceImportStore,
        *,
        build_version: str,
        source_commit: str,
        build_channel: str,
        instance_id: str,
        readiness_key: bytes,
        execution_mode_capabilities: ExecutionModeCapabilitiesV1,
        remote_lifecycle: DesktopRemoteLifecycle,
        core_runtime: DesktopCoreRuntimeOwnerV1 | None = None,
        core_bridge: DesktopCoreBridgeV1 | None = None,
        event_broker: DesktopEventBrokerV1 | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if re.fullmatch(r"[0-9a-f]{32}", instance_id) is None:
            raise ValueError("native instance id must be 32 lowercase hex characters")
        if type(readiness_key) is not bytes or len(readiness_key) != 32:
            raise ValueError("native readiness key must contain exactly 32 bytes")
        if core_runtime is not None and (core_bridge is not None or event_broker is not None):
            raise ValueError("core_runtime cannot be combined with injected Core resources")
        self._store = store
        self._workspace_import_store = workspace_import_store
        self._remote_lifecycle = remote_lifecycle
        self._core_runtime = core_runtime
        self._core_bridge = core_runtime.core_bridge if core_runtime is not None else core_bridge
        self._event_broker = (
            core_runtime.event_broker if core_runtime is not None else event_broker
        )
        self._project_session_lock = RLock()
        self._connection_state_lock = Lock()
        self._session_generation = 0
        self._connection_owner: str | None = None
        self._connection_action_lock = Lock()
        self._connection_gate_lock = Lock()
        self._connection_gates: dict[tuple[str, str, str], _ConnectionActionGate] = {}
        self._close_lock = Lock()
        self._closed = False
        self._project_executor = _BoundedProjectExecutor()
        self._core_state = CoreConnectionStateV1(state="disconnected", active_tunnel=False)
        self._core_session_binding: CoreRuntimeSessionBinding | None = None
        self._instance_id = instance_id
        self._readiness_key = readiness_key
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._execution_mode_capabilities = execution_mode_capabilities
        self._execution_modes = {
            capability.mode: capability for capability in execution_mode_capabilities.modes
        }
        self._reconcile_workspace_imports()
        feature_flags: tuple[local_v1.FeatureFlagV1, ...] = ("remote_profiles",)
        if self._core_bridge is not None and self._event_broker is not None:
            feature_flags = (
                "remote_profiles",
                "project_validation",
                "operation_events",
                "run_observability",
                "artifact_inspection",
            )
        self._version = VersionV1(
            openapi_sha256=DESKTOP_OPENAPI_SHA256,
            build_version=build_version,
            source_commit=source_commit,
            build_channel=build_channel,
            provider_kind="desktop_sidecar",
            feature_flags=feature_flags,
        )
        self._handlers: dict[str, OperationHandler] = {
            "getDesktopContractVersion": self._get_version,
            "getDesktopHealth": self._get_health,
            "getDesktopState": self._get_state,
            "listRemoteProfiles": self._list_profiles,
            "createRemoteProfile": self._create_profile,
            "getRemoteProfile": self._get_profile,
            "updateRemoteProfile": self._update_profile,
            "deleteRemoteProfile": self._delete_profile,
            "connectRemoteProfile": self._connect_profile,
            "disconnectRemoteProfile": self._disconnect_profile,
            "acceptRemoteHostKey": self._accept_host_key,
            "listProjects": self._list_projects,
            "createProject": self._create_project,
            "getProject": self._get_project,
            "updateProject": self._update_project,
            "deleteProject": self._delete_project,
            "activateProject": self._activate_project,
            "getProjectCapabilities": self._get_project_capabilities,
            "validateProject": self._validate_project,
            "getLocalOperation": self._get_local_operation,
            "cancelLocalOperation": self._cancel_local_operation,
            "listRuns": self._list_runs,
            "createRun": self._create_run,
            "getRun": self._get_run,
            "deleteRun": self._delete_run,
            "cancelRun": self._cancel_run,
            "retryRun": self._retry_run,
            "listRunTimeline": self._list_run_timeline,
            "listRunLogs": self._list_run_logs,
            "getRunContext": self._get_run_context,
            "listRunArtifacts": self._list_run_artifacts,
            "getArtifact": self._get_artifact,
            "getArtifactContent": self._get_artifact_content,
            "getArtifactDiff": self._get_artifact_diff,
            "listServices": self._list_services,
            "getCoreOperation": self._get_core_operation,
            "getCoreLogsByRef": self._get_core_logs_by_ref,
            "listServiceLogs": self._list_service_logs,
            "cleanupCaches": self._cleanup_caches,
            "subscribeDesktopEvents": self._subscribe_events,
        }
        if self._core_runtime is not None:
            self._core_runtime.start(
                active_project=self._active_project_for_runtime,
                publish=self._publish_core_event_invalidation,
                session_lost=self._handle_core_session_loss,
            )

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        failure = _collect_cleanup_failure(self._project_executor.close, None)
        if self._core_runtime is not None:
            failure = _collect_cleanup_failure(self._core_runtime.stop, failure)
        elif self._core_bridge is not None:
            failure = _collect_cleanup_failure(self._core_bridge.close, failure)
        if self._core_runtime is not None:
            failure = _collect_cleanup_failure(self._core_runtime.close, failure)
        elif self._event_broker is not None:
            failure = _collect_cleanup_failure(self._event_broker.close, failure)
        failure = _collect_cleanup_failure(self._remote_lifecycle.close, failure)
        failure = _collect_cleanup_failure(self._store.close, failure)
        failure = _collect_cleanup_failure(self._workspace_import_store.close, failure)
        if failure is not None:
            raise failure

    @property
    def workspace_import_store(self) -> WorkspaceImportStore:
        return self._workspace_import_store

    def invoke(self, operation_id: str, arguments: Mapping[str, object]) -> object:
        handler = self._handlers.get(operation_id)
        if handler is None:
            self._unavailable(operation_id)
        return handler(arguments)

    def _get_version(self, arguments: Mapping[str, object]) -> VersionV1:
        del arguments
        return self._version

    def _get_health(self, arguments: Mapping[str, object]) -> HealthV1:
        challenge = arguments.get("x_openevo_native_challenge")
        if type(challenge) is not str or re.fullmatch(r"[0-9a-f]{64}", challenge) is None:
            raise InvalidNativeChallengeError
        domain = f"{NATIVE_SIDECAR_PROTOCOL}\0{self._instance_id}\0{challenge}".encode("ascii")
        proof = hmac.new(self._readiness_key, domain, hashlib.sha256).hexdigest()
        return HealthV1(
            status="ok",
            protocol=NATIVE_SIDECAR_PROTOCOL,
            instance_id=self._instance_id,
            instance_proof=proof,
        )

    def _get_state(self, arguments: Mapping[str, object]) -> DesktopStateV1:
        del arguments
        with self._project_session_lock:
            active_projects = self._store.list_projects(limit=2, filters={"state": "active"}).items
            with self._connection_state_lock:
                core_state = self._core_state
                core_binding = self._core_session_binding
                session_generation = self._session_generation
            pending_operation_ids = self._store.pending_operation_ids()
        active_project = None
        if active_projects:
            project = active_projects[0]
            if (
                core_state.state == "online"
                and core_state.profile_id == project.profile_id
                and core_binding is not None
                and core_binding.project.project_id == project.project_id
                and core_binding.project.profile_id == project.profile_id
                and core_binding.project.etag == project.etag
                and core_binding.generation == session_generation
                and project.remote is not None
            ):
                connection_state = "ready"
            elif (
                core_state.state
                in {
                    "connecting",
                    "checking",
                    "bootstrapping",
                    "core_starting",
                    "reconnecting",
                }
                and core_state.profile_id == project.profile_id
            ):
                connection_state = "connecting"
            else:
                connection_state = "offline"
            active_project = ActiveProjectStateV1(
                project_id=project.project_id,
                project_etag=project.etag,
                profile_id=project.profile_id,
                connection_state=connection_state,
            )
        return DesktopStateV1(
            observed_at=self._timestamp(),
            contract=ContractNegotiationV1(
                selected_major=1,
                desktop_openapi_sha256=DESKTOP_OPENAPI_SHA256,
                core_openapi_sha256=(
                    core_state.core.contract_digest if core_state.core is not None else None
                ),
                compatible=True,
            ),
            execution_mode_capabilities=self._execution_mode_capabilities,
            core=core_state,
            active_project=active_project,
            pending_operation_ids=pending_operation_ids,
        )

    def _list_profiles(self, arguments: Mapping[str, object]) -> object:
        return self._store.list_profiles(
            limit=cast(int, arguments["limit"]),
            after=cast(str | None, arguments["after"]),
            sort=cast(str, arguments["sort"]),
            direction=cast(str, arguments["direction"]),
        )

    def _create_profile(self, arguments: Mapping[str, object]) -> Response:
        profile = self._store.create_profile(
            cast(RemoteProfileCreateV1, arguments["request"]),
            idempotency_key=cast(str, arguments["idempotency_key"]),
        )
        return self._resource_response(profile, status_code=201)

    def _get_profile(self, arguments: Mapping[str, object]) -> Response:
        profile = self._store.get_profile(cast(str, arguments["profile_id"]))
        return self._resource_response(profile)

    def _update_profile(self, arguments: Mapping[str, object]) -> Response:
        profile_id = cast(str, arguments["profile_id"])
        self._require_profile_not_connected(profile_id)
        profile = self._store.patch_profile(
            profile_id,
            cast(RemoteProfilePatchV1, arguments["request"]),
            if_match=cast(str, arguments["if_match"]),
        )
        return self._resource_response(profile)

    def _delete_profile(self, arguments: Mapping[str, object]) -> Response:
        profile_id = cast(str, arguments["profile_id"])
        self._require_profile_not_connected(profile_id)
        self._store.delete_profile(
            profile_id,
            if_match=cast(str, arguments["if_match"]),
        )
        return Response(status_code=204)

    def _connect_profile(self, arguments: Mapping[str, object]) -> Response:
        profile_id = cast(str, arguments["profile_id"])
        if_match = cast(str, arguments["if_match"])
        idempotency_key = cast(str, arguments["idempotency_key"])

        def connect(profile: RemoteProfileV1) -> _ConnectionOutcome:
            result = self._remote_lifecycle.connect(profile)
            review = (
                HostKeyReviewV1(
                    algorithm=result.host_key_candidate.algorithm,
                    fingerprint=result.host_key_candidate.fingerprint,
                )
                if result.host_key_candidate is not None
                else None
            )
            return _ConnectionOutcome(
                result.state,
                profile.host_key_fingerprint,
                host_key_review=review,
            )

        return self._execute_connection_action(
            route=f"/desktop/v1/profiles/{profile_id}/connect",
            operation_kind="profile_connect",
            profile_id=profile_id,
            if_match=if_match,
            idempotency_key=idempotency_key,
            request_body={},
            action=connect,
        )

    def _accept_host_key(self, arguments: Mapping[str, object]) -> Response:
        profile_id = cast(str, arguments["profile_id"])
        if_match = cast(str, arguments["if_match"])
        idempotency_key = cast(str, arguments["idempotency_key"])
        request = cast(HostKeyAcceptV1, arguments["request"])

        def accept(profile: RemoteProfileV1) -> _ConnectionOutcome:
            result = self._remote_lifecycle.accept_host_key(profile, request)
            return _ConnectionOutcome(result.state, request.fingerprint)

        return self._execute_connection_action(
            route=f"/desktop/v1/profiles/{profile_id}/host-key/accept",
            operation_kind="host_key_accept",
            profile_id=profile_id,
            if_match=if_match,
            idempotency_key=idempotency_key,
            request_body=request,
            action=accept,
        )

    def _disconnect_profile(self, arguments: Mapping[str, object]) -> Response:
        profile_id = cast(str, arguments["profile_id"])
        if_match = cast(str, arguments["if_match"])
        idempotency_key = cast(str, arguments["idempotency_key"])

        def disconnect(profile: RemoteProfileV1) -> _ConnectionOutcome:
            self._remote_lifecycle.disconnect(profile.profile_id)
            return _ConnectionOutcome("disconnected", profile.host_key_fingerprint)

        return self._execute_connection_action(
            route=f"/desktop/v1/profiles/{profile_id}/disconnect",
            operation_kind="profile_disconnect",
            profile_id=profile_id,
            if_match=if_match,
            idempotency_key=idempotency_key,
            request_body={},
            action=disconnect,
        )

    def _execute_connection_action(
        self,
        *,
        route: str,
        operation_kind: str,
        profile_id: str,
        if_match: str,
        idempotency_key: str,
        request_body: BaseModel | Mapping[str, object],
        action: ConnectionAction,
    ) -> Response:
        gate_key = (route, profile_id, idempotency_key)
        gate = self._acquire_connection_gate(gate_key)
        try:
            with self._connection_action_lock:
                return self._execute_connection_action_once(
                    route=route,
                    operation_kind=operation_kind,
                    profile_id=profile_id,
                    if_match=if_match,
                    idempotency_key=idempotency_key,
                    request_body=request_body,
                    action=action,
                )
        finally:
            self._release_connection_gate(gate_key, gate)
            try:
                self.publish_state_changed()
            except (ProviderStoreError, sqlite3.Error):
                _LOGGER.exception(
                    "profile operation terminal transition could not publish invalidation",
                    extra={"profile_id": profile_id},
                )

    def _execute_connection_action_once(
        self,
        *,
        route: str,
        operation_kind: str,
        profile_id: str,
        if_match: str,
        idempotency_key: str,
        request_body: BaseModel | Mapping[str, object],
        action: ConnectionAction,
    ) -> Response:
        with self._project_session_lock:
            with self._connection_state_lock:
                reservation = self._store.begin_profile_runtime_action(
                    route=route,
                    operation_kind=operation_kind,
                    profile_id=profile_id,
                    key=idempotency_key,
                    body=request_body,
                    if_match=if_match,
                    displace_existing=operation_kind != "profile_disconnect",
                )
                if reservation.replayed:
                    self._reconcile_failed_profile_transport(profile_id, reservation.operation)
                    return self._operation_response(reservation.operation)
                owner_error: RemoteConnectionFailedError | None = None
                snapshot = self._remote_lifecycle.snapshot()
                if operation_kind == "profile_disconnect" and snapshot.profile_id not in {
                    None,
                    profile_id,
                }:
                    generation = None
                    owner_error = RemoteConnectionFailedError(
                        "Another remote profile owns the connection."
                    )
                else:
                    self._session_generation += 1
                    generation = self._session_generation
                    self._connection_owner = profile_id
        profile = reservation.profile
        if profile is None:
            raise ProviderStoreError("new profile runtime reservation has no profile snapshot")
        try:
            self.publish_state_changed()
        except (ProviderStoreError, sqlite3.Error):
            _LOGGER.exception(
                "profile operation start could not publish invalidation",
                extra={"profile_id": profile_id},
            )

        try:
            if owner_error is not None:
                raise owner_error
            outcome = action(profile)
        except RemoteLifecycleError as exc:
            error = self._remote_action_error(reservation, exc)
            operation = self._finalize_profile_runtime_failure(
                reservation=reservation,
                route=route,
                profile_id=profile_id,
                key=idempotency_key,
                body=request_body,
                if_match=if_match,
                error=error,
            )
            with self._connection_state_lock:
                if generation is not None and self._owns_connection_locked(generation, profile_id):
                    self._reconcile_failed_profile_transport(profile_id, operation)
                    self._core_state = self._connection_failure_state(profile_id, exc)
            return self._operation_response(operation)

        with self._connection_state_lock:
            if generation is None or not self._owns_connection_locked(generation, profile_id):
                superseded = RemoteLifecycleSupersededError(
                    "A newer remote lifecycle operation superseded this result."
                )
                operation = self._finalize_profile_runtime_failure(
                    reservation=reservation,
                    route=route,
                    profile_id=profile_id,
                    key=idempotency_key,
                    body=request_body,
                    if_match=if_match,
                    error=self._remote_action_error(reservation, superseded),
                )
                return self._operation_response(operation)
            try:
                operation = self._store.complete_profile_runtime_action(
                    reservation=reservation,
                    route=route,
                    profile_id=profile_id,
                    key=idempotency_key,
                    body=request_body,
                    if_match=if_match,
                    connection_state=outcome.state,
                    host_key_fingerprint=outcome.host_key_fingerprint,
                )
            except (ProviderStoreError, sqlite3.Error):
                error = self._provider_action_error(reservation)
                operation = self._finalize_profile_runtime_failure(
                    reservation=reservation,
                    route=route,
                    profile_id=profile_id,
                    key=idempotency_key,
                    body=request_body,
                    if_match=if_match,
                    error=error,
                )
                if operation.state == "succeeded":
                    self._core_state = self._core_state_for_outcome(
                        profile_id,
                        operation.operation_id,
                        outcome,
                    )
                    return self._operation_response(operation)
                if operation.state == "failed":
                    self._reconcile_failed_profile_transport(profile_id, operation)
                else:
                    self._disconnect_owned_transport(profile_id)
                self._core_state = self._local_provider_failure_state(profile_id)
                return self._operation_response(operation)
            if operation.state != "succeeded":
                self._disconnect_owned_transport(profile_id)
                self._core_state = CoreConnectionStateV1(state="disconnected", active_tunnel=False)
                return self._operation_response(operation)
            self._core_state = self._core_state_for_outcome(
                profile_id,
                operation.operation_id,
                outcome,
            )
        return self._operation_response(operation)

    def _acquire_connection_gate(
        self,
        key: tuple[str, str, str],
    ) -> _ConnectionActionGate:
        with self._connection_gate_lock:
            gate = self._connection_gates.get(key)
            if gate is None:
                gate = _ConnectionActionGate(lock=Lock())
                self._connection_gates[key] = gate
            gate.users += 1
        gate.lock.acquire()
        return gate

    def _release_connection_gate(
        self,
        key: tuple[str, str, str],
        gate: _ConnectionActionGate,
    ) -> None:
        gate.lock.release()
        with self._connection_gate_lock:
            gate.users -= 1
            if gate.users == 0:
                del self._connection_gates[key]

    def _require_profile_not_connected(self, profile_id: str) -> None:
        snapshot = self._remote_lifecycle.snapshot()
        if snapshot.profile_id == profile_id and snapshot.state != "disconnected":
            raise ResourceInUseError("profile", profile_id)

    def _owns_connection_locked(self, generation: int, profile_id: str) -> bool:
        return generation == self._session_generation and self._connection_owner == profile_id

    def _finalize_profile_runtime_failure(
        self,
        *,
        reservation: ProfileRuntimeActionReservation,
        route: str,
        profile_id: str,
        key: str,
        body: BaseModel | Mapping[str, object],
        if_match: str,
        error: ApiErrorV1,
    ) -> LocalOperationV1:
        for attempt in range(2):
            try:
                return self._store.fail_profile_runtime_action(
                    reservation=reservation,
                    route=route,
                    profile_id=profile_id,
                    key=key,
                    body=body,
                    if_match=if_match,
                    error=error,
                )
            except (ProviderStoreError, sqlite3.Error):
                observed = self._store.observe_profile_runtime_action(
                    reservation=reservation,
                    route=route,
                    profile_id=profile_id,
                    key=key,
                    body=body,
                    if_match=if_match,
                )
                if observed.state != "running":
                    return observed
                if attempt != 0:
                    raise
        raise AssertionError("profile failure finalization loop did not return")

    def _reconcile_failed_profile_transport(
        self,
        profile_id: str,
        operation: LocalOperationV1,
    ) -> None:
        if operation.state != "failed":
            return
        try:
            profile = self._store.get_profile(profile_id)
        except ResourceNotFoundError:
            return
        if profile.connection_state != "disconnected":
            return
        binding = self._core_session_binding
        if (
            binding is not None
            and binding.generation == self._session_generation
            and binding.project.profile_id == profile_id
        ):
            return
        snapshot = self._remote_lifecycle.snapshot()
        if snapshot.profile_id != profile_id or snapshot.state == "disconnected":
            return
        self._disconnect_owned_transport(profile_id)

    def _disconnect_owned_transport(self, profile_id: str) -> None:
        try:
            self._remote_lifecycle.disconnect(profile_id)
        except RemoteLifecycleError:
            pass

    def _core_state_for_outcome(
        self,
        profile_id: str,
        operation_id: str,
        outcome: _ConnectionOutcome,
    ) -> CoreConnectionStateV1:
        if outcome.state == "disconnected":
            return CoreConnectionStateV1(state="disconnected", active_tunnel=False)
        if outcome.state == "host_key_required":
            if outcome.host_key_review is None:
                return self._connection_failure_state(
                    profile_id,
                    RemoteConnectionFailedError("SSH host-key review state is incomplete."),
                )
            return CoreConnectionStateV1(
                state="host_key_review",
                profile_id=profile_id,
                active_tunnel=False,
                operation_id=operation_id,
                host_key_review=outcome.host_key_review,
            )
        return self._core_not_started_state(profile_id)

    @staticmethod
    def _operation_response(operation: LocalOperationV1) -> JSONResponse:
        if operation.state == "failed":
            if operation.error is None:
                raise ProviderStoreError("failed profile operation has no persisted API error")
            return JSONResponse(
                status_code=operation.error.http_status,
                content=operation.error.model_dump(mode="json"),
            )
        return JSONResponse(
            status_code=202,
            content=operation.model_dump(mode="json"),
            headers={"ETag": operation.etag},
        )

    @staticmethod
    def _remote_action_error(
        reservation: ProfileRuntimeActionReservation,
        error: RemoteLifecycleError,
    ) -> ApiErrorV1:
        if isinstance(error, RemoteCredentialUnavailableError):
            return DesktopReleaseProvider._action_error(
                reservation,
                status_code=409,
                code="ssh_credential_unavailable",
                message="The selected SSH credential is not available to OpenEvo Desktop.",
                retryable=False,
                repair_action=RepairAction.USER_ACTION_REQUIRED,
                next_action="Switch this remote workspace to SSH agent authentication.",
            )
        if isinstance(error, RemoteLifecycleSupersededError):
            return DesktopReleaseProvider._action_error(
                reservation,
                status_code=409,
                code="connection_operation_superseded",
                message="A newer connection action replaced this SSH operation.",
                retryable=True,
                repair_action=RepairAction.OPENEVO_CAN_RETRY,
                next_action="Reload the connection state before retrying.",
            )
        return DesktopReleaseProvider._action_error(
            reservation,
            status_code=503,
            code="ssh_connection_failed",
            message="OpenEvo Desktop could not establish the SSH connection.",
            retryable=True,
            repair_action=RepairAction.OPENEVO_CAN_RETRY,
            next_action="Check the server and SSH settings, then retry.",
        )

    @staticmethod
    def _provider_action_error(
        reservation: ProfileRuntimeActionReservation,
    ) -> ApiErrorV1:
        return DesktopReleaseProvider._action_error(
            reservation,
            status_code=503,
            code="local_provider_unavailable",
            message="The local Desktop provider is unavailable.",
            retryable=False,
            repair_action=RepairAction.UNSUPPORTED,
            next_action="Review the error before retrying this operation.",
            category=ErrorCategory.SERVICE,
        )

    @staticmethod
    def _action_error(
        reservation: ProfileRuntimeActionReservation,
        *,
        status_code: int,
        code: str,
        message: str,
        retryable: bool,
        repair_action: RepairAction,
        next_action: str,
        category: ErrorCategory = ErrorCategory.AUTHENTICATION,
    ) -> ApiErrorV1:
        return ApiErrorV1(
            request_id=reservation.operation.operation_id,
            code=code,
            http_status=status_code,
            message=message,
            severity=ErrorSeverity.BLOCKING,
            category=category,
            retryable=retryable,
            repair_action=repair_action,
            next_action=next_action,
        )

    @staticmethod
    def _local_provider_failure_state(profile_id: str) -> CoreConnectionStateV1:
        return CoreConnectionStateV1(
            state="offline",
            profile_id=profile_id,
            active_tunnel=False,
            failure=ConnectionFailureV1(
                code="local_provider_unavailable",
                message="The local Desktop provider is unavailable.",
                retryable=False,
                next_action="Review the error before retrying this operation.",
            ),
        )

    @staticmethod
    def _connection_failure_state(
        profile_id: str,
        error: RemoteLifecycleError,
    ) -> CoreConnectionStateV1:
        if isinstance(error, RemoteCredentialUnavailableError):
            code = "ssh_credential_unavailable"
            message = "The selected SSH credential is not available to OpenEvo Desktop."
            retryable = False
            next_action = "Switch this remote workspace to SSH agent authentication."
        elif isinstance(error, RemoteLifecycleSupersededError):
            code = "connection_operation_superseded"
            message = "A newer connection action replaced this SSH operation."
            retryable = True
            next_action = "Reload the connection state before retrying."
        else:
            code = "ssh_connection_failed"
            message = "OpenEvo Desktop could not establish the SSH connection."
            retryable = True
            next_action = "Check the server and SSH settings, then retry."
        return CoreConnectionStateV1(
            state="offline",
            profile_id=profile_id,
            active_tunnel=False,
            failure=ConnectionFailureV1(
                code=code,
                message=message,
                retryable=retryable,
                next_action=next_action,
            ),
        )

    @staticmethod
    def _core_not_started_state(profile_id: str) -> CoreConnectionStateV1:
        return CoreConnectionStateV1(
            state="offline",
            profile_id=profile_id,
            active_tunnel=False,
            failure=ConnectionFailureV1(
                code="core_not_started",
                message="SSH is connected; OpenEvo Core has not been started for a project.",
                retryable=True,
                next_action="Create or activate a project to prepare OpenEvo Core.",
            ),
        )

    def _list_projects(self, arguments: Mapping[str, object]) -> object:
        return self._store.list_projects(
            limit=cast(int, arguments["limit"]),
            after=cast(str | None, arguments["after"]),
            sort=cast(str, arguments["sort"]),
            direction=cast(str, arguments["direction"]),
        )

    def _create_project(self, arguments: Mapping[str, object]) -> Response:
        request = cast(ProjectCreateV1, arguments["request"])
        self._require_supported_execution_mode("createProject", request.execution.mode)
        with self._store.workspace_import_reference_guard():
            self._verify_project_source(request.source, project_id=None)
            project = self._store.create_project(
                request,
                idempotency_key=cast(str, arguments["idempotency_key"]),
            )
            self._adopt_project_source(project.source, project_id=project.project_id)
        return self._resource_response(project, status_code=201)

    def _get_project(self, arguments: Mapping[str, object]) -> Response:
        project = self._store.get_project(cast(str, arguments["project_id"]))
        return self._resource_response(project)

    def _update_project(self, arguments: Mapping[str, object]) -> Response:
        project_id = cast(str, arguments["project_id"])
        request = cast(ProjectPatchV1, arguments["request"])
        with self._project_session_lock:
            with self._store.workspace_import_reference_guard():
                previous = self._store.get_project(project_id)
                if not hmac.compare_digest(
                    previous.etag,
                    cast(str, arguments["if_match"]),
                ):
                    raise ETagConflictError("project", project_id, previous.etag)
                self._require_supported_execution_mode(
                    "updateProject",
                    request.execution.mode
                    if request.execution is not None
                    else previous.execution.mode,
                )
                if request.source is not None:
                    self._verify_project_source(request.source, project_id=project_id)
                project = self._store.patch_project(
                    project_id,
                    request,
                    if_match=cast(str, arguments["if_match"]),
                )
                self._adopt_project_source(project.source, project_id=project_id)
            if previous.state == "active" and project.state != "active":
                self._retire_edited_project(project_id, project.profile_id)
        if previous.source.import_ref != project.source.import_ref:
            self._release_project_source(previous.source, project_id=project_id)
        self.publish_state_changed()
        return self._resource_response(project)

    def _delete_project(self, arguments: Mapping[str, object]) -> Response:
        project_id = cast(str, arguments["project_id"])
        with self._project_session_lock:
            with self._store.workspace_import_reference_guard():
                project = self._store.get_project(project_id)
                self._store.delete_project(
                    project_id,
                    if_match=cast(str, arguments["if_match"]),
                )
        self._release_project_source(project.source, project_id=project_id)
        self.publish_state_changed()
        return Response(status_code=204)

    def _activate_project(self, arguments: Mapping[str, object]) -> JSONResponse:
        project_id = cast(str, arguments["project_id"])
        if_match = cast(str, arguments["if_match"])
        key = cast(str, arguments["idempotency_key"])
        route = f"/desktop/v1/projects/{project_id}/activate"
        with self._project_session_lock:
            reservation = self._store.begin_project_runtime_action(
                route=route,
                operation_kind="project_activate",
                project_id=project_id,
                key=key,
                body={},
                if_match=if_match,
                admission_guard=lambda project: self._require_supported_execution_mode(
                    "activateProject", project.execution.mode
                ),
            )
        if reservation.replayed:
            return self._project_operation_response(reservation.operation)
        work = _ProjectActivationWork(
            reservation=reservation,
            route=route,
            project_id=project_id,
            key=key,
            if_match=if_match,
        )
        generation: list[int] = []

        def accept() -> None:
            generation.append(self._publish_project_activation_transition(work))

        accepted = self._project_executor.submit(
            reservation.operation.operation_id,
            lambda cancel_event: self._execute_project_activation(
                work, generation[0], cancel_event
            ),
            accepted=accept,
            interrupt=lambda: self._interrupt_project_activation(work),
        )
        operation = reservation.operation
        if not accepted:
            operation = self._store.fail_project_runtime_action(
                reservation=reservation,
                route=route,
                operation_kind="project_activate",
                project_id=project_id,
                key=key,
                body={},
                if_match=if_match,
                error=self._project_queue_capacity_error(reservation),
            )
            self.publish_state_changed()
        return self._project_operation_response(operation)

    def _publish_project_activation_transition(self, work: _ProjectActivationWork) -> int:
        project = work.reservation.project
        if project is None:
            raise ProviderStoreError("project activation reservation lost its project snapshot")
        with self._project_session_lock:
            with self._connection_state_lock:
                self._session_generation += 1
                generation = self._session_generation
                self._connection_owner = None
                self._core_session_binding = CoreRuntimeSessionBinding(
                    project=project,
                    generation=generation,
                )
                self._core_state = CoreConnectionStateV1(
                    state="bootstrapping",
                    profile_id=project.profile_id,
                    active_tunnel=False,
                    operation_id=work.reservation.operation.operation_id,
                )
        try:
            self.publish_state_changed()
        except (ProviderStoreError, sqlite3.Error):
            _LOGGER.exception("project activation transition could not publish invalidation")
        return generation

    def _execute_project_activation(
        self,
        work: _ProjectActivationWork,
        session_generation: int,
        cancel_event: Event,
    ) -> None:
        reservation = work.reservation
        activation: CoreActivationV1 | None = None
        try:
            operation = self._store.start_project_runtime_action(
                reservation=reservation,
                route=work.route,
                operation_kind="project_activate",
                project_id=work.project_id,
                key=work.key,
                body={},
                if_match=work.if_match,
            )
            if operation.state != "running":
                return
            if cancel_event.is_set():
                return
            project = reservation.project
            if project is None:
                raise ProviderStoreError(
                    "project activation reservation lost its project snapshot"
                )
            bridge = self._require_bridge("activateProject")
            activation = bridge.activate_project(
                project,
                idempotency_key=work.key,
                activation_id=reservation.operation.operation_id,
                cancel_event=cancel_event,
            )
            self._validate_activation_identity(project, activation)
            if cancel_event.is_set():
                raise ProviderStoreError(
                    "project activation was cancelled before Local publication"
                )
            with self._project_session_lock:
                with self._connection_state_lock:
                    if not self._owns_core_session_locked(session_generation, project):
                        raise ProviderStoreError(
                            "project activation was superseded before Local publication"
                        )
                operation = self._store.complete_project_runtime_action(
                    reservation=reservation,
                    route=work.route,
                    operation_kind="project_activate",
                    project_id=work.project_id,
                    key=work.key,
                    body={},
                    if_match=work.if_match,
                    remote_state=self._remote_project_state(activation),
                    activation_precommit=lambda active: bridge.commit_local_activation(
                        active, activation=activation
                    ),
                )
                if operation.state != "succeeded":
                    raise ProviderStoreError("project activation did not reach succeeded state")
                active_project = self._store.get_project(work.project_id)
                with self._connection_state_lock:
                    if self._owns_core_session_locked(session_generation, active_project):
                        self._core_session_binding = CoreRuntimeSessionBinding(
                            project=active_project,
                            generation=session_generation,
                        )
                        self._core_state = CoreConnectionStateV1(
                            state="online",
                            profile_id=active_project.profile_id,
                            active_tunnel=True,
                            core=local_v1.CoreCompatibilityV1(
                                contract_digest=CORE_OPENAPI_SHA256,
                                core_version=activation.capabilities.core_version,
                            ),
                        )
        except Exception as exc:
            error = self._project_activation_error(reservation, exc)
            operation = self._finalize_project_runtime_failure(work, error)
            with self._connection_state_lock:
                owns_session = self._owns_core_session_locked(
                    session_generation,
                    reservation.project,
                )
            if activation is not None and (owns_session or operation.state == "cancelled"):
                try:
                    with self._project_session_lock:
                        self._require_bridge("activateProject").deactivate_project(work.project_id)
                except Exception:
                    _LOGGER.warning(
                        "could not retire Core session after failed activation commit",
                        extra={"project_id": work.project_id},
                    )
            profile_id = (
                reservation.project.profile_id if reservation.project is not None else None
            )
            if profile_id is not None:
                with self._connection_state_lock:
                    if self._owns_core_session_locked(
                        session_generation,
                        reservation.project,
                    ):
                        self._core_state = CoreConnectionStateV1(
                            state="offline",
                            profile_id=profile_id,
                            active_tunnel=False,
                            failure=ConnectionFailureV1(
                                code=error.code,
                                message=error.message,
                                retryable=error.retryable,
                                next_action=error.next_action,
                            ),
                        )
            _LOGGER.warning(
                "Desktop project activation failed",
                extra={"project_id": work.project_id, "error_code": error.code},
            )
        finally:
            self.publish_state_changed()

    def _interrupt_project_activation(self, work: _ProjectActivationWork) -> None:
        project = work.reservation.project
        if project is None:
            return
        bridge = self._core_bridge
        if bridge is not None:
            try:
                bridge.cancel_activation(work.reservation.operation.operation_id)
            except Exception:
                _LOGGER.warning(
                    "could not interrupt the active Core project activation",
                    extra={"project_id": work.project_id},
                )
        self._disconnect_owned_transport(project.profile_id)

    def _finalize_project_runtime_failure(
        self,
        work: _ProjectActivationWork,
        error: ApiErrorV1,
    ) -> LocalOperationV1:
        for attempt in range(2):
            try:
                return self._store.fail_project_runtime_action(
                    reservation=work.reservation,
                    route=work.route,
                    operation_kind="project_activate",
                    project_id=work.project_id,
                    key=work.key,
                    body={},
                    if_match=work.if_match,
                    error=error,
                )
            except (ProviderStoreError, sqlite3.Error):
                observed = self._store.observe_project_runtime_action(
                    reservation=work.reservation,
                    route=work.route,
                    operation_kind="project_activate",
                    project_id=work.project_id,
                    key=work.key,
                    body={},
                    if_match=work.if_match,
                )
                if observed.state not in {"queued", "running"}:
                    return observed
                if attempt != 0:
                    raise
        raise AssertionError("project failure finalization loop did not return")

    @staticmethod
    def _validate_activation_identity(
        project: ProjectV1,
        activation: CoreActivationV1,
    ) -> None:
        active_revision = activation.revision_head.active_revision
        if (
            active_revision is None
            or activation.local_project_id != project.project_id
            or activation.profile_id != project.profile_id
            or activation.local_project_etag != project.etag
            or activation.core_project.id != active_revision.project_id
            or activation.core_project.active_revision != active_revision
            or activation.core_project.registry_digest != activation.capabilities.registry_digest
        ):
            raise ProviderStoreError("Core activation authority does not match the local project")

    def _remote_project_state(self, activation: CoreActivationV1) -> local_v1.RemoteProjectStateV1:
        project = activation.core_project
        return local_v1.RemoteProjectStateV1(
            core_project_id=project.id,
            status=project.status.value,
            active_revision=project.active_revision,
            registry_digest=project.registry_digest,
            model_preparation=project.model_preparation,
            observed_at=self._timestamp(),
            etag=project.etag,
        )

    @staticmethod
    def _project_activation_error(
        reservation: ProjectRuntimeActionReservation,
        exc: Exception,
    ) -> ApiErrorV1:
        if isinstance(exc, DesktopCoreBridgeErrorV1):
            return exc.error.model_copy(update={"request_id": reservation.operation.operation_id})
        return ApiErrorV1(
            request_id=reservation.operation.operation_id,
            code="project_activation_failed",
            http_status=503,
            message="OpenEvo Desktop could not activate the remote project.",
            severity=ErrorSeverity.BLOCKING,
            category=ErrorCategory.SERVICE,
            retryable=True,
            repair_action=RepairAction.OPENEVO_CAN_RETRY,
            next_action="Retry project activation from OpenEvo Desktop.",
        )

    @staticmethod
    def _project_queue_capacity_error(
        reservation: ProjectRuntimeActionReservation,
    ) -> ApiErrorV1:
        return ApiErrorV1(
            request_id=reservation.operation.operation_id,
            code="project_operation_capacity_exhausted",
            http_status=503,
            message="OpenEvo Desktop cannot queue another project operation.",
            severity=ErrorSeverity.BLOCKING,
            category=ErrorCategory.SERVICE,
            retryable=True,
            repair_action=RepairAction.OPENEVO_CAN_RETRY,
            next_action="Wait for the current project operation, then retry.",
        )

    @staticmethod
    def _project_operation_response(operation: LocalOperationV1) -> JSONResponse:
        return JSONResponse(
            status_code=202,
            content=operation.model_dump(mode="json"),
            headers={"ETag": operation.etag},
        )

    def _retire_edited_project(self, project_id: str, profile_id: str) -> None:
        if self._core_bridge is None:
            return
        with self._connection_state_lock:
            binding = self._core_session_binding
            self._session_generation += 1
            retirement_generation = self._session_generation
            self._connection_owner = None
            if binding is not None:
                self._core_session_binding = CoreRuntimeSessionBinding(
                    project=binding.project,
                    generation=retirement_generation,
                )
        try:
            self._core_bridge.deactivate_project(project_id)
        except DesktopCoreBridgeErrorV1 as exc:
            with self._connection_state_lock:
                if self._owns_retirement_locked(retirement_generation, project_id):
                    self._core_state = CoreConnectionStateV1(
                        state="offline",
                        profile_id=profile_id,
                        active_tunnel=False,
                        failure=ConnectionFailureV1(
                            code=exc.error.code,
                            message=exc.error.message,
                            retryable=exc.error.retryable,
                            next_action=exc.error.next_action,
                        ),
                    )
            _LOGGER.warning(
                "local project changed after Core session retirement failed",
                extra={"project_id": project_id, "error_code": exc.error.code},
            )
        except Exception:
            with self._connection_state_lock:
                if self._owns_retirement_locked(retirement_generation, project_id):
                    self._core_state = self._local_provider_failure_state(profile_id)
            _LOGGER.exception(
                "local project changed after Core session retirement failed",
                extra={"project_id": project_id},
            )
        else:
            with self._connection_state_lock:
                if self._owns_retirement_locked(retirement_generation, project_id):
                    self._core_session_binding = None
                    self._core_state = self._core_not_started_state(profile_id)

    def _get_project_capabilities(
        self, arguments: Mapping[str, object]
    ) -> local_v1.CapabilitiesEnvelopeV1:
        project_id = cast(str, arguments["project_id"])
        with self._project_session_lock:
            project = self._store.get_project(project_id)
            capabilities = self._require_bridge("getProjectCapabilities").capabilities(project)
        return local_v1.CapabilitiesEnvelopeV1(
            project_id=project.project_id,
            project_etag=project.etag,
            fetched_at=self._timestamp(),
            capabilities=capabilities,
        )

    def _validate_project(self, arguments: Mapping[str, object]) -> local_v1.ProjectValidationV1:
        project_id = cast(str, arguments["project_id"])
        with self._project_session_lock:
            project = self._require_project_match(
                project_id,
                cast(str, arguments["if_match"]),
            )
            validation = self._require_bridge("validateProject").validate_project(
                project,
                idempotency_key=cast(str, arguments["idempotency_key"]),
            )
        return local_v1.ProjectValidationV1(
            project_id=project.project_id,
            project_etag=project.etag,
            registry_digest=validation.registry_digest,
            valid=validation.valid,
            checks=tuple(validation.checks),
            validated_at=validation.validated_at,
        )

    def _get_local_operation(self, arguments: Mapping[str, object]) -> LocalOperationV1:
        with self._project_session_lock:
            return self._store.get_local_operation(cast(str, arguments["operation_id"]))

    def _cancel_local_operation(self, arguments: Mapping[str, object]) -> LocalOperationV1:
        operation_id = cast(str, arguments["operation_id"])
        with self._project_session_lock:
            operation = self._store.get_local_operation(operation_id)
            cancelled = self._store.cancel_local_operation(
                operation_id,
                if_match=cast(str, arguments["if_match"]),
            )
            if operation.state in {"queued", "running", "cancelling"}:
                with self._connection_state_lock:
                    self._session_generation += 1
                    if operation.resource.resource_type == "profile":
                        self._connection_owner = None
                        self._core_state = CoreConnectionStateV1(
                            state="disconnected", active_tunnel=False
                        )
                    elif operation.operation_kind == "project_activate":
                        profile_id = self._core_state.profile_id
                        self._core_session_binding = None
                        self._core_state = (
                            self._core_not_started_state(profile_id)
                            if profile_id is not None
                            else CoreConnectionStateV1(state="disconnected", active_tunnel=False)
                        )
        if operation.state in {"queued", "running", "cancelling"}:
            if operation.operation_kind == "project_activate":
                self._project_executor.cancel(operation.operation_id)
            elif operation.resource.resource_type == "profile":
                self._disconnect_owned_transport(operation.resource.resource_id)
        self.publish_state_changed()
        return cancelled

    def _list_runs(self, arguments: Mapping[str, object]) -> object:
        return self._invoke_active_core(
            "listRuns",
            lambda bridge, project: bridge.list_runs(project, **self._page_arguments(arguments)),
        )

    def _create_run(self, arguments: Mapping[str, object]) -> object:
        request = cast(local_v1.RunCreateV1, arguments["request"])
        if_match = cast(str, arguments["if_match"])

        def create(bridge: DesktopCoreBridgeV1, project: ProjectV1) -> object:
            if project.project_id != request.project_id:
                raise ActiveProjectMismatchError("createRun")
            if not hmac.compare_digest(project.etag, if_match):
                raise ETagConflictError("project", project.project_id, project.etag)
            if project.evolution_configuration_state == "pending":
                raise EvolutionConfigurationPendingError
            return bridge.create_run(
                project, idempotency_key=cast(str, arguments["idempotency_key"])
            )

        return self._invoke_active_core(
            "createRun",
            create,
            enforce_execution_mode_support=True,
        )

    def _get_run(self, arguments: Mapping[str, object]) -> object:
        return self._invoke_active_core(
            "getRun",
            lambda bridge, project: bridge.get_run(project, cast(str, arguments["run_id"])),
        )

    def _delete_run(self, arguments: Mapping[str, object]) -> Response:
        self._invoke_active_core(
            "deleteRun",
            lambda bridge, project: bridge.delete_run(
                project,
                cast(str, arguments["run_id"]),
                if_match=cast(str, arguments["if_match"]),
            ),
        )
        return Response(status_code=204)

    def _cancel_run(self, arguments: Mapping[str, object]) -> object:
        return self._invoke_active_core(
            "cancelRun",
            lambda bridge, project: bridge.cancel_run(
                project,
                cast(str, arguments["run_id"]),
                if_match=cast(str, arguments["if_match"]),
                idempotency_key=cast(str, arguments["idempotency_key"]),
            ),
        )

    def _retry_run(self, arguments: Mapping[str, object]) -> object:
        return self._invoke_active_core(
            "retryRun",
            lambda bridge, project: bridge.retry_run(
                project,
                cast(str, arguments["run_id"]),
                cast(local_v1.RunRetryV1, arguments["request"]),
                if_match=cast(str, arguments["if_match"]),
                idempotency_key=cast(str, arguments["idempotency_key"]),
            ),
            enforce_execution_mode_support=True,
        )

    def _list_run_timeline(self, arguments: Mapping[str, object]) -> object:
        return self._invoke_active_core(
            "listRunTimeline",
            lambda bridge, project: bridge.run_timeline(
                project,
                cast(str, arguments["run_id"]),
                **self._page_arguments(arguments),
            ),
        )

    def _list_run_logs(self, arguments: Mapping[str, object]) -> object:
        return self._invoke_active_core(
            "listRunLogs",
            lambda bridge, project: bridge.run_logs(
                project,
                cast(str, arguments["run_id"]),
                **self._page_arguments(arguments),
            ),
        )

    def _get_run_context(self, arguments: Mapping[str, object]) -> object:
        return self._invoke_active_core(
            "getRunContext",
            lambda bridge, project: bridge.run_context(project, cast(str, arguments["run_id"])),
        )

    def _list_run_artifacts(self, arguments: Mapping[str, object]) -> object:
        return self._invoke_active_core(
            "listRunArtifacts",
            lambda bridge, project: bridge.run_artifacts(
                project,
                cast(str, arguments["run_id"]),
                **self._page_arguments(arguments),
            ),
        )

    def _get_artifact(self, arguments: Mapping[str, object]) -> object:
        return self._invoke_active_core(
            "getArtifact",
            lambda bridge, project: bridge.get_artifact(
                project, cast(str, arguments["artifact_id"])
            ),
        )

    def _get_artifact_content(self, arguments: Mapping[str, object]) -> object:
        return self._invoke_active_core(
            "getArtifactContent",
            lambda bridge, project: bridge.artifact_content(
                project, cast(str, arguments["artifact_id"])
            ),
        )

    def _get_artifact_diff(self, arguments: Mapping[str, object]) -> object:
        return self._invoke_active_core(
            "getArtifactDiff",
            lambda bridge, project: bridge.artifact_diff(
                project, cast(str, arguments["artifact_id"])
            ),
        )

    def _list_services(self, arguments: Mapping[str, object]) -> object:
        page = self._page_arguments(arguments)
        if page["sort"] == "display_name":
            page["sort"] = "kind"
        return self._invoke_active_core(
            "listServices",
            lambda bridge, project: bridge.list_services(project, **page),
        )

    def _get_core_operation(self, arguments: Mapping[str, object]) -> object:
        return self._invoke_active_core(
            "getCoreOperation",
            lambda bridge, project: bridge.get_operation(
                project, cast(str, arguments["operation_id"])
            ),
        )

    def _get_core_logs_by_ref(self, arguments: Mapping[str, object]) -> object:
        return self._invoke_active_core(
            "getCoreLogsByRef",
            lambda bridge, project: bridge.logs_by_ref(
                project,
                cast(str, arguments["logs_ref"]),
                **self._page_arguments(arguments),
            ),
        )

    def _list_service_logs(self, arguments: Mapping[str, object]) -> object:
        return self._invoke_active_core(
            "listServiceLogs",
            lambda bridge, project: bridge.service_logs(
                project,
                cast(str, arguments["service_id"]),
                **self._page_arguments(arguments),
            ),
        )

    def _cleanup_caches(self, arguments: Mapping[str, object]) -> object:
        return self._invoke_active_core(
            "cleanupCaches",
            lambda bridge, project: bridge.cache_cleanup(
                project,
                cast(local_v1.CacheCleanupRequestV1, arguments["request"]),
                idempotency_key=cast(str, arguments["idempotency_key"]),
            ),
        )

    def _subscribe_events(self, arguments: Mapping[str, object]) -> StreamingResponse:
        broker = self._require_event_broker("subscribeDesktopEvents")
        subscription = broker.subscribe(cast(str | None, arguments["last_event_id"]))
        return StreamingResponse(
            subscription,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store",
                "X-Accel-Buffering": "no",
            },
        )

    def publish_state_changed(self) -> None:
        """Publish one invalidation snapshot after a durable Desktop transition."""

        if self._event_broker is not None:
            try:
                self._publish_core_event_invalidation()
            except DesktopEventBrokerError:
                _LOGGER.warning("Desktop state event could not be published")

    def _publish_core_event_invalidation(self) -> None:
        if self._event_broker is not None:
            self._event_broker.publish(StateEventV1(state=self._get_state({})))

    def _active_project_for_runtime(self) -> CoreRuntimeSessionBinding | None:
        with self._project_session_lock:
            projects = self._store.list_projects(limit=2, filters={"state": "active"}).items
            if len(projects) > 1:
                raise ProviderStoreError("multiple active projects violate Desktop authority")
            if not projects:
                return None
            project = projects[0]
            with self._connection_state_lock:
                binding = self._core_session_binding
                state = self._core_state
            if (
                binding is None
                or binding.project.project_id != project.project_id
                or binding.project.profile_id != project.profile_id
                or binding.project.etag != project.etag
                or binding.generation != self._session_generation
                or state.state not in {"online", "degraded"}
                or not state.active_tunnel
                or state.profile_id != project.profile_id
            ):
                return None
            return binding

    def _handle_core_session_loss(
        self,
        binding: CoreRuntimeSessionBinding,
        exc: DesktopCoreBridgeErrorV1,
    ) -> None:
        if exc.error.code not in _LOCAL_CORE_SESSION_LOSS_CODES:
            return
        changed = False
        with self._connection_state_lock:
            if self._closed or self._core_session_binding != binding:
                return
            if self._core_state.state not in {"online", "degraded"}:
                return
            self._core_state = CoreConnectionStateV1(
                state="offline",
                profile_id=binding.project.profile_id,
                active_tunnel=False,
                failure=ConnectionFailureV1(
                    code=exc.error.code,
                    message=exc.error.message,
                    retryable=exc.error.retryable,
                    next_action=exc.error.next_action,
                ),
            )
            changed = True
        if changed:
            self.publish_state_changed()

    def _owns_core_session_locked(
        self,
        generation: int,
        project: ProjectV1 | None,
    ) -> bool:
        binding = self._core_session_binding
        return (
            project is not None
            and generation == self._session_generation
            and binding is not None
            and binding.generation == generation
            and binding.project.project_id == project.project_id
            and binding.project.profile_id == project.profile_id
        )

    def _owns_retirement_locked(self, generation: int, project_id: str) -> bool:
        binding = self._core_session_binding
        return generation == self._session_generation and (
            binding is None
            or (binding.generation == generation and binding.project.project_id == project_id)
        )

    def _require_bridge(self, operation_id: str) -> DesktopCoreBridgeV1:
        if self._core_bridge is None:
            self._unavailable(operation_id)
        return self._core_bridge

    def _require_event_broker(self, operation_id: str) -> DesktopEventBrokerV1:
        if self._event_broker is None:
            self._unavailable(operation_id)
        return self._event_broker

    def _invoke_active_core(
        self,
        operation_id: str,
        call: Callable[[DesktopCoreBridgeV1, ProjectV1], object],
        *,
        enforce_execution_mode_support: bool = False,
    ) -> object:
        bridge = self._require_bridge(operation_id)
        with self._project_session_lock:
            binding = self._active_project_for_runtime()
            if binding is None:
                raise ProviderStoreError("Desktop has no active project for this Core request")
            if enforce_execution_mode_support:
                self._require_supported_execution_mode(
                    operation_id, binding.project.execution.mode
                )
            try:
                return call(bridge, binding.project)
            except DesktopCoreBridgeErrorV1 as exc:
                self._handle_core_session_loss(binding, exc)
                raise

    def _require_project_match(self, project_id: str, if_match: str) -> ProjectV1:
        project = self._store.get_project(project_id)
        if project.etag != if_match:
            raise ETagConflictError("project", project_id, project.etag)
        return project

    @staticmethod
    def _page_arguments(arguments: Mapping[str, object]) -> dict[str, object]:
        return {
            "limit": cast(int, arguments["limit"]),
            "after": cast(str | None, arguments["after"]),
            "sort": cast(str, arguments["sort"]),
            "direction": cast(str, arguments["direction"]),
        }

    def _timestamp(self) -> str:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("provider clock must return a timezone-aware datetime")
        return (
            now.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        )

    def _verify_project_source(self, source: ProjectSourceV1, *, project_id: str | None) -> None:
        if source.kind != "native_folder_snapshot":
            return
        import_ref = source.import_ref
        if import_ref is None:
            raise ValueError("native folder source requires an import reference")
        self._workspace_import_store.verify(
            import_ref,
            ownership=ownership_for_native_import(import_ref, project_id=project_id),
        )

    def _reconcile_workspace_imports(self) -> None:
        with self._store.workspace_import_reference_guard():
            references = self._workspace_import_references()
            self._workspace_import_store.reconcile_references(references)

    def discard_pending_workspace_import(
        self,
        import_ref: WorkspaceImportRefV1,
        *,
        project_id: str | None,
        lease_token: str,
    ) -> None:
        """Discard one native picker lease unless durable project state references it."""

        requested_ownership = ownership_for_native_import(
            import_ref,
            project_id=project_id,
        )
        with self._store.workspace_import_reference_guard():
            references = self._workspace_import_references()
            durable = references.get(import_ref.import_id)
            if durable is not None:
                durable_ref, durable_ownership = durable
                if durable_ref != import_ref or durable_ownership != requested_ownership:
                    raise WorkspaceImportError(
                        "workspace import durable reference conflicts with pending lease"
                    )
                self._workspace_import_store.adopt_pending(
                    durable_ref,
                    ownership=durable_ownership,
                )
                return
            self._workspace_import_store.discard_pending(
                import_ref,
                ownership=requested_ownership,
                lease_token=lease_token,
            )

    def _workspace_import_references(
        self,
    ) -> dict[str, tuple[WorkspaceImportRefV1, WorkspaceImportOwnership]]:
        references: dict[str, tuple[WorkspaceImportRefV1, WorkspaceImportOwnership]] = {}
        for project_id, source in self._store.native_workspace_sources():
            import_ref = source.import_ref
            if import_ref is None:
                raise ValueError("native folder source requires an import reference")
            if import_ref.import_id in references:
                raise ValueError("workspace import is referenced by multiple projects")
            references[import_ref.import_id] = (
                import_ref,
                ownership_for_native_import(import_ref, project_id=project_id),
            )
        return references

    def _release_project_source(self, source: ProjectSourceV1, *, project_id: str) -> None:
        if source.kind != "native_folder_snapshot" or source.import_ref is None:
            return
        try:
            with self._store.workspace_import_reference_guard():
                references = self._workspace_import_references()
                if source.import_ref.import_id in references:
                    return
                self._workspace_import_store.release(
                    source.import_ref,
                    ownership=ownership_for_native_import(
                        source.import_ref,
                        project_id=project_id,
                    ),
                )
        except (OSError, WorkspaceImportError):
            # The project transaction is already durable. Startup reconciliation
            # retries cleanup and fails closed if referenced storage is damaged.
            _LOGGER.warning(
                "deferred workspace import cleanup after committed project mutation",
                extra={"project_id": project_id},
            )

    def _adopt_project_source(self, source: ProjectSourceV1, *, project_id: str) -> None:
        if source.kind != "native_folder_snapshot" or source.import_ref is None:
            return
        self._workspace_import_store.adopt_pending(
            source.import_ref,
            ownership=ownership_for_native_import(
                source.import_ref,
                project_id=project_id,
            ),
        )

    @staticmethod
    def _resource_response(
        resource: RemoteProfileV1 | ProjectV1, *, status_code: int = 200
    ) -> Response:
        return JSONResponse(
            content=resource.model_dump(mode="json"),
            status_code=status_code,
            headers={"ETag": resource.etag},
        )

    @staticmethod
    def _unavailable(operation_id: str) -> NoReturn:
        raise ProviderCapabilityUnavailableError(operation_id)

    def _require_supported_execution_mode(
        self,
        operation_id: str,
        mode: ExecutionModeV1,
    ) -> None:
        capability = self._execution_modes.get(mode)
        if capability is None or capability.support_state != "supported":
            if capability is None:
                raise ProviderStoreError("release execution mode capability is missing")
            raise ExecutionModeReleaseUnavailableError(operation_id, capability)


__all__ = (
    "ActiveProjectMismatchError",
    "DesktopReleaseProvider",
    "EvolutionConfigurationPendingError",
    "ExecutionModeReleaseUnavailableError",
    "InvalidNativeChallengeError",
    "NATIVE_SIDECAR_PROTOCOL",
    "ProviderCapabilityUnavailableError",
)
