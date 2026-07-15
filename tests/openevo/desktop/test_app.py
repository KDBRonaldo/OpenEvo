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
from fastapi import FastAPI
from fastapi.testclient import TestClient

from desktop.server import (
    DesktopStaticAssetsMissingError,
    create_desktop_app,
    packaged_desktop_static_root,
    resolve_desktop_static_root,
)

import desktop.server.launcher as desktop_launcher
from desktop.server.launcher import DEFAULT_DESKTOP_CONFIG_ROOT, create_app
import desktop.packaging.sidecar_entry as sidecar_entry
from desktop.sidecar.workspace_identity import project_id_for_native_import


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


def test_create_desktop_app_serves_spa_without_adding_legacy_api(tmp_path: Path) -> None:
    app = create_desktop_app(FastAPI(), static_root=_static_root(tmp_path))
    client = TestClient(app)

    shell_response = client.get("/openevo-api/desktop/shell")
    assert shell_response.status_code == 404

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
    app = create_desktop_app(FastAPI(), static_root=_static_root(tmp_path))
    client = TestClient(app, follow_redirects=False)

    response = client.get("/")

    assert response.status_code == 307
    assert response.headers["location"] == "/openevo"


def test_create_app_launcher_uses_default_config_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    app = create_app(
        static_root=_static_root(tmp_path),
        native_frame=desktop_launcher._NativeLauncherFrame(
            instance_id="1a" * 16,
            readiness_key=bytes.fromhex("5a" * 32),
            session_token="7c" * 32,
            handoff_token="8d" * 32,
        ),
        source_commit="89baeb26",
        build_channel="test",
    )
    with TestClient(app) as client:
        assert client.get("/openevo").status_code == 200
        assert client.get("/openevo-api/desktop/shell").status_code == 404
    assert DEFAULT_DESKTOP_CONFIG_ROOT.as_posix() == "~/.openevo/desktop"
    assert (
        home
        / ".openevo"
        / "desktop"
        / desktop_launcher.LOCAL_API_STATE_DIRECTORY
        / "provider.sqlite3"
    ).is_file()


