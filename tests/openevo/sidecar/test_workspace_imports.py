from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import errno
import fcntl
import hashlib
import io
import itertools
import json
import multiprocessing
import os
from pathlib import Path
import stat
import threading
import unicodedata

import pytest

from desktop.sidecar.contracts.v1 import WorkspaceImportRefV1
from desktop.sidecar import workspace_imports as workspace_imports_module
from desktop.sidecar.workspace_imports import (
    PendingWorkspaceImport,
    WorkspaceArchiveValidationError,
    WorkspaceImportCancelled,
    WorkspaceImportError,
    WorkspaceImportIntegrityError,
    WorkspaceImportNotFoundError,
    WorkspaceImportOwnership,
    WorkspaceImportStore,
    WorkspaceImportStoreConfigurationError,
)


BLOCK = 512
ZERO_BLOCK = bytes(BLOCK)
_OWNER_SEQUENCE = itertools.count()
_OWNERS_BY_IMPORT: dict[str, WorkspaceImportOwnership] = {}


def _put(header: bytearray, start: int, end: int, value: bytes) -> None:
    assert len(value) <= end - start
    header[start:end] = bytes(end - start)
    header[start : start + len(value)] = value


def _split_header_path(header_path: bytes) -> tuple[bytes, bytes]:
    if len(header_path) <= 100:
        return header_path, b""
    for index in range(len(header_path) - 1, -1, -1):
        prefix = header_path[:index]
        name = header_path[index + 1 :]
        if (
            header_path[index : index + 1] == b"/"
            and 1 <= len(prefix) <= 155
            and 1 <= len(name) <= 100
        ):
            return name, prefix
    raise ValueError("path cannot be encoded in ustar")


def _rewrite_checksum(header: bytearray) -> None:
    header[148:156] = b"        "
    checksum = sum(header)
    header[148:156] = f"{checksum:06o}\0 ".encode("ascii")


def _header(
    path: str | bytes,
    *,
    directory: bool,
    size: int = 0,
    mode: int = 0o644,
) -> bytes:
    logical = path.encode("utf-8") if isinstance(path, str) else path
    header_path = logical + (b"/" if directory else b"")
    name, prefix = _split_header_path(header_path)
    header = bytearray(BLOCK)
    _put(header, 0, 100, name)
    _put(header, 100, 108, f"{0o755 if directory else mode:07o}\0".encode("ascii"))
    _put(header, 108, 116, b"0000000\0")
    _put(header, 116, 124, b"0000000\0")
    _put(header, 124, 136, f"{size:011o}\0".encode("ascii"))
    _put(header, 136, 148, b"00000000000\0")
    _put(header, 148, 156, b"        ")
    _put(header, 156, 157, b"5" if directory else b"0")
    _put(header, 257, 263, b"ustar\0")
    _put(header, 263, 265, b"00")
    _put(header, 345, 500, prefix)
    _rewrite_checksum(header)
    return bytes(header)


def _archive(
    entries: list[tuple[str | bytes, bytes | None, int]],
    *,
    terminator: bytes = ZERO_BLOCK + ZERO_BLOCK,
) -> bytes:
    result = bytearray()
    for path, content, mode in entries:
        directory = content is None
        body = b"" if content is None else content
        result.extend(_header(path, directory=directory, size=len(body), mode=mode))
        result.extend(body)
        result.extend(bytes((-len(body)) % BLOCK))
    result.extend(terminator)
    return bytes(result)


def _mutate_header(
    archive: bytes,
    start: int,
    end: int,
    value: bytes,
    *,
    fix_checksum: bool = True,
) -> bytes:
    changed = bytearray(archive)
    _put(changed, start, end, value)
    if fix_checksum:
        header = bytearray(changed[:BLOCK])
        _rewrite_checksum(header)
        changed[:BLOCK] = header
    return bytes(changed)


def _source(tmp_path: Path, archive: bytes, *, name: str = "source.tar") -> Path:
    path = tmp_path / name
    path.write_bytes(archive)
    return path


def _new_ownership(*, project_id: str = "project-test") -> WorkspaceImportOwnership:
    sequence = next(_OWNER_SEQUENCE)
    return WorkspaceImportOwnership(
        project_id=project_id,
        operation_id=f"workspace-sync-{sequence}",
        idempotency_key=f"workspace-sync-idempotency-{sequence:016d}",
    )


def _ownership(import_ref: WorkspaceImportRefV1) -> WorkspaceImportOwnership:
    return _OWNERS_BY_IMPORT[import_ref.import_id]


def _ingest(
    store: WorkspaceImportStore,
    tmp_path: Path,
    archive: bytes,
    *,
    ownership: WorkspaceImportOwnership | None = None,
) -> WorkspaceImportRefV1:
    path = _source(tmp_path, archive)
    selected_ownership = ownership or _new_ownership()
    with path.open("rb", buffering=0) as stream:
        import_ref = store.ingest(stream, ownership=selected_ownership)
    _OWNERS_BY_IMPORT[import_ref.import_id] = selected_ownership
    return import_ref


def _simple_archive(content: bytes = b"hello") -> bytes:
    return _archive(
        [
            ("src", None, 0o755),
            ("src/main.py", content, 0o644),
        ]
    )


def _stored_directory(root: Path, import_ref: WorkspaceImportRefV1) -> Path:
    return root / import_ref.import_id


def _process_ingest(root: str, source: str, results: multiprocessing.Queue[object]) -> None:
    try:
        store = WorkspaceImportStore(root)
        ownership = WorkspaceImportOwnership(
            project_id="project-process",
            operation_id=f"workspace-sync-{Path(source).name}",
            idempotency_key=f"workspace-sync-process-{Path(source).name}-00000000",
        )
        with open(source, "rb", buffering=0) as stream:
            import_ref = store.ingest(stream, ownership=ownership)
        results.put(("ok", import_ref.import_id, import_ref.content_sha256))
    except BaseException as exc:
        results.put(("error", repr(exc)))


def _process_capacity_ingest(
    root: str,
    source: str,
    max_retained_imports: int,
    results: multiprocessing.Queue[object],
) -> None:
    try:
        store = WorkspaceImportStore(
            root,
            max_retained_imports=max_retained_imports,
        )
        ownership = WorkspaceImportOwnership(
            project_id="project-capacity",
            operation_id=f"workspace-sync-{Path(source).name}",
            idempotency_key=f"workspace-sync-capacity-{Path(source).name}-00000000",
        )
        with open(source, "rb", buffering=0) as stream:
            import_ref = store.ingest(stream, ownership=ownership)
        results.put(("ok", import_ref.import_id))
    except BaseException as exc:
        results.put(("error", repr(exc)))


def _process_initialize_store(
    root: str,
    results: multiprocessing.Queue[object],
    started: multiprocessing.synchronize.Event,
    finished: multiprocessing.synchronize.Event,
    entered_auth_write: multiprocessing.synchronize.Event | None = None,
    release_auth_write: multiprocessing.synchronize.Event | None = None,
) -> None:
    try:
        if entered_auth_write is not None and release_auth_write is not None:

            def hold_auth_write(kind: str, stage: str, _descriptor: int) -> None:
                if kind == "auth_key" and stage == "before_write":
                    entered_auth_write.set()
                    if not release_auth_write.wait(timeout=10):
                        raise TimeoutError("timed out waiting to release auth-key creation")

            workspace_imports_module._initialization_file_fault_point = hold_auth_write
        started.set()
        store = WorkspaceImportStore(root)
        identity = store._root_identity
        store.close()
        results.put(("ok", identity))
    except BaseException as exc:
        results.put(("error", repr(exc)))
    finally:
        finished.set()


