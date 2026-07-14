"""Private storage for native workspace archive handoffs.

This module deliberately accepts open file descriptors instead of host paths.  It
validates the frozen deterministic-tar contract while copying into sidecar-owned
storage, then exposes only the contract's opaque import reference and verified
read handles.
"""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import threading
from typing import BinaryIO, Iterator
import unicodedata

from openevo.backend.contracts.v1.models import (
    MAX_WORKSPACE_ENTRIES,
    MAX_WORKSPACE_UPLOAD_BYTES,
)

from desktop.sidecar.contracts.v1 import WorkspaceImportRefV1


_BLOCK_SIZE = 512
_COPY_CHUNK_BYTES = 1024 * 1024
_MAX_FILE_BYTES = 0o77777777777
_MAX_PATH_BYTES = 256
_MAX_PATH_DEPTH = 32
_METADATA_NAME = "metadata.json"
_ARCHIVE_NAME = "archive.tar"
_METADATA_MAX_BYTES = 4096
_MAX_FLAT_CLEANUP_NODES = 8
_ARCHIVE_TOKEN_XATTR = "user.openevo.workspace-import-token"
_IMPORT_ID_PREFIX = "workspace-import-"
_TEMP_PREFIX = ".workspace-import-tmp-"
_IMPORT_ID_RE = re.compile(r"^workspace-import-[0-9a-f]{48}$")
_DEFAULT_RECONCILE_MAX_NODES = 300_000
_DEFAULT_RECONCILE_MAX_BYTES = 64 * 1024 * 1024 * 1024
_DEFAULT_MAX_RETAINED_IMPORTS = 10_000
_DEFAULT_MAX_RETAINED_ARCHIVE_BYTES = 24 * 1024 * 1024 * 1024
_IMPORT_ID_BYTES = len(os.fsencode(f"{_IMPORT_ID_PREFIX}{'0' * 48}"))
_TEMP_NAME_BYTES = len(os.fsencode(f"{_TEMP_PREFIX}{'0' * 48}"))
_CHILD_NAME_BYTES = len(os.fsencode(_ARCHIVE_NAME)) + len(os.fsencode(_METADATA_NAME))
_RECONCILE_NODES_PER_RETAINED_IMPORT = 5
_RECONCILE_FIXED_BYTES_PER_RETAINED_IMPORT = (
    _IMPORT_ID_BYTES + (2 * _CHILD_NAME_BYTES) + _METADATA_MAX_BYTES
)
_RECONCILE_CRASH_TEMP_NODES = 3
_RECONCILE_CRASH_TEMP_BYTES = _TEMP_NAME_BYTES + _CHILD_NAME_BYTES
_RENAME_NOREPLACE = 1
_RENAME_EXCL = 0x00000004
_ROOT_OPEN_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
_DIR_OPEN_FLAGS = _ROOT_OPEN_FLAGS
_FILE_READ_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK


class WorkspaceImportError(RuntimeError):
    """Base error for the private workspace import store."""


class WorkspaceArchiveValidationError(WorkspaceImportError, ValueError):
    """The native handoff is not the frozen deterministic archive format."""


class WorkspaceImportIntegrityError(WorkspaceImportError):
    """Sidecar-owned workspace import state failed an integrity check."""


class WorkspaceImportNotFoundError(WorkspaceImportError, LookupError):
    """The requested opaque import is not present."""


class WorkspaceImportStoreConfigurationError(WorkspaceImportError):
    """The private store root is not owner-only or cannot be secured."""


class _ReconcileBudgetExceeded(WorkspaceImportIntegrityError):
    pass


class _DeterministicImportCorruption(WorkspaceImportIntegrityError):
    """State proven invalid from successfully observed filesystem contents."""


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    link_count: int
    size: int
    modified_ns: int
    changed_ns: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> _FileIdentity:
        return cls(
            device=value.st_dev,
            inode=value.st_ino,
            mode=value.st_mode,
            uid=value.st_uid,
            gid=value.st_gid,
            link_count=value.st_nlink,
            size=value.st_size,
            modified_ns=value.st_mtime_ns,
            changed_ns=value.st_ctime_ns,
        )


@dataclass(slots=True)
class _ScanBudget:
    remaining_nodes: int
    remaining_bytes: int

    def charge_node(self, name: str) -> None:
        if self.remaining_nodes <= 0:
            raise _ReconcileBudgetExceeded("workspace import reconciliation node budget exceeded")
        self.remaining_nodes -= 1
        self.charge_bytes(len(os.fsencode(name)))

    def charge_bytes(self, count: int) -> None:
        if count < 0 or count > self.remaining_bytes:
            raise _ReconcileBudgetExceeded("workspace import reconciliation byte budget exceeded")
        self.remaining_bytes -= count


@dataclass(frozen=True, slots=True)
class _RetainedUsage:
    import_count: int
    archive_bytes: int


def _required_reconcile_budget(import_count: int, archive_bytes: int) -> tuple[int, int]:
    """Return the worst reconciliation cost for retained imports plus one crash temp."""

    return (
        _RECONCILE_CRASH_TEMP_NODES
        + (import_count * _RECONCILE_NODES_PER_RETAINED_IMPORT),
        _RECONCILE_CRASH_TEMP_BYTES
        + (import_count * _RECONCILE_FIXED_BYTES_PER_RETAINED_IMPORT)
        + (2 * archive_bytes),
    )


_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _reset_thread_locks_after_fork() -> None:
    global _THREAD_LOCKS_GUARD
    _THREAD_LOCKS.clear()
    _THREAD_LOCKS_GUARD = threading.Lock()


