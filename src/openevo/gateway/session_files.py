"""Private, identity-pinned files owned by one gateway session."""

from __future__ import annotations

import errno
import ctypes
import fcntl
import hashlib
import json
import os
import secrets
import stat
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkdtemp
from typing import Callable, Final, TypeAlias


SessionRootIdentity: TypeAlias = tuple[int, int, int]
CredentialFileIdentity: TypeAlias = tuple[int, int, int, int, int, int, int, int]

_AUTH_MAX_BYTES: Final[int] = 1024 * 1024
_AUTH_JSON_MAX_DEPTH: Final[int] = 32
_AUTH_JSON_MAX_NODES: Final[int] = 4096
_AUTH_JSON_MAX_SECRETS: Final[int] = 512
_CAPTURE_REDACTION_MAX_BYTES: Final[int] = 4 * 1024 * 1024
_CAPTURE_REDACTION_MAX_TOTAL_BYTES: Final[int] = 64 * 1024 * 1024
_TRANSCRIPT_MAX_BYTES: Final[int] = 4 * 1024 * 1024
EXEC_LOG_MAX_BYTES: Final[int] = _TRANSCRIPT_MAX_BYTES
_CLEANUP_MAX_DEPTH: Final[int] = 64
_CLEANUP_MAX_NODES: Final[int] = 100_000
CAPTURE_REDACTION_LIMIT_MARKER: Final[str] = "[REDACTED: capture exceeded credential scan limit]"
_CREDENTIAL_REDACTION_MARKER: Final[bytes] = b"[REDACTED: Codex credential]"
_DIRECTORY_FLAGS: Final[int] = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
_PATH_FLAGS: Final[int] = getattr(os, "O_PATH", os.O_RDONLY) | os.O_CLOEXEC | os.O_NOFOLLOW
_LEAF_PIN_FLAGS: Final[int] = (
    getattr(os, "O_PATH", os.O_RDONLY | os.O_NONBLOCK) | os.O_CLOEXEC | os.O_NOFOLLOW
)
_RENAME_NOREPLACE: Final[int] = 1
_MFD_CLOEXEC: Final[int] = 0x0001
_MFD_ALLOW_SEALING: Final[int] = 0x0002
_F_ADD_SEALS: Final[int] = 1033
_F_GET_SEALS: Final[int] = 1034
_F_SEAL_SEAL: Final[int] = 0x0001
_F_SEAL_SHRINK: Final[int] = 0x0002
_F_SEAL_GROW: Final[int] = 0x0004
_F_SEAL_WRITE: Final[int] = 0x0008
_CREDENTIAL_SNAPSHOT_SEALS: Final[int] = (
    _F_SEAL_SEAL | _F_SEAL_SHRINK | _F_SEAL_GROW | _F_SEAL_WRITE
)
CODEX_CREDENTIAL_AUTHORITY_FD_ENV: Final[str] = "OPENEVO_CODEX_CREDENTIAL_AUTHORITY_FD"
CODEX_CREDENTIAL_SNAPSHOT_FD_ENV: Final[str] = "OPENEVO_CODEX_CREDENTIAL_SNAPSHOT_FD"


class SessionFileSecurityError(RuntimeError):
    """Raised when private session state cannot be handled without a path race."""


class HeldCodexCredentialAuthority:
    """Held auth file plus verified authority over its absolute pathname."""

    __slots__ = (
        "_closed",
        "_content_sha256",
        "_descriptor",
        "_identity",
        "_lock",
        "_path",
        "_pin",
    )

    def __init__(
        self,
        *,
        path: Path,
        descriptor: int,
        pin: _AbsoluteDirectoryPin,
        identity: CredentialFileIdentity,
        content_sha256: str,
    ) -> None:
        self._path = path
        self._descriptor = descriptor
        self._pin = pin
        self._identity = identity
        self._content_sha256 = content_sha256
        self._lock = threading.RLock()
        self._closed = False

    @classmethod
    def open(cls, path: Path) -> HeldCodexCredentialAuthority:
        absolute = Path(os.path.abspath(os.fspath(path)))
        if absolute.name in {"", ".", ".."}:
            raise SessionFileSecurityError("Codex subscription auth path is invalid")
        pin = _pin_absolute_directory(absolute.parent)
        descriptor = -1
        try:
            pin.verify(label="Codex subscription auth source")
            before = os.stat(absolute.name, dir_fd=pin.descriptor, follow_symlinks=False)
            _require_private_auth(before)
            descriptor = os.open(
                absolute.name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=pin.descriptor,
            )
            opened = os.fstat(descriptor)
            _require_private_auth(opened)
            if _auth_identity(before) != _auth_identity(opened):
                raise SessionFileSecurityError(
                    "Codex subscription auth changed while authority was acquired"
                )
            authority = cls(
                path=absolute,
                descriptor=descriptor,
                pin=pin,
                identity=_auth_identity(opened),
                content_sha256=_digest_fd(descriptor, opened.st_size),
            )
            authority.verify()
            return authority
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            pin.close()
            raise

    @classmethod
    def from_inherited_environment(
        cls,
        path: Path,
        *,
        required: bool,
    ) -> HeldCodexCredentialAuthority | None:
        raw_descriptor = os.environ.pop(CODEX_CREDENTIAL_AUTHORITY_FD_ENV, None)
        if raw_descriptor is None:
            if required:
                raise SessionFileSecurityError(
                    "release Gateway is missing Codex credential authority"
                )
            return None
        descriptor = -1
        try:
            descriptor = int(raw_descriptor)
            if descriptor < 3 or str(descriptor) != raw_descriptor:
                raise ValueError
            os.set_inheritable(descriptor, False)
            opened = os.fstat(descriptor)
            _require_private_auth(opened)
            absolute = Path(os.path.abspath(os.fspath(path)))
            pin = _pin_absolute_directory(absolute.parent)
        except (OSError, ValueError, SessionFileSecurityError) as exc:
            if descriptor >= 3:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise SessionFileSecurityError(
                "inherited Codex credential authority is invalid"
            ) from exc
        authority = cls(
            path=absolute,
            descriptor=descriptor,
            pin=pin,
            identity=_auth_identity(opened),
            content_sha256=_digest_fd(descriptor, opened.st_size),
        )
        try:
            authority.verify()
        except Exception:
            authority.close()
            raise
        return authority

    @property
    def identity(self) -> CredentialFileIdentity:
        return self._identity

    @property
    def content_sha256(self) -> str:
        return self._content_sha256

    def inheritance_descriptor(self) -> int:
        self.verify()
        return self._descriptor

    def duplicate_verified_descriptor(self) -> int:
        self.verify()
        duplicate = -1
        try:
            duplicate = os.dup(self._descriptor)
            os.set_inheritable(duplicate, False)
            if _auth_identity(os.fstat(duplicate)) != self._identity:
                raise SessionFileSecurityError("held Codex credential descriptor changed")
            return duplicate
        except BaseException:
            if duplicate >= 0:
                os.close(duplicate)
            raise

    def verify(self) -> None:
        with self._lock:
            if self._closed:
                raise SessionFileSecurityError("held Codex credential authority is closed")
            try:
                self._pin.verify(label="Codex subscription auth source")
                held = os.fstat(self._descriptor)
                if _auth_identity(held) != self._identity:
                    raise SessionFileSecurityError("held Codex subscription auth changed")
                _require_private_auth(held)
                if _digest_fd(self._descriptor, held.st_size) != self._content_sha256:
                    raise SessionFileSecurityError("held Codex subscription auth content changed")
                current = os.stat(
                    self._path.name,
                    dir_fd=self._pin.descriptor,
                    follow_symlinks=False,
                )
                if _auth_identity(current) != self._identity:
                    raise SessionFileSecurityError(
                        "Codex subscription auth pathname no longer matches readiness authority"
                    )
                _require_private_auth(current)
                self._pin.verify(label="Codex subscription auth source")
            except SessionFileSecurityError:
                raise
            except OSError as exc:
                raise SessionFileSecurityError(
                    "Codex subscription auth pathname authority is unavailable"
                ) from exc

    def prepare_snapshot(self) -> PreparedCodexCredentialSnapshot:
        """Commit an anonymous credential snapshot while pathname authority is current."""

        with self._lock:
            self.verify()
            descriptor = -1
            try:
                descriptor = _memfd_create("openevo-codex-auth")
                os.fchmod(descriptor, 0o600)
                source = os.fstat(self._descriptor)
                _copy_exact(self._descriptor, descriptor, source.st_size)
                os.fsync(descriptor)
                if _digest_fd(descriptor, source.st_size) != self._content_sha256:
                    raise SessionFileSecurityError(
                        "prepared Codex auth digest does not match readiness authority"
                    )
                self.verify()
                fcntl.fcntl(descriptor, _F_ADD_SEALS, _CREDENTIAL_SNAPSHOT_SEALS)
                if fcntl.fcntl(descriptor, _F_GET_SEALS) != _CREDENTIAL_SNAPSHOT_SEALS:
                    raise SessionFileSecurityError("prepared Codex auth snapshot is not sealed")
                snapshot = PreparedCodexCredentialSnapshot(
                    descriptor=descriptor,
                    size=source.st_size,
                    content_sha256=self._content_sha256,
                    source_identity=self._identity,
                    redactor=CredentialRedactor.from_auth_json(
                        _read_fd_exact(descriptor, source.st_size)
                    ),
                )
                snapshot.verify()
                # This final authority check is the linearization point. Once it
                # succeeds, the sealed snapshot owns this run's credential bytes;
                # later replacement of the original pathname is irrelevant.
                self.verify()
                descriptor = -1
                return snapshot
            except SessionFileSecurityError:
                raise
            except (OSError, ValueError) as exc:
                raise SessionFileSecurityError(
                    "Codex subscription auth could not be snapshotted safely"
                ) from exc
            finally:
                if descriptor >= 0:
                    os.close(descriptor)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                os.close(self._descriptor)
            finally:
                self._pin.close()


