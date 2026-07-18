from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import SecretStr


DAEMON_BUNDLE_HOST_PROFILE_ID = "docker_user_container_v1"
_MAX_BUNDLE_BYTES = 4 * 1024 * 1024 * 1024
_MAX_JSON_BYTES = 16 * 1024
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_GENERATION_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
_VERSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,127}\Z")
_REMOTE_PATH_PATTERN = re.compile(r"/(?:[A-Za-z0-9._@%+=,-]+/)*[A-Za-z0-9._@%+=,-]+\Z")
_BUNDLE_ROOT_PATTERN = re.compile(r"/home/[A-Za-z0-9._@%+=,-]+/\.openevo/daemon-bundles\Z")
_TRANSFER_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
_ERROR_CODE_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")


class DaemonBundleTransportContractError(ValueError):
    """A closed local, remote command, or response contract violation."""


@dataclass(frozen=True, slots=True)
class DaemonBundleHostProfile:
    profile_id: Literal["docker_user_container_v1"]
    digest_command: Literal["sha256sum"]
    required_commands: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            self.profile_id != DAEMON_BUNDLE_HOST_PROFILE_ID
            or self.digest_command != "sha256sum"
            or self.required_commands
            != (
                "/bin/sh",
                "cat",
                "chmod",
                "id",
                "ln",
                "mkdir",
                "rm",
                "rmdir",
                "sha256sum",
                "stat",
            )
        ):
            raise DaemonBundleTransportContractError("Daemon bundle host profile is invalid.")


DOCKER_USER_CONTAINER_V1 = DaemonBundleHostProfile(
    profile_id=DAEMON_BUNDLE_HOST_PROFILE_ID,
    digest_command="sha256sum",
    required_commands=(
        "/bin/sh",
        "cat",
        "chmod",
        "id",
        "ln",
        "mkdir",
        "rm",
        "rmdir",
        "sha256sum",
        "stat",
    ),
)


@dataclass(frozen=True, slots=True)
class StagedDaemonBundle:
    host_profile: Literal["docker_user_container_v1"]
    sha256: str
    size: int
    reused: bool
    _service_root: str = field(repr=False, compare=False)
    _executable_path: str = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        expected_path = f"{self._service_root}/bundle-{self.sha256}"
        if (
            self.host_profile != DAEMON_BUNDLE_HOST_PROFILE_ID
            or not _valid_bundle_root(self._service_root)
            or self._executable_path != expected_path
            or not _valid_remote_path(self._executable_path)
            or _DIGEST_PATTERN.fullmatch(self.sha256) is None
            or type(self.size) is not int
            or not 0 < self.size <= _MAX_BUNDLE_BYTES
            or type(self.reused) is not bool
        ):
            raise DaemonBundleTransportContractError("Staged Daemon bundle receipt is invalid.")


@dataclass(frozen=True, slots=True)
class DaemonBundleIdentity:
    bundle_format: Literal["pyinstaller-onefile"]
    bundle_sha256: str
    bundle_size: int
    core_distribution: Literal["openevo"]
    core_version: str
    core_wheel_sha256: str
    dependency_lock_sha256: str
    framework_lock_sha256: str
    registry_digest: str
    release_identity: str
    source_commit: str
    platform_system: Literal["linux"]
    platform_architecture: Literal["x86_64"]


@dataclass(frozen=True, slots=True)
class DaemonBundleServiceStatus:
    remote_port: int
    bundle_sha256: str
    canonical_manifest_sha256: str
    lifecycle_compatibility: int
    release_identity: str
    registry_digest: str
    source_commit: str
    generation: str
    attached: bool


