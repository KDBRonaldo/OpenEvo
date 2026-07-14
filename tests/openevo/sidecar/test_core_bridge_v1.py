from __future__ import annotations

from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import replace
import hashlib
import io
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
    CoreProjectMappingV1,
    CoreProjectPatchOperationV1,
    CoreProjectPatchStateV1,
    CoreTunnelHandleV1,
    CoreWorkspaceUploadAbortStateV1,
    DesktopCoreBridgeErrorV1,
    DesktopCoreBridgeV1,
    map_project_create_v1,
)
from desktop.sidecar.core_client_v1 import CORE_OPENAPI_SHA256, CoreClientErrorV1
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
        "features": [],
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
        created_at=NOW,
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

    def ensure_core(self, profile_id: str, *, deadline: float) -> CoreHostAttachmentV1:
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
    ) -> CoreProjectCreateOperationV1:
        self.events.append("bind_created_project")
        assert self.operation == operation
        self.operation = replace(
            operation,
            state=CoreProjectCreateStateV1.BOUND,
            core_project_id=core_project_id,
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
    ) -> CoreProjectPatchOperationV1:
        self.events.append("record_patch_applied")
        assert self.patch_operation == operation
        self.patch_operation = replace(
            operation,
            state=CoreProjectPatchStateV1.APPLIED,
            outcome=outcome,
        )
        return self.patch_operation

    def commit_mapping(
        self,
        operation: CoreProjectCreateOperationV1,
        mapping: CoreProjectMappingV1,
        *,
        expected_previous: CoreProjectMappingV1 | None,
        completed_patch: CoreProjectPatchOperationV1 | None,
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
        self.lose_abort_after_apply_once = False
        self.upload: core_v1.WorkspaceUploadSessionV1 | None = None
        self.uploads: dict[str, core_v1.WorkspaceUploadSessionV1] = {}
        self.abort_requests: list[tuple[str, core_v1.WorkspaceUploadAbortV1, str, str]] = []
        self.abort_replays: dict[str, core_v1.WorkspaceUploadSessionV1] = {}
        self.head = _head()
        self.block_method: str | None = None
        self.block_path: str | None = None
        self.block_entered = threading.Event()
        self.block_release = threading.Event()
        self.block_once = True
        self.patch_requests: list[tuple[core_v1.ProjectPatchV1, str, str]] = []
        self.patch_apply_count = 0
        self.patch_replays: dict[str, core_v1.ProjectV1] = {}
        self.upload_count = 0
        self.project_snapshot = READY_PROJECT_SNAPSHOT
        self.task_snapshot = TASK_SNAPSHOT
        self.workspace_snapshot = WORKSPACE_SNAPSHOT
        self.project_etag = ETAG_C
        self.active_revision = REVISION
        self.registry_digest = REGISTRY_DIGEST
        self.project_updated_at = NOW

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        if self.block_once and request.method == self.block_method and path == self.block_path:
            self.block_once = False
            self.block_entered.set()
            assert self.block_release.wait(timeout=2)
        if path == "/version":
            return httpx.Response(200, json=_version())
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
                json=_project(self.request, ready=False).model_dump(mode="json"),
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
                imported_patch = isinstance(
                    self.request.workspace, core_v1.ImportedWorkspaceSpecV1
                )
                patched_project = _project(
                    self.request,
                    ready=not imported_patch,
                    project_snapshot=self.project_snapshot,
                    task_snapshot=self.task_snapshot,
                    workspace_snapshot=self.workspace_snapshot,
                    etag=self.project_etag,
                    active_revision=self.active_revision,
                    registry_digest=self.registry_digest,
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
            return httpx.Response(
                201,
                json=core_v1.WorkspaceUploadFinalizeResponseV1(
                    project_id=CORE_PROJECT_ID,
                    upload=finalized_upload,
                    publication=published.workspace_publication,
                    project=published,
                ).model_dump(mode="json"),
            )
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


def _head() -> core_v1.RevisionHeadV1:
    return core_v1.RevisionHeadV1(
        project_id=CORE_PROJECT_ID,
        active_revision=REVISION,
        updated_at=NOW,
        etag=ETAG_A,
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


def _bridge(
    local_project: local_v1.ProjectV1,
    *,
    persistence: FakePersistence | None = None,
    fake_core: FakeCore | None = None,
    archive_source: FakeArchiveSource | None = None,
    timeout: float = 5.0,
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
                codex_model="gpt-5-codex",
            )
        }
    )

    mapped = map_project_create_v1(local_project)

    assert mapped.spec.execution_mode is core_v1.ExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT
    assert mapped.spec.capture_mode is core_v1.CaptureMode.TRANSCRIPT
    assert mapped.spec.harness_id == "codex"
    assert mapped.spec.agent_model_ref == "gpt-5-codex"


def test_activate_then_create_run_uses_real_strict_clients_and_core_authority() -> None:
    local_project = _local_project()
    bridge, persistence, fake_core, _tunnels = _bridge(local_project)

    activation = bridge.activate_project(
        local_project,
        idempotency_key="activate-local-project-0001",
    )
    run = bridge.create_run(
        LOCAL_PROJECT_ID,
        idempotency_key="create-run-local-project-0001",
    )

    assert activation.local_project_id == LOCAL_PROJECT_ID
    assert activation.core_project.id == CORE_PROJECT_ID
    assert activation.validation.valid is True
    assert persistence.events[0] == "reserve_create"
    assert persistence.mapping is not None
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

    successor = core_v1.RevisionRefV1(
        id="revision-1",
        project_id=CORE_PROJECT_ID,
        generation=1,
        manifest_sha256="7" * 64,
    )
    successor_etag = '"' + "7" * 64 + '"'
    successor_registry = "8" * 64
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

    with pytest.raises(CoreClientErrorV1):
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
    with pytest.raises(CoreClientErrorV1):
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


def test_create_run_requires_reachable_nonterminal_successor() -> None:
    local_project = _local_project()
    bridge, _, fake_core, _ = _bridge(local_project)
    bridge.activate_project(local_project, idempotency_key="activate-local-project-0001")
    successor = core_v1.RevisionRefV1(
        id="revision-1",
        project_id=CORE_PROJECT_ID,
        generation=1,
        manifest_sha256="7" * 64,
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

    bridge.create_run(
        LOCAL_PROJECT_ID,
        idempotency_key="create-run-successor-0001",
    )

    assert fake_core.run_requests[-1].required_revision == (
        core_v1.ReachableRequiredRevisionRefV1(
            revision=successor,
            reachable_from_revision_id=REVISION.id,
            relation=core_v1.RequiredRevisionRelation.SUCCESSOR,
        )
    )


def test_cross_project_proxy_fails_before_transport() -> None:
    local_project = _local_project()
    bridge, _, fake_core, _ = _bridge(local_project)
    bridge.activate_project(local_project, idempotency_key="activate-local-project-0001")
    calls_before = len(fake_core.calls)

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        bridge.capabilities("another-local-project")

    assert exc_info.value.error.code == "active_project_mismatch"
    assert len(fake_core.calls) == calls_before


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

    with pytest.raises(CoreClientErrorV1):
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

    with pytest.raises(CoreClientErrorV1):
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

    with pytest.raises(CoreClientErrorV1):
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


def test_core_503_is_preserved_without_synthetic_capability_success() -> None:
    local_project = _local_project()
    bridge, _, fake_core, _ = _bridge(local_project)
    fake_core.fail_capabilities_with_503 = True

    with pytest.raises(CoreClientErrorV1) as exc_info:
        bridge.activate_project(local_project, idempotency_key="activate-local-project-0001")

    assert exc_info.value.error.http_status == 503
    assert exc_info.value.error.code == "route_not_implemented"


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
            result.append(bridge.list_runs())
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

    assert isinstance(first_result[0], DesktopCoreBridgeErrorV1 | CoreClientErrorV1)
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

    with pytest.raises(CoreClientErrorV1):
        bridge.get_run(bearer)

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
    retry_requests: list[core_v1.RunRetryRequestV1] = []
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
        retry_requests.append(request)
        return run

    def restart(
        _service_id: str, request: core_v1.ServiceRestartRequestV1, **_kwargs: object
    ) -> core_v1.OperationV1:
        restart_requests.append(request)
        return object()  # type: ignore[return-value]

    monkeypatch.setattr(client, "cancel_run", cancel)
    bridge.cancel_run("run-1", if_match=ETAG_A, idempotency_key="cancel-run-0000001")
    monkeypatch.setattr(
        client,
        "get_run",
        lambda *_args, **_kwargs: run.model_copy(
            update={"current_attempt_id": "attempt-terminal-1"}
        ),
    )
    monkeypatch.setattr(client, "retry_run", retry)
    bridge.retry_run("run-1", if_match=ETAG_A, idempotency_key="retry-run-00000001")
    monkeypatch.setattr(client, "restart_service", restart)
    bridge.restart_service(
        "service-1",
        if_match=ETAG_A,
        idempotency_key="restart-service-0001",
    )

    assert cancel_requests == [
        core_v1.RunCancelRequestV1(reason=core_v1.RunCancelReason.USER_REQUESTED)
    ]
    assert retry_requests == [core_v1.RunRetryRequestV1(terminal_attempt_id="attempt-terminal-1")]
    assert restart_requests == [
        core_v1.ServiceRestartRequestV1(reason="Requested from OpenEvo Desktop.")
    ]


def test_close_rejects_new_calls_and_is_idempotent() -> None:
    local_project = _local_project()
    bridge, _, _, _ = _bridge(local_project)
    bridge.activate_project(local_project, idempotency_key="activate-local-project-0001")

    bridge.close()
    bridge.close()

    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        bridge.capabilities(LOCAL_PROJECT_ID)
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
    assert isinstance(activation_result[0], DesktopCoreBridgeErrorV1 | CoreClientErrorV1)
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
    with pytest.raises(CoreClientErrorV1):
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