def test_ingest_returns_exact_contract_ref_and_verified_handle(tmp_path: Path) -> None:
    root = tmp_path / "imports"
    store = WorkspaceImportStore(root)
    archive = _simple_archive()

    import_ref = _ingest(store, tmp_path, archive)

    assert type(import_ref) is WorkspaceImportRefV1
    assert import_ref.model_dump() == {
        "import_id": import_ref.import_id,
        "content_sha256": hashlib.sha256(archive).hexdigest(),
        "byte_size": len(archive),
        "entry_count": 2,
        "extracted_byte_size": 5,
    }
    assert import_ref.import_id.startswith("workspace-import-")
    assert import_ref.content_sha256 not in import_ref.import_id
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    stored = _stored_directory(root, import_ref)
    assert stat.S_IMODE(stored.stat().st_mode) == 0o700
    assert stat.S_IMODE((stored / "archive.tar").stat().st_mode) == 0o600
    assert stat.S_IMODE((stored / "metadata.json").stat().st_mode) == 0o600

    with store.resolve(import_ref, ownership=_ownership(import_ref)) as stream:
        assert isinstance(stream.name, int)
        assert stream.read() == archive
    assert stream.closed


def test_empty_archive_is_valid_and_exactly_two_blocks(tmp_path: Path) -> None:
    store = WorkspaceImportStore(tmp_path / "imports")
    archive = ZERO_BLOCK + ZERO_BLOCK

    import_ref = _ingest(store, tmp_path, archive)

    assert import_ref.entry_count == 0
    assert import_ref.extracted_byte_size == 0
    assert import_ref.byte_size == 1024


def test_same_content_gets_random_independent_import_ids(tmp_path: Path) -> None:
    store = WorkspaceImportStore(tmp_path / "imports")
    archive = _simple_archive()

    first = _ingest(store, tmp_path, archive)
    second = _ingest(store, tmp_path, archive)

    assert first.import_id != second.import_id
    assert first.content_sha256 == second.content_sha256


def test_ingest_accepts_an_integer_fd_without_changing_its_offset(tmp_path: Path) -> None:
    store = WorkspaceImportStore(tmp_path / "imports")
    path = _source(tmp_path, _simple_archive())
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.lseek(descriptor, 17, os.SEEK_SET)
        ownership = _new_ownership()
        import_ref = store.ingest(descriptor, ownership=ownership)
        _OWNERS_BY_IMPORT[import_ref.import_id] = ownership
        assert os.lseek(descriptor, 0, os.SEEK_CUR) == 17
    finally:
        os.close(descriptor)
    assert import_ref.entry_count == 2


@pytest.mark.parametrize("source", ["/host/path.tar", io.BytesIO(ZERO_BLOCK * 2), True])
def test_ingest_never_accepts_a_host_path_or_unverifiable_stream(
    tmp_path: Path,
    source: object,
) -> None:
    store = WorkspaceImportStore(tmp_path / "imports")

    with pytest.raises(TypeError):
        store.ingest(source, ownership=_new_ownership())  # type: ignore[arg-type]


def test_ingest_rejects_nonregular_and_symlink_descriptors(tmp_path: Path) -> None:
    store = WorkspaceImportStore(tmp_path / "imports")
    read_descriptor, write_descriptor = os.pipe()
    try:
        with pytest.raises(WorkspaceArchiveValidationError, match="regular file"):
            store.ingest(read_descriptor, ownership=_new_ownership())
    finally:
        os.close(read_descriptor)
        os.close(write_descriptor)

    if not hasattr(os, "O_PATH"):
        return
    target = _source(tmp_path, _simple_archive())
    link = tmp_path / "source-link"
    link.symlink_to(target)
    descriptor = os.open(link, os.O_PATH | os.O_NOFOLLOW)
    try:
        with pytest.raises(WorkspaceArchiveValidationError, match="regular file"):
            store.ingest(descriptor, ownership=_new_ownership())
    finally:
        os.close(descriptor)


def test_ingest_detects_source_identity_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = WorkspaceImportStore(tmp_path / "imports")
    path = _source(tmp_path, _simple_archive(b"x" * 2048))
    descriptor = os.open(path, os.O_RDWR)
    real_pread = workspace_imports_module.os.pread
    mutated = False

    def mutate_after_read(fd: int, count: int, offset: int) -> bytes:
        nonlocal mutated
        value = real_pread(fd, count, offset)
        if not mutated:
            os.pwrite(fd, b"X", 0)
            mutated = True
        return value

    monkeypatch.setattr(workspace_imports_module.os, "pread", mutate_after_read)
    try:
        with pytest.raises(WorkspaceArchiveValidationError, match="identity changed"):
            store.ingest(descriptor, ownership=_new_ownership())
    finally:
        os.close(descriptor)
    assert list((tmp_path / "imports").iterdir()) == []


@pytest.mark.parametrize(
    ("start", "end", "value", "message"),
    [
        (100, 108, b"0000777\0", "mode"),
        (100, 108, b"000644\0\0", "mode"),
        (108, 116, b"0000001\0", "uid/gid"),
        (116, 124, b"0000001\0", "uid/gid"),
        (124, 136, b"          5\0", "size"),
        (124, 136, b"0000000000A\0", "size"),
        (136, 148, b"00000000001\0", "mtime"),
        (156, 157, b"2", "entry type"),
        (157, 257, b"target", "POSIX ustar"),
        (257, 263, b"ustar ", "POSIX ustar"),
        (263, 265, b" \0", "POSIX ustar"),
        (265, 297, b"root", "unused header"),
        (297, 329, b"root", "unused header"),
        (329, 337, b"0000001\0", "unused header"),
        (337, 345, b"0000001\0", "unused header"),
        (500, 512, b"x", "unused header"),
    ],
)
def test_rejects_noncanonical_header_fields(
    tmp_path: Path,
    start: int,
    end: int,
    value: bytes,
    message: str,
) -> None:
    store = WorkspaceImportStore(tmp_path / "imports")
    malformed = _mutate_header(_archive([("file", b"hello", 0o644)]), start, end, value)

    with pytest.raises(WorkspaceArchiveValidationError, match=message):
        _ingest(store, tmp_path, malformed)


def test_rejects_checksum_mismatch(tmp_path: Path) -> None:
    store = WorkspaceImportStore(tmp_path / "imports")
    malformed = _mutate_header(
        _archive([("file", b"hello", 0o644)]),
        148,
        156,
        b"000000\0 ",
        fix_checksum=False,
    )

    with pytest.raises(WorkspaceArchiveValidationError, match="checksum"):
        _ingest(store, tmp_path, malformed)


@pytest.mark.parametrize(
    "path",
    [
        "/absolute",
        "./dot",
        "a/../escape",
        "a//empty",
        "back\\slash",
        "control\u0001name",
        unicodedata.normalize("NFD", "caf\u00e9"),
    ],
)
def test_rejects_unsafe_or_non_nfc_paths(tmp_path: Path, path: str) -> None:
    store = WorkspaceImportStore(tmp_path / "imports")

    with pytest.raises(WorkspaceArchiveValidationError, match="path|segment|NFC|control"):
        _ingest(store, tmp_path, _archive([(path, b"", 0o644)]))


def test_rejects_invalid_utf8_and_wrong_trailing_slash(tmp_path: Path) -> None:
    store = WorkspaceImportStore(tmp_path / "imports")
    invalid_utf8 = _archive([(b"bad-\xff", b"", 0o644)])
    with pytest.raises(WorkspaceArchiveValidationError, match="UTF-8"):
        _ingest(store, tmp_path, invalid_utf8)

    regular_with_slash = bytearray(_header("dir", directory=True))
    regular_with_slash[156:157] = b"0"
    _rewrite_checksum(regular_with_slash)
    with pytest.raises(WorkspaceArchiveValidationError, match="regular-file path"):
        _ingest(store, tmp_path, bytes(regular_with_slash) + ZERO_BLOCK * 2)

    directory_without_slash = bytearray(_header("dir", directory=False))
    directory_without_slash[100:108] = b"0000755\0"
    directory_without_slash[156:157] = b"5"
    _rewrite_checksum(directory_without_slash)
    with pytest.raises(WorkspaceArchiveValidationError, match="directory header path"):
        _ingest(store, tmp_path, bytes(directory_without_slash) + ZERO_BLOCK * 2)


