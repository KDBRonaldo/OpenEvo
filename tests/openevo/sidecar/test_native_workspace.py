from __future__ import annotations

import os
from pathlib import Path
import stat

import pytest

from desktop.sidecar import native_workspace as native_workspace_module
from desktop.sidecar.native_workspace import (
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
    with (sparse / "sparse.bin").open("wb") as stream:
        stream.seek(1024 * 1024 - 1)
        stream.write(b"\0")
    cases.append(sparse)

    for index, source in enumerate(cases):
        with pytest.raises(NativeWorkspaceArchiveError):
            with _prepare(source, private_root, f"native-source-invalid-{index:04d}"):
                pass


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
