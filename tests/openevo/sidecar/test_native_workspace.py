from __future__ import annotations

import os
from pathlib import Path
import stat
import sys

import pytest

from desktop.sidecar import native_workspace as native_workspace_module
from desktop.sidecar.native_workspace import (
    NativeWorkspaceArchiveCancelled,
    NativeWorkspaceArchiveError,
    prepare_native_workspace,
)
from desktop.sidecar.workspace_identity import (
    native_import_id_for_action,
    ownership_for_native_import,
    project_id_for_native_import,
)
from desktop.sidecar.workspace_imports import (
    WorkspaceImportIntegrityError,
    WorkspaceImportStore,
)


def _prepare(root: Path, private_root: Path, action: str):
    import_id = native_import_id_for_action(action)
    identity = root.resolve().stat()
    return prepare_native_workspace(
        str(root.resolve()),
        import_id=import_id,
        temporary_root=private_root,
        expected_device=identity.st_dev,
        expected_inode=identity.st_ino,
    )


def test_native_workspace_build_is_deterministic_and_store_verified(tmp_path: Path) -> None:
    source = tmp_path / "research"
    source.mkdir()
    (source / "data").mkdir()
    (source / "data" / "results.csv").write_text("sample,value\na,3\n", encoding="utf-8")
    script = source / "analyse.sh"
    script.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    script.chmod(0o751)
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)
    store = WorkspaceImportStore(private_root / "imports")

    with _prepare(source, private_root, "native-source-action-0001") as first:
        first_bytes = first.stream.read()
        ownership = ownership_for_native_import(first.import_ref)
        stored = store.ingest(
            first.stream,
            ownership=ownership,
            import_id=first.import_ref.import_id,
        )
    os.utime(source / "data" / "results.csv", ns=(99, 99))
    with _prepare(source, private_root, "native-source-action-0001") as second:
        second_bytes = second.stream.read()

    assert first_bytes == second_bytes
    assert stored == first.import_ref == second.import_ref
    assert stored.entry_count == 3
    assert stored.extracted_byte_size == len("sample,value\na,3\n") + len("#!/bin/sh\necho ok\n")
    assert project_id_for_native_import(stored.import_id).startswith("project-")
    store.verify(stored, ownership=ownership)
    with store.resolve(stored, ownership=ownership) as snapshot:
        assert snapshot.read() == first_bytes


def test_native_workspace_action_and_store_ingest_are_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "research"
    source.mkdir()
    (source / "notes.txt").write_text("observation", encoding="utf-8")
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)
    store = WorkspaceImportStore(private_root / "imports")

    with _prepare(source, private_root, "native-source-action-0002") as prepared:
        ownership = ownership_for_native_import(prepared.import_ref)
        first = store.ingest(
            prepared.stream,
            ownership=ownership,
            import_id=prepared.import_ref.import_id,
        )
        second = store.ingest(
            prepared.stream,
            ownership=ownership,
            import_id=prepared.import_ref.import_id,
        )

    assert first == second
    assert native_import_id_for_action("native-source-action-0002") == first.import_id


def test_workspace_import_store_inspects_only_verified_internal_authority(
    tmp_path: Path,
) -> None:
    source = tmp_path / "research"
    source.mkdir()
    (source / "notes.txt").write_text("observation", encoding="utf-8")
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)
    store = WorkspaceImportStore(private_root / "imports")

    with _prepare(source, private_root, "native-source-inspect-0001") as prepared:
        ownership = ownership_for_native_import(prepared.import_ref)
        pending = store.ingest_pending(
            prepared.stream,
            ownership=ownership,
            import_id=prepared.import_ref.import_id,
        )

    authority = store.inspect(pending.import_ref.import_id)

    assert authority.import_ref == pending.import_ref
    assert authority.ownership == ownership
    assert authority.pending is True


def test_native_workspace_cancellation_stops_before_the_second_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "research"
    source.mkdir()
    (source / "payload.bin").write_bytes(os.urandom(2 * 1024 * 1024))
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)
    cancelled = False

    def cancel_after_archive(_root_descriptor: int) -> None:
        nonlocal cancelled
        cancelled = True

    monkeypatch.setattr(native_workspace_module, "_after_archive_write", cancel_after_archive)

    with pytest.raises(NativeWorkspaceArchiveCancelled):
        with prepare_native_workspace(
            str(source.resolve()),
            import_id=native_import_id_for_action("native-source-cancelled-0001"),
            temporary_root=private_root,
            expected_device=source.stat().st_dev,
            expected_inode=source.stat().st_ino,
            cancel_check=lambda: cancelled,
        ):
            pass


