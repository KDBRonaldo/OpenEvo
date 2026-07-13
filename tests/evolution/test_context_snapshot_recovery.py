from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import stat
from typing import Iterator

import pytest

from openevo.evolution import context_snapshot_recovery as recovery
from openevo.evolution.context_snapshot_recovery import (
    ContextSnapshotIntegrityError,
    inventory_context_snapshots,
    migrate_legacy_context_snapshot_modes,
    read_context_snapshot,
    reconcile_context_snapshots,
    write_context_snapshot,
)


@contextmanager
def _artifact_root_descriptor(root: Path) -> Iterator[int]:
    root.mkdir()
    (root / "contexts").mkdir()
    (root / "contexts").chmod(0o700)
    descriptor = os.open(
        root,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
    )
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _canonical(context_id: str) -> bytes:
    return f'{{"request":{{"context_id":"{context_id}"}},"response":{{}}}}'.encode()


def test_exclusive_write_read_and_inventory_are_fd_relative(tmp_path: Path) -> None:
    artifact_root = tmp_path / "managed"
    with _artifact_root_descriptor(artifact_root) as artifact_root_fd:
        moved_root = tmp_path / "renamed-managed"
        artifact_root.rename(moved_root)
        contents = _canonical("ctx_1")

        receipt = write_context_snapshot(artifact_root_fd, "ctx_1", contents)

        snapshot = moved_root / "contexts" / "ctx_1.json"
        observed = snapshot.stat()
        assert receipt.name == "ctx_1.json"
        assert receipt.device == observed.st_dev
        assert receipt.inode == observed.st_ino
        assert receipt.link_count == 1
        assert receipt.size_bytes == len(contents)
        assert receipt.sha256 == hashlib.sha256(contents).hexdigest()
        assert stat.S_IMODE(observed.st_mode) == 0o600
        assert (
            read_context_snapshot(
                artifact_root_fd,
                "ctx_1",
                expected_canonical_bytes=contents,
            )
            == contents
        )
        inventory = inventory_context_snapshots(artifact_root_fd)
        assert inventory.snapshots == (receipt,)
        assert inventory.tombstones == ()
        with pytest.raises(FileExistsError):
            write_context_snapshot(artifact_root_fd, "ctx_1", b"replacement")
        assert snapshot.read_bytes() == contents


@pytest.mark.parametrize(
    "context_id",
    ["", "../escape", ".hidden", "ctx/name", "ctx name", "a" * 129],
)
def test_context_id_is_closed(tmp_path: Path, context_id: str) -> None:
    with _artifact_root_descriptor(tmp_path / "managed") as artifact_root_fd:
        with pytest.raises(ValueError, match="closed managed identifier"):
            write_context_snapshot(artifact_root_fd, context_id, b"{}")


def test_contexts_root_symlink_is_rejected_without_touching_target(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "managed"
    artifact_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"keep")
    (artifact_root / "contexts").symlink_to(outside, target_is_directory=True)
    artifact_root_fd = os.open(
        artifact_root,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
    )
    try:
        with pytest.raises(ContextSnapshotIntegrityError, match="opened safely"):
            inventory_context_snapshots(artifact_root_fd)
    finally:
        os.close(artifact_root_fd)
    assert sentinel.read_bytes() == b"keep"


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o770])
def test_contexts_root_requires_exact_mode_0700(tmp_path: Path, mode: int) -> None:
    artifact_root = tmp_path / "managed"
    with _artifact_root_descriptor(artifact_root) as artifact_root_fd:
        (artifact_root / "contexts").chmod(mode)

        with pytest.raises(ContextSnapshotIntegrityError, match="mode 0700"):
            inventory_context_snapshots(artifact_root_fd)
        with pytest.raises(ContextSnapshotIntegrityError, match="mode 0700"):
            migrate_legacy_context_snapshot_modes(artifact_root_fd)


def test_contexts_root_requires_effective_user_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "managed"
    with _artifact_root_descriptor(artifact_root) as artifact_root_fd:
        monkeypatch.setattr(recovery.os, "geteuid", lambda: os.getuid() + 1)

        with pytest.raises(ContextSnapshotIntegrityError, match="euid-owned"):
            inventory_context_snapshots(artifact_root_fd)


