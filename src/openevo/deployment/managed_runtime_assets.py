from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import tempfile
from typing import Iterator

from pydantic import SecretStr

from openevo.deployment.core_runtime import (
    CorePythonRuntimeAuthority,
    build_verified_python_command,
)
from openevo.runtime.managed import MANAGED_RUNTIME_ARCHIVE_RELEASE


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TRANSFER_ID = re.compile(r"[0-9a-f]{32}\Z")
_REMOTE_PATH = re.compile(r"/(?:[A-Za-z0-9._@%+=,-]+/)*[A-Za-z0-9._@%+=,-]+\Z")
_MAX_RESPONSE_BYTES = 4096
MANAGED_RUNTIME_TRANSFER_LEASE = ".openevo-runtime-transfer.lock"
MANAGED_RUNTIME_STAGING_MAX_TRANSFERS = 4


class ManagedRuntimeArchiveSnapshotError(ValueError):
    """A renderer-safe local archive validation failure."""


@dataclass(frozen=True, slots=True, repr=False)
class ManagedRuntimeArchiveSnapshot:
    root: Path
    archive_path: Path
    archive_sha256: str
    archive_size: int


class OpenedManagedRuntimeArchive:
    """Held descriptor authority for one private runtime archive snapshot."""

    def __init__(
        self,
        *,
        path: Path,
        descriptor: int,
        identity: tuple[int, ...],
        sha256: str,
        size: int,
    ) -> None:
        self.path = path
        self.descriptor = descriptor
        self._identity = identity
        self.sha256 = sha256
        self.size = size

    @classmethod
    def open(
        cls,
        snapshot: ManagedRuntimeArchiveSnapshot,
    ) -> OpenedManagedRuntimeArchive:
        if not isinstance(snapshot, ManagedRuntimeArchiveSnapshot):
            raise ManagedRuntimeArchiveSnapshotError("managed runtime snapshot is invalid")
        descriptor = -1
        try:
            if (
                snapshot.archive_path.parent != snapshot.root
                or snapshot.archive_path.name != MANAGED_RUNTIME_ARCHIVE_RELEASE.filename
                or snapshot.archive_sha256 != MANAGED_RUNTIME_ARCHIVE_RELEASE.sha256
                or snapshot.archive_size != MANAGED_RUNTIME_ARCHIVE_RELEASE.byte_size
            ):
                raise ValueError("managed runtime snapshot identity is invalid")
            descriptor = os.open(
                snapshot.archive_path,
                os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            )
            opened = os.fstat(descriptor)
            current = snapshot.archive_path.lstat()
            identity = _archive_file_identity(opened)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.getuid()
                or opened.st_nlink != 1
                or stat.S_IMODE(opened.st_mode) != 0o400
                or opened.st_size != snapshot.archive_size
                or _archive_file_identity(current) != identity
                or _hash_archive_descriptor(descriptor) != snapshot.archive_sha256
            ):
                raise ValueError("managed runtime snapshot identity is invalid")
            os.lseek(descriptor, 0, os.SEEK_SET)
            return cls(
                path=snapshot.archive_path,
                descriptor=descriptor,
                identity=identity,
                sha256=snapshot.archive_sha256,
                size=snapshot.archive_size,
            )
        except (OSError, TypeError, ValueError) as exc:
            if descriptor >= 0:
                os.close(descriptor)
            raise ManagedRuntimeArchiveSnapshotError(
                "managed runtime snapshot identity is invalid"
            ) from exc

    def rewind(self) -> None:
        os.lseek(self.descriptor, 0, os.SEEK_SET)

    def verify_unchanged(self) -> None:
        try:
            opened = os.fstat(self.descriptor)
            current = self.path.lstat()
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.getuid()
                or opened.st_nlink != 1
                or stat.S_IMODE(opened.st_mode) != 0o400
                or opened.st_size != self.size
                or _archive_file_identity(opened) != self._identity
                or _archive_file_identity(current) != self._identity
                or _hash_archive_descriptor(self.descriptor) != self.sha256
            ):
                raise ValueError("managed runtime snapshot changed during transfer")
            self.rewind()
        except (OSError, ValueError) as exc:
            raise ManagedRuntimeArchiveSnapshotError(
                "managed runtime snapshot changed during transfer"
            ) from exc

    def close(self) -> None:
        descriptor, self.descriptor = self.descriptor, -1
        if descriptor >= 0:
            os.close(descriptor)

    def __enter__(self) -> OpenedManagedRuntimeArchive:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class ManagedRuntimeLoadReceipt:
    archive_sha256: str
    archive_size: int
    platform: str
    config_id: str
    oci_index_id: str
    aliases: tuple[str, ...]
    reused: bool

    def __post_init__(self) -> None:
        _validate_release_request(
            archive_sha256=self.archive_sha256,
            archive_size=self.archive_size,
            platform=self.platform,
            config_id=self.config_id,
            oci_index_id=self.oci_index_id,
            aliases=self.aliases,
        )
        if type(self.reused) is not bool:
            raise ValueError("managed runtime receipt is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class ManagedRuntimeTransfer:
    service_root: str
    incoming_root: str
    transfer_id: str
    staging_device: int
    staging_inode: int
    incoming_device: int
    incoming_inode: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.service_root, str)
            or _REMOTE_PATH.fullmatch(self.service_root) is None
            or not isinstance(self.incoming_root, str)
            or _REMOTE_PATH.fullmatch(self.incoming_root) is None
            or not isinstance(self.transfer_id, str)
            or Path(self.incoming_root).parent
            != Path(self.service_root) / "managed-runtime-staging"
            or _TRANSFER_ID.fullmatch(self.transfer_id) is None
            or any(
                type(value) is not int or value < 0
                for value in (self.staging_device, self.incoming_device)
            )
            or any(
                type(value) is not int or value <= 0
                for value in (self.staging_inode, self.incoming_inode)
            )
        ):
            raise ValueError("managed runtime transfer identity is invalid")


@contextmanager
def snapshot_managed_runtime_archive(
    *,
    archive_path: str,
    archive_sha256: str,
    archive_size: int,
) -> Iterator[ManagedRuntimeArchiveSnapshot]:
    release = MANAGED_RUNTIME_ARCHIVE_RELEASE
    temporary: tempfile.TemporaryDirectory[str] | None = None
    try:
        release.__post_init__()
        if (
            Path(archive_path).name != release.filename
            or archive_sha256 != release.sha256
            or archive_size != release.byte_size
        ):
            raise ValueError("archive request differs from release")
        temporary = tempfile.TemporaryDirectory(prefix="openevo-managed-runtime-")
        root = Path(temporary.name)
        os.chmod(root, 0o700)
        destination = root / release.filename
        _copy_verified_archive(
            Path(archive_path),
            destination,
            expected_digest=archive_sha256,
            expected_size=archive_size,
        )
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            root_metadata = os.fstat(root_fd)
            if (
                not stat.S_ISDIR(root_metadata.st_mode)
                or root_metadata.st_uid != os.getuid()
                or stat.S_IMODE(root_metadata.st_mode) != 0o700
            ):
                raise ValueError("snapshot root is invalid")
            os.fsync(root_fd)
        finally:
            os.close(root_fd)
        snapshot = ManagedRuntimeArchiveSnapshot(
            root=root,
            archive_path=destination,
            archive_sha256=archive_sha256,
            archive_size=archive_size,
        )
    except (OSError, TypeError, ValueError) as exc:
        if temporary is not None:
            temporary.cleanup()
        raise ManagedRuntimeArchiveSnapshotError(
            "sealed managed runtime archive identity is invalid"
        ) from exc
    try:
        yield snapshot
    finally:
        temporary.cleanup()


