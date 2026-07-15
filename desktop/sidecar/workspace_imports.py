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
import hmac
import json
import os
import re
import secrets
import stat
import sys
import threading
import time
from typing import BinaryIO, Callable, Iterator, Mapping
import unicodedata

from desktop.sidecar import fd_xattrs as _xattrs
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
_PENDING_LEASE_XATTR = "user.openevo.workspace-import-pending-lease"
_ROOT_TOKEN_XATTR = "user.openevo.workspace-import-root-token"
_IMPORT_ID_PREFIX = "workspace-import-"
_TEMP_PREFIX = ".workspace-import-tmp-"
_SNAPSHOT_PREFIX = ".workspace-import-snapshot-"
_QUARANTINE_PREFIX = ".workspace-import-quarantine-"
_ROOT_MARKER_PREFIX = ".openevo-workspace-import-root-"
_ROOT_MARKER_MAX_BYTES = 2048
_AUTH_KEY_PREFIX = ".openevo-workspace-import-auth-"
_AUTH_KEY_BYTES = 32
_BOOTSTRAP_PREFIX = ".openevo-workspace-import-bootstrap-"
_CREATION_LOCK_PREFIX = ".openevo-workspace-import-creation-lock-"
_INITIALIZATION_TEMP_PREFIX = ".openevo-workspace-import-init-tmp-"
_ROOT_AUTH_DOMAIN = b"openevo.workspace-import.root.v2\0"
_METADATA_AUTH_DOMAIN = b"openevo.workspace-import.metadata.v2\0"
_BOOTSTRAP_AUTH_DOMAIN = b"openevo.workspace-import.bootstrap.v1\0"
_PENDING_LEASE_DOMAIN = b"openevo.workspace-import.pending-lease.v1\0"
_CREATION_LOCK_CONTENT = b"openevo.workspace-import.creation-lock.v1\n"
_IMPORT_ID_RE = re.compile(r"^workspace-import-[0-9a-f]{48}$")
_DEFAULT_RECONCILE_MAX_NODES = 300_000
_DEFAULT_RECONCILE_MAX_BYTES = 64 * 1024 * 1024 * 1024
_DEFAULT_MAX_RETAINED_IMPORTS = 10_000
_DEFAULT_MAX_RETAINED_ARCHIVE_BYTES = 24 * 1024 * 1024 * 1024
_DEFAULT_MAX_PENDING_IMPORTS = 64
_DEFAULT_MAX_PENDING_ARCHIVE_BYTES = MAX_WORKSPACE_UPLOAD_BYTES
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
_LOCK_CANCEL_POLL_SECONDS = 0.01


class WorkspaceImportError(RuntimeError):
    """Base error for the private workspace import store."""


class WorkspaceImportCancelled(WorkspaceImportError):
    """An ingest stopped cooperatively before or just after publication."""


CancellationCheck = Callable[[], bool]


