"""Verified host-path authority for Docker used from a user container."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import socket
import stat
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


DOCKER_SELF_INSPECT_FORMAT: Final[str] = (
    '{"id":{{json .Id}},"hostname":{{json .Config.Hostname}},'
    '"running":{{json .State.Running}},"mounts":{{json .Mounts}}}'
)
_MAX_INSPECT_BYTES: Final[int] = 256 * 1024
_CONTAINER_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_HOSTNAME_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{12}$")
_NAMESPACE_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_DIGEST_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
DOCKER_EXECUTABLE_PATH: Final[str] = "/usr/bin/docker"
DOCKER_SOCKET_PATH: Final[str] = "/var/run/docker.sock"
DOCKER_HOST_ENDPOINT: Final[str] = f"unix://{DOCKER_SOCKET_PATH}"
_DOCKER_CONFIG_PATH: Final[str] = "/proc/self"
_DOCKER_SOCKET_MODES: Final[frozenset[int]] = frozenset({0o600, 0o660})
_DIRECTORY_FLAGS: Final[int] = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
_PRIVATE_DIRECTORY_CREATE_ATTEMPTS: Final[int] = 128
_EXCLUDED_DESTINATIONS: Final[frozenset[str]] = frozenset(
    {"/", "/dev", "/proc", "/run", "/sys", "/var/run"}
)


class DockerHostPathError(RuntimeError):
    """The release-host Docker path authority cannot be established."""


@dataclass(frozen=True, slots=True)
class DockerExecutableAuthority:
    """Pinned identity for the release Docker executable pathname."""

    identity: tuple[int, int, int, int, int, int, int, int, int]
    identity_digest: str

    @classmethod
    def open(cls) -> DockerExecutableAuthority:
        try:
            metadata = os.stat(DOCKER_EXECUTABLE_PATH, follow_symlinks=False)
            identity = _docker_executable_identity(metadata)
        except OSError as exc:
            raise DockerHostPathError(
                "the release Docker executable authority is unavailable"
            ) from exc
        authority = cls(
            identity=identity,
            identity_digest=_identity_digest(
                {"path": DOCKER_EXECUTABLE_PATH, "identity": identity}
            ),
        )
        authority.verify()
        return authority

    def verify(self) -> None:
        try:
            metadata = os.stat(DOCKER_EXECUTABLE_PATH, follow_symlinks=False)
            identity = _docker_executable_identity(metadata)
        except OSError as exc:
            raise DockerHostPathError("the release Docker executable authority changed") from exc
        if identity != self.identity:
            raise DockerHostPathError("the release Docker executable authority changed")

    def argv(self, *arguments: str) -> tuple[str, ...]:
        self.verify()
        return (DOCKER_EXECUTABLE_PATH, *arguments)


@dataclass(frozen=True, slots=True)
class DockerSocketAuthority:
    """Pinned identity for the only Docker Engine socket allowed in release mode."""

    identity: tuple[int, int, int, int, int, int, int]
    identity_digest: str

    @classmethod
    def open(cls) -> DockerSocketAuthority:
        try:
            metadata = os.stat(DOCKER_SOCKET_PATH, follow_symlinks=False)
            identity = _docker_socket_identity(metadata)
        except OSError as exc:
            raise DockerHostPathError(
                "the release Docker Engine socket authority is unavailable"
            ) from exc
        authority = cls(
            identity=identity,
            identity_digest=_identity_digest({"path": DOCKER_SOCKET_PATH, "identity": identity}),
        )
        authority.verify()
        return authority

    def verify(self) -> None:
        try:
            metadata = os.stat(DOCKER_SOCKET_PATH, follow_symlinks=False)
            identity = _docker_socket_identity(metadata)
        except OSError as exc:
            raise DockerHostPathError(
                "the release Docker Engine socket authority changed"
            ) from exc
        if identity != self.identity:
            raise DockerHostPathError("the release Docker Engine socket authority changed")


@dataclass(frozen=True, slots=True)
class DockerEngineAuthority:
    """Pinned executable and local-socket authority for one Docker command phase."""

    executable: DockerExecutableAuthority
    engine_socket: DockerSocketAuthority
    identity_digest: str

    @classmethod
    def open(cls) -> DockerEngineAuthority:
        executable = DockerExecutableAuthority.open()
        engine_socket = DockerSocketAuthority.open()
        authority = cls(
            executable=executable,
            engine_socket=engine_socket,
            identity_digest=_identity_digest(
                {
                    "executable": executable.identity_digest,
                    "engine_socket": engine_socket.identity_digest,
                }
            ),
        )
        authority.verify()
        return authority

    def verify(self) -> None:
        self.executable.verify()
        self.engine_socket.verify()

    def argv(self, *arguments: str) -> tuple[str, ...]:
        self.verify()
        return (DOCKER_EXECUTABLE_PATH, *arguments)

    def environment(self) -> dict[str, str]:
        self.verify()
        return docker_cli_environment()


def docker_cli_environment() -> dict[str, str]:
    """Return the complete, non-inheriting environment for release Docker CLI calls."""

    return {
        "DOCKER_CONFIG": _DOCKER_CONFIG_PATH,
        "DOCKER_HOST": DOCKER_HOST_ENDPOINT,
        "HOME": "/proc/self",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
    }


class DockerHostPathSpec(BaseModel):
    """Closed, persisted mapping from Daemon paths to Docker-daemon paths."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    container_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    mount_destination: str
    mount_source: str
    runtime_container_root: str
    runtime_daemon_root: str
    mount_device: int = Field(ge=0)
    mount_inode: int = Field(gt=0)
    runtime_device: int = Field(ge=0)
    runtime_inode: int = Field(gt=0)
    sessions_device: int = Field(ge=0)
    sessions_inode: int = Field(gt=0)
    runtime_uid: int = Field(ge=0)
    identity_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_closed_identity(self) -> DockerHostPathSpec:
        destination = _canonical_absolute_path(
            self.mount_destination,
            "Docker mount destination",
        )
        source = _canonical_absolute_path(self.mount_source, "Docker mount source")
        runtime_container = _canonical_absolute_path(
            self.runtime_container_root,
            "runtime container root",
        )
        runtime_daemon = _canonical_absolute_path(
            self.runtime_daemon_root,
            "runtime daemon root",
        )
        try:
            relative = runtime_container.relative_to(destination)
        except ValueError as exc:
            raise ValueError("runtime container root is outside the Docker mount") from exc
        if relative == Path(".") or source / relative != runtime_daemon:
            raise ValueError("runtime Docker path mapping is inconsistent")
        if self.identity_digest != _identity_digest(self._identity_payload()):
            raise ValueError("runtime Docker path mapping digest is invalid")
        return self

    def _identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "container_id": self.container_id,
            "mount_destination": self.mount_destination,
            "mount_source": self.mount_source,
            "runtime_container_root": self.runtime_container_root,
            "runtime_daemon_root": self.runtime_daemon_root,
            "mount_device": self.mount_device,
            "mount_inode": self.mount_inode,
            "runtime_device": self.runtime_device,
            "runtime_inode": self.runtime_inode,
            "sessions_device": self.sessions_device,
            "sessions_inode": self.sessions_inode,
            "runtime_uid": self.runtime_uid,
        }

    def translate(self, path: Path) -> Path:
        """Translate one canonical path below the held runtime root."""

        candidate = _canonical_absolute_path(os.fspath(path), "runtime bind source")
        local_root = Path(self.runtime_container_root)
        try:
            relative = candidate.relative_to(local_root)
        except ValueError as exc:
            raise DockerHostPathError(
                "runtime bind source is outside the verified Docker data root"
            ) from exc
        return Path(self.runtime_daemon_root) / relative


