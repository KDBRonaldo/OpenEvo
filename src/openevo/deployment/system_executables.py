from __future__ import annotations

import os
from pathlib import Path
import stat
import sys


SSH_EXECUTABLE = "/usr/bin/ssh"
SSH_KEYSCAN_EXECUTABLE = "/usr/bin/ssh-keyscan"
RSYNC_EXECUTABLE = "/usr/bin/rsync"
OWNED_SUBPROCESS_BIRTH_ARGUMENT = "--openevo-owned-subprocess-birth-v1"

_ALLOWED_EXECUTABLES = frozenset(
    {
        SSH_EXECUTABLE,
        SSH_KEYSCAN_EXECUTABLE,
        RSYNC_EXECUTABLE,
    }
)
_MAX_AUTHORITY_PATH_BYTES = 4096


class VerifiedSystemExecutable:
    """Hold one verified root-owned system executable and its parent directory."""

    __slots__ = ("_descriptor", "_identity", "_name", "_parent_descriptor", "_path")

    def __init__(
        self,
        *,
        path: str,
        name: str,
        descriptor: int,
        parent_descriptor: int,
        identity: tuple[int, int, int, int],
    ) -> None:
        self._path = path
        self._name = name
        self._descriptor = descriptor
        self._parent_descriptor = parent_descriptor
        self._identity = identity

    @classmethod
    def open(cls, path: str) -> VerifiedSystemExecutable:
        if path not in _ALLOWED_EXECUTABLES:
            raise ValueError("system executable is not allowlisted")
        encoded = os.fsencode(path)
        candidate = Path(path)
        if (
            not candidate.is_absolute()
            or len(encoded) > _MAX_AUTHORITY_PATH_BYTES
            or b"\x00" in encoded
            or any(part in {"", ".", ".."} for part in candidate.parts[1:])
        ):
            raise ValueError("system executable path is invalid")

        current = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            _require_root_owned_directory(os.fstat(current))
            for component in candidate.parts[1:-1]:
                following = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=current,
                )
                try:
                    _require_root_owned_directory(os.fstat(following))
                except BaseException:
                    os.close(following)
                    raise
                os.close(current)
                current = following

            name = candidate.name
            before = os.stat(name, dir_fd=current, follow_symlinks=False)
            _require_root_owned_executable(before)
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=current)
            try:
                opened = os.fstat(descriptor)
                _require_root_owned_executable(opened)
                identity = _executable_identity(opened)
                if _executable_identity(before) != identity:
                    raise ValueError("system executable changed during open")
                final = os.stat(name, dir_fd=current, follow_symlinks=False)
                _require_root_owned_executable(final)
                if _executable_identity(final) != identity:
                    raise ValueError("system executable path binding changed")
            except BaseException:
                os.close(descriptor)
                raise
            return cls(
                path=path,
                name=name,
                descriptor=descriptor,
                parent_descriptor=current,
                identity=identity,
            )
        except BaseException:
            os.close(current)
            raise

    @property
    def descriptor(self) -> int:
        return self._descriptor

    @property
    def execution_path(self) -> str:
        return f"/dev/fd/{self._descriptor}"

    @property
    def display_path(self) -> str:
        return self._path

    def verify_path_binding(self) -> None:
        opened = os.fstat(self._descriptor)
        _require_root_owned_executable(opened)
        if _executable_identity(opened) != self._identity:
            raise ValueError("held system executable identity changed")
        final = os.stat(self._name, dir_fd=self._parent_descriptor, follow_symlinks=False)
        _require_root_owned_executable(final)
        if _executable_identity(final) != self._identity:
            raise ValueError("system executable path binding changed")

    def close(self) -> None:
        descriptor, self._descriptor = self._descriptor, -1
        parent, self._parent_descriptor = self._parent_descriptor, -1
        if descriptor >= 0:
            os.close(descriptor)
        if parent >= 0:
            os.close(parent)

    def __enter__(self) -> VerifiedSystemExecutable:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"VerifiedSystemExecutable(path={self._path!r})"