def _native_instance_frame(
    *,
    instance_id: str = "1a" * 16,
    readiness_key: str = "5a" * 32,
    session_token: str = "7c" * 32,
    handoff_token: str = "8d" * 32,
    protocol: str = desktop_launcher.NATIVE_SIDECAR_PROTOCOL,
) -> bytes:
    return (
        json.dumps(
            {
                "protocol": protocol,
                "instance_id": instance_id,
                "readiness_key": readiness_key,
                "session_token": session_token,
                "handoff_token": handoff_token,
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

    frame = desktop_launcher._read_native_instance_frame()

    assert frame.instance_id == "1a" * 16
    assert frame.readiness_key == bytes.fromhex("5a" * 32)
    assert frame.session_token == "7c" * 32
    assert frame.handoff_token == "8d" * 32
    assert desktop_launcher.NATIVE_INSTANCE_FRAME_MAX_BYTES == 512
    assert "5a" * 32 not in repr(frame)
    assert "7c" * 32 not in repr(frame)
    assert "8d" * 32 not in repr(frame)


def test_launcher_accepts_native_frame_at_rust_byte_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compact = _native_instance_frame()
    padded = (
        compact[:-1]
        + b" " * (desktop_launcher.NATIVE_INSTANCE_FRAME_MAX_BYTES - len(compact))
        + b"\n"
    )
    assert len(padded) == desktop_launcher.NATIVE_INSTANCE_FRAME_MAX_BYTES
    monkeypatch.setattr(desktop_launcher.sys, "stdin", _BinaryStdin(padded))

    frame = desktop_launcher._read_native_instance_frame()

    assert frame.session_token == "7c" * 32


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
        _native_instance_frame(session_token="7c" * 31),
        _native_instance_frame(session_token="7C" * 32),
        _native_instance_frame(handoff_token="8d" * 31),
        _native_instance_frame(handoff_token="8D" * 32),
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
    session_token = "7c" * 32
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "desktop.server.launcher",
            "--listener-fd",
            str(listener.fileno()),
            "--native-instance-stdin",
            "--static-root",
            str(_static_root(tmp_path)),
            "--desktop-config-root",
            str(tmp_path / "config"),
            "--source-commit",
            "89baeb26",
            "--build-channel",
            "test",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        pass_fds=(listener.fileno(),),
        start_new_session=True,
    )
    try:
        assert process.stdin is not None
        process.stdin.write(
            _native_instance_frame(
                instance_id=instance_id,
                session_token=session_token,
            )
        )
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

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.25)
        connection.request("GET", "/version")
        version_response = connection.getresponse()
        assert version_response.status == 200
        assert json.loads(version_response.read()) == {
            "schema_version": "1",
            "api_name": "openevo-desktop-local-api",
            "preferred_major": 1,
            "supported_majors": [1],
            "openapi_sha256": "e3bc443ee213eb33de81b82c7f954fb617fab14b8a2c17e154f3d4b980ba441f",
            "build_version": "0.1.0",
            "source_commit": "89baeb26",
            "build_channel": "test",
            "provider_kind": "desktop_sidecar",
            "feature_flags": ["remote_profiles"],
        }
        connection.close()

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.25)
        connection.request(
            "GET",
            "/openevo-native/session",
            headers={desktop_launcher.NATIVE_SESSION_HEADER: session_token},
        )
        probe_response = connection.getresponse()
        assert probe_response.status == 204
        assert probe_response.read() == b""
        connection.close()

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.25)
        connection.request("GET", "/openevo-native/session")
        assert connection.getresponse().status == 403
        connection.close()

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.25)
        connection.request(
            "GET",
            "/openevo-native/session",
            headers={desktop_launcher.NATIVE_SESSION_HEADER: "8d" * 32},
        )
        assert connection.getresponse().status == 403
        connection.close()

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.25)
        connection.putrequest("GET", "/openevo-native/session")
        connection.putheader(desktop_launcher.NATIVE_SESSION_HEADER, session_token)
        connection.putheader(desktop_launcher.NATIVE_SESSION_HEADER, session_token)
        connection.endheaders()
        assert connection.getresponse().status == 403
        connection.close()

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.25)
        connection.request(
            "GET",
            "/openevo-api/desktop/run",
            headers={desktop_launcher.NATIVE_SESSION_HEADER: session_token},
        )
        assert connection.getresponse().status == 404
        connection.close()

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.25)
        connection.request(
            "GET",
            "/openevo-api/desktop/run",
            headers={"X-OpenEvo-Sidecar-Token": session_token},
        )
        assert connection.getresponse().status == 404
        connection.close()

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.25)
        connection.request("GET", "/openevo-api/desktop/shell")
        assert connection.getresponse().status == 404
        connection.close()
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
        native_frame=desktop_launcher._NativeLauncherFrame(
            instance_id="1a" * 16,
            readiness_key=bytes.fromhex("5a" * 32),
            session_token="7c" * 32,
            handoff_token="8d" * 32,
        ),
        source_commit="89baeb26",
        build_channel="test",
    )
    with TestClient(app) as client:
        authenticated = client.get(
            "/desktop/v1/state",
            headers={desktop_launcher.NATIVE_SESSION_HEADER: "7c" * 32},
        )
        assert authenticated.status_code == 200
        assert client.get("/desktop/v1/state").status_code == 401
        assert client.get("/openevo-api/backend/status").status_code == 404

    assert (config_root / desktop_launcher.LOCAL_API_STATE_DIRECTORY).is_dir()
    assert (
        config_root / desktop_launcher.LOCAL_API_STATE_DIRECTORY / "provider.sqlite3"
    ).is_file()


