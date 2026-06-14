from __future__ import annotations

import inspect
import json
from pathlib import Path

from fastapi.testclient import TestClient

from polar_evolution.server import create_app


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
