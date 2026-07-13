from __future__ import annotations

import hashlib
import hmac
from html.parser import HTMLParser
import http.client
import io
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from desktop.server import (
    DesktopStaticAssetsMissingError,
    create_desktop_app,
    packaged_desktop_static_root,
    resolve_desktop_static_root,
)

import desktop.server.launcher as desktop_launcher
from desktop.server.launcher import DEFAULT_DESKTOP_CONFIG_ROOT, create_app
from desktop.sidecar import create_sidecar_app


class _BinaryStdin:
    def __init__(self, value: bytes) -> None:
        self.buffer = io.BytesIO(value)


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

    packaged_text = (
        index_text
        + "\n"
        + "\n".join((root / asset).read_text(encoding="utf-8") for asset in parser.assets)
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

    packaged_text = (
        index_text
        + "\n"
        + "\n".join((root / asset).read_text(encoding="utf-8") for asset in parser.assets)
    )

    assert "self-deployed" in packaged_text
    assert "codex_subscription_transcript`,`codex_managed_local_inference" not in packaged_text


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


def _native_instance_frame(
    *,
    instance_id: str = "1a" * 16,
    readiness_key: str = "5a" * 32,
    protocol: str = desktop_launcher.NATIVE_SIDECAR_PROTOCOL,
) -> bytes:
    return (
        json.dumps(
            {
                "protocol": protocol,
                "instance_id": instance_id,
                "readiness_key": readiness_key,
            },
            separators=(",", ":"),
        ).encode("ascii")
        + b"\n"
    )


def test_launcher_reads_exact_bounded_native_instance_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        desktop_launcher.sys,
        "stdin",
        _BinaryStdin(_native_instance_frame()),
    )

    instance = desktop_launcher._read_native_instance_frame()

    assert instance.instance_id == "1a" * 16
    assert instance.readiness_key == bytes.fromhex("5a" * 32)
    assert "5a" * 32 not in repr(instance)


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        _native_instance_frame()[:-1],
        b"\xff\n",
        b"[]\n",
        b'{"protocol":"openevo-native-sidecar-v1",'
        b'"instance_id":"1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a"}\n',
        b'{"protocol":"openevo-native-sidecar-v1",'
        b'"instance_id":"1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a",'
        b'"instance_id":"2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b",'
        b'"readiness_key":"' + b"5a" * 32 + b'"}\n',
        _native_instance_frame() + b"extra",
        _native_instance_frame() + _native_instance_frame(),
        _native_instance_frame(protocol="openevo-native-sidecar-v2"),
        _native_instance_frame(instance_id="1A" * 16),
        _native_instance_frame(readiness_key="5a" * 31),
        _native_instance_frame()[:-2] + b',"unknown":true}\n',
        b"x" * (desktop_launcher.NATIVE_INSTANCE_FRAME_MAX_BYTES + 1) + b"\n",
    ],
)
def test_launcher_rejects_malformed_native_instance_frame(
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
) -> None:
    monkeypatch.setattr(desktop_launcher.sys, "stdin", _BinaryStdin(payload))

    with pytest.raises(ValueError, match="invalid native instance frame"):
        desktop_launcher._read_native_instance_frame()


def test_launcher_serves_on_inherited_listener_with_instance_proof(
    tmp_path: Path,
) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    instance_id = "1a" * 16
    secret = bytes.fromhex("5a" * 32)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "desktop.server.launcher",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--listener-fd",
            str(listener.fileno()),
            "--native-instance-stdin",
            "--static-root",
            str(_static_root(tmp_path)),
            "--desktop-config-root",
            str(tmp_path / "config"),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        pass_fds=(listener.fileno(),),
        start_new_session=True,
    )
    try:
        assert process.stdin is not None
        process.stdin.write(_native_instance_frame(instance_id=instance_id))
        process.stdin.close()
        process.stdin = None
        listener.close()

        challenge = "3c" * 32
        deadline = time.monotonic() + 5
        payload: dict[str, str] | None = None
        while time.monotonic() < deadline:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.25)
            try:
                connection.request(
                    "GET",
                    "/health",
                    headers={"X-OpenEvo-Native-Challenge": challenge},
                )
                response = connection.getresponse()
                if response.status == 200:
                    payload = json.loads(response.read())
                    break
            except OSError:
                time.sleep(0.02)
            finally:
                connection.close()

        assert process.poll() is None
        assert payload == {
            "service": "openevo-sidecar",
            "status": "ok",
            "protocol": desktop_launcher.NATIVE_SIDECAR_PROTOCOL,
            "instance_id": instance_id,
            "instance_proof": hmac.new(
                secret,
                (f"{desktop_launcher.NATIVE_SIDECAR_PROTOCOL}\0{instance_id}\0{challenge}").encode(
                    "ascii"
                ),
                hashlib.sha256,
            ).hexdigest(),
        }
    finally:
        listener.close()
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)


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


def test_create_app_launcher_wires_backend_facade_base_url(tmp_path: Path) -> None:
    app = create_app(
        static_root=_static_root(tmp_path),
        backend_base_url="http://127.0.0.1:9",
    )
    client = TestClient(app)
    token = client.get("/openevo-api/desktop/shell").json()["sidecar"]["mutation_token"]

    response = client.get(
        "/openevo-api/backend/status",
        headers={"X-OpenEvo-Sidecar-Token": token},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "backend_connection_failed"


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
            "evolution": {
                "targets": {
                    "text_memory": {
                        "enabled": True,
                        "method": "text_memory_reflector",
                        "config": {},
                    },
                    "skill_bundle": {
                        "enabled": True,
                        "method": "skill_bundle_reflector",
                        "config": {},
                    },
                    "agent_system": {
                        "enabled": True,
                        "method": "auto",
                        "config": {"target_path": "AGENTS.md"},
                    },
                    "parametric_memory": {
                        "enabled": False,
                        "method": "parametric_memory_register",
                        "config": {},
                    },
                }
            },
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"]["remote"]["id"] == "science-team"
    assert payload["status"]["sidecar"]["transport"]["id"] == "ssh"
