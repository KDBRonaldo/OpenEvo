from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import errno
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from typing import Literal, cast

import pytest

import desktop.sidecar.provider_store as provider_store_module
from desktop.sidecar.contracts.v1.models import (
    ApiErrorV1,
    ConnectionOperationResultV1,
    CredentialSlotStatusV1,
    LocalOperationV1,
    ProjectCreateV1,
    ProjectOperationResultV1,
    ProjectPatchV1,
    RemoteProfilePatchV1,
    RemoteProfileV1,
    ResourceRefV1,
)
from desktop.sidecar.provider_store import (
    ContractValidationError,
    CursorExpiredError,
    CursorInvalidError,
    DesktopProviderStore,
    ETagConflictError,
    IdempotencyCapacityError,
    IdempotencyConflictError,
    ProviderDataCorruptionError,
    ProviderMutation,
    ProviderSchemaError,
    ProviderStateRootError,
    ResourceInUseError,
    ResourceNotFoundError,
)
from openevo.backend.contracts.v1.models import ErrorCategory, ErrorSeverity, RepairAction


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


def _profile(name: str = "Research server") -> dict[str, object]:
    return {
        "name": name,
        "host": "compute.example.org",
        "port": 2222,
        "user": "researcher",
        "authentication_kind": "native_password",
        "proxy": {
            "https_url": "https://proxy.example.org",
            "no_proxy": ("localhost", "127.0.0.1"),
        },
    }


def _project(profile_id: str, *, name: str = "Protein design") -> dict[str, object]:
    return {
        "name": name,
        "profile_id": profile_id,
        "task": {"title": "Design", "objective": "Improve held-out stability."},
        "source": {"kind": "scratch", "display_name": "New project"},
        "execution": {
            "mode": "codex_subscription_transcript",
            "codex_model": "gpt-5",
        },
        "evolution": {
            "targets": {
                "future_target": {
                    "enabled": False,
                    "method": "plugin.future.v9",
                    "config": {
                        "unknown_nested": {"weights": [1, 2.5, True, None]},
                        "future_flag": "preserve-me",
                        "target_path": "AGENTS.md",
                    },
                }
            }
        },
    }


def _create_profile(
    store: DesktopProviderStore,
    *,
    name: str = "Research server",
    key: str = "profile-create-0001",
):
    return store.create_profile(_profile(name), idempotency_key=key)


def test_initializes_versioned_private_sqlite_store(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    _create_profile(store)

    assert root.stat().st_mode & 0o777 == 0o700
    managed_files = [path for path in root.iterdir() if path.is_file()]
    assert {path.name for path in managed_files} >= {
        "provider.sqlite3",
        "provider.lock",
        "cursor-signing.key",
    }
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in managed_files)

    assert tuple(store._connection.execute("PRAGMA user_version").fetchone()) == (2,)
    assert tuple(store._connection.execute("PRAGMA journal_mode").fetchone()) == ("delete",)
    assert tuple(store._connection.execute("PRAGMA max_page_count").fetchone()) == (
        store._max_page_count,
    )
    assert tuple(store._connection.execute("PRAGMA journal_size_limit").fetchone()) == (
        store._journal_size_limit,
    )
    assert [
        tuple(row)
        for row in store._connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    ] == [(1,), (2,)]
    assert not (root / "provider.sqlite3-wal").exists()
    assert not (root / "provider.sqlite3-shm").exists()


def test_process_lifecycle_owner_lock_rejects_a_second_store(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)

    with pytest.raises(ProviderStateRootError, match="already owned"):
        DesktopProviderStore(root)

    store.close()
    DesktopProviderStore(root).close()


def test_cursor_key_creation_retries_short_writes_and_leaves_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_write = os.write
    write_calls = 0

    def short_write(fd: int, value: bytes) -> int:
        nonlocal write_calls
        write_calls += 1
        return original_write(fd, value[:3])

    monkeypatch.setattr(provider_store_module.os, "write", short_write)
    root = tmp_path / "state"
    store = DesktopProviderStore(root)

    assert write_calls > 1
    assert len((root / "cursor-signing.key").read_bytes()) == 32
    assert not tuple(root.glob(".cursor-signing.key.tmp-*"))
    store.close()


def test_cursor_key_publication_failure_cleans_private_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_publication(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EIO, "injected publication failure")

    monkeypatch.setattr(provider_store_module, "_rename_noreplace", fail_publication)
    root = tmp_path / "state"

    with pytest.raises(ProviderStateRootError, match="publish cursor signing key"):
        DesktopProviderStore(root)

    assert not (root / "cursor-signing.key").exists()
    assert not tuple(root.glob(".cursor-signing.key.tmp-*"))


def test_cursor_key_fsync_failure_cleans_private_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    (root / "provider.lock").touch(mode=0o600)
    (root / "provider.sqlite3").touch(mode=0o600)
    original_fsync = os.fsync
    calls = 0

    def fail_first_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(errno.EIO, "injected key fsync failure")
        original_fsync(fd)

    monkeypatch.setattr(provider_store_module.os, "fsync", fail_first_fsync)
    with pytest.raises(ProviderStateRootError, match="cursor signing key"):
        DesktopProviderStore(root)

    assert not (root / "cursor-signing.key").exists()
    assert not tuple(root.glob(".cursor-signing.key.tmp-*"))


def test_cursor_key_is_fsynced_before_no_replace_publication_and_directory_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    (root / "provider.lock").touch(mode=0o600)
    (root / "provider.sqlite3").touch(mode=0o600)
    root_stat = root.stat()
    original_fsync = os.fsync
    original_rename = provider_store_module._rename_noreplace
    events: list[str] = []

    def tracked_fsync(fd: int) -> None:
        descriptor_stat = os.fstat(fd)
        events.append(
            "directory_fsync"
            if (descriptor_stat.st_dev, descriptor_stat.st_ino)
            == (root_stat.st_dev, root_stat.st_ino)
            else "file_fsync"
        )
        original_fsync(fd)

    def tracked_rename(source: str, destination: str, *, directory_fd: int) -> None:
        source_stat = os.stat(source, dir_fd=directory_fd, follow_symlinks=False)
        assert stat.S_IMODE(source_stat.st_mode) == 0o600
        assert source_stat.st_size == 32
        events.append("publish")
        original_rename(source, destination, directory_fd=directory_fd)

    monkeypatch.setattr(provider_store_module.os, "fsync", tracked_fsync)
    monkeypatch.setattr(provider_store_module, "_rename_noreplace", tracked_rename)
    store = DesktopProviderStore(root)

    assert events[:3] == ["file_fsync", "publish", "directory_fsync"]
    store.close()


def test_concurrent_cursor_key_publication_keeps_exactly_one_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    (root / "provider.sqlite3").touch(mode=0o600)

    barrier = threading.Barrier(2)
    publication_results: list[str] = []
    publication_lock = threading.Lock()
    original_rename = provider_store_module._rename_noreplace

    def tracked_rename(source: str, destination: str, *, directory_fd: int) -> None:
        try:
            original_rename(source, destination, directory_fd=directory_fd)
        except FileExistsError:
            with publication_lock:
                publication_results.append("lost")
            raise
        with publication_lock:
            publication_results.append("won")

    monkeypatch.setattr(provider_store_module, "_rename_noreplace", tracked_rename)

    def initialize_key() -> bytes:
        store = DesktopProviderStore.__new__(DesktopProviderStore)
        store._closed = False
        store._state_root = root
        store._root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        root_stat = os.fstat(store._root_fd)
        store._root_identity = (root_stat.st_dev, root_stat.st_ino)
        try:
            barrier.wait(timeout=5)
            return store._load_or_create_cursor_key()
        finally:
            os.close(store._root_fd)
            store._closed = True

    with ThreadPoolExecutor(max_workers=2) as executor:
        keys = tuple(executor.map(lambda _index: initialize_key(), range(2)))

    assert keys[0] == keys[1] == (root / "cursor-signing.key").read_bytes()
    assert len(keys[0]) == 32
    assert sorted(publication_results) == ["lost", "won"]
    assert not tuple(root.glob(".cursor-signing.key.tmp-*"))


