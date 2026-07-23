"""Deterministic no-follow workspace archive creation shared by Core services."""

from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
from itertools import islice
import os
from pathlib import Path
import stat
import unicodedata

from openevo.backend.contracts.v2.models import (
    MAX_SNAPSHOT_BYTES,
    MAX_SNAPSHOT_ENTRIES,
    WorkspaceArchiveDeclarationV2,
)


_BLOCK_SIZE = 512
_COPY_BYTES = 1024 * 1024
_MAX_FILE_BYTES = 0o77777777777
_MAX_PATH_BYTES = 256
_MAX_PATH_DEPTH = 32
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


class WorkspaceArchiveBuildError(ValueError):
    """A directory cannot be proven safe for deterministic publication."""


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
    parts: tuple[str, ...]
    logical_path: str
    header_path: bytes
    directory: bool
    identity: _Identity


def _safe_component(value: str) -> str:
    if (
        not value
        or value in {".", ".."}
        or "\\" in value
        or unicodedata.normalize("NFC", value) != value
        or any(unicodedata.category(character) == "Cc" for character in value)
    ):
        raise WorkspaceArchiveBuildError("workspace contains an unsupported path")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise WorkspaceArchiveBuildError("workspace path is not valid UTF-8") from exc
    return value


def _split_header_path(value: bytes) -> tuple[bytes, bytes]:
    if len(value) <= 100:
        return value, b""
    for index in range(len(value) - 1, -1, -1):
        if value[index : index + 1] != b"/":
            continue
        prefix = value[:index]
        name = value[index + 1 :]
        if 1 <= len(prefix) <= 155 and 1 <= len(name) <= 100:
            return name, prefix
    raise WorkspaceArchiveBuildError("workspace path cannot be represented as POSIX ustar")


def _header_path(parts: tuple[str, ...], *, directory: bool) -> bytes:
    if not parts or len(parts) > _MAX_PATH_DEPTH:
        raise WorkspaceArchiveBuildError("workspace path exceeds its depth limit")
    value = "/".join(parts).encode("utf-8") + (b"/" if directory else b"")
    if len(value) > _MAX_PATH_BYTES:
        raise WorkspaceArchiveBuildError("workspace path exceeds its byte limit")
    _split_header_path(value)
    return value


def _same_identity(value: os.stat_result, expected: _Identity) -> bool:
    return _Identity.from_stat(value) == expected


def _open_root(path: Path) -> int:
    absolute = path.expanduser().absolute()
    if absolute == Path("/") or not absolute.is_absolute():
        raise WorkspaceArchiveBuildError("workspace archive root is too broad")
    components = tuple(part for part in absolute.parts if part != "/")
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in components:
            _safe_component(component)
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise WorkspaceArchiveBuildError("workspace archive root is not a directory")
        return descriptor
    except WorkspaceArchiveBuildError:
        os.close(descriptor)
        raise
    except OSError as exc:
        os.close(descriptor)
        raise WorkspaceArchiveBuildError(
            "workspace archive root is unavailable"
        ) from exc
    except BaseException:
        os.close(descriptor)
        raise


def _require_non_sparse(descriptor: int, size: int) -> None:
    if size == 0:
        return
    metadata = os.fstat(descriptor)
    if metadata.st_size != size or metadata.st_blocks * 512 < size:
        raise WorkspaceArchiveBuildError("workspace sparse files are unsupported")
    seek_data = getattr(os, "SEEK_DATA", None)
    seek_hole = getattr(os, "SEEK_HOLE", None)
    if type(seek_data) is not int or type(seek_hole) is not int:
        raise WorkspaceArchiveBuildError("workspace sparse-file detection is unavailable")
    try:
        first_data = os.lseek(descriptor, 0, seek_data)
        first_hole = os.lseek(descriptor, 0, seek_hole)
    except OSError as exc:
        if exc.errno == errno.ENXIO:
            raise WorkspaceArchiveBuildError("workspace sparse files are unsupported") from None
        raise WorkspaceArchiveBuildError("workspace sparse-file detection failed") from exc
    if first_data != 0 or first_hole != size:
        raise WorkspaceArchiveBuildError("workspace sparse files are unsupported")


