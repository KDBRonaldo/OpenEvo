"""Runtime abstraction for container-backed rollout execution."""

from __future__ import annotations

import asyncio
import os
import stat
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from openevo.runtime.models import ExecResult, RuntimeSpec

RUNTIME_SESSION_DIR: Final[str] = "/openevo/session"
RUNTIME_ARTIFACTS_DIR: Final[str] = f"{RUNTIME_SESSION_DIR}/artifacts"
RUNTIME_LOGS_DIR: Final[str] = f"{RUNTIME_SESSION_DIR}/logs"
RUNTIME_AGENT_LOG_DIR: Final[str] = f"{RUNTIME_LOGS_DIR}/agent"
RUNTIME_EVAL_LOG_DIR: Final[str] = f"{RUNTIME_LOGS_DIR}/eval"
RUNTIME_EVAL_ARTIFACT_DIR: Final[str] = f"{RUNTIME_SESSION_DIR}/eval_artifacts"
LOCAL_COMMAND_CAPTURE_MAX_BYTES: Final[int] = 4 * 1024 * 1024
_LOCAL_COMMAND_FINALIZE_SECONDS: Final[float] = 5.0
_COPY_MAX_DEPTH: Final[int] = 64
_COPY_MAX_NODES: Final[int] = 100_000
_COPY_CHUNK_BYTES: Final[int] = 64 * 1024
_DIRECTORY_FLAGS: Final[int] = (
    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
)
_FILE_READ_FLAGS: Final[int] = (
    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
)
_FILE_WRITE_FLAGS: Final[int] = (
    os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
)


class RuntimePathSecurityError(RuntimeError):
    """Raised when a bind-mount path cannot be proven to stay in its authority."""


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
                raise RuntimePathSecurityError(
                    "session path ancestor is not an owned directory"
                )
            descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            opened = os.fstat(descriptor)
            if _directory_identity(before) != _directory_identity(opened):
                os.close(descriptor)
                raise RuntimePathSecurityError(
                    "session path ancestor changed while it was opened"
                )
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
        raise RuntimePathSecurityError(
            "session path ancestor could not be opened safely"
        ) from exc


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
                raise RuntimePathSecurityError(
                    "copy source must be a single-link regular file"
                )
            flags = _FILE_READ_FLAGS
        elif stat.S_ISDIR(before.st_mode):
            flags = _DIRECTORY_FLAGS
        else:
            raise RuntimePathSecurityError("copy source is a link or special file")
        descriptor = os.open(name, flags, dir_fd=parent_pin.descriptor)
        opened = os.fstat(descriptor)
        expected = (
            _file_identity(before)
            if stat.S_ISREG(before.st_mode)
            else _directory_identity(before)
        )
        actual = (
            _file_identity(opened)
            if stat.S_ISREG(opened.st_mode)
            else _directory_identity(opened)
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
        _file_identity(named)
        if stat.S_ISREG(named.st_mode)
        else _directory_identity(named)
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
                raise RuntimePathSecurityError(
                    "copy target changed while it was opened"
                )
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != expected_owner
            or opened.st_nlink != 1
        ):
            raise RuntimePathSecurityError(
                "copy target is not an owned single-link regular file"
            )
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
            raise RuntimePathSecurityError(
                "copy target directory changed while it was opened"
            )
        return descriptor, _directory_identity(opened)
    except RuntimePathSecurityError:
        raise
    except PermissionError:
        raise
    except OSError as exc:
        raise RuntimePathSecurityError(
            "copy target directory could not be opened safely"
        ) from exc


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
                raise RuntimePathSecurityError(
                    "copy source contains a hard-linked file"
                )
            child_fd = os.open(name, _FILE_READ_FLAGS, dir_fd=source_fd)
            try:
                opened = os.fstat(child_fd)
                if _file_identity(before) != _file_identity(opened):
                    raise RuntimePathSecurityError(
                        "copy source file changed while it was opened"
                    )
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
                    raise RuntimePathSecurityError(
                        "copy target directory changed during transfer"
                    )
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


class BaseRuntime(ABC):
    """Base class for long-lived per-session execution runtimes."""

    def __init__(self, spec: RuntimeSpec, session_id: str, session_dir: Path) -> None:
        self.spec = spec
        self.session_id = session_id
        self.session_dir = session_dir.absolute()
        session_pin = _pin_absolute_directory(self.session_dir, create=True)
        try:
            self._session_root_identity = _directory_identity(
                os.fstat(session_pin.descriptor)
            )
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

    def resolve_host_path(self, runtime_path: str) -> Path | None:
        """Map a runtime path back to a host path via the session bind mount."""
        return validate_session_bind_path(
            self.session_dir,
            runtime_path,
            expected_identity=self._session_root_identity,
        )

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
                source_parents.descriptor
                if source_parents.descriptors
                else session_pin.descriptor
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
            raise FileNotFoundError(
                f"bind source does not exist: {runtime_path}"
            ) from exc
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
                target_parents.descriptor
                if target_parents.descriptors
                else session_pin.descriptor
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
        capture: bool = False,
    ) -> tuple[int, str | None, str | None]:
        """Run a local subprocess, optionally capturing stdout/stderr."""
        process_env = None if env is None else {**os.environ, **env}
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
                asyncio.create_task(
                    self._drain_bounded_stream(process.stdout, stdout_buffer)
                ),
                asyncio.create_task(
                    self._drain_bounded_stream(process.stderr, stderr_buffer)
                ),
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