def test_accepts_canonical_long_path_split_and_rejects_alternative_split(tmp_path: Path) -> None:
    store = WorkspaceImportStore(tmp_path / "imports")
    parent = "a" * 80
    child = "b" * 30
    archive = _archive([(parent, None, 0o755), (f"{parent}/{child}", b"x", 0o644)])
    assert _ingest(store, tmp_path, archive).entry_count == 2

    malformed = bytearray(_archive([("a", None, 0o755), ("a/b", b"x", 0o644)]))
    header = bytearray(malformed[BLOCK : 2 * BLOCK])
    _put(header, 0, 100, b"b")
    _put(header, 345, 500, b"a")
    _rewrite_checksum(header)
    malformed[BLOCK : 2 * BLOCK] = header
    with pytest.raises(WorkspaceArchiveValidationError, match="split|padding"):
        _ingest(store, tmp_path, bytes(malformed))


def test_rejects_missing_late_duplicate_and_unsorted_entries(tmp_path: Path) -> None:
    store = WorkspaceImportStore(tmp_path / "imports")
    malformed_archives = [
        _archive([("parent/child", b"x", 0o644)]),
        _archive([("parent/child", b"x", 0o644), ("parent", None, 0o755)]),
        _archive([("dup", b"x", 0o644), ("dup", b"y", 0o644)]),
        _archive([("z", b"x", 0o644), ("a", b"y", 0o644)]),
    ]

    for index, malformed in enumerate(malformed_archives):
        with pytest.raises(WorkspaceArchiveValidationError):
            _ingest(store, tmp_path, malformed)
        assert list((tmp_path / "imports").iterdir()) == [], index


def test_rejects_path_depth_and_accepts_exact_header_path_byte_limit(tmp_path: Path) -> None:
    store = WorkspaceImportStore(tmp_path / "imports")
    deep_entries: list[tuple[str, bytes | None, int]] = []
    for depth in range(1, 34):
        path = "/".join(["a"] * depth)
        deep_entries.append((path, None, 0o755))
    with pytest.raises(WorkspaceArchiveValidationError, match="depth"):
        _ingest(store, tmp_path, _archive(deep_entries))

    top = "a" * 55
    parent = f"{top}/{'c' * 99}"
    maximum_file = f"{parent}/{'b' * 100}"
    boundary = _archive([(top, None, 0o755), (parent, None, 0o755), (maximum_file, b"", 0o644)])
    assert _ingest(store, tmp_path, boundary).entry_count == 3


def test_rejects_nonzero_body_padding_and_bad_terminators(tmp_path: Path) -> None:
    store = WorkspaceImportStore(tmp_path / "imports")
    valid = _archive([("file", b"x", 0o644)])
    bad_padding = bytearray(valid)
    bad_padding[BLOCK + 1] = 1
    malformed = [
        bytes(bad_padding),
        valid[:-BLOCK],
        valid + ZERO_BLOCK,
        valid + b"trailing",
        ZERO_BLOCK + _header("late", directory=False) + ZERO_BLOCK,
    ]

    for value in malformed:
        with pytest.raises(WorkspaceArchiveValidationError):
            _ingest(store, tmp_path, value)


def test_enforces_archive_entry_and_extracted_byte_budgets(tmp_path: Path) -> None:
    archive = _archive([("a", b"12", 0o644), ("b", b"34", 0o644)])
    entry_limited = WorkspaceImportStore(tmp_path / "entries", max_entries=1)
    with pytest.raises(WorkspaceArchiveValidationError, match="entry budget"):
        _ingest(entry_limited, tmp_path, archive)

    extracted_limited = WorkspaceImportStore(tmp_path / "bytes", max_extracted_bytes=3)
    with pytest.raises(WorkspaceArchiveValidationError, match="extracted-byte"):
        _ingest(extracted_limited, tmp_path, archive)

    archive_limited = WorkspaceImportStore(tmp_path / "archive", max_archive_bytes=1024)
    with pytest.raises(WorkspaceArchiveValidationError, match="byte-size"):
        _ingest(archive_limited, tmp_path, archive)


def test_concurrent_ingests_of_same_and_different_archives_are_isolated(tmp_path: Path) -> None:
    store = WorkspaceImportStore(tmp_path / "imports")
    archives = [_simple_archive(bytes([index]) * (index + 1)) for index in range(8)]
    barrier = threading.Barrier(len(archives))

    def ingest_one(index: int) -> WorkspaceImportRefV1:
        path = _source(tmp_path, archives[index], name=f"source-{index}.tar")
        ownership = _new_ownership()
        with path.open("rb", buffering=0) as stream:
            barrier.wait()
            import_ref = store.ingest(stream, ownership=ownership)
        _OWNERS_BY_IMPORT[import_ref.import_id] = ownership
        return import_ref

    with ThreadPoolExecutor(max_workers=len(archives)) as executor:
        refs = list(executor.map(ingest_one, range(len(archives))))

    assert len({ref.import_id for ref in refs}) == len(archives)
    assert {ref.content_sha256 for ref in refs} == {
        hashlib.sha256(archive).hexdigest() for archive in archives
    }
    for import_ref, archive in zip(refs, archives, strict=True):
        with store.resolve(import_ref, ownership=_ownership(import_ref)) as stream:
            assert stream.read() == archive


