#!/usr/bin/env python3
"""Build the bundled OpenEvo Desktop sidecar executable for Tauri."""

from __future__ import annotations

import argparse
from email.parser import Parser
import hashlib
from io import BytesIO
from importlib.metadata import version as distribution_version
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import tomllib
from urllib.request import urlopen
from zipfile import BadZipFile, ZipFile

from openevo.evolution.framework.runtime import (
    FrameworkDistributionLock,
    load_framework_distribution_lock,
)

SIDECAR_NAME = "openevo-desktop-sidecar"
CORE_WHEEL_ARCHIVE_ROOT = Path("openevo/wheels")
CORE_FRAMEWORK_LOCK_BASENAME = "framework-lock.json"
FORBIDDEN_LEGACY_CORE_MODULE_FILES = frozenset(
    {
        "openevo/evolution/terminal_bench_bridge.py",
        "openevo/evolution/terminal_bench_local_parametric.py",
        "openevo/evolution/terminal_bench_per_task.py",
        "openevo/evolution/terminal_bench_task_local_parametric.py",
    }
)
FORBIDDEN_LEGACY_SIDECAR_MODULES = frozenset(
    path.removesuffix(".py").replace("/", ".") for path in FORBIDDEN_LEGACY_CORE_MODULE_FILES
)
PRODUCT_WEB_MANIFEST = ".openevo-product-web.json"
SIDECAR_BUILD_METADATA_RELATIVE_PATH = Path("desktop/packaging/sidecar-build-metadata.json")
_SOURCE_COMMIT_PATTERN = re.compile(r"[0-9a-f]{7,40}")


def _validate_core_inventory(names: set[str], *, container: str) -> None:
    benchmark_members = sorted(
        member
        for member in names
        if member.startswith(("openevo_terminal_bench/", "benchmarks/terminal_bench/"))
    )
    if benchmark_members:
        raise RuntimeError(
            f"{container} must not contain Terminal Bench automation: {benchmark_members}"
        )
    legacy_modules = sorted(names & FORBIDDEN_LEGACY_CORE_MODULE_FILES)
    if legacy_modules:
        raise RuntimeError(
            f"{container} must not contain removed Terminal Bench Core modules: {legacy_modules}"
        )