@pytest.mark.parametrize("entry_kind", ["symlink", "hardlink", "directory", "unknown"])
def test_inventory_fails_closed_on_unsafe_or_unknown_entry(
    tmp_path: Path,
    entry_kind: str,
) -> None:
    artifact_root = tmp_path / "managed"
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside-sensitive")
    outside.chmod(0o600)
    with _artifact_root_descriptor(artifact_root) as artifact_root_fd:
        contexts = artifact_root / "contexts"
        if entry_kind == "symlink":
            (contexts / "ctx_bad.json").symlink_to(outside)
        elif entry_kind == "hardlink":
            os.link(outside, contexts / "ctx_bad.json")
        elif entry_kind == "directory":
            (contexts / "ctx_bad.json").mkdir()
        else:
            (contexts / "unexpected").write_bytes(b"unknown")

        with pytest.raises(ContextSnapshotIntegrityError):
            inventory_context_snapshots(artifact_root_fd)
        with pytest.raises(ContextSnapshotIntegrityError):
            migrate_legacy_context_snapshot_modes(artifact_root_fd)

    assert outside.read_bytes() == b"outside-sensitive"


def test_explicit_legacy_mode_migration_tightens_eligible_snapshots(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "managed"
    with _artifact_root_descriptor(artifact_root) as artifact_root_fd:
        contexts = artifact_root / "contexts"
        snapshots = {
            "ctx_0644": (_canonical("ctx_0644"), 0o644),
            "ctx_0640": (_canonical("ctx_0640"), 0o640),
            "ctx_0600": (_canonical("ctx_0600"), 0o600),
        }
        for context_id, (contents, mode) in snapshots.items():
            path = contexts / f"{context_id}.json"
            path.write_bytes(contents)
            path.chmod(mode)

        with pytest.raises(ContextSnapshotIntegrityError, match="mode 0600"):
            inventory_context_snapshots(artifact_root_fd)
        with pytest.raises(ContextSnapshotIntegrityError, match="mode 0600"):
            read_context_snapshot(artifact_root_fd, "ctx_0644")

        result = migrate_legacy_context_snapshot_modes(artifact_root_fd)

        assert result.migrated_context_ids == ("ctx_0640", "ctx_0644")
        assert result.already_private_context_ids == ("ctx_0600",)
        for context_id, (contents, _mode) in snapshots.items():
            path = contexts / f"{context_id}.json"
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert read_context_snapshot(artifact_root_fd, context_id) == contents

        repeated = migrate_legacy_context_snapshot_modes(artifact_root_fd)
        assert repeated.migrated_context_ids == ()
        assert repeated.already_private_context_ids == tuple(sorted(snapshots))


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        (0o660, "group/other writable"),
        (0o602, "group/other writable"),
        (0o700, "executable"),
        (0o400, "owner-readable and writable"),
        (0o200, "owner-readable and writable"),
        (0o4644, "special permission bits"),
    ],
)
def test_legacy_mode_migration_rejects_unsafe_permissions(
    tmp_path: Path,
    mode: int,
    message: str,
) -> None:
    artifact_root = tmp_path / "managed"
    with _artifact_root_descriptor(artifact_root) as artifact_root_fd:
        snapshot = artifact_root / "contexts" / "ctx_unsafe.json"
        snapshot.write_bytes(_canonical("ctx_unsafe"))
        snapshot.chmod(mode)

        with pytest.raises(ContextSnapshotIntegrityError, match=message):
            migrate_legacy_context_snapshot_modes(artifact_root_fd)

        assert stat.S_IMODE(snapshot.stat().st_mode) == mode


