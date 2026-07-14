from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import unicodedata
import uuid

from .models import WorkspaceArchiveDeclarationV1


_BLOCK_SIZE = 512
_ZERO_BLOCK = b"\0" * _BLOCK_SIZE
_OCTAL_7 = re.compile(rb"^[0-7]{7}\0$")
_OCTAL_11 = re.compile(rb"^[0-7]{11}\0$")
_CHECKSUM = re.compile(rb"^[0-7]{6}\0 $")


class WorkspaceArchiveError(ValueError):
    """The uploaded archive does not satisfy deterministic workspace tar v1."""


def verify_and_materialize_workspace(
    archive_path: Path,
    declaration: WorkspaceArchiveDeclarationV1,
    destination: Path,
) -> None:
    """Verify the complete canonical ustar and atomically publish extracted files."""

    stream, archive_identity = _open_verified_archive(archive_path, declaration)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination.parent / f".workspace-{uuid.uuid4().hex}.tmp"
    temporary.mkdir(mode=0o700)
    os.chmod(temporary, 0o700, follow_symlinks=False)
    try:
        _parse_archive(stream, declaration, extract_root=temporary, verify_root_fd=None)
        _fsync_tree(temporary)
        os.rename(temporary, destination)
        _fsync_directory(destination.parent)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    finally:
        _close_verified_archive(archive_path, stream, archive_identity)


def verify_workspace_archive(
    archive_path: Path,
    declaration: WorkspaceArchiveDeclarationV1,
) -> None:
    stream, archive_identity = _open_verified_archive(archive_path, declaration)
    try:
        _parse_archive(stream, declaration, extract_root=None, verify_root_fd=None)
    finally:
        _close_verified_archive(archive_path, stream, archive_identity)


def verify_materialized_workspace(
    archive_path: Path,
    declaration: WorkspaceArchiveDeclarationV1,
    *,
    archive_root_fd: int | None = None,
    archive_name: str | None = None,
    workspace_root_fd: int,
    snapshot_name: str,
) -> None:
    """Verify one published snapshot through a held managed-root directory FD."""

    stream, archive_identity = _open_verified_archive(
        archive_path,
        declaration,
        parent_fd=archive_root_fd,
        entry_name=archive_name,
    )
    snapshot_fd: int | None = None
    try:
        try:
            snapshot_fd = os.open(
                snapshot_name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=workspace_root_fd,
            )
        except OSError as exc:
            raise WorkspaceArchiveError("published workspace snapshot is missing or unsafe") from exc
        snapshot_identity = os.fstat(snapshot_fd)
        _validate_directory_metadata(snapshot_identity, mode=0o700, label="snapshot root")
        _require_entry_binding(workspace_root_fd, snapshot_name, snapshot_identity)
        _parse_archive(
            stream,
            declaration,
            extract_root=None,
            verify_root_fd=snapshot_fd,
        )
        _require_entry_binding(workspace_root_fd, snapshot_name, snapshot_identity)
    finally:
        if snapshot_fd is not None:
            os.close(snapshot_fd)
        _close_verified_archive(
            archive_path,
            stream,
            archive_identity,
            parent_fd=archive_root_fd,
            entry_name=archive_name,
        )