def test_native_workspace_rejects_links_special_files_and_noncanonical_names(
    tmp_path: Path,
) -> None:
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)
    cases: list[Path] = []

    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / "target").write_text("target", encoding="utf-8")
    (linked / "alias").symlink_to("target")
    cases.append(linked)

    special = tmp_path / "special"
    special.mkdir()
    os.mkfifo(special / "pipe", 0o600)
    cases.append(special)

    noncanonical = tmp_path / "noncanonical"
    noncanonical.mkdir()
    (noncanonical / "e\u0301.txt").write_text("text", encoding="utf-8")
    cases.append(noncanonical)

    hardlinked = tmp_path / "hardlinked"
    hardlinked.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    os.link(outside, hardlinked / "outside.txt")
    cases.append(hardlinked)

    sparse = tmp_path / "sparse"
    sparse.mkdir()
    sparse_file = sparse / "sparse.bin"
    with sparse_file.open("wb") as stream:
        stream.seek(1024 * 1024 - 1)
        stream.write(b"\0")
    sparse_status = sparse_file.stat()
    if sparse_status.st_blocks * 512 < sparse_status.st_size:
        cases.append(sparse)

    for index, source in enumerate(cases):
        with pytest.raises(NativeWorkspaceArchiveError):
            with _prepare(source, private_root, f"native-source-invalid-{index:04d}"):
                pass


def test_native_workspace_rejects_4096_byte_unwritten_sparse_extent(tmp_path: Path) -> None:
    if not hasattr(os, "posix_fallocate"):
        pytest.skip("platform cannot create an allocated unwritten sparse extent")
    source = tmp_path / "sparse"
    source.mkdir()
    sparse_file = source / "sparse.bin"
    with sparse_file.open("w+b") as stream:
        os.posix_fallocate(stream.fileno(), 0, 4096)
    status = sparse_file.stat()
    if status.st_blocks * 512 < status.st_size:
        pytest.skip("filesystem did not allocate the unwritten extent")
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)

    with pytest.raises(NativeWorkspaceArchiveError, match="sparse"):
        with _prepare(source, private_root, "native-source-sparse-extent-0001"):
            pass


def test_native_workspace_fails_closed_without_extent_queries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "research"
    source.mkdir()
    (source / "notes.txt").write_text("observation", encoding="utf-8")
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)
    monkeypatch.delattr(os, "SEEK_DATA", raising=False)
    monkeypatch.delattr(os, "SEEK_HOLE", raising=False)

    with pytest.raises(NativeWorkspaceArchiveError, match="detection is unavailable"):
        with _prepare(source, private_root, "native-source-no-extent-query-0001"):
            pass


def test_native_workspace_rejects_low_allocation_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SparseStatus:
        st_size = 8 * 1024 * 1024
        st_blocks = 8

    monkeypatch.setattr(native_workspace_module.os, "fstat", lambda _descriptor: SparseStatus())

    with pytest.raises(NativeWorkspaceArchiveError, match="allocation"):
        native_workspace_module._require_non_sparse_file(17, SparseStatus.st_size)


def test_native_workspace_extent_fallback_cannot_hide_low_allocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "research"
    source.mkdir()
    sparse_file = source / "sparse.bin"
    with sparse_file.open("w+b") as stream:
        stream.write(b"x" * 4096)
        stream.truncate(8 * 1024 * 1024)
    status = sparse_file.stat()
    if status.st_blocks * 512 >= status.st_size:
        pytest.skip("filesystem allocated the complete logical file")
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)

    def minimal_extent_map(_descriptor: int, _offset: int, whence: int) -> int:
        return 0 if whence == os.SEEK_DATA else status.st_size

    monkeypatch.setattr(native_workspace_module, "_seek_extent", minimal_extent_map)

    with pytest.raises(NativeWorkspaceArchiveError, match="allocation"):
        with _prepare(source, private_root, "native-source-minimal-extent-0001"):
            pass


def test_native_workspace_accepts_a_fully_allocated_normal_file_with_minimal_extents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "research"
    source.mkdir()
    normal_file = source / "normal.bin"
    normal_file.write_bytes(os.urandom(8193))
    status = normal_file.stat()
    if status.st_blocks * 512 < status.st_size:
        pytest.skip("filesystem does not report full allocation for a normal file")
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)

    def minimal_extent_map(_descriptor: int, _offset: int, whence: int) -> int:
        return 0 if whence == os.SEEK_DATA else status.st_size

    monkeypatch.setattr(native_workspace_module, "_seek_extent", minimal_extent_map)

    with _prepare(source, private_root, "native-source-normal-allocation-0001") as prepared:
        assert prepared.import_ref.entry_count == 1


@pytest.mark.skipif(sys.platform != "darwin", reason="requires the macOS APFS runner")
def test_macos_apfs_accepts_a_normal_allocated_file(tmp_path: Path) -> None:
    source = tmp_path / "apfs-normal"
    source.mkdir()
    normal_file = source / "normal.bin"
    normal_file.write_bytes(os.urandom(2 * 1024 * 1024 + 17))
    status = normal_file.stat()
    assert status.st_blocks * 512 >= status.st_size
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)

    with _prepare(source, private_root, "native-source-apfs-normal-0001") as prepared:
        assert prepared.import_ref.entry_count == 1
        assert prepared.import_ref.extracted_byte_size == status.st_size


