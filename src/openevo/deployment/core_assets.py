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


MAX_CORE_WHEEL_BYTES = 512 * 1024 * 1024
MAX_FRAMEWORK_LOCK_BYTES = 64 * 1024
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_BUNDLE_ID = _DIGEST
_WHEEL_NAME = re.compile(r"[A-Za-z0-9_.+-]+\.whl\Z")
_REMOTE_PATH = re.compile(r"/(?:[A-Za-z0-9._@%+=,-]+/)*[A-Za-z0-9._@%+=,-]+\Z")
_MAX_STAGE_RESPONSE_BYTES = 4096


class CoreBootstrapAssetSnapshotError(ValueError):
    """A renderer-safe local sealed-asset validation failure."""


@dataclass(frozen=True, slots=True, repr=False)
class StagedCoreBootstrapAssets:
    service_root: str
    wheel_path: str
    framework_lock_path: str
    wheel_sha256: str
    framework_lock_sha256: str

    def __post_init__(self) -> None:
        for value in (self.service_root, self.wheel_path, self.framework_lock_path):
            _require_remote_path(value)
        if (
            not isinstance(self.wheel_sha256, str)
            or not isinstance(self.framework_lock_sha256, str)
            or _DIGEST.fullmatch(self.wheel_sha256) is None
            or _DIGEST.fullmatch(self.framework_lock_sha256) is None
            or Path(self.framework_lock_path).name != "framework-lock.json"
            or Path(self.wheel_path).parent != Path(self.framework_lock_path).parent
            or not Path(self.wheel_path).name.endswith(".whl")
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


def build_core_asset_prepare_command(bundle_id: str) -> str:
    _require_bundle_id(bundle_id)
    return f"python3 -I -c {shlex.quote(_REMOTE_PREPARE_SCRIPT)} {shlex.quote(bundle_id)}"


def parse_core_asset_prepare(
    payload: SecretStr,
    *,
    bundle_id: str,
) -> tuple[str, str]:
    _require_bundle_id(bundle_id)
    value = _load_secret_json(payload)
    if not isinstance(value, dict) or set(value) != {
        "incoming_root",
        "schema_version",
        "service_root",
    }:
        raise ValueError("Core asset prepare response is invalid")
    service_root = value.get("service_root")
    incoming_root = value.get("incoming_root")
    if (
        type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or not isinstance(service_root, str)
        or not isinstance(incoming_root, str)
    ):
        raise ValueError("Core asset prepare response is invalid")
    _require_remote_path(service_root)
    _require_remote_path(incoming_root)
    if incoming_root != f"{service_root}/asset-staging/incoming-{bundle_id}":
        raise ValueError("Core asset prepare response identity is invalid")
    return service_root, incoming_root


def build_core_asset_finalize_command(
    *,
    service_root: str,
    bundle_id: str,
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
        wheel_filename,
        wheel_sha256,
        str(wheel_size),
        framework_lock_sha256,
        str(framework_lock_size),
    )
    return " ".join(
        ["python3", "-I", "-c", shlex.quote(_REMOTE_FINALIZE_SCRIPT)]
        + [shlex.quote(value) for value in arguments]
    )


def parse_staged_core_assets(
    payload: SecretStr,
    *,
    service_root: str,
    bundle_id: str,
    wheel_filename: str,
    wheel_sha256: str,
    framework_lock_sha256: str,
) -> StagedCoreBootstrapAssets:
    value = _load_secret_json(payload)
    expected_fields = {
        "framework_lock_path",
        "framework_lock_sha256",
        "schema_version",
        "service_root",
        "wheel_path",
        "wheel_sha256",
    }
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise ValueError("Core asset finalize response is invalid")
    if type(value.get("schema_version")) is not int:
        raise ValueError("Core asset finalize response is invalid")
    expected_root = f"{service_root}/assets/{bundle_id}"
    expected_wheel = f"{expected_root}/{wheel_filename}"
    expected_lock = f"{expected_root}/framework-lock.json"
    if value != {
        "schema_version": 1,
        "service_root": service_root,
        "wheel_path": expected_wheel,
        "framework_lock_path": expected_lock,
        "wheel_sha256": wheel_sha256,
        "framework_lock_sha256": framework_lock_sha256,
    }:
        raise ValueError("Core asset finalize response identity is invalid")
    return StagedCoreBootstrapAssets(
        service_root=service_root,
        wheel_path=expected_wheel,
        framework_lock_path=expected_lock,
        wheel_sha256=wheel_sha256,
        framework_lock_sha256=framework_lock_sha256,
    )


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


_REMOTE_PREPARE_SCRIPT = r"""
import fcntl, json, os, pwd, stat, sys

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
max_incoming_entries = 16

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

def bounded_names(fd):
    names = []
    with os.scandir(fd) as entries:
        for entry in entries:
            if len(names) >= max_incoming_entries:
                raise SystemExit(75)
            names.append(entry.name)
    return names

def clear_incoming(fd):
    names = bounded_names(fd)
    for name in names:
        child_fd = os.open(name, file_flags, dir_fd=fd)
        try:
            opened = os.fstat(child_fd)
            current = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != uid
                    or opened.st_nlink != 1
                    or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
                raise SystemExit(76)
            os.unlink(name, dir_fd=fd)
        finally:
            os.close(child_fd)
    os.fsync(fd)

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
                if (not stat.S_ISREG(lock_meta.st_mode) or lock_meta.st_uid != uid
                        or lock_meta.st_nlink != 1 or stat.S_IMODE(lock_meta.st_mode) != 0o600):
                    raise SystemExit(77)
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                staging_fd = ensure(core_fd, "asset-staging")
                assets_fd = ensure(core_fd, "assets")
                try:
                    incoming = "incoming-" + bundle
                    incoming_fd = ensure(staging_fd, incoming)
                    try:
                        clear_incoming(incoming_fd)
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
    "incoming_root": root + "/asset-staging/incoming-" + bundle,
}, sort_keys=True, separators=(",", ":")))
""".strip()


_REMOTE_FINALIZE_SCRIPT = r"""
import ctypes, errno, fcntl, hashlib, json, os, pwd, stat, sys

service_root, bundle, wheel_name, wheel_digest, wheel_size, lock_digest, lock_size = sys.argv[1:]
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
        or any(c not in "0123456789abcdef" for c in bundle)):
    raise SystemExit(70)
if not wheel_name.endswith(".whl") or "/" in wheel_name or "\\" in wheel_name:
    raise SystemExit(71)
if any(len(value) != 64 or any(c not in "0123456789abcdef" for c in value) for value in (wheel_digest, lock_digest)):
    raise SystemExit(72)
if not 0 < wheel_size <= 536870912 or not 0 < lock_size <= 65536:
    raise SystemExit(72)

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
        raise SystemExit(73)

def open_child_dir(parent, name):
    fd = os.open(name, dir_flags, dir_fd=parent)
    require_dir(fd)
    current = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if (current.st_dev, current.st_ino) != (os.fstat(fd).st_dev, os.fstat(fd).st_ino):
        raise SystemExit(74)
    return fd

def verify_file(parent, name, size, digest):
    fd = os.open(name, file_flags, dir_fd=parent)
    try:
        before = os.fstat(fd)
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != uid or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_size != size
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
        return b"".join(chunks)
    finally:
        os.close(fd)

def no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate")
        result[key] = value
    return result

def verify_bundle(parent, name):
    fd = open_child_dir(parent, name)
    try:
        names = []
        with os.scandir(fd) as entries:
            for entry in entries:
                if len(names) >= 2:
                    raise SystemExit(79)
                names.append(entry.name)
        if set(names) != {wheel_name, "framework-lock.json"}:
            raise SystemExit(79)
        verify_file(fd, wheel_name, wheel_size, wheel_digest)
        lock_bytes = verify_file(fd, "framework-lock.json", lock_size, lock_digest)
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
        return fd
    except BaseException:
        os.close(fd)
        raise

def discard_incoming(parent, name, fd):
    before = os.fstat(fd)
    names = []
    with os.scandir(fd) as entries:
        for entry in entries:
            if len(names) >= 2:
                raise SystemExit(83)
            names.append(entry.name)
    if set(names) != {wheel_name, "framework-lock.json"}:
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

core_fd = open_absolute(service_root)
try:
    require_dir(core_fd)
    lock_fd = os.open("asset-publish.lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600, dir_fd=core_fd)
    try:
        lock_meta = os.fstat(lock_fd)
        if (not stat.S_ISREG(lock_meta.st_mode) or lock_meta.st_uid != uid or lock_meta.st_nlink != 1
                or stat.S_IMODE(lock_meta.st_mode) != 0o600):
            raise SystemExit(81)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        staging_fd = open_child_dir(core_fd, "asset-staging")
        assets_fd = open_child_dir(core_fd, "assets")
        try:
            incoming_name = "incoming-" + bundle
            incoming_fd = verify_bundle(staging_fd, incoming_name)
            try:
                incoming_meta = os.fstat(incoming_fd)
                published = False
                try:
                    final_fd = verify_bundle(assets_fd, bundle)
                except FileNotFoundError:
                    published = rename_noreplace(staging_fd, incoming_name, assets_fd, bundle)
                    if published:
                        os.fsync(assets_fd)
                        os.fsync(staging_fd)
                    final_fd = verify_bundle(assets_fd, bundle)
                try:
                    final_meta = os.fstat(final_fd)
                    if published and (incoming_meta.st_dev, incoming_meta.st_ino) != (final_meta.st_dev, final_meta.st_ino):
                        raise SystemExit(82)
                finally:
                    os.close(final_fd)
                if not published:
                    discard_incoming(staging_fd, incoming_name, incoming_fd)
            finally:
                os.close(incoming_fd)
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
}, sort_keys=True, separators=(",", ":")))
""".strip()


__all__ = (
    "CoreBootstrapAssetSnapshotError",
    "CoreBootstrapAssetSnapshot",
    "MAX_CORE_WHEEL_BYTES",
    "MAX_FRAMEWORK_LOCK_BYTES",
    "StagedCoreBootstrapAssets",
    "build_core_asset_finalize_command",
    "build_core_asset_prepare_command",
    "parse_core_asset_prepare",
    "parse_staged_core_assets",
    "snapshot_core_bootstrap_assets",
)
