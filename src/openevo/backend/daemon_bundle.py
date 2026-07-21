"""Self-contained OpenEvo Daemon bundle entrypoint."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import sys
from typing import Any, Sequence

from openevo import __version__
from openevo.backend.runtime_identity import (
    CoreReleaseIdentity,
    compute_release_identity,
    default_core_service_root,
)
from openevo.backend.service import (
    CoreDaemonBundleIdentity,
    CoreServiceAttachment,
    CoreServiceError,
    CoreServiceErrorCode,
    CoreServicePredecessor,
    ensure_core_service,
    inspect_core_service,
    observe_core_service_predecessor,
    stop_core_service,
    stop_core_service_if_generation,
)
from openevo.evolution.framework import (
    FrameworkDistributionLock,
    load_framework_distribution_lock,
    load_verified_framework_registry,
)


_ASSET_DIRECTORY = "openevo_daemon_bundle"
_BUILD_METADATA_NAME = "build-metadata.json"
_FRAMEWORK_LOCK_NAME = "framework-lock.json"
_MAX_METADATA_BYTES = 1024 * 1024
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_BUILD_METADATA_KEYS = {
    "bundle_format",
    "core",
    "dependency_lock",
    "platform",
    "python",
    "schema_version",
    "source_commit",
}
_CORE_METADATA_KEYS = {
    "distribution",
    "version",
    "wheel_filename",
    "wheel_sha256",
    "wheel_size",
}
_FILE_METADATA_KEYS = {"filename", "sha256"}
_PLATFORM_METADATA_KEYS = {"architecture", "system"}
_PYTHON_METADATA_KEYS = {"implementation", "version"}
_LIFECYCLE_COMPATIBILITY = 6


class DaemonBundleError(RuntimeError):
    """A fail-closed bundle identity or lifecycle error."""


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_descriptor(descriptor: int) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _read_descriptor(descriptor: int, size: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    payload = bytearray()
    while len(payload) <= size:
        chunk = os.read(descriptor, min(1024 * 1024, size + 1 - len(payload)))
        if not chunk:
            break
        payload.extend(chunk)
    if len(payload) != size:
        raise DaemonBundleError("The canonical Daemon manifest size changed while reading.")
    return bytes(payload)


def _verified_running_bundle_identity(
    *,
    expected_bundle_sha256: str,
    expected_canonical_manifest_sha256: str,
    canonical_manifest_path: str,
) -> CoreDaemonBundleIdentity:
    _require_digest(expected_bundle_sha256, "Expected Daemon bundle identity")
    _require_digest(
        expected_canonical_manifest_sha256,
        "Expected canonical Daemon manifest identity",
    )
    flags = os.O_RDONLY | os.O_CLOEXEC
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise DaemonBundleError("No-follow Daemon identity verification is unavailable.")
    try:
        descriptor = os.open("/proc/self/exe", flags)
        try:
            executing = os.fstat(descriptor)
            pathname = os.stat(sys.executable, follow_symlinks=False)
            executable_digest = _hash_descriptor(descriptor)
            final_executing = os.fstat(descriptor)
            final_pathname = os.stat(sys.executable, follow_symlinks=False)
            if (
                not stat.S_ISREG(executing.st_mode)
                or not stat.S_ISREG(pathname.st_mode)
                or executing.st_uid != os.geteuid()
                or (executing.st_dev, executing.st_ino) != (pathname.st_dev, pathname.st_ino)
                or executable_digest != expected_bundle_sha256
                or (
                    final_executing.st_dev,
                    final_executing.st_ino,
                    final_executing.st_size,
                    final_executing.st_mtime_ns,
                    final_executing.st_ctime_ns,
                )
                != (
                    executing.st_dev,
                    executing.st_ino,
                    executing.st_size,
                    executing.st_mtime_ns,
                    executing.st_ctime_ns,
                )
                or (final_pathname.st_dev, final_pathname.st_ino)
                != (executing.st_dev, executing.st_ino)
            ):
                raise DaemonBundleError(
                    "The executing Daemon bundle does not match the sealed deployment identity."
                )
        finally:
            os.close(descriptor)
        expected_manifest_path = (
            Path(sys.executable).parent / f"bundle-{expected_canonical_manifest_sha256}"
        )
        if canonical_manifest_path != str(expected_manifest_path):
            raise DaemonBundleError("The canonical Daemon manifest path is not content-addressed.")
        manifest_fd = os.open(canonical_manifest_path, flags | nofollow)
        try:
            manifest_metadata = os.fstat(manifest_fd)
            manifest_path_metadata = os.stat(
                canonical_manifest_path,
                follow_symlinks=False,
            )
            manifest_identity = (
                manifest_metadata.st_dev,
                manifest_metadata.st_ino,
                manifest_metadata.st_size,
                manifest_metadata.st_mtime_ns,
                manifest_metadata.st_ctime_ns,
            )
            if (
                not stat.S_ISREG(manifest_metadata.st_mode)
                or manifest_metadata.st_uid != os.geteuid()
                or manifest_metadata.st_nlink != 1
                or not 0 < manifest_metadata.st_size <= _MAX_MANIFEST_BYTES
                or (manifest_path_metadata.st_dev, manifest_path_metadata.st_ino)
                != (manifest_metadata.st_dev, manifest_metadata.st_ino)
            ):
                raise DaemonBundleError("The canonical Daemon manifest digest is invalid.")
            payload = _read_descriptor(manifest_fd, manifest_metadata.st_size)
            final_metadata = os.fstat(manifest_fd)
            final_path_metadata = os.stat(
                canonical_manifest_path,
                follow_symlinks=False,
            )
            if (
                hashlib.sha256(payload).hexdigest() != expected_canonical_manifest_sha256
                or (
                    final_metadata.st_dev,
                    final_metadata.st_ino,
                    final_metadata.st_size,
                    final_metadata.st_mtime_ns,
                    final_metadata.st_ctime_ns,
                )
                != manifest_identity
                or (final_path_metadata.st_dev, final_path_metadata.st_ino)
                != (manifest_metadata.st_dev, manifest_metadata.st_ino)
            ):
                raise DaemonBundleError(
                    "The canonical Daemon manifest changed during verification."
                )
        finally:
            os.close(manifest_fd)
    except OSError as exc:
        raise DaemonBundleError(
            "The executing Daemon bundle identity could not be verified."
        ) from exc
    try:
        manifest = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DaemonBundleError("The canonical Daemon manifest is unreadable.") from exc
    if payload != _canonical_json(manifest):
        raise DaemonBundleError("The canonical Daemon manifest is not canonical.")
    metadata, _lock, release = _verified_release()
    _verify_canonical_manifest(
        manifest,
        expected_bundle_sha256=expected_bundle_sha256,
        expected_bundle_size=executing.st_size,
        metadata=metadata,
        release=release,
    )
    return CoreDaemonBundleIdentity(
        bundle_sha256=expected_bundle_sha256,
        canonical_manifest_sha256=expected_canonical_manifest_sha256,
        lifecycle_compatibility=_LIFECYCLE_COMPATIBILITY,
    )


def _verify_canonical_manifest(
    value: object,
    *,
    expected_bundle_sha256: str,
    expected_bundle_size: int,
    metadata: dict[str, Any],
    release: CoreReleaseIdentity,
) -> None:
    keys = {
        "artifact",
        "build_environment_distributions",
        "core",
        "dependency_lock",
        "platform",
        "release",
        "runtime",
        "schema_version",
        "smoke",
    }
    if type(value) is not dict or set(value) != keys:
        raise DaemonBundleError("The canonical Daemon manifest schema is not closed.")
    artifact = _require_closed_dict(value["artifact"], {"filename", "sha256", "size"}, "Artifact")
    core = _require_closed_dict(
        value["core"],
        {"framework_lock", "registry_digest", "wheel"},
        "Core manifest",
    )
    framework_lock = _require_closed_dict(
        core["framework_lock"],
        {"filename", "sha256"},
        "Framework lock manifest",
    )
    wheel = _require_closed_dict(
        core["wheel"],
        {"filename", "sha256", "size", "version"},
        "Core wheel manifest",
    )
    dependency_lock = _require_closed_dict(
        value["dependency_lock"],
        {"filename", "sha256"},
        "Dependency lock manifest",
    )
    release_value = _require_closed_dict(
        value["release"],
        {"identity", "source_commit"},
        "Release manifest",
    )
    runtime = _require_closed_dict(
        value["runtime"],
        {"format", "python", "system_python_required", "target_pypi_required"},
        "Runtime manifest",
    )
    smoke = _require_closed_dict(
        value["smoke"],
        {"backend_readiness", "controlled_exit", "identity"},
        "Smoke manifest",
    )
    if (
        value["schema_version"] != 1
        or artifact["sha256"] != expected_bundle_sha256
        or artifact["size"] != expected_bundle_size
        or core["registry_digest"] != release.registry_digest
        or framework_lock["sha256"] != release.framework_lock_sha256
        or wheel["sha256"] != metadata["core"]["wheel_sha256"]
        or wheel["size"] != metadata["core"]["wheel_size"]
        or wheel["version"] != metadata["core"]["version"]
        or dependency_lock["sha256"] != metadata["dependency_lock"]["sha256"]
        or value["platform"] != {"architecture": "x86_64", "system": "linux"}
        or release_value != {"identity": release.digest, "source_commit": release.source_commit}
        or runtime["format"] != "pyinstaller-onefile"
        or runtime["system_python_required"] is not False
        or runtime["target_pypi_required"] is not False
        or smoke
        != {
            "backend_readiness": "passed",
            "controlled_exit": "passed",
            "identity": "passed",
        }
        or type(value["build_environment_distributions"]) is not list
    ):
        raise DaemonBundleError(
            "The canonical Daemon manifest does not bind the executing release."
        )


def _bundle_root() -> Path:
    root_value = getattr(sys, "_MEIPASS", None)
    if not isinstance(root_value, str) or not root_value:
        raise DaemonBundleError("The Daemon entrypoint is not running from a frozen bundle.")
    try:
        root = Path(root_value).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise DaemonBundleError("The frozen Daemon asset root is unavailable.") from exc
    if not root.is_dir():
        raise DaemonBundleError("The frozen Daemon asset root is invalid.")
    return root


def _asset_path(name: str) -> Path:
    if Path(name).name != name or name in {"", ".", ".."}:
        raise DaemonBundleError("The embedded Daemon asset name is invalid.")
    path = _bundle_root() / _ASSET_DIRECTORY / name
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise DaemonBundleError(f"The embedded Daemon asset is unavailable: {name}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise DaemonBundleError(f"The embedded Daemon asset is not a regular file: {name}")
    return path


def _load_build_metadata() -> dict[str, Any]:
    path = _asset_path(_BUILD_METADATA_NAME)
    try:
        payload = path.read_bytes()
        if not payload or len(payload) > _MAX_METADATA_BYTES:
            raise DaemonBundleError("The embedded Daemon build metadata size is invalid.")
        value = json.loads(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DaemonBundleError("The embedded Daemon build metadata is unreadable.") from exc
    if type(value) is not dict or set(value) != _BUILD_METADATA_KEYS:
        raise DaemonBundleError("The embedded Daemon build metadata schema is not closed.")
    if payload != _canonical_json(value):
        raise DaemonBundleError("The embedded Daemon build metadata is not canonical.")
    _validate_build_metadata(value)
    return value


def _require_closed_dict(
    value: object,
    keys: set[str],
    subject: str,
) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise DaemonBundleError(f"{subject} does not use the closed bundle schema.")
    return value


def _require_digest(value: object, subject: str) -> str:
    if type(value) is not str or _DIGEST_PATTERN.fullmatch(value) is None:
        raise DaemonBundleError(f"{subject} is not a lowercase SHA-256 digest.")
    return value


def _validate_build_metadata(value: dict[str, Any]) -> None:
    core = _require_closed_dict(value["core"], _CORE_METADATA_KEYS, "Core build metadata")
    dependency_lock = _require_closed_dict(
        value["dependency_lock"],
        _FILE_METADATA_KEYS,
        "Dependency lock metadata",
    )
    platform_metadata = _require_closed_dict(
        value["platform"],
        _PLATFORM_METADATA_KEYS,
        "Platform metadata",
    )
    python_metadata = _require_closed_dict(
        value["python"],
        _PYTHON_METADATA_KEYS,
        "Python metadata",
    )
    source_commit = value["source_commit"]
    if (
        value["schema_version"] != 1
        or value["bundle_format"] != "pyinstaller-onefile"
        or type(source_commit) is not str
        or _COMMIT_PATTERN.fullmatch(source_commit) is None
        or core["distribution"] != "openevo"
        or core["version"] != __version__
        or type(core["wheel_filename"]) is not str
        or Path(core["wheel_filename"]).name != core["wheel_filename"]
        or not core["wheel_filename"].endswith(".whl")
        or type(core["wheel_size"]) is not int
        or core["wheel_size"] <= 0
        or dependency_lock["filename"] != "uv.lock"
        or platform_metadata != {"architecture": "x86_64", "system": "linux"}
        or python_metadata["implementation"] != "CPython"
        or type(python_metadata["version"]) is not str
        or not python_metadata["version"].startswith("3.11.")
    ):
        raise DaemonBundleError("The embedded Daemon build metadata identity is invalid.")
    _require_digest(core["wheel_sha256"], "Core wheel identity")
    _require_digest(dependency_lock["sha256"], "Dependency lock identity")


def _verified_release() -> tuple[
    dict[str, Any],
    FrameworkDistributionLock,
    CoreReleaseIdentity,
]:
    metadata = _load_build_metadata()
    core = metadata["core"]
    lock_path = _asset_path(_FRAMEWORK_LOCK_NAME)
    try:
        lock, wheel_path = load_framework_distribution_lock(lock_path)
        wheel_metadata = wheel_path.lstat()
    except (OSError, ValueError) as exc:
        raise DaemonBundleError("The embedded framework lock cannot be resolved.") from exc
    expected_wheel = _asset_path(core["wheel_filename"])
    try:
        same_wheel = os.path.samefile(wheel_path, expected_wheel)
    except OSError as exc:
        raise DaemonBundleError("The embedded Core wheel binding cannot be verified.") from exc
    if (
        not same_wheel
        or lock.distribution != core["distribution"]
        or lock.distribution_version != core["version"]
        or lock.distribution_digest != core["wheel_sha256"]
        or lock.wheel_filename != core["wheel_filename"]
        or wheel_metadata.st_size != core["wheel_size"]
        or _sha256(expected_wheel) != core["wheel_sha256"]
    ):
        raise DaemonBundleError("The framework lock does not bind the embedded Core wheel.")
    try:
        registry = load_verified_framework_registry(lock_path)
        release = compute_release_identity(
            framework_lock=lock_path,
            registry=registry,
            source_commit=metadata["source_commit"],
        )
    except Exception as exc:
        raise DaemonBundleError(
            "The embedded Core install does not match its verified registry lock."
        ) from exc
    return metadata, lock, release


def release_identity() -> dict[str, object]:
    if sys.platform != "linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise DaemonBundleError("This Daemon bundle requires Linux x86_64.")
    metadata, lock, release = _verified_release()
    executable = Path(sys.executable)
    try:
        executable_metadata = executable.lstat()
    except OSError as exc:
        raise DaemonBundleError("The Daemon executable identity is unavailable.") from exc
    if stat.S_ISLNK(executable_metadata.st_mode) or not stat.S_ISREG(executable_metadata.st_mode):
        raise DaemonBundleError("The Daemon executable must be a regular non-symlink file.")
    return {
        "bundle": {
            "format": metadata["bundle_format"],
            "sha256": _sha256(executable),
            "size": executable_metadata.st_size,
        },
        "core": {
            "distribution": lock.distribution,
            "version": lock.distribution_version,
            "wheel_sha256": lock.distribution_digest,
        },
        "dependencies": {
            "lock_sha256": metadata["dependency_lock"]["sha256"],
        },
        "framework": {
            "lock_sha256": release.framework_lock_sha256,
            "registry_digest": release.registry_digest,
        },
        "platform": {"architecture": "x86_64", "system": "linux"},
        "release": {
            "identity": release.digest,
            "source_commit": release.source_commit,
        },
        "schema_version": 1,
    }


def _service_metadata(attachment: CoreServiceAttachment) -> dict[str, object]:
    if (
        attachment.bundle_sha256 is None
        or attachment.canonical_manifest_sha256 is None
        or attachment.lifecycle_compatibility is None
    ):
        raise CoreServiceError(
            CoreServiceErrorCode.UPDATE_REQUIRED,
            "The active service predates exact Daemon bundle attachment.",
            retryable=False,
        )
    return {
        "attached": attachment.attached,
        "bundle_sha256": attachment.bundle_sha256,
        "canonical_manifest_sha256": attachment.canonical_manifest_sha256,
        "generation": attachment.generation,
        "lifecycle_compatibility": attachment.lifecycle_compatibility,
        "port": attachment.port,
        "registry_digest": attachment.registry_digest,
        "release_identity": attachment.release_identity,
        "schema_version": 2,
        "source_commit": attachment.source_commit,
    }


def _service_predecessor_metadata(
    predecessor: CoreServicePredecessor,
) -> dict[str, object]:
    if predecessor.state == "absent":
        return {"schema_version": 2, "state": "absent"}
    if predecessor.state == "legacy":
        return {
            "generation": predecessor.generation,
            "lifecycle_compatibility": predecessor.lifecycle_compatibility,
            "release_identity": predecessor.release_identity,
            "schema_version": 2,
            "state": "legacy",
        }
    return {
        "bundle_sha256": predecessor.bundle_sha256,
        "canonical_manifest_sha256": predecessor.canonical_manifest_sha256,
        "generation": predecessor.generation,
        "lifecycle_compatibility": predecessor.lifecycle_compatibility,
        "release_identity": predecessor.release_identity,
        "schema_version": 2,
        "state": "running",
    }


def _service_bootstrap_payload(attachment: CoreServiceAttachment) -> dict[str, object]:
    """Return the authenticated attachment only over the caller's secret SSH channel."""

    payload = _service_metadata(attachment)
    payload.update(
        {
            "bearer_token": attachment.bearer_token,
            "capture_mode": "transcript",
            "execution_mode": "subscription",
            "host": "127.0.0.1",
            "status_proof": attachment.status_proof,
        }
    )
    return payload


