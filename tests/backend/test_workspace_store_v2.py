from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3

import pytest

from openevo.backend.contracts.v2.models import (
    WorkspaceArchiveDeclarationV2,
    WorkspaceUploadAbortV2,
    WorkspaceUploadCreateV2,
    WorkspaceUploadFinalizeV2,
)
from openevo.backend.contracts.v2.snapshots import canonical_contract_bytes
import openevo.backend.workspace_store_v2 as workspace_module
from openevo.backend.workspace_store_v2 import (
    WorkspaceConflictV2,
    WorkspaceIdempotencyConflictV2,
    WorkspaceIntegrityErrorV2,
    WorkspacePreconditionFailedV2,
    WorkspaceStoreV2,
)


class _Clock:
    def __init__(self) -> None:
        self._next = datetime(2026, 7, 23, 3, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        value = self._next
        self._next += timedelta(microseconds=1)
        return value


def _tar_header(
    path: str,
    *,
    body_size: int,
    directory: bool = False,
    type_flag: bytes | None = None,
) -> bytes:
    encoded = (path + "/" if directory else path).encode("utf-8")
    if len(encoded) <= 100:
        name, prefix = encoded, b""
    else:
        split = max(
            index
            for index, value in enumerate(encoded)
            if value == ord("/") and index <= 155 and len(encoded) - index - 1 <= 100
        )
        prefix, name = encoded[:split], encoded[split + 1 :]
    header = bytearray(512)
    header[0 : len(name)] = name
    header[100:108] = b"0000755\0" if directory else b"0000644\0"
    header[108:116] = b"0000000\0"
    header[116:124] = b"0000000\0"
    header[124:136] = f"{body_size:011o}\0".encode("ascii")
    header[136:148] = b"00000000000\0"
    header[148:156] = b"        "
    header[156:157] = type_flag or (b"5" if directory else b"0")
    header[257:263] = b"ustar\0"
    header[263:265] = b"00"
    header[329:337] = b"0000000\0"
    header[337:345] = b"0000000\0"
    header[345 : 345 + len(prefix)] = prefix
    checksum = sum(header)
    header[148:156] = f"{checksum:06o}\0 ".encode("ascii")
    return bytes(header)


def _archive(*, forbidden_type: bytes | None = None) -> tuple[bytes, int, int]:
    body = b"OpenEvo v2 workspace\n"
    padding = b"\0" * ((512 - len(body) % 512) % 512)
    archive = b"".join(
        (
            _tar_header("src", body_size=0, directory=True),
            _tar_header(
                "src/AGENTS.md",
                body_size=len(body),
                type_flag=forbidden_type,
            ),
            body,
            padding,
            b"\0" * 1024,
        )
    )
    return archive, 2, len(body)


def _request(archive: bytes, *, entries: int, extracted: int) -> WorkspaceUploadCreateV2:
    chunk_size = 1024
    return WorkspaceUploadCreateV2(
        expected_project_head_id=None,
        expected_project_head_manifest_sha256=None,
        expected_project_config_sha256="a" * 64,
        archive=WorkspaceArchiveDeclarationV2(
            format="openevo_deterministic_tar_v1",
            media_type="application/vnd.openevo.workspace-tar",
            content_sha256=hashlib.sha256(archive).hexdigest(),
            byte_size=len(archive),
            entry_count=entries,
            extracted_byte_size=extracted,
        ),
        chunk_byte_size=chunk_size,
        chunk_count=(len(archive) + chunk_size - 1) // chunk_size,
    )


def _upload_all(
    store: WorkspaceStoreV2,
    archive: bytes,
    request: WorkspaceUploadCreateV2,
    *,
    clock: _Clock,
):
    session, replayed = store.create_upload(
        "project-1",
        request,
        idempotency_key="create-upload",
        now=clock(),
    )
    assert replayed is False
    for index in range(request.chunk_count):
        chunk = archive[
            index * request.chunk_byte_size : (index + 1) * request.chunk_byte_size
        ]
        session, replayed = store.put_chunk(
            "project-1",
            session.upload_id,
            chunk_index=index,
            chunk=chunk,
            chunk_sha256=hashlib.sha256(chunk).hexdigest(),
            chunk_byte_size=len(chunk),
            if_match=session.etag,
            idempotency_key=f"chunk-{index}",
            now=clock(),
        )
        assert replayed is False
    return session


def test_scratch_snapshot_is_private_content_addressed_and_recoverable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace-v2"
    store = WorkspaceStoreV2(root)
    snapshot = store.ensure_empty_snapshot("project-1")
    assert snapshot.entry_count == 0
    assert snapshot.byte_size == 0
    assert store.ensure_empty_snapshot("project-1") == snapshot
    snapshot_path = store.snapshot_path(snapshot)
    assert snapshot_path.is_dir()
    assert list(snapshot_path.iterdir()) == []
    assert (root.stat().st_mode & 0o777) == 0o700
    assert (root / "workspace-store-v2.sqlite3").stat().st_mode & 0o777 == 0o600
    assert (root / ".workspace-store-v2.identity.json").stat().st_mode & 0o777 == 0o600
    store.close()

    restarted = WorkspaceStoreV2(root)
    try:
        assert restarted.get_snapshot(snapshot.workspace_snapshot_id) == snapshot
        assert restarted.snapshot_path(snapshot).is_dir()
    finally:
        restarted.close()


def test_chunked_upload_finalizes_exact_archive_and_survives_restart(
    tmp_path: Path,
) -> None:
    archive, entries, extracted = _archive()
    request = _request(archive, entries=entries, extracted=extracted)
    root = tmp_path / "workspace-v2"
    clock = _Clock()
    store = WorkspaceStoreV2(root)
    session = _upload_all(store, archive, request, clock=clock)
    assert session.accepted_byte_size == len(archive)

    finalized, replayed = store.finalize_upload(
        "project-1",
        session.upload_id,
        WorkspaceUploadFinalizeV2(expected_content_sha256=request.archive.content_sha256),
        if_match=session.etag,
        idempotency_key="finalize-upload",
        now=clock(),
    )
    assert replayed is False
    assert finalized.state == "finalized"
    assert finalized.workspace_snapshot is not None
    snapshot = finalized.workspace_snapshot
    assert snapshot.entry_count == entries
    assert snapshot.byte_size == extracted
    assert (store.snapshot_path(snapshot) / "src" / "AGENTS.md").read_bytes() == (
        b"OpenEvo v2 workspace\n"
    )

    replay, replayed = store.finalize_upload(
        "project-1",
        session.upload_id,
        WorkspaceUploadFinalizeV2(expected_content_sha256=request.archive.content_sha256),
        if_match=session.etag,
        idempotency_key="finalize-upload",
        now=clock(),
    )
    assert replayed is True
    assert replay == finalized
    store.close()

    restarted = WorkspaceStoreV2(root)
    try:
        assert restarted.get_upload("project-1", session.upload_id) == finalized
        assert (restarted.snapshot_path(snapshot) / "src" / "AGENTS.md").is_file()
    finally:
        restarted.close()


def test_chunk_integrity_etag_order_and_idempotency_fail_closed(tmp_path: Path) -> None:
    archive, entries, extracted = _archive()
    request = _request(archive, entries=entries, extracted=extracted)
    clock = _Clock()
    store = WorkspaceStoreV2(tmp_path / "workspace-v2")
    session, _ = store.create_upload(
        "project-1",
        request,
        idempotency_key="create-upload",
        now=clock(),
    )
    first = archive[: request.chunk_byte_size]
    try:
        with pytest.raises(WorkspacePreconditionFailedV2):
            store.put_chunk(
                "project-1",
                session.upload_id,
                chunk_index=1,
                chunk=first,
                chunk_sha256=hashlib.sha256(first).hexdigest(),
                chunk_byte_size=len(first),
                if_match=session.etag,
                idempotency_key="wrong-index",
                now=clock(),
            )
        with pytest.raises(WorkspaceIntegrityErrorV2):
            store.put_chunk(
                "project-1",
                session.upload_id,
                chunk_index=0,
                chunk=first,
                chunk_sha256="f" * 64,
                chunk_byte_size=len(first),
                if_match=session.etag,
                idempotency_key="wrong-digest",
                now=clock(),
            )
        accepted, _ = store.put_chunk(
            "project-1",
            session.upload_id,
            chunk_index=0,
            chunk=first,
            chunk_sha256=hashlib.sha256(first).hexdigest(),
            chunk_byte_size=len(first),
            if_match=session.etag,
            idempotency_key="chunk-0",
            now=clock(),
        )
        replay, replayed = store.put_chunk(
            "project-1",
            session.upload_id,
            chunk_index=0,
            chunk=first,
            chunk_sha256=hashlib.sha256(first).hexdigest(),
            chunk_byte_size=len(first),
            if_match=session.etag,
            idempotency_key="chunk-0",
            now=clock(),
        )
        assert replayed is True
        assert replay == accepted
        with pytest.raises(WorkspaceIdempotencyConflictV2):
            store.put_chunk(
                "project-1",
                session.upload_id,
                chunk_index=1,
                chunk=b"changed",
                chunk_sha256=hashlib.sha256(b"changed").hexdigest(),
                chunk_byte_size=len(b"changed"),
                if_match=session.etag,
                idempotency_key="chunk-0",
                now=clock(),
            )
    finally:
        store.close()


def test_create_and_chunk_replay_preserve_the_exact_historical_response(
    tmp_path: Path,
) -> None:
    archive, entries, extracted = _archive()
    request = _request(archive, entries=entries, extracted=extracted)
    clock = _Clock()
    root = tmp_path / "workspace-v2"
    store = WorkspaceStoreV2(root)
    created, _ = store.create_upload(
        "project-1",
        request,
        idempotency_key="create-upload",
        now=clock(),
    )
    first = archive[: request.chunk_byte_size]
    accepted_first, _ = store.put_chunk(
        "project-1",
        created.upload_id,
        chunk_index=0,
        chunk=first,
        chunk_sha256=hashlib.sha256(first).hexdigest(),
        chunk_byte_size=len(first),
        if_match=created.etag,
        idempotency_key="chunk-0",
        now=clock(),
    )
    second = archive[request.chunk_byte_size : 2 * request.chunk_byte_size]
    accepted_second, _ = store.put_chunk(
        "project-1",
        created.upload_id,
        chunk_index=1,
        chunk=second,
        chunk_sha256=hashlib.sha256(second).hexdigest(),
        chunk_byte_size=len(second),
        if_match=accepted_first.etag,
        idempotency_key="chunk-1",
        now=clock(),
    )
    assert accepted_second != accepted_first

    replayed_create, create_replay = store.create_upload(
        "project-1",
        request,
        idempotency_key="create-upload",
        now=clock(),
    )
    replayed_first, chunk_replay = store.put_chunk(
        "project-1",
        created.upload_id,
        chunk_index=0,
        chunk=first,
        chunk_sha256=hashlib.sha256(first).hexdigest(),
        chunk_byte_size=len(first),
        if_match=created.etag,
        idempotency_key="chunk-0",
        now=clock(),
    )
    assert create_replay is True
    assert chunk_replay is True
    assert replayed_create == created
    assert replayed_first == accepted_first
    store.close()

    restarted = WorkspaceStoreV2(root)
    try:
        replayed_first, chunk_replay = restarted.put_chunk(
            "project-1",
            created.upload_id,
            chunk_index=0,
            chunk=first,
            chunk_sha256=hashlib.sha256(first).hexdigest(),
            chunk_byte_size=len(first),
            if_match=created.etag,
            idempotency_key="chunk-0",
            now=clock(),
        )
        assert chunk_replay is True
        assert replayed_first == accepted_first
    finally:
        restarted.close()


@pytest.mark.parametrize("entry_type", [b"1", b"2", b"3", b"4", b"6"])
def test_finalize_rejects_hardlinks_symlinks_and_special_entries(
    tmp_path: Path,
    entry_type: bytes,
) -> None:
    archive, entries, extracted = _archive(forbidden_type=entry_type)
    request = _request(archive, entries=entries, extracted=extracted)
    clock = _Clock()
    store = WorkspaceStoreV2(tmp_path / "workspace-v2")
    session = _upload_all(store, archive, request, clock=clock)
    try:
        with pytest.raises(WorkspaceIntegrityErrorV2):
            store.finalize_upload(
                "project-1",
                session.upload_id,
                WorkspaceUploadFinalizeV2(
                    expected_content_sha256=request.archive.content_sha256
                ),
                if_match=session.etag,
                idempotency_key="finalize-upload",
                now=clock(),
            )
        assert store.get_upload("project-1", session.upload_id).state == "open"
    finally:
        store.close()


def test_chunk_crash_recovery_truncates_only_uncommitted_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive, entries, extracted = _archive()
    request = _request(archive, entries=entries, extracted=extracted)
    clock = _Clock()
    root = tmp_path / "workspace-v2"
    store = WorkspaceStoreV2(root)
    session, _ = store.create_upload(
        "project-1",
        request,
        idempotency_key="create-upload",
        now=clock(),
    )
    first = archive[: request.chunk_byte_size]

    def crash(*_args: object) -> None:
        raise SystemExit("simulated chunk crash")

    monkeypatch.setattr(workspace_module, "_after_chunk_write_before_commit", crash)
    with pytest.raises(SystemExit):
        store.put_chunk(
            "project-1",
            session.upload_id,
            chunk_index=0,
            chunk=first,
            chunk_sha256=hashlib.sha256(first).hexdigest(),
            chunk_byte_size=len(first),
            if_match=session.etag,
            idempotency_key="chunk-0",
            now=clock(),
        )
    store.close()
    monkeypatch.setattr(
        workspace_module,
        "_after_chunk_write_before_commit",
        lambda *_args: None,
    )

    restarted = WorkspaceStoreV2(root)
    try:
        recovered = restarted.get_upload("project-1", session.upload_id)
        assert recovered.next_chunk_index == 0
        assert recovered.accepted_byte_size == 0
        upload_path = root / "uploads" / f"{session.upload_id}.part"
        assert upload_path.stat().st_size == 0
    finally:
        restarted.close()


def test_abort_is_idempotent_and_prevents_more_chunks(tmp_path: Path) -> None:
    archive, entries, extracted = _archive()
    request = _request(archive, entries=entries, extracted=extracted)
    clock = _Clock()
    store = WorkspaceStoreV2(tmp_path / "workspace-v2")
    session, _ = store.create_upload(
        "project-1",
        request,
        idempotency_key="create-upload",
        now=clock(),
    )
    aborted, replayed = store.abort_upload(
        "project-1",
        session.upload_id,
        WorkspaceUploadAbortV2(reason="user_cancelled"),
        if_match=session.etag,
        idempotency_key="abort-upload",
        now=clock(),
    )
    assert replayed is False
    assert aborted.state == "aborted"
    replay, replayed = store.abort_upload(
        "project-1",
        session.upload_id,
        WorkspaceUploadAbortV2(reason="user_cancelled"),
        if_match=session.etag,
        idempotency_key="abort-upload",
        now=clock(),
    )
    assert replayed is True
    assert replay == aborted
    first = archive[: request.chunk_byte_size]
    with pytest.raises(WorkspacePreconditionFailedV2):
        store.put_chunk(
            "project-1",
            session.upload_id,
            chunk_index=0,
            chunk=first,
            chunk_sha256=hashlib.sha256(first).hexdigest(),
            chunk_byte_size=len(first),
            if_match=aborted.etag,
            idempotency_key="chunk-after-abort",
            now=clock(),
        )
    store.close()


def test_startup_rejects_marker_schema_and_snapshot_path_tamper(tmp_path: Path) -> None:
    for damage in ("marker", "schema", "snapshot"):
        root = tmp_path / damage
        store = WorkspaceStoreV2(root)
        snapshot = store.ensure_empty_snapshot("project-1")
        store.close()
        if damage == "marker":
            (root / ".workspace-store-v2.identity.json").write_bytes(b"{}")
            os.chmod(root / ".workspace-store-v2.identity.json", 0o600)
        elif damage == "schema":
            with sqlite3.connect(root / "workspace-store-v2.sqlite3") as connection:
                connection.execute("CREATE TABLE unexpected(value TEXT) STRICT")
                connection.commit()
        else:
            path = root / "snapshots" / snapshot.workspace_snapshot_id
            path.rmdir()
            path.symlink_to(root / "uploads", target_is_directory=True)
        with pytest.raises(WorkspaceIntegrityErrorV2):
            WorkspaceStoreV2(root)


def test_declared_upload_budget_is_cumulative_and_not_refunded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive, entries, extracted = _archive()
    request = _request(archive, entries=entries, extracted=extracted)
    monkeypatch.setattr(
        workspace_module,
        "_MAX_CUMULATIVE_ARCHIVE_BYTES",
        request.archive.byte_size,
    )
    clock = _Clock()
    store = WorkspaceStoreV2(tmp_path / "workspace-v2")
    first, _ = store.create_upload(
        "project-1",
        request,
        idempotency_key="first",
        now=clock(),
    )
    store.abort_upload(
        "project-1",
        first.upload_id,
        WorkspaceUploadAbortV2(reason="user_cancelled"),
        if_match=first.etag,
        idempotency_key="abort-first",
        now=clock(),
    )
    with pytest.raises(WorkspaceConflictV2, match="cumulative budget"):
        store.create_upload(
            "project-1",
            request,
            idempotency_key="second",
            now=clock(),
        )
    store.close()


def test_finalize_rejects_duplicate_archive_entry(tmp_path: Path) -> None:
    body = b"duplicate\n"
    padding = b"\0" * ((512 - len(body) % 512) % 512)
    archive = b"".join(
        (
            _tar_header("same.txt", body_size=len(body)),
            body,
            padding,
            _tar_header("same.txt", body_size=len(body)),
            body,
            padding,
            b"\0" * 1024,
        )
    )
    request = _request(archive, entries=2, extracted=2 * len(body))
    clock = _Clock()
    store = WorkspaceStoreV2(tmp_path / "workspace-v2")
    session = _upload_all(store, archive, request, clock=clock)
    with pytest.raises(WorkspaceIntegrityErrorV2):
        store.finalize_upload(
            "project-1",
            session.upload_id,
            WorkspaceUploadFinalizeV2(
                expected_content_sha256=request.archive.content_sha256
            ),
            if_match=session.etag,
            idempotency_key="finalize-upload",
            now=clock(),
        )
    store.close()


def test_finalize_never_replaces_an_existing_snapshot_destination(
    tmp_path: Path,
) -> None:
    archive, entries, extracted = _archive()
    request = _request(archive, entries=entries, extracted=extracted)
    clock = _Clock()
    root = tmp_path / "workspace-v2"
    store = WorkspaceStoreV2(root)
    session = _upload_all(store, archive, request, clock=clock)
    snapshot = workspace_module._snapshot_for("project-1", request.archive)
    destination = root / "snapshots" / snapshot.workspace_snapshot_id
    destination.mkdir(mode=0o700)
    sentinel = destination / "do-not-replace"
    sentinel.write_bytes(b"authority-race")
    with pytest.raises(WorkspaceIntegrityErrorV2):
        store.finalize_upload(
            "project-1",
            session.upload_id,
            WorkspaceUploadFinalizeV2(
                expected_content_sha256=request.archive.content_sha256
            ),
            if_match=session.etag,
            idempotency_key="finalize-upload",
            now=clock(),
        )
    assert sentinel.read_bytes() == b"authority-race"
    store.close()


def test_snapshot_publication_crash_is_verified_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive, entries, extracted = _archive()
    request = _request(archive, entries=entries, extracted=extracted)
    clock = _Clock()
    root = tmp_path / "workspace-v2"
    store = WorkspaceStoreV2(root)
    session = _upload_all(store, archive, request, clock=clock)

    def crash(*_args: object) -> None:
        raise SystemExit("simulated publication crash")

    monkeypatch.setattr(
        workspace_module,
        "_after_snapshot_publish_before_commit",
        crash,
    )
    with pytest.raises(SystemExit):
        store.finalize_upload(
            "project-1",
            session.upload_id,
            WorkspaceUploadFinalizeV2(
                expected_content_sha256=request.archive.content_sha256
            ),
            if_match=session.etag,
            idempotency_key="finalize-upload",
            now=clock(),
        )
    store.close()
    monkeypatch.setattr(
        workspace_module,
        "_after_snapshot_publish_before_commit",
        lambda *_args: None,
    )

    restarted = WorkspaceStoreV2(root)
    recovered = restarted.get_upload("project-1", session.upload_id)
    assert recovered.state == "open"
    finalized, replayed = restarted.finalize_upload(
        "project-1",
        session.upload_id,
        WorkspaceUploadFinalizeV2(
            expected_content_sha256=request.archive.content_sha256
        ),
        if_match=recovered.etag,
        idempotency_key="finalize-upload",
        now=clock(),
    )
    assert replayed is False
    assert finalized.state == "finalized"
    restarted.close()


def test_startup_rejects_tampered_committed_chunk_and_uncommitted_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive, entries, extracted = _archive()
    request = _request(archive, entries=entries, extracted=extracted)
    clock = _Clock()

    chunk_root = tmp_path / "chunk"
    store = WorkspaceStoreV2(chunk_root)
    session, _ = store.create_upload(
        "project-1",
        request,
        idempotency_key="create-upload",
        now=clock(),
    )
    first = archive[: request.chunk_byte_size]
    session, _ = store.put_chunk(
        "project-1",
        session.upload_id,
        chunk_index=0,
        chunk=first,
        chunk_sha256=hashlib.sha256(first).hexdigest(),
        chunk_byte_size=len(first),
        if_match=session.etag,
        idempotency_key="chunk-0",
        now=clock(),
    )
    store.close()
    archive_path = chunk_root / "uploads" / f"{session.upload_id}.part"
    with archive_path.open("r+b") as stream:
        stream.write(b"X")
    with pytest.raises(WorkspaceIntegrityErrorV2):
        WorkspaceStoreV2(chunk_root)

    snapshot_root = tmp_path / "snapshot"
    store = WorkspaceStoreV2(snapshot_root)
    session = _upload_all(store, archive, request, clock=clock)

    def crash(*_args: object) -> None:
        raise SystemExit("simulated publication crash")

    monkeypatch.setattr(
        workspace_module,
        "_after_snapshot_publish_before_commit",
        crash,
    )
    with pytest.raises(SystemExit):
        store.finalize_upload(
            "project-1",
            session.upload_id,
            WorkspaceUploadFinalizeV2(
                expected_content_sha256=request.archive.content_sha256
            ),
            if_match=session.etag,
            idempotency_key="finalize-upload",
            now=clock(),
        )
    store.close()
    snapshot = workspace_module._snapshot_for("project-1", request.archive)
    target = snapshot_root / "snapshots" / snapshot.workspace_snapshot_id
    (target / "src" / "AGENTS.md").write_bytes(b"tampered\n")
    with pytest.raises(WorkspaceIntegrityErrorV2):
        WorkspaceStoreV2(snapshot_root)


def test_scratch_snapshot_publication_recovers_after_directory_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workspace-v2"
    store = WorkspaceStoreV2(root)

    def crash(*_args: object) -> None:
        raise SystemExit("simulated scratch publication crash")

    monkeypatch.setattr(
        workspace_module,
        "_after_empty_snapshot_publish_before_commit",
        crash,
    )
    with pytest.raises(SystemExit):
        store.ensure_empty_snapshot("project-1")
    store.close()
    monkeypatch.setattr(
        workspace_module,
        "_after_empty_snapshot_publish_before_commit",
        lambda *_args: None,
    )

    restarted = WorkspaceStoreV2(root)
    try:
        snapshot = restarted.ensure_empty_snapshot("project-1")
        assert restarted.get_snapshot(snapshot.workspace_snapshot_id) == snapshot
        assert list(restarted.snapshot_path(snapshot).iterdir()) == []
    finally:
        restarted.close()


def test_startup_rejects_unknown_root_state_and_broken_database_symlink(
    tmp_path: Path,
) -> None:
    root = tmp_path / "unknown"
    store = WorkspaceStoreV2(root)
    store.close()
    extra = root / "unmanaged"
    extra.write_bytes(b"do not claim")
    os.chmod(extra, 0o600)
    with pytest.raises(WorkspaceIntegrityErrorV2):
        WorkspaceStoreV2(root)
    assert extra.read_bytes() == b"do not claim"

    symlink_root = tmp_path / "symlink"
    symlink_root.mkdir(mode=0o700)
    outside = tmp_path / "must-not-be-created.sqlite3"
    (symlink_root / "workspace-store-v2.sqlite3").symlink_to(outside)
    with pytest.raises(WorkspaceIntegrityErrorV2):
        WorkspaceStoreV2(symlink_root)
    assert not outside.exists()


def test_marker_binds_database_inode_and_pending_identity_rejects_managed_state(
    tmp_path: Path,
) -> None:
    replaced_root = tmp_path / "replaced-database"
    store = WorkspaceStoreV2(replaced_root)
    store.close()
    database = replaced_root / "workspace-store-v2.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    shutil.copy2(database, replacement)
    os.replace(replacement, database)
    with pytest.raises(WorkspaceIntegrityErrorV2):
        WorkspaceStoreV2(replaced_root)

    pending_root = tmp_path / "pending-managed-state"
    store = WorkspaceStoreV2(pending_root)
    store.close()
    snapshot = workspace_module._snapshot_for(
        "project-1",
        workspace_module._empty_archive_declaration(),
    )
    with sqlite3.connect(pending_root / "workspace-store-v2.sqlite3") as connection:
        connection.execute(
            "UPDATE metadata SET binding_state = 'pending' WHERE singleton = 1"
        )
        connection.execute(
            "INSERT INTO empty_snapshot_publications("
            "workspace_snapshot_id, project_id, snapshot_json) VALUES (?, ?, ?)",
            (
                snapshot.workspace_snapshot_id,
                snapshot.project_id,
                canonical_contract_bytes(snapshot),
            ),
        )
        connection.commit()
    with pytest.raises(WorkspaceIntegrityErrorV2):
        WorkspaceStoreV2(pending_root)


@pytest.mark.parametrize("publication_step", ["before_link", "after_link"])
def test_pending_marker_publication_recovers_exact_owned_temporary_link(
    tmp_path: Path,
    publication_step: str,
) -> None:
    root = tmp_path / publication_step
    store = WorkspaceStoreV2(root)
    store.close()
    marker = root / ".workspace-store-v2.identity.json"
    temporary = root / (".workspace-marker-" + "a" * 32 + ".tmp")
    with sqlite3.connect(root / "workspace-store-v2.sqlite3") as connection:
        connection.execute(
            "UPDATE metadata SET binding_state = 'pending' WHERE singleton = 1"
        )
        connection.commit()
    if publication_step == "before_link":
        marker.rename(temporary)
    else:
        os.link(marker, temporary)

    recovered = WorkspaceStoreV2(root)
    recovered.close()
    assert marker.is_file()
    assert not temporary.exists()
    with sqlite3.connect(root / "workspace-store-v2.sqlite3") as connection:
        assert connection.execute(
            "SELECT binding_state FROM metadata WHERE singleton = 1"
        ).fetchone()[0] == "bound"