class PreparedCodexCredentialSnapshot:
    """Sealed anonymous auth bytes committed before session admission side effects."""

    __slots__ = (
        "_closed",
        "_content_sha256",
        "_descriptor",
        "_redactor",
        "_size",
        "_source_identity",
    )

    def __init__(
        self,
        *,
        descriptor: int,
        size: int,
        content_sha256: str,
        source_identity: CredentialFileIdentity,
        redactor: CredentialRedactor,
    ) -> None:
        self._descriptor = descriptor
        self._size = size
        self._content_sha256 = content_sha256
        self._source_identity = source_identity
        self._redactor = redactor
        self._closed = False

    @property
    def redactor(self) -> CredentialRedactor:
        return self._redactor

    @property
    def size(self) -> int:
        return self._size

    @property
    def content_sha256(self) -> str:
        return self._content_sha256

    @property
    def identity(self) -> CredentialFileIdentity:
        return self._source_identity

    @classmethod
    def from_inherited_environment(
        cls,
        *,
        required: bool,
    ) -> PreparedCodexCredentialSnapshot | None:
        """Adopt one sealed readiness snapshot inherited from the supervisor."""

        raw_descriptor = os.environ.pop(CODEX_CREDENTIAL_SNAPSHOT_FD_ENV, None)
        if raw_descriptor is None:
            if required:
                raise SessionFileSecurityError(
                    "release Gateway is missing the Codex credential snapshot"
                )
            return None
        descriptor = -1
        try:
            descriptor = int(raw_descriptor)
            if descriptor < 3 or str(descriptor) != raw_descriptor:
                raise ValueError
            os.set_inheritable(descriptor, False)
            opened = os.fstat(descriptor)
            _require_prepared_snapshot(opened)
            content = _read_fd_exact(descriptor, opened.st_size)
            snapshot = cls(
                descriptor=descriptor,
                size=opened.st_size,
                content_sha256=hashlib.sha256(content).hexdigest(),
                source_identity=_auth_identity(opened),
                redactor=CredentialRedactor.from_auth_json(content),
            )
            snapshot.verify()
            descriptor = -1
            return snapshot
        except (OSError, ValueError, SessionFileSecurityError) as exc:
            raise SessionFileSecurityError(
                "inherited Codex credential snapshot is invalid"
            ) from exc
        finally:
            if descriptor >= 3:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def inheritance_descriptor(self) -> int:
        self.verify()
        return self._descriptor

    def prepare_snapshot(self) -> PreparedCodexCredentialSnapshot:
        """Clone the sealed point-in-time authority for one session publication."""

        self.verify()
        descriptor = os.dup(self._descriptor)
        try:
            os.set_inheritable(descriptor, False)
            snapshot = PreparedCodexCredentialSnapshot(
                descriptor=descriptor,
                size=self._size,
                content_sha256=self._content_sha256,
                source_identity=self._source_identity,
                redactor=self._redactor,
            )
            snapshot.verify()
            descriptor = -1
            return snapshot
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def duplicate_verified_descriptor(self) -> int:
        self.verify()
        descriptor = os.dup(self._descriptor)
        try:
            os.set_inheritable(descriptor, False)
            if _digest_fd(descriptor, self._size) != self._content_sha256:
                raise SessionFileSecurityError("prepared Codex auth snapshot changed")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def verify(self) -> None:
        if self._closed:
            raise SessionFileSecurityError("prepared Codex auth snapshot is closed")
        try:
            opened = os.fstat(self._descriptor)
            _require_prepared_snapshot(opened)
            if opened.st_size != self._size:
                raise SessionFileSecurityError("prepared Codex auth snapshot changed")
            if (
                fcntl.fcntl(self._descriptor, _F_GET_SEALS)
                != _CREDENTIAL_SNAPSHOT_SEALS
            ):
                raise SessionFileSecurityError("prepared Codex auth snapshot seal changed")
            if _digest_fd(self._descriptor, self._size) != self._content_sha256:
                raise SessionFileSecurityError("prepared Codex auth snapshot digest changed")
        except SessionFileSecurityError:
            raise
        except OSError as exc:
            raise SessionFileSecurityError("prepared Codex auth snapshot is unavailable") from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        os.close(self._descriptor)


@dataclass(frozen=True, slots=True)
class VerifiedSessionTranscript:
    """Exact transcript bytes read through one pinned session tree."""

    path: Path
    content: bytes


@dataclass(frozen=True, slots=True)
class StagedCodexCredential:
    """Validated redactor and exact published auth-file identity."""

    redactor: CredentialRedactor
    auth_identity: CredentialFileIdentity


@dataclass(slots=True)
class _AbsoluteDirectoryPin:
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
                if _full_object_identity(os.fstat(descriptor)) != expected:
                    raise SessionFileSecurityError(f"{label} ancestor descriptor changed")

            current_fd = os.dup(self.descriptors[0])
            if _full_object_identity(os.fstat(current_fd)) != self.identities[0]:
                raise SessionFileSecurityError(f"{label} absolute anchor changed")
            for part, expected in zip(self.parts, self.identities[1:]):
                before = os.stat(part, dir_fd=current_fd, follow_symlinks=False)
                if _full_object_identity(before) != expected:
                    raise SessionFileSecurityError(f"{label} ancestor path changed")
                next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=current_fd)
                opened = os.fstat(next_fd)
                if _full_object_identity(opened) != expected:
                    os.close(next_fd)
                    raise SessionFileSecurityError(f"{label} ancestor path changed")
                os.close(current_fd)
                current_fd = next_fd
        except SessionFileSecurityError:
            raise
        except OSError as exc:
            raise SessionFileSecurityError(f"{label} ancestor path changed") from exc
        finally:
            if current_fd >= 0:
                os.close(current_fd)

    def close(self) -> None:
        while self.descriptors:
            os.close(self.descriptors.pop())