@dataclass(frozen=True, slots=True)
class DaemonBundleServicePredecessor:
    state: Literal["absent", "legacy", "running"]
    generation: str | None = None
    release_identity: str | None = None
    bundle_sha256: str | None = None
    canonical_manifest_sha256: str | None = None
    lifecycle_compatibility: int | None = None

    def __post_init__(self) -> None:
        absent = (
            self.state == "absent"
            and self.generation is None
            and self.release_identity is None
            and self.bundle_sha256 is None
            and self.canonical_manifest_sha256 is None
            and self.lifecycle_compatibility is None
        )
        legacy = (
            self.state == "legacy"
            and type(self.generation) is str
            and _GENERATION_PATTERN.fullmatch(self.generation) is not None
            and type(self.release_identity) is str
            and _DIGEST_PATTERN.fullmatch(self.release_identity) is not None
            and self.bundle_sha256 is None
            and self.canonical_manifest_sha256 is None
            and self.lifecycle_compatibility == 1
        )
        running = (
            self.state == "running"
            and type(self.generation) is str
            and _GENERATION_PATTERN.fullmatch(self.generation) is not None
            and type(self.release_identity) is str
            and _DIGEST_PATTERN.fullmatch(self.release_identity) is not None
            and type(self.bundle_sha256) is str
            and _DIGEST_PATTERN.fullmatch(self.bundle_sha256) is not None
            and type(self.canonical_manifest_sha256) is str
            and _DIGEST_PATTERN.fullmatch(self.canonical_manifest_sha256) is not None
            and type(self.lifecycle_compatibility) is int
            and self.lifecycle_compatibility >= 2
        )
        if not absent and not legacy and not running:
            raise DaemonBundleTransportContractError(
                "Daemon service predecessor identity is invalid."
            )


@dataclass(frozen=True, slots=True)
class DaemonBundleStopReceipt:
    stopped: Literal[True]


class OpenedDaemonBundle:
    """An immutable-enough local descriptor snapshot retained across streaming."""

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
        path_value: str,
        *,
        expected_sha256: str,
        expected_size: int,
    ) -> OpenedDaemonBundle:
        if (
            not isinstance(path_value, str)
            or not path_value
            or _DIGEST_PATTERN.fullmatch(expected_sha256) is None
            or type(expected_size) is not int
            or not 0 < expected_size <= _MAX_BUNDLE_BYTES
        ):
            raise DaemonBundleTransportContractError("Local Daemon bundle request is invalid.")
        path = Path(path_value).expanduser()
        flags = os.O_RDONLY | os.O_CLOEXEC
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise DaemonBundleTransportContractError(
                "Local no-follow file opening is unavailable."
            )
        descriptor = -1
        try:
            path_metadata = path.lstat()
            if stat.S_ISLNK(path_metadata.st_mode):
                raise DaemonBundleTransportContractError(
                    "Local Daemon bundle must not be a symlink."
                )
            descriptor = os.open(path, flags | nofollow)
            opened_metadata = os.fstat(descriptor)
            identity = _file_identity(opened_metadata)
            if (
                not stat.S_ISREG(opened_metadata.st_mode)
                or _file_identity(path_metadata) != identity
                or opened_metadata.st_size != expected_size
            ):
                raise DaemonBundleTransportContractError(
                    "Local Daemon bundle identity is invalid."
                )
            digest = _hash_descriptor(descriptor)
            if digest != expected_sha256:
                raise DaemonBundleTransportContractError(
                    "Local Daemon bundle digest does not match."
                )
            os.lseek(descriptor, 0, os.SEEK_SET)
            return cls(
                path=path,
                descriptor=descriptor,
                identity=identity,
                sha256=digest,
                size=opened_metadata.st_size,
            )
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            raise

    def rewind(self) -> None:
        os.lseek(self.descriptor, 0, os.SEEK_SET)

    def verify_unchanged(self) -> None:
        try:
            opened_metadata = os.fstat(self.descriptor)
            path_metadata = self.path.lstat()
        except OSError as exc:
            raise DaemonBundleTransportContractError(
                "Local Daemon bundle identity changed during staging."
            ) from exc
        if (
            not stat.S_ISREG(opened_metadata.st_mode)
            or _file_identity(opened_metadata) != self._identity
            or _file_identity(path_metadata) != self._identity
            or opened_metadata.st_size != self.size
        ):
            raise DaemonBundleTransportContractError(
                "Local Daemon bundle identity changed during staging."
            )
        if _hash_descriptor(self.descriptor) != self.sha256:
            raise DaemonBundleTransportContractError(
                "Local Daemon bundle content changed during staging."
            )
        self.rewind()

    def close(self) -> None:
        descriptor, self.descriptor = self.descriptor, -1
        if descriptor >= 0:
            os.close(descriptor)

    def __enter__(self) -> OpenedDaemonBundle:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()