def test_legacy_mode_migration_preflights_unknown_entries_before_chmod(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "managed"
    with _artifact_root_descriptor(artifact_root) as artifact_root_fd:
        snapshot = artifact_root / "contexts" / "ctx_legacy.json"
        snapshot.write_bytes(_canonical("ctx_legacy"))
        snapshot.chmod(0o644)
        (artifact_root / "contexts" / "unknown-entry").write_bytes(b"unknown")

        with pytest.raises(ContextSnapshotIntegrityError, match="unknown entry"):
            migrate_legacy_context_snapshot_modes(artifact_root_fd)

        assert stat.S_IMODE(snapshot.stat().st_mode) == 0o644


def test_legacy_mode_migration_rechecks_path_identity_before_fchmod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "managed"
    original = _canonical("ctx_legacy_race")
    replacement = _canonical("ctx_replacement")
    with _artifact_root_descriptor(artifact_root) as artifact_root_fd:
        contexts = artifact_root / "contexts"
        snapshot = contexts / "ctx_legacy_race.json"
        snapshot.write_bytes(original)
        snapshot.chmod(0o644)

        def replace_before_fchmod(
            contexts_fd: int,
            _context_id: str,
            name: str,
            _descriptor: int,
        ) -> None:
            os.rename(
                name,
                "ctx_moved.json",
                src_dir_fd=contexts_fd,
                dst_dir_fd=contexts_fd,
            )
            replacement_fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                0o644,
                dir_fd=contexts_fd,
            )
            try:
                os.write(replacement_fd, replacement)
                os.fchmod(replacement_fd, 0o644)
                os.fsync(replacement_fd)
            finally:
                os.close(replacement_fd)

        monkeypatch.setattr(
            recovery,
            "_before_legacy_snapshot_fchmod",
            replace_before_fchmod,
        )
        with pytest.raises(ContextSnapshotIntegrityError, match="changed"):
            migrate_legacy_context_snapshot_modes(artifact_root_fd)

        assert snapshot.read_bytes() == replacement
        assert stat.S_IMODE(snapshot.stat().st_mode) == 0o644
        moved = contexts / "ctx_moved.json"
        assert moved.read_bytes() == original
        assert stat.S_IMODE(moved.stat().st_mode) == 0o644


def test_legacy_mode_migration_rejects_foreign_owner(tmp_path: Path) -> None:
    if os.geteuid() != 0:
        pytest.skip("changing file ownership requires root")
    artifact_root = tmp_path / "managed"
    with _artifact_root_descriptor(artifact_root) as artifact_root_fd:
        snapshot = artifact_root / "contexts" / "ctx_foreign.json"
        snapshot.write_bytes(_canonical("ctx_foreign"))
        snapshot.chmod(0o644)
        os.chown(snapshot, 1, -1)

        with pytest.raises(ContextSnapshotIntegrityError, match="effective user"):
            migrate_legacy_context_snapshot_modes(artifact_root_fd)