class CredentialRedactor:
    """Bounded exact-value redactor derived from one verified auth document."""

    __slots__ = ("_patterns",)

    def __init__(self, patterns: tuple[bytes, ...]) -> None:
        self._patterns = patterns

    def __repr__(self) -> str:
        return f"CredentialRedactor(pattern_count={len(self._patterns)})"

    @classmethod
    def from_auth_json(cls, auth_bytes: bytes) -> CredentialRedactor:
        if not auth_bytes or len(auth_bytes) > _AUTH_MAX_BYTES:
            raise SessionFileSecurityError("Codex subscription auth JSON has invalid size")
        try:
            text = auth_bytes.decode("utf-8")
            document = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SessionFileSecurityError(
                "Codex subscription auth must contain valid UTF-8 JSON"
            ) from exc
        if not isinstance(document, dict):
            raise SessionFileSecurityError("Codex subscription auth JSON must be an object")

        patterns = {auth_bytes}
        nodes = 0
        secrets = 0

        def visit(value: object, depth: int) -> None:
            nonlocal nodes, secrets
            if depth > _AUTH_JSON_MAX_DEPTH:
                raise SessionFileSecurityError(
                    "Codex subscription auth JSON exceeds the depth limit"
                )
            nodes += 1
            if nodes > _AUTH_JSON_MAX_NODES:
                raise SessionFileSecurityError(
                    "Codex subscription auth JSON exceeds the node limit"
                )
            if isinstance(value, dict):
                for child in value.values():
                    visit(child, depth + 1)
                return
            if isinstance(value, list):
                for child in value:
                    visit(child, depth + 1)
                return
            if isinstance(value, str) and len(value) >= 4:
                encoded = value.encode("utf-8")
                if len(encoded) <= _CAPTURE_REDACTION_MAX_BYTES:
                    patterns.add(encoded)
                    secrets += 1
                    if secrets > _AUTH_JSON_MAX_SECRETS:
                        raise SessionFileSecurityError(
                            "Codex subscription auth JSON exceeds the secret limit"
                        )

        visit(document, 0)
        return cls(tuple(sorted(patterns, key=len, reverse=True)))

    def redact(self, value: str) -> str:
        encoded = value.encode("utf-8", errors="replace")
        redacted = self.redact_bytes(encoded)
        return redacted.decode("utf-8", errors="replace")

    def redact_bytes(self, value: bytes) -> bytes:
        if len(value) > _CAPTURE_REDACTION_MAX_BYTES:
            return CAPTURE_REDACTION_LIMIT_MARKER.encode("utf-8")
        redacted = value
        for pattern in self._patterns:
            redacted = redacted.replace(pattern, _CREDENTIAL_REDACTION_MARKER)
        return redacted


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


def create_session_log_authority(
    authority_root: Path,
    session_id: str,
) -> tuple[Path, SessionRootIdentity]:
    """Create one unmounted private log root under a pinned Core authority."""

    if not authority_root.is_absolute():
        raise SessionFileSecurityError("log authority root must be absolute")
    authority_pin = _pin_or_create_absolute_directory(authority_root)
    session_fd = -1
    try:
        authority_opened = os.fstat(authority_pin.descriptor)
        _require_owned_directory(authority_opened, label="log authority root")
        _fchmod_stable(authority_pin.descriptor, 0o700, label="log authority root")
        authority_pin.verify(label="log authority root")

        prefix = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
        for _ in range(32):
            name = f"session-{prefix}-{secrets.token_hex(12)}"
            try:
                os.mkdir(name, mode=0o700, dir_fd=authority_pin.descriptor)
            except FileExistsError:
                continue
            session_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=authority_pin.descriptor)
            opened = os.fstat(session_fd)
            _require_owned_directory(opened, label="session log authority")
            os.fchmod(session_fd, 0o700)
            opened = os.fstat(session_fd)
            named = os.stat(
                name,
                dir_fd=authority_pin.descriptor,
                follow_symlinks=False,
            )
            if (
                _full_object_identity(opened) != _full_object_identity(named)
                or stat.S_IMODE(opened.st_mode) != 0o700
            ):
                raise SessionFileSecurityError(
                    "session log authority changed while it was created"
                )
            authority_pin.verify(label="log authority root")
            return (
                authority_root / name,
                (opened.st_dev, opened.st_ino, opened.st_uid),
            )
        raise SessionFileSecurityError("session log authority name allocation failed")
    except SessionFileSecurityError:
        raise
    except OSError as exc:
        raise SessionFileSecurityError(
            "session log authority could not be created safely"
        ) from exc
    finally:
        if session_fd >= 0:
            os.close(session_fd)
        authority_pin.close()