def smoke_daemon(*, deadline_seconds: float) -> dict[str, object]:
    identity = release_identity()
    metadata = _load_build_metadata()
    service_root = default_core_service_root()
    attachment: CoreServiceAttachment | None = None
    stopped = False
    try:
        attachment = ensure_core_service(
            service_root=service_root,
            framework_lock=_asset_path(_FRAMEWORK_LOCK_NAME),
            source_commit=metadata["source_commit"],
            deadline_seconds=deadline_seconds,
            _reuse_frozen_extraction_for_bounded_smoke=True,
        )
        if attachment.attached:
            raise DaemonBundleError("Daemon smoke requires an unused canonical service root.")
    finally:
        if attachment is not None and not attachment.attached:
            stopped = stop_core_service_if_generation(
                service_root=service_root,
                expected_generation=attachment.generation,
                expected_release_identity=attachment.release_identity,
                deadline_seconds=deadline_seconds,
            )
    if not stopped:
        raise DaemonBundleError("Daemon smoke could not prove controlled service exit.")
    return {
        "identity": identity,
        "readiness": {
            "backend_ready": True,
            "controlled_exit": True,
        },
        "schema_version": 1,
    }


def _required_option_index(arguments: Sequence[str], option: str) -> int:
    indexes = [index for index, argument in enumerate(arguments) if argument == option]
    if len(indexes) != 1 or indexes[0] + 1 >= len(arguments):
        raise DaemonBundleError(f"Internal Daemon invocation is missing {option}.")
    index = indexes[0]
    if arguments[index + 1].startswith("--"):
        raise DaemonBundleError(f"Internal Daemon invocation has an invalid {option}.")
    return index


