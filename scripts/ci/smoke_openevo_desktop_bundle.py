#!/usr/bin/env python3
"""Smoke the sidecar and native renderer bundled in an OpenEvo Desktop app."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import os
from pathlib import Path
import plistlib
import re
import signal
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from smoke_openevo_desktop_sidecar import SmokeFailure, smoke_sidecar  # noqa: E402


SIDECAR_NAME = "openevo-desktop-sidecar"
APP_BUNDLE_NAME = "OpenEvo Desktop.app"
RENDERER_READY_PREFIX = "OPENEVO_DESKTOP_RENDERER_READY_V1"
SIDECAR_PROCESS_PREFIX = "OPENEVO_DESKTOP_SIDECAR_PROCESS_V1"
REPO_ROOT = SCRIPT_DIR.parent.parent
DESKTOP_OPENAPI = REPO_ROOT / "desktop/sidecar/contracts/v1/openapi.json"
DESKTOP_OPENAPI_SHA256 = hashlib.sha256(DESKTOP_OPENAPI.read_bytes()).hexdigest()
RENDERER_READY_MARKER = f"{RENDERER_READY_PREFIX} {DESKTOP_OPENAPI_SHA256}"
NATIVE_LOG_LIMIT = 64 * 1024
NATIVE_LOG_READ_SIZE = 16 * 1024
NATIVE_GROUP_TERM_TIMEOUT_SECONDS = 5.0
NATIVE_GROUP_KILL_TIMEOUT_SECONDS = 5.0
SIDECAR_GROUP_EXIT_TIMEOUT_SECONDS = 5.0
SIDECAR_GROUP_TERM_TIMEOUT_SECONDS = 0.5
SIDECAR_GROUP_KILL_TIMEOUT_SECONDS = 5.0
_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
)
_FILE_OPEN_FLAGS = (
    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
)


def _find_app_bundle(bundle_root: Path) -> Path:
    if not bundle_root.exists():
        raise SmokeFailure(f"OpenEvo Desktop bundle root does not exist: {bundle_root}")

    app_bundle = (
        bundle_root if bundle_root.name == APP_BUNDLE_NAME else bundle_root / APP_BUNDLE_NAME
    )
    if not app_bundle.is_dir():
        raise SmokeFailure(f"No {APP_BUNDLE_NAME} bundle found under {bundle_root}")
    return app_bundle


def find_bundled_sidecar(bundle_root: Path) -> Path:
    app_bundle = _find_app_bundle(bundle_root)

    contents = app_bundle / "Contents"
    candidates = [
        contents / "MacOS" / SIDECAR_NAME,
        contents / "Resources" / SIDECAR_NAME,
        contents / "Resources" / "binaries" / SIDECAR_NAME,
    ]
    candidates = [path for path in candidates if path.is_file()]
    if not candidates:
        raise SmokeFailure(
            f"No bundled OpenEvo Desktop sidecar executable found under {app_bundle}"
        )

    for path in candidates:
        if path.stat().st_mode & 0o111:
            return path

    candidate_names = ", ".join(str(path) for path in candidates)
    raise SmokeFailure(
        f"Bundled OpenEvo Desktop sidecar candidate(s) are not executable: {candidate_names}"
    )


def find_native_executable(bundle_root: Path) -> Path:
    app_bundle = _find_app_bundle(bundle_root)
    with _PinnedNativeExecutable.open(app_bundle) as pinned:
        pinned.validate()
        return pinned.path


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _validate_darwin_component(
    metadata: os.stat_result,
    *,
    kind: str,
    path: Path,
) -> None:
    effective_uid = os.geteuid()
    trusted_owner = metadata.st_uid in ({0} if effective_uid == 0 else {0, effective_uid})
    writable_by_group_or_other = metadata.st_mode & 0o022 != 0
    is_directory = stat.S_ISDIR(metadata.st_mode)
    is_regular = stat.S_ISREG(metadata.st_mode)
    valid_kind = is_directory if kind == "directory" else is_regular
    valid_links = kind == "directory" or metadata.st_nlink == 1
    executable = kind != "executable" or metadata.st_mode & 0o111 != 0
    if (
        not trusted_owner
        or writable_by_group_or_other
        or not valid_kind
        or not valid_links
        or not executable
    ):
        raise SmokeFailure(
            f"OpenEvo Desktop native executable path is not trustworthy: {path}"
        )


def _validate_component(metadata: os.stat_result, *, kind: str, path: Path) -> None:
    if sys.platform == "darwin":
        _validate_darwin_component(metadata, kind=kind, path=path)


class _PinnedNativeExecutable:
    def __init__(
        self,
        *,
        app_bundle: Path,
        executable_name: str,
        descriptors: list[int],
        bindings: list[tuple[int, str, int, tuple[int, ...]]],
    ) -> None:
        self.app_bundle = app_bundle
        self.path = app_bundle / "Contents" / "MacOS" / executable_name
        self._descriptors = descriptors
        self._bindings = bindings

    @classmethod
    def open(cls, app_bundle: Path) -> _PinnedNativeExecutable:
        if sys.platform == "darwin":
            # macOS commonly exposes /private/var through the root-owned /var
            # alias. Resolve only the parent; the app bundle itself remains a
            # no-follow component in the verified chain.
            app_bundle = Path(os.path.realpath(app_bundle.parent)) / app_bundle.name
        descriptors: list[int] = []
        bindings: list[tuple[int, str, int, tuple[int, ...]]] = []

        def open_component(
            parent_fd: int,
            name: str,
            flags: int,
            *,
            kind: str,
            path: Path,
        ) -> int:
            try:
                descriptor = os.open(name, flags, dir_fd=parent_fd)
                descriptors.append(descriptor)
                metadata = os.fstat(descriptor)
                _validate_component(metadata, kind=kind, path=path)
                expected = _file_identity(metadata)
                linked = _file_identity(
                    os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                )
            except OSError as exc:
                raise SmokeFailure(
                    f"OpenEvo Desktop native executable path is not trustworthy: {app_bundle}"
                ) from exc
            if linked != expected:
                raise SmokeFailure(
                    f"OpenEvo Desktop native executable path changed: {app_bundle}"
                )
            bindings.append((parent_fd, name, descriptor, expected))
            return descriptor

        try:
            if sys.platform == "darwin":
                if not app_bundle.is_absolute():
                    raise SmokeFailure(
                        f"OpenEvo Desktop native executable path is not trustworthy: {app_bundle}"
                    )
                current_path = Path("/")
                parent_fd = os.open(current_path, _DIRECTORY_OPEN_FLAGS)
                descriptors.append(parent_fd)
                _validate_component(
                    os.fstat(parent_fd), kind="directory", path=current_path
                )
                for component in app_bundle.parent.parts[1:]:
                    current_path /= component
                    parent_fd = open_component(
                        parent_fd,
                        component,
                        _DIRECTORY_OPEN_FLAGS,
                        kind="directory",
                        path=current_path,
                    )
            else:
                parent_fd = os.open(app_bundle.parent, _DIRECTORY_OPEN_FLAGS)
                descriptors.append(parent_fd)
            app_fd = open_component(
                parent_fd,
                app_bundle.name,
                _DIRECTORY_OPEN_FLAGS,
                kind="directory",
                path=app_bundle,
            )
            contents_fd = open_component(
                app_fd,
                "Contents",
                _DIRECTORY_OPEN_FLAGS,
                kind="directory",
                path=app_bundle / "Contents",
            )
            info_fd = open_component(
                contents_fd,
                "Info.plist",
                _FILE_OPEN_FLAGS,
                kind="info",
                path=app_bundle / "Contents" / "Info.plist",
            )
            info_path = app_bundle / "Contents" / "Info.plist"
            if not stat.S_ISREG(os.fstat(info_fd).st_mode):
                raise SmokeFailure(f"OpenEvo Desktop has an invalid Info.plist: {info_path}")
            try:
                with os.fdopen(os.dup(info_fd), "rb") as info_file:
                    info = plistlib.load(info_file)
            except (OSError, plistlib.InvalidFileException, ValueError) as exc:
                raise SmokeFailure(
                    f"OpenEvo Desktop has an invalid Info.plist: {info_path}"
                ) from exc
            if not isinstance(info, dict):
                raise SmokeFailure(
                    "OpenEvo Desktop Info.plist must contain a top-level dictionary"
                )
            executable_name = info.get("CFBundleExecutable")
            if (
                not isinstance(executable_name, str)
                or not executable_name
                or executable_name in {".", ".."}
                or Path(executable_name).name != executable_name
                or "\x00" in executable_name
            ):
                raise SmokeFailure(
                    "OpenEvo Desktop Info.plist has an invalid CFBundleExecutable"
                )
            macos_fd = open_component(
                contents_fd,
                "MacOS",
                _DIRECTORY_OPEN_FLAGS,
                kind="directory",
                path=app_bundle / "Contents" / "MacOS",
            )
            executable_fd = open_component(
                macos_fd,
                executable_name,
                _FILE_OPEN_FLAGS,
                kind="executable",
                path=app_bundle / "Contents" / "MacOS" / executable_name,
            )
            metadata = os.fstat(executable_fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o111 == 0:
                raise SmokeFailure(
                    "OpenEvo Desktop native executable is not executable: "
                    f"{app_bundle / 'Contents' / 'MacOS' / executable_name}"
                )
            pinned = cls(
                app_bundle=app_bundle,
                executable_name=executable_name,
                descriptors=descriptors,
                bindings=bindings,
            )
            pinned.validate()
            return pinned
        except BaseException:
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise

    @property
    def executable_fd(self) -> int:
        return self._bindings[-1][2]

    @property
    def inherited_fds(self) -> tuple[int, ...]:
        return tuple(self._descriptors)

    def execution_path(self) -> str:
        descriptor = self.executable_fd
        if sys.platform == "darwin":
            return str(self.path)
        if Path("/proc/self/fd").is_dir():
            return f"/proc/self/fd/{descriptor}"
        raise SmokeFailure("OpenEvo Desktop cannot execute the pinned native executable")

    def validate(self) -> None:
        try:
            for parent_fd, name, descriptor, expected in self._bindings:
                if _file_identity(os.fstat(descriptor)) != expected:
                    raise SmokeFailure(
                        f"OpenEvo Desktop native executable authority changed: {self.path}"
                    )
                linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if _file_identity(linked) != expected:
                    raise SmokeFailure(
                        f"OpenEvo Desktop native executable path changed: {self.path}"
                    )
        except OSError as exc:
            raise SmokeFailure(
                f"OpenEvo Desktop native executable path changed: {self.path}"
            ) from exc

    def prepare_exec(self) -> None:
        self.validate()
        for descriptor in self._descriptors:
            if sys.platform == "darwin" or descriptor != self.executable_fd:
                os.close(descriptor)

    def close(self) -> None:
        while self._descriptors:
            descriptor = self._descriptors.pop()
            try:
                os.close(descriptor)
            except OSError:
                pass

    def __enter__(self) -> _PinnedNativeExecutable:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class _NativeLogReader:
    def __init__(self, log_path: Path) -> None:
        self._log_path = log_path
        self._offset = 0
        self._pending = bytearray()
        self._discarding_oversized_line = False
        self._tail = bytearray()

    def _consume_part(self, part: bytes, *, complete: bool, lines: list[str]) -> None:
        if self._discarding_oversized_line:
            if complete:
                self._discarding_oversized_line = False
            return
        self._pending.extend(part)
        if len(self._pending) > NATIVE_LOG_LIMIT:
            self._pending.clear()
            self._discarding_oversized_line = not complete
            return
        if complete:
            lines.append(self._pending.decode("utf-8", errors="replace"))
            self._pending.clear()

    def read_complete_lines(self) -> list[str]:
        lines: list[str] = []
        try:
            with self._log_path.open("rb") as handle:
                handle.seek(self._offset)
                while chunk := handle.read(NATIVE_LOG_READ_SIZE):
                    self._offset += len(chunk)
                    self._tail.extend(chunk)
                    if len(self._tail) > NATIVE_LOG_LIMIT:
                        del self._tail[:-NATIVE_LOG_LIMIT]
                    parts = chunk.split(b"\n")
                    for part in parts[:-1]:
                        self._consume_part(part, complete=True, lines=lines)
                    self._consume_part(parts[-1], complete=False, lines=lines)
        except OSError:
            return lines
        return lines

    def tail_text(self) -> str:
        return self._tail.decode("utf-8", errors="replace")


class _SidecarProcessEvidence:
    __slots__ = (
        "instance_id",
        "pid",
        "process_group",
        "session_id",
        "birth_identity",
    )

    def __init__(
        self,
        *,
        instance_id: str,
        pid: int,
        process_group: int,
        session_id: int,
        birth_identity: str,
    ) -> None:
        self.instance_id = instance_id
        self.pid = pid
        self.process_group = process_group
        self.session_id = session_id
        self.birth_identity = birth_identity


_SIDECAR_INSTANCE_ID = re.compile(r"^[0-9a-f]{32}$")
_LINUX_BIRTH_IDENTITY = re.compile(r"^linux:[1-9][0-9]*$")
_DARWIN_BIRTH_IDENTITY = re.compile(r"^darwin:[1-9][0-9]*:[0-9]{1,6}$")


def _parse_sidecar_process_marker(line: str) -> _SidecarProcessEvidence | None:
    if not line.startswith(f"{SIDECAR_PROCESS_PREFIX} "):
        return None
    parts = line.split(" ")
    if len(parts) != 6 or any(not part for part in parts):
        raise SmokeFailure("OpenEvo Desktop reported invalid sidecar process evidence")
    _, instance_id, pid_text, process_group_text, session_id_text, birth_identity = parts
    try:
        pid = int(pid_text)
        process_group = int(process_group_text)
        session_id = int(session_id_text)
    except ValueError as exc:
        raise SmokeFailure(
            "OpenEvo Desktop reported invalid sidecar process evidence"
        ) from exc
    birth_pattern = (
        _DARWIN_BIRTH_IDENTITY if sys.platform == "darwin" else _LINUX_BIRTH_IDENTITY
    )
    if (
        not _SIDECAR_INSTANCE_ID.fullmatch(instance_id)
        or pid <= 1
        or process_group != pid
        or session_id != pid
        or not birth_pattern.fullmatch(birth_identity)
    ):
        raise SmokeFailure("OpenEvo Desktop reported invalid sidecar process evidence")
    return _SidecarProcessEvidence(
        instance_id=instance_id,
        pid=pid,
        process_group=process_group,
        session_id=session_id,
        birth_identity=birth_identity,
    )


def _linux_process_birth_identity(pid: int) -> str:
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        suffix = stat_text.rsplit(")", 1)[1].split()
        start_ticks = suffix[19]
    except (IndexError, OSError) as exc:
        raise SmokeFailure("OpenEvo Desktop sidecar process identity could not be verified") from exc
    if not start_ticks.isascii() or not start_ticks.isdigit() or int(start_ticks) <= 0:
        raise SmokeFailure("OpenEvo Desktop sidecar process identity could not be verified")
    return f"linux:{start_ticks}"


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


def _darwin_process_birth_identity(pid: int) -> str:
    info = _DarwinProcBsdInfo()
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        proc_pidinfo = libproc.proc_pidinfo
        proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        proc_pidinfo.restype = ctypes.c_int
        result = proc_pidinfo(
            pid,
            3,  # PROC_PIDTBSDINFO
            0,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
    except (AttributeError, OSError) as exc:
        raise SmokeFailure("OpenEvo Desktop sidecar process identity could not be verified") from exc
    if (
        result != ctypes.sizeof(info)
        or info.pbi_pid != pid
        or info.pbi_start_tvsec == 0
        or info.pbi_start_tvusec >= 1_000_000
    ):
        raise SmokeFailure("OpenEvo Desktop sidecar process identity could not be verified")
    return f"darwin:{info.pbi_start_tvsec}:{info.pbi_start_tvusec}"


def _process_birth_identity(pid: int) -> str:
    if sys.platform == "darwin":
        return _darwin_process_birth_identity(pid)
    if Path("/proc").is_dir():
        return _linux_process_birth_identity(pid)
    raise SmokeFailure("OpenEvo Desktop sidecar process identity is unsupported")


def _validate_sidecar_process_evidence(
    evidence: _SidecarProcessEvidence,
    *,
    native_process_group: int,
) -> None:
    try:
        actual_process_group = os.getpgid(evidence.pid)
        actual_session_id = os.getsid(evidence.pid)
    except OSError as exc:
        raise SmokeFailure("OpenEvo Desktop sidecar process identity could not be verified") from exc
    if (
        evidence.process_group == native_process_group
        or actual_process_group != evidence.process_group
        or actual_session_id != evidence.session_id
        or _process_birth_identity(evidence.pid) != evidence.birth_identity
    ):
        raise SmokeFailure("OpenEvo Desktop sidecar process identity could not be verified")


def _wait_for_process_group_exit(process_group: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while _native_process_group_exists(process_group):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def _verify_sidecar_process_group_exit(evidence: _SidecarProcessEvidence) -> None:
    if _wait_for_process_group_exit(
        evidence.process_group, SIDECAR_GROUP_EXIT_TIMEOUT_SECONDS
    ):
        return
    leader_is_gone = False
    try:
        leader_group = os.getpgid(evidence.pid)
        leader_session = os.getsid(evidence.pid)
    except ProcessLookupError:
        leader_is_gone = True
    except OSError as exc:
        raise SmokeFailure(
            "OpenEvo Desktop sidecar process group survived but could not be inspected"
        ) from exc
    if not leader_is_gone and (
        leader_group != evidence.process_group
        or leader_session != evidence.session_id
        or _process_birth_identity(evidence.pid) != evidence.birth_identity
    ):
        raise SmokeFailure(
            "OpenEvo Desktop sidecar process group survived but its leader identity changed"
        )
    _signal_native_process_group(evidence.process_group, signal.SIGTERM)
    if _wait_for_process_group_exit(
        evidence.process_group, SIDECAR_GROUP_TERM_TIMEOUT_SECONDS
    ):
        raise SmokeFailure(
            "OpenEvo Desktop sidecar process group survived the Rust watchdog "
            "and required TERM cleanup"
        )
    _signal_native_process_group(evidence.process_group, signal.SIGKILL)
    cleaned = _wait_for_process_group_exit(
        evidence.process_group, SIDECAR_GROUP_KILL_TIMEOUT_SECONDS
    )
    if not cleaned:
        raise SmokeFailure(
            "OpenEvo Desktop sidecar process group survived active cleanup"
        )
    raise SmokeFailure(
        "OpenEvo Desktop sidecar process group survived the Rust watchdog and required "
        "TERM/KILL cleanup"
    )


def _native_process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except OSError as exc:
        raise SmokeFailure("OpenEvo Desktop native process group could not be inspected") from exc
    return True


def _wait_for_native_process_group_exit(
    process: subprocess.Popen[bytes],
    process_group: int,
    timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        process.poll()
        if not _native_process_group_exists(process_group):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _signal_native_process_group(process_group: int, group_signal: signal.Signals) -> None:
    try:
        os.killpg(process_group, group_signal)
    except ProcessLookupError:
        return
    except OSError as exc:
        raise SmokeFailure("OpenEvo Desktop native process group could not be stopped") from exc


def _terminate_native_process(process: subprocess.Popen[bytes]) -> None:
    process_group = process.pid
    if not _native_process_group_exists(process_group):
        process.poll()
        return
    _signal_native_process_group(process_group, signal.SIGTERM)
    if _wait_for_native_process_group_exit(
        process,
        process_group,
        NATIVE_GROUP_TERM_TIMEOUT_SECONDS,
    ):
        return
    _signal_native_process_group(process_group, signal.SIGKILL)
    if not _wait_for_native_process_group_exit(
        process,
        process_group,
        NATIVE_GROUP_KILL_TIMEOUT_SECONDS,
    ):
        raise SmokeFailure("OpenEvo Desktop native process group survived TERM/KILL cleanup")


def smoke_native_app(bundle_root: Path, *, timeout_seconds: float) -> Path:
    app_bundle = _find_app_bundle(bundle_root)
    with _PinnedNativeExecutable.open(app_bundle) as pinned, TemporaryDirectory(
        prefix="openevo-native-smoke-"
    ) as temporary:
        executable = pinned.path
        log_path = Path(temporary) / "native.log"
        try:
            with log_path.open("wb") as output:
                pinned.validate()
                process = subprocess.Popen(
                    [str(executable)],
                    executable=pinned.execution_path(),
                    cwd=pinned.app_bundle.parent,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    pass_fds=pinned.inherited_fds,
                    preexec_fn=pinned.prepare_exec,
                )
                pinned.validate()
        except (OSError, subprocess.SubprocessError) as exc:
            raise SmokeFailure(
                f"OpenEvo Desktop native executable could not start: {executable}"
            ) from exc
        log_reader = _NativeLogReader(log_path)
        deadline = time.monotonic() + timeout_seconds
        sidecar_evidence: _SidecarProcessEvidence | None = None
        renderer_ready = False

        def capture_native_evidence() -> None:
            nonlocal renderer_ready, sidecar_evidence
            for line in log_reader.read_complete_lines():
                evidence = _parse_sidecar_process_marker(line)
                if evidence is not None:
                    if sidecar_evidence is not None:
                        raise SmokeFailure(
                            "OpenEvo Desktop reported duplicate sidecar process evidence"
                        )
                    _validate_sidecar_process_evidence(
                        evidence, native_process_group=process.pid
                    )
                    sidecar_evidence = evidence
                if line == RENDERER_READY_MARKER:
                    renderer_ready = True

        try:
            while time.monotonic() < deadline:
                capture_native_evidence()
                if renderer_ready:
                    if sidecar_evidence is None:
                        raise SmokeFailure(
                            "OpenEvo Desktop renderer readiness omitted sidecar process evidence"
                        )
                    time.sleep(0.25)
                    if process.poll() is not None:
                        raise SmokeFailure(
                            "OpenEvo Desktop exited immediately after renderer readiness"
                        )
                    _validate_sidecar_process_evidence(
                        sidecar_evidence, native_process_group=process.pid
                    )
                    return executable
                exit_code = process.poll()
                if exit_code is not None:
                    capture_native_evidence()
                    raise SmokeFailure(
                        "OpenEvo Desktop exited before renderer readiness "
                        f"(code {exit_code}): {log_reader.tail_text()[-2048:]}"
                    )
                time.sleep(0.1)
            capture_native_evidence()
            raise SmokeFailure(
                "OpenEvo Desktop did not report renderer readiness before timeout: "
                f"{log_reader.tail_text()[-2048:]}"
            )
        finally:
            capture_error: SmokeFailure | None = None
            try:
                capture_native_evidence()
            except SmokeFailure as exc:
                capture_error = exc
            native_cleanup_error: SmokeFailure | None = None
            try:
                _terminate_native_process(process)
            except SmokeFailure as exc:
                native_cleanup_error = exc
            if sidecar_evidence is not None:
                _verify_sidecar_process_group_exit(sidecar_evidence)
            if native_cleanup_error is not None:
                raise native_cleanup_error
            if capture_error is not None:
                raise capture_error


def smoke_bundle(bundle_root: Path, *, timeout_seconds: float) -> Path:
    sidecar = find_bundled_sidecar(bundle_root)
    smoke_sidecar(sidecar, timeout_seconds=timeout_seconds)
    return sidecar


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle_root", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument(
        "--native-app",
        action="store_true",
        help="also launch the native app and require renderer/Tauri/sidecar readiness",
    )
    args = parser.parse_args(argv)

    try:
        sidecar = smoke_bundle(args.bundle_root, timeout_seconds=args.timeout_seconds)
        native = (
            smoke_native_app(args.bundle_root, timeout_seconds=args.timeout_seconds)
            if args.native_app
            else None
        )
    except SmokeFailure as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"OpenEvo Desktop app bundle sidecar smoke passed: {sidecar}")
    if native is not None:
        print(f"OpenEvo Desktop native renderer smoke passed: {native}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