def write_verified_session_log(
    session_dir: Path,
    session_identity: SessionRootIdentity,
    *,
    directory_parts: tuple[str, ...],
    leaf_name: str,
    content: str,
    max_bytes: int = EXEC_LOG_MAX_BYTES,
) -> Path:
    """Atomically publish one bounded log without following an existing entry."""

    if (
        not directory_parts
        or any(part in {"", ".", ".."} or "/" in part for part in directory_parts)
        or leaf_name in {"", ".", ".."}
        or "/" in leaf_name
    ):
        raise SessionFileSecurityError("session log path is invalid")
    encoded = content.encode("utf-8")
    if max_bytes < 0 or len(encoded) > max_bytes:
        raise SessionFileSecurityError("session log exceeds the byte limit")

    root_pin = _pin_absolute_directory(session_dir)
    directory_fds: list[int] = []
    directory_identities: list[tuple[int, int, int, int]] = []
    temporary_fd = -1
    temporary_name: str | None = None
    temporary_identity: tuple[int, int] | None = None
    try:
        root_pin.verify(label="session log root")
        _require_private_log_root(
            os.fstat(root_pin.descriptor),
            session_identity,
        )
        root_fd = _open_readable_directory(root_pin.descriptor, label="session log root")
        directory_fds.append(root_fd)
        directory_identities.append(_full_object_identity(os.fstat(root_fd)))

        for part in directory_parts:
            parent_fd = directory_fds[-1]
            try:
                os.mkdir(part, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
            before = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode) or before.st_uid != session_identity[2]:
                raise SessionFileSecurityError("session log ancestor is not an owned directory")
            child_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            opened = os.fstat(child_fd)
            if _auth_identity(before) != _auth_identity(opened):
                os.close(child_fd)
                raise SessionFileSecurityError("session log ancestor changed while it was opened")
            _fchmod_stable(child_fd, 0o700, label="session log directory")
            directory_fds.append(child_fd)
            directory_identities.append(_full_object_identity(os.fstat(child_fd)))

        parent_fd = directory_fds[-1]
        for _ in range(32):
            candidate = f".{leaf_name}.{secrets.token_hex(12)}.tmp"
            try:
                temporary_fd = os.open(
                    candidate,
                    os.O_RDWR
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_CLOEXEC
                    | os.O_NOFOLLOW
                    | os.O_NONBLOCK,
                    0o600,
                    dir_fd=parent_fd,
                )
            except FileExistsError:
                continue
            temporary_name = candidate
            break
        if temporary_fd < 0 or temporary_name is None:
            raise SessionFileSecurityError("session log temporary allocation failed")
        os.fchmod(temporary_fd, 0o600)
        temporary_opened = os.fstat(temporary_fd)
        _require_private_log_file(
            temporary_opened,
            expected_owner=session_identity[2],
            expected_size=0,
        )
        temporary_identity = _object_identity(temporary_opened)

        offset = 0
        while offset < len(encoded):
            written = os.pwrite(temporary_fd, encoded[offset:], offset)
            if written <= 0:
                raise SessionFileSecurityError("session log write made no progress")
            offset += written
        os.fsync(temporary_fd)
        written_state = os.fstat(temporary_fd)
        _require_private_log_file(
            written_state,
            expected_owner=session_identity[2],
            expected_size=len(encoded),
        )
        _require_path_identity(
            parent_fd,
            temporary_name,
            written_state,
            label="session log temporary",
        )

        os.link(
            temporary_name,
            leaf_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            follow_symlinks=False,
        )
        os.unlink(temporary_name, dir_fd=parent_fd)
        temporary_name = None
        final_state = os.fstat(temporary_fd)
        _require_private_log_file(
            final_state,
            expected_owner=session_identity[2],
            expected_size=len(encoded),
        )
        _require_path_identity(
            parent_fd,
            leaf_name,
            final_state,
            label="session log",
        )
        if _read_fd_exact(temporary_fd, len(encoded)) != encoded:
            raise SessionFileSecurityError("session log bytes changed after publication")

        for index, (descriptor, expected) in enumerate(zip(directory_fds, directory_identities)):
            if _full_object_identity(os.fstat(descriptor)) != expected:
                raise SessionFileSecurityError("session log ancestor descriptor changed")
            if index:
                named = os.stat(
                    directory_parts[index - 1],
                    dir_fd=directory_fds[index - 1],
                    follow_symlinks=False,
                )
                if _full_object_identity(named) != expected:
                    raise SessionFileSecurityError("session log ancestor path changed")
        root_pin.verify(label="session log root")
        _require_private_log_root(
            os.fstat(root_pin.descriptor),
            session_identity,
        )
        return session_dir.joinpath(*directory_parts, leaf_name)
    except FileExistsError as exc:
        raise SessionFileSecurityError("session log destination already exists") from exc
    except SessionFileSecurityError:
        raise
    except OSError as exc:
        raise SessionFileSecurityError("session log could not be written safely") from exc
    finally:
        if temporary_name is not None:
            _unlink_if_same_identity(
                directory_fds[-1] if directory_fds else -1,
                temporary_name,
                temporary_identity,
            )
        if temporary_fd >= 0:
            os.close(temporary_fd)
        while directory_fds:
            os.close(directory_fds.pop())
        root_pin.close()