@dataclass(slots=True)
class HeldDockerSessionRoot:
    """Held no-follow authority for a verified runtime root and sessions directory."""

    spec: DockerHostPathSpec
    runtime_fd: int
    sessions_fd: int

    @classmethod
    def open(cls, spec: DockerHostPathSpec) -> HeldDockerSessionRoot:
        runtime_fd = -1
        sessions_fd = -1
        try:
            runtime_fd = os.open(spec.runtime_container_root, _DIRECTORY_FLAGS)
            sessions_fd = os.open("sessions", _DIRECTORY_FLAGS, dir_fd=runtime_fd)
            authority = cls(
                spec=spec,
                runtime_fd=runtime_fd,
                sessions_fd=sessions_fd,
            )
            authority.verify()
            runtime_fd = -1
            sessions_fd = -1
            return authority
        except DockerHostPathError:
            raise
        except OSError as exc:
            raise DockerHostPathError(
                "runtime Docker session-root authority could not be pinned"
            ) from exc
        finally:
            if sessions_fd >= 0:
                os.close(sessions_fd)
            if runtime_fd >= 0:
                os.close(runtime_fd)

    def verify(self) -> None:
        if self.runtime_fd < 0 or self.sessions_fd < 0:
            raise DockerHostPathError("runtime Docker session-root authority is closed")
        try:
            opened_runtime = os.fstat(self.runtime_fd)
            named_runtime = os.stat(
                self.spec.runtime_container_root,
                follow_symlinks=False,
            )
            opened_sessions = os.fstat(self.sessions_fd)
            named_sessions = os.stat(
                "sessions",
                dir_fd=self.runtime_fd,
                follow_symlinks=False,
            )
            path_sessions = os.stat(
                Path(self.spec.runtime_container_root) / "sessions",
                follow_symlinks=False,
            )
        except OSError as exc:
            raise DockerHostPathError(
                "runtime Docker session-root authority is unavailable"
            ) from exc
        expected_runtime = (
            self.spec.runtime_device,
            self.spec.runtime_inode,
            self.spec.runtime_uid,
        )
        expected_sessions = (
            self.spec.sessions_device,
            self.spec.sessions_inode,
            self.spec.runtime_uid,
        )
        try:
            changed = (
                _private_identity(opened_runtime) != expected_runtime
                or _private_identity(named_runtime) != expected_runtime
                or _private_identity(opened_sessions) != expected_sessions
                or _private_identity(named_sessions) != expected_sessions
                or _private_identity(path_sessions) != expected_sessions
            )
        except DockerHostPathError as exc:
            raise DockerHostPathError("runtime Docker session-root authority changed") from exc
        if changed:
            raise DockerHostPathError("runtime Docker session-root authority changed")

    def create_private_directory(
        self,
        purpose: Literal["session", "credentials"],
        *,
        child_directories: tuple[str, ...] = (),
    ) -> tuple[Path, tuple[int, int, int]]:
        """Create one random private directory relative to the held sessions FD."""

        if any(
            not child or child in {".", ".."} or "/" in child or "\x00" in child
            for child in child_directories
        ):
            raise ValueError("private child directory name is invalid")
        self.verify()
        for _ in range(_PRIVATE_DIRECTORY_CREATE_ATTEMPTS):
            name = f"{purpose}-{secrets.token_hex(16)}"
            try:
                os.mkdir(name, mode=0o700, dir_fd=self.sessions_fd)
            except FileExistsError:
                continue
            except OSError as exc:
                raise DockerHostPathError(
                    "private runtime directory could not be created"
                ) from exc
            descriptor = -1
            try:
                descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=self.sessions_fd)
                metadata = _require_private_opened_directory(descriptor)
                named = os.stat(name, dir_fd=self.sessions_fd, follow_symlinks=False)
                if _directory_identity(named) != _directory_identity(metadata):
                    raise DockerHostPathError(
                        "private runtime directory changed while it was opened"
                    )
                for child in child_directories:
                    os.mkdir(child, mode=0o700, dir_fd=descriptor)
                    child_fd = os.open(child, _DIRECTORY_FLAGS, dir_fd=descriptor)
                    try:
                        _require_private_opened_directory(child_fd)
                    finally:
                        os.close(child_fd)
                os.fsync(descriptor)
                os.fsync(self.sessions_fd)
                final_named = os.stat(
                    name,
                    dir_fd=self.sessions_fd,
                    follow_symlinks=False,
                )
                if _directory_identity(final_named) != _directory_identity(metadata):
                    raise DockerHostPathError("private runtime directory binding changed")
                self.verify()
                return (
                    Path(self.spec.runtime_container_root) / "sessions" / name,
                    (metadata.st_dev, metadata.st_ino, metadata.st_uid),
                )
            except DockerHostPathError:
                raise
            except OSError as exc:
                raise DockerHostPathError("private runtime directory could not be sealed") from exc
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        raise DockerHostPathError("private runtime directory name space was exhausted")

    def create_private_child_directory(
        self,
        parent: Path,
        parent_identity: tuple[int, int, int],
        name: str,
        *,
        child_directories: tuple[str, ...] = (),
    ) -> Path:
        """Create a fixed private child below an already admitted session root."""

        if not name or name in {".", ".."} or "/" in name or "\x00" in name:
            raise ValueError("private child directory name is invalid")
        sessions_path = Path(self.spec.runtime_container_root) / "sessions"
        try:
            relative = parent.relative_to(sessions_path)
        except ValueError as exc:
            raise DockerHostPathError(
                "private runtime parent is outside the held sessions root"
            ) from exc
        if len(relative.parts) != 1:
            raise DockerHostPathError("private runtime parent is not a session root")
        self.verify()
        parent_fd = -1
        child_fd = -1
        try:
            parent_fd = os.open(relative.name, _DIRECTORY_FLAGS, dir_fd=self.sessions_fd)
            parent_state = _require_private_opened_directory(parent_fd)
            if (
                parent_state.st_dev,
                parent_state.st_ino,
                parent_state.st_uid,
            ) != parent_identity:
                raise DockerHostPathError("private runtime parent identity changed")
            named_parent = os.stat(
                relative.name,
                dir_fd=self.sessions_fd,
                follow_symlinks=False,
            )
            if _directory_identity(named_parent) != _directory_identity(parent_state):
                raise DockerHostPathError("private runtime parent binding changed")
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
            child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            child_state = _require_private_opened_directory(child_fd)
            for child in child_directories:
                os.mkdir(child, mode=0o700, dir_fd=child_fd)
                nested_fd = os.open(child, _DIRECTORY_FLAGS, dir_fd=child_fd)
                try:
                    _require_private_opened_directory(nested_fd)
                finally:
                    os.close(nested_fd)
            os.fsync(child_fd)
            os.fsync(parent_fd)
            final_parent = os.stat(
                relative.name,
                dir_fd=self.sessions_fd,
                follow_symlinks=False,
            )
            final_child = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if _directory_identity(final_parent) != _directory_identity(
                parent_state
            ) or _directory_identity(final_child) != _directory_identity(child_state):
                raise DockerHostPathError("private runtime child directory binding changed")
            self.verify()
            return parent / name
        except DockerHostPathError:
            raise
        except OSError as exc:
            raise DockerHostPathError(
                "private runtime child directory could not be created"
            ) from exc
        finally:
            if child_fd >= 0:
                os.close(child_fd)
            if parent_fd >= 0:
                os.close(parent_fd)

    def close(self) -> None:
        if self.sessions_fd >= 0:
            os.close(self.sessions_fd)
            self.sessions_fd = -1
        if self.runtime_fd >= 0:
            os.close(self.runtime_fd)
            self.runtime_fd = -1


