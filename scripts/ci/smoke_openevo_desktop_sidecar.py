#!/usr/bin/env python3
"""Launch a packaged OpenEvo Desktop sidecar and smoke static assets."""

from __future__ import annotations

import argparse
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import posixpath
import re
import signal
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory, TemporaryFile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import build_opener, ProxyHandler, Request


EXPECTED_DESKTOP_METHOD_IDS = frozenset(
    {
        "agent_system_gepa_reflector",
        "skill_bundle_reflector",
        "text_memory_expel_reflector",
    }
)
EXPECTED_DESKTOP_TARGET_IDS = frozenset(
    {"agent_system", "skill_bundle", "text_memory"}
)
EXPECTED_ACTIVE_SELECTIONS = {
    "skill_bundle": "skill_bundle_reflector",
    "text_memory": "text_memory_reflector",
}
SIDECAR_MUTATION_TOKEN_HEADER = "X-OpenEvo-Sidecar-Token"
_LOCAL_HTTP_OPENER = build_opener(ProxyHandler({}))


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


def _read_url(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    expected_status: int = 200,
    timeout_seconds: float = 2.0,
) -> bytes:
    request = Request(url, headers=headers or {})
    try:
        with _LOCAL_HTTP_OPENER.open(request, timeout=timeout_seconds) as response:
            if response.status != expected_status:
                raise SmokeFailure(
                    f"{url} returned HTTP {response.status}; expected {expected_status}"
                )
            return response.read()
    except HTTPError as exc:
        if exc.code == expected_status:
            return exc.read()
        raise SmokeFailure(
            f"{url} returned HTTP {exc.code}; expected {expected_status}"
        ) from exc
    except URLError as exc:
        raise SmokeFailure(f"{url} was not reachable: {exc}") from exc


def _read_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    expected_status: int = 200,
) -> dict[str, Any]:
    payload = json.loads(
        _read_url(
            url,
            headers=headers,
            expected_status=expected_status,
        ).decode("utf-8")
    )
    if not isinstance(payload, dict):
        raise SmokeFailure(f"{url} did not return a JSON object")
    return payload


def _assert_capabilities(
    payload: dict[str, Any],
    *,
    execution_mode: str,
    expected_core_version: str,
) -> None:
    expected_generic_mode = (
        "subscription"
        if execution_mode == "codex_subscription_transcript"
        else "self_deployed"
    )
    if payload.get("schema_version") != "1":
        raise SmokeFailure("capabilities response has an unexpected schema version")
    if payload.get("core_version") != expected_core_version:
        raise SmokeFailure("capabilities response did not come from the exact Core wheel")
    registry_digest = payload.get("registry_digest")
    if not isinstance(registry_digest, str) or re.fullmatch(
        r"[0-9a-f]{64}", registry_digest
    ) is None:
        raise SmokeFailure("capabilities response has an invalid registry digest")
    if payload.get("evaluated_profile") != {
        "execution_mode": expected_generic_mode,
        "capture_mode": "transcript",
        "harness_id": "codex",
        "harness_capabilities": [],
        "runtime_capabilities": [],
    }:
        raise SmokeFailure(f"capabilities profile mismatch for {execution_mode}")
    targets = payload.get("targets")
    if (
        not isinstance(targets, list)
        or not all(isinstance(target, dict) for target in targets)
        or {target.get("target_id") for target in targets}
        != EXPECTED_DESKTOP_TARGET_IDS
    ):
        raise SmokeFailure("capabilities response has an unexpected target set")
    methods = [
        method
        for target in targets
        if isinstance(target, dict)
        for method in target.get("methods", [])
        if isinstance(method, dict)
    ]
    if {method.get("method_id") for method in methods} != EXPECTED_DESKTOP_METHOD_IDS:
        raise SmokeFailure("capabilities response has an unexpected method set")
    for target in targets:
        if (
            target.get("effective_default_method_id")
            != target.get("configured_default_method_id")
            or target.get("configured_default_support", {}).get("overall")
            != "supported"
        ):
            raise SmokeFailure("capabilities response has an unsupported target default")
        for identity_key in (
            "implementation_identity_digest",
            "handler_identity_digest",
        ):
            identity = target.get(identity_key)
            if not isinstance(identity, str) or len(identity) != 64:
                raise SmokeFailure(f"capabilities target has invalid {identity_key}")
        accepted_methods = {
            method.get("method_id"): method
            for method in target.get("accepted_methods", [])
            if isinstance(method, dict)
        }
        active_selection = EXPECTED_ACTIVE_SELECTIONS.get(target.get("target_id"))
        if active_selection is not None and (
            active_selection not in accepted_methods
            or accepted_methods[active_selection].get("support", {}).get("overall")
            != "supported"
        ):
            raise SmokeFailure(
                "capabilities response does not accept a current Science selection"
            )
        if target.get("target_id") == "agent_system":
            resolvers = {
                resolver.get("selection_value"): resolver
                for resolver in target.get("selection_resolvers", [])
                if isinstance(resolver, dict)
            }
            auto = resolvers.get("auto")
            resolved = auto.get("resolved_methods", []) if isinstance(auto, dict) else []
            if {
                method.get("method_id")
                for method in resolved
                if isinstance(method, dict)
                and method.get("support", {}).get("overall") == "supported"
            } != {
                "agent_system_reflector",
                "agent_system_history_reflector",
            }:
                raise SmokeFailure(
                    "capabilities response does not support agent_system auto"
                )
    for method in methods:
        identity = method.get("implementation_identity_digest")
        if (
            not isinstance(identity, str)
            or len(identity) != 64
            or method.get("support", {}).get("overall") != "supported"
        ):
            raise SmokeFailure("capabilities response has an unsupported method")


