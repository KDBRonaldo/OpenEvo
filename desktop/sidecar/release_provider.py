from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import hashlib
import hmac
import logging
import re
from typing import NoReturn, cast

from fastapi.responses import JSONResponse, Response

from desktop.sidecar.contracts.v1.canonical import DESKTOP_OPENAPI_SHA256
from desktop.sidecar.contracts.v1.models import (
    ActiveProjectStateV1,
    ContractNegotiationV1,
    CoreConnectionStateV1,
    DesktopStateV1,
    HealthV1,
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
from desktop.sidecar.provider_store import DesktopProviderStore
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
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if re.fullmatch(r"[0-9a-f]{32}", instance_id) is None:
            raise ValueError("native instance id must be 32 lowercase hex characters")
        if type(readiness_key) is not bytes or len(readiness_key) != 32:
            raise ValueError("native readiness key must contain exactly 32 bytes")
        self._store = store
        self._workspace_import_store = workspace_import_store
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
            "listProjects": self._list_projects,
            "createProject": self._create_project,
            "getProject": self._get_project,
            "updateProject": self._update_project,
            "deleteProject": self._delete_project,
        }

    def close(self) -> None:
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
        return DesktopStateV1(
            observed_at=self._timestamp(),
            contract=ContractNegotiationV1(
                selected_major=1,
                desktop_openapi_sha256=DESKTOP_OPENAPI_SHA256,
                core_openapi_sha256=None,
                compatible=True,
            ),
            core=CoreConnectionStateV1(state="disconnected", active_tunnel=False),
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
        profile = self._store.patch_profile(
            cast(str, arguments["profile_id"]),
            cast(RemoteProfilePatchV1, arguments["request"]),
            if_match=cast(str, arguments["if_match"]),
        )
        return self._resource_response(profile)

    def _delete_profile(self, arguments: Mapping[str, object]) -> Response:
        self._store.delete_profile(
            cast(str, arguments["profile_id"]),
            if_match=cast(str, arguments["if_match"]),
        )
        return Response(status_code=204)

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

    def _timestamp(self) -> str:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("provider clock must return a timezone-aware datetime")
        return (
            now.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        )

    def _verify_project_source(
        self, source: ProjectSourceV1, *, project_id: str | None
    ) -> None:
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