def docker_self_inspect_argv(hostname: str | None = None) -> tuple[str, ...]:
    """Return the bounded Docker command used for self-container evidence."""

    current = hostname or socket.gethostname()
    if _HOSTNAME_RE.fullmatch(current) is None:
        raise DockerHostPathError(
            "the Docker user-container profile requires a container-ID hostname"
        )
    return (
        DOCKER_EXECUTABLE_PATH,
        "container",
        "inspect",
        "--format",
        DOCKER_SELF_INSPECT_FORMAT,
        current,
    )


def discover_docker_host_path(
    inspect_payload: bytes,
    *,
    namespace: str,
    hostname: str | None = None,
    minimum_available_bytes: int = 512 * 1024 * 1024,
) -> DockerHostPathSpec:
    """Select and pin one writable host bind mount from self-inspect evidence."""

    if _NAMESPACE_RE.fullmatch(namespace) is None:
        raise DockerHostPathError("runtime namespace is invalid")
    if minimum_available_bytes < 0:
        raise ValueError("minimum_available_bytes must not be negative")
    current = hostname or socket.gethostname()
    observation = _parse_observation(inspect_payload, current)
    candidates: list[tuple[str, str, os.stat_result]] = []
    for item in observation["mounts"]:
        if not isinstance(item, dict) or item.get("Type") != "bind" or item.get("RW") is not True:
            continue
        source = item.get("Source")
        destination = item.get("Destination")
        if not isinstance(source, str) or not isinstance(destination, str):
            continue
        try:
            source_path = _canonical_absolute_path(source, "Docker mount source")
            destination_path = _canonical_absolute_path(
                destination,
                "Docker mount destination",
            )
            if os.fspath(destination_path) in _EXCLUDED_DESTINATIONS:
                continue
            metadata = os.lstat(destination_path)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or destination_path.resolve(strict=True) != destination_path
            ):
                continue
            private_parent = destination_path / f".openevo-runtime-{os.geteuid()}"
            mount_is_writable = os.access(destination_path, os.W_OK | os.X_OK)
            if not mount_is_writable and not _is_usable_preprovisioned_private_directory(
                private_parent,
                os.geteuid(),
            ):
                continue
            filesystem = os.statvfs(destination_path if mount_is_writable else private_parent)
            available = filesystem.f_bavail * filesystem.f_frsize
            if available < minimum_available_bytes:
                continue
        except (OSError, ValueError):
            continue
        candidates.append(
            (
                os.fspath(destination_path),
                os.fspath(source_path),
                metadata,
            )
        )
    if not candidates:
        raise DockerHostPathError(
            "no writable Docker bind-mounted data root satisfies the release profile"
        )
    if len(candidates) != 1:
        raise DockerHostPathError("Docker bind-mounted data-root evidence is ambiguous")

    destination_text, source_text, mount_metadata = candidates[0]
    uid = os.geteuid()
    private_parent = Path(destination_text) / f".openevo-runtime-{uid}"
    runtime_root = private_parent / namespace
    _ensure_private_directory(private_parent, uid)
    _ensure_private_directory(runtime_root, uid)
    _ensure_private_directory(runtime_root / "sessions", uid)
    runtime_metadata = os.lstat(runtime_root)
    sessions_metadata = os.lstat(runtime_root / "sessions")
    relative = runtime_root.relative_to(Path(destination_text))
    runtime_daemon_root = Path(source_text) / relative
    payload: dict[str, object] = {
        "schema_version": 1,
        "container_id": observation["id"],
        "mount_destination": destination_text,
        "mount_source": source_text,
        "runtime_container_root": os.fspath(runtime_root),
        "runtime_daemon_root": os.fspath(runtime_daemon_root),
        "mount_device": mount_metadata.st_dev,
        "mount_inode": mount_metadata.st_ino,
        "runtime_device": runtime_metadata.st_dev,
        "runtime_inode": runtime_metadata.st_ino,
        "sessions_device": sessions_metadata.st_dev,
        "sessions_inode": sessions_metadata.st_ino,
        "runtime_uid": uid,
    }
    return DockerHostPathSpec.model_validate(
        {
            **payload,
            "identity_digest": _identity_digest(payload),
        }
    )