def _product_web_files(root: Path) -> dict[str, bytes]:
    if not root.is_dir():
        raise RuntimeError(f"Desktop product web root is missing: {root}")
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RuntimeError(f"Desktop product web must not contain symlinks: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise RuntimeError(f"Desktop product web contains a non-regular entry: {path}")
        files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def _product_web_forbidden_text(desktop_root: Path) -> tuple[str, ...]:
    policy_path = desktop_root / "packaging/product-web-policy.json"
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Desktop product web audit policy is unreadable") from exc
    forbidden = policy.get("forbidden_text") if isinstance(policy, dict) else None
    if (
        not isinstance(policy, dict)
        or policy.get("schema_version") != "1"
        or not isinstance(forbidden, list)
        or not forbidden
    ):
        raise RuntimeError("Desktop product web audit policy is invalid")
    if not all(isinstance(value, str) and value for value in forbidden):
        raise RuntimeError("Desktop product web audit policy contains invalid text")
    return tuple(value.lower() for value in forbidden)


def _audit_product_web_bytes(
    files: dict[str, bytes],
    *,
    forbidden: tuple[str, ...],
    container: str,
) -> None:
    if "index.html" not in files:
        raise RuntimeError(f"{container} is missing index.html")
    for name, payload in files.items():
        if Path(name).suffix.lower() not in {".css", ".html", ".js", ".json", ".map", ".txt"}:
            continue
        try:
            text = payload.decode("utf-8").lower()
        except UnicodeDecodeError as exc:
            raise RuntimeError(f"{container} contains non-UTF-8 static text: {name}") from exc
        for value in forbidden:
            if value in text:
                raise RuntimeError(
                    f"{container} contains forbidden product text in {name}: {value}"
                )


def _validate_product_web_manifest(files: dict[str, bytes], *, container: str) -> str:
    try:
        manifest = json.loads(files[PRODUCT_WEB_MANIFEST].decode("utf-8"))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{container} has no readable product build manifest") from exc
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version",
        "build_digest",
        "files",
    }:
        raise RuntimeError(f"{container} product build manifest is not closed")
    entries = manifest.get("files")
    if manifest.get("schema_version") != "1" or not isinstance(entries, list):
        raise RuntimeError(f"{container} product build manifest is invalid")
    expected_names = sorted(name for name in files if name != PRODUCT_WEB_MANIFEST)
    if [
        entry.get("path") if isinstance(entry, dict) else None for entry in entries
    ] != expected_names:
        raise RuntimeError(f"{container} product build manifest inventory differs from its files")
    canonical_entries: list[dict[str, object]] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "byte_size"}:
            raise RuntimeError(f"{container} product build manifest entry is invalid")
        name = entry["path"]
        if not isinstance(name, str) or name not in files:
            raise RuntimeError(f"{container} product build manifest path is invalid")
        payload = files[name]
        expected = {"path": name, "sha256": _sha256_bytes(payload), "byte_size": len(payload)}
        if entry != expected:
            raise RuntimeError(f"{container} product build manifest digest differs for {name}")
        canonical_entries.append(expected)
    build_digest = _sha256_bytes(
        json.dumps(canonical_entries, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    if manifest.get("build_digest") != build_digest:
        raise RuntimeError(f"{container} product build digest is invalid")
    return build_digest


def _validate_product_web_build(desktop_root: Path) -> str:
    dist_files = _product_web_files(desktop_root / "dist")
    packaged_files = _product_web_files(desktop_root / "packaging/web")
    forbidden = _product_web_forbidden_text(desktop_root)
    _audit_product_web_bytes(dist_files, forbidden=forbidden, container="Desktop dist")
    _audit_product_web_bytes(packaged_files, forbidden=forbidden, container="Desktop packaged web")
    dist_digest = _validate_product_web_manifest(dist_files, container="Desktop dist")
    packaged_digest = _validate_product_web_manifest(
        packaged_files, container="Desktop packaged web"
    )
    if dist_files != packaged_files or dist_digest != packaged_digest:
        raise RuntimeError("Desktop packaged web does not exactly match the audited product build")
    return dist_digest


def _build_product_web(desktop_root: Path) -> str:
    subprocess.run(["npm", "run", "build:openevo"], check=True, cwd=desktop_root)
    return _validate_product_web_build(desktop_root)


NATIVE_LISTENER_FD_ENV = "OPENEVO_NATIVE_LISTENER_FD"
NATIVE_LISTENER_FD = "3"
NATIVE_EXECUTABLE_FD_ENV = "OPENEVO_NATIVE_EXECUTABLE_FD"
NATIVE_EXECUTABLE_FD = "4"
NATIVE_EXECUTABLE_PATH_ENV = "OPENEVO_NATIVE_EXECUTABLE_PATH"
NATIVE_EXECUTABLE_BASENAME = "openevo-desktop-sidecar"
_MAX_PYINSTALLER_SDIST_BYTES = 16 * 1024 * 1024
_MAX_PYINSTALLER_SOURCE_BYTES = 32 * 1024 * 1024
_MAX_PYINSTALLER_SOURCE_MEMBERS = 5_000
_BOOTLOADER_MACOS_INCLUDE_NEEDLE = """#if defined(__APPLE__)
    #include <mach-o/dyld.h>  /* _NSGetExecutablePath() */
#endif
"""
_BOOTLOADER_MACOS_INCLUDE_REPLACEMENT = """#if defined(__APPLE__)
    #include <mach-o/dyld.h>  /* _NSGetExecutablePath() */
    #include <sys/stat.h>  /* fstat(), lstat(), struct stat */
#endif
"""
_BOOTLOADER_RESOLVER_NEEDLE = """static int
_pyi_main_resolve_executable(struct PYI_CONTEXT *pyi_ctx)
{
    /* Resolve using OS-specific implementation */
"""
_BOOTLOADER_RESOLVER_REPLACEMENT = f"""static int
_pyi_main_resolve_executable(struct PYI_CONTEXT *pyi_ctx)
{{
    /* Preserve the native listener and archive across onefile parent extraction. */
    const char *openevo_native_listener_fd = getenv(\"{NATIVE_LISTENER_FD_ENV}\");
    const char *openevo_native_fd = getenv(\"{NATIVE_EXECUTABLE_FD_ENV}\");
    const char *openevo_native_path = getenv(\"{NATIVE_EXECUTABLE_PATH_ENV}\");
    if (openevo_native_fd != NULL) {{
        if (
            openevo_native_listener_fd == NULL ||
            strcmp(openevo_native_listener_fd, \"{NATIVE_LISTENER_FD}\") != 0 ||
            strcmp(openevo_native_fd, \"{NATIVE_EXECUTABLE_FD}\") != 0
        ) {{
            return -1;
        }}
#if defined(__linux__)
        if (openevo_native_path != NULL) {{
            return -1;
        }}
        snprintf(pyi_ctx->executable_filename, PYI_PATH_MAX, \"/proc/self/fd/{NATIVE_EXECUTABLE_FD}\");
#elif defined(__APPLE__)
        struct stat openevo_fd_stat;
        struct stat openevo_path_stat;
        struct stat openevo_resolved_stat;
        char openevo_resolved_path[PYI_PATH_MAX];
        const char *openevo_basename;
        size_t openevo_path_length;
        size_t openevo_index;

        if (openevo_native_path == NULL || openevo_native_path[0] != '/') {{
            return -1;
        }}
        openevo_path_length = strnlen(openevo_native_path, PYI_PATH_MAX);
        if (openevo_path_length == 0 || openevo_path_length >= PYI_PATH_MAX) {{
            return -1;
        }}
        if (
            strstr(openevo_native_path, \"//\") != NULL ||
            strstr(openevo_native_path, \"/./\") != NULL ||
            strstr(openevo_native_path, \"/../\") != NULL
        ) {{
            return -1;
        }}
        openevo_basename = strrchr(openevo_native_path, '/');
        if (openevo_basename == NULL || strcmp(openevo_basename + 1, \"{NATIVE_EXECUTABLE_BASENAME}\") != 0) {{
            return -1;
        }}
        for (openevo_index = 0; openevo_index < openevo_path_length; openevo_index++) {{
            unsigned char character = (unsigned char)openevo_native_path[openevo_index];
            if (character < 0x20 || character == 0x7f) {{
                return -1;
            }}
        }}
        if (realpath(openevo_native_path, openevo_resolved_path) == NULL) {{
            return -1;
        }}
        if (
            fstat({NATIVE_EXECUTABLE_FD}, &openevo_fd_stat) != 0 ||
            lstat(openevo_native_path, &openevo_path_stat) != 0 ||
            lstat(openevo_resolved_path, &openevo_resolved_stat) != 0 ||
            !S_ISREG(openevo_fd_stat.st_mode) ||
            !S_ISREG(openevo_path_stat.st_mode) ||
            !S_ISREG(openevo_resolved_stat.st_mode) ||
            openevo_fd_stat.st_dev != openevo_path_stat.st_dev ||
            openevo_fd_stat.st_ino != openevo_path_stat.st_ino ||
            openevo_fd_stat.st_dev != openevo_resolved_stat.st_dev ||
            openevo_fd_stat.st_ino != openevo_resolved_stat.st_ino ||
            openevo_fd_stat.st_size != openevo_path_stat.st_size ||
            openevo_fd_stat.st_uid != openevo_path_stat.st_uid ||
            openevo_fd_stat.st_mode != openevo_path_stat.st_mode ||
            openevo_path_stat.st_nlink != 1 ||
            openevo_resolved_stat.st_nlink != 1 ||
            (openevo_path_stat.st_mode & (S_IWGRP | S_IWOTH)) != 0 ||
            (openevo_path_stat.st_uid != 0 && openevo_path_stat.st_uid != geteuid())
        ) {{
            return -1;
        }}
        if (snprintf(pyi_ctx->executable_filename, PYI_PATH_MAX, \"%s\", openevo_resolved_path) >= PYI_PATH_MAX) {{
            return -1;
        }}
#else
        return -1;
#endif
        if (pyi_utils_openevo_native_handoff_prepare() != 0) {{
            return -1;
        }}
        return 0;
    }}
    if (openevo_native_listener_fd != NULL || openevo_native_path != NULL) {{
        return -1;
    }}

    /* Resolve using OS-specific implementation */
"""
_BOOTLOADER_ARCHIVE_NEEDLE = """static int
_pyi_main_resolve_pkg_archive(struct PYI_CONTEXT *pyi_ctx)
{
    int status;

    /* Try opening embedded archive first */
"""
_BOOTLOADER_ARCHIVE_REPLACEMENT = f"""static int
_pyi_main_resolve_pkg_archive(struct PYI_CONTEXT *pyi_ctx)
{{
    int status;
    const char *openevo_native_fd = getenv(\"{NATIVE_EXECUTABLE_FD_ENV}\");

    if (openevo_native_fd != NULL) {{
        const char *openevo_archive_path;

        if (strcmp(openevo_native_fd, \"{NATIVE_EXECUTABLE_FD}\") != 0) {{
            return -1;
        }}
#if defined(__linux__)
        openevo_archive_path = \"/proc/self/fd/{NATIVE_EXECUTABLE_FD}\";
#elif defined(__APPLE__)
        openevo_archive_path = \"/dev/fd/{NATIVE_EXECUTABLE_FD}\";
#else
        return -1;
#endif
        pyi_ctx->archive = pyi_archive_open(openevo_archive_path);
        if (pyi_ctx->archive == NULL) {{
            return -1;
        }}
        snprintf(pyi_ctx->archive_filename, PYI_PATH_MAX, \"%s\", openevo_archive_path);
        return 0;
    }}

    /* Try opening embedded archive first */
"""
_BOOTLOADER_POSIX_INCLUDE_NEEDLE = """#include <errno.h>
#include <signal.h> /* kill */
#include <sys/stat.h> /* struct stat */
#include <sys/wait.h>
"""
_BOOTLOADER_POSIX_INCLUDE_REPLACEMENT = """#include <errno.h>
#include <fcntl.h> /* fcntl, F_DUPFD, FD_CLOEXEC */
#include <signal.h> /* kill */
#include <sys/socket.h> /* getsockopt, SOL_SOCKET, SO_ACCEPTCONN */
#include <sys/stat.h> /* struct stat */
#include <sys/wait.h>
"""
_BOOTLOADER_NATIVE_HANDOFF_NEEDLE = """/*
 * If the program is activated by a systemd socket, systemd will set
"""
_BOOTLOADER_NATIVE_HANDOFF_REPLACEMENT = """/* OpenEvo native onefile FD handoff. */
#define OPENEVO_NATIVE_LISTENER_FD 3
#define OPENEVO_NATIVE_ARCHIVE_FD 4
#define OPENEVO_NATIVE_GUARD_MIN_FD 64

static int openevo_listener_guard_fd = -1;
static int openevo_archive_guard_fd = -1;

static int
_pyi_utils_openevo_validate_native_fds(void)
{
    struct stat listener_stat;
    struct stat archive_stat;
    int accepting = 0;
    socklen_t accepting_size = sizeof(accepting);

    if (
        fstat(OPENEVO_NATIVE_LISTENER_FD, &listener_stat) != 0 ||
        fstat(OPENEVO_NATIVE_ARCHIVE_FD, &archive_stat) != 0 ||
        !S_ISSOCK(listener_stat.st_mode) ||
        !S_ISREG(archive_stat.st_mode) ||
        getsockopt(
            OPENEVO_NATIVE_LISTENER_FD,
            SOL_SOCKET,
            SO_ACCEPTCONN,
            &accepting,
            &accepting_size
        ) != 0 ||
        accepting_size != sizeof(accepting) ||
        accepting != 1
    ) {
        return -1;
    }
    return 0;
}

static int
_pyi_utils_openevo_clear_cloexec(int fd)
{
    int flags = fcntl(fd, F_GETFD);
    if (flags == -1 || fcntl(fd, F_SETFD, flags & ~FD_CLOEXEC) == -1) {
        return -1;
    }
    return 0;
}

int
pyi_utils_openevo_native_handoff_prepare(void)
{
    if (openevo_listener_guard_fd != -1 || openevo_archive_guard_fd != -1) {
        return -1;
    }
    if (_pyi_utils_openevo_validate_native_fds() != 0) {
        return -1;
    }
    openevo_listener_guard_fd = fcntl(
        OPENEVO_NATIVE_LISTENER_FD,
        F_DUPFD,
        OPENEVO_NATIVE_GUARD_MIN_FD
    );
    if (openevo_listener_guard_fd == -1) {
        return -1;
    }
    openevo_archive_guard_fd = fcntl(
        OPENEVO_NATIVE_ARCHIVE_FD,
        F_DUPFD,
        OPENEVO_NATIVE_GUARD_MIN_FD
    );
    if (openevo_archive_guard_fd == -1) {
        close(openevo_listener_guard_fd);
        openevo_listener_guard_fd = -1;
        return -1;
    }
    return 0;
}

int
pyi_utils_openevo_native_handoff_restore(void)
{
    if (openevo_listener_guard_fd == -1 && openevo_archive_guard_fd == -1) {
        return 0;
    }
    if (
        openevo_listener_guard_fd == -1 ||
        openevo_archive_guard_fd == -1 ||
        dup2(openevo_listener_guard_fd, OPENEVO_NATIVE_LISTENER_FD) == -1 ||
        dup2(openevo_archive_guard_fd, OPENEVO_NATIVE_ARCHIVE_FD) == -1 ||
        _pyi_utils_openevo_clear_cloexec(OPENEVO_NATIVE_LISTENER_FD) != 0 ||
        _pyi_utils_openevo_clear_cloexec(OPENEVO_NATIVE_ARCHIVE_FD) != 0 ||
        _pyi_utils_openevo_validate_native_fds() != 0
    ) {
        return -1;
    }
    return 0;
}

int
pyi_utils_openevo_native_handoff_finish(void)
{
    if (pyi_utils_openevo_native_handoff_restore() != 0) {
        return -1;
    }
    if (openevo_listener_guard_fd != -1) {
        close(openevo_listener_guard_fd);
        openevo_listener_guard_fd = -1;
    }
    if (openevo_archive_guard_fd != -1) {
        close(openevo_archive_guard_fd);
        openevo_archive_guard_fd = -1;
    }
    return 0;
}

/*
 * If the program is activated by a systemd socket, systemd will set
"""
_BOOTLOADER_CHILD_EXEC_NEEDLE = """        /* Modify the LISTEN_PID environment variable, if necessary */
"""
_BOOTLOADER_CHILD_EXEC_REPLACEMENT = """        if (pyi_utils_openevo_native_handoff_restore() != 0) {
            PYI_ERROR("LOADER: failed to restore OpenEvo native descriptors!\\n");
            exit(-1);
        }

        /* Modify the LISTEN_PID environment variable, if necessary */
"""
_BOOTLOADER_UTILS_HEADER_NEEDLE = """/* Child process */
int pyi_utils_create_child(struct PYI_CONTEXT *pyi_ctx);
"""
_BOOTLOADER_UTILS_HEADER_REPLACEMENT = """/* Child process */
int pyi_utils_create_child(struct PYI_CONTEXT *pyi_ctx);
int pyi_utils_openevo_native_handoff_prepare(void);
int pyi_utils_openevo_native_handoff_restore(void);
int pyi_utils_openevo_native_handoff_finish(void);
"""
_BOOTLOADER_RESTART_NEEDLE = """        if (needs_restart) {
            PYI_DEBUG("LOADER: process needs to restart itself to apply modifications to library search path.\\n");
"""
_BOOTLOADER_RESTART_REPLACEMENT = """        if (needs_restart) {
            PYI_DEBUG("LOADER: process needs to restart itself to apply modifications to library search path.\\n");
            if (pyi_utils_openevo_native_handoff_restore() != 0) {
                PYI_ERROR("LOADER: failed to restore OpenEvo native descriptors for restart!\\n");
                return -1;
            }
"""
_BOOTLOADER_CHILD_MAIN_NEEDLE = """_pyi_main_onedir_or_onefile_child(struct PYI_CONTEXT *pyi_ctx)
{
    int ret;
"""
_BOOTLOADER_CHILD_MAIN_REPLACEMENT = """_pyi_main_onedir_or_onefile_child(struct PYI_CONTEXT *pyi_ctx)
{
    int ret;

    if (pyi_utils_openevo_native_handoff_finish() != 0) {
        PYI_ERROR("LOADER: failed to finalize OpenEvo native descriptor handoff!\\n");
        return -1;
    }
"""


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _trusted_source_commit(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD^{commit}"],
        check=True,
        capture_output=True,
        text=True,
    )
    source_commit = result.stdout.strip()
    if _SOURCE_COMMIT_PATTERN.fullmatch(source_commit) is None or set(source_commit) == {"0"}:
        raise RuntimeError("Git returned an invalid source commit for the Desktop sidecar")
    return source_commit


_BUILD_SOURCE_COMMIT = _trusted_source_commit(_repo_root())


def _write_sidecar_build_metadata(path: Path, *, source_commit: str) -> None:
    if _SOURCE_COMMIT_PATTERN.fullmatch(source_commit) is None or set(source_commit) == {"0"}:
        raise RuntimeError("Desktop sidecar source commit is invalid")
    path.write_text(
        json.dumps(
            {"schema_version": "1", "source_commit": source_commit},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )


def _target_triple() -> str:
    try:
        result = subprocess.run(
            ["rustc", "--print", "host-tuple"],
            check=True,
            capture_output=True,
            text=True,
        )
        triple = result.stdout.strip()
        if triple:
            return triple
    except subprocess.CalledProcessError:
        pass

    result = subprocess.run(
        ["rustc", "-Vv"],
        check=True,
        capture_output=True,
        text=True,
    )
    for line in result.stdout.splitlines():
        if line.startswith("host:"):
            return line.split(":", 1)[1].strip()
    raise RuntimeError("failed to determine Rust host target triple")


def _platform_extension() -> str:
    return ".exe" if os.name == "nt" else ""


def _normalized_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _project_identity(repo: Path) -> tuple[str, str]:
    payload = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    project = payload.get("project")
    if not isinstance(project, dict):
        raise RuntimeError("pyproject.toml does not define [project]")
    name = project.get("name")
    version = project.get("version")
    if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
        raise RuntimeError("pyproject.toml project name and version must be non-empty strings")
    return name, version


def _validate_core_wheel(wheel: Path, *, name: str, version: str) -> None:
    try:
        with ZipFile(wheel) as archive:
            names = set(archive.namelist())
            _validate_core_inventory(names, container="Core wheel")
            nested_wheels = [member for member in names if member.endswith(".whl")]
            if nested_wheels:
                raise RuntimeError(f"Core wheel must not contain nested wheels: {nested_wheels}")
            metadata_names = [member for member in names if member.endswith(".dist-info/METADATA")]
            if len(metadata_names) != 1:
                raise RuntimeError(
                    f"Core wheel must contain one METADATA file, found {len(metadata_names)}"
                )
            metadata = Parser().parsestr(archive.read(metadata_names[0]).decode("utf-8"))
    except OSError as exc:
        raise RuntimeError(f"failed to read built Core wheel: {wheel}") from exc

    actual_name = metadata.get("Name")
    actual_version = metadata.get("Version")
    if (
        not isinstance(actual_name, str)
        or _normalized_distribution_name(actual_name) != _normalized_distribution_name(name)
        or actual_version != version
    ):
        raise RuntimeError(
            "built Core wheel identity does not match pyproject.toml: "
            f"expected {name}=={version}, got {actual_name}=={actual_version}"
        )


def _core_framework_lock_bytes(wheel: Path, *, version: str) -> bytes:
    try:
        wheel_payload = wheel.read_bytes()
        lock = FrameworkDistributionLock(
            distribution_version=version,
            distribution_digest=_sha256_bytes(wheel_payload),
            wheel_filename=wheel.name,
        )
    except (OSError, ValueError) as exc:
        raise RuntimeError("Core wheel cannot produce a valid framework lock identity") from exc
    return (
        json.dumps(lock.model_dump(mode="json"), separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def _validated_framework_lock(payload: bytes) -> FrameworkDistributionLock:
    try:
        lock = FrameworkDistributionLock.model_validate_json(payload)
    except ValueError as exc:
        raise RuntimeError("Core framework lock is invalid") from exc
    canonical = (
        json.dumps(lock.model_dump(mode="json"), separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")
    if payload != canonical:
        raise RuntimeError("Core framework lock is not canonical")
    return lock


def _load_exact_framework_lock(
    framework_lock: Path,
    wheel: Path,
    *,
    version: str,
) -> FrameworkDistributionLock:
    try:
        lock, locked_wheel = load_framework_distribution_lock(framework_lock)
        payload = framework_lock.read_bytes()
        resolved_wheel = wheel.resolve(strict=True)
        resolved_locked_wheel = locked_wheel.resolve(strict=True)
        wheel_digest = _sha256_bytes(resolved_wheel.read_bytes())
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError("Core framework lock cannot be loaded") from exc
    canonical_lock = _validated_framework_lock(payload)
    if lock != canonical_lock:
        raise RuntimeError("Core framework lock loader returned a different identity")
    if (
        resolved_locked_wheel != resolved_wheel
        or lock.distribution != "openevo"
        or lock.distribution_version != version
        or lock.distribution_digest != wheel_digest
        or lock.wheel_filename != wheel.name
    ):
        raise RuntimeError("Core framework lock does not bind the exact built wheel")
    return lock


def _write_core_framework_lock(wheel: Path, *, version: str) -> Path:
    framework_lock = wheel.parent / CORE_FRAMEWORK_LOCK_BASENAME
    payload = _core_framework_lock_bytes(wheel, version=version)
    try:
        with framework_lock.open("xb") as stream:
            stream.write(payload)
    except FileExistsError as exc:
        raise RuntimeError("refusing to replace an existing Core framework lock") from exc
    _load_exact_framework_lock(framework_lock, wheel, version=version)
    return framework_lock


def _copy_core_build_source(repo: Path, destination: Path) -> None:
    destination.mkdir()
    for filename in ("pyproject.toml", "README.md", "LICENSE"):
        source = repo / filename
        if not source.is_file():
            raise RuntimeError(f"Core build source is missing required file: {source}")
        shutil.copy2(source, destination / filename)
    shutil.copytree(
        repo / "src",
        destination / "src",
        ignore=shutil.ignore_patterns(
            "wheels",
            "*.egg-info",
            "__pycache__",
            ".pytest_cache",
            ".ruff_cache",
            ".mypy_cache",
            ".cache",
            "*.pyc",
            "*.pyo",
        ),
    )


def _build_core_wheel(repo: Path, build_root: Path) -> Path:
    build_root.mkdir(parents=True)
    source_root = build_root / "source"
    output_dir = build_root / "wheel-dist"
    _copy_core_build_source(repo, source_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(output_dir),
        ],
        check=True,
        cwd=source_root,
    )
    wheels = sorted(output_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(
            f"Core build must produce exactly one wheel, found {len(wheels)} in {output_dir}"
        )
    name, version = _project_identity(repo)
    _validate_core_wheel(wheels[0], name=name, version=version)
    return wheels[0]


def _raw_carchive_member_names(archive: object, executable: Path) -> tuple[str, ...]:
    try:
        cookie_length = archive._COOKIE_LENGTH
        cookie_format = archive._COOKIE_FORMAT
        toc_entry_length = archive._TOC_ENTRY_LENGTH
        toc_entry_format = archive._TOC_ENTRY_FORMAT
        with executable.open("rb") as stream:
            cookie_offset = archive._find_magic_pattern(
                stream,
                archive._COOKIE_MAGIC_PATTERN,
            )
            if cookie_offset < 0:
                raise RuntimeError("sidecar archive cookie is unavailable")
            stream.seek(cookie_offset)
            cookie = stream.read(cookie_length)
            if len(cookie) != cookie_length:
                raise RuntimeError("sidecar archive cookie is truncated")
            _, archive_length, toc_offset, toc_length, _, _ = struct.unpack(
                cookie_format,
                cookie,
            )
            archive_end = cookie_offset + cookie_length
            archive_start = archive_end - archive_length
            toc_start = archive_start + toc_offset
            if (
                archive_start < 0
                or toc_start < archive_start
                or toc_length < 0
                or toc_start + toc_length > cookie_offset
            ):
                raise RuntimeError("sidecar archive TOC bounds are invalid")
            stream.seek(toc_start)
            toc_payload = stream.read(toc_length)
            if len(toc_payload) != toc_length:
                raise RuntimeError("sidecar archive TOC is truncated")

        names: list[str] = []
        position = 0
        while position < len(toc_payload):
            if len(toc_payload) - position < toc_entry_length:
                raise RuntimeError("sidecar archive TOC entry is truncated")
            entry_length, _, _, _, _, typecode = struct.unpack(
                toc_entry_format,
                toc_payload[position : position + toc_entry_length],
            )
            if entry_length < toc_entry_length or position + entry_length > len(toc_payload):
                raise RuntimeError("sidecar archive TOC entry bounds are invalid")
            encoded_name = toc_payload[
                position + toc_entry_length : position + entry_length
            ]
            name = encoded_name.rstrip(b"\0").decode("utf-8", errors="strict")
            if typecode.decode("ascii", errors="strict") != "o":
                names.append(name.replace("\\", "/"))
            position += entry_length
    except (AttributeError, OSError, struct.error, UnicodeError) as exc:
        raise RuntimeError("sidecar archive TOC cannot be verified") from exc

    if len(names) != len(set(names)):
        raise RuntimeError("sidecar archive TOC contains duplicate members")
    parsed_names = {str(name).replace("\\", "/") for name in archive.toc}
    if set(names) != parsed_names:
        raise RuntimeError("sidecar archive TOC parser inventory differs from raw bytes")
    return tuple(names)


def _archive_member_names(executable: Path) -> tuple[str, ...]:
    try:
        from PyInstaller.archive.readers import CArchiveReader, NotAnArchiveError
    except ImportError as exc:
        raise RuntimeError("PyInstaller is required to inspect the sidecar archive") from exc
    archive = CArchiveReader(str(executable))
    names = list(_raw_carchive_member_names(archive, executable))
    for member_name in archive.toc:
        try:
            embedded = archive.open_embedded_archive(member_name)
        except NotAnArchiveError:
            continue
        names.extend(str(name).replace("\\", "/") for name in embedded.toc)
    return tuple(sorted(names))


def _archive_member_bytes(executable: Path, member_name: str) -> bytes:
    try:
        from PyInstaller.archive.readers import CArchiveReader
    except ImportError as exc:
        raise RuntimeError("PyInstaller is required to inspect the sidecar archive") from exc
    payload = CArchiveReader(str(executable)).extract(member_name)
    if not isinstance(payload, bytes):
        raise RuntimeError(f"sidecar archive member is not byte data: {member_name}")
    return payload


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _prepare_core_wheel_output_dir(output_dir: Path) -> None:
    if output_dir.is_symlink():
        raise RuntimeError(f"Core wheel output must not be a symbolic link: {output_dir}")
    if output_dir.exists() and not output_dir.is_dir():
        raise RuntimeError(f"Core wheel output is not a directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    existing = list(output_dir.iterdir())
    if existing:
        raise RuntimeError(f"Core wheel output directory must be empty: {output_dir}")


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _reject_symlink_path(path: Path) -> None:
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise RuntimeError(
                f"Core wheel output path must not contain a symbolic link: {candidate}"
            )


def _copy_exclusive(source: Path, destination: Path) -> None:
    try:
        with source.open("rb") as source_file, destination.open("xb") as destination_file:
            shutil.copyfileobj(source_file, destination_file)
    except FileExistsError as exc:
        raise RuntimeError(f"refusing to replace existing Core wheel: {destination}") from exc
    shutil.copystat(source, destination)


def _locked_pyinstaller_sdist(repo: Path) -> tuple[str, str, str, int]:
    payload = tomllib.loads((repo / "uv.lock").read_text(encoding="utf-8"))
    packages = [
        package
        for package in payload.get("package", [])
        if isinstance(package, dict) and package.get("name") == "pyinstaller"
    ]
    if len(packages) != 1:
        raise RuntimeError("uv.lock must contain exactly one PyInstaller package")
    package = packages[0]
    version = package.get("version")
    sdist = package.get("sdist")
    if not isinstance(version, str) or not isinstance(sdist, dict):
        raise RuntimeError("uv.lock has an invalid PyInstaller source lock")
    url = sdist.get("url")
    encoded_hash = sdist.get("hash")
    size = sdist.get("size")
    if (
        not isinstance(url, str)
        or not url.startswith("https://files.pythonhosted.org/")
        or not isinstance(encoded_hash, str)
        or not encoded_hash.startswith("sha256:")
        or re.fullmatch(r"[0-9a-f]{64}", encoded_hash[7:]) is None
        or type(size) is not int
        or size <= 0
        or size > _MAX_PYINSTALLER_SDIST_BYTES
    ):
        raise RuntimeError("uv.lock has an unsafe PyInstaller source lock")
    if distribution_version("pyinstaller") != version:
        raise RuntimeError("installed PyInstaller does not match uv.lock")
    return version, url, encoded_hash[7:], size


def _download_locked_file(
    url: str,
    destination: Path,
    *,
    expected_digest: str,
    expected_size: int,
) -> None:
    hasher = hashlib.sha256()
    received = 0
    try:
        with urlopen(url, timeout=30) as response, destination.open("xb") as output:
            while chunk := response.read(1024 * 1024):
                received += len(chunk)
                if received > expected_size:
                    raise RuntimeError("PyInstaller sdist exceeded its locked size")
                hasher.update(chunk)
                output.write(chunk)
    except OSError as exc:
        raise RuntimeError("failed to download locked PyInstaller sdist") from exc
    if received != expected_size or hasher.hexdigest() != expected_digest:
        raise RuntimeError("PyInstaller sdist does not match its locked identity")


def _extract_locked_pyinstaller_sdist(
    archive_path: Path,
    destination: Path,
    *,
    version: str,
) -> Path:
    expected_root = f"pyinstaller-{version}"
    extracted_bytes = 0
    with tarfile.open(archive_path, mode="r:gz") as archive:
        members = archive.getmembers()
        if len(members) > _MAX_PYINSTALLER_SOURCE_MEMBERS:
            raise RuntimeError("PyInstaller sdist contains too many members")
        for member in members:
            relative = PurePosixPath(member.name)
            if (
                relative.is_absolute()
                or not relative.parts
                or relative.parts[0] != expected_root
                or any(part in {"", ".", ".."} for part in relative.parts)
            ):
                raise RuntimeError("PyInstaller sdist contains an unsafe path")
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if member.issym():
                continue
            if not member.isfile() or member.size < 0:
                raise RuntimeError("PyInstaller sdist contains an unsafe member")
            extracted_bytes += member.size
            if extracted_bytes > _MAX_PYINSTALLER_SOURCE_BYTES:
                raise RuntimeError("PyInstaller sdist exceeded its extraction budget")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError("PyInstaller sdist member could not be read")
            try:
                with target.open("xb") as output:
                    shutil.copyfileobj(source, output)
            finally:
                source.close()
            if target.stat().st_size != member.size:
                raise RuntimeError("PyInstaller sdist member size changed during extraction")
            target.chmod(member.mode & 0o777)
    source_root = destination / expected_root
    if (
        not (source_root / "PyInstaller/__main__.py").is_file()
        or not (source_root / "bootloader/waf").is_file()
    ):
        raise RuntimeError("PyInstaller sdist is missing required build sources")
    return source_root


def _patch_fd_bound_bootloader(source_root: Path) -> None:
    source = source_root / "bootloader/src/pyi_main.c"
    text = source.read_text(encoding="utf-8")
    utils_source = source_root / "bootloader/src/pyi_utils_posix.c"
    utils_text = utils_source.read_text(encoding="utf-8")
    utils_header = source_root / "bootloader/src/pyi_utils.h"
    utils_header_text = utils_header.read_text(encoding="utf-8")
    if (
        text.count(_BOOTLOADER_MACOS_INCLUDE_NEEDLE) != 1
        or text.count(_BOOTLOADER_RESOLVER_NEEDLE) != 1
        or text.count(_BOOTLOADER_ARCHIVE_NEEDLE) != 1
        or text.count(_BOOTLOADER_RESTART_NEEDLE) != 1
        or text.count(_BOOTLOADER_CHILD_MAIN_NEEDLE) != 1
        or utils_text.count(_BOOTLOADER_POSIX_INCLUDE_NEEDLE) != 1
        or utils_text.count(_BOOTLOADER_NATIVE_HANDOFF_NEEDLE) != 1
        or utils_text.count(_BOOTLOADER_CHILD_EXEC_NEEDLE) != 1
        or utils_header_text.count(_BOOTLOADER_UTILS_HEADER_NEEDLE) != 1
    ):
        raise RuntimeError("PyInstaller bootloader resolver does not match the audited patch")
    source.write_text(
        text.replace(
            _BOOTLOADER_MACOS_INCLUDE_NEEDLE,
            _BOOTLOADER_MACOS_INCLUDE_REPLACEMENT,
        )
        .replace(
            _BOOTLOADER_RESOLVER_NEEDLE,
            _BOOTLOADER_RESOLVER_REPLACEMENT,
        )
        .replace(
            _BOOTLOADER_ARCHIVE_NEEDLE,
            _BOOTLOADER_ARCHIVE_REPLACEMENT,
        )
        .replace(
            _BOOTLOADER_RESTART_NEEDLE,
            _BOOTLOADER_RESTART_REPLACEMENT,
        )
        .replace(
            _BOOTLOADER_CHILD_MAIN_NEEDLE,
            _BOOTLOADER_CHILD_MAIN_REPLACEMENT,
        ),
        encoding="utf-8",
    )
    utils_source.write_text(
        utils_text.replace(
            _BOOTLOADER_POSIX_INCLUDE_NEEDLE,
            _BOOTLOADER_POSIX_INCLUDE_REPLACEMENT,
        )
        .replace(
            _BOOTLOADER_NATIVE_HANDOFF_NEEDLE,
            _BOOTLOADER_NATIVE_HANDOFF_REPLACEMENT,
        )
        .replace(
            _BOOTLOADER_CHILD_EXEC_NEEDLE,
            _BOOTLOADER_CHILD_EXEC_REPLACEMENT,
        ),
        encoding="utf-8",
    )
    utils_header.write_text(
        utils_header_text.replace(
            _BOOTLOADER_UTILS_HEADER_NEEDLE,
            _BOOTLOADER_UTILS_HEADER_REPLACEMENT,
        ),
        encoding="utf-8",
    )


def _prepare_fd_bound_pyinstaller(repo: Path, temporary_root: Path) -> Path:
    temporary_root.mkdir(parents=True)
    version, url, digest, size = _locked_pyinstaller_sdist(repo)
    archive_path = temporary_root / f"pyinstaller-{version}.tar.gz"
    source_parent = temporary_root / "pyinstaller-source"
    source_parent.mkdir()
    _download_locked_file(
        url,
        archive_path,
        expected_digest=digest,
        expected_size=size,
    )
    source_root = _extract_locked_pyinstaller_sdist(
        archive_path,
        source_parent,
        version=version,
    )
    _patch_fd_bound_bootloader(source_root)
    subprocess.run(
        [sys.executable, "waf", "all"],
        check=True,
        cwd=source_root / "bootloader",
    )
    bootloaders = list((source_root / "PyInstaller/bootloader").glob("*/run"))
    markers = (
        NATIVE_LISTENER_FD_ENV.encode("ascii"),
        NATIVE_EXECUTABLE_FD_ENV.encode("ascii"),
        NATIVE_EXECUTABLE_PATH_ENV.encode("ascii"),
        b"OpenEvo native descriptors",
    )
    if not bootloaders or not any(
        all(marker in payload for marker in markers)
        for payload in (path.read_bytes() for path in bootloaders)
    ):
        raise RuntimeError("custom PyInstaller bootloader is missing native execution support")
    return source_root


def _validate_fd_bound_bootloader(executable: Path) -> None:
    payload = executable.read_bytes()
    required = [
        NATIVE_LISTENER_FD_ENV.encode("ascii"),
        NATIVE_EXECUTABLE_FD_ENV.encode("ascii"),
        NATIVE_EXECUTABLE_PATH_ENV.encode("ascii"),
        b"OpenEvo native descriptors",
    ]
    if sys.platform.startswith("linux"):
        required.append(f"/proc/self/fd/{NATIVE_EXECUTABLE_FD}".encode("ascii"))
    elif sys.platform == "darwin":
        required.extend(
            (
                f"/dev/fd/{NATIVE_EXECUTABLE_FD}".encode("ascii"),
                NATIVE_EXECUTABLE_BASENAME.encode("ascii"),
            )
        )
    else:
        raise RuntimeError("Desktop sidecar bootloader platform is unsupported")
    if not all(marker in payload for marker in required):
        raise RuntimeError("packaged sidecar is missing the native execution bootloader")


def _validate_embedded_core_wheel(executable: Path, wheel: Path) -> str:
    archive_members = _archive_member_names(executable)
    benchmark_members = sorted(
        name
        for name in archive_members
        if name == "openevo_terminal_bench"
        or name.startswith(("openevo_terminal_bench.", "openevo_terminal_bench/"))
        or name.startswith("benchmarks/terminal_bench/")
    )
    if benchmark_members:
        raise RuntimeError(
            f"Desktop sidecar must not contain Terminal Bench automation: {benchmark_members}"
        )
    legacy_modules = sorted(
        name
        for name in archive_members
        if name in FORBIDDEN_LEGACY_CORE_MODULE_FILES or name in FORBIDDEN_LEGACY_SIDECAR_MODULES
    )
    if legacy_modules:
        raise RuntimeError(
            "Desktop sidecar must not contain removed Terminal Bench Core modules: "
            f"{legacy_modules}"
        )
    expected = (CORE_WHEEL_ARCHIVE_ROOT / wheel.name).as_posix()
    embedded_wheels = sorted(
        name
        for name in archive_members
        if name.startswith(f"{CORE_WHEEL_ARCHIVE_ROOT.as_posix()}/") and name.endswith(".whl")
    )
    if embedded_wheels != [expected]:
        raise RuntimeError(
            "sidecar archive does not contain the exact staged Core wheel: "
            f"expected {[expected]}, found {embedded_wheels}"
        )
    source_digest = _sha256_bytes(wheel.read_bytes())
    embedded_payload = _archive_member_bytes(executable, expected)
    try:
        with ZipFile(BytesIO(embedded_payload)) as embedded_wheel:
            _validate_core_inventory(
                set(embedded_wheel.namelist()),
                container="Desktop sidecar embedded Core wheel",
            )
    except BadZipFile as exc:
        raise RuntimeError("Desktop sidecar embedded Core wheel is unreadable") from exc
    embedded_digest = _sha256_bytes(embedded_payload)
    if embedded_digest != source_digest:
        raise RuntimeError(
            "sidecar embedded Core wheel digest does not match the built wheel: "
            f"expected {source_digest}, got {embedded_digest}"
        )
    return source_digest


def _validate_embedded_core_framework_lock(
    executable: Path,
    wheel: Path,
    framework_lock: Path,
    *,
    version: str,
) -> str:
    expected_wheel = (CORE_WHEEL_ARCHIVE_ROOT / wheel.name).as_posix()
    expected_lock = (CORE_WHEEL_ARCHIVE_ROOT / CORE_FRAMEWORK_LOCK_BASENAME).as_posix()
    archive_root = CORE_WHEEL_ARCHIVE_ROOT.as_posix()
    embedded_release_inputs = sorted(
        name
        for name in _archive_member_names(executable)
        if name == archive_root or name.startswith(f"{archive_root}/")
    )
    expected_release_inputs = sorted((expected_wheel, expected_lock))
    if embedded_release_inputs != expected_release_inputs:
        raise RuntimeError(
            "sidecar archive does not contain the exact Core release inputs: "
            f"expected {expected_release_inputs}, found {embedded_release_inputs}"
        )

    expected_payload = _core_framework_lock_bytes(wheel, version=version)
    try:
        source_payload = framework_lock.read_bytes()
    except OSError as exc:
        raise RuntimeError("Core framework lock is unavailable") from exc
    if source_payload != expected_payload:
        raise RuntimeError("staged Core framework lock differs from the exact built wheel")
    _load_exact_framework_lock(framework_lock, wheel, version=version)

    embedded_payload = _archive_member_bytes(executable, expected_lock)
    if embedded_payload != source_payload:
        raise RuntimeError("sidecar embedded Core framework lock differs from the staged lock")
    with TemporaryDirectory(prefix="openevo-embedded-core-lock-") as temporary_dir:
        embedded_root = Path(temporary_dir)
        embedded_wheel = embedded_root / wheel.name
        embedded_lock = embedded_root / CORE_FRAMEWORK_LOCK_BASENAME
        embedded_wheel.write_bytes(_archive_member_bytes(executable, expected_wheel))
        embedded_lock.write_bytes(embedded_payload)
        try:
            _load_exact_framework_lock(embedded_lock, embedded_wheel, version=version)
        except RuntimeError as exc:
            raise RuntimeError("sidecar embedded Core framework lock identity is invalid") from exc
    return _sha256_bytes(source_payload)


def _validate_embedded_product_web(
    executable: Path,
    desktop_root: Path,
    expected_build_digest: str,
) -> None:
    static_root = desktop_root / "packaging/web"
    source_files = _product_web_files(static_root)
    archive_root = "desktop/packaging/web"
    expected_members = {
        f"{archive_root}/{name}": payload for name, payload in source_files.items()
    }
    archive_members = {
        name for name in _archive_member_names(executable) if name.startswith(f"{archive_root}/")
    }
    if archive_members != set(expected_members):
        raise RuntimeError("Desktop sidecar product web inventory differs from the audited build")
    embedded_files: dict[str, bytes] = {}
    for member, expected_payload in expected_members.items():
        payload = _archive_member_bytes(executable, member)
        if payload != expected_payload:
            raise RuntimeError(
                f"Desktop sidecar product web differs from the audited build: {member}"
            )
        embedded_files[member.removeprefix(f"{archive_root}/")] = payload
    forbidden = _product_web_forbidden_text(desktop_root)
    _audit_product_web_bytes(
        embedded_files,
        forbidden=forbidden,
        container="Desktop sidecar product web",
    )
    embedded_digest = _validate_product_web_manifest(
        embedded_files,
        container="Desktop sidecar product web",
    )
    if embedded_digest != expected_build_digest:
        raise RuntimeError("Desktop sidecar product web build digest differs from dist")


def build_sidecar(
    *,
    clean: bool,
    core_wheel_output_dir: Path | None = None,
) -> Path:
    repo = _repo_root()
    desktop_root = repo / "desktop"
    packaging_root = desktop_root / "packaging"
    tauri_root = desktop_root / "src-tauri"
    binary_dir = tauri_root / "binaries"
    dist_dir = packaging_root / "sidecar-dist"
    build_dir = packaging_root / "sidecar-build"
    entrypoint = packaging_root / "sidecar_entry.py"
    static_root = packaging_root / "web"

    if core_wheel_output_dir is not None:
        requested_output = Path(os.path.abspath(core_wheel_output_dir))
        _reject_symlink_path(requested_output)
        resolved_output = requested_output.resolve()
        if any(
            _paths_overlap(resolved_output, path.resolve())
            for path in (dist_dir, build_dir, binary_dir)
        ):
            raise RuntimeError("Core wheel output directory overlaps generated paths")
        _prepare_core_wheel_output_dir(requested_output)
        if requested_output.resolve(strict=True) != resolved_output:
            raise RuntimeError("Core wheel output path changed during validation")
        core_wheel_output_dir = resolved_output
    if clean:
        shutil.rmtree(dist_dir, ignore_errors=True)
        shutil.rmtree(build_dir, ignore_errors=True)
    binary_dir.mkdir(parents=True, exist_ok=True)
    target = binary_dir / f"{SIDECAR_NAME}-{_target_triple()}{_platform_extension()}"
    target.unlink(missing_ok=True)

    with TemporaryDirectory(prefix="openevo-sidecar-build-") as temporary_dir:
        temporary_root = Path(temporary_dir)
        core_wheel = _build_core_wheel(repo, temporary_root / "core")
        _, core_version = _project_identity(repo)
        core_framework_lock = _write_core_framework_lock(
            core_wheel,
            version=core_version,
        )
        product_web_digest = _build_product_web(desktop_root)
        pyinstaller_root = _prepare_fd_bound_pyinstaller(
            repo,
            temporary_root / "pyinstaller",
        )
        build_metadata = temporary_root / SIDECAR_BUILD_METADATA_RELATIVE_PATH.name
        _write_sidecar_build_metadata(
            build_metadata,
            source_commit=_BUILD_SOURCE_COMMIT,
        )

        command = [
            sys.executable,
            "-m",
            "PyInstaller",
            *(["--clean"] if clean else []),
            "--noconfirm",
            "--onefile",
            "--name",
            SIDECAR_NAME,
            "--distpath",
            str(dist_dir),
            "--workpath",
            str(build_dir),
            "--specpath",
            str(build_dir),
            "--paths",
            str(repo),
            "--paths",
            str(repo / "src"),
            "--collect-submodules",
            "desktop",
            "--collect-submodules",
            "openevo",
            "--add-data",
            f"{core_wheel}{os.pathsep}{CORE_WHEEL_ARCHIVE_ROOT.as_posix()}",
            "--add-data",
            f"{core_framework_lock}{os.pathsep}{CORE_WHEEL_ARCHIVE_ROOT.as_posix()}",
            "--add-data",
            f"{static_root}{os.pathsep}desktop/packaging/web",
            "--add-data",
            (
                f"{build_metadata}{os.pathsep}"
                f"{SIDECAR_BUILD_METADATA_RELATIVE_PATH.parent.as_posix()}"
            ),
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
        pyinstaller_env = os.environ.copy()
        pyinstaller_env["PYTHONPATH"] = os.pathsep.join(
            filter(
                None,
                [
                    str(pyinstaller_root),
                    pyinstaller_env.get("PYTHONPATH"),
                ],
            )
        )
        subprocess.run(
            command,
            check=True,
            cwd=repo,
            env=pyinstaller_env,
        )

        built = dist_dir / f"{SIDECAR_NAME}{_platform_extension()}"
        if not built.is_file():
            raise RuntimeError(f"PyInstaller did not produce expected sidecar: {built}")
        _validate_fd_bound_bootloader(built)
        _validate_embedded_core_wheel(built, core_wheel)
        _validate_embedded_core_framework_lock(
            built,
            core_wheel,
            core_framework_lock,
            version=core_version,
        )
        _validate_embedded_product_web(built, desktop_root, product_web_digest)

        if core_wheel_output_dir is not None:
            _copy_exclusive(core_wheel, core_wheel_output_dir / core_wheel.name)
            _copy_exclusive(
                core_framework_lock,
                core_wheel_output_dir / CORE_FRAMEWORK_LOCK_BASENAME,
            )

        shutil.copy2(built, target)
        target.chmod(0o755)
        return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-clean",
        action="store_true",
        help="Reuse PyInstaller build directories instead of removing them first.",
    )
    parser.add_argument(
        "--core-wheel-output-dir",
        type=Path,
        help="Preserve the exact embedded Core wheel and framework lock in this output directory.",
    )
    args = parser.parse_args(argv)
    target = build_sidecar(
        clean=not args.no_clean,
        core_wheel_output_dir=args.core_wheel_output_dir,
    )
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
