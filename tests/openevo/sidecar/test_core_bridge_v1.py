from __future__ import annotations

from dataclasses import replace
import hashlib
import io
import secrets
import threading

import httpx
import pytest

from desktop.sidecar import core_bridge_v1 as bridge_module
from desktop.sidecar.contracts.v1 import models as local_v1
from desktop.sidecar.core_bridge_v1 import (
    CoreHostAttachmentV1,
    CoreProjectCreateOperationV1,
    CoreProjectMappingV1,
    CoreTunnelHandleV1,
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
) -> core_v1.CapabilitiesResponseV1:
    return core_v1.CapabilitiesResponseV1(
        core_version="0.1.0",
        registry_digest=REGISTRY_DIGEST,
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
) -> core_v1.ProjectV1:
    imported = isinstance(request.workspace, core_v1.ImportedWorkspaceSpecV1)
    workspace_snapshot = WORKSPACE_SNAPSHOT if ready or not imported else None
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
            workspace_snapshot=WORKSPACE_SNAPSHOT,
            published_at=NOW,
        )
        workspace_snapshot = WORKSPACE_SNAPSHOT
    return core_v1.ProjectV1(
        id=CORE_PROJECT_ID,
        name=request.name,
        description=request.description,
        status=core_v1.ProjectStatus.READY if ready else core_v1.ProjectStatus.DRAFT,
        execution_mode=request.spec.execution_mode,
        workspace_kind=core_v1.WorkspaceSourceKind(request.workspace.kind),
        current_project_snapshot=READY_PROJECT_SNAPSHOT if ready else PROJECT_SNAPSHOT,
        current_task_snapshot=TASK_SNAPSHOT,
        current_workspace_snapshot=workspace_snapshot,
        workspace_publication=publication,
        active_revision=REVISION if ready else None,
        registry_digest=REGISTRY_DIGEST if ready or imported else None,
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
        updated_at=NOW,
        etag=ETAG_C if ready else ETAG_A,
        spec=request.spec,
        task=request.task,
        workspace=request.workspace,
    )


class FakeHostService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, float]] = []

    def ensure_core(self, profile_id: str, *, deadline: float) -> CoreHostAttachmentV1:
        self.calls.append((profile_id, deadline))
        return CoreHostAttachmentV1(
            profile_id=profile_id,
            remote_port=43117,
            bearer_token=secrets.token_urlsafe(32),
            bearer_identity="core-host-key-1",
        )


class FakeTunnelFactory:
    def __init__(self) -> None:
        self.handles: list[CoreTunnelHandleV1] = []

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
        self.events: list[str] = []

    def load_mapping(self, local_project_id: str) -> CoreProjectMappingV1 | None:
        assert local_project_id == LOCAL_PROJECT_ID
        return self.mapping

    def reserve_create(
        self, operation: CoreProjectCreateOperationV1
    ) -> CoreProjectCreateOperationV1:
        self.events.append("reserve_create")
        if self.operation is None:
            self.operation = operation
        elif self.operation.request_sha256 != operation.request_sha256:
            raise AssertionError("operation request changed")
        return self.operation

    def update_create(self, operation: CoreProjectCreateOperationV1) -> None:
        self.events.append("update_create")
        self.operation = operation

    def commit_mapping(
        self,
        operation: CoreProjectCreateOperationV1,
        mapping: CoreProjectMappingV1,
    ) -> None:
        self.events.append("commit_mapping")
        assert self.operation == operation
        self.operation = operation
        self.mapping = mapping


class FakeArchiveSource:
    def __init__(self) -> None:
        self.archive = b"\0" * 1024
        self.refs: list[local_v1.WorkspaceImportRefV1] = []

    def open_archive(self, ref: local_v1.WorkspaceImportRefV1) -> io.BytesIO:
        self.refs.append(ref)
        return ShortReadArchive(self.archive)


