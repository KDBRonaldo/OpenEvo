from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import pytest

from desktop.sidecar import core_bridge_v1 as bridge_module
import desktop.sidecar.core_bridge_store_v1 as store_module
from desktop.sidecar.contracts.v1 import models as local_v1
from desktop.sidecar.core_bridge_store_v1 import (
    CoreBridgeStoreCapacityError,
    CoreBridgeStoreConflictError,
    CoreBridgeStoreContractError,
    CoreBridgeStoreDataCorruptionError,
    CoreBridgeStoreSchemaError,
    CoreBridgeStoreStateRootError,
    DesktopCoreBridgeStoreV1,
)
from desktop.sidecar.core_bridge_v1 import (
    CoreProjectCreateOperationV1,
    CoreProjectMappingV1,
    CoreProjectPatchOperationV1,
    DesktopCoreBridgePersistence,
    map_project_create_v1,
)
from openevo.backend.contracts.v1 import models as core_v1


NOW = "2026-07-14T12:00:00Z"
LATER = "2026-07-14T12:01:00Z"
LATEST = "2026-07-14T12:02:00Z"
LOCAL_PROJECT_ID = "local-project-1"
CORE_PROJECT_ID = "core-project-1"
PROFILE_ID = "profile-1"
HOST_IDENTITY = "core-host-key-1"
REGISTRY_DIGEST = "4" * 64
ETAG_A = '"' + "a" * 64 + '"'
ETAG_B = '"' + "b" * 64 + '"'


def _prepare_unpublished_store_files(root: Path) -> tuple[Path, Path]:
    root.mkdir(mode=0o700)
    for name in (
        store_module.OWNER_LOCK_FILENAME,
        store_module.DATABASE_FILENAME,
        store_module.IDENTITY_MARKER_FILENAME,
    ):
        path = root / name
        path.touch(mode=0o600)
        os.chmod(path, 0o600)
    anchor = root.parent / (
        store_module.ROOT_ANCHOR_PREFIX
        + hashlib.sha256(os.fsencode(os.path.abspath(root))).hexdigest()
        + ".identity"
    )
    anchor.touch(mode=0o600)
    os.chmod(anchor, 0o600)
    return root / store_module.DATABASE_FILENAME, anchor


