from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from desktop.sidecar.contracts.v1.models import (
    EvolutionConfigV1,
    EvolutionSelectionsV1,
    ExecutionSettingsV1,
    ProjectCreateV1,
    ProjectSourceV1,
    ProjectTaskV1,
    RemoteProfileCreateV1,
)
from desktop.sidecar.contracts.v2.models import ProfileDisplayNamePatchV2
from desktop.sidecar import legacy_v1_import as legacy_module
from desktop.sidecar.legacy_v1_import import LegacyV1Importer
from desktop.sidecar.provider_store import DesktopProviderStore
from desktop.sidecar.provider_store_v2 import DesktopProviderStoreV2
from desktop.sidecar.release_runtime import create_release_local_state_v2


class _Clock:
    def __init__(self) -> None:
        self._next = datetime(2026, 7, 23, 5, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        value = self._next
        self._next += timedelta(microseconds=1)
        return value


def _legacy_profile(name: str, host: str) -> RemoteProfileCreateV1:
    return RemoteProfileCreateV1(
        name=name,
        host=host,
        port=22,
        user="researcher",
        authentication_kind="ssh_agent",
    )


def _legacy_project(profile_id: str, *, name: str = "Preview draft") -> ProjectCreateV1:
    return ProjectCreateV1(
        name=name,
        profile_id=profile_id,
        task=ProjectTaskV1(
            title="Preview task",
            objective="Preserve only validated local draft intent.",
        ),
        source=ProjectSourceV1(kind="scratch", display_name="Scratch"),
        execution=ExecutionSettingsV1(
            mode="codex_subscription_transcript",
            codex_model="gpt-5.5",
            reasoning_effort="high",
        ),
        evolution=EvolutionConfigV1(targets=EvolutionSelectionsV1({})),
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _build_legacy_state(root: Path) -> tuple[str, str]:
    store = DesktopProviderStore(root, clock=_Clock())
    profile = store.create_profile(
        _legacy_profile("Preview GPU", "10.10.0.8"),
        idempotency_key="legacy-profile-create-0001",
    )
    draft = store.create_project(
        _legacy_project(profile.profile_id),
        idempotency_key="legacy-draft-create-0001",
    )
    active = store.create_project(
        _legacy_project(profile.profile_id, name="Cached active project"),
        idempotency_key="legacy-active-create-0001",
    )
    store.close()
    with sqlite3.connect(root / "provider.sqlite3") as connection:
        connection.execute(
            "UPDATE projects SET state = 'active', remote_state_json = ? WHERE project_id = ?",
            (b'{"active_revision":{"revision_id":"generic-v1"}}', active.project_id),
        )
    return profile.profile_id, draft.project_id


def test_read_only_import_keeps_v1_bytes_and_exposes_only_rebind_records(
    tmp_path: Path,
) -> None:
    legacy_root = tmp_path / "state-v2"
    legacy_profile_id, legacy_draft_id = _build_legacy_state(legacy_root)
    database = legacy_root / "provider.sqlite3"
    before = (database.stat().st_size, database.stat().st_mtime_ns, _sha256(database))

    runtime = create_release_local_state_v2(legacy_root, clock=_Clock())
    try:
        report = runtime.legacy_import
        assert len(report.profiles) == 1
        imported = report.profiles[0]
        assert imported.profile_kind == "legacy_explicit"
        assert imported.connectable is False
        assert imported.migration_state == "rebind_required"
        assert imported.profile_id != legacy_profile_id
        assert set(imported.model_dump(mode="json")) == {
            "schema_version",
            "profile_kind",
            "profile_id",
            "display_name",
            "connectable",
            "migration_state",
            "created_at",
            "updated_at",
            "etag",
        }
        assert len(report.drafts) == 1
        draft = report.drafts[0]
        assert draft.legacy_project_id == legacy_draft_id
        assert draft.request.name == "Preview draft"
        assert not hasattr(draft, "remote")
        assert all("revision" not in item.model_dump_json() for item in report.profiles)

        persisted = (legacy_root / "provider-v2" / "provider-v2.sqlite3").read_bytes()
        assert b"10.10.0.8" not in persisted
        assert b"researcher" not in persisted
        assert b"generic-v1" not in persisted
    finally:
        runtime.close()

    after = (database.stat().st_size, database.stat().st_mtime_ns, _sha256(database))
    assert after == before
    assert not (legacy_root / "provider.sqlite3-wal").exists()
    assert not (legacy_root / "provider.sqlite3-shm").exists()


def test_corrupt_and_oversized_rows_are_quarantined_without_blocking_valid_import(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state-v2"
    store = DesktopProviderStore(root, clock=_Clock())
    valid = store.create_profile(
        _legacy_profile("Valid Preview", "valid.example"),
        idempotency_key="legacy-valid-profile-0001",
    )
    corrupt = store.create_profile(
        _legacy_profile("Corrupt Preview", "corrupt.example"),
        idempotency_key="legacy-corrupt-profile-0001",
    )
    oversized = store.create_profile(
        _legacy_profile("Large Preview", "large.example"),
        idempotency_key="legacy-large-profile-0001",
    )
    store.close()
    with sqlite3.connect(root / "provider.sqlite3") as connection:
        connection.execute(
            "UPDATE remote_profiles SET document_json = ? WHERE profile_id = ?",
            (b"{not-json", corrupt.profile_id),
        )
        connection.execute(
            "UPDATE remote_profiles SET document_json = zeroblob(?) WHERE profile_id = ?",
            (legacy_module.MAX_LEGACY_DOCUMENT_BYTES + 1, oversized.profile_id),
        )

    runtime = create_release_local_state_v2(root, clock=_Clock())
    try:
        imported = runtime.legacy_import.profiles
        valid_imports = [item for item in imported if item.migration_state == "rebind_required"]
        quarantined = [item for item in imported if item.migration_state == "quarantined"]
        assert [item.display_name for item in valid_imports] == ["Valid Preview"]
        assert len(quarantined) == 2
        assert {item.display_name for item in quarantined} == {"Legacy profile requires review"}
        codes = {item.code for item in runtime.legacy_import.diagnostics}
        assert "legacy_profile_corrupt" in codes
        assert "legacy_profile_oversized" in codes
        assert runtime.provider_store.get_profile(valid_imports[0].profile_id)
        assert valid.profile_id not in {item.profile_id for item in imported}
    finally:
        runtime.close()


def test_corrupt_and_oversized_legacy_drafts_are_diagnostic_only(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state-v2"
    store = DesktopProviderStore(root, clock=_Clock())
    profile = store.create_profile(
        _legacy_profile("Draft profile", "drafts.example"),
        idempotency_key="legacy-draft-profile-0001",
    )
    valid = store.create_project(
        _legacy_project(profile.profile_id, name="Valid draft"),
        idempotency_key="legacy-valid-draft-0001",
    )
    corrupt = store.create_project(
        _legacy_project(profile.profile_id, name="Corrupt draft"),
        idempotency_key="legacy-corrupt-draft-0001",
    )
    oversized = store.create_project(
        _legacy_project(profile.profile_id, name="Large draft"),
        idempotency_key="legacy-large-draft-0001",
    )
    store.close()
    with sqlite3.connect(root / "provider.sqlite3") as connection:
        connection.execute(
            "UPDATE projects SET document_json = ? WHERE project_id = ?",
            (b"{not-json", corrupt.project_id),
        )
        connection.execute(
            "UPDATE projects SET document_json = zeroblob(?) WHERE project_id = ?",
            (legacy_module.MAX_LEGACY_DOCUMENT_BYTES + 1, oversized.project_id),
        )

    runtime = create_release_local_state_v2(root, clock=_Clock())
    try:
        assert [item.legacy_project_id for item in runtime.legacy_import.drafts] == [
            valid.project_id
        ]
        assert {item.code for item in runtime.legacy_import.diagnostics} >= {
            "legacy_project_corrupt",
            "legacy_project_oversized",
        }
        assert len(runtime.provider_store.list_drafts()) == 0
    finally:
        runtime.close()


def test_repeated_import_converges_without_overwriting_a_local_v2_rename(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state-v2"
    _build_legacy_state(root)
    first = create_release_local_state_v2(root, clock=_Clock())
    legacy = first.legacy_import.profiles[0]
    first.close()

    store = DesktopProviderStoreV2(root / "provider-v2", clock=_Clock())
    renamed = store.rename_profile(
        legacy.profile_id,
        ProfileDisplayNamePatchV2(display_name="My retained Preview profile"),
        if_match=legacy.etag,
        idempotency_key="rename-imported-preview-profile-0001",
    )
    store.close()

    second = create_release_local_state_v2(root, clock=_Clock())
    try:
        assert second.legacy_import.profiles == (renamed,)
        assert "legacy_source_changed" not in {
            item.code for item in second.legacy_import.diagnostics
        }
        assert len(second.provider_store.list_profiles()) == 1
    finally:
        second.close()


@pytest.mark.parametrize("failure", ["oversized", "symlink", "schema"])
def test_unavailable_legacy_store_yields_bounded_diagnostic_and_unrelated_startup(
    tmp_path: Path,
    failure: str,
) -> None:
    root = tmp_path / "state-v2"
    root.mkdir(mode=0o700)
    database = root / "provider.sqlite3"
    if failure == "oversized":
        database.touch(mode=0o600)
        os.truncate(database, legacy_module.MAX_LEGACY_DATABASE_BYTES + 1)
    elif failure == "symlink":
        target = tmp_path / "outside.sqlite3"
        target.touch(mode=0o600)
        database.symlink_to(target)
    else:
        database.touch(mode=0o600)
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE unexpected(value TEXT) STRICT")
            connection.execute("PRAGMA user_version = 7")

    runtime = create_release_local_state_v2(root, clock=_Clock())
    try:
        assert runtime.provider_store.list_profiles() == ()
        assert runtime.legacy_import.profiles == ()
        assert len(runtime.legacy_import.diagnostics) == 1
        assert runtime.legacy_import.diagnostics[0].code in {
            "legacy_store_oversized",
            "legacy_store_unsafe",
            "legacy_schema_unsupported",
        }
    finally:
        runtime.close()


def test_missing_legacy_state_is_not_an_error(tmp_path: Path) -> None:
    root = tmp_path / "state-v2"
    runtime = create_release_local_state_v2(root, clock=_Clock())
    try:
        assert runtime.legacy_import.profiles == ()
        assert runtime.legacy_import.drafts == ()
        assert runtime.legacy_import.diagnostics == ()
    finally:
        runtime.close()


def test_busy_legacy_owner_does_not_block_fresh_v2_startup(tmp_path: Path) -> None:
    root = tmp_path / "state-v2"
    legacy = DesktopProviderStore(root, clock=_Clock())
    try:
        legacy.create_profile(
            _legacy_profile("Busy Preview", "busy.example"),
            idempotency_key="legacy-busy-profile-0001",
        )
        runtime = create_release_local_state_v2(root, clock=_Clock())
        try:
            assert runtime.provider_store.list_profiles() == ()
            assert runtime.legacy_import.profiles == ()
            assert [item.code for item in runtime.legacy_import.diagnostics] == [
                "legacy_store_busy"
            ]
        finally:
            runtime.close()
    finally:
        legacy.close()


def test_legacy_scan_enforces_one_aggregate_row_budget(tmp_path: Path) -> None:
    root = tmp_path / "state-v2"
    store = DesktopProviderStore(root, clock=_Clock())
    profile_ids = []
    for index in range(9):
        profile = store.create_profile(
            _legacy_profile(f"Large profile {index}", f"large-{index}.example"),
            idempotency_key=f"legacy-aggregate-profile-{index:04d}",
        )
        profile_ids.append(profile.profile_id)
    store.close()
    with sqlite3.connect(root / "provider.sqlite3") as connection:
        for profile_id in profile_ids:
            connection.execute(
                "UPDATE remote_profiles SET document_json = zeroblob(?) WHERE profile_id = ?",
                (legacy_module.MAX_LEGACY_DOCUMENT_BYTES, profile_id),
            )

    runtime = create_release_local_state_v2(root, clock=_Clock())
    try:
        assert len(runtime.legacy_import.profiles) < len(profile_ids)
        assert "legacy_row_budget_exhausted" in {
            diagnostic.code for diagnostic in runtime.legacy_import.diagnostics
        }
    finally:
        runtime.close()


def test_legacy_database_replacement_during_scan_is_detected_without_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "state-v2"
    _build_legacy_state(root)
    replacement = tmp_path / "replacement.sqlite3"
    replacement.touch(mode=0o600)
    original = tmp_path / "original.sqlite3"
    replaced = False

    def replace(stage: str) -> None:
        nonlocal replaced
        if stage == "after_database_open" and not replaced:
            replaced = True
            (root / "provider.sqlite3").rename(original)
            replacement.rename(root / "provider.sqlite3")

    monkeypatch.setattr(legacy_module, "_legacy_scan_checkpoint", replace)
    v2 = DesktopProviderStoreV2(root / "provider-v2", clock=_Clock())
    try:
        report = LegacyV1Importer(root).import_into(v2)
        assert report.profiles == ()
        assert report.drafts == ()
        assert len(report.diagnostics) == 1
        assert report.diagnostics[0].code == "legacy_store_replaced"
        assert v2.list_profiles() == ()
    finally:
        v2.close()


def test_legacy_binding_accepts_sqlite_canonical_managed_path(tmp_path: Path) -> None:
    root = tmp_path / "state-v2"
    _build_legacy_state(root)
    root_fd = os.open(
        root,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    database_fd = os.open(
        "provider.sqlite3",
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        dir_fd=root_fd,
    )
    try:
        root_stat = os.fstat(root_fd)
        database_stat = os.fstat(database_fd)
        lock_stat = os.stat(
            "provider.lock",
            dir_fd=root_fd,
            follow_symlinks=False,
        )
        LegacyV1Importer(root)._verify_binding(
            root_fd,
            root_identity=(root_stat.st_dev, root_stat.st_ino),
            database_identity=(database_stat.st_dev, database_stat.st_ino),
            lock_identity=(lock_stat.st_dev, lock_stat.st_ino),
            database_descriptor=database_fd,
            sqlite_path=str(root / "provider.sqlite3"),
        )
    finally:
        os.close(database_fd)
        os.close(root_fd)


def test_legacy_binding_rejects_managed_path_on_different_device(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "state-v2"
    _build_legacy_state(root)
    root_fd = os.open(
        root,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    database_fd = os.open(
        "provider.sqlite3",
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        dir_fd=root_fd,
    )
    real_stat = os.stat
    managed_path = str(root / "provider.sqlite3")

    def spoof_managed_path_device(path, *args, **kwargs):
        observed = real_stat(path, *args, **kwargs)
        if path == managed_path and kwargs.get("follow_symlinks") is False:
            return SimpleNamespace(
                st_dev=observed.st_dev + 1,
                st_ino=observed.st_ino,
                st_size=observed.st_size,
                st_mode=observed.st_mode,
            )
        return observed

    try:
        root_stat = os.fstat(root_fd)
        database_stat = os.fstat(database_fd)
        lock_stat = os.stat(
            "provider.lock",
            dir_fd=root_fd,
            follow_symlinks=False,
        )
        monkeypatch.setattr(legacy_module.os, "stat", spoof_managed_path_device)
        with pytest.raises(legacy_module._LegacyUnavailable, match="legacy_store_replaced"):
            LegacyV1Importer(root)._verify_binding(
                root_fd,
                root_identity=(root_stat.st_dev, root_stat.st_ino),
                database_identity=(database_stat.st_dev, database_stat.st_ino),
                lock_identity=(lock_stat.st_dev, lock_stat.st_ino),
                database_descriptor=database_fd,
                sqlite_path=managed_path,
            )
    finally:
        os.close(database_fd)
        os.close(root_fd)
