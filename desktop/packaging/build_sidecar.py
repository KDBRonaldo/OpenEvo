#!/usr/bin/env python3
"""Build the bundled OpenEvo Desktop sidecar executable for Tauri."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import ctypes
from email.parser import Parser
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
import zlib

from openevo.evolution.framework.runtime import (
    FrameworkDistributionLock,
    load_framework_distribution_lock,
)
from openevo.runtime.managed import (
    MANAGED_RUNTIME_ARCHIVE_RELEASE,
    verify_managed_runtime_archive,
)

SIDECAR_NAME = "openevo-desktop-sidecar"
CORE_WHEEL_ARCHIVE_ROOT = Path("openevo/wheels")
MANAGED_RUNTIME_ARCHIVE_ROOT = Path("openevo/runtime-assets")
DAEMON_ARCHIVE_ROOT = Path("openevo/daemon")
DAEMON_BUNDLE_BASENAME = "openevo-daemon-linux-x86_64"
DAEMON_MANIFEST_BASENAME = "openevo-daemon-bundle.json"
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
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


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
#define OPENEVO_STARTUP_FAILURE(stage, code) \\
    fprintf(stderr, "OPENEVO_STARTUP_V1 stage=" stage " code=" code "\\n")
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
            OPENEVO_STARTUP_FAILURE("bootloader_resolver", "native_env_invalid");
            return -1;
        }}
#if defined(__linux__)
        if (openevo_native_path != NULL) {{
            OPENEVO_STARTUP_FAILURE("bootloader_resolver", "native_path_unexpected");
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
            OPENEVO_STARTUP_FAILURE("bootloader_resolver", "native_path_invalid");
            return -1;
        }}
        openevo_path_length = strnlen(openevo_native_path, PYI_PATH_MAX);
        if (openevo_path_length == 0 || openevo_path_length >= PYI_PATH_MAX) {{
            OPENEVO_STARTUP_FAILURE("bootloader_resolver", "native_path_length_invalid");
            return -1;
        }}
        if (
            strstr(openevo_native_path, \"//\") != NULL ||
            strstr(openevo_native_path, \"/./\") != NULL ||
            strstr(openevo_native_path, \"/../\") != NULL
        ) {{
            OPENEVO_STARTUP_FAILURE("bootloader_resolver", "native_path_not_canonical");
            return -1;
        }}
        openevo_basename = strrchr(openevo_native_path, '/');
        if (openevo_basename == NULL || strcmp(openevo_basename + 1, \"{NATIVE_EXECUTABLE_BASENAME}\") != 0) {{
            OPENEVO_STARTUP_FAILURE("bootloader_resolver", "native_basename_invalid");
            return -1;
        }}
        for (openevo_index = 0; openevo_index < openevo_path_length; openevo_index++) {{
            unsigned char character = (unsigned char)openevo_native_path[openevo_index];
            if (character < 0x20 || character == 0x7f) {{
                OPENEVO_STARTUP_FAILURE("bootloader_resolver", "native_path_character_invalid");
                return -1;
            }}
        }}
        if (realpath(openevo_native_path, openevo_resolved_path) == NULL) {{
            OPENEVO_STARTUP_FAILURE("bootloader_resolver", "native_path_resolve_failed");
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
            OPENEVO_STARTUP_FAILURE("bootloader_resolver", "native_identity_invalid");
            return -1;
        }}
        if (snprintf(pyi_ctx->executable_filename, PYI_PATH_MAX, \"%s\", openevo_resolved_path) >= PYI_PATH_MAX) {{
            OPENEVO_STARTUP_FAILURE("bootloader_resolver", "resolved_path_length_invalid");
            return -1;
        }}
#else
        OPENEVO_STARTUP_FAILURE("bootloader_resolver", "platform_unsupported");
        return -1;
#endif
        if (pyi_utils_openevo_native_handoff_prepare() != 0) {{
            OPENEVO_STARTUP_FAILURE("bootloader_resolver", "handoff_prepare_failed");
            return -1;
        }}
        return 0;
    }}
    if (openevo_native_listener_fd != NULL || openevo_native_path != NULL) {{
        OPENEVO_STARTUP_FAILURE("bootloader_resolver", "native_env_incomplete");
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
            OPENEVO_STARTUP_FAILURE("bootloader_archive", "native_fd_invalid");
            return -1;
        }}
#if defined(__linux__)
        openevo_archive_path = \"/proc/self/fd/{NATIVE_EXECUTABLE_FD}\";
#elif defined(__APPLE__)
        openevo_archive_path = \"/dev/fd/{NATIVE_EXECUTABLE_FD}\";
#else
        OPENEVO_STARTUP_FAILURE("bootloader_archive", "platform_unsupported");
        return -1;
#endif
        pyi_ctx->archive = pyi_archive_open(openevo_archive_path);
        if (pyi_ctx->archive == NULL) {{
            OPENEVO_STARTUP_FAILURE("bootloader_archive", "archive_open_failed");
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
#if defined(__APPLE__)
    #include <arpa/inet.h> /* ntohl */
    #include <libproc.h> /* proc_pidfdinfo */
    #include <netinet/in.h> /* sockaddr_in, IPPROTO_TCP */
    #include <sys/proc_info.h> /* socket_fdinfo, PROC_PIDFDSOCKETINFO */
#endif
"""
_BOOTLOADER_DARWIN_LIB_NEEDLE = """        ctx.check_cc(lib='m', mandatory=True)
"""
_BOOTLOADER_DARWIN_LIB_REPLACEMENT = """        ctx.check_cc(lib='m', mandatory=True)
        if ctx.env.DEST_OS == 'darwin':
            ctx.check_cc(lib='proc', mandatory=True, uselib_store='PROC')
"""
_BOOTLOADER_PROGRAM_LIBS_NEEDLE = """            'THR',  # may be used on FreBSD
"""
_BOOTLOADER_PROGRAM_LIBS_REPLACEMENT = """            'THR',  # may be used on FreBSD
            'PROC',  # macOS process and descriptor inspection
"""
_BOOTLOADER_NATIVE_HANDOFF_NEEDLE = """/*
 * If the program is activated by a systemd socket, systemd will set
"""
_BOOTLOADER_NATIVE_HANDOFF_REPLACEMENT = """/* OpenEvo native onefile FD handoff. */
#define OPENEVO_NATIVE_LISTENER_FD 3
#define OPENEVO_NATIVE_ARCHIVE_FD 4
#define OPENEVO_NATIVE_GUARD_MIN_FD 64
#define OPENEVO_STARTUP_FAILURE(stage, code) \\
    fprintf(stderr, "OPENEVO_STARTUP_V1 stage=" stage " code=" code "\\n")

static int openevo_listener_guard_fd = -1;
static int openevo_archive_guard_fd = -1;

static int
_pyi_utils_openevo_validate_native_fds(void)
{
    struct stat listener_stat;
    struct stat archive_stat;
#if defined(__APPLE__)
    struct socket_fdinfo listener_info;
    struct sockaddr_in listener_address;
    socklen_t listener_address_size = sizeof(listener_address);
    int listener_info_size;
#else
    int accepting = 0;
    socklen_t accepting_size = sizeof(accepting);
#endif

    if (fstat(OPENEVO_NATIVE_LISTENER_FD, &listener_stat) != 0) {
        OPENEVO_STARTUP_FAILURE("bootloader_handoff", "listener_fstat_failed");
        return -1;
    }
    if (fstat(OPENEVO_NATIVE_ARCHIVE_FD, &archive_stat) != 0) {
        OPENEVO_STARTUP_FAILURE("bootloader_handoff", "archive_fstat_failed");
        return -1;
    }
    if (!S_ISSOCK(listener_stat.st_mode)) {
        OPENEVO_STARTUP_FAILURE("bootloader_handoff", "listener_type_invalid");
        return -1;
    }
    if (!S_ISREG(archive_stat.st_mode)) {
        OPENEVO_STARTUP_FAILURE("bootloader_handoff", "archive_type_invalid");
        return -1;
    }
#if defined(__APPLE__)
    memset(&listener_info, 0, sizeof(listener_info));
    listener_info_size = proc_pidfdinfo(
        getpid(),
        OPENEVO_NATIVE_LISTENER_FD,
        PROC_PIDFDSOCKETINFO,
        &listener_info,
        (int)sizeof(listener_info)
    );
    if (listener_info_size != (int)sizeof(listener_info)) {
        OPENEVO_STARTUP_FAILURE("bootloader_handoff", "listener_info_probe_failed");
        return -1;
    }
    if (
        listener_info.psi.soi_type != SOCK_STREAM ||
        listener_info.psi.soi_family != AF_INET ||
        listener_info.psi.soi_protocol != IPPROTO_TCP ||
        listener_info.psi.soi_kind != SOCKINFO_TCP ||
        (listener_info.psi.soi_options & SO_ACCEPTCONN) == 0
    ) {
        OPENEVO_STARTUP_FAILURE("bootloader_handoff", "listener_identity_invalid");
        return -1;
    }
    memset(&listener_address, 0, sizeof(listener_address));
    if (getsockname(
            OPENEVO_NATIVE_LISTENER_FD,
            (struct sockaddr *)&listener_address,
            &listener_address_size
        ) != 0) {
        OPENEVO_STARTUP_FAILURE("bootloader_handoff", "listener_endpoint_probe_failed");
        return -1;
    }
    if (listener_address_size != sizeof(listener_address)) {
        OPENEVO_STARTUP_FAILURE("bootloader_handoff", "listener_endpoint_size_invalid");
        return -1;
    }
    if (
        listener_address.sin_family != AF_INET ||
        listener_address.sin_port == 0 ||
        ntohl(listener_address.sin_addr.s_addr) != INADDR_LOOPBACK
    ) {
        OPENEVO_STARTUP_FAILURE("bootloader_handoff", "listener_endpoint_invalid");
        return -1;
    }
#else
    if (getsockopt(
            OPENEVO_NATIVE_LISTENER_FD,
            SOL_SOCKET,
            SO_ACCEPTCONN,
            &accepting,
            &accepting_size
        ) != 0) {
        OPENEVO_STARTUP_FAILURE("bootloader_handoff", "listener_accept_probe_failed");
        return -1;
    }
    if (accepting_size != sizeof(accepting)) {
        OPENEVO_STARTUP_FAILURE("bootloader_handoff", "listener_accept_size_invalid");
        return -1;
    }
    if (accepting != 1) {
        OPENEVO_STARTUP_FAILURE("bootloader_handoff", "listener_not_accepting");
        return -1;
    }
#endif
    return 0;
}

static int
_pyi_utils_openevo_clear_cloexec(int fd)
{
    int flags = fcntl(fd, F_GETFD);
    if (flags == -1 || fcntl(fd, F_SETFD, flags & ~FD_CLOEXEC) == -1) {
        OPENEVO_STARTUP_FAILURE("bootloader_restore", "cloexec_clear_failed");
        return -1;
    }
    return 0;
}

int
pyi_utils_openevo_native_handoff_prepare(void)
{
    if (openevo_listener_guard_fd != -1 || openevo_archive_guard_fd != -1) {
        OPENEVO_STARTUP_FAILURE("bootloader_handoff", "guard_state_invalid");
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
        OPENEVO_STARTUP_FAILURE("bootloader_handoff", "listener_guard_failed");
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
        OPENEVO_STARTUP_FAILURE("bootloader_handoff", "archive_guard_failed");
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
        OPENEVO_STARTUP_FAILURE("bootloader_restore", "descriptor_restore_failed");
        return -1;
    }
    return 0;
}

int
pyi_utils_openevo_native_handoff_finish(void)
{
    if (pyi_utils_openevo_native_handoff_restore() != 0) {
        OPENEVO_STARTUP_FAILURE("bootloader_restore", "finish_failed");
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
            OPENEVO_STARTUP_FAILURE("bootloader_exec", "restore_failed");
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
                OPENEVO_STARTUP_FAILURE("bootloader_restart", "restore_failed");
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
        OPENEVO_STARTUP_FAILURE("bootloader_child", "handoff_finish_failed");
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


def _validate_core_wheel(
    wheel: Path | _CoreReleaseInput,
    *,
    name: str,
    version: str,
) -> None:
    wheel_name = wheel.name
    try:
        payload = (
            _read_core_release_input(wheel)
            if isinstance(wheel, _CoreReleaseInput)
            else wheel.read_bytes()
        )
        with ZipFile(BytesIO(payload)) as archive:
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
    except (BadZipFile, OSError) as exc:
        raise RuntimeError(f"failed to read built Core wheel: {wheel_name}") from exc

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
    return _core_framework_lock_bytes_for_identity(
        wheel_filename=wheel.name,
        wheel_digest=_sha256_bytes(wheel_payload),
        version=version,
    )


def _core_framework_lock_bytes_for_identity(
    *,
    wheel_filename: str,
    wheel_digest: str,
    version: str,
) -> bytes:
    try:
        lock = FrameworkDistributionLock(
            distribution_version=version,
            distribution_digest=wheel_digest,
            wheel_filename=wheel_filename,
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


def _archive_member_digest(
    executable: Path,
    member_name: str,
    *,
    expected_size: int,
) -> tuple[int, str]:
    """Hash one direct CArchive member without buffering its decompressed bytes."""

    try:
        from PyInstaller.archive.readers import CArchiveReader
    except ImportError as exc:
        raise RuntimeError("PyInstaller is required to inspect the sidecar archive") from exc
    archive = CArchiveReader(str(executable))
    try:
        with executable.open("rb") as stream:
            cookie_offset = archive._find_magic_pattern(
                stream,
                archive._COOKIE_MAGIC_PATTERN,
            )
            if cookie_offset < 0:
                raise RuntimeError("sidecar archive cookie is unavailable")
            stream.seek(cookie_offset)
            cookie = stream.read(archive._COOKIE_LENGTH)
            if len(cookie) != archive._COOKIE_LENGTH:
                raise RuntimeError("sidecar archive cookie is truncated")
            _, archive_length, toc_offset, toc_length, _, _ = struct.unpack(
                archive._COOKIE_FORMAT,
                cookie,
            )
            archive_end = cookie_offset + archive._COOKIE_LENGTH
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

            selected: tuple[int, int, int, int] | None = None
            position = 0
            while position < len(toc_payload):
                if len(toc_payload) - position < archive._TOC_ENTRY_LENGTH:
                    raise RuntimeError("sidecar archive TOC entry is truncated")
                values = struct.unpack(
                    archive._TOC_ENTRY_FORMAT,
                    toc_payload[position : position + archive._TOC_ENTRY_LENGTH],
                )
                entry_length, offset, compressed_size, size, compressed, _typecode = values
                if entry_length < archive._TOC_ENTRY_LENGTH or position + entry_length > len(
                    toc_payload
                ):
                    raise RuntimeError("sidecar archive TOC entry bounds are invalid")
                encoded_name = toc_payload[
                    position + archive._TOC_ENTRY_LENGTH : position + entry_length
                ]
                name = encoded_name.rstrip(b"\0").decode("utf-8", errors="strict")
                if name.replace("\\", "/") == member_name:
                    if selected is not None:
                        raise RuntimeError("sidecar archive contains a duplicate member")
                    selected = (offset, compressed_size, size, compressed)
                position += entry_length

            if selected is None:
                raise RuntimeError("sidecar archive member is unavailable")
            offset, compressed_size, size, compressed = selected
            if (
                size != expected_size
                or offset < 0
                or compressed_size < 0
                or archive_start + offset + compressed_size > toc_start
                or compressed not in (0, 1)
            ):
                raise RuntimeError("sidecar archive member bounds are invalid")

            stream.seek(archive_start + offset)
            digest = hashlib.sha256()
            observed = 0
            remaining_input = compressed_size
            decompressor = zlib.decompressobj() if compressed else None
            while remaining_input:
                chunk = stream.read(min(1024 * 1024, remaining_input))
                if not chunk:
                    raise RuntimeError("sidecar archive member is truncated")
                remaining_input -= len(chunk)
                output = (
                    chunk
                    if decompressor is None
                    else decompressor.decompress(chunk, expected_size + 1 - observed)
                )
                if decompressor is not None and decompressor.unconsumed_tail:
                    raise RuntimeError("sidecar archive member exceeds its byte budget")
                observed += len(output)
                if observed > expected_size:
                    raise RuntimeError("sidecar archive member exceeds its byte budget")
                digest.update(output)
            if decompressor is not None:
                output = decompressor.flush(expected_size + 1 - observed)
                observed += len(output)
                digest.update(output)
                if decompressor.unused_data or not decompressor.eof or observed > expected_size:
                    raise RuntimeError("sidecar archive member compression is invalid")
    except (AttributeError, OSError, struct.error, UnicodeError, zlib.error) as exc:
        raise RuntimeError("sidecar archive member could not be verified") from exc
    if observed != expected_size:
        raise RuntimeError("sidecar archive member size differs from the release contract")
    return observed, digest.hexdigest()


def _validate_managed_runtime_archive(archive: Path) -> tuple[int, str]:
    """Validate the exact Core-owned archive and return its outer identity."""

    try:
        verify_managed_runtime_archive(
            archive,
            release=MANAGED_RUNTIME_ARCHIVE_RELEASE,
        )
    except (OSError, ValueError) as exc:
        raise RuntimeError("managed runtime archive identity is invalid") from exc
    return (
        MANAGED_RUNTIME_ARCHIVE_RELEASE.byte_size,
        MANAGED_RUNTIME_ARCHIVE_RELEASE.sha256,
    )


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class _CoreReleaseInput:
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

    def close(self) -> None:
        if self.file_fd >= 0:
            os.close(self.file_fd)
            self.file_fd = -1

    def __enter__(self) -> _CoreReleaseInput:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _sha256_fd(file_fd: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    byte_size = 0
    os.lseek(file_fd, 0, os.SEEK_SET)
    while chunk := os.read(file_fd, 1024 * 1024):
        byte_size += len(chunk)
        digest.update(chunk)
    os.lseek(file_fd, 0, os.SEEK_SET)
    return byte_size, digest.hexdigest()


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _is_darwin_system_path_alias(path: Path) -> bool:
    if sys.platform != "darwin" or path not in {
        Path("/etc"),
        Path("/tmp"),
        Path("/var"),
    }:
        return False
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return False
    return resolved in {
        Path("/private/etc"),
        Path("/private/tmp"),
        Path("/private/var"),
    }


def _reject_symlink_path(path: Path, *, allow_darwin_system_aliases: bool = False) -> None:
    for candidate in (path, *path.parents):
        if candidate.is_symlink() and not (
            allow_darwin_system_aliases and _is_darwin_system_path_alias(candidate)
        ):
            raise RuntimeError(
                f"Core wheel output path must not contain a symbolic link: {candidate}"
            )


def _require_private_build_directory(descriptor: os.stat_result, *, name: str) -> None:
    if (
        not stat.S_ISDIR(descriptor.st_mode)
        or descriptor.st_uid != os.geteuid()
        or descriptor.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise RuntimeError(f"Core release {name} owner or private permissions are invalid")


def _require_core_release_output_absent(output_dir: Path) -> Path:
    requested = Path(os.path.abspath(output_dir))
    _reject_symlink_path(requested)
    if requested.name in {"", ".", ".."}:
        raise RuntimeError("Core wheel output must name a child of a trusted parent")
    try:
        requested.lstat()
    except FileNotFoundError:
        pass
    else:
        raise RuntimeError(f"Core wheel output must not already exist: {requested}")
    parent = requested.parent.resolve(strict=True)
    return parent / requested.name


def _open_core_release_input(path: Path, *, name: str) -> _CoreReleaseInput:
    if Path(name).name != name or name in {"", ".", ".."}:
        raise RuntimeError("Core release export filename is invalid")
    try:
        file_fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise RuntimeError(f"Core release input cannot be opened safely: {name}") from exc
    try:
        descriptor = os.fstat(file_fd)
        if (
            not stat.S_ISREG(descriptor.st_mode)
            or descriptor.st_uid != os.geteuid()
            or descriptor.st_nlink != 1
            or descriptor.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise RuntimeError(f"Core release input is not a private regular file: {name}")
        byte_size, digest = _sha256_fd(file_fd)
        if byte_size != descriptor.st_size:
            raise RuntimeError(f"Core release input changed while hashing: {name}")
        return _CoreReleaseInput(
            path=path,
            name=name,
            file_fd=file_fd,
            device=descriptor.st_dev,
            inode=descriptor.st_ino,
            byte_size=byte_size,
            sha256=digest,
        )
    except BaseException:
        os.close(file_fd)
        raise


def _verify_core_release_input(source: _CoreReleaseInput) -> None:
    descriptor = os.fstat(source.file_fd)
    current = os.stat(source.path, follow_symlinks=False)
    if (
        not stat.S_ISREG(descriptor.st_mode)
        or (descriptor.st_dev, descriptor.st_ino) != (source.device, source.inode)
        or (current.st_dev, current.st_ino) != (source.device, source.inode)
        or descriptor.st_size != source.byte_size
        or descriptor.st_nlink != 1
        or current.st_nlink != 1
    ):
        raise RuntimeError(f"Core release input identity changed: {source.name}")
    byte_size, digest = _sha256_fd(source.file_fd)
    if byte_size != source.byte_size or digest != source.sha256:
        raise RuntimeError(f"Core release input content changed: {source.name}")


def _read_core_release_input(source: _CoreReleaseInput) -> bytes:
    _verify_core_release_input(source)
    payload = bytearray()
    offset = 0
    while offset < source.byte_size:
        chunk = os.pread(
            source.file_fd,
            min(1024 * 1024, source.byte_size - offset),
            offset,
        )
        if not chunk:
            raise RuntimeError(f"Core release input ended while reading: {source.name}")
        payload.extend(chunk)
        offset += len(chunk)
    if os.pread(source.file_fd, 1, source.byte_size):
        raise RuntimeError(f"Core release input grew while reading: {source.name}")
    if _sha256_bytes(payload) != source.sha256:
        raise RuntimeError(f"Core release input content changed: {source.name}")
    _verify_core_release_input(source)
    return bytes(payload)


def _core_release_identity(
    source: Path | _CoreReleaseInput,
) -> tuple[str, int, str]:
    if isinstance(source, _CoreReleaseInput):
        _verify_core_release_input(source)
        return source.name, source.byte_size, source.sha256
    payload = source.read_bytes()
    return source.name, len(payload), _sha256_bytes(payload)


def _core_release_payload(source: Path | _CoreReleaseInput) -> bytes:
    if isinstance(source, _CoreReleaseInput):
        return _read_core_release_input(source)
    return source.read_bytes()


def _validate_core_release_input_pair(
    wheel: _CoreReleaseInput,
    framework_lock: _CoreReleaseInput,
) -> None:
    lock = _validated_framework_lock(_read_core_release_input(framework_lock))
    if lock.distribution_digest != wheel.sha256 or lock.wheel_filename != wheel.name:
        raise RuntimeError("Core framework lock does not bind the exported wheel")


def _open_core_release_input_pair(
    wheel: Path,
    framework_lock: Path,
) -> tuple[_CoreReleaseInput, _CoreReleaseInput]:
    wheel = Path(os.path.abspath(wheel))
    framework_lock = Path(os.path.abspath(framework_lock))
    if framework_lock.name != CORE_FRAMEWORK_LOCK_BASENAME:
        raise RuntimeError("Core framework lock input must use the canonical filename")
    wheel_source = _open_core_release_input(wheel, name=wheel.name)
    try:
        lock_source = _open_core_release_input(
            framework_lock,
            name=CORE_FRAMEWORK_LOCK_BASENAME,
        )
    except BaseException:
        wheel_source.close()
        raise
    try:
        _validate_core_release_input_pair(wheel_source, lock_source)
    except BaseException:
        lock_source.close()
        wheel_source.close()
        raise
    return wheel_source, lock_source


def _snapshot_core_release_input(
    source: _CoreReleaseInput,
    destination_dir: Path,
) -> _CoreReleaseInput:
    _verify_core_release_input(source)
    destination_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _reject_symlink_path(destination_dir, allow_darwin_system_aliases=True)
    directory_fd = os.open(
        destination_dir,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    try:
        destination_fd = os.open(
            source.name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        try:
            _copy_core_release_input(source, destination_fd)
            os.fsync(destination_fd)
        finally:
            os.close(destination_fd)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    snapshot = _open_core_release_input(destination_dir / source.name, name=source.name)
    try:
        _verify_core_release_input(source)
        if snapshot.byte_size != source.byte_size or snapshot.sha256 != source.sha256:
            raise RuntimeError(f"Core release snapshot differs from source: {source.name}")
    except BaseException:
        snapshot.close()
        raise
    return snapshot


def _snapshot_core_release_input_pair(
    wheel: _CoreReleaseInput,
    framework_lock: _CoreReleaseInput,
    destination_dir: Path,
    *,
    project_name: str,
    project_version: str,
) -> tuple[_CoreReleaseInput, _CoreReleaseInput]:
    wheel_snapshot = _snapshot_core_release_input(wheel, destination_dir)
    try:
        lock_snapshot = _snapshot_core_release_input(framework_lock, destination_dir)
    except BaseException:
        wheel_snapshot.close()
        raise
    try:
        _verify_core_release_input(wheel)
        _verify_core_release_input(framework_lock)
        _validate_core_release_input_pair(wheel_snapshot, lock_snapshot)
        _validate_core_wheel(wheel_snapshot, name=project_name, version=project_version)
        lock = _validated_framework_lock(_read_core_release_input(lock_snapshot))
        if (
            lock.distribution != "openevo"
            or lock.distribution_version != project_version
            or lock.distribution_digest != wheel_snapshot.sha256
            or lock.wheel_filename != wheel_snapshot.name
        ):
            raise RuntimeError("Core framework lock does not bind the exact built wheel")
        _verify_core_release_input(wheel_snapshot)
        _verify_core_release_input(lock_snapshot)
    except BaseException:
        lock_snapshot.close()
        wheel_snapshot.close()
        raise
    return wheel_snapshot, lock_snapshot


def _snapshot_daemon_release_input_pair(
    bundle: _CoreReleaseInput,
    manifest: _CoreReleaseInput,
    destination_dir: Path,
    *,
    repo: Path,
) -> tuple[_CoreReleaseInput, _CoreReleaseInput, dict[str, object]]:
    bundle_snapshot = _snapshot_core_release_input(bundle, destination_dir)
    try:
        manifest_snapshot = _snapshot_core_release_input(manifest, destination_dir)
    except BaseException:
        bundle_snapshot.close()
        raise
    try:
        _verify_core_release_input(bundle)
        _verify_core_release_input(manifest)
        value = _load_daemon_release_manifest(
            bundle_snapshot,
            manifest_snapshot,
            repo=repo,
        )
        _verify_core_release_input(bundle_snapshot)
        _verify_core_release_input(manifest_snapshot)
    except BaseException:
        manifest_snapshot.close()
        bundle_snapshot.close()
        raise
    return bundle_snapshot, manifest_snapshot, value


def _copy_core_release_input(source: _CoreReleaseInput, destination_fd: int) -> None:
    os.lseek(source.file_fd, 0, os.SEEK_SET)
    while chunk := os.read(source.file_fd, 1024 * 1024):
        remaining = memoryview(chunk)
        while remaining:
            written = os.write(destination_fd, remaining)
            if written <= 0:
                raise RuntimeError(f"Core release member copy stalled: {source.name}")
            remaining = remaining[written:]
    os.lseek(source.file_fd, 0, os.SEEK_SET)


def _verify_core_release_member(directory_fd: int, source: _CoreReleaseInput) -> None:
    file_fd = os.open(
        source.name,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        dir_fd=directory_fd,
    )
    try:
        descriptor = os.fstat(file_fd)
        if (
            not stat.S_ISREG(descriptor.st_mode)
            or descriptor.st_uid != os.geteuid()
            or stat.S_IMODE(descriptor.st_mode) != 0o600
            or descriptor.st_nlink != 1
            or descriptor.st_size != source.byte_size
        ):
            raise RuntimeError(f"Core release member attributes are invalid: {source.name}")
        byte_size, digest = _sha256_fd(file_fd)
        if byte_size != source.byte_size or digest != source.sha256:
            raise RuntimeError(f"Core release member content changed: {source.name}")
        current = os.stat(source.name, dir_fd=directory_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (descriptor.st_dev, descriptor.st_ino):
            raise RuntimeError(f"Core release member pathname changed: {source.name}")
    finally:
        os.close(file_fd)


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
        raise RuntimeError("Core release publication requires atomic no-replace rename support")
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), source_name, destination_name)


def _publish_core_release_inputs_once(
    output_dir: Path,
    wheel: Path,
    framework_lock: Path,
) -> None:
    """Publish the exact release pair once on a controlled build filesystem."""
    output = _require_core_release_output_absent(output_dir)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    parent_fd = os.open(output.parent, flags)
    staging_name = f".{output.name}.staging-{secrets.token_hex(16)}"
    staging_fd = -1
    sources: list[_CoreReleaseInput] = []
    try:
        _require_private_build_directory(os.fstat(parent_fd), name="output parent")
        _require_core_release_output_absent(output)
        os.mkdir(staging_name, 0o700, dir_fd=parent_fd)
        staging_fd = os.open(staging_name, flags, dir_fd=parent_fd)
        staging_descriptor = os.fstat(staging_fd)
        _require_private_build_directory(staging_descriptor, name="staging directory")
        if stat.S_IMODE(staging_descriptor.st_mode) != 0o700:
            raise RuntimeError("Core wheel staging directory mode is invalid")

        wheel_source = _open_core_release_input(wheel, name=wheel.name)
        sources.append(wheel_source)
        lock_source = _open_core_release_input(
            framework_lock,
            name=CORE_FRAMEWORK_LOCK_BASENAME,
        )
        sources.append(lock_source)
        _validate_core_release_input_pair(wheel_source, lock_source)

        for source in sources:
            destination_fd = os.open(
                source.name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=staging_fd,
            )
            try:
                _copy_core_release_input(source, destination_fd)
                os.fsync(destination_fd)
            finally:
                os.close(destination_fd)
            _verify_core_release_input(source)
            _verify_core_release_member(staging_fd, source)

        expected = sorted(source.name for source in sources)
        if sorted(os.listdir(staging_fd)) != expected:
            raise RuntimeError("Core wheel staging directory inventory is invalid")
        os.fsync(staging_fd)
        _require_core_release_output_absent(output)
        try:
            _rename_noreplace(parent_fd, staging_name, parent_fd, output.name)
        except OSError as exc:
            raise RuntimeError(
                "Core release inputs could not be published atomically; "
                f"the non-authoritative staging directory was preserved as {staging_name}"
            ) from exc
        os.fsync(parent_fd)

        published = os.stat(output.name, dir_fd=parent_fd, follow_symlinks=False)
        held = os.fstat(staging_fd)
        if (
            (published.st_dev, published.st_ino) != (held.st_dev, held.st_ino)
            or not stat.S_ISDIR(published.st_mode)
        ):
            raise RuntimeError("Published Core release directory identity changed")
        for source in sources:
            _verify_core_release_member(staging_fd, source)
    finally:
        for source in sources:
            source.close()
        if staging_fd >= 0:
            os.close(staging_fd)
        os.close(parent_fd)


def _publish_sidecar_binary(built: Path, target: Path) -> None:
    """Atomically replace the Tauri external binary with verified build bytes."""
    if target.name in {"", ".", ".."} or target.parent == target:
        raise RuntimeError("Desktop sidecar target path is invalid")
    source_fd = -1
    staging_fd = -1
    directory_fd = -1
    staging_name = f".{target.name}.staging-{secrets.token_hex(16)}"
    try:
        source_fd = os.open(built, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        source_descriptor = os.fstat(source_fd)
        source_path_descriptor = os.stat(built, follow_symlinks=False)
        if (
            not stat.S_ISREG(source_descriptor.st_mode)
            or source_descriptor.st_uid != os.geteuid()
            or source_descriptor.st_nlink != 1
            or source_descriptor.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or (source_descriptor.st_dev, source_descriptor.st_ino)
            != (source_path_descriptor.st_dev, source_path_descriptor.st_ino)
        ):
            raise RuntimeError("PyInstaller sidecar output is not a private regular file")
        byte_size, digest = _sha256_fd(source_fd)
        if byte_size != source_descriptor.st_size:
            raise RuntimeError("PyInstaller sidecar output changed while hashing")

        directory_fd = os.open(
            target.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        directory_descriptor = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(directory_descriptor.st_mode)
            or directory_descriptor.st_uid != os.geteuid()
            or directory_descriptor.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise RuntimeError("Desktop sidecar binary directory is not trusted")

        staging_fd = os.open(
            staging_name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o700,
            dir_fd=directory_fd,
        )
        os.lseek(source_fd, 0, os.SEEK_SET)
        while chunk := os.read(source_fd, 1024 * 1024):
            remaining = memoryview(chunk)
            while remaining:
                written = os.write(staging_fd, remaining)
                if written <= 0:
                    raise RuntimeError("Desktop sidecar copy stalled")
                remaining = remaining[written:]
        os.lseek(source_fd, 0, os.SEEK_SET)
        os.fchmod(staging_fd, 0o755)
        os.fsync(staging_fd)

        current_source = os.fstat(source_fd)
        current_source_path = os.stat(built, follow_symlinks=False)
        current_size, current_digest = _sha256_fd(source_fd)
        if (
            (current_source.st_dev, current_source.st_ino)
            != (source_descriptor.st_dev, source_descriptor.st_ino)
            or (current_source_path.st_dev, current_source_path.st_ino)
            != (source_descriptor.st_dev, source_descriptor.st_ino)
            or current_source.st_nlink != 1
            or current_size != byte_size
            or current_digest != digest
        ):
            raise RuntimeError("PyInstaller sidecar output changed while copying")

        staged = os.fstat(staging_fd)
        staged_path = os.stat(staging_name, dir_fd=directory_fd, follow_symlinks=False)
        staged_size, staged_digest = _sha256_fd(staging_fd)
        if (
            not stat.S_ISREG(staged.st_mode)
            or staged.st_uid != os.geteuid()
            or staged.st_nlink != 1
            or stat.S_IMODE(staged.st_mode) != 0o755
            or staged_size != byte_size
            or staged_digest != digest
            or (staged_path.st_dev, staged_path.st_ino) != (staged.st_dev, staged.st_ino)
        ):
            raise RuntimeError("Desktop sidecar staging file failed verification")

        try:
            os.replace(
                staging_name,
                target.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
        except OSError as exc:
            raise RuntimeError(
                "Desktop sidecar could not be published atomically; "
                f"the non-authoritative staging file was preserved as {staging_name}"
            ) from exc
        os.fsync(directory_fd)

        published = os.stat(target.name, dir_fd=directory_fd, follow_symlinks=False)
        final_size, final_digest = _sha256_fd(staging_fd)
        if (
            (published.st_dev, published.st_ino) != (staged.st_dev, staged.st_ino)
            or stat.S_IMODE(published.st_mode) != 0o755
            or final_size != byte_size
            or final_digest != digest
        ):
            raise RuntimeError("Published Desktop sidecar failed final verification")
    finally:
        if staging_fd >= 0:
            os.close(staging_fd)
        if directory_fd >= 0:
            os.close(directory_fd)
        if source_fd >= 0:
            os.close(source_fd)


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
    wscript = source_root / "bootloader/wscript"
    wscript_text = wscript.read_text(encoding="utf-8")
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
        or wscript_text.count(_BOOTLOADER_DARWIN_LIB_NEEDLE) != 1
        or wscript_text.count(_BOOTLOADER_PROGRAM_LIBS_NEEDLE) != 1
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
    wscript.write_text(
        wscript_text.replace(
            _BOOTLOADER_DARWIN_LIB_NEEDLE,
            _BOOTLOADER_DARWIN_LIB_REPLACEMENT,
        ).replace(
            _BOOTLOADER_PROGRAM_LIBS_NEEDLE,
            _BOOTLOADER_PROGRAM_LIBS_REPLACEMENT,
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
                b"listener_info_probe_failed",
                b"listener_endpoint_invalid",
            )
        )
    else:
        raise RuntimeError("Desktop sidecar bootloader platform is unsupported")
    if not all(marker in payload for marker in required):
        raise RuntimeError("packaged sidecar is missing the native execution bootloader")


def _validate_embedded_core_wheel(
    executable: Path,
    wheel: Path | _CoreReleaseInput,
) -> str:
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
    wheel_name, _wheel_size, source_digest = _core_release_identity(wheel)
    expected = (CORE_WHEEL_ARCHIVE_ROOT / wheel_name).as_posix()
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
    wheel: Path | _CoreReleaseInput,
    framework_lock: Path | _CoreReleaseInput,
    *,
    version: str,
) -> str:
    wheel_name, _wheel_size, wheel_digest = _core_release_identity(wheel)
    lock_name, _lock_size, lock_digest = _core_release_identity(framework_lock)
    if lock_name != CORE_FRAMEWORK_LOCK_BASENAME:
        raise RuntimeError("Core framework lock input must use the canonical filename")
    expected_wheel = (CORE_WHEEL_ARCHIVE_ROOT / wheel_name).as_posix()
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

    expected_payload = _core_framework_lock_bytes_for_identity(
        wheel_filename=wheel_name,
        wheel_digest=wheel_digest,
        version=version,
    )
    try:
        source_payload = _core_release_payload(framework_lock)
    except OSError as exc:
        raise RuntimeError("Core framework lock is unavailable") from exc
    if source_payload != expected_payload:
        raise RuntimeError("staged Core framework lock differs from the exact built wheel")
    source_lock = _validated_framework_lock(source_payload)
    if (
        source_lock.distribution != "openevo"
        or source_lock.distribution_version != version
        or source_lock.distribution_digest != wheel_digest
        or source_lock.wheel_filename != wheel_name
        or _sha256_bytes(source_payload) != lock_digest
    ):
        raise RuntimeError("staged Core framework lock identity is invalid")
    embedded_wheel_payload = _archive_member_bytes(executable, expected_wheel)
    if _sha256_bytes(embedded_wheel_payload) != wheel_digest:
        raise RuntimeError("sidecar embedded Core wheel differs from the staged wheel")
    embedded_payload = _archive_member_bytes(executable, expected_lock)
    if embedded_payload != source_payload:
        raise RuntimeError("sidecar embedded Core framework lock differs from the staged lock")
    embedded_lock = _validated_framework_lock(embedded_payload)
    if embedded_lock != source_lock:
        raise RuntimeError("sidecar embedded Core framework lock identity is invalid")
    return lock_digest


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


def _validate_embedded_managed_runtime_archive(
    executable: Path,
    archive: Path,
) -> None:
    runtime = MANAGED_RUNTIME_ARCHIVE_RELEASE
    expected_member = (MANAGED_RUNTIME_ARCHIVE_ROOT / runtime.filename).as_posix()
    members = sorted(
        name
        for name in _archive_member_names(executable)
        if name.startswith(f"{MANAGED_RUNTIME_ARCHIVE_ROOT.as_posix()}/")
    )
    if members != [expected_member]:
        raise RuntimeError("sidecar archive does not contain the exact managed runtime archive")
    source_identity = _validate_managed_runtime_archive(archive)
    embedded_identity = _archive_member_digest(
        executable,
        expected_member,
        expected_size=runtime.byte_size,
    )
    if embedded_identity != source_identity:
        raise RuntimeError("embedded managed runtime archive identity differs from its source")


def _load_daemon_release_manifest(
    bundle: _CoreReleaseInput,
    manifest: _CoreReleaseInput,
    *,
    repo: Path,
) -> dict[str, object]:
    if bundle.name != DAEMON_BUNDLE_BASENAME or manifest.name != DAEMON_MANIFEST_BASENAME:
        raise RuntimeError("Daemon release inputs do not use canonical filenames")
    if manifest.byte_size < 1 or manifest.byte_size > 1024 * 1024:
        raise RuntimeError("Daemon release manifest size is invalid")
    _verify_core_release_input(bundle)
    _verify_core_release_input(manifest)
    os.lseek(manifest.file_fd, 0, os.SEEK_SET)
    payload = os.read(manifest.file_fd, manifest.byte_size + 1)
    os.lseek(manifest.file_fd, 0, os.SEEK_SET)
    if len(payload) != manifest.byte_size:
        raise RuntimeError("Daemon release manifest changed while reading")
    _verify_core_release_input(manifest)

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise RuntimeError("Daemon release manifest contains a duplicate key")
            value[key] = item
        return value

    try:
        value = json.loads(payload, object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Daemon release manifest is unreadable") from exc
    expected_keys = {
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
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise RuntimeError("Daemon release manifest does not use the closed schema")
    canonical = (
        json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")
    if payload != canonical:
        raise RuntimeError("Daemon release manifest is not canonical")
    artifact = value.get("artifact")
    if artifact != {
        "filename": DAEMON_BUNDLE_BASENAME,
        "sha256": bundle.sha256,
        "size": bundle.byte_size,
    }:
        raise RuntimeError("Daemon release manifest does not bind the exact binary")
    if value.get("schema_version") != 1 or value.get("platform") != {
        "architecture": "x86_64",
        "system": "linux",
    }:
        raise RuntimeError("Daemon release platform contract is invalid")
    release = value.get("release")
    if (
        not isinstance(release, dict)
        or set(release) != {"identity", "source_commit"}
        or release.get("source_commit") != _BUILD_SOURCE_COMMIT
        or not isinstance(release.get("identity"), str)
        or _SHA256_PATTERN.fullmatch(release["identity"]) is None
    ):
        raise RuntimeError("Daemon release manifest does not bind the Desktop source commit")
    dependency_lock = value.get("dependency_lock")
    if dependency_lock != {
        "filename": "uv.lock",
        "sha256": _sha256_bytes((repo / "uv.lock").read_bytes()),
    }:
        raise RuntimeError("Daemon release manifest does not bind the dependency lock")
    runtime = value.get("runtime")
    if (
        not isinstance(runtime, dict)
        or set(runtime)
        != {"format", "python", "system_python_required", "target_pypi_required"}
        or runtime.get("format") != "pyinstaller-onefile"
        or runtime.get("system_python_required") is not False
        or runtime.get("target_pypi_required") is not False
    ):
        raise RuntimeError("Daemon release runtime contract is invalid")
    python = runtime.get("python")
    if (
        not isinstance(python, dict)
        or set(python) != {"implementation", "version"}
        or python.get("implementation") != "CPython"
        or not isinstance(python.get("version"), str)
        or not python["version"]
    ):
        raise RuntimeError("Daemon release Python identity is invalid")
    if value.get("smoke") != {
        "backend_readiness": "passed",
        "controlled_exit": "passed",
        "identity": "passed",
    }:
        raise RuntimeError("Daemon release smoke contract is incomplete")
    core = value.get("core")
    if (
        not isinstance(core, dict)
        or set(core) != {"framework_lock", "registry_digest", "wheel"}
        or not isinstance(core.get("registry_digest"), str)
        or _SHA256_PATTERN.fullmatch(core["registry_digest"]) is None
    ):
        raise RuntimeError("Daemon release Core identity is invalid")
    return value


def _validate_daemon_manifest_core(
    manifest: dict[str, object],
    *,
    wheel: Path | _CoreReleaseInput,
    framework_lock: Path | _CoreReleaseInput,
    version: str,
) -> None:
    wheel_name, wheel_size, wheel_digest = _core_release_identity(wheel)
    lock_name, _lock_size, lock_digest = _core_release_identity(framework_lock)
    core = manifest["core"]
    assert isinstance(core, dict)
    if core.get("framework_lock") != {
        "filename": lock_name,
        "sha256": lock_digest,
    } or core.get("wheel") != {
        "filename": wheel_name,
        "sha256": wheel_digest,
        "size": wheel_size,
        "version": version,
    }:
        raise RuntimeError("Daemon release manifest does not bind the embedded Core wheel and lock")


def _validate_embedded_daemon_release_inputs(
    executable: Path,
    bundle: _CoreReleaseInput,
    manifest: _CoreReleaseInput,
) -> None:
    expected = {
        (DAEMON_ARCHIVE_ROOT / bundle.name).as_posix(): bundle,
        (DAEMON_ARCHIVE_ROOT / manifest.name).as_posix(): manifest,
    }
    members = sorted(
        name
        for name in _archive_member_names(executable)
        if name.startswith(f"{DAEMON_ARCHIVE_ROOT.as_posix()}/")
    )
    if members != sorted(expected):
        raise RuntimeError(
            "sidecar archive does not contain exactly the verified Daemon release inputs"
        )
    for member, source in expected.items():
        _verify_core_release_input(source)
        embedded_identity = _archive_member_digest(
            executable,
            member,
            expected_size=source.byte_size,
        )
        if embedded_identity != (source.byte_size, source.sha256):
            raise RuntimeError(
                f"sidecar embedded Daemon release input differs from its source: {source.name}"
            )


def _open_daemon_release_input_pair(
    bundle: Path,
    manifest: Path,
    *,
    repo: Path,
) -> tuple[_CoreReleaseInput, _CoreReleaseInput, dict[str, object]]:
    if bundle.name != DAEMON_BUNDLE_BASENAME or manifest.name != DAEMON_MANIFEST_BASENAME:
        raise RuntimeError("Daemon release inputs do not use canonical filenames")
    bundle_source = _open_core_release_input(bundle, name=DAEMON_BUNDLE_BASENAME)
    try:
        manifest_source = _open_core_release_input(
            manifest,
            name=DAEMON_MANIFEST_BASENAME,
        )
    except BaseException:
        bundle_source.close()
        raise
    try:
        value = _load_daemon_release_manifest(
            bundle_source,
            manifest_source,
            repo=repo,
        )
    except BaseException:
        manifest_source.close()
        bundle_source.close()
        raise
    return bundle_source, manifest_source, value


def build_sidecar(
    *,
    clean: bool,
    core_wheel_output_dir: Path | None = None,
    core_wheel: Path | None = None,
    core_framework_lock: Path | None = None,
    managed_runtime_archive: Path | None = None,
    daemon_bundle: Path | None = None,
    daemon_manifest: Path | None = None,
    release_build: bool = False,
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

    if release_build and (
        managed_runtime_archive is None
        or daemon_bundle is None
        or daemon_manifest is None
    ):
        raise RuntimeError(
            "release sidecar build requires the managed runtime archive and Daemon inputs"
        )
    if (daemon_bundle is None) != (daemon_manifest is None):
        raise RuntimeError("Daemon bundle and manifest must be provided together")
    if (core_wheel is None) != (core_framework_lock is None):
        raise RuntimeError("Core wheel and framework lock inputs must be provided together")
    if (
        release_build
        and daemon_bundle is not None
        and core_wheel is None
    ):
        raise RuntimeError(
            "release sidecar build with Daemon inputs requires the exact Core wheel "
            "and framework lock inputs"
        )
    if core_wheel is not None and core_wheel_output_dir is not None:
        raise RuntimeError(
            "Core wheel input pair cannot be combined with a Core wheel output directory"
        )
    if managed_runtime_archive is not None:
        managed_runtime_archive = Path(os.path.abspath(managed_runtime_archive))
        _validate_managed_runtime_archive(managed_runtime_archive)
    if core_wheel_output_dir is not None:
        output_candidate = Path(os.path.abspath(core_wheel_output_dir))
        _reject_symlink_path(output_candidate)
        resolved_candidate = output_candidate.resolve(strict=False)
        if any(
            _paths_overlap(resolved_candidate, path.resolve())
            for path in (dist_dir, build_dir, binary_dir)
        ):
            raise RuntimeError("Core wheel output directory overlaps generated paths")
        requested_output = _require_core_release_output_absent(output_candidate)
        core_wheel_output_dir = requested_output
    project_name, core_version = _project_identity(repo)
    with ExitStack() as resources:
        temporary_dir = resources.enter_context(
            TemporaryDirectory(prefix="openevo-sidecar-build-")
        )
        temporary_root = Path(temporary_dir)
        provided_core_wheel: _CoreReleaseInput | None = None
        provided_core_lock: _CoreReleaseInput | None = None
        if core_wheel is not None and core_framework_lock is not None:
            raw_core_wheel, raw_core_lock = _open_core_release_input_pair(
                core_wheel,
                core_framework_lock,
            )
            resources.callback(raw_core_lock.close)
            resources.callback(raw_core_wheel.close)
            provided_core_wheel, provided_core_lock = _snapshot_core_release_input_pair(
                raw_core_wheel,
                raw_core_lock,
                temporary_root / "core-inputs",
                project_name=project_name,
                project_version=core_version,
            )
            resources.callback(provided_core_lock.close)
            resources.callback(provided_core_wheel.close)

        daemon_bundle_source: _CoreReleaseInput | None = None
        daemon_manifest_source: _CoreReleaseInput | None = None
        daemon_manifest_value: dict[str, object] | None = None
        if daemon_bundle is not None and daemon_manifest is not None:
            daemon_bundle = Path(os.path.abspath(daemon_bundle))
            daemon_manifest = Path(os.path.abspath(daemon_manifest))
            (
                raw_daemon_bundle,
                raw_daemon_manifest,
                _raw_daemon_manifest_value,
            ) = _open_daemon_release_input_pair(
                daemon_bundle,
                daemon_manifest,
                repo=repo,
            )
            resources.callback(raw_daemon_manifest.close)
            resources.callback(raw_daemon_bundle.close)
            (
                daemon_bundle_source,
                daemon_manifest_source,
                daemon_manifest_value,
            ) = _snapshot_daemon_release_input_pair(
                raw_daemon_bundle,
                raw_daemon_manifest,
                temporary_root / "daemon-inputs",
                repo=repo,
            )
            resources.callback(daemon_manifest_source.close)
            resources.callback(daemon_bundle_source.close)
        if clean:
            shutil.rmtree(dist_dir, ignore_errors=True)
            shutil.rmtree(build_dir, ignore_errors=True)
        binary_dir.mkdir(parents=True, exist_ok=True)
        target = binary_dir / f"{SIDECAR_NAME}-{_target_triple()}{_platform_extension()}"
        if provided_core_wheel is None or provided_core_lock is None:
            core_wheel = _build_core_wheel(repo, temporary_root / "core")
            core_framework_lock = _write_core_framework_lock(
                core_wheel,
                version=core_version,
            )
        else:
            _verify_core_release_input(provided_core_wheel)
            _verify_core_release_input(provided_core_lock)
            core_wheel = provided_core_wheel.path
            core_framework_lock = provided_core_lock.path
        core_release_wheel: Path | _CoreReleaseInput = core_wheel
        core_release_lock: Path | _CoreReleaseInput = core_framework_lock
        if provided_core_wheel is not None and provided_core_lock is not None:
            core_release_wheel = provided_core_wheel
            core_release_lock = provided_core_lock
        if daemon_manifest_value is not None:
            _validate_daemon_manifest_core(
                daemon_manifest_value,
                wheel=core_release_wheel,
                framework_lock=core_release_lock,
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
        if managed_runtime_archive is not None:
            command[-1:-1] = [
                "--add-data",
                (
                    f"{managed_runtime_archive}{os.pathsep}"
                    f"{MANAGED_RUNTIME_ARCHIVE_ROOT.as_posix()}"
                ),
            ]
        if daemon_bundle_source is not None and daemon_manifest_source is not None:
            command[-1:-1] = [
                "--add-data",
                (
                    f"{daemon_bundle_source.path}{os.pathsep}"
                    f"{DAEMON_ARCHIVE_ROOT.as_posix()}"
                ),
                "--add-data",
                (
                    f"{daemon_manifest_source.path}{os.pathsep}"
                    f"{DAEMON_ARCHIVE_ROOT.as_posix()}"
                ),
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
        _validate_embedded_core_wheel(built, core_release_wheel)
        _validate_embedded_core_framework_lock(
            built,
            core_release_wheel,
            core_release_lock,
            version=core_version,
        )
        _validate_embedded_product_web(built, desktop_root, product_web_digest)
        if managed_runtime_archive is not None:
            _validate_embedded_managed_runtime_archive(built, managed_runtime_archive)
        if daemon_bundle_source is not None and daemon_manifest_source is not None:
            _validate_embedded_daemon_release_inputs(
                built,
                daemon_bundle_source,
                daemon_manifest_source,
            )
        if provided_core_wheel is not None and provided_core_lock is not None:
            _verify_core_release_input(provided_core_wheel)
            _verify_core_release_input(provided_core_lock)

        if core_wheel_output_dir is not None:
            _publish_core_release_inputs_once(
                core_wheel_output_dir,
                core_wheel,
                core_framework_lock,
            )

        _publish_sidecar_binary(built, target)
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
    parser.add_argument(
        "--core-wheel",
        type=Path,
        help="Use this already verified Core wheel instead of rebuilding it.",
    )
    parser.add_argument(
        "--framework-lock",
        dest="core_framework_lock",
        type=Path,
        help="Framework lock paired with --core-wheel.",
    )
    parser.add_argument(
        "--managed-runtime-archive",
        type=Path,
        help=(
            "Embed the exact managed subscription Science runtime archive. "
            "Dev/debug builds may omit it."
        ),
    )
    parser.add_argument(
        "--daemon-bundle",
        type=Path,
        help=(
            "Embed the verified Linux x86_64 Daemon binary at "
            f"{DAEMON_ARCHIVE_ROOT.as_posix()}/. Dev/debug builds may omit it."
        ),
    )
    parser.add_argument(
        "--daemon-manifest",
        type=Path,
        help=(
            "Embed the canonical Daemon release manifest at "
            f"{DAEMON_ARCHIVE_ROOT.as_posix()}/. Dev/debug builds may omit it."
        ),
    )
    parser.add_argument(
        "--release-build",
        action="store_true",
        help="Fail closed unless the managed subscription Science runtime is embedded.",
    )
    args = parser.parse_args(argv)
    target = build_sidecar(
        clean=not args.no_clean,
        core_wheel_output_dir=args.core_wheel_output_dir,
        core_wheel=args.core_wheel,
        core_framework_lock=args.core_framework_lock,
        managed_runtime_archive=args.managed_runtime_archive,
        daemon_bundle=args.daemon_bundle,
        daemon_manifest=args.daemon_manifest,
        release_build=args.release_build,
    )
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
