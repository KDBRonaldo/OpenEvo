from __future__ import annotations

import asyncio
import hashlib
import subprocess
import sys
import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from scripts.dev.development_agent_web_layer import (
    DevelopmentDaemonClient,
    DevelopmentDaemonEventCursorExpired,
    DevelopmentAgentDesktopV2Provider,
    DevelopmentAgentStateEventRelay,
    EVENT_SCHEMA_SHA256,
    OPENAPI_SHA256,
    create_development_agent_web_app,
)
from desktop.sidecar.event_broker_v2 import DesktopEventBrokerV2


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
        self.events: list[dict[str, object]] = []
        self.expire_next_event_cursor = False
        self.workspace_files: dict[str, bytes] = {}

    def emit_event(self, project_id: str) -> None:
        sequence = len(self.events) + 1
        self.events.append(
            {
                "sequence": sequence,
                "event_id": f"development-event-{sequence}",
                "project_id": project_id,
                "event_type": "state_changed",
                "payload_sha256": f"{sequence:064x}",
                "occurred_at": "2026-08-23T00:00:00Z",
            }
        )

    def request(self, path: str, *, method: str = "GET", body: object | None = None) -> object:
        del method, body
        if path == "/state":
            self.state_requests += 1
            return self.state
        if path.startswith("/events?"):
            query = parse_qs(urlsplit(path).query)
            if "after" not in query:
                return {
                    "schema_version": "1",
                    "events": [],
                    "latest_sequence": len(self.events),
                    "has_more": False,
                }
            if self.expire_next_event_cursor:
                self.expire_next_event_cursor = False
                raise DevelopmentDaemonEventCursorExpired
            after = int(query["after"][0])
            if int(query.get("wait_ms", ["0"])[0]):
                time.sleep(0.01)
            events = [event for event in self.events if event["sequence"] > after]
            return {
                "schema_version": "1",
                "events": events[:100],
                "latest_sequence": len(self.events),
                "has_more": len(events) > 100,
            }
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

    def request_v2(self, path: str, *, method: str = "GET", body: object | None = None) -> object:
        del method, body
        parsed = urlsplit(path)
        if parsed.path == "/tasks":
            items = []
            for session in self.state["sessions"]:
                state = "closed" if session["state"] == "completed" else session["state"]
                items.append({
                    "schema_version": "2",
                    "task_id": session["session_id"],
                    "project_id": session["project_id"],
                    "state": state,
                    "created_at": session["created_at"],
                    "updated_at": session["updated_at"],
                })
            return {
                "schema_version": "2",
                "items": items,
                "next_cursor": None,
                "has_more": False,
            }
        task_logs = re.fullmatch(r"/tasks/([^/]+)/logs", parsed.path)
        if task_logs:
            session = next(
                item for item in self.state["sessions"]
                if item["session_id"] == task_logs.group(1)
            )
            messages = [
                *[("system", message) for message in session.get("logs", [])],
                *([("transcript", session["response"])] if session.get("response") else []),
                *([("system", session["error"])] if session.get("error") else []),
            ]
            query = parse_qs(parsed.query)
            after = int(query.get("after", ["0"])[0])
            limit = int(query.get("limit", ["100"])[0])
            all_items = [{
                "sequence": index + 1,
                "occurred_at": session["updated_at"],
                "stream": stream,
                "message": message,
            } for index, (stream, message) in enumerate(messages)]
            remaining = [item for item in all_items if item["sequence"] > after]
            items = remaining[:limit]
            has_more = len(remaining) > limit
            return {
                "schema_version": "2",
                "items": items,
                "next_cursor": str(items[-1]["sequence"]) if has_more else None,
                "has_more": has_more,
            }
        task_timeline = re.fullmatch(r"/tasks/([^/]+)/timeline", parsed.path)
        if task_timeline:
            session = next(
                item for item in self.state["sessions"]
                if item["session_id"] == task_timeline.group(1)
            )
            events = [{
                "schema_version": "2",
                "event_id": f"{session['session_id']}-event-admitted",
                "sequence": 1,
                "occurred_at": session["created_at"],
                "project_id": session["project_id"],
                "task_id": session["session_id"],
                "event_type": "task_admitted",
            }, {
                "schema_version": "2",
                "event_id": f"{session['session_id']}-event-attempt",
                "sequence": 2,
                "occurred_at": session["created_at"],
                "project_id": session["project_id"],
                "task_id": session["session_id"],
                "event_type": "attempt_appended",
            }]
            query = parse_qs(parsed.query)
            after = int(query.get("after", ["0"])[0])
            limit = int(query.get("limit", ["100"])[0])
            remaining = [event for event in events if event["sequence"] > after]
            items = remaining[:limit]
            has_more = len(remaining) > limit
            return {
                "schema_version": "2",
                "items": items,
                "next_cursor": str(items[-1]["sequence"]) if has_more else None,
                "has_more": has_more,
            }
        raise AssertionError(path)

    def proxy_v2(
        self,
        path: str,
        *,
        query: str,
        method: str,
        body: bytes,
        content_type: str | None,
        content_sha256: str | None = None,
    ) -> tuple[int, bytes, dict[str, str]]:
        del content_type
        parameters = parse_qs(query)
        if path.endswith("/workspace") and method == "GET":
            entries = [
                {
                    "schema_version": "2",
                    "path": name,
                    "kind": "file",
                    "byte_size": len(payload),
                    "content_sha256": hashlib.sha256(payload).hexdigest(),
                    "media_type": "text/plain",
                    "content": payload.decode(),
                    "modified_at": "2026-08-23T00:00:00Z",
                }
                for name, payload in sorted(self.workspace_files.items())
            ]
            authority = hashlib.sha256(
                json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            payload = {
                "schema_version": "2",
                "project_id": path.split("/")[1],
                "manifest_sha256": authority,
                "items": entries,
                "next_cursor": None,
                "has_more": False,
                "truncated": False,
            }
            return 200, json.dumps(payload).encode(), {"Content-Type": "application/json"}
        if path.endswith("/workspace/files"):
            relative_path = parameters["path"][0]
            project_id = path.split("/")[1]
            if method == "PUT":
                assert content_sha256 == hashlib.sha256(body).hexdigest()
                self.workspace_files[relative_path] = body
                entry = {
                    "schema_version": "2",
                    "path": relative_path,
                    "kind": "file",
                    "byte_size": len(body),
                    "content_sha256": content_sha256,
                    "media_type": "text/plain",
                    "content": body.decode(),
                    "modified_at": "2026-08-23T00:00:00Z",
                }
                result = {
                    "schema_version": "2",
                    "project_id": project_id,
                    "manifest_sha256": "a" * 64,
                    "entry": entry,
                }
                return 201, json.dumps(result).encode(), {"Content-Type": "application/json"}
            if method == "GET":
                payload = self.workspace_files[relative_path]
                return 200, payload, {
                    "Content-Type": "text/plain",
                    "X-OpenEvo-Content-SHA256": hashlib.sha256(payload).hexdigest(),
                }
            if method == "DELETE":
                del self.workspace_files[relative_path]
                result = {
                    "schema_version": "2",
                    "project_id": project_id,
                    "manifest_sha256": "b" * 64,
                    "deleted_path": relative_path,
                }
                return 200, json.dumps(result).encode(), {"Content-Type": "application/json"}
        raise AssertionError((method, path, query))


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
        "event_replay_v2",
        "mutation_idempotency_v2",
    ]
    assert "daemon_bundle_v2" not in version.feature_flags