def _smoke_capability_proxy(base_url: str, *, expected_core_version: str) -> str:
    shell = _read_json(f"{base_url}/openevo-api/desktop/shell")
    sidecar_security = shell.get("sidecar")
    token = (
        sidecar_security.get("mutation_token")
        if isinstance(sidecar_security, dict)
        else None
    )
    if not isinstance(token, str) or not token:
        raise SmokeFailure("packaged sidecar did not expose a mutation token")

    capability_url = f"{base_url}/openevo-api/desktop/capabilities"
    _read_url(
        f"{capability_url}?execution_mode=codex_subscription_transcript",
        expected_status=403,
    )
    headers = {SIDECAR_MUTATION_TOKEN_HEADER: token}
    registry_digests: set[str] = set()
    for execution_mode in ("codex_subscription_transcript", "self-deployed"):
        payload = _read_json(
            f"{capability_url}?execution_mode={execution_mode}",
            headers=headers,
        )
        _assert_capabilities(
            payload,
            execution_mode=execution_mode,
            expected_core_version=expected_core_version,
        )
        registry_digests.add(payload["registry_digest"])
    if len(registry_digests) != 1:
        raise SmokeFailure("release modes did not resolve the same frozen registry")
    _read_url(f"{base_url}/openevo-api/desktop/methods", expected_status=404)
    return next(iter(registry_digests))


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


def smoke_sidecar(
    sidecar: Path,
    *,
    timeout_seconds: float,
    backend_base_url: str | None = None,
    expected_core_version: str | None = None,
) -> str | None:
    if not sidecar.is_file():
        raise SmokeFailure(f"sidecar executable does not exist: {sidecar}")

    port = _allocate_port()
    base_url = f"http://127.0.0.1:{port}"
    with TemporaryDirectory(prefix="openevo-sidecar-smoke-") as config_root:
        command = [
            str(sidecar),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--desktop-config-root",
            config_root,
        ]
        if backend_base_url is not None:
            command.extend(["--backend-base-url", backend_base_url])
        with TemporaryFile(mode="w+", encoding="utf-8") as process_log:
            process = subprocess.Popen(
                command,
                stdout=process_log,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + timeout_seconds
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise SmokeFailure(_process_failure(process, process_log))
                    try:
                        health = _read_json(f"{base_url}/health")
                        if health.get("status") == "ok":
                            break
                    except SmokeFailure:
                        time.sleep(0.25)
                else:
                    raise SmokeFailure(
                        f"sidecar did not become healthy within {timeout_seconds}s"
                    )

                core_artifact = _read_json(
                    f"{base_url}/openevo-api/desktop/core-artifact"
                )
                digest = core_artifact.get("distribution_digest")
                framework_lock = core_artifact.get("framework_lock")
                if (
                    core_artifact.get("available") is not True
                    or core_artifact.get("distribution") != "openevo"
                    or not isinstance(digest, str)
                    or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                    or not isinstance(framework_lock, dict)
                    or framework_lock.get("distribution_digest") != digest
                    or framework_lock.get("wheel_filename")
                    != core_artifact.get("wheel_filename")
                ):
                    raise SmokeFailure(
                        "packaged sidecar did not discover its exact embedded Core wheel"
                    )

                index_html = _read_url(f"{base_url}/openevo").decode("utf-8")
                assets = _asset_references(index_html)
                if not assets:
                    raise SmokeFailure("/openevo did not reference any packaged assets")
                for asset in assets:
                    _read_url(f"{base_url}/{asset}")
                registry_digest = None
                if backend_base_url is not None:
                    if expected_core_version is None:
                        raise SmokeFailure(
                            "expected Core version is required for capability proxy smoke"
                        )
                    registry_digest = _smoke_capability_proxy(
                        base_url,
                        expected_core_version=expected_core_version,
                    )
                return registry_digest
            finally:
                _terminate(process)


def _process_failure(process: subprocess.Popen[str], process_log) -> str:
    process.wait(timeout=2)
    process_log.flush()
    process_log.seek(0)
    output = process_log.read()
    return (
        "sidecar exited before serving /health "
        f"(exit {process.returncode}).\noutput:\n{output}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sidecar", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--backend-base-url")
    parser.add_argument("--expected-core-version")
    args = parser.parse_args(argv)

    try:
        smoke_sidecar(
            args.sidecar,
            timeout_seconds=args.timeout_seconds,
            backend_base_url=args.backend_base_url,
            expected_core_version=args.expected_core_version,
        )
    except SmokeFailure as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"OpenEvo Desktop sidecar smoke passed: {args.sidecar}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
