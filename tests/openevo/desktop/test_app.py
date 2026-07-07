from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openevo.desktop import (
    DesktopStaticAssetsMissingError,
    create_desktop_app,
    packaged_desktop_static_root,
    resolve_desktop_static_root,
)
from openevo.sidecar import create_sidecar_app


def _static_root(tmp_path: Path) -> Path:
    root = tmp_path / "desktop-web"
    assets = root / "assets"
    assets.mkdir(parents=True)
    (root / "index.html").write_text(
        (
            "<!doctype html><title>OpenEvo Test Desktop</title>"
            "<script type='module' src='/assets/app.js'></script>"
            "<div id='root'></div>"
        ),
        encoding="utf-8",
    )
    (assets / "app.js").write_text("window.__openevoTest = true;", encoding="utf-8")
    return root


def test_resolve_desktop_static_root_accepts_override(tmp_path: Path) -> None:
    root = _static_root(tmp_path)

    assert resolve_desktop_static_root(root) == root


def test_resolve_desktop_static_root_reports_missing_index(tmp_path: Path) -> None:
    with pytest.raises(DesktopStaticAssetsMissingError) as exc_info:
        resolve_desktop_static_root(tmp_path / "missing")

    assert "OpenEvo Desktop static assets were not found" in str(exc_info.value)
    assert "--static-root" in str(exc_info.value)


def test_resolve_desktop_static_root_reports_missing_assets_dir(tmp_path: Path) -> None:
    root = tmp_path / "desktop-web"
    root.mkdir()
    (root / "index.html").write_text(
        "<!doctype html><title>OpenEvo Test Desktop</title><div id='root'></div>",
        encoding="utf-8",
    )

    with pytest.raises(DesktopStaticAssetsMissingError) as exc_info:
        resolve_desktop_static_root(root)

    assert "assets" in str(exc_info.value)


def test_resolve_desktop_static_root_reports_missing_referenced_asset(
    tmp_path: Path,
) -> None:
    root = tmp_path / "desktop-web"
    assets = root / "assets"
    assets.mkdir(parents=True)
    (root / "index.html").write_text(
        (
            "<!doctype html><title>OpenEvo Test Desktop</title>"
            "<script type='module' src='/assets/missing.js'></script>"
            "<link rel='stylesheet' href='/assets/present.css'>"
        ),
        encoding="utf-8",
    )
    (assets / "present.css").write_text("body { color: black; }", encoding="utf-8")

    with pytest.raises(DesktopStaticAssetsMissingError) as exc_info:
        resolve_desktop_static_root(root)

    assert "assets/missing.js" in str(exc_info.value)


def test_packaged_desktop_static_root_points_at_bundled_assets() -> None:
    root = packaged_desktop_static_root()

    assert root.name == "web"
    assert (root / "index.html").is_file()


def test_create_desktop_app_serves_spa_and_sidecar_api(tmp_path: Path) -> None:
    app = create_desktop_app(create_sidecar_app(), static_root=_static_root(tmp_path))
    client = TestClient(app)

    shell_response = client.get("/openevo-api/desktop/shell")
    assert shell_response.status_code == 200
    assert shell_response.json()["execution"]["mode"] == "codex_subscription_transcript"

    index_response = client.get("/openevo")
    assert index_response.status_code == 200
    assert "OpenEvo Test Desktop" in index_response.text

    nested_response = client.get("/openevo/projects/folding-baseline")
    assert nested_response.status_code == 200
    assert "OpenEvo Test Desktop" in nested_response.text

    tasks_response = client.get("/tasks")
    assert tasks_response.status_code == 200
    assert "OpenEvo Test Desktop" in tasks_response.text

    session_response = client.get("/sessions/session-a")
    assert session_response.status_code == 200
    assert "OpenEvo Test Desktop" in session_response.text

    missing_api_response = client.get("/openevo-api/not-found")
    assert missing_api_response.status_code == 404

    asset_response = client.get("/assets/app.js")
    assert asset_response.status_code == 200
    assert "window.__openevoTest" in asset_response.text


def test_create_desktop_app_redirects_root_to_openevo(tmp_path: Path) -> None:
    app = create_desktop_app(create_sidecar_app(), static_root=_static_root(tmp_path))
    client = TestClient(app, follow_redirects=False)

    response = client.get("/")

    assert response.status_code == 307
    assert response.headers["location"] == "/openevo"