def build_daemon_bundle_stage_command(
    *,
    service_root: str,
    sha256: str,
    size: int,
    transfer_id: str,
    host_profile: DaemonBundleHostProfile = DOCKER_USER_CONTAINER_V1,
) -> str:
    host_profile.__post_init__()
    if (
        not _valid_bundle_root(service_root)
        or _DIGEST_PATTERN.fullmatch(sha256) is None
        or type(size) is not int
        or not 0 < size <= _MAX_BUNDLE_BYTES
        or _TRANSFER_PATTERN.fullmatch(transfer_id) is None
    ):
        raise DaemonBundleTransportContractError("Daemon bundle staging request is invalid.")
    arguments = (
        "openevo-daemon-stage-v1",
        service_root,
        sha256,
        str(size),
        transfer_id,
        host_profile.profile_id,
    )
    return " ".join(
        (
            "/bin/sh",
            "-c",
            shlex.quote(_STAGE_SCRIPT),
            *(shlex.quote(value) for value in arguments),
        )
    )


def parse_staged_daemon_bundle(payload: str) -> StagedDaemonBundle:
    value = _load_closed_json(payload)
    expected = {
        "executable_path",
        "host_profile",
        "reused",
        "schema_version",
        "sha256",
        "size",
    }
    if type(value) is not dict or set(value) != expected or value["schema_version"] != 1:
        raise DaemonBundleTransportContractError("Daemon bundle staging receipt is invalid.")
    try:
        service_root = str(value["executable_path"]).rsplit("/", 1)[0]
        return StagedDaemonBundle(
            host_profile=value["host_profile"],
            sha256=value["sha256"],
            size=value["size"],
            reused=value["reused"],
            _service_root=service_root,
            _executable_path=value["executable_path"],
        )
    except (TypeError, ValueError):
        raise DaemonBundleTransportContractError(
            "Daemon bundle staging receipt is invalid."
        ) from None


def build_daemon_bundle_identity_command(bundle: StagedDaemonBundle) -> str:
    bundle.__post_init__()
    return f"{shlex.quote(bundle._executable_path)} identity"


def build_daemon_bundle_ensure_command(
    bundle: StagedDaemonBundle,
    *,
    port: int,
    deadline_seconds: float,
    expected_predecessor: DaemonBundleServicePredecessor,
    canonical_manifest_sha256: str,
) -> str:
    bundle.__post_init__()
    if (
        type(port) is not int
        or not 0 <= port <= 65535
        or isinstance(deadline_seconds, bool)
        or not isinstance(deadline_seconds, (int, float))
        or not 0 < deadline_seconds <= 300
        or not isinstance(expected_predecessor, DaemonBundleServicePredecessor)
        or not _valid_digest(canonical_manifest_sha256)
    ):
        raise DaemonBundleTransportContractError("Daemon bundle ensure request is invalid.")
    expected_predecessor.__post_init__()
    executable = shlex.quote(bundle._executable_path)
    manifest_path = shlex.quote(f"{bundle._service_root}/bundle-{canonical_manifest_sha256}")
    command = (
        f"{executable} service ensure --port {port} --deadline-seconds {deadline_seconds:.6f}"
        f" --expected-bundle-sha256 {bundle.sha256}"
        f" --expected-canonical-manifest-sha256 {canonical_manifest_sha256}"
        f" --canonical-manifest-path {manifest_path}"
    )
    if expected_predecessor.state == "absent":
        return f"{command} --expect-service-absent"
    predecessor = (
        f"{command} --expect-service-generation {expected_predecessor.generation}"
        " --expect-service-release-identity "
        f"{expected_predecessor.release_identity}"
        " --expect-service-lifecycle-compatibility "
        f"{expected_predecessor.lifecycle_compatibility}"
    )
    if expected_predecessor.state == "legacy":
        return predecessor
    return (
        f"{predecessor} --expect-service-bundle-sha256 "
        f"{expected_predecessor.bundle_sha256}"
        " --expect-service-canonical-manifest-sha256 "
        f"{expected_predecessor.canonical_manifest_sha256}"
    )