def _event_from_sse(frame: bytes) -> dict[str, object]:
    data_line = next(
        line for line in frame.decode("utf-8").splitlines() if line.startswith("data: ")
    )
    return json.loads(data_line.removeprefix("data: "))


def test_state_event_relay_publishes_changes_and_replays_after_disconnect() -> None:
    fake = FakeDaemonClient()
    fake.state["active_project_id"] = "project-1"
    broker = DesktopEventBrokerV2(
        max_events=8,
        max_subscriber_events=8,
        heartbeat_interval=1,
        poll_interval=0.001,
        event_id_factory=iter(("event-one", "event-two")).__next__,
    )
    provider = DevelopmentAgentDesktopV2Provider(
        fake,
        source_commit="a" * 40,
        event_broker=broker,
    )
    relay = DevelopmentAgentStateEventRelay(
        client=fake,
        provider=provider,
        broker=broker,
    )

    assert relay.poll_once() is False
    first_subscription = broker.subscribe()
    fake.state["sessions"] = [{"session_id": "session-1", "status": "running"}]
    fake.emit_event("project-1")
    assert relay.poll_once(wait_milliseconds=0) is True
    first_event = _event_from_sse(asyncio.run(anext(first_subscription)))
    asyncio.run(first_subscription.aclose())

    fake.state["sessions"] = [{"session_id": "session-1", "status": "closed"}]
    fake.emit_event("project-1")
    assert relay.poll_once(wait_milliseconds=0) is True
    replay_subscription = broker.subscribe(str(first_event["event_id"]))
    replayed_event = _event_from_sse(asyncio.run(anext(replay_subscription)))
    asyncio.run(replay_subscription.aclose())

    assert first_event["event_type"] == "core_authority_changed"
    assert first_event["payload"]["core_event_sequence"] == 1
    assert replayed_event["event_type"] == "core_authority_changed"
    assert replayed_event["payload"]["core_event_sequence"] == 2
    assert replayed_event["event_id"] != first_event["event_id"]
    assert relay.poll_once(wait_milliseconds=0) is False
    broker.close()


