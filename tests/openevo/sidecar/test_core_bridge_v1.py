from __future__ import annotations

from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import fields, replace
import hashlib
import io
import json
import secrets
import threading
import time

import httpx
import pytest

from desktop.sidecar import core_bridge_v1 as bridge_module
from desktop.sidecar.contracts.v1 import models as local_v1
from desktop.sidecar.core_bridge_v1 import (
    CoreHostAttachmentV1,
    CoreProjectCreateOperationV1,
    CoreProjectCreateStateV1,
    CoreProjectHeadSuccessorProofV1,
    CoreProjectMappingV1,
    CoreProjectPatchOperationV1,
    CoreProjectPatchStateV1,
    CoreTunnelHandleV1,
    CoreWorkspaceUploadAbortStateV1,
    CoreWorkspaceUploadFinalizeStateV1,
    DesktopCoreBridgeErrorV1,
    DesktopCoreBridgeV1,
    map_project_create_v1,
)
from desktop.sidecar.core_client_v1 import (
    CORE_OPENAPI_SHA256,
    CoreClientErrorV1,
    CoreMutationOutcomeUnknownV1,
)
from openevo.backend.contracts.v1 import models as core_v1
from openevo.evolution.framework.profiles import execution_profile_for_release_mode


NOW = "2026-07-14T12:00:00Z"
LOCAL_PROJECT_ID = "local-project-1"
CORE_PROJECT_ID = "core-project-1"
PROFILE_ID = "profile-1"
REGISTRY_DIGEST = "4" * 64
ETAG_A = '"' + "a" * 64 + '"'
ETAG_B = '"' + "b" * 64 + '"'
ETAG_C = '"' + "c" * 64 + '"'


def _snapshot(
    snapshot_id: str, kind: core_v1.SnapshotKind, digest_char: str
) -> core_v1.ImmutableSnapshotRefV1:
    return core_v1.ImmutableSnapshotRefV1(
        id=snapshot_id,
        kind=kind,
        content_sha256=digest_char * 64,
        created_at=NOW,
    )


PROJECT_SNAPSHOT = _snapshot("project-snapshot-1", core_v1.SnapshotKind.PROJECT, "1")
READY_PROJECT_SNAPSHOT = _snapshot("project-snapshot-2", core_v1.SnapshotKind.PROJECT, "2")
TASK_SNAPSHOT = _snapshot("task-snapshot-1", core_v1.SnapshotKind.TASK, "3")
WORKSPACE_SNAPSHOT = _snapshot("workspace-snapshot-1", core_v1.SnapshotKind.WORKSPACE, "5")
REVISION = core_v1.RevisionRefV1(
    id="revision-0",
    project_id=CORE_PROJECT_ID,
    generation=0,
    manifest_sha256="6" * 64,
)


def _successor_revision(
    previous: core_v1.RevisionRefV1,
    *,
    project_snapshot: core_v1.ImmutableSnapshotRefV1,
    task_snapshot: core_v1.ImmutableSnapshotRefV1 | None,
    workspace_snapshot: core_v1.ImmutableSnapshotRefV1,
    registry_digest: str = REGISTRY_DIGEST,
    revision_id: str | None = None,
) -> core_v1.RevisionRefV1:
    generation = previous.generation + 1
    return core_v1.RevisionRefV1(
        id=revision_id or f"revision-{generation}",
        project_id=previous.project_id,
        generation=generation,
        manifest_sha256=bridge_module.revision_manifest_sha256_v1(
            project_id=previous.project_id,
            generation=generation,
            predecessor_revision=previous,
            project_snapshot=project_snapshot,
            task_snapshot=task_snapshot,
            workspace_snapshot=workspace_snapshot,
            registry_digest=registry_digest,
        ),
    )


def _active_revision_record(
    revision: core_v1.RevisionRefV1,
    *,
    predecessor: core_v1.RevisionRefV1,
    project_snapshot: core_v1.ImmutableSnapshotRefV1,
    task_snapshot: core_v1.ImmutableSnapshotRefV1 | None,
    workspace_snapshot: core_v1.ImmutableSnapshotRefV1,
    updated_at: str,
    etag: str,
    registry_digest: str = REGISTRY_DIGEST,
) -> core_v1.RevisionV1:
    return core_v1.RevisionV1(
        revision=revision,
        status=core_v1.RevisionStatus.ACTIVE,
        predecessor_revision=predecessor,
        project_snapshot=project_snapshot,
        task_snapshot=task_snapshot,
        workspace_snapshot=workspace_snapshot,
        registry_digest=registry_digest,
        transition=core_v1.RevisionTransitionV1(
            state=core_v1.RevisionTransitionState.ACTIVE,
            predecessor_revision=predecessor,
            successor_revision=revision,
            progress_completed=1,
            progress_total=1,
            message="Project revision activated.",
            updated_at=updated_at,
        ),
        created_at=updated_at,
        updated_at=updated_at,
        activated_at=updated_at,
        etag=etag,
    )


def _version() -> dict[str, object]:
    return {
        "schema_version": "1",
        "preferred_major": 1,
        "supported_majors": [1],
        "openapi_sha256": CORE_OPENAPI_SHA256,
        "build_version": "0.1.0",
        "source_commit": "1234567",
        "build_channel": "release",
        "provider_kind": "openevo_core",
        "features": ["diagnostics"],
    }


def _capabilities(
    mode: core_v1.ExecutionMode = core_v1.ExecutionMode.SELF_DEPLOYED,
    *,
    registry_digest: str = REGISTRY_DIGEST,
) -> core_v1.CapabilitiesResponseV1:
    return core_v1.CapabilitiesResponseV1(
        core_version="0.1.0",
        registry_digest=registry_digest,
        evaluated_profile=execution_profile_for_release_mode(mode),
        targets=(),
    )


def _local_project(*, imported: bool = False) -> local_v1.ProjectV1:
    archive = b"\0" * 1024
    source = {
        "kind": "native_folder_snapshot",
        "display_name": "Selected workspace",
        "import_ref": {
            "import_id": "adopted-import-1",
            "content_sha256": hashlib.sha256(archive).hexdigest(),
            "byte_size": len(archive),
            "entry_count": 0,
            "extracted_byte_size": 0,
        },
    }
    request = local_v1.ProjectCreateV1.model_validate(
        {
            "name": "Protein design",
            "profile_id": PROFILE_ID,
            "task": {"title": "Design", "objective": "Improve stability."},
            "source": source if imported else {"kind": "scratch", "display_name": "New workspace"},
            "execution": {
                "mode": "self-deployed",
                "hf_model": "openai/gpt-oss-20b",
            },
            "evolution": {"targets": {}},
        }
    )
    return local_v1.ProjectV1(
        project_id=LOCAL_PROJECT_ID,
        state="draft",
        etag=ETAG_A,
        created_at=NOW,
        updated_at=NOW,
        **request.model_dump(),
    )


def _committed_local_activation(
    project: local_v1.ProjectV1,
    activation: bridge_module.CoreActivationV1,
    *,
    etag: str = ETAG_B,
) -> local_v1.ProjectV1:
    core_project = activation.core_project
    return project.model_copy(
        update={
            "state": "active",
            "remote": local_v1.RemoteProjectStateV1(
                core_project_id=core_project.id,
                status="ready",
                active_revision=core_project.active_revision,
                registry_digest=core_project.registry_digest,
                model_preparation=core_project.model_preparation,
                observed_at=NOW,
                etag=core_project.etag,
            ),
            "etag": etag,
        }
    )


def _core_create(local_project: local_v1.ProjectV1) -> core_v1.ProjectCreateV1:
    return map_project_create_v1(local_project)


def _project(
    request: core_v1.ProjectCreateV1,
    *,
    ready: bool,
    imported_published: bool = False,
    project_snapshot: core_v1.ImmutableSnapshotRefV1 | None = None,
    task_snapshot: core_v1.ImmutableSnapshotRefV1 = TASK_SNAPSHOT,
    workspace_snapshot: core_v1.ImmutableSnapshotRefV1 = WORKSPACE_SNAPSHOT,
    etag: str | None = None,
    active_revision: core_v1.RevisionRefV1 = REVISION,
    registry_digest: str = REGISTRY_DIGEST,
    created_at: str = NOW,
    updated_at: str = NOW,
) -> core_v1.ProjectV1:
    imported = isinstance(request.workspace, core_v1.ImportedWorkspaceSpecV1)
    current_workspace_snapshot = workspace_snapshot if ready or not imported else None
    publication = None
    if imported_published:
        assert isinstance(request.workspace, core_v1.ImportedWorkspaceSpecV1)
        publication = core_v1.WorkspacePublicationV1(
            archive=request.workspace.archive,
            content_ref=core_v1.ContentRefV1(
                content_id="workspace-content-1",
                sha256=request.workspace.archive.content_sha256,
                byte_size=request.workspace.archive.byte_size,
            ),
            workspace_snapshot=workspace_snapshot,
            published_at=NOW,
        )
        current_workspace_snapshot = workspace_snapshot
    return core_v1.ProjectV1(
        id=CORE_PROJECT_ID,
        name=request.name,
        description=request.description,
        status=core_v1.ProjectStatus.READY if ready else core_v1.ProjectStatus.DRAFT,
        execution_mode=request.spec.execution_mode,
        workspace_kind=core_v1.WorkspaceSourceKind(request.workspace.kind),
        current_project_snapshot=(
            project_snapshot or (READY_PROJECT_SNAPSHOT if ready else PROJECT_SNAPSHOT)
        ),
        current_task_snapshot=task_snapshot,
        current_workspace_snapshot=current_workspace_snapshot,
        workspace_publication=publication,
        active_revision=active_revision if ready else None,
        registry_digest=registry_digest if ready or imported else None,
        model_preparation=core_v1.ModelPreparationV1(
            model_ref=request.spec.agent_model_ref,
            status=(
                core_v1.ModelPreparationStatus.READY
                if ready
                else core_v1.ModelPreparationStatus.UNRESOLVED
            ),
            updated_at=NOW,
        ),
        created_at=created_at,
        updated_at=updated_at,
        etag=etag or (ETAG_C if ready else ETAG_A),
        spec=request.spec,
        task=request.task,
        workspace=request.workspace,
    )


class FakeHostService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, float]] = []
        self.block = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def ensure_core(
        self,
        profile_id: str,
        *,
        deadline: float,
        cancel_event: threading.Event | None = None,
    ) -> CoreHostAttachmentV1:
        del cancel_event
        self.calls.append((profile_id, deadline))
        if self.block:
            self.entered.set()
            assert self.release.wait(timeout=2)
        return CoreHostAttachmentV1(
            profile_id=profile_id,
            remote_port=43117,
            bearer_token=secrets.token_urlsafe(32),
            bearer_identity="core-host-key-1",
        )


class FakeTunnelFactory:
    def __init__(self) -> None:
        self.handles: list[CoreTunnelHandleV1] = []
        self.block_open = False
        self.open_entered = threading.Event()
        self.open_release = threading.Event()

    def open_tunnel(
        self,
        *,
        profile_id: str,
        remote_port: int,
        session_id: str,
        deadline: float,
    ) -> CoreTunnelHandleV1:
        assert profile_id == PROFILE_ID
        assert remote_port == 43117
        del deadline
        if self.block_open:
            self.open_entered.set()
            assert self.open_release.wait(timeout=2)
        handle = CoreTunnelHandleV1(
            endpoint=f"http://127.0.0.1:{48000 + len(self.handles)}",
            session_id=session_id,
            close_callback=lambda: None,
        )
        self.handles.append(handle)
        return handle


class FakePersistence:
    def __init__(self) -> None:
        self.operation: CoreProjectCreateOperationV1 | None = None
        self.mapping: CoreProjectMappingV1 | None = None
        self.mapping_history: list[CoreProjectMappingV1] = []
        self.patch_operation: CoreProjectPatchOperationV1 | None = None
        self.events: list[str] = []
        self.fail_commit_once = False
        self.block_commit = False
        self.commit_entered = threading.Event()
        self.commit_release = threading.Event()

    def load_mapping(self, local_project_id: str) -> CoreProjectMappingV1 | None:
        assert local_project_id == LOCAL_PROJECT_ID
        return self.mapping

    def load_create(self, local_project_id: str) -> CoreProjectCreateOperationV1 | None:
        assert local_project_id == LOCAL_PROJECT_ID
        return self.operation

    def load_patch(self, local_project_id: str) -> CoreProjectPatchOperationV1 | None:
        assert local_project_id == LOCAL_PROJECT_ID
        return self.patch_operation

    def reserve_create(
        self, operation: CoreProjectCreateOperationV1
    ) -> CoreProjectCreateOperationV1:
        self.events.append("reserve_create")
        if self.operation is None:
            self.operation = operation
        elif (
            self.operation.state is not CoreProjectCreateStateV1.BOUND
            and self.operation.request_sha256 != operation.request_sha256
        ):
            raise AssertionError("operation request changed")
        elif self.operation.state is CoreProjectCreateStateV1.PRE_CREATE:
            self.operation = operation
        return self.operation

    def mark_create_unknown(
        self, operation: CoreProjectCreateOperationV1
    ) -> CoreProjectCreateOperationV1:
        self.events.append("mark_create_unknown")
        assert self.operation == operation
        self.operation = replace(operation, state=CoreProjectCreateStateV1.UNKNOWN)
        return self.operation

    def bind_created_project(
        self,
        operation: CoreProjectCreateOperationV1,
        core_project_id: str,
        *,
        immutable_authority: bridge_module.CoreProjectPatchImmutableAuthorityV1,
    ) -> CoreProjectCreateOperationV1:
        self.events.append("bind_created_project")
        assert self.operation == operation
        self.operation = replace(
            operation,
            state=CoreProjectCreateStateV1.BOUND,
            core_project_id=core_project_id,
            project_immutable_authority=immutable_authority,
        )
        return self.operation

    def update_create(
        self,
        operation: CoreProjectCreateOperationV1,
        *,
        expected_previous: CoreProjectCreateOperationV1,
    ) -> CoreProjectCreateOperationV1:
        self.events.append("update_create")
        assert self.operation == expected_previous
        self.operation = operation
        return operation

    def reserve_patch(self, operation: CoreProjectPatchOperationV1) -> CoreProjectPatchOperationV1:
        self.events.append("reserve_patch")
        if self.patch_operation is None:
            self.patch_operation = operation
        return self.patch_operation

    def mark_patch_unknown(
        self, operation: CoreProjectPatchOperationV1
    ) -> CoreProjectPatchOperationV1:
        self.events.append("mark_patch_unknown")
        assert self.patch_operation == operation
        self.patch_operation = replace(operation, state=CoreProjectPatchStateV1.UNKNOWN)
        return self.patch_operation

    def record_patch_applied(
        self,
        operation: CoreProjectPatchOperationV1,
        outcome: core_v1.ProjectV1,
        *,
        outcome_immutable: bridge_module.CoreProjectPatchImmutableAuthorityV1,
        outcome_mutable: bridge_module.CoreProjectPatchMutableAuthorityV1,
    ) -> CoreProjectPatchOperationV1:
        self.events.append("record_patch_applied")
        assert self.patch_operation == operation
        self.patch_operation = replace(
            operation,
            state=CoreProjectPatchStateV1.APPLIED,
            outcome=outcome,
            outcome_immutable=outcome_immutable,
            outcome_mutable=outcome_mutable,
        )
        return self.patch_operation

    def commit_mapping(
        self,
        operation: CoreProjectCreateOperationV1,
        mapping: CoreProjectMappingV1,
        *,
        expected_previous: CoreProjectMappingV1 | None,
        completed_patch: CoreProjectPatchOperationV1 | None,
        project_head_successor: CoreProjectHeadSuccessorProofV1 | None = None,
    ) -> None:
        self.events.append("commit_mapping")
        if self.block_commit:
            self.commit_entered.set()
            assert self.commit_release.wait(timeout=2)
        assert self.operation == operation
        assert self.mapping == expected_previous
        if self.fail_commit_once:
            self.fail_commit_once = False
            raise RuntimeError("injected mapping commit failure")
        if completed_patch is not None:
            assert self.patch_operation == completed_patch
            assert completed_patch.state is CoreProjectPatchStateV1.APPLIED
            self.patch_operation = None
        if project_head_successor is not None:
            if expected_previous is None or completed_patch is not None:
                assert project_head_successor.predecessor_project is not None
            else:
                assert project_head_successor.predecessor_project is None
            assert project_head_successor.project.active_revision == mapping.active_revision
            assert project_head_successor.head.active_revision == mapping.active_revision
            assert project_head_successor.revision.revision == mapping.active_revision
        self.operation = operation
        self.mapping = mapping
        self.mapping_history.append(mapping)


class FakeArchiveSource:
    def __init__(self, archive: bytes | None = None) -> None:
        self.archive = archive if archive is not None else b"\0" * 1024
        self.refs: list[local_v1.WorkspaceImportRefV1] = []

    def open_archive(self, ref: local_v1.WorkspaceImportRefV1) -> io.BytesIO:
        self.refs.append(ref)
        return ShortReadArchive(self.archive)


class ShortReadArchive(io.BytesIO):
    def read(self, size: int = -1) -> bytes:
        if size > 0:
            size = min(size, 73)
        return super().read(size)


class BlockingReadArchive(io.BytesIO):
    def __init__(self, value: bytes) -> None:
        super().__init__(value)
        self.entered = threading.Event()
        self.release = threading.Event()

    def read(self, size: int = -1) -> bytes:
        self.entered.set()
        assert self.release.wait(timeout=2)
        return super().read(size)


class BlockingArchiveSource(FakeArchiveSource):
    def __init__(self) -> None:
        super().__init__()
        self.stream = BlockingReadArchive(self.archive)

    def open_archive(self, ref: local_v1.WorkspaceImportRefV1) -> io.BytesIO:
        self.refs.append(ref)
        return self.stream


