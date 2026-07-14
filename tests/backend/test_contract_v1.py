from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from openevo.backend.contracts.v1.app import create_core_control_contract_app
from openevo.backend.contracts.v1.models import (
    ArtifactContentV1,
    ArtifactDiffV1,
    CapabilitiesResponseV1,
    EventEnvelopeV1,
    ExecutionMode,
    ImmutableSnapshotRefV1,
    ParametricMemoryArtifactSummaryV1,
    ProjectCreateV1,
    ProjectSpecV1,
    ProjectV1,
    ReachableRequiredRevisionRefV1,
    RevisionTransitionState,
    RunCreateV1,
    RunContextV1,
    RunSummaryV1,
    StrongETag,
    TaskSpecV1,
    WorkspaceArchiveV1,
    WorkspaceUploadChunkV1,
)
from openevo.evolution.framework.capabilities import EvolutionCapabilitiesV1
from openevo.evolution.framework.profiles import ReleaseExecutionMode
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
    ("GET", "/v1/projects/{project_id}/revisions"),
    ("GET", "/v1/projects/{project_id}/revisions/head"),
    ("GET", "/v1/revisions/{revision_id}"),
    ("POST", "/v1/projects/{project_id}/workspace-uploads"),
    ("GET", "/v1/projects/{project_id}/workspace-uploads/{upload_id}"),
    ("PUT", "/v1/projects/{project_id}/workspace-uploads/{upload_id}/chunk"),
    ("POST", "/v1/projects/{project_id}/workspace-uploads/{upload_id}/finalize"),
    ("POST", "/v1/projects/{project_id}/workspace-uploads/{upload_id}/abort"),
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
    ("GET", "/v1/services/{service_id}"),
    ("POST", "/v1/services/{service_id}/restart"),
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
        "project_snapshot": _snapshot_ref("project", "project-snapshot-1", "1"),
        "task_snapshot": _snapshot_ref("task", "task-snapshot-1", "2"),
        "workspace_snapshot": _snapshot_ref("workspace", "workspace-snapshot-1", "3"),
        "expected_registry_digest": "a" * 64,
        "required_revision": _required_revision_ref(),
    }


def _snapshot_ref(kind: str, snapshot_id: str, digest_seed: str) -> dict[str, Any]:
    return {
        "id": snapshot_id,
        "kind": kind,
        "content_sha256": digest_seed * 64,
        "created_at": "2026-07-14T00:00:00Z",
    }


def _revision_ref(revision_id: str = "revision-7") -> dict[str, Any]:
    return {
        "id": revision_id,
        "project_id": "project-1",
        "generation": 7,
        "manifest_sha256": "7" * 64,
    }


def _required_revision_ref() -> dict[str, Any]:
    return {
        "revision": _revision_ref(),
        "reachable_from_revision_id": "revision-6",
        "relation": "successor",
    }


def _queued_reason() -> dict[str, Any]:
    return {
        "code": "required_revision_uncommitted",
        "summary": "The required revision is not active yet.",
        "retry_after_seconds": 5,
    }


def _transition() -> dict[str, Any]:
    return {
        "state": "preparing_serving",
        "predecessor_revision": {
            **_revision_ref("revision-6"),
            "generation": 6,
            "manifest_sha256": "6" * 64,
        },
        "successor_revision": _revision_ref(),
        "progress_completed": 4,
        "progress_total": 6,
        "message": "Preparing the successor for serving.",
        "error": None,
        "updated_at": "2026-07-14T00:00:04Z",
    }


def _valid_run_summary() -> dict[str, Any]:
    return {
        "id": "run-1",
        "project_id": "project-1",
        "project_snapshot": _snapshot_ref("project", "project-snapshot-1", "1"),
        "task_snapshot": _snapshot_ref("task", "task-snapshot-1", "2"),
        "workspace_snapshot": _snapshot_ref("workspace", "workspace-snapshot-1", "3"),
        "registry_digest": "a" * 64,
        "execution_mode": "self-deployed",
        "capture_mode": "transcript",
        "status": "queued",
        "queued_reason": _queued_reason(),
        "current_attempt_id": None,
        "current_attempt": None,
        "attempt_count": 0,
        "current_error": None,
        "pinned_revision": None,
        "required_revision": _required_revision_ref(),
        "revision_transition": _transition(),
        "created_at": "2026-07-14T00:00:00Z",
        "updated_at": "2026-07-14T00:00:04Z",
        "started_at": None,
        "finished_at": None,
        "etag": '"' + "e" * 64 + '"',
    }