def test_cross_process_lock_serializes_same_and_different_ingests(tmp_path: Path) -> None:
    root = tmp_path / "imports"
    WorkspaceImportStore(root)
    archives = [_simple_archive(b"same"), _simple_archive(b"same"), _simple_archive(b"other")]
    sources = [
        _source(tmp_path, archive, name=f"process-source-{index}.tar")
        for index, archive in enumerate(archives)
    ]
    context = multiprocessing.get_context("fork")
    results = context.Queue()
    processes = [
        context.Process(target=_process_ingest, args=(str(root), str(source), results))
        for source in sources
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0
    observed = [results.get(timeout=2) for _process in processes]

    assert all(result[0] == "ok" for result in observed)
    assert len({result[1] for result in observed}) == len(archives)
    assert [result[2] for result in observed].count(hashlib.sha256(archives[0]).hexdigest()) == 2


def test_retained_count_and_archive_byte_limits_are_exact_boundaries(tmp_path: Path) -> None:
    archive = _simple_archive()
    count_root = tmp_path / "count-imports"
    count_store = WorkspaceImportStore(
        count_root,
        max_retained_imports=2,
        max_retained_archive_bytes=3 * len(archive),
    )

    _ingest(count_store, tmp_path, archive)
    _ingest(count_store, tmp_path, archive)
    with pytest.raises(WorkspaceImportError, match="retained import count"):
        _ingest(count_store, tmp_path, archive)
    assert len(list(count_root.iterdir())) == 2
    WorkspaceImportStore(count_root)

    byte_root = tmp_path / "byte-imports"
    byte_store = WorkspaceImportStore(
        byte_root,
        max_retained_imports=3,
        max_retained_archive_bytes=2 * len(archive),
    )
    _ingest(byte_store, tmp_path, archive)
    _ingest(byte_store, tmp_path, archive)
    with pytest.raises(WorkspaceImportError, match="retained archive byte"):
        _ingest(byte_store, tmp_path, archive)
    assert len(list(byte_root.iterdir())) == 2


def test_pending_import_lease_adopts_and_discards_idempotently(tmp_path: Path) -> None:
    root = tmp_path / "imports"
    store = WorkspaceImportStore(root)
    ownership = _new_ownership()
    source = _source(tmp_path, _simple_archive())

    with source.open("rb", buffering=0) as stream:
        pending = store.ingest_pending(stream, ownership=ownership)

    assert isinstance(pending, PendingWorkspaceImport)
    assert pending.import_ref.model_dump().keys() == {
        "import_id",
        "content_sha256",
        "byte_size",
        "entry_count",
        "extracted_byte_size",
    }
    assert len(pending.lease_token) == 64
    assert pending.lease_token not in repr(pending)
    assert _stored_directory(root, pending.import_ref).is_dir()
    with pytest.raises(WorkspaceImportIntegrityError, match="lease token"):
        store.discard_pending(
            pending.import_ref,
            ownership=ownership,
            lease_token="0" * 64,
        )

    store.adopt_pending(pending.import_ref, ownership=ownership)
    store.adopt_pending(pending.import_ref, ownership=ownership)
    store.discard_pending(
        pending.import_ref,
        ownership=ownership,
        lease_token=pending.lease_token,
    )
    assert _stored_directory(root, pending.import_ref).is_dir()

    second_ownership = _new_ownership()
    with source.open("rb", buffering=0) as stream:
        abandoned = store.ingest_pending(stream, ownership=second_ownership)
    store.discard_pending(
        abandoned.import_ref,
        ownership=second_ownership,
        lease_token=abandoned.lease_token,
    )
    store.discard_pending(
        abandoned.import_ref,
        ownership=second_ownership,
        lease_token=abandoned.lease_token,
    )
    assert not _stored_directory(root, abandoned.import_ref).exists()


def test_pending_import_capacity_is_bounded_independently(tmp_path: Path) -> None:
    archive = _simple_archive()
    source = _source(tmp_path, archive)
    store = WorkspaceImportStore(
        tmp_path / "imports",
        max_retained_imports=3,
        max_retained_archive_bytes=3 * len(archive),
        max_pending_imports=1,
        max_pending_archive_bytes=len(archive),
    )
    first_ownership = _new_ownership()
    with source.open("rb", buffering=0) as stream:
        first = store.ingest_pending(stream, ownership=first_ownership)

    with source.open("rb", buffering=0) as stream:
        with pytest.raises(WorkspaceImportError, match="pending import count"):
            store.ingest_pending(stream, ownership=_new_ownership())

    store.adopt_pending(first.import_ref, ownership=first_ownership)
    with source.open("rb", buffering=0) as stream:
        second = store.ingest_pending(stream, ownership=_new_ownership())
    assert second.import_ref != first.import_ref


def test_retained_limits_reserve_complete_reconciliation_cost(tmp_path: Path) -> None:
    archive = _simple_archive()
    required_nodes, required_bytes = workspace_imports_module._required_reconcile_budget(
        1,
        len(archive),
    )
    root = tmp_path / "imports"
    options = {
        "max_retained_imports": 1,
        "max_retained_archive_bytes": len(archive),
        "reconcile_max_nodes": required_nodes,
        "reconcile_max_bytes": required_bytes,
    }

    store = WorkspaceImportStore(root, **options)
    _ingest(store, tmp_path, archive)
    WorkspaceImportStore(root, **options)

    with pytest.raises(ValueError, match="exceed reconciliation budgets"):
        WorkspaceImportStore(
            tmp_path / "node-short", **(options | {"reconcile_max_nodes": required_nodes - 1})
        )
    with pytest.raises(ValueError, match="exceed reconciliation budgets"):
        WorkspaceImportStore(
            tmp_path / "byte-short", **(options | {"reconcile_max_bytes": required_bytes - 1})
        )


def test_cross_process_capacity_check_cannot_overcommit_retained_count(tmp_path: Path) -> None:
    root = tmp_path / "imports"
    WorkspaceImportStore(root, max_retained_imports=2)
    archive = _simple_archive()
    sources = [
        _source(tmp_path, archive, name=f"capacity-source-{index}.tar") for index in range(3)
    ]
    context = multiprocessing.get_context("fork")
    results = context.Queue()
    processes = [
        context.Process(
            target=_process_capacity_ingest,
            args=(str(root), str(source), 2, results),
        )
        for source in sources
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0
    observed = [results.get(timeout=2) for _process in processes]

    assert [result[0] for result in observed].count("ok") == 2
    errors = [result[1] for result in observed if result[0] == "error"]
    assert len(errors) == 1
    assert "retained import count budget exceeded" in errors[0]
    assert len(list(root.iterdir())) == 2


@pytest.mark.parametrize(
    "hook", ["_after_archive_fsync", "_after_metadata_fsync", "_before_import_publish"]
)
def test_fault_before_publish_cleans_private_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hook: str,
) -> None:
    store = WorkspaceImportStore(tmp_path / "imports")

    def fail(*_args: object) -> None:
        raise OSError("injected fault")

    monkeypatch.setattr(workspace_imports_module, hook, fail)
    with pytest.raises(OSError, match="injected fault"):
        _ingest(store, tmp_path, _simple_archive())
    assert list((tmp_path / "imports").iterdir()) == []


def test_ingest_rollback_preserves_same_name_temporary_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "imports"
    store = WorkspaceImportStore(root)
    preserved_original = root / "preserved-original"
    replacement_marker_name = "replacement-marker"

    def replace_temporary_then_fail(*_args: object) -> None:
        temporary = next(root.iterdir())
        temporary.rename(preserved_original)
        temporary.mkdir(mode=0o700)
        (temporary / replacement_marker_name).write_text(
            "preserve replacement",
            encoding="ascii",
        )
        raise OSError("injected fault after temporary replacement")

    monkeypatch.setattr(
        workspace_imports_module,
        "_before_import_publish",
        replace_temporary_then_fail,
    )

    with pytest.raises(WorkspaceImportIntegrityError, match="changed before directory cleanup"):
        _ingest(store, tmp_path, _simple_archive())

    temporary_replacement = next(
        entry for entry in root.iterdir() if entry.name.startswith(".workspace-import-tmp-")
    )
    assert (temporary_replacement / replacement_marker_name).read_text(
        encoding="ascii"
    ) == "preserve replacement"
    assert (preserved_original / workspace_imports_module._ARCHIVE_NAME).is_file()


def test_fault_after_publish_preserves_complete_recoverable_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "imports"
    store = WorkspaceImportStore(root)

    def fail(*_args: object) -> None:
        raise OSError("post-publish fault")

    monkeypatch.setattr(workspace_imports_module, "_after_import_publish", fail)
    with pytest.raises(OSError, match="post-publish fault"):
        _ingest(store, tmp_path, _simple_archive())
    assert len(list(root.iterdir())) == 1

    monkeypatch.setattr(workspace_imports_module, "_after_import_publish", lambda *_args: None)
    WorkspaceImportStore(root)
    assert len(list(root.iterdir())) == 1


@pytest.mark.parametrize("target", ["archive", "metadata"])
def test_ingest_revalidates_persisted_files_immediately_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    root = tmp_path / "imports"
    store = WorkspaceImportStore(root)

    def rewrite_persisted_file(*_args: object) -> None:
        temporary = next(root.iterdir())
        path = temporary / (
            workspace_imports_module._ARCHIVE_NAME
            if target == "archive"
            else workspace_imports_module._METADATA_NAME
        )
        with path.open("r+b", buffering=0) as stream:
            if target == "archive":
                stream.seek(BLOCK)
                stream.write(b"X")
            else:
                raw = stream.read()
                offset = raw.index(b"project-test")
                os.pwrite(stream.fileno(), b"project-best", offset)
            stream.flush()
            os.fsync(stream.fileno())

    monkeypatch.setattr(
        workspace_imports_module,
        "_before_import_publish",
        rewrite_persisted_file,
    )
    with pytest.raises(WorkspaceImportIntegrityError, match="digest|authentication"):
        _ingest(store, tmp_path, _simple_archive())
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("target", ["archive", "metadata"])
def test_ingest_revalidates_persisted_files_after_atomic_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    root = tmp_path / "imports"
    store = WorkspaceImportStore(root)

    def rewrite_published_file(_root_descriptor: int, import_id: str) -> None:
        path = (
            root
            / import_id
            / (
                workspace_imports_module._ARCHIVE_NAME
                if target == "archive"
                else workspace_imports_module._METADATA_NAME
            )
        )
        with path.open("r+b", buffering=0) as stream:
            if target == "archive":
                stream.seek(BLOCK)
                stream.write(b"X")
            else:
                raw = stream.read()
                offset = raw.index(b"project-test")
                os.pwrite(stream.fileno(), b"project-best", offset)
            stream.flush()
            os.fsync(stream.fileno())

    monkeypatch.setattr(
        workspace_imports_module,
        "_after_import_publish",
        rewrite_published_file,
    )
    with pytest.raises(WorkspaceImportIntegrityError, match="digest|authentication"):
        _ingest(store, tmp_path, _simple_archive())
    assert list(root.iterdir()) == []


def test_atomic_publish_failure_does_not_leave_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "imports"
    store = WorkspaceImportStore(root)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise FileExistsError("collision")

    monkeypatch.setattr(workspace_imports_module, "_rename_noreplace", fail)
    with pytest.raises(FileExistsError, match="collision"):
        _ingest(store, tmp_path, _simple_archive())
    assert list(root.iterdir()) == []


def test_resolve_rejects_archive_tamper_and_replacement(tmp_path: Path) -> None:
    root = tmp_path / "imports"
    store = WorkspaceImportStore(root)
    import_ref = _ingest(store, tmp_path, _simple_archive())
    archive_path = _stored_directory(root, import_ref) / "archive.tar"

    with archive_path.open("r+b", buffering=0) as stream:
        stream.seek(BLOCK)
        stream.write(b"X")
        stream.flush()
        os.fsync(stream.fileno())
    with pytest.raises(WorkspaceImportIntegrityError, match="digest"):
        with store.resolve(import_ref, ownership=_ownership(import_ref)):
            pass

    archive_path.unlink()
    archive_path.write_bytes(_simple_archive())
    os.chmod(archive_path, 0o600)
    with pytest.raises(WorkspaceImportIntegrityError):
        with store.resolve(import_ref, ownership=_ownership(import_ref)):
            pass


def test_resolve_rejects_metadata_tamper_symlink_and_reference_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "imports"
    store = WorkspaceImportStore(root)
    import_ref = _ingest(store, tmp_path, _simple_archive())
    metadata_path = _stored_directory(root, import_ref) / "metadata.json"
    raw = json.loads(metadata_path.read_text(encoding="ascii"))
    raw["unknown"] = True
    metadata_path.write_text(json.dumps(raw), encoding="ascii")
    os.chmod(metadata_path, 0o600)
    with pytest.raises(WorkspaceImportIntegrityError, match="closed JSON|canonical"):
        with store.resolve(import_ref, ownership=_ownership(import_ref)):
            pass

    metadata_path.unlink()
    target = tmp_path / "outside-metadata"
    target.write_text("outside", encoding="ascii")
    metadata_path.symlink_to(target)
    with pytest.raises(WorkspaceImportIntegrityError, match="metadata"):
        with store.resolve(import_ref, ownership=_ownership(import_ref)):
            pass
    assert target.read_text(encoding="ascii") == "outside"


def test_resolve_detects_mutation_during_rehash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "imports"
    archive = _simple_archive(b"x" * (2 * 1024 * 1024))
    store = WorkspaceImportStore(root)
    import_ref = _ingest(store, tmp_path, archive)
    archive_path = _stored_directory(root, import_ref) / "archive.tar"
    real_read = workspace_imports_module.os.read
    changed = False

    def mutate_after_archive_read(fd: int, count: int) -> bytes:
        nonlocal changed
        value = real_read(fd, count)
        if count > 4096 and not changed:
            with archive_path.open("r+b", buffering=0) as stream:
                stream.seek(BLOCK)
                stream.write(b"Y")
                stream.flush()
                os.fsync(stream.fileno())
            changed = True
        return value

    monkeypatch.setattr(workspace_imports_module.os, "read", mutate_after_archive_read)
    with pytest.raises(WorkspaceImportIntegrityError, match="changed"):
        with store.resolve(import_ref, ownership=_ownership(import_ref)):
            pass


def test_resolve_rejects_equal_length_pwrite_to_snapshot_during_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "imports"
    archive = _simple_archive(b"x" * (2 * 1024 * 1024))
    store = WorkspaceImportStore(root)
    import_ref = _ingest(store, tmp_path, archive)
    real_write_all = workspace_imports_module._write_all
    changed = False

    def rewrite_written_snapshot(descriptor: int, data: bytes | memoryview) -> None:
        nonlocal changed
        real_write_all(descriptor, data)
        if not changed and len(data) >= BLOCK + 1:
            os.pwrite(descriptor, b"Y", BLOCK)
            changed = True

    monkeypatch.setattr(workspace_imports_module, "_write_all", rewrite_written_snapshot)
    with pytest.raises(WorkspaceImportIntegrityError, match="snapshot.*digest"):
        with store.resolve(import_ref, ownership=_ownership(import_ref)):
            pass
    assert changed


def test_release_and_delete_require_exact_ref_and_remove_import(tmp_path: Path) -> None:
    root = tmp_path / "imports"
    store = WorkspaceImportStore(root)
    first = _ingest(store, tmp_path, _simple_archive())
    second = _ingest(store, tmp_path, _simple_archive(b"second"))
    mismatched = first.model_copy(update={"content_sha256": second.content_sha256})

    with pytest.raises(WorkspaceImportIntegrityError, match="reference"):
        store.release(mismatched, ownership=_ownership(first))
    store.release(first, ownership=_ownership(first))
    assert not _stored_directory(root, first).exists()
    store.delete(second, ownership=_ownership(second))
    with pytest.raises(WorkspaceImportNotFoundError):
        with store.resolve(second, ownership=_ownership(second)):
            pass


@pytest.mark.parametrize("operation", ["resolve", "release", "delete"])
@pytest.mark.parametrize(
    "import_id",
    [
        "../workspace-import-" + ("0" * 48),
        "workspace-import-" + ("0" * 47),
        "workspace-import-" + ("0" * 49),
        "workspace-import-" + ("A" * 48),
    ],
)
def test_external_refs_require_exact_store_id_before_filesystem_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    import_id: str,
) -> None:
    root = tmp_path / "imports"
    store = WorkspaceImportStore(root)
    valid_ref = _ingest(store, tmp_path, _simple_archive())
    external_ref = valid_ref.model_copy(update={"import_id": import_id})
    filesystem_reached = False

    def forbidden_root_lock() -> object:
        nonlocal filesystem_reached
        filesystem_reached = True
        raise AssertionError("filesystem access occurred before import ID validation")
        yield  # pragma: no cover

    monkeypatch.setattr(store, "_locked_root", forbidden_root_lock)
    with pytest.raises(WorkspaceImportIntegrityError, match="not issued by this store"):
        if operation == "resolve":
            with store.resolve(external_ref, ownership=_ownership(valid_ref)):
                pass
        else:
            getattr(store, operation)(external_ref, ownership=_ownership(valid_ref))
    assert not filesystem_reached
    assert _stored_directory(root, valid_ref).is_dir()


