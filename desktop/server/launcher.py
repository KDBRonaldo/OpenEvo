from __future__ import annotations

import argparse
from collections.abc import Callable
import json
import os
from pathlib import Path
import re
import socket
import sys

from fastapi import FastAPI

from desktop.server.app import create_desktop_app
from desktop.sidecar import create_sidecar_app
from desktop.sidecar.backend_client import BackendConnection
from desktop.sidecar.api import (
    NATIVE_SIDECAR_PROTOCOL,
    NativeSidecarInstance,
    SidecarTransportKind,
)
from openevo.deployment import (
    RemoteExecutorTransport,
    RemoteProfileConfig,
    SshRemoteExecutorTransport,
)

DEFAULT_DESKTOP_CONFIG_ROOT = Path("~/.openevo/desktop")
NATIVE_INSTANCE_FRAME_MAX_BYTES = 256


def _ssh_transport_factory(profile: RemoteProfileConfig) -> RemoteExecutorTransport:
    return SshRemoteExecutorTransport(profile)


def create_app(
    *,
    static_root: Path | str | None = None,
    desktop_config_root: Path | str | None = None,
    backend_base_url: str | None = None,
    transport_factory: Callable[[RemoteProfileConfig], RemoteExecutorTransport] | None = None,
    transport_kind: SidecarTransportKind = "ssh",
    native_instance: NativeSidecarInstance | None = None,
) -> FastAPI:
    config_root = (
        Path(desktop_config_root).expanduser()
        if desktop_config_root is not None
        else DEFAULT_DESKTOP_CONFIG_ROOT.expanduser()
    )
    return create_desktop_app(
        create_sidecar_app(
            config_root=config_root,
            transport_factory=transport_factory or _ssh_transport_factory,
            transport_kind=transport_kind,
            backend_connection=_backend_connection(backend_base_url),
            native_instance=native_instance,
        ),
        static_root=static_root,
    )


def _read_native_instance_frame() -> NativeSidecarInstance:
    encoded = sys.stdin.buffer.readline(NATIVE_INSTANCE_FRAME_MAX_BYTES + 1)
    if not encoded.endswith(b"\n") or len(encoded) > NATIVE_INSTANCE_FRAME_MAX_BYTES:
        raise ValueError("invalid native instance frame")
    if sys.stdin.buffer.read(1) != b"":
        raise ValueError("invalid native instance frame")
    try:
        text = encoded[:-1].decode("utf-8", errors="strict")
        payload = json.loads(text, object_pairs_hook=_strict_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("invalid native instance frame") from exc
    if type(payload) is not dict or set(payload) != {
        "protocol",
        "instance_id",
        "readiness_key",
    }:
        raise ValueError("invalid native instance frame")
    protocol = payload["protocol"]
    instance_id = payload["instance_id"]
    readiness_key = payload["readiness_key"]
    if (
        type(protocol) is not str
        or protocol != NATIVE_SIDECAR_PROTOCOL
        or type(instance_id) is not str
        or re.fullmatch(r"[0-9a-f]{32}", instance_id) is None
        or type(readiness_key) is not str
        or re.fullmatch(r"[0-9a-f]{64}", readiness_key) is None
    ):
        raise ValueError("invalid native instance frame")
    return NativeSidecarInstance(
        instance_id=instance_id,
        readiness_key=bytes.fromhex(readiness_key),
    )


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate native instance frame key")
        value[key] = item
    return value


def _backend_connection(backend_base_url: str | None) -> BackendConnection | None:
    base_url = backend_base_url or os.environ.get("OPENEVO_DESKTOP_BACKEND_BASE_URL")
    if not base_url:
        return None
    return BackendConnection(base_url=base_url.rstrip("/"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the OpenEvo Desktop sidecar.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--static-root", type=Path, default=None)
    parser.add_argument(
        "--desktop-config-root",
        type=Path,
        default=DEFAULT_DESKTOP_CONFIG_ROOT,
    )
    parser.add_argument("--backend-base-url", default=None)
    parser.add_argument("--listener-fd", type=int, default=None)
    parser.add_argument("--native-instance-stdin", action="store_true")
    args = parser.parse_args(argv)

    if (args.listener_fd is None) != (not args.native_instance_stdin):
        parser.error("--listener-fd and --native-instance-stdin must be provided together")
    if args.listener_fd is not None and args.listener_fd < 3:
        parser.error("--listener-fd must identify an inherited non-standard descriptor")
    native_instance = _read_native_instance_frame() if args.native_instance_stdin else None

    import uvicorn

    app = create_app(
        static_root=args.static_root,
        desktop_config_root=args.desktop_config_root,
        backend_base_url=args.backend_base_url,
        native_instance=native_instance,
    )
    if args.listener_fd is None:
        uvicorn.run(app, host=args.host, port=args.port)
        return 0

    listener = socket.socket(fileno=args.listener_fd)
    listener.set_inheritable(False)
    listener.setblocking(False)
    config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
    )
    uvicorn.Server(config).run(sockets=[listener])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
