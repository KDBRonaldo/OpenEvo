#!/usr/bin/env python3
"""Build and smoke one exact self-contained Linux x86_64 OpenEvo Daemon bundle."""

from __future__ import annotations

import argparse
from email.parser import Parser
import hashlib
import json
import os
from pathlib import Path
import platform
from pathlib import PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any, Sequence
from zipfile import BadZipFile, ZipFile


BUNDLE_NAME = "openevo-daemon-linux-x86_64"
MANIFEST_NAME = "openevo-daemon-bundle.json"
CHECKSUMS_NAME = "SHA256SUMS"
ASSET_DIRECTORY = "openevo_daemon_bundle"
BUILD_METADATA_NAME = "build-metadata.json"
FRAMEWORK_LOCK_NAME = "framework-lock.json"
SOURCE_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_PYTHON_VERSION = "3.11"
EXPECTED_LOCK_KEYS = {
    "distribution",
    "distribution_digest",
    "distribution_version",
    "schema_version",
    "wheel_filename",
}
EXPECTED_IDENTITY_KEYS = {
    "bundle",
    "core",
    "dependencies",
    "framework",
    "platform",
    "release",
    "schema_version",
}


class BundleBuildError(RuntimeError):
    pass


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path, *, maximum_bytes: int = 1024 * 1024) -> Any:
    try:
        payload = path.read_bytes()
        if not payload or len(payload) > maximum_bytes:
            raise BundleBuildError(f"JSON input size is invalid: {path.name}")

        def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in pairs:
                if key in value:
                    raise BundleBuildError(f"JSON input contains a duplicate key: {path.name}")
                value[key] = item
            return value

        return json.loads(payload, object_pairs_hook=reject_duplicates)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleBuildError(f"JSON input is unreadable: {path.name}") from exc


def _write_new(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags, mode)
    except FileExistsError as exc:
        raise BundleBuildError(f"Refusing to replace output: {path.name}") from exc
    with os.fdopen(descriptor, "wb") as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _validate_wheel(wheel: Path) -> tuple[str, int]:
    try:
        wheel_metadata = wheel.lstat()
        if stat.S_ISLNK(wheel_metadata.st_mode) or not stat.S_ISREG(wheel_metadata.st_mode):
            raise BundleBuildError("Core wheel must be a regular non-symlink file")
        with ZipFile(wheel) as archive:
            metadata_names = [
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            ]
            entry_point_names = [
                name for name in archive.namelist() if name.endswith(".dist-info/entry_points.txt")
            ]
            if len(metadata_names) != 1 or len(entry_point_names) != 1:
                raise BundleBuildError("Core wheel metadata or entry points are incomplete")
            metadata = Parser().parsestr(archive.read(metadata_names[0]).decode("utf-8"))
            entry_points = archive.read(entry_point_names[0]).decode("utf-8")
    except (BadZipFile, OSError, UnicodeDecodeError) as exc:
        raise BundleBuildError("Core wheel is unreadable") from exc
    version = str(metadata.get("Version") or "")
    if (
        metadata.get("Name") != "openevo"
        or not version
        or metadata.get("Requires-Python") != ">=3.11"
        or "openevo-backend = openevo.backend.launcher:main" not in entry_points
        or "openevo-core-service = openevo.backend.service:main" not in entry_points
    ):
        raise BundleBuildError("Core wheel does not match the Daemon release contract")
    return version, wheel_metadata.st_size


def _wheel_top_level_packages(wheel: Path) -> tuple[str, ...]:
    try:
        with ZipFile(wheel) as archive:
            top_levels = {
                PurePosixPath(name).parts[0]
                for name in archive.namelist()
                if name
                and not name.endswith("/")
                and ".dist-info/" not in name
                and ".data/" not in name
            }
    except (BadZipFile, OSError) as exc:
        raise BundleBuildError("Core wheel package inventory is unreadable") from exc
    if (
        not top_levels
        or "openevo" not in top_levels
        or any(
            not name or Path(name).name != name or not name.replace("_", "").isalnum()
            for name in top_levels
        )
    ):
        raise BundleBuildError("Core wheel top-level package inventory is invalid")
    return tuple(sorted(top_levels))


