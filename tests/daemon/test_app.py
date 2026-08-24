from __future__ import annotations

from fastapi.testclient import TestClient

from openevo.daemon.app import create_daemon_app


def test_health_is_public_and_closed() -> None:
    client = TestClient(create_daemon_app(token="secret"))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": "1",
        "status": "ready",
        "service": "openevo-daemon",
        "api_version": "daemon-v1alpha1",
    }


def test_status_requires_exact_bearer_token() -> None:
    client = TestClient(
        create_daemon_app(token="secret", started_at="2026-08-24T12:00:00Z")
    )

    missing = client.get("/v1/daemon/status")
    wrong = client.get(
        "/v1/daemon/status", headers={"Authorization": "Bearer wrong"}
    )
    accepted = client.get(
        "/v1/daemon/status", headers={"Authorization": "Bearer secret"}
    )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert missing.json()["code"] == "authentication_required"
    assert accepted.status_code == 200
    assert accepted.json()["started_at"] == "2026-08-24T12:00:00Z"
    assert accepted.json()["pid"] > 0
    assert accepted.json()["capabilities"] == [
        "health",
        "authenticated_status",
    ]


def test_empty_token_is_rejected_at_construction() -> None:
    try:
        create_daemon_app(token="  ")
    except ValueError as exc:
        assert str(exc) == "daemon token must not be empty"
    else:
        raise AssertionError("empty daemon token was accepted")