def _create_uncommitted_initial_schema_hot_journal(
    root: Path,
) -> tuple[Path, Path, Path]:
    database, anchor = _prepare_unpublished_store_files(root)

    script = """
import os
import sqlite3
import sys

from desktop.sidecar import core_bridge_store_v1 as store_module

connection = sqlite3.connect(sys.argv[1], isolation_level=None)
connection.execute("PRAGMA journal_mode = DELETE")
connection.execute("PRAGMA synchronous = FULL")
connection.execute("PRAGMA cache_size = 1")
connection.execute("PRAGMA cache_spill = ON")
connection.execute("BEGIN EXCLUSIVE")
for statement in store_module._SCHEMA:
    connection.execute(statement)
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", script, os.fspath(database)], check=True)

    journal = root / store_module.JOURNAL_FILENAME
    assert database.stat().st_size > 0
    assert journal.stat().st_size > 0
    assert (root / store_module.IDENTITY_MARKER_FILENAME).stat().st_size == 0
    assert anchor.stat().st_size == 0
    return database, journal, anchor


def _leave_pending_store_binding(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupt_binding(
        _self: DesktopCoreBridgeStoreV1,
        _connection: sqlite3.Connection,
    ) -> None:
        raise OSError("injected pending binding interruption")

    with monkeypatch.context() as patch:
        patch.setattr(
            DesktopCoreBridgeStoreV1,
            "_complete_pending_store_binding",
            interrupt_binding,
        )
        with pytest.raises(OSError, match="pending binding interruption"):
            DesktopCoreBridgeStoreV1(root)


def _snapshot(
    snapshot_id: str,
    kind: core_v1.SnapshotKind,
    digest_char: str,
) -> core_v1.ImmutableSnapshotRefV1:
    return core_v1.ImmutableSnapshotRefV1(
        id=snapshot_id,
        kind=kind,
        content_sha256=digest_char * 64,
        created_at=NOW,
    )


PROJECT_SNAPSHOT_A = _snapshot("project-snapshot-a", core_v1.SnapshotKind.PROJECT, "1")
PROJECT_SNAPSHOT_B = _snapshot("project-snapshot-b", core_v1.SnapshotKind.PROJECT, "2")
TASK_SNAPSHOT_A = _snapshot("task-snapshot-a", core_v1.SnapshotKind.TASK, "3")
TASK_SNAPSHOT_B = _snapshot("task-snapshot-b", core_v1.SnapshotKind.TASK, "4")
WORKSPACE_SNAPSHOT = _snapshot("workspace-snapshot-a", core_v1.SnapshotKind.WORKSPACE, "5")
REVISION = core_v1.RevisionRefV1(
    id="revision-0",
    project_id=CORE_PROJECT_ID,
    generation=0,
    manifest_sha256="6" * 64,
)


def _local_project(*, title: str = "Design") -> local_v1.ProjectV1:
    request = local_v1.ProjectCreateV1.model_validate(
        {
            "name": "Protein design",
            "profile_id": PROFILE_ID,
            "task": {"title": title, "objective": "Improve stability."},
            "source": {"kind": "scratch", "display_name": "New workspace"},
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


def _request(*, title: str = "Design") -> core_v1.ProjectCreateV1:
    return map_project_create_v1(_local_project(title=title))


def _project(
    request: core_v1.ProjectCreateV1,
    *,
    project_snapshot: core_v1.ImmutableSnapshotRefV1,
    task_snapshot: core_v1.ImmutableSnapshotRefV1,
    etag: str,
) -> core_v1.ProjectV1:
    return core_v1.ProjectV1(
        id=CORE_PROJECT_ID,
        name=request.name,
        description=request.description,
        status=core_v1.ProjectStatus.READY,
        execution_mode=request.spec.execution_mode,
        workspace_kind=core_v1.WorkspaceSourceKind.SCRATCH,
        current_project_snapshot=project_snapshot,
        current_task_snapshot=task_snapshot,
        current_workspace_snapshot=WORKSPACE_SNAPSHOT,
        workspace_publication=None,
        active_revision=REVISION,
        registry_digest=REGISTRY_DIGEST,
        model_preparation=core_v1.ModelPreparationV1(
            model_ref=request.spec.agent_model_ref,
            status=core_v1.ModelPreparationStatus.READY,
            updated_at=NOW,
        ),
        created_at=NOW,
        updated_at=NOW,
        etag=etag,
        spec=request.spec,
        task=request.task,
        workspace=request.workspace,
    )


def _create_operation(
    *,
    request: core_v1.ProjectCreateV1 | None = None,
    key: str = "activate-project-0001",
) -> CoreProjectCreateOperationV1:
    request = request or _request()
    return CoreProjectCreateOperationV1(
        local_project_id=LOCAL_PROJECT_ID,
        profile_id=PROFILE_ID,
        core_host_identity=HOST_IDENTITY,
        request_sha256=bridge_module._model_digest(request),
        project_create=request,
        idempotency_key=key,
    )


def _mapping(
    request: core_v1.ProjectCreateV1,
    *,
    generation: int,
    project_snapshot: core_v1.ImmutableSnapshotRefV1,
    task_snapshot: core_v1.ImmutableSnapshotRefV1,
    etag: str,
    predecessor: str | None,
) -> CoreProjectMappingV1:
    project = _project(
        request,
        project_snapshot=project_snapshot,
        task_snapshot=task_snapshot,
        etag=etag,
    )
    return CoreProjectMappingV1(
        local_project_id=LOCAL_PROJECT_ID,
        profile_id=PROFILE_ID,
        core_host_identity=HOST_IDENTITY,
        core_project_id=CORE_PROJECT_ID,
        request_sha256=bridge_module._model_digest(request),
        project_create=request,
        project_snapshot=project_snapshot,
        task_snapshot=task_snapshot,
        workspace_snapshot=WORKSPACE_SNAPSHOT,
        registry_digest=REGISTRY_DIGEST,
        project_etag=etag,
        active_revision=REVISION,
        project_updated_at=NOW,
        immutable_authority=bridge_module._patch_immutable_authority(project),
        mutable_authority=bridge_module._patch_mutable_authority(project),
        mapping_generation=generation,
        predecessor_request_sha256=predecessor,
    )


def _patch_operation(
    old_request: core_v1.ProjectCreateV1,
    new_request: core_v1.ProjectCreateV1,
) -> tuple[CoreProjectPatchOperationV1, core_v1.ProjectV1]:
    patch = core_v1.ProjectPatchV1(
        name=new_request.name,
        description=new_request.description,
        spec=new_request.spec,
        task=new_request.task,
        workspace=new_request.workspace,
    )
    base = _project(
        old_request,
        project_snapshot=PROJECT_SNAPSHOT_A,
        task_snapshot=TASK_SNAPSHOT_A,
        etag=ETAG_A,
    )
    outcome = _project(
        new_request,
        project_snapshot=PROJECT_SNAPSHOT_B,
        task_snapshot=TASK_SNAPSHOT_B,
        etag=ETAG_B,
    )
    return (
        CoreProjectPatchOperationV1(
            local_project_id=LOCAL_PROJECT_ID,
            profile_id=PROFILE_ID,
            core_host_identity=HOST_IDENTITY,
            core_project_id=CORE_PROJECT_ID,
            old_request_sha256=bridge_module._model_digest(old_request),
            old_project_create=old_request,
            new_request_sha256=bridge_module._model_digest(new_request),
            new_project_create=new_request,
            patch_request_sha256=bridge_module._model_digest(patch),
            patch=patch,
            idempotency_key="project-patch-0001",
            base_project=base,
        ),
        outcome,
    )


def _bound_create(store: DesktopCoreBridgeStoreV1) -> CoreProjectCreateOperationV1:
    operation = store.reserve_create(_create_operation())
    operation = store.mark_create_unknown(operation)
    project = _project(
        operation.project_create,
        project_snapshot=PROJECT_SNAPSHOT_A,
        task_snapshot=TASK_SNAPSHOT_A,
        etag=ETAG_A,
    )
    return store.bind_created_project(
        operation,
        CORE_PROJECT_ID,
        immutable_authority=bridge_module._patch_immutable_authority(project),
    )


def _assert_protocol(_persistence: DesktopCoreBridgePersistence) -> None:
    pass


def test_full_protocol_round_trip_restart_and_atomic_patch_cleanup(tmp_path: Path) -> None:
    root = tmp_path / "core-bridge"
    store = DesktopCoreBridgeStoreV1(root)
    _assert_protocol(store)
    operation = _bound_create(store)
    request_a = operation.project_create
    mapping_a = _mapping(
        request_a,
        generation=1,
        project_snapshot=PROJECT_SNAPSHOT_A,
        task_snapshot=TASK_SNAPSHOT_A,
        etag=ETAG_A,
        predecessor=None,
    )
    store.commit_mapping(
        operation,
        mapping_a,
        expected_previous=None,
        completed_patch=None,
    )

    request_b = _request(title="Changed task")
    pending, outcome = _patch_operation(request_a, request_b)
    pending = store.reserve_patch(pending)
    pending = store.mark_patch_unknown(pending)
    applied = store.record_patch_applied(
        pending,
        outcome,
        outcome_immutable=bridge_module._patch_immutable_authority(outcome),
        outcome_mutable=bridge_module._patch_mutable_authority(outcome),
    )
    mapping_b = _mapping(
        request_b,
        generation=2,
        project_snapshot=PROJECT_SNAPSHOT_B,
        task_snapshot=TASK_SNAPSHOT_B,
        etag=ETAG_B,
        predecessor=mapping_a.request_sha256,
    )
    store.commit_mapping(
        operation,
        mapping_b,
        expected_previous=mapping_a,
        completed_patch=applied,
    )

    assert store.load_create(LOCAL_PROJECT_ID) == operation
    assert store.load_mapping(LOCAL_PROJECT_ID) == mapping_b
    assert store.load_patch(LOCAL_PROJECT_ID) is None
    assert store.load_mapping_history(LOCAL_PROJECT_ID) == (mapping_a, mapping_b)
    store.close()

    reopened = DesktopCoreBridgeStoreV1(root)
    assert reopened.load_create(LOCAL_PROJECT_ID) == operation
    assert reopened.load_mapping(LOCAL_PROJECT_ID) == mapping_b
    assert reopened.load_patch(LOCAL_PROJECT_ID) is None
    assert reopened.load_mapping_history(LOCAL_PROJECT_ID) == (mapping_a, mapping_b)
    reopened.close()


@pytest.mark.parametrize("imported", [False, True], ids=["scratch", "imported-finalize"])
def test_existing_bridge_fake_core_conforms_and_recovers_across_store_restart(
    tmp_path: Path,
    imported: bool,
) -> None:
    from tests.openevo.sidecar import test_core_bridge_v1 as bridge_tests

    root = tmp_path / "state"
    project = bridge_tests._local_project(imported=imported)
    store = DesktopCoreBridgeStoreV1(root)
    first, _, fake_core, _ = bridge_tests._bridge(project, persistence=store)
    first.activate_project(project, idempotency_key="real-store-activation-0001")
    first.close()
    first_mapping = store.load_mapping(LOCAL_PROJECT_ID)
    first_operation = store.load_create(LOCAL_PROJECT_ID)
    assert first_mapping is not None
    assert first_operation is not None
    if imported:
        assert first_operation.workspace_upload_finalize is not None
    store.close()

    reopened = DesktopCoreBridgeStoreV1(root)
    second, _, _, _ = bridge_tests._bridge(
        project,
        persistence=reopened,
        fake_core=fake_core,
    )
    second.activate_project(project, idempotency_key="real-store-activation-0002")
    second.close()
    assert reopened.load_mapping(LOCAL_PROJECT_ID) == first_mapping
    assert reopened.load_create(LOCAL_PROJECT_ID) == first_operation
    reopened.close()


def test_unknown_workspace_abort_replays_exactly_after_store_restart(tmp_path: Path) -> None:
    from tests.openevo.sidecar import test_core_bridge_v1 as bridge_tests

    root = tmp_path / "state"
    original = bridge_tests._local_project(imported=True)
    store = DesktopCoreBridgeStoreV1(root)
    fake_core = bridge_tests.FakeCore(original)
    first, _, _, _ = bridge_tests._bridge(
        original,
        persistence=store,
        fake_core=fake_core,
        archive_source=bridge_tests.FakeArchiveSource(b"\2" * 1024),
    )
    with pytest.raises(bridge_module.DesktopCoreBridgeErrorV1):
        first.activate_project(original, idempotency_key="real-store-abort-a-0001")
    first.close()

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
    second, _, _, _ = bridge_tests._bridge(
        modified,
        persistence=store,
        fake_core=fake_core,
        archive_source=bridge_tests.FakeArchiveSource(archive_b),
    )
    with pytest.raises(bridge_module.DesktopCoreBridgeErrorV1):
        second.activate_project(modified, idempotency_key="real-store-abort-b-0002")
    second.close()
    unknown = store.load_create(LOCAL_PROJECT_ID)
    assert unknown is not None and unknown.workspace_upload_abort is not None
    assert unknown.workspace_upload_abort.state.value == "unknown"
    store.close()

    reopened = DesktopCoreBridgeStoreV1(root)
    third, _, _, _ = bridge_tests._bridge(
        modified,
        persistence=reopened,
        fake_core=fake_core,
        archive_source=bridge_tests.FakeArchiveSource(archive_b),
    )
    third.activate_project(modified, idempotency_key="real-store-abort-retry-0003")
    third.close()
    assert len(fake_core.abort_requests) == 2
    assert fake_core.abort_requests[0] == fake_core.abort_requests[1]
    completed = reopened.load_create(LOCAL_PROJECT_ID)
    assert completed is not None and completed.workspace_upload_abort is None
    reopened.close()


def test_unknown_workspace_finalize_replays_exact_request_after_store_restart(
    tmp_path: Path,
) -> None:
    from tests.openevo.sidecar import test_core_bridge_v1 as bridge_tests

    root = tmp_path / "state"
    project = bridge_tests._local_project(imported=True)
    archive = b"\0" * 1024
    fake_core = bridge_tests.FakeCore(project)
    fake_core.lose_finalize_after_apply_once = True
    store = DesktopCoreBridgeStoreV1(root)
    first, _, _, _ = bridge_tests._bridge(
        project,
        persistence=store,
        fake_core=fake_core,
        archive_source=bridge_tests.FakeArchiveSource(archive),
    )

    with pytest.raises(bridge_module.DesktopCoreBridgeErrorV1):
        first.activate_project(project, idempotency_key="finalize-loss-0001")
    first.close()
    unknown = store.load_create(LOCAL_PROJECT_ID)
    assert unknown is not None and unknown.workspace_upload_finalize is not None
    assert unknown.workspace_upload_finalize.state.value == "unknown"
    assert unknown.workspace_upload_finalize.outcome is None
    first_request = fake_core.finalize_requests[0]
    assert first_request[2] == unknown.workspace_upload_finalize.upload_etag
    assert first_request[3] == unknown.workspace_upload_finalize.project_etag
    store.close()

    reopened = DesktopCoreBridgeStoreV1(root)
    second, _, _, _ = bridge_tests._bridge(
        project,
        persistence=reopened,
        fake_core=fake_core,
        archive_source=bridge_tests.FakeArchiveSource(archive),
    )
    second.activate_project(project, idempotency_key="finalize-loss-retry-0002")
    second.close()

    assert fake_core.finalize_requests == [first_request, first_request]
    applied = reopened.load_create(LOCAL_PROJECT_ID)
    assert applied is not None and applied.workspace_upload_finalize is not None
    assert applied.workspace_upload_finalize.state.value == "applied"
    assert applied.workspace_upload_finalize.outcome is not None
    reopened.close()


@pytest.mark.parametrize(
    "latest_change", ["workspace", "project"], ids=["workspace", "non-workspace"]
)
def test_unknown_finalize_replays_before_latest_local_intent_after_restart(
    tmp_path: Path,
    latest_change: str,
) -> None:
    from tests.openevo.sidecar import test_core_bridge_v1 as bridge_tests

    root = tmp_path / "state"
    original = bridge_tests._local_project()
    archive_a = b"\1" * 1024
    source_a = local_v1.ProjectSourceV1(
        kind="native_folder_snapshot",
        display_name="Imported workspace A",
        import_ref=local_v1.WorkspaceImportRefV1(
            import_id="adopted-unknown-finalize-a",
            content_sha256=hashlib.sha256(archive_a).hexdigest(),
            byte_size=len(archive_a),
            entry_count=0,
            extracted_byte_size=0,
        ),
    )
    imported_a = original.model_copy(
        update={"source": source_a, "updated_at": "2026-07-14T12:40:00Z"}
    )
    fake_core = bridge_tests.FakeCore(original)
    store = DesktopCoreBridgeStoreV1(root)
    first, _, _, _ = bridge_tests._bridge(
        original,
        persistence=store,
        fake_core=fake_core,
    )
    first.activate_project(original, idempotency_key="unknown-finalize-base-0001")
    first.close()

    fake_core.lose_finalize_after_apply_once = True
    second, _, _, _ = bridge_tests._bridge(
        imported_a,
        persistence=store,
        fake_core=fake_core,
        archive_source=bridge_tests.FakeArchiveSource(archive_a),
    )
    with pytest.raises(bridge_module.DesktopCoreBridgeErrorV1):
        second.activate_project(imported_a, idempotency_key="unknown-finalize-a-0002")
    second.close()

    pending_patch = store.load_patch(LOCAL_PROJECT_ID)
    pending_create = store.load_create(LOCAL_PROJECT_ID)
    assert pending_patch is not None and pending_patch.state.value == "applied"
    assert pending_create is not None and pending_create.workspace_upload_finalize is not None
    assert pending_create.workspace_upload_finalize.state.value == "unknown"
    first_finalize = fake_core.finalize_requests[0]
    store.close()

    archive_latest = archive_a
    if latest_change == "workspace":
        archive_latest = b"\2" * 1024
        latest = imported_a.model_copy(
            update={
                "source": local_v1.ProjectSourceV1(
                    kind="native_folder_snapshot",
                    display_name="Imported workspace B",
                    import_ref=local_v1.WorkspaceImportRefV1(
                        import_id="adopted-unknown-finalize-b",
                        content_sha256=hashlib.sha256(archive_latest).hexdigest(),
                        byte_size=len(archive_latest),
                        entry_count=0,
                        extracted_byte_size=0,
                    ),
                ),
                "updated_at": "2026-07-14T12:41:00Z",
            }
        )
    else:
        latest = imported_a.model_copy(
            update={
                "name": "Protein design with revised objective",
                "task": local_v1.ProjectTaskV1(
                    title="Revised objective",
                    objective="Apply the latest non-workspace Local intent.",
                ),
                "updated_at": "2026-07-14T12:41:00Z",
            }
        )

    reopened = DesktopCoreBridgeStoreV1(root)
    call_start = len(fake_core.calls)
    third, _, _, _ = bridge_tests._bridge(
        latest,
        persistence=reopened,
        fake_core=fake_core,
        archive_source=bridge_tests.FakeArchiveSource(archive_latest),
    )
    activation = third.activate_project(
        latest,
        idempotency_key="unknown-finalize-latest-0003",
    )
    third.close()

    replay_calls = fake_core.calls[call_start:]
    finalize_index = next(
        index
        for index, request in enumerate(replay_calls)
        if request.method == "POST" and request.url.path.endswith("/finalize")
    )
    patch_index = next(
        index for index, request in enumerate(replay_calls) if request.method == "PATCH"
    )
    assert finalize_index < patch_index
    assert fake_core.finalize_requests[:2] == [first_finalize, first_finalize]
    assert activation.core_project.name == latest.name
    assert activation.core_project.workspace == map_project_create_v1(latest).workspace
    assert reopened.load_patch(LOCAL_PROJECT_ID) is None
    completed_create = reopened.load_create(LOCAL_PROJECT_ID)
    assert completed_create is not None
    assert completed_create.workspace_upload_finalize is not None
    assert completed_create.workspace_upload_finalize.state.value == "applied"
    completed_mapping = reopened.load_mapping(LOCAL_PROJECT_ID)
    assert completed_mapping is not None
    assert completed_mapping.project_create == map_project_create_v1(latest)
    reopened.close()


def test_create_reservation_and_every_transition_are_exact_full_row_cas(tmp_path: Path) -> None:
    store = DesktopCoreBridgeStoreV1(tmp_path / "state")
    first = store.reserve_create(_create_operation(key="activate-project-0001"))
    replacement = store.reserve_create(_create_operation(key="activate-project-0002"))
    assert replacement.idempotency_key == "activate-project-0002"

    with pytest.raises(CoreBridgeStoreConflictError):
        store.mark_create_unknown(first)

    unknown = store.mark_create_unknown(replacement)
    assert store.reserve_create(_create_operation(key="activate-project-0003")) == unknown
    project = _project(
        unknown.project_create,
        project_snapshot=PROJECT_SNAPSHOT_A,
        task_snapshot=TASK_SNAPSHOT_A,
        etag=ETAG_A,
    )
    bound = store.bind_created_project(
        unknown,
        CORE_PROJECT_ID,
        immutable_authority=bridge_module._patch_immutable_authority(project),
    )

    changed = replace(
        bound,
        workspace_upload_id="upload-1",
        workspace_upload_project_snapshot=PROJECT_SNAPSHOT_A,
    )
    assert store.update_create(changed, expected_previous=bound) == changed

    with pytest.raises(CoreBridgeStoreConflictError):
        store.update_create(bound, expected_previous=bound)


def test_create_binding_requires_exact_immutable_authority(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = DesktopCoreBridgeStoreV1(root)
    unknown = store.mark_create_unknown(store.reserve_create(_create_operation()))
    project = _project(
        unknown.project_create,
        project_snapshot=PROJECT_SNAPSHOT_A,
        task_snapshot=TASK_SNAPSHOT_A,
        etag=ETAG_A,
    )
    authority = bridge_module._patch_immutable_authority(project)

    with pytest.raises(CoreBridgeStoreContractError, match="does not match"):
        store.bind_created_project(
            unknown,
            CORE_PROJECT_ID,
            immutable_authority=replace(authority, project_id="another-core-project"),
        )
    assert store.load_create(LOCAL_PROJECT_ID) == unknown

    bound = store.bind_created_project(
        unknown,
        CORE_PROJECT_ID,
        immutable_authority=authority,
    )
    assert bound.project_immutable_authority == authority
    store.close()

    reopened = DesktopCoreBridgeStoreV1(root)
    assert reopened.load_create(LOCAL_PROJECT_ID) == bound
    reopened.close()


def test_concurrent_stale_create_cas_has_one_winner(tmp_path: Path) -> None:
    store = DesktopCoreBridgeStoreV1(tmp_path / "state")
    operation = store.reserve_create(_create_operation())
    barrier = threading.Barrier(2)

    def transition() -> CoreProjectCreateOperationV1 | type[BaseException]:
        barrier.wait(timeout=5)
        try:
            return store.mark_create_unknown(operation)
        except BaseException as exc:
            return type(exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _index: transition(), range(2)))

    assert sum(isinstance(value, CoreProjectCreateOperationV1) for value in results) == 1
    assert results.count(CoreBridgeStoreConflictError) == 1


def test_mapping_commit_rollback_retains_old_mapping_and_applied_patch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DesktopCoreBridgeStoreV1(tmp_path / "state")
    operation = _bound_create(store)
    request_a = operation.project_create
    mapping_a = _mapping(
        request_a,
        generation=1,
        project_snapshot=PROJECT_SNAPSHOT_A,
        task_snapshot=TASK_SNAPSHOT_A,
        etag=ETAG_A,
        predecessor=None,
    )
    store.commit_mapping(operation, mapping_a, expected_previous=None, completed_patch=None)
    request_b = _request(title="Changed task")
    pending, outcome = _patch_operation(request_a, request_b)
    pending = store.mark_patch_unknown(store.reserve_patch(pending))
    applied = store.record_patch_applied(
        pending,
        outcome,
        outcome_immutable=bridge_module._patch_immutable_authority(outcome),
        outcome_mutable=bridge_module._patch_mutable_authority(outcome),
    )
    mapping_b = _mapping(
        request_b,
        generation=2,
        project_snapshot=PROJECT_SNAPSHOT_B,
        task_snapshot=TASK_SNAPSHOT_B,
        etag=ETAG_B,
        predecessor=mapping_a.request_sha256,
    )

    def fail_after_writes() -> None:
        raise RuntimeError("injected transaction failure")

    monkeypatch.setattr(store_module, "_before_mapping_commit", fail_after_writes)
    with pytest.raises(RuntimeError, match="injected"):
        store.commit_mapping(
            operation,
            mapping_b,
            expected_previous=mapping_a,
            completed_patch=applied,
        )

    assert store.load_mapping(LOCAL_PROJECT_ID) == mapping_a
    assert store.load_patch(LOCAL_PROJECT_ID) == applied
    assert store.load_mapping_history(LOCAL_PROJECT_ID) == (mapping_a,)


def test_exact_mapping_commit_retry_recovers_commit_ambiguity(tmp_path: Path) -> None:
    store = DesktopCoreBridgeStoreV1(tmp_path / "state")
    operation = _bound_create(store)
    mapping = _mapping(
        operation.project_create,
        generation=1,
        project_snapshot=PROJECT_SNAPSHOT_A,
        task_snapshot=TASK_SNAPSHOT_A,
        etag=ETAG_A,
        predecessor=None,
    )
    store.commit_mapping(operation, mapping, expected_previous=None, completed_patch=None)
    store.commit_mapping(operation, mapping, expected_previous=None, completed_patch=None)
    assert store.load_mapping_history(LOCAL_PROJECT_ID) == (mapping,)


def test_mapping_history_is_ordered_and_capacity_is_fail_closed(tmp_path: Path) -> None:
    store = DesktopCoreBridgeStoreV1(tmp_path / "state", max_mapping_history_rows=1)
    operation = _bound_create(store)
    mapping_a = _mapping(
        operation.project_create,
        generation=1,
        project_snapshot=PROJECT_SNAPSHOT_A,
        task_snapshot=TASK_SNAPSHOT_A,
        etag=ETAG_A,
        predecessor=None,
    )
    store.commit_mapping(operation, mapping_a, expected_previous=None, completed_patch=None)
    successor = REVISION.model_copy(
        update={"id": "revision-1", "generation": 1, "manifest_sha256": "7" * 64}
    )
    mapping_b = replace(
        mapping_a,
        active_revision=successor,
        project_etag=ETAG_B,
        project_updated_at=LATER,
        mutable_authority=replace(
            mapping_a.mutable_authority,
            active_revision=successor,
            etag=ETAG_B,
            updated_at=LATER,
        ),
        mapping_generation=2,
        predecessor_request_sha256=mapping_a.request_sha256,
    )

    with pytest.raises(CoreBridgeStoreCapacityError):
        store.commit_mapping(
            operation, mapping_b, expected_previous=mapping_a, completed_patch=None
        )

    assert store.load_mapping(LOCAL_PROJECT_ID) == mapping_a
    assert store.load_mapping_history(LOCAL_PROJECT_ID) == (mapping_a,)


def test_mapping_commit_rejects_same_revision_generation_rewrite(tmp_path: Path) -> None:
    store = DesktopCoreBridgeStoreV1(tmp_path / "state")
    operation = _bound_create(store)
    mapping_a = _mapping(
        operation.project_create,
        generation=1,
        project_snapshot=PROJECT_SNAPSHOT_A,
        task_snapshot=TASK_SNAPSHOT_A,
        etag=ETAG_A,
        predecessor=None,
    )
    store.commit_mapping(operation, mapping_a, expected_previous=None, completed_patch=None)
    rewritten_revision = REVISION.model_copy(update={"id": "revision-0-rewritten"})
    mapping_b = replace(
        mapping_a,
        active_revision=rewritten_revision,
        project_etag=ETAG_B,
        mutable_authority=replace(
            mapping_a.mutable_authority,
            active_revision=rewritten_revision,
            etag=ETAG_B,
        ),
        mapping_generation=2,
        predecessor_request_sha256=mapping_a.request_sha256,
    )

    with pytest.raises(CoreBridgeStoreContractError, match="revision"):
        store.commit_mapping(
            operation, mapping_b, expected_previous=mapping_a, completed_patch=None
        )


def test_mapping_history_rejects_nonadjacent_etag_reuse(tmp_path: Path) -> None:
    store = DesktopCoreBridgeStoreV1(tmp_path / "state")
    operation = _bound_create(store)
    mapping_a = _mapping(
        operation.project_create,
        generation=1,
        project_snapshot=PROJECT_SNAPSHOT_A,
        task_snapshot=TASK_SNAPSHOT_A,
        etag=ETAG_A,
        predecessor=None,
    )
    store.commit_mapping(operation, mapping_a, expected_previous=None, completed_patch=None)
    successor_b = REVISION.model_copy(
        update={"id": "revision-1", "generation": 1, "manifest_sha256": "7" * 64}
    )
    mapping_b = replace(
        mapping_a,
        active_revision=successor_b,
        project_etag=ETAG_B,
        project_updated_at=LATER,
        mutable_authority=replace(
            mapping_a.mutable_authority,
            active_revision=successor_b,
            etag=ETAG_B,
            updated_at=LATER,
        ),
        mapping_generation=2,
        predecessor_request_sha256=mapping_a.request_sha256,
    )
    store.commit_mapping(
        operation,
        mapping_b,
        expected_previous=mapping_a,
        completed_patch=None,
    )
    successor_c = REVISION.model_copy(
        update={"id": "revision-2", "generation": 2, "manifest_sha256": "8" * 64}
    )
    mapping_rollback = replace(
        mapping_b,
        active_revision=successor_c,
        project_etag=ETAG_A,
        project_updated_at=LATEST,
        mutable_authority=replace(
            mapping_b.mutable_authority,
            active_revision=successor_c,
            etag=ETAG_A,
            updated_at=LATEST,
        ),
        mapping_generation=3,
        predecessor_request_sha256=mapping_b.request_sha256,
    )

    with pytest.raises(CoreBridgeStoreContractError, match="ETag reuses"):
        store.commit_mapping(
            operation,
            mapping_rollback,
            expected_previous=mapping_b,
            completed_patch=None,
        )


def test_mapping_commit_binds_successor_to_applied_patch_outcome(tmp_path: Path) -> None:
    store = DesktopCoreBridgeStoreV1(tmp_path / "state")
    operation = _bound_create(store)
    request_a = operation.project_create
    mapping_a = _mapping(
        request_a,
        generation=1,
        project_snapshot=PROJECT_SNAPSHOT_A,
        task_snapshot=TASK_SNAPSHOT_A,
        etag=ETAG_A,
        predecessor=None,
    )
    store.commit_mapping(operation, mapping_a, expected_previous=None, completed_patch=None)
    request_b = _request(title="Changed task")
    pending, outcome = _patch_operation(request_a, request_b)
    pending = store.mark_patch_unknown(store.reserve_patch(pending))
    applied = store.record_patch_applied(
        pending,
        outcome,
        outcome_immutable=bridge_module._patch_immutable_authority(outcome),
        outcome_mutable=bridge_module._patch_mutable_authority(outcome),
    )
    unbound = _mapping(
        request_b,
        generation=2,
        project_snapshot=PROJECT_SNAPSHOT_A,
        task_snapshot=TASK_SNAPSHOT_B,
        etag=ETAG_B,
        predecessor=mapping_a.request_sha256,
    )

    with pytest.raises(CoreBridgeStoreContractError, match="applied patch"):
        store.commit_mapping(
            operation,
            unbound,
            expected_previous=mapping_a,
            completed_patch=applied,
        )


def test_mapping_commit_rejects_revision_successor_reusing_applied_etag(
    tmp_path: Path,
) -> None:
    store = DesktopCoreBridgeStoreV1(tmp_path / "state")
    operation = _bound_create(store)
    request_a = operation.project_create
    mapping_a = _mapping(
        request_a,
        generation=1,
        project_snapshot=PROJECT_SNAPSHOT_A,
        task_snapshot=TASK_SNAPSHOT_A,
        etag=ETAG_A,
        predecessor=None,
    )
    store.commit_mapping(operation, mapping_a, expected_previous=None, completed_patch=None)
    request_b = _request(title="Changed task")
    pending, outcome = _patch_operation(request_a, request_b)
    pending = store.mark_patch_unknown(store.reserve_patch(pending))
    applied = store.record_patch_applied(
        pending,
        outcome,
        outcome_immutable=bridge_module._patch_immutable_authority(outcome),
        outcome_mutable=bridge_module._patch_mutable_authority(outcome),
    )
    successor = REVISION.model_copy(
        update={
            "id": "revision-1",
            "generation": 1,
            "manifest_sha256": "7" * 64,
        }
    )
    mapping_b_base = _mapping(
        request_b,
        generation=2,
        project_snapshot=PROJECT_SNAPSHOT_B,
        task_snapshot=TASK_SNAPSHOT_B,
        etag=ETAG_B,
        predecessor=mapping_a.request_sha256,
    )
    mapping_b = replace(
        mapping_b_base,
        active_revision=successor,
        mutable_authority=replace(
            mapping_b_base.mutable_authority,
            active_revision=successor,
        ),
    )

    with pytest.raises(CoreBridgeStoreContractError, match="applied patch"):
        store.commit_mapping(
            operation,
            mapping_b,
            expected_previous=mapping_a,
            completed_patch=applied,
        )


def test_first_imported_mapping_is_bound_to_finalize_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.openevo.sidecar import test_core_bridge_v1 as bridge_tests

    root = tmp_path / "state"
    project = bridge_tests._local_project(imported=True)
    store = DesktopCoreBridgeStoreV1(root)
    bridge, _, fake_core, _ = bridge_tests._bridge(project, persistence=store)
    monkeypatch.setattr(
        store_module,
        "_before_mapping_commit",
        lambda: (_ for _ in ()).throw(RuntimeError("injected mapping failure")),
    )
    with pytest.raises(bridge_module.DesktopCoreBridgeErrorV1):
        bridge.activate_project(project, idempotency_key="initial-finalize-proof-0001")
    bridge.close()
    operation = store.load_create(LOCAL_PROJECT_ID)
    assert operation is not None and operation.workspace_upload_finalize is not None
    assert operation.workspace_upload_finalize.outcome is not None
    finalized = operation.workspace_upload_finalize.outcome.project
    request = map_project_create_v1(project)
    mapping = bridge_module._mapping_from_project(
        project,
        bridge_module._model_digest(request),
        finalized,
        bridge_tests._capabilities(),
        core_host_identity=HOST_IDENTITY,
        previous_mapping=None,
    )
    unbound = replace(
        mapping,
        project_snapshot=PROJECT_SNAPSHOT_A,
        mutable_authority=replace(
            mapping.mutable_authority,
            project_snapshot=PROJECT_SNAPSHOT_A,
        ),
    )

    with pytest.raises(CoreBridgeStoreContractError, match="finalize outcome"):
        store.commit_mapping(
            operation,
            unbound,
            expected_previous=None,
            completed_patch=None,
        )
    assert fake_core.finalize_requests
    store.close()


def test_mapping_history_retains_applied_transition_proof(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = DesktopCoreBridgeStoreV1(root)
    operation = _bound_create(store)
    request_a = operation.project_create
    mapping_a = _mapping(
        request_a,
        generation=1,
        project_snapshot=PROJECT_SNAPSHOT_A,
        task_snapshot=TASK_SNAPSHOT_A,
        etag=ETAG_A,
        predecessor=None,
    )
    store.commit_mapping(operation, mapping_a, expected_previous=None, completed_patch=None)
    request_b = _request(title="Changed task")
    pending, outcome = _patch_operation(request_a, request_b)
    pending = store.mark_patch_unknown(store.reserve_patch(pending))
    applied = store.record_patch_applied(
        pending,
        outcome,
        outcome_immutable=bridge_module._patch_immutable_authority(outcome),
        outcome_mutable=bridge_module._patch_mutable_authority(outcome),
    )
    mapping_b = _mapping(
        request_b,
        generation=2,
        project_snapshot=PROJECT_SNAPSHOT_B,
        task_snapshot=TASK_SNAPSHOT_B,
        etag=ETAG_B,
        predecessor=mapping_a.request_sha256,
    )
    store.commit_mapping(
        operation,
        mapping_b,
        expected_previous=mapping_a,
        completed_patch=applied,
    )
    store.close()

    with sqlite3.connect(root / store_module.DATABASE_FILENAME) as connection:
        raw = connection.execute(
            "SELECT document_json FROM mapping_history WHERE mapping_generation = 2"
        ).fetchone()[0]
    assert b'"completed_patch"' in raw
    assert b'"outcome"' in raw
    DesktopCoreBridgeStoreV1(root).close()


def test_process_owner_lock_rejects_second_instance(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = DesktopCoreBridgeStoreV1(root)
    with pytest.raises(CoreBridgeStoreStateRootError, match="already owned"):
        DesktopCoreBridgeStoreV1(root)

    script = """