def test_state_event_relay_resynchronizes_from_daemon_after_cursor_expiry() -> None:
    fake = FakeDaemonClient()
    fake.state["active_project_id"] = "project-1"
    broker = DesktopEventBrokerV2(
        max_events=8,
        max_subscriber_events=8,
        heartbeat_interval=1,
        poll_interval=0.001,
        event_id_factory=lambda: "resync-event",
    )
    provider = DevelopmentAgentDesktopV2Provider(
        fake,
        source_commit="a" * 40,
        event_broker=broker,
    )
    relay = DevelopmentAgentStateEventRelay(
        client=fake,
        provider=provider,
        broker=broker,
    )

    assert relay.poll_once(wait_milliseconds=0) is False
    fake.state["sessions"] = [{"session_id": "session-1", "status": "closed"}]
    fake.emit_event("project-1")
    fake.expire_next_event_cursor = True
    subscription = broker.subscribe()
    assert relay.poll_once(wait_milliseconds=0) is True
    event = _event_from_sse(asyncio.run(anext(subscription)))
    asyncio.run(subscription.aclose())

    assert event["payload"]["core_event_id"].startswith("development-resync-")
    assert event["payload"]["core_event_sequence"] == 1
    broker.close()


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


def test_http_layer_proxies_authenticated_workspace_v2_with_verified_bytes() -> None:
    import scripts.dev.development_agent_web_layer as web

    fake = FakeDaemonClient()
    fake.state.update({
        "active_project_id": "project-workspace-v2",
        "projects": [{
            "project_id": "project-workspace-v2",
            "display_name": "Workspace v2",
            "config": _config(),
            "created_at": "2026-08-23T00:00:00Z",
            "updated_at": "2026-08-23T00:00:00Z",
        }],
    })
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
        )
    finally:
        web.DevelopmentDaemonClient = original

    headers = {"X-OpenEvo-Desktop-Session": "desktop-secret"}
    root = "/desktop/v2/development/projects/project-workspace-v2/workspace"
    with TestClient(app) as client:
        assert client.get(root).status_code == 401
        initial = client.get(f"{root}?limit=100", headers=headers)
        assert initial.status_code == 200
        assert initial.json()["items"] == []

        created = client.put(
            f"{root}/files?path=notes%2Fanswer.txt&overwrite=false",
            headers={**headers, "Content-Type": "text/plain"},
            content=b"OpenEvo v2\n",
        )
        assert created.status_code == 201
        assert created.json()["entry"]["content_sha256"] == hashlib.sha256(
            b"OpenEvo v2\n"
        ).hexdigest()

        inventory = client.get(f"{root}?limit=100", headers=headers)
        assert [entry["path"] for entry in inventory.json()["items"]] == [
            "notes/answer.txt"
        ]
        downloaded = client.get(
            f"{root}/files?path=notes%2Fanswer.txt", headers=headers
        )
        assert downloaded.content == b"OpenEvo v2\n"
        assert downloaded.headers["X-OpenEvo-Content-SHA256"] == hashlib.sha256(
            downloaded.content
        ).hexdigest()

        deleted = client.delete(
            f"{root}/files?path=notes%2Fanswer.txt", headers=headers
        )
        assert deleted.status_code == 200
        assert deleted.json()["deleted_path"] == "notes/answer.txt"


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


