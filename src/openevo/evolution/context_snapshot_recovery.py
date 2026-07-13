"""FD-relative persistence and recovery for Core context snapshots."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import hashlib
import os
import re
import secrets
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager


MAX_CONTEXT_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_CONTEXT_SNAPSHOT_ENTRIES = 16_384
MAX_CONTEXT_SNAPSHOT_INVENTORY_BYTES = 256 * 1024 * 1024

_CONTEXTS_ROOT_NAME = "contexts"
_SNAPSHOT_SUFFIX = ".json"
_PRESERVED_PREFIX = ".openevo-context-preserved-"
_QUARANTINE_PREFIX = ".openevo-context-quarantine-"
_TOMBSTONE_PREFIX = ".openevo-context-tombstone-"
_CONTEXT_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}\Z", re.ASCII)
_TOMBSTONE_RE = re.compile(
    rf"{re.escape(_TOMBSTONE_PREFIX)}[0-9a-f]{{48}}\Z",
    re.ASCII,
)
_RENAME_NOREPLACE = 1
_READ_CHUNK_BYTES = 1024 * 1024


class ContextSnapshotIntegrityError(ValueError):
    """The context snapshot root is not safe to read or reconcile."""


@dataclass(frozen=True, slots=True)
class ContextSnapshotReceipt:
    """Identity established while a private snapshot was held open."""

    context_id: str
    name: str
    device: int
    inode: int
    mode: int
    link_count: int
    size_bytes: int
    mtime_ns: int
    ctime_ns: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ContextSnapshotInventory:
    """One stable verified inventory of snapshots and completed tombstones."""

    snapshots: tuple[ContextSnapshotReceipt, ...]
    tombstones: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ContextSnapshotReconciliation:
    """Result of reconciling disk snapshots against authoritative DB bytes."""

    referenced: tuple[ContextSnapshotReceipt, ...]
    removed_orphan_context_ids: tuple[str, ...]
    tombstones: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ContextSnapshotLegacyModeMigration:
    """Result of one explicit legacy snapshot permission migration."""

    migrated_context_ids: tuple[str, ...]
    already_private_context_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int
    mode: int
    link_count: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True, slots=True)
class _VerifiedSnapshot:
    receipt: ContextSnapshotReceipt
    contents: bytes


@dataclass(frozen=True, slots=True)
class _VerifiedInventory:
    snapshots: tuple[_VerifiedSnapshot, ...]
    tombstones: tuple[tuple[str, _FileIdentity], ...]


@dataclass(frozen=True, slots=True)
class _LegacyModeCandidate:
    context_id: str
    name: str
    identity: _FileIdentity
    permissions: int


def _identity(value: os.stat_result) -> _FileIdentity:
    return _FileIdentity(
        device=value.st_dev,
        inode=value.st_ino,
        mode=value.st_mode,
        link_count=value.st_nlink,
        size=value.st_size,
        mtime_ns=value.st_mtime_ns,
        ctime_ns=value.st_ctime_ns,
    )


def _context_id(value: str) -> str:
    if not isinstance(value, str) or _CONTEXT_ID_RE.fullmatch(value) is None:
        raise ValueError("context ID must be a closed managed identifier")
    return value


def _snapshot_name(context_id: str) -> str:
    return f"{_context_id(context_id)}{_SNAPSHOT_SUFFIX}"


def _context_id_from_snapshot_name(name: str) -> str | None:
    if not name.endswith(_SNAPSHOT_SUFFIX):
        return None
    candidate = name[: -len(_SNAPSHOT_SUFFIX)]
    if _CONTEXT_ID_RE.fullmatch(candidate) is None:
        return None
    return candidate


def _canonical_bytes(value: bytes, *, label: str) -> bytes:
    if not isinstance(value, bytes):
        raise TypeError(f"{label} must be bytes")
    if not value:
        raise ValueError(f"{label} must not be empty")
    if len(value) > MAX_CONTEXT_SNAPSHOT_BYTES:
        raise ValueError(f"{label} exceeds the context snapshot byte limit")
    return value


def _require_artifact_root_descriptor(artifact_root_fd: int) -> None:
    try:
        opened = os.fstat(artifact_root_fd)
    except OSError as exc:
        raise ContextSnapshotIntegrityError("artifact root descriptor is not valid") from exc
    if not stat.S_ISDIR(opened.st_mode):
        raise ContextSnapshotIntegrityError("artifact root descriptor is not a directory")


def _require_contexts_root_path_identity(
    artifact_root_fd: int,
    expected: _FileIdentity,
) -> None:
    try:
        observed_stat = os.stat(
            _CONTEXTS_ROOT_NAME,
            dir_fd=artifact_root_fd,
            follow_symlinks=False,
        )
        observed = _identity(observed_stat)
    except OSError as exc:
        raise ContextSnapshotIntegrityError("contexts root changed or disappeared") from exc
    if (
        not stat.S_ISDIR(observed.mode)
        or observed_stat.st_uid != os.geteuid()
        or stat.S_IMODE(observed.mode) != 0o700
        or (
            observed.device,
            observed.inode,
            observed.mode,
            observed.link_count,
        )
        != (
            expected.device,
            expected.inode,
            expected.mode,
            expected.link_count,
        )
    ):
        raise ContextSnapshotIntegrityError("contexts root identity is not stable")


@contextmanager
def _open_contexts_root(artifact_root_fd: int) -> Iterator[int]:
    _require_artifact_root_descriptor(artifact_root_fd)
    try:
        descriptor = os.open(
            _CONTEXTS_ROOT_NAME,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
            dir_fd=artifact_root_fd,
        )
    except OSError as exc:
        raise ContextSnapshotIntegrityError("contexts root could not be opened safely") from exc
    try:
        opened_stat = os.fstat(descriptor)
        opened = _identity(opened_stat)
        if (
            not stat.S_ISDIR(opened.mode)
            or opened_stat.st_uid != os.geteuid()
            or stat.S_IMODE(opened.mode) != 0o700
        ):
            raise ContextSnapshotIntegrityError(
                "contexts root must be an euid-owned mode 0700 directory"
            )
        _require_contexts_root_path_identity(artifact_root_fd, opened)
        try:
            yield descriptor
        finally:
            _require_contexts_root_path_identity(artifact_root_fd, opened)
    finally:
        os.close(descriptor)


def _require_private_snapshot_identity(identity: _FileIdentity, *, label: str) -> None:
    if not stat.S_ISREG(identity.mode):
        raise ContextSnapshotIntegrityError(f"{label} is not a regular file")
    if identity.link_count != 1:
        raise ContextSnapshotIntegrityError(f"{label} is not link-count-one")
    if stat.S_IMODE(identity.mode) != 0o600:
        raise ContextSnapshotIntegrityError(f"{label} does not have mode 0600")
    if identity.size > MAX_CONTEXT_SNAPSHOT_BYTES:
        raise ContextSnapshotIntegrityError(f"{label} exceeds the byte limit")


def _require_exact_path_identity(
    directory_fd: int,
    name: str,
    expected: _FileIdentity,
    *,
    label: str,
) -> None:
    try:
        observed = _identity(os.stat(name, dir_fd=directory_fd, follow_symlinks=False))
    except OSError as exc:
        raise ContextSnapshotIntegrityError(f"{label} changed or disappeared") from exc
    if observed != expected:
        raise ContextSnapshotIntegrityError(f"{label} identity changed")


def _require_path_identity(
    directory_fd: int,
    name: str,
    expected: _FileIdentity,
    *,
    label: str,
) -> None:
    _require_exact_path_identity(
        directory_fd,
        name,
        expected,
        label=label,
    )
    _require_private_snapshot_identity(expected, label=label)


def _open_snapshot_file(directory_fd: int, name: str, flags: int) -> int:
    try:
        return os.open(
            name,
            flags | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise ContextSnapshotIntegrityError(
            f"context snapshot {name!r} could not be opened safely"
        ) from exc


def _read_open_snapshot(
    directory_fd: int,
    context_id: str,
    name: str,
    descriptor: int,
) -> _VerifiedSnapshot:
    opened = _identity(os.fstat(descriptor))
    _require_private_snapshot_identity(opened, label=f"context snapshot {name!r}")
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    size = 0
    while chunk := os.read(descriptor, _READ_CHUNK_BYTES):
        size += len(chunk)
        if size > MAX_CONTEXT_SNAPSHOT_BYTES:
            raise ContextSnapshotIntegrityError(
                f"context snapshot {name!r} exceeds the byte limit"
            )
        digest.update(chunk)
        chunks.append(chunk)
    after = _identity(os.fstat(descriptor))
    if opened != after or size != opened.size:
        raise ContextSnapshotIntegrityError(f"context snapshot {name!r} changed while being read")
    _require_path_identity(
        directory_fd,
        name,
        after,
        label=f"context snapshot {name!r}",
    )
    return _VerifiedSnapshot(
        receipt=ContextSnapshotReceipt(
            context_id=context_id,
            name=name,
            device=after.device,
            inode=after.inode,
            mode=after.mode,
            link_count=after.link_count,
            size_bytes=size,
            mtime_ns=after.mtime_ns,
            ctime_ns=after.ctime_ns,
            sha256=digest.hexdigest(),
        ),
        contents=b"".join(chunks),
    )


def _read_snapshot_at(directory_fd: int, context_id: str) -> _VerifiedSnapshot:
    name = _snapshot_name(context_id)
    descriptor = _open_snapshot_file(directory_fd, name, os.O_RDONLY)
    try:
        return _read_open_snapshot(directory_fd, context_id, name, descriptor)
    finally:
        os.close(descriptor)


def write_context_snapshot(
    artifact_root_fd: int,
    context_id: str,
    canonical_bytes: bytes,
) -> ContextSnapshotReceipt:
    """Exclusively persist one private snapshot below a verified artifact-root FD."""

    normalized = _context_id(context_id)
    contents = _canonical_bytes(canonical_bytes, label="canonical snapshot bytes")
    name = _snapshot_name(normalized)
    with _open_contexts_root(artifact_root_fd) as contexts_fd:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=contexts_fd,
            )
            os.fchmod(descriptor, 0o600)
            opened = _identity(os.fstat(descriptor))
            _require_private_snapshot_identity(
                opened,
                label=f"context snapshot {name!r}",
            )
            view = memoryview(contents)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("context snapshot write made no progress")
                view = view[written:]
            os.fsync(descriptor)
            persisted = _identity(os.fstat(descriptor))
            _require_private_snapshot_identity(
                persisted,
                label=f"context snapshot {name!r}",
            )
            if persisted.size != len(contents):
                raise ContextSnapshotIntegrityError("context snapshot size differs after write")
            _require_path_identity(
                contexts_fd,
                name,
                persisted,
                label=f"context snapshot {name!r}",
            )
            os.fsync(contexts_fd)
            _require_path_identity(
                contexts_fd,
                name,
                persisted,
                label=f"context snapshot {name!r}",
            )
            return ContextSnapshotReceipt(
                context_id=normalized,
                name=name,
                device=persisted.device,
                inode=persisted.inode,
                mode=persisted.mode,
                link_count=persisted.link_count,
                size_bytes=persisted.size,
                mtime_ns=persisted.mtime_ns,
                ctime_ns=persisted.ctime_ns,
                sha256=hashlib.sha256(contents).hexdigest(),
            )
        except BaseException:
            if descriptor is not None:
                # Never unlink by name on failure: the pathname may already refer
                # to a replacement. Clear only the inode still held by this call.
                try:
                    failed_identity = _identity(os.fstat(descriptor))
                    _require_private_snapshot_identity(
                        failed_identity,
                        label=f"failed context snapshot {name!r}",
                    )
                    os.ftruncate(descriptor, 0)
                    os.fchmod(descriptor, 0o600)
                    os.fsync(descriptor)
                except OSError:
                    pass
                except ContextSnapshotIntegrityError:
                    pass
            raise
        finally:
            if descriptor is not None:
                os.close(descriptor)


def read_context_snapshot(
    artifact_root_fd: int,
    context_id: str,
    *,
    expected_canonical_bytes: bytes | None = None,
) -> bytes:
    """Read one private snapshot and optionally bind it to authoritative bytes."""

    normalized = _context_id(context_id)
    expected = (
        None
        if expected_canonical_bytes is None
        else _canonical_bytes(
            expected_canonical_bytes,
            label="expected canonical snapshot bytes",
        )
    )
    with _open_contexts_root(artifact_root_fd) as contexts_fd:
        verified = _read_snapshot_at(contexts_fd, normalized)
        if expected is not None and verified.contents != expected:
            raise ContextSnapshotIntegrityError(
                f"context snapshot for {normalized!r} differs from DB-authorized bytes"
            )
        return verified.contents


def _scan_names(directory_fd: int) -> tuple[str, ...]:
    names: list[str] = []
    try:
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > MAX_CONTEXT_SNAPSHOT_ENTRIES:
                    raise ContextSnapshotIntegrityError("contexts root exceeds the entry limit")
    except OSError as exc:
        raise ContextSnapshotIntegrityError("contexts root could not be inventoried") from exc
    return tuple(sorted(names))


def _verify_tombstone(directory_fd: int, name: str) -> _FileIdentity:
    descriptor = _open_snapshot_file(directory_fd, name, os.O_RDONLY)
    try:
        opened = _identity(os.fstat(descriptor))
        _require_private_snapshot_identity(opened, label=f"tombstone {name!r}")
        if opened.size != 0 or os.read(descriptor, 1) != b"":
            raise ContextSnapshotIntegrityError(f"tombstone {name!r} is not empty")
        after = _identity(os.fstat(descriptor))
        if opened != after:
            raise ContextSnapshotIntegrityError(f"tombstone {name!r} changed while being read")
        _require_path_identity(
            directory_fd,
            name,
            after,
            label=f"tombstone {name!r}",
        )
        return after
    finally:
        os.close(descriptor)


def _inventory_at(directory_fd: int) -> _VerifiedInventory:
    names = _scan_names(directory_fd)
    snapshots: list[_VerifiedSnapshot] = []
    tombstones: list[tuple[str, _FileIdentity]] = []
    total_bytes = 0
    for name in names:
        context_id = _context_id_from_snapshot_name(name)
        if context_id is not None:
            verified = _read_snapshot_at(directory_fd, context_id)
            total_bytes += verified.receipt.size_bytes
            if total_bytes > MAX_CONTEXT_SNAPSHOT_INVENTORY_BYTES:
                raise ContextSnapshotIntegrityError(
                    "context snapshot inventory exceeds the aggregate byte limit"
                )
            snapshots.append(verified)
            continue
        if _TOMBSTONE_RE.fullmatch(name) is not None:
            tombstones.append((name, _verify_tombstone(directory_fd, name)))
            continue
        raise ContextSnapshotIntegrityError(f"contexts root contains unknown entry {name!r}")

    if _scan_names(directory_fd) != names:
        raise ContextSnapshotIntegrityError("contexts root changed during inventory")
    for snapshot in snapshots:
        receipt = snapshot.receipt
        _require_path_identity(
            directory_fd,
            receipt.name,
            _FileIdentity(
                device=receipt.device,
                inode=receipt.inode,
                mode=receipt.mode,
                link_count=receipt.link_count,
                size=receipt.size_bytes,
                mtime_ns=receipt.mtime_ns,
                ctime_ns=receipt.ctime_ns,
            ),
            label=f"context snapshot {receipt.name!r}",
        )
    for name, identity in tombstones:
        _require_path_identity(
            directory_fd,
            name,
            identity,
            label=f"tombstone {name!r}",
        )
    return _VerifiedInventory(tuple(snapshots), tuple(tombstones))


def inventory_context_snapshots(artifact_root_fd: int) -> ContextSnapshotInventory:
    """Return a verified closed inventory without exposing host paths."""

    with _open_contexts_root(artifact_root_fd) as contexts_fd:
        inventory = _inventory_at(contexts_fd)
        return ContextSnapshotInventory(
            snapshots=tuple(item.receipt for item in inventory.snapshots),
            tombstones=tuple(name for name, _identity_value in inventory.tombstones),
        )


def _require_legacy_mode_candidate(
    value: os.stat_result,
    *,
    label: str,
) -> tuple[_FileIdentity, int]:
    identity = _identity(value)
    permissions = stat.S_IMODE(value.st_mode)
    if not stat.S_ISREG(value.st_mode):
        raise ContextSnapshotIntegrityError(f"{label} is not a regular file")
    if value.st_uid != os.geteuid():
        raise ContextSnapshotIntegrityError(f"{label} is not owned by the effective user")
    if value.st_nlink != 1:
        raise ContextSnapshotIntegrityError(f"{label} is not link-count-one")
    if permissions & 0o7000:
        raise ContextSnapshotIntegrityError(f"{label} has special permission bits")
    if permissions & 0o111:
        raise ContextSnapshotIntegrityError(f"{label} has executable permission bits")
    if permissions & 0o600 != 0o600:
        raise ContextSnapshotIntegrityError(f"{label} is not owner-readable and writable")
    if permissions & 0o022:
        raise ContextSnapshotIntegrityError(f"{label} is group/other writable")
    if identity.size > MAX_CONTEXT_SNAPSHOT_BYTES:
        raise ContextSnapshotIntegrityError(f"{label} exceeds the byte limit")
    return identity, permissions


def _legacy_mode_candidate_at(
    directory_fd: int,
    context_id: str,
) -> _LegacyModeCandidate:
    name = _snapshot_name(context_id)
    label = f"legacy context snapshot {name!r}"
    try:
        path_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise ContextSnapshotIntegrityError(f"{label} changed or disappeared") from exc
    path_identity, path_permissions = _require_legacy_mode_candidate(
        path_stat,
        label=label,
    )
    descriptor = _open_snapshot_file(directory_fd, name, os.O_RDONLY)
    try:
        identity, permissions = _require_legacy_mode_candidate(
            os.fstat(descriptor),
            label=label,
        )
        if identity != path_identity or permissions != path_permissions:
            raise ContextSnapshotIntegrityError(f"{label} changed before it was opened")
        _require_exact_path_identity(
            directory_fd,
            name,
            identity,
            label=label,
        )
        return _LegacyModeCandidate(
            context_id=context_id,
            name=name,
            identity=identity,
            permissions=permissions,
        )
    finally:
        os.close(descriptor)


def _legacy_mode_inventory_at(
    directory_fd: int,
) -> tuple[tuple[_LegacyModeCandidate, ...], tuple[tuple[str, _FileIdentity], ...]]:
    names = _scan_names(directory_fd)
    candidates: list[_LegacyModeCandidate] = []
    tombstones: list[tuple[str, _FileIdentity]] = []
    for name in names:
        context_id = _context_id_from_snapshot_name(name)
        if context_id is not None:
            candidates.append(_legacy_mode_candidate_at(directory_fd, context_id))
            continue
        if _TOMBSTONE_RE.fullmatch(name) is not None:
            tombstones.append((name, _verify_tombstone(directory_fd, name)))
            continue
        raise ContextSnapshotIntegrityError(f"contexts root contains unknown entry {name!r}")

    if _scan_names(directory_fd) != names:
        raise ContextSnapshotIntegrityError("contexts root changed during legacy mode inventory")
    for candidate in candidates:
        _require_exact_path_identity(
            directory_fd,
            candidate.name,
            candidate.identity,
            label=f"legacy context snapshot {candidate.name!r}",
        )
    for name, identity in tombstones:
        _require_path_identity(
            directory_fd,
            name,
            identity,
            label=f"tombstone {name!r}",
        )
    return tuple(candidates), tuple(tombstones)


def _before_legacy_snapshot_fchmod(
    contexts_fd: int,
    context_id: str,
    name: str,
    descriptor: int,
) -> None:
    """Private no-op hook for deterministic migration-race tests."""

    del contexts_fd, context_id, name, descriptor


def _migrate_legacy_mode_candidate(
    directory_fd: int,
    candidate: _LegacyModeCandidate,
) -> None:
    descriptor = _open_snapshot_file(directory_fd, candidate.name, os.O_RDONLY)
    try:
        opened, permissions = _require_legacy_mode_candidate(
            os.fstat(descriptor),
            label=f"legacy context snapshot {candidate.name!r}",
        )
        if opened != candidate.identity or permissions != candidate.permissions:
            raise ContextSnapshotIntegrityError(
                f"legacy context snapshot {candidate.name!r} changed before migration"
            )
        _require_exact_path_identity(
            directory_fd,
            candidate.name,
            opened,
            label=f"legacy context snapshot {candidate.name!r}",
        )
        _before_legacy_snapshot_fchmod(
            directory_fd,
            candidate.context_id,
            candidate.name,
            descriptor,
        )
        before_chmod_stat = os.fstat(descriptor)
        before_chmod, before_permissions = _require_legacy_mode_candidate(
            before_chmod_stat,
            label=f"legacy context snapshot {candidate.name!r}",
        )
        if before_chmod != opened or before_permissions != permissions:
            raise ContextSnapshotIntegrityError(
                f"legacy context snapshot {candidate.name!r} changed before chmod"
            )
        _require_exact_path_identity(
            directory_fd,
            candidate.name,
            before_chmod,
            label=f"legacy context snapshot {candidate.name!r}",
        )
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        migrated_stat = os.fstat(descriptor)
        migrated = _identity(migrated_stat)
        _require_private_snapshot_identity(
            migrated,
            label=f"migrated context snapshot {candidate.name!r}",
        )
        if migrated_stat.st_uid != os.geteuid() or (
            migrated.device,
            migrated.inode,
            migrated.size,
            migrated.mtime_ns,
        ) != (
            opened.device,
            opened.inode,
            opened.size,
            opened.mtime_ns,
        ):
            raise ContextSnapshotIntegrityError(
                f"legacy context snapshot {candidate.name!r} changed during migration"
            )
        _require_path_identity(
            directory_fd,
            candidate.name,
            migrated,
            label=f"migrated context snapshot {candidate.name!r}",
        )
    finally:
        os.close(descriptor)


def migrate_legacy_context_snapshot_modes(
    artifact_root_fd: int,
) -> ContextSnapshotLegacyModeMigration:
    """Explicitly tighten eligible historical snapshot modes to 0600.

    This migration is intentionally separate from strict reads and inventory. The
    caller must serialize it with context publication and startup reconciliation.
    """

    with _open_contexts_root(artifact_root_fd) as contexts_fd:
        candidates, initial_tombstones = _legacy_mode_inventory_at(contexts_fd)
        migrated_context_ids: list[str] = []
        already_private_context_ids: list[str] = []
        for candidate in candidates:
            if candidate.permissions == 0o600:
                already_private_context_ids.append(candidate.context_id)
                continue
            _migrate_legacy_mode_candidate(contexts_fd, candidate)
            migrated_context_ids.append(candidate.context_id)
        os.fsync(contexts_fd)

        final = _inventory_at(contexts_fd)
        final_by_context_id = {item.receipt.context_id: item.receipt for item in final.snapshots}
        if (
            set(final_by_context_id) != {candidate.context_id for candidate in candidates}
            or final.tombstones != initial_tombstones
        ):
            raise ContextSnapshotIntegrityError(
                "contexts root changed during legacy mode migration"
            )
        for candidate in candidates:
            receipt = final_by_context_id[candidate.context_id]
            stable_identity = (
                receipt.device,
                receipt.inode,
                receipt.size_bytes,
                receipt.mtime_ns,
            )
            expected_stable_identity = (
                candidate.identity.device,
                candidate.identity.inode,
                candidate.identity.size,
                candidate.identity.mtime_ns,
            )
            if stable_identity != expected_stable_identity or (
                candidate.permissions == 0o600
                and (
                    receipt.mode,
                    receipt.link_count,
                    receipt.ctime_ns,
                )
                != (
                    candidate.identity.mode,
                    candidate.identity.link_count,
                    candidate.identity.ctime_ns,
                )
            ):
                raise ContextSnapshotIntegrityError(
                    f"context snapshot {candidate.name!r} changed during migration"
                )
        return ContextSnapshotLegacyModeMigration(
            migrated_context_ids=tuple(sorted(migrated_context_ids)),
            already_private_context_ids=tuple(sorted(already_private_context_ids)),
        )


def _rename_noreplace(
    source: str,
    destination: str,
    *,
    directory_fd: int,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise ContextSnapshotIntegrityError("secure orphan quarantine requires renameat2")
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
        os.fsencode(source),
        directory_fd,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def _move_to_random_name(
    source: str,
    prefix: str,
    *,
    directory_fd: int,
) -> str:
    for _ in range(128):
        destination = f"{prefix}{secrets.token_hex(24)}"
        try:
            _rename_noreplace(source, destination, directory_fd=directory_fd)
        except FileExistsError:
            continue
        return destination
    raise RuntimeError("could not allocate a unique context recovery name")


def _after_orphan_quarantine(
    contexts_fd: int,
    context_id: str,
    original_name: str,
    quarantine_name: str,
) -> None:
    """Private no-op hook for deterministic replacement-race tests."""

    del contexts_fd, context_id, original_name, quarantine_name


def _tombstone_orphan(
    directory_fd: int,
    snapshot: _VerifiedSnapshot,
) -> str:
    receipt = snapshot.receipt
    descriptor = _open_snapshot_file(directory_fd, receipt.name, os.O_RDWR)
    quarantine_name: str | None = None
    try:
        opened = _identity(os.fstat(descriptor))
        expected = _FileIdentity(
            device=receipt.device,
            inode=receipt.inode,
            mode=receipt.mode,
            link_count=receipt.link_count,
            size=receipt.size_bytes,
            mtime_ns=receipt.mtime_ns,
            ctime_ns=receipt.ctime_ns,
        )
        if opened != expected:
            raise ContextSnapshotIntegrityError(
                f"orphan snapshot {receipt.name!r} changed before quarantine"
            )
        _require_path_identity(
            directory_fd,
            receipt.name,
            expected,
            label=f"orphan snapshot {receipt.name!r}",
        )
        quarantine_name = _move_to_random_name(
            receipt.name,
            _QUARANTINE_PREFIX,
            directory_fd=directory_fd,
        )
        _after_orphan_quarantine(
            directory_fd,
            receipt.context_id,
            receipt.name,
            quarantine_name,
        )
        moved = _identity(os.fstat(descriptor))
        try:
            moved_path = _identity(
                os.stat(
                    quarantine_name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            )
        except OSError as exc:
            raise ContextSnapshotIntegrityError(
                "quarantined orphan changed or disappeared"
            ) from exc
        _require_private_snapshot_identity(moved, label="quarantined orphan")
        if moved != moved_path:
            raise ContextSnapshotIntegrityError(
                "quarantined orphan identity does not match the held file"
            )
        os.fsync(directory_fd)
        os.ftruncate(descriptor, 0)
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        cleared = _identity(os.fstat(descriptor))
        _require_private_snapshot_identity(cleared, label="cleared orphan tombstone")
        if cleared.size != 0:
            raise ContextSnapshotIntegrityError("orphan contents were not cleared")
        _require_path_identity(
            directory_fd,
            quarantine_name,
            cleared,
            label="cleared orphan quarantine",
        )
        tombstone_name = _move_to_random_name(
            quarantine_name,
            _TOMBSTONE_PREFIX,
            directory_fd=directory_fd,
        )
        quarantine_name = None
        final_identity = _identity(os.fstat(descriptor))
        _require_path_identity(
            directory_fd,
            tombstone_name,
            final_identity,
            label="orphan tombstone",
        )
        os.fsync(directory_fd)
        return tombstone_name
    except OSError as exc:
        raise ContextSnapshotIntegrityError(
            f"orphan snapshot {receipt.name!r} could not be quarantined safely"
        ) from exc
    finally:
        os.close(descriptor)


def _preserve_unexpected_snapshot(
    directory_fd: int,
    snapshot: _VerifiedSnapshot,
) -> None:
    receipt = snapshot.receipt
    descriptor = _open_snapshot_file(directory_fd, receipt.name, os.O_RDONLY)
    try:
        opened = _identity(os.fstat(descriptor))
        expected = _FileIdentity(
            device=receipt.device,
            inode=receipt.inode,
            mode=receipt.mode,
            link_count=receipt.link_count,
            size=receipt.size_bytes,
            mtime_ns=receipt.mtime_ns,
            ctime_ns=receipt.ctime_ns,
        )
        if opened != expected:
            raise ContextSnapshotIntegrityError(
                f"replacement snapshot {receipt.name!r} changed before preservation"
            )
        _require_path_identity(
            directory_fd,
            receipt.name,
            expected,
            label=f"replacement snapshot {receipt.name!r}",
        )
        preserved_name = _move_to_random_name(
            receipt.name,
            _PRESERVED_PREFIX,
            directory_fd=directory_fd,
        )
        moved = _identity(os.fstat(descriptor))
        try:
            moved_path = _identity(
                os.stat(
                    preserved_name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            )
        except OSError as exc:
            raise ContextSnapshotIntegrityError(
                "preserved replacement changed or disappeared"
            ) from exc
        _require_private_snapshot_identity(moved, label="preserved replacement")
        if moved != moved_path:
            raise ContextSnapshotIntegrityError(
                "preserved replacement identity does not match the held file"
            )
        os.fsync(directory_fd)
    except OSError as exc:
        raise ContextSnapshotIntegrityError(
            f"replacement snapshot {receipt.name!r} could not be preserved safely"
        ) from exc
    finally:
        os.close(descriptor)


def _validated_expected(
    expected_canonical_bytes: Mapping[str, bytes],
) -> dict[str, bytes]:
    if not isinstance(expected_canonical_bytes, Mapping):
        raise TypeError("expected canonical snapshot bytes must be a mapping")
    result: dict[str, bytes] = {}
    total = 0
    for raw_context_id, raw_contents in expected_canonical_bytes.items():
        context_id = _context_id(raw_context_id)
        contents = _canonical_bytes(
            raw_contents,
            label=f"expected canonical bytes for {context_id!r}",
        )
        total += len(contents)
        if total > MAX_CONTEXT_SNAPSHOT_INVENTORY_BYTES:
            raise ValueError("expected context snapshots exceed the aggregate byte limit")
        result[context_id] = contents
    if len(result) > MAX_CONTEXT_SNAPSHOT_ENTRIES:
        raise ValueError("expected context snapshots exceed the entry limit")
    return result


def reconcile_context_snapshots(
    artifact_root_fd: int,
    expected_canonical_bytes: Mapping[str, bytes],
) -> ContextSnapshotReconciliation:
    """Verify DB-referenced snapshots and tombstone unreferenced private files.

    The caller supplies canonical bytes read from the database and must serialize
    this operation with context publication and DB commit.
    """

    expected = _validated_expected(expected_canonical_bytes)
    with _open_contexts_root(artifact_root_fd) as contexts_fd:
        initial = _inventory_at(contexts_fd)
        by_context_id = {snapshot.receipt.context_id: snapshot for snapshot in initial.snapshots}
        for context_id, canonical_bytes in expected.items():
            snapshot = by_context_id.get(context_id)
            if snapshot is None:
                raise ContextSnapshotIntegrityError(
                    f"DB-referenced context snapshot {context_id!r} is missing"
                )
            if snapshot.contents != canonical_bytes:
                raise ContextSnapshotIntegrityError(
                    f"DB-referenced context snapshot {context_id!r} is corrupt"
                )

        orphans = tuple(
            snapshot
            for snapshot in initial.snapshots
            if snapshot.receipt.context_id not in expected
        )
        created_tombstones = tuple(
            _tombstone_orphan(contexts_fd, snapshot) for snapshot in orphans
        )

        final = _inventory_at(contexts_fd)
        final_by_context_id = {
            snapshot.receipt.context_id: snapshot for snapshot in final.snapshots
        }
        if set(final_by_context_id) != set(expected):
            for context_id in sorted(set(final_by_context_id) - set(expected)):
                _preserve_unexpected_snapshot(
                    contexts_fd,
                    final_by_context_id[context_id],
                )
            raise ContextSnapshotIntegrityError(
                "context snapshot set changed during reconciliation; replacement preserved"
            )
        for context_id, canonical_bytes in expected.items():
            if final_by_context_id[context_id].contents != canonical_bytes:
                raise ContextSnapshotIntegrityError(
                    f"DB-referenced context snapshot {context_id!r} changed during recovery"
                )
        final_tombstones = tuple(name for name, _identity_value in final.tombstones)
        if not set(created_tombstones).issubset(final_tombstones):
            raise ContextSnapshotIntegrityError(
                "an orphan tombstone changed during reconciliation"
            )
        return ContextSnapshotReconciliation(
            referenced=tuple(
                final_by_context_id[context_id].receipt
                for context_id in sorted(final_by_context_id)
            ),
            removed_orphan_context_ids=tuple(
                sorted(snapshot.receipt.context_id for snapshot in orphans)
            ),
            tombstones=final_tombstones,
        )


__all__ = [
    "MAX_CONTEXT_SNAPSHOT_BYTES",
    "MAX_CONTEXT_SNAPSHOT_ENTRIES",
    "MAX_CONTEXT_SNAPSHOT_INVENTORY_BYTES",
    "ContextSnapshotIntegrityError",
    "ContextSnapshotInventory",
    "ContextSnapshotLegacyModeMigration",
    "ContextSnapshotReceipt",
    "ContextSnapshotReconciliation",
    "inventory_context_snapshots",
    "migrate_legacy_context_snapshot_modes",
    "read_context_snapshot",
    "reconcile_context_snapshots",
    "write_context_snapshot",
]