def test_startup_reconcile_removes_temp_orphan_tamper_and_symlink_without_following(
    tmp_path: Path,
) -> None:
    root = tmp_path / "imports"
    store = WorkspaceImportStore(root)
    valid = _ingest(store, tmp_path, _simple_archive())
    tampered = _ingest(store, tmp_path, _simple_archive(b"tamper"))
    (_stored_directory(root, tampered) / "archive.tar").write_bytes(_simple_archive(b"changed"))

    temporary = root / ".workspace-import-tmp-dead"
    temporary.mkdir(mode=0o700)
    (temporary / "archive.tar").write_bytes(b"partial")
    os.chmod(temporary / "archive.tar", 0o600)
    orphan = root / "unowned-entry"
    orphan.write_text("orphan", encoding="ascii")
    outside = tmp_path / "outside"
    outside.write_text("preserve", encoding="ascii")
    (root / "host-link").symlink_to(outside)

    WorkspaceImportStore(root)

    assert {entry.name for entry in root.iterdir()} == {valid.import_id}
    assert outside.read_text(encoding="ascii") == "preserve"


def test_reconcile_scan_budgets_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "imports"
    store = WorkspaceImportStore(root)
    _ingest(store, tmp_path, _simple_archive())

    store._reconcile_max_nodes = 1
    with pytest.raises(WorkspaceImportIntegrityError, match="node budget"):
        store.reconcile()
    store._reconcile_max_nodes = workspace_imports_module._DEFAULT_RECONCILE_MAX_NODES
    store._reconcile_max_bytes = 128
    with pytest.raises(WorkspaceImportIntegrityError, match="byte budget"):
        store.reconcile()