def build_daemon_bundle_observe_command(
    bundle: StagedDaemonBundle,
    *,
    deadline_seconds: float,
    canonical_manifest_sha256: str,
) -> str:
    bundle.__post_init__()
    if (
        isinstance(deadline_seconds, bool)
        or not isinstance(deadline_seconds, (int, float))
        or not 0 < deadline_seconds <= 300
        or not _valid_digest(canonical_manifest_sha256)
    ):
        raise DaemonBundleTransportContractError("Daemon bundle observe request is invalid.")
    return (
        f"{shlex.quote(bundle._executable_path)} service observe"
        f" --deadline-seconds {deadline_seconds:.6f}"
        f" --expected-bundle-sha256 {bundle.sha256}"
        f" --expected-canonical-manifest-sha256 {canonical_manifest_sha256}"
        " --canonical-manifest-path "
        f"{shlex.quote(f'{bundle._service_root}/bundle-{canonical_manifest_sha256}')}"
    )


def build_daemon_bundle_inspect_command(bundle: StagedDaemonBundle) -> str:
    bundle.__post_init__()
    return f"{shlex.quote(bundle._executable_path)} service inspect"


def build_daemon_bundle_stop_command(
    bundle: StagedDaemonBundle,
    *,
    deadline_seconds: float,
) -> str:
    bundle.__post_init__()
    if (
        isinstance(deadline_seconds, bool)
        or not isinstance(deadline_seconds, (int, float))
        or not 0 < deadline_seconds <= 300
    ):
        raise DaemonBundleTransportContractError("Daemon bundle stop request is invalid.")
    return (
        f"{shlex.quote(bundle._executable_path)} service stop"
        f" --deadline-seconds {deadline_seconds:.6f}"
    )


def parse_daemon_bundle_identity(payload: SecretStr) -> DaemonBundleIdentity:
    value = _load_secret_json(payload)
    expected = {
        "bundle",
        "core",
        "dependencies",
        "framework",
        "platform",
        "release",
        "schema_version",
    }
    if type(value) is not dict or set(value) != expected or value["schema_version"] != 1:
        raise DaemonBundleTransportContractError("Daemon bundle identity is invalid.")
    bundle = _closed_dict(value["bundle"], {"format", "sha256", "size"})
    core = _closed_dict(value["core"], {"distribution", "version", "wheel_sha256"})
    dependencies = _closed_dict(value["dependencies"], {"lock_sha256"})
    framework = _closed_dict(value["framework"], {"lock_sha256", "registry_digest"})
    platform = _closed_dict(value["platform"], {"architecture", "system"})
    release = _closed_dict(value["release"], {"identity", "source_commit"})
    if (
        bundle["format"] != "pyinstaller-onefile"
        or core["distribution"] != "openevo"
        or platform != {"architecture": "x86_64", "system": "linux"}
        or not _valid_digest(bundle["sha256"])
        or type(bundle["size"]) is not int
        or not 0 < bundle["size"] <= _MAX_BUNDLE_BYTES
        or type(core["version"]) is not str
        or _VERSION_PATTERN.fullmatch(core["version"]) is None
        or not all(
            _valid_digest(value)
            for value in (
                core["wheel_sha256"],
                dependencies["lock_sha256"],
                framework["lock_sha256"],
                framework["registry_digest"],
                release["identity"],
            )
        )
        or type(release["source_commit"]) is not str
        or _COMMIT_PATTERN.fullmatch(release["source_commit"]) is None
    ):
        raise DaemonBundleTransportContractError("Daemon bundle identity is invalid.")
    return DaemonBundleIdentity(
        bundle_format=bundle["format"],
        bundle_sha256=bundle["sha256"],
        bundle_size=bundle["size"],
        core_distribution=core["distribution"],
        core_version=core["version"],
        core_wheel_sha256=core["wheel_sha256"],
        dependency_lock_sha256=dependencies["lock_sha256"],
        framework_lock_sha256=framework["lock_sha256"],
        registry_digest=framework["registry_digest"],
        release_identity=release["identity"],
        source_commit=release["source_commit"],
        platform_system=platform["system"],
        platform_architecture=platform["architecture"],
    )