def _validate_exact_lock(lock_path: Path, wheel: Path, *, version: str) -> dict[str, Any]:
    if lock_path.name != FRAMEWORK_LOCK_NAME:
        raise BundleBuildError("framework lock must be named framework-lock.json")
    value = _load_json(lock_path)
    if type(value) is not dict or set(value) != EXPECTED_LOCK_KEYS:
        raise BundleBuildError("framework-lock.json does not use the closed release schema")
    expected = {
        "distribution": "openevo",
        "distribution_digest": _sha256(wheel),
        "distribution_version": version,
        "schema_version": "1",
        "wheel_filename": wheel.name,
    }
    if value != expected:
        raise BundleBuildError("framework-lock.json does not bind the exact Core wheel")
    if lock_path.read_bytes() != _canonical_json(value):
        raise BundleBuildError("framework-lock.json is not canonical")
    try:
        if not os.path.samefile(lock_path.resolve().parent / wheel.name, wheel):
            raise BundleBuildError("framework-lock.json and Core wheel are not colocated")
    except OSError as exc:
        raise BundleBuildError("framework-lock.json cannot resolve its exact Core wheel") from exc
    return value


def _require_linux_x86_64() -> None:
    if sys.platform != "linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise BundleBuildError("Daemon bundles must be built on Linux x86_64")


def _run(
    arguments: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(arguments),
        check=True,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=capture,
    )


def _isolated_environment(base: dict[str, str]) -> dict[str, str]:
    environment = {
        key: value
        for key, value in base.items()
        if key not in {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"}
    }
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def _prepare_build_environment(
    root: Path,
    *,
    wheel: Path,
    uv_lock: Path,
) -> tuple[Path, dict[str, str]]:
    project = root / "project"
    project.mkdir()
    shutil.copy2(REPO_ROOT / "pyproject.toml", project / "pyproject.toml")
    shutil.copy2(uv_lock, project / "uv.lock")
    environment_path = root / "venv"
    environment = _isolated_environment(os.environ.copy())
    environment["UV_PROJECT_ENVIRONMENT"] = str(environment_path)
    environment["UV_LINK_MODE"] = "copy"
    _run(
        [
            "uv",
            "sync",
            "--frozen",
            "--no-install-project",
            "--group",
            "dev",
            "--python",
            BUILD_PYTHON_VERSION,
            "--managed-python",
            "--project",
            str(project),
        ],
        cwd=project,
        env=environment,
    )
    python = environment_path / "bin" / "python"
    _run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--no-deps",
            "--no-index",
            "--reinstall",
            str(wheel),
        ],
        cwd=root,
        env=environment,
    )
    return python, environment


def _installed_identity(
    python: Path,
    *,
    framework_lock: Path,
    source_commit: str,
    cwd: Path,
    env: dict[str, str],
) -> dict[str, str]:
    program = "\n".join(
        (
            "import json",
            "from openevo.backend.runtime_identity import compute_release_identity",
            "from openevo.evolution.framework import load_verified_framework_registry",
            f"lock = {str(framework_lock)!r}",
            "registry = load_verified_framework_registry(lock)",
            f"release = compute_release_identity(framework_lock=lock, registry=registry, source_commit={source_commit!r})",
            "print(json.dumps({'registry_digest': release.registry_digest, 'release_identity': release.digest}, separators=(',', ':'), sort_keys=True))",
        )
    )
    result = _run(
        [str(python), "-I", "-c", program],
        cwd=cwd,
        env=env,
        capture=True,
    )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BundleBuildError("Installed Core identity output is invalid") from exc
    if (
        type(value) is not dict
        or set(value) != {"registry_digest", "release_identity"}
        or any(
            type(value[key]) is not str or DIGEST_PATTERN.fullmatch(value[key]) is None
            for key in value
        )
    ):
        raise BundleBuildError("Installed Core identity does not use the closed schema")
    return value


