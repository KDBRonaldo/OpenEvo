from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from desktop.server import (
    DesktopStaticAssetsMissingError,
    create_desktop_app,
    packaged_desktop_static_root,
    resolve_desktop_static_root,
)
from desktop.server.launcher import DEFAULT_DESKTOP_CONFIG_ROOT, create_app
from desktop.sidecar import create_sidecar_app


class _PackagedAssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.assets: list[Path] = []

    def handle_starttag(
        self,
        _tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        for name, value in attrs:
            if name not in {"href", "src"} or value is None:
                continue
            if value.startswith("/assets/"):
                self.assets.append(Path(value[1:]))
            elif value.startswith("assets/"):
                self.assets.append(Path(value))


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


def test_packaged_desktop_assets_are_openevo_only() -> None:
    root = packaged_desktop_static_root()
    index_text = (root / "index.html").read_text(encoding="utf-8")
    parser = _PackagedAssetParser()
    parser.feed(index_text)

    packaged_text = index_text + "\n" + "\n".join(
        (root / asset).read_text(encoding="utf-8") for asset in parser.assets
    )

    assert "OpenEvo Desktop" in packaged_text
    assert "OpenEvo Observability" not in packaged_text
    assert 'href="/tasks"' not in packaged_text
    assert ">Dashboard<" not in packaged_text
    assert "/api/events" not in packaged_text


def test_packaged_desktop_assets_expose_self_deployed_mode() -> None:
    root = packaged_desktop_static_root()
    index_text = (root / "index.html").read_text(encoding="utf-8")
    parser = _PackagedAssetParser()
    parser.feed(index_text)

    packaged_text = index_text + "\n" + "\n".join(
        (root / asset).read_text(encoding="utf-8") for asset in parser.assets
    )

    assert "self-deployed" in packaged_text
    assert (
        "codex_subscription_transcript`,`codex_managed_local_inference"
        not in packaged_text
    )


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


def test_create_app_launcher_uses_default_config_root(tmp_path: Path) -> None:
    app = create_app(static_root=_static_root(tmp_path))
    client = TestClient(app)

    shell_response = client.get("/openevo-api/desktop/shell")
    assert shell_response.status_code == 200
    assert DEFAULT_DESKTOP_CONFIG_ROOT.as_posix() == "~/.openevo/desktop"


def test_create_app_launcher_accepts_config_root_override(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    app = create_app(
        static_root=_static_root(tmp_path),
        desktop_config_root=config_root,
    )
    client = TestClient(app)
    token = client.get("/openevo-api/desktop/shell").json()["sidecar"]["mutation_token"]

    response = client.get(
        "/openevo-api/desktop/project-configs",
        headers={"X-OpenEvo-Sidecar-Token": token},
    )

    assert response.status_code == 200
    assert response.json() == {"configs": []}


def test_create_app_launcher_saves_first_project_config(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    app = create_app(
        static_root=_static_root(tmp_path),
        desktop_config_root=config_root,
    )
    client = TestClient(app)
    token = client.get("/openevo-api/desktop/shell").json()["sidecar"]["mutation_token"]

    response = client.post(
        "/openevo-api/desktop/project-config",
        headers={"X-OpenEvo-Sidecar-Token": token},
        json={
            "project_name": "Protein Design",
            "task_id": "folding-baseline",
            "objective": "Improve the folding baseline.",
            "source_type": "remote_path",
            "source_path": "/datasets/folding-baseline",
            "remote_profile_id": "science-team",
            "remote_host": "gpu.example.edu",
            "remote_port": 22,
            "remote_user": "alice",
            "auth_method": "ssh_agent",
            "codex_model": "gpt-5.1-codex-mini",
            "text_memory": True,
            "skill_bundle": True,
            "agent_system": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"]["remote"]["id"] == "science-team"
    assert payload["status"]["sidecar"]["transport"]["id"] == "ssh"