@pytest.mark.parametrize("fault", ["metadata_open", "metadata_read", "archive_xattr"])
def test_reconcile_preserves_import_on_transient_filesystem_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    root = tmp_path / "imports"
    store = WorkspaceImportStore(root)
    import_ref = _ingest(store, tmp_path, _simple_archive())
    stored = _stored_directory(root, import_ref)

    if fault == "metadata_open":
        real_operation = workspace_imports_module.os.open

        def fail_metadata_open(path: object, *args: object, **kwargs: object) -> int:
            if path == workspace_imports_module._METADATA_NAME:
                raise OSError(errno.EIO, "injected metadata open failure")
            return real_operation(path, *args, **kwargs)

        monkeypatch.setattr(workspace_imports_module.os, "open", fail_metadata_open)
    elif fault == "metadata_read":
        real_operation = workspace_imports_module.os.read

        def fail_metadata_read(_descriptor: int, _count: int) -> bytes:
            raise OSError(errno.EIO, "injected metadata read failure")

        monkeypatch.setattr(workspace_imports_module.os, "read", fail_metadata_read)
    else:
        real_operation = workspace_imports_module.os.getxattr

        def fail_archive_xattr(
            descriptor: int,
            attribute: str,
            *_args: object,
            **_kwargs: object,
        ) -> bytes:
            if attribute == workspace_imports_module._ARCHIVE_TOKEN_XATTR:
                raise OSError(errno.EIO, "injected archive xattr failure")
            return real_operation(descriptor, attribute)

        monkeypatch.setattr(workspace_imports_module.os, "getxattr", fail_archive_xattr)

    with pytest.raises((OSError, WorkspaceImportIntegrityError)):
        WorkspaceImportStore(root)
    assert stored.is_dir()
    assert (stored / "archive.tar").is_file()
    assert (stored / "metadata.json").is_file()

    if fault == "metadata_open":
        monkeypatch.setattr(workspace_imports_module.os, "open", real_operation)
    elif fault == "metadata_read":
        monkeypatch.setattr(workspace_imports_module.os, "read", real_operation)
    else:
        monkeypatch.setattr(workspace_imports_module.os, "getxattr", real_operation)
    store.reconcile()
    with store.resolve(import_ref, ownership=_ownership(import_ref)) as stream:
        assert stream.read() == _simple_archive()


def test_root_and_stored_modes_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "imports"
    root.mkdir(mode=0o755)
    os.chmod(root, 0o755)
    with pytest.raises(WorkspaceImportStoreConfigurationError, match="0700"):
        WorkspaceImportStore(root)

    root.rmdir()
    store = WorkspaceImportStore(root)
    import_ref = _ingest(store, tmp_path, _simple_archive())
    os.chmod(_stored_directory(root, import_ref) / "archive.tar", 0o644)
    with pytest.raises(WorkspaceImportIntegrityError, match="0600"):
        with store.resolve(import_ref, ownership=_ownership(import_ref)):
            pass


def test_reconcile_refuses_to_follow_nested_orphan_directories(tmp_path: Path) -> None:
    root = tmp_path / "imports"
    WorkspaceImportStore(root)
    nested = root / ".workspace-import-tmp-nested"
    nested.mkdir(mode=0o700)
    (nested / "child").mkdir()

    with pytest.raises(WorkspaceImportIntegrityError, match="nested directories"):
        WorkspaceImportStore(root)
    quarantined = [
        entry for entry in root.iterdir() if entry.name.startswith(".workspace-import-quarantine-")
    ]
    assert len(quarantined) == 1
    assert (quarantined[0] / "child").is_dir()


def test_reconcile_does_not_delete_a_pathname_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "imports"
    store = WorkspaceImportStore(root)
    import_ref = _ingest(store, tmp_path, _simple_archive())
    original = _stored_directory(root, import_ref)
    preserved = root / "preserved-original"
    replacement_marker = original / "replacement-marker"

    def replace_then_fail(*_args: object, **_kwargs: object) -> object:
        original.rename(preserved)
        original.mkdir(mode=0o700)
        replacement_marker.write_text("replacement", encoding="ascii")
        raise workspace_imports_module._DeterministicImportCorruption(
            "injected validation failure"
        )

    monkeypatch.setattr(store, "_validate_import_contents", replace_then_fail)

    with pytest.raises(WorkspaceImportIntegrityError, match="changed during reconciliation"):
        store.reconcile()
    assert replacement_marker.read_text(encoding="ascii") == "replacement"
    assert preserved.is_dir()


def test_root_identity_rejects_live_and_restart_path_replacement(tmp_path: Path) -> None:
    parent = tmp_path / "state"
    root = parent / "imports"
    parent.mkdir(mode=0o700)
    store = WorkspaceImportStore(root)
    _ingest(store, tmp_path, _simple_archive())
    original = parent / "original-imports"
    root.rename(original)
    root.mkdir(mode=0o700)
    sentinel = root / "replacement-sentinel"
    sentinel.write_text("preserve", encoding="ascii")

    with pytest.raises(WorkspaceImportIntegrityError, match="root binding"):
        store.reconcile()
    with pytest.raises(WorkspaceImportStoreConfigurationError, match="root binding"):
        WorkspaceImportStore(root)

    assert sentinel.read_text(encoding="ascii") == "preserve"
    assert original.is_dir()