import sys
from desktop.sidecar.core_bridge_store_v1 import DesktopCoreBridgeStoreV1
DesktopCoreBridgeStoreV1(sys.argv[1])
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, os.fspath(root)],
        cwd=Path(__file__).parents[3],
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                (
                    os.fspath(Path(__file__).parents[3] / "src"),
                    os.fspath(Path(__file__).parents[3]),
                )
            ),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "already owned" in completed.stderr
    store.close()
    DesktopCoreBridgeStoreV1(root).close()


@pytest.mark.parametrize(
    "tamper",
    [
        "root_mode",
        "root_replacement",
        "database_mode",
        "database_replacement",
        "lock_replacement",
    ],
)
def test_live_root_and_database_replacement_or_mode_tamper_fails_closed(
    tmp_path: Path,
    tamper: str,
) -> None:
    root = tmp_path / "state"
    store = DesktopCoreBridgeStoreV1(root)
    if tamper == "root_mode":
        os.chmod(root, 0o755)
    elif tamper == "root_replacement":
        moved = tmp_path / "moved"
        root.rename(moved)
        root.mkdir(mode=0o700)
    elif tamper == "database_mode":
        os.chmod(store.database_path, 0o644)
    else:
        name = (
            store_module.DATABASE_FILENAME
            if tamper == "database_replacement"
            else store_module.OWNER_LOCK_FILENAME
        )
        managed = root / name
        managed.rename(root / f"old-{name}")
        managed.touch(mode=0o600)
        os.chmod(managed, 0o600)

    with pytest.raises(CoreBridgeStoreStateRootError):
        store.load_mapping(LOCAL_PROJECT_ID)