def _replace_option(arguments: list[str], option: str, value: str) -> None:
    index = _required_option_index(arguments, option)
    arguments[index + 1] = value


def _invoke_internal_module_main(
    module: str,
    module_arguments: Sequence[str],
    main: Any,
) -> int:
    previous_argv = sys.argv
    try:
        sys.argv = [module, *module_arguments]
        result = main()
    finally:
        sys.argv = previous_argv
    if result is None:
        return 0
    if type(result) is int:
        return result
    raise DaemonBundleError("Internal Daemon module returned an invalid status.")


def _internal_module_dispatch(arguments: Sequence[str]) -> int | None:
    values = list(arguments)
    if values and values[0] == "-I":
        values.pop(0)
    if not values:
        return None
    if values[0] == "-c":
        if len(values) < 2:
            raise DaemonBundleError("Internal Daemon script invocation is incomplete.")
        from openevo.deployment.managed_runtime_assets import _REMOTE_MANAGED_RUNTIME_SCRIPT

        script = values[1]
        if script != _REMOTE_MANAGED_RUNTIME_SCRIPT:
            raise DaemonBundleError("Internal Daemon script invocation is not allowlisted.")
        previous_argv = sys.argv
        try:
            sys.argv = ["openevo-daemon-managed-runtime", *values[2:]]
            exec(
                compile(script, "<openevo-managed-runtime-v1>", "exec"),
                {"__name__": "__main__"},
            )
        finally:
            sys.argv = previous_argv
        return 0
    if values[0] != "-m":
        return None
    if len(values) < 3:
        raise DaemonBundleError("Internal Daemon module invocation is incomplete.")
    module = values[1]
    module_arguments = values[2:]
    metadata = _load_build_metadata()
    if module == "openevo.backend.launcher":
        _replace_option(
            module_arguments,
            "--framework-lock",
            str(_asset_path(_FRAMEWORK_LOCK_NAME)),
        )
        _replace_option(module_arguments, "--source-commit", metadata["source_commit"])
        from openevo.backend.launcher import main as launcher_main

        return launcher_main(module_arguments)
    if module == "openevo.backend.service":
        if "--framework-lock" in module_arguments:
            _replace_option(
                module_arguments,
                "--framework-lock",
                str(_asset_path(_FRAMEWORK_LOCK_NAME)),
            )
        if "--wheel-path" in module_arguments:
            _replace_option(
                module_arguments,
                "--wheel-path",
                str(_asset_path(metadata["core"]["wheel_filename"])),
            )
        if "--source-commit" in module_arguments:
            _replace_option(module_arguments, "--source-commit", metadata["source_commit"])
        from openevo.backend.service import main as service_main

        return service_main(module_arguments)
    if module == "openevo.evolution.cli":
        if not module_arguments or module_arguments[0] not in {"serve", "worker"}:
            raise DaemonBundleError("Internal evolution invocation is not allowlisted.")
        _replace_option(
            module_arguments,
            "--framework-lock",
            str(_asset_path(_FRAMEWORK_LOCK_NAME)),
        )
        from openevo.evolution.cli import main as evolution_main

        return _invoke_internal_module_main(module, module_arguments, evolution_main)
    if module == "openevo.rollout.server":
        _required_option_index(module_arguments, "--config")
        from openevo.rollout.server import main as rollout_main

        return _invoke_internal_module_main(module, module_arguments, rollout_main)
    if module == "openevo.gateway.server":
        _required_option_index(module_arguments, "--config")
        from openevo.gateway.server import main as gateway_main

        return _invoke_internal_module_main(module, module_arguments, gateway_main)
    raise DaemonBundleError("Internal Daemon module invocation is not allowlisted.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openevo-daemon")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("identity")
    smoke = subparsers.add_parser("smoke")
    smoke.add_argument("--deadline-seconds", type=float, default=45.0)
    service = subparsers.add_parser("service")
    service_subparsers = service.add_subparsers(dest="service_command", required=True)
    ensure = service_subparsers.add_parser("ensure")
    ensure.add_argument("--port", type=int, default=0)
    ensure.add_argument("--deadline-seconds", type=float, default=45.0)
    predecessor = ensure.add_mutually_exclusive_group(required=True)
    predecessor.add_argument("--expect-service-absent", action="store_true")
    predecessor.add_argument("--expect-service-generation")
    ensure.add_argument("--expect-service-release-identity")
    ensure.add_argument("--expect-service-bundle-sha256")
    ensure.add_argument("--expect-service-canonical-manifest-sha256")
    ensure.add_argument("--expect-service-lifecycle-compatibility", type=int)
    ensure.add_argument("--expected-bundle-sha256", required=True)
    ensure.add_argument("--expected-canonical-manifest-sha256", required=True)
    ensure.add_argument("--canonical-manifest-path", required=True)
    observe = service_subparsers.add_parser("observe")
    observe.add_argument("--deadline-seconds", type=float, default=15.0)
    observe.add_argument("--expected-bundle-sha256", required=True)
    observe.add_argument("--expected-canonical-manifest-sha256", required=True)
    observe.add_argument("--canonical-manifest-path", required=True)
    service_subparsers.add_parser("inspect")
    stop = service_subparsers.add_parser("stop")
    stop.add_argument("--deadline-seconds", type=float, default=15.0)
    managed_runtime = subparsers.add_parser("managed-runtime")
    managed_runtime.add_argument(
        "runtime_action",
        choices=("discard", "finalize", "prepare", "probe", "receive"),
    )
    managed_runtime.add_argument("runtime_arguments", nargs=argparse.REMAINDER)
    return parser