def _content_ref(content_id: str = "content-1") -> dict[str, Any]:
    return {
        "content_id": content_id,
        "sha256": "c" * 64,
        "byte_size": 1024,
    }


def _workspace_archive() -> dict[str, Any]:
    return {
        "content_ref": _content_ref("workspace-archive-1"),
        "format": "openevo_deterministic_tar_v1",
        "entry_count": 2,
        "extracted_byte_size": 100,
        "policy": {
            "media_type": "application/vnd.openevo.workspace-tar",
            "tar_format": "posix_ustar",
            "entry_types": "regular_files_and_directories",
            "path_policy": "utf8_nfc_posix_relative",
            "entry_order": "lexicographic",
            "metadata_policy": "uid_gid_zero_names_empty_mtime_zero",
            "file_mode_policy": "0644_or_0755",
            "directory_mode": "0755",
            "allow_symlinks": False,
            "allow_hardlinks": False,
            "allow_devices": False,
            "allow_fifos": False,
            "allow_sparse_files": False,
            "allow_tar_extensions": False,
            "max_entries": 100_000,
            "max_path_depth": 32,
            "max_path_bytes": 1024,
            "max_file_bytes": 8 * 1024 * 1024 * 1024,
            "max_extracted_bytes": 16 * 1024 * 1024 * 1024,
        },
    }


def test_openapi_snapshot_is_exactly_rebuildable() -> None:
    rebuilt = canonical_json_bytes(build_openapi_document())
    assert OPENAPI_SNAPSHOT_PATH.read_bytes() == rebuilt
    assert hashlib.sha256(rebuilt).hexdigest() == openapi_sha256()
    assert openapi_sha256() == ("5670a279cfeb4c424098c1ca01e5d84ab426b820f4b3a67e8855ed785d84db61")


