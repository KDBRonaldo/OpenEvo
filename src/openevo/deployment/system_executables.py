from __future__ import annotations

from collections.abc import Callable
import ctypes
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import secrets
import select
import selectors
import socket
import stat
import struct
import sys
import threading
import time
from typing import TypeVar


SSH_EXECUTABLE = "/usr/bin/ssh"
SSH_KEYGEN_EXECUTABLE = "/usr/bin/ssh-keygen"
SSH_KEYSCAN_EXECUTABLE = "/usr/bin/ssh-keyscan"
RSYNC_EXECUTABLE = "/usr/bin/rsync"
OWNED_SUBPROCESS_BIRTH_ARGUMENT = "--openevo-owned-subprocess-birth-v1"
MACOS_SYSTEM_COMMAND_PATH = ":".join(
    (
        "/usr/local/bin",
        "/System/Cryptexes/App/usr/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    )
)

_ALLOWED_EXECUTABLES = frozenset(
    {
        SSH_EXECUTABLE,
        SSH_KEYGEN_EXECUTABLE,
        SSH_KEYSCAN_EXECUTABLE,
        RSYNC_EXECUTABLE,
    }
)
_MAX_AUTHORITY_PATH_BYTES = 4096
_MAX_AUTHORITY_PATH_COMPONENTS = 64
_AGENT_CONNECT_TIMEOUT_SECONDS = 2.0
_AGENT_PROXY_ACCEPT_TIMEOUT_SECONDS = 15.0
_AGENT_PROXY_JOIN_TIMEOUT_SECONDS = 2.0
_AGENT_PROXY_BUFFER_BYTES = 256 * 1024
_AGENT_PROXY_CHUNK_BYTES = 64 * 1024
_AGENT_PROXY_CREATE_ATTEMPTS = 32
_AGENT_PROXY_RANDOM_BYTES = 12
_MAX_PENDING_AGENT_PROXY_CLEANUPS = 32
_DARWIN_SOL_LOCAL = getattr(socket, "SOL_LOCAL", 0)
_DARWIN_LOCAL_PEERPID = getattr(socket, "LOCAL_PEERPID", 0x002)
_DARWIN_LOCAL_PEERTOKEN = getattr(socket, "LOCAL_PEERTOKEN", 0x006)
_DARWIN_PROCESS_ID = struct.Struct("=i")
_DARWIN_AUDIT_TOKEN = struct.Struct("=8I")
_LINUX_INOTIFY_ATTRIB = 0x00000004
_LINUX_INOTIFY_DELETE_SELF = 0x00000400
_LINUX_INOTIFY_MOVE_SELF = 0x00000800
_LINUX_INOTIFY_UNMOUNT = 0x00002000
_LINUX_INOTIFY_Q_OVERFLOW = 0x00004000
_LINUX_INOTIFY_IGNORED = 0x00008000
_LINUX_INOTIFY_DONT_FOLLOW = 0x02000000
_LINUX_INOTIFY_TARGET_MASK = (
    _LINUX_INOTIFY_ATTRIB
    | _LINUX_INOTIFY_DELETE_SELF
    | _LINUX_INOTIFY_MOVE_SELF
    | _LINUX_INOTIFY_UNMOUNT
)
_LINUX_INOTIFY_EVENT = struct.Struct("iIII")
_MAX_INOTIFY_OBSERVATION_BYTES = 1024 * 1024
_SSH_AGENT_AUTHORITY_FAILURE = "SSH agent authority validation failed."

_AGENT_PROXY_SETUP_GUARD = threading.Lock()
_PENDING_AGENT_PROXY_CLEANUPS: dict[
    int,
    _PendingProxyCleanup | _PendingProxyNameCleanup,
] = {}

_ExecutableIdentity = tuple[int, int, int, int, int, int, int, int]
_DirectoryIdentity = tuple[int, int, int, int]
_SocketIdentity = tuple[int, int, int, int, int, int]
_AuthorityResult = TypeVar("_AuthorityResult")


@dataclass(frozen=True, slots=True)
class _UnixPeerAuthority:
    process_id: int
    user_id: int
    audit_token: bytes | None

    @property
    def process(self) -> tuple[int, int]:
        return self.process_id, self.user_id


class SshAgentAuthorityError(RuntimeError):
    """A fixed, path-free SSH agent authority failure."""

    def __init__(self) -> None:
        super().__init__(_SSH_AGENT_AUTHORITY_FAILURE)


def _run_agent_authority_operation(
    operation: Callable[[], _AuthorityResult],
) -> _AuthorityResult:
    try:
        return operation()
    except Exception:
        pass
    # Raise after leaving the handler so Python cannot retain the rejected exception.
    raise SshAgentAuthorityError() from None


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
        identity: _ExecutableIdentity,
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
            or len(candidate.parts) > _MAX_AUTHORITY_PATH_COMPONENTS
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
        return _verified_execution_path(self._path, self._descriptor)

    @property
    def display_path(self) -> str:
        return self._path

    @property
    def identity(self) -> _ExecutableIdentity:
        return self._identity

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
        directories: tuple[tuple[int, str | None, _DirectoryIdentity], ...],
        identity: _SocketIdentity,
    ) -> None:
        self._path = path
        self._name = name
        self._directories = directories
        self._identity = identity

    @classmethod
    def open(cls, path: str) -> VerifiedSshAgentSocket:
        return _run_agent_authority_operation(lambda: cls._open(path))

    @classmethod
    def _open(cls, path: str) -> VerifiedSshAgentSocket:
        path = _canonical_agent_socket_path(path)
        encoded = os.fsencode(path)
        candidate = Path(path)
        if (
            not candidate.is_absolute()
            or not encoded
            or len(encoded) > _MAX_AUTHORITY_PATH_BYTES
            or b"\x00" in encoded
            or len(candidate.parts) > _MAX_AUTHORITY_PATH_COMPONENTS
            or any(part in {"", ".", ".."} for part in candidate.parts[1:])
        ):
            raise ValueError("SSH agent socket path is invalid")

        directories: list[tuple[int, str | None, _DirectoryIdentity]] = []
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

    @property
    def identity(self) -> _SocketIdentity:
        return self._identity

    def verify_path_binding(self) -> None:
        _run_agent_authority_operation(self._verify_path_binding)

    def _verify_path_binding(self) -> None:
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

    def connect(
        self,
        *,
        expected_peer: tuple[int, int] | None = None,
    ) -> tuple[socket.socket, tuple[int, int]]:
        upstream, peer = _run_agent_authority_operation(
            lambda: self._connect_authority(expected_process=expected_peer)
        )
        return upstream, peer.process

    def _connect_authority(
        self,
        *,
        expected_peer: _UnixPeerAuthority | None = None,
        expected_process: tuple[int, int] | None = None,
    ) -> tuple[socket.socket, _UnixPeerAuthority]:
        if expected_peer is not None and expected_process is not None:
            raise ValueError("SSH agent socket peer authority is ambiguous")
        mutation_monitor = _AgentTargetMutationMonitor.open(
            self._directories,
            socket_name=self._name,
            socket_identity=self._identity,
        )
        upstream: socket.socket | None = None
        try:
            self._verify_path_binding()
            upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                upstream.settimeout(_AGENT_CONNECT_TIMEOUT_SECONDS)
                upstream.connect(self._path)
                self._verify_path_binding()
                # The second drain catches events queued during the first exact recheck.
                for _ in range(2):
                    target_mutated = mutation_monitor.observed_target_mutation()
                    self._verify_path_binding()
                    if target_mutated:
                        raise ValueError("SSH agent socket target changed during connect")
                peer = _unix_peer_authority(upstream)
                if peer.user_id != os.geteuid():
                    raise ValueError("SSH agent socket peer identity changed")
                if expected_peer is not None and peer != expected_peer:
                    raise ValueError("SSH agent socket peer identity changed")
                if expected_process is not None and peer.process != expected_process:
                    raise ValueError("SSH agent socket peer identity changed")
                self._verify_path_binding()
                if mutation_monitor.observed_target_mutation():
                    self._verify_path_binding()
                    raise ValueError("SSH agent socket target changed during connect")
                self._verify_path_binding()
                upstream.settimeout(None)
                return upstream, peer
            except BaseException:
                upstream.close()
                upstream = None
                raise
        finally:
            try:
                mutation_monitor.close()
            except BaseException:
                if upstream is not None:
                    upstream.close()
                raise

    def close(self) -> None:
        directories, self._directories = self._directories, ()
        for descriptor, _name, _identity in reversed(directories):
            os.close(descriptor)

    def __enter__(self) -> VerifiedSshAgentSocket:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()


