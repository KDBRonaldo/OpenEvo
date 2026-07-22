#!/usr/bin/env python3
"""Launch and inspect the Tauri executable in an OpenEvo Desktop macOS app."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import NamedTuple

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from smoke_openevo_desktop_sidecar import (  # noqa: E402
    EXPECTED_DESKTOP_OPENAPI_SHA256,
    SmokeFailure,
)


SIDECAR_NAME = "openevo-desktop-sidecar"
APP_BUNDLE_NAME = "OpenEvo Desktop.app"
EXPECTED_MACOS_ICON = REPO_ROOT / "desktop" / "src-tauri" / "icons" / "icon.icns"
EVIDENCE_SCHEMA_VERSION = 3
LAUNCH_ORIGINS = {"mounted_dmg", "detached_copy"}
REQUIRED_BOOLEAN_EVIDENCE = (
    "renderer_ready",
    "sidecar_ready",
    "bundled_external_bin_resolved",
    "native_listener_fd_handoff",
    "native_executable_fd_handoff",
)
MACH_O_ARCHITECTURES = {"arm64", "x86_64"}
NATIVE_PROCESS_MARKER_PREFIX = b"OPENEVO_DESKTOP_SIDECAR_PROCESS_"
NATIVE_RENDERER_MARKER_PREFIX = b"OPENEVO_DESKTOP_RENDERER_READY_"
NATIVE_RENDERER_STAGE_MARKER_PREFIX = b"OPENEVO_DESKTOP_RENDERER_STAGE_"
NATIVE_HOST_LOG_MAX_BYTES = 64 * 1024
NATIVE_HOST_LOG_MAX_LINES = 512
NATIVE_GROUP_MAX_PROCESSES = 16
PROBE_REAP_TIMEOUT_SECONDS = 2.0
PROBE_DESCENDANT_SNAPSHOT_TIMEOUT_SECONDS = 0.5
DARWIN_PROCESS_PATH_MAX_BYTES = 4096
NATIVE_READINESS_STAGES = (
    "native_marker_absent",
    "native_process_unavailable",
    "listener_fd_unavailable",
    "executable_fd_unavailable",
    "renderer_ack_absent",
)
NATIVE_FAILURE_STAGES = frozenset(NATIVE_READINESS_STAGES)
_NATIVE_READINESS_STAGE_RANK = {
    stage: rank for rank, stage in enumerate(NATIVE_READINESS_STAGES)
}
PROBE_DEADLINE_STAGE = "probe_deadline_exhausted"
NATIVE_RENDERER_STAGES = frozenset(
    {
        "sidecar_start_requested",
        "sidecar_start_returned",
        "sidecar_start_failed",
        "bootstrap_context_validated",
        "bootstrap_context_failed",
        "local_api_version_verified",
        "local_api_version_failed",
        "retry_recovery_ready",
        "retry_recovery_failed",
        "provider_adapter_ready",
        "provider_adapter_failed",
        "provider_created",
        "provider_create_failed",
        "initial_snapshot_failed",
        "product_committed",
        "ready_requested",
        "window_identity_valid",
        "window_identity_invalid",
        "window_visible",
        "window_not_visible",
        "window_visibility_unknown",
        "ready_validation_failed",
    }
)
REQUIRED_RENDERER_COMPLETION_STAGES = frozenset(
    {
        "retry_recovery_ready",
        "provider_created",
        "product_committed",
        "ready_requested",
    }
)
RENDERER_FAILURE_STAGES = frozenset(
    {
        "sidecar_start_failed",
        "bootstrap_context_failed",
        "local_api_version_failed",
        "retry_recovery_failed",
        "provider_adapter_failed",
        "provider_create_failed",
        "initial_snapshot_failed",
        "window_identity_invalid",
        "window_not_visible",
        "ready_validation_failed",
    }
)
_NATIVE_PROCESS_MARKER_PATTERN = re.compile(
    rb"OPENEVO_DESKTOP_SIDECAR_PROCESS_V2 "
    rb"pid=([1-9][0-9]{0,9}) pgid=([1-9][0-9]{0,9}) "
    rb"sid=([1-9][0-9]{0,9}) birth=(darwin:([1-9][0-9]{0,19}):([0-9]{1,6})) "
    rb"executable_device=([1-9][0-9]{0,19}) executable_inode=([1-9][0-9]{0,19}) "
    rb"executable_sha256=([0-9a-f]{64}) executable_size=([1-9][0-9]{0,19})"
)
_NATIVE_RENDERER_MARKER_PATTERN = re.compile(
    rb"OPENEVO_DESKTOP_RENDERER_READY_V2 ([0-9a-f]{64})"
)
_NATIVE_RENDERER_STAGE_MARKER_PATTERN = re.compile(
    rb"OPENEVO_DESKTOP_RENDERER_STAGE_V1 ([a-z_]{1,64})"
)


class NativeSidecarProcessMarker(NamedTuple):
    pid: int
    process_group: int
    session_id: int
    birth_identity: str
    executable_device: int
    executable_inode: int
    executable_sha256: str
    executable_size: int


class NativeHostObservation(NamedTuple):
    active_process: NativeSidecarProcessMarker | None
    renderer_ready: bool
    process_groups: frozenset[int]
    renderer_stages: frozenset[str] = frozenset()


class NativeFileDescriptorObservation(NamedTuple):
    file_type: str | None
    name: str | None
    size: int | None
    tcp_state: str | None
    device: int | None
    inode: int | None


class _DarwinProcBsdInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


def _bundle_path_metadata(
    app: Path,
    path: Path,
    *,
    subject: str,
    required: bool,
) -> os.stat_result | None:
    try:
        relative = path.relative_to(app)
    except ValueError as exc:
        raise SmokeFailure(f"{subject} escapes the app bundle") from exc
    current = app
    components = (None, *relative.parts)
    for index, component in enumerate(components):
        if component is not None:
            current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if required:
                raise SmokeFailure(f"{subject} is missing: {path}") from None
            return None
        except OSError as exc:
            raise SmokeFailure(f"Could not inspect {subject}: {path}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise SmokeFailure(f"{subject} must not traverse a symbolic link: {current}")
        if index < len(components) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise SmokeFailure(f"{subject} has a non-directory ancestor: {current}")
    return metadata


def _app_bundle(bundle_root: Path) -> Path:
    app = bundle_root if bundle_root.name == APP_BUNDLE_NAME else bundle_root / APP_BUNDLE_NAME
    metadata = _bundle_path_metadata(
        app,
        app,
        subject="OpenEvo Desktop app bundle",
        required=False,
    )
    if metadata is None or not stat.S_ISDIR(metadata.st_mode):
        raise SmokeFailure(f"No {APP_BUNDLE_NAME} bundle found under {bundle_root}")
    return app


def _read_app_info_plist(app: Path) -> dict[str, object]:
    info_plist = app / "Contents" / "Info.plist"
    info_metadata = _bundle_path_metadata(
        app,
        info_plist,
        subject="App Info.plist",
        required=True,
    )
    assert info_metadata is not None
    if not stat.S_ISREG(info_metadata.st_mode):
        raise SmokeFailure(f"App Info.plist is not a regular file: {info_plist}")
    try:
        with info_plist.open("rb") as stream:
            payload = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException) as exc:
        raise SmokeFailure(f"Could not read app Info.plist: {info_plist}") from exc
    if type(payload) is not dict:
        raise SmokeFailure("App Info.plist root must be a dictionary")
    return payload


def _validate_macos_loopback_transport(app: Path) -> None:
    payload = _read_app_info_plist(app)
    transport = payload.get("NSAppTransportSecurity")
    if type(transport) is not dict or set(transport) != {"NSExceptionDomains"}:
        raise SmokeFailure("App Info.plist does not contain the closed loopback ATS policy")
    domains = transport.get("NSExceptionDomains")
    if (
        type(domains) is not dict
        or set(domains) != {"127.0.0.1"}
        or domains.get("127.0.0.1")
        != {"NSExceptionAllowsInsecureHTTPLoads": True}
    ):
        raise SmokeFailure("App Info.plist loopback ATS policy is invalid")


def _validate_macos_app_icon(app: Path, expected_icon: Path = EXPECTED_MACOS_ICON) -> None:
    payload = _read_app_info_plist(app)
    icon_name = payload.get("CFBundleIconFile")
    if icon_name not in {"icon", "icon.icns"}:
        raise SmokeFailure("App Info.plist does not select the expected macOS icon")
    packaged_icon = app / "Contents" / "Resources" / "icon.icns"
    packaged_metadata = _bundle_path_metadata(
        app,
        packaged_icon,
        subject="App macOS icon",
        required=True,
    )
    assert packaged_metadata is not None
    if not stat.S_ISREG(packaged_metadata.st_mode):
        raise SmokeFailure("App macOS icon is not a regular file")
    try:
        expected_metadata = expected_icon.lstat()
    except OSError as exc:
        raise SmokeFailure("Expected generated macOS icon is unavailable") from exc
    if not stat.S_ISREG(expected_metadata.st_mode):
        raise SmokeFailure("Expected generated macOS icon is not a regular file")
    if _sha256(packaged_icon) != _sha256(expected_icon):
        raise SmokeFailure("App macOS icon does not match the generated release icon")


def find_app_executable(bundle_root: Path) -> Path:
    app = _app_bundle(bundle_root)
    payload = _read_app_info_plist(app)
    executable_name = payload.get("CFBundleExecutable")
    if (
        type(executable_name) is not str
        or not executable_name
        or executable_name in {".", ".."}
        or "/" in executable_name
        or "\x00" in executable_name
    ):
        raise SmokeFailure("App Info.plist has an invalid CFBundleExecutable")
    executable = app / "Contents" / "MacOS" / executable_name
    executable_metadata = _bundle_path_metadata(
        app,
        executable,
        subject="App CFBundleExecutable",
        required=True,
    )
    assert executable_metadata is not None
    if not stat.S_ISREG(executable_metadata.st_mode) or not os.access(executable, os.X_OK):
        raise SmokeFailure(f"App CFBundleExecutable is missing or not executable: {executable}")
    return executable


def find_bundled_sidecar(bundle_root: Path) -> Path:
    app = _app_bundle(bundle_root)
    contents = app / "Contents"
    candidates = [
        contents / "MacOS" / SIDECAR_NAME,
        contents / "Resources" / SIDECAR_NAME,
        contents / "Resources" / "binaries" / SIDECAR_NAME,
    ]
    found: list[Path] = []
    for path in candidates:
        metadata = _bundle_path_metadata(
            app,
            path,
            subject="Packaged sidecar",
            required=False,
        )
        if metadata is not None and stat.S_ISREG(metadata.st_mode):
            found.append(path)
    executable = [path for path in found if os.access(path, os.X_OK)]
    if len(executable) != 1:
        raise SmokeFailure(
            "App bundle must contain exactly one executable OpenEvo Desktop sidecar; "
            f"found {len(executable)}"
        )
    return executable[0]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_dmg_identity(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise SmokeFailure("App smoke source DMG must be a regular non-symlink file")
    return {"filename": path.name, "sha256": _sha256(path)}


def _file_identity(path: Path) -> tuple[int, int, int]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SmokeFailure(f"Could not inspect packaged binary identity: {path.name}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise SmokeFailure(f"Packaged binary is no longer a regular file: {path.name}")
    return metadata.st_dev, metadata.st_ino, metadata.st_size


def _verify_binary_unchanged(
    path: Path,
    *,
    expected_identity: tuple[int, int, int],
    expected_digest: str,
) -> None:
    if _file_identity(path) != expected_identity or _sha256(path) != expected_digest:
        raise SmokeFailure(f"Packaged binary changed during native smoke: {path.name}")


def _validate_mach_o_observation(payload: object) -> dict[str, object]:
    if type(payload) is not dict or set(payload) != {"file_output", "slices"}:
        raise SmokeFailure("Mach-O evidence does not use the closed schema")
    file_output = payload.get("file_output")
    slices = payload.get("slices")
    if (
        type(file_output) is not str
        or not file_output
        or len(file_output) > 512
        or "Mach-O" not in file_output
        or any(ord(character) < 32 or ord(character) == 127 for character in file_output)
    ):
        raise SmokeFailure("Binary is not a Mach-O executable according to file")
    if (
        type(slices) is not list
        or not slices
        or any(type(value) is not str or value not in MACH_O_ARCHITECTURES for value in slices)
        or slices != sorted(set(slices))
    ):
        raise SmokeFailure("Mach-O architecture slices are invalid")
    return {"file_output": file_output, "slices": slices}


def inspect_mach_o(path: Path) -> dict[str, object]:
    file_result = subprocess.run(
        ["file", "-b", str(path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if file_result.returncode != 0:
        raise SmokeFailure(f"file could not inspect packaged binary: {path.name}")
    lipo_result = subprocess.run(
        ["lipo", "-archs", str(path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if lipo_result.returncode != 0:
        raise SmokeFailure(f"lipo could not inspect packaged binary: {path.name}")
    return _validate_mach_o_observation(
        {
            "file_output": file_result.stdout.strip(),
            "slices": sorted(lipo_result.stdout.split()),
        }
    )


def _validate_mach_o_evidence(payload: object) -> dict[str, object]:
    if type(payload) is not dict or set(payload) != {
        "native_executable",
        "bundled_external_bin",
    }:
        raise SmokeFailure("App smoke Mach-O evidence does not use the closed schema")
    return {
        name: _validate_mach_o_observation(payload[name])
        for name in ("native_executable", "bundled_external_bin")
    }


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _running_process_groups(process_groups: set[int], deadline: float) -> set[int]:
    result = _run_probe(
        ["ps", "-axo", "pgid=,stat="],
        deadline=deadline,
        timeout_cap=0.5,
    )
    if result is None or result.returncode != 0:
        return {group for group in process_groups if _process_group_exists(group)}
    live_groups: set[int] = set()
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2 or parts[1].startswith("Z"):
            continue
        try:
            process_group = int(parts[0])
        except ValueError:
            continue
        if process_group in process_groups:
            live_groups.add(process_group)
    return live_groups


def _wait_for_groups_to_exit(process_groups: set[int], deadline: float) -> bool:
    while time.monotonic() < deadline:
        if not any(_process_group_exists(group) for group in process_groups):
            return True
        time.sleep(0.05)
    return not any(_process_group_exists(group) for group in process_groups)


def _kill_groups(process_groups: set[int], sig: signal.Signals) -> None:
    for process_group in process_groups:
        try:
            os.killpg(process_group, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _probe_descendant_process_groups(root_pid: int) -> set[int]:
    try:
        inventory = subprocess.Popen(
            ["/bin/ps", "-axo", "pid=,ppid=,pgid="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
    except OSError:
        return set()
    try:
        stdout, _stderr = inventory.communicate(
            timeout=PROBE_DESCENDANT_SNAPSHOT_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired:
        inventory.kill()
        inventory.wait(timeout=PROBE_REAP_TIMEOUT_SECONDS)
        return set()
    if inventory.returncode != 0:
        return set()
    rows: list[tuple[int, int, int]] = []
    for line in stdout.splitlines():
        parts = line.strip().split()
        if len(parts) != 3:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), int(parts[2])))
        except ValueError:
            continue
    descendants = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, parent, _group in rows:
            if parent in descendants and pid not in descendants:
                descendants.add(pid)
                changed = True
    return {
        group
        for pid, _parent, group in rows
        if pid != root_pid and pid in descendants and group > 0
    }


def _run_probe(
    arguments: list[str],
    *,
    deadline: float,
    timeout_cap: float,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str] | None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    try:
        process = subprocess.Popen(
            arguments,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,
        )
    except OSError:
        return None
    try:
        stdout, stderr = process.communicate(timeout=min(timeout_cap, remaining))
    except subprocess.TimeoutExpired:
        descendant_groups = _probe_descendant_process_groups(process.pid)
        descendant_groups.discard(process.pid)
        _kill_groups(descendant_groups, signal.SIGKILL)
        _kill_groups({process.pid}, signal.SIGKILL)
        try:
            process.communicate(timeout=PROBE_REAP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=PROBE_REAP_TIMEOUT_SECONDS)
        return None
    return subprocess.CompletedProcess(arguments, process.returncode, stdout, stderr)


def _process_rows(deadline: float) -> list[tuple[int, int, int, str]]:
    result = _run_probe(
        ["ps", "-axo", "pid=,ppid=,pgid=,command="],
        deadline=deadline,
        timeout_cap=5,
    )
    if result is None or result.returncode != 0:
        raise SmokeFailure("Native process inventory is unavailable")
    rows: list[tuple[int, int, int, str]] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=3)
        if len(parts) != 4:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), int(parts[2]), parts[3]))
        except ValueError:
            continue
    return rows


def _descendants(root_pid: int, deadline: float) -> list[tuple[int, int, int, str]]:
    rows = _process_rows(deadline)
    descendants: set[int] = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, parent, _group, _command in rows:
            if parent in descendants and pid not in descendants:
                descendants.add(pid)
                changed = True
    return [row for row in rows if row[0] in descendants and row[0] != root_pid]


def _parse_native_host_observation(
    payload: bytes,
    observed_process_groups: set[int] | None = None,
) -> NativeHostObservation:
    byte_limit_exceeded = len(payload) > NATIVE_HOST_LOG_MAX_BYTES
    bounded_payload = payload[:NATIVE_HOST_LOG_MAX_BYTES]
    lines = bounded_payload.splitlines()
    if bounded_payload and not bounded_payload.endswith((b"\n", b"\r")):
        lines = lines[:-1]
    line_limit_exceeded = len(lines) > NATIVE_HOST_LOG_MAX_LINES
    lines = lines[:NATIVE_HOST_LOG_MAX_LINES]

    active_process: NativeSidecarProcessMarker | None = None
    renderer_ready = False
    process_groups: set[int] = set()
    renderer_stages: set[str] = set()
    for line in lines:
        if line.startswith(NATIVE_RENDERER_STAGE_MARKER_PREFIX):
            matched = _NATIVE_RENDERER_STAGE_MARKER_PATTERN.fullmatch(line)
            stage = matched.group(1).decode("ascii") if matched is not None else None
            if stage not in NATIVE_RENDERER_STAGES:
                raise SmokeFailure("Native host renderer stage is malformed")
            renderer_stages.add(stage)
            continue
        if line.startswith(NATIVE_PROCESS_MARKER_PREFIX):
            matched = _NATIVE_PROCESS_MARKER_PATTERN.fullmatch(line)
            if matched is None:
                raise SmokeFailure("Native host sidecar process marker is malformed")
            pid, process_group, session_id = (
                int(matched.group(index)) for index in (1, 2, 3)
            )
            birth_seconds = int(matched.group(5))
            birth_microseconds = int(matched.group(6))
            executable_device = int(matched.group(7))
            executable_inode = int(matched.group(8))
            executable_size = int(matched.group(10))
            if (
                pid > 2**31 - 1
                or process_group != pid
                or session_id != pid
                or birth_seconds <= 0
                or birth_microseconds >= 1_000_000
                or executable_device > 2**64 - 1
                or executable_inode > 2**64 - 1
                or executable_size > 2**63 - 1
            ):
                raise SmokeFailure("Native host sidecar process marker is malformed")
            active_process = NativeSidecarProcessMarker(
                pid=pid,
                process_group=process_group,
                session_id=session_id,
                birth_identity=matched.group(4).decode("ascii"),
                executable_device=executable_device,
                executable_inode=executable_inode,
                executable_sha256=matched.group(9).decode("ascii"),
                executable_size=executable_size,
            )
            process_groups.add(process_group)
            if observed_process_groups is not None:
                observed_process_groups.add(process_group)
            renderer_ready = False
            continue
        if line.startswith(NATIVE_RENDERER_MARKER_PREFIX):
            matched = _NATIVE_RENDERER_MARKER_PATTERN.fullmatch(line)
            if (
                matched is None
                or active_process is None
                or matched.group(1).decode("ascii") != EXPECTED_DESKTOP_OPENAPI_SHA256
            ):
                raise SmokeFailure("Native host renderer marker is malformed")
            renderer_ready = True

    if byte_limit_exceeded:
        raise SmokeFailure("Native host smoke diagnostics exceeded the byte limit")
    if line_limit_exceeded:
        raise SmokeFailure("Native host smoke diagnostics exceeded the line limit")

    return NativeHostObservation(
        active_process=active_process,
        renderer_ready=renderer_ready,
        process_groups=frozenset(process_groups),
        renderer_stages=frozenset(renderer_stages),
    )


def _drain_native_host_stderr(
    stream: object,
    buffer: bytearray,
    observed_process_groups: set[int] | None = None,
) -> NativeHostObservation:
    fileno = getattr(stream, "fileno", None)
    if not callable(fileno):
        raise SmokeFailure("Native host smoke diagnostics are unavailable")
    while True:
        try:
            chunk = os.read(fileno(), 4096)
        except BlockingIOError:
            break
        except OSError as exc:
            raise SmokeFailure("Native host smoke diagnostics are unreadable") from exc
        if not chunk:
            break
        if len(buffer) + len(chunk) > NATIVE_HOST_LOG_MAX_BYTES:
            remaining = NATIVE_HOST_LOG_MAX_BYTES - len(buffer)
            if remaining > 0:
                buffer.extend(chunk[:remaining])
            _parse_native_host_observation(bytes(buffer), observed_process_groups)
            raise SmokeFailure("Native host smoke diagnostics exceeded the byte limit")
        buffer.extend(chunk)
    return _parse_native_host_observation(bytes(buffer), observed_process_groups)


def _darwin_process_birth_identity(pid: int) -> str | None:
    if sys.platform != "darwin":
        return None
    try:
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        proc_pidinfo = library.proc_pidinfo
        proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        proc_pidinfo.restype = ctypes.c_int
        info = _DarwinProcBsdInfo()
        size = ctypes.sizeof(info)
        result = proc_pidinfo(pid, 3, 0, ctypes.byref(info), size)
    except (AttributeError, OSError, ValueError):
        return None
    if (
        result != size
        or info.pbi_pid != pid
        or info.pbi_pgid == 0
        or info.pbi_start_tvsec == 0
        or info.pbi_start_tvusec >= 1_000_000
    ):
        return None
    return f"darwin:{info.pbi_start_tvsec}:{info.pbi_start_tvusec}"


def _darwin_process_executable_path(pid: int) -> str | None:
    if sys.platform != "darwin":
        return None
    try:
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        proc_pidpath = library.proc_pidpath
        proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        proc_pidpath.restype = ctypes.c_int
        buffer = ctypes.create_string_buffer(DARWIN_PROCESS_PATH_MAX_BYTES)
        length = proc_pidpath(pid, buffer, len(buffer))
    except (AttributeError, OSError, ValueError):
        return None
    if length <= 0 or length >= len(buffer):
        return None
    path = os.fsdecode(buffer.value)
    return path if path.startswith("/") and "\x00" not in path else None


def _lsof_fd(
    pid: int,
    descriptor: int,
    deadline: float,
) -> NativeFileDescriptorObservation:
    empty = NativeFileDescriptorObservation(None, None, None, None, None, None)
    result = _run_probe(
        [
            "lsof",
            "-nP",
            "-a",
            "-p",
            str(pid),
            "-d",
            str(descriptor),
            "-FnftsTDi",
        ],
        deadline=deadline,
        timeout_cap=5,
    )
    if result is None:
        return empty
    if result.returncode != 0:
        return empty
    file_type = next((line[1:] for line in result.stdout.splitlines() if line.startswith("t")), None)
    name = next((line[1:] for line in result.stdout.splitlines() if line.startswith("n")), None)
    size_text = next(
        (line[1:] for line in result.stdout.splitlines() if line.startswith("s")),
        None,
    )
    try:
        size = int(size_text) if size_text is not None else None
    except ValueError:
        size = None
    tcp_state = next(
        (
            line.removeprefix("TST=")
            for line in result.stdout.splitlines()
            if line.startswith("TST=")
        ),
        None,
    )
    device_text = next(
        (line[1:] for line in result.stdout.splitlines() if line.startswith("D")),
        None,
    )
    inode_text = next(
        (line[1:] for line in result.stdout.splitlines() if line.startswith("i")),
        None,
    )
    try:
        device = int(device_text, 16) if device_text is not None else None
        inode = int(inode_text) if inode_text is not None else None
    except ValueError:
        device = None
        inode = None
    return NativeFileDescriptorObservation(
        file_type=file_type,
        name=name,
        size=size,
        tcp_state=tcp_state,
        device=device,
        inode=inode,
    )


def _is_loopback_listener(observation: NativeFileDescriptorObservation) -> bool:
    if observation.file_type != "IPv4" or observation.name is None:
        return False
    if observation.tcp_state != "LISTEN" and not observation.name.endswith(" (LISTEN)"):
        return False
    normalized_name = observation.name.removesuffix(" (LISTEN)")
    matched = re.search(r"(?:^|\s)127\.0\.0\.1:([1-9][0-9]{0,4})$", normalized_name)
    return matched is not None and int(matched.group(1)) <= 65_535


def _macos_native_evidence(
    app_pid: int,
    executable: Path,
    sidecar: Path,
    mach_o: dict[str, object],
    expected_sidecar_digest: str,
    native_observation: NativeHostObservation,
    deadline: float,
    cleanup_process_groups: set[int] | None = None,
) -> tuple[dict[str, object] | None, set[int], str]:
    process_groups = set(native_observation.process_groups)
    if cleanup_process_groups is not None:
        cleanup_process_groups.update(process_groups)
    if time.monotonic() >= deadline:
        return None, process_groups, PROBE_DEADLINE_STAGE
    try:
        rows = _descendants(app_pid, deadline)
    except SmokeFailure:
        if time.monotonic() >= deadline:
            return None, process_groups, PROBE_DEADLINE_STAGE
        raise
    if time.monotonic() >= deadline:
        return None, process_groups, PROBE_DEADLINE_STAGE
    process_groups.update(row[2] for row in rows if row[2] > 0)
    if cleanup_process_groups is not None:
        cleanup_process_groups.update(process_groups)
    marker = native_observation.active_process
    if marker is None:
        return None, process_groups, "native_marker_absent"

    expected_sidecar_size = _file_identity(sidecar)[2]
    if (
        marker.executable_sha256 != expected_sidecar_digest
        or marker.executable_size != expected_sidecar_size
    ):
        raise SmokeFailure("Native host process marker identifies a different packaged sidecar")

    marker_row = next((row for row in rows if row[0] == marker.pid), None)
    if marker_row is None:
        return None, process_groups, "native_process_unavailable"
    try:
        observed_group = os.getpgid(marker.pid)
        observed_session = os.getsid(marker.pid)
    except (OSError, ProcessLookupError):
        return None, process_groups, "native_process_unavailable"
    if (
        marker_row[2] != marker.process_group
        or observed_group != marker.process_group
        or observed_session != marker.session_id
    ):
        raise SmokeFailure("Native host sidecar process identity changed during smoke")
    observed_birth_identity = _darwin_process_birth_identity(marker.pid)
    if time.monotonic() >= deadline:
        return None, process_groups, PROBE_DEADLINE_STAGE
    if observed_birth_identity is None:
        return None, process_groups, "native_process_unavailable"
    if observed_birth_identity != marker.birth_identity:
        raise SmokeFailure("Native host sidecar birth identity changed during smoke")
    bundled_identity = sidecar.stat()
    if (
        marker.executable_device != bundled_identity.st_dev
        or marker.executable_inode != bundled_identity.st_ino
        or marker.executable_size != bundled_identity.st_size
    ):
        raise SmokeFailure(
            "Native host did not execute the verified sidecar inside the macOS app bundle"
        )
    if sys.platform == "darwin":
        process_path = _darwin_process_executable_path(marker.pid)
        if process_path is None:
            return None, process_groups, "native_process_unavailable"
        process_image = Path(process_path)
        if (
            os.path.realpath(process_image) != os.path.realpath(sidecar)
            or _file_identity(process_image) != _file_identity(sidecar)
        ):
            raise SmokeFailure(
                "Native host sidecar process image is not the verified app-bundle executable"
            )

    listener_seen = False
    sidecar_rows = [row for row in rows if row[2] == marker.process_group]
    if len(sidecar_rows) > NATIVE_GROUP_MAX_PROCESSES:
        raise SmokeFailure("Native host sidecar process group exceeded the observation limit")
    for pid, _parent, _group, _command in sidecar_rows:
        if time.monotonic() >= deadline:
            return None, process_groups, PROBE_DEADLINE_STAGE
        listener = _lsof_fd(pid, 3, deadline)
        if time.monotonic() >= deadline:
            return None, process_groups, PROBE_DEADLINE_STAGE
        executable_fd = _lsof_fd(pid, 4, deadline)
        if time.monotonic() >= deadline:
            return None, process_groups, PROBE_DEADLINE_STAGE
        listener_ready = _is_loopback_listener(listener)
        listener_seen = listener_seen or listener_ready
        if not listener_ready:
            continue
        if (
            executable_fd.file_type != "REG"
            or executable_fd.size != marker.executable_size
            or executable_fd.device != marker.executable_device
            or executable_fd.inode != marker.executable_inode
        ):
            continue
        if not native_observation.renderer_ready:
            return None, process_groups, "renderer_ack_absent"
        return (
            {
                "schema_version": EVIDENCE_SCHEMA_VERSION,
                "native_executable": executable.name,
                "bundled_external_bin": sidecar.name,
                "renderer_ready": True,
                "sidecar_ready": True,
                "bundled_external_bin_resolved": True,
                "native_listener_fd_handoff": True,
                "native_executable_fd_handoff": True,
                "mach_o": mach_o,
            },
            process_groups,
            "ready",
        )
    return (
        None,
        process_groups,
        "executable_fd_unavailable" if listener_seen else "listener_fd_unavailable",
    )


def _advance_readiness_stage(
    current: str,
    observed: set[str],
    candidate: str,
) -> str:
    if candidate == PROBE_DEADLINE_STAGE:
        return current
    if current not in NATIVE_FAILURE_STAGES or candidate not in NATIVE_FAILURE_STAGES:
        raise SmokeFailure("Native host readiness stage is invalid")
    observed.add(candidate)
    if _NATIVE_READINESS_STAGE_RANK[candidate] > _NATIVE_READINESS_STAGE_RANK[current]:
        return candidate
    return current


def _cleanup_launched_app(
    process: subprocess.Popen[bytes],
    process_groups: set[int],
    *,
    timeout_seconds: float,
) -> bool:
    termination_deadline = time.monotonic() + min(5.0, max(0.1, timeout_seconds))
    has_unreaped_child_authority = process.returncode is None
    if has_unreaped_child_authority:
        _kill_groups({process.pid}, signal.SIGTERM)
        while time.monotonic() < termination_deadline:
            if not _running_process_groups({process.pid}, termination_deadline):
                break
            time.sleep(0.05)
        if _running_process_groups({process.pid}, time.monotonic() + 0.5):
            _kill_groups({process.pid}, signal.SIGKILL)
        try:
            process.wait(timeout=PROBE_REAP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=PROBE_REAP_TIMEOUT_SECONDS)
    cleanup_deadline = time.monotonic() + min(15.0, max(2.0, timeout_seconds))
    return _wait_for_groups_to_exit(process_groups, cleanup_deadline)


def _read_emitted_evidence(
    evidence_path: Path,
    *,
    nonce: str,
    executable: Path,
    sidecar: Path,
) -> dict[str, object] | None:
    if not evidence_path.is_file():
        return None
    try:
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SmokeFailure("App release smoke evidence is unreadable") from exc
    if type(payload) is not dict or payload.get("nonce") != nonce:
        raise SmokeFailure("App release smoke evidence nonce does not match this launch")
    if payload.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
        raise SmokeFailure("App release smoke evidence schema is unsupported")
    if payload.get("native_executable") != executable.name:
        raise SmokeFailure("App release smoke evidence names the wrong native executable")
    if payload.get("bundled_external_bin") != sidecar.name:
        raise SmokeFailure("App release smoke evidence names the wrong externalBin")
    if any(payload.get(field) is not True for field in REQUIRED_BOOLEAN_EVIDENCE):
        raise SmokeFailure("App release smoke evidence is incomplete")
    payload["mach_o"] = _validate_mach_o_evidence(payload.get("mach_o"))
    return payload


def _write_evidence(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _validate_renderer_completion_stages(stages: set[str]) -> None:
    failures = stages & RENDERER_FAILURE_STAGES
    if failures:
        raise SmokeFailure("Renderer reported a release startup failure stage")
    if not REQUIRED_RENDERER_COMPLETION_STAGES.issubset(stages):
        raise SmokeFailure("Renderer readiness omitted required release startup stages")


def _validate_existing_home(path: Path) -> Path:
    if not path.is_absolute():
        raise SmokeFailure("Existing smoke HOME must be absolute")
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise SmokeFailure("Existing smoke HOME is unavailable") from exc
    if (
        path.is_symlink()
        or not path.is_dir()
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022 != 0
    ):
        raise SmokeFailure("Existing smoke HOME is not an owner-controlled directory")
    return resolved


def smoke_bundle(
    bundle_root: Path,
    *,
    launch_origin: str,
    source_dmg: Path,
    timeout_seconds: float,
    evidence_out: Path | None = None,
    existing_home: Path | None = None,
) -> dict[str, object]:
    if timeout_seconds <= 0:
        raise SmokeFailure("Bundle smoke timeout must be positive")
    executable = find_app_executable(bundle_root)
    sidecar = find_bundled_sidecar(bundle_root)
    if launch_origin not in LAUNCH_ORIGINS:
        raise SmokeFailure("App smoke launch origin is unsupported")
    source_dmg_identity = _source_dmg_identity(source_dmg)
    binary_sha256 = {
        "native_executable": _sha256(executable),
        "bundled_external_bin": _sha256(sidecar),
    }
    binary_identities = {
        "native_executable": _file_identity(executable),
        "bundled_external_bin": _file_identity(sidecar),
    }
    mach_o = (
        {
            "native_executable": inspect_mach_o(executable),
            "bundled_external_bin": inspect_mach_o(sidecar),
        }
        if sys.platform == "darwin"
        else None
    )
    app = _app_bundle(bundle_root)
    if sys.platform == "darwin":
        _validate_macos_loopback_transport(app)
        _validate_macos_app_icon(app)
    nonce = os.urandom(32).hex()
    with tempfile.TemporaryDirectory(prefix="openevo-desktop-app-smoke-") as temporary:
        smoke_root = Path(temporary)
        emitted_path = smoke_root / "app-evidence.json"
        if existing_home is None:
            home = smoke_root / "home"
            home.mkdir(mode=0o700)
        else:
            home = _validate_existing_home(existing_home)
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(home),
                "OPENEVO_RELEASE_SMOKE_EVIDENCE_PATH": str(emitted_path),
                "OPENEVO_RELEASE_SMOKE_NONCE": nonce,
            }
        )
        try:
            process = subprocess.Popen(
                [str(executable)],
                cwd=app.parent,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise SmokeFailure("OpenEvo Desktop native executable could not be launched") from exc
        if process.stderr is None:
            process.kill()
            process.wait(timeout=5)
            raise SmokeFailure("Native host smoke diagnostics are unavailable")
        try:
            os.set_blocking(process.stderr.fileno(), False)
        except OSError as exc:
            process.kill()
            process.wait(timeout=5)
            process.stderr.close()
            raise SmokeFailure("Native host smoke diagnostics are unavailable") from exc
        native_stderr_buffer = bytearray()
        process_groups = {process.pid}
        evidence: dict[str, object] | None = None
        readiness_stage = "native_marker_absent"
        observed_readiness_stages = {readiness_stage}
        observed_renderer_stages: set[str] = set()
        deadline = time.monotonic() + timeout_seconds
        captured_error: BaseException | None = None
        try:
            while time.monotonic() < deadline:
                native_observation = _drain_native_host_stderr(
                    process.stderr,
                    native_stderr_buffer,
                    process_groups,
                )
                observed_renderer_stages.update(native_observation.renderer_stages)
                if sys.platform == "darwin":
                    if process.poll() is None:
                        evidence, observed_groups, candidate_stage = _macos_native_evidence(
                            process.pid,
                            executable,
                            sidecar,
                            mach_o,
                            binary_sha256["bundled_external_bin"],
                            native_observation,
                            deadline,
                            process_groups,
                        )
                        process_groups.update(observed_groups)
                        if evidence is not None:
                            _validate_renderer_completion_stages(observed_renderer_stages)
                            break
                        readiness_stage = _advance_readiness_stage(
                            readiness_stage,
                            observed_readiness_stages,
                            candidate_stage,
                        )
                else:
                    evidence = _read_emitted_evidence(
                        emitted_path,
                        nonce=nonce,
                        executable=executable,
                        sidecar=sidecar,
                    )
                    if evidence is not None:
                        break
                if process.poll() is not None:
                    raise SmokeFailure(
                        "OpenEvo Desktop native executable exited before smoke evidence was ready"
                    )
                time.sleep(0.1)
            if evidence is None:
                raise SmokeFailure(
                    "Timed out waiting for OpenEvo Desktop app smoke evidence "
                    f"(stage={readiness_stage}; observed_stages="
                    f"{','.join(sorted(observed_readiness_stages))}; renderer_stages="
                    f"{','.join(sorted(observed_renderer_stages)) or 'none'})"
                )
        except BaseException as exc:
            captured_error = exc
        finally:
            process_group_cleanup = _cleanup_launched_app(
                process,
                process_groups,
                timeout_seconds=timeout_seconds,
            )
            process.stderr.close()
        if not process_group_cleanup:
            cleanup_error = SmokeFailure(
                "OpenEvo Desktop left an app or sidecar process group running"
            )
            if captured_error is not None:
                raise cleanup_error from captured_error
            raise cleanup_error
        if captured_error is not None:
            raise captured_error
        assert evidence is not None
        _verify_binary_unchanged(
            executable,
            expected_identity=binary_identities["native_executable"],
            expected_digest=binary_sha256["native_executable"],
        )
        _verify_binary_unchanged(
            sidecar,
            expected_identity=binary_identities["bundled_external_bin"],
            expected_digest=binary_sha256["bundled_external_bin"],
        )
        evidence.pop("nonce", None)
        evidence["process_group_cleanup"] = True
        evidence["launch_origin"] = launch_origin
        evidence["source_dmg"] = source_dmg_identity
        evidence["binary_sha256"] = binary_sha256
        if evidence_out is not None:
            _write_evidence(evidence_out, evidence)
        return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle_root", type=Path)
    parser.add_argument("--launch-origin", choices=sorted(LAUNCH_ORIGINS), required=True)
    parser.add_argument("--source-dmg", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--evidence-out", type=Path)
    parser.add_argument(
        "--existing-home",
        type=Path,
        help="Use an existing owner-controlled HOME for an explicit state-compatibility smoke.",
    )
    args = parser.parse_args(argv)

    try:
        evidence = smoke_bundle(
            args.bundle_root,
            launch_origin=args.launch_origin,
            source_dmg=args.source_dmg,
            timeout_seconds=args.timeout_seconds,
            evidence_out=args.evidence_out,
            existing_home=args.existing_home,
        )
    except SmokeFailure as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(evidence, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
