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
LOCAL_PROJECT_ID = "local-project-1"
CORE_PROJECT_ID = "core-project-1"
PROFILE_ID = "profile-1"
HOST_IDENTITY = "core-host-key-1"
REGISTRY_DIGEST = "4" * 64
ETAG_A = '"' + "a" * 64 + '"'
ETAG_B = '"' + "b" * 64 + '"'


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
    return store.bind_created_project(operation, CORE_PROJECT_ID)


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
    with pytest.raises(bridge_tests.CoreClientErrorV1):
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


def test_create_reservation_and_every_transition_are_exact_full_row_cas(tmp_path: Path) -> None:
    store = DesktopCoreBridgeStoreV1(tmp_path / "state")
    first = store.reserve_create(_create_operation(key="activate-project-0001"))
    replacement = store.reserve_create(_create_operation(key="activate-project-0002"))
    assert replacement.idempotency_key == "activate-project-0002"

    with pytest.raises(CoreBridgeStoreConflictError):
        store.mark_create_unknown(first)

    unknown = store.mark_create_unknown(replacement)
    assert store.reserve_create(_create_operation(key="activate-project-0003")) == unknown
    bound = store.bind_created_project(unknown, CORE_PROJECT_ID)

    changed = replace(
        bound,
        workspace_upload_id="upload-1",
        workspace_upload_project_snapshot=PROJECT_SNAPSHOT_A,
    )
    assert store.update_create(changed, expected_previous=bound) == changed

    with pytest.raises(CoreBridgeStoreConflictError):
        store.update_create(bound, expected_previous=bound)


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
    mapping_b = replace(
        mapping_a,
        project_etag=ETAG_B,
        mapping_generation=2,
        predecessor_request_sha256=mapping_a.request_sha256,
    )

    with pytest.raises(CoreBridgeStoreCapacityError):
        store.commit_mapping(
            operation, mapping_b, expected_previous=mapping_a, completed_patch=None
        )

    assert store.load_mapping(LOCAL_PROJECT_ID) == mapping_a
    assert store.load_mapping_history(LOCAL_PROJECT_ID) == (mapping_a,)


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


def test_forked_inherited_store_is_rejected(tmp_path: Path) -> None:
    if not hasattr(os, "fork"):
        pytest.skip("fork is unavailable")
    store = DesktopCoreBridgeStoreV1(tmp_path / "state")
    pid = os.fork()
    if pid == 0:
        try:
            store.load_mapping(LOCAL_PROJECT_ID)
        except CoreBridgeStoreStateRootError:
            store.close()
            os._exit(0)
        os._exit(1)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    with pytest.raises(CoreBridgeStoreStateRootError, match="already owned"):
        DesktopCoreBridgeStoreV1(store.state_root)
