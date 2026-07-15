from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import errno
import hmac
import os
from pathlib import Path
import sqlite3
import stat
import struct
import subprocess
import sys
import threading
import time
from typing import Callable, Literal, cast

import pytest

import desktop.sidecar.provider_store as provider_store_module
from desktop.sidecar.contracts.v1.models import (
    ApiErrorV1,
    ConnectionOperationResultV1,
    CredentialSlotStatusV1,
    LocalOperationV1,
    NormalizedCheckV1,
    ProjectCreateV1,
    ProjectOperationResultV1,
    ProjectPatchV1,
    ProjectV1,
    RemoteProjectStateV1,
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
from openevo.backend.contracts.v1 import models as core_v1
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


def _remote_project_state(
    project_id: str,
    revision_id: str = "core-revision-0001",
    *,
    status: Literal["draft", "ready", "blocked", "archived"] = "ready",
) -> RemoteProjectStateV1:
    ready = status == "ready"
    core_project_id = f"core-{project_id}"
    return RemoteProjectStateV1(
        core_project_id=core_project_id,
        status=status,
        active_revision=(
            core_v1.RevisionRefV1(
                id=revision_id,
                project_id=core_project_id,
                generation=0,
                manifest_sha256="a" * 64,
            )
            if ready
            else None
        ),
        registry_digest="b" * 64 if ready else None,
        model_preparation=core_v1.ModelPreparationV1(
            model_ref="gpt-5-codex",
            status=(
                core_v1.ModelPreparationStatus.READY
                if ready
                else core_v1.ModelPreparationStatus.UNRESOLVED
            ),
            updated_at="2026-07-14T12:00:00Z",
        ),
        observed_at="2026-07-14T12:00:00Z",
        etag='"' + "c" * 64 + '"',
    )


def _activate_project(
    store: DesktopProviderStore,
    project: ProjectV1,
    *,
    key: str = "project-remote-activation-0001",
    revision_id: str = "core-revision-0001",
) -> tuple[LocalOperationV1, RemoteProjectStateV1]:
    action = {
        "route": f"/desktop/v1/projects/{project.project_id}/activate",
        "operation_kind": "project_activate",
        "project_id": project.project_id,
        "key": key,
        "body": {},
        "if_match": project.etag,
    }
    reservation = store.begin_project_runtime_action(**action)
    store.start_project_runtime_action(reservation=reservation, **action)
    remote = _remote_project_state(project.project_id, revision_id)
    operation = store.complete_project_runtime_action(
        reservation=reservation,
        remote_state=remote,
        **action,
    )
    return operation, remote


def _reseal_provider_usage(
    connection: sqlite3.Connection,
    root: Path,
) -> tuple[tuple[int, ...], bytes]:
    values = tuple(
        cast(
            tuple[int, ...],
            connection.execute(
                "SELECT total_rows, total_bytes, remote_payload_count, "
                "remote_payload_bytes, remote_accumulator_0, remote_accumulator_1, "
                "remote_accumulator_2, remote_accumulator_3, profile_reservations, "
                "project_reservations, idempotency_record_count, "
                "pagination_cursor_count, generation FROM provider_storage_usage"
            ).fetchone(),
        )
    )
    tag = hmac.digest(
        (root / "cursor-signing.key").read_bytes(),
        provider_store_module._PROVIDER_STORAGE_USAGE_AUTHORITY_DOMAIN
        + struct.pack(f">{len(values)}Q", *values),
        "sha256",
    )
    updated = connection.execute(
        "UPDATE provider_storage_usage SET authority_tag = ? WHERE singleton = 1",
        (tag,),
    )
    assert updated.rowcount == 1
    return values, tag


def _provider_usage_row(connection: sqlite3.Connection) -> tuple[object, ...]:
    return tuple(connection.execute("SELECT * FROM provider_storage_usage").fetchone())


def _replay_provider_usage(
    connection: sqlite3.Connection,
    row: tuple[object, ...],
) -> None:
    columns = provider_store_module._PROVIDER_USAGE_COLUMNS
    updated = connection.execute(
        f"UPDATE provider_storage_usage SET "
        f"{', '.join(f'{column} = ?' for column in columns)} WHERE singleton = 1",
        row[1:],
    )
    assert updated.rowcount == 1


def _initialize_empty_v4_provider_store(root: Path) -> tuple[int, int]:
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    (root / "provider.lock").touch(mode=0o600)
    key = b"k" * 32
    (root / "cursor-signing.key").write_bytes(key)
    os.chmod(root / "cursor-signing.key", 0o600)
    timestamp = "2026-07-14T12:00:00.000000Z"
    authority_tag = hmac.digest(
        key,
        provider_store_module._REMOTE_PAYLOAD_USAGE_AUTHORITY_DOMAIN + struct.pack(">QQ", 0, 0),
        "sha256",
    )
    with sqlite3.connect(root / "provider.sqlite3") as connection:
        for statement in provider_store_module._SCHEMA_V4:
            connection.execute(statement)
        connection.executemany(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            ((version, timestamp) for version in range(1, 5)),
        )
        connection.execute(
            "INSERT INTO remote_payload_usage VALUES (1, 0, 0, ?)",
            (authority_tag,),
        )
        connection.execute("PRAGMA user_version = 4")
        usage = DesktopProviderStore._recovery_usage_v4(
            connection,
            include_remote_payload_usage=True,
        )
    os.chmod(root / "provider.sqlite3", 0o600)
    return usage


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

    assert tuple(store._connection.execute("PRAGMA user_version").fetchone()) == (5,)
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
    ] == [(1,), (2,), (3,), (4,), (5,)]
    assert tuple(
        store._connection.execute(
            "SELECT total_rows, remote_payload_count, remote_payload_bytes, "
            "length(authority_tag) FROM provider_storage_usage"
        ).fetchone()
    ) == (8, 0, 0, 32)
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


def test_migrates_a_canonical_v1_store_to_v5(tmp_path: Path) -> None:
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

    assert tuple(store._connection.execute("PRAGMA user_version").fetchone()) == (5,)
    assert [
        tuple(row)
        for row in store._connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    ] == [(1,), (2,), (3,), (4,), (5,)]
    profile = _create_profile(store, key="post-migration-create")
    assert store.get_profile(profile.profile_id) == profile


def test_migrates_a_canonical_v2_store_to_v5(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    (root / "provider.lock").touch(mode=0o600)
    (root / "cursor-signing.key").write_bytes(b"k" * 32)
    os.chmod(root / "cursor-signing.key", 0o600)
    with sqlite3.connect(root / "provider.sqlite3") as connection:
        for statement in provider_store_module._SCHEMA_V2:
            connection.execute(statement)
        connection.executemany(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (
                (1, "2026-07-14T12:00:00.000000Z"),
                (2, "2026-07-14T12:00:00.000000Z"),
            ),
        )
        connection.execute(
            """
            INSERT INTO remote_profiles(
                profile_id, name, document_json, connection_state,
                credential_slots_json, host_key_fingerprint, resource_version,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'disconnected', ?, NULL, 1, ?, ?)
            """,
            (
                "profile-v2",
                "Research server",
                provider_store_module._canonical_json_bytes(_profile()),
                b"[]",
                "2026-07-14T12:00:00.000000Z",
                "2026-07-14T12:00:00.000000Z",
            ),
        )
        connection.execute(
            """
            INSERT INTO projects(
                project_id, profile_id, name, document_json, state,
                current_revision_id, resource_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'active', ?, 1, ?, ?)
            """,
            (
                "project-v2",
                "profile-v2",
                "Protein design",
                provider_store_module._canonical_json_bytes(_project("profile-v2")),
                "legacy-revision",
                "2026-07-14T12:00:00.000000Z",
                "2026-07-14T12:00:00.000000Z",
            ),
        )
        connection.execute("PRAGMA user_version = 2")
    os.chmod(root / "provider.sqlite3", 0o600)

    store = DesktopProviderStore(root)

    assert tuple(store._connection.execute("PRAGMA user_version").fetchone()) == (5,)
    assert [
        tuple(row)
        for row in store._connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    ] == [(1,), (2,), (3,), (4,), (5,)]
    assert [row[1] for row in store._connection.execute("PRAGMA table_info(projects)")] == [
        "project_id",
        "profile_id",
        "name",
        "document_json",
        "state",
        "current_revision_id",
        "remote_state_json",
        "remote_state_token_0",
        "remote_state_token_1",
        "remote_state_token_2",
        "remote_state_token_3",
        "resource_version",
        "created_at",
        "updated_at",
    ]
    migrated = store.get_project("project-v2")
    assert migrated.name == "Protein design"
    assert migrated.state == "draft"
    assert migrated.remote is None
    assert tuple(
        store._connection.execute(
            "SELECT current_revision_id, resource_version FROM projects WHERE project_id = ?",
            ("project-v2",),
        ).fetchone()
    ) == (None, 2)
    schema_sql = cast(
        str,
        store._connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = 'projects'"
        ).fetchone()[0],
    )
    assert (
        f"length(remote_state_json) <= {provider_store_module.MAX_REMOTE_PROJECT_STATE_BYTES}"
    ) in schema_sql
    with pytest.raises(sqlite3.IntegrityError):
        store._connection.execute(
            "UPDATE projects SET remote_state_json = zeroblob(?) WHERE project_id = ?",
            (provider_store_module.MAX_REMOTE_PROJECT_STATE_BYTES + 1, "project-v2"),
        )


def test_migrates_v3_remote_payload_usage_into_provider_authority(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    (root / "provider.lock").touch(mode=0o600)
    (root / "cursor-signing.key").write_bytes(b"k" * 32)
    os.chmod(root / "cursor-signing.key", 0o600)
    remote = _remote_project_state("project-v3", "revision-v3")
    remote_bytes = provider_store_module._canonical_json_bytes(remote.model_dump(mode="json"))
    timestamp = "2026-07-14T12:00:00.000000Z"
    with sqlite3.connect(root / "provider.sqlite3") as connection:
        for statement in provider_store_module._SCHEMA_V3:
            connection.execute(statement)
        connection.executemany(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            ((1, timestamp), (2, timestamp), (3, timestamp)),
        )
        connection.execute(
            """
            INSERT INTO remote_profiles(
                profile_id, name, document_json, connection_state,
                credential_slots_json, host_key_fingerprint, resource_version,
                created_at, updated_at
            ) VALUES ('profile-v3', 'Research server', ?, 'disconnected', ?, NULL, 1, ?, ?)
            """,
            (provider_store_module._canonical_json_bytes(_profile()), b"[]", timestamp, timestamp),
        )
        connection.execute(
            """
            INSERT INTO projects(
                project_id, profile_id, name, document_json, state,
                current_revision_id, remote_state_json, resource_version,
                created_at, updated_at
            ) VALUES ('project-v3', 'profile-v3', 'Protein design', ?, 'active', ?, ?, 1, ?, ?)
            """,
            (
                provider_store_module._canonical_json_bytes(_project("profile-v3")),
                "revision-v3",
                remote_bytes,
                timestamp,
                timestamp,
            ),
        )
        connection.execute("PRAGMA user_version = 3")
    os.chmod(root / "provider.sqlite3", 0o600)

    store = DesktopProviderStore(root)

    assert tuple(store._connection.execute("PRAGMA user_version").fetchone()) == (5,)
    assert tuple(
        store._connection.execute(
            "SELECT remote_payload_count, remote_payload_bytes, length(authority_tag) "
            "FROM provider_storage_usage"
        ).fetchone()
    ) == (1, len(remote_bytes), 32)
    migrated = store.get_project("project-v3")
    assert migrated.state == "draft"
    assert migrated.remote == remote


def test_v3_to_v4_usage_migration_is_one_crash_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    (root / "provider.lock").touch(mode=0o600)
    (root / "cursor-signing.key").write_bytes(b"k" * 32)
    os.chmod(root / "cursor-signing.key", 0o600)
    timestamp = "2026-07-14T12:00:00.000000Z"
    with sqlite3.connect(root / "provider.sqlite3") as connection:
        for statement in provider_store_module._SCHEMA_V3:
            connection.execute(statement)
        connection.executemany(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            ((1, timestamp), (2, timestamp), (3, timestamp)),
        )
        connection.execute("PRAGMA user_version = 3")
    os.chmod(root / "provider.sqlite3", 0o600)
    original = DesktopProviderStore._migrate_v3_to_v4

    def crash_after_migration(self: DesktopProviderStore, connection: sqlite3.Connection) -> None:
        original(self, connection)
        raise RuntimeError("injected usage migration crash")

    monkeypatch.setattr(DesktopProviderStore, "_migrate_v3_to_v4", crash_after_migration)
    with pytest.raises(RuntimeError, match="usage migration crash"):
        DesktopProviderStore(root)

    with sqlite3.connect(root / "provider.sqlite3") as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (3,)
        assert (
            connection.execute(
                "SELECT name FROM sqlite_schema WHERE name = 'remote_payload_usage'"
            ).fetchone()
            is None
        )
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,), (2,), (3,)]


