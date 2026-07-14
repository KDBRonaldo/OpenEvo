from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import hashlib
import hmac
import re
from threading import Lock
from typing import Literal, NoReturn, cast

from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from desktop.sidecar.contracts.v1.canonical import DESKTOP_OPENAPI_SHA256
from desktop.sidecar.contracts.v1.models import (
    ActiveProjectStateV1,
    ConnectionFailureV1,
    ConnectionOperationResultV1,
    ContractNegotiationV1,
    CoreConnectionStateV1,
    DesktopStateV1,
    HealthV1,
    HostKeyAcceptV1,
    HostKeyReviewV1,
    LocalOperationV1,
    ProjectCreateV1,
    ProjectPatchV1,
    ProjectV1,
    RemoteProfileCreateV1,
    RemoteProfilePatchV1,
    RemoteProfileV1,
    ResourceRefV1,
    VersionV1,
)
from desktop.sidecar.provider_store import DesktopProviderStore, ResourceInUseError
from desktop.sidecar.remote_lifecycle import (
    DesktopRemoteLifecycle,
    RemoteConnectionFailedError,
    RemoteCredentialUnavailableError,
    RemoteLifecycleError,
    RemoteLifecycleSupersededError,
)


NATIVE_SIDECAR_PROTOCOL = "openevo-native-sidecar-v1"


class ProviderCapabilityUnavailableError(Exception):
    """The release provider has no verified implementation for an operation."""

    def __init__(self, operation_id: str) -> None:
        super().__init__("required provider capability is unavailable")
        self.operation_id = operation_id


class InvalidNativeChallengeError(Exception):
    """The native readiness challenge is missing or malformed."""


OperationHandler = Callable[[Mapping[str, object]], object]
ProfileRuntimeState = Literal["connected", "disconnected", "host_key_required"]
ConnectionAction = Callable[[RemoteProfileV1], tuple[ProfileRuntimeState, str | None]]


