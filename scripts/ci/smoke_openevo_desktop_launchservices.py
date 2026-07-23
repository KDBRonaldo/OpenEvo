#!/usr/bin/env python3
"""Exercise a quarantined-then-allowed OpenEvo Desktop app through LaunchServices."""

from __future__ import annotations

import argparse
import ctypes
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import signal
import subprocess
import sys
import time
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


APP_NAME_SUFFIX = ".app"
SIDECAR_BASENAME = "openevo-desktop-sidecar"
EVIDENCE_SCHEMA_VERSION = 1
LAUNCH_ORIGIN = "launchservices_open_n_post_quarantine_allow"
MAX_TIMEOUT_SECONDS = 300.0
COMMAND_TIMEOUT_SECONDS = 5.0
HTTP_RESPONSE_MAX_BYTES = 16 * 1024
PROCESS_ROW_LIMIT = 16_384
PROC_PIDPATHINFO_MAXSIZE = 4_096
DESKTOP_LOG_DIRECTORY = Path.home() / "Library/Application Support/org.openevo.desktop/logs-v1"
DESKTOP_LOG_MAX_FILE_BYTES = 2 * 1024 * 1024
DESKTOP_LOG_MAX_FILES = 8
DESKTOP_LOG_MAX_LINE_BYTES = 1024
_DESKTOP_LOG_EVENT_KEYS = {
    "code",
    "errno",
    "event",
    "exit_code",
    "level",
    "occurred_at",
    "schema_version",
    "sequence",
    "signal",
    "source",
}
_LOADER_FAILURE_EVENT_CODE = (
    "embedded_python_loader_python_shared_library_validation_failed"
)
_LOADER_FAILURE = (
    "embedded_python_loader",
    "python_shared_library_validation_failed",
)


class SmokeFailure(RuntimeError):
    """A release acceptance condition was not met."""


class SidecarNotReady(SmokeFailure):
    """The owned listener exists but has not served the version contract yet."""


@dataclass(frozen=True, order=True)
class ProcessIdentity:
    pid: int
    birth: str


@dataclass(frozen=True)
class ProcessRow:
    identity: ProcessIdentity
    parent_pid: int
    command: str


@dataclass(frozen=True)
class Listener:
    owner: ProcessIdentity
    port: int


_PS_ROW = re.compile(
    r"^\s*([1-9][0-9]{0,9})\s+([0-9][0-9]{0,9})\s+"
    r"([A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\d{4})\s+(.+)$"
)
_LOOPBACK_LISTENER = re.compile(r"^(?:127\.0\.0\.1|\[::1\]):([1-9][0-9]{0,4})$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[.+-][A-Za-z0-9._-]+)?$")


