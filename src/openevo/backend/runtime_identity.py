from __future__ import annotations

import ctypes
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import stat
from typing import Any, Mapping

from openevo.codex_models import codex_cli_model_name, validate_codex_model_ref
from openevo.runtime.codex_isolation import (
    CODEX_SUBSCRIPTION_POLICY_ID,
    CODEX_SUBSCRIPTION_POLICY_SHA256,
)
from openevo.runtime.managed import (
    MANAGED_CODEX_VERSION,
    require_immutable_managed_runtime_image,
)


_BEARER_PATTERN = re.compile(r"[A-Za-z0-9_-]{64}\Z")
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_MAX_BEARER_BYTES = 65
_MAX_JSON_BYTES = 64 * 1024
_RENAME_NOREPLACE = 1


class RuntimeIdentityError(RuntimeError):
    """A user-safe failure at the host-global Core service boundary."""


@dataclass(frozen=True, slots=True)
class CoreReleaseIdentity:
    digest: str
    registry_digest: str
    framework_lock_sha256: str
    source_commit: str

    def __post_init__(self) -> None:
        if _DIGEST_PATTERN.fullmatch(self.digest) is None:
            raise ValueError("release identity digest is invalid")
        if _DIGEST_PATTERN.fullmatch(self.registry_digest) is None:
            raise ValueError("registry digest is invalid")
        if _DIGEST_PATTERN.fullmatch(self.framework_lock_sha256) is None:
            raise ValueError("framework lock digest is invalid")
        if re.fullmatch(r"[0-9a-f]{40}", self.source_commit) is None:
            raise ValueError("source commit is invalid")


@dataclass(frozen=True, slots=True)
class ManagedSubscriptionRuntimeIdentity:
    """Closed non-secret projection of one verified Subscription run binding."""

    harness_id: str
    harness_version: str
    codex_model: str
    image_digest: str
    runtime_identity_digest: str
    runtime_policy_id: str
    runtime_policy_digest: str

    def __post_init__(self) -> None:
        if self.harness_id != "codex":
            raise ValueError("managed subscription harness identity is invalid")
        if self.harness_version != MANAGED_CODEX_VERSION:
            raise ValueError("managed subscription harness version is invalid")
        validated_model = validate_codex_model_ref(
            self.codex_model,
            field_name="managed subscription Codex model",
        )
        if codex_cli_model_name(validated_model) != self.codex_model:
            raise ValueError("managed subscription Codex model is not canonical")
        for value, label in (
            (self.image_digest, "managed subscription image digest"),
            (self.runtime_identity_digest, "managed subscription runtime digest"),
            (self.runtime_policy_digest, "managed subscription policy digest"),
        ):
            if _DIGEST_PATTERN.fullmatch(value) is None:
                raise ValueError(f"{label} is invalid")
        if self.runtime_policy_id != CODEX_SUBSCRIPTION_POLICY_ID:
            raise ValueError("managed subscription runtime policy ID is invalid")
        if self.runtime_policy_digest != CODEX_SUBSCRIPTION_POLICY_SHA256:
            raise ValueError("managed subscription runtime policy digest is invalid")


def require_managed_subscription_runtime_identity(
    service_binding: object,
) -> ManagedSubscriptionRuntimeIdentity:
    """Project trusted service evidence into the closed effective-runtime identity."""

    # Imported lazily so the host identity primitives remain independent of the
    # supervisor's process-lifecycle module at import time.
    from openevo.backend.service_supervisor import (
        ServiceExecutionMode,
        ServiceRunBinding,
    )

    if type(service_binding) is not ServiceRunBinding:
        raise RuntimeIdentityError("Managed Subscription runtime identity is unavailable")
    try:
        service_binding.__post_init__()
        if (
            service_binding.execution_mode
            is not ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT
        ):
            raise ValueError("run binding is not the Subscription profile")
        release = require_immutable_managed_runtime_image(
            profile="managed_science",
            image=service_binding.runtime_image_immutable_reference,
        )
        if release.image != service_binding.runtime_image:
            raise ValueError("managed image alias differs from its immutable release")
        model = validate_codex_model_ref(
            service_binding.codex_model,
            field_name="managed Subscription Codex model",
        )
        model = codex_cli_model_name(model)
        identity = ManagedSubscriptionRuntimeIdentity(
            harness_id="codex",
            harness_version=MANAGED_CODEX_VERSION,
            codex_model=model,
            image_digest=release.trusted_digest.removeprefix("sha256:"),
            runtime_identity_digest=service_binding.runtime_identity_digest,
            runtime_policy_id=CODEX_SUBSCRIPTION_POLICY_ID,
            runtime_policy_digest=CODEX_SUBSCRIPTION_POLICY_SHA256,
        )
        identity.__post_init__()
        return identity
    except (TypeError, ValueError) as exc:
        raise RuntimeIdentityError(
            "Managed Subscription runtime identity is unavailable"
        ) from exc


