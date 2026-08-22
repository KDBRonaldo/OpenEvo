from __future__ import annotations

import subprocess
import sys
import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from scripts.dev.development_agent_web_layer import (
    DevelopmentDaemonClient,
    DevelopmentAgentDesktopV2Provider,
    EVENT_SCHEMA_SHA256,
    OPENAPI_SHA256,
    create_development_agent_web_app,
)


class FakeDaemonClient:
    def __init__(self) -> None:
        self.state_requests = 0
        self.state = {
            "schema_version": "1",
            "active_project_id": None,
            "projects": [],
            "sessions": [],
            "artifacts": [],
            "evolution_jobs": [],
            "evolution_runs": [],
            "workspaces": [],
        }

    def request(self, path: str, *, method: str = "GET", body: object | None = None) -> object:
        del method, body
        if path == "/state":
            self.state_requests += 1
            return self.state
        if path == "/capabilities":
            return {
                "schema_version": "1",
                "authority": "development_catalog_unverified",
                "capabilities": {
                    "schema_version": "1",
                    "core_version": "development",
                    "registry_digest": "a" * 64,
                    "evaluated_profile": {
                        "execution_mode": "subscription",
                        "capture_mode": "transcript",
                        "harness_id": "codex",
                        "harness_capabilities": [],
                        "runtime_capabilities": [],
                    },
                    "targets": [],
                },
            }
        raise AssertionError(path)


def _config() -> dict[str, object]:
    return {
        "schema_version": "2",
        "task": {"title": "Test task", "objective": "Return a concise result."},
        "workspace": {"kind": "scratch", "display_name": "Scratch"},
        "execution": {
            "mode": "codex_subscription_transcript",
            "capture_mode": "transcript",
            "token_level_metrics_available": False,
            "harness_id": "codex",
            "codex_model": "gpt-5.5",
            "reasoning_effort": "high",
            "token_limit": 4096,
            "task_network_allow_internet": False,
        },
        "evolution": {"targets": {}},
    }


def test_provider_exposes_only_honest_development_features() -> None:
    version = DevelopmentAgentDesktopV2Provider(
        FakeDaemonClient(), source_commit="a" * 40
    ).invoke("getDesktopContractVersionV2", {})

    assert version.build_channel == "development"
    assert version.openapi_sha256 == OPENAPI_SHA256
    assert version.event_schema_sha256 == EVENT_SCHEMA_SHA256
    assert version.feature_flags == [
        "development_agent_bridge_v2",
        "mutation_idempotency_v2",
    ]
    assert "daemon_bundle_v2" not in version.feature_flags


def test_daemon_client_accepts_bounded_aggregate_state_larger_than_one_mib(
    monkeypatch,
) -> None:
    payload = json.dumps({"schema_version": "1", "padding": "x" * 1_100_000}).encode()

    class Response:
        headers = {"Content-Length": str(len(payload))}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, limit: int) -> bytes:
            assert limit > len(payload)
            return payload

    monkeypatch.setattr(
        "scripts.dev.development_agent_web_layer.urllib.request.urlopen",
        lambda request, timeout: Response(),
    )

    result = DevelopmentDaemonClient("http://127.0.0.1:8765", "secret").request("/state")

    assert result["schema_version"] == "1"


def test_module_imports_when_launcher_is_started_from_desktop() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    scripts_root = repository_root / "scripts" / "dev"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                f"sys.path.insert(0, {str(scripts_root)!r}); "
                "import development_agent_web_layer"
            ),
        ],
        cwd=repository_root / "desktop",
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_http_layer_requires_exact_session_and_projects_empty_state() -> None:
    import scripts.dev.development_agent_web_layer as web

    fake = FakeDaemonClient()
    original = web.DevelopmentDaemonClient
    web.DevelopmentDaemonClient = lambda endpoint, token: fake  # type: ignore[assignment]
    try:
        app = create_development_agent_web_app(
            daemon_endpoint="http://127.0.0.1:8765",
            daemon_token="daemon-secret",
            session_token="desktop-secret",
            bootstrap_token="c" * 64,
            browser_endpoint="http://127.0.0.1:5173",
            source_commit="a" * 40,
        )
    finally:
        web.DevelopmentDaemonClient = original

    with TestClient(app) as client:
        assert client.get("/desktop/v2/projects").status_code == 401
        response = client.get(
            "/desktop/v2/projects",
            headers={"X-OpenEvo-Desktop-Session": "desktop-secret"},
        )
        assert response.status_code == 200
        assert response.json() == {
            "schema_version": "2",
            "items": [],
            "next_cursor": None,
            "has_more": False,
        }
        missing = client.get(
            "/desktop/v2/projects/missing",
            headers={"X-OpenEvo-Desktop-Session": "desktop-secret"},
        )
        assert missing.status_code == 404
        assert missing.json() == {
            "schema_version": "2",
            "code": "desktop_resource_not_found",
            "summary": "project not found",
            "retryable": False,
            "action": "none",
            "affected_resource_id": None,
        }


