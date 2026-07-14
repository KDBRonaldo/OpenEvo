from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

import desktop.sidecar.provider_store as provider_store_module
from desktop.sidecar.contracts.v1.models import (
    ConnectionOperationResultV1,
    CredentialSlotStatusV1,
    LocalOperationV1,
    ProjectCreateV1,
    ProjectOperationResultV1,
    ProjectPatchV1,
    RemoteProfilePatchV1,
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


def test_profile_runtime_state_is_closed_and_recovered_on_restart(
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
    assert replay.response_bytes != first.response_bytes
    assert replayed_operation.state == "cancelled"
    assert replayed_operation.result == ConnectionOperationResultV1(
        profile_id=profile.profile_id,
        connection_state="disconnected",
    )


def test_deleted_profile_action_replay_cannot_return_connected(tmp_path: Path) -> None:
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
    assert operation.state == "cancelled"
    assert operation.result == ConnectionOperationResultV1(
        profile_id=profile.profile_id,
        connection_state="disconnected",
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
    with pytest.raises(CursorExpiredError):
        reopened.list_profiles(
            limit=1,
            after=first.next_cursor,
            sort="name",
            direction="asc",
        )


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
            for index in range(5)
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
    assert recovered_project.current_revision_id is None
    assert recovered_operation.state == "cancelled"
    assert recovered_operation.finished_at is not None
    assert recovered_operation.result == ConnectionOperationResultV1(
        profile_id=profile.profile_id,
        connection_state="disconnected",
    )


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