def _scan_directory(
    descriptor: int,
    *,
    prefix: tuple[str, ...],
    entries: list[_Entry],
    seen_directories: set[tuple[int, int]],
    remaining_entries: list[int],
    extracted_bytes: list[int],
) -> None:
    before = os.fstat(descriptor)
    directory_identity = _Identity.from_stat(before)
    key = (before.st_dev, before.st_ino)
    if key in seen_directories:
        raise WorkspaceArchiveBuildError("workspace contains a repeated directory")
    seen_directories.add(key)
    try:
        with os.scandir(descriptor) as iterator:
            children = list(islice(iterator, remaining_entries[0] + 1))
    except OSError as exc:
        raise WorkspaceArchiveBuildError("workspace directory cannot be enumerated") from exc
    if len(children) > remaining_entries[0]:
        raise WorkspaceArchiveBuildError("workspace entry budget exceeded")
    remaining_entries[0] -= len(children)
    children.sort(key=lambda child: os.fsencode(child.name))
    for child in children:
        if type(child.name) is not str:
            raise WorkspaceArchiveBuildError("workspace path is not valid UTF-8")
        name = _safe_component(child.name)
        parts = prefix + (name,)
        try:
            metadata = child.stat(follow_symlinks=False)
        except OSError as exc:
            raise WorkspaceArchiveBuildError("workspace entry cannot be inspected") from exc
        identity = _Identity.from_stat(metadata)
        if stat.S_ISDIR(metadata.st_mode):
            entry = _Entry(
                parts=parts,
                logical_path="/".join(parts),
                header_path=_header_path(parts, directory=True),
                directory=True,
                identity=identity,
            )
            entries.append(entry)
            try:
                opened = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except OSError as exc:
                raise WorkspaceArchiveBuildError("workspace directory changed") from exc
            try:
                if not _same_identity(os.fstat(opened), identity):
                    raise WorkspaceArchiveBuildError("workspace directory changed")
                _scan_directory(
                    opened,
                    prefix=parts,
                    entries=entries,
                    seen_directories=seen_directories,
                    remaining_entries=remaining_entries,
                    extracted_bytes=extracted_bytes,
                )
                if not _same_identity(os.fstat(opened), identity):
                    raise WorkspaceArchiveBuildError("workspace directory changed")
            finally:
                os.close(opened)
        elif stat.S_ISREG(metadata.st_mode):
            if metadata.st_nlink != 1:
                raise WorkspaceArchiveBuildError("workspace files must have one link")
            if not 0 <= metadata.st_size <= _MAX_FILE_BYTES:
                raise WorkspaceArchiveBuildError("workspace file exceeds its size limit")
            try:
                opened = os.open(name, _FILE_FLAGS, dir_fd=descriptor)
            except OSError as exc:
                raise WorkspaceArchiveBuildError("workspace file changed") from exc
            try:
                if not _same_identity(os.fstat(opened), identity):
                    raise WorkspaceArchiveBuildError("workspace file changed")
                _require_non_sparse(opened, metadata.st_size)
                if not _same_identity(os.fstat(opened), identity):
                    raise WorkspaceArchiveBuildError("workspace file changed")
            finally:
                os.close(opened)
            if metadata.st_size > MAX_SNAPSHOT_BYTES - extracted_bytes[0]:
                raise WorkspaceArchiveBuildError("workspace extracted-byte budget exceeded")
            extracted_bytes[0] += metadata.st_size
            entries.append(
                _Entry(
                    parts=parts,
                    logical_path="/".join(parts),
                    header_path=_header_path(parts, directory=False),
                    directory=False,
                    identity=identity,
                )
            )
        else:
            raise WorkspaceArchiveBuildError("workspace contains an unsupported entry type")
    if not _same_identity(os.fstat(descriptor), directory_identity):
        raise WorkspaceArchiveBuildError("workspace directory changed")


def _scan(root_descriptor: int) -> tuple[tuple[_Entry, ...], int]:
    entries: list[_Entry] = []
    extracted_bytes = [0]
    _scan_directory(
        root_descriptor,
        prefix=(),
        entries=entries,
        seen_directories=set(),
        remaining_entries=[MAX_SNAPSHOT_ENTRIES],
        extracted_bytes=extracted_bytes,
    )
    entries.sort(key=lambda entry: entry.header_path)
    if len({entry.logical_path for entry in entries}) != len(entries):
        raise WorkspaceArchiveBuildError("workspace contains duplicate paths")
    estimated = 2 * _BLOCK_SIZE
    for entry in entries:
        estimated += _BLOCK_SIZE
        if not entry.directory:
            estimated += entry.identity.size + (-entry.identity.size) % _BLOCK_SIZE
        if estimated > MAX_SNAPSHOT_BYTES:
            raise WorkspaceArchiveBuildError("workspace archive byte budget exceeded")
    return tuple(entries), extracted_bytes[0]


def _put(header: bytearray, start: int, end: int, value: bytes) -> None:
    if len(value) > end - start:
        raise WorkspaceArchiveBuildError("workspace archive header is invalid")
    header[start:end] = bytes(end - start)
    header[start : start + len(value)] = value


def _tar_header(entry: _Entry) -> bytes:
    size = 0 if entry.directory else entry.identity.size
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
    _put(header, 329, 337, b"0000000\0")
    _put(header, 337, 345, b"0000000\0")
    _put(header, 345, 500, prefix)
    header[148:156] = f"{sum(header):06o}\0 ".encode("ascii")
    return bytes(header)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    offset = 0
    while offset < len(view):
        try:
            written = os.write(descriptor, view[offset:])
        except InterruptedError:
            continue
        if written <= 0:
            raise WorkspaceArchiveBuildError("workspace archive write failed")
        offset += written


