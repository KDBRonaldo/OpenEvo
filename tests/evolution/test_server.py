from __future__ import annotations

from contextlib import contextmanager
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
import pytest

from openevo.evolution.client import EvolutionClient
from openevo.evolution.server import create_app
from openevo.evolution.worker import EvolutionWorkerClient
from openevo.internal_auth import InternalServiceIdentity
from openevo.runtime.base import RUNTIME_READBACK_MAX_BYTES


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


def test_materialized_blob_transport_rejects_oversize_before_read(
    tmp_path,
    monkeypatch,
) -> None:
    credential = "materialized-blob-transport-test-credential"
    server_identity = InternalServiceIdentity(
        service_id="evolution-backend",
        generation_digest="1" * 64,
        registry_digest="2" * 64,
        framework_lock_digest="3" * 64,
        credential=credential,
    )
    caller_identity = InternalServiceIdentity(
        service_id="gateway",
        generation_digest=server_identity.generation_digest,
        registry_digest=server_identity.registry_digest,
        framework_lock_digest=server_identity.framework_lock_digest,
        credential=credential,
    )
    app = create_app(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
        internal_identity=server_identity,
    )

    class UnreadableStream:
        def read(self, _size: int = -1) -> bytes:
            raise AssertionError("oversized materialized blob must not be read")

    @contextmanager
    def open_oversized_blob(_context_id: str, _blob_id: str):
        yield SimpleNamespace(
            blob=SimpleNamespace(
                size_bytes=RUNTIME_READBACK_MAX_BYTES + 1,
                sha256="4" * 64,
                media_type="application/octet-stream",
            ),
            stream=UnreadableStream(),
        )

    monkeypatch.setattr(app.state.store, "open_materialized_blob", open_oversized_blob)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/v1/internal/materialized-contexts/context-1/blobs/blob-1",
            headers=caller_identity.request_headers(),
        )

    assert response.status_code == 404
    assert response.json()["detail"] == (
        "materialized context blob exceeds the runtime transport budget"
    )


@pytest.mark.parametrize(
    ("method", "path", "core_status"),
    [
        ("GET", "/v1/internal/jobs/job-missing", 404),
        (
            "GET",
            "/v1/internal/successor-transitions/"
            "successor-missing/artifacts/artifact-missing",
            404,
        ),
        (
            "POST",
            "/v1/internal/successor-transitions/"
            "successor-missing/discard",
            200,
        ),
    ],
)
def test_core_control_routes_reject_other_authenticated_services(
    tmp_path,
    method: str,
    path: str,
    core_status: int,
) -> None:
    credential = "core-control-route-fence-test-credential"
    server_identity = InternalServiceIdentity(
        service_id="evolution-backend",
        generation_digest="1" * 64,
        registry_digest="2" * 64,
        framework_lock_digest="3" * 64,
        credential=credential,
    )
    core_identity = InternalServiceIdentity(
        service_id="core-control",
        generation_digest=server_identity.generation_digest,
        registry_digest=server_identity.registry_digest,
        framework_lock_digest=(
            server_identity.framework_lock_digest
        ),
        credential=credential,
    )
    app = create_app(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
        internal_identity=server_identity,
    )

    with TestClient(app) as client:
        wrong_caller = client.request(
            method,
            path,
            headers=server_identity.request_headers(),
        )
        core_caller = client.request(
            method,
            path,
            headers=core_identity.request_headers(),
        )

    assert wrong_caller.status_code == 403
    assert core_caller.status_code == core_status