def test_event_schema_snapshot_is_exactly_rebuildable() -> None:
    rebuilt = canonical_json_bytes(build_events_schema_document())
    assert EVENTS_SCHEMA_SNAPSHOT_PATH.read_bytes() == rebuilt
    assert hashlib.sha256(rebuilt).hexdigest() == events_schema_sha256()
    assert events_schema_sha256() == (
        "80944325e12ce9beb3844a573e4930df2e00e19e646a2df2cf4044d7701e52a1"
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
            if method in {"post", "put", "patch", "delete"}:
                assert parameters[("header", "Idempotency-Key")]["required"] is True
            if method in {"patch", "delete"}:
                assert parameters[("header", "If-Match")]["required"] is True


def test_capability_request_is_bound_only_by_the_release_execution_mode() -> None:
    assert ExecutionMode is ReleaseExecutionMode
    operation = build_openapi_document()["paths"]["/v1/capabilities"]["get"]
    query_parameters = {
        parameter["name"] for parameter in operation["parameters"] if parameter["in"] == "query"
    }
    assert query_parameters == {"execution_mode"}


def test_capability_response_is_the_authoritative_registry_contract() -> None:
    assert CapabilitiesResponseV1 is EvolutionCapabilitiesV1
    schema = build_openapi_document()["components"]["schemas"]
    target = schema["EvolutionTargetCapabilityV1"]["properties"]
    method = schema["EvolutionMethodCapabilityV1"]["properties"]

    assert {
        "exposure",
        "maturity",
        "handler_id",
        "renderer_kind",
        "renderer_contract_version",
        "contribution_contract_version",
        "implementation_identity_digest",
        "handler_identity_digest",
        "accepted_methods",
        "selection_resolvers",
    } <= set(target)
    assert {
        "input_bindings",
        "output_artifact_types",
        "config_schema_json",
        "default_config_json",
        "support",
    } <= set(method)


def test_project_spec_uses_the_core_owned_evolution_target_map() -> None:
    spec = _json_model(
        ProjectSpecV1,
        {
            "execution_mode": "self-deployed",
            "capture_mode": "transcript",
            "harness_id": "codex",
            "agent_model_ref": "openai/gpt-oss-20b",
            "evolution": {
                "targets": {
                    "text_memory": {
                        "enabled": True,
                        "method": "reference_text_memory",
                        "config": {"max_records": 10},
                    }
                }
            },
        },
    )

    assert spec.evolution.targets["text_memory"].method == "reference_text_memory"
    assert spec.model_dump(mode="json")["evolution"]["targets"]["text_memory"] == {
        "enabled": True,
        "method": "reference_text_memory",
        "config": {"max_records": 10},
    }
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _json_model(
            ProjectSpecV1,
            {
                "execution_mode": "self-deployed",
                "capture_mode": "transcript",
                "harness_id": "codex",
                "agent_model_ref": "openai/gpt-oss-20b",
                "evolution_targets": [],
            },
        )


def test_self_deployed_model_ref_is_not_a_managed_model_id() -> None:
    schema = build_openapi_document()["components"]["schemas"]
    field = schema["ProjectSpecV1"]["properties"]["agent_model_ref"]
    assert "exact bounded Hugging Face model string" in field["description"]
    assert field["maxLength"] == 256
    assert {
        "unresolved",
        "downloading",
        "ready",
        "failed",
    } == set(schema["ModelPreparationStatus"]["enum"])
    for resource in ("ProjectSummaryV1", "EnvironmentCheckV1", "ServiceSummaryV1"):
        assert "model_preparation" in schema[resource]["properties"]


def test_project_lifecycle_owns_task_and_workspace_snapshot_inputs() -> None:
    openapi = build_openapi_document()
    schemas = openapi["components"]["schemas"]
    assert {"task", "workspace"} <= set(schemas["ProjectCreateV1"]["required"])
    assert {"task", "workspace"} <= set(schemas["ProjectCreateV1"]["properties"])
    assert {"task", "workspace"} <= set(schemas["ProjectPatchV1"]["properties"])
    assert {"task", "workspace"} <= set(schemas["ProjectV1"]["required"])
    assert "current_task_snapshot" in schemas["ProjectSummaryV1"]["required"]

    task = {
        "title": "Prove the theorem",
        "objective": "Produce a checked proof and explain each inference.",
        "content_ref": _content_ref("task-content-1"),
    }
    assert _json_model(TaskSpecV1, task).title == "Prove the theorem"
    for forbidden, value in (
        ("host_path", "/tmp/task.md"),
        ("benchmark_id", "terminal-bench"),
        ("command", "cat task.md"),
    ):
        malicious = {**task, forbidden: value}
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            _json_model(TaskSpecV1, malicious)


def test_scratch_project_create_has_a_core_signed_empty_workspace_path() -> None:
    create = {
        "schema_version": "1",
        "name": "Scratch science",
        "description": None,
        "spec": {
            "execution_mode": "self-deployed",
            "capture_mode": "transcript",
            "harness_id": "codex",
            "agent_model_ref": "openai/gpt-oss-20b",
            "evolution": {"targets": {}},
        },
        "task": {
            "title": "Explore",
            "objective": "Create the result in a new empty workspace.",
            "content_ref": None,
        },
        "workspace": {"kind": "scratch", "display_name": "Empty workspace"},
    }
    assert _json_model(ProjectCreateV1, create).workspace.kind == "scratch"

    project = {
        "id": "project-1",
        "name": "Scratch science",
        "description": None,
        "status": "draft",
        "execution_mode": "self-deployed",
        "workspace_kind": "scratch",
        "current_project_snapshot": _snapshot_ref("project", "project-snapshot-1", "1"),
        "current_task_snapshot": _snapshot_ref("task", "task-snapshot-1", "2"),
        "current_workspace_snapshot": None,
        "active_revision": None,
        "registry_digest": None,
        "model_preparation": {
            "model_ref": "openai/gpt-oss-20b",
            "status": "unresolved",
            "downloaded_bytes": None,
            "total_bytes": None,
            "error": None,
            "updated_at": "2026-07-14T00:00:00Z",
        },
        "created_at": "2026-07-14T00:00:00Z",
        "updated_at": "2026-07-14T00:00:00Z",
        "etag": '"' + "e" * 64 + '"',
        "spec": create["spec"],
        "task": create["task"],
        "workspace": create["workspace"],
    }
    with pytest.raises(ValidationError, match="scratch project requires"):
        _json_model(ProjectV1, project)

    project["current_workspace_snapshot"] = _snapshot_ref(
        "workspace", "empty-workspace-snapshot-1", "0"
    )
    assert _json_model(ProjectV1, project).current_workspace_snapshot is not None


def test_workspace_archive_format_and_extraction_policy_are_frozen() -> None:
    archive = _json_model(WorkspaceArchiveV1, _workspace_archive())
    assert archive.format.value == "openevo_deterministic_tar_v1"
    assert archive.policy.allow_symlinks is False
    assert archive.policy.allow_hardlinks is False
    assert archive.policy.allow_devices is False
    assert archive.policy.max_entries == 100_000
    assert archive.policy.max_extracted_bytes == 16 * 1024 * 1024 * 1024

    for field in (
        "allow_symlinks",
        "allow_hardlinks",
        "allow_devices",
        "allow_fifos",
        "allow_sparse_files",
        "allow_tar_extensions",
    ):
        unsafe = _workspace_archive()
        unsafe["policy"][field] = True
        with pytest.raises(ValidationError):
            _json_model(WorkspaceArchiveV1, unsafe)

    unsupported = _workspace_archive()
    unsupported["format"] = "zip"
    with pytest.raises(ValidationError):
        _json_model(WorkspaceArchiveV1, unsupported)

    schema = build_openapi_document()["components"]["schemas"]
    assert schema["WorkspaceArchivePolicyV1"]["additionalProperties"] is False
    assert "dot-dot" in schema["WorkspaceArchivePolicyV1"]["properties"]["path_policy"][
        "description"
    ]
    assert "regular-file and directory" in schema["WorkspaceArchivePolicyV1"][
        "properties"
    ]["entry_types"]["description"]
    assert schema["WorkspaceArchivePolicyV1"]["properties"]["max_path_depth"][
        "const"
    ] == 32
    assert set(schema["WorkspaceUploadCreateV1"]["properties"]) == {
        "schema_version",
        "archive",
        "base_workspace_snapshot",
    }


def test_revision_transition_includes_model_serving_preparation() -> None:
    assert RevisionTransitionState.PREPARING_SERVING.value == "preparing_serving"


def test_immutable_refs_are_closed_content_addressed_values() -> None:
    ref = _json_model(
        ImmutableSnapshotRefV1,
        _snapshot_ref("workspace", "workspace-snapshot-1", "3"),
    )
    assert ref.kind.value == "workspace"

    malicious = _snapshot_ref("workspace", "workspace-snapshot-1", "3")
    malicious["host_path"] = "/tmp/workspace"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _json_model(ImmutableSnapshotRefV1, malicious)

    required = _required_revision_ref()
    required["relation"] = "unreachable"
    with pytest.raises(ValidationError):
        _json_model(ReachableRequiredRevisionRefV1, required)


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


def test_run_create_leaves_execution_and_capture_to_the_core_project() -> None:
    assert _json_model(RunCreateV1, _valid_run_create()).project_id == "project-1"

    for field, value in (
        ("execution_mode", "self-deployed"),
        ("capture_mode", "transcript"),
    ):
        client_owned = _valid_run_create()
        client_owned[field] = value
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            _json_model(RunCreateV1, client_owned)


def test_run_create_schema_contains_only_immutable_control_references() -> None:
    schema = build_openapi_document()["components"]["schemas"]["RunCreateV1"]
    assert set(schema["properties"]) == {
        "schema_version",
        "project_id",
        "project_snapshot",
        "task_snapshot",
        "workspace_snapshot",
        "expected_registry_digest",
        "required_revision",
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
            "pinned_revision": _revision_ref(),
            "started_at": "2026-07-14T00:00:01Z",
        }
    )
    with pytest.raises(ValidationError, match="finished_at"):
        _json_model(RunSummaryV1, terminal_without_finish)

    unknown_state = _valid_run_summary()
    unknown_state["status"] = "completed"
    with pytest.raises(ValidationError):
        _json_model(RunSummaryV1, unknown_state)