def parse_daemon_bundle_error_code(payload: SecretStr) -> str:
    value = _load_secret_json(payload)
    if type(value) is not dict or set(value) != {"error", "schema_version"}:
        raise DaemonBundleTransportContractError("Daemon bundle error receipt is invalid.")
    error = _closed_dict(value["error"], {"code", "message", "retryable"})
    if (
        value["schema_version"] != 1
        or type(error["code"]) is not str
        or _ERROR_CODE_PATTERN.fullmatch(error["code"]) is None
        or type(error["message"]) is not str
        or not 0 < len(error["message"].encode("utf-8")) <= 1024
        or type(error["retryable"]) is not bool
    ):
        raise DaemonBundleTransportContractError("Daemon bundle error receipt is invalid.")
    return error["code"]


def parse_daemon_bundle_service_status(payload: SecretStr) -> DaemonBundleServiceStatus:
    value = _load_secret_json(payload)
    expected = {
        "attached",
        "bundle_sha256",
        "canonical_manifest_sha256",
        "generation",
        "lifecycle_compatibility",
        "port",
        "registry_digest",
        "release_identity",
        "schema_version",
        "source_commit",
    }
    if type(value) is not dict or set(value) != expected or value["schema_version"] != 2:
        raise DaemonBundleTransportContractError("Daemon service status is invalid.")
    if (
        type(value["attached"]) is not bool
        or type(value["port"]) is not int
        or not 1 <= value["port"] <= 65535
        or not _valid_digest(value["registry_digest"])
        or not _valid_digest(value["release_identity"])
        or not _valid_digest(value["bundle_sha256"])
        or not _valid_digest(value["canonical_manifest_sha256"])
        or type(value["lifecycle_compatibility"]) is not int
        or value["lifecycle_compatibility"] < 2
        or type(value["source_commit"]) is not str
        or _COMMIT_PATTERN.fullmatch(value["source_commit"]) is None
        or type(value["generation"]) is not str
        or _GENERATION_PATTERN.fullmatch(value["generation"]) is None
    ):
        raise DaemonBundleTransportContractError("Daemon service status is invalid.")
    return DaemonBundleServiceStatus(
        remote_port=value["port"],
        bundle_sha256=value["bundle_sha256"],
        canonical_manifest_sha256=value["canonical_manifest_sha256"],
        lifecycle_compatibility=value["lifecycle_compatibility"],
        release_identity=value["release_identity"],
        registry_digest=value["registry_digest"],
        source_commit=value["source_commit"],
        generation=value["generation"],
        attached=value["attached"],
    )


def split_daemon_bundle_service_attachment(
    payload: SecretStr,
) -> tuple[DaemonBundleServiceStatus, SecretStr]:
    value = _load_secret_json(payload)
    secret_keys = {
        "bearer_token",
        "capture_mode",
        "execution_mode",
        "host",
        "status_proof",
    }
    public_keys = {
        "attached",
        "bundle_sha256",
        "canonical_manifest_sha256",
        "generation",
        "lifecycle_compatibility",
        "port",
        "registry_digest",
        "release_identity",
        "schema_version",
        "source_commit",
    }
    if type(value) is not dict or set(value) != public_keys | secret_keys:
        raise DaemonBundleTransportContractError("Daemon service attachment is invalid.")
    public = {key: value[key] for key in public_keys}
    status = parse_daemon_bundle_service_status(
        SecretStr(json.dumps(public, separators=(",", ":"), sort_keys=True) + "\n")
    )
    control = dict(value)
    for key in (
        "bundle_sha256",
        "canonical_manifest_sha256",
        "lifecycle_compatibility",
    ):
        del control[key]
    control["schema_version"] = 1
    return (
        status,
        SecretStr(json.dumps(control, separators=(",", ":"), sort_keys=True)),
    )