class FakeCore:
    def __init__(self, local_project: local_v1.ProjectV1) -> None:
        self.request = _core_create(local_project)
        self.created = False
        self.calls: list[httpx.Request] = []
        self.run_requests: list[core_v1.RunCreateV1] = []
        self.fail_capabilities_with_503 = False
        self.lose_create_response_once = False
        self.lose_patch_before_apply_once = False
        self.lose_patch_after_apply_once = False
        self.lose_finalize_before_apply_once = False
        self.lose_finalize_after_apply_once = False
        self.lose_abort_after_apply_once = False
        self.expire_patch_replay_once = False
        self.expire_finalize_replay_once = False
        self.expire_abort_replay_once = False
        self.upload: core_v1.WorkspaceUploadSessionV1 | None = None
        self.uploads: dict[str, core_v1.WorkspaceUploadSessionV1] = {}
        self.abort_requests: list[tuple[str, core_v1.WorkspaceUploadAbortV1, str, str]] = []
        self.abort_replays: dict[str, core_v1.WorkspaceUploadSessionV1] = {}
        self.finalize_requests: list[
            tuple[str, core_v1.WorkspaceUploadFinalizeV1, str, str, str]
        ] = []
        self.finalize_replays: dict[str, core_v1.WorkspaceUploadFinalizeResponseV1] = {}
        self.head = _head()
        self.block_method: str | None = None
        self.block_path: str | None = None
        self.block_entered = threading.Event()
        self.block_release = threading.Event()
        self.block_once = True
        self.patch_requests: list[tuple[core_v1.ProjectPatchV1, str, str]] = []
        self.patch_apply_count = 0
        self.patch_replays: dict[str, core_v1.ProjectV1] = {}
        self.patch_advances_revision_once = False
        self.upload_count = 0
        self.project_snapshot = READY_PROJECT_SNAPSHOT
        self.task_snapshot = TASK_SNAPSHOT
        self.workspace_snapshot = WORKSPACE_SNAPSHOT
        self.project_etag = ETAG_C
        self._active_revision = REVISION
        self.revision_predecessors: dict[str, core_v1.RevisionRefV1] = {}
        self.revision_records: dict[str, core_v1.RevisionV1] = {}
        self.registry_digest = REGISTRY_DIGEST
        self.project_created_at = NOW
        self.project_updated_at = NOW
        self.features = [
            "projects",
            "workspace_sync",
            "verified_capabilities",
            "transcript_capture",
            "non_parametric_evolution",
            "sse_replay",
            "diagnostics",
        ]
        self.create_created_at: str | None = None
        self.finalize_created_at: str | None = None
        self.patch_created_at: str | None = None

    @property
    def active_revision(self) -> core_v1.RevisionRefV1:
        return self._active_revision

    @active_revision.setter
    def active_revision(self, revision: core_v1.RevisionRefV1) -> None:
        previous = self._active_revision
        if (
            revision != previous
            and revision.project_id == previous.project_id
            and revision.generation == previous.generation + 1
        ):
            self.revision_predecessors[revision.id] = previous
        self._active_revision = revision

    def current_revision_record(self) -> core_v1.RevisionV1:
        predecessor = self.revision_predecessors.get(self.active_revision.id)
        transition = (
            None
            if predecessor is None
            else core_v1.RevisionTransitionV1(
                state=core_v1.RevisionTransitionState.ACTIVE,
                predecessor_revision=predecessor,
                successor_revision=self.active_revision,
                progress_completed=1,
                progress_total=1,
                message="Project revision activated.",
                updated_at=self.project_updated_at,
            )
        )
        return core_v1.RevisionV1(
            revision=self.active_revision,
            status=core_v1.RevisionStatus.ACTIVE,
            predecessor_revision=predecessor,
            project_snapshot=self.project_snapshot,
            task_snapshot=self.task_snapshot,
            workspace_snapshot=self.workspace_snapshot,
            registry_digest=self.registry_digest,
            transition=transition,
            created_at=self.project_updated_at,
            updated_at=self.project_updated_at,
            activated_at=self.project_updated_at,
            etag=self.project_etag,
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        if self.block_once and request.method == self.block_method and path == self.block_path:
            self.block_once = False
            self.block_entered.set()
            assert self.block_release.wait(timeout=2)
        if path == "/version":
            version = _version()
            version["features"] = self.features
            return httpx.Response(200, json=version)
        if path == "/v1/capabilities":
            if self.fail_capabilities_with_503:
                return httpx.Response(503, json=_core_error())
            return httpx.Response(
                200,
                json=_capabilities(registry_digest=self.registry_digest).model_dump(mode="json"),
            )
        if path == "/v1/projects" and request.method == "POST":
            assert "Idempotency-Key" in request.headers
            self.created = True
            if self.lose_create_response_once:
                self.lose_create_response_once = False
                raise httpx.ReadError("response lost", request=request)
            return httpx.Response(
                201,
                json=_project(
                    self.request,
                    ready=False,
                    created_at=self.create_created_at or self.project_created_at,
                ).model_dump(mode="json"),
            )
        if path == f"/v1/projects/{CORE_PROJECT_ID}":
            if request.method == "PATCH":
                patch = core_v1.ProjectPatchV1.model_validate_json(request.content, strict=True)
                self.patch_requests.append(
                    (
                        patch,
                        request.headers["If-Match"],
                        request.headers["Idempotency-Key"],
                    )
                )
                if self.expire_patch_replay_once:
                    self.expire_patch_replay_once = False
                    self.patch_replays.pop(request.headers["Idempotency-Key"], None)
                    return httpx.Response(412, json=_retention_conflict())
                replay = self.patch_replays.get(request.headers["Idempotency-Key"])
                if replay is not None:
                    return httpx.Response(200, json=replay.model_dump(mode="json"))
                if self.lose_patch_before_apply_once:
                    self.lose_patch_before_apply_once = False
                    raise httpx.ReadError("patch response lost", request=request)
                prior = self.request
                self.patch_apply_count += 1
                patch_digest_char = f"{(7 + self.patch_apply_count) % 16:x}"
                self.request = core_v1.ProjectCreateV1(
                    name=patch.name if patch.name is not None else prior.name,
                    description=(
                        patch.description
                        if "description" in patch.model_fields_set
                        else prior.description
                    ),
                    spec=patch.spec if patch.spec is not None else prior.spec,
                    task=patch.task if patch.task is not None else prior.task,
                    workspace=(
                        patch.workspace if patch.workspace is not None else prior.workspace
                    ),
                )
                self.project_snapshot = _snapshot(
                    f"project-snapshot-patched-{self.patch_apply_count}",
                    core_v1.SnapshotKind.PROJECT,
                    patch_digest_char,
                )
                if self.request.task != prior.task:
                    self.task_snapshot = _snapshot(
                        f"task-snapshot-patched-{self.patch_apply_count}",
                        core_v1.SnapshotKind.TASK,
                        patch_digest_char,
                    )
                if self.request.workspace != prior.workspace:
                    self.workspace_snapshot = _snapshot(
                        f"workspace-snapshot-patched-{self.patch_apply_count}",
                        core_v1.SnapshotKind.WORKSPACE,
                        patch_digest_char,
                    )
                    self.upload = None
                self.project_etag = '"' + patch_digest_char * 64 + '"'
                if self.patch_advances_revision_once:
                    self.patch_advances_revision_once = False
                    self.project_updated_at = "2026-07-14T12:43:00Z"
                    self.active_revision = _successor_revision(
                        self.active_revision,
                        project_snapshot=self.project_snapshot,
                        task_snapshot=self.task_snapshot,
                        workspace_snapshot=self.workspace_snapshot,
                    )
                    self.head = _head(
                        active_revision=self.active_revision,
                        etag=self.project_etag,
                        updated_at=self.project_updated_at,
                    )
                imported_patch = isinstance(
                    self.request.workspace, core_v1.ImportedWorkspaceSpecV1
                )
                imported_published = (
                    imported_patch
                    and self.upload is not None
                    and self.upload.status is core_v1.WorkspaceUploadStatus.FINALIZED
                )
                patched_project = _project(
                    self.request,
                    ready=not imported_patch or imported_published,
                    imported_published=imported_published,
                    project_snapshot=self.project_snapshot,
                    task_snapshot=self.task_snapshot,
                    workspace_snapshot=self.workspace_snapshot,
                    etag=self.project_etag,
                    active_revision=self.active_revision,
                    registry_digest=self.registry_digest,
                    created_at=self.patch_created_at or self.project_created_at,
                    updated_at=self.project_updated_at,
                )
                self.patch_replays[request.headers["Idempotency-Key"]] = patched_project
                if self.lose_patch_after_apply_once:
                    self.lose_patch_after_apply_once = False
                    raise httpx.ReadError("patch response lost", request=request)
                return httpx.Response(
                    200,
                    json=patched_project.model_dump(mode="json"),
                )
            imported = isinstance(self.request.workspace, core_v1.ImportedWorkspaceSpecV1)
            published = (
                self.upload is not None
                and self.upload.status is core_v1.WorkspaceUploadStatus.FINALIZED
            )
            return httpx.Response(
                200,
                json=_project(
                    self.request,
                    ready=not imported or published,
                    imported_published=published,
                    project_snapshot=self.project_snapshot,
                    task_snapshot=self.task_snapshot,
                    workspace_snapshot=self.workspace_snapshot,
                    etag=self.project_etag,
                    active_revision=self.active_revision,
                    registry_digest=self.registry_digest,
                    created_at=self.project_created_at,
                    updated_at=self.project_updated_at,
                ).model_dump(mode="json"),
            )
        if path.endswith("/workspace-uploads") and request.method == "POST":
            self.upload_count += 1
            self.upload = _upload(
                self.request,
                upload_id=f"upload-{self.upload_count}",
                project_snapshot=self.project_snapshot,
                project_etag=self.project_etag,
            )
            self.uploads[self.upload.id] = self.upload
            return httpx.Response(201, json=self.upload.model_dump(mode="json"))
        if "/workspace-uploads/" in path and request.method == "GET":
            upload_id = path.rsplit("/", 1)[-1]
            upload = self.uploads[upload_id]
            return httpx.Response(200, json=upload.model_dump(mode="json"))
        if path.endswith("/chunk") and "/workspace-uploads/" in path:
            assert self.upload is not None
            assert path.endswith(f"/workspace-uploads/{self.upload.id}/chunk")
            chunk = core_v1.WorkspaceUploadChunkV1.model_validate_json(
                request.content, strict=True
            )
            self.upload = self.upload.model_copy(
                update={
                    "accepted_offset": chunk.offset + chunk.byte_length,
                    "updated_at": "2026-07-14T12:00:01Z",
                    "etag": ETAG_C,
                }
            )
            self.uploads[self.upload.id] = self.upload
            return httpx.Response(200, json=self.upload.model_dump(mode="json"))
        if path.endswith("/finalize") and "/workspace-uploads/" in path:
            assert self.upload is not None
            assert path.endswith(f"/workspace-uploads/{self.upload.id}/finalize")
            finalize_request = core_v1.WorkspaceUploadFinalizeV1.model_validate_json(
                request.content, strict=True
            )
            finalize_key = request.headers["Idempotency-Key"]
            self.finalize_requests.append(
                (
                    self.upload.id,
                    finalize_request,
                    request.headers["If-Match"],
                    request.headers["If-Project-Match"],
                    finalize_key,
                )
            )
            if self.expire_finalize_replay_once:
                self.expire_finalize_replay_once = False
                self.finalize_replays.pop(finalize_key, None)
                return httpx.Response(412, json=_retention_conflict())
            replay = self.finalize_replays.get(finalize_key)
            if replay is not None:
                return httpx.Response(201, json=replay.model_dump(mode="json"))
            if self.lose_finalize_before_apply_once:
                self.lose_finalize_before_apply_once = False
                raise httpx.ReadError("finalize response lost", request=request)
            if self.upload_count == 1:
                self.project_snapshot = _snapshot(
                    "project-snapshot-finalized", core_v1.SnapshotKind.PROJECT, "b"
                )
                self.project_etag = '"' + "f" * 64 + '"'
            else:
                self.project_snapshot = _snapshot(
                    f"project-snapshot-finalized-{self.upload_count}",
                    core_v1.SnapshotKind.PROJECT,
                    "c",
                )
                self.project_etag = '"' + "0" * 64 + '"'
            published = _project(
                self.request,
                ready=True,
                imported_published=True,
                project_snapshot=self.project_snapshot,
                task_snapshot=self.task_snapshot,
                workspace_snapshot=self.workspace_snapshot,
                etag=self.project_etag,
                active_revision=self.active_revision,
                registry_digest=self.registry_digest,
                created_at=self.finalize_created_at or self.project_created_at,
                updated_at=self.project_updated_at,
            )
            assert published.workspace_publication is not None
            finalized_upload = self.upload.model_copy(
                update={
                    "status": core_v1.WorkspaceUploadStatus.FINALIZED,
                    "publication": published.workspace_publication,
                    "updated_at": "2026-07-14T12:00:02Z",
                    "etag": '"' + "d" * 64 + '"',
                }
            )
            self.upload = finalized_upload
            self.uploads[finalized_upload.id] = finalized_upload
            outcome = core_v1.WorkspaceUploadFinalizeResponseV1(
                project_id=CORE_PROJECT_ID,
                upload=finalized_upload,
                publication=published.workspace_publication,
                project=published,
            )
            self.finalize_replays[finalize_key] = outcome
            if self.lose_finalize_after_apply_once:
                self.lose_finalize_after_apply_once = False
                raise httpx.ReadError("finalize response lost", request=request)
            return httpx.Response(201, json=outcome.model_dump(mode="json"))
        if path.endswith("/abort") and "/workspace-uploads/" in path:
            upload_id = path.rsplit("/", 2)[-2]
            abort_request = core_v1.WorkspaceUploadAbortV1.model_validate_json(
                request.content, strict=True
            )
            self.abort_requests.append(
                (
                    upload_id,
                    abort_request,
                    request.headers["If-Match"],
                    request.headers["Idempotency-Key"],
                )
            )
            if self.expire_abort_replay_once:
                self.expire_abort_replay_once = False
                self.abort_replays.pop(request.headers["Idempotency-Key"], None)
                return httpx.Response(412, json=_retention_conflict())
            replay = self.abort_replays.get(request.headers["Idempotency-Key"])
            if replay is not None:
                return httpx.Response(200, json=replay.model_dump(mode="json"))
            upload = self.uploads[upload_id]
            assert upload.status is core_v1.WorkspaceUploadStatus.OPEN
            assert request.headers["If-Match"] == upload.etag
            aborted = upload.model_copy(
                update={
                    "status": core_v1.WorkspaceUploadStatus.ABORTED,
                    "updated_at": "2026-07-14T12:00:03Z",
                    "etag": '"' + "1" * 64 + '"',
                }
            )
            self.uploads[upload_id] = aborted
            self.abort_replays[request.headers["Idempotency-Key"]] = aborted
            if self.lose_abort_after_apply_once:
                self.lose_abort_after_apply_once = False
                raise httpx.ReadError("abort response lost", request=request)
            return httpx.Response(200, json=aborted.model_dump(mode="json"))
        if path.endswith("/revisions/head"):
            return httpx.Response(200, json=self.head.model_dump(mode="json"))
        if path.endswith("/revisions") and request.method == "GET":
            records = {
                **self.revision_records,
                self.active_revision.id: self.current_revision_record(),
            }
            page = core_v1.RevisionPageV1(
                items=sorted(
                    records.values(),
                    key=lambda item: (item.revision.generation, item.revision.id),
                    reverse=request.url.params.get("direction", "desc") == "desc",
                ),
                next_cursor=None,
                has_more=False,
            )
            return httpx.Response(200, json=page.model_dump(mode="json"))
        if "/revisions/" in path and request.method == "GET":
            revision_id = path.rsplit("/", 1)[-1]
            revision = self.revision_records.get(revision_id)
            if revision is None:
                assert revision_id == self.active_revision.id
                revision = self.current_revision_record()
            return httpx.Response(200, json=revision.model_dump(mode="json"))
        if path.endswith("/validate"):
            return httpx.Response(
                200,
                json=core_v1.ProjectValidationResponseV1(
                    valid=True,
                    registry_digest=self.registry_digest,
                    checks=[],
                    validated_at=NOW,
                ).model_dump(mode="json"),
            )
        if path == "/v1/runs" and request.method == "POST":
            run_request = core_v1.RunCreateV1.model_validate_json(request.content, strict=True)
            self.run_requests.append(run_request)
            return httpx.Response(
                202,
                json=_run(run_request, transition=self.head.transition).model_dump(mode="json"),
            )
        raise AssertionError(f"unexpected request: {request.method} {path}")


def _head(
    *,
    active_revision: core_v1.RevisionRefV1 = REVISION,
    etag: str = ETAG_A,
    updated_at: str = NOW,
) -> core_v1.RevisionHeadV1:
    return core_v1.RevisionHeadV1(
        project_id=CORE_PROJECT_ID,
        active_revision=active_revision,
        updated_at=updated_at,
        etag=etag,
    )


def _upload(
    request: core_v1.ProjectCreateV1,
    *,
    upload_id: str = "upload-1",
    project_snapshot: core_v1.ImmutableSnapshotRefV1 = PROJECT_SNAPSHOT,
    project_etag: str = ETAG_A,
) -> core_v1.WorkspaceUploadSessionV1:
    assert isinstance(request.workspace, core_v1.ImportedWorkspaceSpecV1)
    return core_v1.WorkspaceUploadSessionV1(
        id=upload_id,
        project_id=CORE_PROJECT_ID,
        status=core_v1.WorkspaceUploadStatus.OPEN,
        accepted_offset=0,
        project_snapshot=project_snapshot,
        project_etag=project_etag,
        archive=request.workspace.archive,
        created_at=NOW,
        updated_at=NOW,
        etag=ETAG_B,
    )


def _run(
    request: core_v1.RunCreateV1,
    *,
    transition: core_v1.RevisionTransitionV1 | None = None,
) -> core_v1.RunV1:
    return core_v1.RunV1(
        id="run-1",
        project_id=CORE_PROJECT_ID,
        project_snapshot=request.project_snapshot,
        task_snapshot=request.task_snapshot,
        workspace_snapshot=request.workspace_snapshot,
        registry_digest=request.expected_registry_digest,
        execution_mode=core_v1.ExecutionMode.SELF_DEPLOYED,
        capture_mode=core_v1.CaptureMode.TRANSCRIPT,
        status=core_v1.RunStatus.QUEUED,
        queued_reason=core_v1.QueuedReasonV1(
            code=core_v1.QueuedReasonCode.CAPACITY,
            summary="Capacity is pending.",
            retry_after_seconds=1,
        ),
        attempt_count=0,
        required_revision=request.required_revision,
        revision_transition=transition,
        created_at=NOW,
        updated_at=NOW,
        etag=ETAG_A,
        attempts=[],
    )


def _core_error() -> dict[str, object]:
    return {
        "schema_version": "1",
        "request_id": "request-1",
        "code": "route_not_implemented",
        "http_status": 503,
        "message": "This Core route is not implemented.",
        "severity": "blocking",
        "category": "service",
        "retryable": False,
        "repair_action": "unsupported",
        "next_action": "Install a Core build that implements this route.",
        "details": {},
        "logs_ref": None,
    }


def _retention_conflict() -> dict[str, object]:
    return {
        **_core_error(),
        "code": "etag_mismatch",
        "http_status": 412,
        "message": "The retained idempotency response expired and the resource ETag advanced.",
        "category": "project",
        "repair_action": "openevo_can_retry",
        "next_action": "Reread the authoritative resource before retrying.",
    }


def _assert_exact_retention_conflict(error: core_v1.ApiErrorV1) -> None:
    assert error == core_v1.ApiErrorV1.model_validate_json(
        json.dumps(_retention_conflict()),
        strict=True,
    )


def _bridge(
    local_project: local_v1.ProjectV1,
    *,
    persistence: FakePersistence | None = None,
    fake_core: FakeCore | None = None,
    archive_source: FakeArchiveSource | None = None,
    timeout: float = 5.0,
    activation_timeout: float | None = None,
) -> tuple[DesktopCoreBridgeV1, FakePersistence, FakeCore, FakeTunnelFactory]:
    persistence = persistence or FakePersistence()
    fake_core = fake_core or FakeCore(local_project)
    tunnels = FakeTunnelFactory()
    bridge = DesktopCoreBridgeV1(
        host_service=FakeHostService(),
        tunnel_factory=tunnels,
        persistence=persistence,
        archive_source=archive_source or FakeArchiveSource(),
        transport_factory=lambda: httpx.MockTransport(fake_core),
        timeout=timeout,
        activation_timeout=activation_timeout,
    )
    return bridge, persistence, fake_core, tunnels


def test_project_mapping_is_deterministic_and_has_no_local_or_host_identity() -> None:
    local_project = _local_project(imported=True)

    first = map_project_create_v1(local_project)
    second = map_project_create_v1(local_project)

    assert first == second
    assert first.name == local_project.name
    assert first.task.objective == local_project.task.objective
    assert first.spec.execution_mode is core_v1.ExecutionMode.SELF_DEPLOYED
    assert first.spec.capture_mode is core_v1.CaptureMode.TRANSCRIPT
    assert first.spec.harness_id == "codex"
    assert first.spec.agent_model_ref == local_project.execution.hf_model
    assert first.spec.evolution.model_dump(mode="json") == local_project.evolution.model_dump(
        mode="json"
    )
    assert isinstance(first.workspace, core_v1.ImportedWorkspaceSpecV1)
    assert first.workspace.archive.content_sha256 == local_project.source.import_ref.content_sha256
    encoded = first.model_dump_json()
    assert LOCAL_PROJECT_ID not in encoded
    assert PROFILE_ID not in encoded
    assert "import_id" not in encoded
    assert "/home/" not in encoded


def test_subscription_mapping_preserves_model_and_forces_transcript_capture() -> None:
    local_project = _local_project().model_copy(
        update={
            "execution": local_v1.ExecutionSettingsV1(
                mode="codex_subscription_transcript",
                codex_model="gpt-5.3-codex-spark",
                reasoning_effort="xhigh",
            )
        }
    )

    mapped = map_project_create_v1(local_project)

    assert mapped.spec.execution_mode is core_v1.ExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT
    assert mapped.spec.capture_mode is core_v1.CaptureMode.TRANSCRIPT
    assert mapped.spec.harness_id == "codex"
    assert mapped.spec.agent_model_ref == "gpt-5.3-codex-spark"
    assert mapped.spec.reasoning_effort == "xhigh"


def test_activate_then_create_run_uses_real_strict_clients_and_core_authority() -> None:
    local_project = _local_project()
    bridge, persistence, fake_core, _tunnels = _bridge(local_project)

    activation = bridge.activate_project(
        local_project,
        idempotency_key="activate-local-project-0001",
    )
    capabilities = bridge.capabilities(local_project)
    validation = bridge.validate_project(
        local_project,
        idempotency_key="validate-local-project-0001",
    )
    run = bridge.create_run(
        local_project,
        idempotency_key="create-run-local-project-0001",
    )

    assert activation.local_project_id == LOCAL_PROJECT_ID
    assert activation.profile_id == PROFILE_ID
    assert activation.local_project_etag == ETAG_A
    assert activation.local_project_intent_sha256 == bridge_module._model_digest(
        map_project_create_v1(local_project)
    )
    assert activation.core_project.id == CORE_PROJECT_ID
    assert activation.validation.valid is True
    assert capabilities.registry_digest == REGISTRY_DIGEST
    assert validation.valid is True
    assert persistence.events[0] == "reserve_create"
    assert persistence.operation is not None
    assert persistence.mapping is not None
    assert (
        persistence.operation.project_immutable_authority
        == persistence.mapping.immutable_authority
        == bridge_module._patch_immutable_authority(activation.core_project)
    )
    assert persistence.mapping.mutable_authority == bridge_module._patch_mutable_authority(
        activation.core_project
    )
    assert fake_core.created is True
    assert run.id == "run-1"
    assert fake_core.run_requests == [
        core_v1.RunCreateV1(
            project_id=CORE_PROJECT_ID,
            project_snapshot=READY_PROJECT_SNAPSHOT,
            task_snapshot=TASK_SNAPSHOT,
            workspace_snapshot=WORKSPACE_SNAPSHOT,
            expected_registry_digest=REGISTRY_DIGEST,
            required_revision=core_v1.ReachableRequiredRevisionRefV1(
                revision=REVISION,
                reachable_from_revision_id=REVISION.id,
                relation=core_v1.RequiredRevisionRelation.ACTIVE,
            ),
        )
    ]


def test_activation_rejects_core_without_required_release_features() -> None:
    local_project = _local_project()
    fake_core = FakeCore(local_project)
    fake_core.features = []
    bridge, _persistence, _fake_core, _tunnels = _bridge(
        local_project,
        fake_core=fake_core,
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as raised:
        bridge.activate_project(
            local_project,
            idempotency_key="activate-missing-features-0001",
        )

    assert raised.value.error.code == "core_required_features_unavailable"
    assert raised.value.error.http_status == 426
    assert raised.value.error.category is core_v1.ErrorCategory.CONTRACT
    assert raised.value.error.repair_action is core_v1.RepairAction.USER_ACTION_REQUIRED
    assert bridge._active is None


def test_activation_total_deadline_does_not_expand_core_request_timeout() -> None:
    local_project = _local_project()
    bridge, _, _, _ = _bridge(
        local_project,
        timeout=60.0,
        activation_timeout=900.0,
    )

    activation = bridge.activate_project(
        local_project,
        idempotency_key="activate-long-total-deadline-0001",
    )

    assert activation.core_project.id == CORE_PROJECT_ID
    assert bridge._active is not None
    assert bridge._active.client._request_deadline_seconds == 60.0
    bridge.close()


def test_durable_activation_projection_rebinds_the_live_local_etag() -> None:
    local_project = _local_project()
    bridge, _, _, _ = _bridge(local_project)
    activation = bridge.activate_project(
        local_project,
        idempotency_key="activate-local-rebind-0001",
    )
    committed = _committed_local_activation(local_project, activation)

    with pytest.raises(DesktopCoreBridgeErrorV1) as stale:
        bridge.capabilities(committed)
    assert stale.value.error.code == "active_local_project_version_mismatch"

    bridge.commit_local_activation(committed, activation=activation)

    assert bridge.capabilities(committed) == activation.capabilities
    assert bridge._active is not None
    assert bridge._active.local_project_etag == committed.etag
    bridge.commit_local_activation(committed, activation=activation)
    bridge.close()


@pytest.mark.parametrize(
    "mutation",
    ["core_etag", "registry", "intent", "state"],
)
def test_local_activation_rebind_rejects_a_mismatched_projection(mutation: str) -> None:
    local_project = _local_project()
    bridge, _, _, _ = _bridge(local_project)
    activation = bridge.activate_project(
        local_project,
        idempotency_key="activate-local-rebind-0002",
    )
    committed = _committed_local_activation(local_project, activation)
    remote = committed.remote
    assert remote is not None
    if mutation == "core_etag":
        committed = committed.model_copy(
            update={"remote": remote.model_copy(update={"etag": '"' + "d" * 64 + '"'})}
        )
    elif mutation == "registry":
        committed = committed.model_copy(
            update={"remote": remote.model_copy(update={"registry_digest": "9" * 64})}
        )
    elif mutation == "intent":
        committed = committed.model_copy(update={"name": "Changed after activation"})
    else:
        committed = committed.model_copy(update={"state": "draft"})

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        bridge.commit_local_activation(committed, activation=activation)

    assert exc_info.value.error.code == "local_activation_projection_mismatch"
    assert bridge._active is not None
    assert bridge._active.local_project_etag == local_project.etag
    assert bridge.capabilities(local_project) == activation.capabilities
    bridge.close()


@pytest.mark.parametrize(
    "mutation",
    ["generation", "source_etag", "authority", "core_projection"],
)
def test_local_activation_acknowledgement_rejects_substituted_authority(
    mutation: str,
) -> None:
    local_project = _local_project()
    bridge, _, _, _ = _bridge(local_project)
    activation = bridge.activate_project(
        local_project,
        idempotency_key="activate-authority-binding-0001",
    )
    committed = _committed_local_activation(local_project, activation)
    if mutation == "generation":
        acknowledgement = replace(activation, generation=activation.generation + 1)
    elif mutation == "source_etag":
        acknowledgement = replace(activation, local_project_etag=ETAG_C)
    elif mutation == "authority":
        acknowledgement = replace(
            activation,
            _authority=bridge_module._CoreActivationAuthorityV1(),
        )
    else:
        acknowledgement = replace(
            activation,
            core_project=activation.core_project.model_copy(update={"etag": '"' + "d" * 64 + '"'}),
        )

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        bridge.commit_local_activation(committed, activation=acknowledgement)

    assert exc_info.value.error.code == "local_activation_acknowledgement_mismatch"
    assert bridge._active is not None
    assert bridge._active.local_project_etag == local_project.etag
    bridge.close()


def test_local_activation_etag_cas_accepts_only_the_same_committed_retry() -> None:
    local_project = _local_project()
    bridge, _, _, _ = _bridge(local_project)
    activation = bridge.activate_project(
        local_project,
        idempotency_key="activate-local-cas-0001",
    )
    committed = _committed_local_activation(local_project, activation)

    bridge.commit_local_activation(committed, activation=activation)
    bridge.commit_local_activation(committed, activation=activation)

    conflicting = committed.model_copy(update={"etag": ETAG_C})
    with pytest.raises(DesktopCoreBridgeErrorV1) as conflict:
        bridge.commit_local_activation(conflicting, activation=activation)
    assert conflict.value.error.code == "local_activation_already_committed"

    unadvanced = _committed_local_activation(local_project, activation, etag=ETAG_A)
    with pytest.raises(DesktopCoreBridgeErrorV1) as source:
        bridge.commit_local_activation(unadvanced, activation=activation)
    assert source.value.error.code == "local_activation_source_etag_mismatch"
    assert bridge._active is not None
    assert bridge._active.local_project_etag == ETAG_B
    bridge.close()


def test_late_activation_acknowledgement_cannot_roll_back_a_new_session_etag() -> None:
    local_project = _local_project()
    bridge, _, _, _ = _bridge(local_project)
    old_activation = bridge.activate_project(
        local_project,
        idempotency_key="activate-before-late-ack-0001",
    )
    project_b = _committed_local_activation(local_project, old_activation, etag=ETAG_B)
    new_activation = bridge.activate_project(
        project_b,
        idempotency_key="activate-before-late-ack-0002",
    )
    project_c = _committed_local_activation(project_b, new_activation, etag=ETAG_C)
    bridge.commit_local_activation(project_c, activation=new_activation)

    with pytest.raises(DesktopCoreBridgeErrorV1) as stale:
        bridge.commit_local_activation(project_b, activation=old_activation)

    assert stale.value.error.code == "local_activation_acknowledgement_mismatch"
    assert bridge._active is not None
    assert bridge._active.local_project_etag == ETAG_C
    assert bridge.capabilities(project_c) == new_activation.capabilities
    bridge.close()


def test_deactivate_retires_only_the_named_session_and_bridge_can_reactivate() -> None:
    local_project = _local_project()
    bridge, _, _, tunnels = _bridge(local_project)
    bridge.activate_project(local_project, idempotency_key="activate-before-retire-0001")

    with pytest.raises(DesktopCoreBridgeErrorV1) as mismatch:
        bridge.deactivate_project("another-local-project")
    assert mismatch.value.error.code == "active_project_mismatch"
    assert tunnels.handles[-1].closed is False

    bridge.deactivate_project(local_project.project_id)

    assert tunnels.handles[-1].closed is True
    with pytest.raises(DesktopCoreBridgeErrorV1) as unavailable:
        bridge.capabilities(local_project)
    assert unavailable.value.error.code == "active_project_session_unavailable"
    bridge.deactivate_project(local_project.project_id)

    bridge.activate_project(local_project, idempotency_key="activate-after-retire-0002")
    assert tunnels.handles[-1].closed is False
    assert bridge.capabilities(local_project).registry_digest == REGISTRY_DIGEST
    bridge.close()


def test_create_get_rejects_project_created_at_drift_before_mutation() -> None:
    local_project = _local_project()
    persistence = FakePersistence()
    fake_core = FakeCore(local_project)
    fake_core.create_created_at = NOW
    fake_core.project_created_at = "2026-07-14T11:59:00Z"
    bridge, _, _, _ = _bridge(
        local_project,
        persistence=persistence,
        fake_core=fake_core,
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        bridge.activate_project(
            local_project,
            idempotency_key="create-get-created-at-reject-0001",
        )

    assert exc_info.value.error.code == "core_project_initial_publication_mismatch"
    assert exc_info.value.error.http_status == 409
    assert persistence.operation is not None
    assert persistence.operation.project_immutable_authority is not None
    assert persistence.operation.project_immutable_authority.created_at == NOW
    assert persistence.mapping is None
    assert not fake_core.patch_requests
    assert _workspace_mutation_count(fake_core) == 0


def test_existing_mapping_is_reopened_without_project_create() -> None:
    local_project = _local_project()
    first, persistence, fake_core, _ = _bridge(local_project)
    first.activate_project(local_project, idempotency_key="activate-local-project-0001")
    first.close()
    fake_core.calls.clear()

    second, _, _, _ = _bridge(
        local_project,
        persistence=persistence,
        fake_core=fake_core,
    )
    second.activate_project(local_project, idempotency_key="activate-local-project-0002")

    assert not any(
        request.method == "POST" and request.url.path == "/v1/projects"
        for request in fake_core.calls
    )


def test_reactivation_versions_mutable_successor_authority_and_patches_current_etag() -> None:
    original = _local_project()
    first, persistence, fake_core, _ = _bridge(original)
    first.activate_project(original, idempotency_key="successor-authority-base-0001")
    first.close()
    original_mapping = persistence.mapping
    assert original_mapping is not None

    successor_etag = '"' + "7" * 64 + '"'
    successor_registry = "8" * 64
    successor = _successor_revision(
        REVISION,
        project_snapshot=fake_core.project_snapshot,
        task_snapshot=fake_core.task_snapshot,
        workspace_snapshot=fake_core.workspace_snapshot,
        registry_digest=successor_registry,
    )
    fake_core.active_revision = successor
    fake_core.project_etag = successor_etag
    fake_core.registry_digest = successor_registry
    fake_core.project_updated_at = "2026-07-14T12:10:00Z"
    fake_core.head = core_v1.RevisionHeadV1(
        project_id=CORE_PROJECT_ID,
        active_revision=successor,
        successor_revision=None,
        transition=None,
        updated_at="2026-07-14T12:10:00Z",
        etag=successor_etag,
    )

    second, _, _, _ = _bridge(
        original,
        persistence=persistence,
        fake_core=fake_core,
    )
    activation = second.activate_project(
        original,
        idempotency_key="successor-authority-refresh-0002",
    )
    second.close()

    refreshed_mapping = persistence.mapping
    assert refreshed_mapping is not None
    assert activation.core_project.active_revision == successor
    assert refreshed_mapping.project_snapshot == original_mapping.project_snapshot
    assert refreshed_mapping.task_snapshot == original_mapping.task_snapshot
    assert refreshed_mapping.workspace_snapshot == original_mapping.workspace_snapshot
    assert refreshed_mapping.project_etag == successor_etag
    assert refreshed_mapping.registry_digest == successor_registry
    assert refreshed_mapping.active_revision == successor
    assert refreshed_mapping.project_updated_at == "2026-07-14T12:10:00Z"
    assert refreshed_mapping.mapping_generation == original_mapping.mapping_generation + 1
    assert refreshed_mapping.predecessor_request_sha256 == original_mapping.request_sha256

    modified = original.model_copy(
        update={
            "task": local_v1.ProjectTaskV1(
                title="Successor-aware edit",
                objective="Patch from the current Core authority.",
            ),
            "updated_at": "2026-07-14T12:11:00Z",
        }
    )
    third, _, _, _ = _bridge(
        modified,
        persistence=persistence,
        fake_core=fake_core,
    )
    third.activate_project(modified, idempotency_key="successor-authority-edit-0003")

    assert fake_core.patch_requests[-1][1] == successor_etag


def test_mapping_recovery_rejects_same_revision_etag_rewrite_before_mutation() -> None:
    local_project = _local_project()
    first, persistence, fake_core, _ = _bridge(local_project)
    first.activate_project(local_project, idempotency_key="mapped-etag-base-0001")
    first.close()
    mapping = persistence.mapping
    assert mapping is not None

    rewritten_etag = '"' + "6" * 64 + '"'
    fake_core.project_etag = rewritten_etag
    fake_core.head = _head(active_revision=mapping.active_revision, etag=rewritten_etag)
    patch_count = len(fake_core.patch_requests)
    workspace_mutations = _workspace_mutation_count(fake_core)
    history_count = len(persistence.mapping_history)
    recovered, _, _, _ = _bridge(
        local_project,
        persistence=persistence,
        fake_core=fake_core,
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        recovered.activate_project(
            local_project,
            idempotency_key="mapped-etag-recovery-0002",
        )

    assert exc_info.value.error.code == "core_project_mapping_mismatch"
    assert exc_info.value.error.http_status == 409
    assert persistence.mapping == mapping
    assert len(persistence.mapping_history) == history_count
    assert len(fake_core.patch_requests) == patch_count
    assert _workspace_mutation_count(fake_core) == workspace_mutations


def test_mapping_recovery_rejects_successor_reusing_project_etag_before_mutation() -> None:
    local_project = _local_project()
    first, persistence, fake_core, _ = _bridge(local_project)
    first.activate_project(local_project, idempotency_key="mapped-reused-etag-base-0001")
    first.close()
    mapping = persistence.mapping
    assert mapping is not None

    successor = _successor_revision(
        REVISION,
        project_snapshot=fake_core.project_snapshot,
        task_snapshot=fake_core.task_snapshot,
        workspace_snapshot=fake_core.workspace_snapshot,
    )
    fake_core.active_revision = successor
    fake_core.project_etag = mapping.project_etag
    fake_core.project_updated_at = "2026-07-14T12:10:00Z"
    fake_core.head = _head(active_revision=successor, etag=mapping.project_etag)
    patch_count = len(fake_core.patch_requests)
    workspace_mutations = _workspace_mutation_count(fake_core)
    history_count = len(persistence.mapping_history)
    recovered, _, _, _ = _bridge(
        local_project,
        persistence=persistence,
        fake_core=fake_core,
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        recovered.activate_project(
            local_project,
            idempotency_key="mapped-reused-etag-recovery-0002",
        )

    assert exc_info.value.error.code == "core_project_successor_proof_mismatch"
    assert exc_info.value.error.http_status == 409
    assert persistence.mapping == mapping
    assert len(persistence.mapping_history) == history_count
    assert len(fake_core.patch_requests) == patch_count
    assert _workspace_mutation_count(fake_core) == workspace_mutations


def test_mapping_recovery_rejects_same_revision_updated_at_rollback_before_mutation() -> None:
    local_project = _local_project()
    first, persistence, fake_core, _ = _bridge(local_project)
    first.activate_project(local_project, idempotency_key="mapped-time-base-0001")
    first.close()
    mapping = persistence.mapping
    assert mapping is not None

    fake_core.project_updated_at = "2026-07-14T11:59:59Z"
    patch_count = len(fake_core.patch_requests)
    workspace_mutations = _workspace_mutation_count(fake_core)
    history_count = len(persistence.mapping_history)
    recovered, _, _, _ = _bridge(
        local_project,
        persistence=persistence,
        fake_core=fake_core,
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        recovered.activate_project(
            local_project,
            idempotency_key="mapped-time-recovery-0002",
        )

    assert exc_info.value.error.code == "core_project_mapping_mismatch"
    assert exc_info.value.error.http_status == 409
    assert persistence.mapping == mapping
    assert len(persistence.mapping_history) == history_count
    assert len(fake_core.patch_requests) == patch_count
    assert _workspace_mutation_count(fake_core) == workspace_mutations


def test_mapping_recovery_rejects_project_created_at_drift_before_mutation() -> None:
    local_project = _local_project()
    first, persistence, fake_core, _ = _bridge(local_project)
    first.activate_project(local_project, idempotency_key="mapped-created-at-base-0001")
    first.close()
    mapping = persistence.mapping
    assert mapping is not None

    fake_core.project_created_at = "2026-07-14T11:59:00Z"
    patch_count = len(fake_core.patch_requests)
    workspace_mutations = _workspace_mutation_count(fake_core)
    history_count = len(persistence.mapping_history)
    recovered, _, _, _ = _bridge(
        local_project,
        persistence=persistence,
        fake_core=fake_core,
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        recovered.activate_project(
            local_project,
            idempotency_key="mapped-created-at-recovery-0002",
        )

    assert exc_info.value.error.code == "core_project_mapping_mismatch"
    assert exc_info.value.error.http_status == 409
    assert persistence.mapping == mapping
    assert len(persistence.mapping_history) == history_count
    assert len(fake_core.patch_requests) == patch_count
    assert _workspace_mutation_count(fake_core) == workspace_mutations


@pytest.mark.parametrize(
    "reported_revision",
    [
        REVISION,
        core_v1.RevisionRefV1(
            id="revision-1-rewritten",
            project_id=CORE_PROJECT_ID,
            generation=1,
            manifest_sha256="9" * 64,
        ),
    ],
    ids=[
        "generation-rollback",
        "same-generation-identity-rewrite",
    ],
)
def test_mapped_revision_authority_rejects_nonmonotonic_core_head(
    reported_revision: core_v1.RevisionRefV1,
) -> None:
    local_project = _local_project()
    first, persistence, fake_core, _ = _bridge(local_project)
    first.activate_project(local_project, idempotency_key="mapped-revision-base-0001")
    first.close()

    successor = _successor_revision(
        REVISION,
        project_snapshot=fake_core.project_snapshot,
        task_snapshot=fake_core.task_snapshot,
        workspace_snapshot=fake_core.workspace_snapshot,
    )
    successor_etag = '"' + "7" * 64 + '"'
    fake_core.active_revision = successor
    fake_core.project_etag = successor_etag
    fake_core.project_updated_at = "2026-07-14T12:10:00Z"
    fake_core.head = _head(
        active_revision=successor,
        etag=successor_etag,
        updated_at="2026-07-14T12:10:00Z",
    )
    second, _, _, _ = _bridge(
        local_project,
        persistence=persistence,
        fake_core=fake_core,
    )
    second.activate_project(local_project, idempotency_key="mapped-revision-forward-0002")
    second.close()
    successor_mapping = persistence.mapping
    assert successor_mapping is not None
    assert successor_mapping.active_revision == successor

    reported_etag = '"' + "9" * 64 + '"'
    fake_core.active_revision = reported_revision
    fake_core.project_etag = reported_etag
    fake_core.project_updated_at = "2026-07-14T12:11:00Z"
    fake_core.head = _head(active_revision=reported_revision, etag=reported_etag)
    patch_count = len(fake_core.patch_requests)
    history_count = len(persistence.mapping_history)
    third, _, _, _ = _bridge(
        local_project,
        persistence=persistence,
        fake_core=fake_core,
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        third.activate_project(local_project, idempotency_key="mapped-revision-reject-0003")

    assert exc_info.value.error.code == "core_project_successor_proof_mismatch"
    assert persistence.mapping == successor_mapping
    assert len(persistence.mapping_history) == history_count
    assert len(fake_core.patch_requests) == patch_count


def test_legal_two_generation_lag_is_a_precise_history_closure_upgrade_blocker() -> None:
    local_project = _local_project()
    first, persistence, fake_core, _ = _bridge(local_project)
    first.activate_project(local_project, idempotency_key="mapped-jump-base-0001")
    first.close()
    mapping = persistence.mapping
    assert mapping is not None

    revision_1 = _successor_revision(
        mapping.active_revision,
        project_snapshot=fake_core.project_snapshot,
        task_snapshot=fake_core.task_snapshot,
        workspace_snapshot=fake_core.workspace_snapshot,
        revision_id="revision-lag-1",
    )
    revision_2 = _successor_revision(
        revision_1,
        project_snapshot=fake_core.project_snapshot,
        task_snapshot=fake_core.task_snapshot,
        workspace_snapshot=fake_core.workspace_snapshot,
        revision_id="revision-lag-2",
    )
    jumped_etag = '"' + "2" * 64 + '"'
    fake_core.revision_records[revision_1.id] = _active_revision_record(
        revision_1,
        predecessor=mapping.active_revision,
        project_snapshot=fake_core.project_snapshot,
        task_snapshot=fake_core.task_snapshot,
        workspace_snapshot=fake_core.workspace_snapshot,
        updated_at="2026-07-14T12:11:00Z",
        etag='"1' + "1" * 63 + '"',
    )
    fake_core.active_revision = revision_1
    fake_core.active_revision = revision_2
    fake_core.project_etag = jumped_etag
    fake_core.project_updated_at = "2026-07-14T12:12:00Z"
    fake_core.head = _head(
        active_revision=revision_2,
        etag=jumped_etag,
        updated_at=fake_core.project_updated_at,
    )
    history_count = len(persistence.mapping_history)
    patch_count = len(fake_core.patch_requests)
    second, _, _, _ = _bridge(
        local_project,
        persistence=persistence,
        fake_core=fake_core,
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        second.activate_project(local_project, idempotency_key="mapped-jump-reject-0002")

    assert exc_info.value.error.code == "core_project_history_head_closure_unavailable"
    assert exc_info.value.error.http_status == 426
    assert exc_info.value.error.category is core_v1.ErrorCategory.CONTRACT
    assert exc_info.value.error.repair_action is core_v1.RepairAction.USER_ACTION_REQUIRED
    assert persistence.mapping == mapping
    assert persistence.mapping.project_etag != jumped_etag
    assert len(persistence.mapping_history) == history_count
    assert len(fake_core.patch_requests) == patch_count
    assert any(request.url.path.endswith("/revisions") for request in fake_core.calls)
    assert any(request.url.path.endswith(revision_1.id) for request in fake_core.calls)
    assert any(request.url.path.endswith(revision_2.id) for request in fake_core.calls)

    restarted, _, _, _ = _bridge(
        local_project,
        persistence=persistence,
        fake_core=fake_core,
    )
    with pytest.raises(DesktopCoreBridgeErrorV1) as retry:
        restarted.activate_project(
            local_project,
            idempotency_key="mapped-jump-retry-0003",
        )
    assert retry.value.error.code == "core_project_history_head_closure_unavailable"
    assert persistence.mapping == mapping


def test_lagging_revision_history_rejects_a_missing_adjacent_generation() -> None:
    local_project = _local_project()
    first, persistence, fake_core, _ = _bridge(local_project)
    first.activate_project(local_project, idempotency_key="mapped-gap-base-0001")
    first.close()
    mapping = persistence.mapping
    assert mapping is not None

    revision_1 = _successor_revision(
        mapping.active_revision,
        project_snapshot=fake_core.project_snapshot,
        task_snapshot=fake_core.task_snapshot,
        workspace_snapshot=fake_core.workspace_snapshot,
        revision_id="revision-gap-1",
    )
    revision_2 = _successor_revision(
        revision_1,
        project_snapshot=fake_core.project_snapshot,
        task_snapshot=fake_core.task_snapshot,
        workspace_snapshot=fake_core.workspace_snapshot,
        revision_id="revision-gap-2",
    )
    fake_core.active_revision = revision_1
    fake_core.active_revision = revision_2
    fake_core.project_updated_at = "2026-07-14T12:12:00Z"
    fake_core.project_etag = ETAG_B
    fake_core.head = _head(
        active_revision=revision_2,
        etag=ETAG_B,
        updated_at=fake_core.project_updated_at,
    )
    second, _, _, _ = _bridge(
        local_project,
        persistence=persistence,
        fake_core=fake_core,
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as raised:
        second.activate_project(local_project, idempotency_key="mapped-gap-reject-0002")

    assert raised.value.error.code == "core_project_successor_history_unavailable"
    assert "omits" in raised.value.error.message
    assert persistence.mapping == mapping


def test_import_activation_reads_only_opaque_archive_and_finalizes_workspace() -> None:
    local_project = _local_project(imported=True)
    bridge, persistence, fake_core, _ = _bridge(local_project)

    activation = bridge.activate_project(
        local_project,
        idempotency_key="activate-import-project-0001",
    )

    assert activation.core_project.workspace_publication is not None
    assert activation.core_project.current_workspace_snapshot == WORKSPACE_SNAPSHOT
    assert persistence.operation is not None
    assert persistence.operation.workspace_upload_id == "upload-1"
    upload_calls = [
        request for request in fake_core.calls if "/workspace-uploads" in request.url.path
    ]
    assert [request.method for request in upload_calls] == ["POST", "PUT", "POST"]
    assert all("adopted-import-1" not in str(request.url) for request in upload_calls)
    assert all("/home/" not in request.content.decode("ascii") for request in upload_calls)


def test_unknown_finalize_converges_only_from_the_exact_replay() -> None:
    project = _local_project(imported=True)
    persistence = FakePersistence()
    fake_core = FakeCore(project)
    fake_core.lose_finalize_after_apply_once = True
    first, _, _, _ = _bridge(
        project,
        persistence=persistence,
        fake_core=fake_core,
    )

    with pytest.raises(DesktopCoreBridgeErrorV1):
        first.activate_project(project, idempotency_key="finalize-replay-base-0001")
    first.close()
    assert persistence.operation is not None
    finalize = persistence.operation.workspace_upload_finalize
    assert finalize is not None
    assert finalize.state is CoreWorkspaceUploadFinalizeStateV1.UNKNOWN

    second, _, _, _ = _bridge(
        project,
        persistence=persistence,
        fake_core=fake_core,
    )
    activation = second.activate_project(
        project,
        idempotency_key="finalize-replay-caller-key-0002",
    )

    assert len(fake_core.finalize_requests) == 2
    assert fake_core.finalize_requests[0] == fake_core.finalize_requests[1]
    assert fake_core.finalize_requests[1][4] == finalize.idempotency_key
    assert activation.core_project.workspace_publication is not None
    assert persistence.operation.workspace_upload_finalize is not None
    assert (
        persistence.operation.workspace_upload_finalize.state
        is CoreWorkspaceUploadFinalizeStateV1.APPLIED
    )
    assert persistence.mapping is not None
    second.close()


def test_unknown_finalize_retention_conflict_stays_unknown() -> None:
    project = _local_project(imported=True)
    persistence = FakePersistence()
    fake_core = FakeCore(project)
    fake_core.lose_finalize_after_apply_once = True
    first, _, _, _ = _bridge(
        project,
        persistence=persistence,
        fake_core=fake_core,
    )

    with pytest.raises(DesktopCoreBridgeErrorV1):
        first.activate_project(project, idempotency_key="finalize-retention-base-0001")
    first.close()
    assert persistence.operation is not None
    assert persistence.operation.workspace_upload_finalize is not None
    assert (
        persistence.operation.workspace_upload_finalize.state
        is CoreWorkspaceUploadFinalizeStateV1.UNKNOWN
    )

    fake_core.expire_finalize_replay_once = True
    second, _, _, _ = _bridge(
        project,
        persistence=persistence,
        fake_core=fake_core,
    )
    with pytest.raises(DesktopCoreBridgeErrorV1) as raised:
        second.activate_project(
            project,
            idempotency_key="finalize-retention-recover-0002",
        )

    _assert_exact_retention_conflict(raised.value.error)
    assert len(fake_core.finalize_requests) == 2
    assert fake_core.finalize_requests[0] == fake_core.finalize_requests[1]
    assert persistence.operation.workspace_upload_finalize is not None
    assert (
        persistence.operation.workspace_upload_finalize.state
        is CoreWorkspaceUploadFinalizeStateV1.UNKNOWN
    )
    assert persistence.operation.workspace_upload_finalize.outcome is None
    assert persistence.mapping is None
    second.close()


def test_workspace_finalize_rejects_project_created_at_drift_before_persistence() -> None:
    local_project = _local_project(imported=True)
    persistence = FakePersistence()
    fake_core = FakeCore(local_project)
    fake_core.finalize_created_at = "2026-07-14T11:59:00Z"
    bridge, _, _, _ = _bridge(
        local_project,
        persistence=persistence,
        fake_core=fake_core,
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        bridge.activate_project(
            local_project,
            idempotency_key="finalize-created-at-reject-0001",
        )

    assert exc_info.value.error.code == "workspace_finalize_authority_mismatch"
    assert exc_info.value.error.http_status == 409
    assert persistence.operation is not None
    assert persistence.operation.workspace_upload_finalize is not None
    assert (
        persistence.operation.workspace_upload_finalize.state
        is CoreWorkspaceUploadFinalizeStateV1.UNKNOWN
    )
    assert persistence.operation.workspace_upload_finalize.outcome is None
    assert persistence.mapping is None


def test_unknown_project_create_outcome_retries_exact_persisted_intent() -> None:
    local_project = _local_project()
    persistence = FakePersistence()
    fake_core = FakeCore(local_project)
    fake_core.lose_create_response_once = True
    first, _, _, _ = _bridge(
        local_project,
        persistence=persistence,
        fake_core=fake_core,
    )

    with pytest.raises(DesktopCoreBridgeErrorV1):
        first.activate_project(
            local_project,
            idempotency_key="activate-unknown-outcome-0001",
        )

    second, _, _, _ = _bridge(
        local_project,
        persistence=persistence,
        fake_core=fake_core,
    )
    second.activate_project(
        local_project,
        idempotency_key="activate-unknown-outcome-0001",
    )
    create_calls = [
        request
        for request in fake_core.calls
        if request.method == "POST" and request.url.path == "/v1/projects"
    ]
    assert len(create_calls) == 2
    assert create_calls[0].headers["Idempotency-Key"] == create_calls[1].headers["Idempotency-Key"]
    assert create_calls[0].content == create_calls[1].content


def test_unknown_project_create_outcome_rejects_a_different_retry_key() -> None:
    local_project = _local_project()
    persistence = FakePersistence()
    fake_core = FakeCore(local_project)
    fake_core.lose_create_response_once = True
    first, _, _, _ = _bridge(
        local_project,
        persistence=persistence,
        fake_core=fake_core,
    )
    with pytest.raises(DesktopCoreBridgeErrorV1):
        first.activate_project(
            local_project,
            idempotency_key="activate-unknown-outcome-0001",
        )
    calls_before = len(fake_core.calls)
    second, _, _, _ = _bridge(
        local_project,
        persistence=persistence,
        fake_core=fake_core,
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        second.activate_project(
            local_project,
            idempotency_key="activate-unknown-outcome-CHANGED",
        )

    assert exc_info.value.error.code == "core_project_create_replay_mismatch"
    assert len(fake_core.calls) == calls_before


def test_create_run_waits_for_nonterminal_successor_to_become_active() -> None:
    local_project = _local_project()
    bridge, _, fake_core, _ = _bridge(local_project)
    bridge.activate_project(local_project, idempotency_key="activate-local-project-0001")
    successor = _successor_revision(
        REVISION,
        project_snapshot=fake_core.project_snapshot,
        task_snapshot=fake_core.task_snapshot,
        workspace_snapshot=fake_core.workspace_snapshot,
        registry_digest=fake_core.registry_digest,
        revision_id="revision-1",
    )
    transition = core_v1.RevisionTransitionV1(
        state=core_v1.RevisionTransitionState.MATERIALIZING,
        predecessor_revision=REVISION,
        successor_revision=successor,
        progress_completed=1,
        progress_total=2,
        message="Materializing the next revision.",
        updated_at=NOW,
    )
    fake_core.head = core_v1.RevisionHeadV1(
        project_id=CORE_PROJECT_ID,
        active_revision=REVISION,
        successor_revision=successor,
        transition=transition,
        updated_at=NOW,
        etag=ETAG_B,
    )
    calls_before = len(fake_core.calls)

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        bridge.create_run(
            local_project,
            idempotency_key="create-run-successor-0001",
        )

    assert exc_info.value.error.code == "core_project_successor_not_ready"
    assert exc_info.value.error.http_status == 409
    assert exc_info.value.error.retryable is True
    assert fake_core.run_requests == []
    assert not any(
        request.url.path.endswith("/validate")
        for request in fake_core.calls[calls_before:]
    )


@pytest.mark.parametrize(
    ("state", "expected_code", "retryable", "repair_action"),
    [
        (
            core_v1.RevisionTransitionState.FAILED,
            "core_project_successor_failed",
            False,
            core_v1.RepairAction.OPENEVO_CAN_RECONFIGURE,
        ),
        (
            core_v1.RevisionTransitionState.CANCELLED,
            "core_project_successor_cancelled",
            True,
            core_v1.RepairAction.OPENEVO_CAN_RETRY,
        ),
        (
            core_v1.RevisionTransitionState.UNAVAILABLE,
            "core_project_successor_unavailable",
            False,
            core_v1.RepairAction.USER_ACTION_REQUIRED,
        ),
    ],
)
def test_create_run_rejects_terminal_successor(
    state: core_v1.RevisionTransitionState,
    expected_code: str,
    retryable: bool,
    repair_action: core_v1.RepairAction,
) -> None:
    local_project = _local_project()
    bridge, _, fake_core, _ = _bridge(local_project)
    bridge.activate_project(local_project, idempotency_key="activate-local-project-0001")
    successor = core_v1.RevisionRefV1(
        id="revision-1",
        project_id=CORE_PROJECT_ID,
        generation=1,
        manifest_sha256="7" * 64,
    )
    transition_error = (
        core_v1.ApiErrorV1.model_validate_json(json.dumps(_core_error())).model_copy(
            update={"logs_ref": "transition-log-1"}
        )
        if state is core_v1.RevisionTransitionState.FAILED
        else None
    )
    transition = core_v1.RevisionTransitionV1(
        state=state,
        predecessor_revision=REVISION,
        successor_revision=successor,
        progress_completed=1,
        progress_total=1,
        message="The successor project head did not become active.",
        error=transition_error,
        updated_at=NOW,
    )
    fake_core.head = core_v1.RevisionHeadV1(
        project_id=CORE_PROJECT_ID,
        active_revision=REVISION,
        successor_revision=successor,
        transition=transition,
        updated_at=NOW,
        etag=ETAG_B,
    )
    calls_before = len(fake_core.calls)

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        bridge.create_run(
            local_project,
            idempotency_key=f"create-run-terminal-successor-{state.value}",
        )

    assert exc_info.value.error.code == expected_code
    assert exc_info.value.error.http_status == 409
    assert exc_info.value.error.retryable is retryable
    assert exc_info.value.error.repair_action is repair_action
    assert "abandon evolution" in exc_info.value.error.next_action
    if transition_error is not None:
        assert exc_info.value.error.message == transition_error.message
        assert exc_info.value.error.category is transition_error.category
        assert exc_info.value.error.details == transition_error.details
        assert exc_info.value.error.logs_ref == transition_error.logs_ref
    assert fake_core.run_requests == []
    assert not any(
        request.url.path.endswith("/validate")
        for request in fake_core.calls[calls_before:]
    )


def test_cross_project_proxy_fails_before_transport() -> None:
    local_project = _local_project()
    bridge, _, fake_core, _ = _bridge(local_project)
    bridge.activate_project(local_project, idempotency_key="activate-local-project-0001")
    calls_before = len(fake_core.calls)
    other_project = local_project.model_copy(update={"project_id": "another-local-project"})

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        bridge.capabilities(other_project)

    assert exc_info.value.error.code == "active_project_mismatch"
    assert len(fake_core.calls) == calls_before


@pytest.mark.parametrize("method", ["capabilities", "validate", "create_run"])
def test_local_project_etag_drift_fails_before_core_transport(method: str) -> None:
    local_project = _local_project()
    bridge, _, fake_core, _ = _bridge(local_project)
    bridge.activate_project(local_project, idempotency_key="activate-binding-a-0001")
    edited = local_project.model_copy(
        update={"etag": ETAG_B, "updated_at": "2026-07-14T12:01:00Z"}
    )
    calls_before = len(fake_core.calls)

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        if method == "capabilities":
            bridge.capabilities(edited)
        elif method == "validate":
            bridge.validate_project(edited, idempotency_key="validate-binding-b-0001")
        else:
            bridge.create_run(edited, idempotency_key="create-run-binding-b-0001")

    assert exc_info.value.error.code == "active_local_project_version_mismatch"
    assert exc_info.value.error.http_status == 409
    assert len(fake_core.calls) == calls_before


@pytest.mark.parametrize(
    "update",
    [
        {"profile_id": "profile-2"},
        {"name": "Edited without a matching activation"},
    ],
    ids=["profile", "mapped-intent"],
)
def test_local_project_binding_drift_fails_before_core_transport(
    update: dict[str, object],
) -> None:
    local_project = _local_project()
    bridge, _, fake_core, _ = _bridge(local_project)
    bridge.activate_project(local_project, idempotency_key="activate-binding-a-0001")
    edited = local_project.model_copy(update=update)
    calls_before = len(fake_core.calls)

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        bridge.capabilities(edited)

    assert exc_info.value.error.code == "active_local_project_version_mismatch"
    assert len(fake_core.calls) == calls_before


def _core_project_intent_drift(fake_core: FakeCore, field: str) -> None:
    request = fake_core.request
    if field == "name":
        request = request.model_copy(update={"name": "Externally rewritten project"})
    elif field == "spec":
        request = request.model_copy(
            update={
                "spec": request.spec.model_copy(
                    update={"agent_model_ref": "external/model-rewrite"}
                )
            }
        )
    elif field == "task":
        request = request.model_copy(
            update={
                "task": core_v1.TaskSpecV1(
                    title="Externally rewritten task",
                    objective="This task was not authorized by Desktop.",
                )
            }
        )
        fake_core.task_snapshot = _snapshot(
            "task-snapshot-external", core_v1.SnapshotKind.TASK, "e"
        )
    elif field == "workspace":
        request = request.model_copy(
            update={
                "workspace": core_v1.ScratchWorkspaceSpecV1(
                    kind=core_v1.WorkspaceSourceKind.SCRATCH,
                    display_name="Externally rewritten workspace",
                )
            }
        )
        fake_core.workspace_snapshot = _snapshot(
            "workspace-snapshot-external", core_v1.SnapshotKind.WORKSPACE, "e"
        )
    else:
        raise AssertionError(f"unsupported drift field: {field}")
    fake_core.request = request
    fake_core.project_snapshot = _snapshot(
        "project-snapshot-external", core_v1.SnapshotKind.PROJECT, "e"
    )
    fake_core.project_etag = '"' + "e" * 64 + '"'
    fake_core.project_updated_at = "2026-07-14T12:01:00Z"
    successor = core_v1.RevisionRefV1(
        id="revision-external-successor",
        project_id=CORE_PROJECT_ID,
        generation=1,
        manifest_sha256="e" * 64,
    )
    fake_core.active_revision = successor
    fake_core.head = _head(active_revision=successor, etag=fake_core.project_etag)


@pytest.mark.parametrize("method", ["capabilities", "validate", "create_run"])
@pytest.mark.parametrize("field", ["name", "spec", "task", "workspace"])
def test_refreshed_core_project_intent_drift_fails_before_transport_mutation(
    method: str,
    field: str,
) -> None:
    local_project = _local_project()
    bridge, _, fake_core, _ = _bridge(local_project)
    bridge.activate_project(local_project, idempotency_key="activate-core-drift-0001")
    _core_project_intent_drift(fake_core, field)
    calls_before = len(fake_core.calls)

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        if method == "capabilities":
            bridge.capabilities(local_project)
        elif method == "validate":
            bridge.validate_project(
                local_project,
                idempotency_key=f"validate-core-{field}-drift-0001",
            )
        else:
            bridge.create_run(
                local_project,
                idempotency_key=f"create-run-core-{field}-drift-0001",
            )

    assert exc_info.value.error.code == "core_project_identity_mismatch"
    calls = fake_core.calls[calls_before:]
    assert calls
    assert all(request.method == "GET" for request in calls)
    assert not fake_core.run_requests


def _project_with_core_invalid_empty_import(
    project: local_v1.ProjectV1,
) -> local_v1.ProjectV1:
    archive = b"\0" * 2048
    source = local_v1.ProjectSourceV1(
        kind="native_folder_snapshot",
        display_name="Core-invalid empty archive",
        import_ref=local_v1.WorkspaceImportRefV1(
            import_id="adopted-import-core-invalid",
            content_sha256=hashlib.sha256(archive).hexdigest(),
            byte_size=len(archive),
            entry_count=0,
            extracted_byte_size=0,
        ),
    )
    return project.model_copy(update={"source": source})


@pytest.mark.parametrize("method", ["capabilities", "validate", "create_run"])
def test_core_invalid_local_mapping_is_a_closed_bridge_error_before_transport(
    method: str,
) -> None:
    local_project = _local_project(imported=True)
    bridge, _, fake_core, _ = _bridge(local_project)
    bridge.activate_project(local_project, idempotency_key="activate-invalid-map-a-0001")
    invalid = _project_with_core_invalid_empty_import(local_project)
    calls_before = len(fake_core.calls)

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        if method == "capabilities":
            bridge.capabilities(invalid)
        elif method == "validate":
            bridge.validate_project(
                invalid,
                idempotency_key="validate-invalid-local-map-0001",
            )
        else:
            bridge.create_run(
                invalid,
                idempotency_key="create-run-invalid-local-map-0001",
            )

    assert exc_info.value.error.code == "invalid_local_project"
    assert exc_info.value.error.http_status == 422
    assert len(fake_core.calls) == calls_before


def test_local_version_mismatch_precedes_invalid_core_mapping() -> None:
    local_project = _local_project(imported=True)
    bridge, _, fake_core, _ = _bridge(local_project)
    bridge.activate_project(local_project, idempotency_key="activate-invalid-map-a-0001")
    invalid = _project_with_core_invalid_empty_import(local_project).model_copy(
        update={"etag": ETAG_B}
    )
    calls_before = len(fake_core.calls)

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        bridge.capabilities(invalid)

    assert exc_info.value.error.code == "active_local_project_version_mismatch"
    assert len(fake_core.calls) == calls_before


def test_same_revision_mutable_authority_drift_is_not_a_successor() -> None:
    local_project = _local_project()
    bridge, _, fake_core, _ = _bridge(local_project)
    bridge.activate_project(local_project, idempotency_key="activate-authority-a-0001")
    fake_core.project_etag = '"' + "d" * 64 + '"'
    fake_core.project_updated_at = "2026-07-14T12:02:00Z"
    calls_before = len(fake_core.calls)

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        bridge.capabilities(local_project)

    assert exc_info.value.error.code == "core_project_refresh_authority_mismatch"
    assert all(request.method == "GET" for request in fake_core.calls[calls_before:])


def test_authority_only_revision_successor_keeps_local_project_binding_valid() -> None:
    local_project = _local_project()
    bridge, persistence, fake_core, _ = _bridge(local_project)
    bridge.activate_project(local_project, idempotency_key="activate-authority-a-0001")
    assert persistence.mapping is not None
    initial_mapping = persistence.mapping
    successor_project_snapshot = _snapshot(
        "project-snapshot-successor-1",
        core_v1.SnapshotKind.PROJECT,
        "7",
    )
    successor_workspace_snapshot = _snapshot(
        "workspace-snapshot-successor-1",
        core_v1.SnapshotKind.WORKSPACE,
        "8",
    )
    successor = _successor_revision(
        REVISION,
        project_snapshot=successor_project_snapshot,
        task_snapshot=fake_core.task_snapshot,
        workspace_snapshot=successor_workspace_snapshot,
    )
    successor_etag = '"' + "7" * 64 + '"'
    fake_core.active_revision = successor
    fake_core.project_snapshot = successor_project_snapshot
    fake_core.workspace_snapshot = successor_workspace_snapshot
    fake_core.project_etag = successor_etag
    fake_core.project_updated_at = "2026-07-14T12:02:00Z"
    fake_core.head = _head(
        active_revision=successor,
        etag=successor_etag,
        updated_at="2026-07-14T12:02:00Z",
    )

    refreshed_project, refreshed_capabilities = bridge.refresh_project_authority(
        local_project
    )
    assert refreshed_project.active_revision == successor
    assert refreshed_capabilities.registry_digest == fake_core.registry_digest

    run = bridge.create_run(
        local_project,
        idempotency_key="create-run-authority-successor-0001",
    )

    assert run.required_revision.revision == successor
    assert fake_core.run_requests[-1].project_snapshot == successor_project_snapshot
    assert fake_core.run_requests[-1].workspace_snapshot == successor_workspace_snapshot
    assert persistence.mapping is not None
    assert persistence.mapping.active_revision == successor
    assert persistence.mapping.project_snapshot == successor_project_snapshot
    assert persistence.mapping.workspace_snapshot == successor_workspace_snapshot
    assert persistence.mapping.mapping_generation == initial_mapping.mapping_generation + 1

    bridge.close()
    bridge, persistence, fake_core, _ = _bridge(
        local_project,
        persistence=persistence,
        fake_core=fake_core,
    )
    bridge.activate_project(local_project, idempotency_key="activate-authority-a-0002")

    next_project_snapshot = _snapshot(
        "project-snapshot-successor-2",
        core_v1.SnapshotKind.PROJECT,
        "9",
    )
    next_successor = _successor_revision(
        successor,
        project_snapshot=next_project_snapshot,
        task_snapshot=fake_core.task_snapshot,
        workspace_snapshot=successor_workspace_snapshot,
    )
    next_etag = '"' + "8" * 64 + '"'
    fake_core.active_revision = next_successor
    fake_core.project_snapshot = next_project_snapshot
    fake_core.project_etag = next_etag
    fake_core.project_updated_at = "2026-07-14T12:03:00Z"
    fake_core.head = _head(
        active_revision=next_successor,
        etag=next_etag,
        updated_at="2026-07-14T12:03:00Z",
    )

    next_run = bridge.create_run(
        local_project,
        idempotency_key="create-run-authority-successor-0002",
    )

    assert next_run.required_revision.revision == next_successor
    assert bridge._active is not None
    assert bridge._active.project.active_revision == next_successor
    assert persistence.mapping is not None
    assert persistence.mapping.active_revision == next_successor
    assert persistence.mapping.project_snapshot == next_project_snapshot
    assert persistence.mapping.mapping_generation == initial_mapping.mapping_generation + 2
    assert fake_core.run_requests[-1].project_snapshot == next_project_snapshot
    assert fake_core.run_requests[-1].workspace_snapshot == successor_workspace_snapshot


def test_project_head_successor_rejects_consistent_false_manifest_digest() -> None:
    local_project = _local_project()
    bridge, persistence, fake_core, _ = _bridge(local_project)
    bridge.activate_project(local_project, idempotency_key="activate-authority-a-0001")
    initial_mapping = persistence.mapping
    assert initial_mapping is not None
    valid_successor = _successor_revision(
        REVISION,
        project_snapshot=fake_core.project_snapshot,
        task_snapshot=fake_core.task_snapshot,
        workspace_snapshot=fake_core.workspace_snapshot,
    )
    false_successor = valid_successor.model_copy(update={"manifest_sha256": "f" * 64})
    successor_etag = '"' + "7" * 64 + '"'
    fake_core.active_revision = false_successor
    fake_core.project_etag = successor_etag
    fake_core.project_updated_at = "2026-07-14T12:02:00Z"
    fake_core.head = _head(
        active_revision=false_successor,
        etag=successor_etag,
        updated_at=fake_core.project_updated_at,
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        bridge.capabilities(local_project)

    assert exc_info.value.error.code == "core_project_successor_proof_mismatch"
    assert persistence.mapping == initial_mapping


def test_project_binding_and_core_transport_share_one_generation_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_project = _local_project()
    bridge, _, _, _ = _bridge(local_project)
    bridge.activate_project(local_project, idempotency_key="activate-lease-a-0001")
    assert bridge._active is not None
    old_client = bridge._active.client
    original_check = bridge._ensure_local_project_binding
    check_entered = threading.Event()
    check_release = threading.Event()
    old_transport_called = threading.Event()
    first_check = True

    def blocking_check(
        session: bridge_module.DesktopCoreActiveSessionV1,
        project: local_v1.ProjectV1,
    ) -> None:
        nonlocal first_check
        original_check(session, project)
        if first_check:
            first_check = False
            check_entered.set()
            assert check_release.wait(timeout=2)

    def old_capabilities(
        _mode: core_v1.ExecutionMode,
    ) -> core_v1.CapabilitiesResponseV1:
        old_transport_called.set()
        return _capabilities()

    monkeypatch.setattr(bridge, "_ensure_local_project_binding", blocking_check)
    monkeypatch.setattr(old_client, "capabilities", old_capabilities)
    capability_result: list[object] = []
    activation_result: list[object] = []

    def read_capabilities() -> None:
        try:
            capability_result.append(bridge.capabilities(local_project))
        except BaseException as exc:
            capability_result.append(exc)

    edited = local_project.model_copy(
        update={"etag": ETAG_B, "updated_at": "2026-07-14T12:03:00Z"}
    )

    def activate_edited() -> None:
        try:
            activation_result.append(
                bridge.activate_project(edited, idempotency_key="activate-lease-b-0002")
            )
        except BaseException as exc:
            activation_result.append(exc)

    reader = threading.Thread(target=read_capabilities)
    reader.start()
    assert check_entered.wait(timeout=1)
    activator = threading.Thread(target=activate_edited)
    activator.start()
    for _ in range(100):
        if bridge._active is not None and bridge._active.token.cancelled:
            break
        time.sleep(0.01)
    assert bridge._active is not None
    assert bridge._active.token.cancelled is True
    check_release.set()
    reader.join(timeout=2)
    activator.join(timeout=2)

    assert isinstance(capability_result[0], DesktopCoreBridgeErrorV1)
    assert capability_result[0].error.code == "active_project_session_superseded"
    assert old_transport_called.is_set() is False
    assert isinstance(activation_result[0], bridge_module.CoreActivationV1)


def test_mapping_snapshot_drift_fails_closed() -> None:
    local_project = _local_project()
    first, persistence, fake_core, _ = _bridge(local_project)
    first.activate_project(local_project, idempotency_key="activate-local-project-0001")
    first.close()
    assert persistence.mapping is not None
    persistence.mapping = replace(
        persistence.mapping,
        workspace_snapshot=_snapshot(
            "workspace-snapshot-other", core_v1.SnapshotKind.WORKSPACE, "9"
        ),
    )

    second, _, _, _ = _bridge(
        local_project,
        persistence=persistence,
        fake_core=fake_core,
    )
    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        second.activate_project(local_project, idempotency_key="activate-local-project-0002")

    assert exc_info.value.error.code == "core_project_mapping_mismatch"


def test_mapping_canonical_request_digest_corruption_fails_before_core_transport() -> None:
    local_project = _local_project()
    first, persistence, fake_core, _ = _bridge(local_project)
    first.activate_project(local_project, idempotency_key="mapping-digest-base-0001")
    first.close()
    assert persistence.mapping is not None
    persistence.mapping = replace(
        persistence.mapping,
        project_create=persistence.mapping.project_create.model_copy(
            update={"name": "Corrupted persisted intent"}
        ),
    )
    calls_before = len(fake_core.calls)
    second, _, _, _ = _bridge(
        local_project,
        persistence=persistence,
        fake_core=fake_core,
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        second.activate_project(
            local_project,
            idempotency_key="mapping-digest-reopen-0002",
        )

    assert exc_info.value.error.code == "core_project_mapping_mismatch"
    assert len(fake_core.calls) == calls_before


def test_patch_response_rejects_project_created_at_drift_before_persistence() -> None:
    original = _local_project()
    first, persistence, fake_core, _ = _bridge(original)
    first.activate_project(original, idempotency_key="patch-created-at-base-0001")
    first.close()
    mapping = persistence.mapping
    assert mapping is not None

    modified = original.model_copy(
        update={
            "task": local_v1.ProjectTaskV1(
                title="Reject rewritten creation time",
                objective="Preserve immutable Core project identity.",
            ),
            "updated_at": "2026-07-14T12:01:00Z",
        }
    )
    fake_core.patch_created_at = "2026-07-14T11:59:00Z"
    workspace_mutations = _workspace_mutation_count(fake_core)
    history_count = len(persistence.mapping_history)
    applied_count = persistence.events.count("record_patch_applied")
    second, _, _, _ = _bridge(
        modified,
        persistence=persistence,
        fake_core=fake_core,
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        second.activate_project(
            modified,
            idempotency_key="patch-created-at-reject-0002",
        )

    assert exc_info.value.error.code == "core_project_patch_outcome_mismatch"
    assert exc_info.value.error.http_status == 409
    assert persistence.mapping == mapping
    assert len(persistence.mapping_history) == history_count
    assert persistence.patch_operation is not None
    assert persistence.patch_operation.state is CoreProjectPatchStateV1.UNKNOWN
    assert persistence.events.count("record_patch_applied") == applied_count
    assert _workspace_mutation_count(fake_core) == workspace_mutations


def test_mapped_project_edits_patch_core_and_commit_versioned_mapping() -> None:
    original = _local_project()
    first, persistence, fake_core, _ = _bridge(original)
    first.activate_project(original, idempotency_key="activate-original-project-0001")
    first.close()
    original_mapping = persistence.mapping
    assert original_mapping is not None
    imported_source = _local_project(imported=True).source
    modified = original.model_copy(
        update={
            "task": local_v1.ProjectTaskV1(
                title="Redesigned task",
                objective="Use the updated objective.",
            ),
            "execution": local_v1.ExecutionSettingsV1(
                mode="self-deployed",
                hf_model="openai/gpt-oss-120b",
            ),
            "evolution": local_v1.EvolutionConfigV1.model_validate(
                {
                    "targets": {
                        "future_target": {
                            "enabled": False,
                            "method": "plugin.future.v2",
                            "config": {"preserve": [1, True, "value"]},
                        }
                    }
                },
                strict=True,
            ),
            "source": imported_source,
            "updated_at": "2026-07-14T12:01:00Z",
        }
    )
    second, _, _, _ = _bridge(
        modified,
        persistence=persistence,
        fake_core=fake_core,
    )

    activation = second.activate_project(
        modified,
        idempotency_key="activate-modified-project-0002",
    )

    assert len(fake_core.patch_requests) == 1
    patch, if_match, patch_key = fake_core.patch_requests[0]
    assert patch.task == map_project_create_v1(modified).task
    assert patch.spec == map_project_create_v1(modified).spec
    assert patch.workspace == map_project_create_v1(modified).workspace
    assert if_match == original_mapping.project_etag
    assert patch_key.startswith("desktop-core-")
    assert activation.core_project.current_project_snapshot != original_mapping.project_snapshot
    assert activation.core_project.current_task_snapshot != original_mapping.task_snapshot
    assert (
        activation.core_project.current_workspace_snapshot != original_mapping.workspace_snapshot
    )
    assert persistence.mapping is not None
    assert persistence.mapping.request_sha256 != original_mapping.request_sha256
    assert persistence.mapping.mapping_generation == original_mapping.mapping_generation + 1
    assert persistence.mapping.predecessor_request_sha256 == original_mapping.request_sha256
    assert persistence.mapping.project_create == map_project_create_v1(modified)
    assert persistence.mapping_history == [original_mapping, persistence.mapping]
    assert (
        len(
            [
                request
                for request in fake_core.calls
                if request.method == "POST" and request.url.path == "/v1/projects"
            ]
        )
        == 1
    )


def test_imported_workspace_patch_uses_a_new_snapshot_bound_upload() -> None:
    original = _local_project(imported=True)
    first, persistence, fake_core, _ = _bridge(original)
    first.activate_project(original, idempotency_key="activate-import-original-0001")
    first.close()
    assert persistence.operation is not None
    original_upload_id = persistence.operation.workspace_upload_id
    original_upload_snapshot = persistence.operation.workspace_upload_project_snapshot
    original_mapping = persistence.mapping
    assert original_upload_id == "upload-1"
    assert original_upload_snapshot is not None
    assert original_mapping is not None

    archive = b"\1" * 1024
    source = local_v1.ProjectSourceV1(
        kind="native_folder_snapshot",
        display_name="Updated workspace",
        import_ref=local_v1.WorkspaceImportRefV1(
            import_id="adopted-import-2",
            content_sha256=hashlib.sha256(archive).hexdigest(),
            byte_size=len(archive),
            entry_count=0,
            extracted_byte_size=0,
        ),
    )
    modified = original.model_copy(update={"source": source, "updated_at": "2026-07-14T12:02:00Z"})
    second, _, _, _ = _bridge(
        modified,
        persistence=persistence,
        fake_core=fake_core,
        archive_source=FakeArchiveSource(archive),
    )

    activation = second.activate_project(
        modified,
        idempotency_key="activate-import-updated-0002",
    )

    assert fake_core.upload_count == 2
    assert persistence.operation is not None
    assert persistence.operation.workspace_upload_id == "upload-2"
    assert persistence.operation.workspace_upload_project_snapshot != original_upload_snapshot
    assert activation.core_project.current_project_snapshot != original_mapping.project_snapshot
    assert (
        activation.core_project.current_workspace_snapshot != original_mapping.workspace_snapshot
    )
    assert persistence.mapping is not None
    assert (
        persistence.mapping.project_create.workspace == map_project_create_v1(modified).workspace
    )


def test_unfinalized_import_patch_aborts_stale_upload_and_only_finalizes_new_workspace() -> None:
    original = _local_project(imported=True)
    persistence = FakePersistence()
    fake_core = FakeCore(original)
    first, _, _, _ = _bridge(
        original,
        persistence=persistence,
        fake_core=fake_core,
        archive_source=FakeArchiveSource(b"\2" * 1024),
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        first.activate_project(original, idempotency_key="draft-import-a-0001")

    assert exc_info.value.error.code == "workspace_archive_mismatch"
    assert persistence.mapping is None
    assert persistence.operation is not None
    assert persistence.operation.workspace_upload_id == "upload-1"
    assert fake_core.uploads["upload-1"].status is core_v1.WorkspaceUploadStatus.OPEN
    assert not any(request.url.path.endswith("/finalize") for request in fake_core.calls)

    archive_b = b"\1" * 1024
    source_b = local_v1.ProjectSourceV1(
        kind="native_folder_snapshot",
        display_name="Imported workspace B",
        import_ref=local_v1.WorkspaceImportRefV1(
            import_id="adopted-import-b",
            content_sha256=hashlib.sha256(archive_b).hexdigest(),
            byte_size=len(archive_b),
            entry_count=0,
            extracted_byte_size=0,
        ),
    )
    modified = original.model_copy(
        update={"source": source_b, "updated_at": "2026-07-14T12:30:00Z"}
    )
    fake_core.lose_abort_after_apply_once = True
    second, _, _, _ = _bridge(
        modified,
        persistence=persistence,
        fake_core=fake_core,
        archive_source=FakeArchiveSource(archive_b),
    )

    with pytest.raises(DesktopCoreBridgeErrorV1):
        second.activate_project(modified, idempotency_key="draft-import-b-0002")

    assert fake_core.uploads["upload-1"].status is core_v1.WorkspaceUploadStatus.ABORTED
    assert persistence.operation is not None
    assert persistence.operation.workspace_upload_abort is not None
    assert (
        persistence.operation.workspace_upload_abort.state
        is CoreWorkspaceUploadAbortStateV1.UNKNOWN
    )
    third, _, _, _ = _bridge(
        modified,
        persistence=persistence,
        fake_core=fake_core,
        archive_source=FakeArchiveSource(archive_b),
    )
    activation = third.activate_project(
        modified,
        idempotency_key="draft-import-b-retry-0003",
    )

    assert len(fake_core.abort_requests) == 2
    assert fake_core.abort_requests[0] == fake_core.abort_requests[1]
    assert fake_core.upload_count == 2
    finalize_paths = [
        request.url.path
        for request in fake_core.calls
        if request.method == "POST" and request.url.path.endswith("/finalize")
    ]
    assert finalize_paths == [
        f"/v1/projects/{CORE_PROJECT_ID}/workspace-uploads/upload-2/finalize"
    ]
    assert activation.core_project.workspace == map_project_create_v1(modified).workspace
    assert activation.core_project.workspace_publication is not None
    assert persistence.mapping is not None
    assert persistence.mapping.project_create == map_project_create_v1(modified)
    assert persistence.operation.workspace_upload_abort is None


def test_unknown_abort_retention_conflict_stays_unknown() -> None:
    original = _local_project(imported=True)
    persistence = FakePersistence()
    fake_core = FakeCore(original)
    first, _, _, _ = _bridge(
        original,
        persistence=persistence,
        fake_core=fake_core,
        archive_source=FakeArchiveSource(b"\2" * 1024),
    )
    with pytest.raises(DesktopCoreBridgeErrorV1):
        first.activate_project(original, idempotency_key="abort-retention-a-0001")
    first.close()

    archive_b = b"\1" * 1024
    modified = original.model_copy(
        update={
            "source": local_v1.ProjectSourceV1(
                kind="native_folder_snapshot",
                display_name="Imported workspace B",
                import_ref=local_v1.WorkspaceImportRefV1(
                    import_id="abort-retention-import-b",
                    content_sha256=hashlib.sha256(archive_b).hexdigest(),
                    byte_size=len(archive_b),
                    entry_count=0,
                    extracted_byte_size=0,
                ),
            ),
            "updated_at": "2026-07-14T12:30:00Z",
        }
    )
    fake_core.lose_abort_after_apply_once = True
    second, _, _, _ = _bridge(
        modified,
        persistence=persistence,
        fake_core=fake_core,
        archive_source=FakeArchiveSource(archive_b),
    )
    with pytest.raises(DesktopCoreBridgeErrorV1):
        second.activate_project(modified, idempotency_key="abort-retention-b-0002")
    second.close()
    assert persistence.operation is not None
    assert persistence.operation.workspace_upload_abort is not None
    with pytest.raises(ValueError, match="abort authority"):
        replace(
            persistence.operation,
            workspace_upload_abort=replace(
                persistence.operation.workspace_upload_abort,
                idempotency_key="tampered-abort-key",
            ),
        )

    fake_core.expire_abort_replay_once = True
    third, _, _, _ = _bridge(
        modified,
        persistence=persistence,
        fake_core=fake_core,
        archive_source=FakeArchiveSource(archive_b),
    )
    with pytest.raises(DesktopCoreBridgeErrorV1) as raised:
        third.activate_project(
            modified,
            idempotency_key="abort-retention-recover-0003",
        )

    _assert_exact_retention_conflict(raised.value.error)
    assert len(fake_core.abort_requests) == 2
    assert fake_core.abort_requests[0] == fake_core.abort_requests[1]
    assert fake_core.uploads["upload-1"].status is core_v1.WorkspaceUploadStatus.ABORTED
    assert persistence.operation.workspace_upload_abort is not None
    assert (
        persistence.operation.workspace_upload_abort.state
        is CoreWorkspaceUploadAbortStateV1.UNKNOWN
    )
    assert persistence.mapping is None
    third.close()


def test_unknown_patch_outcome_replays_the_exact_versioned_request_key() -> None:
    original = _local_project()
    bridge, persistence, fake_core, _ = _bridge(original)
    bridge.activate_project(original, idempotency_key="activate-patch-base-0001")
    modified = original.model_copy(
        update={
            "task": local_v1.ProjectTaskV1(
                title="Changed after transport loss",
                objective="Retry the exact Core patch.",
            ),
            "updated_at": "2026-07-14T12:03:00Z",
        }
    )
    fake_core.lose_patch_before_apply_once = True

    with pytest.raises(DesktopCoreBridgeErrorV1):
        bridge.activate_project(modified, idempotency_key="local-patch-action-0001")

    original_mapping = persistence.mapping
    assert original_mapping is not None
    activation = bridge.activate_project(
        modified,
        idempotency_key="local-patch-action-0002",
    )

    assert len(fake_core.patch_requests) == 2
    first_patch, first_etag, first_key = fake_core.patch_requests[0]
    second_patch, second_etag, second_key = fake_core.patch_requests[1]
    assert first_patch == second_patch
    assert first_etag == second_etag == original_mapping.project_etag
    assert first_key == second_key
    assert activation.core_project.task == map_project_create_v1(modified).task
    assert persistence.mapping is not None
    assert persistence.mapping.request_sha256 != original_mapping.request_sha256


def test_unknown_patch_outcome_already_applied_is_replayed_exactly() -> None:
    original = _local_project()
    bridge, persistence, fake_core, _ = _bridge(original)
    bridge.activate_project(original, idempotency_key="applied-patch-base-0001")
    original_mapping = persistence.mapping
    assert original_mapping is not None
    modified = original.model_copy(
        update={
            "execution": local_v1.ExecutionSettingsV1(
                mode="self-deployed",
                hf_model="openai/gpt-oss-120b",
            ),
            "updated_at": "2026-07-14T12:04:00Z",
        }
    )
    fake_core.lose_patch_after_apply_once = True

    with pytest.raises(DesktopCoreBridgeErrorV1):
        bridge.activate_project(modified, idempotency_key="applied-patch-action-0001")

    activation = bridge.activate_project(
        modified,
        idempotency_key="applied-patch-action-0002",
    )

    assert len(fake_core.patch_requests) == 2
    assert fake_core.patch_requests[0] == fake_core.patch_requests[1]
    assert fake_core.patch_apply_count == 1
    assert activation.core_project.spec == map_project_create_v1(modified).spec
    assert persistence.mapping is not None
    assert persistence.mapping.mapping_generation == 2
    assert persistence.mapping.predecessor_request_sha256 == original_mapping.request_sha256


def test_unknown_patch_retention_conflict_cannot_use_revision_get_as_success() -> None:
    original = _local_project()
    first, persistence, fake_core, _ = _bridge(original)
    first.activate_project(original, idempotency_key="patch-retention-base-0001")
    modified = original.model_copy(
        update={
            "task": local_v1.ProjectTaskV1(
                title="Patch after replay retention",
                objective="Recover only from the authoritative revision closure.",
            ),
            "updated_at": "2026-07-14T12:43:00Z",
        }
    )
    fake_core.patch_advances_revision_once = True
    fake_core.lose_patch_after_apply_once = True

    with pytest.raises(DesktopCoreBridgeErrorV1):
        first.activate_project(modified, idempotency_key="patch-retention-loss-0002")
    first.close()
    assert persistence.patch_operation is not None
    assert persistence.patch_operation.state is CoreProjectPatchStateV1.UNKNOWN

    fake_core.expire_patch_replay_once = True
    second, _, _, _ = _bridge(
        modified,
        persistence=persistence,
        fake_core=fake_core,
    )
    with pytest.raises(DesktopCoreBridgeErrorV1) as raised:
        second.activate_project(
            modified,
            idempotency_key="patch-retention-recover-0003",
        )

    _assert_exact_retention_conflict(raised.value.error)
    assert len(fake_core.patch_requests) == 2
    assert fake_core.patch_requests[0] == fake_core.patch_requests[1]
    assert fake_core.patch_apply_count == 1
    assert persistence.patch_operation is not None
    assert persistence.patch_operation.state is CoreProjectPatchStateV1.UNKNOWN
    assert persistence.mapping is not None
    assert persistence.mapping.request_sha256 != bridge_module._model_digest(
        map_project_create_v1(modified)
    )
    second.close()


def test_unknown_patch_retention_conflict_without_terminal_closure_stays_unknown() -> None:
    original = _local_project()
    bridge, persistence, fake_core, _ = _bridge(original)
    bridge.activate_project(original, idempotency_key="patch-conflict-base-0001")
    original_mapping = persistence.mapping
    modified = original.model_copy(
        update={
            "task": local_v1.ProjectTaskV1(
                title="Unapplied retained patch",
                objective="Do not turn a conflict into success.",
            ),
            "updated_at": "2026-07-14T12:44:00Z",
        }
    )
    fake_core.lose_patch_before_apply_once = True

    with pytest.raises(DesktopCoreBridgeErrorV1):
        bridge.activate_project(modified, idempotency_key="patch-conflict-loss-0002")

    fake_core.expire_patch_replay_once = True
    with pytest.raises(DesktopCoreBridgeErrorV1) as raised:
        bridge.activate_project(modified, idempotency_key="patch-conflict-retry-0003")

    _assert_exact_retention_conflict(raised.value.error)
    assert persistence.mapping == original_mapping
    assert persistence.patch_operation is not None
    assert persistence.patch_operation.state is CoreProjectPatchStateV1.UNKNOWN
    assert fake_core.patch_apply_count == 0


def test_patch_published_revision_requires_complete_successor_proof() -> None:
    original = _local_project()
    bridge, persistence, fake_core, _ = _bridge(original)
    bridge.activate_project(original, idempotency_key="patch-revision-base-0001")
    previous_mapping = persistence.mapping
    assert previous_mapping is not None
    edited = original.model_copy(
        update={
            "task": local_v1.ProjectTaskV1(
                title="Patch publishes revision",
                objective="Verify the patch successor closure.",
            ),
            "updated_at": "2026-07-14T12:42:00Z",
        }
    )
    fake_core.patch_advances_revision_once = True

    activation = bridge.activate_project(
        edited,
        idempotency_key="patch-revision-action-0002",
    )

    assert activation.core_project.active_revision.generation == (
        previous_mapping.active_revision.generation + 1
    )
    assert persistence.mapping is not None
    assert persistence.mapping.active_revision == activation.core_project.active_revision
    assert any(
        request.method == "GET"
        and request.url.path.endswith(
            f"/revisions/{activation.core_project.active_revision.id}"
        )
        for request in fake_core.calls
    )


def test_patch_commit_crash_adopts_applied_a_before_patching_new_local_b() -> None:
    original = _local_project()
    first, persistence, fake_core, _ = _bridge(original)
    first.activate_project(original, idempotency_key="patch-chain-o-0001")
    first.close()
    mapping_o = persistence.mapping
    assert mapping_o is not None

    edited_a = original.model_copy(
        update={
            "task": local_v1.ProjectTaskV1(
                title="Patch A",
                objective="First durable edit.",
            ),
            "updated_at": "2026-07-14T12:40:00Z",
        }
    )
    persistence.fail_commit_once = True
    second, _, _, _ = _bridge(
        edited_a,
        persistence=persistence,
        fake_core=fake_core,
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        second.activate_project(edited_a, idempotency_key="patch-chain-a-0002")

    assert exc_info.value.error.code == "core_bridge_adapter_failed"
    assert persistence.mapping == mapping_o
    assert persistence.patch_operation is not None
    assert persistence.patch_operation.state is CoreProjectPatchStateV1.APPLIED
    assert persistence.patch_operation.new_project_create == map_project_create_v1(edited_a)
    assert fake_core.request == map_project_create_v1(edited_a)
    assert len(fake_core.patch_requests) == 1

    edited_b = edited_a.model_copy(
        update={
            "task": local_v1.ProjectTaskV1(
                title="Patch B",
                objective="Second durable edit after A commit recovery.",
            ),
            "updated_at": "2026-07-14T12:41:00Z",
        }
    )
    third, _, _, _ = _bridge(
        edited_b,
        persistence=persistence,
        fake_core=fake_core,
    )
    activation = third.activate_project(
        edited_b,
        idempotency_key="patch-chain-b-0003",
    )

    assert len(fake_core.patch_requests) == 2
    patch_a, _, key_a = fake_core.patch_requests[0]
    patch_b, _, key_b = fake_core.patch_requests[1]
    assert patch_a.task == map_project_create_v1(edited_a).task
    assert patch_b.task == map_project_create_v1(edited_b).task
    assert key_a != key_b
    assert (
        len(
            [
                request
                for request in fake_core.calls
                if request.method == "POST" and request.url.path == "/v1/projects"
            ]
        )
        == 1
    )
    assert activation.core_project.task == map_project_create_v1(edited_b).task
    assert persistence.mapping is not None
    assert persistence.mapping.project_create == map_project_create_v1(edited_b)
    assert persistence.mapping.mapping_generation == mapping_o.mapping_generation + 2
    assert persistence.patch_operation is None
    assert [mapping.project_create for mapping in persistence.mapping_history] == [
        map_project_create_v1(original),
        map_project_create_v1(edited_a),
        map_project_create_v1(edited_b),
    ]


def test_imported_project_old_finalize_does_not_replace_patch_successor_authority() -> None:
    archive = b"\0" * 1024
    original = _local_project(imported=True)
    first, persistence, fake_core, _ = _bridge(
        original,
        archive_source=FakeArchiveSource(archive),
    )
    first.activate_project(original, idempotency_key="old-finalize-base-0001")
    first.close()
    original_mapping = persistence.mapping
    assert original_mapping is not None
    assert persistence.operation is not None
    initial_finalize = persistence.operation.workspace_upload_finalize
    assert initial_finalize is not None

    edited = original.model_copy(
        update={
            "task": local_v1.ProjectTaskV1(
                title="Task-only edit after import",
                objective="Keep the imported workspace but change the task.",
            ),
            "updated_at": "2026-07-14T12:40:00Z",
        }
    )
    persistence.fail_commit_once = True
    second, _, _, _ = _bridge(
        edited,
        persistence=persistence,
        fake_core=fake_core,
        archive_source=FakeArchiveSource(archive),
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        second.activate_project(edited, idempotency_key="old-finalize-patch-0002")

    assert exc_info.value.error.code == "core_bridge_adapter_failed"
    applied = persistence.patch_operation
    assert applied is not None
    assert applied.state is CoreProjectPatchStateV1.APPLIED
    assert applied.outcome is not None
    assert applied.outcome.task == map_project_create_v1(edited).task
    assert initial_finalize.outcome is not None
    assert initial_finalize.outcome.project.task != applied.outcome.task

    successor = _successor_revision(
        applied.outcome.active_revision,
        project_snapshot=applied.outcome.current_project_snapshot,
        task_snapshot=applied.outcome.current_task_snapshot,
        workspace_snapshot=applied.outcome.current_workspace_snapshot,
    )
    successor_etag = '"' + "6" * 64 + '"'
    _set_current_revision(fake_core, successor, etag=successor_etag)
    recovered, _, _, _ = _bridge(
        edited,
        persistence=persistence,
        fake_core=fake_core,
        archive_source=FakeArchiveSource(archive),
    )

    activation = recovered.activate_project(
        edited,
        idempotency_key="old-finalize-recover-0003",
    )

    assert activation.core_project.active_revision == successor
    assert persistence.mapping is not None
    assert persistence.mapping.active_revision == successor
    assert persistence.mapping.mapping_generation == original_mapping.mapping_generation + 1
    assert persistence.patch_operation is None
    assert any(
        request.method == "GET"
        and request.url.path.endswith(f"/revisions/{successor.id}")
        for request in fake_core.calls
    )
    recovered.close()


def _prepare_finalized_import_patch_crash(
    *,
    finalize_revision: core_v1.RevisionRefV1,
) -> tuple[
    local_v1.ProjectV1,
    bytes,
    FakePersistence,
    FakeCore,
    CoreProjectMappingV1,
]:
    original = _local_project()
    first, persistence, fake_core, _ = _bridge(original)
    first.activate_project(original, idempotency_key="finalize-revision-base-0001")
    first.close()
    mapping_o = persistence.mapping
    assert mapping_o is not None

    fake_core.active_revision = finalize_revision
    if finalize_revision != mapping_o.active_revision:
        fake_core.project_etag = '"' + "7" * 64 + '"'
        fake_core.project_updated_at = "2026-07-14T12:42:00Z"
    fake_core.head = _head(
        active_revision=finalize_revision,
        etag=fake_core.project_etag,
        updated_at=fake_core.project_updated_at,
    )
    if finalize_revision != mapping_o.active_revision:
        sync, _, _, _ = _bridge(
            original,
            persistence=persistence,
            fake_core=fake_core,
        )
        sync.activate_project(
            original,
            idempotency_key="finalize-revision-successor-sync-0002",
        )
        sync.close()
        mapping_o = persistence.mapping
        assert mapping_o is not None
        assert mapping_o.active_revision == finalize_revision
    archive = b"\1" * 1024
    edited = original.model_copy(
        update={
            "source": local_v1.ProjectSourceV1(
                kind="native_folder_snapshot",
                display_name="Imported workspace revision authority",
                import_ref=local_v1.WorkspaceImportRefV1(
                    import_id="adopted-finalize-revision-authority",
                    content_sha256=hashlib.sha256(archive).hexdigest(),
                    byte_size=len(archive),
                    entry_count=0,
                    extracted_byte_size=0,
                ),
            ),
            "updated_at": "2026-07-14T12:45:00Z",
        }
    )
    persistence.fail_commit_once = True
    second, _, _, _ = _bridge(
        edited,
        persistence=persistence,
        fake_core=fake_core,
        archive_source=FakeArchiveSource(archive),
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        second.activate_project(edited, idempotency_key="finalize-revision-crash-0002")

    assert exc_info.value.error.code == "core_bridge_adapter_failed"
    assert persistence.mapping == mapping_o
    assert persistence.patch_operation is not None
    assert persistence.patch_operation.state is CoreProjectPatchStateV1.APPLIED
    assert persistence.operation is not None
    finalize_authority = persistence.operation.workspace_upload_finalize
    assert finalize_authority is not None
    assert finalize_authority.outcome.project.active_revision == finalize_revision
    return edited, archive, persistence, fake_core, mapping_o


REVISION_1 = _successor_revision(
    REVISION,
    project_snapshot=READY_PROJECT_SNAPSHOT,
    task_snapshot=TASK_SNAPSHOT,
    workspace_snapshot=WORKSPACE_SNAPSHOT,
)


def _replace_finalize_revision_authority(
    persistence: FakePersistence,
    revision: core_v1.RevisionRefV1,
    *,
    bind_outcome: bool = True,
) -> None:
    operation = persistence.operation
    assert operation is not None
    authority = operation.workspace_upload_finalize
    assert authority is not None
    finalized_project = core_v1.ProjectV1.model_validate(
        authority.outcome.project.model_copy(
            update={"active_revision": revision},
        ).model_dump(),
        strict=True,
    )
    outcome = core_v1.WorkspaceUploadFinalizeResponseV1.model_validate(
        authority.outcome.model_copy(
            update={"project": finalized_project},
        ).model_dump(),
        strict=True,
    )
    if bind_outcome:
        persistence.operation = replace(
            operation,
            workspace_upload_finalize=replace(
                authority,
                outcome=outcome,
                outcome_sha256=bridge_module._model_digest(outcome),
            ),
        )
    else:
        _replace_finalize_outcome_authority(persistence, outcome)


def _unsafe_dataclass_replace(value: object, **changes: object) -> object:
    altered = object.__new__(type(value))
    for field in fields(value):
        object.__setattr__(
            altered,
            field.name,
            changes.get(field.name, getattr(value, field.name)),
        )
    return altered


def _replace_finalize_outcome_authority(
    persistence: FakePersistence,
    outcome: core_v1.WorkspaceUploadFinalizeResponseV1,
) -> None:
    operation = persistence.operation
    assert operation is not None
    authority = operation.workspace_upload_finalize
    assert authority is not None
    altered_authority = _unsafe_dataclass_replace(authority, outcome=outcome)
    persistence.operation = _unsafe_dataclass_replace(
        operation,
        workspace_upload_finalize=altered_authority,
    )


def _workspace_mutation_count(fake_core: FakeCore) -> int:
    return len(
        [
            request
            for request in fake_core.calls
            if request.method in {"POST", "PUT"} and "/workspace-uploads" in request.url.path
        ]
    )


def _prepare_initial_import_finalize_mapping_crash() -> tuple[
    local_v1.ProjectV1,
    bytes,
    FakePersistence,
    FakeCore,
]:
    project = _local_project(imported=True)
    archive = b"\0" * 1024
    persistence = FakePersistence()
    persistence.fail_commit_once = True
    fake_core = FakeCore(project)
    first, _, _, _ = _bridge(
        project,
        persistence=persistence,
        fake_core=fake_core,
        archive_source=FakeArchiveSource(archive),
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        first.activate_project(project, idempotency_key="initial-finalize-crash-0001")

    assert exc_info.value.error.code == "core_bridge_adapter_failed"
    assert persistence.mapping is None
    assert persistence.operation is not None
    finalize_authority = persistence.operation.workspace_upload_finalize
    assert finalize_authority is not None
    assert finalize_authority.outcome.project.active_revision == REVISION
    assert finalize_authority.outcome_sha256 == bridge_module._model_digest(
        finalize_authority.outcome
    )
    return project, archive, persistence, fake_core


def _edited_initial_import(project: local_v1.ProjectV1) -> local_v1.ProjectV1:
    archive = b"\1" * 1024
    return project.model_copy(
        update={
            "task": local_v1.ProjectTaskV1(
                title="Edited after initial finalize crash",
                objective="Validate durable initial publication authority before patching.",
            ),
            "source": local_v1.ProjectSourceV1(
                kind="native_folder_snapshot",
                display_name="Edited imported workspace",
                import_ref=local_v1.WorkspaceImportRefV1(
                    import_id="edited-initial-finalize-import",
                    content_sha256=hashlib.sha256(archive).hexdigest(),
                    byte_size=len(archive),
                    entry_count=0,
                    extracted_byte_size=0,
                ),
            ),
            "updated_at": "2026-07-14T12:44:00Z",
        }
    )


def _set_current_revision(
    fake_core: FakeCore,
    revision: core_v1.RevisionRefV1,
    *,
    etag: str,
) -> None:
    fake_core.active_revision = revision
    fake_core.project_etag = etag
    fake_core.project_updated_at = "2026-07-14T12:43:00Z"
    fake_core.head = _head(
        active_revision=revision,
        etag=etag,
        updated_at=fake_core.project_updated_at,
    )


@pytest.mark.parametrize("changed_intent", [False, True], ids=["unchanged", "changed"])
def test_initial_finalize_recovery_rejects_unproven_generation_jump_before_mutation(
    changed_intent: bool,
) -> None:
    original, archive, persistence, fake_core = _prepare_initial_import_finalize_mapping_crash()
    requested = _edited_initial_import(original) if changed_intent else original
    requested_archive = b"\1" * 1024 if changed_intent else archive
    jumped_revision = core_v1.RevisionRefV1(
        id="revision-2",
        project_id=CORE_PROJECT_ID,
        generation=2,
        manifest_sha256="2" * 64,
    )
    jumped_etag = '"' + "2" * 64 + '"'
    _set_current_revision(fake_core, jumped_revision, etag=jumped_etag)
    patch_count = len(fake_core.patch_requests)
    workspace_mutations = _workspace_mutation_count(fake_core)
    commit_count = persistence.events.count("commit_mapping")
    recovered, _, _, _ = _bridge(
        requested,
        persistence=persistence,
        fake_core=fake_core,
        archive_source=FakeArchiveSource(requested_archive),
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        recovered.activate_project(
            requested,
            idempotency_key="initial-finalize-jump-recovery-0002",
        )

    assert exc_info.value.error.code == "core_project_revision_authority_mismatch"
    assert persistence.mapping is None
    assert len(fake_core.patch_requests) == patch_count
    assert _workspace_mutation_count(fake_core) == workspace_mutations
    assert persistence.events.count("commit_mapping") == commit_count


@pytest.mark.parametrize("changed_intent", [False, True], ids=["unchanged", "changed"])
def test_initial_finalize_recovery_requires_complete_direct_revision_successor_proof(
    changed_intent: bool,
) -> None:
    original, archive, persistence, fake_core = _prepare_initial_import_finalize_mapping_crash()
    requested = _edited_initial_import(original) if changed_intent else original
    requested_archive = b"\1" * 1024 if changed_intent else archive
    successor_etag = '"' + "7" * 64 + '"'
    successor_revision = _successor_revision(
        REVISION,
        project_snapshot=fake_core.project_snapshot,
        task_snapshot=fake_core.task_snapshot,
        workspace_snapshot=fake_core.workspace_snapshot,
    )
    _set_current_revision(fake_core, successor_revision, etag=successor_etag)
    patch_count = len(fake_core.patch_requests)
    recovered, _, _, _ = _bridge(
        requested,
        persistence=persistence,
        fake_core=fake_core,
        archive_source=FakeArchiveSource(requested_archive),
    )

    if changed_intent:
        with pytest.raises(DesktopCoreBridgeErrorV1) as raised:
            recovered.activate_project(
                requested,
                idempotency_key="initial-finalize-successor-recovery-0002",
            )
        assert raised.value.error.code == "core_project_initial_revision_unproved"
        assert persistence.mapping is None
        assert persistence.patch_operation is not None
        return

    activation = recovered.activate_project(
        requested,
        idempotency_key="initial-finalize-successor-recovery-0002",
    )

    assert activation.core_project.active_revision == successor_revision
    assert persistence.mapping is not None
    assert persistence.mapping.active_revision == successor_revision
    assert len(fake_core.patch_requests) == patch_count
    assert persistence.mapping.project_etag == successor_etag
    recovered.close()


def test_first_mapping_completed_patch_cannot_hide_finalize_successor_proof() -> None:
    original, archive, persistence, fake_core = _prepare_initial_import_finalize_mapping_crash()
    successor = _successor_revision(
        REVISION,
        project_snapshot=fake_core.project_snapshot,
        task_snapshot=fake_core.task_snapshot,
        workspace_snapshot=fake_core.workspace_snapshot,
    )
    _set_current_revision(fake_core, successor, etag='"' + "7" * 64 + '"')
    edited = original.model_copy(
        update={
            "task": local_v1.ProjectTaskV1(
                title="Edited after successor activation",
                objective="Do not let the completed patch hide the H0 to H1 proof.",
            ),
            "updated_at": "2026-07-14T12:45:00Z",
        }
    )
    recovered, _, _, _ = _bridge(
        edited,
        persistence=persistence,
        fake_core=fake_core,
        archive_source=FakeArchiveSource(archive),
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as raised:
        recovered.activate_project(
            edited,
            idempotency_key="first-mapping-finalize-proof-0002",
        )

    assert raised.value.error.code == "core_project_successor_proof_mismatch"
    assert persistence.mapping is None
    assert persistence.patch_operation is not None
    assert persistence.patch_operation.state is CoreProjectPatchStateV1.APPLIED
    assert any(
        request.method == "GET"
        and request.url.path.endswith(f"/revisions/{successor.id}")
        for request in fake_core.calls
    )


@pytest.mark.parametrize(
    ("finalize_revision", "reported_revision"),
    [
        (REVISION_1, REVISION),
        (
            REVISION,
            core_v1.RevisionRefV1(
                id="revision-0-rewritten",
                project_id=CORE_PROJECT_ID,
                generation=0,
                manifest_sha256=REVISION.manifest_sha256,
            ),
        ),
        (
            REVISION,
            core_v1.RevisionRefV1(
                id=REVISION.id,
                project_id=CORE_PROJECT_ID,
                generation=0,
                manifest_sha256="9" * 64,
            ),
        ),
    ],
    ids=["rollback", "same-generation-id-rewrite", "same-generation-manifest-rewrite"],
)
def test_initial_finalize_recovery_rejects_nonmonotonic_revision_authority(
    finalize_revision: core_v1.RevisionRefV1,
    reported_revision: core_v1.RevisionRefV1,
) -> None:
    project, archive, persistence, fake_core = _prepare_initial_import_finalize_mapping_crash()
    _replace_finalize_revision_authority(persistence, finalize_revision)
    reported_etag = '"' + "9" * 64 + '"'
    _set_current_revision(fake_core, reported_revision, etag=reported_etag)
    patch_count = len(fake_core.patch_requests)
    workspace_mutations = _workspace_mutation_count(fake_core)
    commit_count = persistence.events.count("commit_mapping")
    recovered, _, _, _ = _bridge(
        project,
        persistence=persistence,
        fake_core=fake_core,
        archive_source=FakeArchiveSource(archive),
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        recovered.activate_project(
            project,
            idempotency_key="initial-finalize-nonmonotonic-0002",
        )

    assert exc_info.value.error.code == "core_project_revision_authority_mismatch"
    assert persistence.mapping is None
    assert len(fake_core.patch_requests) == patch_count
    assert _workspace_mutation_count(fake_core) == workspace_mutations
    assert persistence.events.count("commit_mapping") == commit_count


def test_initial_finalize_recovery_rejects_tampered_finalize_outcome() -> None:
    project, archive, persistence, fake_core = _prepare_initial_import_finalize_mapping_crash()
    operation = persistence.operation
    assert operation is not None
    authority = operation.workspace_upload_finalize
    assert authority is not None
    tampered_project = core_v1.ProjectV1.model_validate(
        authority.outcome.project.model_copy(
            update={
                "current_project_snapshot": _snapshot(
                    "tampered-initial-finalize-project-snapshot",
                    core_v1.SnapshotKind.PROJECT,
                    "9",
                )
            }
        ).model_dump(),
        strict=True,
    )
    tampered_outcome = core_v1.WorkspaceUploadFinalizeResponseV1.model_validate(
        authority.outcome.model_copy(update={"project": tampered_project}).model_dump(),
        strict=True,
    )
    _replace_finalize_outcome_authority(persistence, tampered_outcome)
    patch_count = len(fake_core.patch_requests)
    workspace_mutations = _workspace_mutation_count(fake_core)
    commit_count = persistence.events.count("commit_mapping")
    recovered, _, _, _ = _bridge(
        project,
        persistence=persistence,
        fake_core=fake_core,
        archive_source=FakeArchiveSource(archive),
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        recovered.activate_project(
            project,
            idempotency_key="initial-finalize-tampered-0002",
        )

    assert exc_info.value.error.code == "workspace_finalize_authority_mismatch"
    assert persistence.mapping is None
    assert len(fake_core.patch_requests) == patch_count
    assert _workspace_mutation_count(fake_core) == workspace_mutations
    assert persistence.events.count("commit_mapping") == commit_count


def test_initial_finalize_recovery_rejects_outcome_revision_substitution_matching_core() -> None:
    project, archive, persistence, fake_core = _prepare_initial_import_finalize_mapping_crash()
    jumped_revision = core_v1.RevisionRefV1(
        id="revision-2",
        project_id=CORE_PROJECT_ID,
        generation=2,
        manifest_sha256="2" * 64,
    )
    _replace_finalize_revision_authority(
        persistence,
        jumped_revision,
        bind_outcome=False,
    )
    jumped_etag = '"' + "2" * 64 + '"'
    _set_current_revision(fake_core, jumped_revision, etag=jumped_etag)
    patch_count = len(fake_core.patch_requests)
    workspace_mutations = _workspace_mutation_count(fake_core)
    commit_count = persistence.events.count("commit_mapping")
    recovered, _, _, _ = _bridge(
        project,
        persistence=persistence,
        fake_core=fake_core,
        archive_source=FakeArchiveSource(archive),
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        recovered.activate_project(
            project,
            idempotency_key="initial-finalize-outcome-substitution-0002",
        )

    assert exc_info.value.error.code == "workspace_finalize_authority_mismatch"
    assert persistence.mapping is None
    assert len(fake_core.patch_requests) == patch_count
    assert _workspace_mutation_count(fake_core) == workspace_mutations
    assert persistence.events.count("commit_mapping") == commit_count


def test_workspace_finalize_authority_binds_request_and_outcome_independently() -> None:
    _, _, persistence, _ = _prepare_initial_import_finalize_mapping_crash()
    operation = persistence.operation
    assert operation is not None
    authority = operation.workspace_upload_finalize
    assert authority is not None
    substituted_request = core_v1.WorkspaceUploadFinalizeV1(content_sha256="9" * 64)
    substituted_project = core_v1.ProjectV1.model_validate(
        authority.outcome.project.model_copy(update={"active_revision": REVISION_1}).model_dump(),
        strict=True,
    )
    substituted_outcome = core_v1.WorkspaceUploadFinalizeResponseV1.model_validate(
        authority.outcome.model_copy(update={"project": substituted_project}).model_dump(),
        strict=True,
    )

    with pytest.raises(ValueError, match="finalize request digest"):
        replace(authority, request=substituted_request)
    with pytest.raises(ValueError, match="finalize outcome digest"):
        replace(authority, outcome=substituted_outcome)
    with pytest.raises(ValueError, match="finalize request digest"):
        replace(authority, request_sha256=authority.outcome_sha256)
    with pytest.raises(ValueError, match="finalize outcome digest"):
        replace(authority, outcome_sha256=authority.request_sha256)


def test_initial_finalize_recovery_rejects_request_substitution() -> None:
    project, archive, persistence, fake_core = _prepare_initial_import_finalize_mapping_crash()
    operation = persistence.operation
    assert operation is not None
    authority = operation.workspace_upload_finalize
    assert authority is not None
    substituted_request = core_v1.WorkspaceUploadFinalizeV1(content_sha256="9" * 64)
    substituted_authority = _unsafe_dataclass_replace(
        authority,
        request=substituted_request,
    )
    persistence.operation = _unsafe_dataclass_replace(
        operation,
        workspace_upload_finalize=substituted_authority,
    )
    patch_count = len(fake_core.patch_requests)
    workspace_mutations = _workspace_mutation_count(fake_core)
    commit_count = persistence.events.count("commit_mapping")
    recovered, _, _, _ = _bridge(
        project,
        persistence=persistence,
        fake_core=fake_core,
        archive_source=FakeArchiveSource(archive),
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        recovered.activate_project(
            project,
            idempotency_key="initial-finalize-request-substitution-0002",
        )

    assert exc_info.value.error.code == "workspace_finalize_authority_mismatch"
    assert persistence.mapping is None
    assert len(fake_core.patch_requests) == patch_count
    assert _workspace_mutation_count(fake_core) == workspace_mutations
    assert persistence.events.count("commit_mapping") == commit_count


def test_initial_finalize_recovery_rejects_record_without_outcome_binding() -> None:
    project, archive, persistence, fake_core = _prepare_initial_import_finalize_mapping_crash()
    operation = persistence.operation
    assert operation is not None
    authority = operation.workspace_upload_finalize
    assert authority is not None
    unbound_authority = _unsafe_dataclass_replace(authority, outcome_sha256=None)
    persistence.operation = _unsafe_dataclass_replace(
        operation,
        workspace_upload_finalize=unbound_authority,
    )
    patch_count = len(fake_core.patch_requests)
    workspace_mutations = _workspace_mutation_count(fake_core)
    commit_count = persistence.events.count("commit_mapping")
    recovered, _, _, _ = _bridge(
        project,
        persistence=persistence,
        fake_core=fake_core,
        archive_source=FakeArchiveSource(archive),
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        recovered.activate_project(
            project,
            idempotency_key="initial-finalize-unbound-record-0002",
        )

    assert exc_info.value.error.code == "workspace_finalize_authority_mismatch"
    assert persistence.mapping is None
    assert len(fake_core.patch_requests) == patch_count
    assert _workspace_mutation_count(fake_core) == workspace_mutations
    assert persistence.events.count("commit_mapping") == commit_count


@pytest.mark.parametrize(
    "reported_revision",
    [
        REVISION,
        core_v1.RevisionRefV1(
            id="revision-1-rewritten",
            project_id=CORE_PROJECT_ID,
            generation=1,
            manifest_sha256=REVISION_1.manifest_sha256,
        ),
        core_v1.RevisionRefV1(
            id=REVISION_1.id,
            project_id=CORE_PROJECT_ID,
            generation=1,
            manifest_sha256="9" * 64,
        ),
        core_v1.RevisionRefV1(
            id="revision-3",
            project_id=CORE_PROJECT_ID,
            generation=3,
            manifest_sha256="3" * 64,
        ),
    ],
    ids=[
        "generation-rollback",
        "same-generation-id-rewrite",
        "same-generation-manifest-rewrite",
        "generation-jump",
    ],
)
def test_applied_imported_draft_recovery_uses_base_revision_authority_before_mutation(
    reported_revision: core_v1.RevisionRefV1,
) -> None:
    edited, archive, persistence, fake_core, mapping_o = _prepare_finalized_import_patch_crash(
        finalize_revision=REVISION_1
    )
    _replace_finalize_revision_authority(persistence, reported_revision)
    reported_etag = '"' + "9" * 64 + '"'
    fake_core.active_revision = reported_revision
    fake_core.project_etag = reported_etag
    fake_core.project_updated_at = "2026-07-14T12:46:00Z"
    fake_core.head = _head(active_revision=reported_revision, etag=reported_etag)
    workspace_mutations = _workspace_mutation_count(fake_core)
    history_count = len(persistence.mapping_history)
    recovered, _, _, _ = _bridge(
        edited,
        persistence=persistence,
        fake_core=fake_core,
        archive_source=FakeArchiveSource(archive),
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        recovered.activate_project(
            edited,
            idempotency_key="applied-draft-revision-recovery-0003",
        )

    assert exc_info.value.error.code == "core_project_revision_authority_mismatch"
    assert "durable applied patch outcome" in exc_info.value.error.message
    assert persistence.mapping == mapping_o
    assert persistence.mapping.project_etag != reported_etag
    assert len(persistence.mapping_history) == history_count
    assert _workspace_mutation_count(fake_core) == workspace_mutations


@pytest.mark.parametrize(
    ("base_revision", "finalize_revision", "reported_revision"),
    [
        (REVISION_1, REVISION, REVISION_1),
        (
            REVISION_1,
            core_v1.RevisionRefV1(
                id="revision-1-rewritten",
                project_id=CORE_PROJECT_ID,
                generation=1,
                manifest_sha256=REVISION_1.manifest_sha256,
            ),
            core_v1.RevisionRefV1(
                id="revision-2",
                project_id=CORE_PROJECT_ID,
                generation=2,
                manifest_sha256="2" * 64,
            ),
        ),
        (
            REVISION_1,
            core_v1.RevisionRefV1(
                id=REVISION_1.id,
                project_id=CORE_PROJECT_ID,
                generation=1,
                manifest_sha256="9" * 64,
            ),
            core_v1.RevisionRefV1(
                id="revision-2",
                project_id=CORE_PROJECT_ID,
                generation=2,
                manifest_sha256="2" * 64,
            ),
        ),
        (
            REVISION,
            core_v1.RevisionRefV1(
                id="revision-2",
                project_id=CORE_PROJECT_ID,
                generation=2,
                manifest_sha256="2" * 64,
            ),
            REVISION_1,
        ),
    ],
    ids=[
        "generation-rollback",
        "same-generation-id-rewrite",
        "same-generation-manifest-rewrite",
        "generation-jump",
    ],
)
def test_workspace_finalize_recovery_uses_effective_patch_predecessor(
    base_revision: core_v1.RevisionRefV1,
    finalize_revision: core_v1.RevisionRefV1,
    reported_revision: core_v1.RevisionRefV1,
) -> None:
    edited, archive, persistence, fake_core, mapping_o = _prepare_finalized_import_patch_crash(
        finalize_revision=base_revision
    )
    _replace_finalize_revision_authority(persistence, finalize_revision)
    reported_etag = '"' + "9" * 64 + '"'
    fake_core.active_revision = reported_revision
    fake_core.project_etag = reported_etag
    fake_core.project_updated_at = "2026-07-14T12:46:00Z"
    fake_core.head = _head(active_revision=reported_revision, etag=reported_etag)
    workspace_mutations = _workspace_mutation_count(fake_core)
    history_count = len(persistence.mapping_history)
    recovered, _, _, _ = _bridge(
        edited,
        persistence=persistence,
        fake_core=fake_core,
        archive_source=FakeArchiveSource(archive),
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        recovered.activate_project(
            edited,
            idempotency_key="finalize-predecessor-recovery-0003",
        )

    assert exc_info.value.error.code == "core_project_revision_authority_mismatch"
    assert "durable workspace finalize predecessor" in exc_info.value.error.message
    assert persistence.mapping == mapping_o
    assert persistence.mapping.project_etag != reported_etag
    assert len(persistence.mapping_history) == history_count
    assert _workspace_mutation_count(fake_core) == workspace_mutations


def test_empty_revision_authority_accepts_none_and_same_project_genesis() -> None:
    bridge_module._ensure_revision_authority_successor(
        None,
        None,
        project_id=CORE_PROJECT_ID,
        label="empty project revision",
    )
    bridge_module._ensure_revision_authority_successor(
        None,
        REVISION,
        project_id=CORE_PROJECT_ID,
        label="first project revision",
    )


@pytest.mark.parametrize(
    "reported_revision",
    [
        REVISION_1,
        core_v1.RevisionRefV1(
            id="other-project-revision-0",
            project_id="other-core-project",
            generation=0,
            manifest_sha256="8" * 64,
        ),
    ],
    ids=["generation-jump", "wrong-project-genesis"],
)
def test_empty_revision_authority_rejects_non_genesis_or_foreign_identity(
    reported_revision: core_v1.RevisionRefV1,
) -> None:
    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        bridge_module._ensure_revision_authority_successor(
            None,
            reported_revision,
            project_id=CORE_PROJECT_ID,
            label="first project revision",
        )

    assert exc_info.value.error.code == "core_project_revision_authority_mismatch"


@pytest.mark.parametrize(
    ("finalize_revision", "reported_revision"),
    [
        (
            REVISION_1,
            REVISION,
        ),
        (
            REVISION,
            core_v1.RevisionRefV1(
                id="revision-0-rewritten",
                project_id=CORE_PROJECT_ID,
                generation=0,
                manifest_sha256="9" * 64,
            ),
        ),
    ],
    ids=["generation-rollback", "same-generation-identity-rewrite"],
)
def test_finalize_after_crash_rejects_nonmonotonic_revision_authority(
    finalize_revision: core_v1.RevisionRefV1,
    reported_revision: core_v1.RevisionRefV1,
) -> None:
    edited, archive, persistence, fake_core, mapping_o = _prepare_finalized_import_patch_crash(
        finalize_revision=finalize_revision
    )
    fake_core.active_revision = reported_revision
    reported_etag = '"' + "9" * 64 + '"'
    fake_core.project_etag = reported_etag
    fake_core.project_updated_at = "2026-07-14T12:46:00Z"
    fake_core.head = _head(active_revision=reported_revision, etag=reported_etag)
    patch_count = len(fake_core.patch_requests)
    history_count = len(persistence.mapping_history)
    recovered, _, _, _ = _bridge(
        edited,
        persistence=persistence,
        fake_core=fake_core,
        archive_source=FakeArchiveSource(archive),
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        recovered.activate_project(
            edited,
            idempotency_key="finalize-revision-recovery-0003",
        )

    assert exc_info.value.error.code == "core_project_revision_authority_mismatch"
    assert persistence.mapping == mapping_o
    assert len(persistence.mapping_history) == history_count
    assert len(fake_core.patch_requests) == patch_count


def test_finalize_after_crash_accepts_direct_revision_successor() -> None:
    edited, archive, persistence, fake_core, mapping_o = _prepare_finalized_import_patch_crash(
        finalize_revision=REVISION
    )
    successor = _successor_revision(
        mapping_o.active_revision,
        project_snapshot=fake_core.project_snapshot,
        task_snapshot=fake_core.task_snapshot,
        workspace_snapshot=fake_core.workspace_snapshot,
        registry_digest=fake_core.registry_digest,
        revision_id="revision-1",
    )
    successor_etag = '"' + "7" * 64 + '"'
    fake_core.active_revision = successor
    fake_core.project_etag = successor_etag
    fake_core.project_updated_at = "2026-07-14T12:46:00Z"
    fake_core.head = _head(
        active_revision=successor,
        etag=successor_etag,
        updated_at=fake_core.project_updated_at,
    )
    recovered, _, _, _ = _bridge(
        edited,
        persistence=persistence,
        fake_core=fake_core,
        archive_source=FakeArchiveSource(archive),
    )

    activation = recovered.activate_project(
        edited,
        idempotency_key="finalize-revision-successor-0003",
    )

    assert activation.core_project.active_revision == successor
    assert persistence.mapping is not None
    assert persistence.mapping.active_revision == successor
    assert persistence.mapping.mapping_generation == mapping_o.mapping_generation + 1
    assert persistence.patch_operation is None


def test_finalize_recovery_rejects_same_timestamp_etag_rewrite_before_mutation() -> None:
    edited, archive, persistence, fake_core, mapping_o = _prepare_finalized_import_patch_crash(
        finalize_revision=REVISION
    )
    operation = persistence.operation
    assert operation is not None
    finalize_authority = operation.workspace_upload_finalize
    assert finalize_authority is not None
    finalized_project = finalize_authority.outcome.project
    rewritten_etag = '"' + "6" * 64 + '"'
    assert rewritten_etag != finalized_project.etag
    fake_core.active_revision = finalized_project.active_revision
    fake_core.project_etag = rewritten_etag
    fake_core.project_updated_at = finalized_project.updated_at
    fake_core.head = _head(
        active_revision=finalized_project.active_revision,
        etag=rewritten_etag,
    )
    patch_count = len(fake_core.patch_requests)
    workspace_mutations = _workspace_mutation_count(fake_core)
    history_count = len(persistence.mapping_history)
    recovered, _, _, _ = _bridge(
        edited,
        persistence=persistence,
        fake_core=fake_core,
        archive_source=FakeArchiveSource(archive),
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        recovered.activate_project(
            edited,
            idempotency_key="finalize-same-time-recovery-0003",
        )

    assert exc_info.value.error.code == "core_project_patch_outcome_mismatch"
    assert exc_info.value.error.http_status == 409
    assert persistence.mapping == mapping_o
    assert len(persistence.mapping_history) == history_count
    assert len(fake_core.patch_requests) == patch_count
    assert _workspace_mutation_count(fake_core) == workspace_mutations


def test_finalize_after_crash_rejects_unproven_two_edge_revision_chain() -> None:
    edited, archive, persistence, fake_core, mapping_o = _prepare_finalized_import_patch_crash(
        finalize_revision=REVISION
    )
    _replace_finalize_revision_authority(persistence, REVISION_1)
    successor = core_v1.RevisionRefV1(
        id="revision-2",
        project_id=CORE_PROJECT_ID,
        generation=2,
        manifest_sha256="2" * 64,
    )
    successor_etag = '"' + "2" * 64 + '"'
    fake_core.active_revision = successor
    fake_core.project_etag = successor_etag
    fake_core.project_updated_at = "2026-07-14T12:47:00Z"
    fake_core.head = _head(active_revision=successor, etag=successor_etag)
    recovered, _, _, _ = _bridge(
        edited,
        persistence=persistence,
        fake_core=fake_core,
        archive_source=FakeArchiveSource(archive),
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as raised:
        recovered.activate_project(
            edited,
            idempotency_key="finalize-revision-chain-0003",
        )

    assert raised.value.error.code == "core_project_successor_history_unavailable"
    assert persistence.mapping == mapping_o
    assert persistence.patch_operation is not None


def test_durable_revision_chain_rejects_tampered_finalize_outcome() -> None:
    edited, archive, persistence, fake_core, mapping_o = _prepare_finalized_import_patch_crash(
        finalize_revision=REVISION
    )
    _replace_finalize_revision_authority(persistence, REVISION_1)
    operation = persistence.operation
    assert operation is not None
    authority = operation.workspace_upload_finalize
    assert authority is not None
    tampered_snapshot = _snapshot(
        "tampered-finalize-project-snapshot",
        core_v1.SnapshotKind.PROJECT,
        "9",
    )
    tampered_project = core_v1.ProjectV1.model_validate(
        authority.outcome.project.model_copy(
            update={"current_project_snapshot": tampered_snapshot}
        ).model_dump(),
        strict=True,
    )
    tampered_outcome = core_v1.WorkspaceUploadFinalizeResponseV1.model_validate(
        authority.outcome.model_copy(update={"project": tampered_project}).model_dump(),
        strict=True,
    )
    _replace_finalize_outcome_authority(persistence, tampered_outcome)
    successor = core_v1.RevisionRefV1(
        id="revision-2",
        project_id=CORE_PROJECT_ID,
        generation=2,
        manifest_sha256="2" * 64,
    )
    successor_etag = '"' + "2" * 64 + '"'
    fake_core.active_revision = successor
    fake_core.project_etag = successor_etag
    fake_core.project_updated_at = "2026-07-14T12:47:00Z"
    fake_core.head = _head(active_revision=successor, etag=successor_etag)
    history_count = len(persistence.mapping_history)
    workspace_mutations = _workspace_mutation_count(fake_core)
    recovered, _, _, _ = _bridge(
        edited,
        persistence=persistence,
        fake_core=fake_core,
        archive_source=FakeArchiveSource(archive),
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        recovered.activate_project(
            edited,
            idempotency_key="finalize-tampered-outcome-0003",
        )

    assert exc_info.value.error.code == "workspace_finalize_authority_mismatch"
    assert persistence.mapping == mapping_o
    assert persistence.mapping.project_etag != successor_etag
    assert len(persistence.mapping_history) == history_count
    assert _workspace_mutation_count(fake_core) == workspace_mutations


@pytest.mark.parametrize(
    "reported_revision",
    [
        REVISION,
        core_v1.RevisionRefV1(
            id="revision-1-rewritten",
            project_id=CORE_PROJECT_ID,
            generation=1,
            manifest_sha256=REVISION_1.manifest_sha256,
        ),
        core_v1.RevisionRefV1(
            id=REVISION_1.id,
            project_id=CORE_PROJECT_ID,
            generation=1,
            manifest_sha256="9" * 64,
        ),
    ],
    ids=["rollback", "same-generation-id-rewrite", "same-generation-manifest-rewrite"],
)
def test_durable_revision_chain_rejects_nonmonotonic_current_authority(
    reported_revision: core_v1.RevisionRefV1,
) -> None:
    edited, archive, persistence, fake_core, mapping_o = _prepare_finalized_import_patch_crash(
        finalize_revision=REVISION
    )
    _replace_finalize_revision_authority(persistence, REVISION_1)
    reported_etag = '"' + "9" * 64 + '"'
    fake_core.active_revision = reported_revision
    fake_core.project_etag = reported_etag
    fake_core.project_updated_at = "2026-07-14T12:47:00Z"
    fake_core.head = _head(active_revision=reported_revision, etag=reported_etag)
    history_count = len(persistence.mapping_history)
    workspace_mutations = _workspace_mutation_count(fake_core)
    recovered, _, _, _ = _bridge(
        edited,
        persistence=persistence,
        fake_core=fake_core,
        archive_source=FakeArchiveSource(archive),
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        recovered.activate_project(
            edited,
            idempotency_key="finalize-nonmonotonic-current-0003",
        )

    assert exc_info.value.error.code == "core_project_revision_authority_mismatch"
    assert persistence.mapping == mapping_o
    assert persistence.mapping.project_etag != reported_etag
    assert len(persistence.mapping_history) == history_count
    assert _workspace_mutation_count(fake_core) == workspace_mutations


def test_finalized_import_patch_recovery_commits_a_then_patches_b_from_latest_authority() -> None:
    original = _local_project()
    first, persistence, fake_core, _ = _bridge(original)
    first.activate_project(original, idempotency_key="finalize-chain-o-0001")
    first.close()
    mapping_o = persistence.mapping
    assert mapping_o is not None

    archive_a = b"\1" * 1024
    source_a = local_v1.ProjectSourceV1(
        kind="native_folder_snapshot",
        display_name="Imported workspace A",
        import_ref=local_v1.WorkspaceImportRefV1(
            import_id="adopted-finalize-a",
            content_sha256=hashlib.sha256(archive_a).hexdigest(),
            byte_size=len(archive_a),
            entry_count=0,
            extracted_byte_size=0,
        ),
    )
    edited_a = original.model_copy(
        update={"source": source_a, "updated_at": "2026-07-14T12:50:00Z"}
    )
    persistence.fail_commit_once = True
    second, _, _, _ = _bridge(
        edited_a,
        persistence=persistence,
        fake_core=fake_core,
        archive_source=FakeArchiveSource(archive_a),
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        second.activate_project(edited_a, idempotency_key="finalize-chain-a-0002")

    assert exc_info.value.error.code == "core_bridge_adapter_failed"
    assert persistence.mapping == mapping_o
    assert persistence.patch_operation is not None
    assert persistence.patch_operation.state is CoreProjectPatchStateV1.APPLIED
    assert persistence.patch_operation.outcome is not None
    assert persistence.patch_operation.outcome.workspace_publication is None
    assert fake_core.upload is not None
    assert fake_core.upload.status is core_v1.WorkspaceUploadStatus.FINALIZED
    finalized_publication = fake_core.upload.publication
    assert finalized_publication is not None
    assert persistence.operation is not None
    assert persistence.operation.workspace_upload_finalize is not None
    assert (
        persistence.operation.workspace_upload_finalize.outcome.publication
        == finalized_publication
    )
    with pytest.raises(ValueError, match="finalize authority"):
        replace(
            persistence.operation,
            workspace_upload_finalize=replace(
                persistence.operation.workspace_upload_finalize,
                idempotency_key="tampered-workspace-finalize-key",
            ),
        )

    finalized_project_snapshot = fake_core.project_snapshot
    fake_core.project_snapshot = _snapshot(
        "project-snapshot-unproven-after-finalize",
        core_v1.SnapshotKind.PROJECT,
        "6",
    )
    unproven, _, _, _ = _bridge(
        edited_a,
        persistence=persistence,
        fake_core=fake_core,
        archive_source=FakeArchiveSource(archive_a),
    )
    with pytest.raises(DesktopCoreBridgeErrorV1) as unproven_exc:
        unproven.activate_project(
            edited_a,
            idempotency_key="finalize-chain-unproven-0003",
        )
    assert unproven_exc.value.error.code == "core_project_patch_outcome_mismatch"
    fake_core.project_snapshot = finalized_project_snapshot

    successor = _successor_revision(
        mapping_o.active_revision,
        project_snapshot=fake_core.project_snapshot,
        task_snapshot=fake_core.task_snapshot,
        workspace_snapshot=fake_core.workspace_snapshot,
        registry_digest="8" * 64,
        revision_id="revision-after-finalize",
    )
    successor_etag = '"' + "7" * 64 + '"'
    fake_core.active_revision = successor
    fake_core.project_etag = successor_etag
    fake_core.registry_digest = "8" * 64
    fake_core.project_updated_at = "2026-07-14T12:51:00Z"
    fake_core.head = core_v1.RevisionHeadV1(
        project_id=CORE_PROJECT_ID,
        active_revision=successor,
        successor_revision=None,
        transition=None,
        updated_at="2026-07-14T12:51:00Z",
        etag=successor_etag,
    )
    third, _, _, _ = _bridge(
        edited_a,
        persistence=persistence,
        fake_core=fake_core,
        archive_source=FakeArchiveSource(archive_a),
    )

    recovered_a = third.activate_project(
        edited_a,
        idempotency_key="finalize-chain-a-retry-0003",
    )
    third.close()

    mapping_a = persistence.mapping
    assert mapping_a is not None
    assert recovered_a.core_project.status is core_v1.ProjectStatus.READY
    assert recovered_a.core_project.workspace_publication == finalized_publication
    assert mapping_a.request_sha256 == bridge_module._model_digest(map_project_create_v1(edited_a))
    assert mapping_a.project_snapshot == recovered_a.core_project.current_project_snapshot
    assert mapping_a.workspace_snapshot == finalized_publication.workspace_snapshot
    assert mapping_a.project_etag == successor_etag
    assert mapping_a.active_revision == successor
    assert mapping_a.registry_digest == "8" * 64
    assert mapping_a.mapping_generation == mapping_o.mapping_generation + 1
    assert persistence.patch_operation is None

    archive_b = b"\2" * 1024
    source_b = local_v1.ProjectSourceV1(
        kind="native_folder_snapshot",
        display_name="Imported workspace B",
        import_ref=local_v1.WorkspaceImportRefV1(
            import_id="adopted-finalize-b",
            content_sha256=hashlib.sha256(archive_b).hexdigest(),
            byte_size=len(archive_b),
            entry_count=0,
            extracted_byte_size=0,
        ),
    )
    edited_b = edited_a.model_copy(
        update={"source": source_b, "updated_at": "2026-07-14T12:52:00Z"}
    )
    fourth, _, _, _ = _bridge(
        edited_b,
        persistence=persistence,
        fake_core=fake_core,
        archive_source=FakeArchiveSource(archive_b),
    )
    recovered_b = fourth.activate_project(
        edited_b,
        idempotency_key="finalize-chain-b-0004",
    )

    assert len(fake_core.patch_requests) == 2
    assert fake_core.patch_requests[0][0].workspace == map_project_create_v1(edited_a).workspace
    assert fake_core.patch_requests[1][0].workspace == map_project_create_v1(edited_b).workspace
    assert fake_core.patch_requests[1][1] == successor_etag
    assert recovered_b.core_project.workspace == map_project_create_v1(edited_b).workspace
    assert persistence.mapping is not None
    assert persistence.mapping.mapping_generation == mapping_o.mapping_generation + 2
    assert [mapping.project_create for mapping in persistence.mapping_history] == [
        map_project_create_v1(original),
        map_project_create_v1(edited_a),
        map_project_create_v1(edited_b),
    ]


def test_core_503_is_preserved_without_synthetic_capability_success() -> None:
    local_project = _local_project()
    bridge, _, fake_core, _ = _bridge(local_project)
    fake_core.fail_capabilities_with_503 = True

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        bridge.activate_project(local_project, idempotency_key="activate-local-project-0001")

    assert exc_info.value.error.http_status == 503
    assert exc_info.value.error.code == "route_not_implemented"


def test_active_proxy_preserves_exact_core_api_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_project = _local_project()
    bridge, _, _, _ = _bridge(local_project)
    bridge.activate_project(local_project, idempotency_key="activate-error-boundary-0001")
    assert bridge._active is not None
    api_error = core_v1.ApiErrorV1.model_validate_json(
        json.dumps(_core_error(), separators=(",", ":"))
    )

    def fail_list_runs(**_kwargs: object) -> core_v1.RunPageV1:
        raise CoreClientErrorV1(api_error.http_status, api_error)

    monkeypatch.setattr(bridge._active.client, "list_runs", fail_list_runs)

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        bridge.list_runs(local_project)

    assert exc_info.value.error is api_error


def test_event_iteration_translates_core_client_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_project = _local_project()
    bridge, _, _, _ = _bridge(local_project)
    bridge.activate_project(local_project, idempotency_key="activate-event-error-0001")
    assert bridge._active is not None
    api_error = core_v1.ApiErrorV1.model_validate_json(
        json.dumps(_core_error(), separators=(",", ":"))
    )

    class FailingEventContext:
        def __enter__(self) -> FailingEventContext:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def __iter__(self) -> FailingEventContext:
            return self

        def __next__(self) -> core_v1.SseFrameV1:
            raise CoreClientErrorV1(api_error.http_status, api_error)

    monkeypatch.setattr(
        bridge._active.client,
        "events",
        lambda **_kwargs: FailingEventContext(),
    )

    with bridge.events(local_project) as events:
        with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
            next(events)

    assert exc_info.value.error is api_error


def test_switch_and_close_seal_old_client_before_new_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_project = _local_project()
    bridge, _, _, tunnels = _bridge(local_project)
    bridge.activate_project(local_project, idempotency_key="activate-local-project-0001")
    old_client = bridge._active.client
    started = threading.Event()
    release = threading.Event()

    def blocked_list_runs(**_kwargs: object) -> core_v1.RunPageV1:
        started.set()
        assert release.wait(timeout=2)
        return core_v1.RunPageV1(items=[], has_more=False)

    monkeypatch.setattr(old_client, "list_runs", blocked_list_runs)
    result: list[object] = []

    def read_runs() -> None:
        try:
            result.append(bridge.list_runs(local_project))
        except BaseException as exc:
            result.append(exc)

    worker = threading.Thread(target=read_runs)
    worker.start()
    assert started.wait(timeout=1)
    closer = threading.Thread(target=bridge.close)
    closer.start()
    time.sleep(0.05)
    assert closer.is_alive()
    release.set()
    worker.join(timeout=2)
    closer.join(timeout=2)

    assert len(tunnels.handles) == 1
    assert tunnels.handles[0].closed is True
    assert isinstance(result[0], DesktopCoreBridgeErrorV1)
    assert result[0].error.code == "active_project_session_superseded"


@pytest.mark.parametrize("transition", ["deactivate", "close", "activate"])
def test_activation_acknowledgement_is_linearized_with_session_transition(
    monkeypatch: pytest.MonkeyPatch,
    transition: str,
) -> None:
    local_project = _local_project()
    bridge, _, _, _ = _bridge(local_project)
    activation = bridge.activate_project(
        local_project,
        idempotency_key="activate-before-ack-race-0001",
    )
    committed = _committed_local_activation(local_project, activation)
    old_session = bridge._active
    assert old_session is not None
    entered = threading.Event()
    release = threading.Event()
    original_projection_check = bridge._ensure_local_activation_projection

    def blocked_projection_check(
        session: bridge_module.DesktopCoreActiveSessionV1,
        project: local_v1.ProjectV1,
    ) -> None:
        entered.set()
        assert release.wait(timeout=2)
        original_projection_check(session, project)

    monkeypatch.setattr(
        bridge,
        "_ensure_local_activation_projection",
        blocked_projection_check,
    )
    acknowledgement_result: list[object] = []
    transition_result: list[object] = []

    def acknowledge() -> None:
        try:
            bridge.commit_local_activation(committed, activation=activation)
            acknowledgement_result.append("committed")
        except BaseException as exc:
            acknowledgement_result.append(exc)

    def run_transition() -> None:
        try:
            if transition == "deactivate":
                bridge.deactivate_project(local_project.project_id)
                transition_result.append("deactivated")
            elif transition == "close":
                bridge.close()
                transition_result.append("closed")
            else:
                transition_result.append(
                    bridge.activate_project(
                        committed,
                        idempotency_key="activate-during-ack-race-0002",
                    )
                )
        except BaseException as exc:
            transition_result.append(exc)

    acknowledgement_thread = threading.Thread(target=acknowledge)
    acknowledgement_thread.start()
    assert entered.wait(timeout=1)
    transition_thread = threading.Thread(target=run_transition)
    transition_thread.start()
    time.sleep(0.05)
    assert transition_thread.is_alive()
    release.set()
    acknowledgement_thread.join(timeout=2)
    transition_thread.join(timeout=2)

    assert acknowledgement_result == ["committed"]
    assert old_session.local_project_etag == ETAG_B
    assert old_session.committed_local_project == committed
    assert not isinstance(transition_result[0], BaseException)
    if transition == "activate":
        assert isinstance(transition_result[0], bridge_module.CoreActivationV1)
        assert bridge._active is not None
        assert bridge._active.local_project_etag == ETAG_B
        bridge.close()


def test_new_activation_retires_an_inflight_candidate_before_starting_core_work() -> None:
    local_project = _local_project()
    bridge, _, fake_core, tunnels = _bridge(local_project)
    fake_core.block_method = "GET"
    fake_core.block_path = "/v1/capabilities"
    first_result: list[object] = []
    second_result: list[object] = []

    def activate(key: str, destination: list[object]) -> None:
        try:
            destination.append(bridge.activate_project(local_project, idempotency_key=key))
        except BaseException as exc:
            destination.append(exc)

    first = threading.Thread(
        target=activate,
        args=("candidate-first-action-0001", first_result),
    )
    first.start()
    assert fake_core.block_entered.wait(timeout=1)
    second = threading.Thread(
        target=activate,
        args=("candidate-second-action-0002", second_result),
    )
    second.start()
    time.sleep(0.05)
    assert second.is_alive()
    fake_core.block_release.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert isinstance(first_result[0], DesktopCoreBridgeErrorV1)
    assert isinstance(second_result[0], bridge_module.CoreActivationV1)
    create_calls = [
        request
        for request in fake_core.calls
        if request.method == "POST" and request.url.path == "/v1/projects"
    ]
    assert len(create_calls) == 1
    assert create_calls[0].headers["Idempotency-Key"] == "candidate-second-action-0002"
    assert len(tunnels.handles) == 2
    assert tunnels.handles[0].closed is True
    assert tunnels.handles[1].closed is False
    bridge.close()


def test_private_identity_is_rejected_before_proxy_transport() -> None:
    local_project = _local_project()
    bridge, _, fake_core, _ = _bridge(local_project)
    bridge.activate_project(local_project, idempotency_key="activate-local-project-0001")
    calls_before = len(fake_core.calls)
    bearer = bridge._active.attachment.bearer_token

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        bridge.get_run(local_project, bearer)

    assert exc_info.value.error.code == "invalid_core_request"
    assert len(fake_core.calls) == calls_before


def test_bodyless_local_actions_derive_closed_core_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_project = _local_project()
    bridge, _, _, _ = _bridge(local_project)
    bridge.activate_project(local_project, idempotency_key="activate-local-project-0001")
    client = bridge._active.client
    run = _run(
        core_v1.RunCreateV1(
            project_id=CORE_PROJECT_ID,
            project_snapshot=READY_PROJECT_SNAPSHOT,
            task_snapshot=TASK_SNAPSHOT,
            workspace_snapshot=WORKSPACE_SNAPSHOT,
            expected_registry_digest=REGISTRY_DIGEST,
            required_revision=core_v1.ReachableRequiredRevisionRefV1(
                revision=REVISION,
                reachable_from_revision_id=REVISION.id,
                relation=core_v1.RequiredRevisionRelation.ACTIVE,
            ),
        )
    )
    cancel_requests: list[core_v1.RunCancelRequestV1] = []
    retry_requests: list[tuple[core_v1.RunRetryRequestV1, str, str]] = []
    restart_requests: list[core_v1.ServiceRestartRequestV1] = []

    monkeypatch.setattr(client, "get_run", lambda *_args, **_kwargs: run)

    def cancel(
        _run_id: str, request: core_v1.RunCancelRequestV1, **_kwargs: object
    ) -> core_v1.RunV1:
        cancel_requests.append(request)
        return run

    def retry(
        _run_id: str, request: core_v1.RunRetryRequestV1, **_kwargs: object
    ) -> core_v1.RunV1:
        key = str(_kwargs["idempotency_key"])
        etag = str(_kwargs["if_match"])
        retry_requests.append((request, key, etag))
        if key != "retry-run-00000001" or etag != ETAG_A:
            status = 409 if key != "retry-run-00000001" else 412
            raise CoreClientErrorV1(
                status,
                core_v1.ApiErrorV1.model_validate_json(
                    json.dumps({**_core_error(), "http_status": status, "code": "retry_conflict"})
                ),
            )
        return run

    def restart(
        _service_id: str, request: core_v1.ServiceRestartRequestV1, **_kwargs: object
    ) -> core_v1.OperationV1:
        restart_requests.append(request)
        return object()  # type: ignore[return-value]

    monkeypatch.setattr(client, "cancel_run", cancel)
    bridge.cancel_run(
        local_project,
        "run-1",
        if_match=ETAG_A,
        idempotency_key="cancel-run-0000001",
    )
    monkeypatch.setattr(
        client,
        "get_run",
        lambda *_args, **_kwargs: pytest.fail("retry must not read the latest run attempt"),
    )
    monkeypatch.setattr(client, "retry_run", retry)
    retry_authority = local_v1.RunRetryV1(terminal_attempt_id="attempt-terminal-1")
    for _ in range(2):
        bridge.retry_run(
            local_project,
            "run-1",
            retry_authority,
            if_match=ETAG_A,
            idempotency_key="retry-run-00000001",
        )
    with pytest.raises(DesktopCoreBridgeErrorV1) as changed_key:
        bridge.retry_run(
            local_project,
            "run-1",
            retry_authority,
            if_match=ETAG_A,
            idempotency_key="retry-run-different-key-0002",
        )
    with pytest.raises(DesktopCoreBridgeErrorV1) as changed_etag:
        bridge.retry_run(
            local_project,
            "run-1",
            retry_authority,
            if_match=ETAG_B,
            idempotency_key="retry-run-00000001",
        )
    monkeypatch.setattr(client, "restart_service", restart)
    bridge.restart_service(
        local_project,
        "service-1",
        if_match=ETAG_A,
        idempotency_key="restart-service-0001",
    )

    assert cancel_requests == [
        core_v1.RunCancelRequestV1(reason=core_v1.RunCancelReason.USER_REQUESTED)
    ]
    expected_retry = core_v1.RunRetryRequestV1(terminal_attempt_id="attempt-terminal-1")
    assert retry_requests[:2] == [
        (expected_retry, "retry-run-00000001", ETAG_A),
        (expected_retry, "retry-run-00000001", ETAG_A),
    ]
    assert changed_key.value.error.http_status == 409
    assert changed_etag.value.error.http_status == 412
    assert restart_requests == [
        core_v1.ServiceRestartRequestV1(reason="Requested from OpenEvo Desktop.")
    ]


def test_project_doctor_and_repair_bind_execution_mode_and_action_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_project = _local_project()
    bridge, _, _, _ = _bridge(local_project)
    bridge.activate_project(local_project, idempotency_key="activate-local-project-0001")
    client = bridge._active.client
    doctor_requests: list[tuple[core_v1.EnvironmentDoctorRequestV1, str]] = []
    repair_requests: list[tuple[core_v1.EnvironmentRepairRequestV1, str]] = []
    doctor_response = core_v1.EnvironmentDoctorResponseV1(
        status=core_v1.DoctorStatus.OK,
        checks=[],
        checked_at=NOW,
    )
    repair_operation = object()

    def doctor(
        request: core_v1.EnvironmentDoctorRequestV1,
        *,
        idempotency_key: str,
    ) -> core_v1.EnvironmentDoctorResponseV1:
        doctor_requests.append((request, idempotency_key))
        return doctor_response

    def repair(
        request: core_v1.EnvironmentRepairRequestV1,
        *,
        idempotency_key: str,
    ) -> core_v1.OperationV1:
        repair_requests.append((request, idempotency_key))
        return repair_operation  # type: ignore[return-value]

    monkeypatch.setattr(client, "environment_doctor", doctor)
    monkeypatch.setattr(client, "environment_repair", repair)

    assert (
        bridge.doctor_project(
            local_project,
            idempotency_key="project-doctor-0000001",
        )
        == doctor_response
    )
    assert (
        bridge.repair_project(
            local_project,
            actions=(core_v1.EnvironmentRepairAction.REPAIR_REGISTRY_INSTALL,),
            idempotency_key="project-repair-0000001",
        )
        is repair_operation
    )

    assert doctor_requests == [
        (
            core_v1.EnvironmentDoctorRequestV1(
                execution_mode=core_v1.ExecutionMode.SELF_DEPLOYED,
                checks=[],
            ),
            "project-doctor-0000001",
        )
    ]
    assert repair_requests == [
        (
            core_v1.EnvironmentRepairRequestV1(
                execution_mode=core_v1.ExecutionMode.SELF_DEPLOYED,
                actions=[core_v1.EnvironmentRepairAction.REPAIR_REGISTRY_INSTALL],
            ),
            "project-repair-0000001",
        )
    ]


def test_retry_post_response_bridge_gate_loss_is_an_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_project = _local_project()
    bridge, _, _, _ = _bridge(local_project)
    bridge.activate_project(local_project, idempotency_key="activate-local-project-0001")
    client = bridge._active.client
    run = _run(
        core_v1.RunCreateV1(
            project_id=CORE_PROJECT_ID,
            project_snapshot=READY_PROJECT_SNAPSHOT,
            task_snapshot=TASK_SNAPSHOT,
            workspace_snapshot=WORKSPACE_SNAPSHOT,
            expected_registry_digest=REGISTRY_DIGEST,
            required_revision=core_v1.ReachableRequiredRevisionRefV1(
                revision=REVISION,
                reachable_from_revision_id=REVISION.id,
                relation=core_v1.RequiredRevisionRelation.ACTIVE,
            ),
        )
    )
    monkeypatch.setattr(client, "retry_run", lambda *_args, **_kwargs: run)
    original_gate = bridge._gate_token
    gate_calls = 0

    def lose_bridge_authority_after_response(
        token: bridge_module._GenerationToken,
        deadline: float,
    ) -> None:
        nonlocal gate_calls
        gate_calls += 1
        if gate_calls == 2:
            raise bridge_module._bridge_error(
                "active_project_session_superseded",
                "A newer active project session superseded this result.",
                retryable=True,
            )
        original_gate(token, deadline)

    monkeypatch.setattr(bridge, "_gate_token", lose_bridge_authority_after_response)

    with pytest.raises(CoreMutationOutcomeUnknownV1):
        bridge.retry_run(
            local_project,
            "run-1",
            local_v1.RunRetryV1(terminal_attempt_id="attempt-terminal-1"),
            if_match=ETAG_A,
            idempotency_key="retry-post-gate-loss-0001",
        )

    assert gate_calls == 2


def test_close_rejects_new_calls_and_is_idempotent() -> None:
    local_project = _local_project()
    bridge, _, _, _ = _bridge(local_project)
    bridge.activate_project(local_project, idempotency_key="activate-local-project-0001")

    bridge.close()
    bridge.close()

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        bridge.capabilities(local_project)
    assert exc_info.value.error.code == "desktop_core_bridge_closed"


def test_bearer_never_appears_in_attachment_or_session_repr() -> None:
    token = secrets.token_urlsafe(32)
    attachment = CoreHostAttachmentV1(
        profile_id=PROFILE_ID,
        remote_port=43117,
        bearer_token=token,
        bearer_identity="core-host-key-1",
    )

    assert token not in repr(attachment)
    assert token not in str(attachment)

    local_project = _local_project()
    bridge, _, _, _ = _bridge(local_project)
    bridge.activate_project(local_project, idempotency_key="repr-activation-0001")
    assert bridge._active is not None
    session_token = bridge._active.attachment.bearer_token
    assert session_token not in repr(bridge._active)
    assert session_token not in repr(bridge._active.tunnel)
    bridge.close()


def test_injected_adapter_exception_does_not_leak_path_or_error_detail() -> None:
    local_project = _local_project()
    tunnels = FakeTunnelFactory()
    private_detail = "/home/private-user/.ssh/id_ed25519"

    def leaky_transport_factory() -> httpx.BaseTransport:
        raise TimeoutError(private_detail)

    bridge = DesktopCoreBridgeV1(
        host_service=FakeHostService(),
        tunnel_factory=tunnels,
        persistence=FakePersistence(),
        archive_source=FakeArchiveSource(),
        transport_factory=leaky_transport_factory,
        timeout=1.0,
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        bridge.activate_project(
            local_project,
            idempotency_key="leaky-adapter-activation-0001",
        )

    assert exc_info.value.error.code == "core_bridge_adapter_failed"
    assert private_detail not in str(exc_info.value)
    assert private_detail not in repr(exc_info.value)
    assert private_detail not in exc_info.value.error.model_dump_json()
    assert len(tunnels.handles) == 1
    assert tunnels.handles[0].closed is True


@pytest.mark.parametrize("error_type", [RuntimeError, TimeoutError])
def test_tunnel_close_failure_is_observable_and_retryable(
    error_type: type[Exception],
) -> None:
    attempts = 0

    def close_callback() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise error_type("injected close failure")

    handle = CoreTunnelHandleV1(
        endpoint="http://127.0.0.1:48765",
        session_id="session-close-test",
        close_callback=close_callback,
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        handle.close(deadline=bridge_module.time.monotonic() + 1)

    assert exc_info.value.error.code == "core_tunnel_close_failed"
    assert handle.closed is False
    assert handle.close_failure == "callback_failed"

    handle.close(deadline=bridge_module.time.monotonic() + 1)
    assert handle.closed is True
    assert handle.close_failure is None
    assert attempts == 2


def test_tunnel_close_timeout_boundary_consumes_a_completed_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    class BoundaryFuture(Future[None]):
        def __init__(self) -> None:
            super().__init__()
            self._boundary = True

        def result(self, timeout: float | None = None) -> None:
            if self._boundary:
                self._boundary = False
                raise FutureTimeoutError
            return super().result(timeout=timeout)

    def submit(action):
        nonlocal attempts
        future = BoundaryFuture()
        action()
        attempts += 1
        future.set_result(None)
        return future

    monkeypatch.setattr(bridge_module._ADAPTER_EXECUTOR, "submit", submit)
    handle = CoreTunnelHandleV1(
        endpoint="http://127.0.0.1:48766",
        session_id="session-close-boundary",
        close_callback=lambda: None,
    )

    handle.close(deadline=bridge_module.time.monotonic() + 1)
    handle.close(deadline=bridge_module.time.monotonic() + 1)

    assert handle.closed is True
    assert handle.close_failure is None
    assert attempts == 1


def test_tunnel_close_deadline_after_submit_retains_and_reuses_the_same_future(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback_attempts = 0
    submit_attempts = 0
    submitted: list[tuple[Future[None], object]] = []
    remaining_calls = 0

    def close_callback() -> None:
        nonlocal callback_attempts
        callback_attempts += 1

    def submit(action):
        nonlocal submit_attempts
        submit_attempts += 1
        future: Future[None] = Future()
        submitted.append((future, action))
        return future

    def remaining(_deadline: float) -> float:
        nonlocal remaining_calls
        remaining_calls += 1
        if remaining_calls == 1:
            raise bridge_module._bridge_error(
                "desktop_core_bridge_deadline_exceeded",
                "The Desktop Core bridge operation deadline expired.",
                retryable=True,
            )
        return 1.0

    monkeypatch.setattr(bridge_module._ADAPTER_EXECUTOR, "submit", submit)
    monkeypatch.setattr(bridge_module, "_remaining_seconds", remaining)
    handle = CoreTunnelHandleV1(
        endpoint="http://127.0.0.1:48767",
        session_id="session-close-post-submit-deadline",
        close_callback=close_callback,
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        handle.close(deadline=1.0)

    assert exc_info.value.error.code == "desktop_core_bridge_deadline_exceeded"
    assert handle.closed is False
    assert handle.close_failure == "deadline_exceeded"
    assert submit_attempts == 1
    future, action = submitted[0]
    action()
    future.set_result(None)

    handle.close(deadline=2.0)

    assert handle.closed is True
    assert handle.close_failure is None
    assert submit_attempts == 1
    assert callback_attempts == 1


def test_bridge_close_is_bounded_and_not_announced_until_tunnel_closes() -> None:
    local_project = _local_project()
    bridge, _, _, tunnels = _bridge(local_project, timeout=0.1)
    bridge.activate_project(local_project, idempotency_key="bounded-close-activate-0001")
    release = threading.Event()
    handle = tunnels.handles[0]
    handle._close_callback = lambda: release.wait(timeout=2)

    started = time.monotonic()
    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        bridge.close()
    elapsed = time.monotonic() - started

    assert exc_info.value.error.code == "core_tunnel_close_deadline_exceeded"
    assert elapsed < 0.75
    assert bridge._closed is False
    assert handle.closed is False
    assert handle.close_failure == "deadline_exceeded"

    release.set()
    bridge.close()
    assert bridge._closed is True
    assert handle.closed is True


def test_tunnel_returning_after_adapter_deadline_is_adopted_and_closed() -> None:
    local_project = _local_project()
    bridge, _, _, tunnels = _bridge(local_project, timeout=0.1)
    tunnels.block_open = True
    release_timer = threading.Timer(0.15, tunnels.open_release.set)
    release_timer.start()
    try:
        with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
            bridge.activate_project(
                local_project,
                idempotency_key="late-tunnel-activate-0001",
            )
    finally:
        release_timer.cancel()

    assert exc_info.value.error.code == "core_bridge_adapter_deadline_exceeded"
    assert tunnels.open_entered.is_set()
    assert len(tunnels.handles) == 1
    assert tunnels.handles[0].closed is True
    assert bridge._active is None


def test_client_returning_after_adapter_deadline_is_adopted_and_closed() -> None:
    class TrackingTransport(httpx.BaseTransport):
        def __init__(self) -> None:
            self.closed = False

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"late client performed transport: {request.url.path}")

        def close(self) -> None:
            self.closed = True

    local_project = _local_project()
    tunnels = FakeTunnelFactory()
    release = threading.Event()
    transport = TrackingTransport()

    def delayed_transport_factory() -> httpx.BaseTransport:
        assert release.wait(timeout=2)
        return transport

    bridge = DesktopCoreBridgeV1(
        host_service=FakeHostService(),
        tunnel_factory=tunnels,
        persistence=FakePersistence(),
        archive_source=FakeArchiveSource(),
        transport_factory=delayed_transport_factory,
        timeout=0.1,
    )
    release_timer = threading.Timer(0.15, release.set)
    release_timer.start()
    try:
        with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
            bridge.activate_project(
                local_project,
                idempotency_key="late-client-activate-0001",
            )
    finally:
        release_timer.cancel()

    assert exc_info.value.error.code == "core_bridge_adapter_deadline_exceeded"
    assert transport.closed is True
    assert len(tunnels.handles) == 1
    assert tunnels.handles[0].closed is True
    assert bridge._active is None


def test_failed_tunnel_retirement_blocks_switch_until_a_retry_succeeds() -> None:
    local_project = _local_project()
    bridge, _, _, tunnels = _bridge(local_project)
    bridge.activate_project(local_project, idempotency_key="switch-close-base-0001")
    attempts = 0

    def close_callback() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("injected switch close failure")

    tunnels.handles[0]._close_callback = close_callback

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        bridge.activate_project(local_project, idempotency_key="switch-close-failed-0002")

    assert exc_info.value.error.code == "core_tunnel_close_failed"
    assert len(tunnels.handles) == 1
    assert bridge._active is not None
    assert bridge._active.token.cancelled is True

    activation = bridge.activate_project(
        local_project,
        idempotency_key="switch-close-retry-0003",
    )
    assert activation.local_project_id == LOCAL_PROJECT_ID
    assert attempts == 2
    assert len(tunnels.handles) == 2
    assert tunnels.handles[0].closed is True
    bridge.close()


@pytest.mark.parametrize(
    ("imported", "method", "path", "forbidden_path"),
    [
        (False, "GET", "/v1/capabilities", "/v1/projects"),
        (False, "POST", "/v1/projects", "/v1/projects/core-project-1"),
        (
            True,
            "PUT",
            "/v1/projects/core-project-1/workspace-uploads/upload-1/chunk",
            "/v1/projects/core-project-1/workspace-uploads/upload-1/finalize",
        ),
        (
            False,
            "POST",
            "/v1/projects/core-project-1/validate",
            "/never-after-validation",
        ),
    ],
)
def test_close_seals_inflight_candidate_before_any_later_core_mutation(
    imported: bool,
    method: str,
    path: str,
    forbidden_path: str,
) -> None:
    local_project = _local_project(imported=imported)
    bridge, persistence, fake_core, tunnels = _bridge(local_project)
    fake_core.block_method = method
    fake_core.block_path = path
    activation_result: list[object] = []
    close_result: list[object] = []

    def activate() -> None:
        try:
            activation_result.append(
                bridge.activate_project(
                    local_project,
                    idempotency_key="candidate-close-race-0001",
                )
            )
        except BaseException as exc:
            activation_result.append(exc)

    def close() -> None:
        try:
            bridge.close()
            close_result.append("closed")
        except BaseException as exc:
            close_result.append(exc)

    activation_thread = threading.Thread(target=activate)
    activation_thread.start()
    assert fake_core.block_entered.wait(timeout=1)
    close_thread = threading.Thread(target=close)
    close_thread.start()
    time.sleep(0.05)
    assert close_thread.is_alive()
    fake_core.block_release.set()
    activation_thread.join(timeout=2)
    close_thread.join(timeout=2)

    assert close_result == ["closed"]
    assert isinstance(activation_result[0], DesktopCoreBridgeErrorV1)
    assert all(request.url.path != forbidden_path for request in fake_core.calls)
    if path.endswith("/validate"):
        assert persistence.mapping is None
    assert all(handle.closed for handle in tunnels.handles)


def test_close_waits_for_blocking_host_adapter_and_prevents_core_calls() -> None:
    local_project = _local_project()
    bridge, _, fake_core, _ = _bridge(local_project)
    host = bridge._host_service
    assert isinstance(host, FakeHostService)
    host.block = True
    activation_result: list[object] = []

    def activate() -> None:
        try:
            activation_result.append(
                bridge.activate_project(
                    local_project,
                    idempotency_key="blocked-host-activate-0001",
                )
            )
        except BaseException as exc:
            activation_result.append(exc)

    activation_thread = threading.Thread(target=activate)
    activation_thread.start()
    assert host.entered.wait(timeout=1)
    close_thread = threading.Thread(target=bridge.close)
    close_thread.start()
    time.sleep(0.05)
    assert close_thread.is_alive()
    host.release.set()
    activation_thread.join(timeout=2)
    close_thread.join(timeout=2)

    assert fake_core.calls == []
    assert isinstance(activation_result[0], DesktopCoreBridgeErrorV1)


def test_cancel_activation_requires_exact_identity_and_rejects_late_success() -> None:
    local_project = _local_project()
    bridge, _, fake_core, _ = _bridge(local_project)
    host = bridge._host_service
    assert isinstance(host, FakeHostService)
    host.block = True
    cancel_event = threading.Event()
    activation_result: list[object] = []

    def activate() -> None:
        try:
            activation_result.append(
                bridge.activate_project(
                    local_project,
                    idempotency_key="cancel-exact-activation-0001",
                    activation_id="operation-exact-activation-0001",
                    cancel_event=cancel_event,
                )
            )
        except BaseException as exc:
            activation_result.append(exc)

    activation_thread = threading.Thread(target=activate)
    activation_thread.start()
    assert host.entered.wait(timeout=1)

    assert not bridge.cancel_activation("operation-wrong-activation-0001")
    assert not cancel_event.is_set()
    assert bridge.cancel_activation("operation-exact-activation-0001")
    assert cancel_event.is_set()
    host.release.set()
    activation_thread.join(timeout=2)

    assert not activation_thread.is_alive()
    assert len(activation_result) == 1
    assert isinstance(activation_result[0], DesktopCoreBridgeErrorV1)
    assert activation_result[0].error.code == "active_project_session_superseded"
    assert fake_core.calls == []
    assert not bridge.cancel_activation("operation-exact-activation-0001")

    host.block = False
    activation = bridge.activate_project(
        local_project,
        idempotency_key="cancel-exact-activation-retry-0002",
    )
    assert activation.local_project_id == local_project.project_id


def test_close_waits_for_blocking_persistence_commit_before_returning() -> None:
    local_project = _local_project()
    persistence = FakePersistence()
    persistence.block_commit = True
    bridge, _, fake_core, tunnels = _bridge(
        local_project,
        persistence=persistence,
    )
    activation_result: list[object] = []
    close_result: list[object] = []

    def activate() -> None:
        try:
            activation_result.append(
                bridge.activate_project(
                    local_project,
                    idempotency_key="blocked-persistence-activate-0001",
                )
            )
        except BaseException as exc:
            activation_result.append(exc)

    def close() -> None:
        try:
            bridge.close()
            close_result.append("closed")
        except BaseException as exc:
            close_result.append(exc)

    activation_thread = threading.Thread(target=activate)
    activation_thread.start()
    assert persistence.commit_entered.wait(timeout=1)
    close_thread = threading.Thread(target=close)
    close_thread.start()
    time.sleep(0.05)
    assert close_thread.is_alive()
    persistence.commit_release.set()
    activation_thread.join(timeout=2)
    close_thread.join(timeout=2)

    assert close_result == ["closed"]
    assert isinstance(activation_result[0], DesktopCoreBridgeErrorV1)
    assert persistence.mapping is not None
    events_after_close = tuple(persistence.events)
    time.sleep(0.05)
    assert tuple(persistence.events) == events_after_close
    assert all(handle.closed for handle in tunnels.handles)
    assert not any(request.url.path == "/v1/runs" for request in fake_core.calls)


def test_blocking_persistence_adapter_has_a_bounded_activation_deadline() -> None:
    local_project = _local_project()
    persistence = FakePersistence()
    persistence.block_commit = True
    bridge, _, _, _ = _bridge(
        local_project,
        persistence=persistence,
        timeout=0.5,
    )

    started = time.monotonic()
    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        bridge.activate_project(
            local_project,
            idempotency_key="persistence-deadline-activate-0001",
        )
    elapsed = time.monotonic() - started

    assert exc_info.value.error.code == "core_bridge_retirement_deadline_exceeded"
    assert elapsed < 1.75
    assert bridge._active is None
    persistence.commit_release.set()
    bridge.close()
    assert bridge._closed is True


def test_close_waits_for_blocking_archive_read_and_prevents_upload_mutation() -> None:
    local_project = _local_project(imported=True)
    archive_source = BlockingArchiveSource()
    bridge, _, fake_core, tunnels = _bridge(
        local_project,
        archive_source=archive_source,
    )
    activation_result: list[object] = []

    def activate() -> None:
        try:
            activation_result.append(
                bridge.activate_project(
                    local_project,
                    idempotency_key="blocked-archive-activate-0001",
                )
            )
        except BaseException as exc:
            activation_result.append(exc)

    activation_thread = threading.Thread(target=activate)
    activation_thread.start()
    assert archive_source.stream.entered.wait(timeout=1)
    close_thread = threading.Thread(target=bridge.close)
    close_thread.start()
    time.sleep(0.05)
    assert close_thread.is_alive()
    archive_source.stream.release.set()
    activation_thread.join(timeout=2)
    close_thread.join(timeout=2)

    assert isinstance(activation_result[0], DesktopCoreBridgeErrorV1)
    assert archive_source.stream.closed is True
    assert not any(request.url.path.endswith("/chunk") for request in fake_core.calls)
    assert not any(request.url.path.endswith("/finalize") for request in fake_core.calls)
    assert all(handle.closed for handle in tunnels.handles)


def test_deterministic_precreate_failure_allows_a_new_local_retry_key() -> None:
    local_project = _local_project()
    persistence = FakePersistence()
    fake_core = FakeCore(local_project)
    fake_core.fail_capabilities_with_503 = True
    first, _, _, _ = _bridge(
        local_project,
        persistence=persistence,
        fake_core=fake_core,
    )
    with pytest.raises(DesktopCoreBridgeErrorV1):
        first.activate_project(local_project, idempotency_key="precreate-failure-key-0001")

    assert persistence.operation is not None
    assert persistence.operation.state is CoreProjectCreateStateV1.PRE_CREATE
    fake_core.fail_capabilities_with_503 = False
    second, _, _, _ = _bridge(
        local_project,
        persistence=persistence,
        fake_core=fake_core,
    )
    second.activate_project(local_project, idempotency_key="precreate-retry-key-00002")

    create_calls = [
        request
        for request in fake_core.calls
        if request.method == "POST" and request.url.path == "/v1/projects"
    ]
    assert len(create_calls) == 1
    assert create_calls[0].headers["Idempotency-Key"] == "precreate-retry-key-00002"


def test_bound_create_resumes_with_new_local_action_without_duplicate_create() -> None:
    local_project = _local_project()
    persistence = FakePersistence()
    persistence.fail_commit_once = True
    fake_core = FakeCore(local_project)
    first, _, _, _ = _bridge(
        local_project,
        persistence=persistence,
        fake_core=fake_core,
    )
    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        first.activate_project(local_project, idempotency_key="bound-create-key-00001")

    assert exc_info.value.error.code == "core_bridge_adapter_failed"
    assert "injected mapping commit failure" not in str(exc_info.value)
    assert "/home/" not in repr(exc_info.value)
    assert persistence.operation is not None
    assert persistence.operation.state is CoreProjectCreateStateV1.BOUND
    second, _, _, _ = _bridge(
        local_project,
        persistence=persistence,
        fake_core=fake_core,
    )
    second.activate_project(local_project, idempotency_key="bound-resume-new-key-0002")

    assert (
        len(
            [
                request
                for request in fake_core.calls
                if request.method == "POST" and request.url.path == "/v1/projects"
            ]
        )
        == 1
    )


def test_bound_create_crash_recovers_original_authority_then_patches_edited_intent() -> None:
    original = _local_project()
    persistence = FakePersistence()
    persistence.fail_commit_once = True
    fake_core = FakeCore(original)
    first, _, _, _ = _bridge(
        original,
        persistence=persistence,
        fake_core=fake_core,
    )
    with pytest.raises(DesktopCoreBridgeErrorV1):
        first.activate_project(original, idempotency_key="bound-edit-base-0001")

    assert persistence.operation is not None
    assert persistence.operation.state is CoreProjectCreateStateV1.BOUND
    assert persistence.operation.project_create == map_project_create_v1(original)
    assert persistence.mapping is None
    modified = original.model_copy(
        update={
            "name": "Protein design edited after recovery",
            "task": local_v1.ProjectTaskV1(
                title="Recovered edit",
                objective="Converge the edited Local intent without another create.",
            ),
            "updated_at": "2026-07-14T12:20:00Z",
        }
    )
    second, _, _, _ = _bridge(
        modified,
        persistence=persistence,
        fake_core=fake_core,
    )
    activation = second.activate_project(
        modified,
        idempotency_key="bound-edit-recovery-0002",
    )

    create_calls = [
        request
        for request in fake_core.calls
        if request.method == "POST" and request.url.path == "/v1/projects"
    ]
    assert len(create_calls) == 1
    assert len(fake_core.patch_requests) == 1
    assert fake_core.patch_requests[0][1] == ETAG_C
    assert activation.core_project.name == modified.name
    assert persistence.mapping is not None
    assert persistence.mapping.project_create == map_project_create_v1(modified)


def test_deadline_is_shared_with_host_and_rejects_expired_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_project = _local_project()
    bridge, _, fake_core, _ = _bridge(local_project)
    values = iter((10.0, 20.0))
    monkeypatch.setattr(bridge_module.time, "monotonic", lambda: next(values))

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        bridge.activate_project(local_project, idempotency_key="activate-local-project-0001")

    assert exc_info.value.error.code == "desktop_core_bridge_deadline_exceeded"
    assert fake_core.calls == []