def build_managed_runtime_probe_command(
    runtime: CorePythonRuntimeAuthority,
    *,
    archive_sha256: str,
    archive_size: int,
    platform: str,
    config_id: str,
    oci_index_id: str,
    aliases: tuple[str, ...],
) -> str:
    _validate_release_request(
        archive_sha256=archive_sha256,
        archive_size=archive_size,
        platform=platform,
        config_id=config_id,
        oci_index_id=oci_index_id,
        aliases=aliases,
    )
    return build_verified_python_command(
        runtime,
        _REMOTE_MANAGED_RUNTIME_SCRIPT,
        "probe",
        archive_sha256,
        str(archive_size),
        platform,
        config_id,
        oci_index_id,
        *aliases,
    )


def _build_daemon_managed_runtime_command(
    daemon_path: str,
    action: str,
    *arguments: str,
) -> str:
    if (
        not isinstance(daemon_path, str)
        or _REMOTE_PATH.fullmatch(daemon_path) is None
        or any(part in {"", ".", ".."} for part in daemon_path.split("/")[1:])
        or action not in {"discard", "finalize", "prepare", "probe", "receive"}
        or any(not isinstance(value, str) or "\x00" in value for value in arguments)
    ):
        raise ValueError("managed runtime Daemon invocation is invalid")
    return " ".join(
        (
            shlex.quote(daemon_path),
            "managed-runtime",
            action,
            *(shlex.quote(value) for value in arguments),
        )
    )


def build_daemon_managed_runtime_probe_command(
    daemon_path: str,
    *,
    archive_sha256: str,
    archive_size: int,
    platform: str,
    config_id: str,
    oci_index_id: str,
    aliases: tuple[str, ...],
) -> str:
    _validate_release_request(
        archive_sha256=archive_sha256,
        archive_size=archive_size,
        platform=platform,
        config_id=config_id,
        oci_index_id=oci_index_id,
        aliases=aliases,
    )
    return _build_daemon_managed_runtime_command(
        daemon_path,
        "probe",
        archive_sha256,
        str(archive_size),
        platform,
        config_id,
        oci_index_id,
        *aliases,
    )


def parse_managed_runtime_probe(payload: SecretStr) -> ManagedRuntimeLoadReceipt | None:
    value = _load_secret_json(payload)
    if not isinstance(value, dict) or set(value) not in (
        {"schema_version", "status"},
        {
            "aliases",
            "archive_sha256",
            "archive_size",
            "config_id",
            "oci_index_id",
            "platform",
            "reused",
            "schema_version",
            "status",
        },
    ):
        raise ValueError("managed runtime probe response is invalid")
    if type(value.get("schema_version")) is not int or value.get("schema_version") != 2:
        raise ValueError("managed runtime probe response is invalid")
    if value.get("status") == "load_required" and len(value) == 2:
        return None
    if value.get("status") != "ready":
        raise ValueError("managed runtime probe response is invalid")
    return _receipt_from_mapping(value)


def build_managed_runtime_prepare_command(
    runtime: CorePythonRuntimeAuthority,
    *,
    archive_sha256: str,
    archive_size: int,
) -> str:
    release = MANAGED_RUNTIME_ARCHIVE_RELEASE
    if archive_sha256 != release.sha256 or archive_size != release.byte_size:
        raise ValueError("managed runtime archive digest is invalid")
    return build_verified_python_command(
        runtime,
        _REMOTE_MANAGED_RUNTIME_SCRIPT,
        "prepare",
        archive_sha256,
        str(archive_size),
    )


def build_daemon_managed_runtime_prepare_command(
    daemon_path: str,
    *,
    archive_sha256: str,
    archive_size: int,
) -> str:
    release = MANAGED_RUNTIME_ARCHIVE_RELEASE
    if archive_sha256 != release.sha256 or archive_size != release.byte_size:
        raise ValueError("managed runtime archive digest is invalid")
    return _build_daemon_managed_runtime_command(
        daemon_path,
        "prepare",
        archive_sha256,
        str(archive_size),
    )


def parse_managed_runtime_prepare(payload: SecretStr) -> ManagedRuntimeTransfer:
    value = _load_secret_json(payload)
    expected = {
        "incoming_device",
        "incoming_inode",
        "incoming_root",
        "schema_version",
        "service_root",
        "staging_device",
        "staging_inode",
        "transfer_id",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
    ):
        raise ValueError("managed runtime prepare response is invalid")
    try:
        return ManagedRuntimeTransfer(
            service_root=value["service_root"],
            incoming_root=value["incoming_root"],
            transfer_id=value["transfer_id"],
            staging_device=value["staging_device"],
            staging_inode=value["staging_inode"],
            incoming_device=value["incoming_device"],
            incoming_inode=value["incoming_inode"],
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("managed runtime prepare response is invalid") from exc


def build_managed_runtime_rsync_path(
    transfer: ManagedRuntimeTransfer,
    *,
    archive_size: int | None = None,
) -> str:
    transfer.__post_init__()
    expected_size = (
        MANAGED_RUNTIME_ARCHIVE_RELEASE.byte_size if archive_size is None else archive_size
    )
    if expected_size != MANAGED_RUNTIME_ARCHIVE_RELEASE.byte_size:
        raise ValueError("managed runtime archive size is invalid")
    arguments = (
        transfer.service_root,
        transfer.transfer_id,
        str(transfer.staging_device),
        str(transfer.staging_inode),
        str(transfer.incoming_device),
        str(transfer.incoming_inode),
        str(expected_size),
    )
    return " ".join(
        (
            "/usr/bin/python3",
            "-I",
            "-c",
            shlex.quote(_REMOTE_RSYNC_LEASE_SCRIPT),
            *(shlex.quote(value) for value in arguments),
            "/usr/bin/rsync",
        )
    )


def build_managed_runtime_finalize_command(
    runtime: CorePythonRuntimeAuthority,
    transfer: ManagedRuntimeTransfer,
    *,
    archive_sha256: str,
    archive_size: int,
    platform: str,
    config_id: str,
    oci_index_id: str,
    aliases: tuple[str, ...],
    load_timeout_seconds: int,
) -> str:
    transfer.__post_init__()
    _validate_release_request(
        archive_sha256=archive_sha256,
        archive_size=archive_size,
        platform=platform,
        config_id=config_id,
        oci_index_id=oci_index_id,
        aliases=aliases,
    )
    if type(load_timeout_seconds) is not int or not 1 <= load_timeout_seconds <= 900:
        raise ValueError("managed runtime load timeout is invalid")
    return build_verified_python_command(
        runtime,
        _REMOTE_MANAGED_RUNTIME_SCRIPT,
        "finalize",
        transfer.service_root,
        transfer.transfer_id,
        str(transfer.staging_device),
        str(transfer.staging_inode),
        str(transfer.incoming_device),
        str(transfer.incoming_inode),
        archive_sha256,
        str(archive_size),
        platform,
        config_id,
        oci_index_id,
        str(load_timeout_seconds),
        *aliases,
    )


def _transfer_arguments(transfer: ManagedRuntimeTransfer) -> tuple[str, ...]:
    transfer.__post_init__()
    return (
        transfer.service_root,
        transfer.transfer_id,
        str(transfer.staging_device),
        str(transfer.staging_inode),
        str(transfer.incoming_device),
        str(transfer.incoming_inode),
    )


def build_daemon_managed_runtime_receive_command(
    daemon_path: str,
    transfer: ManagedRuntimeTransfer,
    *,
    archive_sha256: str,
    archive_size: int,
) -> str:
    release = MANAGED_RUNTIME_ARCHIVE_RELEASE
    if archive_sha256 != release.sha256 or archive_size != release.byte_size:
        raise ValueError("managed runtime archive identity is invalid")
    return _build_daemon_managed_runtime_command(
        daemon_path,
        "receive",
        *_transfer_arguments(transfer),
        archive_sha256,
        str(archive_size),
    )


def build_daemon_managed_runtime_finalize_command(
    daemon_path: str,
    transfer: ManagedRuntimeTransfer,
    *,
    archive_sha256: str,
    archive_size: int,
    platform: str,
    config_id: str,
    oci_index_id: str,
    aliases: tuple[str, ...],
    load_timeout_seconds: int,
) -> str:
    _validate_release_request(
        archive_sha256=archive_sha256,
        archive_size=archive_size,
        platform=platform,
        config_id=config_id,
        oci_index_id=oci_index_id,
        aliases=aliases,
    )
    if type(load_timeout_seconds) is not int or not 1 <= load_timeout_seconds <= 900:
        raise ValueError("managed runtime load timeout is invalid")
    return _build_daemon_managed_runtime_command(
        daemon_path,
        "finalize",
        *_transfer_arguments(transfer),
        archive_sha256,
        str(archive_size),
        platform,
        config_id,
        oci_index_id,
        str(load_timeout_seconds),
        *aliases,
    )


def build_managed_runtime_discard_command(
    runtime: CorePythonRuntimeAuthority,
    transfer: ManagedRuntimeTransfer,
    *,
    archive_sha256: str,
    archive_size: int,
) -> str:
    transfer.__post_init__()
    release = MANAGED_RUNTIME_ARCHIVE_RELEASE
    if archive_sha256 != release.sha256 or archive_size != release.byte_size:
        raise ValueError("managed runtime archive identity is invalid")
    return build_verified_python_command(
        runtime,
        _REMOTE_MANAGED_RUNTIME_SCRIPT,
        "discard",
        transfer.service_root,
        transfer.transfer_id,
        str(transfer.staging_device),
        str(transfer.staging_inode),
        str(transfer.incoming_device),
        str(transfer.incoming_inode),
        archive_sha256,
        str(archive_size),
    )


def build_daemon_managed_runtime_discard_command(
    daemon_path: str,
    transfer: ManagedRuntimeTransfer,
    *,
    archive_sha256: str,
    archive_size: int,
) -> str:
    release = MANAGED_RUNTIME_ARCHIVE_RELEASE
    if archive_sha256 != release.sha256 or archive_size != release.byte_size:
        raise ValueError("managed runtime archive identity is invalid")
    return _build_daemon_managed_runtime_command(
        daemon_path,
        "discard",
        *_transfer_arguments(transfer),
        archive_sha256,
        str(archive_size),
    )


def parse_managed_runtime_discard(payload: SecretStr) -> None:
    value = _load_secret_json(payload)
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "status"}
        or type(value.get("schema_version")) is not int
        or value != {"schema_version": 1, "status": "discarded"}
    ):
        raise ValueError("managed runtime discard response is invalid")