class VerifiedSshAgentSocket:
    """Hold the parent authority and identity of one external SSH agent socket."""

    __slots__ = ("_directories", "_identity", "_name", "_path")

    def __init__(
        self,
        *,
        path: str,
        name: str,
        directories: tuple[tuple[int, str | None, tuple[int, int, int, int]], ...],
        identity: tuple[int, int, int, int, int, int],
    ) -> None:
        self._path = path
        self._name = name
        self._directories = directories
        self._identity = identity

    @classmethod
    def open(cls, path: str) -> VerifiedSshAgentSocket:
        path = _canonical_agent_socket_path(path)
        encoded = os.fsencode(path)
        candidate = Path(path)
        if (
            not candidate.is_absolute()
            or not encoded
            or len(encoded) > _MAX_AUTHORITY_PATH_BYTES
            or b"\x00" in encoded
            or any(part in {"", ".", ".."} for part in candidate.parts[1:])
        ):
            raise ValueError("SSH agent socket path is invalid")

        directories: list[tuple[int, str | None, tuple[int, int, int, int]]] = []
        current = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            root_metadata = os.fstat(current)
            _require_agent_directory(root_metadata)
            directories.append((current, None, _agent_directory_identity(root_metadata)))
            for component in candidate.parts[1:-1]:
                following = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=current,
                )
                try:
                    metadata = os.fstat(following)
                    _require_agent_directory(metadata)
                    path_metadata = os.stat(
                        component,
                        dir_fd=current,
                        follow_symlinks=False,
                    )
                    if _agent_directory_identity(path_metadata) != _agent_directory_identity(
                        metadata
                    ):
                        raise ValueError("SSH agent socket ancestor changed during open")
                except BaseException:
                    os.close(following)
                    raise
                directories.append((following, component, _agent_directory_identity(metadata)))
                current = following

            name = candidate.name
            metadata = os.stat(name, dir_fd=current, follow_symlinks=False)
            _require_agent_socket(metadata)
            identity = _agent_socket_identity(metadata)
            final = os.stat(name, dir_fd=current, follow_symlinks=False)
            _require_agent_socket(final)
            if _agent_socket_identity(final) != identity:
                raise ValueError("SSH agent socket path binding changed")
            return cls(
                path=path,
                name=name,
                directories=tuple(directories),
                identity=identity,
            )
        except BaseException:
            for descriptor, _name, _identity in reversed(directories):
                os.close(descriptor)
            if not directories:
                os.close(current)
            raise

    @property
    def path(self) -> str:
        return self._path

    def verify_path_binding(self) -> None:
        for index, (descriptor, name, identity) in enumerate(self._directories):
            opened = os.fstat(descriptor)
            _require_agent_directory(opened)
            if _agent_directory_identity(opened) != identity:
                raise ValueError("held SSH agent socket ancestor identity changed")
            if name is not None:
                parent = self._directories[index - 1][0]
                path_metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
                _require_agent_directory(path_metadata)
                if _agent_directory_identity(path_metadata) != identity:
                    raise ValueError("SSH agent socket ancestor path binding changed")
        metadata = os.stat(
            self._name,
            dir_fd=self._directories[-1][0],
            follow_symlinks=False,
        )
        _require_agent_socket(metadata)
        if _agent_socket_identity(metadata) != self._identity:
            raise ValueError("SSH agent socket path binding changed")

    def close(self) -> None:
        directories, self._directories = self._directories, ()
        for descriptor, _name, _identity in reversed(directories):
            os.close(descriptor)

    def __enter__(self) -> VerifiedSshAgentSocket:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()


def closed_ssh_environment(authentication_method: str) -> dict[str, str]:
    """Build the complete environment for release SSH and rsync children."""

    if authentication_method != "ssh_agent":
        return {}
    socket_path = os.environ.get("SSH_AUTH_SOCK")
    if socket_path is None:
        return {}
    encoded = os.fsencode(socket_path)
    if (
        not os.path.isabs(socket_path)
        or not encoded
        or len(encoded) > _MAX_AUTHORITY_PATH_BYTES
        or b"\x00" in encoded
    ):
        raise ValueError("SSH agent socket path is invalid")
    with VerifiedSshAgentSocket.open(socket_path) as authority:
        authority.verify_path_binding()
        return {"SSH_AUTH_SOCK": authority.path}


