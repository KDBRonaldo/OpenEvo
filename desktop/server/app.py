from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import posixpath
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles


class DesktopStaticAssetsMissingError(FileNotFoundError):
    """Raised when the built OpenEvo WebUI assets are unavailable."""


def packaged_desktop_static_root() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "src"
        / "openevo"
        / "web_gateway"
        / "static"
    )


def resolve_desktop_static_root(static_root: Path | str | None = None) -> Path:
    root = (
        Path(static_root).expanduser()
        if static_root is not None
        else packaged_desktop_static_root()
    )
    index_path = root / "index.html"
    if not index_path.is_file():
        raise DesktopStaticAssetsMissingError(
            "OpenEvo WebUI static assets were not found. Run `cd desktop && npm run "
            "build:webui-gateway`, or pass `--static-root`."
        )
    _validate_desktop_static_assets(root, index_path)
    return root


class _IndexAssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.assets: list[Path] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name not in {"href", "src"} or value is None:
                continue
            asset = _asset_reference(value)
            if asset is not None:
                self.assets.append(asset)


def _validate_desktop_static_assets(root: Path, index_path: Path) -> None:
    assets_path = root / "assets"
    if not assets_path.is_dir():
        raise DesktopStaticAssetsMissingError(
            "OpenEvo WebUI static assets were not found: assets directory is "
            "missing. Run `cd desktop && npm run build:webui-gateway` "
            "or pass `--static-root`."
        )
    if not any(path.is_file() for path in assets_path.rglob("*")):
        raise DesktopStaticAssetsMissingError(
            "OpenEvo WebUI static assets were not found: assets directory is "
            "empty. Run `cd desktop && npm run build:webui-gateway` "
            "or pass `--static-root`."
        )

    parser = _IndexAssetParser()
    parser.feed(index_path.read_text(encoding="utf-8"))
    for asset in parser.assets:
        if not (root / asset).is_file():
            raise DesktopStaticAssetsMissingError(
                "OpenEvo WebUI static assets were not found: referenced asset "
                f"`{asset.as_posix()}` is missing. Run `cd desktop && npm run "
                "build:webui-gateway`, or pass `--static-root`."
            )


def _asset_reference(value: str) -> Path | None:
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
        raise DesktopStaticAssetsMissingError(
            f"OpenEvo WebUI static asset reference is invalid: `{value}`."
        )
    return Path(*normalized.split("/"))


def create_desktop_app(
    sidecar_app: FastAPI,
    *,
    static_root: Path | str | None = None,
) -> FastAPI:
    root = resolve_desktop_static_root(static_root)
    index_path = root / "index.html"
    assets_path = root / "assets"

    if assets_path.is_dir():
        sidecar_app.mount(
            "/assets",
            StaticFiles(directory=str(assets_path)),
            name="openevo-assets",
        )

    @sidecar_app.get("/", include_in_schema=False)
    def redirect_to_desktop() -> RedirectResponse:
        return RedirectResponse("/openevo")

    @sidecar_app.get("/openevo", include_in_schema=False)
    def serve_desktop_index() -> FileResponse:
        return FileResponse(index_path)

    @sidecar_app.get("/openevo/{path:path}", include_in_schema=False)
    def serve_desktop_spa(path: str) -> FileResponse:
        candidate = root / path
        if candidate.is_file() and candidate.resolve().is_relative_to(root.resolve()):
            return FileResponse(candidate)
        return FileResponse(index_path)

    @sidecar_app.get("/tasks", include_in_schema=False)
    def serve_tasks_index() -> FileResponse:
        return FileResponse(index_path)

    @sidecar_app.get("/tasks/{path:path}", include_in_schema=False)
    def serve_tasks_route(path: str) -> FileResponse:
        return FileResponse(index_path)

    @sidecar_app.get("/sessions", include_in_schema=False)
    def serve_sessions_index() -> FileResponse:
        return FileResponse(index_path)

    @sidecar_app.get("/sessions/{path:path}", include_in_schema=False)
    def serve_sessions_route(path: str) -> FileResponse:
        return FileResponse(index_path)

    @sidecar_app.get("/compare", include_in_schema=False)
    def serve_compare_route() -> FileResponse:
        return FileResponse(index_path)

    return sidecar_app
