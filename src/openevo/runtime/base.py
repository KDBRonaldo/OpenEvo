"""Runtime abstraction for container-backed rollout execution."""

from __future__ import annotations

import asyncio
import ctypes
import errno
import hashlib
import os
import secrets
import stat
import struct
import sys
import tempfile
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Coroutine, Final, Sequence

from openevo.runtime.models import ExecResult, RuntimeSpec

RUNTIME_SESSION_DIR: Final[str] = "/openevo/session"
RUNTIME_ARTIFACTS_DIR: Final[str] = f"{RUNTIME_SESSION_DIR}/artifacts"
RUNTIME_LOGS_DIR: Final[str] = f"{RUNTIME_SESSION_DIR}/logs"
RUNTIME_AGENT_LOG_DIR: Final[str] = f"{RUNTIME_LOGS_DIR}/agent"
RUNTIME_EVAL_LOG_DIR: Final[str] = f"{RUNTIME_LOGS_DIR}/eval"
RUNTIME_EVAL_ARTIFACT_DIR: Final[str] = f"{RUNTIME_SESSION_DIR}/eval_artifacts"
LOCAL_COMMAND_CAPTURE_MAX_BYTES: Final[int] = 4 * 1024 * 1024
RUNTIME_READBACK_MAX_FILES: Final[int] = 4096
RUNTIME_READBACK_MAX_BYTES: Final[int] = 64 * 1024 * 1024
# A maximally nested 4096-file tree has at most 8191 entries. Every directory
# is enumerated twice, so this limit admits that full receipt inventory while
# still bounding failed and verification enumeration attempts.
RUNTIME_READBACK_MAX_NODES: Final[int] = 4 * RUNTIME_READBACK_MAX_FILES
_LOCAL_COMMAND_FINALIZE_SECONDS: Final[float] = 5.0
_COPY_MAX_DEPTH: Final[int] = 64
_COPY_MAX_NODES: Final[int] = 100_000
_COPY_CHUNK_BYTES: Final[int] = 64 * 1024
_RENAME_NOREPLACE: Final[int] = 1
_READBACK_QUARANTINE_ATTEMPTS: Final[int] = 128
_DOWNLOAD_QUOTA_POLL_SECONDS: Final[float] = 0.01
_DOWNLOAD_JOIN_SECONDS: Final[float] = 1.0
_IN_NONBLOCK: Final[int] = os.O_NONBLOCK
_IN_CLOEXEC: Final[int] = os.O_CLOEXEC
_IN_MODIFY: Final[int] = 0x00000002
_IN_ATTRIB: Final[int] = 0x00000004
_IN_CLOSE_WRITE: Final[int] = 0x00000008
_IN_MOVED_FROM: Final[int] = 0x00000040
_IN_MOVED_TO: Final[int] = 0x00000080
_IN_CREATE: Final[int] = 0x00000100
_IN_DELETE: Final[int] = 0x00000200
_IN_DELETE_SELF: Final[int] = 0x00000400
_IN_MOVE_SELF: Final[int] = 0x00000800
_IN_Q_OVERFLOW: Final[int] = 0x00004000
_IN_IGNORED: Final[int] = 0x00008000
_IN_ISDIR: Final[int] = 0x40000000
_IN_MUTATION_MASK: Final[int] = (
    _IN_MODIFY
    | _IN_ATTRIB
    | _IN_CLOSE_WRITE
    | _IN_MOVED_FROM
    | _IN_MOVED_TO
    | _IN_CREATE
    | _IN_DELETE
    | _IN_DELETE_SELF
    | _IN_MOVE_SELF
)
_INOTIFY_EVENT_HEADER: Final[struct.Struct] = struct.Struct("iIII")
_DIRECTORY_FLAGS: Final[int] = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
_FILE_READ_FLAGS: Final[int] = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
_FILE_WRITE_FLAGS: Final[int] = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK


class RuntimePathSecurityError(RuntimeError):
    """Raised when a bind-mount path cannot be proven to stay in its authority."""


@dataclass(slots=True)
class RuntimeReadbackBudget:
    """Non-refundable limits shared by one trusted runtime readback."""

    max_files: int = RUNTIME_READBACK_MAX_FILES
    max_nodes: int = RUNTIME_READBACK_MAX_NODES
    max_bytes: int = RUNTIME_READBACK_MAX_BYTES
    _files: int = field(default=0, init=False)
    _nodes: int = field(default=0, init=False)
    _bytes: int = field(default=0, init=False)
    _lock: threading.Lock = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not 1 <= self.max_files <= RUNTIME_READBACK_MAX_FILES:
            raise ValueError("runtime readback file budget is outside the closed limit")
        if not 1 <= self.max_nodes <= RUNTIME_READBACK_MAX_NODES:
            raise ValueError("runtime readback node budget is outside the closed limit")
        if not 1 <= self.max_bytes <= RUNTIME_READBACK_MAX_BYTES:
            raise ValueError("runtime readback byte budget is outside the closed limit")
        self._lock = threading.Lock()

    @property
    def files_consumed(self) -> int:
        with self._lock:
            return self._files

    @property
    def nodes_consumed(self) -> int:
        with self._lock:
            return self._nodes

    @property
    def bytes_consumed(self) -> int:
        with self._lock:
            return self._bytes

    @property
    def remaining_files(self) -> int:
        with self._lock:
            return max(0, self.max_files - self._files)

    @property
    def remaining_nodes(self) -> int:
        with self._lock:
            return max(0, self.max_nodes - self._nodes)

    @property
    def remaining_bytes(self) -> int:
        with self._lock:
            return max(0, self.max_bytes - self._bytes)

    def consume_node(self) -> None:
        with self._lock:
            self._nodes += 1
            if self._nodes > self.max_nodes:
                raise RuntimePathSecurityError("runtime readback exceeds the node budget")

    def consume_file(self) -> None:
        with self._lock:
            self._files += 1
            if self._files > self.max_files:
                raise RuntimePathSecurityError("runtime readback exceeds the file budget")

    def require_byte_capacity(self, size: int) -> None:
        with self._lock:
            if size < 0 or size > self.max_bytes - self._bytes:
                raise RuntimePathSecurityError("runtime readback exceeds the byte budget")

    def consume_bytes(self, size: int) -> None:
        with self._lock:
            if size < 0 or size > self.max_bytes - self._bytes:
                raise RuntimePathSecurityError("runtime readback exceeds the byte budget")
            self._bytes += size

    def consume_report(self, *, files: int, nodes: int, bytes_read: int) -> None:
        if min(files, nodes, bytes_read) < 0:
            raise RuntimePathSecurityError("runtime readback returned an invalid budget report")
        with self._lock:
            self._files += files
            self._nodes += nodes
            self._bytes += bytes_read
            if (
                self._files > self.max_files
                or self._nodes > self.max_nodes
                or self._bytes > self.max_bytes
            ):
                raise RuntimePathSecurityError("runtime readback exceeds its shared budget")

    def exhaust(self) -> None:
        """Pessimistically prevent retry when remote failure hides exact usage."""

        with self._lock:
            self._files = max(self._files, self.max_files)
            self._nodes = max(self._nodes, self.max_nodes)
            self._bytes = max(self._bytes, self.max_bytes)


@dataclass(frozen=True, slots=True)
class RuntimeReadbackFile:
    relative_path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class RuntimeReadback:
    files: tuple[RuntimeReadbackFile, ...]


@dataclass(slots=True)
class _DirectoryPin:
    path: Path
    parts: tuple[str, ...]
    descriptors: list[int]
    identities: tuple[tuple[int, int, int, int], ...]

    @property
    def descriptor(self) -> int:
        return self.descriptors[-1]

    def verify(self, *, label: str) -> None:
        current_fd = -1
        try:
            for descriptor, expected in zip(self.descriptors, self.identities):
                if _directory_identity(os.fstat(descriptor)) != expected:
                    raise RuntimePathSecurityError(f"{label} descriptor changed")
            current_fd = os.dup(self.descriptors[0])
            for part, expected in zip(self.parts, self.identities[1:]):
                before = os.stat(part, dir_fd=current_fd, follow_symlinks=False)
                if _directory_identity(before) != expected:
                    raise RuntimePathSecurityError(f"{label} path changed")
                next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=current_fd)
                opened = os.fstat(next_fd)
                if _directory_identity(opened) != expected:
                    os.close(next_fd)
                    raise RuntimePathSecurityError(f"{label} path changed")
                os.close(current_fd)
                current_fd = next_fd
        except RuntimePathSecurityError:
            raise
        except OSError as exc:
            raise RuntimePathSecurityError(f"{label} path changed") from exc
        finally:
            if current_fd >= 0:
                os.close(current_fd)

    def close(self) -> None:
        while self.descriptors:
            os.close(self.descriptors.pop())


@dataclass(slots=True)
class _RelativeDirectoryPin:
    names: tuple[str, ...]
    descriptors: list[int]
    identities: tuple[tuple[int, int, int, int], ...]

    @property
    def descriptor(self) -> int:
        return self.descriptors[-1]

    def verify(self, root_fd: int, *, label: str) -> None:
        parent_fd = root_fd
        for index, (name, descriptor, expected) in enumerate(
            zip(self.names, self.descriptors, self.identities)
        ):
            if _directory_identity(os.fstat(descriptor)) != expected:
                raise RuntimePathSecurityError(f"{label} descriptor changed")
            named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if _directory_identity(named) != expected:
                raise RuntimePathSecurityError(f"{label} path changed")
            parent_fd = self.descriptors[index]

    def close(self) -> None:
        while self.descriptors:
            os.close(self.descriptors.pop())


def _object_identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _directory_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_mode, value.st_uid


def _file_identity(
    value: os.stat_result,
) -> tuple[int, int, int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _canonical_absolute_parts(value: str, *, label: str) -> tuple[str, ...]:
    if value == os.sep:
        return ()
    if not value.startswith("/") or "\x00" in value:
        raise RuntimePathSecurityError(f"{label} must be an absolute canonical path")
    parts = value.split("/")
    if len(parts) < 2 or any(part in {"", ".", ".."} for part in parts[1:]):
        raise RuntimePathSecurityError(f"{label} must be an absolute canonical path")
    return tuple(parts[1:])


def _runtime_relative_parts(runtime_path: str) -> tuple[str, ...] | None:
    if not isinstance(runtime_path, str) or "\x00" in runtime_path:
        raise RuntimePathSecurityError("runtime path is invalid")
    if not runtime_path.startswith("/"):
        raise RuntimePathSecurityError("runtime path must be absolute")
    parts = runtime_path.split("/")
    if any(part in {"", ".", ".."} for part in parts[1:]):
        raise RuntimePathSecurityError("runtime path must be canonical")
    if parts[1:3] != ["openevo", "session"]:
        return None
    return tuple(parts[3:])


def _pin_absolute_directory(path: Path, *, create: bool = False) -> _DirectoryPin:
    parts = _canonical_absolute_parts(str(path), label="directory")
    descriptors = [os.open(os.sep, _DIRECTORY_FLAGS)]
    identities = [_directory_identity(os.fstat(descriptors[0]))]
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptors[-1])
                except FileExistsError:
                    pass
            before = os.stat(part, dir_fd=descriptors[-1], follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode):
                raise RuntimePathSecurityError("directory path contains a non-directory")
            descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptors[-1])
            opened = os.fstat(descriptor)
            if _directory_identity(before) != _directory_identity(opened):
                os.close(descriptor)
                raise RuntimePathSecurityError("directory path changed while it was opened")
            descriptors.append(descriptor)
            identities.append(_directory_identity(opened))
        return _DirectoryPin(
            path=path,
            parts=parts,
            descriptors=descriptors,
            identities=tuple(identities),
        )
    except RuntimePathSecurityError:
        while descriptors:
            os.close(descriptors.pop())
        raise
    except FileNotFoundError:
        while descriptors:
            os.close(descriptors.pop())
        raise
    except OSError as exc:
        while descriptors:
            os.close(descriptors.pop())
        raise RuntimePathSecurityError("directory path could not be opened safely") from exc


def _open_relative_directories(
    root_fd: int,
    parts: tuple[str, ...],
    *,
    create: bool,
    expected_owner: int,
) -> _RelativeDirectoryPin:
    descriptors: list[int] = []
    identities: list[tuple[int, int, int, int]] = []
    parent_fd = root_fd
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
            before = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode) or before.st_uid != expected_owner:
                raise RuntimePathSecurityError("session path ancestor is not an owned directory")
            descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            opened = os.fstat(descriptor)
            if _directory_identity(before) != _directory_identity(opened):
                os.close(descriptor)
                raise RuntimePathSecurityError("session path ancestor changed while it was opened")
            descriptors.append(descriptor)
            identities.append(_directory_identity(opened))
            parent_fd = descriptor
        return _RelativeDirectoryPin(
            names=parts,
            descriptors=descriptors,
            identities=tuple(identities),
        )
    except RuntimePathSecurityError:
        while descriptors:
            os.close(descriptors.pop())
        raise
    except OSError as exc:
        while descriptors:
            os.close(descriptors.pop())
        raise RuntimePathSecurityError("session path ancestor could not be opened safely") from exc


def _require_session_root(
    pin: _DirectoryPin,
    expected: tuple[int, int, int, int],
) -> None:
    opened = os.fstat(pin.descriptor)
    if _directory_identity(opened) != expected:
        raise RuntimePathSecurityError("session root identity changed")
    if not stat.S_ISDIR(opened.st_mode) or opened.st_uid != os.geteuid():
        raise RuntimePathSecurityError("session root is not an owned directory")