def parse_managed_runtime_receive(payload: SecretStr) -> None:
    value = _load_secret_json(payload)
    if value != {"schema_version": 1, "status": "received"}:
        raise ValueError("managed runtime receive response is invalid")


def parse_managed_runtime_receipt(payload: SecretStr) -> ManagedRuntimeLoadReceipt:
    value = _load_secret_json(payload)
    expected = {
        "aliases",
        "archive_sha256",
        "archive_size",
        "config_id",
        "oci_index_id",
        "platform",
        "reused",
        "schema_version",
        "status",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 2
        or value.get("status") != "ready"
    ):
        raise ValueError("managed runtime load response is invalid")
    return _receipt_from_mapping(value)


def _receipt_from_mapping(value: dict[str, object]) -> ManagedRuntimeLoadReceipt:
    try:
        aliases = value["aliases"]
        if not isinstance(aliases, list):
            raise ValueError
        return ManagedRuntimeLoadReceipt(
            archive_sha256=value["archive_sha256"],
            archive_size=value["archive_size"],
            platform=value["platform"],
            config_id=value["config_id"],
            oci_index_id=value["oci_index_id"],
            aliases=tuple(aliases),
            reused=value["reused"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("managed runtime load response is invalid") from exc


def _validate_release_request(
    *,
    archive_sha256: object,
    archive_size: object,
    platform: object,
    config_id: object,
    oci_index_id: object,
    aliases: object,
) -> None:
    release = MANAGED_RUNTIME_ARCHIVE_RELEASE
    release.__post_init__()
    if (
        archive_sha256 != release.sha256
        or archive_size != release.byte_size
        or platform != release.platform
        or config_id != release.config_id
        or oci_index_id != release.oci_index_id
        or aliases != release.aliases
    ):
        raise ValueError("managed runtime release request is invalid")


def _copy_verified_archive(
    source: Path,
    destination: Path,
    *,
    expected_digest: str,
    expected_size: int,
) -> None:
    requested = Path(os.path.abspath(source))
    if requested.name != MANAGED_RUNTIME_ARCHIVE_RELEASE.filename:
        raise ValueError("managed runtime archive filename is invalid")
    parent_fd = _open_absolute_directory_no_follow(requested.parent)
    source_fd = -1
    destination_fd = -1
    try:
        source_fd = os.open(
            requested.name,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        metadata = os.fstat(source_fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size != expected_size
        ):
            raise ValueError("managed runtime archive metadata is invalid")
        _after_managed_runtime_snapshot_open(requested, source_fd)
        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o400,
        )
        digest = hashlib.sha256()
        observed = 0
        while observed < expected_size:
            chunk = os.read(source_fd, min(1024 * 1024, expected_size - observed))
            if not chunk:
                break
            observed += len(chunk)
            digest.update(chunk)
            offset = 0
            while offset < len(chunk):
                offset += os.write(destination_fd, chunk[offset:])
        if os.read(source_fd, 1):
            raise ValueError("managed runtime archive exceeds its release size")
        os.fsync(destination_fd)
        destination_metadata = os.fstat(destination_fd)
        current = os.stat(requested.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            observed != expected_size
            or digest.hexdigest() != expected_digest
            or not stat.S_ISREG(destination_metadata.st_mode)
            or destination_metadata.st_uid != os.getuid()
            or destination_metadata.st_nlink != 1
            or stat.S_IMODE(destination_metadata.st_mode) != 0o400
            or destination_metadata.st_size != expected_size
            or (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino)
            or current.st_uid != os.getuid()
            or current.st_nlink != 1
            or not stat.S_ISREG(current.st_mode)
            or stat.S_IMODE(current.st_mode) & 0o077
            or current.st_size != expected_size
        ):
            raise ValueError("managed runtime archive changed during snapshot")
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        if source_fd >= 0:
            os.close(source_fd)
        os.close(parent_fd)


def _archive_file_identity(metadata: os.stat_result) -> tuple[int, ...]:
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


def _hash_archive_descriptor(descriptor: int) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while chunk := os.read(descriptor, 1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _open_absolute_directory_no_follow(path: Path) -> int:
    if not path.is_absolute():
        raise ValueError("managed runtime archive parent must be absolute")
    current_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=current_fd,
            )
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _after_managed_runtime_snapshot_open(source: Path, source_fd: int) -> None:
    del source, source_fd


def _load_secret_json(payload: SecretStr) -> object:
    if not isinstance(payload, SecretStr):
        raise ValueError("managed runtime response is invalid")
    encoded = payload.get_secret_value().encode("utf-8")
    if len(encoded) > _MAX_RESPONSE_BYTES:
        raise ValueError("managed runtime response exceeds its byte budget")
    try:
        return json.loads(encoded, object_pairs_hook=_closed_json_object)
    except (json.JSONDecodeError, RecursionError, UnicodeError, ValueError) as exc:
        raise ValueError("managed runtime response is invalid") from exc


def _closed_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


_REMOTE_MANAGED_RUNTIME_SCRIPT = r"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform as platform_module
import secrets
import signal
import stat
import subprocess
import sys

FILENAME = "openevo-science-runtime-0.1.1-linux-amd64.tar.gz"
LEASE = ".openevo-runtime-transfer.lock"
GLOBAL_LOCK = "managed-runtime-staging.lock"
LABEL = "io.openevo.managed-runtime"
MAX_TRANSFERS = 4
MAX_STAGING_NODES = MAX_TRANSFERS * 3
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_RECEIPTS = 16
MAX_RECEIPT_BYTES = 4096
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
FILE_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
DOCKER = "/usr/bin/docker"
DOCKER_SOCKET = "/var/run/docker.sock"
DOCKER_ENV = {
    "DOCKER_CONFIG": "/proc/self",
    "DOCKER_HOST": "unix:///var/run/docker.sock",
    "HOME": "/proc/self",
    "LC_ALL": "C",
    "PATH": "/usr/bin:/bin",
}
uid = os.getuid()
current_child = None
docker_authority = None

def fail():
    raise SystemExit(1)

def closed_object(pairs):
    value = {}
    for key, child in pairs:
        if key in value:
            fail()
        value[key] = child
    return value

def stop_child():
    global current_child
    child = current_child
    if child is None:
        return
    try:
        child.terminate()
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=2)
    except ProcessLookupError:
        pass

def interrupted(signum, frame):
    del signum, frame
    stop_child()
    raise SystemExit(1)

signal.signal(signal.SIGHUP, interrupted)
signal.signal(signal.SIGTERM, interrupted)

def bounded_names(fd, limit):
    names = []
    with os.scandir(fd) as entries:
        for entry in entries:
            if len(names) >= limit:
                fail()
            names.append(entry.name)
    return sorted(names)

def read_exact(fd, size):
    payload = bytearray()
    while len(payload) < size:
        chunk = os.read(fd, min(65536, size - len(payload)))
        if not chunk:
            fail()
        payload.extend(chunk)
    if os.read(fd, 1):
        fail()
    return bytes(payload)

def same_path(parent_fd, name, opened):
    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    return (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino)

def require_dir(parent_fd, name, mode=0o700, create=True):
    if create:
        try:
            os.mkdir(name, mode, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except FileExistsError:
            pass
    fd = os.open(name, DIR_FLAGS, dir_fd=parent_fd)
    try:
        opened = os.fstat(fd)
        if (not stat.S_ISDIR(opened.st_mode) or opened.st_uid != uid
                or not same_path(parent_fd, name, opened)):
            fail()
        os.fchmod(fd, mode)
        opened = os.fstat(fd)
        if stat.S_IMODE(opened.st_mode) != mode or not same_path(parent_fd, name, opened):
            fail()
        return fd
    except BaseException:
        os.close(fd)
        raise

def open_home():
    home = Path.home()
    if not home.is_absolute():
        fail()
    fd = os.open("/", DIR_FLAGS)
    try:
        for part in home.parts[1:]:
            next_fd = os.open(part, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        opened = os.fstat(fd)
        if not stat.S_ISDIR(opened.st_mode) or opened.st_uid != uid:
            fail()
        return fd, home
    except BaseException:
        os.close(fd)
        raise

def roots():
    home_fd, home = open_home()
    openevo_fd = core_fd = staging_fd = receipts_fd = -1
    try:
        openevo_fd = require_dir(home_fd, ".openevo")
        core_fd = require_dir(openevo_fd, "core")
        staging_fd = require_dir(core_fd, "managed-runtime-staging")
        receipts_fd = require_dir(core_fd, "managed-runtime-receipts")
        return core_fd, staging_fd, receipts_fd, home / ".openevo" / "core"
    except BaseException:
        for fd in (receipts_fd, staging_fd, core_fd):
            if fd >= 0:
                os.close(fd)
        raise
    finally:
        if openevo_fd >= 0:
            os.close(openevo_fd)
        os.close(home_fd)

def global_lock(core_fd):
    fd = os.open(
        GLOBAL_LOCK,
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
        dir_fd=core_fd,
    )
    try:
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != uid
                or opened.st_nlink != 1 or stat.S_IMODE(opened.st_mode) != 0o600
                or not same_path(core_fd, GLOBAL_LOCK, opened)):
            fail()
        fcntl.flock(fd, fcntl.LOCK_EX)
        if not same_path(core_fd, GLOBAL_LOCK, opened):
            fail()
        return fd
    except BaseException:
        os.close(fd)
        raise

def valid_hex(value, length):
    return len(value) == length and all(character in "0123456789abcdef" for character in value)

def archive_identity(sha, encoded_size):
    if not valid_hex(sha, 64):
        fail()
    try:
        size = int(encoded_size)
    except ValueError:
        fail()
    if str(size) != encoded_size or not 0 < size <= MAX_ARCHIVE_BYTES:
        fail()
    return size

def transfer_name(name):
    value = name[9:] if name.startswith("incoming-") else ""
    return valid_hex(value, 32)

def open_file(parent_fd, name, mode, maximum_size, exact_size=None):
    fd = os.open(name, FILE_FLAGS, dir_fd=parent_fd)
    try:
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != uid
                or opened.st_nlink != 1 or stat.S_IMODE(opened.st_mode) != mode
                or opened.st_size > maximum_size
                or (exact_size is not None and opened.st_size != exact_size)
                or not same_path(parent_fd, name, opened)):
            fail()
        return fd, opened
    except BaseException:
        os.close(fd)
        raise

def open_transfer(staging_fd, name, archive_size):
    fd = os.open(name, DIR_FLAGS, dir_fd=staging_fd)
    lease_fd = archive_fd = -1
    try:
        opened = os.fstat(fd)
        if (not stat.S_ISDIR(opened.st_mode) or opened.st_uid != uid
                or stat.S_IMODE(opened.st_mode) != 0o700
                or not same_path(staging_fd, name, opened)):
            fail()
        names = bounded_names(fd, 3)
        if any(child not in {LEASE, FILENAME} for child in names):
            fail()
        if LEASE not in names:
            if names:
                fail()
            return {
                "archive": None,
                "archive_fd": -1,
                "bytes": 0,
                "fd": fd,
                "held": False,
                "lease": None,
                "lease_fd": -1,
                "name": name,
                "nodes": 1,
                "opened": opened,
            }
        lease_fd, lease = open_file(fd, LEASE, 0o600, 0, exact_size=0)
        held = False
        try:
            fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            held = True
        archive = None
        if FILENAME in names:
            archive_fd, archive = open_file(fd, FILENAME, 0o600, archive_size)
        return {
            "archive": archive,
            "archive_fd": archive_fd,
            "bytes": 0 if archive is None else archive.st_size,
            "fd": fd,
            "held": held,
            "lease": lease,
            "lease_fd": lease_fd,
            "name": name,
            "nodes": 1 + len(names),
            "opened": opened,
        }
    except BaseException:
        if archive_fd >= 0:
            os.close(archive_fd)
        if lease_fd >= 0:
            os.close(lease_fd)
        os.close(fd)
        raise

def close_transfer(record):
    for key in ("archive_fd", "lease_fd", "fd"):
        fd = record[key]
        if fd >= 0:
            os.close(fd)
            record[key] = -1

def clear_transfer(staging_fd, record):
    fd = record["fd"]
    expected = []
    for name, metadata_key, descriptor_key in (
        (FILENAME, "archive", "archive_fd"),
        (LEASE, "lease", "lease_fd"),
    ):
        opened = record[metadata_key]
        if opened is None:
            continue
        expected.append(name)
        descriptor = record[descriptor_key]
        if descriptor < 0 or not same_path(fd, name, os.fstat(descriptor)):
            fail()
    if bounded_names(fd, 3) != sorted(expected):
        fail()
    if not same_path(staging_fd, record["name"], record["opened"]):
        fail()
    for name in expected:
        os.unlink(name, dir_fd=fd)
    os.fsync(fd)
    if bounded_names(fd, 1):
        fail()
    if not same_path(staging_fd, record["name"], record["opened"]):
        fail()
    close_transfer(record)
    os.rmdir(record["name"], dir_fd=staging_fd)
    os.fsync(staging_fd)
    try:
        os.stat(record["name"], dir_fd=staging_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    fail()

def reconcile(staging_fd, archive_size, preserve=None):
    names = bounded_names(staging_fd, MAX_TRANSFERS + 1)
    records = []
    try:
        for name in names:
            if not transfer_name(name):
                fail()
            records.append(open_transfer(staging_fd, name, archive_size))
        if len(records) > MAX_TRANSFERS:
            fail()
        total_nodes = sum(record["nodes"] for record in records)
        total_bytes = sum(record["bytes"] for record in records)
        if total_nodes > MAX_STAGING_NODES or total_bytes > MAX_TRANSFERS * archive_size:
            fail()
        held = 0
        for record in records:
            if record["held"]:
                held += 1
                continue
            if record["name"] == preserve:
                continue
            clear_transfer(staging_fd, record)
        return held
    finally:
        for record in records:
            close_transfer(record)

def exact_transfer(staging_fd, name, archive_size, staging_identity, incoming_identity):
    staging = os.fstat(staging_fd)
    if (staging.st_dev, staging.st_ino) != staging_identity:
        fail()
    try:
        record = open_transfer(staging_fd, name, archive_size)
    except FileNotFoundError:
        return None
    incoming = record["opened"]
    if ((incoming.st_dev, incoming.st_ino) != incoming_identity
            or record["held"] or record["lease"] is None):
        close_transfer(record)
        fail()
    return record

def reconcile_receipts(receipts_fd, preserve_cleanup=None):
    names = bounded_names(receipts_fd, MAX_RECEIPTS + 1)
    if len(names) > MAX_RECEIPTS:
        fail()
    records = []
    for name in names:
        candidate = name.startswith(".receipt-") and valid_hex(name[9:], 32)
        cleanup = name.endswith(".cleanup.json") and valid_hex(name[:-13], 64)
        final = name.endswith(".json") and not cleanup and valid_hex(name[:-5], 64)
        if not candidate and not final and not cleanup:
            fail()
        fd = os.open(name, FILE_FLAGS, dir_fd=receipts_fd)
        try:
            opened = os.fstat(fd)
            if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != uid
                    or opened.st_nlink not in {1, 2}
                    or stat.S_IMODE(opened.st_mode) != 0o600
                    or opened.st_size > MAX_RECEIPT_BYTES
                    or not same_path(receipts_fd, name, opened)):
                fail()
            payload = read_exact(fd, opened.st_size)
        finally:
            os.close(fd)
        value = None
        if final:
            try:
                value = json.loads(payload, object_pairs_hook=closed_object)
            except Exception:
                fail()
            expected_keys = {
                "aliases", "archive_sha256", "archive_size", "config_id",
                "oci_index_id",
                "platform", "reused", "schema_version", "status",
            }
            if (not isinstance(value, dict) or set(value) != expected_keys
                    or value.get("archive_sha256") != name[:-5]
                    or type(value.get("archive_size")) is not int
                    or not 0 < value["archive_size"] <= MAX_ARCHIVE_BYTES
                    or value.get("platform") != "linux-amd64"
                    or not isinstance(value.get("config_id"), str)
                    or not value["config_id"].startswith("sha256:")
                    or not valid_hex(value["config_id"][7:], 64)
                    or not isinstance(value.get("oci_index_id"), str)
                    or not value["oci_index_id"].startswith("sha256:")
                    or not valid_hex(value["oci_index_id"][7:], 64)
                    or not isinstance(value.get("aliases"), list)
                    or len(value["aliases"]) != 1
                    or any(not isinstance(alias, str) or not alias for alias in value["aliases"])
                    or len(set(value["aliases"])) != 1
                    or value.get("reused") is not False
                    or type(value.get("schema_version")) is not int
                    or value.get("schema_version") != 2
                    or value.get("status") != "ready"
                    or (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode()
                    != payload):
                fail()
        elif cleanup:
            try:
                value = json.loads(payload, object_pairs_hook=closed_object)
            except Exception:
                fail()
            expected_keys = {
                "aliases", "archive_sha256", "oci_index_id", "schema_version", "status",
            }
            if (not isinstance(value, dict) or set(value) != expected_keys
                    or value.get("archive_sha256") != name[:-13]
                    or not isinstance(value.get("oci_index_id"), str)
                    or not value["oci_index_id"].startswith("sha256:")
                    or not valid_hex(value["oci_index_id"][7:], 64)
                    or not isinstance(value.get("aliases"), list)
                    or len(value["aliases"]) != 1
                    or len(set(value["aliases"])) != 1
                    or any(not isinstance(alias, str) or not alias for alias in value["aliases"])
                    or type(value.get("schema_version")) is not int
                    or value.get("schema_version") != 1
                    or value.get("status") != "cleanup_required"
                    or (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode()
                    != payload):
                fail()
        records.append({
            "candidate": candidate,
            "cleanup": cleanup,
            "final": final,
            "name": name,
            "opened": opened,
            "payload": payload,
            "value": value,
        })
    by_inode = {}
    for record in records:
        opened = record["opened"]
        by_inode.setdefault((opened.st_dev, opened.st_ino), []).append(record)
    for group in by_inode.values():
        links = group[0]["opened"].st_nlink
        if links == 2:
            candidates = [record for record in group if record["candidate"]]
            finals = [record for record in group if record["final"]]
            if len(group) != 2 or len(candidates) != 1 or len(finals) != 1:
                fail()
            candidate = candidates[0]
            final = finals[0]
            if candidate["payload"] != final["payload"]:
                fail()
            if not same_path(receipts_fd, candidate["name"], candidate["opened"]):
                fail()
            os.unlink(candidate["name"], dir_fd=receipts_fd)
            os.fsync(receipts_fd)
            candidate["removed"] = True
        elif len(group) != 1:
            fail()
    for record in records:
        if record.get("removed"):
            continue
        if record["candidate"]:
            if not same_path(receipts_fd, record["name"], record["opened"]):
                fail()
            os.unlink(record["name"], dir_fd=receipts_fd)
            os.fsync(receipts_fd)
            continue
        if record["cleanup"]:
            continue
        current = os.stat(record["name"], dir_fd=receipts_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (
            record["opened"].st_dev,
            record["opened"].st_ino,
        ) or current.st_nlink != 1:
            fail()
    finals = {record["name"][:-5] for record in records if record["final"]}
    for record in records:
        if not record["cleanup"]:
            continue
        sha = record["name"][:-13]
        if sha == preserve_cleanup:
            continue
        if sha not in finals:
            value = record["value"]
            for alias in reversed(value["aliases"]):
                remove_alias(alias, value["oci_index_id"])
        if not same_path(receipts_fd, record["name"], record["opened"]):
            fail()
        os.unlink(record["name"], dir_fd=receipts_fd)
        os.fsync(receipts_fd)

def docker_engine_identity():
    try:
        executable = os.stat(DOCKER, follow_symlinks=False)
        engine_socket = os.stat(DOCKER_SOCKET, follow_symlinks=False)
    except OSError:
        fail()
    executable_mode = stat.S_IMODE(executable.st_mode)
    socket_mode = stat.S_IMODE(engine_socket.st_mode)
    if (not stat.S_ISREG(executable.st_mode) or executable.st_uid != 0
            or executable.st_nlink < 1 or not executable_mode & 0o111
            or executable_mode & 0o022 or executable.st_size <= 0
            or not stat.S_ISSOCK(engine_socket.st_mode)
            or engine_socket.st_uid != 0 or socket_mode not in {0o600, 0o660}
            or engine_socket.st_nlink != 1):
        fail()
    return (
        (
            executable.st_dev,
            executable.st_ino,
            executable.st_mode,
            executable.st_uid,
            executable.st_gid,
            executable.st_nlink,
            executable.st_size,
            executable.st_mtime_ns,
            executable.st_ctime_ns,
        ),
        (
            engine_socket.st_dev,
            engine_socket.st_ino,
            engine_socket.st_mode,
            engine_socket.st_uid,
            engine_socket.st_gid,
            engine_socket.st_nlink,
            engine_socket.st_ctime_ns,
        ),
    )

def verify_docker_engine():
    global docker_authority
    identity = docker_engine_identity()
    if docker_authority is None:
        docker_authority = identity
    elif docker_authority != identity:
        fail()

def run_docker(arguments, timeout=30, capture=False, pass_fds=()):
    global current_child
    verify_docker_engine()
    current_child = subprocess.Popen(
        [DOCKER, *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
        env=DOCKER_ENV,
        cwd="/",
        close_fds=True,
        pass_fds=pass_fds,
    )
    try:
        verify_docker_engine()
    except BaseException:
        stop_child()
        raise
    try:
        try:
            stdout, stderr = current_child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            stop_child()
            fail()
        return current_child.returncode, stdout or b"", stderr or b""
    finally:
        current_child = None
        verify_docker_engine()

def docker_not_found(error):
    if len(error) > 4096:
        return False
    normalized = error.strip().lower()
    return (normalized.startswith(b"error: no such image")
            or normalized.startswith(b"error response from daemon: no such image"))

def inspect_state(alias, expected):
    returncode, output, error = run_docker(["image", "inspect", alias], capture=True)
    if returncode != 0:
        if docker_not_found(error):
            return "missing"
        fail()
    if len(output) > 1024 * 1024 or len(error) > 4096:
        fail()
    try:
        value = json.loads(output, object_pairs_hook=closed_object)
    except Exception:
        fail()
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        fail()
    record = value[0]
    config = record.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    if (record.get("Id") != expected or not isinstance(labels, dict)
            or labels.get(LABEL) != "true"):
        fail()
    return "ready"

def inspect(alias, expected):
    return inspect_state(alias, expected) == "ready"

def receipt(sha, size, platform, config, oci_index, aliases, reused):
    return {
        "aliases": aliases,
        "archive_sha256": sha,
        "archive_size": size,
        "config_id": config,
        "oci_index_id": oci_index,
        "platform": platform,
        "reused": reused,
        "schema_version": 2,
        "status": "ready",
    }

def write_all(fd, payload):
    offset = 0
    while offset < len(payload):
        written = os.write(fd, payload[offset:])
        if written <= 0:
            fail()
        offset += written

def publish_receipt(receipts_fd, sha, payload):
    reconcile_receipts(receipts_fd, preserve_cleanup=sha)
    final_name = sha + ".json"
    try:
        existing_fd, existing = open_file(
            receipts_fd, final_name, 0o600, MAX_RECEIPT_BYTES
        )
    except FileNotFoundError:
        existing_fd = -1
    if existing_fd >= 0:
        try:
            if os.read(existing_fd, MAX_RECEIPT_BYTES + 1) != payload:
                fail()
        finally:
            os.close(existing_fd)
        return None
    candidate = ".receipt-" + secrets.token_hex(16)
    candidate_fd = os.open(
        candidate,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
        dir_fd=receipts_fd,
    )
    linked = False
    completed = False
    candidate_value = None
    try:
        write_all(candidate_fd, payload)
        os.fsync(candidate_fd)
        candidate_value = os.fstat(candidate_fd)
        if (not stat.S_ISREG(candidate_value.st_mode) or candidate_value.st_uid != uid
                or candidate_value.st_nlink != 1
                or stat.S_IMODE(candidate_value.st_mode) != 0o600
                or not same_path(receipts_fd, candidate, candidate_value)):
            fail()
        os.close(candidate_fd)
        candidate_fd = -1
        os.link(
            candidate,
            final_name,
            src_dir_fd=receipts_fd,
            dst_dir_fd=receipts_fd,
            follow_symlinks=False,
        )
        linked = True
        os.unlink(candidate, dir_fd=receipts_fd)
        os.fsync(receipts_fd)
        final_fd, final_value = open_file(
            receipts_fd, final_name, 0o600, MAX_RECEIPT_BYTES
        )
        try:
            if os.read(final_fd, MAX_RECEIPT_BYTES + 1) != payload:
                fail()
        finally:
            os.close(final_fd)
        completed = True
        return final_value
    finally:
        if candidate_fd >= 0:
            os.close(candidate_fd)
        try:
            candidate_value = os.stat(
                candidate, dir_fd=receipts_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            pass
        else:
            if (not stat.S_ISREG(candidate_value.st_mode)
                    or candidate_value.st_uid != uid
                    or stat.S_IMODE(candidate_value.st_mode) != 0o600):
                fail()
            os.unlink(candidate, dir_fd=receipts_fd)
            os.fsync(receipts_fd)
        if linked and not completed:
            if candidate_value is None:
                fail()
            try:
                final_fd = os.open(final_name, FILE_FLAGS, dir_fd=receipts_fd)
            except FileNotFoundError:
                pass
            else:
                try:
                    final_value = os.fstat(final_fd)
                    if ((final_value.st_dev, final_value.st_ino)
                            != (candidate_value.st_dev, candidate_value.st_ino)
                            or not same_path(receipts_fd, final_name, final_value)):
                        fail()
                finally:
                    os.close(final_fd)
                os.unlink(final_name, dir_fd=receipts_fd)
                os.fsync(receipts_fd)
        elif linked:
            try:
                os.stat(final_name, dir_fd=receipts_fd, follow_symlinks=False)
            except FileNotFoundError:
                fail()

def rollback_receipt(receipts_fd, sha, opened):
    if opened is None:
        return
    name = sha + ".json"
    fd, current = open_file(receipts_fd, name, 0o600, MAX_RECEIPT_BYTES)
    try:
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            fail()
    finally:
        os.close(fd)
    os.unlink(name, dir_fd=receipts_fd)
    os.fsync(receipts_fd)

def cleanup_payload(sha, oci_index, aliases):
    value = {
        "aliases": aliases,
        "archive_sha256": sha,
        "oci_index_id": oci_index,
        "schema_version": 1,
        "status": "cleanup_required",
    }
    return (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode()

def publish_cleanup_authority(receipts_fd, sha, payload):
    name = sha + ".cleanup.json"
    try:
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=receipts_fd,
        )
    except FileExistsError:
        fd, opened = open_file(receipts_fd, name, 0o600, MAX_RECEIPT_BYTES)
        try:
            if os.read(fd, MAX_RECEIPT_BYTES + 1) != payload:
                fail()
        finally:
            os.close(fd)
        return opened
    try:
        write_all(fd, payload)
        os.fsync(fd)
        opened = os.fstat(fd)
        if not same_path(receipts_fd, name, opened):
            fail()
    finally:
        os.close(fd)
    os.fsync(receipts_fd)
    return opened

def clear_cleanup_authority(receipts_fd, sha, opened):
    name = sha + ".cleanup.json"
    fd, current = open_file(receipts_fd, name, 0o600, MAX_RECEIPT_BYTES)
    try:
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            fail()
    finally:
        os.close(fd)
    os.unlink(name, dir_fd=receipts_fd)
    os.fsync(receipts_fd)

def remove_alias(alias, image):
    state = inspect_state(alias, image)
    if state == "missing":
        return
    returncode, unused, error = run_docker(["image", "rm", alias], capture=True)
    del unused
    if returncode != 0 and not docker_not_found(error):
        fail()
    if inspect_state(alias, image) != "missing":
        fail()

def validate_release(sha, encoded_size, platform, config, oci_index, aliases):
    size = archive_identity(sha, encoded_size)
    if (platform != "linux-amd64" or not config.startswith("sha256:")
            or not valid_hex(config[7:], 64) or not oci_index.startswith("sha256:")
            or not valid_hex(oci_index[7:], 64) or config == oci_index
            or len(aliases) != 1 or len(set(aliases)) != 1
            or any(not alias or "@" in alias
                   or alias.rfind(":") <= alias.rfind("/") for alias in aliases)):
        fail()
    return size

action = sys.argv[1] if len(sys.argv) > 1 else ""
if action == "probe":
    sha, encoded_size, platform, config, oci_index, *aliases = sys.argv[2:]
    size = validate_release(sha, encoded_size, platform, config, oci_index, aliases)
    if (platform_module.system() != "Linux"
            or platform_module.machine() not in {"x86_64", "amd64"}):
        fail()
    core_fd, staging_fd, receipts_fd, service = roots()
    lock_fd = global_lock(core_fd)
    try:
        reconcile(staging_fd, size)
        reconcile_receipts(receipts_fd)
        states = [inspect_state(alias, oci_index) for alias in aliases]
        if states == ["ready"]:
            output = receipt(sha, size, platform, config, oci_index, aliases, True)
            print(json.dumps(output, separators=(",", ":"), sort_keys=True))
        elif states == ["missing"]:
            print('{"schema_version":2,"status":"load_required"}')
        else:
            fail()
    finally:
        os.close(lock_fd)
        os.close(receipts_fd)
        os.close(staging_fd)
        os.close(core_fd)
elif action == "prepare":
    sha, encoded_size = sys.argv[2:]
    size = archive_identity(sha, encoded_size)
    core_fd, staging_fd, receipts_fd, service = roots()
    lock_fd = global_lock(core_fd)
    try:
        held = reconcile(staging_fd, size)
        reconcile_receipts(receipts_fd)
        if held >= MAX_TRANSFERS:
            fail()
        staging_value = os.fstat(staging_fd)
        for unused in range(4):
            del unused
            transfer = secrets.token_hex(16)
            incoming_name = "incoming-" + transfer
            try:
                os.mkdir(incoming_name, 0o700, dir_fd=staging_fd)
                os.fsync(staging_fd)
                break
            except FileExistsError:
                continue
        else:
            fail()
        incoming_fd = os.open(incoming_name, DIR_FLAGS, dir_fd=staging_fd)
        try:
            incoming_value = os.fstat(incoming_fd)
            if (not stat.S_ISDIR(incoming_value.st_mode)
                    or incoming_value.st_uid != uid
                    or stat.S_IMODE(incoming_value.st_mode) != 0o700
                    or not same_path(staging_fd, incoming_name, incoming_value)):
                fail()
            lease_fd = os.open(
                LEASE,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=incoming_fd,
            )
            try:
                lease = os.fstat(lease_fd)
                if (not stat.S_ISREG(lease.st_mode) or lease.st_uid != uid
                        or lease.st_nlink != 1 or stat.S_IMODE(lease.st_mode) != 0o600
                        or lease.st_size != 0 or not same_path(incoming_fd, LEASE, lease)):
                    fail()
                os.fsync(lease_fd)
                os.fsync(incoming_fd)
            finally:
                os.close(lease_fd)
        finally:
            os.close(incoming_fd)
        output = {
            "incoming_device": incoming_value.st_dev,
            "incoming_inode": incoming_value.st_ino,
            "incoming_root": str(service / "managed-runtime-staging" / incoming_name),
            "schema_version": 1,
            "service_root": str(service),
            "staging_device": staging_value.st_dev,
            "staging_inode": staging_value.st_ino,
            "transfer_id": transfer,
        }
        print(json.dumps(output, separators=(",", ":"), sort_keys=True))
    finally:
        os.close(lock_fd)
        os.close(receipts_fd)
        os.close(staging_fd)
        os.close(core_fd)
elif action in {"discard", "finalize", "receive"}:
    service_argument, transfer = Path(sys.argv[2]), sys.argv[3]
    if not valid_hex(transfer, 32):
        fail()
    try:
        staging_identity = (int(sys.argv[4]), int(sys.argv[5]))
        incoming_identity = (int(sys.argv[6]), int(sys.argv[7]))
    except ValueError:
        fail()
    if action in {"discard", "receive"}:
        sha, encoded_size = sys.argv[8:]
        size = archive_identity(sha, encoded_size)
    else:
        sha, encoded_size, platform, config, oci_index, encoded_timeout, *aliases = sys.argv[8:]
        size = validate_release(sha, encoded_size, platform, config, oci_index, aliases)
        try:
            timeout = int(encoded_timeout)
        except ValueError:
            fail()
        if str(timeout) != encoded_timeout or not 1 <= timeout <= 900:
            fail()
    core_fd, staging_fd, receipts_fd, service = roots()
    lock_fd = global_lock(core_fd)
    incoming_name = "incoming-" + transfer
    try:
        if service_argument != service:
            fail()
        staging_value = os.fstat(staging_fd)
        if (staging_value.st_dev, staging_value.st_ino) != staging_identity:
            fail()
        reconcile(staging_fd, size, preserve=incoming_name)
        reconcile_receipts(receipts_fd)
        record = exact_transfer(
            staging_fd,
            incoming_name,
            size,
            staging_identity,
            incoming_identity,
        )
        if action == "discard":
            if record is not None:
                clear_transfer(staging_fd, record)
            print('{"schema_version":1,"status":"discarded"}')
        elif action == "receive":
            if record is None or record["archive"] is not None:
                if record is not None:
                    close_transfer(record)
                fail()
            archive_fd = -1
            archive = None
            try:
                archive_fd = os.open(
                    FILENAME,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=record["fd"],
                )
                archive = os.fstat(archive_fd)
                if (not stat.S_ISREG(archive.st_mode) or archive.st_uid != uid
                        or archive.st_nlink != 1 or stat.S_IMODE(archive.st_mode) != 0o600
                        or archive.st_size != 0
                        or not same_path(record["fd"], FILENAME, archive)):
                    fail()
                digest = hashlib.sha256()
                observed = 0
                while observed < size:
                    chunk = os.read(0, min(1024 * 1024, size - observed))
                    if not chunk:
                        fail()
                    write_all(archive_fd, chunk)
                    digest.update(chunk)
                    observed += len(chunk)
                if os.read(0, 1) or observed != size or digest.hexdigest() != sha:
                    fail()
                os.fsync(archive_fd)
                final = os.fstat(archive_fd)
                if (final.st_dev, final.st_ino) != (archive.st_dev, archive.st_ino):
                    fail()
                archive = final
                if (archive.st_size != size
                        or not same_path(record["fd"], FILENAME, archive)):
                    fail()
                print('{"schema_version":1,"status":"received"}')
            except BaseException:
                if archive_fd >= 0:
                    try:
                        opened = os.fstat(archive_fd)
                        if same_path(record["fd"], FILENAME, opened):
                            os.unlink(FILENAME, dir_fd=record["fd"])
                            os.fsync(record["fd"])
                    except (FileNotFoundError, OSError):
                        pass
                raise
            finally:
                if archive_fd >= 0:
                    os.close(archive_fd)
                close_transfer(record)
        else:
            if record is None or record["archive"] is None:
                if record is not None:
                    close_transfer(record)
                fail()
            archive_fd = record["archive_fd"]
            archive = record["archive"]
            if archive.st_size != size:
                close_transfer(record)
                fail()
            digest = hashlib.sha256()
            observed = 0
            while observed < size:
                chunk = os.read(archive_fd, min(1024 * 1024, size - observed))
                if not chunk:
                    break
                observed += len(chunk)
                digest.update(chunk)
            if (observed != size or os.read(archive_fd, 1)
                    or digest.hexdigest() != sha
                    or not same_path(record["fd"], FILENAME, archive)):
                close_transfer(record)
                fail()
            published_aliases = []
            published_receipt = None
            committed = False
            if any(inspect_state(alias, oci_index) != "missing" for alias in aliases):
                fail()
            payload = (
                json.dumps(
                    receipt(sha, size, platform, config, oci_index, aliases, False),
                    separators=(",", ":"),
                    sort_keys=True,
                ) + "\n"
            ).encode()
            cleanup = publish_cleanup_authority(
                receipts_fd,
                sha,
                cleanup_payload(sha, oci_index, aliases),
            )
            try:
                os.lseek(archive_fd, 0, os.SEEK_SET)
                os.set_inheritable(archive_fd, True)
                returncode, unused, error = run_docker(
                    ["load", "--input", "/proc/self/fd/" + str(archive_fd)],
                    timeout=timeout,
                    pass_fds=(archive_fd,),
                )
                del unused, error
                os.set_inheritable(archive_fd, False)
                if returncode != 0 or inspect_state(oci_index, oci_index) != "ready":
                    fail()
                if any(inspect_state(alias, oci_index) != "missing" for alias in aliases):
                    fail()
                for alias in aliases:
                    published_aliases.append(alias)
                    returncode, unused, error = run_docker(["tag", oci_index, alias], capture=True)
                    del unused, error
                    if returncode != 0 or not inspect(alias, oci_index):
                        fail()
                published_receipt = publish_receipt(receipts_fd, sha, payload)
                committed = True
                clear_cleanup_authority(receipts_fd, sha, cleanup)
            except BaseException:
                if not committed:
                    for alias in aliases:
                        if inspect_state(alias, oci_index) == "ready" and alias not in published_aliases:
                            published_aliases.append(alias)
                    for alias in reversed(aliases):
                        remove_alias(alias, oci_index)
                    rollback_receipt(receipts_fd, sha, published_receipt)
                    clear_cleanup_authority(receipts_fd, sha, cleanup)
                raise
            finally:
                os.set_inheritable(archive_fd, False)
                clear_transfer(staging_fd, record)
            if not committed:
                fail()
            print(payload.decode().strip())
    finally:
        os.close(lock_fd)
        os.close(receipts_fd)
        os.close(staging_fd)
        os.close(core_fd)
else:
    fail()
"""


_REMOTE_RSYNC_LEASE_SCRIPT = r"""
import fcntl
import os
from pathlib import Path
import resource
import stat
import sys

service = Path(sys.argv[1])
transfer = sys.argv[2]
staging_identity = (int(sys.argv[3]), int(sys.argv[4]))
incoming_identity = (int(sys.argv[5]), int(sys.argv[6]))
archive_size = int(sys.argv[7])
command = sys.argv[8:]
staging = service / "managed-runtime-staging"
incoming = staging / ("incoming-" + transfer)
directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
if not service.is_absolute():
    raise SystemExit(1)
service_fd = os.open("/", directory_flags)
try:
    for part in service.parts[1:]:
        next_fd = os.open(part, directory_flags, dir_fd=service_fd)
        os.close(service_fd)
        service_fd = next_fd
    service_value = os.fstat(service_fd)
    if (
        not stat.S_ISDIR(service_value.st_mode)
        or service_value.st_uid != os.getuid()
        or stat.S_IMODE(service_value.st_mode) != 0o700
    ):
        raise SystemExit(1)
except BaseException:
    os.close(service_fd)
    raise
staging_fd = os.open("managed-runtime-staging", directory_flags, dir_fd=service_fd)
incoming_fd = os.open(incoming.name, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=staging_fd)
staging_value = os.fstat(staging_fd)
incoming_value = os.fstat(incoming_fd)
if (
    not stat.S_ISDIR(staging_value.st_mode)
    or stat.S_ISLNK(staging_value.st_mode)
    or staging_value.st_uid != os.getuid()
    or stat.S_IMODE(staging_value.st_mode) != 0o700
    or (staging_value.st_dev, staging_value.st_ino) != staging_identity
    or not stat.S_ISDIR(incoming_value.st_mode)
    or stat.S_ISLNK(incoming_value.st_mode)
    or incoming_value.st_uid != os.getuid()
    or stat.S_IMODE(incoming_value.st_mode) != 0o700
    or (incoming_value.st_dev, incoming_value.st_ino) != incoming_identity
):
    raise SystemExit(1)
lock_fd = os.open(".openevo-runtime-transfer.lock", os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=incoming_fd)
lock_value = os.fstat(lock_fd)
current_lock = os.stat(
    ".openevo-runtime-transfer.lock", dir_fd=incoming_fd, follow_symlinks=False
)
if (
    not stat.S_ISREG(lock_value.st_mode)
    or lock_value.st_uid != os.getuid()
    or lock_value.st_nlink != 1
    or stat.S_IMODE(lock_value.st_mode) != 0o600
    or lock_value.st_size != 0
    or (lock_value.st_dev, lock_value.st_ino)
    != (current_lock.st_dev, current_lock.st_ino)
    or not 0 < archive_size <= 512 * 1024 * 1024
    or not command
    or command[0] != "/usr/bin/rsync"
):
    raise SystemExit(1)
fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
current_lock = os.stat(
    ".openevo-runtime-transfer.lock", dir_fd=incoming_fd, follow_symlinks=False
)
if (lock_value.st_dev, lock_value.st_ino) != (
    current_lock.st_dev,
    current_lock.st_ino,
):
    raise SystemExit(1)
resource.setrlimit(resource.RLIMIT_FSIZE, (archive_size, archive_size))
os.set_inheritable(lock_fd, True)
os.execv(command[0], command)
"""


__all__ = (
    "MANAGED_RUNTIME_STAGING_MAX_TRANSFERS",
    "MANAGED_RUNTIME_TRANSFER_LEASE",
    "ManagedRuntimeArchiveSnapshot",
    "ManagedRuntimeArchiveSnapshotError",
    "ManagedRuntimeLoadReceipt",
    "ManagedRuntimeTransfer",
    "OpenedManagedRuntimeArchive",
    "build_daemon_managed_runtime_discard_command",
    "build_daemon_managed_runtime_finalize_command",
    "build_daemon_managed_runtime_prepare_command",
    "build_daemon_managed_runtime_probe_command",
    "build_daemon_managed_runtime_receive_command",
    "build_managed_runtime_discard_command",
    "build_managed_runtime_finalize_command",
    "build_managed_runtime_prepare_command",
    "build_managed_runtime_probe_command",
    "build_managed_runtime_rsync_path",
    "parse_managed_runtime_prepare",
    "parse_managed_runtime_probe",
    "parse_managed_runtime_discard",
    "parse_managed_runtime_receive",
    "parse_managed_runtime_receipt",
    "snapshot_managed_runtime_archive",
)