class HostServiceRoot:
    """Pinned, owner-private root for one OS user's Core Control service."""

    def __init__(self, path: str | Path, *, create: bool = True) -> None:
        candidate = Path(path)
        if not candidate.is_absolute() or candidate == Path("/"):
            raise RuntimeIdentityError("Core service root must be an absolute private path")
        self.path = candidate
        self._fd = _open_absolute_directory(candidate, create=create)
        try:
            _require_directory(self._fd, mode=0o700, label="Core service root")
        except BaseException:
            os.close(self._fd)
            self._fd = -1
            raise

    @property
    def fd(self) -> int:
        if self._fd < 0:
            raise RuntimeIdentityError("Core service root is closed")
        return self._fd

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self) -> HostServiceRoot:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def ensure_directory(self, name: str) -> Path:
        _validate_leaf(name)
        try:
            os.mkdir(name, mode=0o700, dir_fd=self.fd)
            os.fsync(self.fd)
        except FileExistsError:
            pass
        child_fd = _open_at(self.fd, name, os.O_RDONLY | _directory_flag())
        try:
            _require_directory(child_fd, mode=0o700, label="Core service child")
        finally:
            os.close(child_fd)
        return self.path / name

    def open_lock(self, name: str) -> int:
        _validate_leaf(name)
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | _nofollow_flag()
        fd = os.open(name, flags, 0o600, dir_fd=self.fd)
        try:
            _require_regular(fd, mode=0o600, max_bytes=0, allow_any_size=True)
            _require_at_binding(self.fd, name, fd)
            os.fsync(self.fd)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def read_bytes(self, name: str, *, max_bytes: int) -> bytes:
        _validate_leaf(name)
        fd = _open_at(self.fd, name, os.O_RDONLY)
        try:
            initial = os.fstat(fd)
            _require_regular_metadata(initial, mode=0o600, max_bytes=max_bytes)
            _require_at_binding(self.fd, name, fd)
            payload = _read_exact(fd, initial.st_size)
            final = os.fstat(fd)
            if _metadata_identity(initial) != _metadata_identity(final):
                raise RuntimeIdentityError("Core service state changed during read")
            _require_at_binding(self.fd, name, fd)
            return payload
        finally:
            os.close(fd)

    def read_optional_bytes(self, name: str, *, max_bytes: int) -> bytes | None:
        try:
            return self.read_bytes(name, max_bytes=max_bytes)
        except FileNotFoundError:
            return None

    def read_json(self, name: str, *, max_bytes: int = _MAX_JSON_BYTES) -> Any:
        payload = self.read_bytes(name, max_bytes=max_bytes)
        return load_bounded_json(payload, max_bytes=max_bytes)

    def read_optional_json(self, name: str, *, max_bytes: int = _MAX_JSON_BYTES) -> Any:
        payload = self.read_optional_bytes(name, max_bytes=max_bytes)
        if payload is None:
            return None
        return load_bounded_json(payload, max_bytes=max_bytes)

    def atomic_write(self, name: str, payload: bytes, *, replace: bool) -> None:
        _validate_leaf(name)
        if not payload or len(payload) > _MAX_JSON_BYTES:
            raise RuntimeIdentityError("Core service state payload size is invalid")
        temporary = f".{name}.tmp-{secrets.token_hex(16)}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | _nofollow_flag()
        fd = os.open(temporary, flags, 0o600, dir_fd=self.fd)
        published = False
        try:
            _require_regular(fd, mode=0o600, max_bytes=0)
            _write_all(fd, payload)
            os.fsync(fd)
            _require_regular(fd, mode=0o600, max_bytes=len(payload))
            if os.fstat(fd).st_size != len(payload):
                raise RuntimeIdentityError("Core service state write was incomplete")
            _require_at_binding(self.fd, temporary, fd)
            if replace:
                existing = self.read_optional_bytes(name, max_bytes=_MAX_JSON_BYTES)
                del existing
                os.rename(
                    temporary,
                    name,
                    src_dir_fd=self.fd,
                    dst_dir_fd=self.fd,
                )
            else:
                _rename_noreplace(self.fd, temporary, name)
            published = True
            os.fsync(self.fd)
            _require_at_binding(self.fd, name, fd)
        finally:
            os.close(fd)
            if not published:
                _unlink_at_if_regular(self.fd, temporary)

    def atomic_write_json(self, name: str, value: Mapping[str, Any], *, replace: bool) -> None:
        payload = canonical_json_bytes(value) + b"\n"
        self.atomic_write(name, payload, replace=replace)

    def unlink_regular(self, name: str) -> None:
        _validate_leaf(name)
        try:
            fd = _open_at(self.fd, name, os.O_RDONLY)
        except FileNotFoundError:
            return
        try:
            _require_regular(fd, mode=0o600, max_bytes=_MAX_JSON_BYTES)
            _require_at_binding(self.fd, name, fd)
            os.unlink(name, dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            os.close(fd)


def default_core_service_root() -> Path:
    try:
        home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
    except KeyError as exc:
        raise RuntimeIdentityError("OS user home is unavailable") from exc
    if not home.is_absolute():
        raise RuntimeIdentityError("OS user home is unavailable")
    return home / ".openevo" / "core"


def require_host_global_service_root(path: str | Path) -> Path:
    candidate = Path(path)
    expected = default_core_service_root()
    if candidate != expected:
        raise RuntimeIdentityError(
            "Core service root must be the OS user's canonical host-global root"
        )
    return candidate


def load_or_create_core_bearer_token(root: HostServiceRoot) -> str:
    try:
        return _decode_bearer(root.read_bytes("bearer-token", max_bytes=_MAX_BEARER_BYTES))
    except FileNotFoundError:
        pass
    token = secrets.token_urlsafe(48)
    try:
        root.atomic_write("bearer-token", (token + "\n").encode("ascii"), replace=False)
        return token
    except FileExistsError:
        return _decode_bearer(root.read_bytes("bearer-token", max_bytes=_MAX_BEARER_BYTES))


def rotate_core_bearer_token(root: HostServiceRoot) -> str:
    root.read_bytes("bearer-token", max_bytes=_MAX_BEARER_BYTES)
    token = secrets.token_urlsafe(48)
    root.atomic_write("bearer-token", (token + "\n").encode("ascii"), replace=True)
    return token


def compute_release_identity(
    *,
    framework_lock: str | Path,
    registry: object,
    source_commit: str,
) -> CoreReleaseIdentity:
    if re.fullmatch(r"[0-9a-f]{40}", source_commit) is None:
        raise RuntimeIdentityError("Core source identity is invalid")
    lock_payload = _read_external_regular_file(Path(framework_lock), max_bytes=1024 * 1024)
    snapshot = getattr(registry, "snapshot", None)
    registry_digest = getattr(snapshot, "registry_digest", None)
    attestations = getattr(registry, "distribution_attestations", None)
    if _DIGEST_PATTERN.fullmatch(str(registry_digest)) is None or not isinstance(
        attestations, Mapping
    ):
        raise RuntimeIdentityError("Verified framework identity is unavailable")
    distributions: list[dict[str, str]] = []
    for digest, attestation in sorted(attestations.items()):
        expectation = getattr(attestation, "expectation", None)
        inventory_digest = getattr(attestation, "inventory_digest", None)
        item = {
            "distribution": str(getattr(expectation, "distribution", "")),
            "distribution_version": str(getattr(expectation, "distribution_version", "")),
            "distribution_digest": str(digest),
            "inventory_digest": str(inventory_digest),
        }
        if any(not value for value in item.values()) or any(
            _DIGEST_PATTERN.fullmatch(item[key]) is None
            for key in ("distribution_digest", "inventory_digest")
        ):
            raise RuntimeIdentityError("Verified install inventory identity is invalid")
        distributions.append(item)
    if not distributions:
        raise RuntimeIdentityError("Verified install inventory is empty")
    lock_digest = hashlib.sha256(lock_payload).hexdigest()
    material = canonical_json_bytes(
        {
            "schema_version": 1,
            "framework_lock_sha256": lock_digest,
            "registry_digest": registry_digest,
            "source_commit": source_commit,
            "distributions": distributions,
        }
    )
    return CoreReleaseIdentity(
        digest=hashlib.sha256(b"openevo-core-release-v1\0" + material).hexdigest(),
        registry_digest=str(registry_digest),
        framework_lock_sha256=lock_digest,
        source_commit=source_commit,
    )


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("ascii")


def load_bounded_json(payload: bytes, *, max_bytes: int) -> Any:
    if not payload or len(payload) > max_bytes:
        raise RuntimeIdentityError("Core service JSON size is invalid")

    def closed_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise RuntimeIdentityError("Core service JSON contains duplicate keys")
            result[key] = value
        return result

    try:
        return json.loads(payload.decode("utf-8"), object_pairs_hook=closed_pairs)
    except RuntimeIdentityError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeIdentityError("Core service JSON is invalid") from exc


def _decode_bearer(payload: bytes) -> str:
    try:
        token = payload.decode("ascii").removesuffix("\n")
    except UnicodeError as exc:
        raise RuntimeIdentityError("Core bearer credential is invalid") from exc
    if _BEARER_PATTERN.fullmatch(token) is None:
        raise RuntimeIdentityError("Core bearer credential is invalid")
    return token


def _open_absolute_directory(path: Path, *, create: bool) -> int:
    parts = path.parts
    fd = os.open("/", os.O_RDONLY | os.O_CLOEXEC | _directory_flag())
    try:
        for index, component in enumerate(parts[1:], start=1):
            if component in {"", ".", ".."}:
                raise RuntimeIdentityError("Core service root path is invalid")
            try:
                child = _open_at(fd, component, os.O_RDONLY | _directory_flag())
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                os.fsync(fd)
                child = _open_at(fd, component, os.O_RDONLY | _directory_flag())
                _require_directory(child, mode=0o700, label="Core service path")
            os.close(fd)
            fd = child
            if index == len(parts) - 1:
                _require_directory(fd, mode=0o700, label="Core service root")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_external_regular_file(path: Path, *, max_bytes: int) -> bytes:
    if not path.is_absolute():
        raise RuntimeIdentityError("Framework lock path must be absolute")
    flags = os.O_RDONLY | os.O_CLOEXEC | _nofollow_flag()
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RuntimeIdentityError("Framework lock is unavailable") from exc
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or not 0 < metadata.st_size <= max_bytes
        ):
            raise RuntimeIdentityError("Framework lock metadata is invalid")
        return _read_exact(fd, metadata.st_size)
    finally:
        os.close(fd)