def _open_entry(
    root_descriptor: int,
    entry: _Entry,
    directories: dict[str, _Entry],
) -> int:
    descriptor = os.dup(root_descriptor)
    try:
        for index, name in enumerate(entry.parts):
            final = index == len(entry.parts) - 1
            flags = _DIRECTORY_FLAGS if (not final or entry.directory) else _FILE_FLAGS
            child = os.open(name, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            expected = entry if final else directories["/".join(entry.parts[: index + 1])]
            if not _same_identity(os.fstat(descriptor), expected.identity):
                raise WorkspaceArchiveBuildError("workspace entry changed")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _write_entries(root_descriptor: int, entries: tuple[_Entry, ...], output: int) -> None:
    directories = {entry.logical_path: entry for entry in entries if entry.directory}
    for entry in entries:
        opened = _open_entry(root_descriptor, entry, directories)
        try:
            _write_all(output, _tar_header(entry))
            if entry.directory:
                continue
            offset = 0
            while offset < entry.identity.size:
                try:
                    chunk = os.pread(
                        opened,
                        min(_COPY_BYTES, entry.identity.size - offset),
                        offset,
                    )
                except InterruptedError:
                    continue
                if not chunk:
                    raise WorkspaceArchiveBuildError("workspace file changed while reading")
                _write_all(output, chunk)
                offset += len(chunk)
            if os.pread(opened, 1, entry.identity.size):
                raise WorkspaceArchiveBuildError("workspace file changed while reading")
            if not _same_identity(os.fstat(opened), entry.identity):
                raise WorkspaceArchiveBuildError("workspace file changed while reading")
            padding = (-entry.identity.size) % _BLOCK_SIZE
            if padding:
                _write_all(output, bytes(padding))
        finally:
            os.close(opened)
    _write_all(output, bytes(2 * _BLOCK_SIZE))


def _digest_file(descriptor: int, size: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while offset < size:
        chunk = os.pread(descriptor, min(_COPY_BYTES, size - offset), offset)
        if not chunk:
            raise WorkspaceArchiveBuildError("workspace archive changed while hashing")
        digest.update(chunk)
        offset += len(chunk)
    if os.pread(descriptor, 1, size):
        raise WorkspaceArchiveBuildError("workspace archive changed while hashing")
    return digest.hexdigest()


def write_workspace_archive(
    workspace_root: Path | str,
    output_descriptor: int,
) -> WorkspaceArchiveDeclarationV2:
    """Write one canonical tar to an already-owned empty regular file descriptor."""

    if type(output_descriptor) is not int or output_descriptor < 0:
        raise WorkspaceArchiveBuildError("workspace archive output is invalid")
    output_metadata = os.fstat(output_descriptor)
    if (
        not stat.S_ISREG(output_metadata.st_mode)
        or output_metadata.st_uid != os.geteuid()
        or output_metadata.st_nlink != 1
        or stat.S_IMODE(output_metadata.st_mode) != 0o600
        or output_metadata.st_size != 0
        or os.lseek(output_descriptor, 0, os.SEEK_CUR) != 0
    ):
        raise WorkspaceArchiveBuildError(
            "workspace archive output must be a private empty regular file"
        )
    root_descriptor = _open_root(Path(workspace_root))
    try:
        root_identity = _Identity.from_stat(os.fstat(root_descriptor))
        entries, extracted_bytes = _scan(root_descriptor)
        _write_entries(root_descriptor, entries, output_descriptor)
        os.fsync(output_descriptor)
        verified_entries, verified_bytes = _scan(root_descriptor)
        if (
            verified_entries != entries
            or verified_bytes != extracted_bytes
            or not _same_identity(os.fstat(root_descriptor), root_identity)
        ):
            raise WorkspaceArchiveBuildError("workspace changed while creating its archive")
        output_metadata = os.fstat(output_descriptor)
        if (
            output_metadata.st_size < 1024
            or output_metadata.st_size > MAX_SNAPSHOT_BYTES
            or output_metadata.st_size % _BLOCK_SIZE
        ):
            raise WorkspaceArchiveBuildError("workspace archive size is invalid")
        return WorkspaceArchiveDeclarationV2(
            format="openevo_deterministic_tar_v1",
            media_type="application/vnd.openevo.workspace-tar",
            content_sha256=_digest_file(output_descriptor, output_metadata.st_size),
            byte_size=output_metadata.st_size,
            entry_count=len(entries),
            extracted_byte_size=extracted_bytes,
        )
    except OSError as exc:
        raise WorkspaceArchiveBuildError("workspace could not be archived safely") from exc
    finally:
        os.close(root_descriptor)


__all__ = ["WorkspaceArchiveBuildError", "write_workspace_archive"]