def read_verified_session_transcript(
    session_dir: Path,
    session_identity: SessionRootIdentity,
    *,
    step_index: int,
    max_bytes: int = _TRANSCRIPT_MAX_BYTES,
    require_private_root: bool = False,
) -> VerifiedSessionTranscript:
    """Read one fixed agent transcript without reopening any pathname."""

    if not isinstance(step_index, int) or isinstance(step_index, bool) or step_index < 0:
        raise SessionFileSecurityError("transcript step index is invalid")
    if max_bytes < 0:
        raise SessionFileSecurityError("transcript byte limit is invalid")
    leaf_name = f"step.{step_index:02d}.stdout.log"
    relative_parts = ("logs", "agent")
    transcript_path = session_dir.joinpath(*relative_parts, leaf_name)
    root_pin = _pin_absolute_directory(session_dir)
    directory_fds: list[int] = []
    directory_identities: list[tuple[int, int, int, int, int, int, int, int]] = []
    leaf_pin_fd = -1
    leaf_fd = -1
    try:
        root_pin.verify(label="subscription transcript root")
        root_state = os.fstat(root_pin.descriptor)
        if require_private_root:
            _require_private_log_root(root_state, session_identity)
        else:
            _require_session_identity(root_state, session_identity)
        root_fd = _open_readable_directory(root_pin.descriptor, label="session root")
        directory_fds.append(root_fd)
        directory_identities.append(_auth_identity(os.fstat(root_fd)))

        for part in relative_parts:
            parent_fd = directory_fds[-1]
            before = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode) or before.st_uid != session_identity[2]:
                raise SessionFileSecurityError(
                    "subscription transcript ancestor is not an owned directory"
                )
            child_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            opened = os.fstat(child_fd)
            if _auth_identity(before) != _auth_identity(opened):
                os.close(child_fd)
                raise SessionFileSecurityError(
                    "subscription transcript ancestor changed while it was opened"
                )
            directory_fds.append(child_fd)
            directory_identities.append(_auth_identity(opened))

        leaf_before = os.stat(
            leaf_name,
            dir_fd=directory_fds[-1],
            follow_symlinks=False,
        )
        leaf_pin_fd = os.open(
            leaf_name,
            _LEAF_PIN_FLAGS,
            dir_fd=directory_fds[-1],
        )
        leaf_pinned = os.fstat(leaf_pin_fd)
        if _auth_identity(leaf_before) != _auth_identity(leaf_pinned):
            raise SessionFileSecurityError("subscription transcript changed while it was pinned")
        if (
            not stat.S_ISREG(leaf_pinned.st_mode)
            or leaf_pinned.st_uid != session_identity[2]
            or leaf_pinned.st_nlink != 1
            or leaf_pinned.st_size < 0
            or leaf_pinned.st_size > max_bytes
        ):
            raise SessionFileSecurityError(
                "subscription transcript must be an owned, single-link, bounded regular file"
            )
        leaf_fd = os.open(
            leaf_name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory_fds[-1],
        )
        leaf_opened = os.fstat(leaf_fd)
        if _auth_identity(leaf_pinned) != _auth_identity(leaf_opened):
            raise SessionFileSecurityError("subscription transcript changed while it was opened")

        remaining = leaf_opened.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(leaf_fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining or os.read(leaf_fd, 1):
            raise SessionFileSecurityError("subscription transcript changed while it was read")

        leaf_after = os.fstat(leaf_fd)
        named_leaf_after = os.stat(
            leaf_name,
            dir_fd=directory_fds[-1],
            follow_symlinks=False,
        )
        if (
            _auth_identity(leaf_pinned) != _auth_identity(os.fstat(leaf_pin_fd))
            or _auth_identity(leaf_opened) != _auth_identity(leaf_after)
            or _auth_identity(leaf_opened) != _auth_identity(named_leaf_after)
        ):
            raise SessionFileSecurityError("subscription transcript changed during verified read")

        for index, (descriptor, expected) in enumerate(zip(directory_fds, directory_identities)):
            if _auth_identity(os.fstat(descriptor)) != expected:
                raise SessionFileSecurityError(
                    "subscription transcript ancestor descriptor changed"
                )
            if index:
                named = os.stat(
                    relative_parts[index - 1],
                    dir_fd=directory_fds[index - 1],
                    follow_symlinks=False,
                )
                if _auth_identity(named) != expected:
                    raise SessionFileSecurityError("subscription transcript ancestor path changed")

        root_pin.verify(label="subscription transcript root")
        root_state = os.fstat(root_pin.descriptor)
        if require_private_root:
            _require_private_log_root(root_state, session_identity)
        else:
            _require_session_identity(root_state, session_identity)
        return VerifiedSessionTranscript(
            path=transcript_path,
            content=b"".join(chunks),
        )
    except SessionFileSecurityError:
        raise
    except OSError as exc:
        raise SessionFileSecurityError("subscription transcript could not be read safely") from exc
    finally:
        if leaf_fd >= 0:
            os.close(leaf_fd)
        if leaf_pin_fd >= 0:
            os.close(leaf_pin_fd)
        while directory_fds:
            os.close(directory_fds.pop())
        root_pin.close()


def stage_codex_subscription_auth(
    *,
    source: Path,
    source_authority: HeldCodexCredentialAuthority | None = None,
    prepared_snapshot: PreparedCodexCredentialSnapshot | None = None,
    session_dir: Path,
    session_identity: SessionRootIdentity,
    target_home_parts: tuple[str, ...],
    on_identity: Callable[[CredentialFileIdentity], None] | None = None,
) -> StagedCodexCredential:
    """Atomically publish committed auth bytes inside a managed private root."""

    source_fd = -1
    target_parent_fd = -1
    staged_fd = -1
    staged_file_identity: tuple[int, int] | None = None
    redactor: CredentialRedactor | None = None
    target_pin: _AbsoluteDirectoryPin | None = None
    staging_pin: _AbsoluteDirectoryPin | None = None
    staging_dir: Path | None = None
    staging_root_identity: SessionRootIdentity | None = None
    published = False
    owned_snapshot: PreparedCodexCredentialSnapshot | None = None
    try:
        if prepared_snapshot is not None and source_authority is not None:
            raise SessionFileSecurityError(
                "prepared Codex auth cannot be combined with source authority"
            )
        if prepared_snapshot is None:
            if not source.is_absolute() or source.name in {"", ".", ".."}:
                raise SessionFileSecurityError(
                    "Codex subscription auth source must be absolute and canonical"
                )
            if source_authority is None:
                temporary_authority = HeldCodexCredentialAuthority.open(source)
                try:
                    owned_snapshot = temporary_authority.prepare_snapshot()
                finally:
                    temporary_authority.close()
            else:
                owned_snapshot = source_authority.prepare_snapshot()
            prepared_snapshot = owned_snapshot
        prepared_snapshot.verify()
        source_fd = prepared_snapshot.duplicate_verified_descriptor()
        source_opened = os.fstat(source_fd)
        if source_opened.st_size != prepared_snapshot.size:
            raise SessionFileSecurityError("prepared Codex auth snapshot changed while opened")

        target_pin = _pin_absolute_directory(session_dir)
        target_pin.verify(label="credential root")
        _require_session_identity(os.fstat(target_pin.descriptor), session_identity)
        _fchmod_stable(target_pin.descriptor, 0o700, label="credential root")

        staging_dir = Path(
            mkdtemp(
                prefix=".openevo-credential-staging-",
                dir=session_dir,
            )
        )
        staging_root_identity = capture_session_root_identity(staging_dir)
        staging_pin = _pin_absolute_directory(staging_dir)
        staging_pin.verify(label="credential staging root")
        _require_session_identity(
            os.fstat(staging_pin.descriptor),
            staging_root_identity,
        )
        _fchmod_stable(
            staging_pin.descriptor,
            0o700,
            label="credential staging root",
        )

        staged_fd = os.open(
            "auth.json",
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=staging_pin.descriptor,
        )
        os.fchmod(staged_fd, 0o600)
        staged_opened = os.fstat(staged_fd)
        _require_private_staged_auth(
            staged_opened,
            expected_owner=staging_root_identity[2],
        )
        staged_file_identity = _object_identity(staged_opened)
        if on_identity is not None:
            on_identity(_auth_identity(staged_opened))

        _copy_exact(source_fd, staged_fd, source_opened.st_size)
        os.fsync(staged_fd)

        source_digest = _digest_fd(source_fd, source_opened.st_size)
        staged_digest = _digest_fd(staged_fd, source_opened.st_size)
        if source_digest != staged_digest:
            raise SessionFileSecurityError(
                "staged Codex auth digest does not match the verified source"
            )
        source_after = os.fstat(source_fd)
        if source_opened.st_size != source_after.st_size:
            raise SessionFileSecurityError("prepared Codex auth snapshot changed while copied")
        prepared_snapshot.verify()

        staged_after = os.fstat(staged_fd)
        _require_private_staged_auth(
            staged_after,
            expected_owner=staging_root_identity[2],
            expected_size=source_opened.st_size,
        )
        if _object_identity(staged_opened) != _object_identity(staged_after):
            raise SessionFileSecurityError("staged Codex auth changed while it was written")
        _require_path_identity(
            staging_pin.descriptor,
            "auth.json",
            staged_after,
            label="staged Codex auth",
        )
        redactor = prepared_snapshot.redactor

        source_final = os.fstat(source_fd)
        staged_final = os.fstat(staged_fd)
        if source_after.st_size != source_final.st_size:
            raise SessionFileSecurityError(
                "prepared Codex auth snapshot changed during final verification"
            )
        if _auth_identity(staged_after) != _auth_identity(staged_final):
            raise SessionFileSecurityError("staged Codex auth changed during final verification")
        prepared_snapshot.verify()
        _require_path_identity(
            staging_pin.descriptor,
            "auth.json",
            staged_final,
            label="staged Codex auth",
        )
        staging_pin.verify(label="credential staging root")

        target_pin.verify(label="credential root")
        _require_session_identity(os.fstat(target_pin.descriptor), session_identity)
        _fchmod_stable(target_pin.descriptor, 0o700, label="credential root")
        target_parent_fd = _open_private_directories(
            target_pin.descriptor,
            target_home_parts,
            expected_owner=session_identity[2],
        )
        target_pin.verify(label="credential root")
        staging_pin.verify(label="credential staging root")
        _rename_noreplace(
            "auth.json",
            "auth.json",
            source_dir_fd=staging_pin.descriptor,
            destination_dir_fd=target_parent_fd,
        )
        published = True
        os.fsync(target_parent_fd)
        os.fsync(staging_pin.descriptor)

        try:
            target_named = os.stat(
                "auth.json",
                dir_fd=target_parent_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise SessionFileSecurityError(
                "staged Codex auth path changed during publication"
            ) from exc
        if _object_identity(target_named) != _object_identity(staged_final):
            raise SessionFileSecurityError("staged Codex auth path changed during publication")
        target_final = os.fstat(staged_fd)
        _require_private_staged_auth(
            target_final,
            expected_owner=session_identity[2],
            expected_size=source_opened.st_size,
        )
        if _auth_identity(staged_final)[:-1] != _auth_identity(target_final)[:-1]:
            raise SessionFileSecurityError("staged Codex auth changed during publication")
        _require_path_identity(
            target_parent_fd,
            "auth.json",
            target_final,
            label="staged Codex auth",
        )
        target_pin.verify(label="credential root")
        staging_pin.verify(label="credential staging root")
        credential = StagedCodexCredential(
            redactor=redactor,
            auth_identity=_auth_identity(target_final),
        )
        if on_identity is not None:
            on_identity(credential.auth_identity)
            target_after_callback = os.fstat(staged_fd)
            if _auth_identity(target_after_callback) != credential.auth_identity:
                raise SessionFileSecurityError(
                    "staged Codex auth changed during publication handoff"
                )
            _require_path_identity(
                target_parent_fd,
                "auth.json",
                target_after_callback,
                label="staged Codex auth",
            )
            target_pin.verify(label="credential root")
        return credential
    except FileNotFoundError as exc:
        _scrub_staged_auth(staged_fd, staged_file_identity)
        _unlink_if_same_identity(
            target_parent_fd
            if published
            else (staging_pin.descriptor if staging_pin is not None else -1),
            "auth.json",
            staged_file_identity,
        )
        raise SessionFileSecurityError(
            "Codex subscription auth was not found at ~/.codex/auth.json; "
            "sign in with Codex on the remote host before retrying"
        ) from exc
    except SessionFileSecurityError:
        _scrub_staged_auth(staged_fd, staged_file_identity)
        _unlink_if_same_identity(
            target_parent_fd
            if published
            else (staging_pin.descriptor if staging_pin is not None else -1),
            "auth.json",
            staged_file_identity,
        )
        raise
    except Exception as exc:
        _scrub_staged_auth(staged_fd, staged_file_identity)
        _unlink_if_same_identity(
            target_parent_fd
            if published
            else (staging_pin.descriptor if staging_pin is not None else -1),
            "auth.json",
            staged_file_identity,
        )
        raise SessionFileSecurityError(
            "Codex subscription auth could not be staged safely; ensure "
            "~/.codex/auth.json is a private, user-owned regular file"
        ) from exc
    finally:
        for descriptor in (staged_fd, target_parent_fd, source_fd):
            if descriptor >= 0:
                os.close(descriptor)
        if target_pin is not None:
            target_pin.close()
        if owned_snapshot is not None:
            owned_snapshot.close()
        if staging_pin is not None:
            cleanup_failed = False
            try:
                _remove_pinned_private_staging(
                    staging_pin,
                    staging_root_identity,
                )
            except (OSError, SessionFileSecurityError):
                cleanup_failed = True
            staging_pin.close()
            # The staging root is inside the already journaled credential
            # authority. After publication it is empty, so a cleanup fault is
            # recoverable with the credential root and must not negate success.
            if cleanup_failed and sys.exc_info()[0] is None and not published:
                raise SessionFileSecurityError(
                    "credential staging root could not be removed safely"
                )


def load_staged_codex_subscription_redactor(
    credential_dir: Path,
    credential_identity: SessionRootIdentity,
    auth_identity: CredentialFileIdentity | None = None,
) -> CredentialRedactor:
    """Rebuild a redactor from the pinned private credential authority."""

    root_pin = _pin_absolute_directory(credential_dir)
    descriptor = -1
    try:
        root_pin.verify(label="credential root")
        root_stat = os.fstat(root_pin.descriptor)
        _require_session_identity(root_stat, credential_identity)
        descriptor = os.open(
            "auth.json",
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=root_pin.descriptor,
        )
        opened = os.fstat(descriptor)
        _require_private_staged_auth(
            opened,
            expected_owner=credential_identity[2],
        )
        if auth_identity is not None and _auth_identity(opened) != auth_identity:
            raise SessionFileSecurityError("journal-bound credential auth identity changed")
        content = _read_fd_exact(descriptor, opened.st_size)
        after = os.fstat(descriptor)
        if _auth_identity(opened) != _auth_identity(after):
            raise SessionFileSecurityError("staged Codex auth changed during recovery read")
        if auth_identity is not None and _auth_identity(after) != auth_identity:
            raise SessionFileSecurityError("journal-bound credential auth identity changed")
        _require_path_identity(
            root_pin.descriptor,
            "auth.json",
            after,
            label="staged Codex auth",
        )
        root_pin.verify(label="credential root")
        return CredentialRedactor.from_auth_json(content)
    except SessionFileSecurityError:
        raise
    except (OSError, ValueError) as exc:
        raise SessionFileSecurityError("staged Codex auth could not be recovered safely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        root_pin.close()


def _remove_pinned_private_staging(
    staging_pin: _AbsoluteDirectoryPin,
    staging_identity: SessionRootIdentity | None,
) -> None:
    if staging_identity is None or not staging_pin.parts or len(staging_pin.descriptors) < 2:
        raise SessionFileSecurityError("credential staging authority is incomplete")
    for descriptor, expected in zip(staging_pin.descriptors, staging_pin.identities):
        if _full_object_identity(os.fstat(descriptor)) != expected:
            raise SessionFileSecurityError("credential staging descriptor changed")
    root_state = os.fstat(staging_pin.descriptor)
    _require_session_identity(root_state, staging_identity)
    root_fd = _open_readable_directory(
        staging_pin.descriptor,
        label="credential staging root",
    )
    try:
        _remove_directory_contents(
            root_fd,
            expected_owner=staging_identity[2],
            depth=0,
            max_depth=1,
            budget=[2],
        )
    finally:
        os.close(root_fd)
    parent_fd = staging_pin.descriptors[-2]
    root_name = staging_pin.parts[-1]
    _require_named_identity(
        parent_fd,
        root_name,
        _object_identity(root_state),
        label="credential staging root",
        expected_owner=staging_identity[2],
    )
    os.rmdir(root_name, dir_fd=parent_fd)
    os.fsync(parent_fd)


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


def remove_credential_tree(
    credential_dir: Path,
    credential_identity: SessionRootIdentity,
    auth_identity: CredentialFileIdentity | None,
    *,
    max_depth: int = _CLEANUP_MAX_DEPTH,
    max_nodes: int = _CLEANUP_MAX_NODES,
    max_auth_nodes: int = _CLEANUP_MAX_NODES,
) -> None:
    """Scrub credential inodes before bounded root cleanup."""

    root_path_fd, parent_fd, root_name = _open_pinned_session_root(
        credential_dir,
        credential_identity,
    )
    try:
        _fchmod_stable(root_path_fd, 0o700, label="credential root")
        root_fd = _open_readable_directory(root_path_fd, label="credential root")
        try:
            matches = _scrub_credential_auth_inodes(
                root_fd,
                expected=auth_identity,
                expected_owner=credential_identity[2],
                depth=0,
                max_depth=max_depth,
                budget=[max_auth_nodes],
            )
            # A failed publisher may already have scrubbed and unlinked its
            # journaled inode. An empty pinned root proves there is no
            # replacement to erase; any remaining entry stays fail-closed.
            if auth_identity is not None and matches == 0 and os.listdir(root_fd):
                raise SessionFileSecurityError(
                    "journal-bound credential auth was not found during cleanup"
                )
            _remove_directory_contents(
                root_fd,
                expected_owner=credential_identity[2],
                depth=0,
                max_depth=max_depth,
                budget=[max_nodes],
            )
        finally:
            os.close(root_fd)
        _require_named_identity(
            parent_fd,
            root_name,
            credential_identity[:2],
            label="credential root",
            expected_owner=credential_identity[2],
        )
        os.rmdir(root_name, dir_fd=parent_fd)
    except SessionFileSecurityError:
        raise
    except OSError as exc:
        raise SessionFileSecurityError("credential root cleanup failed safely") from exc
    finally:
        os.close(root_path_fd)
        os.close(parent_fd)


def _scrub_credential_auth_inodes(
    directory_fd: int,
    *,
    expected: CredentialFileIdentity | None,
    expected_owner: int,
    depth: int,
    max_depth: int,
    budget: list[int],
) -> int:
    if depth > max_depth:
        raise SessionFileSecurityError("credential auth scan exceeds the depth limit")
    names: list[str] = []
    try:
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > budget[0]:
                    raise SessionFileSecurityError("credential auth scan exceeds the node limit")
    except OSError as exc:
        raise SessionFileSecurityError(
            "credential auth directory could not be inventoried"
        ) from exc
    names.sort()

    matches = 0
    for name in names:
        budget[0] -= 1
        if budget[0] < 0:
            raise SessionFileSecurityError("credential auth scan exceeds the node limit")
        try:
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            raise SessionFileSecurityError("credential auth entry changed during scan") from exc
        if before.st_uid != expected_owner:
            raise SessionFileSecurityError(
                "credential auth scan found an entry owned by another user"
            )
        entry_fd = -1
        try:
            entry_fd = os.open(name, _PATH_FLAGS, dir_fd=directory_fd)
            opened = os.fstat(entry_fd)
            if _full_object_identity(before) != _full_object_identity(opened):
                raise SessionFileSecurityError("credential auth entry changed while it was opened")
            if stat.S_ISDIR(opened.st_mode):
                _fchmod_stable(entry_fd, 0o700, label="credential auth directory")
                child_fd = _open_readable_directory(
                    entry_fd,
                    label="credential auth directory",
                )
                try:
                    matches += _scrub_credential_auth_inodes(
                        child_fd,
                        expected=expected,
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
                    label="credential auth directory",
                    expected_owner=expected_owner,
                )
            elif stat.S_ISREG(opened.st_mode) and (
                expected is None or _object_identity(opened) == expected[:2]
            ):
                if expected is not None and opened.st_uid != expected[3]:
                    raise SessionFileSecurityError("journal-bound credential auth owner changed")
                _scrub_named_credential_inode(
                    directory_fd,
                    name,
                    opened,
                    expected_owner=expected_owner,
                )
                matches += 1
        except SessionFileSecurityError:
            raise
        except OSError as exc:
            raise SessionFileSecurityError("credential auth scan failed safely") from exc
        finally:
            if entry_fd >= 0:
                os.close(entry_fd)
    return matches


def _scrub_named_credential_inode(
    directory_fd: int,
    name: str,
    expected: os.stat_result,
    *,
    expected_owner: int,
) -> None:
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory_fd,
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != expected_owner
            or _full_object_identity(opened) != _full_object_identity(expected)
        ):
            raise SessionFileSecurityError("credential auth inode changed while it was opened")
        os.ftruncate(descriptor, 0)
        os.fsync(descriptor)
        scrubbed = os.fstat(descriptor)
        if _object_identity(scrubbed) != _object_identity(opened) or scrubbed.st_size != 0:
            raise SessionFileSecurityError("credential auth inode scrub was not stable")
        _require_named_identity(
            directory_fd,
            name,
            _object_identity(opened),
            label="credential auth inode",
            expected_owner=expected_owner,
        )
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def redact_core_capture_tree(
    capture_dir: Path,
    capture_identity: SessionRootIdentity,
    redactor: CredentialRedactor,
    *,
    max_depth: int = _CLEANUP_MAX_DEPTH,
    max_nodes: int = _CLEANUP_MAX_NODES,
    max_total_bytes: int = _CAPTURE_REDACTION_MAX_TOTAL_BYTES,
) -> None:
    """Redact exact credential material from one Core-owned capture authority."""

    root_pin = _pin_absolute_directory(capture_dir)
    try:
        root_pin.verify(label="session capture root")
        _require_session_identity(os.fstat(root_pin.descriptor), capture_identity)
        for apply_redaction in (False, True):
            root_fd = _open_readable_directory(root_pin.descriptor, label="session root")
            try:
                _redact_directory_contents(
                    root_fd,
                    expected_owner=capture_identity[2],
                    redactor=redactor,
                    depth=0,
                    max_depth=max_depth,
                    budget=[max_nodes, max_total_bytes],
                    apply_redaction=apply_redaction,
                )
            finally:
                os.close(root_fd)
        root_pin.verify(label="session capture root")
        _require_session_identity(os.fstat(root_pin.descriptor), capture_identity)
    finally:
        root_pin.close()


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


def _pin_absolute_directory(path: Path) -> _AbsoluteDirectoryPin:
    parts = path.parts
    if not parts or parts[0] != os.sep or any(part in {"", ".", ".."} for part in parts[1:]):
        raise SessionFileSecurityError("private session path must be absolute and canonical")
    descriptors = [os.open(os.sep, _DIRECTORY_FLAGS)]
    identities = [_full_object_identity(os.fstat(descriptors[0]))]
    try:
        for part in parts[1:]:
            descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptors[-1])
            descriptors.append(descriptor)
            identities.append(_full_object_identity(os.fstat(descriptor)))
        return _AbsoluteDirectoryPin(
            path=path,
            parts=tuple(parts[1:]),
            descriptors=descriptors,
            identities=tuple(identities),
        )
    except Exception:
        while descriptors:
            os.close(descriptors.pop())
        raise


def _pin_or_create_absolute_directory(path: Path) -> _AbsoluteDirectoryPin:
    parts = path.parts
    if (
        not parts
        or parts[0] != os.sep
        or any(part in {"", ".", ".."} for part in parts[1:])
        or len(parts) < 2
    ):
        raise SessionFileSecurityError("private session path must be absolute and canonical")
    descriptors = [os.open(os.sep, _DIRECTORY_FLAGS)]
    identities = [_full_object_identity(os.fstat(descriptors[0]))]
    try:
        for index, part in enumerate(parts[1:]):
            try:
                os.mkdir(part, mode=0o700, dir_fd=descriptors[-1])
            except FileExistsError:
                pass
            before = os.stat(part, dir_fd=descriptors[-1], follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode):
                raise SessionFileSecurityError("private session path contains a non-directory")
            descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptors[-1])
            opened = os.fstat(descriptor)
            if _full_object_identity(before) != _full_object_identity(opened):
                os.close(descriptor)
                raise SessionFileSecurityError("private session path changed while it was opened")
            if index == len(parts[1:]) - 1:
                _require_owned_directory(opened, label="private authority root")
                os.fchmod(descriptor, 0o700)
                opened = os.fstat(descriptor)
            descriptors.append(descriptor)
            identities.append(_full_object_identity(opened))
        return _AbsoluteDirectoryPin(
            path=path,
            parts=tuple(parts[1:]),
            descriptors=descriptors,
            identities=tuple(identities),
        )
    except Exception:
        while descriptors:
            os.close(descriptors.pop())
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


