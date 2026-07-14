from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from openevo.backend.contracts.v1.app import create_core_control_contract_app
from openevo.backend.contracts.v1.models import (
    EventEnvelopeV1,
    ParametricMemoryArtifactSummaryV1,
    RunCreateV1,
    RunSummaryV1,
)
from openevo.backend.contracts.v1.snapshots import (
    EVENTS_SCHEMA_SNAPSHOT_PATH,
    OPENAPI_SNAPSHOT_PATH,
    build_events_schema_document,
    build_openapi_document,
    canonical_json_bytes,
    deterministic_sha256,
    events_schema_sha256,
    openapi_sha256,
)


EXPECTED_OPERATIONS = {
    ("GET", "/version"),
    ("GET", "/health"),
    ("GET", "/v1/status"),
    ("POST", "/v1/environment/doctor"),
    ("POST", "/v1/environment/repair"),
    ("GET", "/v1/capabilities"),
    ("GET", "/v1/projects"),
    ("POST", "/v1/projects"),
    ("GET", "/v1/projects/{project_id}"),
    ("PATCH", "/v1/projects/{project_id}"),
    ("DELETE", "/v1/projects/{project_id}"),
    ("POST", "/v1/projects/{project_id}/workspace-sync"),
    ("POST", "/v1/projects/{project_id}/validate"),
    ("GET", "/v1/runs"),
    ("POST", "/v1/runs"),
    ("GET", "/v1/runs/{run_id}"),
    ("DELETE", "/v1/runs/{run_id}"),
    ("POST", "/v1/runs/{run_id}/cancel"),
    ("POST", "/v1/runs/{run_id}/retry"),
    ("GET", "/v1/runs/{run_id}/timeline"),
    ("GET", "/v1/runs/{run_id}/logs"),
    ("GET", "/v1/runs/{run_id}/context"),
    ("GET", "/v1/runs/{run_id}/artifacts"),
    ("GET", "/v1/artifacts/{artifact_id}"),
    ("GET", "/v1/artifacts/{artifact_id}/content"),
    ("GET", "/v1/artifacts/{artifact_id}/diff"),
    ("GET", "/v1/services"),
    ("POST", "/v1/services/{service_id}/restart"),
    ("POST", "/v1/services/{service_id}/stop"),
    ("GET", "/v1/services/{service_id}/logs"),
    ("POST", "/v1/diagnostics"),
    ("GET", "/v1/diagnostics/{diagnostic_id}"),
    ("DELETE", "/v1/diagnostics/{diagnostic_id}"),
    ("POST", "/v1/maintenance/cache-cleanup"),
    ("GET", "/v1/events"),
}


def _operations(openapi: dict[str, Any]) -> set[tuple[str, str]]:
    return {
        (method.upper(), path)
        for path, path_item in openapi["paths"].items()
        for method in path_item
        if method in {"get", "post", "patch", "delete", "put"}
    }


def _json_model(model: type[Any], value: dict[str, Any]) -> Any:
    return model.model_validate_json(json.dumps(value))


def _valid_run_create() -> dict[str, Any]:
    return {
        "schema_version": "1",
        "project_id": "project-1",
        "project_snapshot_id": "project-snapshot-1",
        "task_snapshot_id": "task-snapshot-1",
        "workspace_snapshot_id": "workspace-snapshot-1",
        "expected_registry_digest": "a" * 64,
        "required_revision_id": "revision-7",
        "execution_mode": "self-deployed",
        "capture_mode": "transcript",
    }


def _valid_run_summary() -> dict[str, Any]:
    return {
        "id": "run-1",
        "project_id": "project-1",
        "project_snapshot_id": "project-snapshot-1",
        "task_snapshot_id": "task-snapshot-1",
        "workspace_snapshot_id": "workspace-snapshot-1",
        "status": "queued",
        "queued_reason": "required_revision_uncommitted",
        "current_attempt_id": None,
        "attempt_count": 0,
        "pinned_revision_id": None,
        "required_revision_id": "revision-7",
        "created_at": "2026-07-14T00:00:00Z",
        "started_at": None,
        "finished_at": None,
    }