def test_run_list_shape_contains_current_attempt_error_and_transition() -> None:
    properties = build_openapi_document()["components"]["schemas"]["RunSummaryV1"][
        "properties"
    ]
    assert {
        "current_attempt",
        "current_error",
        "required_revision",
        "pinned_revision",
        "revision_transition",
        "updated_at",
        "etag",
    } <= set(properties)


def test_queued_run_and_context_never_fabricate_a_pinned_revision() -> None:
    run = _json_model(RunSummaryV1, _valid_run_summary())
    assert run.pinned_revision is None

    fabricated = _valid_run_summary()
    fabricated["pinned_revision"] = _revision_ref()
    with pytest.raises(ValidationError, match="queued run"):
        _json_model(RunSummaryV1, fabricated)

    context = {
        "schema_version": "1",
        **_valid_run_summary(),
        "run_id": "run-1",
        "token_level_metrics_available": False,
        "artifacts": [],
        "adapters": [],
    }
    context.pop("id")
    parsed = _json_model(RunContextV1, context)
    assert parsed.pinned_revision is None


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
        "project_id": "project-1",
        "run_id": "run-1",
        "target_id": "parametric_memory",
        "display_name": "Adapter",
        "summary": "A reserved adapter output.",
        "byte_size": 10,
        "produced_revision": _revision_ref(),
        "membership_revisions": [_revision_ref()],
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
        "metadata": {
            "adapter_id": "adapter-1",
            "base_model_ref": "openai/gpt-oss-20b",
            "adapter_format": "lora",
        },
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
    assert operation["x-sse-replay-max-events"] == 10_000


