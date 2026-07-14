"""Private, identity-pinned files owned by one gateway session."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypeAlias


SessionRootIdentity: TypeAlias = tuple[int, int, int]

_AUTH_MAX_BYTES: Final[int] = 1024 * 1024
_AUTH_JSON_MAX_DEPTH: Final[int] = 32
_AUTH_JSON_MAX_NODES: Final[int] = 4096
_AUTH_JSON_MAX_SECRETS: Final[int] = 512
_CAPTURE_REDACTION_MAX_BYTES: Final[int] = 4 * 1024 * 1024
_CAPTURE_REDACTION_MAX_TOTAL_BYTES: Final[int] = 64 * 1024 * 1024
_CLEANUP_MAX_DEPTH: Final[int] = 64
_CLEANUP_MAX_NODES: Final[int] = 100_000
CAPTURE_REDACTION_LIMIT_MARKER: Final[str] = (
    "[REDACTED: capture exceeded credential scan limit]"
)
_CREDENTIAL_REDACTION_MARKER: Final[bytes] = b"[REDACTED: Codex credential]"
_DIRECTORY_FLAGS: Final[int] = (
    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
)
_PATH_FLAGS: Final[int] = (
    getattr(os, "O_PATH", os.O_RDONLY) | os.O_CLOEXEC | os.O_NOFOLLOW
)


class SessionFileSecurityError(RuntimeError):
    """Raised when private session state cannot be handled without a path race."""


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


def stage_codex_subscription_auth(
    *,
    source: Path,
    session_dir: Path,
    session_identity: SessionRootIdentity,
    target_home_parts: tuple[str, ...],
) -> CredentialRedactor:
    """Copy a verified private auth file into a private session directory."""

    source_parent_fd = -1
    source_name = source.name
    source_fd = -1
    target_parent_fd = -1
    target_fd = -1
    target_identity: tuple[int, int] | None = None
    redactor: CredentialRedactor | None = None
    source_pin: _AbsoluteDirectoryPin | None = None
    target_pin: _AbsoluteDirectoryPin | None = None
    try:
        if not source.is_absolute() or source.name in {"", ".", ".."}:
            raise SessionFileSecurityError(
                "Codex subscription auth source must be absolute and canonical"
            )
        source_pin = _pin_absolute_directory(source.parent)
        source_pin.verify(label="Codex subscription auth source")
        source_parent_fd = os.dup(source_pin.descriptor)
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

        target_pin = _pin_absolute_directory(session_dir)
        target_pin.verify(label="credential root")
        _require_session_identity(os.fstat(target_pin.descriptor), session_identity)
        _fchmod_stable(target_pin.descriptor, 0o700, label="session root")
        target_parent_fd = _open_private_directories(
            target_pin.descriptor,
            target_home_parts,
            expected_owner=session_identity[2],
        )

        target_fd = os.open(
            "auth.json",
            os.O_RDWR
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

        source_digest = _digest_fd(source_fd, source_opened.st_size)
        target_digest = _digest_fd(target_fd, source_opened.st_size)
        if source_digest != target_digest:
            raise SessionFileSecurityError(
                "staged Codex auth digest does not match the verified source"
            )
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
            expected_size=source_opened.st_size,
        )
        if _object_identity(target_opened) != _object_identity(target_after):
            raise SessionFileSecurityError("staged Codex auth changed while it was written")
        _require_path_identity(
            target_parent_fd,
            "auth.json",
            target_after,
            label="staged Codex auth",
        )
        redactor = CredentialRedactor.from_auth_json(
            _read_fd_exact(target_fd, source_opened.st_size)
        )
        source_final = os.fstat(source_fd)
        target_final = os.fstat(target_fd)
        if _auth_identity(source_after) != _auth_identity(source_final):
            raise SessionFileSecurityError(
                "Codex subscription auth changed during final verification"
            )
        if _auth_identity(target_after) != _auth_identity(target_final):
            raise SessionFileSecurityError(
                "staged Codex auth changed during final verification"
            )
        _require_path_identity(
            source_parent_fd,
            source_name,
            source_final,
            label="Codex subscription auth",
        )
        _require_path_identity(
            target_parent_fd,
            "auth.json",
            target_final,
            label="staged Codex auth",
        )
        source_pin.verify(label="Codex subscription auth source")
        target_pin.verify(label="credential root")
        return redactor
    except FileNotFoundError as exc:
        _scrub_staged_auth(target_fd, target_identity)
        _unlink_if_same_identity(target_parent_fd, "auth.json", target_identity)
        raise SessionFileSecurityError(
            "Codex subscription auth was not found at ~/.codex/auth.json; "
            "sign in with Codex on the remote host before retrying"
        ) from exc
    except SessionFileSecurityError:
        _scrub_staged_auth(target_fd, target_identity)
        _unlink_if_same_identity(target_parent_fd, "auth.json", target_identity)
        raise
    except (OSError, ValueError) as exc:
        _scrub_staged_auth(target_fd, target_identity)
        _unlink_if_same_identity(target_parent_fd, "auth.json", target_identity)
        raise SessionFileSecurityError(
            "Codex subscription auth could not be staged safely; ensure "
            "~/.codex/auth.json is a private, user-owned regular file"
        ) from exc
    finally:
        for descriptor in (target_fd, target_parent_fd, source_fd, source_parent_fd):
            if descriptor >= 0:
                os.close(descriptor)
        if target_pin is not None:
            target_pin.close()
        if source_pin is not None:
            source_pin.close()


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


def redact_session_capture_tree(
    session_dir: Path,
    session_identity: SessionRootIdentity,
    redactor: CredentialRedactor,
    *,
    max_depth: int = _CLEANUP_MAX_DEPTH,
    max_nodes: int = _CLEANUP_MAX_NODES,
    max_total_bytes: int = _CAPTURE_REDACTION_MAX_TOTAL_BYTES,
) -> None:
    """Redact exact credential material from session-owned capture surfaces."""

    root_pin = _pin_absolute_directory(session_dir)
    budget = [max_nodes, max_total_bytes]
    try:
        root_pin.verify(label="session capture root")
        _require_session_identity(os.fstat(root_pin.descriptor), session_identity)
        root_fd = _open_readable_directory(root_pin.descriptor, label="session root")
        try:
            _redact_directory_contents(
                root_fd,
                expected_owner=session_identity[2],
                redactor=redactor,
                depth=0,
                max_depth=max_depth,
                budget=budget,
            )
        finally:
            os.close(root_fd)
        root_pin.verify(label="session capture root")
        _require_session_identity(os.fstat(root_pin.descriptor), session_identity)
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
    if not parts or parts[0] != os.sep or any(
        part in {"", ".", ".."} for part in parts[1:]
    ):
        raise SessionFileSecurityError(
            "private session path must be absolute and canonical"
        )
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


def _redact_directory_contents(
    directory_fd: int,
    *,
    expected_owner: int,
    redactor: CredentialRedactor,
    depth: int,
    max_depth: int,
    budget: list[int],
) -> None:
    if depth > max_depth:
        raise SessionFileSecurityError("credential scan exceeds the depth limit")
    names: list[str] = []
    try:
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > budget[0]:
                    raise SessionFileSecurityError(
                        "credential scan exceeds the node limit"
                    )
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
            raise SessionFileSecurityError(
                "credential scan found an entry owned by another user"
            )
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
            os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory_fd,
        )
        try:
            opened = os.fstat(descriptor)
            if _auth_identity(before) != _auth_identity(opened):
                raise SessionFileSecurityError(
                    "credential scan file changed while it was opened"
                )
            original_size = opened.st_size
            if (
                original_size > _CAPTURE_REDACTION_MAX_BYTES
                or original_size > budget[1]
            ):
                redacted = CAPTURE_REDACTION_LIMIT_MARKER.encode("utf-8")
            else:
                budget[1] -= original_size
                redacted = redactor.redact_bytes(
                    _read_fd_exact(descriptor, original_size)
                )
            if len(redacted) != original_size or (
                original_size <= _CAPTURE_REDACTION_MAX_BYTES
                and redacted != _read_fd_exact(descriptor, original_size)
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
