#!/usr/bin/env python3
"""Build the bundled OpenEvo Desktop sidecar executable for Tauri."""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
import ctypes
from email.parser import Parser
import errno
import fcntl
import hashlib
from io import BytesIO
from importlib.metadata import version as distribution_version
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
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
CORE_RELEASE_TRANSACTION_MARKER = "transaction.json"
CORE_RELEASE_TRANSACTION_READY = "transaction.ready"
CORE_RELEASE_CLEANUP_AUTHORITY_CANDIDATE = ".openevo-core-release-cleanup.ready"
_CORE_RELEASE_TRANSACTION_PATTERN = re.compile(r"\.openevo-core-release-([0-9a-f]{32})")
_CORE_RELEASE_STAGING_PATTERN = re.compile(r"\.member-([0-9a-f]{32})")
_CORE_RELEASE_RETIRED_MARKER_PATTERN = re.compile(r"\.marker-retired-([0-9a-f]+)-([0-9a-f]+)")
_CORE_RELEASE_PURGE_PATTERN = re.compile(r"\.purge-([0-9a-f]+)-([0-9a-f]+)")
_CORE_RELEASE_CLEANUP_AUTHORITY_PATTERN = re.compile(
    r"\.openevo-core-release-cleanup-([0-9a-f]+)-([0-9a-f]+)-([0-9a-f]+)-([0-9a-f]+)"
)
_CORE_RELEASE_CLEANUP_AUTHORITY_PURGE_PATTERN = re.compile(
    r"\.openevo-core-release-cleanup-purge-"
    r"([0-9a-f]+)-([0-9a-f]+)-([0-9a-f]+)-([0-9a-f]+)"
)
_MAX_CORE_RELEASE_ROOT_MEMBERS = 4
_MAX_CORE_RELEASE_TRANSACTION_MEMBERS = 16
_MAX_CORE_RELEASE_MARKER_BYTES = 4096
_MAX_CORE_RELEASE_PARENT_RECOVERY_MEMBERS = 4096
_DARWIN_ACL_TYPE_EXTENDED = 0x0000_0100
_DARWIN_ACL_FIRST_ENTRY = 0
_DARWIN_ACL_NEXT_ENTRY = -1
_DARWIN_ACL_EXTENDED_ALLOW = 1
_DARWIN_ACL_EXTENDED_DENY = 2
_DARWIN_ACL_KNOWN_PERMISSIONS = sum(1 << bit for bit in (*range(1, 14), 20))
_DARWIN_ACL_MUTATING_PERMISSIONS = sum(1 << bit for bit in (2, 4, 5, 6, 8, 10, 12, 13))
_DARWIN_CARBON_CORE = (
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/CarbonCore.framework/CarbonCore"
)
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


def _trusted_source_date_epoch(repo: Path) -> int:
    result = subprocess.run(
        ["git", "-C", str(repo), "show", "-s", "--format=%ct", "HEAD^{commit}"],
        check=True,
        capture_output=True,
        text=True,
    )
    value = result.stdout.strip()
    if not value.isascii() or not value.isdecimal():
        raise RuntimeError("Git returned an invalid source timestamp for the Desktop sidecar")
    source_date_epoch = int(value)
    if source_date_epoch < 315532800:
        raise RuntimeError("Git source timestamp predates the portable wheel timestamp range")
    return source_date_epoch


_BUILD_SOURCE_COMMIT = _trusted_source_commit(_repo_root())
_BUILD_SOURCE_DATE_EPOCH = _trusted_source_date_epoch(_repo_root())


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
    except (OSError, RuntimeError) as exc:
        raise RuntimeError("Core wheel is unavailable for framework lock generation") from exc
    try:
        lock = FrameworkDistributionLock(
            distribution_version=version,
            distribution_digest=_sha256_bytes(wheel_payload),
            wheel_filename=wheel.name,
        )
    except ValueError as exc:
        raise RuntimeError("Core wheel cannot produce a valid framework lock identity") from exc
    payload = lock.model_dump(mode="json")
    return (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


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
    build_env = os.environ.copy()
    build_env["SOURCE_DATE_EPOCH"] = str(_BUILD_SOURCE_DATE_EPOCH)
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
        env=build_env,
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
            encoded_name = toc_payload[position + toc_entry_length : position + entry_length]
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


def _darwin_extended_acl_entries(file_fd: int) -> tuple[tuple[int, int], ...]:
    if sys.platform != "darwin":
        return ()
    libc = ctypes.CDLL(None, use_errno=True)
    libc.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
    libc.acl_get_fd_np.restype = ctypes.c_void_p
    libc.acl_get_entry.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
    libc.acl_get_entry.restype = ctypes.c_int
    libc.acl_get_tag_type.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
    libc.acl_get_tag_type.restype = ctypes.c_int
    libc.acl_get_permset_mask_np.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint64)]
    libc.acl_get_permset_mask_np.restype = ctypes.c_int
    libc.acl_free.argtypes = [ctypes.c_void_p]
    libc.acl_free.restype = ctypes.c_int
    ctypes.set_errno(0)
    acl = libc.acl_get_fd_np(file_fd, _DARWIN_ACL_TYPE_EXTENDED)
    if not acl:
        error = ctypes.get_errno()
        if error == errno.ENOENT:
            return ()
        raise RuntimeError(f"macOS extended ACL cannot be read from held FD: errno {error}")
    entries: list[tuple[int, int]] = []
    try:
        entry_id = _DARWIN_ACL_FIRST_ENTRY
        while True:
            entry = ctypes.c_void_p()
            ctypes.set_errno(0)
            result = libc.acl_get_entry(acl, entry_id, ctypes.byref(entry))
            error = ctypes.get_errno()
            if result == -1:
                if error == errno.EINVAL:
                    break
                raise RuntimeError("macOS extended ACL contains an unreadable entry")
            if result != 0 or not entry:
                raise RuntimeError("macOS extended ACL contains an unreadable entry")
            tag = ctypes.c_int()
            permissions = ctypes.c_uint64()
            if (
                libc.acl_get_tag_type(entry, ctypes.byref(tag)) != 0
                or libc.acl_get_permset_mask_np(entry, ctypes.byref(permissions)) != 0
            ):
                raise RuntimeError("macOS extended ACL entry cannot be decoded")
            entries.append((tag.value, permissions.value))
            entry_id = _DARWIN_ACL_NEXT_ENTRY
    finally:
        if libc.acl_free(acl) != 0:
            raise RuntimeError("macOS extended ACL storage could not be released")
    return tuple(entries)


def _delete_darwin_extended_acl(file_fd: int) -> None:
    if sys.platform != "darwin":
        return
    libc = ctypes.CDLL(None, use_errno=True)
    libc.acl_delete_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
    libc.acl_delete_fd_np.restype = ctypes.c_int
    if libc.acl_delete_fd_np(file_fd, _DARWIN_ACL_TYPE_EXTENDED) != 0:
        error = ctypes.get_errno()
        raise RuntimeError(f"macOS extended ACL cannot be cleared from held FD: errno {error}")


def _validate_darwin_acl_entries(
    entries: tuple[tuple[int, int], ...],
    *,
    reject_mutating: bool,
) -> None:
    for tag, permissions in entries:
        if tag not in {_DARWIN_ACL_EXTENDED_ALLOW, _DARWIN_ACL_EXTENDED_DENY}:
            raise RuntimeError("macOS extended ACL contains an unknown tag")
        if permissions & ~_DARWIN_ACL_KNOWN_PERMISSIONS:
            raise RuntimeError("macOS extended ACL contains an unknown permission")
        if (
            reject_mutating
            and tag == _DARWIN_ACL_EXTENDED_ALLOW
            and permissions & _DARWIN_ACL_MUTATING_PERMISSIONS
        ):
            raise RuntimeError("macOS extended ACL permits mutation by an additional principal")


