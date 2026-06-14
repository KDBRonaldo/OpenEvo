from __future__ import annotations

import inspect
import json
from pathlib import Path

from fastapi.testclient import TestClient
import httpx

from polar_evolution.server import create_app
from polar_evolution.worker import EvolutionWorkerClient


def test_health_reports_artifact_root(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    with TestClient(app) as client:
        response = client.get("/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "db": "ok",
        "artifact_root": str(tmp_path / "artifacts"),
    }


def test_post_event_ingests_once(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    payload = {
        "source": "polar",
        "event_type": "polar.session_completed",
        "source_event_id": "session:abc",
        "task_id": "task_1",
        "session_id": "abc",
        "payload": {"session_result": {"session_id": "abc"}},
    }

    with TestClient(app) as client:
        first_response = client.post("/v1/events", json=payload)
        second_response = client.post("/v1/events", json=payload)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    first = first_response.json()
    second = second_response.json()
    assert first["ingested"] is True
    assert first["duplicate"] is False
    assert second["event_id"] == first["event_id"]
    assert second["ingested"] is False
    assert second["duplicate"] is True


def test_event_ingest_route_uses_sync_handler(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")

    route = next(route for route in app.routes if getattr(route, "path", None) == "/v1/events")

    assert inspect.iscoroutinefunction(route.endpoint) is False


def test_register_artifact_route(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")

    with TestClient(app) as client:
        response = client.post(
            "/v1/artifacts",
            json={
                "type": "parametric_memory",
                "name": "pmem_calc",
                "uri": "file:///tmp/adapter",
                "manifest": {"base_model": "Qwen/Qwen3.6-27B", "adapter_format": "lora"},
                "compatibility": {"base_model": "Qwen/Qwen3.6-27B"},
                "scores": {"heldout_reward_delta": 0.08},
                "tags": ["calculator"],
                "promoted": True,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["artifact_id"].startswith("art_")
    assert body["type"] == "parametric_memory"
    assert body["name"] == "pmem_calc"
    assert body["version"] == 1
    assert body["state"] == "active"
    assert body["manifest"]["adapter_format"] == "lora"
    assert body["compatibility"] == {"base_model": "Qwen/Qwen3.6-27B"}
    assert body["scores"] == {"heldout_reward_delta": 0.08}
    assert body["tags"] == ["calculator"]
    assert body["promoted"] is True

    with app.state.store.connect() as conn:
        row = conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ?",
            (body["artifact_id"],),
        ).fetchone()

    assert row is not None
    assert row["type"] == "parametric_memory"
    assert row["name"] == "pmem_calc"
    assert json.loads(row["compatibility_json"]) == {"base_model": "Qwen/Qwen3.6-27B"}
    assert json.loads(row["scores_json"]) == {"heldout_reward_delta": 0.08}
    assert json.loads(row["tags_json"]) == ["calculator"]
    manifest_path = Path(row["manifest_path"])
    assert manifest_path == (
        tmp_path
        / "artifacts"
        / "artifacts"
        / "parametric_memory"
        / body["artifact_id"]
        / "manifest.json"
    ).resolve()
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["manifest"] == {
        "base_model": "Qwen/Qwen3.6-27B",
        "adapter_format": "lora",
    }


def test_register_artifact_route_uses_sync_handler(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")

    route = next(route for route in app.routes if getattr(route, "path", None) == "/v1/artifacts")

    assert inspect.iscoroutinefunction(route.endpoint) is False


def test_register_artifact_route_rejects_non_finite_metadata_without_writes(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/v1/artifacts",
            content=b"""
            {
                "type": "text_memory",
                "name": "invalid api artifact",
                "uri": "file:///tmp/invalid-api.md",
                "manifest": {"quality": NaN}
            }
            """,
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 422
    with app.state.store.connect() as conn:
        artifact_count = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]

    assert artifact_count == 0
    assert not list((tmp_path / "artifacts" / "artifacts" / "text_memory").glob("*/manifest.json"))


def test_create_dataset_route(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")

    with TestClient(app) as client:
        event_response = client.post(
            "/v1/events",
            json={
                "source": "polar",
                "event_type": "polar.session_completed",
                "source_event_id": "session:dataset-route",
                "status": "COMPLETED",
                "reward": 1.0,
                "policy_version": "policy_route",
                "payload": {
                    "session_result": {
                        "trajectory": {"traces": [{"reward": 1.0}, {"reward": 0.5}]}
                    }
                },
            },
        )
        dataset_response = client.post(
            "/v1/datasets",
            json={
                "name": "route_dataset",
                "purpose": "skill_distillation",
                "query": {
                    "event_types": ["polar.session_completed"],
                    "status": ["COMPLETED"],
                    "reward_min": 0.8,
                    "policy_version": "policy_route",
                },
            },
        )

    assert event_response.status_code == 200
    assert dataset_response.status_code == 200
    body = dataset_response.json()
    assert body["dataset_id"].startswith("ds_")
    assert body["artifact_id"].startswith("art_")
    assert body["event_count"] == 1
    assert body["trace_count"] == 2

    with app.state.store.connect() as conn:
        dataset_row = conn.execute(
            "SELECT * FROM datasets WHERE dataset_id = ?",
            (body["dataset_id"],),
        ).fetchone()
        artifact_row = conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ?",
            (body["artifact_id"],),
        ).fetchone()

    assert dataset_row is not None
    assert dataset_row["name"] == "route_dataset"
    assert dataset_row["artifact_id"] == body["artifact_id"]
    assert artifact_row is not None
    assert artifact_row["type"] == "dataset"
    assert artifact_row["uri"] == Path(dataset_row["manifest_path"]).as_uri()


def test_create_dataset_route_rejects_task_tags_without_writes(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")

    with TestClient(app) as client:
        event_response = client.post(
            "/v1/events",
            json={
                "source": "polar",
                "event_type": "polar.session_completed",
                "source_event_id": "session:dataset-task-tags",
                "status": "COMPLETED",
                "payload": {"session_result": {"trajectory": {"traces": [{"reward": 1.0}]}}},
            },
        )
        dataset_response = client.post(
            "/v1/datasets",
            json={
                "name": "tagged_route_dataset",
                "purpose": "skill_distillation",
                "query": {"task_tags": ["calculator"]},
            },
        )

    assert event_response.status_code == 200
    assert dataset_response.status_code == 422
    assert "task_tags" in dataset_response.json()["detail"]

    with app.state.store.connect() as conn:
        dataset_count = conn.execute("SELECT COUNT(*) FROM datasets").fetchone()[0]
        dataset_event_count = conn.execute("SELECT COUNT(*) FROM dataset_events").fetchone()[0]
        artifact_count = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]

    assert dataset_count == 0
    assert dataset_event_count == 0
    assert artifact_count == 0
    assert not list((tmp_path / "artifacts" / "datasets").glob("*/manifest.json"))
    assert not list((tmp_path / "artifacts" / "artifacts" / "datasets").glob("*/manifest.json"))


def test_create_dataset_route_uses_sync_handler(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")

    route = next(route for route in app.routes if getattr(route, "path", None) == "/v1/datasets")

    assert inspect.iscoroutinefunction(route.endpoint) is False


def test_job_route_claim_heartbeat_and_complete(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")

    with TestClient(app) as client:
        create_response = client.post(
            "/v1/jobs",
            json={"method": "mock", "job_type": "text_memory_mining"},
        )
        job_id = create_response.json()["job_id"]
        claim_response = client.post(
            "/v1/jobs/claim",
            json={"worker_id": "worker_1", "capabilities": ["text_memory_mining"]},
        )
        lease_id = claim_response.json()["job"]["lease_id"]
        heartbeat_response = client.post(
            f"/v1/jobs/{job_id}/heartbeat",
            json={"lease_id": lease_id, "progress": 0.5, "message": "halfway"},
        )
        complete_response = client.post(
            f"/v1/jobs/{job_id}/complete",
            json={
                "lease_id": lease_id,
                "artifacts": [
                    {
                        "type": "text_memory",
                        "name": "route memory",
                        "uri": "file:///tmp/route-memory.md",
                    }
                ],
            },
        )

    assert create_response.status_code == 200
    assert claim_response.status_code == 200
    assert heartbeat_response.status_code == 200
    assert heartbeat_response.json()["state"] == "running"
    assert complete_response.status_code == 200
    body = complete_response.json()
    assert body["state"] == "succeeded"
    assert body["artifact_ids"][0].startswith("art_")

    with app.state.store.connect() as conn:
        job_row = conn.execute(
            "SELECT state FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        artifact_count = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]

    assert job_row["state"] == "succeeded"
    assert artifact_count == 1


def test_job_route_invalid_lease_returns_422(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")

    with TestClient(app) as client:
        create_response = client.post(
            "/v1/jobs",
            json={"method": "mock", "job_type": "text_memory_mining"},
        )
        job_id = create_response.json()["job_id"]
        client.post(
            "/v1/jobs/claim",
            json={"worker_id": "worker_1", "capabilities": ["text_memory_mining"]},
        )
        fail_response = client.post(
            f"/v1/jobs/{job_id}/fail",
            json={"lease_id": "lease_wrong", "error": "wrong worker", "retryable": False},
        )

    assert fail_response.status_code == 422
    assert "invalid lease" in fail_response.json()["detail"]
    with app.state.store.connect() as conn:
        job_row = conn.execute(
            "SELECT state, error FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()

    assert job_row["state"] == "claimed"
    assert job_row["error"] is None


def test_job_route_invalid_completion_marks_failed_without_artifacts(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")

    with TestClient(app, raise_server_exceptions=False) as client:
        create_response = client.post(
            "/v1/jobs",
            json={"method": "mock", "job_type": "text_memory_mining"},
        )
        job_id = create_response.json()["job_id"]
        claim_response = client.post(
            "/v1/jobs/claim",
            json={"worker_id": "worker_1", "capabilities": ["text_memory_mining"]},
        )
        lease_id = claim_response.json()["job"]["lease_id"]
        complete_response = client.post(
            f"/v1/jobs/{job_id}/complete",
            content=f"""
            {{
                "lease_id": "{lease_id}",
                "artifacts": [
                    {{
                        "type": "text_memory",
                        "name": "invalid route memory",
                        "uri": "file:///tmp/invalid-route-memory.md",
                        "scores": {{"quality": NaN}}
                    }}
                ]
            }}
            """.encode(),
            headers={"content-type": "application/json"},
        )

    assert complete_response.status_code == 422
    assert "non-finite float" in complete_response.json()["detail"]
    with app.state.store.connect() as conn:
        job_row = conn.execute(
            "SELECT state, error FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        artifact_count = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]

    assert job_row["state"] == "failed"
    assert "non-finite float" in job_row["error"]
    assert artifact_count == 0
    assert not list((tmp_path / "artifacts" / "artifacts" / "text_memory").glob("*/manifest.json"))


def test_job_routes_use_sync_handlers(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")

    for path in (
        "/v1/jobs",
        "/v1/jobs/claim",
        "/v1/jobs/{job_id}/heartbeat",
        "/v1/jobs/{job_id}/complete",
        "/v1/jobs/{job_id}/fail",
    ):
        route = next(route for route in app.routes if getattr(route, "path", None) == path)
        assert inspect.iscoroutinefunction(route.endpoint) is False


def test_worker_client_posts_job_protocol_methods():
    requests: list[tuple[str, str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        requests.append((request.method, request.url.path, payload))
        if request.url.path == "/v1/jobs/claim":
            return httpx.Response(
                200,
                json={
                    "job": {
                        "job_id": "job_1",
                        "lease_id": "lease_1",
                        "job_type": "text_memory_mining",
                        "method": "mock",
                        "input_artifacts": [],
                        "config": {},
                    }
                },
            )
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    with EvolutionWorkerClient("http://evolution.test", transport=transport) as worker:
        claim = worker.claim(
            "worker_1",
            ["text_memory_mining"],
            lease_seconds=30,
        )
        heartbeat = worker.heartbeat("job_1", "lease_1", progress=0.5, message="halfway")
        complete = worker.complete(
            "job_1",
            "lease_1",
            artifacts=[
                {
                    "type": "text_memory",
                    "name": "client memory",
                    "uri": "file:///tmp/client-memory.md",
                }
            ],
            report={"ok": True},
        )
        failed = worker.fail("job_2", "lease_2", "worker command failed", retryable=False)

    assert claim is not None
    assert claim["job_id"] == "job_1"
    assert heartbeat == {"ok": True}
    assert complete == {"ok": True}
    assert failed == {"ok": True}
    assert requests == [
        (
            "POST",
            "/v1/jobs/claim",
            {
                "worker_id": "worker_1",
                "capabilities": ["text_memory_mining"],
                "lease_seconds": 30,
            },
        ),
        (
            "POST",
            "/v1/jobs/job_1/heartbeat",
            {"lease_id": "lease_1", "progress": 0.5, "message": "halfway"},
        ),
        (
            "POST",
            "/v1/jobs/job_1/complete",
            {
                "lease_id": "lease_1",
                "artifacts": [
                    {
                        "type": "text_memory",
                        "name": "client memory",
                        "uri": "file:///tmp/client-memory.md",
                    }
                ],
                "report": {"ok": True},
            },
        ),
        (
            "POST",
            "/v1/jobs/job_2/fail",
            {"lease_id": "lease_2", "error": "worker command failed", "retryable": False},
        ),
    ]
