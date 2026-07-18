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


MAX_CORE_WHEEL_BYTES = 512 * 1024 * 1024
MAX_FRAMEWORK_LOCK_BYTES = 64 * 1024
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_BUNDLE_ID = _DIGEST
_TRANSFER_ID = re.compile(r"[0-9a-f]{32}\Z")
_WHEEL_NAME = re.compile(r"[A-Za-z0-9_.+-]+\.whl\Z")
_REMOTE_PATH = re.compile(r"/(?:[A-Za-z0-9._@%+=,-]+/)*[A-Za-z0-9._@%+=,-]+\Z")
_MAX_STAGE_RESPONSE_BYTES = 4096
CORE_ASSET_TRANSFER_LEASE = ".openevo-transfer.lock"


class CoreBootstrapAssetSnapshotError(ValueError):
    """A renderer-safe local sealed-asset validation failure."""


@dataclass(frozen=True, slots=True, repr=False)
class StagedCoreBootstrapAssets:
    service_root: str
    wheel_path: str
    framework_lock_path: str
    wheel_sha256: str
    framework_lock_sha256: str
    wheel_size: int
    framework_lock_size: int
    bundle_device: int
    bundle_inode: int
    wheel_device: int
    wheel_inode: int
    framework_lock_device: int
    framework_lock_inode: int

    def __post_init__(self) -> None:
        wheel_parent = Path(self.wheel_path).parent
        for value in (self.service_root, self.wheel_path, self.framework_lock_path):
            _require_remote_path(value)
        if (
            not isinstance(self.wheel_sha256, str)
            or not isinstance(self.framework_lock_sha256, str)
            or _DIGEST.fullmatch(self.wheel_sha256) is None
            or _DIGEST.fullmatch(self.framework_lock_sha256) is None
            or Path(self.framework_lock_path).name != "framework-lock.json"
            or wheel_parent != Path(self.framework_lock_path).parent
            or wheel_parent.parent != Path(self.service_root) / "assets"
            or _BUNDLE_ID.fullmatch(wheel_parent.name) is None
            or not Path(self.wheel_path).name.endswith(".whl")
            or type(self.wheel_size) is not int
            or not 0 < self.wheel_size <= MAX_CORE_WHEEL_BYTES
            or type(self.framework_lock_size) is not int
            or not 0 < self.framework_lock_size <= MAX_FRAMEWORK_LOCK_BYTES
            or any(
                type(value) is not int or value < 0
                for value in (
                    self.bundle_device,
                    self.wheel_device,
                    self.framework_lock_device,
                )
            )
            or any(
                type(value) is not int or value <= 0
                for value in (
                    self.bundle_inode,
                    self.wheel_inode,
                    self.framework_lock_inode,
                )
            )
        ):
            raise ValueError("staged Core bootstrap asset identity is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class CoreBootstrapAssetSnapshot:
    root: Path
    wheel_path: Path
    framework_lock_path: Path
    wheel_filename: str
    wheel_sha256: str
    framework_lock_sha256: str
    wheel_size: int
    framework_lock_size: int


@contextmanager
def snapshot_core_bootstrap_assets(
    *,
    wheel_path: str,
    wheel_sha256: str,
    wheel_size: int,
    framework_lock_path: str,
    framework_lock_sha256: str,
    framework_lock_size: int,
) -> Iterator[CoreBootstrapAssetSnapshot]:
    try:
        _validate_asset_request(
            wheel_path=wheel_path,
            wheel_sha256=wheel_sha256,
            wheel_size=wheel_size,
            framework_lock_path=framework_lock_path,
            framework_lock_sha256=framework_lock_sha256,
            framework_lock_size=framework_lock_size,
        )
        wheel_filename = Path(wheel_path).name
        if _WHEEL_NAME.fullmatch(wheel_filename) is None:
            raise ValueError("Core wheel filename is invalid")
    except (OSError, TypeError, ValueError) as exc:
        raise CoreBootstrapAssetSnapshotError(
            "sealed Core bootstrap asset identity is invalid"
        ) from exc
    with tempfile.TemporaryDirectory(prefix="openevo-core-assets-") as temporary:
        root = Path(temporary)
        copied_wheel = root / wheel_filename
        copied_lock = root / "framework-lock.json"
        try:
            os.chmod(root, 0o700)
            _copy_verified_asset(
                wheel_path,
                copied_wheel,
                expected_digest=wheel_sha256,
                expected_size=wheel_size,
                max_size=MAX_CORE_WHEEL_BYTES,
            )
            lock_bytes = _copy_verified_asset(
                framework_lock_path,
                copied_lock,
                expected_digest=framework_lock_sha256,
                expected_size=framework_lock_size,
                max_size=MAX_FRAMEWORK_LOCK_BYTES,
            )
            _verify_framework_lock(
                lock_bytes,
                wheel_filename=wheel_filename,
                wheel_sha256=wheel_sha256,
            )
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(root_fd)
            finally:
                os.close(root_fd)
        except (OSError, TypeError, ValueError) as exc:
            raise CoreBootstrapAssetSnapshotError(
                "sealed Core bootstrap asset identity is invalid"
            ) from exc
        yield CoreBootstrapAssetSnapshot(
            root=root,
            wheel_path=copied_wheel,
            framework_lock_path=copied_lock,
            wheel_filename=wheel_filename,
            wheel_sha256=wheel_sha256,
            framework_lock_sha256=framework_lock_sha256,
            wheel_size=wheel_size,
            framework_lock_size=framework_lock_size,
        )


def build_core_asset_prepare_command(
    bundle_id: str,
    runtime: CorePythonRuntimeAuthority,
) -> str:
    _require_bundle_id(bundle_id)
    return build_verified_python_command(runtime, _REMOTE_PREPARE_SCRIPT, bundle_id)


def build_core_asset_rsync_path(
    *,
    service_root: str,
    bundle_id: str,
    transfer_id: str,
) -> str:
    _require_remote_path(service_root)
    _require_bundle_id(bundle_id)
    if not isinstance(transfer_id, str) or _TRANSFER_ID.fullmatch(transfer_id) is None:
        raise ValueError("Core asset transfer identity is invalid")
    arguments = (service_root, bundle_id, transfer_id)
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


def parse_core_asset_prepare(
    payload: SecretStr,
    *,
    bundle_id: str,
) -> tuple[str, str, str]:
    _require_bundle_id(bundle_id)
    value = _load_secret_json(payload)
    if not isinstance(value, dict) or set(value) != {
        "incoming_root",
        "schema_version",
        "service_root",
        "transfer_id",
    }:
        raise ValueError("Core asset prepare response is invalid")
    service_root = value.get("service_root")
    incoming_root = value.get("incoming_root")
    transfer_id = value.get("transfer_id")
    if (
        type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or not isinstance(service_root, str)
        or not isinstance(incoming_root, str)
        or not isinstance(transfer_id, str)
        or _TRANSFER_ID.fullmatch(transfer_id) is None
    ):
        raise ValueError("Core asset prepare response is invalid")
    _require_remote_path(service_root)
    _require_remote_path(incoming_root)
    if incoming_root != (f"{service_root}/asset-staging/incoming-{bundle_id}-{transfer_id}"):
        raise ValueError("Core asset prepare response identity is invalid")
    return service_root, incoming_root, transfer_id


def build_core_asset_finalize_command(
    *,
    runtime: CorePythonRuntimeAuthority,
    service_root: str,
    bundle_id: str,
    transfer_id: str,
    wheel_filename: str,
    wheel_sha256: str,
    wheel_size: int,
    framework_lock_sha256: str,
    framework_lock_size: int,
) -> str:
    _require_remote_path(service_root)
    _require_bundle_id(bundle_id)
    if (
        not isinstance(wheel_filename, str)
        or not isinstance(wheel_sha256, str)
        or not isinstance(framework_lock_sha256, str)
        or _WHEEL_NAME.fullmatch(wheel_filename) is None
        or not isinstance(transfer_id, str)
        or _TRANSFER_ID.fullmatch(transfer_id) is None
        or _DIGEST.fullmatch(wheel_sha256) is None
        or _DIGEST.fullmatch(framework_lock_sha256) is None
        or type(wheel_size) is not int
        or not 0 < wheel_size <= MAX_CORE_WHEEL_BYTES
        or type(framework_lock_size) is not int
        or not 0 < framework_lock_size <= MAX_FRAMEWORK_LOCK_BYTES
    ):
        raise ValueError("Core asset finalize identity is invalid")
    arguments = (
        service_root,
        bundle_id,
        transfer_id,
        wheel_filename,
        wheel_sha256,
        str(wheel_size),
        framework_lock_sha256,
        str(framework_lock_size),
    )
    return build_verified_python_command(runtime, _REMOTE_FINALIZE_SCRIPT, *arguments)


def build_core_asset_discard_command(
    *,
    runtime: CorePythonRuntimeAuthority,
    service_root: str,
    bundle_id: str,
    transfer_id: str,
) -> str:
    _require_remote_path(service_root)
    _require_bundle_id(bundle_id)
    if not isinstance(transfer_id, str) or _TRANSFER_ID.fullmatch(transfer_id) is None:
        raise ValueError("Core asset transfer identity is invalid")
    return build_verified_python_command(
        runtime,
        _REMOTE_DISCARD_SCRIPT,
        service_root,
        bundle_id,
        transfer_id,
    )


def parse_staged_core_assets(
    payload: SecretStr,
    *,
    service_root: str,
    bundle_id: str,
    wheel_filename: str,
    wheel_sha256: str,
    wheel_size: int,
    framework_lock_sha256: str,
    framework_lock_size: int,
) -> StagedCoreBootstrapAssets:
    value = _load_secret_json(payload)
    expected_fields = {
        "framework_lock_path",
        "framework_lock_sha256",
        "framework_lock_device",
        "framework_lock_inode",
        "bundle_device",
        "bundle_inode",
        "schema_version",
        "service_root",
        "wheel_path",
        "wheel_sha256",
        "wheel_device",
        "wheel_inode",
    }
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise ValueError("Core asset finalize response is invalid")
    identity_fields = (
        "bundle_device",
        "bundle_inode",
        "wheel_device",
        "wheel_inode",
        "framework_lock_device",
        "framework_lock_inode",
    )
    if type(value.get("schema_version")) is not int or any(
        type(value.get(field)) is not int for field in identity_fields
    ):
        raise ValueError("Core asset finalize response is invalid")
    expected_root = f"{service_root}/assets/{bundle_id}"
    expected_wheel = f"{expected_root}/{wheel_filename}"
    expected_lock = f"{expected_root}/framework-lock.json"
    fixed = {
        "schema_version": 1,
        "service_root": service_root,
        "wheel_path": expected_wheel,
        "framework_lock_path": expected_lock,
        "wheel_sha256": wheel_sha256,
        "framework_lock_sha256": framework_lock_sha256,
    }
    if any(value.get(key) != expected for key, expected in fixed.items()) or any(
        value[field] < (1 if field.endswith("inode") else 0) for field in identity_fields
    ):
        raise ValueError("Core asset finalize response identity is invalid")
    return StagedCoreBootstrapAssets(
        service_root=service_root,
        wheel_path=expected_wheel,
        framework_lock_path=expected_lock,
        wheel_sha256=wheel_sha256,
        framework_lock_sha256=framework_lock_sha256,
        wheel_size=wheel_size,
        framework_lock_size=framework_lock_size,
        bundle_device=value["bundle_device"],
        bundle_inode=value["bundle_inode"],
        wheel_device=value["wheel_device"],
        wheel_inode=value["wheel_inode"],
        framework_lock_device=value["framework_lock_device"],
        framework_lock_inode=value["framework_lock_inode"],
    )


def build_core_asset_consumer_command(
    command: str,
    assets: StagedCoreBootstrapAssets,
    runtime: CorePythonRuntimeAuthority,
) -> str:
    if not isinstance(command, str) or not command or "\x00" in command:
        raise ValueError("Core asset consumer command is invalid")
    final_root = str(Path(assets.wheel_path).parent)
    if assets.wheel_path not in command or assets.framework_lock_path not in command:
        raise ValueError("Core asset consumer command does not bind both release assets")
    arguments = (
        assets.service_root,
        Path(final_root).name,
        Path(assets.wheel_path).name,
        assets.wheel_sha256,
        str(assets.wheel_size),
        assets.framework_lock_sha256,
        str(assets.framework_lock_size),
        str(assets.bundle_device),
        str(assets.bundle_inode),
        str(assets.wheel_device),
        str(assets.wheel_inode),
        str(assets.framework_lock_device),
        str(assets.framework_lock_inode),
        command,
    )
    return build_verified_python_command(runtime, _REMOTE_CONSUME_SCRIPT, *arguments)


def _validate_asset_request(
    *,
    wheel_path: str,
    wheel_sha256: str,
    wheel_size: int,
    framework_lock_path: str,
    framework_lock_sha256: str,
    framework_lock_size: int,
) -> None:
    if (
        not isinstance(wheel_path, str)
        or not Path(wheel_path).is_absolute()
        or not isinstance(framework_lock_path, str)
        or not Path(framework_lock_path).is_absolute()
        or _DIGEST.fullmatch(wheel_sha256) is None
        or _DIGEST.fullmatch(framework_lock_sha256) is None
        or type(wheel_size) is not int
        or not 0 < wheel_size <= MAX_CORE_WHEEL_BYTES
        or type(framework_lock_size) is not int
        or not 0 < framework_lock_size <= MAX_FRAMEWORK_LOCK_BYTES
    ):
        raise ValueError("Core bootstrap asset request is invalid")


def _copy_verified_asset(
    source_path: str,
    destination: Path,
    *,
    expected_digest: str,
    expected_size: int,
    max_size: int,
) -> bytes:
    source_parent_fd, source_fd, source_name = _open_absolute_asset(source_path)
    destination_fd = -1
    chunks: list[bytes] = []
    try:
        before = os.fstat(source_fd)
        current = os.stat(source_name, dir_fd=source_parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid not in {0, os.geteuid()}
            or stat.S_IMODE(before.st_mode) & 0o022
            or before.st_size != expected_size
            or not 0 < before.st_size <= max_size
            or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise ValueError("sealed Core bootstrap asset is invalid")
        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        digest = hashlib.sha256()
        remaining = before.st_size
        while remaining:
            chunk = os.read(source_fd, min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError("sealed Core bootstrap asset was truncated")
            digest.update(chunk)
            written = 0
            while written < len(chunk):
                count = os.write(destination_fd, chunk[written:])
                if count <= 0:
                    raise OSError("sealed Core bootstrap asset snapshot write failed")
                written += count
            if before.st_size <= MAX_FRAMEWORK_LOCK_BYTES:
                chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(source_fd, 1):
            raise ValueError("sealed Core bootstrap asset grew while reading")
        os.fchmod(destination_fd, 0o600)
        os.fsync(destination_fd)
        after = os.fstat(source_fd)
        current_after = os.stat(
            source_name,
            dir_fd=source_parent_fd,
            follow_symlinks=False,
        )
        rebound = _stat_absolute_asset(source_path)
        identity = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if (
            any(getattr(before, name) != getattr(after, name) for name in identity)
            or (after.st_dev, after.st_ino) != (current_after.st_dev, current_after.st_ino)
            or (after.st_dev, after.st_ino) != (rebound.st_dev, rebound.st_ino)
            or digest.hexdigest() != expected_digest
            or os.fstat(destination_fd).st_size != expected_size
        ):
            raise ValueError("sealed Core bootstrap asset identity changed")
        return b"".join(chunks)
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        os.close(source_fd)
        os.close(source_parent_fd)


def _open_absolute_asset(path: str) -> tuple[int, int, str]:
    parts = path.split("/")
    if len(parts) < 2 or parts[0] or any(part in {"", ".", ".."} for part in parts[1:]):
        raise ValueError("sealed Core bootstrap asset path is invalid")
    directory_fd = os.open(
        "/",
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        for part in parts[1:-1]:
            child_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = child_fd
        source_fd = os.open(
            parts[-1],
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
        return directory_fd, source_fd, parts[-1]
    except BaseException:
        os.close(directory_fd)
        raise


def _stat_absolute_asset(path: str) -> os.stat_result:
    parent_fd, source_fd, source_name = _open_absolute_asset(path)
    try:
        opened = os.fstat(source_fd)
        current = os.stat(source_name, dir_fd=parent_fd, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("sealed Core bootstrap asset path identity changed")
        return current
    finally:
        os.close(source_fd)
        os.close(parent_fd)


def _verify_framework_lock(
    payload: bytes,
    *,
    wheel_filename: str,
    wheel_sha256: str,
) -> None:
    try:
        value = json.loads(payload, object_pairs_hook=_closed_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("framework lock is invalid") from exc
    expected = {
        "distribution",
        "distribution_digest",
        "distribution_version",
        "schema_version",
        "wheel_filename",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or value.get("schema_version") != "1"
        or value.get("distribution") != "openevo"
        or not isinstance(value.get("distribution_version"), str)
        or not value["distribution_version"]
        or value.get("distribution_digest") != wheel_sha256
        or value.get("wheel_filename") != wheel_filename
    ):
        raise ValueError("framework lock does not bind the sealed Core wheel")


def _load_secret_json(payload: SecretStr) -> object:
    if not isinstance(payload, SecretStr):
        raise ValueError("Core asset response is not secret-bearing")
    raw = payload.get_secret_value()
    try:
        encoded = raw.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("Core asset response is invalid") from exc
    if len(encoded) > _MAX_STAGE_RESPONSE_BYTES:
        raise ValueError("Core asset response is invalid")
    try:
        return json.loads(encoded, object_pairs_hook=_closed_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("Core asset response is invalid") from exc


def _closed_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _require_remote_path(path: str) -> None:
    if (
        not isinstance(path, str)
        or _REMOTE_PATH.fullmatch(path) is None
        or any(part in {"", ".", ".."} for part in path.split("/")[1:])
    ):
        raise ValueError("remote Core asset path is invalid")


def _require_bundle_id(bundle_id: str) -> None:
    if not isinstance(bundle_id, str) or _BUNDLE_ID.fullmatch(bundle_id) is None:
        raise ValueError("Core asset bundle identity is invalid")


_REMOTE_RSYNC_LEASE_SCRIPT = r"""
import fcntl, os, pwd, stat, sys

service_root, bundle, transfer, executable, *arguments = sys.argv[1:]
uid = os.geteuid()
home = pwd.getpwuid(uid).pw_dir
home_parts = home.split("/")[1:]
allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._@%+=,-")
if (not hasattr(os, "O_NOFOLLOW") or not home_parts
        or any(not part or part in {".", ".."} or any(c not in allowed for c in part) for part in home_parts)
        or home != "/" + "/".join(home_parts)
        or service_root != home + "/.openevo/core"
        or len(bundle) != 64 or any(c not in "0123456789abcdef" for c in bundle)
        or len(transfer) != 32 or any(c not in "0123456789abcdef" for c in transfer)
        or executable != "/usr/bin/rsync" or not arguments):
    raise SystemExit(70)

dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
lock_flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW

def open_absolute(path):
    fd = os.open("/", dir_flags)
    try:
        for part in [item for item in path.split("/") if item]:
            child = os.open(part, dir_flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise

def require_dir(fd, mode):
    meta = os.fstat(fd)
    if (not stat.S_ISDIR(meta.st_mode) or meta.st_uid != uid
            or stat.S_IMODE(meta.st_mode) != mode):
        raise SystemExit(71)

core_fd = open_absolute(service_root)
try:
    require_dir(core_fd, 0o700)
    staging_fd = os.open("asset-staging", dir_flags, dir_fd=core_fd)
    try:
        require_dir(staging_fd, 0o700)
        incoming_name = "incoming-" + bundle + "-" + transfer
        incoming_fd = os.open(incoming_name, dir_flags, dir_fd=staging_fd)
        try:
            require_dir(incoming_fd, 0o700)
            current = os.stat(incoming_name, dir_fd=staging_fd, follow_symlinks=False)
            opened = os.fstat(incoming_fd)
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise SystemExit(72)
            lease_fd = os.open(".openevo-transfer.lock", lock_flags, dir_fd=incoming_fd)
            lease = os.fstat(lease_fd)
            current_lease = os.stat(
                ".openevo-transfer.lock", dir_fd=incoming_fd, follow_symlinks=False
            )
            if (not stat.S_ISREG(lease.st_mode) or lease.st_uid != uid or lease.st_nlink != 1
                    or stat.S_IMODE(lease.st_mode) != 0o600
                    or (lease.st_dev, lease.st_ino)
                        != (current_lease.st_dev, current_lease.st_ino)):
                raise SystemExit(73)
            fcntl.flock(lease_fd, fcntl.LOCK_EX)
            current_lease = os.stat(
                ".openevo-transfer.lock", dir_fd=incoming_fd, follow_symlinks=False
            )
            if (lease.st_dev, lease.st_ino) != (current_lease.st_dev, current_lease.st_ino):
                raise SystemExit(73)
            os.set_inheritable(lease_fd, True)
        finally:
            os.close(incoming_fd)
    finally:
        os.close(staging_fd)
finally:
    os.close(core_fd)

os.execv(executable, [executable, *arguments])
""".strip()


_REMOTE_PREPARE_SCRIPT = r"""
import fcntl, json, os, pwd, secrets, stat, sys, time

bundle = sys.argv[1]
if len(bundle) != 64 or any(c not in "0123456789abcdef" for c in bundle):
    raise SystemExit(70)
uid = os.geteuid()
home = pwd.getpwuid(uid).pw_dir
home_parts = home.split("/")[1:]
allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._@%+=,-")
if (not hasattr(os, "O_NOFOLLOW") or not home_parts
        or any(not part or part in {".", ".."} or any(c not in allowed for c in part) for part in home_parts)
        or home != "/" + "/".join(home_parts)):
    raise SystemExit(71)

flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
max_staging_entries = 32
max_incoming_attempts = 16
stale_incoming_seconds = 600
max_asset_entries = 1024

def open_absolute(path):
    fd = os.open("/", flags)
    try:
        for part in [item for item in path.split("/") if item]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise

def require_dir(fd, mode=None):
    meta = os.fstat(fd)
    if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != uid:
        raise SystemExit(72)
    if mode is not None and stat.S_IMODE(meta.st_mode) != mode:
        raise SystemExit(73)

def ensure(parent, name):
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
        os.fsync(parent)
    except FileExistsError:
        pass
    fd = os.open(name, flags, dir_fd=parent)
    require_dir(fd, 0o700)
    current = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if (current.st_dev, current.st_ino) != (os.fstat(fd).st_dev, os.fstat(fd).st_ino):
        raise SystemExit(74)
    return fd

def bounded_names(fd, maximum):
    names = []
    with os.scandir(fd) as entries:
        for entry in entries:
            if len(names) >= maximum:
                raise SystemExit(75)
            names.append(entry.name)
    return names

def closed_attempt_name(name, prefix):
    value = name[len(prefix):] if name.startswith(prefix) else ""
    parts = value.split("-")
    return (len(parts) == 2 and len(parts[0]) == 64 and len(parts[1]) == 32
            and all(c in "0123456789abcdef" for part in parts for c in part))

def clear_private_attempt(parent, attempt_name, require_transfer_lease=False):
    fd = os.open(attempt_name, flags, dir_fd=parent)
    lease_fd = None
    try:
        before = os.fstat(fd)
        if (not stat.S_ISDIR(before.st_mode) or before.st_uid != uid
                or stat.S_IMODE(before.st_mode) not in {0o500, 0o700}):
            raise SystemExit(76)
        if require_transfer_lease:
            lease_fd = os.open(
                ".openevo-transfer.lock",
                os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=fd,
            )
            lease = os.fstat(lease_fd)
            current_lease = os.stat(
                ".openevo-transfer.lock", dir_fd=fd, follow_symlinks=False
            )
            if (not stat.S_ISREG(lease.st_mode) or lease.st_uid != uid
                    or lease.st_nlink != 1 or stat.S_IMODE(lease.st_mode) != 0o600
                    or (lease.st_dev, lease.st_ino)
                        != (current_lease.st_dev, current_lease.st_ino)):
                raise SystemExit(76)
            try:
                fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            current_lease = os.stat(
                ".openevo-transfer.lock", dir_fd=fd, follow_symlinks=False
            )
            if (lease.st_dev, lease.st_ino) != (
                current_lease.st_dev,
                current_lease.st_ino,
            ):
                raise SystemExit(76)
        os.fchmod(fd, 0o700)
        names = bounded_names(fd, max_staging_entries)
        for child_name in names:
            child_fd = os.open(child_name, file_flags, dir_fd=fd)
            try:
                opened = os.fstat(child_fd)
                current = os.stat(child_name, dir_fd=fd, follow_symlinks=False)
                if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != uid
                        or opened.st_nlink != 1
                        or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
                    raise SystemExit(76)
                os.unlink(child_name, dir_fd=fd)
            finally:
                os.close(child_fd)
        os.fsync(fd)
        current = os.stat(attempt_name, dir_fd=parent, follow_symlinks=False)
        if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
            raise SystemExit(76)
    finally:
        if lease_fd is not None:
            os.close(lease_fd)
        os.close(fd)
    os.rmdir(attempt_name, dir_fd=parent)
    os.fsync(parent)
    return True

def reconcile_incoming_attempt(parent, attempt_name, stale_before_ns):
    fd = os.open(attempt_name, flags, dir_fd=parent)
    try:
        opened = os.fstat(fd)
        current = os.stat(attempt_name, dir_fd=parent, follow_symlinks=False)
        if (not stat.S_ISDIR(opened.st_mode) or opened.st_uid != uid
                or stat.S_IMODE(opened.st_mode) != 0o700
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
            raise SystemExit(76)
        names = bounded_names(fd, max_staging_entries)
        if ".openevo-transfer.lock" not in names:
            if names:
                raise SystemExit(76)
            current = os.stat(attempt_name, dir_fd=parent, follow_symlinks=False)
            if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                raise SystemExit(76)
            os.rmdir(attempt_name, dir_fd=parent)
            os.fsync(parent)
            try:
                os.stat(attempt_name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise SystemExit(76)
            return
        lease_fd = os.open(
            ".openevo-transfer.lock",
            os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=fd,
        )
        try:
            lease = os.fstat(lease_fd)
            current_lease = os.stat(
                ".openevo-transfer.lock", dir_fd=fd, follow_symlinks=False
            )
            if (not stat.S_ISREG(lease.st_mode) or lease.st_uid != uid
                    or lease.st_nlink != 1 or stat.S_IMODE(lease.st_mode) != 0o600
                    or (lease.st_dev, lease.st_ino)
                        != (current_lease.st_dev, current_lease.st_ino)):
                raise SystemExit(76)
        finally:
            os.close(lease_fd)
        if opened.st_mtime_ns > stale_before_ns:
            return
    finally:
        os.close(fd)
    clear_private_attempt(parent, attempt_name, require_transfer_lease=True)

def reconcile_staging(fd):
    names = bounded_names(fd, max_staging_entries)
    for name in names:
        if closed_attempt_name(name, "retired-") or closed_attempt_name(name, "publish-"):
            try:
                clear_private_attempt(fd, name)
            except FileNotFoundError:
                pass
    stale_before_ns = time.time_ns() - stale_incoming_seconds * 1000000000
    names = bounded_names(fd, max_staging_entries)
    for name in names:
        if not closed_attempt_name(name, "incoming-"):
            continue
        try:
            reconcile_incoming_attempt(fd, name, stale_before_ns)
        except FileNotFoundError:
            pass
    remaining = bounded_names(fd, max_staging_entries)
    if any(not closed_attempt_name(name, "incoming-") for name in remaining):
        raise SystemExit(75)
    if len(remaining) >= max_incoming_attempts:
        raise SystemExit(75)

def reconcile_assets(fd):
    names = bounded_names(fd, max_asset_entries)
    for name in names:
        if closed_attempt_name(name, "publish-"):
            try:
                clear_private_attempt(fd, name)
            except FileNotFoundError:
                pass
        elif len(name) != 64 or any(c not in "0123456789abcdef" for c in name):
            raise SystemExit(75)

home_fd = open_absolute(home)
try:
    require_dir(home_fd)
    openevo_fd = ensure(home_fd, ".openevo")
    try:
        core_fd = ensure(openevo_fd, "core")
        try:
            lock_fd = os.open("asset-publish.lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600, dir_fd=core_fd)
            try:
                lock_meta = os.fstat(lock_fd)
                current_lock = os.stat(
                    "asset-publish.lock", dir_fd=core_fd, follow_symlinks=False
                )
                if (not stat.S_ISREG(lock_meta.st_mode) or lock_meta.st_uid != uid
                        or lock_meta.st_nlink != 1 or stat.S_IMODE(lock_meta.st_mode) != 0o600
                        or (lock_meta.st_dev, lock_meta.st_ino)
                            != (current_lock.st_dev, current_lock.st_ino)):
                    raise SystemExit(77)
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                current_lock = os.stat(
                    "asset-publish.lock", dir_fd=core_fd, follow_symlinks=False
                )
                if (lock_meta.st_dev, lock_meta.st_ino) != (
                    current_lock.st_dev,
                    current_lock.st_ino,
                ):
                    raise SystemExit(77)
                staging_fd = ensure(core_fd, "asset-staging")
                assets_fd = ensure(core_fd, "assets")
                try:
                    reconcile_staging(staging_fd)
                    reconcile_assets(assets_fd)
                    for unused in range(4):
                        transfer = secrets.token_hex(16)
                        incoming = "incoming-" + bundle + "-" + transfer
                        try:
                            os.mkdir(incoming, 0o700, dir_fd=staging_fd)
                            os.fsync(staging_fd)
                            break
                        except FileExistsError:
                            continue
                    else:
                        raise SystemExit(78)
                    incoming_fd = os.open(incoming, flags, dir_fd=staging_fd)
                    try:
                        require_dir(incoming_fd, 0o700)
                        current_incoming = os.stat(
                            incoming, dir_fd=staging_fd, follow_symlinks=False
                        )
                        opened_incoming = os.fstat(incoming_fd)
                        if (current_incoming.st_dev, current_incoming.st_ino) != (
                            opened_incoming.st_dev,
                            opened_incoming.st_ino,
                        ):
                            raise SystemExit(78)
                        lease_fd = os.open(
                            ".openevo-transfer.lock",
                            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                            0o600,
                            dir_fd=incoming_fd,
                        )
                        try:
                            lease = os.fstat(lease_fd)
                            current_lease = os.stat(
                                ".openevo-transfer.lock",
                                dir_fd=incoming_fd,
                                follow_symlinks=False,
                            )
                            if (not stat.S_ISREG(lease.st_mode) or lease.st_uid != uid
                                    or lease.st_nlink != 1
                                    or stat.S_IMODE(lease.st_mode) != 0o600
                                    or (lease.st_dev, lease.st_ino) != (
                                        current_lease.st_dev,
                                        current_lease.st_ino,
                                    )):
                                raise SystemExit(78)
                            os.fsync(lease_fd)
                            os.fsync(incoming_fd)
                            current_lease = os.stat(
                                ".openevo-transfer.lock",
                                dir_fd=incoming_fd,
                                follow_symlinks=False,
                            )
                            current_incoming = os.stat(
                                incoming, dir_fd=staging_fd, follow_symlinks=False
                            )
                            if ((lease.st_dev, lease.st_ino) != (
                                    current_lease.st_dev, current_lease.st_ino)
                                    or (opened_incoming.st_dev, opened_incoming.st_ino) != (
                                        current_incoming.st_dev, current_incoming.st_ino
                                    )):
                                raise SystemExit(78)
                        finally:
                            os.close(lease_fd)
                    finally:
                        os.close(incoming_fd)
                finally:
                    os.close(assets_fd)
                    os.close(staging_fd)
            finally:
                os.close(lock_fd)
        finally:
            os.close(core_fd)
    finally:
        os.close(openevo_fd)
finally:
    os.close(home_fd)

root = home + "/.openevo/core"
print(json.dumps({
    "schema_version": 1,
    "service_root": root,
    "incoming_root": root + "/asset-staging/" + incoming,
    "transfer_id": transfer,
}, sort_keys=True, separators=(",", ":")))
""".strip()


_REMOTE_DISCARD_SCRIPT = r"""
import ctypes, errno, fcntl, os, pwd, stat, sys

service_root, bundle, transfer = sys.argv[1:]
uid = os.geteuid()
home = pwd.getpwuid(uid).pw_dir
home_parts = home.split("/")[1:]
allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._@%+=,-")
if (not hasattr(os, "O_NOFOLLOW") or not home_parts
        or any(not part or part in {".", ".."} or any(c not in allowed for c in part) for part in home_parts)
        or home != "/" + "/".join(home_parts)
        or service_root != home + "/.openevo/core"
        or len(bundle) != 64 or any(c not in "0123456789abcdef" for c in bundle)
        or len(transfer) != 32 or any(c not in "0123456789abcdef" for c in transfer)):
    raise SystemExit(70)

dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW

def open_absolute(path):
    fd = os.open("/", dir_flags)
    try:
        for part in [item for item in path.split("/") if item]:
            child = os.open(part, dir_flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise

def require_dir(fd):
    meta = os.fstat(fd)
    if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != uid or stat.S_IMODE(meta.st_mode) != 0o700:
        raise SystemExit(71)

def open_child_dir(parent, name):
    fd = os.open(name, dir_flags, dir_fd=parent)
    require_dir(fd)
    current = os.stat(name, dir_fd=parent, follow_symlinks=False)
    opened = os.fstat(fd)
    if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
        raise SystemExit(72)
    return fd

def rename_noreplace(parent, source_name, destination_name):
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise SystemExit(73)
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(parent, os.fsencode(source_name), parent, os.fsencode(destination_name), 1)
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise SystemExit(73)
    raise OSError(error, "Core asset transfer retirement failed")

def clear_retired(parent, name, fd):
    before = os.fstat(fd)
    names = []
    with os.scandir(fd) as entries:
        for entry in entries:
            if len(names) >= 16:
                return
            names.append(entry.name)
    for child_name in names:
        try:
            child_fd = os.open(child_name, file_flags, dir_fd=fd)
        except OSError:
            return
        try:
            child = os.fstat(child_fd)
            current = os.stat(child_name, dir_fd=fd, follow_symlinks=False)
            if (not stat.S_ISREG(child.st_mode) or child.st_uid != uid or child.st_nlink != 1
                    or (child.st_dev, child.st_ino) != (current.st_dev, current.st_ino)):
                return
            os.unlink(child_name, dir_fd=fd)
        except OSError:
            return
        finally:
            os.close(child_fd)
    try:
        os.fsync(fd)
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
            return
        os.rmdir(name, dir_fd=parent)
        os.fsync(parent)
    except OSError:
        pass

core_fd = open_absolute(service_root)
try:
    require_dir(core_fd)
    lock_fd = os.open("asset-publish.lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600, dir_fd=core_fd)
    try:
        lock_meta = os.fstat(lock_fd)
        current_lock = os.stat(
            "asset-publish.lock", dir_fd=core_fd, follow_symlinks=False
        )
        if (not stat.S_ISREG(lock_meta.st_mode) or lock_meta.st_uid != uid
                or lock_meta.st_nlink != 1 or stat.S_IMODE(lock_meta.st_mode) != 0o600
                or (lock_meta.st_dev, lock_meta.st_ino)
                    != (current_lock.st_dev, current_lock.st_ino)):
            raise SystemExit(74)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        current_lock = os.stat(
            "asset-publish.lock", dir_fd=core_fd, follow_symlinks=False
        )
        if (lock_meta.st_dev, lock_meta.st_ino) != (
            current_lock.st_dev,
            current_lock.st_ino,
        ):
            raise SystemExit(74)
        staging_fd = open_child_dir(core_fd, "asset-staging")
        try:
            incoming_name = "incoming-" + bundle + "-" + transfer
            retired_name = "retired-" + bundle + "-" + transfer
            try:
                incoming_fd = open_child_dir(staging_fd, incoming_name)
            except FileNotFoundError:
                incoming_fd = None
            if incoming_fd is not None:
                lease_fd = None
                try:
                    lease_fd = os.open(
                        ".openevo-transfer.lock",
                        os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
                        dir_fd=incoming_fd,
                    )
                    lease = os.fstat(lease_fd)
                    current_lease = os.stat(
                        ".openevo-transfer.lock",
                        dir_fd=incoming_fd,
                        follow_symlinks=False,
                    )
                    if (not stat.S_ISREG(lease.st_mode) or lease.st_uid != uid
                            or lease.st_nlink != 1 or stat.S_IMODE(lease.st_mode) != 0o600
                            or (lease.st_dev, lease.st_ino)
                                != (current_lease.st_dev, current_lease.st_ino)):
                        raise SystemExit(75)
                    try:
                        fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        raise SystemExit(75) from None
                    rename_noreplace(staging_fd, incoming_name, retired_name)
                    os.fsync(staging_fd)
                    clear_retired(staging_fd, retired_name, incoming_fd)
                finally:
                    if lease_fd is not None:
                        os.close(lease_fd)
                    os.close(incoming_fd)
        finally:
            os.close(staging_fd)
    finally:
        os.close(lock_fd)
finally:
    os.close(core_fd)
""".strip()


_REMOTE_FINALIZE_SCRIPT = r"""
import ctypes, errno, fcntl, hashlib, json, os, pwd, secrets, stat, sys

service_root, bundle, transfer, wheel_name, wheel_digest, wheel_size, lock_digest, lock_size = sys.argv[1:]
wheel_size = int(wheel_size)
lock_size = int(lock_size)
uid = os.geteuid()
home = pwd.getpwuid(uid).pw_dir
home_parts = home.split("/")[1:]
allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._@%+=,-")
expected_root = home + "/.openevo/core"
if (not hasattr(os, "O_NOFOLLOW") or not home_parts
        or any(not part or part in {".", ".."} or any(c not in allowed for c in part) for part in home_parts)
        or home != "/" + "/".join(home_parts)
        or service_root != expected_root or len(bundle) != 64
        or any(c not in "0123456789abcdef" for c in bundle)
        or len(transfer) != 32 or any(c not in "0123456789abcdef" for c in transfer)):
    raise SystemExit(70)
if not wheel_name.endswith(".whl") or "/" in wheel_name or "\\" in wheel_name:
    raise SystemExit(71)
if any(len(value) != 64 or any(c not in "0123456789abcdef" for c in value) for value in (wheel_digest, lock_digest)):
    raise SystemExit(72)
if not 0 < wheel_size <= 536870912 or not 0 < lock_size <= 65536:
    raise SystemExit(72)

dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
lease_name = ".openevo-transfer.lock"

def open_absolute(path):
    fd = os.open("/", dir_flags)
    try:
        for part in [item for item in path.split("/") if item]:
            child = os.open(part, dir_flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise

def require_dir(fd, mode=0o700):
    meta = os.fstat(fd)
    if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != uid or stat.S_IMODE(meta.st_mode) != mode:
        raise SystemExit(73)

def open_child_dir(parent, name, mode=0o700):
    fd = os.open(name, dir_flags, dir_fd=parent)
    require_dir(fd, mode)
    current = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if (current.st_dev, current.st_ino) != (os.fstat(fd).st_dev, os.fstat(fd).st_ino):
        raise SystemExit(74)
    return fd

def acquire_transfer_lease(fd):
    lease_fd = os.open(lease_name, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=fd)
    try:
        lease = os.fstat(lease_fd)
        current_lease = os.stat(lease_name, dir_fd=fd, follow_symlinks=False)
        if (not stat.S_ISREG(lease.st_mode) or lease.st_uid != uid or lease.st_nlink != 1
                or stat.S_IMODE(lease.st_mode) != 0o600
                or (lease.st_dev, lease.st_ino)
                    != (current_lease.st_dev, current_lease.st_ino)):
            raise SystemExit(74)
        try:
            fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(74) from None
        current_lease = os.stat(lease_name, dir_fd=fd, follow_symlinks=False)
        if (lease.st_dev, lease.st_ino) != (
            current_lease.st_dev,
            current_lease.st_ino,
        ):
            raise SystemExit(74)
        return lease_fd
    except BaseException:
        os.close(lease_fd)
        raise

def verify_file(parent, name, size, digest, mode):
    fd = os.open(name, file_flags, dir_fd=parent)
    try:
        before = os.fstat(fd)
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != uid or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != mode or before.st_size != size
                or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)):
            raise SystemExit(75)
        value = hashlib.sha256()
        remaining = size
        chunks = []
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                raise SystemExit(76)
            value.update(chunk)
            if size <= 65536:
                chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1) or value.hexdigest() != digest:
            raise SystemExit(77)
        after = os.fstat(fd)
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)):
            raise SystemExit(78)
        os.fsync(fd)
        return b"".join(chunks), after
    finally:
        os.close(fd)

def copy_verified_file(source_parent, destination_parent, name, size, digest):
    source_fd = os.open(name, file_flags, dir_fd=source_parent)
    destination_fd = -1
    chunks = []
    try:
        before = os.fstat(source_fd)
        current = os.stat(name, dir_fd=source_parent, follow_symlinks=False)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != uid or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_size != size
                or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)):
            raise SystemExit(75)
        destination_fd = os.open(name, create_flags, 0o400, dir_fd=destination_parent)
        value = hashlib.sha256()
        remaining = size
        while remaining:
            chunk = os.read(source_fd, min(1024 * 1024, remaining))
            if not chunk:
                raise SystemExit(76)
            value.update(chunk)
            if size <= 65536:
                chunks.append(chunk)
            offset = 0
            while offset < len(chunk):
                written = os.write(destination_fd, chunk[offset:])
                if written <= 0:
                    raise SystemExit(76)
                offset += written
            remaining -= len(chunk)
        if os.read(source_fd, 1) or value.hexdigest() != digest:
            raise SystemExit(77)
        os.fchmod(destination_fd, 0o400)
        os.fsync(destination_fd)
        after = os.fstat(source_fd)
        current = os.stat(name, dir_fd=source_parent, follow_symlinks=False)
        destination = os.fstat(destination_fd)
        current_destination = os.stat(name, dir_fd=destination_parent, follow_symlinks=False)
        identity = ("st_dev", "st_ino", "st_mode", "st_uid", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
        if (any(getattr(before, field) != getattr(after, field) for field in identity)
                or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)
                or not stat.S_ISREG(destination.st_mode) or destination.st_uid != uid
                or destination.st_nlink != 1 or stat.S_IMODE(destination.st_mode) != 0o400
                or destination.st_size != size
                or (destination.st_dev, destination.st_ino)
                    != (current_destination.st_dev, current_destination.st_ino)):
            raise SystemExit(78)
        return b"".join(chunks)
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        os.close(source_fd)

def no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate")
        result[key] = value
    return result

def verify_bundle_fd(fd, directory_mode, file_mode, include_lease=False):
    require_dir(fd, directory_mode)
    names = []
    with os.scandir(fd) as entries:
        for entry in entries:
            if len(names) >= (3 if include_lease else 2):
                raise SystemExit(79)
            names.append(entry.name)
    expected_names = {wheel_name, "framework-lock.json"}
    if include_lease:
        expected_names.add(lease_name)
    if set(names) != expected_names:
        raise SystemExit(79)
    unused_wheel_bytes, wheel_meta = verify_file(
        fd, wheel_name, wheel_size, wheel_digest, file_mode
    )
    lock_bytes, lock_meta = verify_file(
        fd, "framework-lock.json", lock_size, lock_digest, file_mode
    )
    lock = json.loads(lock_bytes, object_pairs_hook=no_duplicates)
    if (not isinstance(lock, dict) or set(lock) != {"schema_version", "distribution",
            "distribution_version", "distribution_digest", "wheel_filename"}
            or lock.get("schema_version") != "1" or lock.get("distribution") != "openevo"
            or not isinstance(lock.get("distribution_version"), str)
            or not lock.get("distribution_version")
            or lock.get("distribution_digest") != wheel_digest
            or lock.get("wheel_filename") != wheel_name):
        raise SystemExit(80)
    os.fsync(fd)
    bundle_meta = os.fstat(fd)
    return {
        "bundle_device": bundle_meta.st_dev,
        "bundle_inode": bundle_meta.st_ino,
        "wheel_device": wheel_meta.st_dev,
        "wheel_inode": wheel_meta.st_ino,
        "framework_lock_device": lock_meta.st_dev,
        "framework_lock_inode": lock_meta.st_ino,
    }

def verify_bundle(parent, name, directory_mode, file_mode):
    fd = open_child_dir(parent, name, directory_mode)
    try:
        return fd, verify_bundle_fd(fd, directory_mode, file_mode)
    except BaseException:
        os.close(fd)
        raise

def seal_bundle_members(fd):
    for name in (wheel_name, "framework-lock.json"):
        child_fd = os.open(name, file_flags, dir_fd=fd)
        try:
            os.fchmod(child_fd, 0o400)
            os.fsync(child_fd)
        finally:
            os.close(child_fd)
    os.fsync(fd)

def discard_private_bundle(parent, name, fd):
    before = os.fstat(fd)
    if (not stat.S_ISDIR(before.st_mode) or before.st_uid != uid
            or stat.S_IMODE(before.st_mode) not in {0o500, 0o700}):
        raise SystemExit(83)
    os.fchmod(fd, 0o700)
    names = []
    with os.scandir(fd) as entries:
        for entry in entries:
            if len(names) >= 3:
                raise SystemExit(83)
            names.append(entry.name)
    if set(names) not in (
        {wheel_name, "framework-lock.json"},
        {wheel_name, "framework-lock.json", ".openevo-transfer.lock"},
    ):
        raise SystemExit(83)
    for child_name in names:
        child_fd = os.open(child_name, file_flags, dir_fd=fd)
        try:
            child = os.fstat(child_fd)
            current_child = os.stat(child_name, dir_fd=fd, follow_symlinks=False)
            if (not stat.S_ISREG(child.st_mode) or child.st_uid != uid or child.st_nlink != 1
                    or (child.st_dev, child.st_ino) != (current_child.st_dev, current_child.st_ino)):
                raise SystemExit(83)
            os.unlink(child_name, dir_fd=fd)
        finally:
            os.close(child_fd)
    os.fsync(fd)
    current = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
        raise SystemExit(83)
    os.rmdir(name, dir_fd=parent)
    os.fsync(parent)

def rename_noreplace(source_parent, source_name, destination_parent, destination_name):
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise SystemExit(82)
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_parent,
        os.fsencode(source_name),
        destination_parent,
        os.fsencode(destination_name),
        1,
    )
    if result == 0:
        return True
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        return False
    raise OSError(error, "atomic Core asset publication failed")

def create_private_candidate(parent):
    for unused in range(4):
        name = "publish-" + bundle + "-" + secrets.token_hex(16)
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
            os.fsync(parent)
            return name, open_child_dir(parent, name)
        except FileExistsError:
            continue
    raise SystemExit(82)

def retire_incoming(parent, incoming_name, incoming_fd, lease_fd):
    current = os.stat(incoming_name, dir_fd=parent, follow_symlinks=False)
    opened = os.fstat(incoming_fd)
    if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
        raise SystemExit(83)
    retired_name = "retired-" + bundle + "-" + transfer
    if not rename_noreplace(parent, incoming_name, parent, retired_name):
        raise SystemExit(83)
    os.fsync(parent)
    lease = os.fstat(lease_fd)
    current_lease = os.stat(lease_name, dir_fd=incoming_fd, follow_symlinks=False)
    if (lease.st_dev, lease.st_ino) != (current_lease.st_dev, current_lease.st_ino):
        raise SystemExit(83)
    os.unlink(lease_name, dir_fd=incoming_fd)
    os.fsync(incoming_fd)
    try:
        discard_private_bundle(parent, retired_name, incoming_fd)
    except (FileNotFoundError, OSError, SystemExit):
        pass

core_fd = open_absolute(service_root)
try:
    require_dir(core_fd)
    lock_fd = os.open("asset-publish.lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600, dir_fd=core_fd)
    try:
        lock_meta = os.fstat(lock_fd)
        current_lock = os.stat(
            "asset-publish.lock", dir_fd=core_fd, follow_symlinks=False
        )
        if (not stat.S_ISREG(lock_meta.st_mode) or lock_meta.st_uid != uid or lock_meta.st_nlink != 1
                or stat.S_IMODE(lock_meta.st_mode) != 0o600
                or (lock_meta.st_dev, lock_meta.st_ino)
                    != (current_lock.st_dev, current_lock.st_ino)):
            raise SystemExit(81)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        current_lock = os.stat(
            "asset-publish.lock", dir_fd=core_fd, follow_symlinks=False
        )
        if (lock_meta.st_dev, lock_meta.st_ino) != (
            current_lock.st_dev,
            current_lock.st_ino,
        ):
            raise SystemExit(81)
        staging_fd = open_child_dir(core_fd, "asset-staging")
        assets_fd = open_child_dir(core_fd, "assets")
        try:
            incoming_name = "incoming-" + bundle + "-" + transfer
            incoming_fd = None
            candidate_fd = None
            candidate_name = None
            candidate_parent = staging_fd
            transfer_lease_fd = None
            owns_incoming = False
            try:
                try:
                    incoming_fd = open_child_dir(staging_fd, incoming_name)
                except FileNotFoundError:
                    incoming_fd = None
                if incoming_fd is not None:
                    transfer_lease_fd = acquire_transfer_lease(incoming_fd)
                    owns_incoming = True
                try:
                    final_fd, receipt = verify_bundle(assets_fd, bundle, 0o500, 0o400)
                except FileNotFoundError:
                    if incoming_fd is None:
                        raise
                    unused_receipt = verify_bundle_fd(
                        incoming_fd, 0o700, 0o600, include_lease=True
                    )
                    candidate_name, candidate_fd = create_private_candidate(staging_fd)
                    copy_verified_file(incoming_fd, candidate_fd, wheel_name, wheel_size, wheel_digest)
                    lock_bytes = copy_verified_file(
                        incoming_fd,
                        candidate_fd,
                        "framework-lock.json",
                        lock_size,
                        lock_digest,
                    )
                    lock = json.loads(lock_bytes, object_pairs_hook=no_duplicates)
                    if (not isinstance(lock, dict) or set(lock) != {"schema_version", "distribution",
                            "distribution_version", "distribution_digest", "wheel_filename"}
                            or lock.get("schema_version") != "1" or lock.get("distribution") != "openevo"
                            or not isinstance(lock.get("distribution_version"), str)
                            or not lock.get("distribution_version")
                            or lock.get("distribution_digest") != wheel_digest
                            or lock.get("wheel_filename") != wheel_name):
                        raise SystemExit(80)
                    seal_bundle_members(candidate_fd)
                    verify_bundle_fd(candidate_fd, 0o700, 0o400)
                    moved = rename_noreplace(
                        staging_fd, candidate_name, assets_fd, candidate_name
                    )
                    if not moved:
                        raise SystemExit(82)
                    candidate_parent = assets_fd
                    os.fsync(assets_fd)
                    os.fsync(staging_fd)
                    current = os.stat(
                        candidate_name, dir_fd=assets_fd, follow_symlinks=False
                    )
                    opened = os.fstat(candidate_fd)
                    if (current.st_dev, current.st_ino) != (
                        opened.st_dev,
                        opened.st_ino,
                    ):
                        raise SystemExit(82)
                    os.fchmod(candidate_fd, 0o500)
                    os.fsync(candidate_fd)
                    candidate_receipt = verify_bundle_fd(candidate_fd, 0o500, 0o400)
                    current = os.stat(
                        candidate_name, dir_fd=assets_fd, follow_symlinks=False
                    )
                    if (current.st_dev, current.st_ino) != (
                        candidate_receipt["bundle_device"],
                        candidate_receipt["bundle_inode"],
                    ):
                        raise SystemExit(82)
                    published = rename_noreplace(
                        assets_fd, candidate_name, assets_fd, bundle
                    )
                    if published:
                        os.fsync(assets_fd)
                        current = os.stat(bundle, dir_fd=assets_fd, follow_symlinks=False)
                        if (current.st_dev, current.st_ino) != (
                            candidate_receipt["bundle_device"],
                            candidate_receipt["bundle_inode"],
                        ):
                            raise SystemExit(82)
                        receipt = verify_bundle_fd(candidate_fd, 0o500, 0o400)
                        if receipt != candidate_receipt:
                            raise SystemExit(82)
                        current = os.stat(bundle, dir_fd=assets_fd, follow_symlinks=False)
                        if (current.st_dev, current.st_ino) != (
                            receipt["bundle_device"],
                            receipt["bundle_inode"],
                        ):
                            raise SystemExit(82)
                        candidate_name = None
                        final_fd = candidate_fd
                        candidate_fd = None
                    else:
                        final_fd, receipt = verify_bundle(
                            assets_fd, bundle, 0o500, 0o400
                        )
                os.close(final_fd)
            finally:
                if candidate_fd is not None:
                    os.close(candidate_fd)
                if candidate_name is not None:
                    try:
                        private_fd = os.open(
                            candidate_name, dir_flags, dir_fd=candidate_parent
                        )
                        try:
                            discard_private_bundle(
                                candidate_parent, candidate_name, private_fd
                            )
                        finally:
                            os.close(private_fd)
                    except (FileNotFoundError, OSError, SystemExit):
                        pass
                try:
                    if owns_incoming and incoming_fd is None:
                        try:
                            incoming_fd = open_child_dir(staging_fd, incoming_name)
                        except FileNotFoundError:
                            pass
                    if owns_incoming and incoming_fd is not None:
                        retire_incoming(
                            staging_fd,
                            incoming_name,
                            incoming_fd,
                            transfer_lease_fd,
                        )
                finally:
                    if incoming_fd is not None:
                        os.close(incoming_fd)
                    if transfer_lease_fd is not None:
                        os.close(transfer_lease_fd)
        finally:
            os.close(assets_fd)
            os.close(staging_fd)
    finally:
        os.close(lock_fd)
finally:
    os.close(core_fd)

final_root = service_root + "/assets/" + bundle
print(json.dumps({
    "schema_version": 1,
    "service_root": service_root,
    "wheel_path": final_root + "/" + wheel_name,
    "framework_lock_path": final_root + "/framework-lock.json",
    "wheel_sha256": wheel_digest,
    "framework_lock_sha256": lock_digest,
    **receipt,
}, sort_keys=True, separators=(",", ":")))
""".strip()


_REMOTE_CONSUME_SCRIPT = r"""
import fcntl, hashlib, os, pwd, stat, subprocess, sys

(service_root, bundle, wheel_name, wheel_digest, wheel_size, lock_digest, lock_size,
 bundle_device, bundle_inode, wheel_device, wheel_inode, lock_device, lock_inode,
 command) = sys.argv[1:]
wheel_size = int(wheel_size)
lock_size = int(lock_size)
bundle_device = int(bundle_device)
bundle_inode = int(bundle_inode)
wheel_device = int(wheel_device)
wheel_inode = int(wheel_inode)
lock_device = int(lock_device)
lock_inode = int(lock_inode)
uid = os.geteuid()
home = pwd.getpwuid(uid).pw_dir
home_parts = home.split("/")[1:]
allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._@%+=,-")
if (not hasattr(os, "O_NOFOLLOW") or not home_parts
        or any(not part or part in {".", ".."} or any(c not in allowed for c in part) for part in home_parts)
        or home != "/" + "/".join(home_parts)
        or service_root != home + "/.openevo/core"
        or len(bundle) != 64 or any(c not in "0123456789abcdef" for c in bundle)
        or not wheel_name.endswith(".whl") or "/" in wheel_name or "\\" in wheel_name
        or any(len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
               for value in (wheel_digest, lock_digest))
        or not 0 < wheel_size <= 536870912 or not 0 < lock_size <= 65536
        or min(bundle_device, wheel_device, lock_device) < 0
        or min(bundle_inode, wheel_inode, lock_inode) <= 0
        or not command or "\x00" in command):
    raise SystemExit(70)

dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW

def open_absolute(path):
    fd = os.open("/", dir_flags)
    try:
        for part in [item for item in path.split("/") if item]:
            child = os.open(part, dir_flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise

def require_dir(fd, mode, identity=None):
    meta = os.fstat(fd)
    if (not stat.S_ISDIR(meta.st_mode) or meta.st_uid != uid
            or stat.S_IMODE(meta.st_mode) != mode
            or identity is not None and (meta.st_dev, meta.st_ino) != identity):
        raise SystemExit(71)
    return meta

def open_child_dir(parent, name, mode, identity=None):
    fd = os.open(name, dir_flags, dir_fd=parent)
    try:
        opened = require_dir(fd, mode, identity)
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise SystemExit(72)
        return fd
    except BaseException:
        os.close(fd)
        raise

def open_verified_file(parent, name, size, digest, identity):
    fd = os.open(name, file_flags, dir_fd=parent)
    try:
        verify_open_file(parent, name, fd, size, digest, identity)
        return fd
    except BaseException:
        os.close(fd)
        raise

def verify_open_file(parent, name, fd, size, digest, identity):
    os.lseek(fd, 0, os.SEEK_SET)
    before = os.fstat(fd)
    current = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != uid or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o400 or before.st_size != size
            or (before.st_dev, before.st_ino) != identity
            or identity != (current.st_dev, current.st_ino)):
        raise SystemExit(73)
    value = hashlib.sha256()
    remaining = size
    while remaining:
        chunk = os.read(fd, min(1024 * 1024, remaining))
        if not chunk:
            raise SystemExit(74)
        value.update(chunk)
        remaining -= len(chunk)
    if os.read(fd, 1) or value.hexdigest() != digest:
        raise SystemExit(74)
    after = os.fstat(fd)
    current = os.stat(name, dir_fd=parent, follow_symlinks=False)
    identity_fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
    if (any(getattr(before, field) != getattr(after, field) for field in identity_fields)
            or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)):
        raise SystemExit(75)

core_fd = open_absolute(service_root)
try:
    require_dir(core_fd, 0o700)
    lock_fd = os.open("asset-publish.lock", os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=core_fd)
    try:
        lock_meta = os.fstat(lock_fd)
        current_lock = os.stat(
            "asset-publish.lock", dir_fd=core_fd, follow_symlinks=False
        )
        if (not stat.S_ISREG(lock_meta.st_mode) or lock_meta.st_uid != uid
                or lock_meta.st_nlink != 1 or stat.S_IMODE(lock_meta.st_mode) != 0o600
                or (lock_meta.st_dev, lock_meta.st_ino)
                    != (current_lock.st_dev, current_lock.st_ino)):
            raise SystemExit(76)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        current_lock = os.stat(
            "asset-publish.lock", dir_fd=core_fd, follow_symlinks=False
        )
        if (lock_meta.st_dev, lock_meta.st_ino) != (
            current_lock.st_dev,
            current_lock.st_ino,
        ):
            raise SystemExit(76)
        assets_fd = open_child_dir(core_fd, "assets", 0o700)
        try:
            bundle_fd = open_child_dir(
                assets_fd, bundle, 0o500, (bundle_device, bundle_inode)
            )
            try:
                names = []
                with os.scandir(bundle_fd) as entries:
                    for entry in entries:
                        if len(names) >= 2:
                            raise SystemExit(77)
                        names.append(entry.name)
                if set(names) != {wheel_name, "framework-lock.json"}:
                    raise SystemExit(77)
                wheel_fd = open_verified_file(
                    bundle_fd, wheel_name, wheel_size, wheel_digest,
                    (wheel_device, wheel_inode),
                )
                try:
                    lock_data_fd = open_verified_file(
                        bundle_fd, "framework-lock.json", lock_size, lock_digest,
                        (lock_device, lock_inode),
                    )
                    try:
                        final_root = service_root + "/assets/" + bundle
                        wheel_path = final_root + "/" + wheel_name
                        framework_lock_path = final_root + "/framework-lock.json"
                        if (wheel_path not in command
                                or framework_lock_path not in command):
                            raise SystemExit(78)
                        pinned_root = (
                            "/proc/" + str(os.getpid()) + "/fd/" + str(bundle_fd)
                        )
                        rewritten = command.replace(
                            wheel_path,
                            pinned_root + "/" + wheel_name,
                        )
                        completed = subprocess.run(
                            rewritten,
                            shell=True,
                            executable="/bin/sh",
                            close_fds=True,
                            pass_fds=(bundle_fd,),
                            check=False,
                        )
                        verify_open_file(
                            bundle_fd, wheel_name, wheel_fd, wheel_size, wheel_digest,
                            (wheel_device, wheel_inode),
                        )
                        verify_open_file(
                            bundle_fd, "framework-lock.json", lock_data_fd, lock_size,
                            lock_digest, (lock_device, lock_inode),
                        )
                        current = os.stat(bundle, dir_fd=assets_fd, follow_symlinks=False)
                        if (current.st_dev, current.st_ino) != (bundle_device, bundle_inode):
                            raise SystemExit(79)
                    finally:
                        os.close(lock_data_fd)
                finally:
                    os.close(wheel_fd)
            finally:
                os.close(bundle_fd)
        finally:
            os.close(assets_fd)
    finally:
        os.close(lock_fd)
finally:
    os.close(core_fd)

raise SystemExit(completed.returncode)
""".strip()


__all__ = (
    "CoreBootstrapAssetSnapshotError",
    "CoreBootstrapAssetSnapshot",
    "MAX_CORE_WHEEL_BYTES",
    "MAX_FRAMEWORK_LOCK_BYTES",
    "CORE_ASSET_TRANSFER_LEASE",
    "StagedCoreBootstrapAssets",
    "build_core_asset_discard_command",
    "build_core_asset_consumer_command",
    "build_core_asset_finalize_command",
    "build_core_asset_prepare_command",
    "build_core_asset_rsync_path",
    "parse_core_asset_prepare",
    "parse_staged_core_assets",
    "snapshot_core_bootstrap_assets",
)
