"""OpenEvo WebUI static-host helpers."""

from __future__ import annotations

from desktop.server.app import (
    DesktopStaticAssetsMissingError,
    create_desktop_app,
    packaged_desktop_static_root,
    resolve_desktop_static_root,
)

__all__ = [
    "DesktopStaticAssetsMissingError",
    "create_desktop_app",
    "packaged_desktop_static_root",
    "resolve_desktop_static_root",
]