def run_packaged_owned_subprocess_birth(arguments: list[str]) -> None:
    """Publish process identity, then exec one inherited allowlisted executable FD."""

    positions = [
        index for index, value in enumerate(arguments) if value == OWNED_SUBPROCESS_BIRTH_ARGUMENT
    ]
    if len(positions) != 1:
        raise ValueError("owned subprocess birth invocation is invalid")
    payload = arguments[positions[0] + 1 :]
    if len(payload) < 3 or payload[2] not in _ALLOWED_EXECUTABLES:
        raise ValueError("owned subprocess birth payload is invalid")
    birth_descriptor = _canonical_descriptor(payload[0])
    executable_descriptor = _canonical_descriptor(payload[1])
    if birth_descriptor == executable_descriptor:
        raise ValueError("owned subprocess descriptors overlap")
    birth_metadata = os.fstat(birth_descriptor)
    if (
        not stat.S_ISREG(birth_metadata.st_mode)
        or birth_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(birth_metadata.st_mode) != 0o600
        or birth_metadata.st_nlink > 1
        or birth_metadata.st_size > 128
    ):
        raise ValueError("owned subprocess birth authority is invalid")
    executable_metadata = os.fstat(executable_descriptor)
    _require_root_owned_executable(executable_metadata)
    path_metadata = os.stat(payload[2], follow_symlinks=False)
    _require_root_owned_executable(path_metadata)
    if _executable_identity(path_metadata) != _executable_identity(executable_metadata):
        raise ValueError("owned subprocess executable binding changed")
    os.fchmod(birth_descriptor, 0o600)
    os.ftruncate(birth_descriptor, 0)
    os.lseek(birth_descriptor, 0, os.SEEK_SET)
    process_group_id = os.getpgrp()
    record = f"{process_group_id} {process_group_id} {os.getsid(0)}\n".encode("ascii")
    offset = 0
    while offset < len(record):
        offset += os.write(birth_descriptor, record[offset:])
    os.fsync(birth_descriptor)
    os.close(birth_descriptor)
    os.set_inheritable(executable_descriptor, False)
    os.execve(
        f"/dev/fd/{executable_descriptor}",
        payload[2:],
        dict(os.environ),
    )


def _canonical_descriptor(value: str) -> int:
    if not value or not value.isascii() or not value.isdecimal():
        raise ValueError("owned subprocess descriptor is invalid")
    descriptor = int(value)
    if descriptor < 3 or str(descriptor) != value:
        raise ValueError("owned subprocess descriptor is invalid")
    return descriptor


def _canonical_agent_socket_path(path: str) -> str:
    if sys.platform != "darwin":
        return path
    for alias in ("/tmp", "/var"):
        if path.startswith(f"{alias}/"):
            return f"/private{path}"
    return path


def _require_root_owned_directory(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise ValueError("system executable ancestor is not trusted")


def _require_root_owned_executable(metadata: os.stat_result) -> None:
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or mode & 0o022
        or not mode & 0o111
    ):
        raise ValueError("system executable metadata is not trusted")


def _require_agent_directory(metadata: os.stat_result) -> None:
    mode = stat.S_IMODE(metadata.st_mode)
    owner = metadata.st_uid
    if not stat.S_ISDIR(metadata.st_mode) or owner not in {0, os.geteuid()}:
        raise ValueError("SSH agent socket ancestor is not trusted")
    if mode & 0o022 and not (owner == 0 and mode & stat.S_ISVTX):
        raise ValueError("SSH agent socket ancestor is not trusted")


def _require_agent_socket(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise ValueError("SSH agent socket identity is invalid")


def _executable_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _agent_directory_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
    )


def _agent_socket_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_nlink,
        metadata.st_ctime_ns,
    )


__all__ = (
    "RSYNC_EXECUTABLE",
    "OWNED_SUBPROCESS_BIRTH_ARGUMENT",
    "SSH_EXECUTABLE",
    "SSH_KEYSCAN_EXECUTABLE",
    "VerifiedSshAgentSocket",
    "VerifiedSystemExecutable",
    "closed_ssh_environment",
    "run_packaged_owned_subprocess_birth",
)
