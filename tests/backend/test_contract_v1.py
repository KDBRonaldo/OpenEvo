from __future__ import annotations

import hashlib
import inspect
import json
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

import openevo.backend.contracts.v1.app as contract_app_module
from openevo.backend.contracts.v1.app import create_core_control_contract_app
from openevo.backend.contracts.v1.models import (
    ArtifactContentV1,
    ArtifactDiffV1,
    ArtifactPageV1,
    CapabilitiesResponseV1,
    DiagnosticsRequestV1,
    EnvironmentCheckV1,
    EventEnvelopeV1,
    ExecutionMode,
    ImmutableSnapshotRefV1,
    LogPageV1,
    OperationV1,
    ModelPreparationV1,
    ParametricMemoryArtifactSummaryV1,
    ProjectCreateV1,
    ProjectPageV1,
    ProjectPatchV1,
    ProjectSpecV1,
    ProjectV1,
    ReachableRequiredRevisionRefV1,
    RevisionV1,
    RevisionPageV1,
    RevisionTransitionState,
    RunCreateV1,
    RunContextV1,
    RunPageV1,
    RunSummaryV1,
    RunTimelinePageV1,
    RunV1,
    SseFrameV1,
    ServiceSummaryV1,
    ServicePageV1,
    StrongETag,
    TaskSpecV1,
    WorkspaceArchiveDeclarationV1,
    WorkspaceUploadChunkV1,
    WorkspaceUploadFinalizeResponseV1,
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
    ("GET", "/v1/projects/{project_id}/artifacts/{artifact_id}"),
    ("GET", "/v1/projects/{project_id}/artifacts/{artifact_id}/content"),
    ("GET", "/v1/projects/{project_id}/artifacts/{artifact_id}/diff"),
    ("GET", "/v1/services"),
    ("GET", "/v1/services/{service_id}"),
    ("POST", "/v1/services/{service_id}/restart"),
    ("GET", "/v1/services/{service_id}/logs"),
    ("GET", "/v1/operations/{operation_id}"),
    ("POST", "/v1/operations/{operation_id}/cancel"),
    ("GET", "/v1/logs/{logs_ref}"),
    ("POST", "/v1/diagnostics"),
    ("GET", "/v1/diagnostics/{diagnostic_id}"),
    ("DELETE", "/v1/diagnostics/{diagnostic_id}"),
    ("POST", "/v1/maintenance/cache-cleanup"),
    ("GET", "/v1/events"),
}


def test_provider_route_iteration_recurses_deferred_included_router() -> None:
    app = FastAPI()
    router = APIRouter(prefix="/v1")

    @app.get("/version", operation_id="topLevelOperation")
    def top_level() -> None:
        return None

    @router.get("/status", operation_id="nestedOperation")
    def nested() -> None:
        return None

    class DeferredIncludedRouter:
        original_router = router

    routes = [app.routes[-1], DeferredIncludedRouter()]

    assert {route.operation_id for route in contract_app_module._iter_api_routes(routes)} == {
        "topLevelOperation",
        "nestedOperation",
    }


def test_provider_binding_preserves_frozen_endpoint_signatures() -> None:
    class Provider:
        def authenticate(self, _authorization_values: tuple[bytes, ...]) -> bool:
            return True

        def invoke(self, _operation_id: str, _arguments: dict[str, object]) -> object:
            raise AssertionError("sync provider dispatch is not used")

        async def invoke_async(self, _operation_id: str, _arguments: dict[str, object]) -> object:
            raise AssertionError("provider dispatch is not part of this test")

    contract_app = create_core_control_contract_app()
    provider_app = create_core_control_contract_app(Provider())
    contract_routes = {
        route.operation_id: route
        for route in contract_app_module._iter_api_routes(contract_app.routes)
    }
    provider_routes = {
        route.operation_id: route
        for route in contract_app_module._iter_api_routes(provider_app.routes)
    }

    assert provider_routes.keys() == contract_routes.keys()
    for operation_id, route in provider_routes.items():
        assert inspect.signature(route.endpoint) == inspect.signature(
            contract_routes[operation_id].endpoint
        )


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


def _revision() -> dict[str, Any]:
    transition = _transition()
    transition.update(
        {
            "state": "active",
            "progress_completed": 6,
            "progress_total": 6,
            "message": "The successor is active.",
        }
    )
    return {
        "schema_version": "1",
        "revision": _revision_ref(),
        "status": "active",
        "predecessor_revision": transition["predecessor_revision"],
        "project_snapshot": _snapshot_ref("project", "project-snapshot-2", "4"),
        "task_snapshot": _snapshot_ref("task", "task-snapshot-2", "5"),
        "workspace_snapshot": _snapshot_ref("workspace", "workspace-snapshot-2", "6"),
        "registry_digest": "a" * 64,
        "transition": transition,
        "created_at": "2026-07-14T00:00:00Z",
        "updated_at": "2026-07-14T00:00:06Z",
        "activated_at": "2026-07-14T00:00:06Z",
        "error": None,
        "etag": '"' + "7" * 64 + '"',
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
        "admitted_at": None,
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
        "content_sha256": "c" * 64,
        "byte_size": 2560,
        "format": "openevo_deterministic_tar_v1",
        "entry_count": 2,
        "extracted_byte_size": 100,
        "policy": {
            "media_type": "application/vnd.openevo.workspace-tar",
            "tar_format": "posix_ustar",
            "entry_types": "regular_files_and_directories",
            "path_policy": "utf8_nfc_posix_relative_ustar_split_v1",
            "entry_order": "header_path_byte_lexicographic_parents_first",
            "metadata_policy": "uid_gid_zero_names_empty_mtime_zero",
            "header_policy": "posix_ustar_canonical_header_v1",
            "body_policy": "zero_pad_to_512_bytes",
            "terminator_policy": "two_zero_blocks_no_trailing_bytes",
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
            "max_path_bytes": 256,
            "max_file_bytes": 0o77777777777,
            "max_extracted_bytes": 16 * 1024 * 1024 * 1024,
        },
    }


def test_openapi_snapshot_is_exactly_rebuildable() -> None:
    rebuilt = canonical_json_bytes(build_openapi_document())
    assert OPENAPI_SNAPSHOT_PATH.read_bytes() == rebuilt
    assert hashlib.sha256(rebuilt).hexdigest() == openapi_sha256()
    assert openapi_sha256() == ("0553a38f229c4fe091b29c609c7557e12d0d30354170d19ba8377da04469ee48")


