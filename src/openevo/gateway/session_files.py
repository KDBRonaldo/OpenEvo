"""Private, identity-pinned files owned by one gateway session."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from typing import Final, TypeAlias


SessionRootIdentity: TypeAlias = tuple[int, int, int]

_AUTH_MAX_BYTES: Final[int] = 1024 * 1024
_CLEANUP_MAX_DEPTH: Final[int] = 64
_CLEANUP_MAX_NODES: Final[int] = 100_000
_DIRECTORY_FLAGS: Final[int] = (
    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
)
_PATH_FLAGS: Final[int] = (
    getattr(os, "O_PATH", os.O_RDONLY) | os.O_CLOEXEC | os.O_NOFOLLOW
)


class SessionFileSecurityError(RuntimeError):
    """Raised when private session state cannot be handled without a path race."""


def capture_session_root_identity(session_dir: Path) -> SessionRootIdentity:
    """Pin a newly-created session root before a runtime can mutate it."""

    parent_fd, name = _open_absolute_parent(session_dir)
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        try:
            opened = os.fstat(descriptor)
            _require_owned_directory(opened, label="session root")
            if _object_identity(before) != _object_identity(opened):
                raise SessionFileSecurityError("session root changed while it was opened")
            return (opened.st_dev, opened.st_ino, opened.st_uid)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise SessionFileSecurityError("session root could not be opened safely") from exc
    finally:
        os.close(parent_fd)


def stage_codex_subscription_auth(
    *,
    source: Path,
    session_dir: Path,
    session_identity: SessionRootIdentity,
    target_home_parts: tuple[str, ...],
) -> None:
    """Copy a verified private auth file into a private session directory."""

    source_parent_fd = -1
    source_name = source.name
    source_fd = -1
    target_parent_fd = -1
    target_fd = -1
    target_identity: tuple[int, int] | None = None
    try:
        source_parent_fd, source_name = _open_absolute_parent(source)
        source_before = os.stat(
            source_name,
            dir_fd=source_parent_fd,
            follow_symlinks=False,
        )
        _require_private_auth(source_before)
        source_fd = os.open(
            source_name,
            os.O_RDONLY
            | os.O_CLOEXEC
            | os.O_NOFOLLOW
            | os.O_NONBLOCK,
            dir_fd=source_parent_fd,
        )
        source_opened = os.fstat(source_fd)
        _require_private_auth(source_opened)
        if _auth_identity(source_before) != _auth_identity(source_opened):
            raise SessionFileSecurityError(
                "Codex subscription auth changed while it was opened"
            )

        root_path_fd, _parent_fd, _root_name = _open_pinned_session_root(
            session_dir,
            session_identity,
        )
        try:
            _fchmod_stable(root_path_fd, 0o700, label="session root")
            root_fd = _open_readable_directory(root_path_fd, label="session root")
            try:
                target_parent_fd = _open_private_directories(
                    root_fd,
                    target_home_parts,
                    expected_owner=session_identity[2],
                )
            finally:
                os.close(root_fd)
        finally:
            os.close(root_path_fd)
            os.close(_parent_fd)

        target_fd = os.open(
            "auth.json",
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | os.O_NOFOLLOW,
            0o600,
            dir_fd=target_parent_fd,
        )
        os.fchmod(target_fd, 0o600)
        target_opened = os.fstat(target_fd)
        _require_private_staged_auth(
            target_opened,
            expected_owner=session_identity[2],
        )
        target_identity = _object_identity(target_opened)

        _copy_exact(source_fd, target_fd, source_opened.st_size)
        os.fsync(target_fd)

        source_after = os.fstat(source_fd)
        if _auth_identity(source_opened) != _auth_identity(source_after):
            raise SessionFileSecurityError(
                "Codex subscription auth changed while it was copied"
            )
        _require_private_auth(source_after)
        _require_path_identity(
            source_parent_fd,
            source_name,
            source_after,
            label="Codex subscription auth",
        )

        target_after = os.fstat(target_fd)
        if target_after.st_nlink != 1:
            raise SessionFileSecurityError(
                "staged Codex auth path changed while it was written"
            )
        _require_private_staged_auth(
            target_after,
            expected_owner=session_identity[2],
        )
        if _object_identity(target_opened) != _object_identity(target_after):
            raise SessionFileSecurityError("staged Codex auth changed while it was written")
        _require_path_identity(
            target_parent_fd,
            "auth.json",
            target_after,
            label="staged Codex auth",
        )
    except FileNotFoundError as exc:
        _unlink_if_same_identity(target_parent_fd, "auth.json", target_identity)
        raise SessionFileSecurityError(
            "Codex subscription auth was not found at ~/.codex/auth.json; "
            "sign in with Codex on the remote host before retrying"
        ) from exc
    except SessionFileSecurityError:
        _unlink_if_same_identity(target_parent_fd, "auth.json", target_identity)
        raise
    except (OSError, ValueError) as exc:
        _unlink_if_same_identity(target_parent_fd, "auth.json", target_identity)
        raise SessionFileSecurityError(
            "Codex subscription auth could not be staged safely; ensure "
            "~/.codex/auth.json is a private, user-owned regular file"
        ) from exc
    finally:
        for descriptor in (target_fd, target_parent_fd, source_fd, source_parent_fd):
            if descriptor >= 0:
                os.close(descriptor)


def remove_session_tree(
    session_dir: Path,
    session_identity: SessionRootIdentity,
    *,
    max_depth: int = _CLEANUP_MAX_DEPTH,
    max_nodes: int = _CLEANUP_MAX_NODES,
) -> None:
    """Remove one pinned session tree without following or widening permissions."""

    root_path_fd, parent_fd, root_name = _open_pinned_session_root(
        session_dir,
        session_identity,
    )
    budget = [max_nodes]
    try:
        _fchmod_stable(root_path_fd, 0o700, label="session root")
        root_fd = _open_readable_directory(root_path_fd, label="session root")
        try:
            _remove_directory_contents(
                root_fd,
                expected_owner=session_identity[2],
                depth=0,
                max_depth=max_depth,
                budget=budget,
            )
        finally:
            os.close(root_fd)
        _require_named_identity(
            parent_fd,
            root_name,
            session_identity[:2],
            label="session root",
            expected_owner=session_identity[2],
        )
        os.rmdir(root_name, dir_fd=parent_fd)
    except SessionFileSecurityError:
        raise
    except OSError as exc:
        raise SessionFileSecurityError("session root cleanup failed safely") from exc
    finally:
        os.close(root_path_fd)
        os.close(parent_fd)


def _open_absolute_parent(path: Path) -> tuple[int, str]:
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise SessionFileSecurityError("private session path must be absolute and canonical")
    parent_fd = _open_absolute_directory(path.parent)
    return parent_fd, path.name


def _open_absolute_directory(path: Path) -> int:
    parts = path.parts
    if not parts or parts[0] != os.sep or any(part in {"", ".", ".."} for part in parts[1:]):
        raise SessionFileSecurityError("private session path must be absolute and canonical")
    current_fd = os.open(os.sep, _DIRECTORY_FLAGS)
    try:
        for part in parts[1:]:
            next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _open_pinned_session_root(
    path: Path,
    expected: SessionRootIdentity,
) -> tuple[int, int, str]:
    parent_fd, name = _open_absolute_parent(path)
    root_fd = -1
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        _require_session_identity(before, expected)
        root_fd = os.open(
            name,
            _PATH_FLAGS | os.O_DIRECTORY,
            dir_fd=parent_fd,
        )
        opened = os.fstat(root_fd)
        _require_session_identity(opened, expected)
        if _object_identity(before) != _object_identity(opened):
            raise SessionFileSecurityError("session root changed while it was opened")
        return root_fd, parent_fd, name
    except Exception:
        if root_fd >= 0:
            os.close(root_fd)
        os.close(parent_fd)
        raise


def _open_private_directories(
    root_fd: int,
    parts: tuple[str, ...],
    *,
    expected_owner: int,
) -> int:
    current_fd = os.dup(root_fd)
    try:
        for part in parts:
            if part in {"", ".", ".."} or "/" in part:
                raise SessionFileSecurityError("Codex subscription CODEX_HOME is not safe")
            try:
                os.mkdir(part, mode=0o700, dir_fd=current_fd)
            except FileExistsError:
                pass
            next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=current_fd)
            try:
                opened = os.fstat(next_fd)
                _require_owned_directory(
                    opened,
                    label="Codex subscription state directory",
                    expected_owner=expected_owner,
                )
                os.fchmod(next_fd, 0o700)
            except Exception:
                os.close(next_fd)
                raise
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _copy_exact(source_fd: int, target_fd: int, expected_size: int) -> None:
    remaining = expected_size
    while remaining:
        chunk = os.read(source_fd, min(64 * 1024, remaining))
        if not chunk:
            raise SessionFileSecurityError(
                "Codex subscription auth changed while it was copied"
            )
        view = memoryview(chunk)
        while view:
            written = os.write(target_fd, view)
            if written <= 0:
                raise SessionFileSecurityError("staged Codex auth could not be written")
            view = view[written:]
        remaining -= len(chunk)
    if os.read(source_fd, 1):
        raise SessionFileSecurityError(
            "Codex subscription auth changed while it was copied"
        )


def _remove_directory_contents(
    directory_fd: int,
    *,
    expected_owner: int,
    depth: int,
    max_depth: int,
    budget: list[int],
) -> None:
    if depth > max_depth:
        raise SessionFileSecurityError("session cleanup exceeds the depth limit")
    names: list[str] = []
    try:
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > budget[0]:
                    raise SessionFileSecurityError(
                        "session cleanup exceeds the node limit"
                    )
    except OSError as exc:
        raise SessionFileSecurityError("session directory could not be inventoried") from exc
    names.sort()

    for name in names:
        budget[0] -= 1
        if budget[0] < 0:
            raise SessionFileSecurityError("session cleanup exceeds the node limit")
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if before.st_uid != expected_owner:
            raise SessionFileSecurityError(
                "session cleanup found an entry owned by another user"
            )
        entry_fd = os.open(name, _PATH_FLAGS, dir_fd=directory_fd)
        try:
            opened = os.fstat(entry_fd)
            if _full_object_identity(before) != _full_object_identity(opened):
                raise SessionFileSecurityError(
                    "session cleanup entry changed while it was opened"
                )
            if stat.S_ISDIR(opened.st_mode):
                _fchmod_stable(entry_fd, 0o700, label="session directory")
                child_fd = _open_readable_directory(
                    entry_fd,
                    label="session directory",
                )
                try:
                    _remove_directory_contents(
                        child_fd,
                        expected_owner=expected_owner,
                        depth=depth + 1,
                        max_depth=max_depth,
                        budget=budget,
                    )
                finally:
                    os.close(child_fd)
                _require_named_identity(
                    directory_fd,
                    name,
                    _object_identity(opened),
                    label="session directory",
                    expected_owner=expected_owner,
                )
                os.rmdir(name, dir_fd=directory_fd)
            else:
                _require_named_identity(
                    directory_fd,
                    name,
                    _object_identity(opened),
                    label="session entry",
                    expected_owner=expected_owner,
                )
                os.unlink(name, dir_fd=directory_fd)
        finally:
            os.close(entry_fd)


def _fchmod_stable(descriptor: int, mode: int, *, label: str) -> None:
    identity = _object_identity(os.fstat(descriptor))
    try:
        os.fchmod(descriptor, mode)
    except OSError as exc:
        if exc.errno != errno.EBADF:
            raise SessionFileSecurityError(f"{label} permissions could not be restored") from exc
        try:
            os.chmod(f"/proc/self/fd/{descriptor}", mode)
        except OSError as proc_exc:
            raise SessionFileSecurityError(
                f"{label} permissions could not be restored through its stable descriptor"
            ) from proc_exc
    if _object_identity(os.fstat(descriptor)) != identity:
        raise SessionFileSecurityError(f"{label} changed during permission restoration")


def _open_readable_directory(path_fd: int, *, label: str) -> int:
    try:
        descriptor = os.open(".", _DIRECTORY_FLAGS, dir_fd=path_fd)
    except OSError as exc:
        raise SessionFileSecurityError(f"{label} could not be opened after permission recovery") from exc
    if _object_identity(os.fstat(descriptor)) != _object_identity(os.fstat(path_fd)):
        os.close(descriptor)
        raise SessionFileSecurityError(f"{label} changed while it was reopened")
    return descriptor


def _require_private_auth(value: os.stat_result) -> None:
    permissions = stat.S_IMODE(value.st_mode)
    if not stat.S_ISREG(value.st_mode):
        raise SessionFileSecurityError(
            "Codex subscription auth must be a regular file, not a link or special file"
        )
    if value.st_uid != os.geteuid():
        raise SessionFileSecurityError(
            "Codex subscription auth must be owned by the Core service user"
        )
    if value.st_nlink != 1:
        raise SessionFileSecurityError(
            "Codex subscription auth must not have additional hard links"
        )
    if permissions not in {0o400, 0o600}:
        raise SessionFileSecurityError(
            "Codex subscription auth must be private and owner-readable"
        )
    if value.st_size <= 0 or value.st_size > _AUTH_MAX_BYTES:
        raise SessionFileSecurityError(
            "Codex subscription auth has an invalid or excessive size"
        )


def _require_private_staged_auth(
    value: os.stat_result,
    *,
    expected_owner: int,
) -> None:
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_uid != expected_owner
        or value.st_nlink != 1
        or stat.S_IMODE(value.st_mode) != 0o600
    ):
        raise SessionFileSecurityError("staged Codex auth is not a private regular file")


def _require_owned_directory(
    value: os.stat_result,
    *,
    label: str,
    expected_owner: int | None = None,
) -> None:
    owner = os.geteuid() if expected_owner is None else expected_owner
    if not stat.S_ISDIR(value.st_mode) or value.st_uid != owner:
        raise SessionFileSecurityError(f"{label} is not an owned directory")


def _require_session_identity(
    value: os.stat_result,
    expected: SessionRootIdentity,
) -> None:
    _require_owned_directory(value, label="session root", expected_owner=expected[2])
    if (value.st_dev, value.st_ino, value.st_uid) != expected:
        raise SessionFileSecurityError("session root identity does not match its dispatch pin")


def _require_path_identity(
    directory_fd: int,
    name: str,
    expected: os.stat_result,
    *,
    label: str,
) -> None:
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise SessionFileSecurityError(f"{label} path changed during the operation") from exc
    if _full_object_identity(current) != _full_object_identity(expected):
        raise SessionFileSecurityError(f"{label} path changed during the operation")


def _require_named_identity(
    directory_fd: int,
    name: str,
    expected: tuple[int, int],
    *,
    label: str,
    expected_owner: int,
) -> None:
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise SessionFileSecurityError(f"{label} identity changed during cleanup") from exc
    if _object_identity(current) != expected or current.st_uid != expected_owner:
        raise SessionFileSecurityError(f"{label} identity changed during cleanup")


def _unlink_if_same_identity(
    directory_fd: int,
    name: str,
    expected: tuple[int, int] | None,
) -> None:
    if directory_fd < 0 or expected is None:
        return
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if _object_identity(current) == expected:
            os.unlink(name, dir_fd=directory_fd)
    except OSError:
        return


def _object_identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _full_object_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_mode, value.st_uid


def _auth_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int, int, int]:
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