class SshAgentSocketSource:
    """A redacted source from which each child gets a fresh private relay."""

    __slots__ = ("_identity", "_path", "_peer")

    def __init__(
        self,
        path: str,
        identity: _SocketIdentity,
        peer: _UnixPeerAuthority,
    ) -> None:
        self._path = path
        self._identity = identity
        self._peer = peer

    @classmethod
    def from_environment(cls, authentication_method: str) -> SshAgentSocketSource | None:
        if authentication_method != "ssh_agent":
            return None
        return _run_agent_authority_operation(cls._from_environment)

    @classmethod
    def _from_environment(cls) -> SshAgentSocketSource | None:
        socket_path = os.environ.get("SSH_AUTH_SOCK")
        if socket_path is None:
            return None
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
            canonical_path = authority.path
            identity = authority.identity
            baseline, peer = authority._connect_authority()
            baseline.close()
        return cls(canonical_path, identity, peer)

    def open_proxy(self) -> SshAgentProxy:
        return _run_agent_authority_operation(self._open_proxy)

    def _open_proxy(self) -> SshAgentProxy:
        authority = VerifiedSshAgentSocket.open(self._path)
        try:
            if authority.identity != self._identity:
                raise ValueError("SSH agent socket source identity changed")
            return SshAgentProxy.open_authority(
                authority,
                expected_upstream_peer=self._peer,
            )
        except BaseException:
            authority.close()
            raise

    def __repr__(self) -> str:
        return "SshAgentSocketSource(<redacted>)"


def _linux_inotify_payload_has_target_mutation(
    payload: bytes,
    target_kinds: dict[int, bool],
) -> bool:
    offset = 0
    while offset < len(payload):
        header_end = offset + _LINUX_INOTIFY_EVENT.size
        if header_end > len(payload):
            return True
        watch, mask, _cookie, name_length = _LINUX_INOTIFY_EVENT.unpack_from(
            payload,
            offset,
        )
        event_end = header_end + name_length
        if event_end > len(payload):
            return True
        if mask & (_LINUX_INOTIFY_Q_OVERFLOW | _LINUX_INOTIFY_IGNORED):
            return True
        socket_target = target_kinds.get(watch)
        if socket_target is None:
            return True
        name = payload[header_end:event_end].rstrip(b"\x00")
        if socket_target or not name:
            return True
        offset = event_end
    return False


