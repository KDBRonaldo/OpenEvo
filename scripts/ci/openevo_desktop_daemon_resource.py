#!/usr/bin/env python3
"""Compose and verify the immutable Desktop release-asset resource tree."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import ModuleType
from typing import Sequence

from openevo.runtime.managed import (
    MANAGED_RUNTIME_ARCHIVE_RELEASE,
    verify_managed_runtime_archive,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
DAEMON_BUNDLE_NAME = "openevo-daemon-linux-x86_64"
DAEMON_MANIFEST_NAME = "openevo-daemon-bundle.json"
FRAMEWORK_LOCK_NAME = "framework-lock.json"
RELEASE_ASSETS_DIRECTORY = Path("openevo-release-assets")
RELEASE_ASSETS_MANIFEST_NAME = "release-assets.json"
CORE_DIRECTORY = Path("core")
DAEMON_DIRECTORY = Path("daemon")
RUNTIME_DIRECTORY = Path("runtime")
MACOS_RESOURCE_ROOT = Path("Contents/Resources") / RELEASE_ASSETS_DIRECTORY
MACOS_ASKPASS_HELPER_PATH = Path("Contents/MacOS/openevo-ssh-askpass")
MAX_ASKPASS_HELPER_BYTES = 16 * 1024 * 1024
SOURCE_COMMIT_LENGTH = 40
_SHA256_HEX = frozenset("0123456789abcdef")
_MACOS_CODESIGN_FLAGS_PATTERN = re.compile(r"\bflags=0x[0-9a-fA-F]+\(([^)]*)\)")
_MAX_CODESIGN_OUTPUT_BYTES = 64 * 1024


class ResourceCompositionError(RuntimeError):
    pass


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ResourceCompositionError(f"Release composition module is unavailable: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def _require_source_commit(source_commit: str) -> None:
    if len(source_commit) != SOURCE_COMMIT_LENGTH or any(
        character not in _SHA256_HEX for character in source_commit
    ):
        raise ResourceCompositionError("source_commit must be one full lowercase Git commit")


def _is_darwin_system_path_alias(path: Path) -> bool:
    if sys.platform != "darwin" or path not in {Path("/etc"), Path("/tmp"), Path("/var")}:
        return False
    try:
        return path.resolve(strict=True) == Path("/private") / path.name
    except OSError:
        return False


def _reject_symlink_components(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        if current.exists() and current.is_symlink() and not _is_darwin_system_path_alias(current):
            raise ResourceCompositionError(
                f"Release asset path must not traverse a symlink: {path}"
            )


def _read_controlled_file(path: Path, *, executable: bool = False) -> bytes:
    _reject_symlink_components(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ResourceCompositionError(f"Controlled release input is unavailable: {path}") from exc
    try:
        before = os.fstat(descriptor)
        pathname = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or (before.st_dev, before.st_ino) != (pathname.st_dev, pathname.st_ino)
            or (executable and not before.st_mode & stat.S_IXUSR)
        ):
            raise ResourceCompositionError(f"Controlled release input is not trusted: {path}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
        if (
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
            or len(payload) != before.st_size
        ):
            raise ResourceCompositionError(
                f"Controlled release input changed while reading: {path}"
            )
        return payload
    finally:
        os.close(descriptor)


def _write_new_at(directory_fd: int, name: str, payload: bytes, *, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(name, flags, mode, dir_fd=directory_fd)
    except FileExistsError as exc:
        raise ResourceCompositionError(f"Refusing to replace release output: {name}") from exc
    try:
        os.fchmod(descriptor, mode)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ResourceCompositionError(f"Release output copy stalled: {name}")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_controlled_file_at(directory_fd: int, name: str, *, executable: bool = False) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise ResourceCompositionError(
            f"Controlled staged release asset is unavailable: {name}"
        ) from exc
    try:
        before = os.fstat(descriptor)
        pathname = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or (before.st_dev, before.st_ino) != (pathname.st_dev, pathname.st_ino)
            or (executable and not before.st_mode & stat.S_IXUSR)
        ):
            raise ResourceCompositionError(
                f"Controlled staged release asset is not trusted: {name}"
            )
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
            or len(payload) != before.st_size
        ):
            raise ResourceCompositionError(
                f"Controlled staged release asset changed while reading: {name}"
            )
        return payload
    finally:
        os.close(descriptor)


def _mkdir_new_at(directory_fd: int, name: str) -> int:
    os.mkdir(name, 0o700, dir_fd=directory_fd)
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        dir_fd=directory_fd,
    )
    try:
        os.fchmod(descriptor, 0o700)
        os.fsync(descriptor)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _rename_noreplace(
    source_fd: int, source_name: str, destination_fd: int, destination_name: str
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "linux":
        libc.renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        libc.renameat2.restype = ctypes.c_int
        result = libc.renameat2(
            source_fd, os.fsencode(source_name), destination_fd, os.fsencode(destination_name), 1
        )
    elif sys.platform == "darwin":
        libc.renameatx_np.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        libc.renameatx_np.restype = ctypes.c_int
        result = libc.renameatx_np(
            source_fd,
            os.fsencode(source_name),
            destination_fd,
            os.fsencode(destination_name),
            0x0000_0004,
        )
    else:
        raise ResourceCompositionError(
            "Release asset publication requires atomic no-replace rename support"
        )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), source_name, destination_name)


def _candidate_module() -> ModuleType:
    return _load_module(
        "_openevo_release_candidate_for_release_assets",
        REPO_ROOT / "scripts/ci/openevo_release_candidate.py",
    )


def _validate_release_inputs(
    *,
    bundle: Path,
    manifest: Path,
    wheel: Path,
    framework_lock: Path,
    source_commit: str,
    registry_digest: str,
) -> None:
    candidate = _candidate_module()
    try:
        candidate.validate_daemon_release_inputs(
            bundle=bundle,
            manifest_path=manifest,
            wheel=wheel,
            framework_lock=framework_lock,
            source_commit=source_commit,
            registry_digest=registry_digest,
        )
    except candidate.CandidateError as exc:
        raise ResourceCompositionError(str(exc)) from exc


def _validate_runtime_archive(archive: Path) -> None:
    if archive.name != MANAGED_RUNTIME_ARCHIVE_RELEASE.filename:
        raise ResourceCompositionError(
            "Managed runtime archive does not use the fixed release filename"
        )
    try:
        verify_managed_runtime_archive(archive, release=MANAGED_RUNTIME_ARCHIVE_RELEASE)
    except (OSError, ValueError) as exc:
        raise ResourceCompositionError("Managed runtime archive identity is invalid") from exc


def _release_asset_inputs(
    *,
    wheel: Path,
    framework_lock: Path,
    bundle: Path,
    manifest: Path,
    managed_runtime_archive: Path,
) -> list[tuple[PurePosixPath, bytes, int]]:
    if framework_lock.name != FRAMEWORK_LOCK_NAME:
        raise ResourceCompositionError("Core framework lock does not use the canonical filename")
    if bundle.name != DAEMON_BUNDLE_NAME or manifest.name != DAEMON_MANIFEST_NAME:
        raise ResourceCompositionError("Daemon release inputs do not use canonical filenames")
    return [
        (
            PurePosixPath("core") / FRAMEWORK_LOCK_NAME,
            _read_controlled_file(framework_lock),
            0o644,
        ),
        (PurePosixPath("core") / wheel.name, _read_controlled_file(wheel), 0o644),
        (PurePosixPath("daemon") / DAEMON_MANIFEST_NAME, _read_controlled_file(manifest), 0o644),
        (
            PurePosixPath("daemon") / DAEMON_BUNDLE_NAME,
            _read_controlled_file(bundle, executable=True),
            0o755,
        ),
        (
            PurePosixPath("runtime") / MANAGED_RUNTIME_ARCHIVE_RELEASE.filename,
            _read_controlled_file(managed_runtime_archive),
            0o644,
        ),
    ]


def _release_assets_manifest(
    *, source_commit: str, files: list[tuple[PurePosixPath, bytes, int]]
) -> bytes:
    entries = [
        {
            "relative_path": path.as_posix(),
            "sha256": _sha256_bytes(payload),
            "byte_size": len(payload),
        }
        for path, payload, _mode in files
    ]
    if [entry["relative_path"] for entry in entries] != sorted(
        entry["relative_path"] for entry in entries
    ):
        raise ResourceCompositionError("Release asset inventory is not canonically ordered")
    return _canonical_json({"files": entries, "schema_version": 1, "source_commit": source_commit})


def _write_asset_tree(
    staging_fd: int, *, files: list[tuple[PurePosixPath, bytes, int]], manifest: bytes
) -> None:
    directories: dict[str, int] = {}
    try:
        for directory in (CORE_DIRECTORY, DAEMON_DIRECTORY, RUNTIME_DIRECTORY):
            directories[directory.as_posix()] = _mkdir_new_at(staging_fd, directory.as_posix())
        for relative_path, payload, mode in files:
            parent = relative_path.parent.as_posix()
            if parent not in directories or relative_path.name in {"", ".", ".."}:
                raise ResourceCompositionError("Release asset path is outside the closed layout")
            _write_new_at(directories[parent], relative_path.name, payload, mode=mode)
        _write_new_at(staging_fd, RELEASE_ASSETS_MANIFEST_NAME, manifest, mode=0o644)
        expected_root = sorted([RELEASE_ASSETS_MANIFEST_NAME, *directories])
        if sorted(os.listdir(staging_fd)) != expected_root:
            raise ResourceCompositionError("Release asset staging inventory is invalid")
        for relative_path, payload, mode in files:
            parent_fd = directories[relative_path.parent.as_posix()]
            staged = _read_controlled_file_at(
                parent_fd,
                relative_path.name,
                executable=mode == 0o755,
            )
            if staged != payload:
                raise ResourceCompositionError("Staged release asset differs from verified input")
        if _read_controlled_file_at(staging_fd, RELEASE_ASSETS_MANIFEST_NAME) != manifest:
            raise ResourceCompositionError(
                "Staged release asset manifest differs from its canonical bytes"
            )
        os.fsync(staging_fd)
    finally:
        for descriptor in directories.values():
            os.close(descriptor)


def stage_release_assets(
    *,
    bundle: Path,
    manifest: Path,
    wheel: Path,
    framework_lock: Path,
    managed_runtime_archive: Path,
    source_commit: str,
    registry_digest: str,
    output_dir: Path,
) -> None:
    _require_source_commit(source_commit)
    _validate_release_inputs(
        bundle=bundle,
        manifest=manifest,
        wheel=wheel,
        framework_lock=framework_lock,
        source_commit=source_commit,
        registry_digest=registry_digest,
    )
    _validate_runtime_archive(managed_runtime_archive)
    files = _release_asset_inputs(
        wheel=wheel,
        framework_lock=framework_lock,
        bundle=bundle,
        manifest=manifest,
        managed_runtime_archive=managed_runtime_archive,
    )
    manifest_payload = _release_assets_manifest(source_commit=source_commit, files=files)
    output = output_dir.absolute()
    _reject_symlink_components(output)
    if output.name != RELEASE_ASSETS_DIRECTORY.name:
        raise ResourceCompositionError(
            "Release asset output directory must use the canonical name"
        )
    if output.exists():
        raise ResourceCompositionError("Release asset output directory must not already exist")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _reject_symlink_components(output.parent)
    parent_fd = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    staging_name = f".{output.name}.staging-{secrets.token_hex(16)}"
    staging_fd = -1
    try:
        if os.path.lexists(output):
            raise ResourceCompositionError("Release asset output directory must not already exist")
        os.mkdir(staging_name, 0o700, dir_fd=parent_fd)
        staging_fd = os.open(
            staging_name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        _write_asset_tree(staging_fd, files=files, manifest=manifest_payload)
        _rename_noreplace(parent_fd, staging_name, parent_fd, output.name)
        os.fsync(parent_fd)
        published = os.stat(output.name, dir_fd=parent_fd, follow_symlinks=False)
        held = os.fstat(staging_fd)
        if not stat.S_ISDIR(published.st_mode) or (published.st_dev, published.st_ino) != (
            held.st_dev,
            held.st_ino,
        ):
            raise ResourceCompositionError("Published release asset directory identity changed")
    except OSError as exc:
        raise ResourceCompositionError(
            "Release assets could not be published atomically; the non-authoritative staging directory was preserved"
        ) from exc
    finally:
        if staging_fd >= 0:
            os.close(staging_fd)
        os.close(parent_fd)


def build_daemon_release_inputs(*, output_dir: Path, source_commit: str) -> None:
    _require_source_commit(source_commit)
    output = output_dir.absolute()
    if output.exists():
        raise ResourceCompositionError("Daemon release output directory must not already exist")
    output.mkdir(mode=0o700, parents=True)
    os.chmod(output, 0o700)
    sidecar_builder = _load_module(
        "_openevo_sidecar_builder_for_release_assets",
        REPO_ROOT / "desktop/packaging/build_sidecar.py",
    )
    daemon_builder = _load_module(
        "_openevo_daemon_builder_for_release_assets",
        REPO_ROOT / "scripts/ci/build_openevo_daemon_bundle.py",
    )
    if sidecar_builder._BUILD_SOURCE_COMMIT != source_commit:
        raise ResourceCompositionError("Checkout identity changed before Daemon composition")
    with TemporaryDirectory(prefix="openevo-desktop-core-", dir=output) as temporary:
        temporary_root = Path(temporary)
        wheel = sidecar_builder._build_core_wheel(REPO_ROOT, temporary_root / "core-build")
        _name, version = sidecar_builder._project_identity(REPO_ROOT)
        framework_lock = sidecar_builder._write_core_framework_lock(wheel, version=version)
        sidecar_builder._publish_core_release_inputs_once(output / "core", wheel, framework_lock)
    published_wheels = list((output / "core").glob("openevo-*.whl"))
    if len(published_wheels) != 1:
        raise ResourceCompositionError("Daemon composition did not publish one exact Core wheel")
    daemon_builder.build_bundle(
        wheel=published_wheels[0],
        framework_lock=output / "core/framework-lock.json",
        uv_lock=REPO_ROOT / "uv.lock",
        source_commit=source_commit,
        output_dir=output / "daemon",
    )


def _load_packaged_runtime_loader():
    repo_root = os.fspath(REPO_ROOT)
    inserted_repo_root = repo_root not in sys.path
    if inserted_repo_root:
        sys.path.insert(0, repo_root)
    try:
        runtime_module = importlib.import_module("desktop.sidecar.release_runtime")
    finally:
        if inserted_repo_root:
            sys.path.remove(repo_root)
    expected_module = (REPO_ROOT / "desktop/sidecar/release_runtime.py").resolve(strict=True)
    module_path = getattr(runtime_module, "__file__", None)
    try:
        observed_module = (
            Path(module_path).resolve(strict=True) if isinstance(module_path, str) else None
        )
    except OSError:
        observed_module = None
    if observed_module != expected_module:
        raise ResourceCompositionError(
            "Desktop runtime loader did not come from the candidate source checkout"
        )
    load_core_bootstrap_config = getattr(runtime_module, "load_core_bootstrap_config", None)
    if not callable(load_core_bootstrap_config):
        raise ResourceCompositionError("Desktop runtime loader is unavailable")
    return load_core_bootstrap_config


def _validate_packaged_runtime_loader(resource_root: Path, *, source_commit: str) -> None:
    try:
        load_core_bootstrap_config = _load_packaged_runtime_loader()

        config = load_core_bootstrap_config(
            resource_root / CORE_DIRECTORY,
            release_assets_root=resource_root,
            daemon_asset_root=resource_root / DAEMON_DIRECTORY,
            runtime_asset_root=resource_root / RUNTIME_DIRECTORY,
            source_commit=source_commit,
            packaged_resource_assets=True,
        )
    except Exception as exc:
        raise ResourceCompositionError(
            "Packaged release assets cannot be loaded by the Desktop runtime"
        ) from exc
    if config.daemon_bundle is None or config.managed_runtime_archive is None:
        raise ResourceCompositionError("Packaged Desktop runtime assets are incomplete")


def _thin_mach_o_architecture(payload: bytes) -> str:
    if len(payload) < 32 or payload[:4] != b"\xcf\xfa\xed\xfe":
        raise ResourceCompositionError(
            "Packaged SSH askpass helper is not a thin 64-bit Mach-O executable"
        )
    cpu_type = struct.unpack_from("<i", payload, 4)[0]
    try:
        return {0x0100_000C: "arm64", 0x0100_0007: "x86_64"}[cpu_type]
    except KeyError as exc:
        raise ResourceCompositionError(
            "Packaged SSH askpass helper architecture is unsupported"
        ) from exc


def _codesign_output(arguments: list[str]) -> str:
    result = subprocess.run(
        ["/usr/bin/codesign", *arguments],
        check=False,
        capture_output=True,
    )
    output = result.stdout + result.stderr
    if result.returncode != 0 or len(output) > _MAX_CODESIGN_OUTPUT_BYTES:
        raise ResourceCompositionError("Packaged SSH askpass helper signature is invalid")
    try:
        return output.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ResourceCompositionError(
            "Packaged SSH askpass helper signature evidence is invalid"
        ) from exc


def _verify_macos_adhoc_signature(executable: Path) -> str:
    _codesign_output(["--verify", "--strict", str(executable)])
    description = _codesign_output(["-d", "--verbose=4", str(executable)])
    lines = tuple(line.strip() for line in description.splitlines() if line.strip())
    flag_lines = tuple(line for line in lines if line.startswith("CodeDirectory "))
    if (
        "Signature=adhoc" not in lines
        or "TeamIdentifier=not set" not in lines
        or len(flag_lines) != 1
    ):
        raise ResourceCompositionError("Packaged SSH askpass helper is not ad-hoc signed")
    match = _MACOS_CODESIGN_FLAGS_PATTERN.search(flag_lines[0])
    flags = (
        frozenset(value.strip() for value in match.group(1).split(",") if value.strip())
        if match is not None
        else frozenset()
    )
    if flags != {"adhoc"} or any(line.startswith("Runtime Version=") for line in lines):
        raise ResourceCompositionError(
            "Packaged SSH askpass helper signature policy is not closed"
        )
    return "adhoc"


def _inspect_packaged_askpass_helper(app: Path) -> dict[str, object]:
    helper = app / MACOS_ASKPASS_HELPER_PATH
    macos_root = helper.parent
    _reject_symlink_components(macos_root)
    try:
        with os.scandir(macos_root) as entries:
            helper_names = sorted(
                entry.name
                for entry in entries
                if entry.name.startswith("openevo-ssh-askpass")
            )
    except OSError as exc:
        raise ResourceCompositionError("Packaged SSH askpass helper inventory is unavailable") from exc
    if helper_names != [helper.name]:
        raise ResourceCompositionError("App bundle does not contain the exact SSH askpass helper")

    try:
        before = os.stat(helper, follow_symlinks=False)
    except OSError as exc:
        raise ResourceCompositionError("Packaged SSH askpass helper is unavailable") from exc
    payload = _read_controlled_file(helper, executable=True)
    if stat.S_IMODE(before.st_mode) != 0o755:
        raise ResourceCompositionError("Packaged SSH askpass helper mode must be exactly 0755")
    if not 0 < len(payload) <= MAX_ASKPASS_HELPER_BYTES:
        raise ResourceCompositionError("Packaged SSH askpass helper exceeds its byte limit")
    architecture = _thin_mach_o_architecture(payload)
    signature = _verify_macos_adhoc_signature(helper)
    try:
        after = os.stat(helper, follow_symlinks=False)
    except OSError as exc:
        raise ResourceCompositionError(
            "Packaged SSH askpass helper changed during verification"
        ) from exc
    if (
        (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        or stat.S_IMODE(after.st_mode) != 0o755
    ):
        raise ResourceCompositionError("Packaged SSH askpass helper changed during verification")
    return {
        "architecture": architecture,
        "byte_size": len(payload),
        "mode": "0755",
        "relative_path": MACOS_ASKPASS_HELPER_PATH.as_posix(),
        "sha256": _sha256_bytes(payload),
        "signature": signature,
    }


def verify_app_resource(
    *,
    app: Path,
    bundle: Path,
    manifest: Path,
    wheel: Path,
    framework_lock: Path,
    managed_runtime_archive: Path,
    source_commit: str,
    source_dmg: Path,
    launch_origin: str,
    evidence_out: Path,
) -> None:
    if launch_origin not in {"mounted_dmg", "detached_copy"}:
        raise ResourceCompositionError("Release asset launch origin is invalid")
    _require_source_commit(source_commit)
    _validate_runtime_archive(managed_runtime_archive)
    files = _release_asset_inputs(
        wheel=wheel,
        framework_lock=framework_lock,
        bundle=bundle,
        manifest=manifest,
        managed_runtime_archive=managed_runtime_archive,
    )
    expected_manifest = _release_assets_manifest(source_commit=source_commit, files=files)
    resource_root = app / MACOS_RESOURCE_ROOT
    packaged_entries: list[dict[str, object]] = []
    for relative_path, payload, mode in files:
        packaged = _read_controlled_file(resource_root / relative_path, executable=mode == 0o755)
        if packaged != payload:
            raise ResourceCompositionError("Packaged release asset differs from verified input")
        packaged_entries.append(
            {
                "byte_size": len(packaged),
                "relative_path": (MACOS_RESOURCE_ROOT / relative_path).as_posix(),
                "sha256": _sha256_bytes(packaged),
            }
        )
    packaged_manifest = _read_controlled_file(resource_root / RELEASE_ASSETS_MANIFEST_NAME)
    if packaged_manifest != expected_manifest:
        raise ResourceCompositionError(
            "Packaged release asset manifest differs from verified inputs"
        )
    _validate_packaged_runtime_loader(resource_root, source_commit=source_commit)
    askpass_helper = _inspect_packaged_askpass_helper(app)
    evidence = {
        "launch_origin": launch_origin,
        "release_assets": {
            "files": packaged_entries,
            "manifest": {
                "byte_size": len(packaged_manifest),
                "relative_path": (MACOS_RESOURCE_ROOT / RELEASE_ASSETS_MANIFEST_NAME).as_posix(),
                "sha256": _sha256_bytes(packaged_manifest),
            },
        },
        "schema_version": 3,
        "ssh_askpass_helper": askpass_helper,
        "source_dmg": {
            "filename": source_dmg.name,
            "sha256": _sha256_bytes(_read_controlled_file(source_dmg)),
        },
    }
    evidence_path = evidence_out.absolute()
    evidence_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if evidence_path.exists():
        raise ResourceCompositionError("Refusing to replace release asset evidence")
    parent_fd = os.open(
        evidence_path.parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        _write_new_at(parent_fd, evidence_path.name, _canonical_json(evidence), mode=0o600)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--output-dir", type=Path, required=True)
    build.add_argument("--source-commit", required=True)
    stage = commands.add_parser("stage")
    for option in ("bundle", "manifest", "wheel", "framework-lock", "managed-runtime-archive"):
        stage.add_argument(f"--{option}", type=Path, required=True)
    stage.add_argument("--source-commit", required=True)
    stage.add_argument("--registry-digest", required=True)
    stage.add_argument("--output-dir", type=Path, required=True)
    verify = commands.add_parser("verify-app")
    for option in (
        "app",
        "bundle",
        "manifest",
        "wheel",
        "framework-lock",
        "managed-runtime-archive",
        "source-dmg",
    ):
        verify.add_argument(f"--{option}", type=Path, required=True)
    verify.add_argument("--source-commit", required=True)
    verify.add_argument("--launch-origin", choices=("mounted_dmg", "detached_copy"), required=True)
    verify.add_argument("--evidence-out", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "build":
        build_daemon_release_inputs(output_dir=args.output_dir, source_commit=args.source_commit)
    elif args.command == "stage":
        stage_release_assets(
            bundle=args.bundle,
            manifest=args.manifest,
            wheel=args.wheel,
            framework_lock=args.framework_lock,
            managed_runtime_archive=args.managed_runtime_archive,
            source_commit=args.source_commit,
            registry_digest=args.registry_digest,
            output_dir=args.output_dir,
        )
    else:
        verify_app_resource(
            app=args.app,
            bundle=args.bundle,
            manifest=args.manifest,
            wheel=args.wheel,
            framework_lock=args.framework_lock,
            managed_runtime_archive=args.managed_runtime_archive,
            source_commit=args.source_commit,
            source_dmg=args.source_dmg,
            launch_origin=args.launch_origin,
            evidence_out=args.evidence_out,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