def test_native_workspace_route_is_private_idempotent_and_project_bound(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    source_root = tmp_path / "research-data"
    source_root.mkdir()
    (source_root / "observations.csv").write_text("sample,value\na,7\n", encoding="utf-8")
    session_token = "7c" * 32
    handoff_token = "8d" * 32
    session_headers = {desktop_launcher.NATIVE_SESSION_HEADER: session_token}
    handoff_headers = {desktop_launcher.NATIVE_HANDOFF_HEADER: handoff_token}
    app = create_app(
        static_root=_static_root(tmp_path),
        desktop_config_root=config_root,
        native_frame=desktop_launcher._NativeLauncherFrame(
            instance_id="1a" * 16,
            readiness_key=bytes.fromhex("5a" * 32),
            session_token=session_token,
            handoff_token=handoff_token,
        ),
        source_commit="89baeb26",
        build_channel="test",
    )
    request = {
        "schema_version": "1",
        "kind": "native_folder_snapshot",
        "action_id": "native-source-action-0001",
        "selected_path": str(source_root.resolve()),
        "selected_device": source_root.stat().st_dev,
        "selected_inode": source_root.stat().st_ino,
        "cancellation_token": "9c" * 32,
    }

    with TestClient(app) as client:
        assert client.post("/openevo-native/workspace-imports", json=request).status_code == 403
        assert (
            client.post(
                "/openevo-native/workspace-imports",
                headers=session_headers,
                json=request,
            ).status_code
            == 403
        )
        imported = client.post(
            "/openevo-native/workspace-imports",
            headers=handoff_headers,
            json=request,
        )
        replayed = client.post(
            "/openevo-native/workspace-imports",
            headers=handoff_headers,
            json=request,
        )
        reselected = client.post(
            "/openevo-native/workspace-imports",
            headers=handoff_headers,
            json={
                **request,
                "action_id": "native-source-action-0002",
                "cancellation_token": "9d" * 32,
                "project_id": project_id_for_native_import(
                    imported.json()["source"]["import_ref"]["import_id"]
                ),
            },
        )

        assert imported.status_code == replayed.status_code == reselected.status_code == 201
        assert imported.json() == replayed.json()
        assert (
            imported.json()["source"]["import_ref"]["content_sha256"]
            == reselected.json()["source"]["import_ref"]["content_sha256"]
        )
        assert (
            imported.json()["source"]["import_ref"]["import_id"]
            != reselected.json()["source"]["import_ref"]["import_id"]
        )
        payload = imported.json()
        assert set(payload) == {"schema_version", "source", "lease_token"}
        source = payload["source"]
        assert source["kind"] == "native_folder_snapshot"
        assert source["display_name"] == "research-data"
        assert source["import_ref"]["import_id"].startswith("workspace-import-")
        assert str(source_root) not in imported.text
        assert "selected_path" not in imported.text
        assert "/openevo-native/workspace-imports" not in client.get("/openapi.json").text

        profile = client.post(
            "/desktop/v1/profiles",
            headers={**session_headers, "Idempotency-Key": "create-profile-action-0001"},
            json={
                "name": "Research server",
                "host": "compute.example.org",
                "port": 22,
                "user": "researcher",
            },
        )
        assert profile.status_code == 201
        project = client.post(
            "/desktop/v1/projects",
            headers={**session_headers, "Idempotency-Key": "create-project-action-0001"},
            json={
                "name": "Native research project",
                "profile_id": profile.json()["profile_id"],
                "task": {"title": "Analyse", "objective": "Analyse the imported results."},
                "source": source,
                "execution": {
                    "mode": "codex_subscription_transcript",
                    "codex_model": "gpt-5",
                },
                "evolution": {"targets": {}},
            },
        )

        assert project.status_code == 201
        assert project.json()["project_id"] == project_id_for_native_import(
            source["import_ref"]["import_id"]
        )


@pytest.mark.parametrize(
    ("source_commit", "build_channel"),
    [
        ("ABCDEF0", "test"),
        ("123456", "test"),
        ("0" * 40, "release"),
    ],
)
def test_create_app_launcher_rejects_invalid_source_commit(
    tmp_path: Path,
    source_commit: str,
    build_channel: str,
) -> None:
    with pytest.raises(ValueError, match="source commit"):
        create_app(
            static_root=_static_root(tmp_path),
            native_frame=desktop_launcher._NativeLauncherFrame(
                instance_id="1a" * 16,
                readiness_key=bytes.fromhex("5a" * 32),
                session_token="7c" * 32,
                handoff_token="8d" * 32,
            ),
            source_commit=source_commit,
            build_channel=build_channel,
        )


@pytest.mark.parametrize(
    "payload",
    [
        b'{"schema_version":"1","source_commit":"0000000"}\n',
        b'{"schema_version":"1","source_commit":"89BAEB26"}\n',
        b'{"schema_version":"1","source_commit":"89baeb26","extra":true}\n',
        b'{"schema_version":"1","schema_version":"1","source_commit":"89baeb26"}\n',
    ],
)
def test_packaged_sidecar_build_metadata_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
) -> None:
    path = tmp_path / sidecar_entry.BUILD_METADATA_RELATIVE_PATH
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    monkeypatch.setattr(sidecar_entry.sys, "_MEIPASS", str(tmp_path), raising=False)

    with pytest.raises(ValueError, match="invalid packaged sidecar build metadata"):
        sidecar_entry._load_packaged_build_metadata()


def test_packaged_sidecar_build_metadata_returns_baked_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / sidecar_entry.BUILD_METADATA_RELATIVE_PATH
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"schema_version":"1","source_commit":"89baeb26"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(sidecar_entry.sys, "_MEIPASS", str(tmp_path), raising=False)

    metadata = sidecar_entry._load_packaged_build_metadata()

    assert metadata.source_commit == "89baeb26"
