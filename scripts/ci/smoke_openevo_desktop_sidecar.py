#!/usr/bin/env python3
"""Launch a packaged OpenEvo Desktop sidecar and smoke static assets."""

from __future__ import annotations

import argparse
from html.parser import HTMLParser
import json
from pathlib import Path
import posixpath
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from typing import Any
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen


class SmokeFailure(RuntimeError):
    """Raised when the packaged sidecar cannot serve the Desktop shell."""


class _AssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.assets: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name not in {"href", "src"} or value is None:
                continue
            asset = _asset_reference(value)
            if asset is not None:
                self.assets.append(asset)


def _asset_reference(value: str) -> str | None:
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return None
    path = parsed.path
    if path.startswith("/assets/"):
        path = path[1:]
    elif not path.startswith("assets/"):
        return None
    normalized = posixpath.normpath(path)
    if normalized == "assets" or not normalized.startswith("assets/"):
        raise SmokeFailure(f"Invalid Desktop asset reference: {value}")
    return normalized


def _asset_references(index_html: str) -> list[str]:
    parser = _AssetParser()
    parser.feed(index_html)
    return sorted(set(parser.assets))


def _allocate_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _read_url(url: str, *, timeout_seconds: float = 2.0) -> bytes:
    try:
        with urlopen(url, timeout=timeout_seconds) as response:
            if response.status != 200:
                raise SmokeFailure(f"{url} returned HTTP {response.status}")
            return response.read()
    except URLError as exc:
        raise SmokeFailure(f"{url} was not reachable: {exc}") from exc


def _read_json(url: str) -> dict[str, Any]:
    payload = json.loads(_read_url(url).decode("utf-8"))
    if not isinstance(payload, dict):
        raise SmokeFailure(f"{url} did not return a JSON object")
    return payload


def smoke_sidecar(sidecar: Path, *, timeout_seconds: float) -> None:
    if not sidecar.is_file():
        raise SmokeFailure(f"sidecar executable does not exist: {sidecar}")

    port = _allocate_port()
    base_url = f"http://127.0.0.1:{port}"
    with TemporaryDirectory(prefix="openevo-sidecar-smoke-") as config_root:
        process = subprocess.Popen(
            [
                str(sidecar),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--desktop-config-root",
                config_root,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise SmokeFailure(_process_failure(process))
                try:
                    health = _read_json(f"{base_url}/health")
                    if health.get("status") == "ok":
                        break
                except SmokeFailure:
                    time.sleep(0.25)
            else:
                raise SmokeFailure(f"sidecar did not become healthy within {timeout_seconds}s")

            index_html = _read_url(f"{base_url}/openevo").decode("utf-8")
            assets = _asset_references(index_html)
            if not assets:
                raise SmokeFailure("/openevo did not reference any packaged assets")
            for asset in assets:
                _read_url(f"{base_url}/{asset}")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def _process_failure(process: subprocess.Popen[str]) -> str:
    stdout, stderr = process.communicate(timeout=2)
    return (
        "sidecar exited before serving /health "
        f"(exit {process.returncode}).\nstdout:\n{stdout}\nstderr:\n{stderr}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sidecar", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    args = parser.parse_args(argv)

    try:
        smoke_sidecar(args.sidecar, timeout_seconds=args.timeout_seconds)
    except SmokeFailure as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"OpenEvo Desktop sidecar smoke passed: {args.sidecar}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