@pytest.mark.parametrize(
    "mutation",
    [
        "CREATE VIEW injected AS SELECT local_project_id FROM mappings",
        "CREATE INDEX injected_index ON mappings(core_project_id)",
        "CREATE TRIGGER injected_trigger AFTER INSERT ON mappings BEGIN SELECT 1; END",
    ],
)
def test_startup_rejects_schema_fingerprint_tamper(tmp_path: Path, mutation: str) -> None:
    root = tmp_path / "state"
    store = DesktopCoreBridgeStoreV1(root)
    store.close()
    with sqlite3.connect(root / store_module.DATABASE_FILENAME) as connection:
        connection.execute(mutation)

    with pytest.raises(CoreBridgeStoreSchemaError, match="fingerprint"):
        DesktopCoreBridgeStoreV1(root)


def test_startup_rejects_canonical_row_tamper(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = DesktopCoreBridgeStoreV1(root)
    _bound_create(store)
    store.close()
    with sqlite3.connect(root / store_module.DATABASE_FILENAME) as connection:
        connection.execute(
            "UPDATE create_operations SET document_sha256 = ?",
            ("0" * 64,),
        )

    with pytest.raises(CoreBridgeStoreDataCorruptionError):
        DesktopCoreBridgeStoreV1(root)


def test_startup_recovery_budget_fails_before_document_decode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "state"
    store = DesktopCoreBridgeStoreV1(root)
    _bound_create(store)
    store.close()
    monkeypatch.setattr(store_module, "MAX_RECOVERY_BYTES", 1)

    with pytest.raises(CoreBridgeStoreCapacityError, match="recovery capacity"):
        DesktopCoreBridgeStoreV1(root)


def test_startup_rejects_oversized_row_before_loading_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "state"
    store = DesktopCoreBridgeStoreV1(root)
    _bound_create(store)
    store.close()
    with sqlite3.connect(root / store_module.DATABASE_FILENAME) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE create_operations SET document_json = ?",
            (b"x" * (store_module.MAX_DOCUMENT_BYTES + 1),),
        )
    monkeypatch.setattr(
        store_module,
        "_create_from_value",
        lambda _value: pytest.fail("oversized document reached the JSON decoder"),
    )

    with pytest.raises(CoreBridgeStoreDataCorruptionError):
        DesktopCoreBridgeStoreV1(root)