def verify_docker_host_path(
    spec: DockerHostPathSpec,
    inspect_payload: bytes,
    *,
    hostname: str | None = None,
) -> None:
    """Revalidate persisted Docker and local filesystem authority."""

    current = hostname or socket.gethostname()
    observation = _parse_observation(inspect_payload, current)
    if observation["id"] != spec.container_id:
        raise DockerHostPathError("the Daemon container identity changed")
    matches = [
        item
        for item in observation["mounts"]
        if isinstance(item, dict) and item.get("Destination") == spec.mount_destination
    ]
    if len(matches) != 1:
        raise DockerHostPathError("the verified Docker data mount is unavailable")
    mount = matches[0]
    if (
        mount.get("Type") != "bind"
        or mount.get("Source") != spec.mount_source
        or mount.get("RW") is not True
    ):
        raise DockerHostPathError("the verified Docker data mount changed")
    mount_metadata = _private_directory_metadata(
        Path(spec.mount_destination),
        expected_uid=None,
        require_private=False,
    )
    runtime_metadata = _private_directory_metadata(
        Path(spec.runtime_container_root),
        expected_uid=spec.runtime_uid,
        require_private=True,
    )
    if (mount_metadata.st_dev, mount_metadata.st_ino) != (spec.mount_device, spec.mount_inode) or (
        runtime_metadata.st_dev,
        runtime_metadata.st_ino,
    ) != (spec.runtime_device, spec.runtime_inode):
        raise DockerHostPathError("the verified Docker data-root identity changed")
    sessions_metadata = _private_directory_metadata(
        Path(spec.runtime_container_root) / "sessions",
        expected_uid=spec.runtime_uid,
        require_private=True,
    )
    if (sessions_metadata.st_dev, sessions_metadata.st_ino) != (
        spec.sessions_device,
        spec.sessions_inode,
    ):
        raise DockerHostPathError("the verified Docker data-root identity changed")