def test_restart_rejects_forged_root_marker_and_xattr_after_offline_replacement(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "state"
    root = parent / "imports"
    parent.mkdir(mode=0o700)
    store = WorkspaceImportStore(root)
    _ingest(store, tmp_path, _simple_archive())
    store.close()

    marker_path = next(
        path
        for path in parent.iterdir()
        if path.name.startswith(workspace_imports_module._ROOT_MARKER_PREFIX)
        and path.name.endswith(".json")
    )
    marker = json.loads(marker_path.read_text(encoding="ascii"))
    original = parent / "original-imports"
    root.rename(original)
    root.mkdir(mode=0o700)
    root_status = root.stat()
    marker["root_identity"] = {
        "device": root_status.st_dev,
        "inode": root_status.st_ino,
    }
    marker_path.write_text(
        json.dumps(marker, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
        encoding="ascii",
    )
    os.chmod(marker_path, 0o600)
    os.setxattr(
        root,
        workspace_imports_module._ROOT_TOKEN_XATTR,
        bytes.fromhex(marker["store_token"]),
    )

    with pytest.raises(
        WorkspaceImportStoreConfigurationError,
        match="authentication",
    ):
        WorkspaceImportStore(root)
    assert original.is_dir()
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("key_mutation", ["missing", "replacement"])
@pytest.mark.parametrize("with_import", [False, True])
def test_restart_fails_closed_when_authentication_key_is_missing_or_replaced(
    tmp_path: Path,
    key_mutation: str,
    with_import: bool,
) -> None:
    parent = tmp_path / "state"
    root = parent / "imports"
    parent.mkdir(mode=0o700)
    store = WorkspaceImportStore(root)
    if with_import:
        import_ref = _ingest(store, tmp_path, _simple_archive())
    key_path = parent / store._auth_key_name
    original_key = key_path.read_bytes()
    store.close()

    if key_mutation == "missing":
        key_path.unlink()
        expected_error = "no authentication key"
    else:
        replacement_key = os.urandom(workspace_imports_module._AUTH_KEY_BYTES)
        while replacement_key == original_key:
            replacement_key = os.urandom(workspace_imports_module._AUTH_KEY_BYTES)
        key_path.unlink()
        key_path.write_bytes(replacement_key)
        os.chmod(key_path, 0o600)
        expected_error = "authentication"

    with pytest.raises(WorkspaceImportStoreConfigurationError, match=expected_error):
        WorkspaceImportStore(root)

    if key_mutation == "replacement":
        assert key_path.read_bytes() != original_key
    if with_import:
        assert _stored_directory(root, import_ref).is_dir()
    else:
        assert list(root.iterdir()) == []


def test_ancestor_replacement_and_symlinked_ancestor_fail_closed(tmp_path: Path) -> None:
    parent = tmp_path / "state"
    root = parent / "imports"
    parent.mkdir(mode=0o700)
    store = WorkspaceImportStore(root)
    moved_parent = tmp_path / "original-state"
    parent.rename(moved_parent)
    parent.mkdir(mode=0o700)
    root.mkdir(mode=0o700)
    sentinel = root / "replacement-sentinel"
    sentinel.write_text("preserve", encoding="ascii")

    with pytest.raises(WorkspaceImportIntegrityError, match="ancestor binding"):
        store.reconcile()
    with pytest.raises(WorkspaceImportStoreConfigurationError, match="marker|existing root"):
        WorkspaceImportStore(root)
    assert sentinel.read_text(encoding="ascii") == "preserve"

    symlink_parent = tmp_path / "symlink-state"
    symlink_parent.symlink_to(moved_parent, target_is_directory=True)
    with pytest.raises(WorkspaceImportStoreConfigurationError, match="no-follow ancestor"):
        WorkspaceImportStore(symlink_parent / "other-imports")


def test_fresh_root_marker_recovers_after_parent_entry_fsync_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "state" / "imports"
    root.parent.mkdir(mode=0o700)

    def fail(_parent_descriptor: int, _root_name: str) -> None:
        raise OSError("injected root initialization crash")

    monkeypatch.setattr(workspace_imports_module, "_after_fresh_root_parent_fsync", fail)
    with pytest.raises(OSError, match="initialization crash"):
        WorkspaceImportStore(root)

    monkeypatch.setattr(
        workspace_imports_module,
        "_after_fresh_root_parent_fsync",
        lambda *_args: None,
    )
    restarted = WorkspaceImportStore(root)
    import_ref = _ingest(restarted, tmp_path, _simple_archive())
    with restarted.resolve(import_ref, ownership=_ownership(import_ref)) as stream:
        assert stream.read() == _simple_archive()


@pytest.mark.parametrize("kind", ["auth_key", "pending_marker", "final_marker"])
@pytest.mark.parametrize(
    "stage",
    ["before_write", "before_file_fsync", "before_parent_fsync"],
)
def test_initialization_file_publication_recovers_from_write_and_fsync_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    stage: str,
) -> None:
    parent = tmp_path / "state"
    root = parent / "imports"
    parent.mkdir(mode=0o700)
    marker_suffix = hashlib.sha256(os.fsencode(root.name)).hexdigest()[:32]
    marker_name = f"{workspace_imports_module._ROOT_MARKER_PREFIX}{marker_suffix}.json"
    target_names = {
        "auth_key": f"{workspace_imports_module._AUTH_KEY_PREFIX}{marker_suffix}.key",
        "pending_marker": f"{marker_name}.pending",
        "final_marker": marker_name,
    }
    injected = False

    def fail_operation(observed_kind: str, observed_stage: str, descriptor: int) -> None:
        nonlocal injected
        if injected or (observed_kind, observed_stage) != (kind, stage):
            return
        injected = True
        if stage == "before_write":
            os.write(descriptor, b"partial")
        raise OSError(errno.EIO, f"injected {kind} {stage} failure")

    monkeypatch.setattr(
        workspace_imports_module,
        "_initialization_file_fault_point",
        fail_operation,
    )
    with pytest.raises(OSError, match=f"injected {kind} {stage} failure"):
        WorkspaceImportStore(root)
    assert injected

    target = parent / target_names[kind]
    if stage == "before_parent_fsync":
        assert target.is_file()
    else:
        assert not target.exists()

    monkeypatch.setattr(
        workspace_imports_module,
        "_initialization_file_fault_point",
        lambda *_args: None,
    )
    restarted = WorkspaceImportStore(root)
    assert restarted._root_identity == (root.stat().st_dev, root.stat().st_ino)
    restarted.close()
    assert not any(
        entry.name.startswith(workspace_imports_module._INITIALIZATION_TEMP_PREFIX)
        for entry in parent.iterdir()
    )


def test_concurrent_first_initialization_is_serialized_before_key_publication(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "state"
    root = parent / "imports"
    parent.mkdir(mode=0o700)
    context = multiprocessing.get_context("fork")
    results: multiprocessing.Queue[object] = context.Queue()
    first_started = context.Event()
    first_finished = context.Event()
    entered_auth_write = context.Event()
    release_auth_write = context.Event()
    second_started = context.Event()
    second_finished = context.Event()
    first = context.Process(
        target=_process_initialize_store,
        args=(
            str(root),
            results,
            first_started,
            first_finished,
            entered_auth_write,
            release_auth_write,
        ),
    )
    second = context.Process(
        target=_process_initialize_store,
        args=(str(root), results, second_started, second_finished),
    )

    first.start()
    assert first_started.wait(timeout=5)
    assert entered_auth_write.wait(timeout=5)
    second.start()
    assert second_started.wait(timeout=5)
    assert not second_finished.wait(timeout=0.25)
    release_auth_write.set()
    first.join(timeout=10)
    second.join(timeout=10)

    assert first.exitcode == 0
    assert second.exitcode == 0
    observed = [results.get(timeout=2), results.get(timeout=2)]
    assert all(result[0] == "ok" for result in observed)
    assert observed[0][1] == observed[1][1]
    restarted = WorkspaceImportStore(root)
    assert restarted._root_identity == observed[0][1]
    restarted.close()


def test_restart_does_not_claim_an_unrecorded_authentication_key(tmp_path: Path) -> None:
    parent = tmp_path / "state"
    root = parent / "imports"
    parent.mkdir(mode=0o700)
    marker_suffix = hashlib.sha256(os.fsencode(root.name)).hexdigest()[:32]
    key = parent / f"{workspace_imports_module._AUTH_KEY_PREFIX}{marker_suffix}.key"
    key.write_bytes(os.urandom(workspace_imports_module._AUTH_KEY_BYTES))
    os.chmod(key, 0o600)

    with pytest.raises(
        WorkspaceImportStoreConfigurationError,
        match="authentication key has no initialization record",
    ):
        WorkspaceImportStore(root)
    assert not root.exists()
    assert key.is_file()


def test_restart_preserves_unpublished_initialization_temporary_and_recovers_fresh_root(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "state"
    root = parent / "imports"
    parent.mkdir(mode=0o700)
    interrupted = parent / f"{workspace_imports_module._INITIALIZATION_TEMP_PREFIX}unknown"
    interrupted.write_bytes(b"partial unknown initialization state")
    os.chmod(interrupted, 0o600)

    store = WorkspaceImportStore(root)

    assert interrupted.read_bytes() == b"partial unknown initialization state"
    assert store._root_identity == (root.stat().st_dev, root.stat().st_ino)
    store.close()


def test_ownership_is_atomic_idempotent_and_project_scoped(tmp_path: Path) -> None:
    root = tmp_path / "imports"
    store = WorkspaceImportStore(root)
    archive = _simple_archive()
    owner = _new_ownership(project_id="project-a")
    first = _ingest(store, tmp_path, archive, ownership=owner)
    retry = _ingest(store, tmp_path, archive, ownership=owner)

    assert retry == first
    assert [entry.name for entry in root.iterdir()] == [first.import_id]

    with pytest.raises(WorkspaceImportIntegrityError, match="idempotency"):
        _ingest(store, tmp_path, _simple_archive(b"different"), ownership=owner)

    other_owner = WorkspaceImportOwnership(
        project_id="project-b",
        operation_id=owner.operation_id,
        idempotency_key=owner.idempotency_key,
    )
    with pytest.raises(WorkspaceImportIntegrityError, match="ownership"):
        with store.resolve(first, ownership=other_owner):
            pass
    with pytest.raises(WorkspaceImportIntegrityError, match="ownership"):
        store.release(first, ownership=other_owner)
    with store.resolve(first, ownership=owner) as stream:
        assert stream.read() == archive

    independent_owner = _new_ownership(project_id="project-b")
    independent = _ingest(
        store,
        tmp_path,
        archive,
        ownership=independent_owner,
    )
    store.release(first, ownership=owner)
    with store.resolve(independent, ownership=independent_owner) as stream:
        assert stream.read() == archive


def test_canonical_metadata_rewrite_cannot_forge_ownership(tmp_path: Path) -> None:
    root = tmp_path / "imports"
    store = WorkspaceImportStore(root)
    original_owner = WorkspaceImportOwnership(
        project_id="project-a",
        operation_id="workspace-sync-operation-a",
        idempotency_key="workspace-sync-idempotency-a",
    )
    import_ref = _ingest(
        store,
        tmp_path,
        _simple_archive(),
        ownership=original_owner,
    )
    metadata_path = _stored_directory(root, import_ref) / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="ascii"))
    forged_owner = WorkspaceImportOwnership(
        project_id="project-b",
        operation_id="workspace-sync-operation-b",
        idempotency_key="workspace-sync-idempotency-b",
    )
    metadata["ownership"] = {
        "idempotency_key": forged_owner.idempotency_key,
        "operation_id": forged_owner.operation_id,
        "project_id": forged_owner.project_id,
    }
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
        encoding="ascii",
    )
    os.chmod(metadata_path, 0o600)

    with pytest.raises(WorkspaceImportIntegrityError, match="authentication"):
        with store.resolve(import_ref, ownership=forged_owner):
            pass