def test_startup_rejects_database_byte_tamper(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = DesktopCoreBridgeStoreV1(root)
    _bound_create(store)
    database = store.database_path
    store.close()
    with database.open("r+b") as stream:
        stream.seek(100)
        stream.write(b"not-a-valid-sqlite-page")

    with pytest.raises((CoreBridgeStoreSchemaError, CoreBridgeStoreDataCorruptionError)):
        DesktopCoreBridgeStoreV1(root)


def test_startup_rejects_foreign_valid_database_at_managed_path(tmp_path: Path) -> None:
    root_a = tmp_path / "state-a"
    store_a = DesktopCoreBridgeStoreV1(root_a)
    _bound_create(store_a)
    store_a.close()
    foreign_database = (root_a / store_module.DATABASE_FILENAME).read_bytes()

    root_b = tmp_path / "state-b"
    DesktopCoreBridgeStoreV1(root_b).close()
    database_b = root_b / store_module.DATABASE_FILENAME
    database_b.write_bytes(foreign_database)
    os.chmod(database_b, 0o600)

    with pytest.raises(CoreBridgeStoreStateRootError, match="identity"):
        DesktopCoreBridgeStoreV1(root_b)


def test_startup_rejects_foreign_valid_root_at_managed_path(tmp_path: Path) -> None:
    root_a = tmp_path / "state-a"
    store_a = DesktopCoreBridgeStoreV1(root_a)
    _bound_create(store_a)
    store_a.close()

    root_b = tmp_path / "state-b"
    DesktopCoreBridgeStoreV1(root_b).close()
    root_b.rename(tmp_path / "displaced-state-b")
    root_a.rename(root_b)

    with pytest.raises(CoreBridgeStoreStateRootError, match="identity"):
        DesktopCoreBridgeStoreV1(root_b)


def test_startup_rejects_durable_database_rollback(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = DesktopCoreBridgeStoreV1(root)
    operation = _bound_create(store)
    request_a = operation.project_create
    mapping_a = _mapping(
        request_a,
        generation=1,
        project_snapshot=PROJECT_SNAPSHOT_A,
        task_snapshot=TASK_SNAPSHOT_A,
        etag=ETAG_A,
        predecessor=None,
    )
    store.commit_mapping(operation, mapping_a, expected_previous=None, completed_patch=None)
    store.close()
    old_database = (root / store_module.DATABASE_FILENAME).read_bytes()

    store = DesktopCoreBridgeStoreV1(root)
    request_b = _request(title="Changed task")
    pending, outcome = _patch_operation(request_a, request_b)
    pending = store.mark_patch_unknown(store.reserve_patch(pending))
    applied = store.record_patch_applied(
        pending,
        outcome,
        outcome_immutable=bridge_module._patch_immutable_authority(outcome),
        outcome_mutable=bridge_module._patch_mutable_authority(outcome),
    )
    mapping_b = _mapping(
        request_b,
        generation=2,
        project_snapshot=PROJECT_SNAPSHOT_B,
        task_snapshot=TASK_SNAPSHOT_B,
        etag=ETAG_B,
        predecessor=mapping_a.request_sha256,
    )
    store.commit_mapping(
        operation,
        mapping_b,
        expected_previous=mapping_a,
        completed_patch=applied,
    )
    store.close()

    database = root / store_module.DATABASE_FILENAME
    database.write_bytes(old_database)
    os.chmod(database, 0o600)
    with pytest.raises(CoreBridgeStoreStateRootError, match="rollback"):
        DesktopCoreBridgeStoreV1(root)


def test_nonempty_store_without_identity_marker_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = DesktopCoreBridgeStoreV1(root)
    _bound_create(store)
    store.close()
    (root / store_module.IDENTITY_MARKER_FILENAME).unlink()

    with pytest.raises(CoreBridgeStoreStateRootError, match="identity marker"):
        DesktopCoreBridgeStoreV1(root)


def test_fresh_initial_schema_hot_journal_rolls_back_then_bootstraps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "state"
    database, journal, _anchor = _create_uncommitted_initial_schema_hot_journal(root)
    original_initialize = DesktopCoreBridgeStoreV1._initialize_schema
    recovered_state: tuple[bool, int, bool, int, int] | None = None

    def inspect_recovered_state(
        self: DesktopCoreBridgeStoreV1,
        *,
        fresh_database: bool,
    ) -> None:
        nonlocal recovered_state
        recovered_state = (
            fresh_database,
            os.fstat(self._database_fd).st_size,
            journal.exists(),
            self._connection.execute("PRAGMA page_count").fetchone()[0],
            self._connection.execute("SELECT count(*) FROM sqlite_schema").fetchone()[0],
        )
        original_initialize(self, fresh_database=fresh_database)

    monkeypatch.setattr(
        DesktopCoreBridgeStoreV1,
        "_initialize_schema",
        inspect_recovered_state,
    )

    store = DesktopCoreBridgeStoreV1(root)
    assert recovered_state == (True, 0, False, 0, 0)
    assert database.stat().st_size > 0
    store.close()

    reopened = DesktopCoreBridgeStoreV1(root)
    with sqlite3.connect(reopened.database_path) as connection:
        assert connection.execute(
            "SELECT marker_generation, binding_state FROM store_identity"
        ).fetchone() == (0, "bound")
    reopened.close()


def test_initial_schema_hot_journal_rollback_failure_is_not_claimed(tmp_path: Path) -> None:
    root = tmp_path / "state"
    database, journal, _anchor = _create_uncommitted_initial_schema_hot_journal(root)
    journal.write_bytes(journal.read_bytes()[:100])
    os.chmod(journal, 0o600)
    database_size = database.stat().st_size

    with pytest.raises(CoreBridgeStoreDataCorruptionError, match="safely opened"):
        DesktopCoreBridgeStoreV1(root)

    assert database.stat().st_size == database_size
    assert (root / store_module.IDENTITY_MARKER_FILENAME).stat().st_size == 0


def test_non_full_sqlite_default_does_not_open_or_recover_target_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "state"
    database, journal, _anchor = _create_uncommitted_initial_schema_hot_journal(root)
    database_before = database.read_bytes()
    journal_before = journal.read_bytes()
    original_connect = store_module.sqlite3.connect
    opened: list[object] = []

    def connect_with_non_full_default(
        database_name: object,
        *args: object,
        **kwargs: object,
    ) -> sqlite3.Connection:
        opened.append(database_name)
        connection = original_connect(database_name, *args, **kwargs)
        if database_name == ":memory:":
            connection.execute("PRAGMA synchronous = OFF")
        return connection

    monkeypatch.setattr(store_module.sqlite3, "connect", connect_with_non_full_default)

    with pytest.raises(CoreBridgeStoreStateRootError, match="default synchronous is not FULL"):
        DesktopCoreBridgeStoreV1(root)

    assert opened == [":memory:"]
    assert database.read_bytes() == database_before
    assert journal.read_bytes() == journal_before


def test_pending_binding_proves_empty_authority_before_each_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "state"
    _leave_pending_store_binding(root, monkeypatch)
    original_usage = DesktopCoreBridgeStoreV1._recovery_usage
    original_digest = DesktopCoreBridgeStoreV1._authority_digest
    calls: list[str] = []

    def record_usage(connection: sqlite3.Connection) -> tuple[int, int]:
        calls.append("count-length")
        return original_usage(connection)

    def record_digest(connection: sqlite3.Connection) -> str:
        calls.append("digest")
        return original_digest(connection)

    monkeypatch.setattr(
        DesktopCoreBridgeStoreV1,
        "_recovery_usage",
        staticmethod(record_usage),
    )
    monkeypatch.setattr(
        DesktopCoreBridgeStoreV1,
        "_authority_digest",
        staticmethod(record_digest),
    )

    store = DesktopCoreBridgeStoreV1(root)
    store.close()

    assert calls[:4] == ["count-length", "digest", "count-length", "digest"]


@pytest.mark.parametrize(
    ("limit_name", "limit", "row_count", "history_limit"),
    [
        ("MAX_RECOVERY_ROWS", 1, 2, 1),
        ("MAX_RECOVERY_BYTES", 1, 1, store_module.DEFAULT_MAX_MAPPING_HISTORY_ROWS),
    ],
    ids=["rows", "bytes"],
)
def test_pending_binding_capacity_fails_before_authority_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    limit: int,
    row_count: int,
    history_limit: int,
) -> None:
    root = tmp_path / "state"
    _leave_pending_store_binding(root, monkeypatch)
    with sqlite3.connect(root / store_module.DATABASE_FILENAME) as connection:
        connection.executemany(
            """
            INSERT INTO create_operations(
                local_project_id, state, document_json, document_sha256
            ) VALUES (?, 'pre_create', X'7B7D', ?)
            """,
            [(f"corrupt-{index}", "0" * 64) for index in range(row_count)],
        )
    monkeypatch.setattr(store_module, limit_name, limit)
    monkeypatch.setattr(
        DesktopCoreBridgeStoreV1,
        "_authority_digest",
        staticmethod(
            lambda _connection: pytest.fail(
                "over-capacity pending authority reached materializing digest query"
            )
        ),
    )

    with pytest.raises(CoreBridgeStoreCapacityError, match="pending bridge authority"):
        DesktopCoreBridgeStoreV1(
            root,
            max_mapping_history_rows=history_limit,
        )


def test_pending_identity_oversize_is_not_materialized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "state"
    _leave_pending_store_binding(root, monkeypatch)
    with sqlite3.connect(root / store_module.DATABASE_FILENAME) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE store_identity SET store_id = ?",
            ("x" * (store_module.MAX_IDENTITY_BYTES + 1),),
        )
    original_complete = DesktopCoreBridgeStoreV1._complete_pending_store_binding

    def reject_oversized_python_text(
        self: DesktopCoreBridgeStoreV1,
        connection: sqlite3.Connection,
    ) -> None:
        def bounded_text(raw: bytes) -> str:
            if len(raw) > store_module.MAX_IDENTITY_BYTES:
                pytest.fail("oversized store identity entered Python")
            return raw.decode("utf-8")

        connection.text_factory = bounded_text
        original_complete(self, connection)

    monkeypatch.setattr(
        DesktopCoreBridgeStoreV1,
        "_complete_pending_store_binding",
        reject_oversized_python_text,
    )

    with pytest.raises(CoreBridgeStoreStateRootError, match="store identity is invalid"):
        DesktopCoreBridgeStoreV1(root)


