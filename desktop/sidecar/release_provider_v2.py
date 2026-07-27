"""Packaged Desktop Local API v2 provider.

This module owns renderer-safe local projection only.  System OpenSSH remains
the connection authority before compatibility, while every business resource
is delegated to the active project Core v2 bridge.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import hashlib
import json
import threading
from typing import Literal, Protocol, TypeVar

from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from desktop.sidecar.askpass_broker import AskpassPromptObservation
from desktop.sidecar.contracts.v1 import WorkspaceImportRefV1
from desktop.sidecar.contracts.v2 import models as local_v2
from desktop.sidecar.core_bridge_v2 import (
    CoreProjectMappingV2,
    DesktopCoreBridgeErrorV2,
)
from desktop.sidecar.event_broker_v2 import DesktopEventBrokerV2
from desktop.sidecar.provider_store_v2 import DesktopProviderStoreV2
from desktop.sidecar.release_capabilities import (
    V0110_RELEASE_AUTHORITY_POLICY,
    ReleaseAuthorityNegotiationError,
    negotiate_core_v2_mutation,
)
from desktop.sidecar.system_ssh_session import SystemOpenSshSessionError
from desktop.sidecar.workspace_identity import (
    native_import_id_for_action,
    ownership_for_native_import,
    project_id_for_native_import,
)
from desktop.sidecar.workspace_imports import (
    WorkspaceImportIntegrityError,
    WorkspaceImportNotFoundError,
    WorkspaceImportStore,
)
from openevo.backend.contracts.v2 import models as core_v2


class DesktopReleaseProviderV2Error(RuntimeError):
    """One closed renderer-safe provider failure."""

    def __init__(self, status_code: int, error: local_v2.DesktopErrorV2) -> None:
        self.status_code = status_code
        self.error = error
        super().__init__(error.code)


class _CatalogProviderV2(Protocol):
    def list_catalog(self) -> local_v2.SshHostCatalogV2: ...

    def rescan(
        self,
        request: local_v2.SshHostCatalogRescanV2,
        *,
        resource_generation: int,
        idempotency_key: str,
    ) -> local_v2.SshHostCatalogV2: ...


class _RemoteLifecycleV2(Protocol):
    def connect(self, profile: local_v2.RemoteWorkspaceProfileV2) -> None: ...

    def disconnect(self, profile_id: str, connection_generation: int) -> None: ...

    def review_host_key(
        self,
        profile: local_v2.RemoteWorkspaceProfileV2,
        request: local_v2.HostKeyReviewRequestV2,
    ) -> Literal["connected", "rejected"]: ...

    def close(self) -> None: ...


class _CoreProfileConnectorV2(Protocol):
    def connect_profile(
        self,
        profile_id: str,
        profile_connection_generation: int,
    ) -> core_v2.VersionResponseV2: ...

    def close(self) -> None: ...


class _CoreBridgeStoreLookupV2(Protocol):
    def load_mapping_by_core_project_id(
        self,
        core_project_id: str,
    ) -> CoreProjectMappingV2 | None: ...

    def close(self) -> None: ...


class _CoreBridgeV2(Protocol):
    @property
    def active_activation(self) -> object | None: ...

    def activate_project(
        self,
        desktop_project_id: str,
        request: local_v2.ProjectCreateV2,
        *,
        idempotency_key: str,
    ) -> object: ...

    def deactivate_project(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
    ) -> None: ...

    def get_project(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
    ) -> core_v2.ProjectV2: ...

    def create_workspace_upload(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        request: core_v2.WorkspaceUploadCreateV2,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> core_v2.WorkspaceUploadSessionV2: ...

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
    ) -> core_v2.WorkspaceUploadSessionV2: ...

    def finalize_workspace_upload(
        self,
        desktop_project_id: str,
        profile_connection_generation: int,
        upload_id: str,
        request: core_v2.WorkspaceUploadFinalizeV2,
        *,
        if_match: str,
        idempotency_key: str,
    ) -> core_v2.WorkspaceUploadSessionV2: ...

    def close(self) -> None: ...


_OPERATIONS = frozenset(
    {
        "getDesktopContractVersionV2",
        "getDesktopHealthV2",
        "getDesktopStateV2",
        "listConfiguredSshHostsV2",
        "rescanConfiguredSshHostsV2",
        "listRemoteWorkspaceProfilesV2",
        "createSystemOpenSshProfileV2",
        "getRemoteWorkspaceProfileV2",
        "renameRemoteWorkspaceProfileV2",
        "deleteRemoteWorkspaceProfileV2",
        "rebindLegacyProfileToSystemOpenSshV2",
        "connectRemoteWorkspaceProfileV2",
        "disconnectRemoteWorkspaceProfileV2",
        "reviewRemoteWorkspaceHostKeyV2",
        "listDesktopProjectsV2",
        "createDesktopProjectV2",
        "getDesktopProjectV2",
        "updateDesktopProjectV2",
        "activateDesktopProjectV2",
        "getDesktopProjectCapabilitiesV2",
        "validateDesktopProjectV2",
        "listDesktopTasksV2",
        "submitDesktopTaskV2",
        "getDesktopTaskV2",
        "cancelDesktopTaskV2",
        "retryDesktopTaskV2",
        "getDesktopTaskTimelineV2",
        "getDesktopTaskLogsV2",
        "getDesktopTaskContextV2",
        "listDesktopTaskArtifactsV2",
        "getDesktopProjectHeadV2",
        "getDesktopEvolutionRevisionV2",
        "getDesktopRuntimeContextV2",
        "getDesktopTransitionV2",
        "retryDesktopTransitionV2",
        "replaceDesktopTransitionV2",
        "abandonDesktopTransitionV2",
        "getDesktopArtifactV2",
        "getDesktopArtifactContentV2",
        "getDesktopArtifactDiffV2",
        "listDesktopServicesV2",
        "restartDesktopServiceV2",
        "createDesktopDiagnosticV2",
        "getDesktopDiagnosticV2",
        "streamDesktopEventsV2",
    }
)
_SSH_PROFILE_ACTION_OPERATIONS = frozenset(
    {
        "connectRemoteWorkspaceProfileV2",
        "disconnectRemoteWorkspaceProfileV2",
        "reviewRemoteWorkspaceHostKeyV2",
    }
)

_ModelT = TypeVar("_ModelT", bound=BaseModel)
_LocalOperationKindV2 = Literal[
    "ssh_catalog_rescan",
    "profile_connect",
    "profile_disconnect",
    "host_key_review",
    "project_activate",
    "task_cancel",
    "task_retry",
    "transition_retry",
    "transition_replace",
    "transition_abandon",
    "service_restart",
    "diagnostic",
]


class DesktopReleaseProviderV2:
    """Strict packaged provider for the 0.1.10 Local API v2 surface."""

    def __init__(
        self,
        *,
        store: DesktopProviderStoreV2,
        catalog: _CatalogProviderV2,
        lifecycle: _RemoteLifecycleV2,
        core_connector: _CoreProfileConnectorV2,
        bridge: _CoreBridgeV2,
        bridge_store: _CoreBridgeStoreLookupV2 | None,
        workspace_import_store: WorkspaceImportStore | None,
        event_broker: DesktopEventBrokerV2,
        build_version: str,
        source_commit: str,
        build_channel: Literal["release", "development", "test"],
        instance_id: str,
        clock: Callable[[], datetime] | None = None,
        own_resources: bool = True,
    ) -> None:
        if type(store) is not DesktopProviderStoreV2:
            raise TypeError("release v2 provider requires the exact v2 store")
        for label, value, method in (
            ("catalog", catalog, "list_catalog"),
            ("lifecycle", lifecycle, "connect"),
            ("Core connector", core_connector, "connect_profile"),
            ("Core bridge", bridge, "close"),
        ):
            if not callable(getattr(value, method, None)):
                raise TypeError(f"release v2 provider {label} is invalid")
        if type(event_broker) is not DesktopEventBrokerV2:
            raise TypeError("release v2 provider requires the exact event broker")
        if bridge_store is not None and not callable(
            getattr(bridge_store, "load_mapping_by_core_project_id", None)
        ):
            raise TypeError("release v2 Core bridge store is invalid")
        if workspace_import_store is not None and type(workspace_import_store) is not WorkspaceImportStore:
            raise TypeError("release v2 workspace import store is invalid")
        if build_version != "0.1.10":
            raise ValueError("release v2 provider requires version 0.1.10")
        if (
            type(source_commit) is not str
            or not 7 <= len(source_commit) <= 40
            or any(character not in "0123456789abcdef" for character in source_commit)
        ):
            raise ValueError("release v2 source commit is invalid")
        if build_channel not in {"release", "development", "test"}:
            raise ValueError("release v2 build channel is invalid")
        if type(instance_id) is not str or not 1 <= len(instance_id) <= 128:
            raise ValueError("release v2 instance identity is invalid")
        if type(own_resources) is not bool:
            raise TypeError("release v2 resource ownership must be boolean")
        self._store = store
        self._catalog = catalog
        self._lifecycle = lifecycle
        self._core_connector = core_connector
        self._bridge = bridge
        self._bridge_store = bridge_store
        self._workspace_import_store = workspace_import_store
        self._event_broker = event_broker
        self._build_version = build_version
        self._source_commit = source_commit
        self._build_channel = build_channel
        self._instance_id = instance_id
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._own_resources = own_resources
        self._guard = threading.RLock()
        self._ssh_profile_transition = threading.Lock()
        self._closed = False
        set_prompt_observer = getattr(lifecycle, "set_prompt_observer", None)
        if callable(set_prompt_observer):
            set_prompt_observer(self._observe_ssh_prompt)

    def invoke(self, operation_id: str, arguments: Mapping[str, object]) -> object:
        if operation_id not in _OPERATIONS:
            raise _provider_error(
                "desktop_operation_unknown",
                "The requested Desktop operation is not part of Local API v2.",
                status=404,
                action="none",
            )
        handler = {
            "getDesktopContractVersionV2": self._version,
            "getDesktopHealthV2": self._health,
            "getDesktopStateV2": self._state,
            "listConfiguredSshHostsV2": self._list_hosts,
            "rescanConfiguredSshHostsV2": self._rescan_hosts,
            "listRemoteWorkspaceProfilesV2": self._list_profiles,
            "createSystemOpenSshProfileV2": self._create_profile,
            "getRemoteWorkspaceProfileV2": self._get_profile,
            "renameRemoteWorkspaceProfileV2": self._rename_profile,
            "rebindLegacyProfileToSystemOpenSshV2": self._rebind_profile,
            "connectRemoteWorkspaceProfileV2": self._connect_profile,
            "disconnectRemoteWorkspaceProfileV2": self._disconnect_profile,
            "reviewRemoteWorkspaceHostKeyV2": self._review_host_key,
            "listDesktopProjectsV2": self._list_projects,
            "createDesktopProjectV2": self._create_project,
            "getDesktopProjectV2": self._get_project,
            "updateDesktopProjectV2": self._update_project,
            "activateDesktopProjectV2": self._activate_project,
            "getDesktopProjectCapabilitiesV2": self._project_capabilities,
            "validateDesktopProjectV2": self._validate_project,
            "listDesktopTasksV2": self._list_tasks,
            "submitDesktopTaskV2": self._submit_task,
            "getDesktopTaskV2": self._get_task,
            "cancelDesktopTaskV2": self._cancel_task,
            "retryDesktopTaskV2": self._retry_task,
            "getDesktopTaskTimelineV2": self._task_timeline,
            "getDesktopTaskLogsV2": self._task_logs,
            "getDesktopTaskContextV2": self._task_context,
            "listDesktopTaskArtifactsV2": self._task_artifacts,
            "getDesktopProjectHeadV2": self._get_project_head,
            "getDesktopEvolutionRevisionV2": self._get_evolution_revision,
            "getDesktopRuntimeContextV2": self._get_runtime_context,
            "getDesktopTransitionV2": self._get_transition,
            "retryDesktopTransitionV2": self._retry_transition,
            "replaceDesktopTransitionV2": self._replace_transition,
            "abandonDesktopTransitionV2": self._abandon_transition,
            "getDesktopArtifactV2": self._get_artifact,
            "getDesktopArtifactContentV2": self._artifact_content,
            "getDesktopArtifactDiffV2": self._artifact_diff,
            "listDesktopServicesV2": self._list_services,
            "restartDesktopServiceV2": self._restart_service,
            "createDesktopDiagnosticV2": self._create_diagnostic,
            "getDesktopDiagnosticV2": self._get_diagnostic,
            "streamDesktopEventsV2": self._events,
        }.get(operation_id)
        if handler is None:
            raise _provider_error(
                "provider_capability_unavailable",
                "This Local API v2 operation is not available in the active release session.",
                status=503,
                retryable=False,
                action="none",
            )
        if operation_id in _SSH_PROFILE_ACTION_OPERATIONS:
            # One release process owns one system-OpenSSH master. Serialize
            # generation replacement and exact retries without blocking the
            # prompt observer, which uses the independent state guard.
            with self._ssh_profile_transition:
                return handler(arguments)
        return handler(arguments)

    def close(self) -> None:
        with self._guard:
            if self._closed:
                return
            self._closed = True
        if not self._own_resources:
            return
        failure: BaseException | None = None
        for resource in (
            self._bridge,
            self._core_connector,
            self._lifecycle,
            self._event_broker,
            self._bridge_store,
            self._store,
            self._workspace_import_store,
        ):
            close = getattr(resource, "close", None)
            if not callable(close):
                continue
            try:
                close()
            except BaseException as exc:
                if failure is None:
                    failure = exc
        if failure is not None:
            raise failure

    @property
    def workspace_import_store(self) -> WorkspaceImportStore | None:
        return self._workspace_import_store

    def discard_pending_workspace_import(
        self,
        import_ref: WorkspaceImportRefV1,
        *,
        project_id: str | None,
        lease_token: str,
    ) -> None:
        store = self._require_workspace_import_store()
        store.discard_pending(
            import_ref,
            ownership=ownership_for_native_import(import_ref, project_id=project_id),
            lease_token=lease_token,
        )

    def _version(self, arguments: Mapping[str, object]) -> local_v2.DesktopVersionV2:
        _require_no_arguments(arguments)
        features = list(V0110_RELEASE_AUTHORITY_POLICY.required_desktop_feature_flags)
        build_id = _digest(
            {
                "schema_version": "2",
                "release_version": self._build_version,
                "source_commit": self._source_commit,
                "build_channel": self._build_channel,
                "instance_id": self._instance_id,
                "features": features,
            }
        )
        return local_v2.DesktopVersionV2(
            api_name="openevo-desktop-local-api",
            preferred_major=2,
            supported_majors=[2],
            mutation_major=2,
            openapi_sha256=V0110_RELEASE_AUTHORITY_POLICY.desktop_openapi_sha256,
            event_schema_sha256=(
                V0110_RELEASE_AUTHORITY_POLICY.desktop_event_schema_sha256
            ),
            release_version=self._build_version,
            build_id=build_id,
            source_commit=self._source_commit,
            build_channel=self._build_channel,
            provider_kind="desktop_sidecar",
            feature_flags=features,
            feature_set_sha256=_digest(features),
            required_core_api_major=2,
            mutation_compatible=self._build_channel == "release",
        )

    def _health(self, arguments: Mapping[str, object]) -> local_v2.DesktopHealthV2:
        _require_no_arguments(arguments)
        return local_v2.DesktopHealthV2(status="ready", checked_at=self._timestamp())

    def _state(self, arguments: Mapping[str, object]) -> local_v2.DesktopStateV2:
        _require_no_arguments(arguments)
        profiles = list(self._store.list_profiles())
        active = next(
            (
                profile
                for profile in profiles
                if isinstance(profile, local_v2.RemoteWorkspaceProfileV2)
                and profile.connection_state == "connected"
            ),
            None,
        )
        return local_v2.DesktopStateV2(
            profiles=profiles,
            active_profile_id=None if active is None else active.profile_id,
            active_project_id=None if active is None else active.active_project_id,
            last_event_id=None,
            updated_at=self._timestamp(),
        )

    def _list_hosts(self, arguments: Mapping[str, object]) -> local_v2.SshHostCatalogV2:
        _require_no_arguments(arguments)
        return self._catalog.list_catalog()

    def _rescan_hosts(self, arguments: Mapping[str, object]) -> local_v2.SshHostCatalogV2:
        request = _argument(arguments, "request", local_v2.SshHostCatalogRescanV2)
        generation = _integer_argument(arguments, "resource_generation")
        key = _string_argument(arguments, "idempotency_key")
        result = self._catalog.rescan(
            request,
            resource_generation=generation,
            idempotency_key=key,
        )
        self._event_broker.publish(
            local_v2.HostCatalogEventPayloadV2(
                payload_kind="ssh_host_catalog_changed",
                catalog_generation=result.catalog_generation,
                host_count=len(result.hosts),
                warning_count=len(result.warnings),
            )
        )
        return result

    def _list_profiles(self, arguments: Mapping[str, object]) -> local_v2.RemoteProfilePageV2:
        limit = _integer_argument(arguments, "limit")
        after = arguments.get("after")
        if after is not None and type(after) is not str:
            raise TypeError("profile cursor has the wrong type")
        profiles = sorted(self._store.list_profiles(), key=lambda item: item.profile_id)
        start = 0
        if after is not None:
            matches = [index for index, item in enumerate(profiles) if item.profile_id == after]
            if len(matches) != 1:
                raise _provider_error(
                    "cursor_invalid",
                    "The profile cursor is invalid.",
                    status=400,
                    action="none",
                )
            start = matches[0] + 1
        page = profiles[start : start + limit]
        has_more = start + limit < len(profiles)
        return local_v2.RemoteProfilePageV2(
            items=page,
            has_more=has_more,
            next_cursor=page[-1].profile_id if has_more and page else None,
        )

    def _create_profile(self, arguments: Mapping[str, object]) -> object:
        request = _argument(arguments, "request", local_v2.SystemOpenSshProfileCreateV2)
        generation = _integer_argument(arguments, "resource_generation")
        key = _string_argument(arguments, "idempotency_key")
        catalog = self._catalog.list_catalog()
        if generation != catalog.catalog_generation:
            raise _provider_error(
                "ssh_catalog_generation_changed",
                "The configured SSH host catalog changed.",
                status=412,
                retryable=True,
                action="rescan",
            )
        return self._store.create_system_profile(
            request,
            catalog_generation=generation,
            idempotency_key=key,
        )

    def _get_profile(self, arguments: Mapping[str, object]) -> local_v2.RemoteProfileV2:
        return self._store.get_profile(_string_argument(arguments, "profile_id"))

    def _rename_profile(self, arguments: Mapping[str, object]) -> local_v2.RemoteProfileV2:
        profile_id = _string_argument(arguments, "profile_id")
        generation = _integer_argument(arguments, "resource_generation")
        current = self._store.get_profile(profile_id)
        expected = (
            current.connection_generation
            if isinstance(current, local_v2.RemoteWorkspaceProfileV2)
            else 0
        )
        if generation != expected:
            raise _generation_changed(profile_id)
        return self._store.rename_profile(
            profile_id,
            _argument(arguments, "request", local_v2.ProfileDisplayNamePatchV2),
            if_match=_string_argument(arguments, "if_match"),
            idempotency_key=_string_argument(arguments, "idempotency_key"),
        )

    def _rebind_profile(self, arguments: Mapping[str, object]) -> object:
        profile_id = _string_argument(arguments, "profile_id")
        if _integer_argument(arguments, "resource_generation") != 0:
            raise _generation_changed(profile_id)
        request = _argument(arguments, "request", local_v2.ProfileRebindV2)
        catalog = self._catalog.list_catalog()
        if request.catalog_generation != catalog.catalog_generation:
            raise _provider_error(
                "ssh_catalog_generation_changed",
                "The configured SSH host catalog changed.",
                status=412,
                retryable=True,
                action="rescan",
                affected_resource_id=profile_id,
            )
        current = self._store.get_profile(profile_id)
        if not isinstance(current, local_v2.LegacyExplicitProfileV2):
            raise _provider_error(
                "profile_rebind_invalid",
                "Only a retained Preview profile can be rebound.",
                status=409,
                action="none",
                affected_resource_id=profile_id,
            )
        return self._store.rebind_legacy_profile(
            profile_id,
            request,
            display_name=current.display_name,
            if_match=_string_argument(arguments, "if_match"),
            idempotency_key=_string_argument(arguments, "idempotency_key"),
        )

    def _connect_profile(self, arguments: Mapping[str, object]) -> local_v2.LocalOperationV2:
        profile_id = _string_argument(arguments, "profile_id")
        request = _argument(arguments, "request", local_v2.ProfileConnectionActionV2)
        generation = _integer_argument(arguments, "resource_generation")
        key = _string_argument(arguments, "idempotency_key")
        started = self._store.begin_profile_action(
            profile_id,
            request,
            action="connect",
            resource_generation=generation,
            if_match=_string_argument(arguments, "if_match"),
            idempotency_key=key,
        )
        current = self._store.get_profile(profile_id)
        if (
            not isinstance(current, local_v2.RemoteWorkspaceProfileV2)
            or current.connection_generation != started.connection_generation
        ):
            raise _generation_changed(profile_id)
        if (
            current.connection_state == "connected"
        ):
            return _completed_operation("profile_connect", key, started.updated_at)
        if current.connection_state == "host_key_review":
            raise _provider_error(
                "ssh_host_key_changed",
                "The configured server identity changed and requires review.",
                status=409,
                action="review_host_key",
                affected_resource_id=profile_id,
            )
        if current.connection_state == "failed" and current.failure is not None:
            raise _replayed_profile_failure(current.failure)
        if current.connection_state == "prompt_pending":
            raise _provider_error(
                "ssh_prompt_pending",
                "System OpenSSH is waiting for the native authentication prompt.",
                status=409,
                retryable=True,
                action="retry",
                affected_resource_id=profile_id,
            )
        self._deactivate_profile_project(
            profile_id,
            started.connection_generation - 1,
            started.active_project_id,
        )
        try:
            self._lifecycle.connect(started)
        except SystemOpenSshSessionError as exc:
            review = exc.host_key_review
            if review is not None and exc.code == "ssh_host_key_changed":
                pending = self._store.publish_profile_host_key_review(
                    profile_id,
                    connection_generation=started.connection_generation,
                    review=review,
                )
                self._publish_profile(pending)
                raise _provider_error(
                    "ssh_host_key_changed",
                    "The configured server identity changed and requires review.",
                    status=409,
                    action="review_host_key",
                    affected_resource_id=profile_id,
                ) from None
            raise self._fail_profile_connect(started, exc.code) from None
        except Exception:
            raise self._fail_profile_connect(started, "ssh_connection_failed") from None
        try:
            remote = self._core_connector.connect_profile(
                profile_id,
                started.connection_generation,
            )
            negotiated = self._exact_core_version(remote, profile_id)
            self._reactivate_saved_project(started)
        except DesktopReleaseProviderV2Error as exc:
            self._abort_profile_transport(started)
            failed = self._store.fail_profile_connection(
                profile_id,
                connection_generation=started.connection_generation,
                failure=exc.error,
            )
            self._publish_profile(failed)
            raise
        except DesktopCoreBridgeErrorV2 as exc:
            raise self._fail_profile_core_connect(started, exc) from None
        except Exception:
            raise self._fail_profile_connect(
                started,
                "core_connection_failed",
                abort_transport=True,
            ) from None
        connected = self._store.complete_profile_connection(
            profile_id,
            connection_generation=started.connection_generation,
            core_version=negotiated,
        )
        self._publish_profile(connected)
        return _completed_operation("profile_connect", key, started.updated_at)

    def _disconnect_profile(self, arguments: Mapping[str, object]) -> local_v2.LocalOperationV2:
        profile_id = _string_argument(arguments, "profile_id")
        request = _argument(arguments, "request", local_v2.ProfileConnectionActionV2)
        generation = _integer_argument(arguments, "resource_generation")
        key = _string_argument(arguments, "idempotency_key")
        started = self._store.begin_profile_action(
            profile_id,
            request,
            action="disconnect",
            resource_generation=generation,
            if_match=_string_argument(arguments, "if_match"),
            idempotency_key=key,
        )
        current = self._store.get_profile(profile_id)
        if (
            not isinstance(current, local_v2.RemoteWorkspaceProfileV2)
            or current.connection_generation != started.connection_generation
        ):
            raise _generation_changed(profile_id)
        if (
            current.connection_state == "disconnected"
        ):
            return _completed_operation("profile_disconnect", key, started.updated_at)
        self._deactivate_profile_project(
            profile_id,
            started.connection_generation - 1,
            started.active_project_id,
        )
        self._lifecycle.disconnect(profile_id, started.connection_generation)
        disconnected = self._store.complete_profile_disconnect(
            profile_id,
            connection_generation=started.connection_generation,
        )
        self._publish_profile(disconnected)
        return _completed_operation("profile_disconnect", key, started.updated_at)

    def _review_host_key(self, arguments: Mapping[str, object]) -> local_v2.LocalOperationV2:
        profile_id = _string_argument(arguments, "profile_id")
        request = _argument(arguments, "request", local_v2.HostKeyReviewRequestV2)
        generation = _integer_argument(arguments, "resource_generation")
        key = _string_argument(arguments, "idempotency_key")
        started = self._store.begin_profile_action(
            profile_id,
            request,
            action="host_key_review",
            resource_generation=generation,
            if_match=_string_argument(arguments, "if_match"),
            idempotency_key=key,
        )
        current = self._store.get_profile(profile_id)
        if (
            not isinstance(current, local_v2.RemoteWorkspaceProfileV2)
            or current.connection_generation != started.connection_generation
        ):
            raise _generation_changed(profile_id)
        if current.connection_state == "connected":
            return _completed_operation("host_key_review", key, started.updated_at)
        if current.connection_state == "disconnected" and current.trust.state == "rejected":
            return _completed_operation("host_key_review", key, started.updated_at)
        if current.connection_state == "failed" and current.failure is not None:
            raise _replayed_profile_failure(current.failure)
        if current.connection_state == "prompt_pending":
            raise _provider_error(
                "ssh_prompt_pending",
                "System OpenSSH is waiting for the native authentication prompt.",
                status=409,
                retryable=True,
                action="retry",
                affected_resource_id=profile_id,
            )
        try:
            outcome = self._lifecycle.review_host_key(started, request)
        except SystemOpenSshSessionError as exc:
            raise self._fail_profile_connect(started, exc.code) from None
        except Exception:
            raise self._fail_profile_connect(started, "ssh_host_key_review_failed") from None
        if outcome == "rejected":
            rejected = self._store.complete_profile_rejection(
                profile_id,
                connection_generation=started.connection_generation,
            )
            self._publish_profile(rejected)
            return _completed_operation("host_key_review", key, started.updated_at)
        if outcome != "connected":
            raise self._fail_profile_connect(started, "ssh_host_key_review_failed")
        try:
            remote = self._core_connector.connect_profile(
                profile_id,
                started.connection_generation,
            )
            negotiated = self._exact_core_version(remote, profile_id)
            self._reactivate_saved_project(started)
        except DesktopReleaseProviderV2Error as exc:
            self._abort_profile_transport(started)
            failed = self._store.fail_profile_connection(
                profile_id,
                connection_generation=started.connection_generation,
                failure=exc.error,
            )
            self._publish_profile(failed)
            raise
        except DesktopCoreBridgeErrorV2 as exc:
            raise self._fail_profile_core_connect(started, exc) from None
        except Exception:
            raise self._fail_profile_connect(
                started,
                "core_connection_failed",
                abort_transport=True,
            ) from None
        connected = self._store.complete_profile_connection(
            profile_id,
            connection_generation=started.connection_generation,
            core_version=negotiated,
        )
        self._publish_profile(connected)
        return _completed_operation("host_key_review", key, started.updated_at)

    def _create_project(self, arguments: Mapping[str, object]) -> core_v2.ProjectV2:
        request = _argument(arguments, "request", local_v2.ProjectCreateV2)
        generation = _integer_argument(arguments, "resource_generation")
        key = _string_argument(arguments, "idempotency_key")
        profile = self._connected_profile(
            request.profile_id,
            request.profile_connection_generation,
        )
        if generation != profile.connection_generation:
            raise _generation_changed(profile.profile_id)
        native_import_id = None
        if request.config.workspace.kind == "native_folder_snapshot":
            native_import_id = native_import_id_for_action(key)
            desktop_project_id = project_id_for_native_import(native_import_id)
        else:
            desktop_project_id = _desktop_project_id(profile.profile_id, key)
        activation = self._bridge.activate_project(
            desktop_project_id,
            request,
            idempotency_key=key,
        )
        project = getattr(activation, "project", None)
        if type(project) is not core_v2.ProjectV2:
            raise _provider_error(
                "core_authority_invalid",
                "The remote OpenEvo Daemon returned invalid project authority.",
                status=502,
                action="install_repair_daemon",
                affected_resource_id=desktop_project_id,
            )
        self._store.bind_active_project(
            profile.profile_id,
            connection_generation=profile.connection_generation,
            project_id=project.project_id,
        )
        if native_import_id is not None:
            project = self._materialize_native_workspace(
                desktop_project_id=desktop_project_id,
                profile_connection_generation=profile.connection_generation,
                project=project,
                import_id=native_import_id,
                action_id=key,
            )
        return project

    def _materialize_native_workspace(
        self,
        *,
        desktop_project_id: str,
        profile_connection_generation: int,
        project: core_v2.ProjectV2,
        import_id: str,
        action_id: str,
    ) -> core_v2.ProjectV2:
        if project.active_project_head is not None:
            self._release_completed_native_import(import_id, desktop_project_id)
            return project
        if project.state != "not_ready" or project.admission_etag is not None:
            raise _core_authority_invalid(project.project_id)
        store = self._require_workspace_import_store()
        authority = store.inspect(import_id)
        expected_ownership = ownership_for_native_import(
            authority.import_ref,
            project_id=desktop_project_id,
        )
        if authority.ownership != expected_ownership or not authority.pending:
            raise WorkspaceImportIntegrityError(
                "native workspace import is not bound to this pending project action"
            )
        import_ref = authority.import_ref
        archive = core_v2.WorkspaceArchiveDeclarationV2(
            format="openevo_deterministic_tar_v1",
            media_type="application/vnd.openevo.workspace-tar",
            content_sha256=import_ref.content_sha256,
            byte_size=import_ref.byte_size,
            entry_count=import_ref.entry_count,
            extracted_byte_size=import_ref.extracted_byte_size,
        )
        chunk_byte_size = min(
            core_v2.MAX_WORKSPACE_CHUNK_BYTES,
            import_ref.byte_size,
        )
        chunk_count = (import_ref.byte_size + chunk_byte_size - 1) // chunk_byte_size
        create = core_v2.WorkspaceUploadCreateV2(
            expected_project_head_id=None,
            expected_project_head_manifest_sha256=None,
            expected_project_config_sha256=project.project_config_sha256,
            archive=archive,
            chunk_byte_size=chunk_byte_size,
            chunk_count=chunk_count,
        )
        upload = self._bridge.create_workspace_upload(
            desktop_project_id,
            profile_connection_generation,
            create,
            if_match=project.etag,
            idempotency_key=_derived_idempotency_key(action_id, "workspace-upload"),
        )
        self._validate_workspace_upload(upload, project, create)
        if upload.state == "aborted":
            raise _provider_error(
                "workspace_upload_aborted",
                "The remote workspace upload was aborted.",
                status=409,
                action="correct_project",
                affected_resource_id=project.project_id,
            )
        if upload.state == "open":
            with store.resolve(import_ref, ownership=expected_ownership) as stream:
                stream.seek(upload.accepted_byte_size)
                while upload.next_chunk_index < upload.chunk_count:
                    chunk_index = upload.next_chunk_index
                    remaining = import_ref.byte_size - upload.accepted_byte_size
                    chunk = stream.read(min(upload.chunk_byte_size, remaining))
                    if type(chunk) is not bytes or not chunk:
                        raise WorkspaceImportIntegrityError(
                            "native workspace snapshot ended before its declared size"
                        )
                    upload = self._bridge.put_workspace_upload_chunk(
                        desktop_project_id,
                        profile_connection_generation,
                        upload.upload_id,
                        chunk_index,
                        chunk,
                        chunk_sha256=hashlib.sha256(chunk).hexdigest(),
                        if_match=upload.etag,
                        idempotency_key=_derived_idempotency_key(
                            action_id,
                            f"workspace-chunk-{chunk_index}",
                        ),
                    )
                    self._validate_workspace_upload(upload, project, create)
                    if upload.state != "open" and (
                        upload.next_chunk_index != upload.chunk_count
                    ):
                        raise _core_authority_invalid(project.project_id)
                if stream.read(1) != b"":
                    raise WorkspaceImportIntegrityError(
                        "native workspace snapshot exceeds its declared size"
                    )
        if upload.state != "finalized":
            upload = self._bridge.finalize_workspace_upload(
                desktop_project_id,
                profile_connection_generation,
                upload.upload_id,
                core_v2.WorkspaceUploadFinalizeV2(
                    expected_content_sha256=import_ref.content_sha256
                ),
                if_match=upload.etag,
                idempotency_key=_derived_idempotency_key(
                    action_id,
                    "workspace-finalize",
                ),
            )
            self._validate_workspace_upload(upload, project, create)
        if upload.state != "finalized" or upload.workspace_snapshot is None:
            raise _core_authority_invalid(project.project_id)
        refreshed = self._bridge.get_project(
            desktop_project_id,
            profile_connection_generation,
        )
        if (
            refreshed.project_id != project.project_id
            or refreshed.config != project.config
            or refreshed.project_config_sha256 != project.project_config_sha256
            or refreshed.active_project_head is None
            or refreshed.admission_etag is None
            or refreshed.state != "ready"
            or refreshed.active_project_head.workspace_snapshot != upload.workspace_snapshot
        ):
            raise _core_authority_invalid(project.project_id)
        store.adopt_pending(import_ref, ownership=expected_ownership)
        store.release(import_ref, ownership=expected_ownership)
        return refreshed

    @staticmethod
    def _validate_workspace_upload(
        upload: core_v2.WorkspaceUploadSessionV2,
        project: core_v2.ProjectV2,
        request: core_v2.WorkspaceUploadCreateV2,
    ) -> None:
        if type(upload) is not core_v2.WorkspaceUploadSessionV2 or (
            upload.project_id != project.project_id
            or upload.expected_project_head_id is not None
            or upload.expected_project_head_manifest_sha256 is not None
            or upload.expected_project_config_sha256
            != project.project_config_sha256
            or upload.archive != request.archive
            or upload.chunk_byte_size != request.chunk_byte_size
            or upload.chunk_count != request.chunk_count
        ):
            raise _core_authority_invalid(project.project_id)

    def _release_completed_native_import(
        self,
        import_id: str,
        desktop_project_id: str,
    ) -> None:
        store = self._workspace_import_store
        if store is None:
            return
        try:
            authority = store.inspect(import_id)
        except WorkspaceImportNotFoundError:
            return
        expected = ownership_for_native_import(
            authority.import_ref,
            project_id=desktop_project_id,
        )
        if authority.ownership != expected:
            raise WorkspaceImportIntegrityError(
                "completed native workspace import ownership changed"
            )
        if authority.pending:
            store.adopt_pending(authority.import_ref, ownership=expected)
        store.release(authority.import_ref, ownership=expected)

    def _require_workspace_import_store(self) -> WorkspaceImportStore:
        if self._workspace_import_store is None:
            raise _provider_error(
                "workspace_import_unavailable",
                "The selected native workspace snapshot is unavailable.",
                status=409,
                action="correct_project",
            )
        return self._workspace_import_store

    def _list_projects(self, arguments: Mapping[str, object]) -> core_v2.ProjectPageV2:
        limit, after = _page_arguments(arguments)
        activation, desktop_project_id, generation, _project = self._active_authority()
        del activation
        return self._bridge.list_projects(  # type: ignore[attr-defined]
            desktop_project_id,
            generation,
            limit=limit,
            after=after,
        )

    def _get_project(self, arguments: Mapping[str, object]) -> core_v2.ProjectV2:
        project_id = _string_argument(arguments, "project_id")
        activation, desktop_project_id, generation, _project = self._active_authority(
            project_id
        )
        del activation
        return self._bridge.get_project(
            desktop_project_id,
            generation,
        )

    def _update_project(self, arguments: Mapping[str, object]) -> core_v2.ProjectV2:
        project_id = _string_argument(arguments, "project_id")
        request = _argument(arguments, "request", local_v2.ProjectPatchV2)
        generation = _integer_argument(arguments, "resource_generation")
        if_match = _string_argument(arguments, "if_match")
        key = _string_argument(arguments, "idempotency_key")
        _activation, desktop_project_id, profile_generation, project = (
            self._active_authority(project_id)
        )
        self._require_project_cas(project, generation, if_match)
        update = core_v2.ProjectUpdateV2(
            expected_project_head_id=request.expected_project_head_id,
            expected_project_head_manifest_sha256=(
                request.expected_project_head_manifest_sha256
            ),
            expected_project_config_sha256=request.expected_project_config_sha256,
            display_name=request.display_name,
            config=request.config,
        )
        return self._bridge.update_project(  # type: ignore[attr-defined]
            desktop_project_id,
            profile_generation,
            update,
            if_match=if_match,
            idempotency_key=key,
        )

    def _activate_project(self, arguments: Mapping[str, object]) -> local_v2.LocalOperationV2:
        project_id = _string_argument(arguments, "project_id")
        request = _argument(arguments, "request", local_v2.ProjectActionV2)
        generation = _integer_argument(arguments, "resource_generation")
        if_match = _string_argument(arguments, "if_match")
        key = _string_argument(arguments, "idempotency_key")
        _activation, _desktop_id, _profile_generation, project = self._active_authority(
            project_id
        )
        self._require_project_cas(project, generation, if_match)
        head = project.active_project_head
        if head is None or (
            request.expected_project_head_id != head.project_head_id
            or request.expected_project_head_manifest_sha256 != head.manifest_sha256
        ):
            raise _resource_changed("project", project_id, action="correct_project")
        return _completed_operation("project_activate", key, project.updated_at)

    def _project_capabilities(
        self,
        arguments: Mapping[str, object],
    ) -> local_v2.ProjectCapabilityProjectionV2:
        project_id = _string_argument(arguments, "project_id")
        _activation, desktop_id, profile_generation, project = self._active_authority(
            project_id
        )
        mode = project.config.execution.mode
        capabilities = self._bridge.capabilities(  # type: ignore[attr-defined]
            desktop_id,
            profile_generation,
            mode,
        )
        return local_v2.ProjectCapabilityProjectionV2(
            project_id=project.project_id,
            execution_mode=mode,
            registry_sha256=capabilities.registry_digest,
            capabilities_sha256=local_v2.evolution_capabilities_sha256_for(capabilities),
            capabilities=capabilities,
            fetched_at=self._timestamp(),
        )

    def _validate_project(
        self,
        arguments: Mapping[str, object],
    ) -> local_v2.ProjectValidationV2:
        project_id = _string_argument(arguments, "project_id")
        request = _argument(arguments, "request", local_v2.ProjectValidationRequestV2)
        generation = _integer_argument(arguments, "resource_generation")
        if_match = _string_argument(arguments, "if_match")
        key = _string_argument(arguments, "idempotency_key")
        _activation, desktop_id, profile_generation, project = self._active_authority(
            project_id
        )
        self._require_project_cas(project, generation, if_match)
        head = project.active_project_head
        if head is None or (
            request.expected_project_head_id != head.project_head_id
            or request.expected_project_head_manifest_sha256 != head.manifest_sha256
            or request.expected_project_config_sha256 != project.project_config_sha256
        ):
            raise _resource_changed("project", project_id, action="correct_project")
        response = self._bridge.validate_project(  # type: ignore[attr-defined]
            desktop_id,
            profile_generation,
            core_v2.ProjectValidationRequestV2(
                expected_project_head_id=request.expected_project_head_id,
                expected_project_head_manifest_sha256=(
                    request.expected_project_head_manifest_sha256
                ),
                expected_project_config_sha256=request.expected_project_config_sha256,
                expected_registry_sha256=request.capability_registry_sha256,
            ),
            idempotency_key=key,
        )
        return local_v2.ProjectValidationV2(
            project_id=response.project_id,
            valid=response.valid,
            registry_sha256=response.registry_sha256,
            checks=[
                local_v2.ValidationCheckV2(
                    check_id=check.check_id,
                    status=check.status,
                    action="none" if check.status == "passed" else "correct_project",
                )
                for check in response.checks
            ],
            validated_at=response.validated_at,
        )

    def _list_tasks(self, arguments: Mapping[str, object]) -> core_v2.TaskPageV2:
        limit, after = _page_arguments(arguments)
        requested_project = arguments.get("project_id")
        if requested_project is not None and type(requested_project) is not str:
            raise TypeError("project_id has the wrong type")
        _activation, desktop_id, generation, project = self._active_authority(
            requested_project
        )
        page = self._bridge.list_tasks(  # type: ignore[attr-defined]
            desktop_id,
            generation,
            limit=limit,
            after=after,
        )
        if any(task.project_id != project.project_id for task in page.items):
            raise _core_authority_invalid(project.project_id)
        return page

    def _submit_task(self, arguments: Mapping[str, object]) -> core_v2.TaskV2:
        request = _argument(arguments, "request", core_v2.TaskSubmitRequestV2)
        generation = _integer_argument(arguments, "resource_generation")
        key = _string_argument(arguments, "idempotency_key")
        _activation, desktop_id, profile_generation, project = self._active_authority(
            request.project_id
        )
        head = project.active_project_head
        if (
            head is None
            or generation != head.generation
            or project.admission_etag is None
            or request.expected_project_admission_etag != project.admission_etag
            or request.expected_project_head_id != head.project_head_id
            or request.expected_project_head_manifest_sha256 != head.manifest_sha256
            or request.expected_project_config_sha256 != project.project_config_sha256
        ):
            raise _resource_changed("project", project.project_id, action="wait_for_successor")
        return self._bridge.submit_task(  # type: ignore[attr-defined]
            desktop_id,
            profile_generation,
            request,
            idempotency_key=key,
        )

    def _get_task(self, arguments: Mapping[str, object]) -> core_v2.TaskV2:
        task_id = _string_argument(arguments, "task_id")
        _activation, desktop_id, generation, project = self._active_authority()
        task = self._bridge.get_task(  # type: ignore[attr-defined]
            desktop_id,
            generation,
            task_id,
        )
        if task.project_id != project.project_id:
            raise _core_authority_invalid(task_id)
        return task

    def _cancel_task(self, arguments: Mapping[str, object]) -> local_v2.LocalOperationV2:
        task_id, task, request, generation, if_match, key, desktop_id, profile_generation = (
            self._task_action_arguments(arguments)
        )
        attempt = task.attempts[-1]
        operation = self._bridge.cancel_task_attempt(  # type: ignore[attr-defined]
            desktop_id,
            profile_generation,
            task_id,
            attempt.attempt_id,
            core_v2.TaskActionRequestV2(
                task_admission_id=request.task_admission_id,
                admission_sha256=request.admission_sha256,
            ),
            if_match=if_match,
            idempotency_key=key,
        )
        del generation
        return _local_operation(operation, "task_cancel")

    def _retry_task(self, arguments: Mapping[str, object]) -> local_v2.LocalOperationV2:
        task_id, task, request, _generation, _if_match, key, desktop_id, profile_generation = (
            self._task_action_arguments(arguments)
        )
        previous = task.attempts[-1]
        attempt = self._bridge.append_task_attempt(  # type: ignore[attr-defined]
            desktop_id,
            profile_generation,
            task_id,
            core_v2.AttemptAppendRequestV2(
                task_admission_id=request.task_admission_id,
                admission_sha256=request.admission_sha256,
                expected_previous_attempt_id=previous.attempt_id,
                expected_next_ordinal=previous.ordinal + 1,
            ),
            idempotency_key=key,
        )
        return _completed_operation("task_retry", key, attempt.created_at)

    def _task_timeline(self, arguments: Mapping[str, object]) -> core_v2.TimelinePageV2:
        task_id = _string_argument(arguments, "task_id")
        limit, after = _page_arguments(arguments)
        _activation, desktop_id, generation, _project = self._active_authority()
        return self._bridge.task_timeline(  # type: ignore[attr-defined]
            desktop_id,
            generation,
            task_id,
            limit=limit,
            after=after,
        )

    def _task_logs(self, arguments: Mapping[str, object]) -> core_v2.LogPageV2:
        task_id = _string_argument(arguments, "task_id")
        limit, after = _page_arguments(arguments)
        _activation, desktop_id, generation, _project = self._active_authority()
        return self._bridge.task_logs(  # type: ignore[attr-defined]
            desktop_id,
            generation,
            task_id,
            limit=limit,
            after=after,
        )

    def _task_context(self, arguments: Mapping[str, object]) -> core_v2.TaskContextV2:
        task_id = _string_argument(arguments, "task_id")
        _activation, desktop_id, generation, project = self._active_authority()
        context = self._bridge.task_context(  # type: ignore[attr-defined]
            desktop_id,
            generation,
            task_id,
        )
        if context.project_head.project_id != project.project_id:
            raise _core_authority_invalid(task_id)
        return context

    def _task_artifacts(self, arguments: Mapping[str, object]) -> core_v2.ArtifactPageV2:
        task_id = _string_argument(arguments, "task_id")
        limit, after = _page_arguments(arguments)
        _activation, desktop_id, generation, project = self._active_authority()
        page = self._bridge.task_artifacts(  # type: ignore[attr-defined]
            desktop_id,
            generation,
            task_id,
            limit=limit,
            after=after,
        )
        if any(item.project_id != project.project_id for item in page.items):
            raise _core_authority_invalid(task_id)
        return page

    def _get_project_head(self, arguments: Mapping[str, object]) -> core_v2.ProjectHeadRefV2:
        head_id = _string_argument(arguments, "project_head_id")
        _activation, desktop_id, generation, project = self._active_authority()
        head = self._bridge.get_project_head(  # type: ignore[attr-defined]
            desktop_id,
            generation,
            head_id,
        )
        if head.project_id != project.project_id or head.project_head_id != head_id:
            raise _core_authority_invalid(head_id)
        return head

    def _get_evolution_revision(
        self,
        arguments: Mapping[str, object],
    ) -> core_v2.EvolutionRevisionRefV2:
        revision_id = _string_argument(arguments, "evolution_revision_id")
        head = self._find_project_head_component(
            revision_id,
            component="evolution_revision",
        )
        return head.evolution_revision

    def _get_runtime_context(
        self,
        arguments: Mapping[str, object],
    ) -> core_v2.RuntimeContextSnapshotRefV2:
        context_id = _string_argument(arguments, "runtime_context_snapshot_id")
        head = self._find_project_head_component(
            context_id,
            component="runtime_context",
        )
        return head.runtime_context_snapshot

    def _get_transition(self, arguments: Mapping[str, object]) -> core_v2.SuccessorTransitionV2:
        transition_id = _string_argument(arguments, "transition_id")
        _activation, desktop_id, generation, project = self._active_authority()
        transition = self._bridge.get_transition(  # type: ignore[attr-defined]
            desktop_id,
            generation,
            transition_id,
        )
        if (
            transition.transition.project_id != project.project_id
            or transition.transition.successor_transition_id != transition_id
        ):
            raise _core_authority_invalid(transition_id)
        return transition

    def _retry_transition(self, arguments: Mapping[str, object]) -> local_v2.LocalOperationV2:
        transition_id, request, key, desktop_id, generation = (
            self._transition_action_arguments(arguments)
        )
        operation = self._bridge.retry_transition(  # type: ignore[attr-defined]
            desktop_id,
            generation,
            transition_id,
            core_v2.ActionRequestV2(
                expected_project_head_id=request.expected_predecessor_project_head_id
            ),
            idempotency_key=key,
        )
        return _local_operation(operation, "transition_retry")

    def _replace_transition(self, arguments: Mapping[str, object]) -> object:
        _argument(arguments, "request", local_v2.TransitionReplaceV2)
        _string_argument(arguments, "transition_id")
        _integer_argument(arguments, "resource_generation")
        _string_argument(arguments, "if_match")
        _string_argument(arguments, "idempotency_key")
        raise _provider_error(
            "transition_replace_unavailable",
            "The active Core v2 authority does not support replacing a transition plan.",
            status=409,
            action="correct_project",
        )

    def _abandon_transition(self, arguments: Mapping[str, object]) -> local_v2.LocalOperationV2:
        transition_id, request, key, desktop_id, generation = (
            self._transition_action_arguments(arguments)
        )
        operation = self._bridge.abandon_transition(  # type: ignore[attr-defined]
            desktop_id,
            generation,
            transition_id,
            core_v2.ActionRequestV2(
                expected_project_head_id=request.expected_predecessor_project_head_id
            ),
            idempotency_key=key,
        )
        return _local_operation(operation, "transition_abandon")

    def _get_artifact(self, arguments: Mapping[str, object]) -> core_v2.ArtifactV2:
        artifact_id = _string_argument(arguments, "artifact_id")
        _activation, desktop_id, generation, project = self._active_authority()
        artifact = self._bridge.get_artifact(  # type: ignore[attr-defined]
            desktop_id,
            generation,
            artifact_id,
        )
        if artifact.project_id != project.project_id or artifact.artifact_id != artifact_id:
            raise _core_authority_invalid(artifact_id)
        return artifact

    def _artifact_content(self, arguments: Mapping[str, object]) -> core_v2.ArtifactContentV2:
        artifact_id = _string_argument(arguments, "artifact_id")
        _activation, desktop_id, generation, project = self._active_authority()
        content = self._bridge.artifact_content(  # type: ignore[attr-defined]
            desktop_id,
            generation,
            artifact_id,
        )
        if (
            content.artifact.project_id != project.project_id
            or content.artifact.artifact_id != artifact_id
        ):
            raise _core_authority_invalid(artifact_id)
        return content

    def _artifact_diff(self, arguments: Mapping[str, object]) -> local_v2.ArtifactDiffV2:
        artifact_id = _string_argument(arguments, "artifact_id")
        previous_id = arguments.get("previous_artifact_id")
        if previous_id is not None and type(previous_id) is not str:
            raise TypeError("previous artifact identity has the wrong type")
        current = self._get_artifact({"artifact_id": artifact_id})
        previous = (
            None
            if previous_id is None
            else self._get_artifact({"artifact_id": previous_id})
        )
        comparable = previous is not None and previous.artifact_type == current.artifact_type
        return local_v2.ArtifactDiffV2(
            artifact_id=current.artifact_id,
            previous_artifact_id=None if previous is None else previous.artifact_id,
            current_manifest_sha256=current.manifest_sha256,
            previous_manifest_sha256=(
                None if previous is None else previous.manifest_sha256
            ),
            status=(
                "unavailable"
                if previous is None
                else "available" if comparable else "not_comparable"
            ),
        )

    def _list_services(self, arguments: Mapping[str, object]) -> core_v2.ServicePageV2:
        limit, after = _page_arguments(arguments)
        _activation, desktop_id, generation, _project = self._active_authority()
        return self._bridge.list_services(  # type: ignore[attr-defined]
            desktop_id,
            generation,
            limit=limit,
            after=after,
        )

    def _restart_service(self, arguments: Mapping[str, object]) -> local_v2.LocalOperationV2:
        service_id = _string_argument(arguments, "service_id")
        request = _argument(arguments, "request", local_v2.ServiceRestartV2)
        resource_generation = _integer_argument(arguments, "resource_generation")
        if_match = _string_argument(arguments, "if_match")
        key = _string_argument(arguments, "idempotency_key")
        _activation, desktop_id, generation, project = self._active_authority()
        if request.expected_service_id != service_id or resource_generation != generation:
            raise _resource_changed("service", service_id, action="retry")
        service = self._bridge.get_service(  # type: ignore[attr-defined]
            desktop_id,
            generation,
            service_id,
        )
        if service.service_id != service_id or service.etag != if_match:
            raise _resource_changed("service", service_id, action="retry")
        operation = self._bridge.restart_service(  # type: ignore[attr-defined]
            desktop_id,
            generation,
            service_id,
            core_v2.ActionRequestV2(
                expected_project_head_id=(
                    project.active_project_head.project_head_id
                    if project.active_project_head is not None
                    else _raise_not_ready(project.project_id)
                )
            ),
            if_match=if_match,
            idempotency_key=key,
        )
        return _local_operation(operation, "service_restart")

    def _create_diagnostic(self, arguments: Mapping[str, object]) -> core_v2.DiagnosticV2:
        request = _argument(arguments, "request", local_v2.DiagnosticRequestV2)
        resource_generation = _integer_argument(arguments, "resource_generation")
        key = _string_argument(arguments, "idempotency_key")
        _activation, desktop_id, generation, project = self._active_authority()
        if (
            request.profile_id != getattr(_activation, "profile_id")
            or request.profile_connection_generation != generation
            or resource_generation != generation
            or (
                request.scope != "system"
                and request.resource_id is None
            )
            or (
                request.scope == "project"
                and request.resource_id != project.project_id
            )
        ):
            raise _resource_changed("profile", request.profile_id, action="reconnect")
        diagnostic = self._bridge.create_diagnostic(  # type: ignore[attr-defined]
            desktop_id,
            generation,
            core_v2.DiagnosticRequestV2(
                scope=request.scope,
                resource_id=request.resource_id,
            ),
            idempotency_key=key,
        )
        self._event_broker.publish(
            local_v2.DiagnosticEventPayloadV2(
                payload_kind="diagnostic_changed",
                diagnostic_id=diagnostic.diagnostic_id,
                status=diagnostic.status,
            )
        )
        return diagnostic

    def _get_diagnostic(self, arguments: Mapping[str, object]) -> core_v2.DiagnosticV2:
        diagnostic_id = _string_argument(arguments, "diagnostic_id")
        _activation, desktop_id, generation, _project = self._active_authority()
        diagnostic = self._bridge.get_diagnostic(  # type: ignore[attr-defined]
            desktop_id,
            generation,
            diagnostic_id,
        )
        if diagnostic.diagnostic_id != diagnostic_id:
            raise _core_authority_invalid(diagnostic_id)
        return diagnostic

    def _task_action_arguments(
        self,
        arguments: Mapping[str, object],
    ) -> tuple[
        str,
        core_v2.TaskV2,
        local_v2.TaskActionV2,
        int,
        str,
        str,
        str,
        int,
    ]:
        task_id = _string_argument(arguments, "task_id")
        request = _argument(arguments, "request", local_v2.TaskActionV2)
        resource_generation = _integer_argument(arguments, "resource_generation")
        if_match = _string_argument(arguments, "if_match")
        key = _string_argument(arguments, "idempotency_key")
        _activation, desktop_id, profile_generation, project = self._active_authority()
        task = self._bridge.get_task(  # type: ignore[attr-defined]
            desktop_id,
            profile_generation,
            task_id,
        )
        admission = task.admission
        if (
            task.project_id != project.project_id
            or task.etag != if_match
            or not task.attempts
            or resource_generation != task.attempts[-1].ordinal
            or request.task_admission_id != admission.task_admission_id
            or request.admission_sha256 != admission.admission_sha256
            or request.predecessor_project_head_id
            != admission.predecessor_project_head.project_head_id
        ):
            raise _resource_changed("task", task_id, action="retry")
        return (
            task_id,
            task,
            request,
            resource_generation,
            if_match,
            key,
            desktop_id,
            profile_generation,
        )

    def _transition_action_arguments(
        self,
        arguments: Mapping[str, object],
    ) -> tuple[str, local_v2.TransitionActionV2, str, str, int]:
        transition_id = _string_argument(arguments, "transition_id")
        request = _argument(arguments, "request", local_v2.TransitionActionV2)
        resource_generation = _integer_argument(arguments, "resource_generation")
        if_match = _string_argument(arguments, "if_match")
        key = _string_argument(arguments, "idempotency_key")
        _activation, desktop_id, generation, project = self._active_authority()
        transition = self._bridge.get_transition(  # type: ignore[attr-defined]
            desktop_id,
            generation,
            transition_id,
        )
        ref = transition.transition
        if (
            ref.project_id != project.project_id
            or ref.successor_transition_id != transition_id
            or resource_generation != ref.expected_successor_generation
            or if_match != project.etag
            or request.expected_predecessor_project_head_id
            != ref.predecessor_project_head.project_head_id
            or request.plan_sha256 != ref.plan_sha256
        ):
            raise _resource_changed("transition", transition_id, action="retry")
        return transition_id, request, key, desktop_id, generation

    def _find_project_head_component(
        self,
        resource_id: str,
        *,
        component: Literal["evolution_revision", "runtime_context"],
    ) -> core_v2.ProjectHeadRefV2:
        _activation, desktop_id, generation, _project = self._active_authority()
        after: str | None = None
        for _page in range(3):
            page = self._bridge.list_project_heads(  # type: ignore[attr-defined]
                desktop_id,
                generation,
                limit=100,
                after=after,
            )
            for head in page.items:
                candidate = (
                    head.evolution_revision.evolution_revision_id
                    if component == "evolution_revision"
                    else head.runtime_context_snapshot.runtime_context_snapshot_id
                )
                if candidate == resource_id:
                    return head
            if not page.has_more:
                break
            after = page.next_cursor
            if after is None:
                raise _core_authority_invalid(resource_id)
        raise _provider_error(
            "resource_not_found",
            "The requested remote Core authority was not found.",
            status=404,
            action="none",
            affected_resource_id=resource_id,
        )

    @staticmethod
    def _require_project_cas(
        project: core_v2.ProjectV2,
        resource_generation: int,
        if_match: str,
    ) -> None:
        expected_generation = (
            0 if project.active_project_head is None else project.active_project_head.generation
        )
        if resource_generation != expected_generation or if_match != project.etag:
            raise _resource_changed("project", project.project_id, action="retry")

    def _events(self, arguments: Mapping[str, object]) -> StreamingResponse:
        last_event_id = arguments.get("last_event_id")
        if last_event_id is not None and type(last_event_id) is not str:
            raise TypeError("Desktop event cursor has the wrong type")
        subscription = self._event_broker.subscribe(last_event_id)
        return StreamingResponse(subscription, media_type="text/event-stream")

    def _publish_profile(self, profile: local_v2.RemoteWorkspaceProfileV2) -> None:
        self._event_broker.publish(
            local_v2.ProfileEventPayloadV2(
                payload_kind="profile_connection_changed",
                profile_id=profile.profile_id,
                connection_generation=profile.connection_generation,
                connection_state=profile.connection_state,
                failure=profile.failure,
            )
        )

    def _observe_ssh_prompt(
        self,
        profile_id: str,
        observation: AskpassPromptObservation,
    ) -> None:
        if type(observation) is not AskpassPromptObservation:
            return
        with self._guard:
            if self._closed:
                return
        updated = self._store.observe_profile_prompt(
            profile_id,
            connection_generation=observation.connection_generation,
            kind=observation.kind,
            state=observation.state,
        )
        if updated is not None:
            self._publish_profile(updated)

    def _exact_core_version(
        self,
        remote: core_v2.VersionResponseV2,
        profile_id: str,
    ) -> core_v2.VersionResponseV2:
        try:
            negotiated = negotiate_core_v2_mutation(remote.model_dump(mode="json"))
        except (AttributeError, ReleaseAuthorityNegotiationError, TypeError, ValueError):
            raise _provider_error(
                "core_release_incompatible",
                "The remote OpenEvo Daemon does not match this Desktop release.",
                status=409,
                action="install_repair_daemon",
                affected_resource_id=profile_id,
            ) from None
        if (
            negotiated.release_version != self._build_version
            or negotiated.source_commit != self._source_commit
        ):
            raise _provider_error(
                "core_release_incompatible",
                "The remote OpenEvo Daemon does not match this Desktop release.",
                status=409,
                action="install_repair_daemon",
                affected_resource_id=profile_id,
            )
        return negotiated

    def _reactivate_saved_project(
        self,
        profile: local_v2.RemoteWorkspaceProfileV2,
    ) -> None:
        core_project_id = profile.active_project_id
        if core_project_id is None:
            return
        persistence = self._bridge_store
        if persistence is None:
            raise _provider_error(
                "core_project_mapping_unavailable",
                "Desktop could not restore the saved project tunnel.",
                status=409,
                action="reconnect",
                affected_resource_id=core_project_id,
            )
        mapping = persistence.load_mapping_by_core_project_id(core_project_id)
        if (
            type(mapping) is not CoreProjectMappingV2
            or mapping.profile_id != profile.profile_id
            or mapping.core_project_id != core_project_id
            or mapping.core_project.project_id != core_project_id
        ):
            raise _provider_error(
                "core_project_mapping_unavailable",
                "Desktop could not restore the saved project tunnel.",
                status=409,
                action="reconnect",
                affected_resource_id=core_project_id,
            )
        request = local_v2.ProjectCreateV2(
            profile_id=profile.profile_id,
            profile_connection_generation=profile.connection_generation,
            display_name=mapping.core_project.display_name,
            config=mapping.core_project.config,
        )
        activation = self._bridge.activate_project(
            mapping.desktop_project_id,
            request,
            idempotency_key=_derived_idempotency_key(
                f"{profile.profile_id}-{profile.connection_generation}",
                "reconnect-project",
            ),
        )
        project = getattr(activation, "project", None)
        if type(project) is not core_v2.ProjectV2 or project.project_id != core_project_id:
            raise _core_authority_invalid(core_project_id)

    def _fail_profile_connect(
        self,
        profile: local_v2.RemoteWorkspaceProfileV2,
        failure_code: str,
        *,
        abort_transport: bool = False,
    ) -> DesktopReleaseProviderV2Error:
        known: dict[str, tuple[str, str, bool, local_v2.DesktopActionV2]] = {
            "ssh_prompt_cancelled": (
                "ssh_prompt_cancelled",
                "The system SSH prompt was cancelled.",
                True,
                "retry",
            ),
            "ssh_host_key_rejected": (
                "ssh_host_key_rejected",
                "The first-use server identity was not approved.",
                True,
                "retry",
            ),
            "core_connection_failed": (
                "core_connection_failed",
                "Desktop could not negotiate the OpenEvo Daemon.",
                True,
                "install_repair_daemon",
            ),
        }
        code, summary, retryable, action = known.get(
            failure_code,
            (
                "ssh_connection_failed",
                "System OpenSSH could not establish the remote workspace connection.",
                True,
                "retry",
            ),
        )
        error = local_v2.DesktopErrorV2(
            code=code,
            summary=summary,
            retryable=retryable,
            action=action,
            affected_resource_id=profile.profile_id,
        )
        if abort_transport:
            self._abort_profile_transport(profile)
        failed = self._store.fail_profile_connection(
            profile.profile_id,
            connection_generation=profile.connection_generation,
            failure=error,
        )
        self._publish_profile(failed)
        return DesktopReleaseProviderV2Error(503, error)

    def _fail_profile_core_connect(
        self,
        profile: local_v2.RemoteWorkspaceProfileV2,
        failure: DesktopCoreBridgeErrorV2,
    ) -> DesktopReleaseProviderV2Error:
        error = failure.error
        if error.affected_resource_id not in {None, profile.profile_id}:
            return self._fail_profile_connect(
                profile,
                "core_connection_failed",
                abort_transport=True,
            )
        if error.affected_resource_id is None:
            error = error.model_copy(
                update={"affected_resource_id": profile.profile_id}
            )
        self._abort_profile_transport(profile)
        failed = self._store.fail_profile_connection(
            profile.profile_id,
            connection_generation=profile.connection_generation,
            failure=error,
        )
        self._publish_profile(failed)
        return DesktopReleaseProviderV2Error(failure.status_code, error)

    def _abort_profile_transport(
        self,
        profile: local_v2.RemoteWorkspaceProfileV2,
    ) -> None:
        try:
            self._lifecycle.disconnect(
                profile.profile_id,
                profile.connection_generation + 1,
            )
        except Exception:
            pass

    def _deactivate_profile_project(
        self,
        profile_id: str,
        profile_connection_generation: int,
        active_project_id: str | None,
    ) -> None:
        del profile_id, profile_connection_generation, active_project_id
        activation = self._bridge.active_activation
        if activation is None:
            return
        desktop_project_id = getattr(activation, "desktop_project_id", None)
        generation = getattr(activation, "profile_connection_generation", None)
        if type(desktop_project_id) is not str or type(generation) is not int:
            raise _provider_error(
                "core_authority_invalid",
                "The active project tunnel has invalid authority.",
                status=502,
                action="install_repair_daemon",
            )
        self._bridge.deactivate_project(desktop_project_id, generation)

    def _connected_profile(
        self,
        profile_id: str,
        connection_generation: int,
    ) -> local_v2.RemoteWorkspaceProfileV2:
        profile = self._store.get_profile(profile_id)
        if (
            not isinstance(profile, local_v2.RemoteWorkspaceProfileV2)
            or profile.connection_state != "connected"
            or profile.connection_generation != connection_generation
        ):
            raise _generation_changed(profile_id)
        return profile

    def _active_authority(
        self,
        project_id: str | None = None,
    ) -> tuple[object, str, int, core_v2.ProjectV2]:
        activation = self._bridge.active_activation
        project = None if activation is None else getattr(activation, "project", None)
        if (
            activation is None
            or type(project) is not core_v2.ProjectV2
            or (project_id is not None and project.project_id != project_id)
        ):
            raise _provider_error(
                "active_project_mismatch",
                "The requested resource does not belong to the active project tunnel.",
                status=409,
                action="reconnect",
                affected_resource_id=project_id,
            )
        profile_id = getattr(activation, "profile_id", None)
        generation = getattr(activation, "profile_connection_generation", None)
        if type(profile_id) is not str or type(generation) is not int:
            raise _provider_error(
                "core_authority_invalid",
                "The active project tunnel has invalid authority.",
                status=502,
                action="install_repair_daemon",
                affected_resource_id=project_id,
            )
        self._connected_profile(profile_id, generation)
        desktop_project_id = getattr(activation, "desktop_project_id", None)
        if type(desktop_project_id) is not str:
            raise _core_authority_invalid(project.project_id)
        return activation, desktop_project_id, generation, project

    def _active_project(self, project_id: str) -> object:
        return self._active_authority(project_id)[0]

    def _timestamp(self) -> str:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("release v2 clock must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _completed_operation(
    kind: _LocalOperationKindV2,
    idempotency_key: str,
    timestamp: str,
) -> local_v2.LocalOperationV2:
    operation_id = "local-" + hashlib.sha256(
        b"openevo-desktop-local-operation-v2\0"
        + str(kind).encode("ascii")
        + b"\0"
        + idempotency_key.encode("utf-8")
    ).hexdigest()[:48]
    return local_v2.LocalOperationV2(
        operation_id=operation_id,
        kind=kind,
        status="succeeded",
        failure=None,
        created_at=timestamp,
        updated_at=timestamp,
    )


def _provider_error(
    code: str,
    summary: str,
    *,
    status: int,
    retryable: bool = False,
    action: local_v2.DesktopActionV2,
    affected_resource_id: str | None = None,
) -> DesktopReleaseProviderV2Error:
    return DesktopReleaseProviderV2Error(
        status,
        local_v2.DesktopErrorV2(
            code=code,
            summary=summary,
            retryable=retryable,
            action=action,
            affected_resource_id=affected_resource_id,
        ),
    )


def _generation_changed(resource_id: str) -> DesktopReleaseProviderV2Error:
    return _provider_error(
        "profile_generation_changed",
        "The remote-workspace profile generation changed.",
        status=412,
        retryable=True,
        action="reconnect",
        affected_resource_id=resource_id,
    )


def _replayed_profile_failure(
    error: local_v2.DesktopErrorV2,
) -> DesktopReleaseProviderV2Error:
    status = {
        "core_release_incompatible": 409,
        "core_project_mapping_unavailable": 409,
        "core_authority_invalid": 502,
    }.get(error.code, 503)
    return DesktopReleaseProviderV2Error(status, error)


def _resource_changed(
    kind: str,
    resource_id: str,
    *,
    action: local_v2.DesktopActionV2,
) -> DesktopReleaseProviderV2Error:
    return _provider_error(
        f"{kind}_generation_changed",
        f"The {kind} authority changed before this action.",
        status=412,
        retryable=True,
        action=action,
        affected_resource_id=resource_id,
    )


def _core_authority_invalid(resource_id: str) -> DesktopReleaseProviderV2Error:
    return _provider_error(
        "core_authority_invalid",
        "The remote OpenEvo Daemon returned inconsistent v2 authority.",
        status=502,
        action="install_repair_daemon",
        affected_resource_id=resource_id,
    )


def _raise_not_ready(project_id: str) -> str:
    raise _provider_error(
        "project_not_ready",
        "The project has no active Project Head.",
        status=409,
        retryable=True,
        action="wait_for_successor",
        affected_resource_id=project_id,
    )


def _local_operation(
    operation: core_v2.OperationV2,
    kind: _LocalOperationKindV2,
) -> local_v2.LocalOperationV2:
    if type(operation) is not core_v2.OperationV2:
        raise _core_authority_invalid("core-operation")
    failure = None
    if operation.error is not None:
        repair_actions: dict[str, local_v2.DesktopActionV2] = {
            "retry": "retry",
            "repair": "install_repair_daemon",
            "reconfigure": "correct_project",
            "user_action_required": "administrator_action",
            "unsupported": "none",
        }
        failure = local_v2.DesktopErrorV2(
            code=operation.error.code,
            summary=operation.error.message,
            retryable=operation.error.retryable,
            action=repair_actions[operation.error.repair_action],
            affected_resource_id=None,
        )
    return local_v2.LocalOperationV2(
        operation_id=operation.operation_id,
        kind=kind,
        status=operation.status,
        failure=failure,
        created_at=operation.created_at,
        updated_at=operation.updated_at,
    )


def _desktop_project_id(profile_id: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(
        b"openevo-desktop-project-v2\0"
        + profile_id.encode("ascii")
        + b"\0"
        + idempotency_key.encode("utf-8")
    ).hexdigest()
    return f"desktop-project-{digest[:48]}"


def _derived_idempotency_key(parent: str, operation: str) -> str:
    digest = hashlib.sha256(
        b"openevo-desktop-derived-action-v2\0"
        + parent.encode("utf-8")
        + b"\0"
        + operation.encode("ascii")
    ).hexdigest()
    return f"desktop-v2-{digest}"


def _argument(
    arguments: Mapping[str, object],
    name: str,
    expected: type[_ModelT],
) -> _ModelT:
    value = arguments.get(name)
    if type(value) is not expected:
        raise TypeError(f"{name} has the wrong exact v2 type")
    return value


def _string_argument(arguments: Mapping[str, object], name: str) -> str:
    value = arguments.get(name)
    if type(value) is not str:
        raise TypeError(f"{name} has the wrong type")
    return value


def _integer_argument(arguments: Mapping[str, object], name: str) -> int:
    value = arguments.get(name)
    if type(value) is not int:
        raise TypeError(f"{name} has the wrong type")
    return value


def _page_arguments(arguments: Mapping[str, object]) -> tuple[int, str | None]:
    limit = _integer_argument(arguments, "limit")
    after = arguments.get("after")
    if after is not None and type(after) is not str:
        raise TypeError("page cursor has the wrong type")
    return limit, after


def _require_no_arguments(arguments: Mapping[str, object]) -> None:
    if arguments:
        raise TypeError("operation does not accept arguments")


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


__all__ = ["DesktopReleaseProviderV2", "DesktopReleaseProviderV2Error"]