def test_v4_to_v5_provider_authority_migration_is_one_crash_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    (root / "provider.lock").touch(mode=0o600)
    key = b"k" * 32
    (root / "cursor-signing.key").write_bytes(key)
    os.chmod(root / "cursor-signing.key", 0o600)
    timestamp = "2026-07-14T12:00:00.000000Z"
    authority_tag = hmac.digest(
        key,
        provider_store_module._REMOTE_PAYLOAD_USAGE_AUTHORITY_DOMAIN + struct.pack(">QQ", 0, 0),
        "sha256",
    )
    with sqlite3.connect(root / "provider.sqlite3") as connection:
        for statement in provider_store_module._SCHEMA_V4:
            connection.execute(statement)
        connection.executemany(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            ((version, timestamp) for version in range(1, 5)),
        )
        connection.execute(
            "INSERT INTO remote_payload_usage VALUES (1, 0, 0, ?)",
            (authority_tag,),
        )
        connection.execute("PRAGMA user_version = 4")
    os.chmod(root / "provider.sqlite3", 0o600)
    original = DesktopProviderStore._migrate_v4_to_v5

    def crash_after_migration(self: DesktopProviderStore, connection: sqlite3.Connection) -> None:
        original(self, connection)
        raise RuntimeError("injected v5 authority migration crash")

    monkeypatch.setattr(DesktopProviderStore, "_migrate_v4_to_v5", crash_after_migration)
    with pytest.raises(RuntimeError, match="v5 authority migration crash"):
        DesktopProviderStore(root)

    with sqlite3.connect(root / "provider.sqlite3") as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (4,)
        assert connection.execute(
            "SELECT payload_count, payload_bytes FROM remote_payload_usage"
        ).fetchone() == (0, 0)
        assert (
            connection.execute(
                "SELECT name FROM sqlite_schema WHERE name = 'provider_storage_usage'"
            ).fetchone()
            is None
        )


@pytest.mark.parametrize(
    ("budget_name", "usage_index"),
    (("MAX_RECOVERY_ROWS", 0), ("MAX_RECOVERY_BYTES", 1)),
    ids=("row-boundary", "byte-boundary"),
)
def test_v4_to_v5_migration_validates_final_write_budget_before_seal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    budget_name: str,
    usage_index: int,
) -> None:
    root = tmp_path / "state"
    v4_usage = _initialize_empty_v4_provider_store(root)
    original_budget = getattr(provider_store_module, budget_name)
    original_seal = DesktopProviderStore._seal_provider_storage_usage
    seal_called = False

    def unexpected_seal(
        _self: DesktopProviderStore,
        _connection: sqlite3.Connection,
    ) -> tuple[tuple[int, ...], bytes]:
        nonlocal seal_called
        seal_called = True
        raise AssertionError("v5 authority was sealed before final budget validation")

    monkeypatch.setattr(provider_store_module, budget_name, v4_usage[usage_index])
    monkeypatch.setattr(DesktopProviderStore, "_seal_provider_storage_usage", unexpected_seal)

    with pytest.raises(ProviderDataCorruptionError, match="provider recovery budget exceeded"):
        DesktopProviderStore(root)

    assert seal_called is False
    with sqlite3.connect(root / "provider.sqlite3") as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (4,)
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,), (2,), (3,), (4,)]
        assert (
            connection.execute(
                "SELECT name FROM sqlite_schema WHERE name = 'provider_storage_usage'"
            ).fetchone()
            is None
        )

    monkeypatch.setattr(provider_store_module, budget_name, original_budget)
    monkeypatch.setattr(DesktopProviderStore, "_seal_provider_storage_usage", original_seal)
    reopened = DesktopProviderStore(root)
    assert tuple(reopened._connection.execute("PRAGMA user_version").fetchone()) == (5,)


def test_v3_usage_migration_applies_recovery_budget_before_payload_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    (root / "provider.lock").touch(mode=0o600)
    (root / "cursor-signing.key").write_bytes(b"k" * 32)
    os.chmod(root / "cursor-signing.key", 0o600)
    timestamp = "2026-07-14T12:00:00.000000Z"
    with sqlite3.connect(root / "provider.sqlite3") as connection:
        for statement in provider_store_module._SCHEMA_V3:
            connection.execute(statement)
        connection.executemany(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            ((1, timestamp), (2, timestamp), (3, timestamp)),
        )
        connection.execute("PRAGMA user_version = 3")
    os.chmod(root / "provider.sqlite3", 0o600)

    def unexpected_payload_scan(_connection: sqlite3.Connection) -> tuple[int, int, int]:
        raise AssertionError("remote payload scan ran before the recovery row budget")

    monkeypatch.setattr(provider_store_module, "MAX_RECOVERY_ROWS", 2)
    monkeypatch.setattr(
        DesktopProviderStore,
        "_remote_state_recovery_usage",
        staticmethod(unexpected_payload_scan),
    )

    with pytest.raises(ProviderDataCorruptionError, match="provider recovery budget"):
        DesktopProviderStore(root)


def test_v2_migration_rejects_a_noncanonical_ledger(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    (root / "provider.lock").touch(mode=0o600)
    (root / "cursor-signing.key").write_bytes(b"k" * 32)
    os.chmod(root / "cursor-signing.key", 0o600)
    with sqlite3.connect(root / "provider.sqlite3") as connection:
        for statement in provider_store_module._SCHEMA_V2:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (1, ?)",
            ("2026-07-14T12:00:00.000000Z",),
        )
        connection.execute("PRAGMA user_version = 2")
    os.chmod(root / "provider.sqlite3", 0o600)

    with pytest.raises(ProviderSchemaError, match="ledger"):
        DesktopProviderStore(root)


def test_v2_to_v3_migration_is_one_crash_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    (root / "provider.lock").touch(mode=0o600)
    (root / "cursor-signing.key").write_bytes(b"k" * 32)
    os.chmod(root / "cursor-signing.key", 0o600)
    with sqlite3.connect(root / "provider.sqlite3") as connection:
        for statement in provider_store_module._SCHEMA_V2:
            connection.execute(statement)
        connection.executemany(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (
                (1, "2026-07-14T12:00:00.000000Z"),
                (2, "2026-07-14T12:00:00.000000Z"),
            ),
        )
        connection.execute("PRAGMA user_version = 2")
    os.chmod(root / "provider.sqlite3", 0o600)
    original = DesktopProviderStore._migrate_v2_to_v3

    def crash_after_migration(self: DesktopProviderStore, connection: sqlite3.Connection) -> None:
        original(self, connection)
        raise RuntimeError("injected migration crash")

    monkeypatch.setattr(DesktopProviderStore, "_migrate_v2_to_v3", crash_after_migration)
    with pytest.raises(RuntimeError, match="injected migration crash"):
        DesktopProviderStore(root)

    with sqlite3.connect(root / "provider.sqlite3") as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)
        assert [row[1] for row in connection.execute("PRAGMA table_info(projects)")] == [
            "project_id",
            "profile_id",
            "name",
            "document_json",
            "state",
            "current_revision_id",
            "resource_version",
            "created_at",
            "updated_at",
        ]
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,), (2,)]


def test_rejects_unreleased_intermediate_v2_without_exact_action_authority(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    (root / "provider.lock").touch(mode=0o600)
    (root / "cursor-signing.key").write_bytes(b"k" * 32)
    os.chmod(root / "cursor-signing.key", 0o600)
    intermediate_schema = list(provider_store_module._SCHEMA_V2)
    intermediate_schema[3] = (
        intermediate_schema[3]
        .replace("        action_identity_digest TEXT UNIQUE,\n", "")
        .replace(
            ",\n        CHECK (\n"
            "            action_identity_digest IS NULL OR "
            "length(action_identity_digest) = 64\n"
            "        )",
            "",
        )
    )
    intermediate_schema[4] = (
        intermediate_schema[4]
        .replace("        operation_id TEXT UNIQUE,\n", "")
        .replace(
            "        CHECK (\n"
            "            operation_id IS NULL OR\n"
            "            length(CAST(operation_id AS BLOB)) BETWEEN 1 AND 512\n"
            "        ),\n",
            "",
        )
        .replace(
            ",\n        CHECK (\n"
            "            (response_type = 'LocalOperationV1' "
            "AND operation_id IS NOT NULL) OR\n"
            "            (response_type != 'LocalOperationV1' "
            "AND operation_id IS NULL)\n"
            "        ),\n"
            "        FOREIGN KEY (operation_id) REFERENCES "
            "local_operations(operation_id) ON DELETE RESTRICT",
            "",
        )
    )
    assert "action_identity_digest" not in intermediate_schema[3]
    assert "operation_id TEXT UNIQUE" not in intermediate_schema[4]
    with sqlite3.connect(root / "provider.sqlite3") as connection:
        for statement in intermediate_schema:
            connection.execute(statement)
        connection.executemany(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (
                (1, "2026-07-14T12:00:00.000000Z"),
                (2, "2026-07-14T12:00:00.000000Z"),
            ),
        )
        connection.execute("PRAGMA user_version = 2")
    os.chmod(root / "provider.sqlite3", 0o600)

    with pytest.raises(ProviderSchemaError, match="fingerprint"):
        DesktopProviderStore(root)


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
        _reseal_provider_usage(connection, root)

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
        _reseal_provider_usage(connection, root)

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


def test_remote_project_projection_survives_restart_as_historical_observation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    profile = _create_profile(store)
    project = store.create_project(
        _project(profile.profile_id), idempotency_key="remote-restart-project-create"
    )
    _, remote = _activate_project(store, project)
    active = store.get_project(project.project_id)
    assert active.state == "active"
    assert active.remote == remote
    store.close()

    reopened = DesktopProviderStore(root)
    historical = reopened.get_project(project.project_id)
    assert historical.state == "draft"
    assert historical.remote == remote
    assert (
        reopened._connection.execute(
            "SELECT current_revision_id FROM projects WHERE project_id = ?",
            (project.project_id,),
        ).fetchone()[0]
        is None
    )
    reopened.close()

    reopened_again = DesktopProviderStore(root)
    historical_again = reopened_again.get_project(project.project_id)
    assert historical_again.remote == remote
    reopened_again.delete_project(project.project_id, if_match=historical_again.etag)
    assert reopened_again._validate_remote_payload_usage_authority(reopened_again._connection) == (
        0,
        0,
    )


@pytest.mark.parametrize(
    "corruption",
    ["noncanonical", "project_identity", "revision_identity"],
)
def test_remote_project_projection_recovery_fails_closed(tmp_path: Path, corruption: str) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    profile = _create_profile(store)
    project = store.create_project(
        _project(profile.profile_id), idempotency_key=f"remote-corrupt-{corruption}-create"
    )
    _, remote = _activate_project(
        store,
        project,
        key=f"remote-corrupt-{corruption}-activate",
    )
    if corruption == "noncanonical":
        remote_bytes = (
            provider_store_module._canonical_json_bytes(remote.model_dump(mode="json")) + b" "
        )
    else:
        remote_value = remote.model_dump(mode="json")
        if corruption == "project_identity":
            cast(dict[str, object], remote_value["active_revision"])["project_id"] = (
                "different-project"
            )
        else:
            cast(dict[str, object], remote_value["active_revision"])["id"] = "different-revision"
        remote_bytes = provider_store_module._canonical_json_bytes(remote_value)
    remote_token = store._remote_payload_content_token(
        project_id=project.project_id,
        payload=remote_bytes,
    )
    store.close()
    with sqlite3.connect(root / "provider.sqlite3") as connection:
        connection.execute(
            """
            UPDATE projects
            SET remote_state_json = ?,
                remote_state_token_0 = ?, remote_state_token_1 = ?,
                remote_state_token_2 = ?, remote_state_token_3 = ?
            WHERE project_id = ?
            """,
            (remote_bytes, *remote_token, project.project_id),
        )
        _reseal_provider_usage(connection, root)

    with pytest.raises(ProviderDataCorruptionError, match="remote project"):
        DesktopProviderStore(root)


@pytest.mark.parametrize("budget", ["per_row", "aggregate"])
def test_remote_project_projection_budget_rejects_before_payload_read_or_parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    budget: str,
) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    profile = _create_profile(store)
    first = store.create_project(
        _project(profile.profile_id, name="First"),
        idempotency_key=f"remote-budget-{budget}-first",
    )
    _, first_remote = _activate_project(
        store,
        first,
        key=f"remote-budget-{budget}-first-activate",
    )
    if budget == "aggregate":
        second = store.create_project(
            _project(profile.profile_id, name="Second"),
            idempotency_key="remote-budget-aggregate-second",
        )
        _activate_project(store, second, key="remote-budget-aggregate-second-activate")
    store.close()

    if budget == "per_row":
        remote_bytes = provider_store_module._canonical_json_bytes(
            first_remote.model_dump(mode="json")
        )
        monkeypatch.setattr(
            provider_store_module,
            "MAX_REMOTE_PROJECT_STATE_BYTES",
            len(remote_bytes) - 1,
        )
    else:
        with sqlite3.connect(root / "provider.sqlite3") as connection:
            total_bytes = cast(
                int,
                connection.execute(
                    "SELECT sum(length(remote_state_json)) FROM projects"
                ).fetchone()[0],
            )
        monkeypatch.setattr(
            provider_store_module,
            "MAX_REMOTE_PROJECT_STATE_RECOVERY_BYTES",
            total_bytes - 1,
        )

    statements: list[str] = []
    original_connect = sqlite3.connect

    class ProbeConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters=()):
            normalized = " ".join(sql.lower().split())
            if normalized.startswith("select"):
                statements.append(normalized)
            return super().execute(sql, parameters)

    def connect(*args, **kwargs):
        kwargs["factory"] = ProbeConnection
        return original_connect(*args, **kwargs)

    original_decode = provider_store_module._decode_json_object

    def decode_probe(raw: bytes, *, label: str):
        if label == "remote project state":
            raise AssertionError("oversized remote state reached JSON parsing")
        return original_decode(raw, label=label)

    monkeypatch.setattr(provider_store_module.sqlite3, "connect", connect)
    monkeypatch.setattr(provider_store_module, "_decode_json_object", decode_probe)

    with pytest.raises(ProviderDataCorruptionError, match="remote project state"):
        DesktopProviderStore(root)

    assert not any("select * from projects" in statement for statement in statements)
    assert not any("then remote_state_json" in statement for statement in statements)