class DesktopReleaseProvider:
    """First release provider slice backed by ``DesktopProviderStore``."""

    def __init__(
        self,
        store: DesktopProviderStore,
        *,
        build_version: str,
        source_commit: str,
        build_channel: str,
        instance_id: str,
        readiness_key: bytes,
        remote_lifecycle: DesktopRemoteLifecycle,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if re.fullmatch(r"[0-9a-f]{32}", instance_id) is None:
            raise ValueError("native instance id must be 32 lowercase hex characters")
        if type(readiness_key) is not bytes or len(readiness_key) != 32:
            raise ValueError("native readiness key must contain exactly 32 bytes")
        self._store = store
        self._remote_lifecycle = remote_lifecycle
        self._connection_state_lock = Lock()
        self._core_state = CoreConnectionStateV1(state="disconnected", active_tunnel=False)
        self._instance_id = instance_id
        self._readiness_key = readiness_key
        self._clock = clock or (lambda: datetime.now(timezone.utc))
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
        }

    def close(self) -> None:
        try:
            self._remote_lifecycle.close()
        finally:
            self._store.close()

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

    def _connect_profile(self, arguments: Mapping[str, object]) -> LocalOperationV1:
        profile_id = cast(str, arguments["profile_id"])
        if_match = cast(str, arguments["if_match"])
        idempotency_key = cast(str, arguments["idempotency_key"])

        def connect(profile: RemoteProfileV1) -> tuple[ProfileRuntimeState, str | None]:
            result = self._remote_lifecycle.connect(profile)
            fingerprint = (
                result.host_key_candidate.fingerprint
                if result.host_key_candidate is not None
                else profile.host_key_fingerprint
            )
            return result.state, fingerprint

        return self._execute_connection_action(
            route=f"/desktop/v1/profiles/{profile_id}/connect",
            operation_kind="profile_connect",
            profile_id=profile_id,
            if_match=if_match,
            idempotency_key=idempotency_key,
            request_body={},
            action=connect,
        )

    def _accept_host_key(self, arguments: Mapping[str, object]) -> LocalOperationV1:
        profile_id = cast(str, arguments["profile_id"])
        if_match = cast(str, arguments["if_match"])
        idempotency_key = cast(str, arguments["idempotency_key"])
        request = cast(HostKeyAcceptV1, arguments["request"])

        def accept(profile: RemoteProfileV1) -> tuple[ProfileRuntimeState, str | None]:
            result = self._remote_lifecycle.accept_host_key(profile, request)
            return result.state, request.fingerprint

        return self._execute_connection_action(
            route=f"/desktop/v1/profiles/{profile_id}/host-key/accept",
            operation_kind="host_key_accept",
            profile_id=profile_id,
            if_match=if_match,
            idempotency_key=idempotency_key,
            request_body=request,
            action=accept,
        )

    def _disconnect_profile(self, arguments: Mapping[str, object]) -> LocalOperationV1:
        profile_id = cast(str, arguments["profile_id"])
        if_match = cast(str, arguments["if_match"])
        idempotency_key = cast(str, arguments["idempotency_key"])

        def disconnect(profile: RemoteProfileV1) -> tuple[ProfileRuntimeState, str | None]:
            self._remote_lifecycle.disconnect(profile.profile_id)
            return "disconnected", profile.host_key_fingerprint

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
    ) -> LocalOperationV1:
        def mutation(transaction):
            profile = transaction.require_profile_authority(profile_id, if_match=if_match)
            connection_state, host_key_fingerprint = action(profile)
            updated = transaction.set_profile_runtime_state(
                profile_id,
                if_match=if_match,
                connection_state=connection_state,
                credential_slots=profile.credential_slots,
                host_key_fingerprint=host_key_fingerprint,
            )
            return 202, transaction.create_local_operation(
                operation_kind=operation_kind,
                resource=ResourceRefV1(
                    resource_type="profile", resource_id=profile_id
                ),
                state="succeeded",
                result=ConnectionOperationResultV1(
                    profile_id=profile_id,
                    connection_state=updated.connection_state,
                ),
            )

        try:
            stored = self._store.execute_idempotent_action(
                route=route,
                resource_scope=profile_id,
                key=idempotency_key,
                body=request_body,
                if_match=if_match,
                semantic_headers={},
                response_model=LocalOperationV1,
                mutation=mutation,
            )
        except RemoteLifecycleError as exc:
            self._set_core_state(self._connection_failure_state(profile_id, exc))
            raise
        operation = LocalOperationV1.model_validate_json(stored.response_bytes)
        self._sync_core_state(profile_id, operation.operation_id)
        return operation

    def _require_profile_not_connected(self, profile_id: str) -> None:
        snapshot = self._remote_lifecycle.snapshot()
        if snapshot.profile_id == profile_id and snapshot.state != "disconnected":
            raise ResourceInUseError("profile", profile_id)

    def _sync_core_state(self, profile_id: str, operation_id: str) -> None:
        snapshot = self._remote_lifecycle.snapshot()
        if snapshot.profile_id not in {None, profile_id}:
            return
        if snapshot.state == "disconnected":
            state = CoreConnectionStateV1(state="disconnected", active_tunnel=False)
        elif snapshot.state == "host_key_required":
            candidate = snapshot.host_key_candidate
            if candidate is None:
                state = self._connection_failure_state(
                    profile_id,
                    RemoteConnectionFailedError("SSH host-key review state is incomplete."),
                )
            else:
                state = CoreConnectionStateV1(
                    state="host_key_review",
                    profile_id=profile_id,
                    active_tunnel=False,
                    operation_id=operation_id,
                    host_key_review=HostKeyReviewV1(
                        algorithm=candidate.algorithm,
                        fingerprint=candidate.fingerprint,
                    ),
                )
        elif snapshot.state == "connected":
            state = self._core_not_started_state(profile_id)
        elif snapshot.state == "connecting":
            state = CoreConnectionStateV1(
                state="connecting",
                profile_id=profile_id,
                active_tunnel=False,
                operation_id=operation_id,
            )
        else:
            state = self._connection_failure_state(
                profile_id,
                RemoteConnectionFailedError("The SSH connection is unavailable."),
            )
        self._set_core_state(state)

    def _set_core_state(self, state: CoreConnectionStateV1) -> None:
        with self._connection_state_lock:
            self._core_state = state

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
        project = self._store.create_project(
            cast(ProjectCreateV1, arguments["request"]),
            idempotency_key=cast(str, arguments["idempotency_key"]),
        )
        return self._resource_response(project, status_code=201)

    def _get_project(self, arguments: Mapping[str, object]) -> Response:
        project = self._store.get_project(cast(str, arguments["project_id"]))
        return self._resource_response(project)

    def _update_project(self, arguments: Mapping[str, object]) -> Response:
        project = self._store.patch_project(
            cast(str, arguments["project_id"]),
            cast(ProjectPatchV1, arguments["request"]),
            if_match=cast(str, arguments["if_match"]),
        )
        return self._resource_response(project)

    def _delete_project(self, arguments: Mapping[str, object]) -> Response:
        self._store.delete_project(
            cast(str, arguments["project_id"]),
            if_match=cast(str, arguments["if_match"]),
        )
        return Response(status_code=204)

    def _timestamp(self) -> str:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("provider clock must return a timezone-aware datetime")
        return (
            now.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
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