def test_openapi_snapshot_is_exactly_rebuildable() -> None:
    rebuilt = canonical_json_bytes(build_openapi_document())
    assert OPENAPI_SNAPSHOT_PATH.read_bytes() == rebuilt
    assert hashlib.sha256(rebuilt).hexdigest() == openapi_sha256()
    assert openapi_sha256() == ("1589c7141f00acdee9de1c3a1c01c77805ad3d9460d717755af81ab86b755279")


def test_event_schema_snapshot_is_exactly_rebuildable() -> None:
    rebuilt = canonical_json_bytes(build_events_schema_document())
    assert EVENTS_SCHEMA_SNAPSHOT_PATH.read_bytes() == rebuilt
    assert hashlib.sha256(rebuilt).hexdigest() == events_schema_sha256()
    assert events_schema_sha256() == (
        "f8b81b535450349e45c9625eb9cde45101722ec43ff8d40ce3cfb19f0c355532"
    )


def test_deterministic_digest_uses_canonical_json() -> None:
    first = {"z": [3, 2, 1], "a": {"value": True}}
    second = {"a": {"value": True}, "z": [3, 2, 1]}
    assert deterministic_sha256(first) == deterministic_sha256(second)
    assert canonical_json_bytes(first) == canonical_json_bytes(second)


def test_contract_app_exposes_the_exact_core_v1_surface() -> None:
    app = create_core_control_contract_app()
    openapi = app.openapi()
    assert _operations(openapi) == EXPECTED_OPERATIONS
    assert openapi["x-openevo-contract-only"] is True
    assert openapi["x-openevo-business-provider"] is False
    assert "Schema Only" in openapi["info"]["title"]
    assert {route.path for route in app.routes} == {path for _, path in EXPECTED_OPERATIONS}


def test_contract_app_never_returns_a_business_fixture() -> None:
    response = TestClient(create_core_control_contract_app()).get("/version")
    assert response.status_code == 501
    assert response.json() == {
        "schema_version": "1",
        "code": "contract_only_not_implemented",
        "message": ("This app defines the Core Control API v1 contract and has no provider."),
    }


def test_core_routes_declare_bearer_security_and_mutation_headers() -> None:
    openapi = build_openapi_document()
    for path, path_item in openapi["paths"].items():
        for method, operation in path_item.items():
            if method not in {"get", "post", "patch", "delete"}:
                continue
            if path.startswith("/v1/"):
                assert operation["security"] == [{"CoreBearerAuth": []}]
            parameters = {
                (parameter["in"], parameter["name"]): parameter
                for parameter in operation.get("parameters", [])
            }
            if method in {"post", "patch", "delete"}:
                assert parameters[("header", "Idempotency-Key")]["required"] is True
            if method == "patch" or method == "delete":
                assert parameters[("header", "If-Match")]["required"] is True


def test_capability_request_is_bound_only_by_the_release_execution_mode() -> None:
    operation = build_openapi_document()["paths"]["/v1/capabilities"]["get"]
    query_parameters = {
        parameter["name"] for parameter in operation["parameters"] if parameter["in"] == "query"
    }
    assert query_parameters == {"execution_mode"}


def test_openapi_object_models_are_closed_and_collections_are_bounded() -> None:
    openapi = build_openapi_document()
    for name, schema in openapi["components"]["schemas"].items():
        if schema.get("type") == "object":
            assert schema.get("additionalProperties") is False, name

    stack: list[tuple[str, object]] = [("$", openapi)]
    while stack:
        path, value = stack.pop()
        if isinstance(value, dict):
            if value.get("type") == "array":
                assert "maxItems" in value, path
            stack.extend((f"{path}/{key}", item) for key, item in value.items())
        elif isinstance(value, list):
            stack.extend((f"{path}/{index}", item) for index, item in enumerate(value))


@pytest.mark.parametrize(
    "forbidden_field,forbidden_value",
    [
        ("runtime", {"container": "docker"}),
        ("model", {"name": "provider-model"}),
        ("host_path", "/tmp/workspace"),
        ("command", "rm -rf /"),
        ("benchmark_id", "terminal-bench"),
        ("admission_envelope", {"raw": True}),
    ],
)
def test_run_create_rejects_open_or_provider_owned_fields(
    forbidden_field: str,
    forbidden_value: object,
) -> None:
    payload = _valid_run_create()
    payload[forbidden_field] = forbidden_value
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _json_model(RunCreateV1, payload)