def _parse_observation(payload: bytes, hostname: str) -> dict[str, object]:
    if len(payload) > _MAX_INSPECT_BYTES:
        raise DockerHostPathError("Docker self-inspect evidence exceeded its limit")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DockerHostPathError("Docker self-inspect evidence is invalid") from exc
    if not isinstance(value, dict) or set(value) != {
        "id",
        "hostname",
        "running",
        "mounts",
    }:
        raise DockerHostPathError("Docker self-inspect evidence is not closed")
    container_id = value.get("id")
    observed_hostname = value.get("hostname")
    mounts = value.get("mounts")
    if (
        not isinstance(container_id, str)
        or _CONTAINER_ID_RE.fullmatch(container_id) is None
        or _HOSTNAME_RE.fullmatch(hostname) is None
        or hostname != container_id[:12]
        or observed_hostname != hostname
        or value.get("running") is not True
        or not isinstance(mounts, list)
        or len(mounts) > 128
    ):
        raise DockerHostPathError("Docker self-container identity is invalid")
    return value


def _ensure_private_directory(path: Path, uid: int) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    metadata = _private_directory_metadata(
        path,
        expected_uid=uid,
        require_private=True,
    )
    if path.resolve(strict=True) != path or metadata.st_nlink < 2:
        raise DockerHostPathError("runtime Docker data root is not private")