@pytest.mark.parametrize("corruption", ["counter", "row"])
def test_remote_payload_usage_tamper_fails_before_project_payload_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    profile = _create_profile(store)
    project = store.create_project(
        _project(profile.profile_id), idempotency_key=f"usage-tamper-{corruption}-create"
    )
    _, remote = _activate_project(store, project, key=f"usage-tamper-{corruption}-activate")
    with sqlite3.connect(root / "provider.sqlite3") as connection:
        if corruption == "counter":
            connection.execute(
                "UPDATE provider_storage_usage SET remote_payload_bytes = remote_payload_bytes + 1"
            )
        else:
            changed = remote.model_copy(update={"observed_at": "2026-07-14T12:00:01Z"})
            connection.execute(
                "UPDATE projects SET remote_state_json = ? WHERE project_id = ?",
                (
                    provider_store_module._canonical_json_bytes(changed.model_dump(mode="json")),
                    project.project_id,
                ),
            )

    original_decode = provider_store_module._decode_json_object

    def decode_probe(raw: bytes, *, label: str):
        if label == "remote project state":
            raise AssertionError("tampered remote state reached JSON parsing")
        return original_decode(raw, label=label)

    monkeypatch.setattr(provider_store_module, "_decode_json_object", decode_probe)
    with pytest.raises(ProviderDataCorruptionError, match="storage usage authority"):
        store.get_profile(profile.profile_id)


def test_startup_reconciles_sealed_usage_authority_against_project_rows(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    profile = _create_profile(store)
    project = store.create_project(
        _project(profile.profile_id), idempotency_key="usage-reconcile-create"
    )
    _activate_project(store, project, key="usage-reconcile-activate")
    authority = tuple(
        store._connection.execute(
            "SELECT total_rows, total_bytes, remote_payload_count, remote_payload_bytes, "
            "remote_accumulator_0, remote_accumulator_1, remote_accumulator_2, "
            "remote_accumulator_3, profile_reservations, project_reservations, "
            "idempotency_record_count, pagination_cursor_count, generation "
            "FROM provider_storage_usage"
        ).fetchone()
    )
    forged = (*authority[:3], authority[3] + 1, *authority[4:])
    forged_authority = store._provider_storage_usage_authority_tag(forged)
    store.close()
    with sqlite3.connect(root / "provider.sqlite3") as connection:
        connection.execute(
            """
            UPDATE provider_storage_usage
            SET remote_payload_bytes = ?, authority_tag = ?
            WHERE singleton = 1
            """,
            (forged[3], forged_authority),
        )

    with pytest.raises(ProviderDataCorruptionError, match="differs from provider rows"):
        DesktopProviderStore(root)


def test_provider_usage_singleton_rejects_delete_and_replace_with_recursive_triggers_off(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    row = _provider_usage_row(store._connection)

    with sqlite3.connect(root / "provider.sqlite3") as connection:
        connection.execute("PRAGMA recursive_triggers = OFF")
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            connection.execute("DELETE FROM provider_storage_usage WHERE singleton = 1")
        with pytest.raises(sqlite3.IntegrityError, match="cannot be inserted"):
            connection.execute(
                f"INSERT OR REPLACE INTO provider_storage_usage "
                f"(singleton, {', '.join(provider_store_module._PROVIDER_USAGE_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in row)})",
                row,
            )

    assert _provider_usage_row(store._connection) == row
    store.get_profile(_create_profile(store, key="singleton-guard-profile").profile_id)


@pytest.mark.parametrize("probe", ["online", "restart"])
def test_signed_provider_usage_replay_is_rejected(
    tmp_path: Path,
    probe: str,
) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    profile = _create_profile(store)
    project = store.create_project(
        _project(profile.profile_id), idempotency_key=f"authority-replay-{probe}-create"
    )
    _, remote = _activate_project(store, project, key=f"authority-replay-{probe}-activate")
    stale_authority = _provider_usage_row(store._connection)
    changed = remote.model_copy(update={"observed_at": "2026-07-14T12:00:01Z"})
    changed_bytes = provider_store_module._canonical_json_bytes(changed.model_dump(mode="json"))
    old_bytes = provider_store_module._canonical_json_bytes(remote.model_dump(mode="json"))
    assert len(changed_bytes) == len(old_bytes)
    token = store._remote_payload_content_token(
        project_id=project.project_id,
        payload=changed_bytes,
    )
    with store._transaction(write=True) as connection:
        connection.execute(
            """
            UPDATE projects
            SET remote_state_json = ?,
                remote_state_token_0 = ?, remote_state_token_1 = ?,
                remote_state_token_2 = ?, remote_state_token_3 = ?
            WHERE project_id = ?
            """,
            (changed_bytes, *token, project.project_id),
        )
    assert store.get_project(project.project_id).remote == changed

    with sqlite3.connect(root / "provider.sqlite3") as connection:
        _replay_provider_usage(connection, stale_authority)

    if probe == "online":
        with pytest.raises(ProviderDataCorruptionError, match="replayed during this process"):
            store.get_profile(profile.profile_id)
    else:
        store.close()
        with pytest.raises(ProviderDataCorruptionError, match="differs from provider rows"):
            DesktopProviderStore(root)


@pytest.mark.parametrize("probe", ["online", "restart"])
def test_equal_length_remote_payload_rewrite_cannot_reuse_signed_aggregate(
    tmp_path: Path,
    probe: str,
) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    profile = _create_profile(store)
    project = store.create_project(
        _project(profile.profile_id), idempotency_key=f"content-rewrite-{probe}-create"
    )
    _, remote = _activate_project(store, project, key=f"content-rewrite-{probe}-activate")
    signed_authority = _provider_usage_row(store._connection)
    changed = remote.model_copy(update={"observed_at": "2026-07-14T12:00:01Z"})
    original_bytes = provider_store_module._canonical_json_bytes(remote.model_dump(mode="json"))
    changed_bytes = provider_store_module._canonical_json_bytes(changed.model_dump(mode="json"))
    assert changed_bytes != original_bytes
    assert len(changed_bytes) == len(original_bytes)

    with sqlite3.connect(root / "provider.sqlite3") as connection:
        connection.execute(
            "UPDATE projects SET remote_state_json = ? WHERE project_id = ?",
            (changed_bytes, project.project_id),
        )
        _replay_provider_usage(connection, signed_authority)

    if probe == "restart":
        store.close()
        with pytest.raises(
            ProviderDataCorruptionError,
            match="remote project state content authority",
        ):
            DesktopProviderStore(root)
    else:
        with pytest.raises(
            ProviderDataCorruptionError,
            match="remote project state content authority",
        ):
            store.get_project(project.project_id)


@pytest.mark.parametrize("mutation", ["removed", "corrupt"])
@pytest.mark.parametrize("probe", ["online", "restart"])
def test_provider_usage_trigger_tamper_fails_closed(
    tmp_path: Path,
    mutation: str,
    probe: str,
) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    profile = _create_profile(store)
    trigger_sql = provider_store_module._provider_usage_trigger_v5(
        "projects",
        next(
            columns
            for table, columns in provider_store_module._RECOVERY_USAGE_SPECIFICATIONS
            if table == "projects"
        ),
        "UPDATE",
    )
    with sqlite3.connect(root / "provider.sqlite3") as connection:
        connection.execute("DROP TRIGGER provider_usage_projects_update")
        if mutation == "corrupt":
            connection.execute(trigger_sql.replace("generation + 1", "generation + 2"))

    if probe == "restart":
        store.close()
        with pytest.raises(ProviderSchemaError, match="fingerprint"):
            DesktopProviderStore(root)
    else:
        with pytest.raises(ProviderSchemaError, match="fingerprint"):
            store.get_profile(profile.profile_id)


def test_profile_point_read_does_not_scan_one_hundred_thousand_projects(
    tmp_path: Path,
) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    project_document = provider_store_module._canonical_json_bytes(
        ProjectCreateV1.model_validate(
            _project(profile.profile_id, name="Bulk project")
        ).model_dump(mode="json")
    )
    timestamp = "2026-07-14T12:00:00.000000Z"
    current_rows = cast(
        int,
        store._connection.execute("SELECT total_rows FROM provider_storage_usage").fetchone()[0],
    )
    project_count = provider_store_module.MAX_RECOVERY_ROWS - current_rows
    store._connection.execute("BEGIN IMMEDIATE")
    try:
        store._connection.executemany(
            """
            INSERT INTO projects(
                project_id, profile_id, name, document_json, state,
                current_revision_id, remote_state_json, resource_version,
                created_at, updated_at
            ) VALUES (?, ?, 'Bulk project', ?, 'draft', NULL, NULL, 1, ?, ?)
            """,
            (
                (
                    f"bulk-project-{index:06d}",
                    profile.profile_id,
                    project_document,
                    timestamp,
                    timestamp,
                )
                for index in range(project_count)
            ),
        )
        sealed = store._seal_provider_storage_usage(store._connection)
        store._connection.commit()
        store._provider_usage_snapshot = sealed
    except BaseException:
        store._connection.rollback()
        raise

    plan = tuple(
        str(row[3])
        for row in store._connection.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM remote_profiles WHERE profile_id = ?",
            (profile.profile_id,),
        )
    )
    assert any("SEARCH remote_profiles" in step for step in plan)
    assert not any("SCAN remote_profiles" in step for step in plan)
    authority_plan = tuple(
        str(row[3])
        for row in store._connection.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM provider_storage_usage WHERE singleton = 1"
        )
    )
    assert any(
        "SEARCH provider_storage_usage USING PRIMARY KEY" in step for step in authority_plan
    )
    assert not any("SCAN provider_storage_usage" in step for step in authority_plan)
    statements: list[str] = []
    store._connection.set_trace_callback(
        lambda statement: statements.append(" ".join(statement.lower().split()))
    )

    patched = store.patch_profile(
        profile.profile_id,
        {"name": "Research serveX"},
        if_match=profile.etag,
    )
    assert patched.name == "Research serveX"
    assert not any(" from projects" in statement for statement in statements)
    assert sum("from provider_storage_usage" in statement for statement in statements) == 3
    for table, _ in provider_store_module._RECOVERY_USAGE_SPECIFICATIONS:
        assert not any(
            ("count(" in statement or "sum(" in statement) and f" from {table}" in statement
            for statement in statements
        )