def test_recovered_empty_hot_journal_with_foreign_marker_is_not_fresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "source"
    source_store = DesktopCoreBridgeStoreV1(source_root)
    source_store.close()
    foreign_marker = (source_root / store_module.IDENTITY_MARKER_FILENAME).read_bytes()

    root = tmp_path / "state"
    database, journal, _anchor = _create_uncommitted_initial_schema_hot_journal(root)
    marker = root / store_module.IDENTITY_MARKER_FILENAME
    marker.write_bytes(foreign_marker)
    os.chmod(marker, 0o600)
    original_initialize = DesktopCoreBridgeStoreV1._initialize_schema
    fresh_after_recovery: bool | None = None

    def observe_fresh_eligibility(
        self: DesktopCoreBridgeStoreV1,
        *,
        fresh_database: bool,
    ) -> None:
        nonlocal fresh_after_recovery
        fresh_after_recovery = fresh_database
        original_initialize(self, fresh_database=fresh_database)

    monkeypatch.setattr(
        DesktopCoreBridgeStoreV1,
        "_initialize_schema",
        observe_fresh_eligibility,
    )

    with pytest.raises(CoreBridgeStoreSchemaError, match="not eligible for fresh creation"):
        DesktopCoreBridgeStoreV1(root)

    assert fresh_after_recovery is False
    assert database.stat().st_size == 0
    assert not journal.exists()
    assert marker.read_bytes() == foreign_marker


