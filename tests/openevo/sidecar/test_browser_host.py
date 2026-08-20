from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from desktop.server.browser_host import install_browser_host_routes


ENDPOINT = "http://127.0.0.1:43117"
BOOTSTRAP_TOKEN = "a1" * 32
SESSION_TOKEN = "b2" * 32


def _app() -> FastAPI:
    app = FastAPI()
    install_browser_host_routes(
        app,
        endpoint=ENDPOINT,
        bootstrap_token=BOOTSTRAP_TOKEN,
        session_token=SESSION_TOKEN,
        negotiated_contract={"schema_version": "2", "major": 2},
    )
    return app


def test_browser_bootstrap_is_loopback_bound_and_idempotent() -> None:
    with TestClient(_app(), base_url=ENDPOINT) as client:
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
        assert repeated.status_code == 200
        assert repeated.json() == response.json()

        invalid = client.post(
            "/openevo-native/browser/bootstrap",
            json={"schema_version": "2", "bootstrap_token": "c3" * 32},
        )
        assert invalid.status_code == 403


def test_browser_host_does_not_accept_raw_ssh_registration() -> None:
    with TestClient(_app(), base_url=ENDPOINT) as client:
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
        assert response.status_code == 404