def test_project_pagination_never_runs_remote_payload_aggregate_scans(
    tmp_path: Path,
) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    for index in range(3):
        store.create_project(
            _project(profile.profile_id, name=f"Project {index}"),
            idempotency_key=f"usage-pagination-create-{index:02d}",
        )
    statements: list[str] = []
    store._connection.set_trace_callback(
        lambda statement: statements.append(" ".join(statement.lower().split()))
    )

    first = store.list_projects(limit=1, sort="name", direction="asc")
    assert first.next_cursor is not None
    second = store.list_projects(
        limit=1,
        after=first.next_cursor,
        sort="name",
        direction="asc",
    )

    assert len(first.items) == len(second.items) == 1
    assert first.items[0].project_id != second.items[0].project_id
    assert not any(
        "sum(length(cast(remote_state_json as blob)))" in statement
        or "max(length(cast(remote_state_json as blob)))" in statement
        for statement in statements
    )


def test_normal_provider_writes_use_o1_counters_and_bounded_expiry_cleanup(
    tmp_path: Path,
) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    project = store.create_project(
        _project(profile.profile_id),
        idempotency_key="sql-plan-project-create",
    )

    def traced(write: Callable[[], object]) -> tuple[str, ...]:
        statements: list[str] = []
        store._connection.set_trace_callback(
            lambda statement: statements.append(" ".join(statement.lower().split()))
        )
        try:
            write()
        finally:
            store._connection.set_trace_callback(None)
        return tuple(statements)

    traces = {
        "create": traced(
            lambda: _create_profile(
                store,
                name="SQL trace profile",
                key="sql-trace-profile-create",
            )
        ),
        "profile-action": traced(
            lambda: store.begin_profile_runtime_action(
                route=f"/desktop/v1/profiles/{profile.profile_id}/disconnect",
                operation_kind="profile_disconnect",
                profile_id=profile.profile_id,
                key="sql-trace-profile-action",
                body={},
                if_match=profile.etag,
                displace_existing=False,
            )
        ),
        "project-action": traced(
            lambda: store.begin_project_runtime_action(
                route=f"/desktop/v1/projects/{project.project_id}/workspace-sync",
                operation_kind="workspace_sync",
                project_id=project.project_id,
                key="sql-trace-project-action",
                body={},
                if_match=project.etag,
            )
        ),
        "cursor": traced(lambda: store.list_profiles(limit=1, sort="name", direction="asc")),
    }

    for path in ("create", "profile-action", "project-action"):
        statements = traces[path]
        assert not any("select count(*) from idempotency_records" in sql for sql in statements)
        assert any(
            "from idempotency_records indexed by idempotency_expiry_idx" in sql and "limit" in sql
            for sql in statements
        )
        assert any(
            "idempotency_record_count" in sql
            and "from provider_storage_usage where singleton = 1" in sql
            for sql in statements
        )
    cursor_statements = traces["cursor"]
    assert not any("select count(*) from pagination_cursors" in sql for sql in cursor_statements)
    assert any(
        "delete from pagination_cursors" in sql
        and "pagination_cursors_expiry_idx" in sql
        and "limit" in sql
        for sql in cursor_statements
    )
    assert any(
        "pagination_cursor_count" in sql
        and "from provider_storage_usage where singleton = 1" in sql
        for sql in cursor_statements
    )


def test_normal_cleanup_queries_are_indexed_and_return_a_fixed_batch(tmp_path: Path) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    idempotency_plan = tuple(
        str(row[3])
        for row in store._connection.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT * FROM idempotency_records INDEXED BY idempotency_expiry_idx
            WHERE cleanup_eligible = 1 AND expires_at_epoch <= ?
            ORDER BY expires_at_epoch
            LIMIT ?
            """,
            (0, provider_store_module.NORMAL_WRITE_CLEANUP_ROWS),
        )
    )
    assert any(
        "SEARCH idempotency_records USING INDEX idempotency_expiry_idx" in step
        for step in idempotency_plan
    )
    assert not any("SCAN idempotency_records" in step for step in idempotency_plan)

    cursor_plan = tuple(
        str(row[3])
        for row in store._connection.execute(
            """
            EXPLAIN QUERY PLAN
            DELETE FROM pagination_cursors
            WHERE cursor_digest IN (
                SELECT cursor_digest
                FROM pagination_cursors INDEXED BY pagination_cursors_expiry_idx
                WHERE expires_at_epoch <= ?
                ORDER BY expires_at_epoch
                LIMIT ?
            )
            """,
            (0, provider_store_module.NORMAL_WRITE_CLEANUP_ROWS),
        )
    )
    assert any(
        "SEARCH pagination_cursors USING INDEX pagination_cursors_expiry_idx" in step
        for step in cursor_plan
    )
    assert not any("SCAN pagination_cursors" in step for step in cursor_plan)

    counter_plan = tuple(
        str(row[3])
        for row in store._connection.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT idempotency_record_count, pagination_cursor_count
            FROM provider_storage_usage WHERE singleton = 1
            """
        )
    )
    assert any("SEARCH provider_storage_usage USING PRIMARY KEY" in step for step in counter_plan)
    assert not any("SCAN provider_storage_usage" in step for step in counter_plan)


def test_provider_usage_authority_tracks_crud_replay_and_operation_lifecycle(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)

    def assert_reconciled() -> None:
        store._reconcile_provider_storage_usage(store._connection)

    profile = _create_profile(store, key="usage-lifecycle-profile")
    assert_reconciled()
    replayed = store.create_profile(_profile(), idempotency_key="usage-lifecycle-profile")
    assert replayed == profile
    assert_reconciled()
    project = store.create_project(
        _project(profile.profile_id), idempotency_key="usage-lifecycle-project"
    )
    assert_reconciled()
    patched = store.patch_project(project.project_id, {"name": "Changed"}, if_match=project.etag)
    assert_reconciled()
    action = {
        "route": f"/desktop/v1/projects/{patched.project_id}/workspace-sync",
        "operation_kind": "workspace_sync",
        "project_id": patched.project_id,
        "key": "usage-lifecycle-operation",
        "body": {},
        "if_match": patched.etag,
    }
    reservation = store.begin_project_runtime_action(**action)
    assert_reconciled()
    store.start_project_runtime_action(reservation=reservation, **action)
    assert_reconciled()
    store.complete_project_runtime_action(reservation=reservation, remote_state=None, **action)
    assert_reconciled()
    store.delete_project(patched.project_id, if_match=patched.etag)
    assert_reconciled()
    store.delete_profile(profile.profile_id, if_match=profile.etag)
    assert_reconciled()
    store.close()
    reopened = DesktopProviderStore(root)
    reopened._reconcile_provider_storage_usage(reopened._connection)


def test_active_project_intent_patch_invalidates_remote_and_allows_reactivation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    profile = _create_profile(store)
    project = store.create_project(
        _project(profile.profile_id), idempotency_key="remote-patch-project-create"
    )
    _, remote = _activate_project(store, project, key="remote-patch-project-activate")
    active = store.get_project(project.project_id)
    assert active.remote == remote
    version_before = store._connection.execute(
        "SELECT resource_version FROM projects WHERE project_id = ?",
        (project.project_id,),
    ).fetchone()[0]

    patched = store.patch_project(
        active.project_id, {"name": "Changed intent"}, if_match=active.etag
    )

    assert patched.state == "draft"
    assert patched.remote is None
    row = store._connection.execute(
        "SELECT current_revision_id, remote_state_json, resource_version "
        "FROM projects WHERE project_id = ?",
        (project.project_id,),
    ).fetchone()
    assert tuple(row) == (None, None, version_before + 1)
    assert patched.etag == store._etag("project", project.project_id, version_before + 1)
    assert store._validate_remote_payload_usage_authority(store._connection) == (0, 0)

    reservation = store.begin_project_runtime_action(
        route=f"/desktop/v1/projects/{project.project_id}/activate",
        operation_kind="project_activate",
        project_id=project.project_id,
        key="remote-patch-project-reactivate",
        body={},
        if_match=patched.etag,
    )
    assert reservation.project == patched


@pytest.mark.parametrize("start_operation", [False, True], ids=["queued", "running"])
def test_displacing_activation_reservation_blocks_active_project_intent_patch(
    tmp_path: Path,
    start_operation: bool,
) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    first = store.create_project(
        _project(profile.profile_id, name="First"),
        idempotency_key=f"remote-patch-authority-first-{start_operation}",
    )
    second = store.create_project(
        _project(profile.profile_id, name="Second"),
        idempotency_key=f"remote-patch-authority-second-{start_operation}",
    )
    _activate_project(
        store,
        first,
        key=f"remote-patch-authority-first-activate-{start_operation}",
    )
    active = store.get_project(first.project_id)
    action = {
        "route": f"/desktop/v1/projects/{second.project_id}/activate",
        "operation_kind": "project_activate",
        "project_id": second.project_id,
        "key": f"remote-patch-authority-second-activate-{start_operation}",
        "body": {},
        "if_match": second.etag,
    }
    reservation = store.begin_project_runtime_action(**action)
    if start_operation:
        store.start_project_runtime_action(reservation=reservation, **action)

    with pytest.raises(ResourceInUseError) as raised:
        store.patch_project(
            active.project_id,
            {"name": "Must remain active"},
            if_match=active.etag,
        )

    assert raised.value.resource_id == active.project_id
    assert store.get_project(active.project_id) == active
    assert store.get_local_operation(reservation.operation.operation_id).state == (
        "running" if start_operation else "queued"
    )


def test_running_activation_and_active_project_patch_are_serialized(
    tmp_path: Path,
) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    first = store.create_project(
        _project(profile.profile_id, name="First"),
        idempotency_key="remote-patch-race-first",
    )
    second = store.create_project(
        _project(profile.profile_id, name="Second"),
        idempotency_key="remote-patch-race-second",
    )
    _activate_project(store, first, key="remote-patch-race-first-activate")
    active = store.get_project(first.project_id)
    action = {
        "route": f"/desktop/v1/projects/{second.project_id}/activate",
        "operation_kind": "project_activate",
        "project_id": second.project_id,
        "key": "remote-patch-race-second-activate",
        "body": {},
        "if_match": second.etag,
    }
    reservation = store.begin_project_runtime_action(**action)
    activation_running = threading.Event()
    patch_finished = threading.Event()

    def activate() -> LocalOperationV1:
        store.start_project_runtime_action(reservation=reservation, **action)
        activation_running.set()
        assert patch_finished.wait(timeout=5)
        return store.complete_project_runtime_action(
            reservation=reservation,
            remote_state=_remote_project_state(second.project_id, "second-revision"),
            **action,
        )

    def patch() -> str:
        assert activation_running.wait(timeout=5)
        try:
            store.patch_project(
                active.project_id,
                {"name": "Must not race activation"},
                if_match=active.etag,
            )
        except ResourceInUseError:
            return "busy"
        finally:
            patch_finished.set()
        return "patched"

    with ThreadPoolExecutor(max_workers=2) as executor:
        activation_future = executor.submit(activate)
        patch_future = executor.submit(patch)
        assert patch_future.result(timeout=5) == "busy"
        finished = activation_future.result(timeout=5)

    assert finished.state == "succeeded"
    assert store.get_project(first.project_id).state == "draft"
    assert store.get_project(first.project_id).name == active.name
    assert store.get_project(first.project_id).remote == active.remote
    assert store.get_project(second.project_id).state == "active"