def _validate_existing_relative_path(
    root_fd: int,
    parts: tuple[str, ...],
    *,
    expected_owner: int,
) -> None:
    current_fd = os.dup(root_fd)
    try:
        for index, part in enumerate(parts):
            try:
                before = os.stat(part, dir_fd=current_fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            is_last = index == len(parts) - 1
            if stat.S_ISLNK(before.st_mode):
                raise RuntimePathSecurityError("session path contains a symbolic link")
            if before.st_uid != expected_owner:
                raise RuntimePathSecurityError("session path contains an unowned entry")
            if is_last:
                if not (stat.S_ISREG(before.st_mode) or stat.S_ISDIR(before.st_mode)):
                    raise RuntimePathSecurityError("session path ends in a special file")
                return
            if not stat.S_ISDIR(before.st_mode):
                raise RuntimePathSecurityError("session path ancestor is not a directory")
            next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=current_fd)
            opened = os.fstat(next_fd)
            if _directory_identity(before) != _directory_identity(opened):
                os.close(next_fd)
                raise RuntimePathSecurityError("session path changed while it was opened")
            os.close(current_fd)
            current_fd = next_fd
    except RuntimePathSecurityError:
        raise
    except OSError as exc:
        raise RuntimePathSecurityError("session path could not be validated safely") from exc
    finally:
        os.close(current_fd)


def validate_session_bind_path(
    session_dir: Path,
    runtime_path: str,
    *,
    expected_identity: tuple[int, int, int, int] | None = None,
) -> Path | None:
    """Validate a runtime bind path against one pinned host session root."""

    relative_parts = _runtime_relative_parts(runtime_path)
    if relative_parts is None:
        return None
    root_pin = _pin_absolute_directory(session_dir)
    try:
        identity = expected_identity or _directory_identity(os.fstat(root_pin.descriptor))
        _require_session_root(root_pin, identity)
        _validate_existing_relative_path(
            root_pin.descriptor,
            relative_parts,
            expected_owner=identity[3],
        )
        root_pin.verify(label="session root")
        _require_session_root(root_pin, identity)
        return session_dir.joinpath(*relative_parts)
    finally:
        root_pin.close()


def _open_source_path(
    source_path: str,
) -> tuple[_DirectoryPin, str, int, os.stat_result]:
    parts = _canonical_absolute_parts(source_path, label="copy source")
    if not parts:
        raise RuntimePathSecurityError("copy source must name a file or directory")
    parent = Path(os.sep).joinpath(*parts[:-1])
    parent_pin = _pin_absolute_directory(parent)
    descriptor = -1
    name = parts[-1]
    try:
        before = os.stat(name, dir_fd=parent_pin.descriptor, follow_symlinks=False)
        if before.st_uid != os.geteuid():
            raise RuntimePathSecurityError("copy source is not owned by the Core user")
        if stat.S_ISREG(before.st_mode):
            if before.st_nlink != 1 or before.st_size < 0:
                raise RuntimePathSecurityError("copy source must be a single-link regular file")
            flags = _FILE_READ_FLAGS
        elif stat.S_ISDIR(before.st_mode):
            flags = _DIRECTORY_FLAGS
        else:
            raise RuntimePathSecurityError("copy source is a link or special file")
        descriptor = os.open(name, flags, dir_fd=parent_pin.descriptor)
        opened = os.fstat(descriptor)
        expected = (
            _file_identity(before) if stat.S_ISREG(before.st_mode) else _directory_identity(before)
        )
        actual = (
            _file_identity(opened) if stat.S_ISREG(opened.st_mode) else _directory_identity(opened)
        )
        if expected != actual:
            raise RuntimePathSecurityError("copy source changed while it was opened")
        result = descriptor
        descriptor = -1
        return parent_pin, name, result, opened
    except RuntimePathSecurityError:
        parent_pin.close()
        raise
    except FileNotFoundError:
        parent_pin.close()
        raise
    except PermissionError:
        parent_pin.close()
        raise
    except OSError as exc:
        parent_pin.close()
        raise RuntimePathSecurityError("copy source could not be opened safely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _require_named_file_identity(
    parent_fd: int,
    name: str,
    expected: os.stat_result,
    *,
    label: str,
) -> None:
    try:
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise RuntimePathSecurityError(f"{label} path changed") from exc
    expected_identity = (
        _file_identity(expected)
        if stat.S_ISREG(expected.st_mode)
        else _directory_identity(expected)
    )
    named_identity = (
        _file_identity(named) if stat.S_ISREG(named.st_mode) else _directory_identity(named)
    )
    if named_identity != expected_identity:
        raise RuntimePathSecurityError(f"{label} path changed")


def _copy_fd_contents(source_fd: int, target_fd: int, expected_size: int) -> None:
    offset = 0
    while offset < expected_size:
        chunk = os.pread(
            source_fd,
            min(_COPY_CHUNK_BYTES, expected_size - offset),
            offset,
        )
        if not chunk:
            raise RuntimePathSecurityError("copy source ended before its verified size")
        written_offset = 0
        while written_offset < len(chunk):
            written = os.pwrite(
                target_fd,
                chunk[written_offset:],
                offset + written_offset,
            )
            if written <= 0:
                raise RuntimePathSecurityError("copy target write made no progress")
            written_offset += written
        offset += len(chunk)
    if os.pread(source_fd, 1, expected_size):
        raise RuntimePathSecurityError("copy source grew during transfer")


def _open_target_file(
    parent_fd: int,
    name: str,
    *,
    expected_owner: int,
    mode: int,
) -> tuple[int, tuple[int, int]]:
    descriptor = -1
    try:
        try:
            before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            descriptor = os.open(
                name,
                _FILE_WRITE_FLAGS | os.O_CREAT | os.O_EXCL,
                mode,
                dir_fd=parent_fd,
            )
            opened = os.fstat(descriptor)
        else:
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != expected_owner
                or before.st_nlink != 1
            ):
                raise RuntimePathSecurityError(
                    "copy target is not an owned single-link regular file"
                )
            descriptor = os.open(name, _FILE_WRITE_FLAGS, dir_fd=parent_fd)
            opened = os.fstat(descriptor)
            if _file_identity(before) != _file_identity(opened):
                raise RuntimePathSecurityError("copy target changed while it was opened")
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != expected_owner
            or opened.st_nlink != 1
        ):
            raise RuntimePathSecurityError("copy target is not an owned single-link regular file")
        os.fchmod(descriptor, mode)
        identity = _object_identity(os.fstat(descriptor))
        result = descriptor
        descriptor = -1
        return result, identity
    except RuntimePathSecurityError:
        raise
    except PermissionError:
        raise
    except OSError as exc:
        raise RuntimePathSecurityError("copy target could not be opened safely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _copy_regular_file(
    source_fd: int,
    source_opened: os.stat_result,
    target_parent_fd: int,
    target_name: str,
    *,
    expected_owner: int,
) -> None:
    target_mode = stat.S_IMODE(source_opened.st_mode)
    target_fd, target_identity = _open_target_file(
        target_parent_fd,
        target_name,
        expected_owner=expected_owner,
        mode=target_mode,
    )
    try:
        os.ftruncate(target_fd, 0)
        _copy_fd_contents(source_fd, target_fd, source_opened.st_size)
        os.ftruncate(target_fd, source_opened.st_size)
        os.fsync(target_fd)
        source_after = os.fstat(source_fd)
        target_after = os.fstat(target_fd)
        if _file_identity(source_opened) != _file_identity(source_after):
            raise RuntimePathSecurityError("copy source changed during transfer")
        if (
            _object_identity(target_after) != target_identity
            or not stat.S_ISREG(target_after.st_mode)
            or target_after.st_uid != expected_owner
            or target_after.st_nlink != 1
            or target_after.st_size != source_opened.st_size
            or stat.S_IMODE(target_after.st_mode) != target_mode
        ):
            raise RuntimePathSecurityError("copy target changed during transfer")
        _require_named_file_identity(
            target_parent_fd,
            target_name,
            target_after,
            label="copy target",
        )
    finally:
        os.close(target_fd)


def _remove_directory_contents(
    directory_fd: int,
    *,
    expected_owner: int,
    depth: int,
    budget: list[int],
) -> None:
    if depth > _COPY_MAX_DEPTH:
        raise RuntimePathSecurityError("copy target exceeds the directory depth limit")
    names: list[str] = []
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            names.append(entry.name)
            if len(names) > budget[0]:
                raise RuntimePathSecurityError("copy target exceeds the node limit")
    names.sort()
    for name in names:
        budget[0] -= 1
        if budget[0] < 0:
            raise RuntimePathSecurityError("copy target exceeds the node limit")
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if before.st_uid != expected_owner:
            raise RuntimePathSecurityError("copy target contains an unowned entry")
        if stat.S_ISDIR(before.st_mode):
            child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            try:
                opened = os.fstat(child_fd)
                if _directory_identity(before) != _directory_identity(opened):
                    raise RuntimePathSecurityError(
                        "copy target directory changed while it was opened"
                    )
                _remove_directory_contents(
                    child_fd,
                    expected_owner=expected_owner,
                    depth=depth + 1,
                    budget=budget,
                )
                _require_named_file_identity(
                    directory_fd,
                    name,
                    opened,
                    label="copy target directory",
                )
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            _require_named_file_identity(
                directory_fd,
                name,
                before,
                label="copy target entry",
            )
            os.unlink(name, dir_fd=directory_fd)


def _open_or_create_target_directory(
    parent_fd: int,
    name: str,
    *,
    expected_owner: int,
) -> tuple[int, tuple[int, int, int, int]]:
    try:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(before.st_mode) or before.st_uid != expected_owner:
            raise RuntimePathSecurityError("copy target is not an owned directory")
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        opened = os.fstat(descriptor)
        if _directory_identity(before) != _directory_identity(opened):
            os.close(descriptor)
            raise RuntimePathSecurityError("copy target directory changed while it was opened")
        return descriptor, _directory_identity(opened)
    except RuntimePathSecurityError:
        raise
    except PermissionError:
        raise
    except OSError as exc:
        raise RuntimePathSecurityError("copy target directory could not be opened safely") from exc


def _copy_directory_contents(
    source_fd: int,
    target_fd: int,
    *,
    expected_owner: int,
    depth: int,
    budget: list[int],
) -> None:
    if depth > _COPY_MAX_DEPTH:
        raise RuntimePathSecurityError("copy source exceeds the directory depth limit")
    names: list[str] = []
    with os.scandir(source_fd) as entries:
        for entry in entries:
            names.append(entry.name)
            if len(names) > budget[0]:
                raise RuntimePathSecurityError("copy source exceeds the node limit")
    names.sort()
    for name in names:
        budget[0] -= 1
        if budget[0] < 0:
            raise RuntimePathSecurityError("copy source exceeds the node limit")
        before = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        if before.st_uid != os.geteuid():
            raise RuntimePathSecurityError("copy source contains an unowned entry")
        if stat.S_ISREG(before.st_mode):
            if before.st_nlink != 1:
                raise RuntimePathSecurityError("copy source contains a hard-linked file")
            child_fd = os.open(name, _FILE_READ_FLAGS, dir_fd=source_fd)
            try:
                opened = os.fstat(child_fd)
                if _file_identity(before) != _file_identity(opened):
                    raise RuntimePathSecurityError("copy source file changed while it was opened")
                _copy_regular_file(
                    child_fd,
                    opened,
                    target_fd,
                    name,
                    expected_owner=expected_owner,
                )
                _require_named_file_identity(
                    source_fd,
                    name,
                    opened,
                    label="copy source file",
                )
            finally:
                os.close(child_fd)
        elif stat.S_ISDIR(before.st_mode):
            child_source_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=source_fd)
            child_target_fd = -1
            try:
                source_opened = os.fstat(child_source_fd)
                if _directory_identity(before) != _directory_identity(source_opened):
                    raise RuntimePathSecurityError(
                        "copy source directory changed while it was opened"
                    )
                child_target_fd, target_identity = _open_or_create_target_directory(
                    target_fd,
                    name,
                    expected_owner=expected_owner,
                )
                _copy_directory_contents(
                    child_source_fd,
                    child_target_fd,
                    expected_owner=expected_owner,
                    depth=depth + 1,
                    budget=budget,
                )
                if _directory_identity(os.fstat(child_target_fd)) != target_identity:
                    raise RuntimePathSecurityError("copy target directory changed during transfer")
                _require_named_file_identity(
                    target_fd,
                    name,
                    os.fstat(child_target_fd),
                    label="copy target directory",
                )
                _require_named_file_identity(
                    source_fd,
                    name,
                    source_opened,
                    label="copy source directory",
                )
            finally:
                if child_target_fd >= 0:
                    os.close(child_target_fd)
                os.close(child_source_fd)
        else:
            raise RuntimePathSecurityError("copy source contains a link or special file")


def _copy_opened_entry(
    source_fd: int,
    source_opened: os.stat_result,
    target_parent_fd: int,
    target_name: str,
    *,
    expected_owner: int,
) -> None:
    if stat.S_ISREG(source_opened.st_mode):
        _copy_regular_file(
            source_fd,
            source_opened,
            target_parent_fd,
            target_name,
            expected_owner=expected_owner,
        )
        return
    if not stat.S_ISDIR(source_opened.st_mode):
        raise RuntimePathSecurityError("copy source is not a file or directory")
    target_fd, target_identity = _open_or_create_target_directory(
        target_parent_fd,
        target_name,
        expected_owner=expected_owner,
    )
    try:
        budget = [_COPY_MAX_NODES]
        _remove_directory_contents(
            target_fd,
            expected_owner=expected_owner,
            depth=0,
            budget=budget,
        )
        _copy_directory_contents(
            source_fd,
            target_fd,
            expected_owner=expected_owner,
            depth=0,
            budget=budget,
        )
        if _directory_identity(os.fstat(target_fd)) != target_identity:
            raise RuntimePathSecurityError("copy target directory changed during transfer")
        _require_named_file_identity(
            target_parent_fd,
            target_name,
            os.fstat(target_fd),
            label="copy target directory",
        )
    finally:
        os.close(target_fd)