def test_event_schema_snapshot_is_exactly_rebuildable() -> None:
    rebuilt = canonical_json_bytes(build_events_schema_document())
    assert EVENTS_SCHEMA_SNAPSHOT_PATH.read_bytes() == rebuilt
    assert hashlib.sha256(rebuilt).hexdigest() == events_schema_sha256()
    assert events_schema_sha256() == (
        "48678a1054cc205ff82d97fd38cf76fccf9ad84ea790c167af3bbb1fa52f1f65"
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


def test_capability_support_axes_preserve_framework_three_state_values() -> None:
    schema = build_openapi_document()["components"]["schemas"]
    assert set(schema["MethodSupportOverall"]["enum"]) == {
        "supported",
        "unsupported",
        "unavailable",
    }
    assert set(schema["SupportState"]["enum"]) == {
        "supported",
        "unsupported",
        "unavailable",
    }
    support = schema["MethodSupport"]["properties"]
    for axis in ("execution", "capture", "harness", "runtime"):
        assert support[axis] == {"$ref": "#/components/schemas/AxisSupport"}


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


def test_project_spec_scopes_codex_reasoning_effort_to_subscription() -> None:
    subscription = _json_model(
        ProjectSpecV1,
        {
            "execution_mode": "codex_subscription_transcript",
            "capture_mode": "transcript",
            "harness_id": "codex",
            "agent_model_ref": "gpt-5.3-codex-spark",
            "reasoning_effort": "xhigh",
            "evolution": {"targets": {}},
        },
    )
    assert subscription.reasoning_effort == "xhigh"
    assert subscription.model_dump(mode="json")["reasoning_effort"] == "xhigh"

    legacy_compatible = _json_model(
        ProjectSpecV1,
        {
            "execution_mode": "codex_subscription_transcript",
            "capture_mode": "transcript",
            "harness_id": "codex",
            "agent_model_ref": "gpt-5.3-codex-spark",
            "evolution": {"targets": {}},
        },
    )
    assert legacy_compatible.reasoning_effort is None
    assert "reasoning_effort" not in legacy_compatible.model_dump(mode="json")

    with pytest.raises(ValidationError, match="reasoning_effort"):
        _json_model(
            ProjectSpecV1,
            {
                "execution_mode": "self-deployed",
                "capture_mode": "transcript",
                "harness_id": "codex",
                "agent_model_ref": "openai/gpt-oss-20b",
                "reasoning_effort": "high",
                "evolution": {"targets": {}},
            },
        )

    for agent_model_ref in (
        "gpt-5",
        "openai/gpt-5",
        "anthropic/gpt-5",
        "google/gpt-5",
        "gcp/google/gpt-5",
    ):
        with pytest.raises(ValidationError, match="bare gpt-5 is unsupported"):
            _json_model(
                ProjectSpecV1,
                {
                    "execution_mode": "codex_subscription_transcript",
                    "capture_mode": "transcript",
                    "harness_id": "codex",
                    "agent_model_ref": agent_model_ref,
                    "evolution": {"targets": {}},
                },
            )

    for historical_model_ref in (
        "gpt-5",
        "openai/gpt-5",
        "anthropic/gpt-5",
        "google/gpt-5",
        "gcp/google/gpt-5",
    ):
        historical = ProjectSpecV1.model_validate_json(
            json.dumps(
                {
                    "execution_mode": "codex_subscription_transcript",
                    "capture_mode": "transcript",
                    "harness_id": "codex",
                    "agent_model_ref": historical_model_ref,
                    "evolution": {"targets": {}},
                }
            ),
            strict=True,
            context={"_openevo_historical_codex_model_recovery": True},
        )
        assert historical.agent_model_ref == historical_model_ref

    for agent_model_ref in (
        "openai/",
        "a" * 129,
        "gpt-5.5 alpha",
        "gpt-5.5-\u4e2d\u6587",
    ):
        with pytest.raises(ValidationError, match="invalid final Codex CLI model value"):
            _json_model(
                ProjectSpecV1,
                {
                    "execution_mode": "codex_subscription_transcript",
                    "capture_mode": "transcript",
                    "harness_id": "codex",
                    "agent_model_ref": agent_model_ref,
                    "evolution": {"targets": {}},
                },
            )

        with pytest.raises(ValidationError, match="invalid final Codex CLI model value"):
            ProjectSpecV1.model_validate_json(
                json.dumps(
                    {
                        "execution_mode": "codex_subscription_transcript",
                        "capture_mode": "transcript",
                        "harness_id": "codex",
                        "agent_model_ref": agent_model_ref,
                        "evolution": {"targets": {}},
                    }
                ),
                strict=True,
                context={"_openevo_historical_codex_model_recovery": True},
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

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _json_model(TaskSpecV1, {**task, "content_ref": _content_ref("unissued")})


@pytest.mark.parametrize("field", ["name", "spec", "task", "workspace"])
def test_project_patch_rejects_explicit_null_for_non_nullable_fields(field: str) -> None:
    with pytest.raises(ValidationError, match="must not be null"):
        _json_model(
            ProjectPatchV1,
            {
                "schema_version": "1",
                "description": "A valid simultaneous change.",
                field: None,
            },
        )


def test_project_patch_preserves_nullable_description_clear_semantics() -> None:
    patch = _json_model(ProjectPatchV1, {"schema_version": "1", "description": None})
    assert patch.description is None
    assert "description" in patch.model_fields_set


def test_project_patch_openapi_fields_are_optional_with_exact_nullability() -> None:
    schema = build_openapi_document()["components"]["schemas"]["ProjectPatchV1"]
    assert not (
        {"name", "description", "spec", "task", "workspace"} & set(schema.get("required", []))
    )

    for field in ("name", "spec", "task", "workspace"):
        assert {"type": "null"} not in schema["properties"][field].get("anyOf", [])
    assert {"type": "null"} in schema["properties"]["description"]["anyOf"]


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


def test_imported_workspace_request_declares_content_without_a_core_id() -> None:
    create = {
        "schema_version": "1",
        "name": "Imported science",
        "description": None,
        "spec": {
            "execution_mode": "self-deployed",
            "capture_mode": "transcript",
            "harness_id": "codex",
            "agent_model_ref": "openai/gpt-oss-20b",
            "evolution": {"targets": {}},
        },
        "task": {"title": "Analyze", "objective": "Analyze the imported workspace."},
        "workspace": {
            "kind": "native_folder_snapshot",
            "display_name": "Imported workspace",
            "archive": _workspace_archive(),
        },
    }
    assert _json_model(ProjectCreateV1, create).workspace.archive.byte_size == 2560

    invented = json.loads(json.dumps(create))
    invented["workspace"]["archive"]["content_id"] = "caller-invented"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _json_model(ProjectCreateV1, invented)


def test_workspace_archive_format_and_extraction_policy_are_frozen() -> None:
    archive = _json_model(WorkspaceArchiveDeclarationV1, _workspace_archive())
    assert archive.format.value == "openevo_deterministic_tar_v1"
    assert archive.policy.allow_symlinks is False
    assert archive.policy.allow_hardlinks is False
    assert archive.policy.allow_devices is False
    assert archive.policy.max_entries == 100_000
    assert archive.policy.max_extracted_bytes == 16 * 1024 * 1024 * 1024
    assert archive.policy.max_file_bytes == 0o77777777777
    assert archive.policy.max_path_bytes == 256

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
            _json_model(WorkspaceArchiveDeclarationV1, unsafe)

    unsupported = _workspace_archive()
    unsupported["format"] = "zip"
    with pytest.raises(ValidationError):
        _json_model(WorkspaceArchiveDeclarationV1, unsupported)

    impossible_size = _workspace_archive()
    impossible_size["byte_size"] = 1024
    with pytest.raises(ValidationError, match="too small"):
        _json_model(WorkspaceArchiveDeclarationV1, impossible_size)

    schema = build_openapi_document()["components"]["schemas"]
    assert schema["WorkspaceArchivePolicyV1"]["additionalProperties"] is False
    assert (
        "dot-dot" in schema["WorkspaceArchivePolicyV1"]["properties"]["path_policy"]["description"]
    )
    assert (
        "rightmost"
        in schema["WorkspaceArchivePolicyV1"]["properties"]["path_policy"]["description"]
    )
    assert (
        "0000644\\0"
        in schema["WorkspaceArchivePolicyV1"]["properties"]["header_policy"]["description"]
    )
    assert (
        "six octal digits"
        in schema["WorkspaceArchivePolicyV1"]["properties"]["header_policy"]["description"]
    )
    assert (
        "regular-file and directory"
        in schema["WorkspaceArchivePolicyV1"]["properties"]["entry_types"]["description"]
    )
    assert schema["WorkspaceArchivePolicyV1"]["properties"]["max_path_depth"]["const"] == 32
    assert set(schema["WorkspaceUploadCreateV1"]["properties"]) == {
        "schema_version",
        "archive",
        "base_workspace_snapshot",
        "project_snapshot",
    }
    assert "content_id" not in json.dumps(schema["WorkspaceUploadCreateV1"], sort_keys=True)


def test_workspace_finalize_closes_upload_and_project_cas() -> None:
    openapi = build_openapi_document()
    create = openapi["paths"]["/v1/projects/{project_id}/workspace-uploads"]["post"]
    finalize = openapi["paths"][
        "/v1/projects/{project_id}/workspace-uploads/{upload_id}/finalize"
    ]["post"]
    assert {parameter["name"] for parameter in create["parameters"]} >= {
        "project_id",
        "If-Match",
        "Idempotency-Key",
    }
    assert {parameter["name"] for parameter in finalize["parameters"]} >= {
        "project_id",
        "upload_id",
        "If-Match",
        "If-Project-Match",
        "Idempotency-Key",
    }
    project_match = next(
        parameter
        for parameter in finalize["parameters"]
        if parameter["name"] == "If-Project-Match"
    )
    assert project_match["schema"]["pattern"] == r'^"[0-9a-f]{64}"$'
    assert project_match["required"] is True
    assert "upload.project_etag" in project_match["description"]
    assert "upload.project_snapshot" in project_match["description"]
    assert _response_schema_name(finalize) == "WorkspaceUploadFinalizeResponseV1"
    finalize_request = openapi["components"]["schemas"]["WorkspaceUploadFinalizeV1"]
    assert set(finalize_request["properties"]) == {"schema_version", "content_sha256"}
    response_schema = openapi["components"]["schemas"]["WorkspaceUploadFinalizeResponseV1"]
    assert {"project", "upload", "publication"} <= set(response_schema["properties"])
    assert WorkspaceUploadFinalizeResponseV1 is not None

    workspace_snapshot = _snapshot_ref("workspace", "workspace-snapshot-2", "6")
    content_ref = {
        "content_id": "content-1",
        "sha256": "c" * 64,
        "byte_size": 2560,
    }
    publication = {
        "archive": _workspace_archive(),
        "content_ref": content_ref,
        "workspace_snapshot": workspace_snapshot,
        "published_at": "2026-07-14T00:00:02Z",
    }
    upload = {
        "schema_version": "1",
        "id": "upload-1",
        "project_id": "project-1",
        "status": "finalized",
        "accepted_offset": 2560,
        "project_snapshot": _snapshot_ref("project", "project-snapshot-1", "1"),
        "project_etag": '"' + "a" * 64 + '"',
        "archive": _workspace_archive(),
        "base_workspace_snapshot": None,
        "publication": publication,
        "created_at": "2026-07-14T00:00:00Z",
        "updated_at": "2026-07-14T00:00:02Z",
        "etag": '"' + "b" * 64 + '"',
    }
    project = {
        "id": "project-1",
        "name": "Imported science",
        "description": None,
        "status": "draft",
        "execution_mode": "self-deployed",
        "workspace_kind": "native_folder_snapshot",
        "current_project_snapshot": _snapshot_ref("project", "project-snapshot-2", "4"),
        "current_task_snapshot": _snapshot_ref("task", "task-snapshot-1", "2"),
        "current_workspace_snapshot": workspace_snapshot,
        "workspace_publication": publication,
        "active_revision": None,
        "registry_digest": None,
        "model_preparation": {
            "model_ref": "openai/gpt-oss-20b",
            "status": "unresolved",
            "downloaded_bytes": None,
            "total_bytes": None,
            "error": None,
            "updated_at": "2026-07-14T00:00:02Z",
        },
        "created_at": "2026-07-14T00:00:00Z",
        "updated_at": "2026-07-14T00:00:02Z",
        "etag": '"' + "c" * 64 + '"',
        "spec": {
            "execution_mode": "self-deployed",
            "capture_mode": "transcript",
            "harness_id": "codex",
            "agent_model_ref": "openai/gpt-oss-20b",
            "evolution": {"targets": {}},
        },
        "task": {"title": "Explore", "objective": "Analyze the imported workspace."},
        "workspace": {
            "kind": "native_folder_snapshot",
            "display_name": "Imported workspace",
            "archive": _workspace_archive(),
        },
    }
    response = {
        "schema_version": "1",
        "project_id": "project-1",
        "upload": upload,
        "publication": publication,
        "project": project,
    }
    assert _json_model(WorkspaceUploadFinalizeResponseV1, response).project.etag == project["etag"]

    stale = json.loads(json.dumps(response))
    stale["project"]["current_project_snapshot"] = upload["project_snapshot"]
    with pytest.raises(ValidationError, match="new project snapshot"):
        _json_model(WorkspaceUploadFinalizeResponseV1, stale)

    unchanged_etag = json.loads(json.dumps(response))
    unchanged_etag["project"]["etag"] = upload["project_etag"]
    with pytest.raises(ValidationError, match="new project ETag"):
        _json_model(WorkspaceUploadFinalizeResponseV1, unchanged_etag)

    split_publication = json.loads(json.dumps(response))
    split_publication["project"]["workspace_publication"]["content_ref"]["content_id"] = (
        "content-other"
    )
    with pytest.raises(ValidationError, match="publication"):
        _json_model(WorkspaceUploadFinalizeResponseV1, split_publication)


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
    "page_model",
    [
        ProjectPageV1,
        RevisionPageV1,
        RunPageV1,
        RunTimelinePageV1,
        LogPageV1,
        ArtifactPageV1,
        ServicePageV1,
    ],
)
def test_every_page_has_more_iff_next_cursor_is_present(page_model: type[Any]) -> None:
    assert (
        _json_model(
            page_model,
            {"schema_version": "1", "items": [], "next_cursor": None, "has_more": False},
        ).has_more
        is False
    )
    assert (
        _json_model(
            page_model,
            {"schema_version": "1", "items": [], "next_cursor": "next", "has_more": True},
        ).has_more
        is True
    )
    for next_cursor, has_more in ((None, True), ("next", False)):
        with pytest.raises(ValidationError, match="if and only if"):
            _json_model(
                page_model,
                {
                    "schema_version": "1",
                    "items": [],
                    "next_cursor": next_cursor,
                    "has_more": has_more,
                },
            )


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
            "admitted_at": "2026-07-14T00:00:01Z",
            "started_at": "2026-07-14T00:00:01Z",
        }
    )
    with pytest.raises(ValidationError, match="finished_at"):
        _json_model(RunSummaryV1, terminal_without_finish)

    unknown_state = _valid_run_summary()
    unknown_state["status"] = "completed"
    with pytest.raises(ValidationError):
        _json_model(RunSummaryV1, unknown_state)

    active_required = _valid_run_summary()
    active_required["required_revision"] = {
        "revision": _revision_ref("revision-6"),
        "reachable_from_revision_id": "revision-6",
        "relation": "active",
    }
    active_required["revision_transition"] = None
    assert _json_model(RunSummaryV1, active_required).revision_transition is None

    missing_successor_transition = _valid_run_summary()
    missing_successor_transition["revision_transition"] = None
    with pytest.raises(ValidationError, match="successor"):
        _json_model(RunSummaryV1, missing_successor_transition)

    cancelled_before_admission = _valid_run_summary()
    cancelled_before_admission.update(
        {
            "status": "cancelled",
            "queued_reason": None,
            "finished_at": "2026-07-14T00:00:01Z",
        }
    )
    assert _json_model(RunSummaryV1, cancelled_before_admission).pinned_revision is None