def test_active_project_patch_fault_rolls_back_intent_and_remote_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    project = store.create_project(
        _project(profile.profile_id), idempotency_key="remote-patch-fault-create"
    )
    _activate_project(store, project, key="remote-patch-fault-activate")
    active = store.get_project(project.project_id)

    def fail_readback(_row: sqlite3.Row) -> ProjectV1:
        raise RuntimeError("injected patch readback fault")

    with monkeypatch.context() as fault:
        fault.setattr(store, "_project_from_row", fail_readback)
        with pytest.raises(RuntimeError, match="patch readback fault"):
            store.patch_project(
                active.project_id,
                {"name": "Must roll back"},
                if_match=active.etag,
            )

    assert store.get_project(project.project_id) == active


def test_blocked_project_intent_patch_demotes_to_draft_and_clears_remote(tmp_path: Path) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    project = store.create_project(
        _project(profile.profile_id), idempotency_key="remote-blocked-patch-create"
    )
    _activate_project(store, project, key="remote-blocked-patch-activate")
    active = store.get_project(project.project_id)
    with store._transaction(write=True) as connection:
        blocked = ProviderMutation(store, connection).set_project_state(
            active.project_id,
            if_match=active.etag,
            state="blocked",
        )

    patched = store.patch_project(
        blocked.project_id, {"name": "Blocked intent changed"}, if_match=blocked.etag
    )

    assert patched.state == "draft"
    assert patched.remote is None


def test_project_demote_and_archive_preserve_remote_as_observed_history(tmp_path: Path) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    project = store.create_project(
        _project(profile.profile_id), idempotency_key="remote-demote-project-create"
    )
    _, remote = _activate_project(store, project, key="remote-demote-project-activate")
    active = store.get_project(project.project_id)
    with store._transaction(write=True) as connection:
        draft = ProviderMutation(store, connection).set_project_state(
            active.project_id,
            if_match=active.etag,
            state="draft",
        )
    with store._transaction(write=True) as connection:
        archived = ProviderMutation(store, connection).set_project_state(
            draft.project_id,
            if_match=draft.etag,
            state="archived",
        )

    assert draft.remote == remote
    assert archived.remote == remote
    assert (
        store._connection.execute(
            "SELECT current_revision_id FROM projects WHERE project_id = ?",
            (project.project_id,),
        ).fetchone()[0]
        is None
    )


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