class _AgentTargetMutationMonitor:
    __slots__ = (
        "_darwin_parent_descriptor",
        "_darwin_queue",
        "_descriptor",
        "_linux_target_kinds",
        "_max_events",
    )

    def __init__(
        self,
        *,
        descriptor: int = -1,
        darwin_queue: object | None = None,
        darwin_parent_descriptor: int = -1,
        linux_target_kinds: dict[int, bool] | None = None,
        max_events: int = 0,
    ) -> None:
        self._descriptor = descriptor
        self._darwin_queue = darwin_queue
        self._darwin_parent_descriptor = darwin_parent_descriptor
        self._linux_target_kinds = linux_target_kinds or {}
        self._max_events = max_events

    @classmethod
    def open(
        cls,
        directories: tuple[tuple[int, str | None, _DirectoryIdentity], ...],
        *,
        socket_name: str,
        socket_identity: _SocketIdentity,
    ) -> _AgentTargetMutationMonitor:
        if not directories:
            raise RuntimeError("SSH agent socket directory authority is unavailable")
        if sys.platform.startswith("linux"):
            return cls._open_linux(
                directories,
                socket_name=socket_name,
            )
        if sys.platform == "darwin":
            return cls._open_darwin(directories)
        raise RuntimeError("SSH agent socket mutation monitoring is unsupported")

    @classmethod
    def _open_linux(
        cls,
        directories: tuple[tuple[int, str | None, _DirectoryIdentity], ...],
        *,
        socket_name: str,
    ) -> _AgentTargetMutationMonitor:
        libc = ctypes.CDLL(None, use_errno=True)
        inotify_init1 = libc.inotify_init1
        inotify_init1.argtypes = [ctypes.c_int]
        inotify_init1.restype = ctypes.c_int
        inotify_add_watch = libc.inotify_add_watch
        inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        inotify_add_watch.restype = ctypes.c_int
        descriptor = inotify_init1(os.O_CLOEXEC | os.O_NONBLOCK)
        if descriptor < 0:
            error = ctypes.get_errno()
            raise OSError(error, "SSH agent socket mutation monitor is unavailable")
        target_kinds: dict[int, bool] = {}
        try:
            for held, _name, _identity in directories[1:]:
                watch = inotify_add_watch(
                    descriptor,
                    os.fsencode(f"/proc/self/fd/{held}"),
                    _LINUX_INOTIFY_TARGET_MASK,
                )
                if watch < 0:
                    error = ctypes.get_errno()
                    raise OSError(
                        error,
                        "SSH agent socket mutation monitor is unavailable",
                    )
                target_kinds[watch] = False
            parent = directories[-1][0]
            socket_watch = inotify_add_watch(
                descriptor,
                os.fsencode(f"/proc/self/fd/{parent}/{socket_name}"),
                _LINUX_INOTIFY_TARGET_MASK | _LINUX_INOTIFY_DONT_FOLLOW,
            )
            if socket_watch < 0:
                error = ctypes.get_errno()
                raise OSError(
                    error,
                    "SSH agent socket mutation monitor is unavailable",
                )
            target_kinds[socket_watch] = True
        except BaseException:
            os.close(descriptor)
            raise
        return cls(descriptor=descriptor, linux_target_kinds=target_kinds)

    @classmethod
    def _open_darwin(
        cls,
        directories: tuple[tuple[int, str | None, _DirectoryIdentity], ...],
    ) -> _AgentTargetMutationMonitor:
        watched_directories = directories[1:]
        if not watched_directories:
            raise RuntimeError("SSH agent socket mutation monitoring is unsupported")
        kqueue: object | None = None
        try:
            kqueue = select.kqueue()
            directory_flags = (
                select.KQ_NOTE_ATTRIB
                | select.KQ_NOTE_DELETE
                | select.KQ_NOTE_RENAME
                | select.KQ_NOTE_REVOKE
            )
            parent_descriptor = watched_directories[-1][0]
            changes = []
            for held, _name, _identity in watched_directories:
                flags = directory_flags
                if held == parent_descriptor:
                    flags |= select.KQ_NOTE_WRITE
                changes.append(
                    select.kevent(
                        held,
                        filter=select.KQ_FILTER_VNODE,
                        flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                        fflags=flags,
                    )
                )
            kqueue.control(changes, 0, 0)
        except BaseException:
            if kqueue is not None:
                kqueue.close()
            raise
        return cls(
            darwin_queue=kqueue,
            darwin_parent_descriptor=parent_descriptor,
            max_events=len(changes),
        )

    def _darwin_observed_target_mutation(self) -> bool:
        events = self._darwin_queue.control(None, self._max_events, 0)
        for event in events:
            if event.flags & select.KQ_EV_ERROR:
                return True
            if event.ident != self._darwin_parent_descriptor:
                return True
            hard_mutation_flags = (
                select.KQ_NOTE_DELETE
                | select.KQ_NOTE_RENAME
                | select.KQ_NOTE_REVOKE
            )
            if event.fflags & hard_mutation_flags:
                return True
            # Creating and removing a child directory can coalesce NOTE_ATTRIB
            # with NOTE_WRITE on Darwin because the parent's link count changes.
            # The caller immediately revalidates the held directory chain and
            # exact socket identity after every accepted namespace-write event.
            if not event.fflags & select.KQ_NOTE_WRITE:
                return True
        return False

    def observed_target_mutation(self) -> bool:
        if self._descriptor >= 0:
            observed = 0
            while True:
                try:
                    payload = os.read(self._descriptor, 64 * 1024)
                except BlockingIOError:
                    return False
                observed += len(payload)
                if (
                    not payload
                    or observed > _MAX_INOTIFY_OBSERVATION_BYTES
                    or _linux_inotify_payload_has_target_mutation(
                        payload,
                        self._linux_target_kinds,
                    )
                ):
                    return True
        if self._darwin_queue is None:
            raise RuntimeError("SSH agent socket mutation monitor is unavailable")
        return self._darwin_observed_target_mutation()

    def close(self) -> None:
        descriptor, self._descriptor = self._descriptor, -1
        if descriptor >= 0:
            os.close(descriptor)
        darwin_queue, self._darwin_queue = self._darwin_queue, None
        if darwin_queue is not None:
            darwin_queue.close()

    def __repr__(self) -> str:
        return "_AgentTargetMutationMonitor(<redacted>)"