def test_workspace_upload_chunks_are_bounded_and_content_addressed() -> None:
    payload = {
        "schema_version": "1",
        "offset": 0,
        "byte_length": 3,
        "content_base64": "YWJj",
        "content_sha256": hashlib.sha256(b"abc").hexdigest(),
    }
    assert _json_model(WorkspaceUploadChunkV1, payload).byte_length == 3

    wrong_digest = {**payload, "content_sha256": "0" * 64}
    with pytest.raises(ValidationError, match="digest"):
        _json_model(WorkspaceUploadChunkV1, wrong_digest)

    host_path = {**payload, "host_path": "/tmp/source"}
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _json_model(WorkspaceUploadChunkV1, host_path)

    schema = build_openapi_document()["components"]["schemas"]["WorkspaceUploadChunkV1"]
    assert schema["properties"]["byte_length"]["maximum"] == 8 * 1024 * 1024
    assert schema["properties"]["offset"]["maximum"] == 16 * 1024 * 1024 * 1024


def test_artifact_content_is_one_bounded_document_preview_shape() -> None:
    document = {
        "document_id": "doc-1",
        "display_name": "SKILL.md",
        "relative_path": "SKILL.md",
        "mime_type": "text/markdown",
        "content": "skill",
        "content_sha256": hashlib.sha256(b"skill").hexdigest(),
        "byte_size": 5,
        "truncated": False,
    }
    payload = {
        "schema_version": "1",
        "artifact_id": "artifact-1",
        "artifact_type": "skill_bundle",
        "documents": [document],
        "total_documents": 1,
        "total_utf8_bytes": 5,
        "returned_utf8_bytes": 5,
        "truncated": False,
    }
    assert _json_model(ArtifactContentV1, payload).total_documents == 1

    oversized = {
        **payload,
        "documents": [{**document, "content": "x" * (2 * 1024 * 1024 + 1)}],
        "total_utf8_bytes": 2 * 1024 * 1024 + 1,
        "returned_utf8_bytes": 2 * 1024 * 1024 + 1,
    }
    with pytest.raises(ValidationError):
        _json_model(ArtifactContentV1, oversized)


def test_artifact_diff_is_bounded_structured_data() -> None:
    payload = {
        "schema_version": "1",
        "artifact_id": "artifact-2",
        "previous_artifact_id": "artifact-1",
        "hunks": [
            {
                "old_start": 1,
                "old_count": 1,
                "new_start": 1,
                "new_count": 1,
                "lines": [
                    {
                        "kind": "context",
                        "old_line_number": 1,
                        "new_line_number": 1,
                        "text": "unchanged",
                    }
                ],
            }
        ],
        "total_hunks": 1,
        "total_lines": 1,
        "truncated": False,
    }
    diff = _json_model(ArtifactDiffV1, payload)
    assert diff.hunks[0].lines[0].kind.value == "context"


def _response_schema_name(operation: dict[str, Any]) -> str | None:
    for status in ("200", "201", "202"):
        response = operation["responses"].get(status)
        if response is not None:
            return response["content"]["application/json"]["schema"].get("$ref", "").rsplit(
                "/", 1
            )[-1]
    return None