def _clear_and_verify_fd_acl(file_fd: int, *, name: str) -> None:
    if sys.platform != "darwin":
        return
    before = os.fstat(file_fd)
    entries = _darwin_extended_acl_entries(file_fd)
    _validate_darwin_acl_entries(entries, reject_mutating=False)
    if entries:
        _delete_darwin_extended_acl(file_fd)
    if _darwin_extended_acl_entries(file_fd):
        raise RuntimeError(f"Core release extended ACL remained after clearing: {name}")
    after = os.fstat(file_fd)
    if (
        (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
        or after.st_uid != before.st_uid
        or after.st_mode != before.st_mode
    ):
        raise RuntimeError(f"Core release identity changed while clearing extended ACL: {name}")


def _require_fd_acl_free(file_fd: int, *, name: str) -> None:
    entries = _darwin_extended_acl_entries(file_fd)
    _validate_darwin_acl_entries(entries, reject_mutating=True)
    if entries:
        raise RuntimeError(f"Core release extended ACL changed after initialization: {name}")


def _require_fd_acl_no_mutating_allow(file_fd: int, *, name: str) -> None:
    entries = _darwin_extended_acl_entries(file_fd)
    try:
        _validate_darwin_acl_entries(entries, reject_mutating=True)
    except RuntimeError as exc:
        raise RuntimeError(f"Core release {name} extended ACL is unsafe") from exc


def _require_private_directory(descriptor: os.stat_result, *, name: str) -> None:
    if (
        not stat.S_ISDIR(descriptor.st_mode)
        or descriptor.st_uid != os.geteuid()
        or descriptor.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise RuntimeError(f"Core release {name} owner or private permissions are invalid")


def _acquire_core_release_output_lock(
    parent_fd: int,
    directory_fd: int,
    output_name: str,
) -> os.stat_result:
    before = os.fstat(directory_fd)
    _require_private_directory(before, name="output lock")
    _require_fd_acl_free(directory_fd, name="Core wheel output lock")
    current = os.stat(output_name, dir_fd=parent_fd, follow_symlinks=False)
    if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
        raise RuntimeError("Core wheel output changed before lock acquisition")
    try:
        fcntl.flock(directory_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            raise RuntimeError(
                "Core wheel output is locked by another active sidecar builder"
            ) from exc
        raise RuntimeError("Core wheel output lock cannot be acquired") from exc
    after = os.fstat(directory_fd)
    _require_private_directory(after, name="output lock")
    _require_fd_acl_free(directory_fd, name="Core wheel output lock")
    current = os.stat(output_name, dir_fd=parent_fd, follow_symlinks=False)
    if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino) or (
        current.st_dev,
        current.st_ino,
    ) != (before.st_dev, before.st_ino):
        raise RuntimeError("Core wheel output changed during lock acquisition")
    return after


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _reject_symlink_path(path: Path) -> None:
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise RuntimeError(
                f"Core wheel output path must not contain a symbolic link: {candidate}"
            )


class _CoreReleaseOutput:
    __slots__ = (
        "path",
        "resolved_path",
        "parent_fd",
        "directory_fd",
        "device",
        "inode",
        "initial_inventory",
        "transaction_name",
        "transaction_fd",
        "marker_fd",
        "marker_identity",
        "member_intents",
        "sources",
        "members",
        "preexisting",
        "committed",
    )

    def __init__(
        self,
        *,
        path: Path,
        resolved_path: Path,
        parent_fd: int,
        directory_fd: int,
        device: int,
        inode: int,
    ) -> None:
        self.path = path
        self.resolved_path = resolved_path
        self.parent_fd = parent_fd
        self.directory_fd = directory_fd
        self.device = device
        self.inode = inode
        self.initial_inventory: tuple[str, ...] = ()
        self.transaction_name: str | None = None
        self.transaction_fd = -1
        self.marker_fd = -1
        self.marker_identity: tuple[int, int] | None = None
        self.member_intents: dict[str, str] = {}
        self.sources: list[_CoreReleaseSource] = []
        self.members: list[_CoreReleaseMember] = []
        self.preexisting = False
        self.committed = False

    def require_bound_path(self) -> None:
        try:
            _reject_symlink_path(self.path)
            current_path = self.path.resolve(strict=True)
            current = os.stat(self.path, follow_symlinks=False)
            pinned = os.fstat(self.directory_fd)
            parent = os.fstat(self.parent_fd)
            parent_current = os.stat(
                self.resolved_path.name,
                dir_fd=self.parent_fd,
                follow_symlinks=False,
            )
            _require_private_directory(parent, name="output parent")
            _require_fd_acl_no_mutating_allow(
                self.parent_fd,
                name="output parent",
            )
            _require_fd_acl_free(self.directory_fd, name="Core wheel output")
        except (OSError, RuntimeError) as exc:
            raise RuntimeError("Core wheel output path changed during the sidecar build") from exc
        expected_identity = (self.device, self.inode)
        if (
            current_path != self.resolved_path
            or (current.st_dev, current.st_ino) != expected_identity
            or (pinned.st_dev, pinned.st_ino) != expected_identity
            or (parent_current.st_dev, parent_current.st_ino) != expected_identity
            or not stat.S_ISDIR(current.st_mode)
            or not stat.S_ISDIR(pinned.st_mode)
            or current.st_uid != os.geteuid()
            or pinned.st_uid != os.geteuid()
            or current.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or pinned.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise RuntimeError(
                "Core wheel output path, owner, or private permissions changed during the sidecar build"
            )

    def close(self) -> None:
        for member in self.members:
            if member.file_fd >= 0:
                os.close(member.file_fd)
                member.file_fd = -1
        for source in self.sources:
            if source.file_fd >= 0:
                os.close(source.file_fd)
                source.file_fd = -1
        if self.marker_fd >= 0:
            os.close(self.marker_fd)
            self.marker_fd = -1
        if self.transaction_fd >= 0:
            os.close(self.transaction_fd)
            self.transaction_fd = -1


class _CoreReleaseSource:
    __slots__ = ("path", "name", "file_fd", "device", "inode", "byte_size", "sha256")

    def __init__(
        self,
        *,
        path: Path,
        name: str,
        file_fd: int,
        device: int,
        inode: int,
        byte_size: int,
        sha256: str,
    ) -> None:
        self.path = path
        self.name = name
        self.file_fd = file_fd
        self.device = device
        self.inode = inode
        self.byte_size = byte_size
        self.sha256 = sha256


class _CoreReleaseMember:
    __slots__ = ("source", "file_fd", "device", "inode")

    def __init__(
        self,
        *,
        source: _CoreReleaseSource,
        file_fd: int,
        device: int,
        inode: int,
    ) -> None:
        self.source = source
        self.file_fd = file_fd
        self.device = device
        self.inode = inode


def _bounded_directory_scan(
    directory_fd: int,
    *,
    limit: int,
    container: str,
) -> tuple[str, ...]:
    names: list[str] = []
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            names.append(entry.name)
            if len(names) > limit:
                raise RuntimeError(f"{container} contains too many entries")
    names.sort()
    return tuple(names)


def _bounded_listdir(directory_fd: int, *, limit: int, container: str) -> tuple[str, ...]:
    first = _bounded_directory_scan(directory_fd, limit=limit, container=container)
    second = _bounded_directory_scan(directory_fd, limit=limit, container=container)
    if first != second:
        raise RuntimeError(f"{container} changed while it was enumerated")
    return second


def _bounded_directory_identity_scan(
    directory_fd: int,
    *,
    limit: int,
    container: str,
) -> tuple[tuple[str, int, int], ...]:
    def scan() -> tuple[tuple[str, int, int], ...]:
        entries: list[tuple[str, int, int]] = []
        try:
            with os.scandir(directory_fd) as iterator:
                for entry in iterator:
                    descriptor = entry.stat(follow_symlinks=False)
                    entries.append((entry.name, descriptor.st_dev, descriptor.st_ino))
                    if len(entries) > limit:
                        raise RuntimeError(f"{container} contains too many entries")
        except FileNotFoundError as exc:
            raise RuntimeError(f"{container} changed while it was enumerated") from exc
        entries.sort()
        return tuple(entries)

    first = scan()
    second = scan()
    if first != second:
        raise RuntimeError(f"{container} changed while it was enumerated")
    return second


def _core_release_cleanup_authority_identities(
    name: str,
) -> tuple[tuple[int, int], tuple[int, int]] | None:
    match = _CORE_RELEASE_CLEANUP_AUTHORITY_PATTERN.fullmatch(name)
    if match is None:
        match = _CORE_RELEASE_CLEANUP_AUTHORITY_PURGE_PATTERN.fullmatch(name)
    if match is None:
        return None
    return (
        (int(match.group(1), 16), int(match.group(2), 16)),
        (int(match.group(3), 16), int(match.group(4), 16)),
    )


def _is_core_release_cleanup_authority(name: str) -> bool:
    return name == CORE_RELEASE_CLEANUP_AUTHORITY_CANDIDATE or (
        _core_release_cleanup_authority_identities(name) is not None
    )


def _classify_core_release_inventory(directory_fd: int) -> tuple[str, ...]:
    names = _bounded_listdir(
        directory_fd,
        limit=_MAX_CORE_RELEASE_ROOT_MEMBERS,
        container="Core wheel output",
    )
    if not names:
        return names
    transactions = [name for name in names if _CORE_RELEASE_TRANSACTION_PATTERN.fullmatch(name)]
    cleanup_authorities = [name for name in names if _is_core_release_cleanup_authority(name)]
    ordinary = [
        name for name in names if name not in transactions and name not in cleanup_authorities
    ]
    wheels = [name for name in ordinary if name.endswith(".whl")]
    locks = [name for name in ordinary if name == CORE_FRAMEWORK_LOCK_BASENAME]
    if len(transactions) > 1 or len(cleanup_authorities) > 1 or len(wheels) > 1 or len(locks) > 1:
        raise RuntimeError("Core wheel output has an ambiguous release transaction")
    if any(name not in {*wheels, *locks} for name in ordinary):
        raise RuntimeError("Core wheel output contains an unknown entry")
    if transactions:
        return names
    if cleanup_authorities and (
        not ordinary or (len(wheels) == 1 and len(locks) == 1 and len(ordinary) == 2)
    ):
        return names
    if len(wheels) == 1 and len(locks) == 1 and len(ordinary) == 2:
        return names
    raise RuntimeError(
        "Core wheel output must be empty or contain one complete recoverable release pair"
    )


def _sha256_fd(file_fd: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    byte_size = 0
    os.lseek(file_fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(file_fd, 1024 * 1024)
        if not chunk:
            break
        byte_size += len(chunk)
        digest.update(chunk)
    os.lseek(file_fd, 0, os.SEEK_SET)
    return byte_size, digest.hexdigest()


def _open_core_release_source(path: Path, *, name: str) -> _CoreReleaseSource:
    if Path(name).name != name or name in {"", ".", ".."}:
        raise RuntimeError("Core release export filename is invalid")
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        file_fd = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError(f"Core release input cannot be opened safely: {name}") from exc
    try:
        descriptor = os.fstat(file_fd)
        if not stat.S_ISREG(descriptor.st_mode) or descriptor.st_nlink != 1:
            raise RuntimeError(f"Core release input is not a private regular file: {name}")
        _clear_and_verify_fd_acl(file_fd, name=name)
        descriptor = os.fstat(file_fd)
        byte_size, sha256 = _sha256_fd(file_fd)
        if byte_size != descriptor.st_size:
            raise RuntimeError(f"Core release input changed while hashing: {name}")
        return _CoreReleaseSource(
            path=path,
            name=name,
            file_fd=file_fd,
            device=descriptor.st_dev,
            inode=descriptor.st_ino,
            byte_size=byte_size,
            sha256=sha256,
        )
    except BaseException:
        os.close(file_fd)
        raise


def _source_marker_entry(
    source: _CoreReleaseSource,
    *,
    staging_name: str,
) -> dict[str, object]:
    return {
        "name": source.name,
        "staging_name": staging_name,
        "byte_size": source.byte_size,
        "sha256": source.sha256,
    }


def _marker_bytes(
    authority: _CoreReleaseOutput,
    *,
    phase: str,
    transaction_device: int,
    transaction_inode: int,
    member_identities: dict[str, tuple[int, int]] | None = None,
    cleanup_index: int | None = None,
) -> bytes:
    if phase == "cleaning":
        if member_identities is None or cleanup_index is None:
            raise RuntimeError("Core release cleaning marker state is incomplete")
    elif cleanup_index is not None:
        raise RuntimeError("Core release marker has cleanup progress outside cleaning")
    members: list[dict[str, object]] = []
    for source in authority.sources:
        try:
            staging_name = authority.member_intents[source.name]
        except KeyError as exc:
            raise RuntimeError("Core release member intent is incomplete") from exc
        entry = _source_marker_entry(source, staging_name=staging_name)
        if member_identities is not None and source.name in member_identities:
            device, inode = member_identities[source.name]
            entry["device"] = device
            entry["inode"] = inode
        members.append(entry)
    payload = {
        "schema_version": "2",
        "phase": phase,
        "output_device": authority.device,
        "output_inode": authority.inode,
        "transaction_device": transaction_device,
        "transaction_inode": transaction_inode,
        "members": members,
    }
    if cleanup_index is not None:
        payload["cleanup_index"] = cleanup_index
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _core_release_cleanup_authority_name(
    cleanup_identity: tuple[int, int],
    file_identity: tuple[int, int],
    *,
    purging: bool = False,
) -> str:
    prefix = (
        ".openevo-core-release-cleanup-purge-" if purging else ".openevo-core-release-cleanup-"
    )
    return (
        f"{prefix}{cleanup_identity[0]:x}-{cleanup_identity[1]:x}-"
        f"{file_identity[0]:x}-{file_identity[1]:x}"
    )


def _core_release_cleanup_authority_bytes(
    authority: _CoreReleaseOutput,
    cleanup_identity: tuple[int, int],
) -> bytes:
    parent = os.fstat(authority.parent_fd)
    payload = {
        "schema_version": "1",
        "parent_device": parent.st_dev,
        "parent_inode": parent.st_ino,
        "output_device": authority.device,
        "output_inode": authority.inode,
        "cleanup_device": cleanup_identity[0],
        "cleanup_inode": cleanup_identity[1],
        "tombstone_name": _core_release_tombstone_name(authority),
        "purge_name": _core_release_directory_purge_name(authority),
        "members": [
            {
                "name": source.name,
                "byte_size": source.byte_size,
                "sha256": source.sha256,
            }
            for source in authority.sources
        ],
    }
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _validate_core_release_cleanup_authority(
    authority: _CoreReleaseOutput,
    payload: bytes,
) -> tuple[int, int]:
    marker = _decode_marker(payload)
    cleanup_device = marker.get("cleanup_device")
    cleanup_inode = marker.get("cleanup_inode")
    if (
        type(cleanup_device) is not int
        or type(cleanup_inode) is not int
        or cleanup_device < 0
        or cleanup_inode <= 0
    ):
        raise RuntimeError("Core release cleanup authority identity is invalid")
    cleanup_identity = (cleanup_device, cleanup_inode)
    expected = _core_release_cleanup_authority_bytes(authority, cleanup_identity)
    if payload != expected:
        raise RuntimeError("Core release cleanup authority does not match current inputs")
    return cleanup_identity


def _write_bound_member(
    directory_fd: int,
    name: str,
    payload: bytes,
    *,
    mode: int,
) -> tuple[int, tuple[int, int]]:
    file_fd = os.open(
        name,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        mode,
        dir_fd=directory_fd,
    )
    try:
        _clear_and_verify_fd_acl(file_fd, name=name)
        view = memoryview(payload)
        while view:
            written = os.write(file_fd, view)
            if written <= 0:
                raise OSError("short write while creating Core release transaction")
            view = view[written:]
        os.fsync(file_fd)
        descriptor = os.fstat(file_fd)
        return file_fd, (descriptor.st_dev, descriptor.st_ino)
    except BaseException:
        os.close(file_fd)
        raise


def _require_member_attributes(
    descriptor: os.stat_result,
    *,
    name: str,
    mode: int,
    identity: tuple[int, int] | None = None,
    link_counts: frozenset[int] = frozenset({1}),
) -> None:
    if (
        not stat.S_ISREG(descriptor.st_mode)
        or descriptor.st_uid != os.geteuid()
        or stat.S_IMODE(descriptor.st_mode) != mode
        or descriptor.st_nlink not in link_counts
        or (identity is not None and (descriptor.st_dev, descriptor.st_ino) != identity)
    ):
        raise RuntimeError(f"Core release member identity or permissions changed: {name}")


def _verify_member_path(
    directory_fd: int,
    source: _CoreReleaseSource,
    *,
    name: str | None = None,
    identity: tuple[int, int] | None = None,
    link_counts: frozenset[int] = frozenset({1}),
) -> tuple[int, int]:
    path_name = source.name if name is None else name
    file_fd = os.open(
        path_name,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        dir_fd=directory_fd,
    )
    try:
        descriptor = os.fstat(file_fd)
        _require_member_attributes(
            descriptor,
            name=path_name,
            mode=0o644,
            identity=identity,
            link_counts=link_counts,
        )
        _require_fd_acl_free(file_fd, name=path_name)
        descriptor = os.fstat(file_fd)
        _require_member_attributes(
            descriptor,
            name=path_name,
            mode=0o644,
            identity=identity,
            link_counts=link_counts,
        )
        byte_size, sha256 = _sha256_fd(file_fd)
        if (
            descriptor.st_size != source.byte_size
            or byte_size != source.byte_size
            or sha256 != source.sha256
        ):
            raise RuntimeError(f"Core release member content changed: {source.name}")
        current = os.stat(path_name, dir_fd=directory_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (descriptor.st_dev, descriptor.st_ino):
            raise RuntimeError(f"Core release member pathname changed: {path_name}")
        return descriptor.st_dev, descriptor.st_ino
    finally:
        os.close(file_fd)


def _read_marker(directory_fd: int, name: str) -> tuple[bytes, tuple[int, int]]:
    file_fd = os.open(
        name,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        dir_fd=directory_fd,
    )
    try:
        descriptor = os.fstat(file_fd)
        _require_member_attributes(descriptor, name=name, mode=0o600)
        _require_fd_acl_free(file_fd, name=name)
        descriptor = os.fstat(file_fd)
        _require_member_attributes(descriptor, name=name, mode=0o600)
        if descriptor.st_size > _MAX_CORE_RELEASE_MARKER_BYTES:
            raise RuntimeError("Core release transaction marker is too large")
        payload = os.read(file_fd, _MAX_CORE_RELEASE_MARKER_BYTES + 1)
        if len(payload) != descriptor.st_size:
            raise RuntimeError("Core release transaction marker changed while reading")
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        identity = (descriptor.st_dev, descriptor.st_ino)
        if (current.st_dev, current.st_ino) != identity:
            raise RuntimeError("Core release transaction marker pathname changed")
        return payload, identity
    finally:
        os.close(file_fd)


@contextmanager
def _open_core_release_output(output_dir: Path) -> Iterator[_CoreReleaseOutput]:
    output_dir = Path(os.path.abspath(output_dir))
    _reject_symlink_path(output_dir)
    if output_dir.name in {"", ".", ".."}:
        raise RuntimeError("Core wheel output must name a child of a trusted parent")
    resolved_parent = output_dir.parent.resolve(strict=True)
    resolved_path = resolved_parent / output_dir.name
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    if not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("Core wheel output requires no-follow file support")
    flags |= os.O_NOFOLLOW
    parent_fd = -1
    directory_fd = -1
    created = False
    try:
        parent_fd = os.open(resolved_parent, flags)
        parent_descriptor = os.fstat(parent_fd)
        _require_private_directory(parent_descriptor, name="output parent")
        _require_fd_acl_no_mutating_allow(parent_fd, name="output parent")
        try:
            os.mkdir(output_dir.name, 0o700, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            pass
        directory_fd = os.open(output_dir.name, flags, dir_fd=parent_fd)
    except (OSError, RuntimeError) as exc:
        if directory_fd >= 0:
            os.close(directory_fd)
        if parent_fd >= 0:
            os.close(parent_fd)
        raise RuntimeError(
            f"Core wheel output parent owner, or private permissions are invalid: {output_dir}"
        ) from exc
    try:
        if output_dir.resolve(strict=True) != resolved_path:
            raise RuntimeError("Core wheel output path changed while it was opened")
        descriptor = os.fstat(directory_fd)
        try:
            _require_private_directory(descriptor, name="output")
        except RuntimeError as exc:
            raise RuntimeError(
                "Core wheel output path, owner, or private permissions are invalid"
            ) from exc
        if created:
            _clear_and_verify_fd_acl(directory_fd, name="Core wheel output")
        else:
            _require_fd_acl_free(directory_fd, name="Core wheel output")
        descriptor = _acquire_core_release_output_lock(
            parent_fd,
            directory_fd,
            output_dir.name,
        )
        authority = _CoreReleaseOutput(
            path=output_dir,
            resolved_path=resolved_path,
            parent_fd=parent_fd,
            directory_fd=directory_fd,
            device=descriptor.st_dev,
            inode=descriptor.st_ino,
        )
        authority.require_bound_path()
        authority.initial_inventory = _classify_core_release_inventory(directory_fd)
        try:
            yield authority
            authority.require_bound_path()
            _commit_core_release_inputs(authority)
            authority.require_bound_path()
            authority.committed = True
        except BaseException:
            try:
                _rollback_core_release_inputs(authority)
            except BaseException as rollback_exc:
                raise RuntimeError(
                    "Core release export rollback could not be verified; "
                    "identity-mismatched entries were preserved"
                ) from rollback_exc
            raise
        finally:
            authority.close()
    finally:
        if directory_fd >= 0:
            os.close(directory_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def _verify_source(source: _CoreReleaseSource, *, require_path: bool) -> None:
    descriptor = os.fstat(source.file_fd)
    if (
        not stat.S_ISREG(descriptor.st_mode)
        or (descriptor.st_dev, descriptor.st_ino) != (source.device, source.inode)
        or descriptor.st_size != source.byte_size
    ):
        raise RuntimeError(f"Core release source identity changed: {source.name}")
    _require_fd_acl_free(source.file_fd, name=source.name)
    byte_size, sha256 = _sha256_fd(source.file_fd)
    if byte_size != source.byte_size or sha256 != source.sha256:
        raise RuntimeError(f"Core release source content changed: {source.name}")
    if require_path:
        current = os.stat(source.path, follow_symlinks=False)
        if (
            (current.st_dev, current.st_ino) != (source.device, source.inode)
            or not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
        ):
            raise RuntimeError(f"Core release source pathname changed: {source.name}")


def _validate_framework_lock_contract(
    wheel: _CoreReleaseSource,
    framework_lock: _CoreReleaseSource,
) -> None:
    if framework_lock.byte_size > _MAX_CORE_RELEASE_MARKER_BYTES:
        raise RuntimeError("Core framework lock is unexpectedly large")
    os.lseek(framework_lock.file_fd, 0, os.SEEK_SET)
    payload = os.read(framework_lock.file_fd, framework_lock.byte_size + 1)
    os.lseek(framework_lock.file_fd, 0, os.SEEK_SET)
    lock = _validated_framework_lock(payload)
    if lock.distribution_digest != wheel.sha256 or lock.wheel_filename != wheel.name:
        raise RuntimeError("Core framework lock does not bind the exported wheel")


def _open_release_sources(
    authority: _CoreReleaseOutput,
    wheel: Path,
    framework_lock: Path,
) -> None:
    if authority.sources:
        raise RuntimeError("Core release inputs were already opened")
    wheel_source = _open_core_release_source(wheel, name=wheel.name)
    try:
        lock_source = _open_core_release_source(
            framework_lock,
            name=CORE_FRAMEWORK_LOCK_BASENAME,
        )
    except BaseException:
        os.close(wheel_source.file_fd)
        wheel_source.file_fd = -1
        raise
    authority.sources.extend((wheel_source, lock_source))
    _validate_framework_lock_contract(wheel_source, lock_source)


def _open_transaction_directory(
    authority: _CoreReleaseOutput,
    name: str,
    *,
    initialize_acl: bool = False,
) -> tuple[int, os.stat_result]:
    transaction_fd = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        dir_fd=authority.directory_fd,
    )
    descriptor = os.fstat(transaction_fd)
    current = os.stat(name, dir_fd=authority.directory_fd, follow_symlinks=False)
    if (
        not stat.S_ISDIR(descriptor.st_mode)
        or descriptor.st_uid != os.geteuid()
        or stat.S_IMODE(descriptor.st_mode) != 0o700
        or (current.st_dev, current.st_ino) != (descriptor.st_dev, descriptor.st_ino)
    ):
        os.close(transaction_fd)
        raise RuntimeError("Core release transaction directory identity changed")
    try:
        if initialize_acl:
            _clear_and_verify_fd_acl(transaction_fd, name="Core release transaction")
        else:
            _require_fd_acl_free(transaction_fd, name="Core release transaction")
    except BaseException:
        os.close(transaction_fd)
        raise
    descriptor = os.fstat(transaction_fd)
    current = os.stat(name, dir_fd=authority.directory_fd, follow_symlinks=False)
    if (current.st_dev, current.st_ino) != (descriptor.st_dev, descriptor.st_ino):
        os.close(transaction_fd)
        raise RuntimeError("Core release transaction directory changed during ACL verification")
    return transaction_fd, descriptor


def _rename_noreplace(
    source_fd: int,
    source_name: str,
    destination_fd: int,
    destination_name: str,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    encoded_source = os.fsencode(source_name)
    encoded_destination = os.fsencode(destination_name)
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
            source_fd,
            encoded_source,
            destination_fd,
            encoded_destination,
            1,
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
            encoded_source,
            destination_fd,
            encoded_destination,
            0x0000_0004,
        )
    else:
        raise RuntimeError("Core release cleanup requires atomic no-replace rename support")
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), source_name, destination_name)


class _DarwinFileSystemReference(ctypes.Structure):
    _fields_ = [("hidden", ctypes.c_uint8 * 80)]


def _core_release_fd_removal_supported() -> bool:
    return sys.platform == "darwin"


def _prepare_core_release_fd_removal(
    object_fd: int,
    *,
    is_directory: bool,
) -> tuple[ctypes.CDLL, _DarwinFileSystemReference]:
    if not _core_release_fd_removal_supported():
        raise RuntimeError(
            "Core release cleanup cannot safely remove an inode on this platform; "
            "the quarantined object was preserved"
        )
    try:
        carbon_core = ctypes.CDLL(_DARWIN_CARBON_CORE)
    except OSError as exc:
        raise RuntimeError(
            "Core release cleanup cannot load the identity-bound macOS removal API; "
            "the quarantined object was preserved"
        ) from exc
    carbon_core.FSPathMakeRef.argtypes = [
        ctypes.c_char_p,
        ctypes.POINTER(_DarwinFileSystemReference),
        ctypes.POINTER(ctypes.c_uint8),
    ]
    carbon_core.FSPathMakeRef.restype = ctypes.c_int32
    carbon_core.FSUnlinkObject.argtypes = [ctypes.POINTER(_DarwinFileSystemReference)]
    carbon_core.FSUnlinkObject.restype = ctypes.c_int16
    reference = _DarwinFileSystemReference()
    reference_is_directory = ctypes.c_uint8()
    # This descriptor path cannot be rebound by another process; FSUnlinkObject
    # later consumes only the resulting opaque reference, never the cleanup name.
    result = carbon_core.FSPathMakeRef(
        os.fsencode(f"/dev/fd/{object_fd}"),
        ctypes.byref(reference),
        ctypes.byref(reference_is_directory),
    )
    if result != 0 or bool(reference_is_directory.value) != is_directory:
        raise RuntimeError(
            "Core release cleanup could not bind the held inode to a macOS file-system "
            f"reference (status {result}); the quarantined object was preserved"
        )
    return carbon_core, reference


def _execute_core_release_fd_removal(
    token: tuple[ctypes.CDLL, _DarwinFileSystemReference],
    parent_fd: int,
    name: str,
    object_fd: int,
    *,
    is_directory: bool,
) -> None:
    del parent_fd, name, object_fd, is_directory
    carbon_core, reference = token
    result = carbon_core.FSUnlinkObject(ctypes.byref(reference))
    if result != 0:
        raise RuntimeError(
            "Core release cleanup identity-bound removal failed "
            f"with macOS status {result}; the quarantined object was preserved"
        )


def _before_core_release_fd_removal(
    parent_fd: int,
    name: str,
    object_fd: int,
    *,
    is_directory: bool,
) -> None:
    del parent_fd, name, object_fd, is_directory


def _remove_core_release_fd_bound_entry(
    parent_fd: int,
    name: str,
    object_fd: int,
    *,
    identity: tuple[int, int],
    is_directory: bool,
    subject: str,
) -> None:
    held = os.fstat(object_fd)
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise RuntimeError(f"{subject} pathname disappeared before removal") from exc
    if (
        (held.st_dev, held.st_ino) != identity
        or (current.st_dev, current.st_ino) != identity
        or stat.S_ISDIR(held.st_mode) != is_directory
        or held.st_nlink <= 0
    ):
        raise RuntimeError(f"{subject} replacement was preserved before removal")
    token = _prepare_core_release_fd_removal(object_fd, is_directory=is_directory)
    held = os.fstat(object_fd)
    if (held.st_dev, held.st_ino) != identity or held.st_nlink <= 0:
        raise RuntimeError(f"{subject} held identity changed before removal")
    _before_core_release_fd_removal(
        parent_fd,
        name,
        object_fd,
        is_directory=is_directory,
    )
    try:
        _execute_core_release_fd_removal(
            token,
            parent_fd,
            name,
            object_fd,
            is_directory=is_directory,
        )
    except BaseException as exc:
        raise RuntimeError(
            f"{subject} identity-bound removal failed; the object was preserved"
        ) from exc
    if os.fstat(object_fd).st_nlink != 0:
        raise RuntimeError(f"{subject} remained linked after identity-bound removal")
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise RuntimeError(f"{subject} pathname replacement was preserved")


def _retired_marker_name(identity: tuple[int, int]) -> str:
    return f".marker-retired-{identity[0]:x}-{identity[1]:x}"


def _retired_marker_identity(name: str) -> tuple[int, int] | None:
    match = _CORE_RELEASE_RETIRED_MARKER_PATTERN.fullmatch(name)
    if match is None:
        return None
    return int(match.group(1), 16), int(match.group(2), 16)


def _core_release_tombstone_name(authority: _CoreReleaseOutput) -> str:
    return f".openevo-core-release-tombstone-{authority.device:x}-{authority.inode:x}"


def _core_release_directory_purge_name(authority: _CoreReleaseOutput) -> str:
    return f".openevo-core-release-purge-{authority.device:x}-{authority.inode:x}"


def _core_release_entry_purge_name(identity: tuple[int, int]) -> str:
    return f".purge-{identity[0]:x}-{identity[1]:x}"


def _core_release_entry_purge_identity(name: str) -> tuple[int, int] | None:
    match = _CORE_RELEASE_PURGE_PATTERN.fullmatch(name)
    if match is None:
        return None
    return int(match.group(1), 16), int(match.group(2), 16)


def _after_core_release_tombstone_window(
    authority: _CoreReleaseOutput,
    window: str,
) -> None:
    del authority, window


def _publish_core_release_cleanup_authority(
    authority: _CoreReleaseOutput,
    cleanup_identity: tuple[int, int],
) -> tuple[str, tuple[int, int], tuple[int, int]]:
    payload = _core_release_cleanup_authority_bytes(authority, cleanup_identity)
    file_fd, file_identity = _write_bound_member(
        authority.directory_fd,
        CORE_RELEASE_CLEANUP_AUTHORITY_CANDIDATE,
        payload,
        mode=0o600,
    )
    try:
        os.fsync(authority.directory_fd)
        _after_core_release_tombstone_window(authority, "cleanup-authority-candidate")
        name = _core_release_cleanup_authority_name(cleanup_identity, file_identity)
        _rename_noreplace(
            authority.directory_fd,
            CORE_RELEASE_CLEANUP_AUTHORITY_CANDIDATE,
            authority.directory_fd,
            name,
        )
        os.fsync(authority.directory_fd)
        _after_core_release_tombstone_window(authority, "cleanup-authority-published")
        actual_payload, actual_identity = _read_marker(authority.directory_fd, name)
        if actual_payload != payload or actual_identity != file_identity:
            raise RuntimeError("Core release cleanup authority changed during publication")
        return name, cleanup_identity, file_identity
    finally:
        os.close(file_fd)


def _recover_core_release_cleanup_authority(
    authority: _CoreReleaseOutput,
) -> tuple[str, tuple[int, int], tuple[int, int]] | None:
    names = [
        name for name in authority.initial_inventory if _is_core_release_cleanup_authority(name)
    ]
    if not names:
        return None
    if len(names) != 1:
        raise RuntimeError("Core release cleanup authority is ambiguous")
    name = names[0]
    payload, file_identity = _read_marker(authority.directory_fd, name)
    cleanup_identity = _validate_core_release_cleanup_authority(authority, payload)
    encoded = _core_release_cleanup_authority_identities(name)
    if name == CORE_RELEASE_CLEANUP_AUTHORITY_CANDIDATE:
        final_name = _core_release_cleanup_authority_name(cleanup_identity, file_identity)
        try:
            _rename_noreplace(
                authority.directory_fd,
                name,
                authority.directory_fd,
                final_name,
            )
        except FileExistsError as exc:
            raise RuntimeError("Core release cleanup authority replacement was preserved") from exc
        os.fsync(authority.directory_fd)
        payload, recovered_identity = _read_marker(authority.directory_fd, final_name)
        if (
            recovered_identity != file_identity
            or _validate_core_release_cleanup_authority(authority, payload) != cleanup_identity
        ):
            raise RuntimeError("Core release cleanup authority changed during recovery")
        return final_name, cleanup_identity, file_identity
    if encoded != (cleanup_identity, file_identity):
        raise RuntimeError("Core release cleanup authority filename identity changed")
    return name, cleanup_identity, file_identity


def _discard_core_release_cleanup_authority(
    authority: _CoreReleaseOutput,
    name: str,
    cleanup_identity: tuple[int, int],
    file_identity: tuple[int, int],
) -> None:
    payload, actual_identity = _read_marker(authority.directory_fd, name)
    if (
        actual_identity != file_identity
        or _validate_core_release_cleanup_authority(authority, payload) != cleanup_identity
    ):
        raise RuntimeError("Core release cleanup authority identity changed")
    file_fd = os.open(
        name,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        dir_fd=authority.directory_fd,
    )
    try:
        descriptor = os.fstat(file_fd)
        _require_member_attributes(
            descriptor,
            name=name,
            mode=0o600,
            identity=file_identity,
        )
        _require_fd_acl_free(file_fd, name="Core release cleanup authority")
        purge_name = _core_release_cleanup_authority_name(
            cleanup_identity,
            file_identity,
            purging=True,
        )
        if name != purge_name:
            _rename_noreplace(
                authority.directory_fd,
                name,
                authority.directory_fd,
                purge_name,
            )
            os.fsync(authority.directory_fd)
            _after_core_release_tombstone_window(authority, "cleanup-authority-quarantined")
        current = os.stat(purge_name, dir_fd=authority.directory_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != file_identity:
            raise RuntimeError("Core release cleanup authority replacement was preserved")
        _remove_core_release_fd_bound_entry(
            authority.directory_fd,
            purge_name,
            file_fd,
            identity=file_identity,
            is_directory=False,
            subject="Core release cleanup authority",
        )
        os.fsync(authority.directory_fd)
        _after_core_release_tombstone_window(authority, "cleanup-authority-removed")
    finally:
        os.close(file_fd)


def _core_release_parent_bindings(
    authority: _CoreReleaseOutput,
    identity: tuple[int, int],
) -> tuple[str, ...]:
    inventory = _bounded_directory_identity_scan(
        authority.parent_fd,
        limit=_MAX_CORE_RELEASE_PARENT_RECOVERY_MEMBERS,
        container="Core release output parent",
    )
    return tuple(name for name, device, inode in inventory if (device, inode) == identity)


def _core_release_output_bindings(
    authority: _CoreReleaseOutput,
    identity: tuple[int, int],
) -> tuple[str, ...]:
    inventory = _bounded_directory_identity_scan(
        authority.directory_fd,
        limit=_MAX_CORE_RELEASE_ROOT_MEMBERS,
        container="Core wheel output",
    )
    return tuple(name for name, device, inode in inventory if (device, inode) == identity)


def _after_core_release_cleanup_identity_verified(
    source_fd: int,
    source_name: str,
) -> None:
    del source_fd, source_name


def _quarantine_regular_path(
    source_fd: int,
    source_name: str,
    destination_fd: int,
    destination_name: str,
    *,
    identity: tuple[int, int],
    modes: frozenset[int],
) -> None:
    file_fd = os.open(
        source_name,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        dir_fd=source_fd,
    )
    try:
        descriptor = os.fstat(file_fd)
        if (
            not stat.S_ISREG(descriptor.st_mode)
            or descriptor.st_uid != os.geteuid()
            or stat.S_IMODE(descriptor.st_mode) not in modes
            or (descriptor.st_dev, descriptor.st_ino) != identity
        ):
            raise RuntimeError(f"Core release cleanup identity changed: {source_name}")
        _require_fd_acl_free(file_fd, name=source_name)
        descriptor = os.fstat(file_fd)
        if (descriptor.st_dev, descriptor.st_ino) != identity:
            raise RuntimeError(f"Core release cleanup identity changed: {source_name}")
        current = os.stat(source_name, dir_fd=source_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != identity:
            raise RuntimeError(f"Core release cleanup pathname changed: {source_name}")
        _after_core_release_cleanup_identity_verified(source_fd, source_name)
        _rename_noreplace(source_fd, source_name, destination_fd, destination_name)
        quarantined = os.stat(destination_name, dir_fd=destination_fd, follow_symlinks=False)
        if (quarantined.st_dev, quarantined.st_ino) != identity:
            raise RuntimeError(
                f"Core release cleanup replacement was preserved: {destination_name}"
            )
        held = os.fstat(file_fd)
        if (held.st_dev, held.st_ino) != identity:
            raise RuntimeError(f"Core release cleanup held identity changed: {source_name}")
    finally:
        os.close(file_fd)


def _open_core_release_tombstone(
    authority: _CoreReleaseOutput,
    name: str,
    identity: tuple[int, int],
) -> tuple[int, os.stat_result]:
    directory_fd = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        dir_fd=authority.parent_fd,
    )
    try:
        descriptor = os.fstat(directory_fd)
        _require_private_directory(descriptor, name="transaction tombstone")
        if stat.S_IMODE(descriptor.st_mode) != 0o700:
            raise RuntimeError("Core release transaction tombstone mode changed")
        _require_fd_acl_free(directory_fd, name="Core release transaction tombstone")
        current = os.stat(name, dir_fd=authority.parent_fd, follow_symlinks=False)
        if (descriptor.st_dev, descriptor.st_ino) != identity or (
            current.st_dev,
            current.st_ino,
        ) != identity:
            raise RuntimeError("Core release transaction tombstone pathname changed")
        return directory_fd, descriptor
    except BaseException:
        os.close(directory_fd)
        raise


def _purge_core_release_regular_entry(
    authority: _CoreReleaseOutput,
    directory_fd: int,
    name: str,
    *,
    identity: tuple[int, int],
    modes: frozenset[int],
    clear_payload: bool,
) -> None:
    purge_name = _core_release_entry_purge_name(identity)
    source_name = name
    if name != purge_name:
        file_fd = os.open(
            name,
            os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
        try:
            descriptor = os.fstat(file_fd)
            _require_member_attributes(
                descriptor,
                name=name,
                mode=stat.S_IMODE(descriptor.st_mode),
                identity=identity,
            )
            if stat.S_IMODE(descriptor.st_mode) not in modes:
                raise RuntimeError(f"Core release tombstone entry mode changed: {name}")
            _require_fd_acl_free(file_fd, name=name)
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != identity:
                raise RuntimeError(f"Core release tombstone entry pathname changed: {name}")
            _rename_noreplace(directory_fd, name, directory_fd, purge_name)
            os.fsync(directory_fd)
            _after_core_release_tombstone_window(authority, "entry-quarantined")
        except BaseException:
            os.close(file_fd)
            raise
    else:
        file_fd = os.open(
            purge_name,
            os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
    try:
        descriptor = os.fstat(file_fd)
        _require_member_attributes(
            descriptor,
            name=source_name,
            mode=stat.S_IMODE(descriptor.st_mode),
            identity=identity,
        )
        if stat.S_IMODE(descriptor.st_mode) not in modes:
            raise RuntimeError(f"Core release tombstone entry mode changed: {source_name}")
        _require_fd_acl_free(file_fd, name=source_name)
        current = os.stat(purge_name, dir_fd=directory_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != identity:
            raise RuntimeError(
                f"Core release tombstone entry replacement was preserved: {source_name}"
            )
        if clear_payload and descriptor.st_size:
            os.ftruncate(file_fd, 0)
            os.fsync(file_fd)
        _after_core_release_tombstone_window(authority, "entry-cleared")
        current = os.stat(purge_name, dir_fd=directory_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != identity:
            raise RuntimeError(
                f"Core release tombstone entry replacement was preserved: {source_name}"
            )
        _remove_core_release_fd_bound_entry(
            directory_fd,
            purge_name,
            file_fd,
            identity=identity,
            is_directory=False,
            subject=f"Core release tombstone entry {source_name}",
        )
        os.fsync(directory_fd)
        _after_core_release_tombstone_window(authority, "entry-removed")
    finally:
        os.close(file_fd)


def _remove_empty_core_release_tombstone(
    authority: _CoreReleaseOutput,
    directory_fd: int,
    name: str,
    *,
    identity: tuple[int, int],
    already_quarantined: bool,
) -> None:
    if _bounded_listdir(
        directory_fd,
        limit=_MAX_CORE_RELEASE_TRANSACTION_MEMBERS,
        container="Core release transaction tombstone",
    ):
        raise RuntimeError("Core release transaction tombstone is not empty")
    purge_name = _core_release_directory_purge_name(authority)
    if not already_quarantined:
        current = os.stat(name, dir_fd=authority.parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != identity:
            raise RuntimeError("Core release transaction tombstone identity changed")
        _rename_noreplace(authority.parent_fd, name, authority.parent_fd, purge_name)
        os.fsync(authority.parent_fd)
        _after_core_release_tombstone_window(authority, "directory-quarantined")
    current = os.stat(purge_name, dir_fd=authority.parent_fd, follow_symlinks=False)
    held = os.fstat(directory_fd)
    if (
        (current.st_dev, current.st_ino) != identity
        or (held.st_dev, held.st_ino) != identity
        or not stat.S_ISDIR(current.st_mode)
        or current.st_uid != os.geteuid()
        or stat.S_IMODE(current.st_mode) != 0o700
    ):
        raise RuntimeError("Core release transaction purge replacement was preserved")
    _require_fd_acl_free(directory_fd, name="Core release transaction purge")
    if _bounded_listdir(
        directory_fd,
        limit=0,
        container="Core release transaction purge",
    ):
        raise RuntimeError("Core release transaction purge changed before removal")
    _remove_core_release_fd_bound_entry(
        authority.parent_fd,
        purge_name,
        directory_fd,
        identity=identity,
        is_directory=True,
        subject="Core release transaction purge",
    )
    os.fsync(authority.parent_fd)
    _after_core_release_tombstone_window(authority, "directory-removed")


def _purge_core_release_tombstone(
    authority: _CoreReleaseOutput,
    directory_fd: int,
    name: str,
    *,
    identity: tuple[int, int],
) -> None:
    names = _bounded_listdir(
        directory_fd,
        limit=_MAX_CORE_RELEASE_TRANSACTION_MEMBERS,
        container="Core release transaction tombstone",
    )
    if not names:
        _remove_empty_core_release_tombstone(
            authority,
            directory_fd,
            name,
            identity=identity,
            already_quarantined=False,
        )
        return
    purge_names = [item for item in names if _core_release_entry_purge_identity(item) is not None]
    if CORE_RELEASE_TRANSACTION_MARKER not in names:
        if len(names) != 1 or len(purge_names) != 1:
            raise RuntimeError("Core release tombstone lost its authoritative marker")
        purge_identity = _core_release_entry_purge_identity(purge_names[0])
        assert purge_identity is not None
        _purge_core_release_regular_entry(
            authority,
            directory_fd,
            purge_names[0],
            identity=purge_identity,
            modes=frozenset({0o600, 0o644}),
            clear_payload=True,
        )
    else:
        marker_payload, marker_identity = _read_marker(
            directory_fd,
            CORE_RELEASE_TRANSACTION_MARKER,
        )
        try:
            _adopt_marker_intents(authority, marker_payload)
            phase, identities, cleanup_index = _validate_marker_payload(
                authority,
                marker_payload,
                transaction_identity=identity,
            )
        except RuntimeError:
            if names != (CORE_RELEASE_TRANSACTION_MARKER,):
                raise
            _purge_core_release_regular_entry(
                authority,
                directory_fd,
                CORE_RELEASE_TRANSACTION_MARKER,
                identity=marker_identity,
                modes=frozenset({0o600}),
                clear_payload=False,
            )
        else:
            if phase == "cleaning" and cleanup_index != len(authority.sources):
                raise RuntimeError("Core release tombstone cleanup is incomplete")
            if phase not in {"ready", "cleaning"}:
                raise RuntimeError("Core release tombstone phase is invalid")
            _verify_retired_markers(authority, directory_fd, identity, names)
            allowed_member_names: dict[str, _CoreReleaseSource] = {}
            for source in authority.sources:
                staging_name = authority.member_intents[source.name]
                allowed_member_names[staging_name] = source
                allowed_member_names[staging_name.replace(".member-", ".root-", 1)] = source
            retired_names = {item for item in names if _retired_marker_identity(item) is not None}
            ordinary_names = [
                item
                for item in names
                if item != CORE_RELEASE_TRANSACTION_MARKER
                and item not in retired_names
                and item not in purge_names
            ]
            if any(item not in allowed_member_names for item in ordinary_names):
                raise RuntimeError("Core release tombstone contains an unknown entry")
            seen_sources: set[str] = set()
            for item in ordinary_names:
                source = allowed_member_names[item]
                if source.name in seen_sources:
                    raise RuntimeError("Core release tombstone duplicates a member")
                seen_sources.add(source.name)
                descriptor = os.stat(item, dir_fd=directory_fd, follow_symlinks=False)
                observed_identity = (descriptor.st_dev, descriptor.st_ino)
                expected_identity = identities.get(source.name)
                if expected_identity is not None and observed_identity != expected_identity:
                    raise RuntimeError("Core release tombstone member identity changed")
            if len(purge_names) > 1:
                raise RuntimeError("Core release tombstone has ambiguous purge state")
            for item in purge_names:
                purge_identity = _core_release_entry_purge_identity(item)
                assert purge_identity is not None
                _purge_core_release_regular_entry(
                    authority,
                    directory_fd,
                    item,
                    identity=purge_identity,
                    modes=frozenset({0o600, 0o644}),
                    clear_payload=True,
                )
            for item in ordinary_names:
                source = allowed_member_names[item]
                descriptor = os.stat(item, dir_fd=directory_fd, follow_symlinks=False)
                _purge_core_release_regular_entry(
                    authority,
                    directory_fd,
                    item,
                    identity=(descriptor.st_dev, descriptor.st_ino),
                    modes=frozenset({0o600, 0o644}),
                    clear_payload=True,
                )
            for item in sorted(retired_names):
                retired_identity = _retired_marker_identity(item)
                assert retired_identity is not None
                _purge_core_release_regular_entry(
                    authority,
                    directory_fd,
                    item,
                    identity=retired_identity,
                    modes=frozenset({0o600}),
                    clear_payload=False,
                )
            _purge_core_release_regular_entry(
                authority,
                directory_fd,
                CORE_RELEASE_TRANSACTION_MARKER,
                identity=marker_identity,
                modes=frozenset({0o600}),
                clear_payload=False,
            )
    os.fsync(directory_fd)
    _after_core_release_tombstone_window(authority, "tombstone-empty")
    _remove_empty_core_release_tombstone(
        authority,
        directory_fd,
        name,
        identity=identity,
        already_quarantined=False,
    )


def _retire_transaction_directory(
    authority: _CoreReleaseOutput,
    transaction_fd: int,
    name: str,
    *,
    identity: tuple[int, int],
) -> None:
    held = os.fstat(transaction_fd)
    _require_fd_acl_free(transaction_fd, name="Core release transaction")
    current = os.stat(name, dir_fd=authority.directory_fd, follow_symlinks=False)
    if (
        not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != identity
        or (held.st_dev, held.st_ino) != identity
        or current.st_uid != os.geteuid()
        or stat.S_IMODE(current.st_mode) != 0o700
    ):
        raise RuntimeError("Core release transaction directory changed before cleanup")
    cleanup_authority = _publish_core_release_cleanup_authority(authority, identity)
    _after_core_release_cleanup_identity_verified(authority.directory_fd, name)
    tombstone = _core_release_tombstone_name(authority)
    try:
        _rename_noreplace(authority.directory_fd, name, authority.parent_fd, tombstone)
    except FileExistsError as exc:
        raise RuntimeError("Core release cleanup has an unrecovered tombstone") from exc
    moved = os.stat(tombstone, dir_fd=authority.parent_fd, follow_symlinks=False)
    if (moved.st_dev, moved.st_ino) != identity:
        raise RuntimeError("Core release transaction replacement was preserved as a tombstone")
    try:
        replacement = os.stat(name, dir_fd=authority.directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        replacement = None
    if replacement is not None:
        raise RuntimeError(
            "Core release transaction replacement was preserved at its original name"
        )
    os.fsync(authority.parent_fd)
    os.fsync(authority.directory_fd)
    _after_core_release_tombstone_window(authority, "transaction-retired")
    _purge_core_release_tombstone(
        authority,
        transaction_fd,
        tombstone,
        identity=identity,
    )
    _discard_core_release_cleanup_authority(authority, *cleanup_authority)


def _recover_core_release_tombstone(authority: _CoreReleaseOutput) -> None:
    tombstone = _core_release_tombstone_name(authority)
    purge = _core_release_directory_purge_name(authority)
    present: list[tuple[str, tuple[int, int]]] = []
    for name in (tombstone, purge):
        try:
            descriptor = os.stat(name, dir_fd=authority.parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        present.append((name, (descriptor.st_dev, descriptor.st_ino)))
    if len(present) != 1:
        if present:
            raise RuntimeError("Core release cleanup has ambiguous sibling state")
    cleanup_authority = _recover_core_release_cleanup_authority(authority)
    if cleanup_authority is None:
        if present:
            raise RuntimeError("Core release cleanup sibling has no durable authority")
        return
    authority_name, cleanup_identity, authority_identity = cleanup_authority
    if not present:
        output_bindings = _core_release_output_bindings(authority, cleanup_identity)
        if output_bindings:
            _discard_core_release_cleanup_authority(
                authority,
                authority_name,
                cleanup_identity,
                authority_identity,
            )
            authority.initial_inventory = _classify_core_release_inventory(authority.directory_fd)
            authority.require_bound_path()
            return
        bindings = _core_release_parent_bindings(authority, cleanup_identity)
        if bindings:
            raise RuntimeError(
                "Core release cleanup directory was renamed and preserved: " + ", ".join(bindings)
            )
        _discard_core_release_cleanup_authority(
            authority,
            authority_name,
            cleanup_identity,
            authority_identity,
        )
        authority.initial_inventory = _classify_core_release_inventory(authority.directory_fd)
        authority.require_bound_path()
        return
    name, observed_identity = present[0]
    if observed_identity != cleanup_identity:
        bindings = _core_release_parent_bindings(authority, cleanup_identity)
        detail = f"; authorized inode remains at {', '.join(bindings)}" if bindings else ""
        raise RuntimeError("Core release cleanup directory replacement was preserved" + detail)
    directory_fd, _ = _open_core_release_tombstone(authority, name, cleanup_identity)
    try:
        if name == purge:
            _remove_empty_core_release_tombstone(
                authority,
                directory_fd,
                name,
                identity=cleanup_identity,
                already_quarantined=True,
            )
        else:
            _purge_core_release_tombstone(
                authority,
                directory_fd,
                name,
                identity=cleanup_identity,
            )
    finally:
        os.close(directory_fd)
    bindings = _core_release_parent_bindings(authority, cleanup_identity)
    if bindings:
        raise RuntimeError(
            "Core release cleanup directory remained bound after recovery: " + ", ".join(bindings)
        )
    _discard_core_release_cleanup_authority(
        authority,
        authority_name,
        cleanup_identity,
        authority_identity,
    )
    authority.initial_inventory = _classify_core_release_inventory(authority.directory_fd)
    authority.require_bound_path()


def _decode_marker(payload: bytes) -> dict[str, object]:
    try:
        marker = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Core release transaction marker is invalid") from exc
    if not isinstance(marker, dict):
        raise RuntimeError("Core release transaction marker is invalid")
    return marker


def _adopt_marker_intents(authority: _CoreReleaseOutput, payload: bytes) -> None:
    marker = _decode_marker(payload)
    raw_members = marker.get("members")
    if not isinstance(raw_members, list) or len(raw_members) != len(authority.sources):
        raise RuntimeError("Core release marker member intents are invalid")
    intents: dict[str, str] = {}
    for source, entry in zip(authority.sources, raw_members, strict=True):
        if not isinstance(entry, dict) or entry.get("name") != source.name:
            raise RuntimeError("Core release marker member intents are invalid")
        staging_name = entry.get("staging_name")
        if (
            not isinstance(staging_name, str)
            or _CORE_RELEASE_STAGING_PATTERN.fullmatch(staging_name) is None
            or staging_name in intents.values()
        ):
            raise RuntimeError("Core release marker member intents are invalid")
        intents[source.name] = staging_name
    if authority.member_intents and authority.member_intents != intents:
        raise RuntimeError("Core release marker member intents changed")
    authority.member_intents = intents


def _bound_marker_state(
    authority: _CoreReleaseOutput,
    marker: dict[str, object],
    *,
    transaction_identity: tuple[int, int],
) -> tuple[dict[str, tuple[int, int]], int]:
    phase = marker.get("phase")
    if phase not in {"preparing", "ready", "cleaning"}:
        raise RuntimeError("Core release bound marker phase is invalid")
    raw_members = marker.get("members")
    if not isinstance(raw_members, list) or len(raw_members) != len(authority.sources):
        raise RuntimeError("Core release bound marker member inventory is invalid")
    identities: dict[str, tuple[int, int]] = {}
    saw_unbound = False
    for source, entry in zip(authority.sources, raw_members, strict=True):
        if not isinstance(entry, dict):
            raise RuntimeError("Core release bound marker member is invalid")
        keys = frozenset(entry)
        base_keys = frozenset({"name", "staging_name", "byte_size", "sha256"})
        bound_keys = frozenset({*base_keys, "device", "inode"})
        if phase == "ready" and keys != bound_keys:
            raise RuntimeError("Core release ready marker member is invalid")
        if phase == "cleaning" and keys not in {base_keys, bound_keys}:
            raise RuntimeError("Core release cleaning marker member is invalid")
        if entry.get("name") != source.name:
            raise RuntimeError("Core release bound marker member name is invalid")
        staging_name = entry.get("staging_name")
        if (
            not isinstance(staging_name, str)
            or _CORE_RELEASE_STAGING_PATTERN.fullmatch(staging_name) is None
            or authority.member_intents.get(source.name) != staging_name
        ):
            raise RuntimeError("Core release member staging intent is invalid")
        if keys == bound_keys:
            if saw_unbound:
                raise RuntimeError("Core release cleaning marker identities are not a prefix")
            device = entry.get("device")
            inode = entry.get("inode")
            if type(device) is not int or type(inode) is not int or device < 0 or inode <= 0:
                raise RuntimeError("Core release bound marker identity is invalid")
            identities[source.name] = (device, inode)
        else:
            saw_unbound = True
    cleanup_index = 0
    if phase == "cleaning":
        raw_cleanup_index = marker.get("cleanup_index")
        if (
            type(raw_cleanup_index) is not int
            or raw_cleanup_index < 0
            or raw_cleanup_index > len(authority.sources)
        ):
            raise RuntimeError("Core release cleaning marker progress is invalid")
        cleanup_index = raw_cleanup_index
    expected = _marker_bytes(
        authority,
        phase=phase,
        transaction_device=transaction_identity[0],
        transaction_inode=transaction_identity[1],
        member_identities=identities,
        cleanup_index=cleanup_index if phase == "cleaning" else None,
    )
    if marker != _decode_marker(expected):
        raise RuntimeError(f"Core release {phase} marker does not match current inputs")
    return identities, cleanup_index


def _replace_transaction_marker(
    authority: _CoreReleaseOutput,
    transaction_fd: int,
    payload: bytes,
    *,
    current_identity: tuple[int, int],
) -> tuple[int, int]:
    actual_payload, actual_identity = _read_marker(
        transaction_fd,
        CORE_RELEASE_TRANSACTION_MARKER,
    )
    del actual_payload
    if actual_identity != current_identity:
        raise RuntimeError("Core release marker identity changed before replacement")
    candidate_fd = -1
    candidate_identity: tuple[int, int] | None = None
    try:
        candidate_fd, candidate_identity = _write_bound_member(
            transaction_fd,
            CORE_RELEASE_TRANSACTION_READY,
            payload,
            mode=0o600,
        )
        os.fsync(transaction_fd)
        _after_core_release_marker_window(authority, payload, "candidate-durable")
        published_identity = _promote_transaction_marker_candidate(
            authority,
            transaction_fd,
            payload,
            current_identity=current_identity,
            candidate_identity=candidate_identity,
        )
        if (
            authority.transaction_fd == transaction_fd
            and authority.marker_identity == current_identity
        ):
            if authority.marker_fd < 0:
                raise RuntimeError("Core release marker authority was not held")
            retired = os.stat(
                _retired_marker_name(current_identity),
                dir_fd=transaction_fd,
                follow_symlinks=False,
            )
            if (retired.st_dev, retired.st_ino) != current_identity:
                raise RuntimeError("Core release retired marker identity changed")
            os.close(authority.marker_fd)
            authority.marker_fd = candidate_fd
            authority.marker_identity = published_identity
            candidate_fd = -1
        return published_identity
    finally:
        if candidate_fd >= 0:
            os.close(candidate_fd)


def _after_core_release_marker_identity_verified(
    authority: _CoreReleaseOutput,
    payload: bytes,
    identity: tuple[int, int],
) -> None:
    del authority, payload, identity


def _promote_transaction_marker_candidate(
    authority: _CoreReleaseOutput,
    transaction_fd: int,
    payload: bytes,
    *,
    current_identity: tuple[int, int],
    candidate_identity: tuple[int, int],
) -> tuple[int, int]:
    current = os.stat(
        CORE_RELEASE_TRANSACTION_MARKER,
        dir_fd=transaction_fd,
        follow_symlinks=False,
    )
    candidate = os.stat(
        CORE_RELEASE_TRANSACTION_READY,
        dir_fd=transaction_fd,
        follow_symlinks=False,
    )
    if (current.st_dev, current.st_ino) != current_identity:
        raise RuntimeError("Core release marker pathname changed before quarantine")
    if (candidate.st_dev, candidate.st_ino) != candidate_identity:
        raise RuntimeError("Core release marker candidate pathname changed")
    _after_core_release_marker_identity_verified(authority, payload, current_identity)
    retired_name = _retired_marker_name(current_identity)
    _rename_noreplace(
        transaction_fd,
        CORE_RELEASE_TRANSACTION_MARKER,
        transaction_fd,
        retired_name,
    )
    retired = os.stat(retired_name, dir_fd=transaction_fd, follow_symlinks=False)
    if (retired.st_dev, retired.st_ino) != current_identity:
        raise RuntimeError("Core release marker replacement was preserved in quarantine")
    _after_core_release_marker_window(authority, payload, "marker-quarantined")
    try:
        _rename_noreplace(
            transaction_fd,
            CORE_RELEASE_TRANSACTION_READY,
            transaction_fd,
            CORE_RELEASE_TRANSACTION_MARKER,
        )
    except FileExistsError as exc:
        raise RuntimeError("Core release marker replacement was preserved at publication") from exc
    _after_core_release_marker_window(authority, payload, "marker-replaced")
    published_payload, published_identity = _read_marker(
        transaction_fd,
        CORE_RELEASE_TRANSACTION_MARKER,
    )
    if published_payload != payload or published_identity != candidate_identity:
        raise RuntimeError("Core release replacement marker changed")
    os.fsync(transaction_fd)
    _after_core_release_marker_window(authority, payload, "marker-durable")
    return published_identity


def _after_core_release_marker_window(
    authority: _CoreReleaseOutput,
    payload: bytes,
    window: str,
) -> None:
    del authority, payload, window


def _validate_marker_payload(
    authority: _CoreReleaseOutput,
    payload: bytes,
    *,
    transaction_identity: tuple[int, int],
) -> tuple[str, dict[str, tuple[int, int]], int]:
    marker = _decode_marker(payload)
    phase = marker.get("phase")
    if phase == "preparing":
        identities, cleanup_index = _bound_marker_state(
            authority,
            marker,
            transaction_identity=transaction_identity,
        )
        return phase, identities, cleanup_index
    if phase in {"ready", "cleaning"}:
        identities, cleanup_index = _bound_marker_state(
            authority,
            marker,
            transaction_identity=transaction_identity,
        )
        return phase, identities, cleanup_index
    raise RuntimeError("Core release transaction marker phase is invalid")


def _is_valid_marker_transition(
    authority: _CoreReleaseOutput,
    current_payload: bytes,
    candidate_payload: bytes,
    *,
    transaction_identity: tuple[int, int],
) -> bool:
    phase, identities, cleanup_index = _validate_marker_payload(
        authority,
        current_payload,
        transaction_identity=transaction_identity,
    )
    candidate_phase, candidate_identities, candidate_cleanup_index = _validate_marker_payload(
        authority,
        candidate_payload,
        transaction_identity=transaction_identity,
    )
    if phase == "preparing" and candidate_phase == "preparing":
        current_items = tuple(identities.items())
        candidate_items = tuple(candidate_identities.items())
        return (
            len(candidate_items) == len(current_items) + 1
            and candidate_items[: len(current_items)] == current_items
        )
    if phase == "preparing" and candidate_phase == "ready":
        return len(candidate_identities) == len(authority.sources) and tuple(
            candidate_identities.items()
        )[: len(identities)] == tuple(identities.items())
    if phase == "preparing" and candidate_phase == "cleaning":
        return candidate_identities == identities and candidate_cleanup_index == min(
            1, len(authority.sources)
        )
    if phase == "ready" and candidate_phase == "cleaning":
        return candidate_identities == identities and candidate_cleanup_index == 1
    if phase == "cleaning" and candidate_phase == "cleaning":
        return candidate_identities == identities and candidate_cleanup_index == cleanup_index + 1
    return False


def _publish_recovered_marker_candidate(
    authority: _CoreReleaseOutput,
    transaction_fd: int,
    candidate_payload: bytes,
    candidate_identity: tuple[int, int],
) -> tuple[bytes, tuple[int, int]]:
    try:
        _rename_noreplace(
            transaction_fd,
            CORE_RELEASE_TRANSACTION_READY,
            transaction_fd,
            CORE_RELEASE_TRANSACTION_MARKER,
        )
    except FileExistsError as exc:
        raise RuntimeError("Core release recovery preserved a replacement marker") from exc
    os.fsync(transaction_fd)
    recovered_payload, recovered_identity = _read_marker(
        transaction_fd,
        CORE_RELEASE_TRANSACTION_MARKER,
    )
    if recovered_payload != candidate_payload or recovered_identity != candidate_identity:
        raise RuntimeError("Core release pending marker replacement changed")
    return recovered_payload, recovered_identity


def _verify_retired_markers(
    authority: _CoreReleaseOutput,
    transaction_fd: int,
    transaction_identity: tuple[int, int],
    transaction_names: tuple[str, ...],
) -> None:
    for name in transaction_names:
        expected_identity = _retired_marker_identity(name)
        if expected_identity is None:
            continue
        payload, identity = _read_marker(transaction_fd, name)
        if identity != expected_identity:
            raise RuntimeError("Core release retired marker replacement was preserved")
        _adopt_marker_intents(authority, payload)
        _validate_marker_payload(
            authority,
            payload,
            transaction_identity=transaction_identity,
        )


def _recover_pending_marker_replacement(
    authority: _CoreReleaseOutput,
    transaction_fd: int,
    transaction_identity: tuple[int, int],
    transaction_names: tuple[str, ...],
) -> tuple[bytes, tuple[int, int]]:
    _verify_retired_markers(
        authority,
        transaction_fd,
        transaction_identity,
        transaction_names,
    )
    has_marker = CORE_RELEASE_TRANSACTION_MARKER in transaction_names
    has_candidate = CORE_RELEASE_TRANSACTION_READY in transaction_names
    if not has_marker:
        if not has_candidate:
            raise RuntimeError("Core release transaction has no active marker")
        candidate_payload, candidate_identity = _read_marker(
            transaction_fd,
            CORE_RELEASE_TRANSACTION_READY,
        )
        _adopt_marker_intents(authority, candidate_payload)
        predecessors: list[tuple[bytes, tuple[int, int]]] = []
        for name in transaction_names:
            expected_identity = _retired_marker_identity(name)
            if expected_identity is None:
                continue
            retired_payload, retired_identity = _read_marker(transaction_fd, name)
            if retired_identity != expected_identity:
                raise RuntimeError("Core release retired marker replacement was preserved")
            if _is_valid_marker_transition(
                authority,
                retired_payload,
                candidate_payload,
                transaction_identity=transaction_identity,
            ):
                predecessors.append((retired_payload, retired_identity))
        if len(predecessors) != 1:
            raise RuntimeError("Core release pending marker has no unique inode-bound predecessor")
        return _publish_recovered_marker_candidate(
            authority,
            transaction_fd,
            candidate_payload,
            candidate_identity,
        )
    marker_payload, marker_identity = _read_marker(
        transaction_fd,
        CORE_RELEASE_TRANSACTION_MARKER,
    )
    try:
        _adopt_marker_intents(authority, marker_payload)
    except RuntimeError:
        if has_candidate:
            raise
        return marker_payload, marker_identity
    if not has_candidate:
        return marker_payload, marker_identity
    candidate_payload, candidate_identity = _read_marker(
        transaction_fd,
        CORE_RELEASE_TRANSACTION_READY,
    )
    if not _is_valid_marker_transition(
        authority,
        marker_payload,
        candidate_payload,
        transaction_identity=transaction_identity,
    ):
        raise RuntimeError("Core release pending marker transition is invalid")
    _promote_transaction_marker_candidate(
        authority,
        transaction_fd,
        candidate_payload,
        current_identity=marker_identity,
        candidate_identity=candidate_identity,
    )
    return _read_marker(
        transaction_fd,
        CORE_RELEASE_TRANSACTION_MARKER,
    )


def _recover_preparing_transaction(
    authority: _CoreReleaseOutput,
    transaction_fd: int,
    transaction_name: str,
    transaction_identity: tuple[int, int],
    marker_payload: bytes,
    marker_identity: tuple[int, int],
) -> None:
    _cleanup_core_release_transaction(
        authority,
        transaction_fd,
        transaction_name,
        transaction_identity,
        marker_payload,
        marker_identity,
    )


def _after_core_release_member_cleaned(
    authority: _CoreReleaseOutput,
    source: _CoreReleaseSource,
) -> None:
    del authority, source


def _cleanup_core_release_transaction(
    authority: _CoreReleaseOutput,
    transaction_fd: int,
    transaction_name: str,
    transaction_identity: tuple[int, int],
    marker_payload: bytes,
    marker_identity: tuple[int, int],
) -> None:
    _, identities, cleanup_index = _validate_marker_payload(
        authority,
        marker_payload,
        transaction_identity=transaction_identity,
    )
    root_names = _bounded_listdir(
        authority.directory_fd,
        limit=_MAX_CORE_RELEASE_ROOT_MEMBERS,
        container="Core wheel output",
    )
    transaction_names = _bounded_listdir(
        transaction_fd,
        limit=_MAX_CORE_RELEASE_TRANSACTION_MEMBERS,
        container="Core release transaction",
    )
    cleanup_sources = list(authority.sources)
    if list(identities) != [source.name for source in cleanup_sources[: len(identities)]]:
        raise RuntimeError("Core release cleanup identity order changed")
    ordinary_locations = [
        (authority.directory_fd, name, True) for name in root_names if name != transaction_name
    ]
    ordinary_locations.extend(
        (transaction_fd, name, False)
        for name in transaction_names
        if name != CORE_RELEASE_TRANSACTION_MARKER and _retired_marker_identity(name) is None
    )
    consumed_locations: set[tuple[int, str, bool]] = set()
    member_locations: dict[
        str,
        list[tuple[int, str, tuple[int, int], frozenset[int]]],
    ] = {}
    for index, source in enumerate(cleanup_sources):
        expected_identity = identities.get(source.name)
        staging_name = authority.member_intents[source.name]
        root_quarantine_name = staging_name.replace(".member-", ".root-", 1)
        candidates: list[tuple[int, str, bool]] = []
        for directory_fd, name, at_root in ordinary_locations:
            location = (directory_fd, name, at_root)
            if location in consumed_locations:
                continue
            actual_identity: tuple[int, int] | None = None
            if expected_identity is not None:
                descriptor = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                actual_identity = (descriptor.st_dev, descriptor.st_ino)
            if (
                (expected_identity is not None and actual_identity == expected_identity)
                or (at_root and name == source.name)
                or (not at_root and name in {staging_name, root_quarantine_name})
            ):
                candidates.append(location)
                consumed_locations.add(location)
        locations: list[tuple[int, str, tuple[int, int], frozenset[int]]] = []
        expected_links = len(candidates)
        for directory_fd, name, at_root in candidates:
            if expected_identity is None:
                if at_root:
                    raise RuntimeError("Core release unbound intent escaped its transaction")
                file_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                try:
                    descriptor = os.fstat(file_fd)
                    observed_identity = (descriptor.st_dev, descriptor.st_ino)
                    _require_member_attributes(
                        descriptor,
                        name=name,
                        mode=0o600,
                        identity=observed_identity,
                    )
                    _require_fd_acl_free(file_fd, name=name)
                    current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) != observed_identity:
                        raise RuntimeError(f"Core release staging intent changed: {source.name}")
                finally:
                    os.close(file_fd)
                locations.append((directory_fd, name, observed_identity, frozenset({0o600})))
                continue
            descriptor = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (
                not at_root
                and stat.S_ISREG(descriptor.st_mode)
                and stat.S_IMODE(descriptor.st_mode) == 0o600
            ):
                _require_member_attributes(
                    descriptor,
                    name=name,
                    mode=0o600,
                    identity=expected_identity,
                    link_counts=frozenset({expected_links}),
                )
            else:
                _verify_member_path(
                    directory_fd,
                    source,
                    name=name,
                    identity=expected_identity,
                    link_counts=frozenset({expected_links}),
                )
            locations.append((directory_fd, name, expected_identity, frozenset({0o600, 0o644})))
        if not locations and expected_identity is not None:
            held_member = next(
                (
                    member
                    for member in authority.members
                    if member.source.name == source.name
                    and (member.device, member.inode) == expected_identity
                ),
                None,
            )
            if held_member is None or os.fstat(held_member.file_fd).st_nlink != 0:
                raise RuntimeError(f"Core release recovery cannot locate {source.name}")
        member_locations[source.name] = locations
    if len(consumed_locations) != len(ordinary_locations):
        raise RuntimeError("Core release cleanup found an unknown transaction member")
    for index, source in enumerate(cleanup_sources):
        locations = member_locations[source.name]
        if index >= cleanup_index:
            payload = _marker_bytes(
                authority,
                phase="cleaning",
                transaction_device=transaction_identity[0],
                transaction_inode=transaction_identity[1],
                member_identities=identities,
                cleanup_index=index + 1,
            )
            marker_identity = _replace_transaction_marker(
                authority,
                transaction_fd,
                payload,
                current_identity=marker_identity,
            )
            cleanup_index = index + 1
        root_quarantine_name = authority.member_intents[source.name].replace(
            ".member-", ".root-", 1
        )
        for directory_fd, name, location_identity, modes in locations:
            if directory_fd != authority.directory_fd:
                continue
            _quarantine_regular_path(
                authority.directory_fd,
                name,
                transaction_fd,
                root_quarantine_name,
                identity=location_identity,
                modes=modes,
            )
        os.fsync(transaction_fd)
        os.fsync(authority.directory_fd)
        _after_core_release_member_cleaned(authority, source)
    if _bounded_listdir(
        authority.directory_fd,
        limit=_MAX_CORE_RELEASE_ROOT_MEMBERS,
        container="Core wheel output",
    ) != (transaction_name,):
        raise RuntimeError("Core release recovery preserved an identity-mismatched root member")
    if authority.transaction_fd == transaction_fd and authority.marker_identity == marker_identity:
        os.close(authority.marker_fd)
        authority.marker_fd = -1
        authority.marker_identity = None
    os.fsync(transaction_fd)
    _retire_transaction_directory(
        authority,
        transaction_fd,
        transaction_name,
        identity=transaction_identity,
    )
    if authority.transaction_fd == transaction_fd:
        authority.transaction_name = None
    os.fsync(authority.directory_fd)


def _recover_core_release_transaction(authority: _CoreReleaseOutput, name: str) -> None:
    transaction_fd, descriptor = _open_transaction_directory(authority, name)
    transaction_identity = (descriptor.st_dev, descriptor.st_ino)
    try:
        transaction_names = _bounded_listdir(
            transaction_fd,
            limit=_MAX_CORE_RELEASE_TRANSACTION_MEMBERS,
            container="Core release transaction",
        )
        if not transaction_names:
            expected_pair = tuple(sorted(source.name for source in authority.sources))
            root_names = tuple(
                item
                for item in _bounded_listdir(
                    authority.directory_fd,
                    limit=_MAX_CORE_RELEASE_ROOT_MEMBERS,
                    container="Core wheel output",
                )
                if item != name
            )
            if root_names not in {(), expected_pair}:
                raise RuntimeError("Unmarked Core release transaction cannot be recovered")
            _retire_transaction_directory(
                authority,
                transaction_fd,
                name,
                identity=transaction_identity,
            )
            os.fsync(authority.directory_fd)
            authority.initial_inventory = root_names
            return
        marker_payload, marker_identity = _recover_pending_marker_replacement(
            authority,
            transaction_fd,
            transaction_identity,
            transaction_names,
        )
        transaction_names = _bounded_listdir(
            transaction_fd,
            limit=_MAX_CORE_RELEASE_TRANSACTION_MEMBERS,
            container="Core release transaction",
        )
        try:
            marker = _decode_marker(marker_payload)
        except RuntimeError:
            root_names = _bounded_listdir(
                authority.directory_fd,
                limit=_MAX_CORE_RELEASE_ROOT_MEMBERS,
                container="Core wheel output",
            )
            if root_names != (name,) or transaction_names != (CORE_RELEASE_TRANSACTION_MARKER,):
                raise
            _retire_transaction_directory(
                authority,
                transaction_fd,
                name,
                identity=transaction_identity,
            )
            os.fsync(authority.directory_fd)
            authority.initial_inventory = ()
            return
        phase = marker.get("phase")
        if phase == "preparing":
            _recover_preparing_transaction(
                authority,
                transaction_fd,
                name,
                transaction_identity,
                marker_payload,
                marker_identity,
            )
        elif phase in {"ready", "cleaning"}:
            _cleanup_core_release_transaction(
                authority,
                transaction_fd,
                name,
                transaction_identity,
                marker_payload,
                marker_identity,
            )
        else:
            raise RuntimeError("Core release transaction marker phase is invalid")
    finally:
        os.close(transaction_fd)
    authority.initial_inventory = ()


def _verify_preexisting_pair(authority: _CoreReleaseOutput) -> None:
    expected_names = tuple(sorted(source.name for source in authority.sources))
    names = _bounded_listdir(
        authority.directory_fd,
        limit=_MAX_CORE_RELEASE_ROOT_MEMBERS,
        container="Core wheel output",
    )
    if names != expected_names:
        raise RuntimeError("Existing Core release pair does not match current inputs")
    for source in authority.sources:
        _verify_member_path(authority.directory_fd, source)


def _create_core_release_transaction(authority: _CoreReleaseOutput) -> None:
    if _bounded_listdir(
        authority.directory_fd,
        limit=_MAX_CORE_RELEASE_ROOT_MEMBERS,
        container="Core wheel output",
    ):
        raise RuntimeError("Core wheel output is not empty before publication")
    for _ in range(8):
        name = f".openevo-core-release-{secrets.token_hex(16)}"
        try:
            os.mkdir(name, 0o700, dir_fd=authority.directory_fd)
            break
        except FileExistsError:
            continue
    else:
        raise RuntimeError("Could not allocate a private Core release transaction")
    transaction_fd, descriptor = _open_transaction_directory(
        authority,
        name,
        initialize_acl=True,
    )
    authority.transaction_name = name
    authority.transaction_fd = transaction_fd
    authority.member_intents = {
        source.name: f".member-{secrets.token_hex(16)}" for source in authority.sources
    }
    payload = _marker_bytes(
        authority,
        phase="preparing",
        transaction_device=descriptor.st_dev,
        transaction_inode=descriptor.st_ino,
    )
    marker_fd, marker_identity = _write_bound_member(
        transaction_fd,
        CORE_RELEASE_TRANSACTION_MARKER,
        payload,
        mode=0o600,
    )
    authority.marker_fd = marker_fd
    authority.marker_identity = marker_identity
    os.fsync(transaction_fd)
    os.fsync(authority.directory_fd)


def _after_core_release_stage_window(
    authority: _CoreReleaseOutput,
    source: _CoreReleaseSource,
    window: str,
) -> None:
    del authority, source, window


def _publish_preparing_member_identity(
    authority: _CoreReleaseOutput,
    source: _CoreReleaseSource,
    identity: tuple[int, int],
) -> None:
    if authority.marker_identity is None:
        raise RuntimeError("Core release preparing marker is not bound")
    transaction = os.fstat(authority.transaction_fd)
    identities = {
        member.source.name: (member.device, member.inode) for member in authority.members
    }
    identities[source.name] = identity
    payload = _marker_bytes(
        authority,
        phase="preparing",
        transaction_device=transaction.st_dev,
        transaction_inode=transaction.st_ino,
        member_identities=identities,
    )
    _replace_transaction_marker(
        authority,
        authority.transaction_fd,
        payload,
        current_identity=authority.marker_identity,
    )


def _copy_core_release_member(
    authority: _CoreReleaseOutput,
    source: _CoreReleaseSource,
) -> _CoreReleaseMember:
    try:
        staging_name = authority.member_intents[source.name]
    except KeyError as exc:
        raise RuntimeError("Core release member has no durable staging intent") from exc
    _after_core_release_stage_window(authority, source, "intent-durable")
    destination_fd = os.open(
        staging_name,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
        dir_fd=authority.transaction_fd,
    )
    identity: tuple[int, int] | None = None
    try:
        _clear_and_verify_fd_acl(destination_fd, name=staging_name)
        descriptor = os.fstat(destination_fd)
        identity = (descriptor.st_dev, descriptor.st_ino)
        _require_member_attributes(
            descriptor,
            name=staging_name,
            mode=0o600,
            identity=identity,
        )
        current = os.stat(staging_name, dir_fd=authority.transaction_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != identity:
            raise RuntimeError(f"Core release staging intent changed: {source.name}")
        _after_core_release_stage_window(authority, source, "file-created")
        _publish_preparing_member_identity(authority, source, identity)
        _after_core_release_stage_window(authority, source, "inode-bound")
        os.lseek(source.file_fd, 0, os.SEEK_SET)
        with (
            os.fdopen(os.dup(source.file_fd), "rb") as source_file,
            os.fdopen(os.dup(destination_fd), "wb") as destination_file,
        ):
            shutil.copyfileobj(source_file, destination_file)
            destination_file.flush()
        os.fsync(destination_fd)
        _after_core_release_stage_window(authority, source, "bytes-fsynced")
        os.fchmod(destination_fd, 0o644)
        os.fsync(destination_fd)
        _after_core_release_stage_window(authority, source, "mode-fsynced")
        _require_member_attributes(
            os.fstat(destination_fd),
            name=source.name,
            mode=0o644,
            identity=identity,
        )
        byte_size, sha256 = _sha256_fd(destination_fd)
        if byte_size != source.byte_size or sha256 != source.sha256:
            raise RuntimeError(f"Copied Core release member differs from source: {source.name}")
        current = os.stat(staging_name, dir_fd=authority.transaction_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != identity:
            raise RuntimeError(f"Core release transaction member pathname changed: {source.name}")
        return _CoreReleaseMember(
            source=source,
            file_fd=destination_fd,
            device=identity[0],
            inode=identity[1],
        )
    except BaseException:
        os.close(destination_fd)
        raise


def _publish_ready_marker(authority: _CoreReleaseOutput) -> None:
    if authority.marker_identity is None:
        raise RuntimeError("Core release preparing marker is not bound")
    transaction = os.fstat(authority.transaction_fd)
    identities = {
        member.source.name: (member.device, member.inode) for member in authority.members
    }
    payload = _marker_bytes(
        authority,
        phase="ready",
        transaction_device=transaction.st_dev,
        transaction_inode=transaction.st_ino,
        member_identities=identities,
    )
    _replace_transaction_marker(
        authority,
        authority.transaction_fd,
        payload,
        current_identity=authority.marker_identity,
    )


def _after_core_release_member_published(
    authority: _CoreReleaseOutput,
    member: _CoreReleaseMember,
) -> None:
    del authority, member


def _publish_core_release_members(authority: _CoreReleaseOutput) -> None:
    for member in authority.members:
        source = member.source
        staging_name = authority.member_intents[source.name]
        staged_identity = _verify_member_path(
            authority.transaction_fd,
            source,
            name=staging_name,
            identity=(member.device, member.inode),
        )
        try:
            _rename_noreplace(
                authority.transaction_fd,
                staging_name,
                authority.directory_fd,
                source.name,
            )
        except FileExistsError as exc:
            raise RuntimeError(f"Refusing to replace Core release member: {source.name}") from exc
        _verify_member_path(
            authority.directory_fd,
            source,
            identity=staged_identity,
        )
        if os.fstat(member.file_fd).st_nlink != 1:
            raise RuntimeError(f"Core release member link count changed: {source.name}")
        os.fsync(authority.transaction_fd)
        os.fsync(authority.directory_fd)
        _after_core_release_member_published(authority, member)


def _verify_live_transaction(authority: _CoreReleaseOutput) -> None:
    if authority.transaction_name is None or authority.marker_identity is None:
        raise RuntimeError("Core release transaction is incomplete")
    expected_root = tuple(
        sorted((authority.transaction_name, *(source.name for source in authority.sources)))
    )
    if (
        _bounded_listdir(
            authority.directory_fd,
            limit=_MAX_CORE_RELEASE_ROOT_MEMBERS,
            container="Core wheel output",
        )
        != expected_root
    ):
        raise RuntimeError("Core release output inventory changed before commit")
    transaction_names = _bounded_listdir(
        authority.transaction_fd,
        limit=_MAX_CORE_RELEASE_TRANSACTION_MEMBERS,
        container="Core release transaction",
    )
    if CORE_RELEASE_TRANSACTION_MARKER not in transaction_names or any(
        name != CORE_RELEASE_TRANSACTION_MARKER and _retired_marker_identity(name) is None
        for name in transaction_names
    ):
        raise RuntimeError("Core release transaction inventory changed before commit")
    transaction = os.fstat(authority.transaction_fd)
    _require_fd_acl_free(authority.transaction_fd, name="Core release transaction")
    _verify_retired_markers(
        authority,
        authority.transaction_fd,
        (transaction.st_dev, transaction.st_ino),
        transaction_names,
    )
    expected_marker = _marker_bytes(
        authority,
        phase="ready",
        transaction_device=transaction.st_dev,
        transaction_inode=transaction.st_ino,
        member_identities={
            member.source.name: (member.device, member.inode) for member in authority.members
        },
    )
    actual_marker, marker_identity = _read_marker(
        authority.transaction_fd,
        CORE_RELEASE_TRANSACTION_MARKER,
    )
    if actual_marker != expected_marker or marker_identity != authority.marker_identity:
        raise RuntimeError("Core release transaction marker changed before commit")
    for source, member in zip(authority.sources, authority.members, strict=True):
        held = os.fstat(member.file_fd)
        _require_member_attributes(
            held,
            name=source.name,
            mode=0o644,
            identity=(member.device, member.inode),
        )
        _require_fd_acl_free(member.file_fd, name=source.name)
        byte_size, sha256 = _sha256_fd(member.file_fd)
        if byte_size != source.byte_size or sha256 != source.sha256:
            raise RuntimeError(f"Held Core release member changed: {source.name}")
        _verify_member_path(
            authority.directory_fd,
            source,
            identity=(member.device, member.inode),
        )


def _commit_core_release_inputs(authority: _CoreReleaseOutput) -> None:
    if not authority.sources:
        return
    for source in authority.sources:
        _verify_source(source, require_path=False)
    if authority.preexisting:
        _verify_preexisting_pair(authority)
        return
    _verify_live_transaction(authority)
    assert authority.transaction_name is not None
    assert authority.marker_identity is not None
    transaction_identity = (
        os.fstat(authority.transaction_fd).st_dev,
        os.fstat(authority.transaction_fd).st_ino,
    )
    _retire_transaction_directory(
        authority,
        authority.transaction_fd,
        authority.transaction_name,
        identity=transaction_identity,
    )
    os.close(authority.marker_fd)
    authority.marker_fd = -1
    authority.marker_identity = None
    authority.transaction_name = None
    os.fsync(authority.directory_fd)
    _verify_preexisting_pair(authority)


def _rollback_core_release_inputs(authority: _CoreReleaseOutput) -> None:
    if authority.committed or authority.preexisting:
        return
    if authority.transaction_name is None:
        if not authority.members:
            return
        _verify_preexisting_pair(authority)
        for member in authority.members:
            _verify_member_path(
                authority.directory_fd,
                member.source,
                identity=(member.device, member.inode),
            )
        return
    transaction = os.fstat(authority.transaction_fd)
    transaction_identity = (transaction.st_dev, transaction.st_ino)
    transaction_names = _bounded_listdir(
        authority.transaction_fd,
        limit=_MAX_CORE_RELEASE_TRANSACTION_MEMBERS,
        container="Core release transaction",
    )
    marker_payload, marker_identity = _recover_pending_marker_replacement(
        authority,
        authority.transaction_fd,
        transaction_identity,
        transaction_names,
    )
    _cleanup_core_release_transaction(
        authority,
        authority.transaction_fd,
        authority.transaction_name,
        transaction_identity,
        marker_payload,
        marker_identity,
    )
    if _bounded_listdir(
        authority.directory_fd,
        limit=_MAX_CORE_RELEASE_ROOT_MEMBERS,
        container="Core wheel output",
    ):
        raise RuntimeError("Core release rollback did not restore an empty output")


def _export_core_release_inputs(
    authority: _CoreReleaseOutput,
    wheel: Path,
    framework_lock: Path,
) -> None:
    authority.require_bound_path()
    _open_release_sources(authority, wheel, framework_lock)
    for source in authority.sources:
        _verify_source(source, require_path=True)
    _recover_core_release_tombstone(authority)
    transactions = [
        name
        for name in authority.initial_inventory
        if _CORE_RELEASE_TRANSACTION_PATTERN.fullmatch(name)
    ]
    if transactions:
        _recover_core_release_transaction(authority, transactions[0])
    if authority.initial_inventory:
        _verify_preexisting_pair(authority)
        authority.preexisting = True
        return
    _create_core_release_transaction(authority)
    for source in authority.sources:
        authority.members.append(_copy_core_release_member(authority, source))
    _publish_ready_marker(authority)
    _publish_core_release_members(authority)
    for source in authority.sources:
        _verify_source(source, require_path=True)
    _verify_live_transaction(authority)


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
    archive_members = _archive_member_names(executable)
    embedded_release_inputs = sorted(
        name
        for name in archive_members
        if name.startswith(f"{CORE_WHEEL_ARCHIVE_ROOT.as_posix()}/")
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
        if not _core_release_fd_removal_supported():
            raise RuntimeError(
                "Core wheel export requires an identity-bound cleanup primitive; "
                "this platform is unsupported and the output was not modified"
            )
        core_wheel_output_dir = requested_output
    if clean:
        shutil.rmtree(dist_dir, ignore_errors=True)
        shutil.rmtree(build_dir, ignore_errors=True)
    binary_dir.mkdir(parents=True, exist_ok=True)
    target = binary_dir / f"{SIDECAR_NAME}-{_target_triple()}{_platform_extension()}"
    target.unlink(missing_ok=True)

    output_context = (
        _open_core_release_output(core_wheel_output_dir)
        if core_wheel_output_dir is not None
        else nullcontext(None)
    )
    with (
        output_context as core_release_output,
        TemporaryDirectory(prefix="openevo-sidecar-build-") as temporary_dir,
    ):
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

        if core_release_output is not None:
            _export_core_release_inputs(
                core_release_output,
                core_wheel,
                core_framework_lock,
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
