from __future__ import annotations

from fastapi.testclient import TestClient

from openevo.backend.api import BackendHTTPError, create_backend_app
from openevo.backend.models import BackendError


def _client(*, raise_server_exceptions: bool = True) -> TestClient:
    app = create_backend_app()

    @app.get("/_test/http-error")
    def _test_http_error() -> None:
        raise BackendHTTPError(
            409,
            BackendError(
                code="service_conflict",
                message="Service conflict.",
                severity="blocking",
                category="service",
                retryable=True,
                repair_action="openevo_can_retry",
            ),
        )

    @app.get("/_test/unhandled-error")
    def _test_unhandled_error() -> None:
        raise RuntimeError("boom")

    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def test_backend_health() -> None:
    client = _client()
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_backend_capabilities() -> None:
    client = _client()
    response = client.get("/capabilities")
    assert response.status_code == 200
    method_ids = {item["method_id"] for item in response.json()["evolution_methods"]}
    assert "text_memory_reflector" in method_ids
    assert "skill_bundle_reflector" in method_ids


def test_backend_route_surface_is_present() -> None:
    routes = {
        (route.path, next(iter(route.methods - {"HEAD", "OPTIONS"}), None))
        for route in create_backend_app().routes
        if hasattr(route, "methods")
    }
    assert ("/status", "GET") in routes
    assert ("/environment", "GET") in routes
    assert ("/environment/doctor", "POST") in routes
    assert ("/environment/repair", "POST") in routes
    assert ("/projects", "POST") in routes
    assert ("/projects", "GET") in routes
    assert ("/projects/{project_id}", "GET") in routes
    assert ("/projects/{project_id}", "PATCH") in routes
    assert ("/runs", "POST") in routes
    assert ("/runs", "GET") in routes
    assert ("/runs/{run_id}", "GET") in routes
    assert ("/runs/{run_id}/cancel", "POST") in routes
    assert ("/runs/{run_id}/retry", "POST") in routes
    assert ("/runs/{run_id}/timeline", "GET") in routes
    assert ("/runs/{run_id}/logs", "GET") in routes
    assert ("/runs/{run_id}/artifacts", "GET") in routes
    assert ("/artifacts/{artifact_id}", "GET") in routes
    assert ("/artifacts/{artifact_id}/content", "GET") in routes
    assert ("/artifacts/{artifact_id}/diff", "GET") in routes
    assert ("/services", "GET") in routes
    assert ("/services/{service_id}/logs", "GET") in routes
    assert ("/services/{service_id}/restart", "POST") in routes
    assert ("/services/{service_id}/stop", "POST") in routes
    assert ("/capabilities", "GET") in routes


def test_backend_project_run_artifact_flow() -> None:
    client = _client()

    project = client.post(
        "/projects",
        json={"name": "science demo", "workspace_root": "/srv/openevo/workspaces/demo"},
    )
    assert project.status_code == 200
    project_id = project.json()["id"]

    run = client.post(
        "/runs",
        json={"project_id": project_id, "execution_mode": "codex_subscription_transcript"},
    )
    assert run.status_code == 200
    run_id = run.json()["id"]

    timeline = client.get(f"/runs/{run_id}/timeline")
    assert timeline.status_code == 200
    assert timeline.json()[0]["phase"] == "created"

    artifacts = client.get(f"/runs/{run_id}/artifacts")
    assert artifacts.status_code == 200
    artifact_id = artifacts.json()[0]["id"]

    content = client.get(f"/artifacts/{artifact_id}/content")
    assert content.status_code == 200
    assert content.json()["artifact_type"] in {"text_memory", "skill_bundle", "agent_system"}


def test_backend_accepts_advertised_capability_execution_modes() -> None:
    client = _client()
    project = client.post(
        "/projects",
        json={"name": "science demo", "workspace_root": "/srv/openevo/workspaces/demo"},
    )
    assert project.status_code == 200
    project_id = project.json()["id"]

    modes = [mode["mode"] for mode in client.get("/capabilities").json()["execution_modes"]]
    assert modes
    for mode in modes:
        response = client.post(
            "/runs",
            json={"project_id": project_id, "execution_mode": mode},
        )
        assert response.status_code == 200
        assert response.json()["execution_mode"] == mode


def test_backend_typed_error_model() -> None:
    client = _client()
    response = client.get("/projects/missing-project")
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "project_not_found"
    assert body["severity"] == "blocking"
    assert body["category"] == "project"
    assert body["retryable"] is False
    assert body["repair_action"] == "user_action_required"


def test_backend_validation_errors_use_typed_error_model() -> None:
    client = _client()
    response = client.post("/runs", json={})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "request_validation_error"
    assert body["severity"] == "blocking"
    assert body["category"] == "internal"
    assert body["retryable"] is False
    assert body["repair_action"] == "openevo_can_reconfigure"
    assert "errors" in body["details"]


def test_backend_http_errors_use_typed_error_model() -> None:
    client = _client()
    response = client.get("/_test/http-error")
    assert response.status_code == 409
    assert response.json()["code"] == "service_conflict"
    assert response.json()["repair_action"] == "openevo_can_retry"


def test_default_http_errors_use_typed_error_model() -> None:
    client = _client()
    not_found = client.get("/missing-route")
    assert not_found.status_code == 404
    assert not_found.json()["code"] == "http_error"
    assert not_found.json()["severity"] == "blocking"
    assert not_found.json()["category"] == "internal"
    assert not_found.json()["repair_action"] == "user_action_required"

    method_not_allowed = client.delete("/projects")
    assert method_not_allowed.status_code == 405
    assert method_not_allowed.json()["code"] == "http_error"
    assert method_not_allowed.json()["severity"] == "blocking"
    assert method_not_allowed.json()["category"] == "internal"


def test_backend_unhandled_errors_use_typed_error_model() -> None:
    client = _client(raise_server_exceptions=False)
    response = client.get("/_test/unhandled-error")
    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "internal_server_error"
    assert body["severity"] == "blocking"
    assert body["category"] == "internal"
    assert body["retryable"] is True
    assert body["repair_action"] == "openevo_can_retry"


def test_environment_doctor_and_repair_contract() -> None:
    client = _client()
    doctor = client.post("/environment/doctor", json={"repair": False})
    assert doctor.status_code == 200
    assert doctor.json()["checks"][0]["category"] in {"python", "docker", "codex", "network"}

    repair = client.post("/environment/repair", json={"actions": ["clear_stale_state"]})
    assert repair.status_code == 200
    assert repair.json()["status"] in {"ok", "needs_user_action"}
