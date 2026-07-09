from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from desktop.sidecar import create_sidecar_app
from desktop.sidecar.backend_client import (
    BackendClient,
    BackendConnection,
    DesktopBackendError,
)


def test_backend_client_preserves_typed_error_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/projects/missing-project"
        return httpx.Response(
            404,
            json={
                "code": "project_not_found",
                "message": "Project was not found.",
                "severity": "blocking",
                "category": "project",
                "retryable": False,
                "repair_action": "user_action_required",
                "details": {},
                "logs_ref": None,
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = BackendClient(
        BackendConnection(base_url="http://openevo.test"),
        http_client=http_client,
    )

    with pytest.raises(DesktopBackendError) as exc_info:
        client._get("/projects/missing-project")

    assert exc_info.value.status_code == 404
    assert exc_info.value.error["code"] == "project_not_found"
    assert exc_info.value.error["repair_action"] == "user_action_required"


def test_backend_client_normalizes_non_json_http_errors() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="backend unavailable")

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = BackendClient(
        BackendConnection(base_url="http://openevo.test"),
        http_client=http_client,
    )

    with pytest.raises(DesktopBackendError) as exc_info:
        client.status()

    assert exc_info.value.status_code == 503
    assert exc_info.value.error == {
        "code": "backend_http_error",
        "message": "backend unavailable",
        "severity": "blocking",
        "category": "internal",
        "retryable": False,
        "repair_action": "user_action_required",
        "details": {"status_code": 503},
        "logs_ref": None,
    }


def test_backend_client_normalizes_connection_errors() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = BackendClient(
        BackendConnection(base_url="http://127.0.0.1:8765"),
        http_client=http_client,
    )

    with pytest.raises(DesktopBackendError) as exc_info:
        client.health()

    assert exc_info.value.status_code == 503
    assert exc_info.value.error["code"] == "backend_connection_failed"
    assert exc_info.value.error["category"] == "service"
    assert exc_info.value.error["retryable"] is True
    assert exc_info.value.error["repair_action"] == "openevo_can_retry"


def test_backend_client_quotes_opaque_path_segments() -> None:
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, json=[])

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = BackendClient(
        BackendConnection(base_url="http://openevo.test"),
        http_client=http_client,
    )

    assert client.run_timeline("run/one?round=1") == []
    assert seen_urls == [
        "http://openevo.test/runs/run%2Fone%3Fround%3D1/timeline"
    ]


class _FakeBackendClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def health(self) -> dict[str, Any]:
        self.calls.append(("health", None))
        return {"status": "ok"}

    def status(self) -> dict[str, Any]:
        self.calls.append(("status", None))
        return {
            "status": "ready",
            "services": [
                {
                    "id": "gateway",
                    "name": "Gateway",
                    "status": "running",
                    "restartable": True,
                }
            ],
            "active_runs": 1,
            "supervision_mode": "managed",
        }

    def run_timeline(self, run_id: str) -> list[dict[str, Any]]:
        self.calls.append(("run_timeline", run_id))
        return [
            {
                "id": "round-1-memory",
                "phase": "evolution",
                "title": "Memory updated",
                "message": "A memory artifact was promoted.",
                "artifact_ids": ["artifact-1"],
            }
        ]

    def artifact_content(self, artifact_id: str) -> dict[str, Any]:
        self.calls.append(("artifact_content", artifact_id))
        return {
            "id": artifact_id,
            "artifact_type": "text_memory",
            "content": "# Memory",
            "metadata": {},
        }


def test_sidecar_backend_facade_forwards_remote_backend_payloads() -> None:
    backend = _FakeBackendClient()
    client = TestClient(create_sidecar_app(backend_client_factory=lambda: backend))
    token = client.get("/openevo-api/desktop/shell").json()["sidecar"]["mutation_token"]
    headers = {"X-OpenEvo-Sidecar-Token": token}

    assert client.get("/openevo-api/backend/health", headers=headers).json() == {"status": "ok"}
    assert (
        client.get("/openevo-api/backend/status", headers=headers).json()["services"][0]["id"]
        == "gateway"
    )
    timeline = client.get("/openevo-api/backend/runs/run-1/timeline", headers=headers).json()

    assert timeline[0]["artifact_ids"] == ["artifact-1"]
    assert backend.calls == [
        ("health", None),
        ("status", None),
        ("run_timeline", "run-1"),
    ]


def test_sidecar_backend_facade_accepts_encoded_opaque_ids() -> None:
    backend = _FakeBackendClient()
    client = TestClient(create_sidecar_app(backend_client_factory=lambda: backend))
    token = client.get("/openevo-api/desktop/shell").json()["sidecar"]["mutation_token"]
    headers = {"X-OpenEvo-Sidecar-Token": token}

    timeline = client.get(
        "/openevo-api/backend/runs/run%2Fone%3Fround%3D1/timeline",
        headers=headers,
    )
    content = client.get(
        "/openevo-api/backend/artifacts/artifact%2Fone%3Fkind%3Dmemory/content",
        headers=headers,
    )

    assert timeline.status_code == 200
    assert content.status_code == 200
    assert backend.calls == [
        ("run_timeline", "run/one?round=1"),
        ("artifact_content", "artifact/one?kind=memory"),
    ]


def test_sidecar_backend_facade_requires_sidecar_token() -> None:
    backend = _FakeBackendClient()
    client = TestClient(create_sidecar_app(backend_client_factory=lambda: backend))

    response = client.get("/openevo-api/backend/status")

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid OpenEvo sidecar token."
    assert backend.calls == []


def test_sidecar_backend_facade_reports_typed_setup_error_without_tunnel() -> None:
    client = TestClient(create_sidecar_app())
    token = client.get("/openevo-api/desktop/shell").json()["sidecar"]["mutation_token"]

    response = client.get(
        "/openevo-api/backend/status",
        headers={"X-OpenEvo-Sidecar-Token": token},
    )

    assert response.status_code == 409
    assert response.json() == {
        "code": "backend_tunnel_not_configured",
        "message": "Desktop has no active tunnel to the remote OpenEvo backend.",
        "severity": "blocking",
        "category": "service",
        "retryable": True,
        "repair_action": "openevo_can_reconfigure",
        "details": {},
        "logs_ref": None,
    }


def test_sidecar_backend_facade_preserves_typed_backend_error() -> None:
    class ErrorBackendClient:
        def status(self) -> dict[str, Any]:
            raise DesktopBackendError(
                503,
                {
                    "code": "backend_unavailable",
                    "message": "Remote backend is not reachable.",
                    "severity": "blocking",
                    "category": "service",
                    "retryable": True,
                    "repair_action": "openevo_can_retry",
                    "details": {},
                    "logs_ref": "services/openevo-backend",
                },
            )

    client = TestClient(create_sidecar_app(backend_client_factory=ErrorBackendClient))
    token = client.get("/openevo-api/desktop/shell").json()["sidecar"]["mutation_token"]
    response = client.get(
        "/openevo-api/backend/status",
        headers={"X-OpenEvo-Sidecar-Token": token},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "backend_unavailable"
    assert response.json()["repair_action"] == "openevo_can_retry"