os.register_at_fork(after_in_child=_reset_thread_locks_after_fork)


def _thread_lock_for(root: str) -> threading.RLock:
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(root, threading.RLock())


def _identity(value: os.stat_result) -> _FileIdentity:
    return _FileIdentity.from_stat(value)


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _write_all(descriptor: int, data: bytes | memoryview) -> None:
    view = memoryview(data)
    while view:
        try:
            written = os.write(descriptor, view)
        except InterruptedError:
            continue
        if written <= 0:
            raise OSError("workspace import write made no progress")
        view = view[written:]


@dataclass(frozen=True, slots=True)
class _StoredMetadata:
    import_ref: WorkspaceImportRefV1
    directory_device: int
    directory_inode: int
    archive_device: int
    archive_inode: int
    archive_token: str


def _canonical_metadata(metadata: _StoredMetadata) -> bytes:
    return json.dumps(
        {
            "import_ref": metadata.import_ref.model_dump(mode="json"),
            "schema_version": "1",
            "storage_identity": {
                "archive_device": metadata.archive_device,
                "archive_inode": metadata.archive_inode,
                "archive_token": metadata.archive_token,
                "directory_device": metadata.directory_device,
                "directory_inode": metadata.directory_inode,
            },
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _rename_noreplace(source: str, destination: str, *, directory_fd: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if sys.platform.startswith("linux"):
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise OSError(errno.ENOSYS, "atomic no-replace rename is unavailable")
        renameat2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        result = renameat2(
            directory_fd,
            source_bytes,
            directory_fd,
            destination_bytes,
            _RENAME_NOREPLACE,
        )
    elif sys.platform == "darwin":
        renameatx_np = getattr(libc, "renameatx_np", None)
        if renameatx_np is None:
            raise OSError(errno.ENOSYS, "atomic no-replace rename is unavailable")
        renameatx_np.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameatx_np.restype = ctypes.c_int
        result = renameatx_np(
            directory_fd,
            source_bytes,
            directory_fd,
            destination_bytes,
            _RENAME_EXCL,
        )
    else:
        raise OSError(errno.ENOSYS, "atomic no-replace rename is unavailable")
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def _after_archive_fsync(_temporary_descriptor: int) -> None:
    """Private fault-injection hook."""


def _after_metadata_fsync(_temporary_descriptor: int) -> None:
    """Private fault-injection hook."""


def _before_import_publish(_root_descriptor: int, _import_id: str) -> None:
    """Private fault-injection hook."""


def _after_import_publish(_root_descriptor: int, _import_id: str) -> None:
    """Private fault-injection hook."""


class _ArchiveReader:
    def __init__(self, source: int, destination: int, source_size: int) -> None:
        self.source = source
        self.destination = destination
        self.source_size = source_size
        self.offset = 0
        self.digest = hashlib.sha256()

    def read_exact(self, count: int, *, label: str) -> bytes:
        if count < 0 or self.offset + count > self.source_size:
            raise WorkspaceArchiveValidationError(f"truncated workspace archive {label}")
        chunks: list[bytes] = []
        remaining = count
        while remaining:
            try:
                chunk = os.pread(
                    self.source,
                    min(remaining, _COPY_CHUNK_BYTES),
                    self.offset,
                )
            except InterruptedError:
                continue
            if not chunk:
                raise WorkspaceArchiveValidationError(f"truncated workspace archive {label}")
            self.offset += len(chunk)
            remaining -= len(chunk)
            self.digest.update(chunk)
            _write_all(self.destination, chunk)
            chunks.append(chunk)
        return b"".join(chunks)

    def copy_body(self, count: int) -> None:
        remaining = count
        while remaining:
            chunk_size = min(remaining, _COPY_CHUNK_BYTES)
            self.read_exact(chunk_size, label="file body")
            remaining -= chunk_size


@dataclass(frozen=True, slots=True)
class _ArchiveResult:
    content_sha256: str
    byte_size: int
    entry_count: int
    extracted_byte_size: int


def _source_sha256(descriptor: int, size: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while offset < size:
        try:
            chunk = os.pread(descriptor, min(size - offset, _COPY_CHUNK_BYTES), offset)
        except InterruptedError:
            continue
        if not chunk:
            raise WorkspaceArchiveValidationError("workspace import source changed while rehashing")
        digest.update(chunk)
        offset += len(chunk)
    if os.pread(descriptor, 1, size):
        raise WorkspaceArchiveValidationError("workspace import source grew while rehashing")
    return digest.hexdigest()


def _field_bytes(field: bytes, *, label: str, allow_empty: bool) -> bytes:
    nul = field.find(b"\0")
    if nul < 0:
        value = field
    else:
        value = field[:nul]
        if any(field[nul:]):
            raise WorkspaceArchiveValidationError(f"non-canonical {label} padding")
    if not value and not allow_empty:
        raise WorkspaceArchiveValidationError(f"empty workspace archive {label}")
    return value


def _octal_value(field: bytes, digits: int, *, label: str) -> int:
    if len(field) != digits + 1 or field[-1:] != b"\0":
        raise WorkspaceArchiveValidationError(f"non-canonical {label}")
    digits_bytes = field[:-1]
    if any(value < ord("0") or value > ord("7") for value in digits_bytes):
        raise WorkspaceArchiveValidationError(f"non-octal {label}")
    value = int(digits_bytes, 8)
    if field != f"{value:0{digits}o}\0".encode("ascii"):
        raise WorkspaceArchiveValidationError(f"non-canonical {label}")
    return value


def _canonical_path_fields(header_path: bytes) -> tuple[bytes, bytes]:
    if len(header_path) <= 100:
        return header_path, b""
    for index in range(len(header_path) - 1, -1, -1):
        if header_path[index] != ord("/"):
            continue
        prefix = header_path[:index]
        name = header_path[index + 1 :]
        if 1 <= len(prefix) <= 155 and 1 <= len(name) <= 100:
            return name, prefix
    raise WorkspaceArchiveValidationError("workspace path has no canonical ustar split")


def _validate_logical_path(header_path: bytes, *, is_directory: bool) -> tuple[str, tuple[str, ...]]:
    if len(header_path) > _MAX_PATH_BYTES:
        raise WorkspaceArchiveValidationError("workspace archive path exceeds 256 bytes")
    if is_directory:
        if not header_path.endswith(b"/"):
            raise WorkspaceArchiveValidationError("directory header path must end with slash")
        logical_bytes = header_path[:-1]
    else:
        if header_path.endswith(b"/"):
            raise WorkspaceArchiveValidationError("regular-file path must not end with slash")
        logical_bytes = header_path
    try:
        logical_path = logical_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise WorkspaceArchiveValidationError("workspace path is not valid UTF-8") from exc
    if not logical_path or logical_path.startswith("/") or "\\" in logical_path:
        raise WorkspaceArchiveValidationError("workspace path is not safe POSIX-relative")
    if unicodedata.normalize("NFC", logical_path) != logical_path:
        raise WorkspaceArchiveValidationError("workspace path is not NFC-normalized")
    if any(unicodedata.category(character) == "Cc" for character in logical_path):
        raise WorkspaceArchiveValidationError("workspace path contains a control character")
    parts = tuple(logical_path.split("/"))
    if len(parts) > _MAX_PATH_DEPTH:
        raise WorkspaceArchiveValidationError("workspace path exceeds depth limit")
    if any(part in {"", ".", ".."} for part in parts):
        raise WorkspaceArchiveValidationError("workspace path contains an unsafe segment")
    return logical_path, parts


def _parse_header(
    block: bytes,
    *,
    prior_header_path: bytes | None,
    entries: dict[str, bool],
) -> tuple[int, str, bool, bytes]:
    if len(block) != _BLOCK_SIZE:
        raise WorkspaceArchiveValidationError("workspace archive header is not 512 bytes")
    name = _field_bytes(block[0:100], label="name", allow_empty=False)
    prefix = _field_bytes(block[345:500], label="prefix", allow_empty=True)
    mode = block[100:108]
    if block[108:116] != b"0000000\0" or block[116:124] != b"0000000\0":
        raise WorkspaceArchiveValidationError("workspace archive uid/gid must be zero")
    size = _octal_value(block[124:136], 11, label="size")
    if block[136:148] != b"00000000000\0":
        raise WorkspaceArchiveValidationError("workspace archive mtime must be zero")
    checksum = sum(block[:148]) + (8 * ord(" ")) + sum(block[156:])
    expected_checksum = f"{checksum:06o}\0 ".encode("ascii")
    if len(expected_checksum) != 8 or block[148:156] != expected_checksum:
        raise WorkspaceArchiveValidationError("workspace archive checksum is not canonical")
    typeflag = block[156:157]
    if typeflag not in {b"0", b"5"}:
        raise WorkspaceArchiveValidationError("workspace archive entry type is forbidden")
    is_directory = typeflag == b"5"
    if is_directory:
        if mode != b"0000755\0" or size != 0:
            raise WorkspaceArchiveValidationError("directory mode and size must be canonical")
    elif mode not in {b"0000644\0", b"0000755\0"}:
        raise WorkspaceArchiveValidationError("regular-file mode must be 0644 or 0755")
    if size > _MAX_FILE_BYTES:
        raise WorkspaceArchiveValidationError("workspace archive file exceeds size limit")
    if any(block[157:257]) or block[257:263] != b"ustar\0" or block[263:265] != b"00":
        raise WorkspaceArchiveValidationError("workspace archive is not canonical POSIX ustar")
    if any(block[265:345]) or any(block[500:512]):
        raise WorkspaceArchiveValidationError("workspace archive unused header fields must be zero")
    header_path = prefix + (b"/" if prefix else b"") + name
    expected_name, expected_prefix = _canonical_path_fields(header_path)
    if name != expected_name or prefix != expected_prefix:
        raise WorkspaceArchiveValidationError("workspace path does not use the canonical ustar split")
    logical_path, parts = _validate_logical_path(header_path, is_directory=is_directory)
    if prior_header_path is not None and header_path <= prior_header_path:
        raise WorkspaceArchiveValidationError("workspace entries are not in header-path byte order")
    if logical_path in entries:
        raise WorkspaceArchiveValidationError("workspace archive contains a duplicate logical path")
    for index in range(1, len(parts)):
        parent = "/".join(parts[:index])
        if entries.get(parent) is not True:
            raise WorkspaceArchiveValidationError("workspace parent directory is missing or late")
    entries[logical_path] = is_directory
    return size, logical_path, is_directory, header_path


def _copy_and_validate_archive(
    source: int,
    destination: int,
    source_size: int,
    *,
    max_entries: int,
    max_extracted_bytes: int,
) -> _ArchiveResult:
    reader = _ArchiveReader(source, destination, source_size)
    entries: dict[str, bool] = {}
    prior_header_path: bytes | None = None
    extracted_bytes = 0
    while True:
        block = reader.read_exact(_BLOCK_SIZE, label="header")
        if not any(block):
            second = reader.read_exact(_BLOCK_SIZE, label="end marker")
            if any(second):
                raise WorkspaceArchiveValidationError("workspace archive needs two zero end blocks")
            if reader.offset != source_size:
                raise WorkspaceArchiveValidationError("workspace archive has trailing bytes")
            break
        if len(entries) >= max_entries:
            raise WorkspaceArchiveValidationError("workspace archive entry budget exceeded")
        size, _logical_path, is_directory, header_path = _parse_header(
            block,
            prior_header_path=prior_header_path,
            entries=entries,
        )
        prior_header_path = header_path
        if not is_directory:
            if size > max_extracted_bytes - extracted_bytes:
                raise WorkspaceArchiveValidationError("workspace extracted-byte budget exceeded")
            extracted_bytes += size
            reader.copy_body(size)
            padding_size = (-size) % _BLOCK_SIZE
            if padding_size:
                padding = reader.read_exact(padding_size, label="file padding")
                if any(padding):
                    raise WorkspaceArchiveValidationError("workspace file padding is not zero")
    return _ArchiveResult(
        content_sha256=reader.digest.hexdigest(),
        byte_size=reader.offset,
        entry_count=len(entries),
        extracted_byte_size=extracted_bytes,
    )


class WorkspaceImportStore:
    """Owner-only sidecar store for opaque, deterministic workspace archives."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        max_archive_bytes: int = MAX_WORKSPACE_UPLOAD_BYTES,
        max_entries: int = MAX_WORKSPACE_ENTRIES,
        max_extracted_bytes: int = MAX_WORKSPACE_UPLOAD_BYTES,
        max_retained_imports: int = _DEFAULT_MAX_RETAINED_IMPORTS,
        max_retained_archive_bytes: int = _DEFAULT_MAX_RETAINED_ARCHIVE_BYTES,
        reconcile_max_nodes: int = _DEFAULT_RECONCILE_MAX_NODES,
        reconcile_max_bytes: int = _DEFAULT_RECONCILE_MAX_BYTES,
    ) -> None:
        if not 1024 <= max_archive_bytes <= MAX_WORKSPACE_UPLOAD_BYTES:
            raise ValueError("max_archive_bytes is outside the workspace contract")
        if not 1 <= max_entries <= MAX_WORKSPACE_ENTRIES:
            raise ValueError("max_entries is outside the workspace contract")
        if not 0 <= max_extracted_bytes <= MAX_WORKSPACE_UPLOAD_BYTES:
            raise ValueError("max_extracted_bytes is outside the workspace contract")
        if reconcile_max_nodes <= 0 or reconcile_max_bytes <= 0:
            raise ValueError("reconciliation budgets must be positive")
        if max_retained_imports < 0 or max_retained_archive_bytes < 0:
            raise ValueError("retained workspace import limits must be non-negative")
        required_nodes, required_bytes = _required_reconcile_budget(
            max_retained_imports,
            max_retained_archive_bytes,
        )
        if required_nodes > reconcile_max_nodes or required_bytes > reconcile_max_bytes:
            raise ValueError(
                "retained workspace import limits exceed reconciliation budgets"
            )
        self._root = os.path.abspath(os.fspath(root))
        self._max_archive_bytes = max_archive_bytes
        self._max_entries = max_entries
        self._max_extracted_bytes = max_extracted_bytes
        self._max_retained_imports = max_retained_imports
        self._max_retained_archive_bytes = max_retained_archive_bytes
        self._reconcile_max_nodes = reconcile_max_nodes
        self._reconcile_max_bytes = reconcile_max_bytes
        self._prepare_root()
        self.reconcile()

    def _prepare_root(self) -> None:
        created = False
        try:
            os.mkdir(self._root, 0o700)
            created = True
        except FileExistsError:
            pass
        try:
            descriptor = os.open(self._root, _ROOT_OPEN_FLAGS)
        except OSError as exc:
            raise WorkspaceImportStoreConfigurationError(
                "workspace import root must be a no-follow directory"
            ) from exc
        try:
            if created:
                os.fchmod(descriptor, 0o700)
            self._require_private_directory(os.fstat(descriptor), label="workspace import root")
            path_status = os.stat(self._root, follow_symlinks=False)
            if not _same_inode(os.fstat(descriptor), path_status):
                raise WorkspaceImportStoreConfigurationError(
                    "workspace import root changed while it was opened"
                )
        finally:
            os.close(descriptor)

    @staticmethod
    def _require_private_directory(value: os.stat_result, *, label: str) -> None:
        if not stat.S_ISDIR(value.st_mode):
            raise WorkspaceImportStoreConfigurationError(f"{label} is not a directory")
        if value.st_uid != os.geteuid() or stat.S_IMODE(value.st_mode) != 0o700:
            raise WorkspaceImportStoreConfigurationError(f"{label} must be owner-only mode 0700")

    @staticmethod
    def _require_private_file(value: os.stat_result, *, label: str) -> None:
        if not stat.S_ISREG(value.st_mode):
            raise _DeterministicImportCorruption(f"{label} is not a regular file")
        if (
            value.st_uid != os.geteuid()
            or stat.S_IMODE(value.st_mode) != 0o600
            or value.st_nlink != 1
        ):
            raise _DeterministicImportCorruption(
                f"{label} is not private mode 0600 link-count one"
            )

    @staticmethod
    def _require_stored_private_directory(value: os.stat_result, *, label: str) -> None:
        if not stat.S_ISDIR(value.st_mode):
            raise _DeterministicImportCorruption(f"{label} is not a directory")
        if value.st_uid != os.geteuid() or stat.S_IMODE(value.st_mode) != 0o700:
            raise _DeterministicImportCorruption(f"{label} must be owner-only mode 0700")

    @contextmanager
    def _locked_root(self) -> Iterator[int]:
        with _thread_lock_for(self._root):
            descriptor = os.open(self._root, _ROOT_OPEN_FLAGS)
            locked = False
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                locked = True
                opened = os.fstat(descriptor)
                self._require_private_directory(opened, label="workspace import root")
                current = os.stat(self._root, follow_symlinks=False)
                if not _same_inode(opened, current):
                    raise WorkspaceImportIntegrityError("workspace import root binding changed")
                yield descriptor
                current = os.stat(self._root, follow_symlinks=False)
                if not _same_inode(opened, current):
                    raise WorkspaceImportIntegrityError("workspace import root binding changed")
            finally:
                try:
                    if locked:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

    @staticmethod
    def _source_descriptor(source: int | BinaryIO) -> int:
        if isinstance(source, bool):
            raise TypeError("workspace import source must be an open regular-file FD or stream")
        if isinstance(source, int):
            descriptor = source
        else:
            try:
                descriptor = source.fileno()
            except (AttributeError, OSError) as exc:
                raise TypeError(
                    "workspace import stream must expose an open regular-file descriptor"
                ) from exc
        if not isinstance(descriptor, int) or descriptor < 0:
            raise TypeError("workspace import source descriptor is invalid")
        return descriptor

    def ingest(self, source: int | BinaryIO) -> WorkspaceImportRefV1:
        """Validate and persist one already-open native archive handoff."""

        source_descriptor = self._source_descriptor(source)
        before = os.fstat(source_descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise WorkspaceArchiveValidationError("workspace import source must be a regular file")
        if before.st_size < 1024 or before.st_size > self._max_archive_bytes:
            raise WorkspaceArchiveValidationError("workspace archive byte-size budget exceeded")
        source_identity = _identity(before)
        import_id = f"{_IMPORT_ID_PREFIX}{secrets.token_hex(24)}"
        temporary_name = f"{_TEMP_PREFIX}{secrets.token_hex(24)}"
        published = False
        with self._locked_root() as root_descriptor:
            retained = self._retained_usage(root_descriptor)
            self._require_retained_capacity(
                retained.import_count + 1,
                retained.archive_bytes + before.st_size,
            )
            os.mkdir(temporary_name, 0o700, dir_fd=root_descriptor)
            temporary_descriptor: int | None = None
            archive_descriptor: int | None = None
            try:
                temporary_descriptor = os.open(
                    temporary_name,
                    _DIR_OPEN_FLAGS,
                    dir_fd=root_descriptor,
                )
                os.fchmod(temporary_descriptor, 0o700)
                self._require_private_directory(
                    os.fstat(temporary_descriptor), label="temporary workspace import"
                )
                archive_descriptor = os.open(
                    _ARCHIVE_NAME,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=temporary_descriptor,
                )
                os.fchmod(archive_descriptor, 0o600)
                result = _copy_and_validate_archive(
                    source_descriptor,
                    archive_descriptor,
                    before.st_size,
                    max_entries=self._max_entries,
                    max_extracted_bytes=self._max_extracted_bytes,
                )
                archive_token = secrets.token_hex(32)
                os.setxattr(
                    archive_descriptor,
                    _ARCHIVE_TOKEN_XATTR,
                    bytes.fromhex(archive_token),
                )
                os.fsync(archive_descriptor)
                archive_status = os.fstat(archive_descriptor)
                self._require_private_file(archive_status, label="temporary workspace archive")
                os.close(archive_descriptor)
                archive_descriptor = None
                _after_archive_fsync(temporary_descriptor)
                after = os.fstat(source_descriptor)
                if (
                    _identity(after) != source_identity
                    or _source_sha256(source_descriptor, before.st_size)
                    != result.content_sha256
                    or _identity(os.fstat(source_descriptor)) != source_identity
                ):
                    raise WorkspaceArchiveValidationError(
                        "workspace import source identity changed while reading"
                    )
                import_ref = WorkspaceImportRefV1(
                    import_id=import_id,
                    content_sha256=result.content_sha256,
                    byte_size=result.byte_size,
                    entry_count=result.entry_count,
                    extracted_byte_size=result.extracted_byte_size,
                )
                directory_status = os.fstat(temporary_descriptor)
                stored_metadata = _StoredMetadata(
                    import_ref=import_ref,
                    directory_device=directory_status.st_dev,
                    directory_inode=directory_status.st_ino,
                    archive_device=archive_status.st_dev,
                    archive_inode=archive_status.st_ino,
                    archive_token=archive_token,
                )
                metadata = _canonical_metadata(stored_metadata)
                metadata_descriptor = os.open(
                    _METADATA_NAME,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=temporary_descriptor,
                )
                try:
                    os.fchmod(metadata_descriptor, 0o600)
                    _write_all(metadata_descriptor, metadata)
                    os.fsync(metadata_descriptor)
                finally:
                    os.close(metadata_descriptor)
                _after_metadata_fsync(temporary_descriptor)
                os.fsync(temporary_descriptor)
                _before_import_publish(root_descriptor, import_id)
                _rename_noreplace(temporary_name, import_id, directory_fd=root_descriptor)
                published = True
                _after_import_publish(root_descriptor, import_id)
                self._verify_directory_binding(
                    root_descriptor,
                    import_id,
                    temporary_descriptor,
                    label="published workspace import",
                )
                os.fsync(root_descriptor)
                return import_ref
            finally:
                if archive_descriptor is not None:
                    os.close(archive_descriptor)
                if temporary_descriptor is not None:
                    os.close(temporary_descriptor)
                if not published:
                    self._discard_flat_directory(root_descriptor, temporary_name, missing_ok=True)

    def _require_retained_capacity(self, import_count: int, archive_bytes: int) -> None:
        if import_count > self._max_retained_imports:
            raise WorkspaceImportError("workspace retained import count budget exceeded")
        if archive_bytes > self._max_retained_archive_bytes:
            raise WorkspaceImportError("workspace retained archive byte budget exceeded")
        required_nodes, required_bytes = _required_reconcile_budget(import_count, archive_bytes)
        if required_nodes > self._reconcile_max_nodes or required_bytes > self._reconcile_max_bytes:
            raise WorkspaceImportError("workspace retained reconciliation budget exceeded")

    def _retained_usage(self, root_descriptor: int) -> _RetainedUsage:
        budget = _ScanBudget(
            remaining_nodes=self._reconcile_max_nodes,
            remaining_bytes=self._reconcile_max_bytes,
        )
        import_count = 0
        archive_bytes = 0
        with os.scandir(root_descriptor) as entries:
            names = []
            for entry in entries:
                budget.charge_node(entry.name)
                names.append(entry.name)
        for name in names:
            if _IMPORT_ID_RE.fullmatch(name) is None:
                raise WorkspaceImportIntegrityError(
                    "workspace import store requires reconciliation before ingest"
                )
            stored_ref, archive_descriptor, _directory_identity = (
                self._validate_import_contents(
                    root_descriptor,
                    name,
                    None,
                    budget=budget,
                )
            )
            os.close(archive_descriptor)
            import_count += 1
            archive_bytes += stored_ref.byte_size
            self._require_retained_capacity(import_count, archive_bytes)
        return _RetainedUsage(import_count=import_count, archive_bytes=archive_bytes)

    @staticmethod
    def _verify_directory_binding(
        root_descriptor: int,
        name: str,
        descriptor: int,
        *,
        label: str,
    ) -> None:
        opened = os.fstat(descriptor)
        current = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
        if not _same_inode(opened, current):
            raise WorkspaceImportIntegrityError(f"{label} pathname binding changed")

    @staticmethod
    def _read_bounded_file(descriptor: int, size: int, *, label: str) -> bytes:
        if size < 0 or size > _METADATA_MAX_BYTES:
            raise _DeterministicImportCorruption(f"{label} exceeds its byte limit")
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                raise WorkspaceImportIntegrityError(f"{label} was truncated while reading")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise WorkspaceImportIntegrityError(f"{label} grew while reading")
        return b"".join(chunks)

    def _open_import_directory(self, root_descriptor: int, import_id: str) -> int:
        try:
            descriptor = os.open(import_id, _DIR_OPEN_FLAGS, dir_fd=root_descriptor)
        except FileNotFoundError as exc:
            raise WorkspaceImportNotFoundError("workspace import does not exist") from exc
        try:
            self._require_stored_private_directory(
                os.fstat(descriptor), label="workspace import directory"
            )
            self._verify_directory_binding(
                root_descriptor,
                import_id,
                descriptor,
                label="workspace import directory",
            )
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _load_metadata(
        self,
        directory_descriptor: int,
        *,
        budget: _ScanBudget | None = None,
    ) -> _StoredMetadata:
        try:
            descriptor = os.open(_METADATA_NAME, _FILE_READ_FLAGS, dir_fd=directory_descriptor)
        except OSError as exc:
            raise WorkspaceImportIntegrityError("workspace import metadata is unavailable") from exc
        try:
            before = os.fstat(descriptor)
            self._require_private_file(before, label="workspace import metadata")
            if budget is not None:
                budget.charge_bytes(before.st_size)
            raw = self._read_bounded_file(
                descriptor, before.st_size, label="workspace import metadata"
            )
            after = os.fstat(descriptor)
            current = os.stat(
                _METADATA_NAME,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if _identity(before) != _identity(after) or not _same_inode(after, current):
                raise WorkspaceImportIntegrityError("workspace import metadata changed while reading")
            try:
                payload = json.loads(raw)
                if not isinstance(payload, dict) or set(payload) != {
                    "import_ref",
                    "schema_version",
                    "storage_identity",
                }:
                    raise ValueError("metadata fields are not closed")
                if payload["schema_version"] != "1":
                    raise ValueError("metadata schema version is invalid")
                storage_identity = payload["storage_identity"]
                if not isinstance(storage_identity, dict) or set(storage_identity) != {
                    "archive_device",
                    "archive_inode",
                    "archive_token",
                    "directory_device",
                    "directory_inode",
                }:
                    raise ValueError("storage identity fields are not closed")
                identity_values = tuple(
                    storage_identity[key]
                    for key in (
                        "archive_device",
                        "archive_inode",
                        "directory_device",
                        "directory_inode",
                    )
                )
                if any(
                    isinstance(value, bool) or not isinstance(value, int) or value < 0
                    for value in identity_values
                ):
                    raise ValueError("storage identity values are invalid")
                archive_token = storage_identity["archive_token"]
                if (
                    not isinstance(archive_token, str)
                    or re.fullmatch(r"[0-9a-f]{64}", archive_token) is None
                ):
                    raise ValueError("archive storage token is invalid")
                metadata = _StoredMetadata(
                    import_ref=WorkspaceImportRefV1.model_validate(payload["import_ref"]),
                    directory_device=storage_identity["directory_device"],
                    directory_inode=storage_identity["directory_inode"],
                    archive_device=storage_identity["archive_device"],
                    archive_inode=storage_identity["archive_inode"],
                    archive_token=archive_token,
                )
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise _DeterministicImportCorruption(
                    "workspace import metadata is not closed JSON"
                ) from exc
            if raw != _canonical_metadata(metadata):
                raise _DeterministicImportCorruption(
                    "workspace import metadata is not canonical JSON"
                )
            return metadata
        finally:
            os.close(descriptor)

    def _open_verified_archive(
        self,
        directory_descriptor: int,
        import_ref: WorkspaceImportRefV1,
        *,
        archive_identity: tuple[int, int],
        archive_token: str,
        budget: _ScanBudget | None = None,
    ) -> int:
        try:
            descriptor = os.open(_ARCHIVE_NAME, _FILE_READ_FLAGS, dir_fd=directory_descriptor)
        except OSError as exc:
            raise WorkspaceImportIntegrityError("workspace import archive is unavailable") from exc
        try:
            before = os.fstat(descriptor)
            self._require_private_file(before, label="workspace import archive")
            if (before.st_dev, before.st_ino) != archive_identity:
                raise _DeterministicImportCorruption(
                    "workspace import archive identity changed"
                )
            try:
                observed_token = os.getxattr(descriptor, _ARCHIVE_TOKEN_XATTR)
            except OSError as exc:
                raise WorkspaceImportIntegrityError(
                    "workspace import archive storage token is unavailable"
                ) from exc
            if observed_token != bytes.fromhex(archive_token):
                raise _DeterministicImportCorruption(
                    "workspace import archive storage token changed"
                )
            if before.st_size != import_ref.byte_size:
                raise _DeterministicImportCorruption("workspace import archive size changed")
            digests: list[str] = []
            for _attempt in range(2):
                digest = hashlib.sha256()
                remaining = before.st_size
                while remaining:
                    chunk = os.read(descriptor, min(remaining, _COPY_CHUNK_BYTES))
                    if not chunk:
                        raise WorkspaceImportIntegrityError("workspace import archive was truncated")
                    if budget is not None:
                        budget.charge_bytes(len(chunk))
                    digest.update(chunk)
                    remaining -= len(chunk)
                if os.read(descriptor, 1):
                    raise WorkspaceImportIntegrityError("workspace import archive grew while reading")
                digests.append(digest.hexdigest())
                os.lseek(descriptor, 0, os.SEEK_SET)
            after = os.fstat(descriptor)
            current = os.stat(
                _ARCHIVE_NAME,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if _identity(before) != _identity(after) or not _same_inode(after, current):
                raise WorkspaceImportIntegrityError("workspace import archive changed while hashing")
            if any(digest != import_ref.content_sha256 for digest in digests):
                raise _DeterministicImportCorruption(
                    "workspace import archive digest changed"
                )
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _validate_import_contents(
        self,
        root_descriptor: int,
        import_id: str,
        expected_ref: WorkspaceImportRefV1 | None,
        *,
        budget: _ScanBudget | None = None,
    ) -> tuple[WorkspaceImportRefV1, int, tuple[int, int]]:
        self._require_store_import_id(import_id)
        directory_descriptor = self._open_import_directory(root_descriptor, import_id)
        archive_descriptor: int | None = None
        try:
            names: set[str] = set()
            with os.scandir(directory_descriptor) as entries:
                for entry in entries:
                    if budget is not None:
                        budget.charge_node(entry.name)
                    names.add(entry.name)
            if names != {_ARCHIVE_NAME, _METADATA_NAME}:
                raise _DeterministicImportCorruption(
                    "workspace import directory shape is invalid"
                )
            metadata = self._load_metadata(directory_descriptor, budget=budget)
            stored_ref = metadata.import_ref
            if stored_ref.import_id != import_id:
                raise _DeterministicImportCorruption(
                    "workspace import metadata ID does not match"
                )
            if expected_ref is not None and stored_ref != expected_ref:
                raise WorkspaceImportIntegrityError("workspace import reference does not match storage")
            directory_status = os.fstat(directory_descriptor)
            if (directory_status.st_dev, directory_status.st_ino) != (
                metadata.directory_device,
                metadata.directory_inode,
            ):
                raise _DeterministicImportCorruption(
                    "workspace import directory identity changed"
                )
            archive_descriptor = self._open_verified_archive(
                directory_descriptor,
                stored_ref,
                archive_identity=(metadata.archive_device, metadata.archive_inode),
                archive_token=metadata.archive_token,
                budget=budget,
            )
            self._verify_directory_binding(
                root_descriptor,
                import_id,
                directory_descriptor,
                label="workspace import directory",
            )
            return (
                stored_ref,
                archive_descriptor,
                (directory_status.st_dev, directory_status.st_ino),
            )
        except BaseException:
            if archive_descriptor is not None:
                os.close(archive_descriptor)
            raise
        finally:
            os.close(directory_descriptor)

    @staticmethod
    def _require_store_import_id(import_id: str) -> None:
        if _IMPORT_ID_RE.fullmatch(import_id) is None:
            raise WorkspaceImportIntegrityError(
                "workspace import ID was not issued by this store"
            )

    @classmethod
    def _require_external_import_ref(
        cls,
        import_ref: WorkspaceImportRefV1,
        *,
        operation: str,
    ) -> None:
        if not isinstance(import_ref, WorkspaceImportRefV1):
            raise TypeError(f"{operation} requires WorkspaceImportRefV1")
        cls._require_store_import_id(import_ref.import_id)

    @contextmanager
    def resolve(self, import_ref: WorkspaceImportRefV1) -> Iterator[BinaryIO]:
        """Yield a digest-verified read handle without exposing a host path."""

        self._require_external_import_ref(import_ref, operation="resolve")
        archive_descriptor: int | None = None
        try:
            with self._locked_root() as root_descriptor:
                _stored_ref, archive_descriptor, _directory_identity = (
                    self._validate_import_contents(
                        root_descriptor,
                        import_ref.import_id,
                        import_ref,
                    )
                )
            stream = os.fdopen(archive_descriptor, "rb", closefd=True)
            archive_descriptor = None
        except BaseException:
            if archive_descriptor is not None:
                os.close(archive_descriptor)
            raise
        try:
            yield stream
        finally:
            stream.close()

    def release(self, import_ref: WorkspaceImportRefV1) -> None:
        """Delete the exact verified import referenced by ``import_ref``."""

        self._require_external_import_ref(import_ref, operation="release")
        with self._locked_root() as root_descriptor:
            _stored_ref, archive_descriptor, directory_identity = (
                self._validate_import_contents(
                    root_descriptor,
                    import_ref.import_id,
                    import_ref,
                )
            )
            os.close(archive_descriptor)
            self._discard_flat_directory(
                root_descriptor,
                import_ref.import_id,
                missing_ok=False,
                expected_identity=directory_identity,
            )
            os.fsync(root_descriptor)

    def delete(self, import_ref: WorkspaceImportRefV1) -> None:
        """Alias for explicit lifecycle callers that use delete terminology."""

        self.release(import_ref)

    def _discard_flat_directory(
        self,
        root_descriptor: int,
        name: str,
        *,
        missing_ok: bool,
        expected_identity: tuple[int, int] | None = None,
        budget: _ScanBudget | None = None,
    ) -> None:
        try:
            descriptor = os.open(name, _DIR_OPEN_FLAGS, dir_fd=root_descriptor)
        except FileNotFoundError:
            if missing_ok:
                return
            raise WorkspaceImportNotFoundError("workspace import does not exist") from None
        opened = os.fstat(descriptor)
        try:
            if expected_identity is not None and (opened.st_dev, opened.st_ino) != expected_identity:
                raise WorkspaceImportIntegrityError(
                    "workspace import changed before directory cleanup"
                )
            with os.scandir(descriptor) as entries:
                child_names = []
                for entry in entries:
                    if budget is not None:
                        budget.charge_node(entry.name)
                    elif len(child_names) >= _MAX_FLAT_CLEANUP_NODES:
                        raise WorkspaceImportIntegrityError(
                            "workspace import cleanup node budget exceeded"
                        )
                    child_names.append(entry.name)
            for child_name in child_names:
                child = os.stat(child_name, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISDIR(child.st_mode):
                    raise WorkspaceImportIntegrityError(
                        "workspace import cleanup refuses nested directories"
                    )
                os.unlink(child_name, dir_fd=descriptor)
            os.fsync(descriptor)
            current = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
            if not _same_inode(opened, current):
                raise WorkspaceImportIntegrityError(
                    "workspace import changed before directory cleanup"
                )
        finally:
            os.close(descriptor)
        os.rmdir(name, dir_fd=root_descriptor)

    def _discard_root_entry(
        self,
        root_descriptor: int,
        name: str,
        *,
        expected_identity: tuple[int, int],
        budget: _ScanBudget,
    ) -> None:
        value = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
        if (value.st_dev, value.st_ino) != expected_identity:
            raise WorkspaceImportIntegrityError(
                "workspace import root entry changed during reconciliation"
            )
        if stat.S_ISDIR(value.st_mode):
            self._discard_flat_directory(
                root_descriptor,
                name,
                missing_ok=True,
                expected_identity=expected_identity,
                budget=budget,
            )
        else:
            os.unlink(name, dir_fd=root_descriptor)

    def reconcile(self) -> None:
        """Boundedly remove temp, malformed, tampered, and non-store root entries."""

        budget = _ScanBudget(
            remaining_nodes=self._reconcile_max_nodes,
            remaining_bytes=self._reconcile_max_bytes,
        )
        with self._locked_root() as root_descriptor:
            with os.scandir(root_descriptor) as entries:
                observed_entries = []
                for entry in entries:
                    budget.charge_node(entry.name)
                    value = entry.stat(follow_symlinks=False)
                    observed_entries.append(
                        (entry.name, (value.st_dev, value.st_ino), value.st_mode)
                    )
            for name, observed_identity, observed_mode in observed_entries:
                if (
                    name.startswith(_TEMP_PREFIX)
                    or _IMPORT_ID_RE.fullmatch(name) is None
                    or not stat.S_ISDIR(observed_mode)
                ):
                    self._discard_root_entry(
                        root_descriptor,
                        name,
                        expected_identity=observed_identity,
                        budget=budget,
                    )
                    continue
                try:
                    _stored_ref, archive_descriptor, directory_identity = (
                        self._validate_import_contents(
                            root_descriptor,
                            name,
                            None,
                            budget=budget,
                        )
                    )
                    if directory_identity != observed_identity:
                        os.close(archive_descriptor)
                        raise WorkspaceImportIntegrityError(
                            "workspace import changed during reconciliation"
                        )
                except _ReconcileBudgetExceeded:
                    raise
                except _DeterministicImportCorruption:
                    self._discard_root_entry(
                        root_descriptor,
                        name,
                        expected_identity=observed_identity,
                        budget=budget,
                    )
                else:
                    os.close(archive_descriptor)
            os.fsync(root_descriptor)


__all__ = [
    "WorkspaceArchiveValidationError",
    "WorkspaceImportError",
    "WorkspaceImportIntegrityError",
    "WorkspaceImportNotFoundError",
    "WorkspaceImportStore",
    "WorkspaceImportStoreConfigurationError",
]
