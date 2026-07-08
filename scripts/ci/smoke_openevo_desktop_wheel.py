#!/usr/bin/env python3
"""Smoke test the installed OpenEvo Desktop app and packaged static assets."""

from __future__ import annotations

from html.parser import HTMLParser
import posixpath
import sys
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from openevo.desktop.app import create_desktop_app
from openevo.sidecar.api import create_sidecar_app


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
        raise ValueError(f"Invalid Desktop asset reference: {value}")
    return normalized


def _asset_references(index_html: str) -> list[str]:
    parser = _AssetParser()
    parser.feed(index_html)
    return sorted(set(parser.assets))


def main() -> int:
    app = create_desktop_app(create_sidecar_app())
    with TestClient(app) as client:
        index = client.get("/openevo")
        if index.status_code != 200:
            print(f"/openevo returned HTTP {index.status_code}", file=sys.stderr)
            return 1
        assets = _asset_references(index.text)
        if not assets:
            print("/openevo did not reference any packaged assets", file=sys.stderr)
            return 1
        for asset in assets:
            response = client.get(f"/{asset}")
            if response.status_code != 200:
                print(
                    f"/{asset} returned HTTP {response.status_code}",
                    file=sys.stderr,
                )
                return 1
    print(f"OpenEvo Desktop wheel smoke passed for {len(assets)} asset(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