def test_lower_idempotency_capacity_reopen_rejects_retries_and_recovers(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    clock = MutableClock()
    record_count = provider_store_module.NORMAL_WRITE_CLEANUP_ROWS + 2
    store = DesktopProviderStore(
        root,
        clock=clock,
        idempotency_retention_seconds=2,
        max_idempotency_records=record_count,
    )
    first_profile: RemoteProfileV1 | None = None
    for index in range(record_count - 1):
        created = _create_profile(
            store,
            name=f"Capacity profile {index}",
            key=f"lower-capacity-profile-{index:03d}",
        )
        if first_profile is None:
            first_profile = created
    assert first_profile is not None
    clock.now += timedelta(seconds=1)
    exact_key = f"lower-capacity-profile-{record_count - 1:03d}"
    exact_name = f"Capacity profile {record_count - 1}"
    original_exact = _create_profile(store, name=exact_name, key=exact_key)
    with store._transaction(write=True) as connection:
        ProviderMutation(store, connection, if_match=first_profile.etag).set_profile_runtime_state(
            first_profile.profile_id,
            if_match=first_profile.etag,
            connection_state="connected",
            credential_slots=first_profile.credential_slots,
            host_key_fingerprint=first_profile.host_key_fingerprint,
        )
    clock.now += timedelta(seconds=3)
    store.close()

    for _ in range(2):
        with pytest.raises(
            provider_store_module.ProviderCapacityConfigurationError,
            match="idempotency record capacity is lower than persisted usage",
        ) as raised:
            DesktopProviderStore(
                root,
                clock=clock,
                idempotency_retention_seconds=2,
                max_idempotency_records=1,
            )
        assert raised.value.record_type == "idempotency"
        assert raised.value.configured_limit == 1
        assert raised.value.persisted_count == record_count

    with sqlite3.connect(root / "provider.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM idempotency_records").fetchone() == (
            record_count,
        )
        assert connection.execute(
            "SELECT connection_state FROM remote_profiles WHERE profile_id = ?",
            (first_profile.profile_id,),
        ).fetchone() == ("connected",)

    recovered = DesktopProviderStore(
        root,
        clock=clock,
        idempotency_retention_seconds=2,
        max_idempotency_records=record_count,
    )
    assert recovered.get_profile(first_profile.profile_id).connection_state == "disconnected"
    recreated_exact = _create_profile(recovered, name=exact_name, key=exact_key)
    assert recreated_exact.profile_id != original_exact.profile_id
    assert recovered._provider_record_counts(recovered._connection)[0] == 2
    assert tuple(
        recovered._connection.execute("SELECT count(*) FROM idempotency_records").fetchone()
    ) == (2,)


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
            remote_state=_remote_project_state(project.project_id),
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
    reservation = store.begin_profile_runtime_action(
        route=f"/desktop/v1/profiles/{profile.profile_id}/disconnect",
        operation_kind="profile_disconnect",
        profile_id=profile.profile_id,
        key=f"profile-delete-guard-{state}",
        body={},
        if_match=profile.etag,
        displace_existing=False,
    )
    if state != "running":
        operation = LocalOperationV1.model_validate(
            {
                **reservation.operation.model_dump(mode="python"),
                "state": state,
                "started_at": None if state == "queued" else reservation.operation.started_at,
            }
        )
        operation_bytes = provider_store_module._canonical_json_bytes(
            operation.model_dump(mode="json")
        )
        with store._transaction(write=True) as connection:
            connection.execute(
                """
                UPDATE local_operations
                SET state = ?, document_json = ?
                WHERE operation_id = ?
                """,
                (state, operation_bytes, operation.operation_id),
            )
            connection.execute(
                """
                UPDATE idempotency_records
                SET response_bytes = ?
                WHERE operation_id = ?
                """,
                (operation_bytes, operation.operation_id),
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


def test_live_profile_operation_get_and_replay_are_read_only_and_completion_wins(
    tmp_path: Path,
) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    action = {
        "route": f"/desktop/v1/profiles/{profile.profile_id}/connect",
        "operation_kind": "profile_connect",
        "profile_id": profile.profile_id,
        "key": "profile-read-only-observation",
        "body": {},
        "if_match": profile.etag,
        "displace_existing": True,
    }
    reservation = store.begin_profile_runtime_action(**action)
    initial = reservation.operation

    observed = store.get_local_operation(initial.operation_id)
    replay = store.begin_profile_runtime_action(**action)

    assert observed == initial
    assert replay.replayed is True
    assert replay.operation == initial
    assert store.get_profile(profile.profile_id).connection_state == "connecting"
    finished = store.complete_profile_runtime_action(
        reservation=reservation,
        route=cast(str, action["route"]),
        profile_id=profile.profile_id,
        key=cast(str, action["key"]),
        body={},
        if_match=profile.etag,
        connection_state="connected",
        host_key_fingerprint="SHA256:read-only-observation",
    )
    assert finished.state == "succeeded"
    assert finished.result == ConnectionOperationResultV1(
        profile_id=profile.profile_id,
        connection_state="connected",
    )


def test_generic_live_operation_replay_does_not_reconcile_or_cancel(tmp_path: Path) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)

    def begin(transaction: ProviderMutation):
        transaction.set_profile_runtime_state(
            profile.profile_id,
            if_match=profile.etag,
            connection_state="connecting",
            credential_slots=(),
            host_key_fingerprint=None,
        )
        return 202, transaction.create_local_operation(
            operation_kind="profile_connect",
            resource=ResourceRefV1(resource_type="profile", resource_id=profile.profile_id),
            state="running",
        )

    action = {
        "route": f"/desktop/v1/profiles/{profile.profile_id}/connect",
        "resource_scope": profile.profile_id,
        "key": "profile-generic-read-only-replay",
        "body": {},
        "if_match": profile.etag,
        "semantic_headers": {},
        "response_model": LocalOperationV1,
        "mutation": begin,
    }
    first = store.execute_idempotent_action(**action)
    replay = store.execute_idempotent_action(
        **{
            **action,
            "mutation": lambda transaction: pytest.fail(f"unexpected mutation: {transaction}"),
        }
    )

    operation = LocalOperationV1.model_validate_json(replay.response_bytes)
    assert replay.replayed is True
    assert replay.response_bytes == first.response_bytes
    assert operation.state == "running"
    assert operation.result is None
    assert store.get_local_operation(operation.operation_id) == operation


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
            remote_state=_remote_project_state(project.project_id),
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
        remote_state=_remote_project_state(project.project_id),
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
    assert active.remote == _remote_project_state(project.project_id)
    with store._transaction(write=False) as connection:
        document = provider_store_module._decode_json_object(
            bytes(
                connection.execute(
                    "SELECT document_json FROM projects WHERE project_id = ?",
                    (project.project_id,),
                ).fetchone()[0]
            ),
            label="project",
        )
        assert "remote" not in document
        assert "current_revision_id" not in document
        assert (
            connection.execute(
                "SELECT current_revision_id FROM projects WHERE project_id = ?",
                (project.project_id,),
            ).fetchone()[0]
            == "core-revision-0001"
        )
    assert store.begin_project_runtime_action(**action).operation == finished


def test_project_runtime_action_admission_guard_is_atomic_and_skipped_for_replay(
    tmp_path: Path,
) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    project = store.create_project(
        _project(profile.profile_id), idempotency_key="project-runtime-guard-create-01"
    )
    action = {
        "route": f"/desktop/v1/projects/{project.project_id}/activate",
        "operation_kind": "project_activate",
        "project_id": project.project_id,
        "key": "project-runtime-guard-activate-01",
        "body": {},
        "if_match": project.etag,
    }

    def reject(_project: ProjectV1) -> None:
        raise RuntimeError("release capability rejected the project")

    with pytest.raises(RuntimeError, match="release capability"):
        store.begin_project_runtime_action(**action, admission_guard=reject)
    assert store.pending_operation_ids() == ()

    reservation = store.begin_project_runtime_action(**action)
    replay = store.begin_project_runtime_action(**action, admission_guard=reject)
    assert reservation.replayed is False
    assert replay.replayed is True
    assert replay.operation == reservation.operation


@pytest.mark.parametrize("remote_kind", ["missing", "not_ready", "wrong_revision_project"])
def test_project_activation_requires_a_matching_ready_remote_projection(
    tmp_path: Path, remote_kind: str
) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    project = store.create_project(
        _project(profile.profile_id), idempotency_key=f"remote-invalid-{remote_kind}-create"
    )
    action = {
        "route": f"/desktop/v1/projects/{project.project_id}/activate",
        "operation_kind": "project_activate",
        "project_id": project.project_id,
        "key": f"remote-invalid-{remote_kind}-activate",
        "body": {},
        "if_match": project.etag,
    }
    reservation = store.begin_project_runtime_action(**action)
    store.start_project_runtime_action(reservation=reservation, **action)
    remote = None
    if remote_kind == "not_ready":
        remote = _remote_project_state(project.project_id, status="draft")
    elif remote_kind == "wrong_revision_project":
        valid_remote = _remote_project_state(project.project_id)
        assert valid_remote.active_revision is not None
        remote = valid_remote.model_copy(
            update={
                "active_revision": valid_remote.active_revision.model_copy(
                    update={"project_id": "different-project"}
                )
            }
        )

    with pytest.raises(ContractValidationError, match="activation"):
        store.complete_project_runtime_action(
            reservation=reservation,
            remote_state=remote,
            **action,
        )

    assert store.get_project(project.project_id) == project
    assert store.get_local_operation(reservation.operation.operation_id).state == "running"


def test_project_activation_rejects_remote_projection_over_its_persistence_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    project = store.create_project(
        _project(profile.profile_id), idempotency_key="remote-write-bound-create"
    )
    action = {
        "route": f"/desktop/v1/projects/{project.project_id}/activate",
        "operation_kind": "project_activate",
        "project_id": project.project_id,
        "key": "remote-write-bound-activate",
        "body": {},
        "if_match": project.etag,
    }
    reservation = store.begin_project_runtime_action(**action)
    store.start_project_runtime_action(reservation=reservation, **action)
    remote_state = _remote_project_state(project.project_id)
    remote_bytes = provider_store_module._canonical_json_bytes(
        remote_state.model_dump(mode="json")
    )
    monkeypatch.setattr(
        provider_store_module,
        "MAX_REMOTE_PROJECT_STATE_BYTES",
        len(remote_bytes) - 1,
    )

    with pytest.raises(ContractValidationError, match="remote project state exceeds"):
        store.complete_project_runtime_action(
            reservation=reservation,
            remote_state=remote_state,
            **action,
        )

    assert store.get_project(project.project_id) == project
    assert store.get_local_operation(reservation.operation.operation_id).state == "running"


def test_project_activation_remote_publication_rolls_back_as_one_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    first = store.create_project(
        _project(profile.profile_id, name="First"),
        idempotency_key="remote-atomic-first-create",
    )
    second = store.create_project(
        _project(profile.profile_id, name="Second"),
        idempotency_key="remote-atomic-second-create",
    )
    _, first_remote = _activate_project(
        store, first, key="remote-atomic-first-activate", revision_id="first-revision"
    )
    first_active = store.get_project(first.project_id)
    action = {
        "route": f"/desktop/v1/projects/{second.project_id}/activate",
        "operation_kind": "project_activate",
        "project_id": second.project_id,
        "key": "remote-atomic-second-activate",
        "body": {},
        "if_match": second.etag,
    }
    reservation = store.begin_project_runtime_action(**action)
    running = store.start_project_runtime_action(reservation=reservation, **action)
    usage_before = tuple(
        store._connection.execute("SELECT * FROM provider_storage_usage").fetchone()
    )

    def fail_terminal_write(*_args: object, **_kwargs: object) -> LocalOperationV1:
        raise RuntimeError("injected terminal write fault")

    monkeypatch.setattr(store, "_finish_reserved_project_action", fail_terminal_write)
    with pytest.raises(RuntimeError, match="terminal write fault"):
        store.complete_project_runtime_action(
            reservation=reservation,
            remote_state=_remote_project_state(second.project_id, "second-revision"),
            **action,
        )

    assert store.get_project(first.project_id) == first_active
    assert store.get_project(first.project_id).remote == first_remote
    assert store.get_project(second.project_id) == second
    assert store.get_local_operation(running.operation_id) == running
    assert (
        tuple(store._connection.execute("SELECT * FROM provider_storage_usage").fetchone())
        == usage_before
    )


def test_non_activation_project_completion_rejects_remote_projection(tmp_path: Path) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    project = store.create_project(
        _project(profile.profile_id), idempotency_key="remote-nonactivation-create"
    )
    action = {
        "route": f"/desktop/v1/projects/{project.project_id}/workspace-sync",
        "operation_kind": "workspace_sync",
        "project_id": project.project_id,
        "key": "remote-nonactivation-sync",
        "body": {},
        "if_match": project.etag,
    }
    reservation = store.begin_project_runtime_action(**action)
    store.start_project_runtime_action(reservation=reservation, **action)

    with pytest.raises(ContractValidationError, match="non-activation"):
        store.complete_project_runtime_action(
            reservation=reservation,
            remote_state=_remote_project_state(project.project_id),
            **action,
        )


def test_pending_operation_ids_are_stable_filtered_and_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    reservations = []
    actions = []
    for index in range(4):
        project = store.create_project(
            _project(profile.profile_id, name=f"Pending {index}"),
            idempotency_key=f"pending-project-create-{index:02d}",
        )
        action = {
            "route": f"/desktop/v1/projects/{project.project_id}/workspace-sync",
            "operation_kind": "workspace_sync",
            "project_id": project.project_id,
            "key": f"pending-project-action-{index:02d}",
            "body": {},
            "if_match": project.etag,
        }
        actions.append(action)
        reservations.append(store.begin_project_runtime_action(**action))

    running = store.start_project_runtime_action(reservation=reservations[1], **actions[1])
    cancelling = LocalOperationV1.model_validate(
        {
            **reservations[2].operation.model_dump(mode="python"),
            "state": "cancelling",
        }
    )
    cancelling_bytes = provider_store_module._canonical_json_bytes(
        cancelling.model_dump(mode="json")
    )
    with store._transaction(write=True) as connection:
        connection.execute(
            "UPDATE local_operations SET state = 'cancelling', document_json = ? "
            "WHERE operation_id = ?",
            (cancelling_bytes, cancelling.operation_id),
        )
        connection.execute(
            "UPDATE idempotency_records SET response_bytes = ? WHERE operation_id = ?",
            (cancelling_bytes, cancelling.operation_id),
        )
    store.start_project_runtime_action(reservation=reservations[3], **actions[3])
    store.complete_project_runtime_action(
        reservation=reservations[3], remote_state=None, **actions[3]
    )

    expected = tuple(
        sorted(
            (
                reservations[0].operation.operation_id,
                running.operation_id,
                cancelling.operation_id,
            )
        )
    )
    assert store.pending_operation_ids() == expected

    monkeypatch.setattr(provider_store_module, "MAX_RECOVERY_ROWS", 2)
    with pytest.raises(ProviderDataCorruptionError, match="pending operation"):
        store.pending_operation_ids()


@pytest.mark.parametrize(
    ("operation_kind", "route_suffix"),
    [
        ("project_doctor", "doctor"),
        ("project_repair", "repair"),
        ("bootstrap", "bootstrap"),
        ("workspace_sync", "workspace-sync"),
    ],
)
@pytest.mark.parametrize("start_operation", [False, True], ids=["queued", "running"])
def test_cross_project_activation_rejects_busy_active_project(
    tmp_path: Path,
    operation_kind: str,
    route_suffix: str,
    start_operation: bool,
) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    first = store.create_project(
        _project(profile.profile_id, name="First"),
        idempotency_key="activation-busy-first-create",
    )
    second = store.create_project(
        _project(profile.profile_id, name="Second"),
        idempotency_key="activation-busy-second-create",
    )
    with store._transaction(write=True) as connection:
        first = ProviderMutation(store, connection).set_project_state(
            first.project_id,
            if_match=first.etag,
            state="active",
            remote_state=_remote_project_state(first.project_id, "first-revision"),
        )
    first_action = {
        "route": f"/desktop/v1/projects/{first.project_id}/{route_suffix}",
        "operation_kind": operation_kind,
        "project_id": first.project_id,
        "key": f"activation-busy-{operation_kind}-{start_operation}",
        "body": {},
        "if_match": first.etag,
    }
    first_reservation = store.begin_project_runtime_action(**first_action)
    if start_operation:
        store.start_project_runtime_action(reservation=first_reservation, **first_action)
    second_action = {
        "route": f"/desktop/v1/projects/{second.project_id}/activate",
        "operation_kind": "project_activate",
        "project_id": second.project_id,
        "key": f"activation-busy-second-{operation_kind}-{start_operation}",
        "body": {},
        "if_match": second.etag,
    }

    with pytest.raises(ResourceInUseError) as raised:
        store.begin_project_runtime_action(**second_action)

    assert raised.value.resource_id == first.project_id
    assert store.get_project(first.project_id) == first
    assert store.get_project(second.project_id) == second


@pytest.mark.parametrize(
    ("operation_kind", "route_suffix"),
    [
        ("project_doctor", "doctor"),
        ("project_repair", "repair"),
        ("bootstrap", "bootstrap"),
        ("workspace_sync", "workspace-sync"),
    ],
)
def test_activation_reservation_excludes_new_work_on_active_project(
    tmp_path: Path,
    operation_kind: str,
    route_suffix: str,
) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    first = store.create_project(
        _project(profile.profile_id, name="First"),
        idempotency_key="activation-reverse-first-create",
    )
    second = store.create_project(
        _project(profile.profile_id, name="Second"),
        idempotency_key="activation-reverse-second-create",
    )
    with store._transaction(write=True) as connection:
        first = ProviderMutation(store, connection).set_project_state(
            first.project_id,
            if_match=first.etag,
            state="active",
            remote_state=_remote_project_state(first.project_id, "first-revision"),
        )
    activation = {
        "route": f"/desktop/v1/projects/{second.project_id}/activate",
        "operation_kind": "project_activate",
        "project_id": second.project_id,
        "key": f"activation-reverse-{operation_kind}",
        "body": {},
        "if_match": second.etag,
    }
    reservation = store.begin_project_runtime_action(**activation)
    active_action = {
        "route": f"/desktop/v1/projects/{first.project_id}/{route_suffix}",
        "operation_kind": operation_kind,
        "project_id": first.project_id,
        "key": f"activation-reverse-active-{operation_kind}",
        "body": {},
        "if_match": first.etag,
    }

    with pytest.raises(ResourceInUseError) as raised:
        store.begin_project_runtime_action(**active_action)

    assert raised.value.resource_id == first.project_id
    store.start_project_runtime_action(reservation=reservation, **activation)
    store.complete_project_runtime_action(
        reservation=reservation,
        remote_state=_remote_project_state(second.project_id, "second-revision"),
        **activation,
    )
    assert store.get_project(first.project_id).state == "draft"
    assert store.get_project(second.project_id).state == "active"


def test_activation_completion_rechecks_late_cross_project_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    first = store.create_project(
        _project(profile.profile_id, name="First"),
        idempotency_key="activation-late-first-create",
    )
    second = store.create_project(
        _project(profile.profile_id, name="Second"),
        idempotency_key="activation-late-second-create",
    )
    with store._transaction(write=True) as connection:
        first = ProviderMutation(store, connection).set_project_state(
            first.project_id,
            if_match=first.etag,
            state="active",
            remote_state=_remote_project_state(first.project_id, "first-revision"),
        )
    activation = {
        "route": f"/desktop/v1/projects/{second.project_id}/activate",
        "operation_kind": "project_activate",
        "project_id": second.project_id,
        "key": "activation-late-second-action",
        "body": {},
        "if_match": second.etag,
    }
    reservation = store.begin_project_runtime_action(**activation)
    store.start_project_runtime_action(reservation=reservation, **activation)
    active_action = {
        "route": f"/desktop/v1/projects/{first.project_id}/doctor",
        "operation_kind": "project_doctor",
        "project_id": first.project_id,
        "key": "activation-late-first-doctor",
        "body": {},
        "if_match": first.etag,
    }
    with monkeypatch.context() as bypass:
        bypass.setattr(
            DesktopProviderStore,
            "_require_project_operation_reservation_available",
            classmethod(lambda _cls, *_args, **_kwargs: None),
        )
        store.begin_project_runtime_action(**active_action)

    with pytest.raises(ResourceInUseError) as raised:
        store.complete_project_runtime_action(
            reservation=reservation,
            remote_state=_remote_project_state(second.project_id, "second-revision"),
            **activation,
        )

    assert raised.value.resource_id == first.project_id
    assert store.get_local_operation(reservation.operation.operation_id).state == "running"
    assert store.get_project(first.project_id) == first
    assert store.get_project(second.project_id) == second


def test_project_activation_and_active_work_reservations_are_serialized(
    tmp_path: Path,
) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    first = store.create_project(
        _project(profile.profile_id, name="First"),
        idempotency_key="activation-race-first-create",
    )
    second = store.create_project(
        _project(profile.profile_id, name="Second"),
        idempotency_key="activation-race-second-create",
    )
    with store._transaction(write=True) as connection:
        first = ProviderMutation(store, connection).set_project_state(
            first.project_id,
            if_match=first.etag,
            state="active",
            remote_state=_remote_project_state(first.project_id, "first-revision"),
        )
    actions = (
        {
            "route": f"/desktop/v1/projects/{first.project_id}/workspace-sync",
            "operation_kind": "workspace_sync",
            "project_id": first.project_id,
            "key": "activation-race-first-sync",
            "body": {},
            "if_match": first.etag,
        },
        {
            "route": f"/desktop/v1/projects/{second.project_id}/activate",
            "operation_kind": "project_activate",
            "project_id": second.project_id,
            "key": "activation-race-second-activate",
            "body": {},
            "if_match": second.etag,
        },
    )
    barrier = threading.Barrier(2)

    def reserve(action: dict[str, object]) -> str:
        barrier.wait(timeout=5)
        try:
            store.begin_project_runtime_action(**action)
        except ResourceInUseError:
            return "busy"
        return "reserved"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(reserve, actions))

    assert sorted(outcomes) == ["busy", "reserved"]
    assert (
        store._connection.execute(
            "SELECT count(*) FROM local_operations "
            "WHERE state IN ('queued', 'running', 'cancelling')"
        ).fetchone()[0]
        == 1
    )
    assert store.get_project(first.project_id) == first
    assert store.get_project(second.project_id) == second