def _distribution_inventory(
    python: Path,
    *,
    cwd: Path,
    env: dict[str, str],
) -> list[dict[str, str]]:
    program = "\n".join(
        (
            "import json",
            "from importlib import metadata",
            "items = sorted({(str(d.metadata.get('Name') or '').lower().replace('_', '-'), str(d.version)) for d in metadata.distributions() if d.metadata.get('Name')})",
            "print(json.dumps([{'name': name, 'version': version} for name, version in items], separators=(',', ':'), sort_keys=True))",
        )
    )
    result = _run(
        [str(python), "-I", "-c", program],
        cwd=cwd,
        env=env,
        capture=True,
    )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BundleBuildError("Build distribution inventory is invalid") from exc
    if (
        type(value) is not list
        or not value
        or any(
            type(item) is not dict
            or set(item) != {"name", "version"}
            or type(item["name"]) is not str
            or not item["name"]
            or type(item["version"]) is not str
            or not item["version"]
            for item in value
        )
    ):
        raise BundleBuildError("Build distribution inventory does not use the closed schema")
    return value


def _installed_distribution_paths(
    python: Path,
    *,
    top_levels: tuple[str, ...],
    cwd: Path,
    env: dict[str, str],
) -> tuple[tuple[Path, ...], Path]:
    program = "\n".join(
        (
            "from importlib import metadata",
            "from pathlib import Path",
            "d = metadata.distribution('openevo')",
            "root = Path(d.locate_file('')).resolve(strict=True)",
            "metadata_path = Path(d._path).resolve(strict=True)",
            "print(root)",
            "print(metadata_path)",
        )
    )
    result = _run(
        [str(python), "-I", "-c", program],
        cwd=cwd,
        env=env,
        capture=True,
    )
    lines = result.stdout.splitlines()
    if len(lines) != 2:
        raise BundleBuildError("Installed Core paths are unavailable")
    install_root = Path(lines[0])
    metadata_path = Path(lines[1])
    package_paths = tuple(install_root / name for name in top_levels)
    if (
        any(not package_path.is_dir() for package_path in package_paths)
        or not metadata_path.is_dir()
        or metadata_path.parent != install_root
        or not metadata_path.name.endswith(".dist-info")
    ):
        raise BundleBuildError("Installed Core is not one wheel-backed distribution")
    direct_url = metadata_path / "direct_url.json"
    if direct_url.is_file():
        direct_url_value = _load_json(direct_url)
        if type(direct_url_value) is dict and "dir_info" in direct_url_value:
            raise BundleBuildError("Editable Core installs are forbidden for Daemon bundles")
    return package_paths, metadata_path


def _build_metadata(
    *,
    lock: dict[str, Any],
    wheel_size: int,
    source_commit: str,
    uv_lock: Path,
    python: Path,
    cwd: Path,
    env: dict[str, str],
) -> dict[str, object]:
    version_result = _run(
        [
            str(python),
            "-I",
            "-c",
            "import platform; print(platform.python_implementation()); print(platform.python_version())",
        ],
        cwd=cwd,
        env=env,
        capture=True,
    ).stdout.splitlines()
    if len(version_result) != 2 or version_result[0] != "CPython":
        raise BundleBuildError("Daemon build Python identity is invalid")
    return {
        "bundle_format": "pyinstaller-onefile",
        "core": {
            "distribution": lock["distribution"],
            "version": lock["distribution_version"],
            "wheel_filename": lock["wheel_filename"],
            "wheel_sha256": lock["distribution_digest"],
            "wheel_size": wheel_size,
        },
        "dependency_lock": {
            "filename": "uv.lock",
            "sha256": _sha256(uv_lock),
        },
        "platform": {"architecture": "x86_64", "system": "linux"},
        "python": {
            "implementation": version_result[0],
            "version": version_result[1],
        },
        "schema_version": 1,
        "source_commit": source_commit,
    }


