from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import logging
import re
import sqlite3
from threading import Lock
from typing import Literal, NoReturn, cast

from fastapi.responses import JSONResponse, Response
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
    VersionV1,
    WorkspaceImportRefV1,
)
from desktop.sidecar.core_bridge_v1 import DesktopCoreBridgeV1
from desktop.sidecar.provider_store import (
    DesktopProviderStore,
    ETagConflictError,
    ProfileRuntimeActionReservation,
    ProviderStoreError,
    ResourceNotFoundError,
    ResourceInUseError,
)
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


class ProviderCapabilityUnavailableError(Exception):
    """The release provider has no verified implementation for an operation."""

    def __init__(self, operation_id: str) -> None:
        super().__init__("required provider capability is unavailable")
        self.operation_id = operation_id


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


@dataclass
class _ConnectionActionGate:
    lock: Lock
    users: int = 0


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
        remote_lifecycle: DesktopRemoteLifecycle,
        core_bridge: DesktopCoreBridgeV1 | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if re.fullmatch(r"[0-9a-f]{32}", instance_id) is None:
            raise ValueError("native instance id must be 32 lowercase hex characters")
        if type(readiness_key) is not bytes or len(readiness_key) != 32:
            raise ValueError("native readiness key must contain exactly 32 bytes")
        self._store = store
        self._workspace_import_store = workspace_import_store
        self._remote_lifecycle = remote_lifecycle
        self._core_bridge = core_bridge
        self._connection_state_lock = Lock()
        self._connection_generation = 0
        self._connection_owner: str | None = None
        self._connection_action_lock = Lock()
        self._connection_gate_lock = Lock()
        self._connection_gates: dict[tuple[str, str, str], _ConnectionActionGate] = {}
        self._core_state = CoreConnectionStateV1(state="disconnected", active_tunnel=False)
        self._instance_id = instance_id
        self._readiness_key = readiness_key
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._reconcile_workspace_imports()
        self._version = VersionV1(
            openapi_sha256=DESKTOP_OPENAPI_SHA256,
            build_version=build_version,
            source_commit=source_commit,
            build_channel=build_channel,
            provider_kind="desktop_sidecar",
            feature_flags=("remote_profiles",),
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
            "getProjectCapabilities": self._get_project_capabilities,
            "validateProject": self._validate_project,
            "getLocalOperation": self._get_local_operation,
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
            "restartService": self._restart_service,
            "getCoreOperation": self._get_core_operation,
            "getCoreLogsByRef": self._get_core_logs_by_ref,
            "listServiceLogs": self._list_service_logs,
            "createDiagnostic": self._create_diagnostic,
            "getDiagnostic": self._get_diagnostic,
            "deleteDiagnostic": self._delete_diagnostic,
            "cleanupCaches": self._cleanup_caches,
        }

    def close(self) -> None:
        try:
            if self._core_bridge is not None:
                self._core_bridge.close()
        finally:
            try:
                self._remote_lifecycle.close()
            finally:
                try:
                    self._store.close()
                finally:
                    self._workspace_import_store.close()

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
        active_projects = self._store.list_projects(limit=2, filters={"state": "active"}).items
        active_project = None
        if active_projects:
            project = active_projects[0]
            active_project = ActiveProjectStateV1(
                project_id=project.project_id,
                project_etag=project.etag,
                profile_id=project.profile_id,
                connection_state="offline",
            )
        with self._connection_state_lock:
            core_state = self._core_state
        return DesktopStateV1(
            observed_at=self._timestamp(),
            contract=ContractNegotiationV1(
                selected_major=1,
                desktop_openapi_sha256=DESKTOP_OPENAPI_SHA256,
                core_openapi_sha256=None,
                compatible=True,
            ),
            core=core_state,
            active_project=active_project,
            pending_operation_ids=(),
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
        self._require_profile_not_connected(cast(str, arguments["profile_id"]))
        profile = self._store.patch_profile(
            cast(str, arguments["profile_id"]),
            cast(RemoteProfilePatchV1, arguments["request"]),
            if_match=cast(str, arguments["if_match"]),
        )
        return self._resource_response(profile)

    def _delete_profile(self, arguments: Mapping[str, object]) -> Response:
        self._require_profile_not_connected(cast(str, arguments["profile_id"]))
        self._store.delete_profile(
            cast(str, arguments["profile_id"]),
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
                self._connection_generation += 1
                generation = self._connection_generation
                self._connection_owner = profile_id
        profile = reservation.profile
        if profile is None:
            raise ProviderStoreError("new profile runtime reservation has no profile snapshot")

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
                self._disconnect_owned_transport(profile_id)
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
        return generation == self._connection_generation and self._connection_owner == profile_id

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
                next_action=(
                    "Choose SSH agent authentication or configure the native credential."
                ),
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
            next_action = "Choose SSH agent authentication or configure the native credential."
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
        with self._store.workspace_import_reference_guard():
            previous = self._store.get_project(project_id)
            if request.source is not None:
                self._verify_project_source(request.source, project_id=project_id)
            project = self._store.patch_project(
                project_id,
                request,
                if_match=cast(str, arguments["if_match"]),
            )
            self._adopt_project_source(project.source, project_id=project_id)
        if previous.source.import_ref != project.source.import_ref:
            self._release_project_source(previous.source, project_id=project_id)
        return self._resource_response(project)

    def _delete_project(self, arguments: Mapping[str, object]) -> Response:
        project_id = cast(str, arguments["project_id"])
        with self._store.workspace_import_reference_guard():
            project = self._store.get_project(project_id)
            self._store.delete_project(
                project_id,
                if_match=cast(str, arguments["if_match"]),
            )
        self._release_project_source(project.source, project_id=project_id)
        return Response(status_code=204)

    def _get_project_capabilities(
        self, arguments: Mapping[str, object]
    ) -> local_v1.CapabilitiesEnvelopeV1:
        project_id = cast(str, arguments["project_id"])
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
        return self._store.get_local_operation(cast(str, arguments["operation_id"]))

    def _list_runs(self, arguments: Mapping[str, object]) -> object:
        return self._require_bridge("listRuns").list_runs(**self._page_arguments(arguments))

    def _create_run(self, arguments: Mapping[str, object]) -> object:
        request = cast(local_v1.RunCreateV1, arguments["request"])
        project = self._require_project_match(
            request.project_id,
            cast(str, arguments["if_match"]),
        )
        return self._require_bridge("createRun").create_run(
            project,
            idempotency_key=cast(str, arguments["idempotency_key"]),
        )

    def _get_run(self, arguments: Mapping[str, object]) -> object:
        return self._require_bridge("getRun").get_run(cast(str, arguments["run_id"]))

    def _delete_run(self, arguments: Mapping[str, object]) -> Response:
        self._require_bridge("deleteRun").delete_run(
            cast(str, arguments["run_id"]),
            if_match=cast(str, arguments["if_match"]),
        )
        return Response(status_code=204)

    def _cancel_run(self, arguments: Mapping[str, object]) -> object:
        return self._require_bridge("cancelRun").cancel_run(
            cast(str, arguments["run_id"]),
            if_match=cast(str, arguments["if_match"]),
            idempotency_key=cast(str, arguments["idempotency_key"]),
        )

    def _retry_run(self, arguments: Mapping[str, object]) -> object:
        return self._require_bridge("retryRun").retry_run(
            cast(str, arguments["run_id"]),
            if_match=cast(str, arguments["if_match"]),
            idempotency_key=cast(str, arguments["idempotency_key"]),
        )

    def _list_run_timeline(self, arguments: Mapping[str, object]) -> object:
        return self._require_bridge("listRunTimeline").run_timeline(
            cast(str, arguments["run_id"]),
            **self._page_arguments(arguments),
        )

    def _list_run_logs(self, arguments: Mapping[str, object]) -> object:
        return self._require_bridge("listRunLogs").run_logs(
            cast(str, arguments["run_id"]),
            **self._page_arguments(arguments),
        )

    def _get_run_context(self, arguments: Mapping[str, object]) -> object:
        return self._require_bridge("getRunContext").run_context(cast(str, arguments["run_id"]))

    def _list_run_artifacts(self, arguments: Mapping[str, object]) -> object:
        return self._require_bridge("listRunArtifacts").run_artifacts(
            cast(str, arguments["run_id"]),
            **self._page_arguments(arguments),
        )

    def _get_artifact(self, arguments: Mapping[str, object]) -> object:
        return self._require_bridge("getArtifact").get_artifact(
            cast(str, arguments["artifact_id"])
        )

    def _get_artifact_content(self, arguments: Mapping[str, object]) -> object:
        return self._require_bridge("getArtifactContent").artifact_content(
            cast(str, arguments["artifact_id"])
        )

    def _get_artifact_diff(self, arguments: Mapping[str, object]) -> object:
        return self._require_bridge("getArtifactDiff").artifact_diff(
            cast(str, arguments["artifact_id"])
        )

    def _list_services(self, arguments: Mapping[str, object]) -> object:
        page = self._page_arguments(arguments)
        if page["sort"] == "display_name":
            page["sort"] = "kind"
        return self._require_bridge("listServices").list_services(**page)

    def _restart_service(self, arguments: Mapping[str, object]) -> object:
        return self._require_bridge("restartService").restart_service(
            cast(str, arguments["service_id"]),
            if_match=cast(str, arguments["if_match"]),
            idempotency_key=cast(str, arguments["idempotency_key"]),
        )

    def _get_core_operation(self, arguments: Mapping[str, object]) -> object:
        return self._require_bridge("getCoreOperation").get_operation(
            cast(str, arguments["operation_id"])
        )

    def _get_core_logs_by_ref(self, arguments: Mapping[str, object]) -> object:
        return self._require_bridge("getCoreLogsByRef").logs_by_ref(
            cast(str, arguments["logs_ref"]),
            **self._page_arguments(arguments),
        )

    def _list_service_logs(self, arguments: Mapping[str, object]) -> object:
        return self._require_bridge("listServiceLogs").service_logs(
            cast(str, arguments["service_id"]),
            **self._page_arguments(arguments),
        )

    def _create_diagnostic(self, arguments: Mapping[str, object]) -> object:
        return self._require_bridge("createDiagnostic").create_diagnostic(
            cast(local_v1.DiagnosticRequestV1, arguments["request"]),
            idempotency_key=cast(str, arguments["idempotency_key"]),
        )

    def _get_diagnostic(self, arguments: Mapping[str, object]) -> object:
        return self._require_bridge("getDiagnostic").get_diagnostic(
            cast(str, arguments["diagnostic_id"])
        )

    def _delete_diagnostic(self, arguments: Mapping[str, object]) -> Response:
        self._require_bridge("deleteDiagnostic").delete_diagnostic(
            cast(str, arguments["diagnostic_id"]),
            if_match=cast(str, arguments["if_match"]),
            idempotency_key=cast(str, arguments["idempotency_key"]),
        )
        return Response(status_code=204)

    def _cleanup_caches(self, arguments: Mapping[str, object]) -> object:
        return self._require_bridge("cleanupCaches").cache_cleanup(
            cast(local_v1.CacheCleanupRequestV1, arguments["request"]),
            idempotency_key=cast(str, arguments["idempotency_key"]),
        )

    def _require_bridge(self, operation_id: str) -> DesktopCoreBridgeV1:
        if self._core_bridge is None:
            self._unavailable(operation_id)
        return self._core_bridge

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


__all__ = (
    "DesktopReleaseProvider",
    "InvalidNativeChallengeError",
    "NATIVE_SIDECAR_PROTOCOL",
    "ProviderCapabilityUnavailableError",
)