def test_provider_reads_terminal_agent_result_from_daemon_v2_logs() -> None:
    fake = FakeDaemonClient()
    fake.state.update(
        {
            "active_project_id": "project-1",
            "projects": [{
                "project_id": "project-1",
                "display_name": "Development project",
                "config": _config(),
                "created_at": "2026-08-22T00:00:00Z",
                "updated_at": "2026-08-22T00:00:00Z",
            }],
            "sessions": [{
                "session_id": "session-1",
                "project_id": "project-1",
                "state": "completed",
                "logs": ["completed"],
                "response": "Authoritative v2 answer.",
                "error": None,
                "created_at": "2026-08-22T00:01:00Z",
                "updated_at": "2026-08-22T00:02:00Z",
            }],
        }
    )
    provider = DevelopmentAgentDesktopV2Provider(fake, source_commit="a" * 40)
    provider.invoke("listDesktopTasksV2", {"project_id": "project-1"})

    logs = provider.invoke(
        "getDesktopTaskLogsV2",
        {"task_id": "session-1", "limit": 100, "after": None},
    )

    assert logs.items[-1].stream == "transcript"
    assert logs.items[-1].message == "Authoritative v2 answer."


def test_provider_projects_daemon_v2_timeline_into_bound_desktop_events() -> None:
    fake = FakeDaemonClient()
    fake.state.update(
        {
            "active_project_id": "project-1",
            "projects": [{
                "project_id": "project-1",
                "display_name": "Development project",
                "config": _config(),
                "created_at": "2026-08-22T00:00:00Z",
                "updated_at": "2026-08-22T00:00:00Z",
            }],
            "sessions": [{
                "session_id": "session-1",
                "project_id": "project-1",
                "state": "running",
                "logs": ["started"],
                "created_at": "2026-08-22T00:01:00Z",
                "updated_at": "2026-08-22T00:01:00Z",
            }],
        }
    )
    provider = DevelopmentAgentDesktopV2Provider(fake, source_commit="a" * 40)
    task = provider.invoke("getDesktopTaskV2", {"task_id": "session-1"})

    first = provider.invoke(
        "getDesktopTaskTimelineV2",
        {"task_id": "session-1", "limit": 1, "after": None},
    )
    second = provider.invoke(
        "getDesktopTaskTimelineV2",
        {"task_id": "session-1", "limit": 100, "after": first.next_cursor},
    )

    assert first.has_more is True
    assert first.next_cursor == "1"
    assert first.items[0].event_type == "task_admitted"
    assert first.items[0].admission == task.admission
    assert second.items[0].event_type == "attempt_appended"
    assert second.items[0].attempt == task.attempts[0]


def test_active_project_tunnel_exposes_only_its_bound_project() -> None:
    fake = FakeDaemonClient()
    fake.state.update(
        {
            "active_project_id": "project-1",
            "projects": [
                {
                    "project_id": project_id,
                    "display_name": project_id,
                    "config": _config(),
                    "created_at": "2026-08-22T00:00:00Z",
                    "updated_at": "2026-08-22T00:00:00Z",
                }
                for project_id in ("project-1", "project-2")
            ],
        }
    )
    provider = DevelopmentAgentDesktopV2Provider(fake, source_commit="a" * 40)

    projects = provider.invoke("listDesktopProjectsV2", {})

    assert [project.project_id for project in projects.items] == ["project-1"]


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