def _run_service_command(args: argparse.Namespace) -> dict[str, object]:
    service_root = default_core_service_root()
    if args.service_command == "ensure":
        bundle_identity = _verified_running_bundle_identity(
            expected_bundle_sha256=args.expected_bundle_sha256,
            expected_canonical_manifest_sha256=(args.expected_canonical_manifest_sha256),
            canonical_manifest_path=args.canonical_manifest_path,
        )
        if args.expect_service_absent:
            if any(
                value is not None
                for value in (
                    args.expect_service_release_identity,
                    args.expect_service_bundle_sha256,
                    args.expect_service_canonical_manifest_sha256,
                    args.expect_service_lifecycle_compatibility,
                )
            ):
                raise DaemonBundleError("Daemon service predecessor expectation is invalid.")
            predecessor = CoreServicePredecessor.absent()
        else:
            try:
                if args.expect_service_lifecycle_compatibility == 1:
                    if (
                        args.expect_service_bundle_sha256 is not None
                        or args.expect_service_canonical_manifest_sha256 is not None
                    ):
                        raise ValueError
                    predecessor = CoreServicePredecessor.legacy(
                        generation=args.expect_service_generation,
                        release_identity=args.expect_service_release_identity,
                    )
                else:
                    predecessor = CoreServicePredecessor.running(
                        generation=args.expect_service_generation,
                        release_identity=args.expect_service_release_identity,
                        bundle_sha256=args.expect_service_bundle_sha256,
                        canonical_manifest_sha256=(args.expect_service_canonical_manifest_sha256),
                        lifecycle_compatibility=(args.expect_service_lifecycle_compatibility),
                    )
            except (TypeError, ValueError):
                raise DaemonBundleError(
                    "Daemon service predecessor expectation is invalid."
                ) from None
        metadata = _load_build_metadata()
        attachment = ensure_core_service(
            service_root=service_root,
            framework_lock=_asset_path(_FRAMEWORK_LOCK_NAME),
            source_commit=metadata["source_commit"],
            port=args.port,
            deadline_seconds=args.deadline_seconds,
            replace_mismatched=True,
            expected_predecessor=predecessor,
            daemon_bundle_identity=bundle_identity,
        )
        return _service_bootstrap_payload(attachment)
    if args.service_command == "observe":
        _verified_running_bundle_identity(
            expected_bundle_sha256=args.expected_bundle_sha256,
            expected_canonical_manifest_sha256=(args.expected_canonical_manifest_sha256),
            canonical_manifest_path=args.canonical_manifest_path,
        )
        predecessor = observe_core_service_predecessor(
            service_root=service_root,
            deadline_seconds=args.deadline_seconds,
        )
        return _service_predecessor_metadata(predecessor)
    if args.service_command == "inspect":
        return _service_metadata(inspect_core_service(service_root=service_root))
    if args.service_command == "stop":
        stop_core_service(
            service_root=service_root,
            deadline_seconds=args.deadline_seconds,
            preserve_compatibility_floor=True,
        )
        return {"schema_version": 1, "stopped": True}
    raise DaemonBundleError("The Daemon service command is invalid.")