def _open_verified_archive(
    archive_path: Path,
    declaration: WorkspaceArchiveDeclarationV1,
    *,
    parent_fd: int | None = None,
    entry_name: str | None = None,
):
    try:
        if parent_fd is None:
            fd = os.open(archive_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        elif entry_name is not None:
            fd = os.open(
                entry_name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
        else:
            raise WorkspaceArchiveError("workspace archive entry identity is incomplete")
    except OSError as exc:
        raise WorkspaceArchiveError("workspace archive is missing or unsafe") from exc
    stream = os.fdopen(fd, "rb", buffering=1024 * 1024)
    try:
        metadata = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
        ):
            raise WorkspaceArchiveError("workspace archive must be an owner-bound regular file")
        if parent_fd is None:
            _require_path_binding(archive_path, metadata)
        else:
            assert entry_name is not None
            _require_entry_binding(parent_fd, entry_name, metadata)
        if metadata.st_size != declaration.byte_size:
            raise WorkspaceArchiveError("workspace archive size differs from its declaration")
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        if digest.hexdigest() != declaration.content_sha256:
            raise WorkspaceArchiveError("workspace archive digest differs from its declaration")
        stream.seek(0)
        return stream, metadata
    except Exception:
        stream.close()
        raise


def _close_verified_archive(
    archive_path: Path,
    stream,
    identity: os.stat_result,
    *,
    parent_fd: int | None = None,
    entry_name: str | None = None,
) -> None:
    try:
        if not _same_identity(os.fstat(stream.fileno()), identity):
            raise WorkspaceArchiveError("workspace archive identity changed during verification")
        if parent_fd is None:
            _require_path_binding(archive_path, identity)
        else:
            assert entry_name is not None
            _require_entry_binding(parent_fd, entry_name, identity)
    finally:
        stream.close()


def _parse_archive(
    stream,
    declaration: WorkspaceArchiveDeclarationV1,
    *,
    extract_root: Path | None,
    verify_root_fd: int | None,
) -> None:
    entries: set[str] = set()
    directories: set[str] = set()
    expected_children: dict[str, set[str]] = {"": set()}
    previous_header_path: bytes | None = None
    entry_count = 0
    extracted_bytes = 0

    while True:
        header = stream.read(_BLOCK_SIZE)
        if len(header) != _BLOCK_SIZE:
            raise WorkspaceArchiveError("workspace archive ended before its terminator")
        if header == _ZERO_BLOCK:
            if stream.read(_BLOCK_SIZE) != _ZERO_BLOCK or stream.read(1) != b"":
                raise WorkspaceArchiveError(
                    "workspace archive requires exactly two zero blocks and no trailing data"
                )
            break

        entry = _parse_header(header)
        header_path = entry.header_path
        if previous_header_path is not None and header_path <= previous_header_path:
            raise WorkspaceArchiveError("workspace entries are not in canonical byte order")
        previous_header_path = header_path
        logical_path = entry.logical_path
        if logical_path in entries:
            raise WorkspaceArchiveError("workspace paths must be unique")
        entries.add(logical_path)
        _validate_parents(logical_path, directories)
        parent, _, name = logical_path.rpartition("/")
        expected_children.setdefault(parent, set()).add(name)

        entry_count += 1
        if entry_count > declaration.policy.max_entries:
            raise WorkspaceArchiveError("workspace archive exceeds its entry budget")
        extracted_bytes += entry.size
        if extracted_bytes > declaration.policy.max_extracted_bytes:
            raise WorkspaceArchiveError("workspace archive exceeds its extracted byte budget")

        target = extract_root / logical_path if extract_root is not None else None
        if entry.directory:
            directories.add(logical_path)
            expected_children.setdefault(logical_path, set())
            if target is not None:
                target.mkdir(mode=0o755)
                os.chmod(target, entry.mode, follow_symlinks=False)
            if verify_root_fd is not None:
                directory_fd = _open_directory_at(verify_root_fd, logical_path)
                try:
                    _validate_directory_metadata(
                        os.fstat(directory_fd), mode=entry.mode, label="workspace directory"
                    )
                finally:
                    os.close(directory_fd)
            continue

        remaining = entry.size
        output_fd: int | None = None
        verified_fd: int | None = None
        try:
            if target is not None:
                output_fd = os.open(
                    target,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
            if verify_root_fd is not None:
                verified_fd = _open_file_at(verify_root_fd, logical_path)
                _validate_file_metadata(
                    os.fstat(verified_fd), size=entry.size, mode=entry.mode
                )
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise WorkspaceArchiveError("workspace file body is truncated")
                if output_fd is not None:
                    _write_all(output_fd, chunk)
                if verified_fd is not None and _read_exact(verified_fd, len(chunk)) != chunk:
                    raise WorkspaceArchiveError("published workspace file content is invalid")
                remaining -= len(chunk)
            padding_size = (-entry.size) % _BLOCK_SIZE
            if padding_size and stream.read(padding_size) != b"\0" * padding_size:
                raise WorkspaceArchiveError("workspace file padding is not canonical")
            if output_fd is not None:
                os.fchmod(output_fd, entry.mode)
                os.fsync(output_fd)
            if verified_fd is not None:
                _validate_file_metadata(
                    os.fstat(verified_fd), size=entry.size, mode=entry.mode
                )
        finally:
            if verified_fd is not None:
                os.close(verified_fd)
            if output_fd is not None:
                os.close(output_fd)

    if entry_count != declaration.entry_count:
        raise WorkspaceArchiveError("workspace entry count differs from its declaration")
    if extracted_bytes != declaration.extracted_byte_size:
        raise WorkspaceArchiveError("workspace extracted size differs from its declaration")
    if verify_root_fd is not None:
        _validate_tree_shape(verify_root_fd, expected_children)


class _Entry:
    def __init__(
        self,
        *,
        header_path: bytes,
        logical_path: str,
        size: int,
        mode: int,
        directory: bool,
    ) -> None:
        self.header_path = header_path
        self.logical_path = logical_path
        self.size = size
        self.mode = mode
        self.directory = directory


def _parse_header(header: bytes) -> _Entry:
    if len(header) != _BLOCK_SIZE:
        raise WorkspaceArchiveError("workspace header has the wrong size")
    checksum_field = header[148:156]
    if _CHECKSUM.fullmatch(checksum_field) is None:
        raise WorkspaceArchiveError("workspace header checksum field is not canonical")
    expected_checksum = int(checksum_field[:6], 8)
    checksum_header = bytearray(header)
    checksum_header[148:156] = b"        "
    if sum(checksum_header) != expected_checksum:
        raise WorkspaceArchiveError("workspace header checksum is invalid")

    entry_type = header[156:157]
    if entry_type not in {b"0", b"5"}:
        raise WorkspaceArchiveError("workspace archive contains a forbidden entry type")
    directory = entry_type == b"5"
    mode_field = header[100:108]
    allowed_modes = {b"0000755\0"} if directory else {b"0000644\0", b"0000755\0"}
    if mode_field not in allowed_modes:
        raise WorkspaceArchiveError("workspace entry mode is not canonical")
    if any(
        (
            _OCTAL_7.fullmatch(header[108:116]) is None,
            header[108:116] != b"0000000\0",
            header[116:124] != b"0000000\0",
            _OCTAL_11.fullmatch(header[124:136]) is None,
            header[136:148] != b"00000000000\0",
            header[157:257] != b"\0" * 100,
            header[257:263] != b"ustar\0",
            header[263:265] != b"00",
            header[265:329] != b"\0" * 64,
            header[329:337] != b"0000000\0",
            header[337:345] != b"0000000\0",
            header[500:512] != b"\0" * 12,
        )
    ):
        raise WorkspaceArchiveError("workspace header metadata is not canonical")

    size = int(header[124:135], 8)
    if directory and size != 0:
        raise WorkspaceArchiveError("workspace directory size must be zero")
    if size > 0o77777777777:
        raise WorkspaceArchiveError("workspace file exceeds the canonical file-size limit")

    name = _nul_padded_value(header[0:100], "name")
    prefix = _nul_padded_value(header[345:500], "prefix")
    if not name:
        raise WorkspaceArchiveError("workspace header name is empty")
    header_path = prefix + (b"/" if prefix else b"") + name
    expected_name, expected_prefix = _canonical_split(header_path)
    if name != expected_name or prefix != expected_prefix:
        raise WorkspaceArchiveError("workspace header path split is not canonical")
    if len(header_path) > 256:
        raise WorkspaceArchiveError("workspace path exceeds the byte budget")
    if directory:
        if not header_path.endswith(b"/"):
            raise WorkspaceArchiveError("workspace directory header requires a trailing slash")
        logical_bytes = header_path[:-1]
    else:
        if header_path.endswith(b"/"):
            raise WorkspaceArchiveError("workspace file path cannot have a trailing slash")
        logical_bytes = header_path
    try:
        logical_path = logical_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkspaceArchiveError("workspace path is not valid UTF-8") from exc
    _validate_logical_path(logical_path)
    return _Entry(
        header_path=header_path,
        logical_path=logical_path,
        size=size,
        mode=int(mode_field[:7], 8),
        directory=directory,
    )


def _nul_padded_value(field: bytes, label: str) -> bytes:
    value, separator, padding = field.partition(b"\0")
    if separator and any(padding):
        raise WorkspaceArchiveError(f"workspace {label} padding is not NUL")
    return value if separator else field


def _canonical_split(header_path: bytes) -> tuple[bytes, bytes]:
    if len(header_path) <= 100:
        return header_path, b""
    candidates = [
        index
        for index, value in enumerate(header_path)
        if value == ord("/") and 1 <= index <= 155 and 1 <= len(header_path) - index - 1 <= 100
    ]
    if not candidates:
        raise WorkspaceArchiveError("workspace path has no valid ustar split")
    split = max(candidates)
    return header_path[split + 1 :], header_path[:split]


def _validate_logical_path(path: str) -> None:
    if not path or path.startswith("/") or path.endswith("/") or "\\" in path:
        raise WorkspaceArchiveError("workspace path is not POSIX relative")
    if unicodedata.normalize("NFC", path) != path:
        raise WorkspaceArchiveError("workspace path is not NFC normalized")
    parts = path.split("/")
    if len(parts) > 32 or any(part in {"", ".", ".."} for part in parts):
        raise WorkspaceArchiveError("workspace path violates its segment budget")
    if any(ord(character) < 32 or ord(character) == 127 for character in path):
        raise WorkspaceArchiveError("workspace path contains a control character")


def _validate_parents(path: str, directories: set[str]) -> None:
    parts = path.split("/")
    for index in range(1, len(parts)):
        if "/".join(parts[:index]) not in directories:
            raise WorkspaceArchiveError("workspace parent directory is missing or out of order")


def _open_directory_at(root_fd: int, path: str) -> int:
    current_fd = os.dup(root_fd)
    try:
        for part in path.split("/"):
            next_fd = os.open(
                part,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=current_fd,
            )
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except OSError as exc:
        os.close(current_fd)
        raise WorkspaceArchiveError("published workspace directory is missing or unsafe") from exc


def _open_file_at(root_fd: int, path: str) -> int:
    parent, _, name = path.rpartition("/")
    parent_fd = _open_directory_at(root_fd, parent) if parent else os.dup(root_fd)
    try:
        return os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
    except OSError as exc:
        raise WorkspaceArchiveError("published workspace file is missing or unsafe") from exc
    finally:
        os.close(parent_fd)


def _validate_directory_metadata(metadata: os.stat_result, *, mode: int, label: str) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != mode
    ):
        raise WorkspaceArchiveError(f"published {label} metadata is invalid")


def _validate_file_metadata(metadata: os.stat_result, *, size: int, mode: int) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != mode
        or metadata.st_size != size
    ):
        raise WorkspaceArchiveError("published workspace file metadata is invalid")