def _readback_directory_identity(
    value: os.stat_result,
) -> tuple[int, int, int, int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _readback_file_identity(
    value: os.stat_result,
) -> tuple[int, int, int, int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _readback_object_identity(
    value: os.stat_result,
) -> tuple[int, int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_gid


@dataclass(slots=True)
class _RuntimeDownloadCredit:
    identity: tuple[int, ...]
    is_directory: bool
    node_available: bool = True
    file_available: bool = False
    byte_available: int = 0


class _RuntimeDownloadAccounting:
    """Bind cumulative download charges to surviving filesystem identities."""

    def __init__(
        self,
        budget: RuntimeReadbackBudget,
        *,
        relative_prefix: str,
    ) -> None:
        self._budget = budget
        self._relative_prefix = relative_prefix
        self._credits: dict[str, _RuntimeDownloadCredit] = {}

    @property
    def max_bytes(self) -> int:
        return self._budget.max_bytes

    def exhaust(self) -> None:
        self._budget.exhaust()

    def _receipt_path(self, relative_path: str) -> str:
        if not relative_path:
            return self._relative_prefix
        if not self._relative_prefix:
            return relative_path
        return f"{self._relative_prefix}/{relative_path}"

    def observe_created(
        self,
        relative_path: str,
        *,
        is_directory: bool,
        observed: os.stat_result | None,
    ) -> None:
        receipt_path = self._receipt_path(relative_path)
        identity = _readback_object_identity(observed) if observed is not None else None
        existing = self._credits.get(receipt_path)
        if (
            identity is not None
            and existing is not None
            and existing.identity == identity
            and existing.is_directory is is_directory
        ):
            return
        self._budget.consume_node()
        if not is_directory:
            self._budget.consume_file()
        if identity is None:
            self._credits.pop(receipt_path, None)
            return
        self._credits[receipt_path] = _RuntimeDownloadCredit(
            identity=identity,
            is_directory=is_directory,
            file_available=not is_directory,
        )

    def forget(self, relative_path: str) -> None:
        receipt_path = self._receipt_path(relative_path)
        prefix = f"{receipt_path}/"
        for path in tuple(self._credits):
            if path == receipt_path or path.startswith(prefix):
                self._credits.pop(path, None)

    def claim_node(
        self,
        relative_path: str,
        observed: os.stat_result,
        *,
        is_directory: bool,
    ) -> bool:
        credit = self._credits.get(relative_path)
        if (
            credit is None
            or not credit.node_available
            or credit.is_directory is not is_directory
            or credit.identity != _readback_object_identity(observed)
        ):
            return False
        credit.node_available = False
        return True

    def claim_file(self, relative_path: str, observed: os.stat_result) -> bool:
        credit = self._credits.get(relative_path)
        if (
            credit is None
            or credit.is_directory
            or not credit.file_available
            or credit.identity != _readback_object_identity(observed)
        ):
            return False
        credit.file_available = False
        return True

    def observe_closed_file(
        self,
        relative_path: str,
        observed: os.stat_result,
    ) -> None:
        receipt_path = self._receipt_path(relative_path)
        credit = self._credits.get(receipt_path)
        identity = _readback_object_identity(observed)
        if credit is None or credit.is_directory or credit.identity != identity:
            self.observe_created(
                relative_path,
                is_directory=False,
                observed=observed,
            )
            credit = self._credits[receipt_path]
        self._budget.consume_bytes(observed.st_size)
        credit.byte_available = observed.st_size

    def claim_bytes(self, relative_path: str, observed: os.stat_result) -> int:
        credit = self._credits.get(relative_path)
        if (
            credit is None
            or credit.is_directory
            or credit.identity != _readback_object_identity(observed)
        ):
            return 0
        available = min(credit.byte_available, observed.st_size)
        credit.byte_available -= available
        return available


@dataclass(frozen=True, slots=True)
class _ReadbackSourceEntry:
    name: str
    relative_path: str
    stat: os.stat_result
    is_directory: bool

    @property
    def identity(self) -> tuple[int, ...]:
        if self.is_directory:
            return _readback_directory_identity(self.stat)
        return _readback_file_identity(self.stat)


@dataclass(frozen=True, slots=True)
class _ReadbackDirectorySnapshot:
    identity: tuple[int, ...]
    entries: tuple[_ReadbackSourceEntry, ...]


@dataclass(frozen=True, slots=True)
class _ReadbackTargetEntry:
    name: str
    is_directory: bool
    identity: tuple[int, ...]
    children: tuple[_ReadbackTargetEntry, ...] = ()


class _ReadbackMutationAuthority:
    """One Linux event generation covering every held source directory."""

    def __init__(self) -> None:
        if not sys.platform.startswith("linux"):
            raise RuntimePathSecurityError(
                "trusted runtime readback mutation authority requires Linux"
            )
        libc = ctypes.CDLL(None, use_errno=True)
        init = libc.inotify_init1
        init.argtypes = (ctypes.c_int,)
        init.restype = ctypes.c_int
        descriptor = init(_IN_NONBLOCK | _IN_CLOEXEC)
        if descriptor < 0:
            error = ctypes.get_errno()
            raise RuntimePathSecurityError(
                "trusted runtime readback mutation authority is unavailable"
            ) from OSError(error, os.strerror(error))
        self._descriptor = descriptor
        self._watches: set[int] = set()
        self._closed = False

    def add(self, directory_fd: int) -> None:
        if self._closed:
            raise RuntimePathSecurityError("runtime readback mutation authority is closed")
        libc = ctypes.CDLL(None, use_errno=True)
        add_watch = libc.inotify_add_watch
        add_watch.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32)
        add_watch.restype = ctypes.c_int
        watch = add_watch(
            self._descriptor,
            os.fsencode(f"/proc/self/fd/{directory_fd}"),
            _IN_MUTATION_MASK,
        )
        if watch < 0:
            error = ctypes.get_errno()
            raise RuntimePathSecurityError(
                "trusted runtime readback mutation authority is unavailable"
            ) from OSError(error, os.strerror(error))
        self._watches.add(watch)

    def require_quiet(self) -> None:
        if self._closed:
            raise RuntimePathSecurityError("runtime readback mutation authority is closed")
        while True:
            try:
                payload = os.read(self._descriptor, 64 * 1024)
            except BlockingIOError:
                return
            except OSError as exc:
                raise RuntimePathSecurityError(
                    "runtime readback mutation authority is unavailable"
                ) from exc
            if not payload:
                raise RuntimePathSecurityError(
                    "runtime readback mutation authority is unavailable"
                )
            offset = 0
            while offset < len(payload):
                if len(payload) - offset < _INOTIFY_EVENT_HEADER.size:
                    raise RuntimePathSecurityError(
                        "runtime readback mutation evidence is malformed"
                    )
                watch, mask, _cookie, name_size = _INOTIFY_EVENT_HEADER.unpack_from(
                    payload,
                    offset,
                )
                offset += _INOTIFY_EVENT_HEADER.size + name_size
                if offset > len(payload):
                    raise RuntimePathSecurityError(
                        "runtime readback mutation evidence is malformed"
                    )
                if watch not in self._watches or mask & (
                    _IN_Q_OVERFLOW | _IN_IGNORED | _IN_MUTATION_MASK
                ):
                    raise RuntimePathSecurityError(
                        "runtime readback source tree changed during transfer"
                    )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        os.close(self._descriptor)


def _check_readback_cancel(cancellation: threading.Event) -> None:
    if cancellation.is_set():
        raise RuntimePathSecurityError("runtime readback was cancelled")


def _enumerate_readback_source_directory(
    directory_fd: int,
    *,
    relative_prefix: str,
    expected_owner: int,
    budget: RuntimeReadbackBudget,
    cancellation: threading.Event,
    consume_files: bool,
    accounting: _RuntimeDownloadAccounting | None = None,
) -> _ReadbackDirectorySnapshot:
    _check_readback_cancel(cancellation)
    before = os.fstat(directory_fd)
    if not stat.S_ISDIR(before.st_mode) or before.st_uid != expected_owner:
        raise RuntimePathSecurityError("runtime readback source is not an owned directory")
    names: list[str] = []
    with os.scandir(directory_fd) as iterator:
        for entry in iterator:
            _check_readback_cancel(cancellation)
            if accounting is None:
                budget.consume_node()
            names.append(entry.name)
    names.sort()
    entries: list[_ReadbackSourceEntry] = []
    for name in names:
        _check_readback_cancel(cancellation)
        try:
            observed = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError:
            if accounting is not None:
                budget.consume_node()
            raise
        if observed.st_uid != expected_owner:
            raise RuntimePathSecurityError("runtime readback source contains an unowned entry")
        relative_path = f"{relative_prefix}/{name}" if relative_prefix else name
        if stat.S_ISDIR(observed.st_mode):
            is_directory = True
        elif stat.S_ISREG(observed.st_mode):
            if observed.st_nlink != 1:
                raise RuntimePathSecurityError(
                    "runtime readback source contains a hard-linked file"
                )
            if consume_files and (
                accounting is None or not accounting.claim_file(relative_path, observed)
            ):
                budget.consume_file()
            is_directory = False
        else:
            raise RuntimePathSecurityError(
                "runtime readback source contains a link or special file"
            )
        if accounting is not None and not accounting.claim_node(
            relative_path,
            observed,
            is_directory=is_directory,
        ):
            budget.consume_node()
        entries.append(
            _ReadbackSourceEntry(
                name=name,
                relative_path=relative_path,
                stat=observed,
                is_directory=is_directory,
            )
        )
    after = os.fstat(directory_fd)
    if _readback_directory_identity(after) != _readback_directory_identity(before):
        raise RuntimePathSecurityError("runtime readback directory changed during enumeration")
    return _ReadbackDirectorySnapshot(
        identity=_readback_directory_identity(before),
        entries=tuple(entries),
    )


def _verify_readback_source_directory(
    directory_fd: int,
    expected: _ReadbackDirectorySnapshot,
    *,
    relative_prefix: str,
    expected_owner: int,
    budget: RuntimeReadbackBudget,
    cancellation: threading.Event,
    accounting: _RuntimeDownloadAccounting | None = None,
) -> None:
    observed = _enumerate_readback_source_directory(
        directory_fd,
        relative_prefix=relative_prefix,
        expected_owner=expected_owner,
        budget=budget,
        cancellation=cancellation,
        consume_files=False,
        accounting=accounting,
    )
    expected_entries = tuple(
        (entry.name, entry.is_directory, entry.identity) for entry in expected.entries
    )
    observed_entries = tuple(
        (entry.name, entry.is_directory, entry.identity) for entry in observed.entries
    )
    if observed.identity != expected.identity or observed_entries != expected_entries:
        raise RuntimePathSecurityError("runtime readback source tree changed during transfer")


def _open_private_readback_file(parent_fd: int, name: str) -> int:
    try:
        return os.open(
            name,
            _FILE_WRITE_FLAGS | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise RuntimePathSecurityError(
            "runtime readback target file could not be created"
        ) from exc


def _copy_readback_regular_file(
    source_parent_fd: int,
    source: _ReadbackSourceEntry,
    target_parent_fd: int,
    *,
    budget: RuntimeReadbackBudget,
    cancellation: threading.Event,
) -> tuple[RuntimeReadbackFile, _ReadbackTargetEntry]:
    budget.require_byte_capacity(source.stat.st_size)
    source_fd = os.open(source.name, _FILE_READ_FLAGS, dir_fd=source_parent_fd)
    target_fd = -1
    try:
        opened = os.fstat(source_fd)
        if _readback_file_identity(opened) != source.identity:
            raise RuntimePathSecurityError("runtime readback source file changed while opening")
        target_fd = _open_private_readback_file(target_parent_fd, source.name)
        digest = hashlib.sha256()
        offset = 0
        while offset < opened.st_size:
            _check_readback_cancel(cancellation)
            chunk = os.pread(
                source_fd,
                min(_COPY_CHUNK_BYTES, opened.st_size - offset),
                offset,
            )
            if not chunk:
                raise RuntimePathSecurityError(
                    "runtime readback source ended before its verified size"
                )
            budget.consume_bytes(len(chunk))
            digest.update(chunk)
            written_offset = 0
            while written_offset < len(chunk):
                _check_readback_cancel(cancellation)
                written = os.pwrite(
                    target_fd,
                    chunk[written_offset:],
                    offset + written_offset,
                )
                if written <= 0:
                    raise RuntimePathSecurityError(
                        "runtime readback target write made no progress"
                    )
                written_offset += written
            offset += len(chunk)
        os.ftruncate(target_fd, opened.st_size)
        os.fsync(target_fd)
        source_after = os.fstat(source_fd)
        named_after = os.stat(source.name, dir_fd=source_parent_fd, follow_symlinks=False)
        if (
            _readback_file_identity(source_after) != source.identity
            or _readback_file_identity(named_after) != source.identity
        ):
            raise RuntimePathSecurityError("runtime readback source file changed during transfer")
        target_after = os.fstat(target_fd)
        named_target = os.stat(source.name, dir_fd=target_parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(target_after.st_mode)
            or target_after.st_uid != os.geteuid()
            or target_after.st_nlink != 1
            or target_after.st_size != opened.st_size
            or stat.S_IMODE(target_after.st_mode) != 0o600
            or _readback_file_identity(named_target) != _readback_file_identity(target_after)
        ):
            raise RuntimePathSecurityError("runtime readback target file changed during transfer")
        return (
            RuntimeReadbackFile(
                relative_path=source.relative_path,
                size_bytes=opened.st_size,
                sha256=digest.hexdigest(),
            ),
            _ReadbackTargetEntry(
                name=source.name,
                is_directory=False,
                identity=_readback_file_identity(target_after),
            ),
        )
    finally:
        if target_fd >= 0:
            os.close(target_fd)
        os.close(source_fd)


def _copy_readback_directory(
    source_fd: int,
    target_fd: int,
    *,
    relative_prefix: str,
    expected_owner: int,
    budget: RuntimeReadbackBudget,
    cancellation: threading.Event,
    mutation_authority: _ReadbackMutationAuthority,
    depth: int,
) -> tuple[list[RuntimeReadbackFile], tuple[_ReadbackTargetEntry, ...]]:
    if depth > _COPY_MAX_DEPTH:
        raise RuntimePathSecurityError("runtime readback exceeds the directory depth limit")
    mutation_authority.add(source_fd)
    source_snapshot = _enumerate_readback_source_directory(
        source_fd,
        relative_prefix=relative_prefix,
        expected_owner=expected_owner,
        budget=budget,
        cancellation=cancellation,
        consume_files=True,
    )
    files: list[RuntimeReadbackFile] = []
    target_entries: list[_ReadbackTargetEntry] = []
    for source in source_snapshot.entries:
        _check_readback_cancel(cancellation)
        if source.is_directory:
            child_source_fd = os.open(source.name, _DIRECTORY_FLAGS, dir_fd=source_fd)
            child_target_fd = -1
            try:
                if _readback_directory_identity(os.fstat(child_source_fd)) != source.identity:
                    raise RuntimePathSecurityError(
                        "runtime readback source directory changed while opening"
                    )
                os.mkdir(source.name, mode=0o700, dir_fd=target_fd)
                child_target_fd = os.open(source.name, _DIRECTORY_FLAGS, dir_fd=target_fd)
                child_files, child_entries = _copy_readback_directory(
                    child_source_fd,
                    child_target_fd,
                    relative_prefix=source.relative_path,
                    expected_owner=expected_owner,
                    budget=budget,
                    cancellation=cancellation,
                    mutation_authority=mutation_authority,
                    depth=depth + 1,
                )
                os.fsync(child_target_fd)
                target_after = os.fstat(child_target_fd)
                named_target = os.stat(
                    source.name,
                    dir_fd=target_fd,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISDIR(target_after.st_mode)
                    or target_after.st_uid != os.geteuid()
                    or stat.S_IMODE(target_after.st_mode) != 0o700
                    or _readback_directory_identity(named_target)
                    != _readback_directory_identity(target_after)
                ):
                    raise RuntimePathSecurityError(
                        "runtime readback target directory changed during transfer"
                    )
                files.extend(child_files)
                target_entries.append(
                    _ReadbackTargetEntry(
                        name=source.name,
                        is_directory=True,
                        identity=_readback_directory_identity(target_after),
                        children=child_entries,
                    )
                )
            finally:
                if child_target_fd >= 0:
                    os.close(child_target_fd)
                os.close(child_source_fd)
        else:
            file, target_entry = _copy_readback_regular_file(
                source_fd,
                source,
                target_fd,
                budget=budget,
                cancellation=cancellation,
            )
            files.append(file)
            target_entries.append(target_entry)
    _verify_readback_source_directory(
        source_fd,
        source_snapshot,
        relative_prefix=relative_prefix,
        expected_owner=expected_owner,
        budget=budget,
        cancellation=cancellation,
    )
    return files, tuple(target_entries)


def _verify_readback_target_directory(
    directory_fd: int,
    expected: tuple[_ReadbackTargetEntry, ...],
    *,
    cancellation: threading.Event,
    depth: int,
) -> None:
    if depth > _COPY_MAX_DEPTH:
        raise RuntimePathSecurityError("runtime readback target exceeds the depth limit")
    _check_readback_cancel(cancellation)
    with os.scandir(directory_fd) as iterator:
        names = sorted(entry.name for entry in iterator)
    if names != [entry.name for entry in expected]:
        raise RuntimePathSecurityError("runtime readback target inventory changed")
    for entry in expected:
        observed = os.stat(entry.name, dir_fd=directory_fd, follow_symlinks=False)
        if entry.is_directory:
            if _readback_directory_identity(observed) != entry.identity:
                raise RuntimePathSecurityError("runtime readback target directory changed")
            child_fd = os.open(entry.name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            try:
                if _readback_directory_identity(os.fstat(child_fd)) != entry.identity:
                    raise RuntimePathSecurityError("runtime readback target directory changed")
                _verify_readback_target_directory(
                    child_fd,
                    entry.children,
                    cancellation=cancellation,
                    depth=depth + 1,
                )
            finally:
                os.close(child_fd)
        elif _readback_file_identity(observed) != entry.identity:
            raise RuntimePathSecurityError("runtime readback target file changed")


def _rename_readback_noreplace(
    source_dir_fd: int,
    source: str,
    target_dir_fd: int,
    target: str,
) -> None:
    _renameat2_noreplace(source_dir_fd, source, target_dir_fd, target)


def _rename_readback_cleanup_noreplace(
    source_dir_fd: int,
    source: str,
    target_dir_fd: int,
    target: str,
) -> None:
    _renameat2_noreplace(source_dir_fd, source, target_dir_fd, target)


def _renameat2_noreplace(
    source_dir_fd: int,
    source: str,
    target_dir_fd: int,
    target: str,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "runtime readback requires atomic no-replace rename")
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    if (
        renameat2(
            source_dir_fd,
            os.fsencode(source),
            target_dir_fd,
            os.fsencode(target),
            _RENAME_NOREPLACE,
        )
        != 0
    ):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _remove_quarantined_readback_contents(
    directory_fd: int,
    *,
    expected_owner: int,
    depth: int,
    budget: list[int],
) -> None:
    if depth > _COPY_MAX_DEPTH:
        raise RuntimePathSecurityError(
            "runtime readback cleanup exceeds the directory depth limit"
        )
    names: list[str] = []
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            names.append(entry.name)
            if len(names) > budget[0]:
                raise RuntimePathSecurityError("runtime readback cleanup exceeds the node limit")
    names.sort()
    for name in names:
        budget[0] -= 1
        if budget[0] < 0:
            raise RuntimePathSecurityError("runtime readback cleanup exceeds the node limit")
        try:
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            raise RuntimePathSecurityError(
                "runtime readback cleanup entry is unavailable"
            ) from exc
        if before.st_uid != expected_owner:
            raise RuntimePathSecurityError("runtime readback cleanup contains an unowned entry")
        if stat.S_ISDIR(before.st_mode):
            is_directory = True
            flags = _DIRECTORY_FLAGS
        elif stat.S_ISREG(before.st_mode) and before.st_nlink == 1:
            is_directory = False
            flags = _FILE_READ_FLAGS
        else:
            raise RuntimePathSecurityError(
                "runtime readback cleanup contains a link or special file"
            )
        try:
            child_fd = os.open(name, flags, dir_fd=directory_fd)
        except OSError as exc:
            raise RuntimePathSecurityError(
                "runtime readback cleanup entry changed while opening"
            ) from exc
        try:
            opened_identity = _readback_object_identity(os.fstat(child_fd))
            if opened_identity != _readback_object_identity(before):
                raise RuntimePathSecurityError(
                    "runtime readback cleanup entry changed while opening"
                )
            quarantine_name = _quarantine_readback_entry(
                directory_fd,
                name,
                child_fd,
                opened_identity,
            )
            if is_directory:
                _remove_quarantined_readback_contents(
                    child_fd,
                    expected_owner=expected_owner,
                    depth=depth + 1,
                    budget=budget,
                )
                os.fsync(child_fd)
            _require_quarantined_readback_binding(
                directory_fd,
                quarantine_name,
                child_fd,
                opened_identity,
            )
            if is_directory:
                os.rmdir(quarantine_name, dir_fd=directory_fd)
            else:
                os.unlink(quarantine_name, dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            os.close(child_fd)


def _discard_readback_staging(
    parent_fd: int,
    staging_name: str,
    staging_fd: int,
    staging_identity: tuple[int, ...],
) -> None:
    quarantine_name = _quarantine_readback_entry(
        parent_fd,
        staging_name,
        staging_fd,
        staging_identity,
    )
    _remove_quarantined_readback_contents(
        staging_fd,
        expected_owner=os.geteuid(),
        depth=0,
        budget=[RUNTIME_READBACK_MAX_NODES],
    )
    os.fsync(staging_fd)
    _require_quarantined_readback_binding(
        parent_fd,
        quarantine_name,
        staging_fd,
        staging_identity,
    )
    os.rmdir(quarantine_name, dir_fd=parent_fd)
    os.fsync(parent_fd)


def _discard_readback_publication(
    parent_fd: int,
    target_name: str,
    publication_fd: int,
    publication_identity: tuple[int, ...],
    *,
    is_directory: bool,
) -> None:
    quarantine_name = _quarantine_readback_entry(
        parent_fd,
        target_name,
        publication_fd,
        publication_identity,
    )
    if is_directory:
        _remove_quarantined_readback_contents(
            publication_fd,
            expected_owner=os.geteuid(),
            depth=0,
            budget=[RUNTIME_READBACK_MAX_NODES],
        )
        os.fsync(publication_fd)
    _require_quarantined_readback_binding(
        parent_fd,
        quarantine_name,
        publication_fd,
        publication_identity,
    )
    if is_directory:
        os.rmdir(quarantine_name, dir_fd=parent_fd)
    else:
        os.unlink(quarantine_name, dir_fd=parent_fd)
    os.fsync(parent_fd)


def _quarantine_readback_entry(
    parent_fd: int,
    name: str,
    held_fd: int,
    expected_identity: tuple[int, ...],
) -> str:
    held = _readback_object_identity(os.fstat(held_fd))
    if held != expected_identity:
        raise RuntimePathSecurityError("runtime readback cleanup descriptor changed")
    try:
        first = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise RuntimePathSecurityError("runtime readback cleanup pathname is unavailable") from exc
    if _readback_object_identity(first) != expected_identity:
        raise RuntimePathSecurityError("runtime readback cleanup pathname changed")

    for _attempt in range(_READBACK_QUARANTINE_ATTEMPTS):
        quarantine_name = f".openevo-readback-quarantine-{secrets.token_hex(24)}"
        try:
            _rename_readback_cleanup_noreplace(
                parent_fd,
                name,
                parent_fd,
                quarantine_name,
            )
        except FileExistsError:
            continue
        break
    else:
        raise RuntimePathSecurityError("runtime readback cleanup quarantine is unavailable")
    os.fsync(parent_fd)
    _require_quarantined_readback_binding(
        parent_fd,
        quarantine_name,
        held_fd,
        expected_identity,
    )
    return quarantine_name


def _require_quarantined_readback_binding(
    parent_fd: int,
    quarantine_name: str,
    held_fd: int,
    expected_identity: tuple[int, ...],
) -> None:
    held = _readback_object_identity(os.fstat(held_fd))
    try:
        named = os.stat(
            quarantine_name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise RuntimePathSecurityError(
            "runtime readback quarantine pathname is unavailable"
        ) from exc
    if held != expected_identity or _readback_object_identity(named) != expected_identity:
        raise RuntimePathSecurityError("runtime readback quarantine identity changed")


class _RuntimeReadbackTemporaryRoot:
    """FD-held private root for one receipt readback attempt."""

    def __init__(
        self,
        *,
        path: Path,
        parent_pin: _DirectoryPin,
        name: str,
        descriptor: int,
        identity: tuple[int, ...],
    ) -> None:
        self.path = path
        self._parent_pin = parent_pin
        self._name = name
        self._descriptor = descriptor
        self._identity = identity
        self._quarantine_name: str | None = None
        self._worker_owned = False
        self._cleaning = False
        self._closed = False
        self._cleanup_failure: BaseException | None = None
        self._lock = threading.Lock()

    @property
    def descriptor(self) -> int:
        return self._descriptor

    def quarantine_for_worker(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimePathSecurityError("runtime readback temporary root is already closed")
            if self._quarantine_name is None:
                self._quarantine_name = _quarantine_readback_entry(
                    self._parent_pin.descriptor,
                    self._name,
                    self._descriptor,
                    self._identity,
                )
            self._worker_owned = True

    def release_worker(self) -> None:
        with self._lock:
            self._worker_owned = False

    def cleanup(self) -> bool:
        with self._lock:
            if self._closed:
                if self._cleanup_failure is not None:
                    raise RuntimePathSecurityError(
                        "runtime readback temporary root cleanup previously failed"
                    ) from self._cleanup_failure
                return True
            if self._worker_owned or self._cleaning:
                return False
            self._cleaning = True
        try:
            quarantine_name = self._quarantine_name
            if quarantine_name is None:
                quarantine_name = _quarantine_readback_entry(
                    self._parent_pin.descriptor,
                    self._name,
                    self._descriptor,
                    self._identity,
                )
                self._quarantine_name = quarantine_name
            _remove_quarantined_readback_contents(
                self._descriptor,
                expected_owner=os.geteuid(),
                depth=0,
                budget=[RUNTIME_READBACK_MAX_NODES],
            )
            os.fsync(self._descriptor)
            _require_quarantined_readback_binding(
                self._parent_pin.descriptor,
                quarantine_name,
                self._descriptor,
                self._identity,
            )
            os.rmdir(quarantine_name, dir_fd=self._parent_pin.descriptor)
            os.fsync(self._parent_pin.descriptor)
            return True
        except BaseException as exc:
            with self._lock:
                self._cleanup_failure = exc
            raise
        finally:
            with self._lock:
                self._closed = True
                self._cleaning = False
            os.close(self._descriptor)
            self._parent_pin.close()


def _create_runtime_readback_temporary_root(
    *,
    prefix: str = "openevo-evolution-readback-",
    parent: Path | None = None,
) -> _RuntimeReadbackTemporaryRoot:
    if not prefix or "/" in prefix or "\x00" in prefix or prefix in {".", ".."}:
        raise ValueError("runtime readback temporary prefix is invalid")
    parent_path = (parent or Path(tempfile.gettempdir())).absolute()
    parent_pin = _pin_absolute_directory(parent_path)
    name: str | None = None
    descriptor = -1
    identity: tuple[int, ...] | None = None
    try:
        for _attempt in range(_READBACK_QUARANTINE_ATTEMPTS):
            candidate = f"{prefix}{secrets.token_hex(24)}"
            try:
                os.mkdir(candidate, mode=0o700, dir_fd=parent_pin.descriptor)
            except FileExistsError:
                continue
            name = candidate
            break
        if name is None:
            raise RuntimePathSecurityError("runtime readback temporary root is unavailable")
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_pin.descriptor)
        opened = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_pin.descriptor, follow_symlinks=False)
        identity = _readback_object_identity(opened)
        if (
            identity != _readback_object_identity(named)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != 0o700
        ):
            raise RuntimePathSecurityError("runtime readback temporary root is not private")
        os.fsync(parent_pin.descriptor)
        return _RuntimeReadbackTemporaryRoot(
            path=parent_path / name,
            parent_pin=parent_pin,
            name=name,
            descriptor=descriptor,
            identity=identity,
        )
    except BaseException:
        if descriptor >= 0 and name is not None and identity is not None:
            try:
                _discard_readback_staging(
                    parent_pin.descriptor,
                    name,
                    descriptor,
                    identity,
                )
            except BaseException:
                pass
        if descriptor >= 0:
            os.close(descriptor)
        parent_pin.close()
        raise


_RUNTIME_READBACK_CLEANUP_TASKS: set[asyncio.Task[object]] = set()
_PERMANENT_RUNTIME_READBACK_ISOLATIONS: list[_RuntimeReadbackTemporaryRoot] = []


def _schedule_runtime_readback_root_cleanup(
    root: _RuntimeReadbackTemporaryRoot,
) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(asyncio.to_thread(root.cleanup))
    _RUNTIME_READBACK_CLEANUP_TASKS.add(task)

    def consume(completed: asyncio.Task[object]) -> None:
        _RUNTIME_READBACK_CLEANUP_TASKS.discard(completed)
        if not completed.cancelled():
            completed.exception()

    task.add_done_callback(consume)


async def _cleanup_runtime_readback_temporary_root(
    root: _RuntimeReadbackTemporaryRoot,
) -> bool:
    cleanup = asyncio.create_task(asyncio.to_thread(root.cleanup))
    cleanup_cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cleanup_cancelled = True
    cleaned = cleanup.result()
    if cleanup_cancelled:
        raise asyncio.CancelledError
    return cleaned


def _source_ancestor_identities(
    session_fd: int,
    parents: _RelativeDirectoryPin,
) -> tuple[tuple[int, ...], ...]:
    return tuple(
        _readback_directory_identity(os.fstat(descriptor))
        for descriptor in (session_fd, *parents.descriptors)
    )


def _require_source_ancestor_identities(
    session_fd: int,
    parents: _RelativeDirectoryPin,
    expected: tuple[tuple[int, ...], ...],
) -> None:
    observed = _source_ancestor_identities(session_fd, parents)
    if observed != expected:
        raise RuntimePathSecurityError("runtime readback source ancestor changed")


class BaseRuntime(ABC):
    """Base class for long-lived per-session execution runtimes."""

    def __init__(self, spec: RuntimeSpec, session_id: str, session_dir: Path) -> None:
        self.spec = spec
        self.session_id = session_id
        self.session_dir = session_dir.absolute()
        session_pin = _pin_absolute_directory(self.session_dir, create=True)
        try:
            self._session_root_identity = _directory_identity(os.fstat(session_pin.descriptor))
            _require_session_root(session_pin, self._session_root_identity)
            session_pin.verify(label="session root")
        finally:
            session_pin.close()
        self.artifacts_dir = self.session_dir / "artifacts"
        self.runtime_session_dir = RUNTIME_SESSION_DIR
        self.runtime_artifacts_dir = RUNTIME_ARTIFACTS_DIR
        self.runtime_logs_dir = RUNTIME_LOGS_DIR
        self.runtime_agent_log_dir = RUNTIME_AGENT_LOG_DIR
        self._active_process: asyncio.subprocess.Process | None = None
        self._destroyed = False

    @property
    @abstractmethod
    def runtime_id(self) -> str:
        """Identifier for the live runtime instance."""

    @property
    def supports_gpus(self) -> bool:
        return False

    @property
    def can_disable_internet(self) -> bool:
        return False

    @property
    def supports_cpu_limits(self) -> bool:
        return False

    @property
    def supports_memory_limits(self) -> bool:
        return False

    @property
    def supports_storage_limits(self) -> bool:
        return False

    @abstractmethod
    async def start(self) -> None:
        """Create and start the runtime instance."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop and remove the runtime instance."""

    async def cancel(self) -> None:
        """Stop any in-flight command and tear the runtime down."""
        process = self._active_process
        if process is not None and process.returncode is None:
            await self._kill_and_reap(process)
        await self.stop()

    @abstractmethod
    async def exec(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: float | None = None,
    ) -> ExecResult:
        """Execute one command inside the runtime and return captured output."""

    @abstractmethod
    async def upload_file(self, local_path: str, remote_path: str) -> None:
        """Copy a single file from the host into the runtime."""

    @abstractmethod
    async def upload_dir(self, local_path: str, remote_path: str) -> None:
        """Copy a directory tree from the host into the runtime."""

    @abstractmethod
    async def download_file(self, remote_path: str, local_path: str) -> None:
        """Copy a single file from inside the runtime to the host."""

    @abstractmethod
    async def download_dir(self, remote_path: str, local_path: str) -> None:
        """Copy a directory tree from inside the runtime to the host."""

    def _start_download_dir_operation(
        self, remote_path: str, local_path: str
    ) -> RuntimeDownloadOperation | None:
        """Return real termination authority when the backend can provide it."""

        del remote_path, local_path
        return None

    def resolve_host_path(self, runtime_path: str) -> Path | None:
        """Map a runtime path back to a host path via the session bind mount."""
        return validate_session_bind_path(
            self.session_dir,
            runtime_path,
            expected_identity=self._session_root_identity,
        )

    async def _trusted_runtime_readback(
        self,
        runtime_path: str,
        local_path: Path,
        *,
        budget: RuntimeReadbackBudget,
        expected_directory: bool,
    ) -> RuntimeReadback:
        if not isinstance(budget, RuntimeReadbackBudget):
            raise TypeError("runtime readback requires the closed budget authority")
        cancellation = threading.Event()
        worker = asyncio.create_task(
            asyncio.to_thread(
                BaseRuntime._trusted_runtime_readback_sync,
                self,
                runtime_path,
                local_path,
                budget,
                expected_directory,
                cancellation,
            )
        )
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancellation.set()
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    cancellation.set()
                except BaseException:
                    break
            if worker.done() and not worker.cancelled():
                try:
                    worker.result()
                except BaseException:
                    pass
            raise

    def _trusted_runtime_readback_sync(
        self,
        runtime_path: str,
        local_path: Path,
        budget: RuntimeReadbackBudget,
        expected_directory: bool,
        cancellation: threading.Event,
    ) -> RuntimeReadback:
        relative_parts = _runtime_relative_parts(runtime_path)
        if relative_parts is None:
            raise RuntimePathSecurityError(
                "trusted runtime readback requires the session bind mount"
            )
        if not relative_parts:
            raise RuntimePathSecurityError("runtime readback source must not be the session root")

        session_pin = _pin_absolute_directory(self.session_dir)
        source_parents: _RelativeDirectoryPin | None = None
        source_fd = -1
        target_parent_pin: _DirectoryPin | None = None
        staging_fd = -1
        payload_fd = -1
        staging_name: str | None = None
        staging_identity: tuple[int, ...] | None = None
        publication_identity: tuple[int, ...] | None = None
        publication_active = False
        readback_complete = False
        mutation_authority: _ReadbackMutationAuthority | None = None
        try:
            _check_readback_cancel(cancellation)
            _require_session_root(session_pin, self._session_root_identity)
            source_parents = _open_relative_directories(
                session_pin.descriptor,
                relative_parts[:-1],
                create=False,
                expected_owner=self._session_root_identity[3],
            )
            source_parent_fd = (
                source_parents.descriptor if source_parents.descriptors else session_pin.descriptor
            )
            source_name = relative_parts[-1]
            source_before = os.stat(
                source_name,
                dir_fd=source_parent_fd,
                follow_symlinks=False,
            )
            if source_before.st_uid != self._session_root_identity[3]:
                raise RuntimePathSecurityError("runtime readback source is not owned by Core")
            if stat.S_ISDIR(source_before.st_mode):
                if not expected_directory:
                    raise RuntimePathSecurityError("runtime readback expected a regular file")
                source_flags = _DIRECTORY_FLAGS
                source_identity = _readback_directory_identity(source_before)
            elif stat.S_ISREG(source_before.st_mode):
                if expected_directory:
                    raise RuntimePathSecurityError("runtime readback expected a directory")
                if source_before.st_nlink != 1:
                    raise RuntimePathSecurityError("runtime readback source is a hard-linked file")
                source_flags = _FILE_READ_FLAGS
                source_identity = _readback_file_identity(source_before)
                budget.consume_node()
                budget.consume_file()
            else:
                raise RuntimePathSecurityError("runtime readback source is a link or special file")
            source_fd = os.open(source_name, source_flags, dir_fd=source_parent_fd)
            source_opened = os.fstat(source_fd)
            opened_identity = (
                _readback_directory_identity(source_opened)
                if expected_directory
                else _readback_file_identity(source_opened)
            )
            if opened_identity != source_identity:
                raise RuntimePathSecurityError("runtime readback source changed while opening")
            mutation_authority = _ReadbackMutationAuthority()
            mutation_authority.add(session_pin.descriptor)
            for descriptor in source_parents.descriptors:
                mutation_authority.add(descriptor)
            if expected_directory:
                mutation_authority.add(source_fd)
            ancestor_identities = _source_ancestor_identities(
                session_pin.descriptor,
                source_parents,
            )

            target_parts = _canonical_absolute_parts(
                str(local_path.absolute()),
                label="runtime readback target",
            )
            if not target_parts:
                raise RuntimePathSecurityError(
                    "runtime readback target must not be the filesystem root"
                )
            target_parent = Path(os.sep).joinpath(*target_parts[:-1])
            target_name = target_parts[-1]
            target_parent_pin = _pin_absolute_directory(target_parent, create=True)
            try:
                os.stat(target_name, dir_fd=target_parent_pin.descriptor, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise RuntimePathSecurityError("runtime readback target already exists")

            for _attempt in range(16):
                candidate = f".{target_name}.openevo-readback-{secrets.token_hex(12)}"
                try:
                    os.mkdir(candidate, mode=0o700, dir_fd=target_parent_pin.descriptor)
                except FileExistsError:
                    continue
                staging_name = candidate
                break
            if staging_name is None:
                raise RuntimePathSecurityError("runtime readback staging name is unavailable")
            staging_fd = os.open(
                staging_name,
                _DIRECTORY_FLAGS,
                dir_fd=target_parent_pin.descriptor,
            )
            staging_opened = os.fstat(staging_fd)
            if (
                not stat.S_ISDIR(staging_opened.st_mode)
                or staging_opened.st_uid != os.geteuid()
                or stat.S_IMODE(staging_opened.st_mode) != 0o700
            ):
                raise RuntimePathSecurityError("runtime readback staging is not private")
            staging_identity = _readback_object_identity(staging_opened)
            target_parent_after_staging = _readback_directory_identity(
                os.fstat(target_parent_pin.descriptor)
            )

            if expected_directory:
                payload_name = "payload"
                os.mkdir(payload_name, mode=0o700, dir_fd=staging_fd)
                payload_fd = os.open(payload_name, _DIRECTORY_FLAGS, dir_fd=staging_fd)
                files, target_entries = _copy_readback_directory(
                    source_fd,
                    payload_fd,
                    relative_prefix="",
                    expected_owner=self._session_root_identity[3],
                    budget=budget,
                    cancellation=cancellation,
                    mutation_authority=mutation_authority,
                    depth=0,
                )
                os.fsync(payload_fd)
                _verify_readback_target_directory(
                    payload_fd,
                    target_entries,
                    cancellation=cancellation,
                    depth=0,
                )
            else:
                payload_name = source_name
                source_entry = _ReadbackSourceEntry(
                    name=source_name,
                    relative_path=source_name,
                    stat=source_before,
                    is_directory=False,
                )
                file, target_entry = _copy_readback_regular_file(
                    source_parent_fd,
                    source_entry,
                    staging_fd,
                    budget=budget,
                    cancellation=cancellation,
                )
                files = [file]
                payload_fd = os.open(payload_name, _FILE_READ_FLAGS, dir_fd=staging_fd)
                if _readback_file_identity(os.fstat(payload_fd)) != target_entry.identity:
                    raise RuntimePathSecurityError("runtime readback target file changed")

            _check_readback_cancel(cancellation)
            named_source_after = os.stat(
                source_name,
                dir_fd=source_parent_fd,
                follow_symlinks=False,
            )
            final_source_identity = (
                _readback_directory_identity(named_source_after)
                if expected_directory
                else _readback_file_identity(named_source_after)
            )
            if final_source_identity != source_identity:
                raise RuntimePathSecurityError("runtime readback source path changed")
            _require_source_ancestor_identities(
                session_pin.descriptor,
                source_parents,
                ancestor_identities,
            )
            source_parents.verify(session_pin.descriptor, label="runtime readback source")
            session_pin.verify(label="session root")
            _require_session_root(session_pin, self._session_root_identity)
            mutation_authority.require_quiet()

            try:
                os.stat(target_name, dir_fd=target_parent_pin.descriptor, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise RuntimePathSecurityError("runtime readback target was replaced")
            if (
                _readback_directory_identity(os.fstat(target_parent_pin.descriptor))
                != target_parent_after_staging
            ):
                raise RuntimePathSecurityError("runtime readback target parent changed")
            target_parent_pin.verify(label="runtime readback target parent")
            os.fsync(staging_fd)
            _check_readback_cancel(cancellation)
            payload_identity = _readback_object_identity(os.fstat(payload_fd))
            _rename_readback_noreplace(
                staging_fd,
                payload_name,
                target_parent_pin.descriptor,
                target_name,
            )
            publication_active = True
            publication_identity = payload_identity
            named_target = os.stat(
                target_name,
                dir_fd=target_parent_pin.descriptor,
                follow_symlinks=False,
            )
            if _readback_object_identity(named_target) != payload_identity:
                raise RuntimePathSecurityError("runtime readback target was replaced")
            if expected_directory:
                _verify_readback_target_directory(
                    payload_fd,
                    target_entries,
                    cancellation=cancellation,
                    depth=0,
                )
            elif (
                not stat.S_ISREG(named_target.st_mode)
                or named_target.st_nlink != 1
                or named_target.st_size != files[0].size_bytes
            ):
                raise RuntimePathSecurityError("runtime readback target file changed")
            target_parent_pin.verify(label="runtime readback target parent")
            readback_complete = True
            return RuntimeReadback(files=tuple(sorted(files, key=lambda item: item.relative_path)))
        except FileNotFoundError as exc:
            raise FileNotFoundError(
                f"runtime readback source is unavailable: {runtime_path}"
            ) from exc
        except RuntimePathSecurityError:
            raise
        except OSError as exc:
            raise RuntimePathSecurityError("trusted runtime readback failed closed") from exc
        finally:
            cleanup_error: BaseException | None = None
            if mutation_authority is not None:
                mutation_authority.close()
            if (
                publication_active
                and not readback_complete
                and publication_identity is not None
                and payload_fd >= 0
                and target_parent_pin is not None
            ):
                try:
                    _discard_readback_publication(
                        target_parent_pin.descriptor,
                        target_name,
                        payload_fd,
                        publication_identity,
                        is_directory=expected_directory,
                    )
                except BaseException as exc:
                    cleanup_error = exc
            if payload_fd >= 0:
                os.close(payload_fd)
            if staging_fd >= 0 and staging_name is not None and staging_identity is not None:
                try:
                    _discard_readback_staging(
                        target_parent_pin.descriptor if target_parent_pin is not None else -1,
                        staging_name,
                        staging_fd,
                        staging_identity,
                    )
                except BaseException as exc:
                    if cleanup_error is None:
                        cleanup_error = exc
                os.close(staging_fd)
            if target_parent_pin is not None:
                target_parent_pin.close()
            if source_fd >= 0:
                os.close(source_fd)
            if source_parents is not None:
                source_parents.close()
            session_pin.close()
            if cleanup_error is not None:
                raise RuntimePathSecurityError(
                    "runtime readback cleanup could not preserve its authority"
                ) from cleanup_error

    def _copy_from_bind_mount(self, runtime_path: str, local_path: Path) -> bool:
        relative_parts = _runtime_relative_parts(runtime_path)
        if relative_parts is None:
            return False
        if not relative_parts:
            raise RuntimePathSecurityError("copy source must not be the session root")

        session_pin = _pin_absolute_directory(self.session_dir)
        source_parents: _RelativeDirectoryPin | None = None
        source_fd = -1
        target_parent_pin: _DirectoryPin | None = None
        try:
            _require_session_root(session_pin, self._session_root_identity)
            source_parents = _open_relative_directories(
                session_pin.descriptor,
                relative_parts[:-1],
                create=False,
                expected_owner=self._session_root_identity[3],
            )
            source_parent_fd = (
                source_parents.descriptor if source_parents.descriptors else session_pin.descriptor
            )
            source_name = relative_parts[-1]
            before = os.stat(
                source_name,
                dir_fd=source_parent_fd,
                follow_symlinks=False,
            )
            if before.st_uid != self._session_root_identity[3]:
                raise RuntimePathSecurityError("copy source is not owned by Core")
            if stat.S_ISREG(before.st_mode):
                if before.st_nlink != 1:
                    raise RuntimePathSecurityError(
                        "copy source must be a single-link regular file"
                    )
                source_flags = _FILE_READ_FLAGS
            elif stat.S_ISDIR(before.st_mode):
                source_flags = _DIRECTORY_FLAGS
            else:
                raise RuntimePathSecurityError("copy source is a link or special file")
            source_fd = os.open(source_name, source_flags, dir_fd=source_parent_fd)
            source_opened = os.fstat(source_fd)
            expected_source = (
                _file_identity(before)
                if stat.S_ISREG(before.st_mode)
                else _directory_identity(before)
            )
            opened_source = (
                _file_identity(source_opened)
                if stat.S_ISREG(source_opened.st_mode)
                else _directory_identity(source_opened)
            )
            if expected_source != opened_source:
                raise RuntimePathSecurityError("copy source changed while it was opened")

            local_parts = _canonical_absolute_parts(
                str(local_path.absolute()),
                label="copy target",
            )
            if not local_parts:
                raise RuntimePathSecurityError("copy target must not be the filesystem root")
            target_parent = Path(os.sep).joinpath(*local_parts[:-1])
            target_parent_pin = _pin_absolute_directory(target_parent, create=True)
            _copy_opened_entry(
                source_fd,
                source_opened,
                target_parent_pin.descriptor,
                local_parts[-1],
                expected_owner=os.geteuid(),
            )

            _require_named_file_identity(
                source_parent_fd,
                source_name,
                source_opened,
                label="copy source",
            )
            source_parents.verify(session_pin.descriptor, label="copy source ancestor")
            session_pin.verify(label="session root")
            _require_session_root(session_pin, self._session_root_identity)
            target_parent_pin.verify(label="copy target ancestor")
            return True
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"bind source does not exist: {runtime_path}") from exc
        finally:
            if target_parent_pin is not None:
                target_parent_pin.close()
            if source_fd >= 0:
                os.close(source_fd)
            if source_parents is not None:
                source_parents.close()
            session_pin.close()

    def _copy_to_bind_mount(self, local_path: str, runtime_path: str) -> bool:
        relative_parts = _runtime_relative_parts(runtime_path)
        if relative_parts is None:
            return False
        if not relative_parts:
            raise RuntimePathSecurityError("copy target must not be the session root")

        source_parent, source_name, source_fd, source_opened = _open_source_path(
            str(Path(local_path).absolute())
        )
        session_pin = _pin_absolute_directory(self.session_dir)
        target_parents: _RelativeDirectoryPin | None = None
        try:
            _require_session_root(session_pin, self._session_root_identity)
            target_parents = _open_relative_directories(
                session_pin.descriptor,
                relative_parts[:-1],
                create=True,
                expected_owner=self._session_root_identity[3],
            )
            target_parent_fd = (
                target_parents.descriptor if target_parents.descriptors else session_pin.descriptor
            )
            _copy_opened_entry(
                source_fd,
                source_opened,
                target_parent_fd,
                relative_parts[-1],
                expected_owner=self._session_root_identity[3],
            )
            _require_named_file_identity(
                source_parent.descriptor,
                source_name,
                source_opened,
                label="copy source",
            )
            source_parent.verify(label="copy source ancestor")
            target_parents.verify(session_pin.descriptor, label="copy target ancestor")
            session_pin.verify(label="session root")
            _require_session_root(session_pin, self._session_root_identity)
            return True
        finally:
            if target_parents is not None:
                target_parents.close()
            session_pin.close()
            os.close(source_fd)
            source_parent.close()

    async def _run_local_command(
        self,
        *args: str,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
        inherit_env: bool = True,
        capture: bool = False,
    ) -> tuple[int, str | None, str | None]:
        """Run a local subprocess, optionally capturing stdout/stderr."""
        process_env = (
            None if env is None else ({**os.environ, **env} if inherit_env else dict(env))
        )
        if capture:
            stdout_target = asyncio.subprocess.PIPE
            stderr_target = asyncio.subprocess.PIPE
        else:
            stdout_target = asyncio.subprocess.DEVNULL
            stderr_target = asyncio.subprocess.DEVNULL

        process = await asyncio.create_subprocess_exec(
            *args,
            env=process_env,
            stdout=stdout_target,
            stderr=stderr_target,
        )
        self._active_process = process
        stdout_buffer = bytearray()
        stderr_buffer = bytearray()
        drain_tasks: list[asyncio.Task[None]] = []
        if capture:
            assert process.stdout is not None
            assert process.stderr is not None
            drain_tasks = [
                asyncio.create_task(self._drain_bounded_stream(process.stdout, stdout_buffer)),
                asyncio.create_task(self._drain_bounded_stream(process.stderr, stderr_buffer)),
            ]
        try:
            try:
                if timeout is None:
                    await process.wait()
                else:
                    await asyncio.wait_for(process.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                await self._kill_and_reap(process)
                await self._finish_stream_drains(drain_tasks)
                return (
                    -1,
                    self._decode_capture(stdout_buffer),
                    self._decode_capture(stderr_buffer),
                )
            except asyncio.CancelledError:
                if process.returncode is None:
                    await self._kill_and_reap(process)
                await self._finish_stream_drains(drain_tasks)
                raise
            await self._finish_stream_drains(drain_tasks)
        finally:
            if self._active_process is process:
                self._active_process = None

        rc = process.returncode or 0
        return (
            rc,
            self._decode_capture(stdout_buffer),
            self._decode_capture(stderr_buffer),
        )

    @staticmethod
    async def _drain_bounded_stream(
        stream: asyncio.StreamReader,
        destination: bytearray,
    ) -> None:
        while True:
            chunk = await stream.read(_COPY_CHUNK_BYTES)
            if not chunk:
                return
            remaining = LOCAL_COMMAND_CAPTURE_MAX_BYTES - len(destination)
            if remaining > 0:
                destination.extend(chunk[:remaining])

    @staticmethod
    async def _finish_stream_drains(tasks: list[asyncio.Task[None]]) -> None:
        if not tasks:
            return
        try:
            async with asyncio.timeout(_LOCAL_COMMAND_FINALIZE_SECONDS):
                await asyncio.gather(*tasks)
        except TimeoutError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _decode_capture(value: bytearray) -> str | None:
        if not value:
            return None
        decoded = bytes(value).decode(errors="replace")
        encoded = decoded.encode("utf-8")
        if len(encoded) <= LOCAL_COMMAND_CAPTURE_MAX_BYTES:
            return decoded
        return encoded[:LOCAL_COMMAND_CAPTURE_MAX_BYTES].decode(
            "utf-8",
            errors="ignore",
        )

    @staticmethod
    async def _kill_and_reap(process: asyncio.subprocess.Process) -> None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        try:
            await process.wait()
        except ProcessLookupError:
            pass


def _has_sealed_session_bind_readback(runtime: object) -> bool:
    """Return whether Core can independently inspect this runtime's session bind."""

    return (
        sys.platform.startswith("linux")
        and isinstance(runtime, BaseRuntime)
        and runtime.runtime_session_dir == RUNTIME_SESSION_DIR
        and runtime.spec.import_path is None
    )


async def _sealed_session_bind_readback(
    runtime: BaseRuntime,
    runtime_path: str,
    local_path: Path,
    *,
    budget: RuntimeReadbackBudget,
    expected_directory: bool,
) -> RuntimeReadback:
    """Read a Core-owned session bind without consulting backend download hooks."""

    if not _has_sealed_session_bind_readback(runtime):
        raise RuntimePathSecurityError("sealed session-bind readback is unavailable")
    return await BaseRuntime._trusted_runtime_readback(
        runtime,
        runtime_path,
        local_path,
        budget=budget,
        expected_directory=expected_directory,
    )


class _RuntimeDownloadQuotaExceeded(RuntimePathSecurityError):
    pass


class RuntimeDownloadOperation:
    """Backend-owned proof that cancellation joins the real download worker."""

    def __init__(self, awaitable: Coroutine[Any, Any, None]) -> None:
        self.task = asyncio.create_task(awaitable)

    async def terminate(self) -> None:
        self.task.cancel()
        while not self.task.done():
            try:
                await asyncio.shield(self.task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if not self.task.cancelled():
            try:
                self.task.result()
            except BaseException:
                pass


@dataclass(slots=True)
class _RuntimeDownloadWatch:
    descriptor: int
    relative_path: str | None


def _read_runtime_download_events(descriptor: int) -> bytes:
    try:
        return os.read(descriptor, 64 * 1024)
    except BlockingIOError:
        return b""


class _RuntimeDownloadEventMonitor:
    """Cumulative Linux mutation accounting for a public download root."""

    def __init__(
        self,
        parent_fd: int,
        target_name: str,
        *,
        expected_owner: int,
        accounting: _RuntimeDownloadAccounting,
    ) -> None:
        libc = ctypes.CDLL(None, use_errno=True)
        init = getattr(libc, "inotify_init1", None)
        add_watch = getattr(libc, "inotify_add_watch", None)
        if init is None or add_watch is None or not Path("/proc/self/fd").is_dir():
            raise RuntimePathSecurityError(
                "runtime compatibility download event authority is unavailable"
            )
        init.argtypes = (ctypes.c_int,)
        init.restype = ctypes.c_int
        descriptor = init(_IN_NONBLOCK | _IN_CLOEXEC)
        if descriptor < 0:
            error = ctypes.get_errno()
            raise RuntimePathSecurityError(
                "runtime compatibility download event authority is unavailable"
            ) from OSError(error, os.strerror(error))
        self._descriptor = descriptor
        self._add_watch_call = add_watch
        self._add_watch_call.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint32,
        )
        self._add_watch_call.restype = ctypes.c_int
        self._target_name = target_name
        self._expected_owner = expected_owner
        self._accounting = accounting
        self._watches: dict[int, _RuntimeDownloadWatch] = {}
        self._watched_identities: dict[tuple[int, int], int] = {}
        self._target_watch: int | None = None
        self._closed = False
        try:
            self._add_watch(parent_fd, relative_path=None)
            self._attach_target(parent_fd)
        except BaseException:
            self.close()
            raise

    def _add_watch(self, directory_fd: int, *, relative_path: str | None) -> int:
        opened = os.fstat(directory_fd)
        identity = (opened.st_dev, opened.st_ino)
        existing = self._watched_identities.get(identity)
        if existing is not None:
            return existing
        held_fd = os.dup(directory_fd)
        watch = self._add_watch_call(
            self._descriptor,
            os.fsencode(f"/proc/self/fd/{held_fd}"),
            _IN_MUTATION_MASK,
        )
        if watch < 0:
            error = ctypes.get_errno()
            os.close(held_fd)
            raise RuntimePathSecurityError(
                "runtime compatibility download event authority is unavailable"
            ) from OSError(error, os.strerror(error))
        self._watches[watch] = _RuntimeDownloadWatch(
            descriptor=held_fd,
            relative_path=relative_path,
        )
        self._watched_identities[identity] = watch
        return watch

    @staticmethod
    def _join_relative(parent: str, name: str) -> str:
        return f"{parent}/{name}" if parent else name

    def _observe_existing_directory(
        self,
        directory_fd: int,
        *,
        relative_path: str,
        depth: int,
    ) -> None:
        if depth > _COPY_MAX_DEPTH:
            raise _RuntimeDownloadQuotaExceeded(
                "runtime compatibility download exceeds the depth quota"
            )
        self._add_watch(directory_fd, relative_path=relative_path)
        with os.scandir(directory_fd) as entries:
            names = sorted(entry.name for entry in entries)
        for name in names:
            observed = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if observed.st_uid != self._expected_owner:
                raise _RuntimeDownloadQuotaExceeded(
                    "runtime compatibility download contains an unowned entry"
                )
            child_path = self._join_relative(relative_path, name)
            if stat.S_ISDIR(observed.st_mode):
                self._accounting.observe_created(
                    child_path,
                    is_directory=True,
                    observed=observed,
                )
                child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
                try:
                    if _readback_object_identity(os.fstat(child_fd)) != (
                        _readback_object_identity(observed)
                    ):
                        raise RuntimePathSecurityError(
                            "runtime compatibility download directory changed while opening"
                        )
                    self._observe_existing_directory(
                        child_fd,
                        relative_path=child_path,
                        depth=depth + 1,
                    )
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(observed.st_mode) and observed.st_nlink == 1:
                self._accounting.observe_created(
                    child_path,
                    is_directory=False,
                    observed=observed,
                )
            else:
                raise _RuntimeDownloadQuotaExceeded(
                    "runtime compatibility download contains a link or special file"
                )

    def _attach_target(self, parent_fd: int) -> None:
        before = os.stat(
            self._target_name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if not stat.S_ISDIR(before.st_mode) or before.st_uid != self._expected_owner:
            raise _RuntimeDownloadQuotaExceeded(
                "runtime compatibility download target is not an owned directory"
            )
        target_fd = os.open(self._target_name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        try:
            if _readback_object_identity(os.fstat(target_fd)) != (
                _readback_object_identity(before)
            ):
                raise RuntimePathSecurityError(
                    "runtime compatibility download target changed while opening"
                )
            self._target_watch = self._add_watch(target_fd, relative_path="")
            self._observe_existing_directory(
                target_fd,
                relative_path="",
                depth=0,
            )
        finally:
            os.close(target_fd)

    def _observe_created_child(
        self,
        watch: _RuntimeDownloadWatch,
        name: str,
        *,
        is_directory: bool,
    ) -> None:
        if watch.relative_path is None:
            if name != self._target_name or not is_directory:
                raise RuntimePathSecurityError(
                    "runtime compatibility download escaped its private target"
                )
            self._attach_target(watch.descriptor)
            return
        relative_path = self._join_relative(watch.relative_path, name)
        try:
            observed = os.stat(
                name,
                dir_fd=watch.descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            self._accounting.observe_created(
                relative_path,
                is_directory=is_directory,
                observed=None,
            )
            return
        if observed.st_uid != self._expected_owner:
            raise RuntimePathSecurityError(
                "runtime compatibility download created an unowned entry"
            )
        observed_directory = stat.S_ISDIR(observed.st_mode)
        if observed_directory is not is_directory:
            raise RuntimePathSecurityError("runtime compatibility download entry type is unstable")
        if not observed_directory and (
            not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1
        ):
            raise RuntimePathSecurityError(
                "runtime compatibility download created a link or special file"
            )
        self._accounting.observe_created(
            relative_path,
            is_directory=is_directory,
            observed=observed,
        )
        if is_directory:
            self._accounting.exhaust()
            raise RuntimePathSecurityError(
                "runtime compatibility download created a directory with an "
                "unobservable mutation window"
            )

    def _handle_event(self, watch_id: int, mask: int, name: str) -> None:
        watch = self._watches.get(watch_id)
        if watch is None or mask & (_IN_Q_OVERFLOW | _IN_IGNORED):
            raise RuntimePathSecurityError(
                "runtime compatibility download event authority was lost"
            )
        if mask & (_IN_DELETE_SELF | _IN_MOVE_SELF):
            raise RuntimePathSecurityError(
                "runtime compatibility download directory authority changed"
            )
        if mask & (_IN_MOVED_FROM | _IN_MOVED_TO):
            raise RuntimePathSecurityError(
                "runtime compatibility download contains unobservable moved work"
            )
        is_directory = bool(mask & _IN_ISDIR)
        if mask & _IN_CREATE:
            self._observe_created_child(
                watch,
                name,
                is_directory=is_directory,
            )
        if mask & _IN_DELETE:
            if watch.relative_path is None:
                raise RuntimePathSecurityError("runtime compatibility download target was removed")
            self._accounting.forget(self._join_relative(watch.relative_path, name))
        if mask & (_IN_MODIFY | _IN_ATTRIB | _IN_CLOSE_WRITE):
            if watch.relative_path is None:
                if name == self._target_name and self._target_watch is None:
                    self._attach_target(watch.descriptor)
                elif name != self._target_name:
                    raise RuntimePathSecurityError(
                        "runtime compatibility download escaped its private target"
                    )
            elif not is_directory:
                relative_path = self._join_relative(watch.relative_path, name)
                try:
                    observed = os.stat(
                        name,
                        dir_fd=watch.descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError as exc:
                    raise RuntimePathSecurityError(
                        "runtime compatibility download byte work became unobservable"
                    ) from exc
                if observed.st_size > self._accounting.max_bytes:
                    raise _RuntimeDownloadQuotaExceeded(
                        "runtime compatibility download exceeds the byte quota"
                    )
                if mask & _IN_CLOSE_WRITE:
                    self._accounting.observe_closed_file(relative_path, observed)

    def drain(self) -> None:
        while True:
            payload = _read_runtime_download_events(self._descriptor)
            if not payload:
                return
            offset = 0
            while offset < len(payload):
                if len(payload) - offset < _INOTIFY_EVENT_HEADER.size:
                    raise RuntimePathSecurityError(
                        "runtime compatibility download event evidence is malformed"
                    )
                watch, mask, _cookie, name_size = _INOTIFY_EVENT_HEADER.unpack_from(
                    payload,
                    offset,
                )
                offset += _INOTIFY_EVENT_HEADER.size
                end = offset + name_size
                if end > len(payload):
                    raise RuntimePathSecurityError(
                        "runtime compatibility download event evidence is malformed"
                    )
                raw_name = payload[offset:end].split(b"\x00", 1)[0]
                offset = end
                name = os.fsdecode(raw_name)
                self._handle_event(watch, mask, name)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for watch in self._watches.values():
            os.close(watch.descriptor)
        self._watches.clear()
        self._watched_identities.clear()
        os.close(self._descriptor)


def _measure_runtime_download_tree(
    directory_fd: int,
    *,
    expected_owner: int,
    max_files: int,
    max_nodes: int,
    max_bytes: int,
    usage: list[int],
    depth: int,
) -> None:
    if depth > _COPY_MAX_DEPTH:
        raise _RuntimeDownloadQuotaExceeded(
            "runtime compatibility download exceeds the depth quota"
        )
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            usage[1] += 1
            if usage[1] > max_nodes:
                raise _RuntimeDownloadQuotaExceeded(
                    "runtime compatibility download exceeds the node quota"
                )
            observed = os.stat(
                entry.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            if observed.st_uid != expected_owner:
                raise _RuntimeDownloadQuotaExceeded(
                    "runtime compatibility download contains an unowned entry"
                )
            if stat.S_ISDIR(observed.st_mode):
                child_fd = os.open(entry.name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
                try:
                    _measure_runtime_download_tree(
                        child_fd,
                        expected_owner=expected_owner,
                        max_files=max_files,
                        max_nodes=max_nodes,
                        max_bytes=max_bytes,
                        usage=usage,
                        depth=depth + 1,
                    )
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
                raise _RuntimeDownloadQuotaExceeded(
                    "runtime compatibility download contains a link or special file"
                )
            usage[0] += 1
            if usage[0] > max_files:
                raise _RuntimeDownloadQuotaExceeded(
                    "runtime compatibility download exceeds the file quota"
                )
            if observed.st_size < 0 or observed.st_size > max_bytes - usage[2]:
                raise _RuntimeDownloadQuotaExceeded(
                    "runtime compatibility download exceeds the byte quota"
                )
            usage[2] += observed.st_size


def _require_runtime_download_quota(
    parent_fd: int,
    target_name: str,
    *,
    expected_owner: int,
    max_files: int,
    max_nodes: int,
    max_bytes: int,
) -> None:
    try:
        observed = os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(observed.st_mode) or observed.st_uid != expected_owner:
        raise _RuntimeDownloadQuotaExceeded(
            "runtime compatibility download target is not an owned directory"
        )
    try:
        target_fd = os.open(target_name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise RuntimePathSecurityError(
            "runtime compatibility download quota inspection failed"
        ) from exc
    try:
        if _readback_directory_identity(os.fstat(target_fd)) != _readback_directory_identity(
            observed
        ):
            raise RuntimePathSecurityError(
                "runtime compatibility download target changed during quota inspection"
            )
        _measure_runtime_download_tree(
            target_fd,
            expected_owner=expected_owner,
            max_files=max_files,
            max_nodes=max_nodes,
            max_bytes=max_bytes,
            usage=[0, 0, 0],
            depth=0,
        )
    except RuntimePathSecurityError:
        raise
    except OSError as exc:
        raise RuntimePathSecurityError(
            "runtime compatibility download quota inspection failed"
        ) from exc
    finally:
        os.close(target_fd)


def _monitor_runtime_download_quota(
    parent_fd: int,
    target_name: str,
    budget: RuntimeReadbackBudget,
    stop: threading.Event,
    ready: threading.Event,
    expected_owner: int,
    accounting: _RuntimeDownloadAccounting,
) -> None:
    event_monitor: _RuntimeDownloadEventMonitor | None = None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if (
            getattr(libc, "inotify_init1", None) is not None
            and getattr(libc, "inotify_add_watch", None) is not None
            and Path("/proc/self/fd").is_dir()
        ):
            event_monitor = _RuntimeDownloadEventMonitor(
                parent_fd,
                target_name,
                expected_owner=expected_owner,
                accounting=accounting,
            )
        ready.set()
        while True:
            if event_monitor is not None:
                event_monitor.drain()
            if stop.wait(_DOWNLOAD_QUOTA_POLL_SECONDS):
                if event_monitor is not None:
                    event_monitor.drain()
                _require_runtime_download_quota(
                    parent_fd,
                    target_name,
                    expected_owner=expected_owner,
                    max_files=budget.max_files,
                    max_nodes=budget.max_nodes // 2,
                    max_bytes=budget.max_bytes,
                )
                return
    except BaseException as exc:
        budget.exhaust()
        if isinstance(exc, RuntimePathSecurityError):
            raise
        raise RuntimePathSecurityError(
            "runtime compatibility download monitor failed closed"
        ) from exc
    finally:
        ready.set()
        if event_monitor is not None:
            event_monitor.close()


def _hash_runtime_readback_file(
    parent_fd: int,
    source: _ReadbackSourceEntry,
    *,
    budget: RuntimeReadbackBudget,
    cancellation: threading.Event,
    accounting: _RuntimeDownloadAccounting,
) -> RuntimeReadbackFile:
    precharged_bytes = accounting.claim_bytes(source.relative_path, source.stat)
    budget.require_byte_capacity(source.stat.st_size - precharged_bytes)
    source_fd = os.open(source.name, _FILE_READ_FLAGS, dir_fd=parent_fd)
    try:
        opened = os.fstat(source_fd)
        if _readback_file_identity(opened) != source.identity:
            raise RuntimePathSecurityError(
                "runtime compatibility readback file changed while opening"
            )
        digest = hashlib.sha256()
        offset = 0
        while offset < opened.st_size:
            _check_readback_cancel(cancellation)
            chunk = os.pread(
                source_fd,
                min(_COPY_CHUNK_BYTES, opened.st_size - offset),
                offset,
            )
            if not chunk:
                raise RuntimePathSecurityError(
                    "runtime compatibility readback ended before its verified size"
                )
            credited = min(precharged_bytes, len(chunk))
            precharged_bytes -= credited
            budget.consume_bytes(len(chunk) - credited)
            digest.update(chunk)
            offset += len(chunk)
        source_after = os.fstat(source_fd)
        named_after = os.stat(
            source.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            _readback_file_identity(source_after) != source.identity
            or _readback_file_identity(named_after) != source.identity
        ):
            raise RuntimePathSecurityError(
                "runtime compatibility readback file changed during hashing"
            )
        return RuntimeReadbackFile(
            relative_path=source.relative_path,
            size_bytes=opened.st_size,
            sha256=digest.hexdigest(),
        )
    finally:
        os.close(source_fd)


def _scan_runtime_download_directory(
    directory_fd: int,
    *,
    relative_prefix: str,
    expected_owner: int,
    budget: RuntimeReadbackBudget,
    cancellation: threading.Event,
    mutation_authority: _ReadbackMutationAuthority | None,
    accounting: _RuntimeDownloadAccounting,
    depth: int,
) -> list[RuntimeReadbackFile]:
    if depth > _COPY_MAX_DEPTH:
        raise RuntimePathSecurityError(
            "runtime compatibility readback exceeds the directory depth limit"
        )
    if mutation_authority is not None:
        mutation_authority.add(directory_fd)
    snapshot = _enumerate_readback_source_directory(
        directory_fd,
        relative_prefix=relative_prefix,
        expected_owner=expected_owner,
        budget=budget,
        cancellation=cancellation,
        consume_files=True,
        accounting=accounting,
    )
    files: list[RuntimeReadbackFile] = []
    for source in snapshot.entries:
        _check_readback_cancel(cancellation)
        if source.is_directory:
            child_fd = os.open(source.name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            try:
                if _readback_directory_identity(os.fstat(child_fd)) != source.identity:
                    raise RuntimePathSecurityError(
                        "runtime compatibility readback directory changed while opening"
                    )
                files.extend(
                    _scan_runtime_download_directory(
                        child_fd,
                        relative_prefix=source.relative_path,
                        expected_owner=expected_owner,
                        budget=budget,
                        cancellation=cancellation,
                        mutation_authority=mutation_authority,
                        accounting=accounting,
                        depth=depth + 1,
                    )
                )
            finally:
                os.close(child_fd)
        else:
            files.append(
                _hash_runtime_readback_file(
                    directory_fd,
                    source,
                    budget=budget,
                    cancellation=cancellation,
                    accounting=accounting,
                )
            )
    _verify_readback_source_directory(
        directory_fd,
        snapshot,
        relative_prefix=relative_prefix,
        expected_owner=expected_owner,
        budget=budget,
        cancellation=cancellation,
        accounting=accounting,
    )
    return files


def _scan_runtime_download_sync(
    parent_fd: int,
    target_name: str,
    relative_prefix: str,
    expected_parent_identity: tuple[int, ...],
    expected_owner: int,
    budget: RuntimeReadbackBudget,
    cancellation: threading.Event,
    accounting: _RuntimeDownloadAccounting,
) -> RuntimeReadback:
    _check_readback_cancel(cancellation)
    if _directory_identity(os.fstat(parent_fd)) != expected_parent_identity:
        raise RuntimePathSecurityError("runtime compatibility readback parent changed")
    before = os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode) or before.st_uid != expected_owner:
        raise RuntimePathSecurityError(
            "runtime compatibility readback target is not an owned directory"
        )
    target_fd = os.open(target_name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    mutation_authority: _ReadbackMutationAuthority | None = None
    try:
        target_identity = _readback_directory_identity(before)
        if _readback_directory_identity(os.fstat(target_fd)) != target_identity:
            raise RuntimePathSecurityError(
                "runtime compatibility readback target changed while opening"
            )
        if sys.platform.startswith("linux"):
            mutation_authority = _ReadbackMutationAuthority()
            mutation_authority.add(parent_fd)
        files = _scan_runtime_download_directory(
            target_fd,
            relative_prefix=relative_prefix,
            expected_owner=expected_owner,
            budget=budget,
            cancellation=cancellation,
            mutation_authority=mutation_authority,
            accounting=accounting,
            depth=0,
        )
        named_after = os.stat(
            target_name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            _readback_directory_identity(os.fstat(target_fd)) != target_identity
            or _readback_directory_identity(named_after) != target_identity
            or _directory_identity(os.fstat(parent_fd)) != expected_parent_identity
        ):
            raise RuntimePathSecurityError(
                "runtime compatibility readback target changed during scanning"
            )
        if mutation_authority is not None:
            mutation_authority.require_quiet()
        return RuntimeReadback(files=tuple(sorted(files, key=lambda item: item.relative_path)))
    finally:
        if mutation_authority is not None:
            mutation_authority.close()
        os.close(target_fd)


async def _stop_runtime_download_tasks(
    download: asyncio.Task[None],
    monitor: asyncio.Task[None],
    stop: threading.Event,
    temporary_root: _RuntimeReadbackTemporaryRoot,
    operation: RuntimeDownloadOperation | None,
) -> bool:
    stop.set()
    quarantined = False
    cancellation_requested = not download.done()
    termination: asyncio.Task[None] | None = None
    if not download.done():
        temporary_root.quarantine_for_worker()
        quarantined = True
        if operation is None:
            download.cancel()
        else:
            termination = asyncio.create_task(operation.terminate())
    deadline = asyncio.get_running_loop().time() + _DOWNLOAD_JOIN_SECONDS
    pending: set[asyncio.Task[None]] = {
        task for task in (download, monitor, termination) if task is not None and not task.done()
    }
    for completed in (download, monitor, termination):
        if completed is not None and completed.done() and not completed.cancelled():
            completed.exception()
    while pending:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        try:
            done, pending = await asyncio.wait(pending, timeout=remaining)
        except asyncio.CancelledError:
            continue
        for completed in done:
            if not completed.cancelled():
                completed.exception()
    joined = not pending and (operation is not None or not cancellation_requested)
    if not joined:
        if not quarantined:
            temporary_root.quarantine_for_worker()
            quarantined = True
        if operation is None:
            _PERMANENT_RUNTIME_READBACK_ISOLATIONS.append(temporary_root)
            return False
        remaining_tasks = set(pending)

        def release(completed: asyncio.Task[None]) -> None:
            if not completed.cancelled():
                completed.exception()
            remaining_tasks.discard(completed)
            if not remaining_tasks:
                temporary_root.release_worker()
                _schedule_runtime_readback_root_cleanup(temporary_root)

        for task in tuple(remaining_tasks):
            task.add_done_callback(release)
        return False
    if quarantined:
        temporary_root.release_worker()
    return True


async def _bounded_public_runtime_readback(
    runtime: object,
    remote_path: str,
    local_path: Path,
    *,
    budget: RuntimeReadbackBudget,
    relative_prefix: str,
    temporary_root: _RuntimeReadbackTemporaryRoot,
    expected_directories: Sequence[str] = (),
) -> RuntimeReadback:
    if not isinstance(budget, RuntimeReadbackBudget):
        raise TypeError("runtime compatibility readback requires the closed budget authority")
    target_parts = _canonical_absolute_parts(
        str(local_path.absolute()),
        label="runtime compatibility readback target",
    )
    if not target_parts:
        raise RuntimePathSecurityError(
            "runtime compatibility readback target must not be the filesystem root"
        )
    target_parent = Path(os.sep).joinpath(*target_parts[:-1])
    target_name = target_parts[-1]
    if target_parent != temporary_root.path:
        raise RuntimePathSecurityError(
            "runtime compatibility readback target is outside its private root"
        )
    parent_pin = _pin_absolute_directory(target_parent)
    parent_state = os.fstat(parent_pin.descriptor)
    if (
        not stat.S_ISDIR(parent_state.st_mode)
        or parent_state.st_uid != os.geteuid()
        or stat.S_IMODE(parent_state.st_mode) != 0o700
        or _readback_object_identity(parent_state)
        != _readback_object_identity(os.fstat(temporary_root.descriptor))
    ):
        parent_pin.close()
        raise RuntimePathSecurityError("runtime compatibility readback parent is not private")
    parent_identity = _directory_identity(parent_state)
    try:
        try:
            os.stat(target_name, dir_fd=parent_pin.descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise RuntimePathSecurityError("runtime compatibility readback target already exists")
        os.mkdir(target_name, mode=0o700, dir_fd=parent_pin.descriptor)
        target_fd = os.open(target_name, _DIRECTORY_FLAGS, dir_fd=parent_pin.descriptor)
        try:
            directories: set[tuple[str, ...]] = set()
            if len(expected_directories) > RUNTIME_READBACK_MAX_NODES:
                raise RuntimePathSecurityError(
                    "runtime compatibility download directory authority exceeds its bound"
                )
            for value in expected_directories:
                if (
                    not isinstance(value, str)
                    or not value
                    or value.startswith("/")
                    or "\x00" in value
                ):
                    raise RuntimePathSecurityError(
                        "runtime compatibility download directory authority is invalid"
                    )
                parts = tuple(value.split("/"))
                if len(parts) > _COPY_MAX_DEPTH or any(part in {"", ".", ".."} for part in parts):
                    raise RuntimePathSecurityError(
                        "runtime compatibility download directory authority is invalid"
                    )
                directories.add(parts)
            for parts in sorted(directories, key=lambda item: (len(item), item)):
                pin = _open_relative_directories(
                    target_fd,
                    parts,
                    create=True,
                    expected_owner=os.geteuid(),
                )
                try:
                    pin.verify(
                        target_fd,
                        label="runtime compatibility download directory authority",
                    )
                finally:
                    pin.close()
        finally:
            os.close(target_fd)
        download_method = getattr(runtime, "download_dir", None)
        if not callable(download_method):
            raise TypeError("runtime does not implement the public download contract")
        stop = threading.Event()
        ready = threading.Event()
        accounting = _RuntimeDownloadAccounting(
            budget,
            relative_prefix=relative_prefix,
        )
        monitor = asyncio.create_task(
            asyncio.to_thread(
                _monitor_runtime_download_quota,
                parent_pin.descriptor,
                target_name,
                budget,
                stop,
                ready,
                os.geteuid(),
                accounting,
            )
        )
        download: asyncio.Task[None] | None = None
        operation: RuntimeDownloadOperation | None = None
        try:
            while not ready.is_set():
                if monitor.done():
                    monitor.result()
                await asyncio.sleep(0)
            operation_method = getattr(runtime, "_start_download_dir_operation", None)
            if callable(operation_method):
                operation = operation_method(remote_path, str(local_path))
                if operation is not None and not isinstance(operation, RuntimeDownloadOperation):
                    raise RuntimePathSecurityError(
                        "runtime download operation authority is invalid"
                    )
            download = (
                operation.task
                if operation is not None
                else asyncio.create_task(download_method(remote_path, str(local_path)))
            )
            done, _pending = await asyncio.wait(
                {download, monitor},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if monitor in done and monitor.exception() is not None:
                monitor.result()
            if download not in done:
                raise RuntimePathSecurityError(
                    "runtime compatibility download quota monitor stopped early"
                )
            download.result()
            stop.set()
            await monitor
        except BaseException as exc:
            budget.exhaust()
            if download is None:
                stop.set()
                while not monitor.done():
                    try:
                        await asyncio.shield(monitor)
                    except asyncio.CancelledError:
                        continue
                    except BaseException:
                        break
                if not monitor.cancelled():
                    monitor.exception()
                joined = True
            else:
                joined = await _stop_runtime_download_tasks(
                    download,
                    monitor,
                    stop,
                    temporary_root,
                    operation,
                )
            if not joined:
                raise RuntimePathSecurityError(
                    "runtime compatibility download remains active after its hard join bound"
                ) from exc
            raise

        parent_pin.verify(label="runtime compatibility readback parent")
        if _directory_identity(os.fstat(parent_pin.descriptor)) != parent_identity:
            raise RuntimePathSecurityError(
                "runtime compatibility readback parent changed during download"
            )
        cancellation = threading.Event()
        worker = asyncio.create_task(
            asyncio.to_thread(
                _scan_runtime_download_sync,
                parent_pin.descriptor,
                target_name,
                relative_prefix,
                parent_identity,
                os.geteuid(),
                budget,
                cancellation,
                accounting,
            )
        )
        try:
            readback = await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancellation.set()
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    cancellation.set()
                except BaseException:
                    break
            if worker.done() and not worker.cancelled():
                try:
                    worker.result()
                except BaseException:
                    pass
            raise
        parent_pin.verify(label="runtime compatibility readback parent")
        if _directory_identity(os.fstat(parent_pin.descriptor)) != parent_identity:
            raise RuntimePathSecurityError(
                "runtime compatibility readback parent changed during scanning"
            )
        return readback
    finally:
        parent_pin.close()