def _open_at(directory_fd: int, name: str, flags: int) -> int:
    return os.open(name, flags | os.O_CLOEXEC | _nofollow_flag(), dir_fd=directory_fd)


def _require_directory(fd: int, *, mode: int, label: str) -> None:
    metadata = os.fstat(fd)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != mode
    ):
        raise RuntimeIdentityError(f"{label} must be owner-only")


def _require_regular(
    fd: int,
    *,
    mode: int,
    max_bytes: int,
    allow_any_size: bool = False,
) -> None:
    _require_regular_metadata(
        os.fstat(fd), mode=mode, max_bytes=max_bytes, allow_any_size=allow_any_size
    )


def _require_regular_metadata(
    metadata: os.stat_result,
    *,
    mode: int,
    max_bytes: int,
    allow_any_size: bool = False,
) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != mode
        or (not allow_any_size and not 0 <= metadata.st_size <= max_bytes)
    ):
        raise RuntimeIdentityError("Core service file metadata is invalid")


def _require_at_binding(directory_fd: int, name: str, fd: int) -> None:
    expected = os.fstat(fd)
    actual = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
        raise RuntimeIdentityError("Core service pathname binding changed")


def _read_exact(fd: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(fd, min(remaining, 64 * 1024))
        if not chunk:
            raise RuntimeIdentityError("Core service file changed during read")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _write_all(fd: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(fd, payload[offset:])
        if written <= 0:
            raise RuntimeIdentityError("Core service state write made no progress")
        offset += written


def _metadata_identity(metadata: os.stat_result) -> tuple[int, ...]:
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


def _validate_leaf(name: str) -> None:
    if not name or name in {".", ".."} or "/" in name or "\0" in name:
        raise RuntimeIdentityError("Core service state name is invalid")


def _nofollow_flag() -> int:
    value = getattr(os, "O_NOFOLLOW", None)
    if value is None:
        raise RuntimeIdentityError("Core service root requires no-follow filesystem support")
    return value


def _directory_flag() -> int:
    value = getattr(os, "O_DIRECTORY", None)
    if value is None:
        raise RuntimeIdentityError("Core service root requires directory FD support")
    return value


def _rename_noreplace(directory_fd: int, source: str, destination: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise RuntimeIdentityError("Core service state requires atomic no-replace rename")
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
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), destination)


def _unlink_at_if_regular(directory_fd: int, name: str) -> None:
    try:
        fd = _open_at(directory_fd, name, os.O_RDONLY)
    except (FileNotFoundError, OSError):
        return
    try:
        metadata = os.fstat(fd)
        actual = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISREG(metadata.st_mode) and (
            actual.st_dev,
            actual.st_ino,
        ) == (metadata.st_dev, metadata.st_ino):
            os.unlink(name, dir_fd=directory_fd)
    finally:
        os.close(fd)


__all__ = [
    "CoreReleaseIdentity",
    "HostServiceRoot",
    "ManagedSubscriptionRuntimeIdentity",
    "RuntimeIdentityError",
    "canonical_json_bytes",
    "compute_release_identity",
    "default_core_service_root",
    "load_bounded_json",
    "load_or_create_core_bearer_token",
    "require_managed_subscription_runtime_identity",
    "require_host_global_service_root",
    "rotate_core_bearer_token",
]