def _validate_identity(
    value: object,
    *,
    bundle: Path,
    lock: dict[str, Any],
    installed_identity: dict[str, str],
    source_commit: str,
    uv_lock_sha256: str,
) -> dict[str, Any]:
    if type(value) is not dict or set(value) != EXPECTED_IDENTITY_KEYS:
        raise BundleBuildError("Daemon identity does not use the closed release schema")
    expected_nested = {
        "bundle": {"format", "sha256", "size"},
        "core": {"distribution", "version", "wheel_sha256"},
        "dependencies": {"lock_sha256"},
        "framework": {"lock_sha256", "registry_digest"},
        "platform": {"architecture", "system"},
        "release": {"identity", "source_commit"},
    }
    for key, keys in expected_nested.items():
        if type(value[key]) is not dict or set(value[key]) != keys:
            raise BundleBuildError(f"Daemon identity {key} schema is not closed")
    if (
        value["schema_version"] != 1
        or value["bundle"]
        != {
            "format": "pyinstaller-onefile",
            "sha256": _sha256(bundle),
            "size": bundle.stat().st_size,
        }
        or value["core"]
        != {
            "distribution": "openevo",
            "version": lock["distribution_version"],
            "wheel_sha256": lock["distribution_digest"],
        }
        or value["framework"]["registry_digest"] != installed_identity["registry_digest"]
        or value["dependencies"] != {"lock_sha256": uv_lock_sha256}
        or value["release"]
        != {
            "identity": installed_identity["release_identity"],
            "source_commit": source_commit,
        }
        or value["platform"] != {"architecture": "x86_64", "system": "linux"}
        or DIGEST_PATTERN.fullmatch(str(value["framework"]["lock_sha256"])) is None
    ):
        raise BundleBuildError("Daemon identity does not match its exact build inputs")
    return value


