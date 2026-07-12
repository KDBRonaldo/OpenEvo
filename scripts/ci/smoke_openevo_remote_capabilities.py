#!/usr/bin/env python3
"""Smoke a packaged sidecar against an exact installed Core wheel backend."""

from __future__ import annotations

import argparse
import hashlib
from importlib import metadata, util
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
from tempfile import TemporaryFile
import time
from types import ModuleType
from urllib.error import URLError
from urllib.request import build_opener, ProxyHandler

import openevo


_LOCAL_HTTP_OPENER = build_opener(ProxyHandler({}))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _allocate_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_backend(
    base_url: str,
    process: subprocess.Popen[str],
    process_log,
    *,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                "exact Core backend exited before readiness:\n"
                f"{_process_output(process_log)}"
            )
        try:
            with _LOCAL_HTTP_OPENER.open(
                f"{base_url}/health", timeout=1
            ) as response:
                if response.status == 200:
                    return
        except URLError:
            time.sleep(0.1)
            continue
        time.sleep(0.1)
    raise RuntimeError("exact Core backend did not become ready")


def _terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        process.wait(timeout=10)


def _process_output(process_log) -> str:
    process_log.flush()
    process_log.seek(0)
    return process_log.read()


def _load_sidecar_smoke() -> ModuleType:
    path = Path(__file__).with_name("smoke_openevo_desktop_sidecar.py")
    spec = util.spec_from_file_location("smoke_openevo_desktop_sidecar", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load packaged sidecar smoke: {path}")
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def smoke(
    wheel_path: Path,
    sidecar_path: Path,
    *,
    timeout_seconds: float,
) -> dict[str, str]:
    wheel = wheel_path.resolve(strict=True)
    sidecar = sidecar_path.resolve(strict=True)
    repository_src = Path(__file__).resolve().parents[2] / "src"
    import_path = Path(openevo.__file__).resolve(strict=True)
    if import_path.is_relative_to(repository_src):
        raise RuntimeError("remote capability smoke imported Core from source")

    version = metadata.version("openevo")
    digest = _sha256(wheel)
    sidecar_smoke = _load_sidecar_smoke()
    with tempfile.TemporaryDirectory(
        prefix="openevo-remote-capability-smoke-"
    ) as temp_dir:
        root = Path(temp_dir)
        locked_wheel = root / wheel.name
        shutil.copy2(wheel, locked_wheel)
        lock_path = root / "framework-lock.json"
        lock_path.write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "distribution": "openevo",
                    "distribution_version": version,
                    "distribution_digest": digest,
                    "wheel_filename": locked_wheel.name,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        port = _allocate_port()
        base_url = f"http://127.0.0.1:{port}"
        executable = Path(sys.executable).with_name("openevo-backend")
        if not executable.is_file():
            raise RuntimeError("installed openevo-backend launcher is unavailable")
        child_env = dict(os.environ)
        child_env.pop("PYTHONPATH", None)
        with TemporaryFile(mode="w+", encoding="utf-8") as process_log:
            process = subprocess.Popen(
                [
                    str(executable),
                    "serve",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--framework-lock",
                    str(lock_path),
                ],
                cwd=root,
                env=child_env,
                stdout=process_log,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            try:
                _wait_for_backend(
                    base_url,
                    process,
                    process_log,
                    timeout_seconds=timeout_seconds,
                )
                registry_digest = sidecar_smoke.smoke_sidecar(
                    sidecar,
                    timeout_seconds=timeout_seconds,
                    backend_base_url=base_url,
                    expected_core_version=version,
                )
                if not isinstance(registry_digest, str):
                    raise RuntimeError(
                        "packaged sidecar capability smoke returned no registry digest"
                    )
            finally:
                _terminate(process)

    return {
        "core_import_path": str(import_path),
        "registry_digest": registry_digest,
        "sidecar_path": str(sidecar),
        "wheel_sha256": digest,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--sidecar", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    args = parser.parse_args(argv)
    print(
        json.dumps(
            smoke(
                args.wheel,
                args.sidecar,
                timeout_seconds=args.timeout_seconds,
            ),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