def _run_managed_runtime_command(args: argparse.Namespace) -> int:
    from openevo.deployment.managed_runtime_assets import _REMOTE_MANAGED_RUNTIME_SCRIPT

    result = _internal_module_dispatch(
        [
            "-I",
            "-c",
            _REMOTE_MANAGED_RUNTIME_SCRIPT,
            args.runtime_action,
            *args.runtime_arguments,
        ]
    )
    if result is None:
        raise DaemonBundleError("The managed runtime command could not be dispatched.")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    try:
        internal_result = _internal_module_dispatch(values)
        if internal_result is not None:
            return internal_result
        args = build_parser().parse_args(values)
        if args.command == "identity":
            result = release_identity()
        elif args.command == "smoke":
            if not 0 < args.deadline_seconds <= 300:
                raise DaemonBundleError("Daemon smoke deadline is invalid.")
            result = smoke_daemon(deadline_seconds=args.deadline_seconds)
        elif args.command == "service":
            result = _run_service_command(args)
        elif args.command == "managed-runtime":
            return _run_managed_runtime_command(args)
        else:
            raise DaemonBundleError("The Daemon command is invalid.")
        sys.stdout.buffer.write(_canonical_json(result))
        return 0
    except (DaemonBundleError, CoreServiceError) as exc:
        retryable = isinstance(exc, CoreServiceError) and exc.retryable
        code = exc.code.value if isinstance(exc, CoreServiceError) else "daemon_bundle_invalid"
        error = {
            "error": {
                "code": code,
                "message": str(exc),
                "retryable": retryable,
            },
            "schema_version": 1,
        }
        sys.stderr.buffer.write(_canonical_json(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