def _run_bundle_json(bundle: Path, arguments: Sequence[str], *, cwd: Path) -> Any:
    environment = _isolated_environment(os.environ.copy())
    result = subprocess.run(
        [str(bundle), *arguments],
        check=False,
        cwd=cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
        raise BundleBuildError(f"Daemon bundle command failed ({' '.join(arguments)}): {detail}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BundleBuildError("Daemon bundle emitted invalid JSON") from exc


def _publish_file(source: Path, destination: Path, *, mode: int) -> None:
    payload = source.read_bytes()
    _write_new(destination, payload, mode=mode)
    if _sha256(destination) != _sha256(source):
        raise BundleBuildError(f"Published output identity changed: {destination.name}")


def build_bundle(
    *,
    wheel: Path,
    framework_lock: Path,
    uv_lock: Path,
    source_commit: str,
    output_dir: Path,
) -> Path:
    _require_linux_x86_64()
    if SOURCE_COMMIT_PATTERN.fullmatch(source_commit) is None:
        raise BundleBuildError("source_commit must be one full lowercase Git commit")
    wheel = wheel.resolve(strict=True)
    framework_lock = framework_lock.resolve(strict=True)
    uv_lock = uv_lock.resolve(strict=True)
    if uv_lock.name != "uv.lock":
        raise BundleBuildError("dependency lock must be named uv.lock")
    version, wheel_size = _validate_wheel(wheel)
    top_levels = _wheel_top_level_packages(wheel)
    lock = _validate_exact_lock(framework_lock, wheel, version=version)
    if output_dir.exists():
        raise BundleBuildError("Daemon bundle output directory must not already exist")
    output_dir.mkdir(mode=0o700, parents=True)

    with TemporaryDirectory(prefix="openevo-daemon-build-") as temporary:
        root = Path(temporary)
        python, environment = _prepare_build_environment(root, wheel=wheel, uv_lock=uv_lock)
        installed_identity = _installed_identity(
            python,
            framework_lock=framework_lock,
            source_commit=source_commit,
            cwd=root,
            env=environment,
        )
        inventory = _distribution_inventory(python, cwd=root, env=environment)
        package_paths, distribution_metadata_path = _installed_distribution_paths(
            python,
            top_levels=top_levels,
            cwd=root,
            env=environment,
        )
        metadata = _build_metadata(
            lock=lock,
            wheel_size=wheel_size,
            source_commit=source_commit,
            uv_lock=uv_lock,
            python=python,
            cwd=root,
            env=environment,
        )
        assets = root / "assets"
        assets.mkdir()
        metadata_path = assets / BUILD_METADATA_NAME
        _write_new(metadata_path, _canonical_json(metadata))
        dist = root / "dist"
        work = root / "work"
        spec = root / "spec"
        package_by_name = dict(zip(top_levels, package_paths, strict=True))
        entrypoint = package_by_name["openevo"] / "backend" / "daemon_bundle.py"
        command = [
            str(python),
            "-I",
            "-m",
            "PyInstaller",
            "--clean",
            "--noconfirm",
            "--onefile",
            "--name",
            BUNDLE_NAME,
            "--distpath",
            str(dist),
            "--workpath",
            str(work),
            "--specpath",
            str(spec),
            "--collect-submodules",
            "openevo",
            "--add-data",
            f"{distribution_metadata_path}{os.pathsep}{distribution_metadata_path.name}",
            "--add-data",
            f"{wheel}{os.pathsep}{ASSET_DIRECTORY}",
            "--add-data",
            f"{framework_lock}{os.pathsep}{ASSET_DIRECTORY}",
            "--add-data",
            f"{metadata_path}{os.pathsep}{ASSET_DIRECTORY}",
            "--hidden-import",
            "uvicorn.logging",
            "--hidden-import",
            "uvicorn.loops.auto",
            "--hidden-import",
            "uvicorn.protocols.http.auto",
            "--hidden-import",
            "uvicorn.protocols.websockets.auto",
            str(entrypoint),
        ]
        for name, package_path in reversed(tuple(package_by_name.items())):
            command[command.index("--add-data") : command.index("--add-data")] = [
                "--add-data",
                f"{package_path}{os.pathsep}{name}",
            ]
        build_environment = _isolated_environment(environment)
        build_environment["SOURCE_DATE_EPOCH"] = os.environ.get("SOURCE_DATE_EPOCH", "0")
        _run(command, cwd=root, env=build_environment)
        built = dist / BUNDLE_NAME
        if not built.is_file():
            raise BundleBuildError("PyInstaller did not produce the Daemon executable")
        os.chmod(built, 0o755)
        identity = _validate_identity(
            _run_bundle_json(built, ["identity"], cwd=root),
            bundle=built,
            lock=lock,
            installed_identity=installed_identity,
            source_commit=source_commit,
            uv_lock_sha256=_sha256(uv_lock),
        )
        smoke = _run_bundle_json(
            built,
            ["smoke", "--deadline-seconds", "60"],
            cwd=root,
        )
        if (
            type(smoke) is not dict
            or set(smoke) != {"identity", "readiness", "schema_version"}
            or smoke["schema_version"] != 1
            or smoke["identity"] != identity
            or smoke["readiness"] != {"backend_ready": True, "controlled_exit": True}
        ):
            raise BundleBuildError("Daemon readiness smoke did not use the closed passing schema")

        output_bundle = output_dir / BUNDLE_NAME
        _publish_file(built, output_bundle, mode=0o755)
        manifest = {
            "artifact": {
                "filename": output_bundle.name,
                "sha256": _sha256(output_bundle),
                "size": output_bundle.stat().st_size,
            },
            "build_environment_distributions": inventory,
            "core": {
                "framework_lock": {
                    "filename": framework_lock.name,
                    "sha256": _sha256(framework_lock),
                },
                "registry_digest": installed_identity["registry_digest"],
                "wheel": {
                    "filename": wheel.name,
                    "sha256": _sha256(wheel),
                    "size": wheel_size,
                    "version": version,
                },
            },
            "dependency_lock": {
                "filename": uv_lock.name,
                "sha256": _sha256(uv_lock),
            },
            "platform": {"architecture": "x86_64", "system": "linux"},
            "release": {
                "identity": installed_identity["release_identity"],
                "source_commit": source_commit,
            },
            "runtime": {
                "format": "pyinstaller-onefile",
                "python": metadata["python"],
                "system_python_required": False,
                "target_pypi_required": False,
            },
            "schema_version": 1,
            "smoke": {
                "backend_readiness": "passed",
                "controlled_exit": "passed",
                "identity": "passed",
            },
        }
        manifest_path = output_dir / MANIFEST_NAME
        _write_new(manifest_path, _canonical_json(manifest))
        checksums = "".join(
            f"{_sha256(path)}  {path.name}\n"
            for path in sorted((output_bundle, manifest_path), key=lambda item: item.name)
        )
        _write_new(output_dir / CHECKSUMS_NAME, checksums.encode("ascii"))
        os.chmod(output_dir, 0o755)
        return output_bundle


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--framework-lock", type=Path, required=True)
    parser.add_argument("--uv-lock", type=Path, default=REPO_ROOT / "uv.lock")
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    bundle = build_bundle(
        wheel=args.wheel,
        framework_lock=args.framework_lock,
        uv_lock=args.uv_lock,
        source_commit=args.source_commit,
        output_dir=args.output_dir,
    )
    print(bundle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