def test_run_create_uses_exact_execution_mode_and_capture_rules() -> None:
    assert _json_model(RunCreateV1, _valid_run_create()).execution_mode.value == ("self-deployed")

    underscore_mode = _valid_run_create()
    underscore_mode["execution_mode"] = "self_deployed"
    with pytest.raises(ValidationError):
        _json_model(RunCreateV1, underscore_mode)

    subscription = _valid_run_create()
    subscription["execution_mode"] = "codex_subscription_transcript"
    subscription["capture_mode"] = "token_level"
    with pytest.raises(ValidationError, match="requires transcript capture"):
        _json_model(RunCreateV1, subscription)


def test_run_create_schema_contains_only_immutable_control_references() -> None:
    schema = build_openapi_document()["components"]["schemas"]["RunCreateV1"]
    assert set(schema["properties"]) == {
        "schema_version",
        "project_id",
        "project_snapshot_id",
        "task_snapshot_id",
        "workspace_snapshot_id",
        "expected_registry_digest",
        "required_revision_id",
        "execution_mode",
        "capture_mode",
    }
    assert schema["additionalProperties"] is False


def test_run_state_shape_enforces_queue_and_terminal_invariants() -> None:
    queued = _json_model(RunSummaryV1, _valid_run_summary())
    assert queued.status.value == "queued"

    missing_queue_reason = _valid_run_summary()
    missing_queue_reason["queued_reason"] = None
    with pytest.raises(ValidationError, match="queued_reason"):
        _json_model(RunSummaryV1, missing_queue_reason)

    terminal_without_finish = _valid_run_summary()
    terminal_without_finish.update(
        {
            "status": "succeeded",
            "queued_reason": None,
            "current_attempt_id": "attempt-1",
            "attempt_count": 1,
            "pinned_revision_id": "revision-7",
            "started_at": "2026-07-14T00:00:01Z",
        }
    )
    with pytest.raises(ValidationError, match="finished_at"):
        _json_model(RunSummaryV1, terminal_without_finish)

    unknown_state = _valid_run_summary()
    unknown_state["status"] = "completed"
    with pytest.raises(ValidationError):
        _json_model(RunSummaryV1, unknown_state)


def test_unknown_sse_fields_and_event_names_are_rejected() -> None:
    event = {
        "schema_version": "1",
        "id": "event-1",
        "sequence": 1,
        "occurred_at": "2026-07-14T00:00:00Z",
        "event": "heartbeat.v1",
        "payload": {"active_run_count": 0},
    }
    assert EventEnvelopeV1.model_validate_json(json.dumps(event)).root.event == ("heartbeat.v1")

    event["payload"]["unknown"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        EventEnvelopeV1.model_validate_json(json.dumps(event))

    event["payload"].pop("unknown")
    event["event"] = "run.created.v1"
    with pytest.raises(ValidationError):
        EventEnvelopeV1.model_validate_json(json.dumps(event))


def test_parametric_memory_is_typed_but_cannot_be_release_enabled() -> None:
    artifact = {
        "id": "artifact-1",
        "run_id": "run-1",
        "title": "Adapter",
        "revision_id": "revision-7",
        "content_sha256": "b" * 64,
        "selected": False,
        "promoted": False,
        "release_enabled": True,
        "compatibility": {
            "execution_modes": ["self-deployed"],
            "harness_ids": ["codex"],
            "base_model_refs": ["model-1"],
        },
        "lineage": {
            "method_id": "method-1",
            "job_id": "job-1",
            "source_dataset_ids": [],
            "source_artifact_ids": [],
        },
        "scores": [],
        "created_at": "2026-07-14T00:00:00Z",
        "artifact_type": "parametric_memory",
    }
    with pytest.raises(ValidationError):
        _json_model(ParametricMemoryArtifactSummaryV1, artifact)


def test_sse_openapi_response_uses_the_standalone_envelope_schema() -> None:
    openapi = build_openapi_document()
    operation = openapi["paths"]["/v1/events"]["get"]
    assert operation["x-sse-delivery"] == "at-least-once"
    assert operation["x-sse-heartbeat-seconds"] == 15
    assert operation["x-sse-replay"] == "bounded"
    assert operation["responses"]["200"]["content"] == {
        "text/event-stream": {"schema": {"$ref": "#/components/schemas/EventEnvelopeV1"}}
    }