def _rename_noreplace(
    source: str,
    destination: str,
    *,
    source_dir_fd: int,
    destination_dir_fd: int,
) -> None:
    if sys.platform != "linux":
        raise OSError(errno.ENOSYS, "safe credential publication requires renameat2")
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "safe credential publication requires renameat2")
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_dir_fd,
        os.fsencode(source),
        destination_dir_fd,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def _copy_exact(source_fd: int, target_fd: int, expected_size: int) -> None:
    offset = 0
    while offset < expected_size:
        chunk = os.pread(
            source_fd,
            min(64 * 1024, expected_size - offset),
            offset,
        )
        if not chunk:
            raise SessionFileSecurityError("Codex subscription auth changed while it was copied")
        view = memoryview(chunk)
        while view:
            written = os.pwrite(target_fd, view, offset)
            if written <= 0:
                raise SessionFileSecurityError("staged Codex auth could not be written")
            view = view[written:]
            offset += written
    if os.pread(source_fd, 1, expected_size):
        raise SessionFileSecurityError("Codex subscription auth changed while it was copied")


def _read_fd_exact(descriptor: int, expected_size: int) -> bytes:
    chunks: list[bytes] = []
    offset = 0
    while offset < expected_size:
        chunk = os.pread(descriptor, min(64 * 1024, expected_size - offset), offset)
        if not chunk:
            raise SessionFileSecurityError("verified credential read ended early")
        chunks.append(chunk)
        offset += len(chunk)
    if os.pread(descriptor, 1, expected_size):
        raise SessionFileSecurityError("verified credential grew during read")
    return b"".join(chunks)