def test_reconcile_tombstones_snapshot_written_before_db_commit(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "managed"
    with _artifact_root_descriptor(artifact_root) as artifact_root_fd:
        referenced = _canonical("ctx_referenced")
        orphan = _canonical("ctx_crash_window")
        write_context_snapshot(artifact_root_fd, "ctx_referenced", referenced)
        write_context_snapshot(artifact_root_fd, "ctx_crash_window", orphan)

        result = reconcile_context_snapshots(
            artifact_root_fd,
            {"ctx_referenced": referenced},
        )

        assert result.removed_orphan_context_ids == ("ctx_crash_window",)
        assert tuple(item.context_id for item in result.referenced) == ("ctx_referenced",)
        assert len(result.tombstones) == 1
        assert not (artifact_root / "contexts" / "ctx_crash_window.json").exists()
        tombstone = artifact_root / "contexts" / result.tombstones[0]
        assert tombstone.read_bytes() == b""
        assert stat.S_IMODE(tombstone.stat().st_mode) == 0o600

        restarted = reconcile_context_snapshots(
            artifact_root_fd,
            {"ctx_referenced": referenced},
        )
        assert restarted.removed_orphan_context_ids == ()
        assert restarted.tombstones == result.tombstones
        migrated = migrate_legacy_context_snapshot_modes(artifact_root_fd)
        assert migrated.migrated_context_ids == ()
        assert migrated.already_private_context_ids == ("ctx_referenced",)
        assert inventory_context_snapshots(artifact_root_fd).tombstones == result.tombstones


@pytest.mark.parametrize("failure", ["missing", "corrupt"])
def test_reconcile_rejects_missing_or_corrupt_referenced_snapshot_before_cleanup(
    tmp_path: Path,
    failure: str,
) -> None:
    artifact_root = tmp_path / "managed"
    with _artifact_root_descriptor(artifact_root) as artifact_root_fd:
        orphan = _canonical("ctx_orphan")
        write_context_snapshot(artifact_root_fd, "ctx_orphan", orphan)
        if failure == "corrupt":
            write_context_snapshot(
                artifact_root_fd,
                "ctx_referenced",
                _canonical("wrong_context"),
            )

        with pytest.raises(ContextSnapshotIntegrityError, match=failure):
            reconcile_context_snapshots(
                artifact_root_fd,
                {"ctx_referenced": _canonical("ctx_referenced")},
            )

        assert (artifact_root / "contexts" / "ctx_orphan.json").read_bytes() == orphan
        assert not list((artifact_root / "contexts").glob(".openevo-context-tombstone-*"))


def test_reconcile_preserves_replacement_created_after_quarantine_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "managed"
    replacement = b"replacement-must-survive"
    with _artifact_root_descriptor(artifact_root) as artifact_root_fd:
        write_context_snapshot(
            artifact_root_fd,
            "ctx_race",
            _canonical("ctx_race"),
        )

        def replace_original(
            contexts_fd: int,
            _context_id: str,
            original_name: str,
            _quarantine_name: str,
        ) -> None:
            descriptor = os.open(
                original_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                0o600,
                dir_fd=contexts_fd,
            )
            try:
                os.write(descriptor, replacement)
                os.fchmod(descriptor, 0o600)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

        monkeypatch.setattr(recovery, "_after_orphan_quarantine", replace_original)
        with pytest.raises(ContextSnapshotIntegrityError, match="replacement preserved"):
            reconcile_context_snapshots(artifact_root_fd, {})

        contexts = artifact_root / "contexts"
        preserved = list(contexts.glob(".openevo-context-preserved-*"))
        tombstones = list(contexts.glob(".openevo-context-tombstone-*"))
        assert len(preserved) == 1
        assert preserved[0].read_bytes() == replacement
        assert len(tombstones) == 1
        assert tombstones[0].read_bytes() == b""
        assert not (contexts / "ctx_race.json").exists()

        monkeypatch.undo()
        with pytest.raises(ContextSnapshotIntegrityError, match="unknown entry"):
            reconcile_context_snapshots(artifact_root_fd, {})
        assert preserved[0].read_bytes() == replacement


def test_reconcile_does_not_clear_hardlink_added_after_quarantine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "managed"
    contents = _canonical("ctx_hardlink_race")
    outside = tmp_path / "outside-hardlink"
    with _artifact_root_descriptor(artifact_root) as artifact_root_fd:
        write_context_snapshot(artifact_root_fd, "ctx_hardlink_race", contents)

        def add_hardlink(
            contexts_fd: int,
            _context_id: str,
            _original_name: str,
            quarantine_name: str,
        ) -> None:
            os.link(quarantine_name, outside, src_dir_fd=contexts_fd)

        monkeypatch.setattr(recovery, "_after_orphan_quarantine", add_hardlink)
        with pytest.raises(ContextSnapshotIntegrityError, match="link-count-one"):
            reconcile_context_snapshots(artifact_root_fd, {})

        assert outside.read_bytes() == contents
        quarantine = list((artifact_root / "contexts").glob(".openevo-context-quarantine-*"))
        assert len(quarantine) == 1
        assert quarantine[0].read_bytes() == contents


def test_read_rejects_authoritative_byte_mismatch(tmp_path: Path) -> None:
    with _artifact_root_descriptor(tmp_path / "managed") as artifact_root_fd:
        write_context_snapshot(artifact_root_fd, "ctx_1", _canonical("ctx_1"))
        with pytest.raises(ContextSnapshotIntegrityError, match="DB-authorized"):
            read_context_snapshot(
                artifact_root_fd,
                "ctx_1",
                expected_canonical_bytes=_canonical("ctx_other"),
            )