def _check_cancel(cancel_check: CancellationCheck | None) -> None:
    if cancel_check is not None and cancel_check():
        raise WorkspaceImportCancelled("workspace import was cancelled")


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
class WorkspaceImportOwnership:
    """Store-internal principal and idempotency binding for one workspace sync."""

    project_id: str
    operation_id: str
    idempotency_key: str

    def __post_init__(self) -> None:
        for label, value, minimum in (
            ("project_id", self.project_id, 1),
            ("operation_id", self.operation_id, 1),
            ("idempotency_key", self.idempotency_key, 16),
        ):
            if (
                not isinstance(value, str)
                or not minimum <= len(value) <= 256
                or value != value.strip()
                or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
            ):
                raise ValueError(f"workspace import {label} is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class PendingWorkspaceImport:
    """Private native-host lease paired with the public opaque import reference."""

    import_ref: WorkspaceImportRefV1
    lease_token: str


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
    pending_count: int
    pending_archive_bytes: int


def _required_reconcile_budget(import_count: int, archive_bytes: int) -> tuple[int, int]:
    """Return the worst reconciliation cost for retained imports plus one crash temp."""

    return (
        _RECONCILE_CRASH_TEMP_NODES + (import_count * _RECONCILE_NODES_PER_RETAINED_IMPORT),
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
    ownership: WorkspaceImportOwnership
    directory_device: int
    directory_inode: int
    archive_device: int
    archive_inode: int
    metadata_device: int
    metadata_inode: int
    archive_token: str
    authentication: str


@dataclass(frozen=True, slots=True)
class _RootMarker:
    store_token: str
    root_name: str
    parent_device: int
    parent_inode: int
    root_device: int | None = None
    root_inode: int | None = None
    authentication: str = ""


@dataclass(frozen=True, slots=True)
class _InitializationBootstrap:
    store_token: str
    authentication_key: bytes
    root_name: str
    parent_device: int
    parent_inode: int
    authentication: str = ""


def _root_marker_payload(marker: _RootMarker) -> dict[str, object]:
    payload: dict[str, object] = {
        "parent_identity": {
            "device": marker.parent_device,
            "inode": marker.parent_inode,
        },
        "root_name": marker.root_name,
        "schema_version": "2",
        "store_token": marker.store_token,
    }
    if marker.root_device is not None and marker.root_inode is not None:
        payload["root_identity"] = {
            "device": marker.root_device,
            "inode": marker.root_inode,
        }
    return payload


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _authentication(key: bytes, domain: bytes, payload: bytes) -> str:
    return hmac.new(key, domain + payload, hashlib.sha256).hexdigest()


def _authenticated_root_marker(marker: _RootMarker, key: bytes) -> _RootMarker:
    authentication = _authentication(
        key,
        _ROOT_AUTH_DOMAIN,
        _canonical_json(_root_marker_payload(marker)),
    )
    return _RootMarker(
        store_token=marker.store_token,
        root_name=marker.root_name,
        parent_device=marker.parent_device,
        parent_inode=marker.parent_inode,
        root_device=marker.root_device,
        root_inode=marker.root_inode,
        authentication=authentication,
    )


def _canonical_root_marker(marker: _RootMarker) -> bytes:
    payload = _root_marker_payload(marker)
    payload["authentication"] = marker.authentication
    return _canonical_json(payload)


def _bootstrap_payload(bootstrap: _InitializationBootstrap) -> dict[str, object]:
    return {
        "authentication_key": bootstrap.authentication_key.hex(),
        "parent_identity": {
            "device": bootstrap.parent_device,
            "inode": bootstrap.parent_inode,
        },
        "root_name": bootstrap.root_name,
        "schema_version": "1",
        "store_token": bootstrap.store_token,
    }


def _authenticated_bootstrap(
    bootstrap: _InitializationBootstrap,
) -> _InitializationBootstrap:
    authentication = _authentication(
        bootstrap.authentication_key,
        _BOOTSTRAP_AUTH_DOMAIN,
        _canonical_json(_bootstrap_payload(bootstrap)),
    )
    return _InitializationBootstrap(
        store_token=bootstrap.store_token,
        authentication_key=bootstrap.authentication_key,
        root_name=bootstrap.root_name,
        parent_device=bootstrap.parent_device,
        parent_inode=bootstrap.parent_inode,
        authentication=authentication,
    )


def _canonical_bootstrap(bootstrap: _InitializationBootstrap) -> bytes:
    payload = _bootstrap_payload(bootstrap)
    payload["authentication"] = bootstrap.authentication
    return _canonical_json(payload)


def _metadata_payload(metadata: _StoredMetadata) -> dict[str, object]:
    return {
        "import_ref": metadata.import_ref.model_dump(mode="json"),
        "ownership": {
            "idempotency_key": metadata.ownership.idempotency_key,
            "operation_id": metadata.ownership.operation_id,
            "project_id": metadata.ownership.project_id,
        },
        "schema_version": "2",
        "storage_identity": {
            "archive_device": metadata.archive_device,
            "archive_inode": metadata.archive_inode,
            "archive_token": metadata.archive_token,
            "directory_device": metadata.directory_device,
            "directory_inode": metadata.directory_inode,
            "metadata_device": metadata.metadata_device,
            "metadata_inode": metadata.metadata_inode,
        },
    }


def _authenticated_metadata(metadata: _StoredMetadata, key: bytes) -> _StoredMetadata:
    authentication = _authentication(
        key,
        _METADATA_AUTH_DOMAIN,
        _canonical_json(_metadata_payload(metadata)),
    )
    return _StoredMetadata(
        import_ref=metadata.import_ref,
        ownership=metadata.ownership,
        directory_device=metadata.directory_device,
        directory_inode=metadata.directory_inode,
        archive_device=metadata.archive_device,
        archive_inode=metadata.archive_inode,
        metadata_device=metadata.metadata_device,
        metadata_inode=metadata.metadata_inode,
        archive_token=metadata.archive_token,
        authentication=authentication,
    )


def _canonical_metadata(metadata: _StoredMetadata) -> bytes:
    payload = _metadata_payload(metadata)
    payload["authentication"] = metadata.authentication
    return _canonical_json(payload)


def _pending_lease_payload(
    import_ref: WorkspaceImportRefV1,
    ownership: WorkspaceImportOwnership,
) -> bytes:
    return _canonical_json(
        {
            "import_ref": import_ref.model_dump(mode="json"),
            "ownership": {
                "idempotency_key": ownership.idempotency_key,
                "operation_id": ownership.operation_id,
                "project_id": ownership.project_id,
            },
            "schema_version": "1",
        }
    )


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


_quarantine_noreplace = _rename_noreplace


def _after_archive_fsync(_temporary_descriptor: int) -> None:
    """Private fault-injection hook."""


def _after_metadata_fsync(_temporary_descriptor: int) -> None:
    """Private fault-injection hook."""


def _before_import_publish(_root_descriptor: int, _import_id: str) -> None:
    """Private fault-injection hook."""


def _after_import_publish(_root_descriptor: int, _import_id: str) -> None:
    """Private fault-injection hook."""


def _after_fresh_root_parent_fsync(_parent_descriptor: int, _root_name: str) -> None:
    """Private fault-injection hook."""


def _initialization_file_fault_point(
    _kind: str,
    _stage: str,
    _descriptor: int,
) -> None:
    """Private fault-injection hook for initialization file publication."""


def _before_snapshot_commit(_root_descriptor: int, _archive_descriptor: int) -> None:
    """Private fault-injection hook."""


class _ArchiveReader:
    def __init__(
        self,
        source: int,
        destination: int,
        source_size: int,
        cancel_check: CancellationCheck | None,
    ) -> None:
        self.source = source
        self.destination = destination
        self.source_size = source_size
        self.offset = 0
        self.digest = hashlib.sha256()
        self.cancel_check = cancel_check

    def read_exact(self, count: int, *, label: str) -> bytes:
        if count < 0 or self.offset + count > self.source_size:
            raise WorkspaceArchiveValidationError(f"truncated workspace archive {label}")
        chunks: list[bytes] = []
        remaining = count
        while remaining:
            _check_cancel(self.cancel_check)
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


def _source_sha256(
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
            chunk = os.pread(descriptor, min(size - offset, _COPY_CHUNK_BYTES), offset)
        except InterruptedError:
            continue
        if not chunk:
            raise WorkspaceArchiveValidationError(
                "workspace import source changed while rehashing"
            )
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


def _validate_logical_path(
    header_path: bytes, *, is_directory: bool
) -> tuple[str, tuple[str, ...]]:
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
        raise WorkspaceArchiveValidationError(
            "workspace archive unused header fields must be zero"
        )
    header_path = prefix + (b"/" if prefix else b"") + name
    expected_name, expected_prefix = _canonical_path_fields(header_path)
    if name != expected_name or prefix != expected_prefix:
        raise WorkspaceArchiveValidationError(
            "workspace path does not use the canonical ustar split"
        )
    logical_path, parts = _validate_logical_path(header_path, is_directory=is_directory)
    if prior_header_path is not None and header_path <= prior_header_path:
        raise WorkspaceArchiveValidationError(
            "workspace entries are not in header-path byte order"
        )
    if logical_path in entries:
        raise WorkspaceArchiveValidationError(
            "workspace archive contains a duplicate logical path"
        )
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
    cancel_check: CancellationCheck | None = None,
) -> _ArchiveResult:
    reader = _ArchiveReader(source, destination, source_size, cancel_check)
    entries: dict[str, bool] = {}
    prior_header_path: bytes | None = None
    extracted_bytes = 0
    while True:
        _check_cancel(cancel_check)
        block = reader.read_exact(_BLOCK_SIZE, label="header")
        if not any(block):
            second = reader.read_exact(_BLOCK_SIZE, label="end marker")
            if any(second):
                raise WorkspaceArchiveValidationError(
                    "workspace archive needs two zero end blocks"
                )
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
        max_pending_imports: int | None = None,
        max_pending_archive_bytes: int | None = None,
        reconcile_max_nodes: int = _DEFAULT_RECONCILE_MAX_NODES,
        reconcile_max_bytes: int = _DEFAULT_RECONCILE_MAX_BYTES,
        reconcile_on_open: bool = True,
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
        if max_pending_imports is None:
            max_pending_imports = min(_DEFAULT_MAX_PENDING_IMPORTS, max_retained_imports)
        if max_pending_archive_bytes is None:
            max_pending_archive_bytes = min(
                _DEFAULT_MAX_PENDING_ARCHIVE_BYTES,
                max_retained_archive_bytes,
            )
        if (
            max_pending_imports < 0
            or max_pending_imports > max_retained_imports
            or max_pending_archive_bytes < 0
            or max_pending_archive_bytes > max_retained_archive_bytes
        ):
            raise ValueError("pending workspace import limits exceed retained limits")
        if type(reconcile_on_open) is not bool:
            raise TypeError("reconcile_on_open must be a boolean")
        required_nodes, required_bytes = _required_reconcile_budget(
            max_retained_imports,
            max_retained_archive_bytes,
        )
        if required_nodes > reconcile_max_nodes or required_bytes > reconcile_max_bytes:
            raise ValueError("retained workspace import limits exceed reconciliation budgets")
        self._root = os.path.abspath(os.fspath(root))
        self._max_archive_bytes = max_archive_bytes
        self._max_entries = max_entries
        self._max_extracted_bytes = max_extracted_bytes
        self._max_retained_imports = max_retained_imports
        self._max_retained_archive_bytes = max_retained_archive_bytes
        self._max_pending_imports = max_pending_imports
        self._max_pending_archive_bytes = max_pending_archive_bytes
        self._reconcile_max_nodes = reconcile_max_nodes
        self._reconcile_max_bytes = reconcile_max_bytes
        self._parent_descriptor = -1
        self._root_descriptor = -1
        self._parent_components = tuple(
            component for component in os.path.dirname(self._root).split(os.sep) if component
        )
        self._root_name = os.path.basename(self._root)
        if not self._root_name or self._root == os.path.abspath(os.sep):
            raise WorkspaceImportStoreConfigurationError(
                "workspace import root must have a dedicated parent directory"
            )
        marker_suffix = hashlib.sha256(os.fsencode(self._root_name)).hexdigest()[:32]
        self._marker_name = f"{_ROOT_MARKER_PREFIX}{marker_suffix}.json"
        self._pending_marker_name = f"{self._marker_name}.pending"
        self._auth_key_name = f"{_AUTH_KEY_PREFIX}{marker_suffix}.key"
        self._bootstrap_name = f"{_BOOTSTRAP_PREFIX}{marker_suffix}.json"
        self._creation_lock_name = f"{_CREATION_LOCK_PREFIX}{marker_suffix}.lock"
        self._auth_key_descriptor = -1
        self._auth_key_identity = (0, 0)
        self._auth_key = b""
        self._ancestor_identities: tuple[tuple[int, int], ...] = ()
        self._root_identity = (0, 0)
        self._root_marker: _RootMarker | None = None
        try:
            self._prepare_root()
            if reconcile_on_open:
                self.reconcile()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Close stable root anchors held by this store instance."""

        for attribute in (
            "_root_descriptor",
            "_auth_key_descriptor",
            "_parent_descriptor",
        ):
            descriptor = getattr(self, attribute, -1)
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                finally:
                    setattr(self, attribute, -1)
        self._auth_key = b""

    def __del__(self) -> None:
        try:
            self.close()
        except OSError:
            pass

    def _open_parent_chain(self) -> tuple[int, tuple[tuple[int, int], ...]]:
        try:
            descriptor = os.open(os.path.abspath(os.sep), _ROOT_OPEN_FLAGS)
        except OSError as exc:  # pragma: no cover - a usable host always has an openable root
            raise WorkspaceImportStoreConfigurationError(
                "workspace import no-follow ancestor root is unavailable"
            ) from exc
        identities = []
        try:
            value = os.fstat(descriptor)
            identities.append((value.st_dev, value.st_ino))
            for component in self._parent_components:
                try:
                    child = os.open(component, _DIR_OPEN_FLAGS, dir_fd=descriptor)
                except OSError as exc:
                    raise WorkspaceImportStoreConfigurationError(
                        "workspace import path has an unavailable no-follow ancestor"
                    ) from exc
                previous = descriptor
                descriptor = child
                os.close(previous)
                value = os.fstat(descriptor)
                if not stat.S_ISDIR(value.st_mode):
                    raise WorkspaceImportStoreConfigurationError(
                        "workspace import no-follow ancestor is not a directory"
                    )
                identities.append((value.st_dev, value.st_ino))
            return descriptor, tuple(identities)
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _require_private_marker(value: os.stat_result, *, label: str) -> None:
        if (
            not stat.S_ISREG(value.st_mode)
            or value.st_uid != os.geteuid()
            or stat.S_IMODE(value.st_mode) != 0o600
            or value.st_nlink != 1
        ):
            raise WorkspaceImportStoreConfigurationError(
                f"{label} must be a private regular file mode 0600 link-count one"
            )

    def _open_authentication_key(self, parent_descriptor: int) -> None:
        try:
            descriptor = os.open(
                self._auth_key_name,
                _FILE_READ_FLAGS,
                dir_fd=parent_descriptor,
            )
        except OSError as exc:
            raise WorkspaceImportStoreConfigurationError(
                "workspace import authentication key is unavailable"
            ) from exc
        try:
            before = os.fstat(descriptor)
            self._require_private_marker(
                before,
                label="workspace import authentication key",
            )
            if before.st_size != _AUTH_KEY_BYTES:
                raise WorkspaceImportStoreConfigurationError(
                    "workspace import authentication key has an invalid size"
                )
            key = os.pread(descriptor, _AUTH_KEY_BYTES, 0)
            after = os.fstat(descriptor)
            current = os.stat(
                self._auth_key_name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (
                len(key) != _AUTH_KEY_BYTES
                or _identity(before) != _identity(after)
                or not _same_inode(after, current)
            ):
                raise WorkspaceImportStoreConfigurationError(
                    "workspace import authentication key changed while reading"
                )
            self._auth_key_descriptor = descriptor
            self._auth_key_identity = (after.st_dev, after.st_ino)
            self._auth_key = key
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _parent_entry_exists(parent_descriptor: int, name: str) -> bool:
        try:
            os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return False
        return True

    @staticmethod
    def _discard_created_parent_file(
        parent_descriptor: int,
        name: str,
        expected_identity: tuple[int, int],
    ) -> None:
        try:
            observed = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return
        if (observed.st_dev, observed.st_ino) != expected_identity:
            raise WorkspaceImportStoreConfigurationError(
                "workspace import initialization temporary changed during cleanup"
            )
        quarantine = f"{_QUARANTINE_PREFIX}{secrets.token_hex(24)}"
        _quarantine_noreplace(name, quarantine, directory_fd=parent_descriptor)
        moved = os.stat(quarantine, dir_fd=parent_descriptor, follow_symlinks=False)
        if (moved.st_dev, moved.st_ino) != expected_identity:
            raise WorkspaceImportStoreConfigurationError(
                "workspace import initialization temporary changed during cleanup"
            )
        os.unlink(quarantine, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)

    def _publish_initialization_file(
        self,
        parent_descriptor: int,
        target_name: str,
        content: bytes,
        *,
        kind: str,
        allow_existing: bool = False,
        inject_faults: bool = True,
    ) -> bool:
        if len(content) > _ROOT_MARKER_MAX_BYTES:
            raise WorkspaceImportStoreConfigurationError(
                "workspace import initialization file exceeds its byte limit"
            )
        temporary_name = f"{_INITIALIZATION_TEMP_PREFIX}{secrets.token_hex(24)}"
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_descriptor,
        )
        created = os.fstat(descriptor)
        created_identity = (created.st_dev, created.st_ino)
        published = False
        try:
            os.fchmod(descriptor, 0o600)
            if inject_faults:
                _initialization_file_fault_point(kind, "before_write", descriptor)
            _write_all(descriptor, content)
            if inject_faults:
                _initialization_file_fault_point(kind, "before_file_fsync", descriptor)
            os.fsync(descriptor)
            written = os.fstat(descriptor)
            self._require_private_marker(
                written,
                label="workspace import initialization temporary",
            )
            current = os.stat(
                temporary_name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (
                (written.st_dev, written.st_ino) != created_identity
                or not _same_inode(written, current)
                or written.st_size != len(content)
            ):
                raise WorkspaceImportStoreConfigurationError(
                    "workspace import initialization temporary changed before publication"
                )
            try:
                _rename_noreplace(
                    temporary_name,
                    target_name,
                    directory_fd=parent_descriptor,
                )
            except FileExistsError:
                if not allow_existing:
                    raise
                return False
            published = True
            target = os.stat(
                target_name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if not _same_inode(written, target):
                raise WorkspaceImportStoreConfigurationError(
                    "workspace import initialization file changed during publication"
                )
            if inject_faults:
                _initialization_file_fault_point(kind, "before_parent_fsync", parent_descriptor)
            os.fsync(parent_descriptor)
            return True
        finally:
            try:
                os.close(descriptor)
            finally:
                if not published:
                    self._discard_created_parent_file(
                        parent_descriptor,
                        temporary_name,
                        created_identity,
                    )

    @contextmanager
    def _locked_parent_creation(self, parent_descriptor: int) -> Iterator[None]:
        with _thread_lock_for(self._root):
            self._publish_initialization_file(
                parent_descriptor,
                self._creation_lock_name,
                _CREATION_LOCK_CONTENT,
                kind="creation_lock",
                allow_existing=True,
                inject_faults=False,
            )
            try:
                descriptor = os.open(
                    self._creation_lock_name,
                    _FILE_READ_FLAGS,
                    dir_fd=parent_descriptor,
                )
            except OSError as exc:
                raise WorkspaceImportStoreConfigurationError(
                    "workspace import creation lock is unavailable"
                ) from exc
            locked = False
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                locked = True
                before = os.fstat(descriptor)
                self._require_private_marker(before, label="workspace import creation lock")
                content = os.pread(descriptor, len(_CREATION_LOCK_CONTENT) + 1, 0)
                after = os.fstat(descriptor)
                current = os.stat(
                    self._creation_lock_name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if (
                    content != _CREATION_LOCK_CONTENT
                    or _identity(before) != _identity(after)
                    or not _same_inode(after, current)
                ):
                    raise WorkspaceImportStoreConfigurationError(
                        "workspace import creation lock binding is invalid"
                    )
                yield
                held = os.fstat(descriptor)
                self._require_private_marker(held, label="workspace import creation lock")
                final = os.stat(
                    self._creation_lock_name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if _identity(after) != _identity(held) or not _same_inode(held, final):
                    raise WorkspaceImportStoreConfigurationError(
                        "workspace import creation lock binding changed"
                    )
            finally:
                try:
                    if locked:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

    def _prepare_authentication_key(
        self,
        parent_descriptor: int,
        expected_key: bytes,
    ) -> None:
        if not self._parent_entry_exists(parent_descriptor, self._auth_key_name):
            self._publish_initialization_file(
                parent_descriptor,
                self._auth_key_name,
                expected_key,
                kind="auth_key",
            )
        self._open_authentication_key(parent_descriptor)
        if not hmac.compare_digest(self._auth_key, expected_key):
            raise WorkspaceImportStoreConfigurationError(
                "workspace import authentication key conflicts with initialization record"
            )

    def _read_root_marker(self, parent_descriptor: int, name: str) -> _RootMarker | None:
        try:
            descriptor = os.open(name, _FILE_READ_FLAGS, dir_fd=parent_descriptor)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise WorkspaceImportStoreConfigurationError(
                "workspace import root marker is unavailable"
            ) from exc
        try:
            before = os.fstat(descriptor)
            self._require_private_marker(before, label="workspace import root marker")
            if before.st_size > _ROOT_MARKER_MAX_BYTES:
                raise WorkspaceImportStoreConfigurationError(
                    "workspace import root marker exceeds its byte limit"
                )
            raw = self._read_bounded_file(
                descriptor,
                before.st_size,
                label="workspace import root marker",
            )
            after = os.fstat(descriptor)
            current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
            if _identity(before) != _identity(after) or not _same_inode(after, current):
                raise WorkspaceImportStoreConfigurationError(
                    "workspace import root marker changed while reading"
                )
            try:
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError("marker is not an object")
                expected = {
                    "authentication",
                    "parent_identity",
                    "root_name",
                    "schema_version",
                    "store_token",
                }
                if "root_identity" in payload:
                    expected.add("root_identity")
                if set(payload) != expected or payload["schema_version"] != "2":
                    raise ValueError("marker fields are not closed")
                parent = payload["parent_identity"]
                root = payload.get("root_identity")
                if not isinstance(parent, dict) or set(parent) != {"device", "inode"}:
                    raise ValueError("parent identity is invalid")
                if root is not None and (
                    not isinstance(root, dict) or set(root) != {"device", "inode"}
                ):
                    raise ValueError("root identity is invalid")
                values = [parent["device"], parent["inode"]]
                if root is not None:
                    values.extend((root["device"], root["inode"]))
                if any(
                    isinstance(value, bool) or not isinstance(value, int) or value < 0
                    for value in values
                ):
                    raise ValueError("marker identity values are invalid")
                token = payload["store_token"]
                if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{64}", token) is None:
                    raise ValueError("marker token is invalid")
                if payload["root_name"] != self._root_name:
                    raise ValueError("marker root name is invalid")
                authentication = payload["authentication"]
                if (
                    not isinstance(authentication, str)
                    or re.fullmatch(r"[0-9a-f]{64}", authentication) is None
                ):
                    raise ValueError("marker authentication is invalid")
                marker = _RootMarker(
                    store_token=token,
                    root_name=payload["root_name"],
                    parent_device=parent["device"],
                    parent_inode=parent["inode"],
                    root_device=None if root is None else root["device"],
                    root_inode=None if root is None else root["inode"],
                    authentication=authentication,
                )
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise WorkspaceImportStoreConfigurationError(
                    "workspace import root marker is invalid"
                ) from exc
            if raw != _canonical_root_marker(marker):
                raise WorkspaceImportStoreConfigurationError(
                    "workspace import root marker is not canonical"
                )
            authenticated = _authenticated_root_marker(marker, self._auth_key)
            if not hmac.compare_digest(
                marker.authentication,
                authenticated.authentication,
            ):
                raise WorkspaceImportStoreConfigurationError(
                    "workspace import root marker authentication failed"
                )
            return marker
        finally:
            os.close(descriptor)

    def _read_bootstrap(
        self,
        parent_descriptor: int,
    ) -> _InitializationBootstrap | None:
        try:
            descriptor = os.open(
                self._bootstrap_name,
                _FILE_READ_FLAGS,
                dir_fd=parent_descriptor,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise WorkspaceImportStoreConfigurationError(
                "workspace import bootstrap record is unavailable"
            ) from exc
        try:
            before = os.fstat(descriptor)
            self._require_private_marker(before, label="workspace import bootstrap record")
            if before.st_size > _ROOT_MARKER_MAX_BYTES:
                raise WorkspaceImportStoreConfigurationError(
                    "workspace import bootstrap record exceeds its byte limit"
                )
            raw = self._read_bounded_file(
                descriptor,
                before.st_size,
                label="workspace import bootstrap record",
            )
            after = os.fstat(descriptor)
            current = os.stat(
                self._bootstrap_name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if _identity(before) != _identity(after) or not _same_inode(after, current):
                raise WorkspaceImportStoreConfigurationError(
                    "workspace import bootstrap record changed while reading"
                )
            try:
                payload = json.loads(raw)
                expected = {
                    "authentication",
                    "authentication_key",
                    "parent_identity",
                    "root_name",
                    "schema_version",
                    "store_token",
                }
                if (
                    not isinstance(payload, dict)
                    or set(payload) != expected
                    or payload["schema_version"] != "1"
                ):
                    raise ValueError("bootstrap fields are not closed")
                parent = payload["parent_identity"]
                if not isinstance(parent, dict) or set(parent) != {"device", "inode"}:
                    raise ValueError("bootstrap parent identity is invalid")
                if any(
                    isinstance(value, bool) or not isinstance(value, int) or value < 0
                    for value in parent.values()
                ):
                    raise ValueError("bootstrap parent identity is invalid")
                store_token = payload["store_token"]
                key_hex = payload["authentication_key"]
                authentication = payload["authentication"]
                if (
                    not isinstance(store_token, str)
                    or re.fullmatch(r"[0-9a-f]{64}", store_token) is None
                    or not isinstance(key_hex, str)
                    or re.fullmatch(r"[0-9a-f]{64}", key_hex) is None
                    or not isinstance(authentication, str)
                    or re.fullmatch(r"[0-9a-f]{64}", authentication) is None
                    or payload["root_name"] != self._root_name
                ):
                    raise ValueError("bootstrap values are invalid")
                bootstrap = _InitializationBootstrap(
                    store_token=store_token,
                    authentication_key=bytes.fromhex(key_hex),
                    root_name=payload["root_name"],
                    parent_device=parent["device"],
                    parent_inode=parent["inode"],
                    authentication=authentication,
                )
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise WorkspaceImportStoreConfigurationError(
                    "workspace import bootstrap record is invalid"
                ) from exc
            if raw != _canonical_bootstrap(bootstrap):
                raise WorkspaceImportStoreConfigurationError(
                    "workspace import bootstrap record is not canonical"
                )
            authenticated = _authenticated_bootstrap(bootstrap)
            if not hmac.compare_digest(
                bootstrap.authentication,
                authenticated.authentication,
            ):
                raise WorkspaceImportStoreConfigurationError(
                    "workspace import bootstrap authentication failed"
                )
            return bootstrap
        finally:
            os.close(descriptor)

    def _remove_parent_marker(self, name: str) -> None:
        try:
            value = os.stat(name, dir_fd=self._parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return
        quarantine = f"{_QUARANTINE_PREFIX}{secrets.token_hex(24)}"
        _quarantine_noreplace(name, quarantine, directory_fd=self._parent_descriptor)
        moved = os.stat(quarantine, dir_fd=self._parent_descriptor, follow_symlinks=False)
        if not _same_inode(value, moved):
            raise WorkspaceImportStoreConfigurationError(
                "workspace import parent marker changed during cleanup"
            )
        os.unlink(quarantine, dir_fd=self._parent_descriptor)
        os.fsync(self._parent_descriptor)

    def _finish_root_binding(self, pending: _RootMarker, root_descriptor: int) -> _RootMarker:
        root_status = os.fstat(root_descriptor)
        self._require_private_directory(root_status, label="workspace import root")
        try:
            observed_token = _xattrs.getxattr(root_descriptor, _ROOT_TOKEN_XATTR)
        except OSError as exc:
            if exc.errno not in {errno.ENODATA, getattr(errno, "ENOATTR", errno.ENODATA)}:
                raise WorkspaceImportStoreConfigurationError(
                    "workspace import root identity token is unavailable"
                ) from exc
            _xattrs.setxattr(
                root_descriptor,
                _ROOT_TOKEN_XATTR,
                bytes.fromhex(pending.store_token),
            )
        else:
            if observed_token != bytes.fromhex(pending.store_token):
                raise WorkspaceImportStoreConfigurationError(
                    "workspace import root identity token changed"
                )
        os.fsync(root_descriptor)
        final = _authenticated_root_marker(
            _RootMarker(
                store_token=pending.store_token,
                root_name=self._root_name,
                parent_device=pending.parent_device,
                parent_inode=pending.parent_inode,
                root_device=root_status.st_dev,
                root_inode=root_status.st_ino,
            ),
            self._auth_key,
        )
        existing = self._read_root_marker(self._parent_descriptor, self._marker_name)
        if existing is None:
            self._publish_initialization_file(
                self._parent_descriptor,
                self._marker_name,
                _canonical_root_marker(final),
                kind="final_marker",
            )
        elif existing != final:
            raise WorkspaceImportStoreConfigurationError(
                "workspace import root binding marker conflicts with recovery"
            )
        self._remove_parent_marker(self._pending_marker_name)
        return final

    def _prepare_root(self) -> None:
        parent_descriptor, ancestor_identities = self._open_parent_chain()
        self._parent_descriptor = parent_descriptor
        self._ancestor_identities = ancestor_identities
        parent_status = os.fstat(parent_descriptor)
        parent_identity = (parent_status.st_dev, parent_status.st_ino)
        with self._locked_parent_creation(parent_descriptor):
            self._prepare_root_under_creation_lock(parent_identity)

    def _prepare_root_under_creation_lock(
        self,
        parent_identity: tuple[int, int],
    ) -> None:
        parent_descriptor = self._parent_descriptor
        bootstrap = self._read_bootstrap(parent_descriptor)
        key_exists = self._parent_entry_exists(parent_descriptor, self._auth_key_name)
        final_exists = self._parent_entry_exists(parent_descriptor, self._marker_name)
        pending_exists = self._parent_entry_exists(
            parent_descriptor,
            self._pending_marker_name,
        )
        root_exists = self._parent_entry_exists(parent_descriptor, self._root_name)

        if bootstrap is not None:
            if (bootstrap.parent_device, bootstrap.parent_inode) != parent_identity:
                raise WorkspaceImportStoreConfigurationError(
                    "workspace import bootstrap parent binding changed"
                )
            if not key_exists and (final_exists or pending_exists or root_exists):
                raise WorkspaceImportStoreConfigurationError(
                    "workspace import bootstrap state is out of order"
                )
        elif not key_exists:
            if root_exists:
                existing_root = os.stat(
                    self._root_name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                self._require_private_directory(
                    existing_root,
                    label="workspace import root",
                )
                raise WorkspaceImportStoreConfigurationError(
                    "workspace import existing root has no authentication key"
                )
            if final_exists or pending_exists:
                raise WorkspaceImportStoreConfigurationError(
                    "workspace import marker has no authentication key"
                )
            bootstrap = _authenticated_bootstrap(
                _InitializationBootstrap(
                    store_token=secrets.token_hex(32),
                    authentication_key=secrets.token_bytes(_AUTH_KEY_BYTES),
                    root_name=self._root_name,
                    parent_device=parent_identity[0],
                    parent_inode=parent_identity[1],
                )
            )
            self._publish_initialization_file(
                parent_descriptor,
                self._bootstrap_name,
                _canonical_bootstrap(bootstrap),
                kind="bootstrap",
            )

        if bootstrap is not None:
            expected_key = bootstrap.authentication_key
        else:
            self._open_authentication_key(parent_descriptor)
            expected_key = self._auth_key
        if self._auth_key_descriptor < 0:
            self._prepare_authentication_key(parent_descriptor, expected_key)

        final = self._read_root_marker(parent_descriptor, self._marker_name)
        pending = self._read_root_marker(parent_descriptor, self._pending_marker_name)
        if final is not None and (final.root_device is None or final.root_inode is None):
            raise WorkspaceImportStoreConfigurationError(
                "workspace import final root marker has no root identity"
            )
        if pending is not None and (
            pending.root_device is not None or pending.root_inode is not None
        ):
            raise WorkspaceImportStoreConfigurationError(
                "workspace import pending root marker already has a root identity"
            )
        if bootstrap is not None:
            for marker in (final, pending):
                if marker is not None and marker.store_token != bootstrap.store_token:
                    raise WorkspaceImportStoreConfigurationError(
                        "workspace import bootstrap conflicts with root marker"
                    )
        if final is None and pending is None:
            if root_exists:
                existing = os.stat(
                    self._root_name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                self._require_private_directory(existing, label="workspace import root")
                raise WorkspaceImportStoreConfigurationError(
                    "workspace import existing root has no durable binding marker"
                )
            if bootstrap is None:
                raise WorkspaceImportStoreConfigurationError(
                    "workspace import authentication key has no initialization record"
                )
            pending = _authenticated_root_marker(
                _RootMarker(
                    store_token=bootstrap.store_token,
                    root_name=self._root_name,
                    parent_device=parent_identity[0],
                    parent_inode=parent_identity[1],
                ),
                self._auth_key,
            )
            self._publish_initialization_file(
                parent_descriptor,
                self._pending_marker_name,
                _canonical_root_marker(pending),
                kind="pending_marker",
            )
        selected = final or pending
        assert selected is not None
        if (selected.parent_device, selected.parent_inode) != parent_identity:
            raise WorkspaceImportStoreConfigurationError(
                "workspace import root marker parent binding changed"
            )
        try:
            root_descriptor = os.open(
                self._root_name,
                _ROOT_OPEN_FLAGS,
                dir_fd=parent_descriptor,
            )
        except FileNotFoundError:
            if final is not None:
                raise WorkspaceImportStoreConfigurationError(
                    "workspace import root binding target is missing"
                ) from None
            os.mkdir(self._root_name, 0o700, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
            _after_fresh_root_parent_fsync(parent_descriptor, self._root_name)
            root_descriptor = os.open(
                self._root_name,
                _ROOT_OPEN_FLAGS,
                dir_fd=parent_descriptor,
            )
            os.fchmod(root_descriptor, 0o700)
        except OSError as exc:
            raise WorkspaceImportStoreConfigurationError(
                "workspace import root must be a no-follow directory"
            ) from exc
        self._root_descriptor = root_descriptor
        root_status = os.fstat(root_descriptor)
        self._require_private_directory(root_status, label="workspace import root")
        path_status = os.stat(
            self._root_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if not _same_inode(root_status, path_status):
            raise WorkspaceImportStoreConfigurationError(
                "workspace import root changed while it was opened"
            )
        if final is None:
            with os.scandir(root_descriptor) as entries:
                if next(entries, None) is not None:
                    raise WorkspaceImportStoreConfigurationError(
                        "workspace import pending root is not empty"
                    )
            final = self._finish_root_binding(selected, root_descriptor)
        else:
            if (final.root_device, final.root_inode) != (root_status.st_dev, root_status.st_ino):
                raise WorkspaceImportStoreConfigurationError(
                    "workspace import root binding changed"
                )
            try:
                token = _xattrs.getxattr(root_descriptor, _ROOT_TOKEN_XATTR)
            except OSError as exc:
                raise WorkspaceImportStoreConfigurationError(
                    "workspace import root identity token is unavailable"
                ) from exc
            if token != bytes.fromhex(final.store_token):
                raise WorkspaceImportStoreConfigurationError(
                    "workspace import root identity token changed"
                )
            if pending is not None:
                expected_pending = _authenticated_root_marker(
                    _RootMarker(
                        store_token=final.store_token,
                        root_name=final.root_name,
                        parent_device=final.parent_device,
                        parent_inode=final.parent_inode,
                    ),
                    self._auth_key,
                )
                if pending != expected_pending:
                    raise WorkspaceImportStoreConfigurationError(
                        "workspace import pending root marker conflicts"
                    )
                self._remove_parent_marker(self._pending_marker_name)
        self._root_identity = (root_status.st_dev, root_status.st_ino)
        self._root_marker = final
        if bootstrap is not None:
            self._remove_parent_marker(self._bootstrap_name)

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
    def _locked_root(
        self,
        *,
        cancel_check: CancellationCheck | None = None,
    ) -> Iterator[int]:
        thread_lock = _thread_lock_for(self._root)
        if cancel_check is None:
            thread_lock.acquire()
        else:
            while True:
                _check_cancel(cancel_check)
                if thread_lock.acquire(timeout=_LOCK_CANCEL_POLL_SECONDS):
                    break
        try:
            _check_cancel(cancel_check)
            self._verify_root_path_binding()
            descriptor = os.dup(self._root_descriptor)
            locked = False
            try:
                if cancel_check is None:
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                else:
                    while True:
                        _check_cancel(cancel_check)
                        try:
                            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            time.sleep(_LOCK_CANCEL_POLL_SECONDS)
                locked = True
                _check_cancel(cancel_check)
                opened = os.fstat(descriptor)
                self._require_private_directory(opened, label="workspace import root")
                if (opened.st_dev, opened.st_ino) != self._root_identity:
                    raise WorkspaceImportIntegrityError("workspace import root binding changed")
                self._verify_root_path_binding()
                yield descriptor
                self._verify_root_path_binding()
            finally:
                try:
                    if locked:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
        finally:
            thread_lock.release()

    def _verify_root_path_binding(self) -> None:
        try:
            parent_descriptor, identities = self._open_parent_chain()
        except WorkspaceImportStoreConfigurationError as exc:
            raise WorkspaceImportIntegrityError(
                "workspace import ancestor binding changed"
            ) from exc
        try:
            if identities != self._ancestor_identities:
                raise WorkspaceImportIntegrityError("workspace import ancestor binding changed")
            current = os.stat(
                self._root_name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (current.st_dev, current.st_ino) != self._root_identity:
                raise WorkspaceImportIntegrityError("workspace import root binding changed")
            opened = os.fstat(self._root_descriptor)
            self._require_private_directory(opened, label="workspace import root")
            if (opened.st_dev, opened.st_ino) != self._root_identity:
                raise WorkspaceImportIntegrityError("workspace import root binding changed")
            marker = self._read_root_marker(parent_descriptor, self._marker_name)
            if marker != self._root_marker:
                raise WorkspaceImportIntegrityError("workspace import root binding marker changed")
            key_status = os.fstat(self._auth_key_descriptor)
            self._require_private_marker(
                key_status,
                label="workspace import authentication key",
            )
            current_key = os.stat(
                self._auth_key_name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (
                (key_status.st_dev, key_status.st_ino) != self._auth_key_identity
                or not _same_inode(key_status, current_key)
                or key_status.st_size != _AUTH_KEY_BYTES
                or os.pread(self._auth_key_descriptor, _AUTH_KEY_BYTES, 0) != self._auth_key
                or _identity(os.fstat(self._auth_key_descriptor)) != _identity(key_status)
            ):
                raise WorkspaceImportIntegrityError(
                    "workspace import authentication key binding changed"
                )
            token = _xattrs.getxattr(self._root_descriptor, _ROOT_TOKEN_XATTR)
            assert self._root_marker is not None
            if token != bytes.fromhex(self._root_marker.store_token):
                raise WorkspaceImportIntegrityError("workspace import root identity token changed")
        except FileNotFoundError as exc:
            raise WorkspaceImportIntegrityError("workspace import root binding changed") from exc
        except (OSError, WorkspaceImportStoreConfigurationError) as exc:
            raise WorkspaceImportIntegrityError(
                "workspace import root binding verification failed"
            ) from exc
        finally:
            os.close(parent_descriptor)

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

    def _pending_lease_token(
        self,
        import_ref: WorkspaceImportRefV1,
        ownership: WorkspaceImportOwnership,
    ) -> str:
        return _authentication(
            self._auth_key,
            _PENDING_LEASE_DOMAIN,
            _pending_lease_payload(import_ref, ownership),
        )

    def _pending_lease_marker(
        self,
        import_ref: WorkspaceImportRefV1,
        ownership: WorkspaceImportOwnership,
    ) -> bytes:
        token = bytes.fromhex(self._pending_lease_token(import_ref, ownership))
        return hashlib.sha256(token).digest()

    def _archive_is_pending(
        self,
        archive_descriptor: int,
        import_ref: WorkspaceImportRefV1,
        ownership: WorkspaceImportOwnership,
    ) -> bool:
        try:
            marker = _xattrs.getxattr(archive_descriptor, _PENDING_LEASE_XATTR)
        except OSError as exc:
            missing_xattr = {errno.ENODATA}
            if hasattr(errno, "ENOATTR"):
                missing_xattr.add(errno.ENOATTR)
            if exc.errno in missing_xattr:
                return False
            raise WorkspaceImportIntegrityError(
                "workspace import pending lease state is unavailable"
            ) from exc
        if not hmac.compare_digest(
            marker,
            self._pending_lease_marker(import_ref, ownership),
        ):
            raise _DeterministicImportCorruption("workspace import pending lease state is invalid")
        return True

    def ingest(
        self,
        source: int | BinaryIO,
        *,
        ownership: WorkspaceImportOwnership,
        import_id: str | None = None,
    ) -> WorkspaceImportRefV1:
        """Validate and persist one already-open adopted archive handoff."""

        return self._ingest(
            source,
            ownership=ownership,
            import_id=import_id,
            pending=False,
        )

    def ingest_pending(
        self,
        source: int | BinaryIO,
        *,
        ownership: WorkspaceImportOwnership,
        import_id: str | None = None,
        cancel_check: CancellationCheck | None = None,
    ) -> PendingWorkspaceImport:
        """Persist one native-picker import under a private pending lease."""

        import_ref = self._ingest(
            source,
            ownership=ownership,
            import_id=import_id,
            pending=True,
            cancel_check=cancel_check,
        )
        return PendingWorkspaceImport(
            import_ref=import_ref,
            lease_token=self._pending_lease_token(import_ref, ownership),
        )

    def _ingest(
        self,
        source: int | BinaryIO,
        *,
        ownership: WorkspaceImportOwnership,
        import_id: str | None = None,
        pending: bool,
        cancel_check: CancellationCheck | None = None,
    ) -> WorkspaceImportRefV1:
        """Shared validated publication path for adopted and pending imports."""

        if not isinstance(ownership, WorkspaceImportOwnership):
            raise TypeError("ingest requires WorkspaceImportOwnership")
        _check_cancel(cancel_check)
        source_descriptor = self._source_descriptor(source)
        before = os.fstat(source_descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise WorkspaceArchiveValidationError("workspace import source must be a regular file")
        if before.st_size < 1024 or before.st_size > self._max_archive_bytes:
            raise WorkspaceArchiveValidationError("workspace archive byte-size budget exceeded")
        source_identity = _identity(before)
        source_digest = _source_sha256(
            source_descriptor,
            before.st_size,
            cancel_check=cancel_check,
        )
        if _identity(os.fstat(source_descriptor)) != source_identity:
            raise WorkspaceArchiveValidationError(
                "workspace import source identity changed while reading"
            )
        if import_id is None:
            import_id = f"{_IMPORT_ID_PREFIX}{secrets.token_hex(24)}"
        else:
            self._require_store_import_id(import_id)
        temporary_name = f"{_TEMP_PREFIX}{secrets.token_hex(24)}"
        temporary_identity: tuple[int, int] | None = None
        published = False
        with self._locked_root(cancel_check=cancel_check) as root_descriptor:
            retained, existing_ref = self._retained_usage(
                root_descriptor,
                requested_import_id=import_id,
                requested_ownership=ownership,
                requested_size=before.st_size,
                requested_digest=source_digest,
                cancel_check=cancel_check,
            )
            if existing_ref is not None:
                _check_cancel(cancel_check)
                if (
                    _identity(os.fstat(source_descriptor)) != source_identity
                    or _source_sha256(
                        source_descriptor,
                        before.st_size,
                        cancel_check=cancel_check,
                    )
                    != source_digest
                    or _identity(os.fstat(source_descriptor)) != source_identity
                ):
                    raise WorkspaceArchiveValidationError(
                        "workspace import source identity changed while reading"
                    )
                return existing_ref
            self._require_retained_capacity(
                retained.import_count + 1,
                retained.archive_bytes + before.st_size,
            )
            if pending:
                self._require_pending_capacity(
                    retained.pending_count + 1,
                    retained.pending_archive_bytes + before.st_size,
                )
            os.mkdir(temporary_name, 0o700, dir_fd=root_descriptor)
            created_temporary = os.stat(
                temporary_name,
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
            temporary_identity = (
                created_temporary.st_dev,
                created_temporary.st_ino,
            )
            temporary_descriptor: int | None = None
            archive_descriptor: int | None = None
            try:
                temporary_descriptor = os.open(
                    temporary_name,
                    _DIR_OPEN_FLAGS,
                    dir_fd=root_descriptor,
                )
                os.fchmod(temporary_descriptor, 0o700)
                opened_temporary = os.fstat(temporary_descriptor)
                self._require_private_directory(
                    opened_temporary, label="temporary workspace import"
                )
                if (
                    opened_temporary.st_dev,
                    opened_temporary.st_ino,
                ) != temporary_identity:
                    raise WorkspaceImportIntegrityError(
                        "temporary workspace import changed after creation"
                    )
                self._verify_directory_binding(
                    root_descriptor,
                    temporary_name,
                    temporary_descriptor,
                    label="temporary workspace import",
                )
                archive_descriptor = os.open(
                    _ARCHIVE_NAME,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=temporary_descriptor,
                )
                os.fchmod(archive_descriptor, 0o600)
                try:
                    result = _copy_and_validate_archive(
                        source_descriptor,
                        archive_descriptor,
                        before.st_size,
                        max_entries=self._max_entries,
                        max_extracted_bytes=self._max_extracted_bytes,
                        cancel_check=cancel_check,
                    )
                except WorkspaceArchiveValidationError as exc:
                    if (
                        _identity(os.fstat(source_descriptor)) != source_identity
                        or _source_sha256(
                            source_descriptor,
                            before.st_size,
                            cancel_check=cancel_check,
                        )
                        != source_digest
                    ):
                        raise WorkspaceArchiveValidationError(
                            "workspace import source identity changed while reading"
                        ) from exc
                    raise
                import_ref = WorkspaceImportRefV1(
                    import_id=import_id,
                    content_sha256=result.content_sha256,
                    byte_size=result.byte_size,
                    entry_count=result.entry_count,
                    extracted_byte_size=result.extracted_byte_size,
                )
                archive_token = secrets.token_hex(32)
                _xattrs.setxattr(
                    archive_descriptor,
                    _ARCHIVE_TOKEN_XATTR,
                    bytes.fromhex(archive_token),
                )
                if pending:
                    _xattrs.setxattr(
                        archive_descriptor,
                        _PENDING_LEASE_XATTR,
                        self._pending_lease_marker(import_ref, ownership),
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
                    or _source_sha256(
                        source_descriptor,
                        before.st_size,
                        cancel_check=cancel_check,
                    )
                    != result.content_sha256
                    or _identity(os.fstat(source_descriptor)) != source_identity
                ):
                    raise WorkspaceArchiveValidationError(
                        "workspace import source identity changed while reading"
                    )
                directory_status = os.fstat(temporary_descriptor)
                metadata_descriptor = os.open(
                    _METADATA_NAME,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=temporary_descriptor,
                )
                try:
                    os.fchmod(metadata_descriptor, 0o600)
                    metadata_status = os.fstat(metadata_descriptor)
                    self._require_private_file(
                        metadata_status,
                        label="temporary workspace import metadata",
                    )
                    stored_metadata = _authenticated_metadata(
                        _StoredMetadata(
                            import_ref=import_ref,
                            ownership=ownership,
                            directory_device=directory_status.st_dev,
                            directory_inode=directory_status.st_ino,
                            archive_device=archive_status.st_dev,
                            archive_inode=archive_status.st_ino,
                            metadata_device=metadata_status.st_dev,
                            metadata_inode=metadata_status.st_ino,
                            archive_token=archive_token,
                            authentication="",
                        ),
                        self._auth_key,
                    )
                    metadata = _canonical_metadata(stored_metadata)
                    if len(metadata) > _METADATA_MAX_BYTES:
                        raise WorkspaceImportError(
                            "workspace import ownership metadata exceeds its byte limit"
                        )
                    _write_all(metadata_descriptor, metadata)
                    os.fsync(metadata_descriptor)
                finally:
                    os.close(metadata_descriptor)
                _after_metadata_fsync(temporary_descriptor)
                os.fsync(temporary_descriptor)
                _check_cancel(cancel_check)
                _before_import_publish(root_descriptor, import_id)
                _check_cancel(cancel_check)
                self._verify_directory_binding(
                    root_descriptor,
                    temporary_name,
                    temporary_descriptor,
                    label="temporary workspace import",
                )
                self._validate_open_import_contents(
                    temporary_descriptor,
                    stored_metadata,
                    cancel_check=cancel_check,
                )
                _check_cancel(cancel_check)
                _rename_noreplace(temporary_name, import_id, directory_fd=root_descriptor)
                published = True
                self._verify_directory_binding(
                    root_descriptor,
                    import_id,
                    temporary_descriptor,
                    label="published workspace import",
                )
                os.fsync(root_descriptor)
                _after_import_publish(root_descriptor, import_id)
                try:
                    _check_cancel(cancel_check)
                    self._validate_open_import_contents(
                        temporary_descriptor,
                        stored_metadata,
                        cancel_check=cancel_check,
                    )
                    self._verify_directory_binding(
                        root_descriptor,
                        import_id,
                        temporary_descriptor,
                        label="published workspace import",
                    )
                except WorkspaceImportCancelled:
                    return import_ref
                except _DeterministicImportCorruption:
                    self._discard_flat_directory(
                        root_descriptor,
                        import_id,
                        missing_ok=False,
                        expected_identity=(
                            directory_status.st_dev,
                            directory_status.st_ino,
                        ),
                    )
                    os.fsync(root_descriptor)
                    raise
                return import_ref
            finally:
                if archive_descriptor is not None:
                    os.close(archive_descriptor)
                if temporary_descriptor is not None:
                    os.close(temporary_descriptor)
                if not published and temporary_identity is not None:
                    self._discard_flat_directory(
                        root_descriptor,
                        temporary_name,
                        missing_ok=True,
                        expected_identity=temporary_identity,
                    )

    def _require_retained_capacity(self, import_count: int, archive_bytes: int) -> None:
        if import_count > self._max_retained_imports:
            raise WorkspaceImportError("workspace retained import count budget exceeded")
        if archive_bytes > self._max_retained_archive_bytes:
            raise WorkspaceImportError("workspace retained archive byte budget exceeded")
        required_nodes, required_bytes = _required_reconcile_budget(import_count, archive_bytes)
        if (
            required_nodes > self._reconcile_max_nodes
            or required_bytes > self._reconcile_max_bytes
        ):
            raise WorkspaceImportError("workspace retained reconciliation budget exceeded")

    def _require_pending_capacity(self, import_count: int, archive_bytes: int) -> None:
        if import_count > self._max_pending_imports:
            raise WorkspaceImportError("workspace pending import count budget exceeded")
        if archive_bytes > self._max_pending_archive_bytes:
            raise WorkspaceImportError("workspace pending archive byte budget exceeded")

    def _retained_usage(
        self,
        root_descriptor: int,
        *,
        requested_import_id: str | None = None,
        requested_ownership: WorkspaceImportOwnership | None = None,
        requested_size: int | None = None,
        requested_digest: str | None = None,
        cancel_check: CancellationCheck | None = None,
    ) -> tuple[_RetainedUsage, WorkspaceImportRefV1 | None]:
        budget = _ScanBudget(
            remaining_nodes=self._reconcile_max_nodes,
            remaining_bytes=self._reconcile_max_bytes,
        )
        import_count = 0
        archive_bytes = 0
        pending_count = 0
        pending_archive_bytes = 0
        existing_ref: WorkspaceImportRefV1 | None = None
        with os.scandir(root_descriptor) as entries:
            names = []
            for entry in entries:
                _check_cancel(cancel_check)
                budget.charge_node(entry.name)
                names.append(entry.name)
        for name in names:
            _check_cancel(cancel_check)
            if _IMPORT_ID_RE.fullmatch(name) is None:
                raise WorkspaceImportIntegrityError(
                    "workspace import store requires reconciliation before ingest"
                )
            stored_ref, stored_ownership, archive_descriptor, _directory_identity = (
                self._validate_import_contents(
                    root_descriptor,
                    name,
                    None,
                    budget=budget,
                    cancel_check=cancel_check,
                )
            )
            try:
                if self._archive_is_pending(
                    archive_descriptor,
                    stored_ref,
                    stored_ownership,
                ):
                    pending_count += 1
                    pending_archive_bytes += stored_ref.byte_size
                    self._require_pending_capacity(
                        pending_count,
                        pending_archive_bytes,
                    )
            finally:
                os.close(archive_descriptor)
            import_count += 1
            archive_bytes += stored_ref.byte_size
            self._require_retained_capacity(import_count, archive_bytes)
            if requested_ownership is None:
                continue
            if name == requested_import_id and (
                stored_ownership != requested_ownership
                or stored_ref.byte_size != requested_size
                or stored_ref.content_sha256 != requested_digest
            ):
                raise WorkspaceImportIntegrityError(
                    "workspace import ID was reused for different content or ownership"
                )
            if stored_ownership == requested_ownership:
                if existing_ref is not None and existing_ref != stored_ref:
                    raise WorkspaceImportIntegrityError(
                        "workspace import ownership ledger has duplicate claims"
                    )
                if (
                    stored_ref.byte_size != requested_size
                    or stored_ref.content_sha256 != requested_digest
                ):
                    raise WorkspaceImportIntegrityError(
                        "workspace import idempotency key was reused for different content"
                    )
                existing_ref = stored_ref
            elif (
                stored_ownership.operation_id == requested_ownership.operation_id
                or stored_ownership.idempotency_key == requested_ownership.idempotency_key
            ):
                raise WorkspaceImportIntegrityError(
                    "workspace import ownership or idempotency binding conflicts"
                )
        return (
            _RetainedUsage(
                import_count=import_count,
                archive_bytes=archive_bytes,
                pending_count=pending_count,
                pending_archive_bytes=pending_archive_bytes,
            ),
            existing_ref,
        )

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
            raise WorkspaceImportIntegrityError(
                "workspace import metadata is unavailable"
            ) from exc
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
                raise WorkspaceImportIntegrityError(
                    "workspace import metadata changed while reading"
                )
            try:
                payload = json.loads(raw)
                if not isinstance(payload, dict) or set(payload) != {
                    "authentication",
                    "import_ref",
                    "ownership",
                    "schema_version",
                    "storage_identity",
                }:
                    raise ValueError("metadata fields are not closed")
                if payload["schema_version"] != "2":
                    raise ValueError("metadata schema version is invalid")
                ownership_payload = payload["ownership"]
                if not isinstance(ownership_payload, dict) or set(ownership_payload) != {
                    "idempotency_key",
                    "operation_id",
                    "project_id",
                }:
                    raise ValueError("ownership fields are not closed")
                storage_identity = payload["storage_identity"]
                if not isinstance(storage_identity, dict) or set(storage_identity) != {
                    "archive_device",
                    "archive_inode",
                    "archive_token",
                    "directory_device",
                    "directory_inode",
                    "metadata_device",
                    "metadata_inode",
                }:
                    raise ValueError("storage identity fields are not closed")
                identity_values = tuple(
                    storage_identity[key]
                    for key in (
                        "archive_device",
                        "archive_inode",
                        "directory_device",
                        "directory_inode",
                        "metadata_device",
                        "metadata_inode",
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
                authentication = payload["authentication"]
                if (
                    not isinstance(authentication, str)
                    or re.fullmatch(r"[0-9a-f]{64}", authentication) is None
                ):
                    raise ValueError("metadata authentication is invalid")
                metadata = _StoredMetadata(
                    import_ref=WorkspaceImportRefV1.model_validate(payload["import_ref"]),
                    ownership=WorkspaceImportOwnership(
                        project_id=ownership_payload["project_id"],
                        operation_id=ownership_payload["operation_id"],
                        idempotency_key=ownership_payload["idempotency_key"],
                    ),
                    directory_device=storage_identity["directory_device"],
                    directory_inode=storage_identity["directory_inode"],
                    archive_device=storage_identity["archive_device"],
                    archive_inode=storage_identity["archive_inode"],
                    metadata_device=storage_identity["metadata_device"],
                    metadata_inode=storage_identity["metadata_inode"],
                    archive_token=archive_token,
                    authentication=authentication,
                )
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise _DeterministicImportCorruption(
                    "workspace import metadata is not closed JSON"
                ) from exc
            if raw != _canonical_metadata(metadata):
                raise _DeterministicImportCorruption(
                    "workspace import metadata is not canonical JSON"
                )
            if (before.st_dev, before.st_ino) != (
                metadata.metadata_device,
                metadata.metadata_inode,
            ):
                raise _DeterministicImportCorruption("workspace import metadata identity changed")
            authenticated = _authenticated_metadata(metadata, self._auth_key)
            if not hmac.compare_digest(
                metadata.authentication,
                authenticated.authentication,
            ):
                raise _DeterministicImportCorruption(
                    "workspace import metadata authentication failed"
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
        ownership: WorkspaceImportOwnership,
        budget: _ScanBudget | None = None,
        cancel_check: CancellationCheck | None = None,
    ) -> int:
        _check_cancel(cancel_check)
        try:
            descriptor = os.open(_ARCHIVE_NAME, _FILE_READ_FLAGS, dir_fd=directory_descriptor)
        except OSError as exc:
            raise WorkspaceImportIntegrityError("workspace import archive is unavailable") from exc
        try:
            before = os.fstat(descriptor)
            self._require_private_file(before, label="workspace import archive")
            if (before.st_dev, before.st_ino) != archive_identity:
                raise _DeterministicImportCorruption("workspace import archive identity changed")
            try:
                observed_token = _xattrs.getxattr(descriptor, _ARCHIVE_TOKEN_XATTR)
            except OSError as exc:
                raise WorkspaceImportIntegrityError(
                    "workspace import archive storage token is unavailable"
                ) from exc
            if observed_token != bytes.fromhex(archive_token):
                raise _DeterministicImportCorruption(
                    "workspace import archive storage token changed"
                )
            self._archive_is_pending(descriptor, import_ref, ownership)
            if before.st_size != import_ref.byte_size:
                raise _DeterministicImportCorruption("workspace import archive size changed")
            digests: list[str] = []
            for _attempt in range(2):
                _check_cancel(cancel_check)
                digest = hashlib.sha256()
                remaining = before.st_size
                while remaining:
                    _check_cancel(cancel_check)
                    chunk = os.read(descriptor, min(remaining, _COPY_CHUNK_BYTES))
                    if not chunk:
                        raise WorkspaceImportIntegrityError(
                            "workspace import archive was truncated"
                        )
                    if budget is not None:
                        budget.charge_bytes(len(chunk))
                    digest.update(chunk)
                    remaining -= len(chunk)
                if os.read(descriptor, 1):
                    raise WorkspaceImportIntegrityError(
                        "workspace import archive grew while reading"
                    )
                digests.append(digest.hexdigest())
                os.lseek(descriptor, 0, os.SEEK_SET)
            after = os.fstat(descriptor)
            current = os.stat(
                _ARCHIVE_NAME,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if _identity(before) != _identity(after) or not _same_inode(after, current):
                raise WorkspaceImportIntegrityError(
                    "workspace import archive changed while hashing"
                )
            if any(digest != import_ref.content_sha256 for digest in digests):
                raise _DeterministicImportCorruption("workspace import archive digest changed")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _validate_open_import_contents(
        self,
        directory_descriptor: int,
        expected_metadata: _StoredMetadata,
        *,
        cancel_check: CancellationCheck | None = None,
    ) -> None:
        _check_cancel(cancel_check)
        names: set[str] = set()
        with os.scandir(directory_descriptor) as entries:
            for entry in entries:
                _check_cancel(cancel_check)
                names.add(entry.name)
        if names != {_ARCHIVE_NAME, _METADATA_NAME}:
            raise _DeterministicImportCorruption("workspace import directory shape is invalid")
        directory_status = os.fstat(directory_descriptor)
        if (directory_status.st_dev, directory_status.st_ino) != (
            expected_metadata.directory_device,
            expected_metadata.directory_inode,
        ):
            raise _DeterministicImportCorruption("workspace import directory identity changed")
        observed_metadata = self._load_metadata(directory_descriptor)
        if observed_metadata != expected_metadata:
            raise _DeterministicImportCorruption("workspace import persisted metadata changed")
        archive_descriptor = self._open_verified_archive(
            directory_descriptor,
            expected_metadata.import_ref,
            archive_identity=(
                expected_metadata.archive_device,
                expected_metadata.archive_inode,
            ),
            archive_token=expected_metadata.archive_token,
            ownership=expected_metadata.ownership,
            cancel_check=cancel_check,
        )
        os.close(archive_descriptor)
        if _identity(os.fstat(directory_descriptor)) != _identity(directory_status):
            raise WorkspaceImportIntegrityError(
                "workspace import directory changed during verification"
            )

    def _validate_import_contents(
        self,
        root_descriptor: int,
        import_id: str,
        expected_ref: WorkspaceImportRefV1 | None,
        *,
        expected_ownership: WorkspaceImportOwnership | None = None,
        budget: _ScanBudget | None = None,
        cancel_check: CancellationCheck | None = None,
    ) -> tuple[WorkspaceImportRefV1, WorkspaceImportOwnership, int, tuple[int, int]]:
        self._require_store_import_id(import_id)
        _check_cancel(cancel_check)
        directory_descriptor = self._open_import_directory(root_descriptor, import_id)
        archive_descriptor: int | None = None
        try:
            names: set[str] = set()
            with os.scandir(directory_descriptor) as entries:
                for entry in entries:
                    _check_cancel(cancel_check)
                    if budget is not None:
                        budget.charge_node(entry.name)
                    names.add(entry.name)
            if names != {_ARCHIVE_NAME, _METADATA_NAME}:
                raise _DeterministicImportCorruption("workspace import directory shape is invalid")
            metadata = self._load_metadata(directory_descriptor, budget=budget)
            stored_ref = metadata.import_ref
            if stored_ref.import_id != import_id:
                raise _DeterministicImportCorruption("workspace import metadata ID does not match")
            if expected_ref is not None and stored_ref != expected_ref:
                raise WorkspaceImportIntegrityError(
                    "workspace import reference does not match storage"
                )
            if expected_ownership is not None and metadata.ownership != expected_ownership:
                raise WorkspaceImportIntegrityError(
                    "workspace import ownership does not match storage"
                )
            directory_status = os.fstat(directory_descriptor)
            if (directory_status.st_dev, directory_status.st_ino) != (
                metadata.directory_device,
                metadata.directory_inode,
            ):
                raise _DeterministicImportCorruption("workspace import directory identity changed")
            archive_descriptor = self._open_verified_archive(
                directory_descriptor,
                stored_ref,
                archive_identity=(metadata.archive_device, metadata.archive_inode),
                archive_token=metadata.archive_token,
                ownership=metadata.ownership,
                budget=budget,
                cancel_check=cancel_check,
            )
            self._verify_directory_binding(
                root_descriptor,
                import_id,
                directory_descriptor,
                label="workspace import directory",
            )
            return (
                stored_ref,
                metadata.ownership,
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
            raise WorkspaceImportIntegrityError("workspace import ID was not issued by this store")

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

    @staticmethod
    def _require_ownership(
        ownership: WorkspaceImportOwnership,
        *,
        operation: str,
    ) -> None:
        if not isinstance(ownership, WorkspaceImportOwnership):
            raise TypeError(f"{operation} requires WorkspaceImportOwnership")

    @staticmethod
    def _unlink_bound_file(
        directory_descriptor: int,
        name: str,
        *,
        expected_identity: tuple[int, int],
        missing_ok: bool,
    ) -> None:
        try:
            value = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            if missing_ok:
                return
            raise WorkspaceImportNotFoundError(
                "workspace import temporary file is missing"
            ) from None
        if (value.st_dev, value.st_ino) != expected_identity:
            raise WorkspaceImportIntegrityError("workspace import file changed before cleanup")
        if stat.S_ISDIR(value.st_mode):
            raise WorkspaceImportIntegrityError(
                "workspace import file cleanup refuses a directory"
            )
        quarantine = f"{_QUARANTINE_PREFIX}{secrets.token_hex(24)}"
        _quarantine_noreplace(name, quarantine, directory_fd=directory_descriptor)
        moved = os.stat(
            quarantine,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (moved.st_dev, moved.st_ino) != expected_identity:
            raise WorkspaceImportIntegrityError("workspace import file changed during quarantine")
        os.unlink(quarantine, dir_fd=directory_descriptor)

    def _snapshot_verified_archive(
        self,
        root_descriptor: int,
        archive_descriptor: int,
        import_ref: WorkspaceImportRefV1,
    ) -> int:
        snapshot_name = f"{_SNAPSHOT_PREFIX}{secrets.token_hex(24)}"
        writer = os.open(
            snapshot_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=root_descriptor,
        )
        snapshot_identity: tuple[int, int] | None = None
        reader: int | None = None
        try:
            os.fchmod(writer, 0o600)
            initial_snapshot = os.fstat(writer)
            snapshot_identity = (initial_snapshot.st_dev, initial_snapshot.st_ino)
            source_before = os.fstat(archive_descriptor)
            source_identity = _identity(source_before)
            digest = hashlib.sha256()
            offset = 0
            while offset < import_ref.byte_size:
                try:
                    chunk = os.pread(
                        archive_descriptor,
                        min(_COPY_CHUNK_BYTES, import_ref.byte_size - offset),
                        offset,
                    )
                except InterruptedError:
                    continue
                if not chunk:
                    raise WorkspaceImportIntegrityError(
                        "workspace import archive was truncated during snapshot"
                    )
                _write_all(writer, chunk)
                digest.update(chunk)
                offset += len(chunk)
            if os.pread(archive_descriptor, 1, import_ref.byte_size):
                raise WorkspaceImportIntegrityError(
                    "workspace import archive grew during snapshot"
                )
            os.fsync(writer)
            snapshot_status = os.fstat(writer)
            if (snapshot_status.st_dev, snapshot_status.st_ino) != snapshot_identity:
                raise WorkspaceImportIntegrityError(
                    "workspace import private snapshot identity changed"
                )
            if (
                snapshot_status.st_size != import_ref.byte_size
                or digest.hexdigest() != import_ref.content_sha256
            ):
                raise WorkspaceImportIntegrityError(
                    "workspace import snapshot digest or size does not match"
                )
            _before_snapshot_commit(root_descriptor, archive_descriptor)
            source_after = os.fstat(archive_descriptor)
            try:
                source_digest = _source_sha256(
                    archive_descriptor,
                    import_ref.byte_size,
                )
            except WorkspaceArchiveValidationError as exc:
                raise WorkspaceImportIntegrityError(
                    "workspace import archive changed before snapshot commit"
                ) from exc
            if (
                _identity(source_after) != source_identity
                or source_digest != import_ref.content_sha256
                or _identity(os.fstat(archive_descriptor)) != source_identity
            ):
                raise WorkspaceImportIntegrityError(
                    "workspace import archive changed before snapshot commit"
                )
            os.close(writer)
            writer = -1
            reader = os.open(snapshot_name, _FILE_READ_FLAGS, dir_fd=root_descriptor)
            opened = os.fstat(reader)
            self._require_private_file(opened, label="workspace import private snapshot")
            try:
                snapshot_digest = _source_sha256(reader, import_ref.byte_size)
            except WorkspaceArchiveValidationError as exc:
                raise WorkspaceImportIntegrityError(
                    "workspace import private snapshot changed while verifying"
                ) from exc
            verified = os.fstat(reader)
            current = os.stat(
                snapshot_name,
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
            if (
                (opened.st_dev, opened.st_ino) != snapshot_identity
                or _identity(opened) != _identity(verified)
                or not _same_inode(opened, current)
                or opened.st_size != import_ref.byte_size
                or snapshot_digest != import_ref.content_sha256
            ):
                raise WorkspaceImportIntegrityError(
                    "workspace import private snapshot binding or digest changed"
                )
            self._unlink_bound_file(
                root_descriptor,
                snapshot_name,
                expected_identity=snapshot_identity,
                missing_ok=False,
            )
            snapshot_identity = None
            os.fsync(root_descriptor)
            unlinked_before = os.fstat(reader)
            try:
                unlinked_digest = _source_sha256(reader, import_ref.byte_size)
            except WorkspaceArchiveValidationError as exc:
                raise WorkspaceImportIntegrityError(
                    "workspace import unlinked snapshot changed while verifying"
                ) from exc
            unlinked_after = os.fstat(reader)
            if (
                (unlinked_before.st_dev, unlinked_before.st_ino) != (opened.st_dev, opened.st_ino)
                or unlinked_before.st_nlink != 0
                or _identity(unlinked_before) != _identity(unlinked_after)
                or unlinked_digest != import_ref.content_sha256
            ):
                raise WorkspaceImportIntegrityError(
                    "workspace import unlinked snapshot identity or digest changed"
                )
            os.lseek(reader, 0, os.SEEK_SET)
            return reader
        except BaseException:
            if reader is not None:
                os.close(reader)
            raise
        finally:
            if writer >= 0:
                os.close(writer)
            if snapshot_identity is not None:
                self._unlink_bound_file(
                    root_descriptor,
                    snapshot_name,
                    expected_identity=snapshot_identity,
                    missing_ok=True,
                )

    def verify(
        self,
        import_ref: WorkspaceImportRefV1,
        *,
        ownership: WorkspaceImportOwnership,
    ) -> None:
        """Verify one exact import and its owner without exposing archive bytes."""

        self._require_external_import_ref(import_ref, operation="verify")
        self._require_ownership(ownership, operation="verify")
        with self._locked_root() as root_descriptor:
            _stored_ref, _stored_ownership, archive_descriptor, _directory_identity = (
                self._validate_import_contents(
                    root_descriptor,
                    import_ref.import_id,
                    import_ref,
                    expected_ownership=ownership,
                )
            )
            os.close(archive_descriptor)

    def adopt_pending(
        self,
        import_ref: WorkspaceImportRefV1,
        *,
        ownership: WorkspaceImportOwnership,
    ) -> None:
        """Idempotently make a verified pending import durable."""

        self._require_external_import_ref(import_ref, operation="adopt")
        self._require_ownership(ownership, operation="adopt")
        with self._locked_root() as root_descriptor:
            _stored_ref, _stored_ownership, archive_descriptor, _directory_identity = (
                self._validate_import_contents(
                    root_descriptor,
                    import_ref.import_id,
                    import_ref,
                    expected_ownership=ownership,
                )
            )
            try:
                if not self._archive_is_pending(archive_descriptor, import_ref, ownership):
                    return
                _xattrs.removexattr(archive_descriptor, _PENDING_LEASE_XATTR)
                os.fsync(archive_descriptor)
                if self._archive_is_pending(archive_descriptor, import_ref, ownership):
                    raise WorkspaceImportIntegrityError(
                        "workspace import pending lease adoption did not persist"
                    )
            finally:
                os.close(archive_descriptor)
            os.fsync(root_descriptor)

    def discard_pending(
        self,
        import_ref: WorkspaceImportRefV1,
        *,
        ownership: WorkspaceImportOwnership,
        lease_token: str,
    ) -> None:
        """Delete an exact unadopted native-picker lease, idempotently."""

        self._require_external_import_ref(import_ref, operation="discard")
        self._require_ownership(ownership, operation="discard")
        if type(lease_token) is not str or re.fullmatch(r"[0-9a-f]{64}", lease_token) is None:
            raise ValueError("workspace import pending lease token is invalid")
        if not hmac.compare_digest(
            lease_token,
            self._pending_lease_token(import_ref, ownership),
        ):
            raise WorkspaceImportIntegrityError(
                "workspace import pending lease token does not match"
            )
        with self._locked_root() as root_descriptor:
            try:
                _stored_ref, _stored_ownership, archive_descriptor, directory_identity = (
                    self._validate_import_contents(
                        root_descriptor,
                        import_ref.import_id,
                        import_ref,
                        expected_ownership=ownership,
                    )
                )
            except WorkspaceImportNotFoundError:
                return
            try:
                if not self._archive_is_pending(archive_descriptor, import_ref, ownership):
                    return
            finally:
                os.close(archive_descriptor)
            self._discard_flat_directory(
                root_descriptor,
                import_ref.import_id,
                missing_ok=False,
                expected_identity=directory_identity,
            )
            os.fsync(root_descriptor)

    @contextmanager
    def resolve(
        self,
        import_ref: WorkspaceImportRefV1,
        *,
        ownership: WorkspaceImportOwnership,
    ) -> Iterator[BinaryIO]:
        """Yield an unlinked private snapshot without exposing a host path."""

        self._require_external_import_ref(import_ref, operation="resolve")
        self._require_ownership(ownership, operation="resolve")
        archive_descriptor: int | None = None
        snapshot_descriptor: int | None = None
        try:
            with self._locked_root() as root_descriptor:
                _stored_ref, _stored_ownership, archive_descriptor, _directory_identity = (
                    self._validate_import_contents(
                        root_descriptor,
                        import_ref.import_id,
                        import_ref,
                        expected_ownership=ownership,
                    )
                )
                snapshot_descriptor = self._snapshot_verified_archive(
                    root_descriptor,
                    archive_descriptor,
                    import_ref,
                )
                os.close(archive_descriptor)
                archive_descriptor = None
            stream = os.fdopen(snapshot_descriptor, "rb", closefd=True)
            snapshot_descriptor = None
        except BaseException:
            if archive_descriptor is not None:
                os.close(archive_descriptor)
            if snapshot_descriptor is not None:
                os.close(snapshot_descriptor)
            raise
        try:
            yield stream
        finally:
            stream.close()

    def release(
        self,
        import_ref: WorkspaceImportRefV1,
        *,
        ownership: WorkspaceImportOwnership,
    ) -> None:
        """Delete the exact verified import referenced by ``import_ref``."""

        self._require_external_import_ref(import_ref, operation="release")
        self._require_ownership(ownership, operation="release")
        with self._locked_root() as root_descriptor:
            _stored_ref, _stored_ownership, archive_descriptor, directory_identity = (
                self._validate_import_contents(
                    root_descriptor,
                    import_ref.import_id,
                    import_ref,
                    expected_ownership=ownership,
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

    def delete(
        self,
        import_ref: WorkspaceImportRefV1,
        *,
        ownership: WorkspaceImportOwnership,
    ) -> None:
        """Alias for explicit lifecycle callers that use delete terminology."""

        self.release(import_ref, ownership=ownership)

    def _discard_flat_directory(
        self,
        root_descriptor: int,
        name: str,
        *,
        missing_ok: bool,
        expected_identity: tuple[int, int],
        budget: _ScanBudget | None = None,
    ) -> None:
        try:
            descriptor = os.open(name, _DIR_OPEN_FLAGS, dir_fd=root_descriptor)
        except FileNotFoundError:
            if missing_ok:
                return
            raise WorkspaceImportNotFoundError("workspace import does not exist") from None
        opened = os.fstat(descriptor)
        quarantine_name: str | None = None
        try:
            if (opened.st_dev, opened.st_ino) != expected_identity:
                raise WorkspaceImportIntegrityError(
                    "workspace import changed before directory cleanup"
                )
            quarantine_name = f"{_QUARANTINE_PREFIX}{secrets.token_hex(24)}"
            _quarantine_noreplace(name, quarantine_name, directory_fd=root_descriptor)
            moved = os.stat(
                quarantine_name,
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
            if not _same_inode(opened, moved):
                raise WorkspaceImportIntegrityError(
                    "workspace import changed during directory quarantine"
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
                self._unlink_bound_file(
                    descriptor,
                    child_name,
                    expected_identity=(child.st_dev, child.st_ino),
                    missing_ok=False,
                )
            os.fsync(descriptor)
            current = os.stat(
                quarantine_name,
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
            if not _same_inode(opened, current):
                raise WorkspaceImportIntegrityError(
                    "workspace import changed before directory cleanup"
                )
        finally:
            os.close(descriptor)
        assert quarantine_name is not None
        os.rmdir(quarantine_name, dir_fd=root_descriptor)

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
            self._unlink_bound_file(
                root_descriptor,
                name,
                expected_identity=expected_identity,
                missing_ok=False,
            )

    def reconcile(self) -> None:
        """Boundedly remove temp, malformed, tampered, and non-store root entries."""

        self._reconcile_references(None)

    def reconcile_references(
        self,
        references: Mapping[
            str,
            tuple[WorkspaceImportRefV1, WorkspaceImportOwnership],
        ],
    ) -> None:
        """Retain exactly the verified imports referenced by durable project state."""

        if not isinstance(references, Mapping):
            raise TypeError("workspace import references must be a mapping")
        expected: dict[
            str,
            tuple[WorkspaceImportRefV1, WorkspaceImportOwnership],
        ] = {}
        for import_id, value in references.items():
            if type(import_id) is not str or not isinstance(value, tuple) or len(value) != 2:
                raise TypeError("workspace import reference entry is invalid")
            import_ref, ownership = value
            self._require_external_import_ref(import_ref, operation="reconcile")
            self._require_ownership(ownership, operation="reconcile")
            if import_ref.import_id != import_id:
                raise ValueError("workspace import reference key does not match its ID")
            expected[import_id] = (import_ref, ownership)
        self._reconcile_references(expected)

    def _reconcile_references(
        self,
        expected: Mapping[
            str,
            tuple[WorkspaceImportRefV1, WorkspaceImportOwnership],
        ]
        | None,
    ) -> None:
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
            observed_entries.sort(key=lambda value: os.fsencode(value[0]))
            observed_by_name = {
                name: (identity, mode) for name, identity, mode in observed_entries
            }

            # Durable references are authoritative. Validate all of them before
            # performing any destructive orphan or corruption cleanup.
            if expected is not None:
                for name in sorted(expected, key=os.fsencode):
                    observed = observed_by_name.get(name)
                    if observed is None:
                        raise WorkspaceImportNotFoundError(
                            "a referenced workspace import does not exist"
                        )
                    observed_identity, observed_mode = observed
                    if not stat.S_ISDIR(observed_mode):
                        raise WorkspaceImportIntegrityError(
                            "referenced workspace import is corrupt"
                        )
                    import_ref, ownership = expected[name]
                    try:
                        _stored_ref, _stored_ownership, archive_descriptor, identity = (
                            self._validate_import_contents(
                                root_descriptor,
                                name,
                                import_ref,
                                expected_ownership=ownership,
                                budget=budget,
                            )
                        )
                    except _DeterministicImportCorruption:
                        raise WorkspaceImportIntegrityError(
                            "referenced workspace import is corrupt"
                        ) from None
                    try:
                        if identity != observed_identity:
                            raise WorkspaceImportIntegrityError(
                                "referenced workspace import changed during reconciliation"
                            )
                        if self._archive_is_pending(
                            archive_descriptor,
                            import_ref,
                            ownership,
                        ):
                            _xattrs.removexattr(archive_descriptor, _PENDING_LEASE_XATTR)
                            os.fsync(archive_descriptor)
                            if self._archive_is_pending(
                                archive_descriptor,
                                import_ref,
                                ownership,
                            ):
                                raise WorkspaceImportIntegrityError(
                                    "referenced workspace import adoption did not persist"
                                )
                    finally:
                        os.close(archive_descriptor)

            for name, observed_identity, observed_mode in observed_entries:
                if expected is not None and name in expected:
                    continue
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
                    _stored_ref, _stored_ownership, archive_descriptor, directory_identity = (
                        self._validate_import_contents(
                            root_descriptor,
                            name,
                            None,
                            expected_ownership=None,
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
                    if expected is not None:
                        self._discard_root_entry(
                            root_descriptor,
                            name,
                            expected_identity=observed_identity,
                            budget=budget,
                        )
            os.fsync(root_descriptor)


__all__ = [
    "PendingWorkspaceImport",
    "WorkspaceArchiveValidationError",
    "WorkspaceImportCancelled",
    "WorkspaceImportError",
    "WorkspaceImportIntegrityError",
    "WorkspaceImportNotFoundError",
    "WorkspaceImportOwnership",
    "WorkspaceImportStore",
    "WorkspaceImportStoreConfigurationError",
]