class ShortReadArchive(io.BytesIO):
    def read(self, size: int = -1) -> bytes:
        if size > 0:
            size = min(size, 73)
        return super().read(size)


class FakeCore:
    def __init__(self, local_project: local_v1.ProjectV1) -> None:
        self.request = _core_create(local_project)
        self.created = False
        self.calls: list[httpx.Request] = []
        self.run_requests: list[core_v1.RunCreateV1] = []
        self.fail_capabilities_with_503 = False
        self.lose_create_response_once = False
        self.upload: core_v1.WorkspaceUploadSessionV1 | None = None
        self.head = _head()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        if path == "/version":
            return httpx.Response(200, json=_version())
        if path == "/v1/capabilities":
            if self.fail_capabilities_with_503:
                return httpx.Response(503, json=_core_error())
            return httpx.Response(200, json=_capabilities().model_dump(mode="json"))
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
                ).model_dump(mode="json"),
            )
        if path.endswith("/workspace-uploads") and request.method == "POST":
            self.upload = _upload(self.request)
            return httpx.Response(201, json=self.upload.model_dump(mode="json"))
        if path.endswith("/workspace-uploads/upload-1") and request.method == "GET":
            assert self.upload is not None
            return httpx.Response(200, json=self.upload.model_dump(mode="json"))
        if path.endswith("/workspace-uploads/upload-1/chunk"):
            assert self.upload is not None
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
            return httpx.Response(200, json=self.upload.model_dump(mode="json"))
        if path.endswith("/workspace-uploads/upload-1/finalize"):
            assert self.upload is not None
            published = _project(self.request, ready=True, imported_published=True)
            assert published.workspace_publication is not None
            finalized_upload = self.upload.model_copy(
                update={
                    "status": core_v1.WorkspaceUploadStatus.FINALIZED,
                    "publication": published.workspace_publication,
                    "updated_at": "2026-07-14T12:00:02Z",
                    "etag": '"' + "d" * 64 + '"',
                }
            )
            return httpx.Response(
                201,
                json=core_v1.WorkspaceUploadFinalizeResponseV1(
                    project_id=CORE_PROJECT_ID,
                    upload=finalized_upload,
                    publication=published.workspace_publication,
                    project=published,
                ).model_dump(mode="json"),
            )
        if path.endswith("/revisions/head"):
            return httpx.Response(200, json=self.head.model_dump(mode="json"))
        if path.endswith("/validate"):
            return httpx.Response(
                200,
                json=core_v1.ProjectValidationResponseV1(
                    valid=True,
                    registry_digest=REGISTRY_DIGEST,
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


def _upload(request: core_v1.ProjectCreateV1) -> core_v1.WorkspaceUploadSessionV1:
    assert isinstance(request.workspace, core_v1.ImportedWorkspaceSpecV1)
    return core_v1.WorkspaceUploadSessionV1(
        id="upload-1",
        project_id=CORE_PROJECT_ID,
        status=core_v1.WorkspaceUploadStatus.OPEN,
        accepted_offset=0,
        project_snapshot=PROJECT_SNAPSHOT,
        project_etag=ETAG_A,
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
) -> tuple[DesktopCoreBridgeV1, FakePersistence, FakeCore, FakeTunnelFactory]:
    persistence = persistence or FakePersistence()
    fake_core = fake_core or FakeCore(local_project)
    tunnels = FakeTunnelFactory()
    bridge = DesktopCoreBridgeV1(
        host_service=FakeHostService(),
        tunnel_factory=tunnels,
        persistence=persistence,
        archive_source=FakeArchiveSource(),
        transport_factory=lambda: httpx.MockTransport(fake_core),
        timeout=5.0,
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
    bridge.close()
    release.set()
    worker.join(timeout=2)

    assert len(tunnels.handles) == 1
    assert tunnels.handles[0].closed is True
    assert isinstance(result[0], DesktopCoreBridgeErrorV1)
    assert result[0].error.code == "active_project_session_superseded"


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