def test_physically_nonempty_empty_schema_is_not_fresh(tmp_path: Path) -> None:
    root = tmp_path / "state"
    database, _anchor = _prepare_unpublished_store_files(root)
    with sqlite3.connect(database) as connection:
        connection.execute("VACUUM")
        assert connection.execute("PRAGMA user_version").fetchone() == (0,)
        assert (
            connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'").fetchall()
            == []
        )
    os.chmod(database, 0o600)
    assert database.stat().st_size > 0

    with pytest.raises(CoreBridgeStoreSchemaError, match="not eligible for fresh creation"):
        DesktopCoreBridgeStoreV1(root)


@pytest.mark.parametrize(
    "failed_stage",
    ["inner-marker", "root-anchor", "binding-commit"],
)
def test_fresh_pending_identity_binding_recovers_after_marker_publish_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_stage: str,
) -> None:
    root = tmp_path / "state"
    attribute = {
        "inner-marker": "_write_identity_marker",
        "root-anchor": "_write_root_anchor",
        "binding-commit": "_complete_pending_store_binding",
    }[failed_stage]
    original = getattr(DesktopCoreBridgeStoreV1, attribute)
    failed = False

    def fail_marker_once(self: DesktopCoreBridgeStoreV1, identity: dict[str, object]) -> None:
        nonlocal failed
        if not failed:
            failed = True
            if failed_stage == "inner-marker":
                name = store_module.IDENTITY_MARKER_FILENAME
                dir_fd = self._root_fd
            else:
                name = self._anchor_name
                dir_fd = self._anchor_parent_fd
            descriptor = os.open(name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=dir_fd)
            try:
                os.ftruncate(descriptor, store_module.MARKER_FILE_BYTES)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            raise OSError("injected initial marker publication failure")
        original(self, identity)

    def fail_binding_once(
        self: DesktopCoreBridgeStoreV1,
        connection: sqlite3.Connection,
    ) -> None:
        nonlocal failed
        if not failed:
            failed = True
            identity = self._identity_row(connection)
            self._write_identity_marker(identity)
            self._write_root_anchor(identity)
            raise OSError("injected initial marker publication failure")
        original(self, connection)

    monkeypatch.setattr(
        DesktopCoreBridgeStoreV1,
        attribute,
        fail_binding_once if failed_stage == "binding-commit" else fail_marker_once,
    )
    with pytest.raises(OSError, match="injected initial marker"):
        DesktopCoreBridgeStoreV1(root)

    reopened = DesktopCoreBridgeStoreV1(root)
    assert reopened.load_create(LOCAL_PROJECT_ID) is None
    with sqlite3.connect(reopened.database_path) as connection:
        assert connection.execute("SELECT binding_state FROM store_identity").fetchone() == (
            "bound",
        )
    reopened.close()


@pytest.mark.parametrize("failed_write", [1, 2], ids=["inner-marker", "root-anchor"])
def test_fresh_pending_identity_binding_recovers_after_short_marker_pwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_write: int,
) -> None:
    root = tmp_path / "state"
    original_pwrite = store_module.os.pwrite
    write_count = 0

    def short_pwrite(descriptor: int, data: bytes, offset: int) -> int:
        nonlocal write_count
        write_count += 1
        if write_count == failed_write:
            written = 32
            assert original_pwrite(descriptor, data[:written], offset) == written
            return written
        return original_pwrite(descriptor, data, offset)

    monkeypatch.setattr(store_module.os, "pwrite", short_pwrite)
    with pytest.raises(CoreBridgeStoreStateRootError, match="write was incomplete"):
        DesktopCoreBridgeStoreV1(root)

    reopened = DesktopCoreBridgeStoreV1(root)
    with sqlite3.connect(reopened.database_path) as connection:
        assert connection.execute("SELECT binding_state FROM store_identity").fetchone() == (
            "bound",
        )
    reopened.close()


@pytest.mark.parametrize("failed_write", [1, 2], ids=["inner-marker", "root-anchor"])
def test_fresh_pending_identity_binding_rejects_dirty_inactive_marker_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_write: int,
) -> None:
    root = tmp_path / "state"
    original_pwrite = store_module.os.pwrite
    write_count = 0

    def short_pwrite(descriptor: int, data: bytes, offset: int) -> int:
        nonlocal write_count
        write_count += 1
        if write_count == failed_write:
            written = 32
            assert original_pwrite(descriptor, data[:written], offset) == written
            return written
        return original_pwrite(descriptor, data, offset)

    monkeypatch.setattr(store_module.os, "pwrite", short_pwrite)
    with pytest.raises(CoreBridgeStoreStateRootError, match="write was incomplete"):
        DesktopCoreBridgeStoreV1(root)

    partial = (
        root / store_module.IDENTITY_MARKER_FILENAME
        if failed_write == 1
        else next(root.parent.glob(f"{store_module.ROOT_ANCHOR_PREFIX}*.identity"))
    )
    with partial.open("r+b") as stream:
        stream.seek(store_module.MARKER_SLOT_BYTES)
        stream.write(b"!")
        stream.flush()
        os.fsync(stream.fileno())

    with pytest.raises(CoreBridgeStoreStateRootError, match="no valid slot"):
        DesktopCoreBridgeStoreV1(root)