def test_self_hosted_layer_serves_the_existing_desktop_renderer() -> None:
    import scripts.dev.development_agent_web_layer as web

    fake = FakeDaemonClient()
    original = web.DevelopmentDaemonClient
    web.DevelopmentDaemonClient = lambda endpoint, token: fake  # type: ignore[assignment]
    try:
        app = create_development_agent_web_app(
            daemon_endpoint="http://127.0.0.1:8787",
            daemon_token="daemon-secret",
            session_token="desktop-secret",
            bootstrap_token="c" * 64,
            browser_endpoint="http://127.0.0.1:8765",
            source_commit="a" * 40,
            static_root=Path(__file__).resolve().parents[2]
            / "src"
            / "openevo"
            / "web_gateway"
            / "static",
        )
    finally:
        web.DevelopmentDaemonClient = original

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        page = client.get("/openevo")
        bootstrap = client.post(
            "/openevo-native/browser/bootstrap",
            headers={"Origin": "http://127.0.0.1:8765"},
            json={"schema_version": "2", "bootstrap_token": "c" * 64},
        )
        asset_paths = re.findall(r'(?:src|href)="(/assets/[^"]+)"', page.text)
        asset_statuses = [client.get(path).status_code for path in asset_paths]

    assert page.status_code == 200
    assert "<title>OpenEvo Desktop</title>" in page.text
    assert asset_paths
    assert asset_statuses == [200] * len(asset_paths)
    assert bootstrap.status_code == 200
    assert bootstrap.json()["endpoint"] == "http://127.0.0.1:8765"
    assert bootstrap.json()["session_token"] == "desktop-secret"


def test_provider_projects_persisted_project_and_task_into_closed_v2_models() -> None:
    fake = FakeDaemonClient()
    fake.state.update(
        {
            "active_project_id": "project-1",
            "projects": [
                {
                    "project_id": "project-1",
                    "display_name": "Development project",
                    "config": _config(),
                    "created_at": "2026-08-22T00:00:00Z",
                    "updated_at": "2026-08-22T00:00:00Z",
                }
            ],
            "sessions": [
                {
                    "session_id": "session-1",
                    "project_id": "project-1",
                    "state": "running",
                    "logs": ["started"],
                    "created_at": "2026-08-22T00:01:00Z",
                    "updated_at": "2026-08-22T00:01:00Z",
                }
            ],
        }
    )
    provider = DevelopmentAgentDesktopV2Provider(fake, source_commit="a" * 40)

    projects = provider.invoke("listDesktopProjectsV2", {})
    tasks = provider.invoke("listDesktopTasksV2", {"project_id": "project-1"})

    assert projects.items[0].active_project_head.project_id == "project-1"
    assert tasks.items[0].task_id == "session-1"
    assert tasks.items[0].state == "running"
    assert tasks.items[0].admission.predecessor_project_head.project_id == "project-1"


def test_initial_snapshot_projections_share_the_bounded_state_cache() -> None:
    fake = FakeDaemonClient()
    provider = DevelopmentAgentDesktopV2Provider(fake, source_commit="a" * 40)

    provider.invoke("getDesktopStateV2", {})
    provider.invoke("listRemoteWorkspaceProfilesV2", {})
    provider.invoke("listDesktopProjectsV2", {})
    provider.invoke("listDesktopTasksV2", {"project_id": None})

    assert fake.state_requests == 1


def test_state_and_profile_collection_publish_identical_authority() -> None:
    fake = FakeDaemonClient()
    provider = DevelopmentAgentDesktopV2Provider(fake, source_commit="a" * 40)

    state = provider.invoke("getDesktopStateV2", {})
    profiles = provider.invoke("listRemoteWorkspaceProfilesV2", {})

    assert state.profiles == profiles.items
    assert state.profiles[0].etag == profiles.items[0].etag
    assert state.profiles[0].updated_at == profiles.items[0].updated_at


def test_development_api_proxy_requires_local_token_and_forwards_to_daemon() -> None:
    import scripts.dev.development_agent_web_layer as web

    fake = FakeDaemonClient()
    calls: list[tuple[str, str, str, bytes, str | None]] = []

    def proxy(path: str, *, query: str, method: str, body: bytes, content_type: str | None):
        calls.append((path, query, method, body, content_type))
        return 200, b'{"schema_version":"1","status":"ok"}', {"Content-Type": "application/json"}

    fake.proxy = proxy  # type: ignore[attr-defined]
    original = web.DevelopmentDaemonClient
    web.DevelopmentDaemonClient = lambda endpoint, token: fake  # type: ignore[assignment]
    try:
        app = create_development_agent_web_app(
            daemon_endpoint="http://127.0.0.1:8765",
            daemon_token="daemon-secret",
            session_token="web-secret",
            bootstrap_token="c" * 64,
            browser_endpoint="http://127.0.0.1:5173",
            source_commit="a" * 40,
        )
    finally:
        web.DevelopmentDaemonClient = original

    with TestClient(app) as client:
        unauthorized = client.get("/openevo-dev-agent/v1/state")
        assert unauthorized.status_code == 401
        response = client.post(
            "/openevo-dev-agent/v1/sessions?poll=true",
            headers={
                "X-OpenEvo-Development-Web-Token": "web-secret",
                "Content-Type": "application/json",
            },
            content=b'{"schema_version":"1"}',
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert calls == [
        ("sessions", "poll=true", "POST", b'{"schema_version":"1"}', "application/json")
    ]
