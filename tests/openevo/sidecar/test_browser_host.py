from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from desktop.server.browser_host import ManagedOpenSshHome, install_browser_host_routes


ENDPOINT = "http://127.0.0.1:43117"
BOOTSTRAP_TOKEN = "a1" * 32
SESSION_TOKEN = "b2" * 32


def _app(root: Path) -> FastAPI:
    app = FastAPI()
    install_browser_host_routes(
        app,
        endpoint=ENDPOINT,
        bootstrap_token=BOOTSTRAP_TOKEN,
        session_token=SESSION_TOKEN,
        negotiated_contract={"schema_version": "2", "major": 2},
        managed_ssh_home=ManagedOpenSshHome(root),
    )
    return app


def test_browser_bootstrap_is_loopback_bound_and_one_time(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path), base_url=ENDPOINT) as client:
        response = client.post(
            "/openevo-native/browser/bootstrap",
            json={"schema_version": "2", "bootstrap_token": BOOTSTRAP_TOKEN},
        )
        assert response.status_code == 200
        assert response.json()["session_token"] == SESSION_TOKEN
        assert response.headers["cache-control"] == "no-store"

        repeated = client.post(
            "/openevo-native/browser/bootstrap",
            json={"schema_version": "2", "bootstrap_token": BOOTSTRAP_TOKEN},
        )
        assert repeated.status_code == 403


def test_browser_registers_server_details_as_private_openssh_alias(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path), base_url=ENDPOINT) as client:
        rejected = client.post(
            "/openevo-native/browser/ssh-hosts",
            json={
                "schema_version": "2",
                "host": "gpu.example.edu",
                "port": 27104,
                "username": "researcher",
            },
        )
        assert rejected.status_code == 403

        response = client.post(
            "/openevo-native/browser/ssh-hosts",
            headers={"X-OpenEvo-Desktop-Session": SESSION_TOKEN},
            json={
                "schema_version": "2",
                "host": "gpu.example.edu",
                "port": 27104,
                "username": "researcher",
            },
        )
        assert response.status_code == 200
        alias = response.json()["ssh_host_alias"]
        assert alias.startswith("openevo-")

    config = (tmp_path / ".ssh" / "config").read_text(encoding="utf-8")
    assert f"Host {alias}" in config
    assert "HostName gpu.example.edu" in config
    assert "User researcher" in config
    assert "Port 27104" in config
    assert "password" not in config.lower()
    state = json.loads((tmp_path / ".ssh" / "openevo-hosts.json").read_text())
    assert state[alias] == {
        "host": "gpu.example.edu",
        "port": 27104,
        "username": "researcher",
    }


@pytest.mark.parametrize(
    ("host", "username"),
    (("gpu host", "researcher"), ("gpu.example.edu", "root\nProxyCommand bad")),
)
def test_managed_openssh_home_rejects_config_injection(
    tmp_path: Path,
    host: str,
    username: str,
) -> None:
    store = ManagedOpenSshHome(tmp_path)
    with pytest.raises(ValueError, match="invalid"):
        store.register(host=host, port=22, username=username)