def _is_usable_preprovisioned_private_directory(path: Path, uid: int) -> bool:
    try:
        metadata = _private_directory_metadata(
            path,
            expected_uid=uid,
            require_private=True,
        )
        return (
            path.resolve(strict=True) == path
            and metadata.st_nlink >= 2
            and os.access(path, os.W_OK | os.X_OK)
        )
    except (DockerHostPathError, OSError):
        return False


def _private_directory_metadata(
    path: Path,
    *,
    expected_uid: int | None,
    require_private: bool,
) -> os.stat_result:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise DockerHostPathError("runtime Docker data root is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or (expected_uid is not None and metadata.st_uid != expected_uid)
        or (require_private and stat.S_IMODE(metadata.st_mode) != 0o700)
    ):
        raise DockerHostPathError("runtime Docker data root is not private")
    return metadata


def _directory_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_uid


def _private_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise DockerHostPathError("runtime Docker data root is not private")
    return metadata.st_dev, metadata.st_ino, metadata.st_uid


def _require_private_opened_directory(descriptor: int) -> os.stat_result:
    metadata = os.fstat(descriptor)
    _private_identity(metadata)
    if metadata.st_nlink < 2:
        raise DockerHostPathError("private runtime directory is invalid")
    return metadata


def _canonical_absolute_path(value: str, label: str) -> Path:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value != os.path.normpath(value)
        or "\x00" in value
        or "," in value
        or any(ord(character) < 0x20 for character in value)
    ):
        raise ValueError(f"{label} must be a canonical safe absolute path")
    parts = PurePosixPath(value).parts
    if any(part in {"", ".", ".."} for part in parts[1:]):
        raise ValueError(f"{label} must be a canonical safe absolute path")
    return Path(value)


def _identity_digest(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _docker_executable_identity(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int, int, int, int]:
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_nlink < 1
        or not mode & 0o111
        or mode & 0o022
        or metadata.st_size <= 0
    ):
        raise DockerHostPathError("the release Docker executable identity is invalid")
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _docker_socket_identity(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int, int]:
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_uid != 0
        or mode not in _DOCKER_SOCKET_MODES
        or metadata.st_nlink != 1
    ):
        raise DockerHostPathError("the release Docker Engine socket identity is invalid")
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_ctime_ns,
    )


__all__ = [
    "DOCKER_EXECUTABLE_PATH",
    "DOCKER_HOST_ENDPOINT",
    "DOCKER_SOCKET_PATH",
    "DOCKER_SELF_INSPECT_FORMAT",
    "DockerEngineAuthority",
    "DockerExecutableAuthority",
    "DockerHostPathError",
    "DockerHostPathSpec",
    "DockerSocketAuthority",
    "HeldDockerSessionRoot",
    "discover_docker_host_path",
    "docker_cli_environment",
    "docker_self_inspect_argv",
    "verify_docker_host_path",
]
