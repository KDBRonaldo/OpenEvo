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
    CoreServiceAttachment,
    CoreServiceError,
    ensure_core_service,
    inspect_core_service,
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
    return {
        "attached": attachment.attached,
        "generation": attachment.generation,
        "port": attachment.port,
        "registry_digest": attachment.registry_digest,
        "release_identity": attachment.release_identity,
        "schema_version": 1,
        "source_commit": attachment.source_commit,
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


def _replace_option(arguments: list[str], option: str, value: str) -> None:
    indexes = [index for index, argument in enumerate(arguments) if argument == option]
    if len(indexes) != 1 or indexes[0] + 1 >= len(arguments):
        raise DaemonBundleError(f"Internal Daemon invocation is missing {option}.")
    index = indexes[0]
    if arguments[index + 1].startswith("--"):
        raise DaemonBundleError(f"Internal Daemon invocation has an invalid {option}.")
    arguments[index + 1] = value


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
        metadata = _load_build_metadata()
        attachment = ensure_core_service(
            service_root=service_root,
            framework_lock=_asset_path(_FRAMEWORK_LOCK_NAME),
            source_commit=metadata["source_commit"],
            port=args.port,
            deadline_seconds=args.deadline_seconds,
            replace_mismatched=True,
        )
        return _service_bootstrap_payload(attachment)
    if args.service_command == "inspect":
        return _service_metadata(inspect_core_service(service_root=service_root))
    if args.service_command == "stop":
        stop_core_service(
            service_root=service_root,
            deadline_seconds=args.deadline_seconds,
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
