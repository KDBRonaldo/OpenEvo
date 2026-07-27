"""No-follow native folder snapshot builder for the private Desktop bridge."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import hashlib
from itertools import islice
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import BinaryIO, Callable, Iterator
import unicodedata

from desktop.sidecar.contracts.v1 import WorkspaceImportRefV1
from openevo.backend.contracts.v1.models import (
    MAX_WORKSPACE_ENTRIES,
    MAX_WORKSPACE_UPLOAD_BYTES,
)


_BLOCK_SIZE = 512
_STAT_BLOCK_BYTES = 512
_COPY_BYTES = 1024 * 1024
_MAX_FILE_BYTES = 0o77777777777
_MAX_PATH_BYTES = 256
_MAX_PATH_DEPTH = 32
_IMPORT_ID_RE = re.compile(r"^workspace-import-[0-9a-f]{48}$")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK


class NativeWorkspaceArchiveError(ValueError):
    """The selected folder cannot become a deterministic workspace archive."""


class NativeWorkspaceArchiveCancelled(NativeWorkspaceArchiveError):
    """The native snapshot was cooperatively cancelled at a bounded checkpoint."""


CancellationCheck = Callable[[], bool]
ProgressObserver = Callable[[int, int], None]


def _check_cancel(cancel_check: CancellationCheck | None) -> None:
    if cancel_check is not None and cancel_check():
        raise NativeWorkspaceArchiveCancelled("workspace import was cancelled")


@dataclass(frozen=True, slots=True)
class _Identity:
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    links: int
    size: int
    blocks: int
    modified_ns: int
    changed_ns: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> _Identity:
        return cls(
            device=value.st_dev,
            inode=value.st_ino,
            mode=value.st_mode,
            uid=value.st_uid,
            gid=value.st_gid,
            links=value.st_nlink,
            size=value.st_size,
            blocks=value.st_blocks,
            modified_ns=value.st_mtime_ns,
            changed_ns=value.st_ctime_ns,
        )


@dataclass(frozen=True, slots=True)
class _Entry:
    logical_path: str
    parts: tuple[str, ...]
    header_path: bytes
    directory: bool
    identity: _Identity


@dataclass(frozen=True, slots=True)
class PreparedNativeWorkspace:
    display_name: str
    import_ref: WorkspaceImportRefV1
    stream: BinaryIO


def _after_archive_write(_root_descriptor: int) -> None:
    """Test seam immediately before the source inventory is revalidated."""


def _write_all(
    descriptor: int,
    payload: bytes,
    *,
    cancel_check: CancellationCheck | None = None,
    progress: Callable[[int], None] | None = None,
) -> None:
    view = memoryview(payload)
    offset = 0
    while offset < len(view):
        _check_cancel(cancel_check)
        try:
            written = os.write(descriptor, view[offset:])
        except InterruptedError:
            continue
        if written <= 0:
            raise NativeWorkspaceArchiveError("workspace archive write failed")
        offset += written
        if progress is not None:
            progress(written)


def _safe_component(value: str) -> str:
    if (
        not value
        or value in {".", ".."}
        or "\\" in value
        or unicodedata.normalize("NFC", value) != value
        or any(unicodedata.category(character) == "Cc" for character in value)
    ):
        raise NativeWorkspaceArchiveError("workspace contains an unsupported path")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise NativeWorkspaceArchiveError("workspace path is not valid UTF-8") from exc
    return value


def _header_path(parts: tuple[str, ...], *, directory: bool) -> bytes:
    if not parts or len(parts) > _MAX_PATH_DEPTH:
        raise NativeWorkspaceArchiveError("workspace path exceeds its depth limit")
    logical = "/".join(parts).encode("utf-8")
    header = logical + (b"/" if directory else b"")
    if len(header) > _MAX_PATH_BYTES:
        raise NativeWorkspaceArchiveError("workspace path exceeds its byte limit")
    _split_header_path(header)
    return header


def _split_header_path(header_path: bytes) -> tuple[bytes, bytes]:
    if len(header_path) <= 100:
        return header_path, b""
    for index in range(len(header_path) - 1, -1, -1):
        if header_path[index : index + 1] != b"/":
            continue
        prefix = header_path[:index]
        name = header_path[index + 1 :]
        if 1 <= len(prefix) <= 155 and 1 <= len(name) <= 100:
            return name, prefix
    raise NativeWorkspaceArchiveError("workspace path cannot be represented as POSIX ustar")


def _put(header: bytearray, start: int, end: int, value: bytes) -> None:
    if len(value) > end - start:
        raise NativeWorkspaceArchiveError("workspace archive header field is too long")
    header[start:end] = bytes(end - start)
    header[start : start + len(value)] = value


def _tar_header(entry: _Entry) -> bytes:
    size = 0 if entry.directory else entry.identity.size
    if size < 0 or size > _MAX_FILE_BYTES:
        raise NativeWorkspaceArchiveError("workspace file exceeds the tar size limit")
    name, prefix = _split_header_path(entry.header_path)
    executable = not entry.directory and bool(entry.identity.mode & 0o111)
    mode = 0o755 if entry.directory or executable else 0o644
    header = bytearray(_BLOCK_SIZE)
    _put(header, 0, 100, name)
    _put(header, 100, 108, f"{mode:07o}\0".encode("ascii"))
    _put(header, 108, 116, b"0000000\0")
    _put(header, 116, 124, b"0000000\0")
    _put(header, 124, 136, f"{size:011o}\0".encode("ascii"))
    _put(header, 136, 148, b"00000000000\0")
    header[148:156] = b"        "
    header[156:157] = b"5" if entry.directory else b"0"
    _put(header, 257, 263, b"ustar\0")
    _put(header, 263, 265, b"00")
    _put(header, 345, 500, prefix)
    checksum = sum(header)
    header[148:156] = f"{checksum:06o}\0 ".encode("ascii")
    return bytes(header)


def _same_identity(value: os.stat_result, expected: _Identity) -> bool:
    return _Identity.from_stat(value) == expected


def _seek_extent(descriptor: int, offset: int, whence: int) -> int:
    while True:
        try:
            return os.lseek(descriptor, offset, whence)
        except InterruptedError:
            continue


def _require_non_sparse_file(descriptor: int, size: int) -> None:
    if size == 0:
        return
    status = os.fstat(descriptor)
    blocks = getattr(status, "st_blocks", None)
    if status.st_size != size:
        raise NativeWorkspaceArchiveError("workspace file changed during sparse-file detection")
    if type(blocks) is not int or blocks < 0:
        raise NativeWorkspaceArchiveError(
            "workspace file allocation metadata is unavailable; sparse or compressed files "
            "are unsupported"
        )
    if blocks * _STAT_BLOCK_BYTES < size:
        raise NativeWorkspaceArchiveError(
            "workspace file allocation cannot prove a fully allocated file; sparse or "
            "compressed files are unsupported"
        )
    seek_data = getattr(os, "SEEK_DATA", None)
    seek_hole = getattr(os, "SEEK_HOLE", None)
    if type(seek_data) is not int or type(seek_hole) is not int:
        raise NativeWorkspaceArchiveError(
            "workspace sparse-file detection is unavailable on this platform"
        )
    try:
        first_data = _seek_extent(descriptor, 0, seek_data)
    except OSError as exc:
        if exc.errno == errno.ENXIO:
            raise NativeWorkspaceArchiveError("workspace sparse files are unsupported") from None
        raise NativeWorkspaceArchiveError("workspace sparse-file detection failed closed") from exc
    if first_data != 0:
        raise NativeWorkspaceArchiveError("workspace sparse files are unsupported")
    try:
        first_hole = _seek_extent(descriptor, 0, seek_hole)
    except OSError as exc:
        raise NativeWorkspaceArchiveError("workspace sparse-file detection failed closed") from exc
    if first_hole != size:
        if 0 <= first_hole < size:
            raise NativeWorkspaceArchiveError("workspace sparse files are unsupported")
        raise NativeWorkspaceArchiveError("workspace sparse-file extent map is invalid")


def _open_selected_root(
    selected_path: str,
    *,
    expected_device: int,
    expected_inode: int,
    cancel_check: CancellationCheck | None = None,
) -> tuple[int, str]:
    _check_cancel(cancel_check)
    if type(selected_path) is not str or not selected_path.startswith("/"):
        raise NativeWorkspaceArchiveError("workspace path must be absolute")
    try:
        selected_path.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise NativeWorkspaceArchiveError("workspace path is not valid UTF-8") from exc
    components = tuple(part for part in selected_path.split("/") if part)
    if not components:
        raise NativeWorkspaceArchiveError("the filesystem root cannot be imported")
    for component in components:
        _safe_component(component)
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in components:
            _check_cancel(cancel_check)
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        status = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(status.st_mode)
            or status.st_dev != expected_device
            or status.st_ino != expected_inode
        ):
            raise NativeWorkspaceArchiveError("selected workspace is not a directory")
        display_name = components[-1]
        if len(display_name) > 256:
            raise NativeWorkspaceArchiveError("workspace display name is too long")
        _check_cancel(cancel_check)
        return descriptor, display_name
    except BaseException:
        os.close(descriptor)
        raise


def _scan_directory(
    descriptor: int,
    *,
    prefix: tuple[str, ...],
    entries: list[_Entry],
    seen_directories: set[tuple[int, int]],
    extracted_bytes: list[int],
    entry_budget: list[int],
    cancel_check: CancellationCheck | None,
) -> None:
    _check_cancel(cancel_check)
    before = os.fstat(descriptor)
    directory_identity = _Identity.from_stat(before)
    directory_key = (before.st_dev, before.st_ino)
    if directory_key in seen_directories:
        raise NativeWorkspaceArchiveError("workspace contains a repeated directory")
    seen_directories.add(directory_key)
    try:
        with os.scandir(descriptor) as iterator:
            children = []
            for child in islice(iterator, entry_budget[0] + 1):
                _check_cancel(cancel_check)
                children.append(child)
    except OSError as exc:
        raise NativeWorkspaceArchiveError("workspace directory cannot be enumerated") from exc
    if len(children) > entry_budget[0]:
        raise NativeWorkspaceArchiveError("workspace entry budget exceeded")
    entry_budget[0] -= len(children)
    children.sort(key=lambda entry: os.fsencode(entry.name))
    for child in children:
        _check_cancel(cancel_check)
        if type(child.name) is not str:
            raise NativeWorkspaceArchiveError("workspace path is not valid UTF-8")
        name = _safe_component(child.name)
        parts = prefix + (name,)
        try:
            value = child.stat(follow_symlinks=False)
        except OSError as exc:
            raise NativeWorkspaceArchiveError("workspace entry cannot be inspected") from exc
        identity = _Identity.from_stat(value)
        if stat.S_ISDIR(value.st_mode):
            entry = _Entry(
                logical_path="/".join(parts),
                parts=parts,
                header_path=_header_path(parts, directory=True),
                directory=True,
                identity=identity,
            )
            entries.append(entry)
            try:
                child_descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except OSError as exc:
                raise NativeWorkspaceArchiveError("workspace directory changed") from exc
            try:
                if not _same_identity(os.fstat(child_descriptor), identity):
                    raise NativeWorkspaceArchiveError("workspace directory changed")
                _scan_directory(
                    child_descriptor,
                    prefix=parts,
                    entries=entries,
                    seen_directories=seen_directories,
                    extracted_bytes=extracted_bytes,
                    entry_budget=entry_budget,
                    cancel_check=cancel_check,
                )
                if not _same_identity(os.fstat(child_descriptor), identity):
                    raise NativeWorkspaceArchiveError("workspace directory changed")
            finally:
                os.close(child_descriptor)
        elif stat.S_ISREG(value.st_mode):
            if value.st_nlink != 1:
                raise NativeWorkspaceArchiveError("workspace files must have one link")
            if value.st_size < 0 or value.st_size > _MAX_FILE_BYTES:
                raise NativeWorkspaceArchiveError("workspace file exceeds its size limit")
            try:
                file_descriptor = os.open(name, _FILE_FLAGS, dir_fd=descriptor)
            except OSError as exc:
                raise NativeWorkspaceArchiveError("workspace file changed") from exc
            try:
                if not _same_identity(os.fstat(file_descriptor), identity):
                    raise NativeWorkspaceArchiveError("workspace file changed")
                _check_cancel(cancel_check)
                _require_non_sparse_file(file_descriptor, value.st_size)
                _check_cancel(cancel_check)
                if not _same_identity(os.fstat(file_descriptor), identity):
                    raise NativeWorkspaceArchiveError("workspace file changed")
            finally:
                os.close(file_descriptor)
            if value.st_size > MAX_WORKSPACE_UPLOAD_BYTES - extracted_bytes[0]:
                raise NativeWorkspaceArchiveError("workspace extracted-byte budget exceeded")
            extracted_bytes[0] += value.st_size
            entries.append(
                _Entry(
                    logical_path="/".join(parts),
                    parts=parts,
                    header_path=_header_path(parts, directory=False),
                    directory=False,
                    identity=identity,
                )
            )
        else:
            raise NativeWorkspaceArchiveError("workspace contains an unsupported entry type")
    _check_cancel(cancel_check)
    if not _same_identity(os.fstat(descriptor), directory_identity):
        raise NativeWorkspaceArchiveError("workspace directory changed")


def _scan(
    root_descriptor: int,
    *,
    cancel_check: CancellationCheck | None = None,
) -> tuple[tuple[_Entry, ...], int]:
    entries: list[_Entry] = []
    extracted_bytes = [0]
    _scan_directory(
        root_descriptor,
        prefix=(),
        entries=entries,
        seen_directories=set(),
        extracted_bytes=extracted_bytes,
        entry_budget=[MAX_WORKSPACE_ENTRIES],
        cancel_check=cancel_check,
    )
    _check_cancel(cancel_check)
    entries.sort(key=lambda entry: entry.header_path)
    if len({entry.logical_path for entry in entries}) != len(entries):
        raise NativeWorkspaceArchiveError("workspace contains duplicate paths")
    estimated_size = 2 * _BLOCK_SIZE
    for entry in entries:
        estimated_size += _BLOCK_SIZE
        if not entry.directory:
            estimated_size += entry.identity.size
            estimated_size += (-entry.identity.size) % _BLOCK_SIZE
        if estimated_size > MAX_WORKSPACE_UPLOAD_BYTES:
            raise NativeWorkspaceArchiveError("workspace archive byte-size budget exceeded")
    return tuple(entries), extracted_bytes[0]


def _archive_byte_size(entries: tuple[_Entry, ...]) -> int:
    total = 2 * _BLOCK_SIZE
    for entry in entries:
        total += _BLOCK_SIZE
        if not entry.directory:
            total += entry.identity.size
            total += (-entry.identity.size) % _BLOCK_SIZE
    return total


def _open_entry(
    root_descriptor: int,
    entry: _Entry,
    directories: dict[str, _Entry],
    *,
    cancel_check: CancellationCheck | None = None,
) -> int:
    descriptor = os.dup(root_descriptor)
    try:
        for index, name in enumerate(entry.parts):
            _check_cancel(cancel_check)
            final = index == len(entry.parts) - 1
            flags = _DIRECTORY_FLAGS if (not final or entry.directory) else _FILE_FLAGS
            child = os.open(name, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            expected = entry if final else directories["/".join(entry.parts[: index + 1])]
            if not _same_identity(os.fstat(descriptor), expected.identity):
                raise NativeWorkspaceArchiveError("workspace entry changed")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _write_archive(
    root_descriptor: int,
    entries: tuple[_Entry, ...],
    archive: int,
    *,
    cancel_check: CancellationCheck | None = None,
    progress_observer: ProgressObserver | None = None,
) -> None:
    directories = {entry.logical_path: entry for entry in entries if entry.directory}
    total_bytes = _archive_byte_size(entries)
    completed_bytes = 0
    reported_bytes = 0
    report_step = max(64 * 1024, total_bytes // 100)

    def progress(written: int) -> None:
        nonlocal completed_bytes, reported_bytes
        completed_bytes += written
        if progress_observer is not None and (
            completed_bytes == total_bytes or completed_bytes - reported_bytes >= report_step
        ):
            progress_observer(completed_bytes, total_bytes)
            reported_bytes = completed_bytes

    if progress_observer is not None:
        progress_observer(0, total_bytes)
    for entry in entries:
        _check_cancel(cancel_check)
        opened = _open_entry(
            root_descriptor,
            entry,
            directories,
            cancel_check=cancel_check,
        )
        try:
            _write_all(
                archive,
                _tar_header(entry),
                cancel_check=cancel_check,
                progress=progress,
            )
            if entry.directory:
                continue
            offset = 0
            while offset < entry.identity.size:
                _check_cancel(cancel_check)
                try:
                    chunk = os.pread(
                        opened,
                        min(_COPY_BYTES, entry.identity.size - offset),
                        offset,
                    )
                except InterruptedError:
                    continue
                if not chunk:
                    raise NativeWorkspaceArchiveError("workspace file changed while reading")
                _write_all(
                    archive,
                    chunk,
                    cancel_check=cancel_check,
                    progress=progress,
                )
                offset += len(chunk)
            if os.pread(opened, 1, entry.identity.size):
                raise NativeWorkspaceArchiveError("workspace file changed while reading")
            if not _same_identity(os.fstat(opened), entry.identity):
                raise NativeWorkspaceArchiveError("workspace file changed while reading")
            padding = (-entry.identity.size) % _BLOCK_SIZE
            if padding:
                _write_all(
                    archive,
                    bytes(padding),
                    cancel_check=cancel_check,
                    progress=progress,
                )
        finally:
            os.close(opened)
    _write_all(
        archive,
        bytes(2 * _BLOCK_SIZE),
        cancel_check=cancel_check,
        progress=progress,
    )
    if completed_bytes != total_bytes:
        raise NativeWorkspaceArchiveError("workspace archive progress is inconsistent")


def _sha256(
    descriptor: int,
    size: int,
    *,
    cancel_check: CancellationCheck | None = None,
) -> str:
    digest = hashlib.sha256()
    offset = 0
    while offset < size:
        _check_cancel(cancel_check)
        try:
            chunk = os.pread(descriptor, min(_COPY_BYTES, size - offset), offset)
        except InterruptedError:
            continue
        if not chunk:
            raise NativeWorkspaceArchiveError("workspace archive changed while hashing")
        digest.update(chunk)
        offset += len(chunk)
    if os.pread(descriptor, 1, size):
        raise NativeWorkspaceArchiveError("workspace archive changed while hashing")
    return digest.hexdigest()


@contextmanager
def prepare_native_workspace(
    selected_path: str,
    *,
    import_id: str,
    temporary_root: Path | str,
    expected_device: int,
    expected_inode: int,
    cancel_check: CancellationCheck | None = None,
    progress_observer: ProgressObserver | None = None,
) -> Iterator[PreparedNativeWorkspace]:
    """Yield one private deterministic tar and its exact opaque contract ref."""

    if type(import_id) is not str or _IMPORT_ID_RE.fullmatch(import_id) is None:
        raise NativeWorkspaceArchiveError("workspace import identity is invalid")
    if (
        type(expected_device) is not int
        or type(expected_inode) is not int
        or expected_device < 0
        or expected_inode <= 0
    ):
        raise NativeWorkspaceArchiveError("workspace identity is invalid")
    root_descriptor, display_name = _open_selected_root(
        selected_path,
        expected_device=expected_device,
        expected_inode=expected_inode,
        cancel_check=cancel_check,
    )
    try:
        entries, extracted_bytes = _scan(root_descriptor, cancel_check=cancel_check)
        with tempfile.TemporaryFile(mode="w+b", dir=temporary_root) as stream:
            os.fchmod(stream.fileno(), 0o600)
            _write_archive(
                root_descriptor,
                entries,
                stream.fileno(),
                cancel_check=cancel_check,
                progress_observer=progress_observer,
            )
            os.fsync(stream.fileno())
            _after_archive_write(root_descriptor)
            _check_cancel(cancel_check)
            verified_entries, verified_extracted_bytes = _scan(
                root_descriptor,
                cancel_check=cancel_check,
            )
            if verified_entries != entries or verified_extracted_bytes != extracted_bytes:
                raise NativeWorkspaceArchiveError("workspace changed while creating its snapshot")
            archive_status = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(archive_status.st_mode)
                or archive_status.st_size < 1024
                or archive_status.st_size > MAX_WORKSPACE_UPLOAD_BYTES
                or archive_status.st_size % _BLOCK_SIZE != 0
            ):
                raise NativeWorkspaceArchiveError("workspace archive size is invalid")
            import_ref = WorkspaceImportRefV1(
                import_id=import_id,
                content_sha256=_sha256(
                    stream.fileno(),
                    archive_status.st_size,
                    cancel_check=cancel_check,
                ),
                byte_size=archive_status.st_size,
                entry_count=len(entries),
                extracted_byte_size=extracted_bytes,
            )
            os.lseek(stream.fileno(), 0, os.SEEK_SET)
            yield PreparedNativeWorkspace(
                display_name=display_name,
                import_ref=import_ref,
                stream=stream,
            )
    except OSError as exc:
        raise NativeWorkspaceArchiveError("workspace could not be read safely") from exc
    finally:
        os.close(root_descriptor)


__all__ = (
    "NativeWorkspaceArchiveCancelled",
    "NativeWorkspaceArchiveError",
    "PreparedNativeWorkspace",
    "ProgressObserver",
    "prepare_native_workspace",
)