@pytest.mark.parametrize("target", ["inner-marker", "root-anchor"])
def test_bound_store_rejects_corrupt_only_published_marker(
    tmp_path: Path,
    target: str,
) -> None:
    root = tmp_path / "state"
    store = DesktopCoreBridgeStoreV1(root)
    anchor_name = store._anchor_name
    store.close()
    marker = (
        root / store_module.IDENTITY_MARKER_FILENAME
        if target == "inner-marker"
        else root.parent / anchor_name
    )
    with marker.open("r+b") as stream:
        stream.seek(0)
        stream.write(b"!")
        stream.flush()
        os.fsync(stream.fileno())

    with pytest.raises(CoreBridgeStoreStateRootError, match="no valid slot"):
        DesktopCoreBridgeStoreV1(root)


def test_darwin_sqlite_connection_uses_managed_path_and_persists_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DarwinPlatform:
        platform = "darwin"

    monkeypatch.setattr(store_module, "sys", DarwinPlatform())
    root = tmp_path / "state"
    store = DesktopCoreBridgeStoreV1(root)
    database_rows = store._connection.execute("PRAGMA database_list").fetchall()
    operation = _bound_create(store)

    assert Path(database_rows[0][2]).resolve() == store.database_path.resolve()
    store.close()
    assert not (root / store_module.JOURNAL_FILENAME).exists()

    reopened = DesktopCoreBridgeStoreV1(root)
    assert reopened.load_create(operation.local_project_id) == operation
    reopened.close()


def test_sqlite_connection_accepts_an_inode_identical_ancestor_alias(tmp_path: Path) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir(mode=0o700)
    (real_parent / "container").mkdir(mode=0o700)
    alias_parent = tmp_path / "alias"
    alias_parent.symlink_to(real_parent, target_is_directory=True)

    store = DesktopCoreBridgeStoreV1(alias_parent / "container" / "state")
    database_rows = store._connection.execute("PRAGMA database_list").fetchall()
    opened_path = Path(database_rows[0][2])

    assert opened_path.is_absolute()
    assert os.stat(opened_path).st_ino == os.stat(store.database_path).st_ino
    operation = _bound_create(store)
    assert store.load_create(operation.local_project_id) == operation
    store.close()


def test_open_store_rejects_database_pathname_replacement(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = DesktopCoreBridgeStoreV1(root)
    operation = _bound_create(store)
    managed = store.database_path
    displaced = tmp_path / "held-core-bridge.sqlite3"
    os.replace(managed, displaced)
    managed.touch(mode=0o600)
    os.chmod(managed, 0o600)
    try:
        with pytest.raises(CoreBridgeStoreStateRootError, match="identity changed"):
            store.load_create(operation.local_project_id)
    finally:
        managed.unlink()
        os.replace(displaced, managed)
        store.close()


def test_database_connect_path_swap_fails_without_writing_replacement_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "state"
    database = root / store_module.DATABASE_FILENAME
    displaced = tmp_path / "displaced.sqlite3"
    original_connect = store_module.sqlite3.connect
    swapped = False
    opened_database_identity: tuple[int, int] | None = None

    def swap_before_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        nonlocal opened_database_identity, swapped
        if args[0] == ":memory:":
            return original_connect(*args, **kwargs)
        if not swapped:
            swapped = True
            database.rename(displaced)
            database.touch(mode=0o600)
            os.chmod(database, 0o600)
        connection = original_connect(*args, **kwargs)
        opened_path = Path(connection.execute("PRAGMA database_list").fetchone()[2])
        opened_stat = opened_path.stat()
        opened_database_identity = (opened_stat.st_dev, opened_stat.st_ino)
        return connection

    monkeypatch.setattr(store_module.sqlite3, "connect", swap_before_connect)
    with pytest.raises(CoreBridgeStoreStateRootError, match="bridge file|database"):
        DesktopCoreBridgeStoreV1(root)

    opened_target = database if sys.platform == "darwin" else displaced
    opened_target_stat = opened_target.stat()
    assert opened_database_identity == (
        opened_target_stat.st_dev,
        opened_target_stat.st_ino,
    )
    assert database.read_bytes() == b""
    assert displaced.read_bytes() == b""


def test_fresh_database_does_not_claim_unknown_old_state(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    unknown = root / "legacy-bridge-state.json"
    unknown.write_text("{}", encoding="utf-8")
    os.chmod(unknown, 0o600)

    with pytest.raises(CoreBridgeStoreStateRootError, match="unknown managed state"):
        DesktopCoreBridgeStoreV1(root)


@pytest.mark.parametrize("failed_stage", ["inner-marker", "root-anchor"])
def test_startup_recovers_only_one_adjacent_committed_marker_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_stage: str,
) -> None:
    root = tmp_path / "state"
    store = DesktopCoreBridgeStoreV1(root)
    operation = _create_operation()
    attribute = (
        "_write_identity_marker" if failed_stage == "inner-marker" else "_write_root_anchor"
    )
    original = getattr(store, attribute)
    failed = False

    def fail_once(identity: dict[str, object]) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("injected durable marker write failure")
        original(identity)

    monkeypatch.setattr(store, attribute, fail_once)
    with pytest.raises(OSError, match="injected"):
        store.reserve_create(operation)
    store.close()

    reopened = DesktopCoreBridgeStoreV1(root)
    assert reopened.load_create(LOCAL_PROJECT_ID) == operation
    reopened.close()


@pytest.mark.parametrize("unsafe", ["root_symlink", "database_symlink", "database_hardlink"])
def test_rejects_unsafe_state_files(tmp_path: Path, unsafe: str) -> None:
    root = tmp_path / "state"
    if unsafe == "root_symlink":
        target = tmp_path / "target"
        target.mkdir(mode=0o700)
        root.symlink_to(target, target_is_directory=True)
    elif unsafe == "database_symlink":
        root.mkdir(mode=0o700)
        target = tmp_path / "database"
        target.touch(mode=0o600)
        (root / store_module.DATABASE_FILENAME).symlink_to(target)
    else:
        store = DesktopCoreBridgeStoreV1(root)
        store.close()
        os.link(root / store_module.DATABASE_FILENAME, tmp_path / "database-link")

    with pytest.raises(CoreBridgeStoreStateRootError):
        DesktopCoreBridgeStoreV1(root)


def test_closed_serialization_contains_no_pickle_secret_or_host_path(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = DesktopCoreBridgeStoreV1(root)
    _bound_create(store)
    store.close()
    raw = (root / store_module.DATABASE_FILENAME).read_bytes()

    assert b"pickle" not in raw.lower()
    assert b"bearer-secret-value" not in raw
    assert b"/Users/researcher/private-workspace" not in raw
    assert b"core-host-key-1" in raw


@pytest.mark.parametrize("action", ["read", "write", "close"])
def test_forked_store_checks_pid_before_inherited_thread_lock(
    tmp_path: Path,
    action: str,
) -> None:
    if not hasattr(os, "fork"):
        pytest.skip("fork is unavailable")
    store = DesktopCoreBridgeStoreV1(tmp_path / "state")
    locked = threading.Event()
    release = threading.Event()

    def hold_transaction_lock() -> None:
        with store._transaction_lock:
            locked.set()
            release.wait(timeout=10)

    holder = threading.Thread(target=hold_transaction_lock)
    holder.start()
    assert locked.wait(timeout=2)
    pid = os.fork()
    if pid == 0:
        import signal

        signal.alarm(2)
        try:
            if action == "read":
                store.load_mapping(LOCAL_PROJECT_ID)
            elif action == "write":
                store.reserve_create(_create_operation())
            else:
                store.close()
        except CoreBridgeStoreStateRootError:
            os._exit(0)
        os._exit(1)
    release.set()
    holder.join(timeout=2)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    store.close()


def test_forked_inherited_store_is_rejected(tmp_path: Path) -> None:
    if not hasattr(os, "fork"):
        pytest.skip("fork is unavailable")
    store = DesktopCoreBridgeStoreV1(tmp_path / "state")
    pid = os.fork()
    if pid == 0:
        try:
            store.load_mapping(LOCAL_PROJECT_ID)
        except CoreBridgeStoreStateRootError:
            os._exit(0)
        os._exit(1)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    with pytest.raises(CoreBridgeStoreStateRootError, match="already owned"):
        DesktopCoreBridgeStoreV1(store.state_root)
    store.close()