def test_post_event_ingests_once(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    payload = {
        "source": "openevo",
        "event_type": "openevo.session_completed",
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
    assert (
        manifest_path
        == (
            tmp_path
            / "artifacts"
            / "artifacts"
            / "parametric_memory"
            / body["artifact_id"]
            / "manifest.json"
        ).resolve()
    )
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["manifest"] == {
        "base_model": "Qwen/Qwen3.6-27B",
        "adapter_format": "lora",
    }


def test_artifact_promotion_routes_update_backend_state(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")

    with TestClient(app) as client:
        registered = client.post(
            "/v1/artifacts",
            json={
                "type": "text_memory",
                "name": "pending memory",
                "uri": "file:///tmp/memory.md",
                "manifest": {"content_path": "memory.md"},
                "promoted": False,
            },
        ).json()
        artifact_id = registered["artifact_id"]

        initial = client.get(f"/v1/artifacts/{artifact_id}")
        empty_patch = client.patch(
            f"/v1/artifacts/{artifact_id}/promotion",
            json={},
        )
        after_empty_patch = client.get(f"/v1/artifacts/{artifact_id}")
        promoted = client.patch(
            f"/v1/artifacts/{artifact_id}/promotion",
            json={"promoted": True},
        )
        fetched = client.get(f"/v1/artifacts/{artifact_id}")

    assert initial.status_code == 200
    assert initial.json()["promoted"] is False
    assert empty_patch.status_code == 422
    assert after_empty_patch.status_code == 200
    assert after_empty_patch.json()["promoted"] is False
    assert promoted.status_code == 200
    assert promoted.json()["promoted"] is True
    assert fetched.status_code == 200
    assert fetched.json()["promoted"] is True


def test_human_feedback_route_rejects_boolean_score_and_confidence(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")

    with TestClient(app) as client:
        review = client.post(
            "/v1/reviews",
            json={
                "review_type": "promotion",
                "artifact_ids": ["art_a"],
                "packet": {"questions": ["Approve?"]},
            },
        ).json()
        claim = client.post(
            f"/v1/reviews/{review['review_id']}/claim",
            json={"reviewer_id": "alice"},
        )
        score_response = client.post(
            f"/v1/reviews/{review['review_id']}/feedback",
            json={"reviewer_id": "alice", "decision": "approve", "score": True},
        )
        confidence_response = client.post(
            f"/v1/reviews/{review['review_id']}/feedback",
            json={
                "reviewer_id": "alice",
                "decision": "approve",
                "confidence": False,
            },
        )
        listed = client.get(f"/v1/reviews/{review['review_id']}/feedback")

    assert claim.status_code == 200
    assert score_response.status_code == 422
    assert confidence_response.status_code == 422
    assert listed.status_code == 200
    assert listed.json() == []


def test_register_artifact_route_uses_sync_handler(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")

    route = next(route for route in app.routes if getattr(route, "path", None) == "/v1/artifacts")

    assert inspect.iscoroutinefunction(route.endpoint) is False


def test_context_resolve_route_uses_sync_handler_and_persists_context(tmp_path):
    artifact_root = tmp_path / "artifacts"
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=artifact_root)
    memory_file = artifact_root / "memory.md"
    memory_file.write_text("Prefer table-driven parser tests.", encoding="utf-8")

    with TestClient(app) as client:
        artifact_response = client.post(
            "/v1/artifacts",
            json={
                "type": "text_memory",
                "name": "route memory",
                "uri": memory_file.as_uri(),
                "compatibility": {"task_tags": "calculator", "agent_harness": "codex"},
                "scores": {"quality": 0.7},
                "tags": ["calculator"],
                "promoted": True,
            },
        )
        context_response = client.post(
            "/v1/contexts/resolve",
            json={
                "task_id": "task_route",
                "instruction": "fix parser",
                "agent": {"harness": "codex"},
                "metadata": {"task_tags": ["calculator"]},
            },
        )

    assert artifact_response.status_code == 200
    assert context_response.status_code == 200
    artifact_id = artifact_response.json()["artifact_id"]
    body = context_response.json()
    assert body["context_id"].startswith("ctx_")
    assert body["memory"]["artifact_ids"] == [artifact_id]
    assert "table-driven parser" in body["memory"]["rendered_text"]

    with app.state.store.connect() as conn:
        context_row = conn.execute(
            "SELECT * FROM contexts WHERE context_id = ?",
            (body["context_id"],),
        ).fetchone()

    assert context_row is not None
    assert json.loads(context_row["selected_artifact_ids_json"]) == [artifact_id]
    route = next(
        route for route in app.routes if getattr(route, "path", None) == "/v1/contexts/resolve"
    )
    assert inspect.iscoroutinefunction(route.endpoint) is False


@pytest.mark.asyncio
async def test_evolution_client_resolve_context_with_mock_transport():
    payload = {
        "task_id": "task_1",
        "instruction": "solve",
        "agent": {"harness": "codex"},
        "base_model": "Qwen/Qwen3.6-27B",
    }

    async def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/v1/contexts/resolve"
        assert json.loads(request.content) == payload
        return httpx.Response(
            200,
            json={
                "context_id": "ctx_test",
                "memory": {"artifact_ids": [], "rendered_text": ""},
                "skills": [],
                "adapter_merge_spec": {
                    "base_model": "Qwen/Qwen3.6-27B",
                    "merge_mode": "reference_only",
                    "adapters": [],
                },
                "selection": {},
            },
        )

    client = EvolutionClient(
        "http://evolution.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        context = await client.resolve_context(payload)
    finally:
        await client.close()

    assert context["context_id"] == "ctx_test"


@pytest.mark.asyncio
async def test_evolution_client_export_event_with_mock_transport():
    payload = {
        "source": "openevo",
        "event_type": "openevo.session_completed",
        "source_event_id": "session:client-test",
        "task_id": "task_1",
        "payload": {"reward": 1.0},
    }
    response_body = {"event_id": "evt_test", "ingested": True, "duplicate": False}

    async def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/v1/events"
        assert json.loads(request.content) == payload
        return httpx.Response(200, json=response_body)

    client = EvolutionClient(
        "http://evolution.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        event = await client.export_event(payload)
    finally:
        await client.close()

    assert event == response_body


@pytest.mark.asyncio
async def test_evolution_client_hitl_review_methods_with_mock_transport():
    review_payload = {
        "review_type": "promotion",
        "artifact_ids": ["art_a"],
        "packet": {"questions": ["Approve?"]},
    }
    feedback_payload = {
        "reviewer_id": "alice",
        "decision": "approve",
        "raw_payload": {"approved": True},
    }
    expectations = [
        (
            "POST",
            "/v1/reviews",
            "",
            review_payload,
            {"review_id": "rev_1", "status": "queued"},
        ),
        (
            "GET",
            "/v1/reviews/rev_1",
            "",
            None,
            {"review_id": "rev_1", "status": "queued"},
        ),
        (
            "GET",
            "/v1/reviews",
            "status=queued",
            None,
            [{"review_id": "rev_1", "status": "queued"}],
        ),
        (
            "GET",
            "/v1/review-packets/rpacket_1",
            "",
            None,
            {"packet_id": "rpacket_1", "packet": {"questions": ["Approve?"]}},
        ),
        (
            "GET",
            "/v1/review-packets",
            "",
            None,
            [{"packet_id": "rpacket_1", "packet": {"questions": ["Approve?"]}}],
        ),
        (
            "POST",
            "/v1/reviews/rev_1/claim",
            "",
            {"reviewer_id": "alice", "reviewer_role": "maintainer"},
            {"review_id": "rev_1", "status": "in_review"},
        ),
        (
            "POST",
            "/v1/reviews/rev_1/feedback",
            "",
            feedback_payload,
            {"feedback_id": "hfb_1", "status": "available_for_evolution"},
        ),
        (
            "GET",
            "/v1/reviews/rev_1/feedback",
            "",
            None,
            [{"feedback_id": "hfb_1", "status": "available_for_evolution"}],
        ),
        (
            "POST",
            "/v1/reviews/rev_1/adjudicate",
            "",
            {"status": "adjudicated"},
            {"review_id": "rev_1", "status": "adjudicated"},
        ),
        (
            "POST",
            "/v1/reviews/rev_1/resolve",
            "",
            None,
            {"review_id": "rev_1", "status": "resolved"},
        ),
        (
            "POST",
            "/v1/reviews/rev_1/mark-stale",
            "",
            None,
            {"review_id": "rev_1", "status": "stale"},
        ),
        (
            "POST",
            "/v1/query-decisions",
            "",
            {"decision": "ask_human"},
            {"query_decision_id": "hqd_1", "decision": "ask_human"},
        ),
        (
            "GET",
            "/v1/query-decisions/hqd_1",
            "",
            None,
            {"query_decision_id": "hqd_1", "decision": "ask_human"},
        ),
        (
            "POST",
            "/v1/feedback-applications",
            "",
            {
                "feedback_id": "hfb_1",
                "target_type": "prompt_seed",
                "target_id": "job_next",
                "consumed_by_method": "reflector",
                "effect_summary": "Used feedback.",
            },
            {"application_id": "hfa_1", "feedback_id": "hfb_1"},
        ),
        (
            "GET",
            "/v1/feedback-applications",
            "feedback_id=hfb_1",
            None,
            [{"application_id": "hfa_1", "feedback_id": "hfb_1"}],
        ),
    ]

    async def handler(request):
        method, path, query, body, response_body = expectations.pop(0)
        assert request.method == method
        assert request.url.path == path
        assert request.url.query.decode() == query
        if body is not None:
            assert json.loads(request.content) == body
        return httpx.Response(200, json=response_body)

    client = EvolutionClient(
        "http://evolution.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert await client.create_review_request(review_payload) == {
            "review_id": "rev_1",
            "status": "queued",
        }
        assert await client.get_review_request("rev_1") == {
            "review_id": "rev_1",
            "status": "queued",
        }
        assert await client.list_review_requests(status="queued") == [
            {"review_id": "rev_1", "status": "queued"}
        ]
        assert await client.get_review_packet("rpacket_1") == {
            "packet_id": "rpacket_1",
            "packet": {"questions": ["Approve?"]},
        }
        assert await client.list_review_packets() == [
            {"packet_id": "rpacket_1", "packet": {"questions": ["Approve?"]}}
        ]
        assert await client.claim_review_request(
            "rev_1",
            {"reviewer_id": "alice", "reviewer_role": "maintainer"},
        ) == {"review_id": "rev_1", "status": "in_review"}
        assert await client.submit_human_feedback("rev_1", feedback_payload) == {
            "feedback_id": "hfb_1",
            "status": "available_for_evolution",
        }
        assert await client.list_human_feedback("rev_1") == [
            {"feedback_id": "hfb_1", "status": "available_for_evolution"}
        ]
        assert await client.adjudicate_review_request(
            "rev_1",
            {"status": "adjudicated"},
        ) == {"review_id": "rev_1", "status": "adjudicated"}
        assert await client.resolve_review_request("rev_1") == {
            "review_id": "rev_1",
            "status": "resolved",
        }
        assert await client.mark_review_stale("rev_1") == {
            "review_id": "rev_1",
            "status": "stale",
        }
        assert await client.create_human_query_decision({"decision": "ask_human"}) == {
            "query_decision_id": "hqd_1",
            "decision": "ask_human",
        }
        assert await client.get_human_query_decision("hqd_1") == {
            "query_decision_id": "hqd_1",
            "decision": "ask_human",
        }
        assert await client.create_feedback_application(
            {
                "feedback_id": "hfb_1",
                "target_type": "prompt_seed",
                "target_id": "job_next",
                "consumed_by_method": "reflector",
                "effect_summary": "Used feedback.",
            }
        ) == {"application_id": "hfa_1", "feedback_id": "hfb_1"}
        assert await client.list_feedback_applications(feedback_id="hfb_1") == [
            {"application_id": "hfa_1", "feedback_id": "hfb_1"}
        ]
    finally:
        await client.close()

    assert expectations == []


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
                "source": "openevo",
                "event_type": "openevo.session_completed",
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
                "idempotency_key": "route-dataset",
                "name": "route_dataset",
                "purpose": "skill_distillation",
                "query": {
                    "event_types": ["openevo.session_completed"],
                    "status": ["COMPLETED"],
                    "reward_min": 0.8,
                    "policy_version": "policy_route",
                },
            },
        )

    assert event_response.status_code == 200
    assert dataset_response.status_code == 200
    body = dataset_response.json()
    assert body["dataset_id"].startswith("ds-")
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
    assert artifact_row["uri"] == Path(
        dataset_row["manifest_path"]
    ).as_uri()


def test_dataset_route_openapi_requires_idempotency_key(tmp_path):
    app = create_app(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
    )

    schema = app.openapi()
    request_schema = schema["components"]["schemas"][
        "DatasetCreateHttpRequest"
    ]

    assert "idempotency_key" in request_schema["required"]


def test_get_dataset_route_recovers_the_exact_sealed_dataset(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")

    with TestClient(app) as client:
        event_response = client.post(
            "/v1/events",
            json={
                "source": "openevo",
                "event_type": "openevo.session_completed",
                "source_event_id": "session:dataset-recovery-route",
                "status": "COMPLETED",
                "payload": {"session_result": {"trajectory": {"traces": [{"reward": 1.0}]}}},
            },
        )
        created = client.post(
            "/v1/datasets",
            json={
                "idempotency_key": "recoverable-dataset-route",
                "name": "recoverable_dataset",
                "purpose": "openevo_science_successor_v2",
                "query": {
                    "event_types": ["openevo.session_completed"],
                    "status": ["COMPLETED"],
                },
            },
        )
        recovered = client.get(f"/v1/datasets/{created.json()['dataset_id']}")
        missing = client.get("/v1/datasets/ds_missing")

    assert event_response.status_code == 200
    assert created.status_code == 200
    assert recovered.status_code == 200
    assert recovered.json() == created.json()
    assert missing.status_code == 404


def test_get_dataset_integrity_failure_is_not_reported_as_missing(tmp_path):
    app = create_app(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
    )

    with TestClient(app) as client:
        client.post(
            "/v1/events",
            json={
                "source": "openevo",
                "event_type": "openevo.session_completed",
                "source_event_id": "session:dataset-integrity-route",
                "status": "COMPLETED",
                "payload": {
                    "session_result": {
                        "trajectory": {"traces": [{"reward": 1.0}]}
                    }
                },
            },
        )
        created = client.post(
            "/v1/datasets",
            json={
                "idempotency_key": "integrity-dataset-route",
                "name": "integrity_dataset",
                "purpose": "openevo_science_successor_v2",
                "query": {
                    "event_types": ["openevo.session_completed"],
                    "status": ["COMPLETED"],
                },
            },
        )
        dataset_id = created.json()["dataset_id"]
        with app.state.store.connect() as connection:
            connection.execute(
                "DELETE FROM dataset_events WHERE dataset_id = ?",
                (dataset_id,),
            )
            connection.commit()
        corrupted = client.get(f"/v1/datasets/{dataset_id}")

    assert created.status_code == 200
    assert corrupted.status_code == 409


def test_create_dataset_integrity_failure_is_not_reported_as_validation(
    tmp_path,
):
    app = create_app(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
    )
    request = {
        "idempotency_key": "dataset-integrity-create-route",
        "name": "integrity dataset replay",
        "purpose": "openevo_science_successor_v2",
        "query": {
            "source": "openevo",
            "source_event_id": "session:dataset-integrity-create-route",
        },
        "limits": {"max_events": 1, "max_traces": 1},
    }

    with TestClient(app) as client:
        client.post(
            "/v1/events",
            json={
                "source": "openevo",
                "event_type": "openevo.session_completed",
                "source_event_id": "session:dataset-integrity-create-route",
                "status": "COMPLETED",
                "payload": {
                    "session_result": {
                        "trajectory": {"traces": [{"reward": 1.0}]}
                    }
                },
            },
        )
        created = client.post("/v1/datasets", json=request)
        with app.state.store.connect() as connection:
            connection.execute(
                "DELETE FROM dataset_events WHERE dataset_id = ?",
                (created.json()["dataset_id"],),
            )
            connection.commit()
        corrupted_replay = client.post("/v1/datasets", json=request)

    assert created.status_code == 200
    assert corrupted_replay.status_code == 409


def test_create_dataset_route_rejects_task_tags_without_writes(tmp_path):
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")

    with TestClient(app) as client:
        event_response = client.post(
            "/v1/events",
            json={
                "source": "openevo",
                "event_type": "openevo.session_completed",
                "source_event_id": "session:dataset-task-tags",
                "status": "COMPLETED",
                "payload": {"session_result": {"trajectory": {"traces": [{"reward": 1.0}]}}},
            },
        )
        dataset_response = client.post(
            "/v1/datasets",
            json={
                "idempotency_key": "tagged-route-dataset",
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


def test_create_dataset_route_requires_idempotency_key(tmp_path):
    app = create_app(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/datasets",
            json={
                "name": "unsafe unjournaled dataset",
                "purpose": "skill_distillation",
            },
        )

    assert response.status_code == 422
    assert any(
        detail["loc"] == ["body", "idempotency_key"]
        and detail["type"] == "missing"
        for detail in response.json()["detail"]
    )
    with app.state.store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM datasets").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM dataset_create_requests"
            ).fetchone()[0]
            == 0
        )


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


def test_backend_event_dataset_job_context_flow(tmp_path):
    artifact_root = tmp_path / "artifacts"
    app = create_app(db_path=tmp_path / "evolution.db", artifact_root=artifact_root)
    memory_file = artifact_root / "calculator-memory.md"
    memory_file.write_text("Prefer exact arithmetic for calculator tasks.", encoding="utf-8")

    with TestClient(app) as client:
        event_response = client.post(
            "/v1/events",
            json={
                "source": "openevo",
                "event_type": "openevo.session_completed",
                "source_event_id": "session:backend-flow",
                "task_id": "task_calculator",
                "session_id": "session_backend_flow",
                "status": "COMPLETED",
                "reward": 1.0,
                "policy_version": "policy_1",
                "payload": {
                    "session_result": {
                        "trajectory": {
                            "traces": [
                                {
                                    "observation": "Calculate 2 + 2 exactly.",
                                    "action": "2 + 2 = 4",
                                    "reward": 1.0,
                                }
                            ]
                        }
                    }
                },
            },
        )
        assert event_response.status_code == 200, event_response.text
        event_body = event_response.json()
        dataset_response = client.post(
            "/v1/datasets",
            json={
                "idempotency_key": "calculator-policy-1",
                "name": "calculator_policy_1",
                "purpose": "text_memory_mining",
                "query": {
                    "event_types": ["openevo.session_completed"],
                    "status": ["COMPLETED"],
                    "reward_min": 0.8,
                    "policy_version": "policy_1",
                },
            },
        )
        assert dataset_response.status_code == 200, dataset_response.text
        dataset_body = dataset_response.json()
        dataset_artifact_id = dataset_body["artifact_id"]
        job_create_response = client.post(
            "/v1/jobs",
            json={
                "method": "mock_memory",
                "job_type": "text_memory_mining",
                "input_artifact_ids": [dataset_artifact_id],
            },
        )
        assert job_create_response.status_code == 200, job_create_response.text
        job_create_body = job_create_response.json()
        job_id = job_create_body["job_id"]
        claim_response = client.post(
            "/v1/jobs/claim",
            json={"worker_id": "worker_1", "capabilities": ["text_memory_mining"]},
        )
        assert claim_response.status_code == 200, claim_response.text
        claim_body = claim_response.json()
        claimed_job = claim_body["job"]
        assert claimed_job is not None, claim_response.text
        lease_id = claimed_job["lease_id"]
        complete_response = client.post(
            f"/v1/jobs/{job_id}/complete",
            json={
                "lease_id": lease_id,
                "artifacts": [
                    {
                        "type": "text_memory",
                        "name": "calculator memory",
                        "uri": memory_file.as_uri(),
                        "compatibility": {
                            "task_tags": ["calculator"],
                            "base_model": "Qwen/Qwen3.6-27B",
                        },
                        "tags": ["calculator"],
                        "promoted": True,
                    }
                ],
            },
        )
        assert complete_response.status_code == 200, complete_response.text
        complete_body = complete_response.json()
        memory_artifact_id = complete_body["artifact_ids"][0]
        context_response = client.post(
            "/v1/contexts/resolve",
            json={
                "task_id": "task_calculator",
                "instruction": "Solve the calculator task.",
                "base_model": "Qwen/Qwen3.6-27B",
                "metadata": {"task_tags": ["calculator"]},
            },
        )
        assert context_response.status_code == 200, context_response.text
        context_body = context_response.json()

    assert event_body["ingested"] is True
    assert dataset_body["event_count"] == 1
    assert dataset_artifact_id.startswith("art_")
    assert claimed_job["job_id"] == job_id
    assert claimed_job["input_artifacts"][0]["artifact_id"] == dataset_artifact_id
    assert claimed_job["input_artifacts"][0]["type"] == "dataset"
    dataset_manifest_path = Path(claimed_job["input_artifacts"][0]["uri"].removeprefix("file://"))
    dataset_manifest = json.loads(dataset_manifest_path.read_text(encoding="utf-8"))
    dataset_records_path = dataset_manifest_path.parent / dataset_manifest["records_path"]
    dataset_records = [
        json.loads(line)
        for line in dataset_records_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert dataset_records[0]["event_id"] == event_body["event_id"]
    assert dataset_records[0]["task_id"] == "task_calculator"
    assert dataset_records[0]["traces"] == [
        {
            "observation": "Calculate 2 + 2 exactly.",
            "action": "2 + 2 = 4",
            "reward": 1.0,
        }
    ]
    assert complete_body["state"] == "succeeded"
    assert memory_artifact_id.startswith("art_")
    assert memory_artifact_id in context_body["memory"]["artifact_ids"]
    assert "Prefer exact arithmetic" in context_body["memory"]["rendered_text"]


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