class _PendingProxyNameCleanup:
    """Retain parent authority when a newly made root could not be opened."""

    __slots__ = ("_directories", "_identity", "_name")

    def __init__(
        self,
        *,
        directories: tuple[tuple[int, str | None, _DirectoryIdentity], ...],
        name: str,
        identity: _DirectoryIdentity | None,
    ) -> None:
        self._directories = directories
        self._name = name
        self._identity = identity

    def cleanup(self) -> None:
        if not self._directories:
            return
        _verify_held_directory_chain(self._directories, private_leaf=False)
        parent_descriptor = self._directories[-1][0]
        try:
            metadata = os.stat(
                self._name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            self._close()
            return
        _require_private_proxy_directory(metadata)
        if self._identity is None or _agent_directory_identity(metadata) != self._identity:
            raise RuntimeError("pending SSH agent proxy root identity is unknown")
        descriptor = os.open(
            self._name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_descriptor,
        )
        try:
            opened = os.fstat(descriptor)
            _require_private_proxy_directory(opened)
            if _agent_directory_identity(opened) != self._identity:
                raise RuntimeError("pending SSH agent proxy root identity changed")
            if not _directory_is_empty(descriptor):
                raise RuntimeError("pending SSH agent proxy root is not empty")
            final = os.stat(
                self._name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            _require_private_proxy_directory(final)
            if _agent_directory_identity(final) != self._identity:
                raise RuntimeError("pending SSH agent proxy root path binding changed")
            os.rmdir(self._name, dir_fd=parent_descriptor)
        finally:
            os.close(descriptor)
        self._close()

    def _close(self) -> None:
        directories, self._directories = self._directories, ()
        for descriptor, _name, _identity in reversed(directories):
            os.close(descriptor)

    def __repr__(self) -> str:
        return "_PendingProxyNameCleanup(<redacted>)"


class _PrivateProxyRoot:
    __slots__ = ("_directories", "_name", "_path", "_removed")

    def __init__(
        self,
        *,
        path: str,
        name: str,
        directories: tuple[tuple[int, str | None, _DirectoryIdentity], ...],
    ) -> None:
        self._path = path
        self._name = name
        self._directories = directories
        self._removed = False

    @classmethod
    def create(cls) -> _PrivateProxyRoot:
        parent_path = _agent_proxy_parent_path()
        directories = list(_open_held_directory_chain(parent_path))
        parent_descriptor = directories[-1][0]
        try:
            for _attempt in range(_AGENT_PROXY_CREATE_ATTEMPTS):
                name = f"openevo-agent-{secrets.token_hex(_AGENT_PROXY_RANDOM_BYTES)}"
                try:
                    os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
                except FileExistsError:
                    continue
                descriptor = -1
                root: _PrivateProxyRoot | None = None
                created_identity: _DirectoryIdentity | None = None
                try:
                    created_metadata = os.stat(
                        name,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                    _require_private_proxy_directory(created_metadata)
                    created_identity = _agent_directory_identity(created_metadata)
                    descriptor = os.open(
                        name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=parent_descriptor,
                    )
                    metadata = os.fstat(descriptor)
                    _require_private_proxy_directory(metadata)
                    identity = _agent_directory_identity(metadata)
                    if identity != created_identity:
                        raise ValueError("private SSH agent proxy root changed during open")
                    directories.append((descriptor, name, identity))
                    root = cls(
                        path=f"{parent_path}/{name}",
                        name=name,
                        directories=tuple(directories),
                    )
                    path_metadata = os.stat(
                        name,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                    _require_private_proxy_directory(path_metadata)
                    if _agent_directory_identity(path_metadata) != identity:
                        raise ValueError("private SSH agent proxy root changed during open")
                    root.verify_path_binding()
                except BaseException:
                    if root is None:
                        if descriptor >= 0:
                            os.close(descriptor)
                        cleanup = _PendingProxyNameCleanup(
                            directories=tuple(directories),
                            name=name,
                            identity=created_identity,
                        )
                        try:
                            cleanup.cleanup()
                        except BaseException:
                            _PENDING_AGENT_PROXY_CLEANUPS[id(cleanup)] = cleanup
                    else:
                        cleanup = _PendingProxyCleanup(
                            root=root,
                            name=None,
                            socket_identity=None,
                        )
                        try:
                            cleanup.cleanup()
                        except BaseException:
                            _PENDING_AGENT_PROXY_CLEANUPS[id(cleanup)] = cleanup
                    directories = []
                    raise
                return root
            raise RuntimeError("private SSH agent proxy root capacity is exhausted")
        except BaseException:
            for descriptor, _name, _identity in reversed(directories):
                os.close(descriptor)
            raise

    @property
    def descriptor(self) -> int:
        return self._directories[-1][0]

    @property
    def path(self) -> str:
        return self._path

    def verify_path_binding(self) -> None:
        _verify_held_directory_chain(self._directories, private_leaf=True)

    def remove(self) -> None:
        if self._removed:
            return
        self.verify_path_binding()
        if not _directory_is_empty(self.descriptor):
            raise RuntimeError("private SSH agent proxy root is not empty")
        parent_descriptor = self._directories[-2][0]
        metadata = os.stat(
            self._name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if _agent_directory_identity(metadata) != self._directories[-1][2]:
            raise RuntimeError("private SSH agent proxy root path binding changed")
        os.rmdir(self._name, dir_fd=parent_descriptor)
        self._removed = True

    def close(self) -> None:
        if not self._removed:
            raise RuntimeError("private SSH agent proxy root has not been removed")
        directories, self._directories = self._directories, ()
        for descriptor, _name, _identity in reversed(directories):
            os.close(descriptor)


class _PendingProxyCleanup:
    __slots__ = ("_name", "_root", "_socket_identity")

    def __init__(
        self,
        *,
        root: _PrivateProxyRoot,
        name: str | None,
        socket_identity: tuple[int, int, int, int] | None,
    ) -> None:
        self._root = root
        self._name = name
        self._socket_identity = socket_identity

    def cleanup(self) -> None:
        self._root.verify_path_binding()
        if self._name is not None:
            try:
                metadata = os.stat(
                    self._name,
                    dir_fd=self._root.descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                _require_owned_proxy_socket(metadata)
                if (
                    self._socket_identity is None
                    or _proxy_socket_cleanup_identity(metadata) != self._socket_identity
                ):
                    raise RuntimeError("pending SSH agent proxy socket identity is unknown")
                os.unlink(self._name, dir_fd=self._root.descriptor)
        self._root.remove()
        self._root.close()

    def __repr__(self) -> str:
        return "_PendingProxyCleanup(<redacted>)"


class SshAgentProxy:
    """One-shot relay from one owned SSH session to one preconnected agent FD."""

    __slots__ = (
        "_cleanup_guard",
        "_closed",
        "_downstream",
        "_expected_executable",
        "_expected_process_group",
        "_expected_session",
        "_guard",
        "_listener",
        "_listener_identity",
        "_name",
        "_root",
        "_stop",
        "_thread",
        "_upstream",
        "_upstream_authority",
    )

    def __init__(
        self,
        *,
        upstream: socket.socket,
        upstream_authority: VerifiedSshAgentSocket,
        root: _PrivateProxyRoot,
        listener: socket.socket,
        name: str,
        listener_identity: _SocketIdentity,
    ) -> None:
        self._upstream = upstream
        self._upstream_authority = upstream_authority
        self._root = root
        self._listener = listener
        self._name = name
        self._listener_identity = listener_identity
        self._downstream: socket.socket | None = None
        self._closed = False
        self._expected_session: int | None = None
        self._expected_process_group: int | None = None
        self._expected_executable: _ExecutableIdentity | None = None
        self._guard = threading.Lock()
        self._cleanup_guard = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @classmethod
    def open(cls, upstream_path: str) -> SshAgentProxy:
        upstream_authority = VerifiedSshAgentSocket.open(upstream_path)
        try:
            return cls.open_authority(upstream_authority)
        except BaseException:
            upstream_authority.close()
            raise

    @classmethod
    def open_authority(
        cls,
        upstream_authority: VerifiedSshAgentSocket,
        *,
        expected_upstream_peer: _UnixPeerAuthority | tuple[int, int] | None = None,
    ) -> SshAgentProxy:
        with _AGENT_PROXY_SETUP_GUARD:
            _retry_pending_proxy_cleanups()
            if len(_PENDING_AGENT_PROXY_CLEANUPS) >= _MAX_PENDING_AGENT_PROXY_CLEANUPS:
                raise RuntimeError("SSH agent proxy cleanup capacity is exhausted")
            return cls._open_authority_locked(
                upstream_authority,
                expected_upstream_peer=expected_upstream_peer,
            )

    @classmethod
    def _open_authority_locked(
        cls,
        upstream_authority: VerifiedSshAgentSocket,
        *,
        expected_upstream_peer: _UnixPeerAuthority | tuple[int, int] | None,
    ) -> SshAgentProxy:
        upstream: socket.socket | None = None
        root: _PrivateProxyRoot | None = None
        listener: socket.socket | None = None
        name: str | None = None
        cleanup_identity: tuple[int, int, int, int] | None = None
        try:
            upstream, _peer = upstream_authority._connect_authority(
                expected_peer=(
                    expected_upstream_peer
                    if isinstance(expected_upstream_peer, _UnixPeerAuthority)
                    else None
                ),
                expected_process=(
                    expected_upstream_peer if isinstance(expected_upstream_peer, tuple) else None
                ),
            )
            root = _PrivateProxyRoot.create()
            name = f"agent-{secrets.token_hex(_AGENT_PROXY_RANDOM_BYTES)}.sock"
            proxy_path = f"{root.path}/{name}"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            root.verify_path_binding()
            listener.bind(proxy_path)
            bound_metadata = os.stat(name, dir_fd=root.descriptor, follow_symlinks=False)
            _require_owned_proxy_socket(bound_metadata)
            cleanup_identity = _proxy_socket_cleanup_identity(bound_metadata)
            os.chmod(name, 0o600, dir_fd=root.descriptor, follow_symlinks=False)
            metadata = os.stat(name, dir_fd=root.descriptor, follow_symlinks=False)
            _require_private_proxy_socket(metadata)
            if _proxy_socket_cleanup_identity(metadata) != cleanup_identity:
                raise RuntimeError("private SSH agent proxy socket changed during setup")
            listener_identity = _agent_socket_identity(metadata)
            root.verify_path_binding()
            upstream_authority.verify_path_binding()
            listener.listen(4)
            listener.settimeout(0.1)
            return cls(
                upstream=upstream,
                upstream_authority=upstream_authority,
                root=root,
                listener=listener,
                name=name,
                listener_identity=listener_identity,
            )
        except BaseException:
            if listener is not None:
                listener.close()
            if root is not None:
                cleanup = _PendingProxyCleanup(
                    root=root,
                    name=name,
                    socket_identity=cleanup_identity,
                )
                try:
                    cleanup.cleanup()
                except BaseException:
                    _PENDING_AGENT_PROXY_CLEANUPS[id(cleanup)] = cleanup
            if upstream is not None:
                upstream.close()
            upstream_authority.close()
            raise

    @property
    def socket_path(self) -> str:
        return f"{self._root.path}/{self._name}"

    def verify_upstream_binding(self) -> None:
        self._upstream_authority.verify_path_binding()
        self._root.verify_path_binding()
        metadata = os.stat(
            self._name,
            dir_fd=self._root.descriptor,
            follow_symlinks=False,
        )
        _require_private_proxy_socket(metadata)
        if _agent_socket_identity(metadata) != self._listener_identity:
            raise RuntimeError("private SSH agent proxy socket binding changed")

    def bind_child(
        self,
        *,
        session_id: int,
        process_group_id: int,
        executable_identity: _ExecutableIdentity,
    ) -> None:
        if session_id <= 1 or process_group_id <= 1:
            raise RuntimeError("SSH agent proxy child authority is invalid")
        with self._guard:
            if self._thread is not None:
                raise RuntimeError("SSH agent proxy child authority is already bound")
            self._expected_session = session_id
            self._expected_process_group = process_group_id
            self._expected_executable = executable_identity
            thread = threading.Thread(
                target=self._serve,
                name="openevo-ssh-agent-proxy",
                daemon=True,
            )
            self._thread = thread
            thread.start()

    def _serve(self) -> None:
        deadline = time.monotonic() + _AGENT_PROXY_ACCEPT_TIMEOUT_SECONDS
        try:
            while not self._stop.is_set() and time.monotonic() < deadline:
                with self._guard:
                    listener = self._listener
                if listener is None:
                    return
                try:
                    downstream, _address = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                if not self._authorize_downstream(downstream):
                    downstream.close()
                    continue
                with self._guard:
                    upstream = self._upstream
                    if upstream is None:
                        downstream.close()
                        return
                    self._downstream = downstream
                self._close_listener_path()
                _relay_agent_streams(
                    downstream,
                    upstream,
                    stop=self._stop,
                )
                return
        finally:
            self._close_streams()
            try:
                self._close_listener_path()
            except BaseException:
                pass

    def _authorize_downstream(self, downstream: socket.socket) -> bool:
        session_id = self._expected_session
        process_group_id = self._expected_process_group
        executable_identity = self._expected_executable
        if session_id is None or process_group_id is None or executable_identity is None:
            return False
        try:
            peer = _unix_peer_authority(downstream)
            if peer.user_id != os.geteuid():
                return False
            if os.getsid(peer.process_id) != session_id:
                return False
            if os.getpgid(peer.process_id) != process_group_id:
                return False
            if _peer_executable_identity(peer.process_id) != executable_identity:
                return False
            return (
                _unix_peer_authority(downstream) == peer
                and os.getsid(peer.process_id) == session_id
                and os.getpgid(peer.process_id) == process_group_id
            )
        except (OSError, RuntimeError, ValueError):
            return False

    def _close_listener_path(self) -> None:
        with self._guard:
            listener, self._listener = self._listener, None
            if listener is not None:
                listener.close()
            self._root.verify_path_binding()
            try:
                metadata = os.stat(
                    self._name,
                    dir_fd=self._root.descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return
            _require_private_proxy_socket(metadata)
            if _agent_socket_identity(metadata) != self._listener_identity:
                raise RuntimeError("private SSH agent proxy socket path binding changed")
            os.unlink(self._name, dir_fd=self._root.descriptor)

    def _close_streams(self) -> None:
        with self._guard:
            downstream, self._downstream = self._downstream, None
            upstream, self._upstream = self._upstream, None
        for stream in (downstream, upstream):
            if stream is None:
                continue
            try:
                stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            stream.close()

    def close(self) -> None:
        with self._cleanup_guard:
            if self._closed:
                return
            self._stop.set()
            self._close_streams()
            self._close_listener_path()
            thread = self._thread
            if thread is not None and thread is not threading.current_thread():
                thread.join(_AGENT_PROXY_JOIN_TIMEOUT_SECONDS)
                if thread.is_alive():
                    raise RuntimeError("SSH agent proxy worker did not stop")
            self._root.remove()
            self._root.close()
            self._upstream_authority.close()
            self._closed = True

    def __repr__(self) -> str:
        return "SshAgentProxy(<redacted>)"


def closed_ssh_environment(authentication_method: str) -> dict[str, str]:
    """Validate agent discovery while returning a closed ordinary environment."""

    SshAgentSocketSource.from_environment(authentication_method)
    return {}


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
    environment: dict[str, str] = {}
    agent_socket = os.environ.get("SSH_AUTH_SOCK")
    if agent_socket is not None:
        environment["SSH_AUTH_SOCK"] = agent_socket
    execution_path = _verified_execution_path(payload[2], executable_descriptor)
    os.execve(
        execution_path,
        payload[2:],
        environment,
    )


def _verified_execution_path(path: str, descriptor: int) -> str:
    if sys.platform.startswith("linux"):
        return f"/dev/fd/{descriptor}"
    if sys.platform == "darwin":
        return path
    raise OSError(errno.ENOTSUP, "verified system execution is unsupported")


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
        or metadata.st_nlink != 1
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


def _executable_identity(metadata: os.stat_result) -> _ExecutableIdentity:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _agent_directory_identity(metadata: os.stat_result) -> _DirectoryIdentity:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
    )


def _agent_socket_identity(metadata: os.stat_result) -> _SocketIdentity:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_nlink,
        metadata.st_ctime_ns,
    )


def _agent_proxy_parent_path() -> str:
    return "/private/tmp" if sys.platform == "darwin" else "/tmp"


def _open_held_directory_chain(
    path: str,
) -> tuple[tuple[int, str | None, _DirectoryIdentity], ...]:
    candidate = Path(path)
    encoded = os.fsencode(path)
    if (
        not candidate.is_absolute()
        or not encoded
        or len(encoded) > _MAX_AUTHORITY_PATH_BYTES
        or b"\x00" in encoded
        or len(candidate.parts) > _MAX_AUTHORITY_PATH_COMPONENTS
        or any(part in {"", ".", ".."} for part in candidate.parts[1:])
    ):
        raise ValueError("private SSH agent proxy parent path is invalid")
    directories: list[tuple[int, str | None, _DirectoryIdentity]] = []
    current = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        root_metadata = os.fstat(current)
        _require_agent_directory(root_metadata)
        directories.append((current, None, _agent_directory_identity(root_metadata)))
        for component in candidate.parts[1:]:
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
                if _agent_directory_identity(path_metadata) != _agent_directory_identity(metadata):
                    raise ValueError("private SSH agent proxy parent changed during open")
            except BaseException:
                os.close(following)
                raise
            directories.append((following, component, _agent_directory_identity(metadata)))
            current = following
        return tuple(directories)
    except BaseException:
        for descriptor, _name, _identity in reversed(directories):
            os.close(descriptor)
        if not directories:
            os.close(current)
        raise


def _verify_held_directory_chain(
    directories: tuple[tuple[int, str | None, _DirectoryIdentity], ...],
    *,
    private_leaf: bool,
) -> None:
    if not directories:
        raise RuntimeError("private SSH agent proxy directory authority is unavailable")
    for index, (descriptor, name, identity) in enumerate(directories):
        metadata = os.fstat(descriptor)
        if private_leaf and index == len(directories) - 1:
            _require_private_proxy_directory(metadata)
        else:
            _require_agent_directory(metadata)
        if _agent_directory_identity(metadata) != identity:
            raise RuntimeError("private SSH agent proxy directory identity changed")
        if name is None:
            continue
        parent_descriptor = directories[index - 1][0]
        path_metadata = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if _agent_directory_identity(path_metadata) != identity:
            raise RuntimeError("private SSH agent proxy directory path binding changed")


def _require_private_proxy_directory(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ValueError("private SSH agent proxy directory identity is invalid")


def _directory_is_empty(descriptor: int) -> bool:
    with os.scandir(descriptor) as entries:
        return next(entries, None) is None


def _require_private_proxy_socket(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise ValueError("private SSH agent proxy socket identity is invalid")


def _require_owned_proxy_socket(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
    ):
        raise ValueError("SSH agent proxy socket ownership is invalid")


def _proxy_socket_cleanup_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_nlink,
    )


def _retry_pending_proxy_cleanups() -> None:
    for key, cleanup in tuple(_PENDING_AGENT_PROXY_CLEANUPS.items()):
        try:
            cleanup.cleanup()
        except BaseException:
            continue
        _PENDING_AGENT_PROXY_CLEANUPS.pop(key, None)


def _darwin_peer_authority(stream: socket.socket) -> _UnixPeerAuthority:
    libc = ctypes.CDLL(None, use_errno=True)
    getpeereid = libc.getpeereid
    getpeereid.argtypes = [
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    getpeereid.restype = ctypes.c_int
    user_id = ctypes.c_uint32()
    group_id = ctypes.c_uint32()
    ctypes.set_errno(0)
    if getpeereid(stream.fileno(), ctypes.byref(user_id), ctypes.byref(group_id)) != 0:
        error = ctypes.get_errno() or errno.EIO
        raise OSError(error, "SSH agent socket peer credentials are unavailable")

    process_payload = stream.getsockopt(
        _DARWIN_SOL_LOCAL,
        _DARWIN_LOCAL_PEERPID,
        _DARWIN_PROCESS_ID.size,
    )
    if not isinstance(process_payload, bytes) or len(process_payload) != _DARWIN_PROCESS_ID.size:
        raise ValueError("SSH agent socket peer PID is invalid")
    (process_id,) = _DARWIN_PROCESS_ID.unpack(process_payload)
    if process_id <= 0:
        raise ValueError("SSH agent socket peer PID is invalid")

    audit_token = stream.getsockopt(
        _DARWIN_SOL_LOCAL,
        _DARWIN_LOCAL_PEERTOKEN,
        _DARWIN_AUDIT_TOKEN.size,
    )
    if not isinstance(audit_token, bytes) or len(audit_token) != _DARWIN_AUDIT_TOKEN.size:
        raise ValueError("SSH agent socket peer audit token is invalid")
    token_fields = _DARWIN_AUDIT_TOKEN.unpack(audit_token)
    if (
        token_fields[1] != user_id.value
        or token_fields[2] != group_id.value
        or token_fields[5] != process_id
    ):
        raise ValueError("SSH agent socket peer audit token is invalid")
    return _UnixPeerAuthority(process_id, user_id.value, audit_token)


def _unix_peer_authority(stream: socket.socket) -> _UnixPeerAuthority:
    if sys.platform.startswith("linux"):
        credential_size = struct.calcsize("3i")
        payload = stream.getsockopt(
            socket.SOL_SOCKET,
            socket.SO_PEERCRED,
            credential_size,
        )
        process_id, user_id, _group_id = struct.unpack("3i", payload)
        return _UnixPeerAuthority(process_id, user_id, None)
    if sys.platform == "darwin":
        return _darwin_peer_authority(stream)
    raise RuntimeError("SSH agent proxy peer credentials are unsupported")


def _unix_peer_process(stream: socket.socket) -> tuple[int, int]:
    return _unix_peer_authority(stream).process


def _peer_executable_identity(process_id: int) -> _ExecutableIdentity:
    if sys.platform.startswith("linux"):
        metadata = os.stat(f"/proc/{process_id}/exe")
        _require_root_owned_executable(metadata)
        return _executable_identity(metadata)
    if sys.platform == "darwin":
        # libproc rejects buffers larger than PROC_PIDPATHINFO_MAXSIZE.
        buffer = ctypes.create_string_buffer(_MAX_AUTHORITY_PATH_BYTES)
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        proc_pidpath = libproc.proc_pidpath
        proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        proc_pidpath.restype = ctypes.c_int
        length = proc_pidpath(process_id, buffer, len(buffer))
        if length <= 0 or length > _MAX_AUTHORITY_PATH_BYTES:
            raise RuntimeError("SSH agent proxy peer executable is unavailable")
        encoded_path = buffer.raw[:length].split(b"\x00", 1)[0]
        if not encoded_path:
            raise RuntimeError("SSH agent proxy peer executable is unavailable")
        path = os.fsdecode(encoded_path)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            metadata = os.fstat(descriptor)
            _require_root_owned_executable(metadata)
            return _executable_identity(metadata)
        finally:
            os.close(descriptor)
    raise RuntimeError("SSH agent proxy peer executable verification is unsupported")


def _relay_agent_streams(
    downstream: socket.socket,
    upstream: socket.socket,
    *,
    stop: threading.Event,
) -> None:
    downstream.setblocking(False)
    upstream.setblocking(False)
    to_downstream = bytearray()
    to_upstream = bytearray()
    downstream_readable = True
    upstream_readable = True
    downstream_write_closed = False
    upstream_write_closed = False
    selector = selectors.DefaultSelector()
    try:
        while not stop.is_set():
            if not downstream_readable and not to_upstream and not upstream_write_closed:
                try:
                    upstream.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                upstream_write_closed = True
            if not upstream_readable and not to_downstream and not downstream_write_closed:
                try:
                    downstream.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                downstream_write_closed = True
            if (
                not downstream_readable
                and not upstream_readable
                and not to_downstream
                and not to_upstream
            ):
                return
            selector.close()
            selector = selectors.DefaultSelector()
            downstream_events = 0
            upstream_events = 0
            if downstream_readable and len(to_upstream) < _AGENT_PROXY_BUFFER_BYTES:
                downstream_events |= selectors.EVENT_READ
            if to_downstream:
                downstream_events |= selectors.EVENT_WRITE
            if upstream_readable and len(to_downstream) < _AGENT_PROXY_BUFFER_BYTES:
                upstream_events |= selectors.EVENT_READ
            if to_upstream:
                upstream_events |= selectors.EVENT_WRITE
            if downstream_events:
                selector.register(downstream, downstream_events, "downstream")
            if upstream_events:
                selector.register(upstream, upstream_events, "upstream")
            if not selector.get_map():
                return
            for key, events in selector.select(0.1):
                stream = key.fileobj
                if key.data == "downstream":
                    if events & selectors.EVENT_READ:
                        chunk = stream.recv(_AGENT_PROXY_CHUNK_BYTES)
                        if chunk:
                            to_upstream.extend(chunk)
                        else:
                            downstream_readable = False
                    if events & selectors.EVENT_WRITE:
                        _send_and_zero(stream, to_downstream)
                else:
                    if events & selectors.EVENT_READ:
                        chunk = stream.recv(_AGENT_PROXY_CHUNK_BYTES)
                        if chunk:
                            to_downstream.extend(chunk)
                        else:
                            upstream_readable = False
                    if events & selectors.EVENT_WRITE:
                        _send_and_zero(stream, to_upstream)
    except OSError:
        return
    finally:
        selector.close()
        _zero_bytearray(to_downstream)
        _zero_bytearray(to_upstream)


def _send_and_zero(stream: socket.socket, buffer: bytearray) -> None:
    sent = stream.send(buffer)
    if sent <= 0:
        raise OSError("SSH agent proxy forwarding stopped")
    buffer[:sent] = b"\x00" * sent
    del buffer[:sent]


def _zero_bytearray(buffer: bytearray) -> None:
    buffer[:] = b"\x00" * len(buffer)
    buffer.clear()


__all__ = (
    "RSYNC_EXECUTABLE",
    "OWNED_SUBPROCESS_BIRTH_ARGUMENT",
    "SSH_EXECUTABLE",
    "SSH_KEYSCAN_EXECUTABLE",
    "SshAgentAuthorityError",
    "SshAgentProxy",
    "SshAgentSocketSource",
    "VerifiedSshAgentSocket",
    "VerifiedSystemExecutable",
    "closed_ssh_environment",
    "run_packaged_owned_subprocess_birth",
)