def test_run_list_shape_contains_current_attempt_error_and_transition() -> None:
    properties = build_openapi_document()["components"]["schemas"]["RunSummaryV1"]["properties"]
    assert {
        "current_attempt",
        "current_error",
        "required_revision",
        "pinned_revision",
        "admitted_at",
        "revision_transition",
        "updated_at",
        "etag",
    } <= set(properties)
    assert properties["attempt_count"]["maximum"] == 100
    assert (
        build_openapi_document()["components"]["schemas"]["AttemptV1"]["properties"]["number"][
            "maximum"
        ]
        == 100
    )


def test_run_detail_closes_attempt_order_status_and_revision_identity() -> None:
    detail = _valid_run_summary()
    detail.update(
        {
            "status": "running",
            "queued_reason": None,
            "current_attempt_id": "attempt-2",
            "current_attempt": {
                "id": "attempt-2",
                "run_id": "run-1",
                "number": 2,
                "status": "running",
                "queued_reason": None,
                "created_at": "2026-07-14T00:00:03Z",
                "updated_at": "2026-07-14T00:00:04Z",
                "started_at": "2026-07-14T00:00:04Z",
                "finished_at": None,
                "error": None,
            },
            "attempt_count": 2,
            "pinned_revision": _revision_ref(),
            "admitted_at": "2026-07-14T00:00:01Z",
            "started_at": "2026-07-14T00:00:01Z",
            "attempts": [
                {
                    "id": "attempt-1",
                    "run_id": "run-1",
                    "number": 1,
                    "status": "cancelled",
                    "queued_reason": None,
                    "created_at": "2026-07-14T00:00:01Z",
                    "updated_at": "2026-07-14T00:00:02Z",
                    "started_at": None,
                    "finished_at": "2026-07-14T00:00:02Z",
                    "error": None,
                },
            ],
        }
    )
    detail["revision_transition"].update(
        {"state": "active", "progress_completed": 6, "progress_total": 6}
    )
    detail["attempts"].append(detail["current_attempt"])
    assert _json_model(RunV1, detail).attempt_count == 2

    wrong_status = json.loads(json.dumps(detail))
    wrong_status["current_attempt"]["status"] = "preparing"
    wrong_status["attempts"][-1]["status"] = "preparing"
    with pytest.raises(ValidationError, match="statuses differ"):
        _json_model(RunV1, wrong_status)

    wrong_order = json.loads(json.dumps(detail))
    wrong_order["attempts"][0]["number"] = 2
    with pytest.raises(ValidationError, match="contiguous"):
        _json_model(RunV1, wrong_order)

    cross_wired_current = json.loads(json.dumps(detail))
    cross_wired_current["attempts"][-1]["updated_at"] = "2026-07-14T00:00:05Z"
    with pytest.raises(ValidationError, match="equal the last attempt"):
        _json_model(RunV1, cross_wired_current)