def parse_daemon_bundle_service_predecessor(
    payload: SecretStr,
) -> DaemonBundleServicePredecessor:
    value = _load_secret_json(payload)
    if type(value) is dict and value == {"schema_version": 2, "state": "absent"}:
        return DaemonBundleServicePredecessor(state="absent")
    if (
        type(value) is dict
        and set(value)
        == {
            "generation",
            "lifecycle_compatibility",
            "release_identity",
            "schema_version",
            "state",
        }
        and value.get("schema_version") == 2
        and value.get("state") == "legacy"
    ):
        try:
            return DaemonBundleServicePredecessor(
                state="legacy",
                generation=value["generation"],
                release_identity=value["release_identity"],
                lifecycle_compatibility=value["lifecycle_compatibility"],
            )
        except DaemonBundleTransportContractError:
            raise DaemonBundleTransportContractError(
                "Daemon service predecessor identity is invalid."
            ) from None
    if (
        type(value) is not dict
        or set(value)
        != {
            "bundle_sha256",
            "canonical_manifest_sha256",
            "generation",
            "lifecycle_compatibility",
            "release_identity",
            "schema_version",
            "state",
        }
        or value.get("schema_version") != 2
        or value.get("state") != "running"
    ):
        raise DaemonBundleTransportContractError("Daemon service predecessor identity is invalid.")
    try:
        return DaemonBundleServicePredecessor(
            state="running",
            generation=value["generation"],
            release_identity=value["release_identity"],
            bundle_sha256=value["bundle_sha256"],
            canonical_manifest_sha256=value["canonical_manifest_sha256"],
            lifecycle_compatibility=value["lifecycle_compatibility"],
        )
    except DaemonBundleTransportContractError:
        raise DaemonBundleTransportContractError(
            "Daemon service predecessor identity is invalid."
        ) from None


def parse_daemon_bundle_stop_receipt(payload: SecretStr) -> DaemonBundleStopReceipt:
    value = _load_secret_json(payload)
    if (
        type(value) is not dict
        or set(value) != {"schema_version", "stopped"}
        or value != {"schema_version": 1, "stopped": True}
    ):
        raise DaemonBundleTransportContractError("Daemon stop receipt is invalid.")
    return DaemonBundleStopReceipt(stopped=True)


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
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


def _hash_descriptor(descriptor: int) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _valid_remote_path(value: object) -> bool:
    return (
        type(value) is str
        and _REMOTE_PATH_PATTERN.fullmatch(value) is not None
        and "//" not in value
        and "/../" not in f"{value}/"
        and "/./" not in f"{value}/"
    )


def _valid_bundle_root(value: object) -> bool:
    return _valid_remote_path(value) and _BUNDLE_ROOT_PATTERN.fullmatch(value) is not None


def _valid_digest(value: object) -> bool:
    return type(value) is str and _DIGEST_PATTERN.fullmatch(value) is not None


