#!/usr/bin/env python3
"""Smoke the sidecar and native renderer bundled in an OpenEvo Desktop app."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import plistlib
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
REPO_ROOT = SCRIPT_DIR.parent.parent
DESKTOP_OPENAPI = REPO_ROOT / "desktop/sidecar/contracts/v1/openapi.json"
DESKTOP_OPENAPI_SHA256 = hashlib.sha256(DESKTOP_OPENAPI.read_bytes()).hexdigest()
RENDERER_READY_MARKER = f"{RENDERER_READY_PREFIX} {DESKTOP_OPENAPI_SHA256}"
NATIVE_LOG_LIMIT = 64 * 1024


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
    info_path = app_bundle / "Contents" / "Info.plist"
    try:
        info = plistlib.loads(info_path.read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        raise SmokeFailure(f"OpenEvo Desktop has an invalid Info.plist: {info_path}") from exc
    executable_name = info.get("CFBundleExecutable")
    if (
        not isinstance(executable_name, str)
        or not executable_name
        or executable_name in {".", ".."}
        or Path(executable_name).name != executable_name
        or "\x00" in executable_name
    ):
        raise SmokeFailure("OpenEvo Desktop Info.plist has an invalid CFBundleExecutable")
    executable = app_bundle / "Contents" / "MacOS" / executable_name
    try:
        metadata = executable.lstat()
    except OSError as exc:
        raise SmokeFailure(f"OpenEvo Desktop native executable is missing: {executable}") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o111 == 0:
        raise SmokeFailure(f"OpenEvo Desktop native executable is not executable: {executable}")
    return executable


def _native_log_tail(log_path: Path) -> str:
    try:
        with log_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - NATIVE_LOG_LIMIT))
            return handle.read(NATIVE_LOG_LIMIT).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _terminate_native_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def smoke_native_app(bundle_root: Path, *, timeout_seconds: float) -> Path:
    executable = find_native_executable(bundle_root)
    app_bundle = _find_app_bundle(bundle_root)
    with TemporaryDirectory(prefix="openevo-native-smoke-") as temporary:
        log_path = Path(temporary) / "native.log"
        try:
            with log_path.open("wb") as output:
                process = subprocess.Popen(
                    [str(executable)],
                    cwd=app_bundle.parent,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except OSError as exc:
            raise SmokeFailure(
                f"OpenEvo Desktop native executable could not start: {executable}"
            ) from exc
        deadline = time.monotonic() + timeout_seconds
        try:
            while time.monotonic() < deadline:
                output_text = _native_log_tail(log_path)
                if RENDERER_READY_MARKER in output_text:
                    time.sleep(0.25)
                    if process.poll() is not None:
                        raise SmokeFailure(
                            "OpenEvo Desktop exited immediately after renderer readiness"
                        )
                    return executable
                exit_code = process.poll()
                if exit_code is not None:
                    raise SmokeFailure(
                        "OpenEvo Desktop exited before renderer readiness "
                        f"(code {exit_code}): {output_text[-2048:]}"
                    )
                time.sleep(0.1)
            raise SmokeFailure(
                "OpenEvo Desktop did not report renderer readiness before timeout: "
                f"{_native_log_tail(log_path)[-2048:]}"
            )
        finally:
            _terminate_native_process(process)


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