def test_revision_resource_binds_nested_transition_identity() -> None:
    assert _json_model(RevisionV1, _revision()).revision.id == "revision-7"
    mismatched = _revision()
    mismatched["transition"]["successor_revision"] = {
        **_revision_ref("revision-other"),
        "generation": 7,
    }
    with pytest.raises(ValidationError, match="bind predecessor and revision"):
        _json_model(RevisionV1, mismatched)


@pytest.mark.parametrize(
    ("status", "transition_state"),
    [
        ("queued", "not_started"),
        ("preparing", "materializing"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
    ],
)
def test_revision_activated_event_rejects_non_active_revision_payloads(
    status: str, transition_state: str
) -> None:
    revision = _revision()
    revision.update({"status": status, "activated_at": None})
    revision["transition"].update(
        {
            "state": transition_state,
            "progress_completed": 0 if status == "queued" else 4,
            "message": f"The successor is {status}.",
        }
    )
    if status == "failed":
        error = {
            "schema_version": "1",
            "request_id": "request-1",
            "code": "revision_failed",
            "http_status": 500,
            "message": "Revision preparation failed.",
            "severity": "blocking",
            "category": "internal",
            "retryable": False,
            "repair_action": "openevo_can_retry",
            "next_action": "Retry the revision preparation.",
            "details": {"field_issues": [], "conflicts": []},
            "logs_ref": None,
        }
        revision["error"] = error
        revision["transition"]["error"] = error

    event = {
        "schema_version": "1",
        "id": "event-1",
        "sequence": 1,
        "occurred_at": "2026-07-14T00:00:07Z",
        "event": "revision.activated.v1",
        "change": {
            "change_id": "change-1",
            "resource_type": "revision",
            "resource_id": revision["revision"]["id"],
            "parent_resource_type": "project",
            "parent_resource_id": revision["revision"]["project_id"],
            "resource_etag": revision["etag"],
            "content_sha256": None,
        },
        "payload": revision,
    }
    with pytest.raises(ValidationError):
        EventEnvelopeV1.model_validate_json(json.dumps(event))


def test_revision_activated_event_schema_requires_active_revision_status() -> None:
    definitions = build_events_schema_document()["$defs"]
    payload_ref = definitions["RevisionActivatedEventV1"]["properties"]["payload"]["$ref"]
    payload_schema = definitions[payload_ref.rsplit("/", 1)[-1]]
    assert payload_schema["properties"]["status"]["const"] == "active"


def test_cancelled_revision_requires_cancelled_transition() -> None:
    cancelled = _revision()
    cancelled.update({"status": "cancelled", "activated_at": None})
    cancelled["transition"].update(
        {
            "state": "cancelled",
            "progress_completed": 4,
            "message": "The successor transition was cancelled.",
        }
    )
    assert _json_model(RevisionV1, cancelled).transition.state.value == "cancelled"

    still_preparing = json.loads(json.dumps(cancelled))
    still_preparing["transition"]["state"] = "materializing"
    with pytest.raises(ValidationError, match="cancelled transition"):
        _json_model(RevisionV1, still_preparing)

    wrong_revision = _revision()
    wrong_revision["transition"]["state"] = "cancelled"
    with pytest.raises(ValidationError, match="only a cancelled revision"):
        _json_model(RevisionV1, wrong_revision)


def test_queued_run_and_context_never_fabricate_a_pinned_revision() -> None:
    run = _json_model(RunSummaryV1, _valid_run_summary())
    assert run.pinned_revision is None

    fabricated = _valid_run_summary()
    fabricated["pinned_revision"] = _revision_ref()
    with pytest.raises(ValidationError, match="admitted_at"):
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


def test_admitted_terminal_run_and_context_retain_pin() -> None:
    cancelled = _valid_run_summary()
    cancelled.update(
        {
            "status": "cancelled",
            "queued_reason": None,
            "admitted_at": "2026-07-14T00:00:01Z",
            "pinned_revision": _revision_ref(),
            "finished_at": "2026-07-14T00:00:02Z",
        }
    )
    cancelled["revision_transition"].update(
        {"state": "active", "progress_completed": 6, "progress_total": 6}
    )
    parsed = _json_model(RunSummaryV1, cancelled)
    assert parsed.pinned_revision == parsed.required_revision.revision

    missing_pin = {**cancelled, "pinned_revision": None}
    with pytest.raises(ValidationError, match="admitted_at"):
        _json_model(RunSummaryV1, missing_pin)

    context = {
        "schema_version": "1",
        **cancelled,
        "run_id": "run-1",
        "token_level_metrics_available": False,
        "artifacts": [],
        "adapters": [],
    }
    context.pop("id")
    assert _json_model(RunContextV1, context).pinned_revision is not None


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


def test_sse_wire_frame_binds_id_event_and_stable_change_identity() -> None:
    data = {
        "schema_version": "1",
        "id": "event-1",
        "sequence": 1,
        "occurred_at": "2026-07-14T00:00:00Z",
        "event": "run.updated.v1",
        "change": {
            "change_id": "change-1",
            "resource_type": "run",
            "resource_id": "run-1",
            "parent_resource_type": "project",
            "parent_resource_id": "project-1",
            "resource_etag": _valid_run_summary()["etag"],
            "content_sha256": None,
        },
        "payload": _valid_run_summary(),
    }
    frame = {"id": "event-1", "event": "run.updated.v1", "data": data}
    assert _json_model(SseFrameV1, frame).data.root.change.change_id == "change-1"

    reemitted = json.loads(json.dumps(frame))
    reemitted["id"] = "event-2"
    reemitted["data"]["id"] = "event-2"
    reemitted["data"]["sequence"] = 2
    assert _json_model(SseFrameV1, reemitted).data.root.change.change_id == "change-1"

    mismatched = json.loads(json.dumps(frame))
    mismatched["event"] = "project.updated.v1"
    with pytest.raises(ValidationError, match="wire id and event"):
        _json_model(SseFrameV1, mismatched)


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


def test_sse_openapi_response_uses_the_frozen_wire_frame_schema() -> None:
    openapi = build_openapi_document()
    operation = openapi["paths"]["/v1/events"]["get"]
    assert operation["x-sse-delivery"] == "at-least-once"
    assert operation["x-sse-heartbeat-seconds"] == 15
    assert operation["x-sse-replay"] == "bounded"
    assert operation["responses"]["200"]["content"] == {
        "text/event-stream": {"schema": {"$ref": "#/components/schemas/SseFrameV1"}}
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
    assert "accepted_offset" in schema["properties"]["offset"]["description"]

    beyond_end = {**payload, "offset": 16 * 1024 * 1024 * 1024}
    with pytest.raises(ValidationError, match="16 GiB"):
        _json_model(WorkspaceUploadChunkV1, beyond_end)


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
        "artifact_content_sha256": "a" * 64,
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
    old_document = {
        "artifact_id": "artifact-1",
        "artifact_content_sha256": "1" * 64,
        "document_id": "doc-1",
        "relative_path": "memory.md",
        "content_sha256": "a" * 64,
    }
    new_document = {
        "artifact_id": "artifact-2",
        "artifact_content_sha256": "2" * 64,
        "document_id": "doc-2",
        "relative_path": "memory.md",
        "content_sha256": "b" * 64,
    }
    payload = {
        "schema_version": "1",
        "artifact_id": "artifact-2",
        "artifact_content_sha256": "2" * 64,
        "previous_artifact_id": "artifact-1",
        "previous_artifact_content_sha256": "1" * 64,
        "document_changes": [
            {
                "kind": "modified",
                "old_document": old_document,
                "new_document": new_document,
                "hunks": [
                    {
                        "old_document": old_document,
                        "new_document": new_document,
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
            }
        ],
        "total_document_changes": 1,
        "total_hunks": 1,
        "total_lines": 1,
        "truncated": False,
    }
    diff = _json_model(ArtifactDiffV1, payload)
    assert diff.document_changes[0].hunks[0].lines[0].kind.value == "context"

    wrong_side = json.loads(json.dumps(payload))
    wrong_side["document_changes"][0]["old_document"]["artifact_id"] = "artifact-2"
    wrong_side["document_changes"][0]["hunks"][0]["old_document"]["artifact_id"] = "artifact-2"
    with pytest.raises(ValidationError, match="old document"):
        _json_model(ArtifactDiffV1, wrong_side)

    unsafe_path = json.loads(json.dumps(payload))
    unsafe_path["document_changes"][0]["new_document"]["relative_path"] = "../secret"
    with pytest.raises(ValidationError, match="unsafe path"):
        _json_model(ArtifactDiffV1, unsafe_path)

    lost_identity = json.loads(json.dumps(payload))
    lost_hunk = lost_identity["document_changes"][0]["hunks"][0]
    lost_hunk.update({"new_document": None, "new_start": 0, "new_count": 0})
    lost_hunk["lines"] = [
        {
            "kind": "removed",
            "old_line_number": 1,
            "new_line_number": None,
            "text": "removed",
        }
    ]
    with pytest.raises(ValidationError, match="document identity"):
        _json_model(ArtifactDiffV1, lost_identity)


def test_artifact_diff_document_change_union_allows_empty_add_and_remove() -> None:
    empty_digest = hashlib.sha256(b"").hexdigest()
    old_document = {
        "artifact_id": "artifact-1",
        "artifact_content_sha256": "1" * 64,
        "document_id": "old-empty",
        "relative_path": "removed.md",
        "content_sha256": empty_digest,
    }
    new_document = {
        "artifact_id": "artifact-2",
        "artifact_content_sha256": "2" * 64,
        "document_id": "new-empty",
        "relative_path": "added.md",
        "content_sha256": empty_digest,
    }
    payload = {
        "schema_version": "1",
        "artifact_id": "artifact-2",
        "artifact_content_sha256": "2" * 64,
        "previous_artifact_id": "artifact-1",
        "previous_artifact_content_sha256": "1" * 64,
        "document_changes": [
            {"kind": "removed", "old_document": old_document, "hunks": []},
            {"kind": "added", "new_document": new_document, "hunks": []},
        ],
        "total_document_changes": 2,
        "total_hunks": 0,
        "total_lines": 0,
        "truncated": False,
    }
    parsed = _json_model(ArtifactDiffV1, payload)
    assert [change.kind.value for change in parsed.document_changes] == ["removed", "added"]

    renamed = json.loads(json.dumps(payload))
    renamed["document_changes"] = [
        {
            "kind": "renamed",
            "old_document": old_document,
            "new_document": new_document,
            "hunks": [],
        }
    ]
    renamed["total_document_changes"] = 1
    assert _json_model(ArtifactDiffV1, renamed).document_changes[0].kind.value == "renamed"


def test_model_preparation_is_present_iff_resource_is_model_backed() -> None:
    service = {
        "id": "service-1",
        "display_name": "Inference",
        "kind": "inference",
        "status": "running",
        "restartable": True,
        "status_message": None,
        "error": None,
        "model_preparation": None,
        "updated_at": "2026-07-14T00:00:00Z",
        "observed_at": "2026-07-14T00:00:00Z",
        "etag": '"' + "e" * 64 + '"',
    }
    with pytest.raises(ValidationError, match="inference"):
        _json_model(ServiceSummaryV1, service)

    preparation = {
        "model_ref": "openai/gpt-oss-20b",
        "status": "unresolved",
        "downloaded_bytes": None,
        "total_bytes": None,
        "error": None,
        "updated_at": "2026-07-14T00:00:00Z",
    }
    service["model_preparation"] = preparation
    assert _json_model(ServiceSummaryV1, service).kind.value == "inference"
    service["kind"] = "control"
    with pytest.raises(ValidationError, match="inference"):
        _json_model(ServiceSummaryV1, service)

    check = {
        "id": "check-1",
        "kind": "model_service",
        "status": "blocking",
        "message": "Model is unresolved.",
        "repair_action": "openevo_can_install",
        "next_action": "Prepare the model.",
        "logs_ref": None,
        "model_preparation": None,
    }
    with pytest.raises(ValidationError, match="model-service"):
        _json_model(EnvironmentCheckV1, check)
    check["model_preparation"] = preparation
    assert _json_model(EnvironmentCheckV1, check).kind.value == "model_service"
    check["kind"] = "network"
    with pytest.raises(ValidationError, match="model-service"):
        _json_model(EnvironmentCheckV1, check)


def test_model_preparation_progress_is_closed_by_status() -> None:
    base = {
        "model_ref": "openai/gpt-oss-20b",
        "status": "ready",
        "downloaded_bytes": 100,
        "total_bytes": 100,
        "error": None,
        "updated_at": "2026-07-14T00:00:00Z",
    }
    assert _json_model(ModelPreparationV1, base).status.value == "ready"

    incomplete_ready = {**base, "downloaded_bytes": 99}
    with pytest.raises(ValidationError, match="complete download"):
        _json_model(ModelPreparationV1, incomplete_ready)

    missing_downloaded = {**base, "downloaded_bytes": None}
    with pytest.raises(ValidationError, match="appear together"):
        _json_model(ModelPreparationV1, missing_downloaded)

    downloading = {**base, "status": "downloading", "downloaded_bytes": 99}
    assert _json_model(ModelPreparationV1, downloading).downloaded_bytes == 99
    with pytest.raises(ValidationError, match="ready status"):
        _json_model(ModelPreparationV1, {**downloading, "downloaded_bytes": 100})

    unresolved_progress = {**base, "status": "unresolved"}
    with pytest.raises(ValidationError, match="unresolved"):
        _json_model(ModelPreparationV1, unresolved_progress)


@pytest.mark.parametrize(
    ("payload", "valid"),
    [
        ({"schema_version": "1", "scopes": ["environment"], "target": {"kind": "global"}}, True),
        (
            {
                "schema_version": "1",
                "scopes": ["project"],
                "target": {"kind": "project", "project_id": "project-1"},
            },
            True,
        ),
        (
            {
                "schema_version": "1",
                "scopes": ["run"],
                "target": {
                    "kind": "run",
                    "project_id": "project-1",
                    "run_id": "run-1",
                },
            },
            True,
        ),
        (
            {
                "schema_version": "1",
                "scopes": ["run"],
                "target": {"kind": "run", "run_id": "run-1"},
            },
            False,
        ),
        (
            {
                "schema_version": "1",
                "scopes": ["services"],
                "target": {"kind": "project", "project_id": "project-1"},
            },
            False,
        ),
    ],
)
def test_diagnostic_scopes_require_exact_resource_identity(
    payload: dict[str, Any], valid: bool
) -> None:
    if valid:
        _json_model(DiagnosticsRequestV1, payload)
    else:
        with pytest.raises(ValidationError):
            _json_model(DiagnosticsRequestV1, payload)


def test_diagnostic_target_identity_is_closed_in_openapi() -> None:
    schemas = build_openapi_document()["components"]["schemas"]
    assert set(schemas["GlobalDiagnosticTargetV1"]["properties"]) == {"kind"}
    assert set(schemas["ProjectDiagnosticTargetV1"]["required"]) == {
        "kind",
        "project_id",
    }
    assert "run_id" not in schemas["ProjectDiagnosticTargetV1"]["properties"]
    assert set(schemas["RunDiagnosticTargetV1"]["required"]) == {
        "kind",
        "project_id",
        "run_id",
    }


def test_every_non_heartbeat_event_binds_change_and_parent_identity() -> None:
    run = _valid_run_summary()
    event = {
        "schema_version": "1",
        "id": "event-1",
        "sequence": 1,
        "occurred_at": "2026-07-14T00:00:00Z",
        "event": "run.updated.v1",
        "change": {
            "change_id": "change-1",
            "resource_type": "run",
            "resource_id": "run-1",
            "parent_resource_type": "project",
            "parent_resource_id": "project-1",
            "resource_etag": run["etag"],
            "content_sha256": None,
        },
        "payload": run,
    }
    EventEnvelopeV1.model_validate_json(json.dumps(event))
    event["change"]["resource_id"] = "run-2"
    with pytest.raises(ValidationError, match="change"):
        EventEnvelopeV1.model_validate_json(json.dumps(event))


def test_each_sse_event_validator_rejects_a_mismatched_change_resource() -> None:
    etag = '"' + "e" * 64 + '"'
    service = {
        "id": "service-1",
        "display_name": "Core",
        "kind": "control",
        "status": "running",
        "restartable": True,
        "status_message": None,
        "error": None,
        "model_preparation": None,
        "updated_at": "2026-07-14T00:00:00Z",
        "observed_at": "2026-07-14T00:00:00Z",
        "etag": etag,
    }
    project = {
        "id": "project-1",
        "name": "Science",
        "description": None,
        "status": "draft",
        "execution_mode": "self-deployed",
        "workspace_kind": "scratch",
        "current_project_snapshot": _snapshot_ref("project", "project-snapshot-1", "1"),
        "current_task_snapshot": _snapshot_ref("task", "task-snapshot-1", "2"),
        "current_workspace_snapshot": _snapshot_ref("workspace", "workspace-snapshot-1", "3"),
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
        "etag": etag,
    }
    timeline = {
        "id": "timeline-1",
        "run_id": "run-1",
        "attempt_id": None,
        "sequence": 1,
        "service_id": "service-1",
        "phase": "admission",
        "status": "running",
        "title": "Admission",
        "message": "Admission is running.",
        "occurred_at": "2026-07-14T00:00:00Z",
        "artifact_ids": [],
        "content_sha256": "1" * 64,
        "error": None,
    }
    diagnostic = {
        "schema_version": "1",
        "id": "diagnostic-1",
        "status": "queued",
        "scopes": ["environment"],
        "target": {"kind": "global"},
        "checks": [],
        "created_at": "2026-07-14T00:00:00Z",
        "updated_at": "2026-07-14T00:00:00Z",
        "observed_at": "2026-07-14T00:00:00Z",
        "finished_at": None,
        "error": None,
        "etag": etag,
    }
    artifact = {
        "id": "artifact-1",
        "project_id": "project-1",
        "run_id": "run-1",
        "target_id": "text_memory",
        "display_name": "Memory",
        "summary": "A memory artifact.",
        "byte_size": 10,
        "produced_revision": _revision_ref(),
        "membership_revisions": [_revision_ref()],
        "content_sha256": "a" * 64,
        "selected": True,
        "promoted": True,
        "release_enabled": True,
        "compatibility": {
            "execution_modes": ["self-deployed"],
            "harness_ids": ["codex"],
            "base_model_refs": ["openai/gpt-oss-20b"],
        },
        "lineage": {
            "method_id": "memory-method",
            "job_id": "job-1",
            "source_dataset_ids": ["dataset-1"],
            "source_artifact_ids": [],
        },
        "scores": [],
        "created_at": "2026-07-14T00:00:00Z",
        "artifact_type": "text_memory",
        "metadata": {"record_count": 1, "source_dataset_ids": ["dataset-1"]},
    }
    log = {
        "id": "log-1",
        "sequence": 1,
        "occurred_at": "2026-07-14T00:00:00Z",
        "stream": "core",
        "level": "info",
        "message": "Core started.",
        "run_id": None,
        "attempt_id": None,
        "service_id": "service-1",
        "content_sha256": "b" * 64,
    }
    transition = _transition()
    revision_head = {
        "schema_version": "1",
        "project_id": "project-1",
        "active_revision": transition["predecessor_revision"],
        "successor_revision": transition["successor_revision"],
        "transition": transition,
        "updated_at": "2026-07-14T00:00:04Z",
        "etag": etag,
    }
    operation = {
        "schema_version": "1",
        "id": "operation-1",
        "kind": "service_restart",
        "descriptor": {"kind": "service_restart", "cancellable": False},
        "status": "queued",
        "request": {
            "kind": "service_restart",
            "service_id": "service-1",
            "request": {"schema_version": "1", "reason": "Recover the service."},
        },
        "result": None,
        "cancellation": None,
        "logs_ref": "logs-1",
        "created_at": "2026-07-14T00:00:00Z",
        "updated_at": "2026-07-14T00:00:00Z",
        "observed_at": "2026-07-14T00:00:00Z",
        "finished_at": None,
        "error": None,
        "etag": etag,
    }

    cases = [
        (
            "run.updated.v1",
            "run",
            "run-1",
            "project",
            "project-1",
            etag,
            None,
            _valid_run_summary(),
        ),
        (
            "run.timeline_appended.v1",
            "timeline_entry",
            "timeline-1",
            "run",
            "run-1",
            None,
            "1" * 64,
            {"run_id": "run-1", "entry": timeline},
        ),
        ("project.updated.v1", "project", "project-1", None, None, etag, None, project),
        ("service.updated.v1", "service", "service-1", None, None, etag, None, service),
        (
            "diagnostic.updated.v1",
            "diagnostic",
            "diagnostic-1",
            None,
            None,
            etag,
            None,
            diagnostic,
        ),
        (
            "artifact.updated.v1",
            "artifact",
            "artifact-1",
            "project",
            "project-1",
            None,
            "a" * 64,
            artifact,
        ),
        ("log.appended.v1", "log_entry", "log-1", "service", "service-1", None, "b" * 64, log),
        (
            "revision.successor_transition_updated.v1",
            "revision_head",
            "project-1",
            None,
            None,
            etag,
            None,
            revision_head,
        ),
        (
            "revision.activated.v1",
            "revision",
            "revision-7",
            "project",
            "project-1",
            _revision()["etag"],
            None,
            _revision(),
        ),
        (
            "operation.updated.v1",
            "operation",
            "operation-1",
            "service",
            "service-1",
            etag,
            None,
            operation,
        ),
    ]
    for index, (
        event_name,
        resource_type,
        resource_id,
        parent_type,
        parent_id,
        resource_etag,
        content_sha256,
        payload,
    ) in enumerate(cases, start=1):
        event = {
            "schema_version": "1",
            "id": f"event-{index}",
            "sequence": index,
            "occurred_at": "2026-07-14T00:00:00Z",
            "event": event_name,
            "change": {
                "change_id": f"change-{index}",
                "resource_type": resource_type,
                "resource_id": resource_id,
                "parent_resource_type": parent_type,
                "parent_resource_id": parent_id,
                "resource_etag": resource_etag,
                "content_sha256": content_sha256,
            },
            "payload": payload,
        }
        EventEnvelopeV1.model_validate_json(json.dumps(event))
        event["change"]["resource_id"] = "wrong-resource"
        with pytest.raises(ValidationError, match="change"):
            EventEnvelopeV1.model_validate_json(json.dumps(event))


def _response_schema_name(operation: dict[str, Any]) -> str | None:
    for status in ("200", "201", "202"):
        response = operation["responses"].get(status)
        if response is not None:
            return (
                response["content"]["application/json"]["schema"]
                .get("$ref", "")
                .rsplit("/", 1)[-1]
            )
    return None


def test_async_core_actions_are_recoverable_operations() -> None:
    openapi = build_openapi_document()
    for method, path in (
        ("post", "/v1/environment/repair"),
        ("post", "/v1/services/{service_id}/restart"),
        ("post", "/v1/maintenance/cache-cleanup"),
    ):
        operation = openapi["paths"][path][method]
        assert set(code for code in operation["responses"] if code.startswith("2")) == {"202"}
        assert _response_schema_name(operation) == "OperationV1"

    get_operation = openapi["paths"]["/v1/operations/{operation_id}"]["get"]
    assert _response_schema_name(get_operation) == "OperationV1"
    assert "etag" in openapi["components"]["schemas"]["OperationV1"]["properties"]
    assert (
        openapi["components"]["schemas"]["ReferencedLogPageV1"]["properties"]["items"]["maxItems"]
        == 100
    )
    assert OperationV1 is not None

    cancel = openapi["paths"]["/v1/operations/{operation_id}/cancel"]["post"]
    assert _response_schema_name(cancel) == "OperationV1"
    assert cancel["responses"]["409"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ApiErrorV1"
    }
    conflict_description = cancel["responses"]["409"]["description"]
    assert "operation_kind_not_cancellable" in conflict_description
    assert "idempotency_key_reused" in conflict_description
    assert {parameter["name"] for parameter in cancel["parameters"]} >= {
        "If-Match",
        "Idempotency-Key",
    }


def test_environment_repair_operation_binds_request_result_and_cancellation() -> None:
    operation = {
        "schema_version": "1",
        "id": "operation-1",
        "kind": "environment_repair",
        "descriptor": {"kind": "environment_repair", "cancellable": True},
        "status": "queued",
        "request": {
            "kind": "environment_repair",
            "request": {
                "schema_version": "1",
                "execution_mode": "self-deployed",
                "actions": ["retry_network"],
            },
        },
        "result": None,
        "cancellation": None,
        "logs_ref": "logs-1",
        "created_at": "2026-07-14T00:00:00Z",
        "updated_at": "2026-07-14T00:00:00Z",
        "observed_at": "2026-07-14T00:00:00Z",
        "finished_at": None,
        "error": None,
        "etag": '"' + "e" * 64 + '"',
    }
    assert _json_model(OperationV1, operation).logs_ref == "logs-1"

    cancelling = json.loads(json.dumps(operation))
    cancelling.update(
        {
            "status": "cancelling",
            "cancellation": {
                "reason": "user_requested",
                "requested_at": "2026-07-14T00:00:01Z",
            },
        }
    )
    assert _json_model(OperationV1, cancelling).status.value == "cancelling"

    cancelled = json.loads(json.dumps(cancelling))
    cancelled.update({"status": "cancelled", "finished_at": "2026-07-14T00:00:02Z"})
    assert _json_model(OperationV1, cancelled).status.value == "cancelled"

    succeeded = json.loads(json.dumps(operation))
    succeeded.update(
        {
            "status": "succeeded",
            "finished_at": "2026-07-14T00:00:02Z",
            "result": {
                "kind": "environment_repair",
                "response": {
                    "schema_version": "1",
                    "status": "ok",
                    "results": [
                        {
                            "action": "retry_network",
                            "status": "ok",
                            "message": "Network retry completed.",
                        }
                    ],
                    "checked_at": "2026-07-14T00:00:02Z",
                },
            },
        }
    )
    assert _json_model(OperationV1, succeeded).result is not None

    mismatched_result = json.loads(json.dumps(succeeded))
    mismatched_result["result"]["response"]["results"][0]["action"] = "restart_model_service"
    with pytest.raises(ValidationError, match="requested actions"):
        _json_model(OperationV1, mismatched_result)

    non_cancellable = {
        **cancelling,
        "kind": "service_restart",
        "descriptor": {"kind": "service_restart", "cancellable": False},
        "request": {
            "kind": "service_restart",
            "service_id": "service-1",
            "request": {"schema_version": "1", "reason": "Recover."},
        },
    }
    with pytest.raises(ValidationError, match="non-cancellable"):
        _json_model(OperationV1, non_cancellable)

    ambiguous_descriptor = {
        **operation,
        "descriptor": {"kind": "environment_repair", "cancellable": False},
    }
    with pytest.raises(ValidationError, match="cancellation policy"):
        _json_model(OperationV1, ambiguous_descriptor)


def test_every_202_response_has_a_recoverable_get_resource() -> None:
    openapi = build_openapi_document()
    recoverable = {
        "RunV1": ("/v1/runs/{run_id}", "RunV1"),
        "DiagnosticV1": ("/v1/diagnostics/{diagnostic_id}", "DiagnosticV1"),
        "OperationV1": ("/v1/operations/{operation_id}", "OperationV1"),
    }
    observed_response_models: set[str] = set()
    for path_item in openapi["paths"].values():
        for method, operation in path_item.items():
            if method not in {"post", "put", "patch", "delete"}:
                continue
            if "202" not in operation["responses"]:
                continue
            response_model = _response_schema_name(operation)
            assert response_model in recoverable
            observed_response_models.add(response_model)
            get_path, expected_model = recoverable[response_model]
            assert _response_schema_name(openapi["paths"][get_path]["get"]) == expected_model
    assert observed_response_models == set(recoverable)


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
            assert any(
                "etag" in schemas[name].get("properties", {}) for name in candidate_names
            ), (
                method,
                path,
                candidate_names,
            )

    conditional_actions = {
        ("patch", "/v1/projects/{project_id}"),
        ("delete", "/v1/projects/{project_id}"),
        ("post", "/v1/projects/{project_id}/workspace-uploads"),
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
        ("post", "/v1/operations/{operation_id}/cancel"),
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
    revision_paths = {path: item for path, item in openapi["paths"].items() if "revision" in path}
    assert revision_paths
    assert all(set(item) <= {"get"} for item in revision_paths.values())
    assert not any("activate" in path or "promote" in path for path in openapi["paths"])

    expected = {
        ("post", "/v1/environment/repair"): "202",
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
        ("post", "/v1/operations/{operation_id}/cancel"): "202",
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