def _startup_log_events(log_root: Path) -> tuple[dict[str, object], ...]:
    try:
        root = log_root.lstat()
        if (
            log_root.is_symlink()
            or not log_root.is_dir()
            or root.st_uid != os.geteuid()
            or root.st_mode & 0o777 != 0o700
        ):
            return ()
    except OSError:
        return ()
    names = [f"desktop.{index}.jsonl" for index in range(7, 0, -1)]
    names.append("desktop.jsonl")
    events: list[dict[str, object]] = []
    for name in names[:DESKTOP_LOG_MAX_FILES]:
        path = log_root / name
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return ()
        if (
            path.is_symlink()
            or not path.is_file()
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or metadata.st_mode & 0o777 != 0o600
            or metadata.st_size > DESKTOP_LOG_MAX_FILE_BYTES
        ):
            return ()
        try:
            with path.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino, opened.st_size) != (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_size,
                ):
                    return ()
                payload = stream.read(DESKTOP_LOG_MAX_FILE_BYTES + 1)
                final = os.fstat(stream.fileno())
        except OSError:
            return ()
        if (
            len(payload) > DESKTOP_LOG_MAX_FILE_BYTES
            or (final.st_dev, final.st_ino, final.st_size)
            != (metadata.st_dev, metadata.st_ino, metadata.st_size)
        ):
            return ()
        if payload and not payload.endswith(b"\n"):
            boundary = payload.rfind(b"\n")
            payload = payload[: boundary + 1] if boundary >= 0 else b""
        for line in payload.splitlines():
            if not line:
                continue
            if len(line) > DESKTOP_LOG_MAX_LINE_BYTES:
                return ()
            try:
                event = json.loads(line.decode("utf-8", errors="strict"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return ()
            if type(event) is not dict or set(event) != _DESKTOP_LOG_EVENT_KEYS:
                return ()
            sequence = event.get("sequence")
            if (
                event.get("schema_version") != "1"
                or type(sequence) is not int
                or sequence <= 0
                or type(event.get("event")) is not str
                or type(event.get("source")) is not str
                or type(event.get("level")) is not str
                or not (event.get("code") is None or type(event.get("code")) is str)
            ):
                return ()
            events.append(event)
    events.sort(key=lambda event: int(event["sequence"]))
    return tuple(events)


def _startup_log_checkpoint(log_root: Path) -> int:
    return max(
        (int(event["sequence"]) for event in _startup_log_events(log_root)),
        default=0,
    )


def _startup_failure_since(
    log_root: Path,
    checkpoint: int,
) -> tuple[str, str] | None:
    matches = [
        event
        for event in _startup_log_events(log_root)
        if int(event["sequence"]) > checkpoint
        and event["source"] == "startup"
        and event["level"] == "error"
        and event["event"] == "sidecar_startup_diagnostic"
        and event["code"] == _LOADER_FAILURE_EVENT_CODE
    ]
    return _LOADER_FAILURE if matches else None


def parse_ps_rows(payload: str) -> list[ProcessRow]:
    """Parse the fixed Darwin ps projection without accepting partial rows."""
    rows: list[ProcessRow] = []
    for line in payload.splitlines():
        match = _PS_ROW.fullmatch(line)
        if match is None:
            continue
        pid, parent = int(match.group(1)), int(match.group(2))
        if parent > 2**31 - 1 or pid > 2**31 - 1:
            continue
        rows.append(
            ProcessRow(
                identity=ProcessIdentity(pid, match.group(3)),
                parent_pid=parent,
                command=match.group(4),
            )
        )
        if len(rows) > PROCESS_ROW_LIMIT:
            raise SmokeFailure("Darwin process inventory exceeded the bounded row limit")
    return rows


def parse_lsof_listeners(payload: str, owner: ProcessIdentity) -> list[Listener]:
    """Return only TCP listeners on numeric loopback endpoints for one owned PID."""
    records: list[dict[str, str]] = []
    record: dict[str, str] = {}
    for line in payload.splitlines():
        if not line:
            continue
        field, value = line[0], line[1:]
        if field == "f":
            if record:
                records.append(record)
            record = {"f": value}
        elif field in {"t", "n"} and record and field not in record:
            record[field] = value
        elif field == "T" and record and value == "ST=LISTEN":
            record["listen"] = value
    if record:
        records.append(record)

    listeners: list[Listener] = []
    for record in records:
        endpoint = record.get("n")
        if record.get("t") not in {"IPv4", "IPv6"} or endpoint is None:
            continue
        if "listen" not in record and not endpoint.endswith(" (LISTEN)"):
            continue
        endpoint = endpoint.removesuffix(" (LISTEN)")
        match = _LOOPBACK_LISTENER.fullmatch(endpoint)
        if match is None:
            continue
        port = int(match.group(1))
        if port <= 65_535:
            listeners.append(Listener(owner=owner, port=port))
    return listeners


def _strict_json(payload: bytes) -> object:
    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    return json.loads(payload.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)


def validate_version(payload: object, expected_version: str) -> None:
    required = {
        "schema_version",
        "api_name",
        "preferred_major",
        "supported_majors",
        "openapi_sha256",
        "build_version",
        "source_commit",
        "build_channel",
        "provider_kind",
        "feature_flags",
    }
    if type(payload) is not dict or set(payload) != required:
        raise SmokeFailure("sidecar /version does not use the closed release schema")
    if (
        payload["schema_version"] != "1"
        or payload["api_name"] != "openevo-desktop-local-api"
        or payload["build_version"] != expected_version
        or payload["build_channel"] != "release"
        or payload["provider_kind"] != "desktop_sidecar"
    ):
        raise SmokeFailure("sidecar /version does not identify the expected release provider")
    preferred = payload["preferred_major"]
    supported = payload["supported_majors"]
    if (
        type(preferred) is not int
        or isinstance(preferred, bool)
        or not 1 <= preferred <= 255
        or type(supported) is not list
        or not supported
        or any(type(item) is not int or isinstance(item, bool) or not 1 <= item <= 255 for item in supported)
        or len(set(supported)) != len(supported)
        or preferred not in supported
        or type(payload["openapi_sha256"]) is not str
        or re.fullmatch(r"[0-9a-f]{64}", payload["openapi_sha256"]) is None
        or type(payload["source_commit"]) is not str
        or re.fullmatch(r"[0-9a-f]{40}", payload["source_commit"]) is None
        or type(payload["feature_flags"]) is not list
        or any(type(item) is not str or not item or len(item) > 128 for item in payload["feature_flags"])
    ):
        raise SmokeFailure("sidecar /version is malformed")


def descendants(rows: Iterable[ProcessRow], roots: Iterable[ProcessIdentity]) -> set[ProcessIdentity]:
    """Return the current tree below exact process identities, never PID-only roots."""
    inventory = list(rows)
    current = {row.identity for row in inventory}
    owned = set(roots) & current
    # Parent links are PID based, but a process joins only after its ancestor identity is current.
    changed = True
    while changed:
        changed = False
        owned_pids = {identity.pid for identity in owned}
        for row in inventory:
            if row.parent_pid in owned_pids and row.identity not in owned:
                owned.add(row.identity)
                changed = True
    return owned


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


class DarwinSystem:
    """Small injectable boundary around the fixed macOS tools used by this smoke."""

    def __init__(self) -> None:
        try:
            self._libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            self._libproc.proc_pidpath.argtypes = [
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_uint32,
            ]
            self._libproc.proc_pidpath.restype = ctypes.c_int
        except (AttributeError, OSError) as exc:
            raise SmokeFailure("macOS process identity service is unavailable") from exc

    def command(self, arguments: list[str], timeout: float = COMMAND_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                arguments,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SmokeFailure("required macOS system tool is unavailable") from exc

    def snapshot(self) -> list[ProcessRow]:
        result = self.command(["/bin/ps", "-axo", "pid=,ppid=,lstart=,command="])
        if result.returncode != 0:
            raise SmokeFailure("Darwin process inventory is unavailable")
        return parse_ps_rows(result.stdout)

    def process_path(self, pid: int) -> str | None:
        buffer = ctypes.create_string_buffer(PROC_PIDPATHINFO_MAXSIZE)
        length = self._libproc.proc_pidpath(pid, buffer, len(buffer))
        if length <= 0 or length >= len(buffer):
            return None
        path = os.fsdecode(buffer.value)
        return path if path.startswith("/") and "\x00" not in path else None

    def listener_rows(self, identity: ProcessIdentity) -> list[Listener]:
        result = self.command(
            [
                "/usr/sbin/lsof", "-nP", "-a", "-p", str(identity.pid), "-iTCP",
                "-sTCP:LISTEN", "-FftnT",
            ]
        )
        if result.returncode != 0:
            return []
        return parse_lsof_listeners(result.stdout, identity)

    def remove_quarantine(self, app: Path) -> None:
        probe = self.command(["/usr/bin/xattr", "-p", "com.apple.quarantine", str(app)])
        if probe.returncode != 0:
            raise SmokeFailure("the copied app was not quarantined before the allow flow")
        result = self.command(["/usr/bin/xattr", "-dr", "com.apple.quarantine", str(app)])
        if result.returncode != 0:
            raise SmokeFailure("could not allow the quarantined app for LaunchServices")
        if self.command(["/usr/bin/xattr", "-p", "com.apple.quarantine", str(app)]).returncode == 0:
            raise SmokeFailure("could not allow the quarantined app for LaunchServices")

    def launch(self, app: Path) -> None:
        result = self.command(["/usr/bin/open", "-n", str(app)], timeout=COMMAND_TIMEOUT_SECONDS)
        if result.returncode != 0:
            raise SmokeFailure("LaunchServices could not launch OpenEvo Desktop")

    def startup_log_checkpoint(self) -> int:
        return _startup_log_checkpoint(DESKTOP_LOG_DIRECTORY)

    def startup_failure_since(self, checkpoint: int) -> tuple[str, str] | None:
        return _startup_failure_since(DESKTOP_LOG_DIRECTORY, checkpoint)

    def http_version(self, port: int, timeout: float) -> bytes:
        url = f"http://127.0.0.1:{port}/version"
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(Request(url, method="GET"), timeout=timeout) as response:
                if response.status != 200 or response.geturl() != url:
                    raise SmokeFailure("sidecar /version did not return a direct success response")
                body = response.read(HTTP_RESPONSE_MAX_BYTES + 1)
        except HTTPError as exc:
            raise SmokeFailure("sidecar /version returned an unexpected HTTP response") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise SidecarNotReady("sidecar /version is not ready") from exc
        if len(body) > HTTP_RESPONSE_MAX_BYTES:
            raise SmokeFailure("sidecar /version response exceeded the bounded size")
        return body

    def signal(self, identity: ProcessIdentity, sig: signal.Signals) -> bool:
        current = {row.identity for row in self.snapshot()}
        if identity not in current:
            return False
        try:
            os.kill(identity.pid, sig)
        except ProcessLookupError:
            return False
        except PermissionError as exc:
            raise SmokeFailure("cannot terminate the LaunchServices-owned app process") from exc
        return True


def _app_executable(app: Path) -> Path:
    if not app.is_absolute() or app.name == APP_NAME_SUFFIX or not app.name.endswith(APP_NAME_SUFFIX):
        raise SmokeFailure("the app argument must be an exact absolute .app path")
    if app.is_symlink() or not app.is_dir():
        raise SmokeFailure("the app argument must name a real app bundle")
    try:
        resolved_app = app.resolve(strict=True)
    except OSError as exc:
        raise SmokeFailure("the app bundle cannot be resolved") from exc
    info = resolved_app / "Contents" / "Info.plist"
    try:
        with info.open("rb") as stream:
            plist = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException) as exc:
        raise SmokeFailure("the app Info.plist is unavailable") from exc
    executable_name = plist.get("CFBundleExecutable") if type(plist) is dict else None
    if (
        type(executable_name) is not str
        or not executable_name
        or executable_name in {".", ".."}
        or "/" in executable_name
        or "\x00" in executable_name
    ):
        raise SmokeFailure("the app Info.plist has an invalid executable name")
    executable = resolved_app / "Contents" / "MacOS" / executable_name
    if executable.is_symlink() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise SmokeFailure("the app executable is unavailable")
    return executable.resolve(strict=True)


def _app_roots(system: DarwinSystem, rows: list[ProcessRow], executable: Path) -> set[ProcessIdentity]:
    expected = str(executable)
    roots: set[ProcessIdentity] = set()
    for row in rows:
        path = system.process_path(row.identity.pid)
        if path is not None and os.path.realpath(path) == expected:
            roots.add(row.identity)
    return roots


def _sidecar_tree(
    system: DarwinSystem,
    rows: list[ProcessRow],
    owned: set[ProcessIdentity],
    expected_executable: Path,
) -> set[ProcessIdentity]:
    expected = str(expected_executable)
    sidecars = {
        identity
        for identity in owned
        if (path := system.process_path(identity.pid)) is not None
        and os.path.realpath(path) == expected
    }
    return descendants(rows, sidecars)


def _single_listener(system: DarwinSystem, sidecar_tree: set[ProcessIdentity]) -> Listener | None:
    listeners = [listener for identity in sorted(sidecar_tree) for listener in system.listener_rows(identity)]
    ports = {listener.port for listener in listeners}
    if len(ports) > 1:
        raise SmokeFailure("multiple loopback listeners were owned by this sidecar tree")
    return listeners[0] if listeners else None


def _cleanup(system: DarwinSystem, owned: set[ProcessIdentity], timeout_seconds: float) -> bool:
    """Signal only identities observed in this launch tree, then prove they are gone."""
    deadline = time.monotonic() + min(15.0, max(2.0, timeout_seconds))
    for sig, grace in ((signal.SIGTERM, 1.5), (signal.SIGKILL, 0.0)):
        current_rows = system.snapshot()
        current = {row.identity for row in current_rows}
        live = owned & current
        # Descendant-first ordering keeps graceful shutdown scoped to this app tree.
        for identity in sorted(live, reverse=True):
            system.signal(identity, sig)
        phase_deadline = min(deadline, time.monotonic() + grace)
        while time.monotonic() < phase_deadline:
            if not (owned & {row.identity for row in system.snapshot()}):
                return True
            time.sleep(0.05)
    while time.monotonic() < deadline:
        if not (owned & {row.identity for row in system.snapshot()}):
            return True
        time.sleep(0.05)
    return not (owned & {row.identity for row in system.snapshot()})


def _os_major() -> int:
    version = platform.mac_ver()[0]
    match = re.fullmatch(r"([1-9][0-9]{0,2})(?:\.[0-9]+){0,2}", version)
    if match is None:
        raise SmokeFailure("macOS version is unavailable")
    return int(match.group(1))


def _file_identity(path: Path) -> tuple[int, int, int, int, int]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SmokeFailure("candidate file identity is unavailable") from exc
    if path.is_symlink() or not path.is_file():
        raise SmokeFailure("candidate file must be a regular non-symlink file")
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _sha256(path: Path) -> str:
    expected = _file_identity(path)
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            ) != expected:
                raise SmokeFailure("candidate file changed before hashing")
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            final = os.fstat(stream.fileno())
    except OSError as exc:
        raise SmokeFailure("candidate file could not be hashed") from exc
    if (
        final.st_dev,
        final.st_ino,
        final.st_size,
        final.st_mtime_ns,
        final.st_ctime_ns,
    ) != expected or _file_identity(path) != expected:
        raise SmokeFailure("candidate file changed while hashing")
    return digest.hexdigest()


def _evidence(
    expected_version: str,
    sidecar_ready: bool,
    cleanup: bool,
    *,
    executable: Path,
    sidecar_executable: Path,
    source_dmg: Path,
) -> dict[str, object]:
    architecture = platform.machine()
    if architecture not in {"arm64", "x86_64"}:
        raise SmokeFailure("macOS architecture is unsupported")
    return {
        "architecture": architecture,
        "binary_sha256": {
            "bundled_external_bin": _sha256(sidecar_executable),
            "native_executable": _sha256(executable),
        },
        "build_version": expected_version,
        "cleanup": {
            "authority_limited_to_observed_tree": True,
            "owned_processes_exited": cleanup,
            "sidecar_descendants_exited": cleanup,
        },
        "launch_origin": LAUNCH_ORIGIN,
        "os_major": _os_major(),
        "process_image_bound": sidecar_ready,
        "quarantine_present_before_allow": True,
        "quarantine_removed_before_launch": True,
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "sidecar_ready": sidecar_ready,
        "source_dmg": {
            "filename": source_dmg.name,
            "sha256": _sha256(source_dmg),
        },
        "version_verified": sidecar_ready,
    }


def _write_evidence(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")


def _system_startup_checkpoint(system: object) -> int | None:
    checkpoint = getattr(system, "startup_log_checkpoint", None)
    if not callable(checkpoint):
        return None
    try:
        value = checkpoint()
    except (OSError, ValueError):
        return None
    return value if type(value) is int and value >= 0 else None


def _system_startup_failure(
    system: object,
    checkpoint: int | None,
) -> tuple[str, str] | None:
    read_failure = getattr(system, "startup_failure_since", None)
    if checkpoint is None or not callable(read_failure):
        return None
    try:
        failure = read_failure(checkpoint)
    except (OSError, ValueError):
        return None
    return failure if failure == _LOADER_FAILURE else None


def smoke_launchservices(
    app: Path,
    *,
    expected_version: str,
    timeout_seconds: float,
    evidence_out: Path,
    source_dmg: Path,
    system: DarwinSystem | None = None,
) -> dict[str, object]:
    if sys.platform != "darwin":
        raise SmokeFailure("LaunchServices smoke is supported only on Darwin")
    if _VERSION.fullmatch(expected_version) is None:
        raise SmokeFailure("expected version is invalid")
    if not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise SmokeFailure("timeout must be positive and bounded")
    executable = _app_executable(app)
    sidecar_executable = executable.parent / SIDECAR_BASENAME
    if (
        sidecar_executable.is_symlink()
        or not sidecar_executable.is_file()
        or not os.access(sidecar_executable, os.X_OK)
    ):
        raise SmokeFailure("the app-bundle sidecar executable is unavailable")
    sidecar_executable = sidecar_executable.resolve(strict=True)
    tools = system or DarwinSystem()
    if _app_roots(tools, tools.snapshot(), executable):
        raise SmokeFailure("an instance of this exact app bundle is already active")

    startup_checkpoint = _system_startup_checkpoint(tools)
    tools.remove_quarantine(app)
    tools.launch(app)
    deadline = time.monotonic() + timeout_seconds
    owned: set[ProcessIdentity] = set()
    captured: BaseException | None = None
    ready = False
    app_observed = False
    sidecar_observed = False
    listener_observed = False
    try:
        while time.monotonic() < deadline:
            rows = tools.snapshot()
            roots = _app_roots(tools, rows, executable)
            if roots:
                app_observed = True
                owned.update(descendants(rows, roots))
            sidecar = _sidecar_tree(
                tools,
                rows,
                owned & {row.identity for row in rows},
                sidecar_executable,
            )
            sidecar_observed = sidecar_observed or bool(sidecar)
            owned.update(sidecar)
            listener = _single_listener(tools, sidecar)
            if listener is not None:
                listener_observed = True
                try:
                    version_bytes = tools.http_version(
                        listener.port,
                        min(1.0, max(0.1, deadline - time.monotonic())),
                    )
                except SidecarNotReady:
                    time.sleep(0.1)
                    continue
                try:
                    validate_version(_strict_json(version_bytes), expected_version)
                except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
                    raise SmokeFailure("sidecar /version is malformed") from exc
                ready = True
                break
            time.sleep(0.1)
        if not ready:
            startup_failure = _system_startup_failure(tools, startup_checkpoint)
            startup_detail = (
                f"; startup_stage={startup_failure[0]}; startup_code={startup_failure[1]}"
                if startup_failure is not None
                else ""
            )
            raise SmokeFailure(
                "timed out waiting for an owned sidecar loopback listener "
                f"(app_process_observed={str(app_observed).lower()}; "
                f"sidecar_process_observed={str(sidecar_observed).lower()}; "
                f"listener_observed={str(listener_observed).lower()}"
                f"{startup_detail})"
            )
    except BaseException as exc:
        captured = exc
    cleanup = _cleanup(tools, owned, timeout_seconds)
    if not cleanup:
        failure = SmokeFailure("the launched app or its observed descendants did not exit")
        if captured is not None:
            raise failure from captured
        raise failure
    if captured is not None:
        raise captured
    evidence = _evidence(
        expected_version,
        ready,
        cleanup,
        executable=executable,
        sidecar_executable=sidecar_executable,
        source_dmg=source_dmg,
    )
    _write_evidence(evidence_out, evidence)
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--source-dmg", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, required=True)
    parser.add_argument("--evidence-out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        evidence = smoke_launchservices(
            args.app,
            expected_version=args.expected_version,
            timeout_seconds=args.timeout_seconds,
            evidence_out=args.evidence_out,
            source_dmg=args.source_dmg,
        )
    except SmokeFailure as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(evidence, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