def _closed_dict(value: object, expected: set[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != expected:
        raise DaemonBundleTransportContractError("Daemon bundle response schema is invalid.")
    return value


def _load_secret_json(payload: SecretStr) -> object:
    if not isinstance(payload, SecretStr):
        raise DaemonBundleTransportContractError("Daemon bundle response is invalid.")
    return _load_closed_json(payload.get_secret_value())


def _load_closed_json(payload: str) -> object:
    if not isinstance(payload, str):
        raise DaemonBundleTransportContractError("Daemon bundle response is invalid.")
    try:
        encoded = payload.encode("utf-8")
    except UnicodeError:
        raise DaemonBundleTransportContractError("Daemon bundle response is invalid.") from None
    if not encoded or len(encoded) > _MAX_JSON_BYTES:
        raise DaemonBundleTransportContractError("Daemon bundle response is invalid.")

    def closed_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise DaemonBundleTransportContractError(
                    "Daemon bundle response contains duplicate fields."
                )
            result[key] = value
        return result

    try:
        value = json.loads(encoded, object_pairs_hook=closed_object)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise DaemonBundleTransportContractError("Daemon bundle response is invalid.") from None
    canonical = (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    if encoded != canonical:
        raise DaemonBundleTransportContractError("Daemon bundle response is not canonical JSON.")
    return value


_STAGE_SCRIPT = r"""
set -eu
LC_ALL=C
export LC_ALL
umask 077

root=$1
expected_digest=$2
expected_size=$3
transfer_id=$4
host_profile=$5
lock="$root/.bundle-stage.lock"
tmp="$root/.incoming-$transfer_id"
target="$root/bundle-$expected_digest"
lock_held=0

cleanup() {
    if [ -n "${tmp-}" ]; then rm -f -- "$tmp" >/dev/null 2>&1 || :; fi
    if [ "$lock_held" -eq 1 ]; then rmdir -- "$lock" >/dev/null 2>&1 || :; fi
}
trap cleanup 0
trap 'exit 129' 1
trap 'exit 130' 2
trap 'exit 143' 15

relative_root=${root#/home/}
remote_user=${relative_root%%/*}
[ -n "$remote_user" ]
[ "$root" = "/home/$remote_user/.openevo/daemon-bundles" ] || exit 64

mkdir -p -- "$root"
root_meta=$(stat -c '%F|%u|%a|%h|%d|%i' -- "$root")
old_ifs=$IFS
IFS='|'
set -- $root_meta
IFS=$old_ifs
[ "$#" -eq 6 ]
[ "$1" = "directory" ]
[ "$2" = "$(id -u)" ]
[ "$3" = "700" ]
[ "$4" -ge 2 ]
root_identity="$5:$6"

mkdir -- "$lock"
lock_held=1
[ "$(stat -c '%F|%u|%a' -- "$lock")" = "directory|$(id -u)|700" ]
[ "$(stat -c '%d:%i' -- "$root")" = "$root_identity" ]

set -C
cat > "$tmp"
set +C
chmod 700 -- "$tmp"
[ "$(stat -c '%d:%i' -- "$root")" = "$root_identity" ]

tmp_meta=$(stat -c '%F|%u|%a|%h|%s' -- "$tmp")
old_ifs=$IFS
IFS='|'
set -- $tmp_meta
IFS=$old_ifs
[ "$#" -eq 5 ]
[ "$1" = "regular file" ]
[ "$2" = "$(id -u)" ]
[ "$3" = "700" ]
[ "$4" = "1" ]
[ "$5" = "$expected_size" ]
actual_digest=$(sha256sum -- "$tmp")
actual_digest=${actual_digest%% *}
[ "$actual_digest" = "$expected_digest" ]

reused=false
if [ -e "$target" ] || [ -L "$target" ]; then
    target_meta=$(stat -c '%F|%u|%a|%h|%s' -- "$target")
    old_ifs=$IFS
    IFS='|'
    set -- $target_meta
    IFS=$old_ifs
    [ "$#" -eq 5 ]
    [ "$1" = "regular file" ]
    [ "$2" = "$(id -u)" ]
    [ "$3" = "700" ]
    [ "$4" = "1" ]
    [ "$5" = "$expected_size" ]
    target_digest=$(sha256sum -- "$target")
    target_digest=${target_digest%% *}
    [ "$target_digest" = "$expected_digest" ]
    reused=true
else
    ln -- "$tmp" "$target"
    rm -- "$tmp"
    tmp=
fi

[ "$(stat -c '%d:%i' -- "$root")" = "$root_identity" ]
final_meta=$(stat -c '%F|%u|%a|%h|%s' -- "$target")
old_ifs=$IFS
IFS='|'
set -- $final_meta
IFS=$old_ifs
[ "$#" -eq 5 ]
[ "$1" = "regular file" ]
[ "$2" = "$(id -u)" ]
[ "$3" = "700" ]
[ "$4" = "1" ]
[ "$5" = "$expected_size" ]
final_digest=$(sha256sum -- "$target")
final_digest=${final_digest%% *}
[ "$final_digest" = "$expected_digest" ]

printf '{"executable_path":"%s","host_profile":"%s","reused":%s,"schema_version":1,"sha256":"%s","size":%s}\n' \
    "$target" "$host_profile" "$reused" "$expected_digest" "$expected_size"
"""
