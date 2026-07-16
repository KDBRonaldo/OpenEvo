#!/usr/bin/env python3
"""Launch and inspect the Tauri executable in an OpenEvo Desktop macOS app."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import signal
import stat
import subprocess
import sys
import tempfile
import time

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from smoke_openevo_desktop_sidecar import SmokeFailure  # noqa: E402


SIDECAR_NAME = "openevo-desktop-sidecar"
APP_BUNDLE_NAME = "OpenEvo Desktop.app"
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


def find_app_executable(bundle_root: Path) -> Path:
    app = _app_bundle(bundle_root)
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


def _process_rows() -> list[tuple[int, int, int, str]]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,pgid=,command="],
        check=True,
        capture_output=True,
        text=True,
    )
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


def _descendants(root_pid: int) -> list[tuple[int, int, int, str]]:
    rows = _process_rows()
    descendants: set[int] = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, parent, _group, _command in rows:
            if parent in descendants and pid not in descendants:
                descendants.add(pid)
                changed = True
    return [row for row in rows if row[0] in descendants and row[0] != root_pid]


def _macos_renderer_ready(app_pid: int) -> bool:
    swift_probe = """
import CoreGraphics
import Foundation
let expected = Int(ProcessInfo.processInfo.environment["OPENEVO_SMOKE_APP_PID"]!)!
let windows = CGWindowListCopyWindowInfo(
    [.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID
) as? [[String: Any]] ?? []
let ready = windows.contains { window in
    let owner = (window[kCGWindowOwnerPID as String] as? NSNumber)?.intValue
    let layer = (window[kCGWindowLayer as String] as? NSNumber)?.intValue
    let bounds = window[kCGWindowBounds as String] as? [String: Any]
    let width = (bounds?["Width"] as? NSNumber)?.doubleValue ?? 0
    let height = (bounds?["Height"] as? NSNumber)?.doubleValue ?? 0
    return owner == expected && layer == 0 && width > 0 && height > 0
}
print(ready ? "1" : "0")
"""
    environment = os.environ.copy()
    environment["OPENEVO_SMOKE_APP_PID"] = str(app_pid)
    swift = subprocess.run(
        ["swift", "-e", swift_probe],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env=environment,
    )
    if swift.returncode == 0:
        return swift.stdout.strip() == "1"

    script = (
        'tell application "System Events" to tell first application process '
        f"whose unix id is {app_pid} to count windows"
    )
    result = subprocess.run(
        ["osascript", "-e", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        return False
    try:
        return int(result.stdout.strip()) > 0
    except ValueError:
        return False


def _lsof_fd(pid: int, descriptor: int) -> tuple[str | None, str | None]:
    result = subprocess.run(
        ["lsof", "-nP", "-a", "-p", str(pid), "-d", str(descriptor), "-Fnft"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        return None, None
    file_type = next((line[1:] for line in result.stdout.splitlines() if line.startswith("t")), None)
    name = next((line[1:] for line in result.stdout.splitlines() if line.startswith("n")), None)
    return file_type, name


def _macos_native_evidence(
    app_pid: int,
    executable: Path,
    sidecar: Path,
    mach_o: dict[str, object],
    expected_sidecar_digest: str,
) -> tuple[dict[str, object] | None, set[int]]:
    sidecar_rows = [row for row in _descendants(app_pid) if SIDECAR_NAME in row[3]]
    process_groups = {row[2] for row in sidecar_rows if row[2] > 0}
    for pid, _parent, _group, _command in sidecar_rows:
        listener_type, listener_name = _lsof_fd(pid, 3)
        executable_type, executable_name = _lsof_fd(pid, 4)
        if listener_type not in {"IPv4", "IPv6"} or not listener_name:
            continue
        if "(LISTEN)" not in listener_name:
            continue
        if executable_type != "REG" or not executable_name:
            continue
        executable_fd_path = Path(executable_name)
        try:
            fd_digest = _sha256(executable_fd_path)
        except OSError:
            continue
        if fd_digest != expected_sidecar_digest:
            continue
        return (
            {
                "schema_version": EVIDENCE_SCHEMA_VERSION,
                "native_executable": executable.name,
                "bundled_external_bin": sidecar.name,
                "renderer_ready": _macos_renderer_ready(app_pid),
                "sidecar_ready": True,
                "bundled_external_bin_resolved": True,
                "native_listener_fd_handoff": True,
                "native_executable_fd_handoff": True,
                "mach_o": mach_o,
            },
            process_groups,
        )
    return None, process_groups


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


def smoke_bundle(
    bundle_root: Path,
    *,
    launch_origin: str,
    source_dmg: Path,
    timeout_seconds: float,
    evidence_out: Path | None = None,
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
    nonce = os.urandom(32).hex()
    with tempfile.TemporaryDirectory(prefix="openevo-desktop-app-smoke-") as temporary:
        smoke_root = Path(temporary)
        emitted_path = smoke_root / "app-evidence.json"
        home = smoke_root / "home"
        home.mkdir(mode=0o700)
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(home),
                "OPENEVO_RELEASE_SMOKE_EVIDENCE_PATH": str(emitted_path),
                "OPENEVO_RELEASE_SMOKE_NONCE": nonce,
            }
        )
        process = subprocess.Popen(
            [str(executable)],
            cwd=app.parent,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        process_groups = {process.pid}
        evidence: dict[str, object] | None = None
        deadline = time.monotonic() + timeout_seconds
        try:
            while time.monotonic() < deadline:
                if sys.platform == "darwin":
                    if process.poll() is None:
                        evidence, observed_groups = _macos_native_evidence(
                            process.pid,
                            executable,
                            sidecar,
                            mach_o,
                            binary_sha256["bundled_external_bin"],
                        )
                        process_groups.update(observed_groups)
                        if evidence is not None and evidence["renderer_ready"] is True:
                            break
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
                raise SmokeFailure("Timed out waiting for OpenEvo Desktop app smoke evidence")

            _kill_groups({process.pid}, signal.SIGTERM)
            cleanup_deadline = time.monotonic() + min(15.0, max(2.0, timeout_seconds))
            try:
                process.wait(timeout=min(5.0, max(0.1, cleanup_deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                pass
            process_group_cleanup = _wait_for_groups_to_exit(process_groups, cleanup_deadline)
            if not process_group_cleanup:
                raise SmokeFailure("OpenEvo Desktop left an app or sidecar process group running")
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
        finally:
            _kill_groups(process_groups, signal.SIGKILL)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle_root", type=Path)
    parser.add_argument("--launch-origin", choices=sorted(LAUNCH_ORIGINS), required=True)
    parser.add_argument("--source-dmg", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--evidence-out", type=Path)
    args = parser.parse_args(argv)

    try:
        evidence = smoke_bundle(
            args.bundle_root,
            launch_origin=args.launch_origin,
            source_dmg=args.source_dmg,
            timeout_seconds=args.timeout_seconds,
            evidence_out=args.evidence_out,
        )
    except SmokeFailure as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(evidence, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