@pytest.mark.skipif(sys.platform != "darwin", reason="requires the macOS APFS runner")
def test_macos_apfs_rejects_a_low_allocation_sparse_file(tmp_path: Path) -> None:
    source = tmp_path / "apfs-sparse"
    source.mkdir()
    sparse_file = source / "sparse.bin"
    with sparse_file.open("w+b") as stream:
        stream.write(b"x" * 4096)
        stream.truncate(8 * 1024 * 1024)
    status = sparse_file.stat()
    if status.st_blocks * 512 >= status.st_size:
        pytest.skip("APFS allocated the complete logical file")
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)

    with pytest.raises(NativeWorkspaceArchiveError, match="allocation"):
        with _prepare(source, private_root, "native-source-apfs-sparse-0001"):
            pass


def test_native_workspace_directory_enumeration_is_bounded_before_sort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "research"
    source.mkdir()
    descriptor = os.open(source, native_workspace_module._DIRECTORY_FLAGS)
    calls = 0

    class Entry:
        name = "entry"

    class Iterator:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal calls
            calls += 1
            if calls > 4:
                raise AssertionError("directory enumeration exceeded remaining budget plus one")
            return Entry()

    monkeypatch.setattr(native_workspace_module, "MAX_WORKSPACE_ENTRIES", 3)
    monkeypatch.setattr(native_workspace_module.os, "scandir", lambda _descriptor: Iterator())
    try:
        with pytest.raises(NativeWorkspaceArchiveError, match="entry budget"):
            native_workspace_module._scan(descriptor)
    finally:
        os.close(descriptor)

    assert calls == 4


def test_native_workspace_real_directories_share_one_global_enumeration_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "research"
    source.mkdir()
    (source / "a").mkdir()
    (source / "b").mkdir()
    (source / "c").mkdir()
    (source / "a" / "one").touch()
    (source / "a" / "two").touch()
    (source / "a" / "three").touch()
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)
    monkeypatch.setattr(native_workspace_module, "MAX_WORKSPACE_ENTRIES", 4)
    real_scandir = os.scandir
    yielded = 0

    class CountingScandir:
        def __init__(self, descriptor: int) -> None:
            self.context = real_scandir(descriptor)
            self.iterator = None

        def __enter__(self):
            self.iterator = self.context.__enter__()
            return self

        def __exit__(self, *args: object):
            return self.context.__exit__(*args)

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal yielded
            assert self.iterator is not None
            entry = next(self.iterator)
            yielded += 1
            return entry

    monkeypatch.setattr(native_workspace_module.os, "scandir", CountingScandir)

    with pytest.raises(NativeWorkspaceArchiveError, match="entry budget"):
        with _prepare(source, private_root, "native-source-global-entry-budget-0001"):
            pass

    assert yielded == 5


def test_native_workspace_detects_inventory_change_before_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "research"
    source.mkdir()
    original = source / "notes.txt"
    original.write_text("stable", encoding="utf-8")
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)

    def mutate(_root_descriptor: int) -> None:
        (source / "late.txt").write_text("late", encoding="utf-8")

    monkeypatch.setattr(native_workspace_module, "_after_archive_write", mutate)

    with pytest.raises(NativeWorkspaceArchiveError, match="workspace changed"):
        with _prepare(source, private_root, "native-source-action-0003"):
            pass


def test_native_workspace_rejects_a_selected_path_rebound_to_another_inode(
    tmp_path: Path,
) -> None:
    selected = tmp_path / "research"
    selected.mkdir()
    (selected / "original.txt").write_text("original", encoding="utf-8")
    selected_identity = selected.stat()
    displaced = tmp_path / "research-displaced"
    selected.rename(displaced)
    selected.mkdir()
    (selected / "replacement.txt").write_text("replacement", encoding="utf-8")
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)

    with pytest.raises(NativeWorkspaceArchiveError):
        with prepare_native_workspace(
            str(selected.resolve()),
            import_id=native_import_id_for_action("native-source-action-identity-0001"),
            temporary_root=private_root,
            expected_device=selected_identity.st_dev,
            expected_inode=selected_identity.st_ino,
        ):
            pass


def test_native_workspace_rejects_reusing_an_import_id_for_changed_content(
    tmp_path: Path,
) -> None:
    source = tmp_path / "research"
    source.mkdir()
    content = source / "notes.txt"
    content.write_text("first", encoding="utf-8")
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)
    store = WorkspaceImportStore(private_root / "imports")
    action = "native-source-action-0004"

    with _prepare(source, private_root, action) as prepared:
        first_ref = prepared.import_ref
        store.ingest(
            prepared.stream,
            ownership=ownership_for_native_import(first_ref),
            import_id=first_ref.import_id,
        )
    content.write_text("second", encoding="utf-8")
    content.chmod(stat.S_IRUSR | stat.S_IWUSR)

    with _prepare(source, private_root, action) as changed:
        with pytest.raises(WorkspaceImportIntegrityError, match="reused for different content"):
            store.ingest(
                changed.stream,
                ownership=ownership_for_native_import(changed.import_ref),
                import_id=changed.import_ref.import_id,
            )


@pytest.mark.parametrize(
    "action",
    ["short", " leading-native-action", "native-action-trailing ", "native-action\n0001"],
)
def test_native_workspace_action_identity_is_closed(action: str) -> None:
    with pytest.raises(ValueError):
        native_import_id_for_action(action)