def test_invalid_cursor_key_is_recovered_only_for_never_initialized_store(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    (root / "cursor-signing.key").write_bytes(b"partial")
    os.chmod(root / "cursor-signing.key", 0o600)

    store = DesktopProviderStore(root)
    assert len((root / "cursor-signing.key").read_bytes()) == 32
    store.close()

    (root / "cursor-signing.key").write_bytes(b"partial")
    with pytest.raises(ProviderStateRootError, match="invalid size"):
        DesktopProviderStore(root)
    assert (root / "cursor-signing.key").read_bytes() == b"partial"

    (root / "cursor-signing.key").unlink()
    with pytest.raises(ProviderStateRootError, match="missing from an initialized"):
        DesktopProviderStore(root)


def test_sqlite_connection_uses_the_canonical_database_path(tmp_path: Path) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    database_list = store._connection.execute("PRAGMA database_list").fetchall()
    main = [row for row in database_list if row[1] == "main"]

    assert len(main) == 1
    assert Path(main[0][2]).resolve() == store.database_path.resolve()
    assert not hasattr(store, "_database_fd")


def test_standard_sqlite_rollback_journal_recovers_a_crashed_transaction(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    profile = _create_profile(store)
    store.close()
    script = """
import os
import sys
from desktop.sidecar.provider_store import DesktopProviderStore

store = DesktopProviderStore(sys.argv[1])
store._connection.execute("BEGIN IMMEDIATE")
store._connection.execute(
    "UPDATE remote_profiles SET name = 'uncommitted-crash-value' WHERE profile_id = ?",
    (sys.argv[2],),
)
os._exit(91)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, os.fspath(root), profile.profile_id],
        check=False,
        cwd=Path(__file__).parents[3],
    )

    assert completed.returncode == 91
    assert (root / "provider.sqlite3-journal").exists()
    reopened = DesktopProviderStore(root)
    assert reopened.get_profile(profile.profile_id).name == profile.name


@pytest.mark.parametrize("kind", ["symlink", "file"])
def test_rejects_non_directory_or_symlink_state_root(tmp_path: Path, kind: str) -> None:
    root = tmp_path / "state"
    if kind == "symlink":
        target = tmp_path / "target"
        target.mkdir()
        root.symlink_to(target, target_is_directory=True)
    else:
        root.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ProviderStateRootError):
        DesktopProviderStore(root)


@pytest.mark.parametrize(
    "unsafe",
    [
        "database_symlink",
        "database_hardlink",
        "journal_symlink",
        "journal_hardlink",
        "key_mode",
        "wal_file",
    ],
)
def test_rejects_unsafe_managed_files(tmp_path: Path, unsafe: str) -> None:
    root = tmp_path / "state"
    if unsafe == "database_symlink":
        root.mkdir(mode=0o700)
        target = tmp_path / "database"
        target.touch(mode=0o600)
        (root / "provider.sqlite3").symlink_to(target)
    elif unsafe == "database_hardlink":
        store = DesktopProviderStore(root)
        store.close()
        os.link(root / "provider.sqlite3", tmp_path / "database-link")
    elif unsafe == "journal_symlink":
        store = DesktopProviderStore(root)
        store.close()
        target = tmp_path / "journal"
        target.touch(mode=0o600)
        (root / "provider.sqlite3-journal").symlink_to(target)
    elif unsafe == "journal_hardlink":
        store = DesktopProviderStore(root)
        store.close()
        journal = root / "provider.sqlite3-journal"
        journal.touch(mode=0o600)
        os.link(journal, tmp_path / "journal-link")
    elif unsafe == "key_mode":
        store = DesktopProviderStore(root)
        store.close()
        os.chmod(root / "cursor-signing.key", 0o644)
    else:
        store = DesktopProviderStore(root)
        store.close()
        (root / "provider.sqlite3-wal").touch(mode=0o600)

    with pytest.raises(ProviderStateRootError):
        DesktopProviderStore(root)


def test_rejects_unknown_schema_version(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    store.close()
    with sqlite3.connect(root / "provider.sqlite3") as connection:
        connection.execute("PRAGMA user_version = 99")

    with pytest.raises(ProviderSchemaError):
        DesktopProviderStore(root)


def test_migrates_a_canonical_v1_store_to_v2(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    (root / "provider.lock").touch(mode=0o600)
    (root / "provider.sqlite3-journal").touch(mode=0o600)
    (root / "cursor-signing.key").write_bytes(b"k" * 32)
    os.chmod(root / "cursor-signing.key", 0o600)
    with sqlite3.connect(root / "provider.sqlite3") as connection:
        for statement in provider_store_module._SCHEMA_V1:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (1, ?)",
            ("2026-07-14T12:00:00.000000Z",),
        )
        connection.execute("PRAGMA user_version = 1")
    os.chmod(root / "provider.sqlite3", 0o600)

    store = DesktopProviderStore(root)

    assert tuple(store._connection.execute("PRAGMA user_version").fetchone()) == (2,)
    assert [
        tuple(row)
        for row in store._connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    ] == [(1,), (2,)]
    profile = _create_profile(store, key="post-migration-create")
    assert store.get_profile(profile.profile_id) == profile


@pytest.mark.parametrize(
    "mutation",
    [
        "CREATE VIEW unexpected_view AS SELECT profile_id FROM remote_profiles",
        "CREATE TRIGGER unexpected_trigger AFTER INSERT ON projects BEGIN SELECT 1; END",
        "CREATE INDEX unexpected_index ON projects(state)",
    ],
)
def test_rejects_noncanonical_schema_objects(tmp_path: Path, mutation: str) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    store.close()
    with sqlite3.connect(root / "provider.sqlite3") as connection:
        connection.execute(mutation)

    with pytest.raises(ProviderSchemaError, match="fingerprint"):
        DesktopProviderStore(root)


@pytest.mark.parametrize(
    ("statement", "parameters"),
    [
        ("UPDATE remote_profiles SET name = ?", ("not-the-document-name",)),
        ("UPDATE remote_profiles SET resource_version = ?", (0,)),
        ("UPDATE remote_profiles SET updated_at = ?", ("not-a-timestamp",)),
        ("UPDATE remote_profiles SET document_json = ?", (b'{"broken":true}',)),
    ],
)
def test_startup_revalidates_canonical_resource_rows(
    tmp_path: Path, statement: str, parameters: tuple[object, ...]
) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    _create_profile(store)
    store.close()
    with sqlite3.connect(root / "provider.sqlite3") as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(statement, parameters)

    with pytest.raises(ProviderDataCorruptionError):
        DesktopProviderStore(root)


def test_startup_rejects_foreign_key_corruption(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    profile = _create_profile(store)
    store.create_project(_project(profile.profile_id), idempotency_key="project-fk-000001")
    store.close()
    with sqlite3.connect(root / "provider.sqlite3") as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("UPDATE projects SET profile_id = 'missing-profile'")

    with pytest.raises(ProviderDataCorruptionError, match="foreign key"):
        DesktopProviderStore(root)


def test_startup_revalidates_typed_idempotency_response(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    _create_profile(store)
    store.close()
    with sqlite3.connect(root / "provider.sqlite3") as connection:
        connection.execute(
            "UPDATE idempotency_records SET response_bytes = ?",
            (b'{"schema_version":"1"}',),
        )

    with pytest.raises(ProviderDataCorruptionError, match="idempotent"):
        DesktopProviderStore(root)


def test_profile_crud_etag_and_reopen_are_stable(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    created = _create_profile(store)
    store.close()
    reopened = DesktopProviderStore(root)
    assert reopened.get_profile(created.profile_id) == created

    patched = reopened.patch_profile(
        created.profile_id,
        RemoteProfilePatchV1(name="Renamed server"),
        if_match=created.etag,
    )
    assert patched.name == "Renamed server"
    assert patched.etag != created.etag
    with pytest.raises(ETagConflictError):
        reopened.patch_profile(
            created.profile_id,
            RemoteProfilePatchV1(port=22),
            if_match=created.etag,
        )

    reopened.delete_profile(created.profile_id, if_match=patched.etag)
    with pytest.raises(ResourceNotFoundError):
        reopened.get_profile(created.profile_id)


def test_patch_revalidates_contract_and_rejects_explicit_null(tmp_path: Path) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)

    with pytest.raises(ContractValidationError):
        store.patch_profile(profile.profile_id, {"name": None}, if_match=profile.etag)
    assert store.get_profile(profile.profile_id) == profile


def test_project_roundtrips_unknown_evolution_config_and_blocks_profile_delete(
    tmp_path: Path,
) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    request = ProjectCreateV1.model_validate(_project(profile.profile_id))
    project = store.create_project(request, idempotency_key="project-create-0001")

    expected_evolution = request.evolution.model_dump(mode="json")
    assert project.evolution.model_dump(mode="json") == expected_evolution
    assert (
        store.get_project(project.project_id).evolution.model_dump(mode="json")
        == expected_evolution
    )
    store.close()
    reopened = DesktopProviderStore(tmp_path / "state")
    assert (
        reopened.get_project(project.project_id).evolution.model_dump(mode="json")
        == expected_evolution
    )

    with pytest.raises(ResourceInUseError):
        reopened.delete_profile(profile.profile_id, if_match=profile.etag)

    patched = reopened.patch_project(
        project.project_id,
        ProjectPatchV1(name="Renamed project"),
        if_match=project.etag,
    )
    assert patched.evolution.model_dump(mode="json") == expected_evolution
    reopened.delete_project(project.project_id, if_match=patched.etag)
    reopened.delete_profile(profile.profile_id, if_match=profile.etag)


def test_project_requires_an_existing_profile(tmp_path: Path) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    with pytest.raises(ResourceNotFoundError):
        store.create_project(
            _project("missing-profile"),
            idempotency_key="project-create-0001",
        )


def test_create_and_idempotency_record_roll_back_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DesktopProviderStore(tmp_path / "state")

    def fail_record(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("simulated crash before commit")

    monkeypatch.setattr(store, "_insert_idempotency_record", fail_record)
    with pytest.raises(RuntimeError, match="simulated crash"):
        _create_profile(store)

    assert store.list_profiles().items == ()
    store.close()
    reopened = DesktopProviderStore(tmp_path / "state")
    assert reopened.list_profiles().items == ()


def test_idempotency_exact_replay_survives_reopen_and_conflict_is_typed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    first = _create_profile(store)
    assert _create_profile(store) == first
    assert len(store.list_profiles().items) == 1
    store.close()
    reopened = DesktopProviderStore(root)
    assert _create_profile(reopened) == first
    with pytest.raises(IdempotencyConflictError):
        _create_profile(reopened, name="Different request")


def test_idempotency_capacity_is_bounded_without_evicting_live_replays(
    tmp_path: Path,
) -> None:
    store = DesktopProviderStore(tmp_path / "state", max_idempotency_records=1)
    first = _create_profile(store)

    assert _create_profile(store) == first
    with pytest.raises(IdempotencyCapacityError):
        _create_profile(store, name="Second", key="profile-create-0002")
    assert store.list_profiles().items == (first,)


def test_idempotent_action_state_change_is_atomic_replayed_and_blocks_delete(
    tmp_path: Path,
) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    project = store.create_project(
        _project(profile.profile_id),
        idempotency_key="project-create-0001",
    )
    calls = 0

    def activate(transaction: ProviderMutation):
        nonlocal calls
        calls += 1
        active = transaction.set_project_state(
            project.project_id,
            if_match=project.etag,
            state="active",
        )
        return 202, transaction.create_local_operation(
            operation_kind="project_activate",
            resource=ResourceRefV1(resource_type="project", resource_id=project.project_id),
            state="succeeded",
            result=ProjectOperationResultV1(
                project_id=active.project_id,
                project_etag=active.etag,
                active=True,
            ),
        )

    first = store.execute_idempotent_action(
        route=f"/desktop/v1/projects/{project.project_id}/activate",
        resource_scope=project.project_id,
        key="project-activate-0001",
        body={},
        if_match=project.etag,
        semantic_headers={},
        response_model=LocalOperationV1,
        mutation=activate,
    )
    replay = store.execute_idempotent_action(
        route=f"/desktop/v1/projects/{project.project_id}/activate",
        resource_scope=project.project_id,
        key="project-activate-0001",
        body={},
        if_match=project.etag,
        semantic_headers={},
        response_model=LocalOperationV1,
        mutation=activate,
    )

    active = store.get_project(project.project_id)
    operation = LocalOperationV1.model_validate_json(first.response_bytes)
    assert calls == 1
    assert store.get_local_operation(operation.operation_id) == operation
    assert (
        bytes(
            store._connection.execute(
                "SELECT document_json FROM local_operations WHERE operation_id = ?",
                (operation.operation_id,),
            ).fetchone()[0]
        )
        == first.response_bytes
    )
    assert replay.replayed is True
    assert replay.response_bytes == first.response_bytes
    with pytest.raises(ResourceInUseError):
        store.delete_project(project.project_id, if_match=active.etag)


def test_profile_runtime_state_resets_but_terminal_replay_is_frozen_on_restart(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    profile = _create_profile(store)

    def connect(transaction: ProviderMutation):
        connected = transaction.set_profile_runtime_state(
            profile.profile_id,
            if_match=profile.etag,
            connection_state="connected",
            credential_slots=(
                CredentialSlotStatusV1(
                    kind="ssh_password",
                    status="stored",
                    updated_at="2026-07-14T12:00:00Z",
                ),
            ),
            host_key_fingerprint="SHA256:renderer-safe-fingerprint",
        )
        return 202, transaction.create_local_operation(
            operation_kind="profile_connect",
            resource=ResourceRefV1(resource_type="profile", resource_id=profile.profile_id),
            state="succeeded",
            result=ConnectionOperationResultV1(
                profile_id=connected.profile_id,
                connection_state="connected",
            ),
        )

    first = store.execute_idempotent_action(
        route=f"/desktop/v1/profiles/{profile.profile_id}/connect",
        resource_scope=profile.profile_id,
        key="profile-connect-0001",
        body={},
        if_match=profile.etag,
        semantic_headers={},
        response_model=LocalOperationV1,
        mutation=connect,
    )
    connected = store.get_profile(profile.profile_id)
    assert connected.connection_state == "connected"
    assert connected.credential_slots[0].status == "stored"
    assert connected.host_key_fingerprint == "SHA256:renderer-safe-fingerprint"
    store.close()
    reopened = DesktopProviderStore(root)
    recovered = reopened.get_profile(profile.profile_id)
    assert recovered.connection_state == "disconnected"
    assert recovered.credential_slots == connected.credential_slots
    assert recovered.host_key_fingerprint == connected.host_key_fingerprint
    replay = reopened.execute_idempotent_action(
        route=f"/desktop/v1/profiles/{profile.profile_id}/connect",
        resource_scope=profile.profile_id,
        key="profile-connect-0001",
        body={},
        if_match=profile.etag,
        semantic_headers={},
        response_model=LocalOperationV1,
        mutation=lambda transaction: pytest.fail(f"unexpected mutation: {transaction}"),
    )
    replayed_operation = LocalOperationV1.model_validate_json(replay.response_bytes)
    assert replay.replayed is True
    assert replay.response_bytes == first.response_bytes
    assert replayed_operation.state == "succeeded"
    assert replayed_operation.result == ConnectionOperationResultV1(
        profile_id=profile.profile_id,
        connection_state="connected",
    )


def test_nonterminal_profile_reservation_is_cancelled_exactly_once_on_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    profile = _create_profile(store)
    action = {
        "route": f"/desktop/v1/profiles/{profile.profile_id}/connect",
        "operation_kind": "profile_connect",
        "profile_id": profile.profile_id,
        "key": "profile-crash-connect-0001",
        "body": {},
        "if_match": profile.etag,
        "displace_existing": True,
    }
    reservation = store.begin_profile_runtime_action(**action)
    assert reservation.operation.state == "running"
    assert store.get_profile(profile.profile_id).connection_state == "connecting"
    _, used_bytes = store._recovery_usage(store._connection)
    monkeypatch.setattr(
        provider_store_module,
        "MAX_RECOVERY_BYTES",
        used_bytes + provider_store_module.PROFILE_RUNTIME_TERMINAL_RESERVATION_BYTES,
    )
    store.close()

    reopened = DesktopProviderStore(root)
    recovered = reopened.begin_profile_runtime_action(**action)
    assert recovered.replayed is True
    assert recovered.operation.state == "cancelled"
    assert recovered.operation.result == ConnectionOperationResultV1(
        profile_id=profile.profile_id,
        connection_state="disconnected",
    )
    frozen = provider_store_module._canonical_json_bytes(
        recovered.operation.model_dump(mode="json")
    )
    persisted = reopened._connection.execute(
        "SELECT response_bytes FROM idempotency_records WHERE idempotency_key = ?",
        (action["key"],),
    ).fetchone()
    assert persisted is not None and bytes(persisted[0]) == frozen
    recovered_etag = recovered.operation.etag
    late_success = reopened.complete_profile_runtime_action(
        reservation=reservation,
        route=cast(str, action["route"]),
        profile_id=profile.profile_id,
        key=cast(str, action["key"]),
        body={},
        if_match=profile.etag,
        connection_state="connected",
        host_key_fingerprint="SHA256:late-success-must-not-publish",
    )
    late_error = ApiErrorV1(
        request_id=reservation.operation.operation_id,
        code="connection_operation_superseded",
        http_status=409,
        message="A newer connection action replaced this SSH operation.",
        severity=ErrorSeverity.BLOCKING,
        category=ErrorCategory.AUTHENTICATION,
        retryable=True,
        repair_action=RepairAction.OPENEVO_CAN_RETRY,
        next_action="Reload the connection state before retrying.",
    )
    late_failure = reopened.fail_profile_runtime_action(
        reservation=reservation,
        route=cast(str, action["route"]),
        profile_id=profile.profile_id,
        key=cast(str, action["key"]),
        body={},
        if_match=profile.etag,
        error=late_error,
    )
    assert late_success == recovered.operation
    assert late_failure == recovered.operation
    assert reopened.get_profile(profile.profile_id).connection_state == "disconnected"
    assert (
        bytes(
            reopened._connection.execute(
                "SELECT response_bytes FROM idempotency_records WHERE idempotency_key = ?",
                (action["key"],),
            ).fetchone()[0]
        )
        == frozen
    )
    reopened.close()

    reopened_again = DesktopProviderStore(root)
    replay = reopened_again.begin_profile_runtime_action(**action)
    assert replay.replayed is True
    assert (
        provider_store_module._canonical_json_bytes(replay.operation.model_dump(mode="json"))
        == frozen
    )
    assert replay.operation.etag == recovered_etag


@pytest.mark.parametrize("state", ["queued", "running", "cancelling"])
def test_nonterminal_profile_runtime_operation_blocks_delete(
    tmp_path: Path,
    state: Literal["queued", "running", "cancelling"],
) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    with store._transaction(write=True) as connection:
        ProviderMutation(store, connection).create_local_operation(
            operation_kind="profile_disconnect",
            resource=ResourceRefV1(resource_type="profile", resource_id=profile.profile_id),
            state=state,
        )

    with pytest.raises(ResourceInUseError):
        store.delete_profile(profile.profile_id, if_match=profile.etag)

    assert store.get_profile(profile.profile_id) == profile


def test_disconnected_profile_disconnect_reservation_blocks_delete_until_terminal(
    tmp_path: Path,
) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    action = {
        "route": f"/desktop/v1/profiles/{profile.profile_id}/disconnect",
        "operation_kind": "profile_disconnect",
        "profile_id": profile.profile_id,
        "key": "profile-disconnect-delete-guard",
        "body": {},
        "if_match": profile.etag,
        "displace_existing": False,
    }
    reservation = store.begin_profile_runtime_action(**action)
    assert reservation.operation.state == "running"
    assert store.get_profile(profile.profile_id).connection_state == "disconnected"

    with pytest.raises(ResourceInUseError):
        store.delete_profile(profile.profile_id, if_match=profile.etag)

    completed = store.complete_profile_runtime_action(
        reservation=reservation,
        route=cast(str, action["route"]),
        profile_id=profile.profile_id,
        key=cast(str, action["key"]),
        body={},
        if_match=profile.etag,
        connection_state="disconnected",
        host_key_fingerprint=None,
    )
    replay = store.begin_profile_runtime_action(**action)

    assert completed.state == "succeeded"
    assert replay.replayed is True
    assert replay.operation == completed
    terminal_profile = store.get_profile(profile.profile_id)
    store.delete_profile(profile.profile_id, if_match=terminal_profile.etag)
    with pytest.raises(ResourceNotFoundError):
        store.get_profile(profile.profile_id)
    replay_after_delete = store.begin_profile_runtime_action(**action)
    assert replay_after_delete.replayed is True
    assert replay_after_delete.operation == completed


def test_profile_reservation_fails_before_ssh_when_terminal_slots_do_not_fit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    before_profile = store.get_profile(profile.profile_id)
    before_operations = cast(
        int, store._connection.execute("SELECT count(*) FROM local_operations").fetchone()[0]
    )
    before_idempotency = cast(
        int, store._connection.execute("SELECT count(*) FROM idempotency_records").fetchone()[0]
    )
    _, used_bytes = store._recovery_usage(store._connection)
    monkeypatch.setattr(
        provider_store_module,
        "MAX_RECOVERY_BYTES",
        used_bytes + provider_store_module.PROFILE_RUNTIME_TERMINAL_RESERVATION_BYTES,
    )

    with pytest.raises(ProviderDataCorruptionError, match="recovery budget exceeded"):
        store.begin_profile_runtime_action(
            route=f"/desktop/v1/profiles/{profile.profile_id}/connect",
            operation_kind="profile_connect",
            profile_id=profile.profile_id,
            key="profile-no-terminal-capacity",
            body={},
            if_match=profile.etag,
            displace_existing=True,
        )

    assert store.get_profile(profile.profile_id) == before_profile
    assert store._connection.execute("SELECT count(*) FROM local_operations").fetchone()[0] == (
        before_operations
    )
    assert store._connection.execute("SELECT count(*) FROM idempotency_records").fetchone()[0] == (
        before_idempotency
    )


def test_profile_failure_finalizes_inside_reserved_terminal_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    action = {
        "route": f"/desktop/v1/profiles/{profile.profile_id}/connect",
        "operation_kind": "profile_connect",
        "profile_id": profile.profile_id,
        "key": "profile-capacity-failure",
        "body": {},
        "if_match": profile.etag,
        "displace_existing": True,
    }
    reservation = store.begin_profile_runtime_action(**action)
    _, used_bytes = store._recovery_usage(store._connection)
    monkeypatch.setattr(
        provider_store_module,
        "MAX_RECOVERY_BYTES",
        used_bytes + provider_store_module.PROFILE_RUNTIME_TERMINAL_RESERVATION_BYTES,
    )
    error = ApiErrorV1(
        request_id=reservation.operation.operation_id,
        code="ssh_connection_failed",
        http_status=503,
        message="OpenEvo Desktop could not establish the SSH connection.",
        severity=ErrorSeverity.BLOCKING,
        category=ErrorCategory.AUTHENTICATION,
        retryable=True,
        repair_action=RepairAction.OPENEVO_CAN_RETRY,
        next_action="Check the server and SSH settings, then retry.",
    )

    failed = store.fail_profile_runtime_action(
        reservation=reservation,
        route=cast(str, action["route"]),
        profile_id=profile.profile_id,
        key=cast(str, action["key"]),
        body={},
        if_match=profile.etag,
        error=error,
    )

    assert failed.state == "failed"
    assert failed.error == error
    assert store.get_profile(profile.profile_id).connection_state == "disconnected"
    replay = store.begin_profile_runtime_action(**action)
    assert replay.replayed is True
    assert replay.operation == failed


def test_connecting_second_profile_atomically_displaces_first_profile(
    tmp_path: Path,
) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    first = _create_profile(store, name="First", key="profile-create-first")
    second = _create_profile(store, name="Second", key="profile-create-second")

    def connect(profile: RemoteProfileV1, key: str) -> LocalOperationV1:
        def mutation(transaction: ProviderMutation):
            updated = transaction.set_profile_runtime_state(
                profile.profile_id,
                if_match=profile.etag,
                connection_state="connected",
                credential_slots=(),
                host_key_fingerprint="SHA256:verified",
            )
            return 202, transaction.create_local_operation(
                operation_kind="profile_connect",
                resource=ResourceRefV1(resource_type="profile", resource_id=profile.profile_id),
                state="succeeded",
                result=ConnectionOperationResultV1(
                    profile_id=profile.profile_id,
                    connection_state=updated.connection_state,
                ),
            )

        result = store.execute_idempotent_action(
            route=f"/desktop/v1/profiles/{profile.profile_id}/connect",
            resource_scope=profile.profile_id,
            key=key,
            body={},
            if_match=profile.etag,
            semantic_headers={},
            response_model=LocalOperationV1,
            mutation=mutation,
        )
        return LocalOperationV1.model_validate_json(result.response_bytes)

    first_operation = connect(first, "profile-connect-first")
    second_operation = connect(second, "profile-connect-second")

    assert store.get_profile(first.profile_id).connection_state == "disconnected"
    assert store.get_profile(second.profile_id).connection_state == "connected"
    displaced = store.get_local_operation(first_operation.operation_id)
    assert displaced.state == "succeeded"
    assert displaced.result == ConnectionOperationResultV1(
        profile_id=first.profile_id,
        connection_state="connected",
    )
    assert store.get_local_operation(second_operation.operation_id) == second_operation


def test_startup_discards_unconfirmed_host_key_fingerprint(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    profile = _create_profile(store)

    def review(transaction: ProviderMutation):
        reviewed = transaction.set_profile_runtime_state(
            profile.profile_id,
            if_match=profile.etag,
            connection_state="host_key_required",
            credential_slots=(),
            host_key_fingerprint="SHA256:unconfirmed-candidate",
        )
        return 202, transaction.create_local_operation(
            operation_kind="profile_connect",
            resource=ResourceRefV1(resource_type="profile", resource_id=profile.profile_id),
            state="succeeded",
            result=ConnectionOperationResultV1(
                profile_id=profile.profile_id,
                connection_state=reviewed.connection_state,
            ),
        )

    store.execute_idempotent_action(
        route=f"/desktop/v1/profiles/{profile.profile_id}/connect",
        resource_scope=profile.profile_id,
        key="profile-review-unconfirmed",
        body={},
        if_match=profile.etag,
        semantic_headers={},
        response_model=LocalOperationV1,
        mutation=review,
    )
    store.close()

    reopened = DesktopProviderStore(root)
    recovered = reopened.get_profile(profile.profile_id)
    assert recovered.connection_state == "disconnected"
    assert recovered.host_key_fingerprint is None


def test_deleted_profile_keeps_historical_terminal_action_replay_frozen(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    profile = _create_profile(store)

    def connect(transaction: ProviderMutation):
        connected = transaction.set_profile_runtime_state(
            profile.profile_id,
            if_match=profile.etag,
            connection_state="connected",
            credential_slots=(),
            host_key_fingerprint=None,
        )
        return 202, transaction.create_local_operation(
            operation_kind="profile_connect",
            resource=ResourceRefV1(resource_type="profile", resource_id=profile.profile_id),
            state="succeeded",
            result=ConnectionOperationResultV1(
                profile_id=profile.profile_id,
                connection_state=connected.connection_state,
            ),
        )

    action = {
        "route": f"/desktop/v1/profiles/{profile.profile_id}/connect",
        "resource_scope": profile.profile_id,
        "key": "deleted-profile-connect",
        "body": {},
        "if_match": profile.etag,
        "semantic_headers": {},
        "response_model": LocalOperationV1,
        "mutation": connect,
    }
    store.execute_idempotent_action(**action)
    connected = store.get_profile(profile.profile_id)
    with store._transaction(write=True) as connection:
        transaction = ProviderMutation(store, connection)
        disconnected = transaction.set_profile_runtime_state(
            profile.profile_id,
            if_match=connected.etag,
            connection_state="disconnected",
            credential_slots=(),
            host_key_fingerprint=None,
        )
    store.delete_profile(profile.profile_id, if_match=disconnected.etag)
    store.close()

    reopened = DesktopProviderStore(root)
    replay = reopened.execute_idempotent_action(
        **{
            **action,
            "mutation": lambda transaction: pytest.fail(f"unexpected mutation: {transaction}"),
        }
    )
    operation = LocalOperationV1.model_validate_json(replay.response_bytes)
    assert operation.state == "succeeded"
    assert operation.result == ConnectionOperationResultV1(
        profile_id=profile.profile_id,
        connection_state="connected",
    )


def test_action_idempotency_binds_if_match_headers_and_response_type(tmp_path: Path) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    project = store.create_project(
        _project(profile.profile_id), idempotency_key="project-00000001"
    )
    calls = 0

    def activate(transaction: ProviderMutation):
        nonlocal calls
        calls += 1
        active = transaction.set_project_state(
            project.project_id,
            if_match=project.etag,
            state="active",
        )
        return 202, transaction.create_local_operation(
            operation_kind="project_activate",
            resource=ResourceRefV1(resource_type="project", resource_id=project.project_id),
            state="succeeded",
            result=ProjectOperationResultV1(
                project_id=active.project_id,
                project_etag=active.etag,
                active=True,
            ),
        )

    arguments = {
        "route": f"/desktop/v1/projects/{project.project_id}/activate",
        "resource_scope": project.project_id,
        "key": "project-action-0001",
        "body": {},
        "if_match": project.etag,
        "semantic_headers": {"X-OpenEvo-Mode": "transcript"},
        "response_model": LocalOperationV1,
        "mutation": activate,
    }
    store.execute_idempotent_action(**arguments)
    replay = store.execute_idempotent_action(
        **{**arguments, "semantic_headers": {"x-openevo-mode": "transcript"}}
    )
    assert replay.replayed is True
    assert calls == 1

    with pytest.raises(IdempotencyConflictError):
        store.execute_idempotent_action(
            **{**arguments, "semantic_headers": {"x-openevo-mode": "token"}}
        )
    with pytest.raises(IdempotencyConflictError):
        store.execute_idempotent_action(**{**arguments, "if_match": f'"{"0" * 64}"'})
    with pytest.raises(ProviderDataCorruptionError, match="response type"):
        store.execute_idempotent_action(**{**arguments, "response_model": type(profile)})


def test_project_runtime_action_is_durable_idempotent_and_completes_atomically(
    tmp_path: Path,
) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    project = store.create_project(
        _project(profile.profile_id), idempotency_key="project-runtime-create-01"
    )
    action = {
        "route": f"/desktop/v1/projects/{project.project_id}/activate",
        "operation_kind": "project_activate",
        "project_id": project.project_id,
        "key": "project-runtime-activate-01",
        "body": {},
        "if_match": project.etag,
    }

    reservation = store.begin_project_runtime_action(**action)
    assert reservation.replayed is False
    assert reservation.project == project
    assert reservation.operation.state == "queued"
    assert reservation.operation.started_at is None
    assert reservation.operation.result == ProjectOperationResultV1(
        project_id=project.project_id,
        project_etag=project.etag,
        active=False,
    )

    replay = store.begin_project_runtime_action(**action)
    assert replay.replayed is True
    assert replay.project is None
    assert replay.operation == reservation.operation
    with pytest.raises(ResourceInUseError):
        store.patch_project(project.project_id, {"name": "Changed"}, if_match=project.etag)
    with pytest.raises(ResourceInUseError):
        store.delete_project(project.project_id, if_match=project.etag)
    with pytest.raises(ResourceInUseError):
        store.begin_project_runtime_action(
            **{
                **action,
                "route": f"/desktop/v1/projects/{project.project_id}/doctor",
                "operation_kind": "project_doctor",
                "key": "project-runtime-doctor-0001",
            }
        )

    running = store.start_project_runtime_action(reservation=reservation, **action)
    assert running.state == "running"
    assert running.started_at is not None
    assert store.begin_project_runtime_action(**action).operation == running

    finished = store.complete_project_runtime_action(
        reservation=reservation,
        current_revision_id="core-revision-0001",
        **action,
    )
    active = store.get_project(project.project_id)
    assert finished.state == "succeeded"
    assert finished.finished_at is not None
    assert finished.result == ProjectOperationResultV1(
        project_id=project.project_id,
        project_etag=active.etag,
        active=True,
    )
    assert active.state == "active"
    with store._transaction(write=False) as connection:
        assert (
            connection.execute(
                "SELECT current_revision_id FROM projects WHERE project_id = ?",
                (project.project_id,),
            ).fetchone()[0]
            == "core-revision-0001"
        )
    assert store.begin_project_runtime_action(**action).operation == finished


def test_project_runtime_action_failure_is_replayable_and_keeps_project_draft(
    tmp_path: Path,
) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    project = store.create_project(
        _project(profile.profile_id), idempotency_key="project-runtime-fail-create"
    )
    action = {
        "route": f"/desktop/v1/projects/{project.project_id}/activate",
        "operation_kind": "project_activate",
        "project_id": project.project_id,
        "key": "project-runtime-fail-action",
        "body": {},
        "if_match": project.etag,
    }
    reservation = store.begin_project_runtime_action(**action)
    store.start_project_runtime_action(reservation=reservation, **action)
    error = ApiErrorV1(
        request_id=reservation.operation.operation_id,
        code="core_activation_failed",
        http_status=503,
        message="OpenEvo Core activation failed.",
        severity=ErrorSeverity.BLOCKING,
        category=ErrorCategory.PROJECT,
        retryable=True,
        repair_action=RepairAction.OPENEVO_CAN_RETRY,
        next_action="Retry project activation.",
    )

    failed = store.fail_project_runtime_action(
        reservation=reservation,
        error=error,
        **action,
    )

    assert failed.state == "failed"
    assert failed.error == error
    assert failed.result == reservation.operation.result
    assert store.get_project(project.project_id).state == "draft"
    assert store.begin_project_runtime_action(**action).operation == failed


def test_project_runtime_failure_rebinds_result_after_another_activation(
    tmp_path: Path,
) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    first = store.create_project(
        _project(profile.profile_id, name="First"),
        idempotency_key="project-runtime-first-create",
    )
    second = store.create_project(
        _project(profile.profile_id, name="Second"),
        idempotency_key="project-runtime-second-create",
    )
    with store._transaction(write=True) as connection:
        first = ProviderMutation(store, connection).set_project_state(
            first.project_id,
            if_match=first.etag,
            state="active",
            current_revision_id="first-revision",
        )
    action = {
        "route": f"/desktop/v1/projects/{first.project_id}/activate",
        "operation_kind": "project_activate",
        "project_id": first.project_id,
        "key": "project-runtime-first-reactivate",
        "body": {},
        "if_match": first.etag,
    }
    reservation = store.begin_project_runtime_action(**action)
    store.start_project_runtime_action(reservation=reservation, **action)
    with store._transaction(write=True) as connection:
        ProviderMutation(store, connection).set_project_state(
            second.project_id,
            if_match=second.etag,
            state="active",
            current_revision_id="second-revision",
        )
    current_first = store.get_project(first.project_id)
    error = ApiErrorV1(
        request_id=reservation.operation.operation_id,
        code="activation_superseded",
        http_status=409,
        message="A newer activation superseded this project.",
        severity=ErrorSeverity.BLOCKING,
        category=ErrorCategory.PROJECT,
        retryable=True,
        repair_action=RepairAction.OPENEVO_CAN_RETRY,
        next_action="Retry project activation.",
    )

    failed = store.fail_project_runtime_action(
        reservation=reservation,
        error=error,
        **action,
    )

    assert failed.state == "failed"
    assert failed.result == ProjectOperationResultV1(
        project_id=first.project_id,
        project_etag=current_first.etag,
        active=False,
    )


def test_project_runtime_action_restart_cancels_once_and_updates_replay(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    profile = _create_profile(store)
    project = store.create_project(
        _project(profile.profile_id), idempotency_key="project-runtime-crash-create"
    )
    action = {
        "route": f"/desktop/v1/projects/{project.project_id}/workspace-sync",
        "operation_kind": "workspace_sync",
        "project_id": project.project_id,
        "key": "project-runtime-crash-action",
        "body": {},
        "if_match": project.etag,
    }
    reservation = store.begin_project_runtime_action(**action)
    running = store.start_project_runtime_action(reservation=reservation, **action)
    assert running.state == "running"
    store.close()

    reopened = DesktopProviderStore(root)
    replay = reopened.begin_project_runtime_action(**action)
    assert replay.replayed is True
    assert replay.operation.state == "cancelled"
    assert replay.operation.finished_at is not None
    frozen_etag = replay.operation.etag
    assert reopened.get_local_operation(replay.operation.operation_id).etag == frozen_etag
    assert reopened.begin_project_runtime_action(**action).operation.etag == frozen_etag


def test_non_activation_project_runtime_action_succeeds_without_project_result(
    tmp_path: Path,
) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    project = store.create_project(
        _project(profile.profile_id), idempotency_key="project-runtime-sync-create"
    )
    action = {
        "route": f"/desktop/v1/projects/{project.project_id}/workspace-sync",
        "operation_kind": "workspace_sync",
        "project_id": project.project_id,
        "key": "project-runtime-sync-action",
        "body": {},
        "if_match": project.etag,
    }
    reservation = store.begin_project_runtime_action(**action)
    store.start_project_runtime_action(reservation=reservation, **action)

    finished = store.complete_project_runtime_action(
        reservation=reservation,
        current_revision_id=None,
        **action,
    )

    assert finished.state == "succeeded"
    assert finished.result is None
    assert store.get_project(project.project_id) == project


def test_active_project_switch_is_atomic_and_active_config_is_immutable(tmp_path: Path) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    first = store.create_project(
        _project(profile.profile_id, name="First"), idempotency_key="project-first-0001"
    )
    second = store.create_project(
        _project(profile.profile_id, name="Second"), idempotency_key="project-second-001"
    )

    def activate(project):
        current = store.get_project(project.project_id)

        def mutation(transaction: ProviderMutation):
            active = transaction.set_project_state(
                current.project_id, if_match=current.etag, state="active"
            )
            return 202, transaction.create_local_operation(
                operation_kind="project_activate",
                resource=ResourceRefV1(resource_type="project", resource_id=current.project_id),
                state="succeeded",
                result=ProjectOperationResultV1(
                    project_id=active.project_id,
                    project_etag=active.etag,
                    active=True,
                ),
            )

        store.execute_idempotent_action(
            route=f"/desktop/v1/projects/{current.project_id}/activate",
            resource_scope=current.project_id,
            key=f"activate-{current.project_id}",
            body={},
            if_match=current.etag,
            semantic_headers={},
            response_model=LocalOperationV1,
            mutation=mutation,
        )

    activate(first)
    activate(second)
    assert store.get_project(first.project_id).state == "draft"
    active = store.get_project(second.project_id)
    assert active.state == "active"
    with pytest.raises(ResourceInUseError):
        store.patch_project(active.project_id, {"name": "Changed"}, if_match=active.etag)
    store.close()
    reopened = DesktopProviderStore(tmp_path / "state")
    assert reopened.get_project(active.project_id).state == "draft"


def test_connected_profile_rejects_all_connection_parameter_changes(tmp_path: Path) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)

    def connect(transaction: ProviderMutation):
        connected = transaction.set_profile_runtime_state(
            profile.profile_id,
            if_match=profile.etag,
            connection_state="connected",
            credential_slots=(),
            host_key_fingerprint=None,
        )
        return 202, transaction.create_local_operation(
            operation_kind="profile_connect",
            resource=ResourceRefV1(resource_type="profile", resource_id=profile.profile_id),
            state="succeeded",
            result=ConnectionOperationResultV1(
                profile_id=connected.profile_id,
                connection_state="connected",
            ),
        )

    store.execute_idempotent_action(
        route=f"/desktop/v1/profiles/{profile.profile_id}/connect",
        resource_scope=profile.profile_id,
        key="profile-connect-guard",
        body={},
        if_match=profile.etag,
        semantic_headers={},
        response_model=LocalOperationV1,
        mutation=connect,
    )
    connected = store.get_profile(profile.profile_id)
    for patch in (
        {"host": "other.example.org"},
        {"port": 2200},
        {"user": "other"},
        {"authentication_kind": "ssh_agent"},
        {"proxy": {"https_url": "https://other-proxy.example.org"}},
    ):
        with pytest.raises(ResourceInUseError):
            store.patch_profile(connected.profile_id, patch, if_match=connected.etag)


def test_cursor_is_stable_tamper_evident_and_expiry_is_distinct(tmp_path: Path) -> None:
    clock = MutableClock()
    store = DesktopProviderStore(tmp_path / "state", clock=clock, cursor_ttl_seconds=30)
    for index in range(3):
        _create_profile(
            store,
            name=f"Server {index}",
            key=f"profile-create-{index:04d}",
        )

    first = store.list_profiles(limit=1, sort="name", direction="asc")
    assert first.has_more is True
    assert first.next_cursor is not None

    store.close()
    reopened = DesktopProviderStore(tmp_path / "state", clock=clock, cursor_ttl_seconds=30)
    second = reopened.list_profiles(
        limit=1,
        after=first.next_cursor,
        sort="name",
        direction="asc",
    )
    assert second.items[0].name == "Server 1"

    tampered = f"{first.next_cursor[:-1]}{'A' if first.next_cursor[-1] != 'A' else 'B'}"
    with pytest.raises(CursorInvalidError):
        reopened.list_profiles(limit=1, after=tampered, sort="name", direction="asc")
    with pytest.raises(CursorInvalidError):
        reopened.list_profiles(limit=1, after=first.next_cursor, sort="updated_at")
    with pytest.raises(CursorInvalidError):
        reopened.list_profiles(
            limit=1,
            after=first.next_cursor,
            sort="name",
            direction="asc",
            filters={"connection_state": "disconnected"},
        )

    clock.now += timedelta(seconds=31)
    cleanup_page = reopened.list_profiles(limit=1, sort="name", direction="asc")
    assert cleanup_page.next_cursor is not None
    with pytest.raises(CursorExpiredError):
        reopened.list_profiles(
            limit=1,
            after=first.next_cursor,
            sort="name",
            direction="asc",
        )
    with pytest.raises(CursorInvalidError):
        reopened.list_profiles(limit=1, after=tampered, sort="name", direction="asc")


def test_cursor_covers_the_contract_maximum_utf8_name(tmp_path: Path) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    maximum_character = chr(0x1F9EA)
    maximum_utf8_name = f"{maximum_character * 511}a"
    next_maximum_name = f"{maximum_character * 511}b"
    _create_profile(store, name=maximum_utf8_name, key="maximum-name-page-01")
    _create_profile(store, name=next_maximum_name, key="maximum-name-page-02")

    first = store.list_profiles(limit=1, sort="name", direction="asc")

    assert first.next_cursor is not None
    assert len(first.next_cursor.encode("utf-8")) <= 2_048
    second = store.list_profiles(
        limit=1,
        after=first.next_cursor,
        sort="name",
        direction="asc",
    )
    assert second.items[0].name == next_maximum_name


@pytest.mark.parametrize("mutation", ["delete", "rename"])
def test_cursor_boundary_survives_anchor_deletion_or_sort_change(
    tmp_path: Path, mutation: str
) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    first_profile = _create_profile(store, name="A", key="profile-anchor-0001")
    _create_profile(store, name="B", key="profile-anchor-0002")
    _create_profile(store, name="C", key="profile-anchor-0003")
    first_page = store.list_profiles(limit=1, sort="name", direction="asc")
    assert first_page.next_cursor is not None

    if mutation == "delete":
        store.delete_profile(first_profile.profile_id, if_match=first_profile.etag)
    else:
        store.patch_profile(first_profile.profile_id, {"name": "Z"}, if_match=first_profile.etag)

    next_page = store.list_profiles(
        limit=1,
        after=first_page.next_cursor,
        sort="name",
        direction="asc",
    )
    assert next_page.items[0].name == "B"


def test_pagination_exposes_opaque_anchor_not_sqlite_rowid(tmp_path: Path) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    for index in range(2):
        _create_profile(
            store,
            name=f"Server {index}",
            key=f"profile-page-{index:04d}",
        )
    page = store.list_profiles(limit=1)

    assert page.next_cursor is not None
    assert str(store.database_path) not in page.next_cursor
    assert "rowid" not in page.next_cursor.lower()


def test_concurrent_writes_are_safe(tmp_path: Path) -> None:
    store = DesktopProviderStore(tmp_path / "state")

    def create(index: int) -> str:
        return _create_profile(
            store,
            name=f"Concurrent {index}",
            key=f"concurrent-create-{index:04d}",
        ).profile_id

    with ThreadPoolExecutor(max_workers=8) as executor:
        ids = tuple(executor.map(create, range(16)))

    assert len(set(ids)) == 16
    assert len(store.list_profiles(limit=100).items) == 16


def test_concurrent_idempotency_replay_commits_one_resource(tmp_path: Path) -> None:
    store = DesktopProviderStore(tmp_path / "state")

    with ThreadPoolExecutor(max_workers=8) as executor:
        profiles = tuple(executor.map(lambda _: _create_profile(store), range(16)))

    assert len({profile.profile_id for profile in profiles}) == 1
    assert store.list_profiles().items == (profiles[0],)


def test_close_waits_for_an_active_transaction(tmp_path: Path) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    entered = threading.Event()
    release = threading.Event()

    def hold_transaction() -> None:
        with store._transaction(write=True):
            entered.set()
            assert release.wait(timeout=5)

    worker = threading.Thread(target=hold_transaction)
    worker.start()
    assert entered.wait(timeout=5)
    closer = threading.Thread(target=store.close)
    closer.start()
    time.sleep(0.05)
    assert closer.is_alive()

    release.set()
    worker.join(timeout=5)
    closer.join(timeout=5)
    assert not worker.is_alive()
    assert not closer.is_alive()
    with pytest.raises(ProviderStateRootError, match="closed"):
        store.list_profiles()


def test_rejected_secret_field_never_reaches_persistent_files(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    canary = "SECRET-CANARY-should-never-be-persisted"
    payload = {**_profile(), "ssh_password": canary}

    with pytest.raises(ContractValidationError):
        store.create_profile(
            payload,
            idempotency_key="profile-secret-0001",
        )

    for path in root.iterdir():
        if path.is_file():
            assert canary.encode() not in path.read_bytes()


@pytest.mark.parametrize(
    "denied_key",
    [
        "API-Token",
        "client_secret",
        "SSH.Private-Key",
        "credential-slot",
        "HOST_path",
        "local.path",
        "model_path",
        "working-directory",
        "RAW-diagnostics",
        "process_stdout",
        "Stack.Trace",
    ],
)
def test_project_method_config_recursively_rejects_persistence_denied_keys(
    tmp_path: Path, denied_key: str
) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    profile = _create_profile(store)
    canary = f"DENIED-CONFIG-{denied_key}"
    project = _project(profile.profile_id)
    project["evolution"] = {
        "targets": {
            "future_target": {
                "enabled": False,
                "method": "plugin.future.v9",
                "config": {"outer": [{"inner": {denied_key: canary}}]},
            }
        }
    }

    with pytest.raises(ContractValidationError, match="persistence-denied"):
        store.create_project(project, idempotency_key="denied-config-key-01")

    assert all(
        canary.encode() not in path.read_bytes() for path in root.iterdir() if path.is_file()
    )


def test_action_secret_is_rejected_before_idempotency_persistence(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    profile = _create_profile(store)
    canary = "ACTION-IDEMPOTENCY-SECRET-CANARY"

    with pytest.raises(ContractValidationError, match="credential-bearing"):
        store.execute_idempotent_action(
            route=f"/desktop/v1/profiles/{profile.profile_id}/connect",
            resource_scope=profile.profile_id,
            key="action-secret-key-01",
            body={"api_token": canary},
            if_match=profile.etag,
            semantic_headers={},
            response_model=LocalOperationV1,
            mutation=lambda transaction: pytest.fail(f"unexpected mutation: {transaction}"),
        )

    assert all(
        canary.encode() not in path.read_bytes() for path in root.iterdir() if path.is_file()
    )


def test_desktop_session_token_is_never_accepted_as_a_persisted_principal(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    token = "DESKTOP-SESSION-TOKEN-CANARY"
    _create_profile(store)
    with pytest.raises(ContractValidationError):
        store.execute_idempotent_action(
            route="/desktop/v1/profiles/opaque/connect",
            resource_scope="opaque",
            key="session-token-test01",
            body={},
            if_match=f'"{"0" * 64}"',
            semantic_headers={"authorization": f"Bearer {token}"},
            response_model=type(store.list_profiles().items[0]),
            mutation=lambda transaction: pytest.fail(f"unexpected mutation: {transaction}"),
        )
    store.close()

    assert all(
        token.encode() not in path.read_bytes() for path in root.iterdir() if path.is_file()
    )
    with sqlite3.connect(root / "provider.sqlite3") as connection:
        assert connection.execute(
            "SELECT DISTINCT principal FROM idempotency_records"
        ).fetchall() == [("desktop-local-v1",)]


def test_store_accepts_contract_valid_aggregate_method_configs(tmp_path: Path) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    project = _project(profile.profile_id)
    project["evolution"] = {
        "targets": {
            f"target_{index}": {
                "enabled": False,
                "method": f"plugin.future.{index}",
                "config": {"content": "x" * 250_000},
            }
            for index in range(4)
        }
    }
    validated = ProjectCreateV1.model_validate(project)

    created = store.create_project(validated, idempotency_key="aggregate-config-01")

    assert created.evolution == validated.evolution


def test_startup_atomically_converges_interrupted_operations_and_resources(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    profile = _create_profile(store)
    project = store.create_project(
        _project(profile.profile_id), idempotency_key="operation-recovery-project"
    )

    def interrupt(transaction: ProviderMutation):
        transaction.set_profile_runtime_state(
            profile.profile_id,
            if_match=profile.etag,
            connection_state="connecting",
            credential_slots=(),
            host_key_fingerprint=None,
        )
        operation = transaction.create_local_operation(
            operation_kind="profile_connect",
            resource=ResourceRefV1(resource_type="profile", resource_id=profile.profile_id),
            state="running",
        )
        return 202, operation

    result = store.execute_idempotent_action(
        route=f"/desktop/v1/profiles/{profile.profile_id}/connect",
        resource_scope=profile.profile_id,
        key="interrupted-connect-01",
        body={},
        if_match=profile.etag,
        semantic_headers={},
        response_model=LocalOperationV1,
        mutation=interrupt,
    )
    operation = LocalOperationV1.model_validate_json(result.response_bytes)
    active = store.get_project(project.project_id)
    with store._transaction(write=True) as connection:
        transaction = ProviderMutation(store, connection)
        transaction.set_project_state(
            project.project_id,
            if_match=active.etag,
            state="active",
            current_revision_id="revision-before-crash",
        )
    store.close()

    reopened = DesktopProviderStore(root)
    recovered_profile = reopened.get_profile(profile.profile_id)
    recovered_project = reopened.get_project(project.project_id)
    recovered_operation = reopened.get_local_operation(operation.operation_id)
    assert recovered_profile.connection_state == "disconnected"
    assert recovered_project.state == "draft"
    with reopened._transaction(write=False) as connection:
        persisted_revision = connection.execute(
            "SELECT current_revision_id FROM projects WHERE project_id = ?",
            (project.project_id,),
        ).fetchone()
    assert persisted_revision is not None
    assert persisted_revision[0] is None
    assert recovered_operation.state == "cancelled"
    assert recovered_operation.finished_at is not None
    assert recovered_operation.result == ConnectionOperationResultV1(
        profile_id=profile.profile_id,
        connection_state="disconnected",
    )


def test_startup_reconciliation_streams_large_operation_sets_in_bounded_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    profile = _create_profile(store)
    operation_count = provider_store_module.STARTUP_OPERATION_BATCH_ROWS * 2 + 3
    with store._transaction(write=True) as connection:
        transaction = ProviderMutation(store, connection)
        for _index in range(operation_count):
            transaction.create_local_operation(
                operation_kind="profile_connect",
                resource=ResourceRefV1(resource_type="profile", resource_id=profile.profile_id),
                state="running",
            )
    store.close()

    original_connect = sqlite3.connect
    batch_limits: list[int] = []

    class CursorProbe:
        def __init__(self, cursor: sqlite3.Cursor, sql: str) -> None:
            self._cursor = cursor
            self._sql = " ".join(sql.lower().split())

        def fetchall(self):
            if "from local_operations" in self._sql:
                raise AssertionError("startup operation recovery must not call fetchall")
            return self._cursor.fetchall()

        def fetchone(self):
            return self._cursor.fetchone()

        def fetchmany(self, size: int | None = None):
            if size is not None:
                assert size <= provider_store_module.STARTUP_OPERATION_BATCH_ROWS
            return self._cursor.fetchmany() if size is None else self._cursor.fetchmany(size)

        def __iter__(self):
            return iter(self._cursor)

    class ConnectionProbe:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection

        @property
        def row_factory(self):
            return self._connection.row_factory

        @row_factory.setter
        def row_factory(self, value) -> None:
            self._connection.row_factory = value

        def execute(self, sql: str, parameters=()):
            normalized = " ".join(sql.lower().split())
            if (
                "from local_operations" in normalized
                and "order by operation_id" in normalized
                and "limit ?" in normalized
            ):
                batch_limits.append(int(parameters[-1]))
            return CursorProbe(self._connection.execute(sql, parameters), sql)

        def executemany(self, sql: str, parameters):
            return self._connection.executemany(sql, parameters)

        def commit(self) -> None:
            self._connection.commit()

        def rollback(self) -> None:
            self._connection.rollback()

        def close(self) -> None:
            self._connection.close()

    def connect(*args, **kwargs):
        return ConnectionProbe(original_connect(*args, **kwargs))

    monkeypatch.setattr(provider_store_module.sqlite3, "connect", connect)
    reopened = DesktopProviderStore(root)

    assert len(batch_limits) >= 3
    assert max(batch_limits) <= provider_store_module.STARTUP_OPERATION_BATCH_ROWS
    assert (
        reopened._connection.execute(
            "SELECT count(*) FROM local_operations WHERE state = 'cancelled'"
        ).fetchone()[0]
        == operation_count
    )
    reopened.close()


def test_storage_budgets_reject_oversized_database_and_journal_before_open(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    store.close()
    with (root / "provider.sqlite3").open("r+b") as database:
        database.truncate(provider_store_module.MAX_DATABASE_BYTES + 1)
    with pytest.raises(ProviderStateRootError, match="database exceeds"):
        DesktopProviderStore(root)

    root2 = tmp_path / "state-journal"
    store2 = DesktopProviderStore(root2)
    store2.close()
    with (root2 / "provider.sqlite3-journal").open("wb") as journal:
        journal.truncate(provider_store_module.MAX_JOURNAL_BYTES + 1)
    os.chmod(root2 / "provider.sqlite3-journal", 0o600)
    with pytest.raises(ProviderStateRootError, match="journal exceeds"):
        DesktopProviderStore(root2)


def test_precommit_budget_failure_rolls_back_instead_of_reporting_after_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    original = store._verify_storage_budget
    calls = 0

    def fail_precommit() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ProviderStateRootError("simulated precommit budget rejection")
        original()

    monkeypatch.setattr(store, "_verify_storage_budget", fail_precommit)
    with pytest.raises(ProviderStateRootError, match="precommit"):
        _create_profile(store)

    monkeypatch.setattr(store, "_verify_storage_budget", original)
    assert store.list_profiles().items == ()


def test_existing_state_root_must_remain_private(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o755)
    os.chmod(root, 0o755)

    with pytest.raises(ProviderStateRootError):
        DesktopProviderStore(root)