def test_restart_cancels_activation_exclusion_before_accepting_active_project_work(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    profile = _create_profile(store)
    first = store.create_project(
        _project(profile.profile_id, name="First"),
        idempotency_key="activation-restart-first-create",
    )
    second = store.create_project(
        _project(profile.profile_id, name="Second"),
        idempotency_key="activation-restart-second-create",
    )
    with store._transaction(write=True) as connection:
        first = ProviderMutation(store, connection).set_project_state(
            first.project_id,
            if_match=first.etag,
            state="active",
            remote_state=_remote_project_state(first.project_id, "first-revision"),
        )
    activation = {
        "route": f"/desktop/v1/projects/{second.project_id}/activate",
        "operation_kind": "project_activate",
        "project_id": second.project_id,
        "key": "activation-restart-second-action",
        "body": {},
        "if_match": second.etag,
    }
    reservation = store.begin_project_runtime_action(**activation)
    store.close()

    reopened = DesktopProviderStore(root)
    replay = reopened.begin_project_runtime_action(**activation)
    recovered_first = reopened.get_project(first.project_id)
    assert replay.operation.operation_id == reservation.operation.operation_id
    assert replay.operation.state == "cancelled"
    assert recovered_first.state == "draft"
    assert recovered_first.etag != first.etag

    doctor = reopened.begin_project_runtime_action(
        route=f"/desktop/v1/projects/{first.project_id}/doctor",
        operation_kind="project_doctor",
        project_id=first.project_id,
        key="activation-restart-first-doctor",
        body={},
        if_match=recovered_first.etag,
    )
    assert doctor.operation.state == "queued"


def test_activation_recovery_fault_rolls_back_authority_and_active_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    profile = _create_profile(store)
    first = store.create_project(
        _project(profile.profile_id, name="First"),
        idempotency_key="activation-recovery-fault-first",
    )
    second = store.create_project(
        _project(profile.profile_id, name="Second"),
        idempotency_key="activation-recovery-fault-second",
    )
    _activate_project(store, first, key="activation-recovery-fault-first-activate")
    active = store.get_project(first.project_id)
    assert active.remote is not None
    assert active.remote.active_revision is not None
    active_version = store._connection.execute(
        "SELECT resource_version FROM projects WHERE project_id = ?",
        (first.project_id,),
    ).fetchone()[0]
    action = {
        "route": f"/desktop/v1/projects/{second.project_id}/activate",
        "operation_kind": "project_activate",
        "project_id": second.project_id,
        "key": "activation-recovery-fault-second-activate",
        "body": {},
        "if_match": second.etag,
    }
    reservation = store.begin_project_runtime_action(**action)
    store.close()

    def fail_reconciliation(_store: DesktopProviderStore, _connection: sqlite3.Connection) -> None:
        raise RuntimeError("injected activation recovery fault")

    with monkeypatch.context() as fault:
        fault.setattr(
            DesktopProviderStore,
            "_reconcile_operations_at_startup",
            fail_reconciliation,
        )
        with pytest.raises(RuntimeError, match="activation recovery fault"):
            DesktopProviderStore(root)

    with sqlite3.connect(root / "provider.sqlite3") as connection:
        project_row = connection.execute(
            "SELECT state, current_revision_id, remote_state_json, resource_version "
            "FROM projects WHERE project_id = ?",
            (first.project_id,),
        ).fetchone()
        operation_state = connection.execute(
            "SELECT state FROM local_operations WHERE operation_id = ?",
            (reservation.operation.operation_id,),
        ).fetchone()[0]
    assert project_row is not None
    assert project_row[0] == "active"
    assert project_row[1] == active.remote.active_revision.id
    assert project_row[2] is not None
    assert project_row[3] == active_version
    assert operation_state == "queued"

    reopened = DesktopProviderStore(root)
    recovered = reopened.get_project(first.project_id)
    replay = reopened.begin_project_runtime_action(**action)
    assert recovered.state == "draft"
    assert recovered.remote == active.remote
    assert replay.operation.state == "cancelled"

    patched = reopened.patch_project(
        recovered.project_id,
        {"name": "Recovered intent"},
        if_match=recovered.etag,
    )
    assert patched.state == "draft"
    assert patched.remote is None


def test_expired_live_project_action_survives_cleanup_and_can_finish(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    store = DesktopProviderStore(
        tmp_path / "state",
        clock=clock,
        idempotency_retention_seconds=1,
    )
    profile = _create_profile(store)
    project = store.create_project(
        _project(profile.profile_id), idempotency_key="project-live-expiry-create"
    )
    action = {
        "route": f"/desktop/v1/projects/{project.project_id}/workspace-sync",
        "operation_kind": "workspace_sync",
        "project_id": project.project_id,
        "key": "project-live-expiry-action",
        "body": {},
        "if_match": project.etag,
    }
    reservation = store.begin_project_runtime_action(**action)
    clock.now += timedelta(seconds=2)

    _create_profile(store, name="Cleanup trigger", key="profile-cleanup-trigger")

    persisted = store._connection.execute(
        "SELECT response_bytes FROM idempotency_records WHERE idempotency_key = ?",
        (action["key"],),
    ).fetchone()
    assert persisted is not None
    running = store.start_project_runtime_action(reservation=reservation, **action)
    finished = store.complete_project_runtime_action(
        reservation=reservation,
        remote_state=None,
        **action,
    )
    assert running.state == "running"
    assert finished.state == "succeeded"
    _create_profile(store, name="Terminal cleanup trigger", key="profile-terminal-cleanup")
    assert (
        store._connection.execute(
            "SELECT 1 FROM idempotency_records WHERE idempotency_key = ?",
            (action["key"],),
        ).fetchone()
        is None
    )


def test_project_action_replay_rejects_cross_project_operation_substitution(
    tmp_path: Path,
) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    first = store.create_project(
        _project(profile.profile_id, name="First"),
        idempotency_key="project-replay-first-create",
    )
    second = store.create_project(
        _project(profile.profile_id, name="Second"),
        idempotency_key="project-replay-second-create",
    )
    first_action = {
        "route": f"/desktop/v1/projects/{first.project_id}/workspace-sync",
        "operation_kind": "workspace_sync",
        "project_id": first.project_id,
        "key": "project-replay-first-action",
        "body": {},
        "if_match": first.etag,
    }
    second_action = {
        "route": f"/desktop/v1/projects/{second.project_id}/workspace-sync",
        "operation_kind": "workspace_sync",
        "project_id": second.project_id,
        "key": "project-replay-second-action",
        "body": {},
        "if_match": second.etag,
    }
    store.begin_project_runtime_action(**first_action)
    second_reservation = store.begin_project_runtime_action(**second_action)
    second_bytes = provider_store_module._canonical_json_bytes(
        second_reservation.operation.model_dump(mode="json")
    )
    store._connection.execute(
        "UPDATE idempotency_records SET response_bytes = ? WHERE idempotency_key = ?",
        (second_bytes, first_action["key"]),
    )
    snapshot = _reseal_provider_usage(store._connection, store.state_root)
    store._connection.commit()
    store._provider_usage_snapshot = snapshot

    with pytest.raises(ProviderDataCorruptionError, match="operation"):
        store.begin_project_runtime_action(**first_action)


@pytest.mark.parametrize("recovery", [False, True], ids=["replay", "startup"])
def test_project_action_rejects_same_scope_operation_substitution(
    tmp_path: Path,
    recovery: bool,
) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    profile = _create_profile(store)
    project = store.create_project(
        _project(profile.profile_id), idempotency_key="project-same-scope-create"
    )
    first_action = {
        "route": f"/desktop/v1/projects/{project.project_id}/workspace-sync",
        "operation_kind": "workspace_sync",
        "project_id": project.project_id,
        "key": "project-same-scope-first-action",
        "body": {},
        "if_match": project.etag,
    }
    first_reservation = store.begin_project_runtime_action(**first_action)
    store.start_project_runtime_action(reservation=first_reservation, **first_action)
    store.complete_project_runtime_action(
        reservation=first_reservation,
        remote_state=None,
        **first_action,
    )
    second_action = {
        **first_action,
        "key": "project-same-scope-second-action",
    }
    second_reservation = store.begin_project_runtime_action(**second_action)
    second_bytes = provider_store_module._canonical_json_bytes(
        second_reservation.operation.model_dump(mode="json")
    )
    if recovery:
        store.close()
        with sqlite3.connect(root / "provider.sqlite3") as connection:
            connection.execute(
                "UPDATE idempotency_records SET response_bytes = ? WHERE idempotency_key = ?",
                (second_bytes, first_action["key"]),
            )
            _reseal_provider_usage(connection, root)
        with pytest.raises(ProviderDataCorruptionError, match="operation"):
            DesktopProviderStore(root)
    else:
        store._connection.execute(
            "UPDATE idempotency_records SET response_bytes = ? WHERE idempotency_key = ?",
            (second_bytes, first_action["key"]),
        )
        snapshot = _reseal_provider_usage(store._connection, store.state_root)
        store._connection.commit()
        store._provider_usage_snapshot = snapshot
        with pytest.raises(ProviderDataCorruptionError, match="operation"):
            store.begin_project_runtime_action(**first_action)


def test_startup_rejects_cross_project_operation_replay_substitution(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    profile = _create_profile(store)
    first = store.create_project(
        _project(profile.profile_id, name="First"),
        idempotency_key="project-startup-first-create",
    )
    second = store.create_project(
        _project(profile.profile_id, name="Second"),
        idempotency_key="project-startup-second-create",
    )
    first_action = {
        "route": f"/desktop/v1/projects/{first.project_id}/workspace-sync",
        "operation_kind": "workspace_sync",
        "project_id": first.project_id,
        "key": "project-startup-first-action",
        "body": {},
        "if_match": first.etag,
    }
    second_action = {
        "route": f"/desktop/v1/projects/{second.project_id}/workspace-sync",
        "operation_kind": "workspace_sync",
        "project_id": second.project_id,
        "key": "project-startup-second-action",
        "body": {},
        "if_match": second.etag,
    }
    store.begin_project_runtime_action(**first_action)
    second_reservation = store.begin_project_runtime_action(**second_action)
    second_bytes = provider_store_module._canonical_json_bytes(
        second_reservation.operation.model_dump(mode="json")
    )
    store.close()
    with sqlite3.connect(root / "provider.sqlite3") as connection:
        connection.execute(
            "UPDATE idempotency_records SET response_bytes = ? WHERE idempotency_key = ?",
            (second_bytes, first_action["key"]),
        )
        _reseal_provider_usage(connection, root)

    with pytest.raises(ProviderDataCorruptionError, match="operation"):
        DesktopProviderStore(root)


def test_startup_rejects_rebinding_operation_id_to_another_action_authority(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    profile = _create_profile(store)
    project = store.create_project(
        _project(profile.profile_id), idempotency_key="project-authority-create"
    )
    first_action = {
        "route": f"/desktop/v1/projects/{project.project_id}/workspace-sync",
        "operation_kind": "workspace_sync",
        "project_id": project.project_id,
        "key": "project-authority-first-action",
        "body": {},
        "if_match": project.etag,
    }
    first = store.begin_project_runtime_action(**first_action)
    store.start_project_runtime_action(reservation=first, **first_action)
    store.complete_project_runtime_action(
        reservation=first,
        remote_state=None,
        **first_action,
    )
    second_action = {**first_action, "key": "project-authority-second-action"}
    second = store.begin_project_runtime_action(**second_action)
    second_bytes = provider_store_module._canonical_json_bytes(
        second.operation.model_dump(mode="json")
    )
    store.close()
    with sqlite3.connect(root / "provider.sqlite3") as connection:
        connection.execute(
            "DELETE FROM idempotency_records WHERE idempotency_key = ?",
            (second_action["key"],),
        )
        connection.execute(
            """
            UPDATE idempotency_records
            SET operation_id = ?, response_bytes = ?
            WHERE idempotency_key = ?
            """,
            (second.operation.operation_id, second_bytes, first_action["key"]),
        )
        _reseal_provider_usage(connection, root)

    with pytest.raises(ProviderDataCorruptionError, match="action authority"):
        DesktopProviderStore(root)


@pytest.mark.parametrize("state", ["running", "succeeded"])
def test_mutation_cannot_commit_an_operation_without_action_authority(
    tmp_path: Path,
    state: Literal["running", "succeeded"],
) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    project = store.create_project(
        _project(profile.profile_id), idempotency_key="project-orphan-mutation-create"
    )

    def create_orphan_then_return_project(
        transaction: ProviderMutation,
    ) -> tuple[int, ProjectV1]:
        transaction.create_local_operation(
            operation_kind="workspace_sync",
            resource=ResourceRefV1(resource_type="project", resource_id=project.project_id),
            state=state,
        )
        return 200, transaction.require_project_authority(
            project.project_id, if_match=project.etag
        )

    with pytest.raises(ProviderDataCorruptionError, match="action authority"):
        store.execute_idempotent_action(
            route=f"/desktop/v1/projects/{project.project_id}/workspace-sync",
            resource_scope=project.project_id,
            key="project-orphan-mutation-action",
            body={},
            if_match=project.etag,
            semantic_headers={},
            response_model=ProjectV1,
            mutation=create_orphan_then_return_project,
        )

    assert store._connection.execute("SELECT count(*) FROM local_operations").fetchone()[0] == 0
    assert (
        store._connection.execute(
            "SELECT 1 FROM idempotency_records WHERE idempotency_key = ?",
            ("project-orphan-mutation-action",),
        ).fetchone()
        is None
    )


@pytest.mark.parametrize(
    "clear_digest",
    [False, True],
    ids=["missing-record", "missing-record-and-digest"],
)
def test_startup_rejects_unbound_nonterminal_operation_before_cancellation(
    tmp_path: Path,
    clear_digest: bool,
) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    profile = _create_profile(store)
    project = store.create_project(
        _project(profile.profile_id), idempotency_key="project-orphan-startup-create"
    )
    action = {
        "route": f"/desktop/v1/projects/{project.project_id}/workspace-sync",
        "operation_kind": "workspace_sync",
        "project_id": project.project_id,
        "key": "project-orphan-startup-action",
        "body": {},
        "if_match": project.etag,
    }
    reservation = store.begin_project_runtime_action(**action)
    assert reservation.operation.state == "queued"
    store.close()

    with sqlite3.connect(root / "provider.sqlite3") as connection:
        connection.execute(
            "DELETE FROM idempotency_records WHERE operation_id = ?",
            (reservation.operation.operation_id,),
        )
        if clear_digest:
            connection.execute(
                """
                UPDATE local_operations
                SET action_identity_digest = NULL
                WHERE operation_id = ?
                """,
                (reservation.operation.operation_id,),
            )
        _reseal_provider_usage(connection, root)

    with pytest.raises(ProviderDataCorruptionError, match="action authority"):
        DesktopProviderStore(root)

    with sqlite3.connect(root / "provider.sqlite3") as connection:
        row = connection.execute(
            "SELECT state, document_json FROM local_operations WHERE operation_id = ?",
            (reservation.operation.operation_id,),
        ).fetchone()
    assert row is not None
    assert row[0] == "queued"
    assert LocalOperationV1.model_validate_json(bytes(row[1])).state == "queued"


def test_project_action_operation_kind_is_part_of_idempotency_identity(tmp_path: Path) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    project = store.create_project(
        _project(profile.profile_id), idempotency_key="project-kind-create"
    )
    action = {
        "route": f"/desktop/v1/projects/{project.project_id}/workspace-sync",
        "operation_kind": "workspace_sync",
        "project_id": project.project_id,
        "key": "project-kind-action",
        "body": {},
        "if_match": project.etag,
    }
    store.begin_project_runtime_action(**action)

    with pytest.raises(IdempotencyConflictError):
        store.begin_project_runtime_action(**{**action, "operation_kind": "project_doctor"})


def test_project_reservation_fails_when_terminal_slots_do_not_fit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    project = store.create_project(
        _project(profile.profile_id), idempotency_key="project-capacity-create"
    )
    _, used_bytes = store._recovery_usage(store._connection)
    monkeypatch.setattr(
        provider_store_module,
        "MAX_RECOVERY_BYTES",
        used_bytes + provider_store_module.PROJECT_RUNTIME_TERMINAL_RESERVATION_BYTES,
    )

    with pytest.raises(ProviderDataCorruptionError, match="recovery budget exceeded"):
        store.begin_project_runtime_action(
            route=f"/desktop/v1/projects/{project.project_id}/workspace-sync",
            operation_kind="workspace_sync",
            project_id=project.project_id,
            key="project-no-terminal-capacity",
            body={},
            if_match=project.etag,
        )


def test_generic_nonterminal_operation_rejects_oversized_terminal_shape(
    tmp_path: Path,
) -> None:
    store = DesktopProviderStore(tmp_path / "state")
    profile = _create_profile(store)
    project = store.create_project(
        _project(profile.profile_id), idempotency_key="project-large-operation-create"
    )
    checks = tuple(
        NormalizedCheckV1(
            check_id=f"check-{index}",
            label="l" * 512,
            status="running",
            summary="s" * 512,
        )
        for index in range(1_425)
    )
    checks_bytes = provider_store_module._canonical_json_bytes(
        [check.model_dump(mode="json") for check in checks]
    )
    assert 1_580_000 <= len(checks_bytes) <= 1_600_000

    def create_large_operation(transaction: ProviderMutation):
        return 202, transaction.create_local_operation(
            operation_kind="workspace_sync",
            resource=ResourceRefV1(resource_type="project", resource_id=project.project_id),
            state="running",
            checks=checks,
        )

    with pytest.raises(ContractValidationError, match="terminal slot"):
        store.execute_idempotent_action(
            route=f"/desktop/v1/projects/{project.project_id}/workspace-sync",
            resource_scope=project.project_id,
            key="project-large-operation-action",
            body={},
            if_match=project.etag,
            semantic_headers={},
            response_model=LocalOperationV1,
            mutation=create_large_operation,
        )
    assert store._connection.execute("SELECT count(*) FROM local_operations").fetchone()[0] == 0
    assert (
        store._connection.execute(
            "SELECT 1 FROM idempotency_records WHERE idempotency_key = ?",
            ("project-large-operation-action",),
        ).fetchone()
        is None
    )


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


def test_project_runtime_reservation_prevents_superseding_activation(
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
            remote_state=_remote_project_state(first.project_id, "first-revision"),
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
        with pytest.raises(ResourceInUseError):
            ProviderMutation(store, connection).set_project_state(
                second.project_id,
                if_match=second.etag,
                state="active",
                remote_state=_remote_project_state(second.project_id, "second-revision"),
            )
    error = ApiErrorV1(
        request_id=reservation.operation.operation_id,
        code="activation_failed",
        http_status=409,
        message="Project activation failed.",
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
        project_etag=first.etag,
        active=True,
    )
    assert store.get_project(first.project_id) == first
    assert store.get_project(second.project_id) == second


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


def test_project_startup_cancellation_rejects_terminal_slot_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    profile = _create_profile(store)
    project = store.create_project(
        _project(profile.profile_id), idempotency_key="project-slot-create"
    )
    store.begin_project_runtime_action(
        route=f"/desktop/v1/projects/{project.project_id}/workspace-sync",
        operation_kind="workspace_sync",
        project_id=project.project_id,
        key="project-slot-action",
        body={},
        if_match=project.etag,
    )
    store.close()
    monkeypatch.setattr(provider_store_module, "PROJECT_RUNTIME_TERMINAL_SLOT_BYTES", 1)

    with pytest.raises(ProviderDataCorruptionError, match="project runtime cancellation"):
        DesktopProviderStore(root)


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
        remote_state=None,
        **action,
    )

    assert finished.state == "succeeded"
    assert finished.result is None
    assert store.get_project(project.project_id) == project


def test_active_project_switch_is_atomic_and_active_config_patch_demotes(tmp_path: Path) -> None:
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
                current.project_id,
                if_match=current.etag,
                state="active",
                remote_state=_remote_project_state(current.project_id),
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
    patched = store.patch_project(active.project_id, {"name": "Changed"}, if_match=active.etag)
    assert patched.state == "draft"
    assert patched.remote is None
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


def test_lower_cursor_capacity_reopen_rejects_retries_and_recovers(tmp_path: Path) -> None:
    root = tmp_path / "state"
    clock = MutableClock()
    record_count = provider_store_module.NORMAL_WRITE_CLEANUP_ROWS + 2
    store = DesktopProviderStore(
        root,
        clock=clock,
        cursor_ttl_seconds=1,
        max_cursor_records=record_count,
    )
    _create_profile(store, name="Cursor A", key="lower-cursor-profile-a")
    _create_profile(store, name="Cursor B", key="lower-cursor-profile-b")
    for _ in range(record_count):
        page = store.list_profiles(limit=1, sort="name", direction="asc")
        assert page.next_cursor is not None
    clock.now += timedelta(seconds=2)
    store.close()

    for _ in range(2):
        with pytest.raises(
            provider_store_module.ProviderCapacityConfigurationError,
            match="pagination cursor capacity is lower than persisted usage",
        ) as raised:
            DesktopProviderStore(
                root,
                clock=clock,
                cursor_ttl_seconds=1,
                max_cursor_records=1,
            )
        assert raised.value.record_type == "cursor"
        assert raised.value.configured_limit == 1
        assert raised.value.persisted_count == record_count

    with sqlite3.connect(root / "provider.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM pagination_cursors").fetchone() == (
            record_count,
        )

    recovered = DesktopProviderStore(
        root,
        clock=clock,
        cursor_ttl_seconds=1,
        max_cursor_records=record_count,
    )
    page = recovered.list_profiles(limit=1, sort="name", direction="asc")
    assert page.next_cursor is not None
    assert recovered._provider_record_counts(recovered._connection)[1] == 3
    assert tuple(
        recovered._connection.execute("SELECT count(*) FROM pagination_cursors").fetchone()
    ) == (3,)


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


def test_concurrent_remote_payload_writers_preserve_usage_authority(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    store = DesktopProviderStore(root)
    profile = _create_profile(store)
    projects = tuple(
        store.create_project(
            _project(profile.profile_id, name=f"Concurrent project {index}"),
            idempotency_key=f"concurrent-remote-project-{index:04d}",
        )
        for index in range(16)
    )

    def activate(project: ProjectV1) -> str:
        with store._transaction(write=True) as connection:
            activated = ProviderMutation(store, connection).set_project_state(
                project.project_id,
                if_match=project.etag,
                state="active",
                remote_state=_remote_project_state(
                    project.project_id,
                    f"concurrent-revision-{project.project_id}",
                ),
            )
            return activated.project_id

    with ThreadPoolExecutor(max_workers=8) as executor:
        activated_ids = tuple(executor.map(activate, projects))

    assert set(activated_ids) == {project.project_id for project in projects}
    expected_bytes = cast(
        int,
        store._connection.execute(
            "SELECT sum(length(remote_state_json)) FROM projects"
        ).fetchone()[0],
    )
    assert store._validate_remote_payload_usage_authority(store._connection) == (
        len(projects),
        expected_bytes,
    )
    assert (
        store._connection.execute(
            "SELECT count(*) FROM projects WHERE state = 'active'"
        ).fetchone()[0]
        == 1
    )
    store.close()

    reopened = DesktopProviderStore(root)
    assert reopened._validate_remote_payload_usage_authority(reopened._connection) == (
        len(projects),
        expected_bytes,
    )


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
            remote_state=_remote_project_state(project.project_id, "revision-before-crash"),
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
    for index in range(operation_count):
        store.begin_profile_runtime_action(
            route=f"/desktop/v1/profiles/{profile.profile_id}/disconnect",
            operation_kind="profile_disconnect",
            profile_id=profile.profile_id,
            key=f"profile-stream-disconnect-{index:04d}",
            body={},
            if_match=profile.etag,
            displace_existing=False,
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

        @property
        def rowcount(self):
            return self._cursor.rowcount

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