def _validate_tree_shape(root_fd: int, expected_children: dict[str, set[str]]) -> None:
    for directory, expected in expected_children.items():
        directory_fd = _open_directory_at(root_fd, directory) if directory else os.dup(root_fd)
        try:
            if set(os.listdir(directory_fd)) != expected:
                raise WorkspaceArchiveError("published workspace tree contains unexpected entries")
        except OSError as exc:
            raise WorkspaceArchiveError("published workspace tree is unreadable") from exc
        finally:
            os.close(directory_fd)


def _read_exact(fd: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _require_path_binding(path: Path, expected: os.stat_result) -> None:
    try:
        current = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise WorkspaceArchiveError("workspace archive path binding is invalid") from exc
    if not _same_identity(current, expected):
        raise WorkspaceArchiveError("workspace archive path binding changed")


def _require_entry_binding(parent_fd: int, name: str, expected: os.stat_result) -> None:
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise WorkspaceArchiveError("published workspace snapshot binding is invalid") from exc
    if not _same_identity(current, expected):
        raise WorkspaceArchiveError("published workspace snapshot binding changed")


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _write_all(fd: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("failed to write workspace file")
        view = view[written:]


def _fsync_tree(root: Path) -> None:
    for directory, _, _ in os.walk(root, topdown=False):
        _fsync_directory(Path(directory))


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


__all__ = [
    "WorkspaceArchiveError",
    "verify_and_materialize_workspace",
    "verify_materialized_workspace",
    "verify_workspace_archive",
]