def _memfd_create(name: str) -> int:
    create = getattr(os, "memfd_create", None)
    if create is not None:
        return int(create(name, _MFD_CLOEXEC | _MFD_ALLOW_SEALING))
    libc = ctypes.CDLL(None, use_errno=True)
    create = getattr(libc, "memfd_create", None)
    if create is None:
        raise OSError(errno.ENOSYS, "memfd_create is unavailable")
    create.argtypes = (ctypes.c_char_p, ctypes.c_uint)
    create.restype = ctypes.c_int
    descriptor = int(
        create(name.encode("ascii"), _MFD_CLOEXEC | _MFD_ALLOW_SEALING)
    )
    if descriptor < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return descriptor


def _digest_fd(descriptor: int, expected_size: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while offset < expected_size:
        chunk = os.pread(descriptor, min(64 * 1024, expected_size - offset), offset)
        if not chunk:
            raise SessionFileSecurityError("verified credential digest read ended early")
        digest.update(chunk)
        offset += len(chunk)
    if os.pread(descriptor, 1, expected_size):
        raise SessionFileSecurityError("verified credential grew during digest read")
    return digest.hexdigest()


def _replace_fd_contents(descriptor: int, value: bytes) -> None:
    os.ftruncate(descriptor, 0)
    offset = 0
    while offset < len(value):
        written = os.pwrite(descriptor, value[offset:], offset)
        if written <= 0:
            raise SessionFileSecurityError("credential redaction write made no progress")
        offset += written
    os.fsync(descriptor)


def _scrub_staged_auth(
    descriptor: int,
    expected: tuple[int, int] | None,
) -> None:
    if descriptor < 0 or expected is None:
        return
    try:
        opened = os.fstat(descriptor)
        if _object_identity(opened) != expected or not stat.S_ISREG(opened.st_mode):
            return
        os.ftruncate(descriptor, 0)
        os.fsync(descriptor)
    except OSError:
        return


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
                    raise SessionFileSecurityError("session cleanup exceeds the node limit")
    except OSError as exc:
        raise SessionFileSecurityError("session directory could not be inventoried") from exc
    names.sort()

    for name in names:
        budget[0] -= 1
        if budget[0] < 0:
            raise SessionFileSecurityError("session cleanup exceeds the node limit")
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if before.st_uid != expected_owner:
            raise SessionFileSecurityError("session cleanup found an entry owned by another user")
        entry_fd = os.open(name, _PATH_FLAGS, dir_fd=directory_fd)
        try:
            opened = os.fstat(entry_fd)
            if _full_object_identity(before) != _full_object_identity(opened):
                raise SessionFileSecurityError("session cleanup entry changed while it was opened")
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


def _redact_directory_contents(
    directory_fd: int,
    *,
    expected_owner: int,
    redactor: CredentialRedactor,
    depth: int,
    max_depth: int,
    budget: list[int],
    apply_redaction: bool,
) -> None:
    if depth > max_depth:
        raise SessionFileSecurityError("credential scan exceeds the depth limit")
    names: list[str] = []
    try:
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > budget[0]:
                    raise SessionFileSecurityError("credential scan exceeds the node limit")
    except OSError as exc:
        raise SessionFileSecurityError(
            "session capture directory could not be inventoried"
        ) from exc
    names.sort()

    for name in names:
        budget[0] -= 1
        if budget[0] < 0:
            raise SessionFileSecurityError("credential scan exceeds the node limit")
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if before.st_uid != expected_owner:
            raise SessionFileSecurityError("credential scan found an entry owned by another user")
        if stat.S_ISLNK(before.st_mode):
            continue
        if stat.S_ISDIR(before.st_mode):
            child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            try:
                opened = os.fstat(child_fd)
                if _full_object_identity(before) != _full_object_identity(opened):
                    raise SessionFileSecurityError(
                        "credential scan directory changed while it was opened"
                    )
                _redact_directory_contents(
                    child_fd,
                    expected_owner=expected_owner,
                    redactor=redactor,
                    depth=depth + 1,
                    max_depth=max_depth,
                    budget=budget,
                    apply_redaction=apply_redaction,
                )
                _require_path_identity(
                    directory_fd,
                    name,
                    opened,
                    label="credential scan directory",
                )
            finally:
                os.close(child_fd)
            continue
        if not stat.S_ISREG(before.st_mode):
            continue
        if before.st_nlink != 1:
            raise SessionFileSecurityError(
                "credential scan refuses a capture file with additional hard links"
            )

        descriptor = os.open(
            name,
            (os.O_RDWR if apply_redaction else os.O_RDONLY)
            | os.O_CLOEXEC
            | os.O_NOFOLLOW
            | os.O_NONBLOCK,
            dir_fd=directory_fd,
        )
        try:
            opened = os.fstat(descriptor)
            if _auth_identity(before) != _auth_identity(opened):
                raise SessionFileSecurityError("credential scan file changed while it was opened")
            original_size = opened.st_size
            if original_size > _CAPTURE_REDACTION_MAX_BYTES:
                raise SessionFileSecurityError("credential scan exceeds the per-file byte limit")
            if original_size > budget[1]:
                raise SessionFileSecurityError("credential scan exceeds the total byte limit")
            budget[1] -= original_size
            if not apply_redaction:
                _require_path_identity(
                    directory_fd,
                    name,
                    opened,
                    label="session capture file",
                )
                continue
            redacted = redactor.redact_bytes(_read_fd_exact(descriptor, original_size))
            if len(redacted) != original_size or (
                redacted != _read_fd_exact(descriptor, original_size)
            ):
                _replace_fd_contents(descriptor, redacted)
            after = os.fstat(descriptor)
            if (
                not stat.S_ISREG(after.st_mode)
                or after.st_uid != expected_owner
                or after.st_nlink != 1
            ):
                raise SessionFileSecurityError(
                    "credential scan file identity changed during redaction"
                )
            _require_path_identity(
                directory_fd,
                name,
                after,
                label="session capture file",
            )
        finally:
            os.close(descriptor)


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
        raise SessionFileSecurityError(
            f"{label} could not be opened after permission recovery"
        ) from exc
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
        raise SessionFileSecurityError("Codex subscription auth has an invalid or excessive size")


def _require_prepared_snapshot(value: os.stat_result) -> None:
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_uid != os.geteuid()
        or value.st_nlink != 0
        or stat.S_IMODE(value.st_mode) != 0o600
        or value.st_size <= 0
        or value.st_size > _AUTH_MAX_BYTES
    ):
        raise SessionFileSecurityError("prepared Codex auth snapshot is invalid")


def _require_private_staged_auth(
    value: os.stat_result,
    *,
    expected_owner: int,
    expected_size: int | None = None,
) -> None:
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_uid != expected_owner
        or value.st_nlink != 1
        or stat.S_IMODE(value.st_mode) != 0o600
        or (expected_size is not None and value.st_size != expected_size)
    ):
        raise SessionFileSecurityError("staged Codex auth is not a private regular file")


def _require_private_log_file(
    value: os.stat_result,
    *,
    expected_owner: int,
    expected_size: int,
) -> None:
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_uid != expected_owner
        or value.st_nlink != 1
        or stat.S_IMODE(value.st_mode) != 0o600
        or value.st_size != expected_size
    ):
        raise SessionFileSecurityError("session log must be a private single-link regular file")


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


def _require_private_log_root(
    value: os.stat_result,
    expected: SessionRootIdentity,
) -> None:
    _require_session_identity(value, expected)
    if stat.S_IMODE(value.st_mode) != 0o700:
        raise SessionFileSecurityError("session log authority root is not private")


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
    if _auth_identity(current) != _auth_identity(expected):
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