def test_publish_crash_is_reclaimed_by_exact_ownership_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "imports"
    store = WorkspaceImportStore(root)
    archive = _simple_archive()
    owner = _new_ownership(project_id="project-crash")

    def fail(*_args: object) -> None:
        raise OSError("post-publish crash")

    monkeypatch.setattr(workspace_imports_module, "_after_import_publish", fail)
    with pytest.raises(OSError, match="post-publish crash"):
        _ingest(store, tmp_path, archive, ownership=owner)

    monkeypatch.setattr(workspace_imports_module, "_after_import_publish", lambda *_args: None)
    restarted = WorkspaceImportStore(root)
    recovered = _ingest(restarted, tmp_path, archive, ownership=owner)
    assert [entry.name for entry in root.iterdir()] == [recovered.import_id]
    with restarted.resolve(recovered, ownership=owner) as stream:
        assert stream.read() == archive


def test_cancel_before_publication_removes_the_bound_temporary_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "imports"
    store = WorkspaceImportStore(root)
    archive = _simple_archive(b"cancel before publication")
    source = _source(tmp_path, archive)
    owner = _new_ownership(project_id="project-cancel-before")
    cancelled = False

    def cancel_at_publish(_root_descriptor: int, _import_id: str) -> None:
        nonlocal cancelled
        cancelled = True

    monkeypatch.setattr(workspace_imports_module, "_before_import_publish", cancel_at_publish)
    with source.open("rb", buffering=0) as stream:
        with pytest.raises(WorkspaceImportCancelled):
            store.ingest_pending(
                stream,
                ownership=owner,
                import_id="workspace-import-" + ("1a" * 24),
                cancel_check=lambda: cancelled,
            )

    assert list(root.iterdir()) == []


def test_cancel_after_publication_returns_a_recoverable_pending_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "imports"
    store = WorkspaceImportStore(root)
    archive = _simple_archive(b"cancel after publication")
    source = _source(tmp_path, archive)
    owner = _new_ownership(project_id="project-cancel-after")
    cancelled = False

    def cancel_after_publish(_root_descriptor: int, _import_id: str) -> None:
        nonlocal cancelled
        cancelled = True

    monkeypatch.setattr(workspace_imports_module, "_after_import_publish", cancel_after_publish)
    with source.open("rb", buffering=0) as stream:
        pending = store.ingest_pending(
            stream,
            ownership=owner,
            import_id="workspace-import-" + ("2b" * 24),
            cancel_check=lambda: cancelled,
        )

    assert (root / pending.import_ref.import_id).is_dir()
    store.discard_pending(
        pending.import_ref,
        ownership=owner,
        lease_token=pending.lease_token,
    )
    assert list(root.iterdir()) == []


def test_cancel_interrupts_wait_for_the_in_process_import_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = WorkspaceImportStore(tmp_path / "imports")
    source = _source(tmp_path, _simple_archive(b"cancel lock wait"))
    ownership = _new_ownership(project_id="project-cancel-lock-wait")
    source_hashed = threading.Event()
    cancelled = threading.Event()
    errors: list[BaseException] = []
    original_sha256 = workspace_imports_module._source_sha256

    def observed_sha256(*args, **kwargs):
        result = original_sha256(*args, **kwargs)
        source_hashed.set()
        return result

    def ingest(stream) -> None:
        try:
            store.ingest_pending(
                stream,
                ownership=ownership,
                cancel_check=cancelled.is_set,
            )
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(workspace_imports_module, "_source_sha256", observed_sha256)
    thread_lock = workspace_imports_module._thread_lock_for(store._root)
    with thread_lock, source.open("rb", buffering=0) as stream:
        worker = threading.Thread(target=ingest, args=(stream,))
        worker.start()
        assert source_hashed.wait(timeout=5)
        cancelled.set()
        worker.join(timeout=1)
        assert not worker.is_alive()

    assert len(errors) == 1
    assert isinstance(errors[0], WorkspaceImportCancelled)
    assert list((tmp_path / "imports").iterdir()) == []


def test_resolve_rejects_same_uid_in_place_rewrite_before_snapshot_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "imports"
    store = WorkspaceImportStore(root)
    archive = _simple_archive(b"original")
    import_ref = _ingest(store, tmp_path, archive)
    archive_path = _stored_directory(root, import_ref) / "archive.tar"

    def rewrite_before_commit(*_args: object) -> None:
        with archive_path.open("r+b", buffering=0) as stream:
            stream.seek(BLOCK)
            stream.write(b"X")
            stream.flush()
            os.fsync(stream.fileno())

    monkeypatch.setattr(
        workspace_imports_module,
        "_before_snapshot_commit",
        rewrite_before_commit,
    )
    with pytest.raises(WorkspaceImportIntegrityError, match="changed|digest"):
        with store.resolve(import_ref, ownership=_ownership(import_ref)):
            pass


def test_resolve_yields_unlinked_read_only_snapshot_not_mutable_store_inode(
    tmp_path: Path,
) -> None:
    root = tmp_path / "imports"
    store = WorkspaceImportStore(root)
    archive = _simple_archive(b"stable snapshot")
    import_ref = _ingest(store, tmp_path, archive)
    archive_path = _stored_directory(root, import_ref) / "archive.tar"

    with store.resolve(import_ref, ownership=_ownership(import_ref)) as stream:
        assert fcntl.fcntl(stream.fileno(), fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
        with archive_path.open("r+b", buffering=0) as stored:
            stored.seek(BLOCK)
            stored.write(b"X")
            stored.flush()
            os.fsync(stored.fileno())
        assert stream.read() == archive
        assert all(
            not entry.name.startswith(".workspace-import-snapshot-") for entry in root.iterdir()
        )