def test_every_if_match_resource_has_the_same_strict_etag_on_read_or_action() -> None:
    openapi = build_openapi_document()
    schemas = openapi["components"]["schemas"]
    strict_etag_schema = StrongETag.__metadata__[0]
    assert strict_etag_schema.pattern == r'^"[0-9a-f]{64}"$'

    for path, path_item in openapi["paths"].items():
        for method, operation in path_item.items():
            if method not in {"post", "put", "patch", "delete"}:
                continue
            parameters = {
                (parameter["in"], parameter["name"]): parameter
                for parameter in operation.get("parameters", [])
            }
            if ("header", "If-Match") not in parameters:
                continue
            header_schema = parameters[("header", "If-Match")]["schema"]
            assert header_schema["pattern"] == r'^"[0-9a-f]{64}"$'
            assert header_schema["minLength"] == 66
            assert header_schema["maxLength"] == 66

            resource_path = path.rsplit("/", 1)[0] if method == "post" else path
            read_operation = openapi["paths"].get(resource_path, {}).get("get")
            response_name = _response_schema_name(operation)
            read_name = _response_schema_name(read_operation) if read_operation else None
            candidate_names = {name for name in (response_name, read_name) if name}
            assert candidate_names, (method, path)
            assert any("etag" in schemas[name].get("properties", {}) for name in candidate_names), (
                method,
                path,
                candidate_names,
            )

    conditional_actions = {
        ("patch", "/v1/projects/{project_id}"),
        ("delete", "/v1/projects/{project_id}"),
        (
            "put",
            "/v1/projects/{project_id}/workspace-uploads/{upload_id}/chunk",
        ),
        (
            "post",
            "/v1/projects/{project_id}/workspace-uploads/{upload_id}/finalize",
        ),
        (
            "post",
            "/v1/projects/{project_id}/workspace-uploads/{upload_id}/abort",
        ),
        ("delete", "/v1/runs/{run_id}"),
        ("post", "/v1/runs/{run_id}/cancel"),
        ("post", "/v1/runs/{run_id}/retry"),
        ("post", "/v1/services/{service_id}/restart"),
        ("delete", "/v1/diagnostics/{diagnostic_id}"),
    }
    discovered = set()
    for path, path_item in openapi["paths"].items():
        for method, operation in path_item.items():
            if method not in {"post", "put", "patch", "delete"}:
                continue
            if any(
                parameter["in"] == "header" and parameter["name"] == "If-Match"
                for parameter in operation.get("parameters", [])
            ):
                discovered.add((method, path))
    assert discovered == conditional_actions


def test_revision_surface_is_read_only_and_mutation_status_codes_are_exact() -> None:
    openapi = build_openapi_document()
    revision_paths = {
        path: item for path, item in openapi["paths"].items() if "revision" in path
    }
    assert revision_paths
    assert all(set(item) <= {"get"} for item in revision_paths.values())
    assert not any("activate" in path or "promote" in path for path in openapi["paths"])

    expected = {
        ("post", "/v1/projects"): "201",
        ("post", "/v1/projects/{project_id}/workspace-uploads"): "201",
        (
            "put",
            "/v1/projects/{project_id}/workspace-uploads/{upload_id}/chunk",
        ): "200",
        (
            "post",
            "/v1/projects/{project_id}/workspace-uploads/{upload_id}/finalize",
        ): "201",
        ("post", "/v1/runs"): "202",
        ("post", "/v1/runs/{run_id}/cancel"): "202",
        ("post", "/v1/runs/{run_id}/retry"): "202",
        ("post", "/v1/services/{service_id}/restart"): "202",
        ("post", "/v1/diagnostics"): "202",
    }
    for (method, path), status in expected.items():
        success_statuses = {
            code for code in openapi["paths"][path][method]["responses"] if code.startswith("2")
        }
        assert success_statuses == {status}, (method, path)


@pytest.mark.parametrize(
    "event_name",
    [
        "artifact.updated.v1",
        "log.appended.v1",
        "revision.successor_transition_updated.v1",
        "revision.activated.v1",
    ],
)
def test_sse_schema_has_remote_resource_change_events(event_name: str) -> None:
    schema_text = json.dumps(build_events_schema_document(), sort_keys=True)
    assert event_name in schema_text
    assert "ResourceChangeIdentityV1" in schema_text
