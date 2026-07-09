from __future__ import annotations

import argparse
from collections.abc import Callable
import os
from pathlib import Path

from fastapi import FastAPI

from desktop.server.app import create_desktop_app
from desktop.sidecar import create_sidecar_app
from desktop.sidecar.backend_client import BackendConnection
from desktop.sidecar.api import SidecarTransportKind
from openevo.deployment import (
    RemoteExecutorTransport,
    RemoteProfileConfig,
    SshRemoteExecutorTransport,
)

DEFAULT_DESKTOP_CONFIG_ROOT = Path("~/.openevo/desktop")


def _ssh_transport_factory(profile: RemoteProfileConfig) -> RemoteExecutorTransport:
    return SshRemoteExecutorTransport(profile)


def create_app(
    *,
    static_root: Path | str | None = None,
    desktop_config_root: Path | str | None = None,
    backend_base_url: str | None = None,
    transport_factory: Callable[[RemoteProfileConfig], RemoteExecutorTransport]
    | None = None,
    transport_kind: SidecarTransportKind = "ssh",
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
        ),
        static_root=static_root,
    )


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
    args = parser.parse_args(argv)

    import uvicorn

    uvicorn.run(
        create_app(
            static_root=args.static_root,
            desktop_config_root=args.desktop_config_root,
            backend_base_url=args.backend_base_url,
        ),
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
