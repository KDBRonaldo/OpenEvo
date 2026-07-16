from __future__ import annotations

from collections.abc import Iterator
import inspect
import json
from pathlib import Path

from fastapi import APIRouter, FastAPI
import pytest
from pydantic import ValidationError

from openevo.backend.contracts.v1 import models as core_contract

from desktop.sidecar.contracts.v1 import (
    DESKTOP_EVENTS_SCHEMA_SHA256,
    DESKTOP_OPENAPI_SHA256,
    ArtifactContentV1,
    ArtifactDiffV1,
    ArtifactPageV1,
    ArtifactV1,
    BoundedJsonObjectV1,
    CoreConnectionStateV1,
    DesktopStateV1,
    DiagnosticReportV1,
    EventEnvelopeV1,
    ExecutionModeCapabilitiesV1,
    ExecutionSettingsV1,
    HealthV1,
    LocalOperationV1,
    LogPageV1,
    OperationV1,
    PageV1,
    ProjectCreateV1,
    ProjectPatchV1,
    RemoteProfileCreateV1,
    RemoteProfilePatchV1,
    RemoteProfileV1,
    RunCreateV1,
    RunContextV1,
    RunPageV1,
    RunRetryV1,
    RunSummaryV1,
    RunV1,
    ReferencedLogPageV1,
    ServicePageV1,
    SseFrameV1,
    VersionV1,
    canonical_json_bytes,
    contract_app,
    desktop_events_schema_document,
    desktop_openapi_document,
    verify_contract_snapshots,
)
from desktop.sidecar.release_capabilities import RELEASE_EXECUTION_MODE_CAPABILITIES_V1
import desktop.sidecar.contracts.v1.app as contract_app_module
from desktop.sidecar.contracts.v1.app import create_contract_app


_DIGEST = "a" * 64
_ETAG = f'"{_DIGEST}"'
_NOW = "2026-07-14T12:00:00Z"

_EXPECTED_OPERATIONS = {
    ("get", "/version"),
    ("get", "/health"),
    ("get", "/desktop/v1/state"),
    ("get", "/desktop/v1/profiles"),
    ("post", "/desktop/v1/profiles"),
    ("get", "/desktop/v1/profiles/{profile_id}"),
    ("patch", "/desktop/v1/profiles/{profile_id}"),
    ("delete", "/desktop/v1/profiles/{profile_id}"),
    ("post", "/desktop/v1/profiles/{profile_id}/connect"),
    ("post", "/desktop/v1/profiles/{profile_id}/disconnect"),
    ("post", "/desktop/v1/profiles/{profile_id}/host-key/accept"),
    ("get", "/desktop/v1/projects"),
    ("post", "/desktop/v1/projects"),
    ("get", "/desktop/v1/projects/{project_id}"),
    ("patch", "/desktop/v1/projects/{project_id}"),
    ("delete", "/desktop/v1/projects/{project_id}"),
    ("post", "/desktop/v1/projects/{project_id}/activate"),
    ("post", "/desktop/v1/projects/{project_id}/doctor"),
    ("post", "/desktop/v1/projects/{project_id}/repair"),
    ("post", "/desktop/v1/projects/{project_id}/bootstrap"),
    ("post", "/desktop/v1/projects/{project_id}/workspace-sync"),
    ("get", "/desktop/v1/projects/{project_id}/capabilities"),
    ("post", "/desktop/v1/projects/{project_id}/validate"),
    ("get", "/desktop/v1/operations/{operation_id}"),
    ("get", "/desktop/v1/operations/{operation_id}/logs"),
    ("post", "/desktop/v1/operations/{operation_id}/cancel"),
    ("get", "/desktop/v1/runs"),
    ("post", "/desktop/v1/runs"),
    ("get", "/desktop/v1/runs/{run_id}"),
    ("delete", "/desktop/v1/runs/{run_id}"),
    ("post", "/desktop/v1/runs/{run_id}/cancel"),
    ("post", "/desktop/v1/runs/{run_id}/retry"),
    ("get", "/desktop/v1/runs/{run_id}/timeline"),
    ("get", "/desktop/v1/runs/{run_id}/logs"),
    ("get", "/desktop/v1/runs/{run_id}/context"),
    ("get", "/desktop/v1/runs/{run_id}/artifacts"),
    ("get", "/desktop/v1/artifacts/{artifact_id}"),
    ("get", "/desktop/v1/artifacts/{artifact_id}/content"),
    ("get", "/desktop/v1/artifacts/{artifact_id}/diff"),
    ("get", "/desktop/v1/services"),
    ("post", "/desktop/v1/services/{service_id}/restart"),
    ("get", "/desktop/v1/services/{service_id}/logs"),
    ("get", "/desktop/v1/core/operations/{operation_id}"),
    ("get", "/desktop/v1/core/logs/{logs_ref}"),
    ("post", "/desktop/v1/diagnostics"),
    ("get", "/desktop/v1/diagnostics/{diagnostic_id}"),
    ("delete", "/desktop/v1/diagnostics/{diagnostic_id}"),
    ("post", "/desktop/v1/maintenance/cache-cleanup"),
    ("get", "/desktop/v1/events"),
}


def test_provider_route_iteration_recurses_deferred_included_router() -> None:
    app = FastAPI()
    router = APIRouter(prefix="/desktop/v1")

    @app.get("/version", operation_id="topLevelOperation")
    def top_level() -> None:
        return None

    @router.get("/state", operation_id="nestedOperation")
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
        def invoke(self, _operation_id: str, _arguments: dict[str, object]) -> object:
            raise AssertionError("provider dispatch is not part of this test")

    plain_app = create_contract_app()
    provider_app = create_contract_app(Provider())
    plain_routes = {
        route.operation_id: route
        for route in contract_app_module._iter_api_routes(plain_app.routes)
    }
    provider_routes = {
        route.operation_id: route
        for route in contract_app_module._iter_api_routes(provider_app.routes)
    }

    assert provider_routes.keys() == plain_routes.keys()
    for operation_id, route in provider_routes.items():
        assert inspect.signature(route.endpoint) == inspect.signature(
            plain_routes[operation_id].endpoint
        )


def _operations(schema: dict) -> set[tuple[str, str]]:
    return {
        (method, path)
        for path, path_item in schema["paths"].items()
        for method in path_item
        if method in {"get", "post", "patch", "delete", "put"}
    }


def _walk_json(value: object) -> Iterator[object]:
    yield value
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _project_payload() -> dict:
    return {
        "name": "Protein Design",
        "profile_id": "profile-1",
        "task": {"title": "Design", "objective": "Produce a candidate."},
        "source": {"kind": "scratch", "display_name": "New workspace"},
        "execution": {"mode": "self-deployed", "hf_model": "open-models/model-1"},
        "evolution": {
            "targets": {
                "text_memory": {
                    "enabled": True,
                    "method": "reference_text_memory",
                    "config": {"future_field": 1},
                }
            }
        },
    }


def _required_headers(schema: dict, method: str, path: str) -> set[str]:
    return {
        parameter["name"]
        for parameter in schema["paths"][path][method].get("parameters", ())
        if parameter["in"] == "header" and parameter["required"]
    }


def test_contract_app_has_exact_release_operation_set() -> None:
    schema = desktop_openapi_document()
    assert _operations(schema) == _EXPECTED_OPERATIONS
    operation_ids = [
        operation["operationId"]
        for path_item in schema["paths"].values()
        for method, operation in path_item.items()
        if method in {"get", "post", "patch", "delete", "put"}
    ]
    assert len(operation_ids) == len(set(operation_ids))
    assert "/desktop/v1/services/{service_id}/stop" not in schema["paths"]


def test_discovery_is_public_and_v1_uses_desktop_session_security() -> None:
    schema = contract_app.openapi()
    assert schema["components"]["securitySchemes"]["DesktopSession"]["name"] == (
        "X-OpenEvo-Desktop-Session"
    )
    assert "security" not in schema["paths"]["/version"]["get"]
    assert "security" not in schema["paths"]["/health"]["get"]
    for method, path in _EXPECTED_OPERATIONS:
        if path.startswith("/desktop/v1"):
            assert schema["paths"][path][method]["security"] == [{"DesktopSession": []}]


def test_execution_mode_capabilities_are_closed_complete_and_versioned() -> None:
    capabilities = RELEASE_EXECUTION_MODE_CAPABILITIES_V1
    assert capabilities.schema_version == "1"
    assert [item.mode for item in capabilities.modes] == [
        "codex_subscription_transcript",
        "self-deployed",
    ]
    assert [item.support_state for item in capabilities.modes] == [
        "supported",
        "unavailable",
    ]
    assert capabilities.modes[1].reason_code == "self_deployed_release_unavailable"

    payload = capabilities.model_dump(mode="json")
    for invalid in (
        {**payload, "modes": payload["modes"][:1]},
        {**payload, "modes": [payload["modes"][0], payload["modes"][0]]},
        {
            **payload,
            "modes": [
                payload["modes"][0],
                {**payload["modes"][1], "mode": "future-mode"},
            ],
        },
        {
            **payload,
            "modes": [
                payload["modes"][0],
                {**payload["modes"][1], "reason_code": None},
            ],
        },
        {
            **payload,
            "modes": [
                payload["modes"][0],
                {**payload["modes"][1], "reason_code": "future_reason"},
            ],
        },
    ):
        with pytest.raises(ValidationError):
            ExecutionModeCapabilitiesV1.model_validate(invalid)

    state_schema = desktop_openapi_document()["components"]["schemas"]["DesktopStateV1"]
    assert "execution_mode_capabilities" in state_schema["required"]


def test_only_sidecar_owned_actions_return_local_operations() -> None:
    schema = desktop_openapi_document()
    local_actions = {
        "/desktop/v1/profiles/{profile_id}/connect",
        "/desktop/v1/profiles/{profile_id}/disconnect",
        "/desktop/v1/profiles/{profile_id}/host-key/accept",
        "/desktop/v1/projects/{project_id}/activate",
        "/desktop/v1/projects/{project_id}/doctor",
        "/desktop/v1/projects/{project_id}/repair",
        "/desktop/v1/projects/{project_id}/bootstrap",
        "/desktop/v1/projects/{project_id}/workspace-sync",
        "/desktop/v1/operations/{operation_id}/cancel",
    }
    for path in local_actions:
        response = schema["paths"][path]["post"]["responses"]["202"]
        assert response["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/LocalOperationV1"
        }

    expected_remote = {
        "/desktop/v1/services/{service_id}/restart": "OperationV1",
        "/desktop/v1/diagnostics": "DiagnosticV1",
        "/desktop/v1/maintenance/cache-cleanup": "OperationV1",
    }
    for path, model in expected_remote.items():
        response = schema["paths"][path]["post"]["responses"]["202"]
        assert response["content"]["application/json"]["schema"] == {
            "$ref": f"#/components/schemas/{model}"
        }


def test_core_owned_dtos_are_reused_without_sidecar_reinterpretation() -> None:
    assert RunSummaryV1 is core_contract.RunSummaryV1
    assert RunV1 is core_contract.RunV1
    assert RunPageV1 is core_contract.RunPageV1
    assert RunContextV1 is core_contract.RunContextV1
    assert LogPageV1 is core_contract.LogPageV1
    assert ArtifactV1 is core_contract.ArtifactSummaryV1
    assert ArtifactPageV1 is core_contract.ArtifactPageV1
    assert ArtifactContentV1 is core_contract.ArtifactContentV1
    assert ArtifactDiffV1 is core_contract.ArtifactDiffV1
    assert ServicePageV1 is core_contract.ServicePageV1
    assert OperationV1 is core_contract.OperationV1
    assert ReferencedLogPageV1 is core_contract.ReferencedLogPageV1
    assert DiagnosticReportV1 is core_contract.DiagnosticV1


def test_mutations_bind_idempotency_and_etag_to_renderer_intent() -> None:
    schema = desktop_openapi_document()
    for path in (
        "/desktop/v1/runs",
        "/desktop/v1/projects/{project_id}/validate",
        "/desktop/v1/services/{service_id}/restart",
    ):
        assert {"Idempotency-Key", "If-Match"}.issubset(_required_headers(schema, "post", path))

    for path in (
        "/desktop/v1/profiles/{profile_id}",
        "/desktop/v1/projects/{project_id}",
        "/desktop/v1/runs/{run_id}",
        "/desktop/v1/diagnostics/{diagnostic_id}",
    ):
        assert "If-Match" in _required_headers(schema, "delete", path)

    assert "Idempotency-Key" in _required_headers(
        schema, "delete", "/desktop/v1/diagnostics/{diagnostic_id}"
    )


def test_renderer_never_authors_core_admission_references() -> None:
    schema = desktop_openapi_document()
    run_create = schema["components"]["schemas"]["RunCreateV1"]
    assert set(run_create["properties"]) == {"project_id"}
    assert run_create["required"] == ["project_id"]
    assert (
        "requestBody" not in schema["paths"]["/desktop/v1/projects/{project_id}/validate"]["post"]
    )

    RunCreateV1.model_validate({"project_id": "project-1"})
    for field in (
        "project_snapshot",
        "task_snapshot",
        "workspace_snapshot",
        "capability_registry_digest",
        "required_revision",
        "runtime",
        "model",
        "workspace_path",
        "command",
    ):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            RunCreateV1.model_validate({"project_id": "project-1", field: "forbidden"})


def test_run_retry_body_is_closed_terminal_attempt_authority() -> None:
    schema = desktop_openapi_document()
    retry = schema["components"]["schemas"]["RunRetryV1"]
    assert retry["additionalProperties"] is False
    assert set(retry["properties"]) == {"terminal_attempt_id"}
    assert retry["required"] == ["terminal_attempt_id"]
    assert retry["properties"]["terminal_attempt_id"]["maxLength"] == 128
    assert retry["properties"]["terminal_attempt_id"]["pattern"] == (
        r"^[^\x00-\x20\x7f](?:[^\x00-\x1f\x7f]*[^\x00-\x20\x7f])?$"
    )
    operation = schema["paths"]["/desktop/v1/runs/{run_id}/retry"]["post"]
    assert operation["requestBody"]["required"] is True
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/RunRetryV1"
    }

    assert RunRetryV1(terminal_attempt_id="attempt-terminal-1").terminal_attempt_id == (
        "attempt-terminal-1"
    )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RunRetryV1.model_validate(
            {"terminal_attempt_id": "attempt-terminal-1", "current_attempt_id": "attempt-new"}
        )
    with pytest.raises(ValidationError):
        RunRetryV1(terminal_attempt_id="a" * 129)
    with pytest.raises(ValidationError):
        RunRetryV1(terminal_attempt_id=" attempt-terminal-1")


def test_capabilities_are_lossless_and_model_readiness_is_typed() -> None:
    schemas = desktop_openapi_document()["components"]["schemas"]
    envelope = schemas["CapabilitiesEnvelopeV1"]
    assert envelope["properties"]["capabilities"] == {
        "$ref": "#/components/schemas/EvolutionCapabilitiesV1"
    }
    assert schemas["SupportState"]["enum"] == [
        "supported",
        "unsupported",
        "unavailable",
    ]
    method_properties = schemas["EvolutionMethodCapabilityV1"]["properties"]
    assert {
        "exposure",
        "execution_modes",
        "capture_modes",
        "harness_requirements",
        "runtime_requirements",
        "input_bindings",
        "output_artifact_types",
        "config_schema_json",
        "default_config_json",
        "implementation_identity_digest",
        "support",
    }.issubset(method_properties)
    assert schemas["ModelPreparationStatus"]["enum"] == [
        "unresolved",
        "downloading",
        "ready",
        "failed",
    ]
    assert schemas["RemoteProjectStateV1"]["properties"]["model_preparation"] == {
        "$ref": "#/components/schemas/ModelPreparationV1"
    }


def test_project_source_uses_only_opaque_native_imports() -> None:
    scratch = ProjectCreateV1.model_validate(_project_payload())
    assert scratch.source.import_ref is None

    imported = _project_payload() | {
        "source": {
            "kind": "native_folder_snapshot",
            "display_name": "Experiment data",
            "import_ref": {
                "import_id": "import-1",
                "content_sha256": _DIGEST,
                "byte_size": 1024,
                "entry_count": 3,
                "extracted_byte_size": 768,
            },
        }
    }
    parsed = ProjectCreateV1.model_validate(imported)
    assert parsed.source.import_ref is not None
    assert parsed.source.import_ref.import_id == "import-1"

    with pytest.raises(ValidationError, match="require an opaque import_ref"):
        ProjectCreateV1.model_validate(
            _project_payload()
            | {
                "source": {
                    "kind": "native_folder_snapshot",
                    "display_name": "Experiment data",
                }
            }
        )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ProjectCreateV1.model_validate(
            imported
            | {"source": imported["source"] | {"workspace_path": "/Users/researcher/private"}}
        )


def test_project_evolution_is_closed_lossless_and_aggregate_bounded() -> None:
    project = _project_payload()
    parsed = ProjectCreateV1.model_validate(project)
    assert parsed.evolution.targets.root["text_memory"].config.model_dump() == {"future_field": 1}
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ProjectCreateV1.model_validate(project | {"evolution": {"text_memory": {}}})

    oversized_targets = {
        f"target_{index}": {
            "enabled": False,
            "method": None,
            "config": {"payload": "x" * 220_000},
        }
        for index in range(5)
    }
    with pytest.raises(ValidationError, match="aggregate byte budget"):
        ProjectCreateV1.model_validate(project | {"evolution": {"targets": oversized_targets}})

    for invalid in (
        project | {"name": "x" * 129},
        project | {"task": {"title": "x" * 257, "objective": "Research."}},
        project
        | {
            "execution": {
                "mode": "self-deployed",
                "hf_model": "x" * 257,
            }
        },
        project
        | {
            "evolution": {
                "targets": {
                    "not/a/stable/id": {
                        "enabled": False,
                        "method": None,
                        "config": {},
                    }
                }
            }
        },
    ):
        with pytest.raises(ValidationError):
            ProjectCreateV1.model_validate(invalid)


def test_profiles_and_patches_never_accept_secrets_or_paths() -> None:
    base = {
        "name": "Lab GPU",
        "host": "gpu.example.org",
        "port": 22,
        "user": "researcher",
        "authentication_kind": "native_private_key",
    }
    RemoteProfileCreateV1.model_validate(base)
    credential_kind = desktop_openapi_document()["components"]["schemas"][
        "CredentialSlotStatusV1"
    ]["properties"]["kind"]["enum"]
    assert "hugging_face_token" in credential_kind
    for forbidden in (
        "token",
        "secret_ref",
        "password_ref",
        "passphrase_ref",
        "private_key_path",
        "workspace_path",
        "command",
        "env",
    ):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            RemoteProfileCreateV1.model_validate(base | {forbidden: "forbidden"})

    with pytest.raises(ValidationError, match="user information"):
        RemoteProfileCreateV1.model_validate(
            base | {"proxy": {"https_url": "https://user:secret@proxy.example.org"}}
        )
    with pytest.raises(ValidationError, match="not a URL or path"):
        RemoteProfileCreateV1.model_validate(base | {"host": "/etc/hosts"})

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RemoteProfileV1.model_validate(
            {
                **base,
                "profile_id": "profile-1",
                "credential_slots": (),
                "etag": _ETAG,
                "created_at": _NOW,
                "updated_at": _NOW,
                "credential_ref": "native/keychain/item",
            }
        )

    patch = RemoteProfilePatchV1.model_validate({"proxy": {"https_url": None}})
    assert patch.model_dump(exclude_unset=True) == {"proxy": {"https_url": None}}
    for model, field in (
        (RemoteProfilePatchV1, "host"),
        (RemoteProfilePatchV1, "proxy"),
        (ProjectPatchV1, "execution"),
    ):
        with pytest.raises(ValidationError):
            model.model_validate({field: None})


def test_execution_modes_are_exact_and_cannot_claim_token_metrics() -> None:
    deployed = ExecutionSettingsV1.model_validate(
        {"mode": "self-deployed", "hf_model": "open-models/research-model-1"}
    )
    assert deployed.capture_mode == "transcript"
    assert deployed.token_level_metrics_available is False

    subscription = ExecutionSettingsV1.model_validate(
        {"mode": "codex_subscription_transcript", "codex_model": "gpt-5"}
    )
    assert subscription.hf_model is None
    with pytest.raises(ValidationError):
        ExecutionSettingsV1.model_validate(
            {"mode": "self_deployed", "hf_model": "open-models/model-1"}
        )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ExecutionSettingsV1.model_validate(
            {"mode": "self-deployed", "managed_model_id": "legacy-model"}
        )


def test_connection_health_and_release_provider_are_fail_closed() -> None:
    state = CoreConnectionStateV1.model_validate(
        {
            "state": "host_key_review",
            "profile_id": "profile-1",
            "active_tunnel": False,
            "operation_id": "operation-1",
            "host_key_review": {
                "algorithm": "ssh-ed25519",
                "fingerprint": "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            },
        }
    )
    assert state.state == "host_key_review"
    with pytest.raises(ValidationError):
        CoreConnectionStateV1.model_validate(
            {"state": "tunnel_ready", "profile_id": "profile-1", "active_tunnel": True}
        )

    native = HealthV1.model_validate(
        {
            "service": "openevo-sidecar",
            "status": "ok",
            "protocol": "openevo-native-sidecar-v1",
            "instance_id": "a" * 32,
            "instance_proof": "b" * 64,
        }
    )
    assert native.instance_id == "a" * 32

    version_payload = {
        "openapi_sha256": _DIGEST,
        "build_version": "1.0.0",
        "source_commit": "dabbfec3",
        "build_channel": "release",
        "provider_kind": "desktop_sidecar",
    }
    VersionV1.model_validate(version_payload)
    for provider_kind in ("contract_simulator", "scaffold", "dry_run"):
        with pytest.raises(ValidationError, match="provider_kind=desktop_sidecar"):
            VersionV1.model_validate(version_payload | {"provider_kind": provider_kind})


def test_events_are_invalidation_only_and_require_authoritative_identity() -> None:
    heartbeat_payload = {
        "event_id": "event-1",
        "event_name": "desktop.v1.heartbeat",
        "occurred_at": _NOW,
        "sequence": 7,
        "data": {"kind": "heartbeat"},
    }
    envelope = EventEnvelopeV1.model_validate(heartbeat_payload)
    frame = SseFrameV1(id="event-1", event="desktop.v1.heartbeat", data=envelope)
    assert frame.data.sequence == 7

    changed = {
        "event_id": "event-2",
        "event_name": "desktop.v1.resource.changed",
        "occurred_at": _NOW,
        "sequence": 8,
        "data": {
            "kind": "resource_changed",
            "authority": "core",
            "resource": {"resource_type": "run", "resource_id": "run-1"},
            "change": "updated",
            "change_id": "core-change-1",
            "resource_etag": _ETAG,
        },
    }
    EventEnvelopeV1.model_validate(changed)
    del changed["data"]["resource_etag"]
    with pytest.raises(ValidationError, match="authoritative ETag or digest"):
        EventEnvelopeV1.model_validate(changed)

    event_response = desktop_openapi_document()["paths"]["/desktop/v1/events"]["get"]
    assert "Last-Event-ID" in {parameter["name"] for parameter in event_response["parameters"]}
    assert set(event_response["responses"]["200"]["content"]) == {"text/event-stream"}


def test_contract_forbids_renderer_host_runtime_and_secret_properties() -> None:
    forbidden = {
        "argv",
        "backend_url",
        "command",
        "core_url",
        "credential_ref",
        "env",
        "file_uri",
        "host_path",
        "passphrase_ref",
        "password_ref",
        "pid",
        "private_key_path",
        "remote_path",
        "secret",
        "stderr",
        "stdout",
        "token",
        "workspace_path",
    }
    schemas = desktop_openapi_document()["components"]["schemas"]
    property_names = {
        property_name
        for schema in schemas.values()
        if isinstance(schema, dict)
        for property_name in schema.get("properties", {})
    }
    assert forbidden.isdisjoint(property_names)


def test_explicit_maps_are_the_only_open_object_schemas() -> None:
    schemas = desktop_openapi_document()["components"]["schemas"]
    open_object_schemas = {
        name
        for name, schema in schemas.items()
        if isinstance(schema, dict)
        and schema.get("type") == "object"
        and schema.get("additionalProperties") is not False
    }
    assert open_object_schemas == {"BoundedJsonObjectV1", "EvolutionSelectionsV1"}
    assert not any(
        "dict[str, Any]" in value for value in _walk_json(schemas) if isinstance(value, str)
    )


def test_local_pagination_and_dynamic_config_are_bounded() -> None:
    page = PageV1[int](items=(1, 2), next_cursor="cursor-2", has_more=True)
    assert page.items == (1, 2)
    with pytest.raises(ValidationError, match="must agree"):
        PageV1[int](items=(), next_cursor=None, has_more=True)

    config = {
        "password": "algorithm-owned-value",
        "command": {"strategy": "reflect"},
        "future_plugin_field": [1, True, None],
    }
    assert BoundedJsonObjectV1.model_validate(config).model_dump() == config
    with pytest.raises(ValidationError, match="JavaScript safe"):
        BoundedJsonObjectV1.model_validate({"integer": 9_007_199_254_740_992})


def test_cross_language_critical_fixture_matches_python_contract() -> None:
    fixture_path = (
        Path(__file__).parents[3] / "desktop/sidecar/contracts/v1/fixtures/contract-critical.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    def parse_json(model: type, value: object) -> object:
        return model.model_validate_json(json.dumps(value))

    parse_json(HealthV1, fixture["health"])
    parse_json(DesktopStateV1, fixture["state"])
    parse_json(RunCreateV1, fixture["run_create"])
    parse_json(RunRetryV1, fixture["run_retry"])
    parse_json(ProjectCreateV1, fixture["project_create"])
    parse_json(LocalOperationV1, fixture["operation_defaults"]["wire"])
    assert fixture["state"]["contract"]["desktop_openapi_sha256"] == (DESKTOP_OPENAPI_SHA256)


def test_snapshots_are_canonical_and_digests_are_stable() -> None:
    openapi_digest, events_digest = verify_contract_snapshots()
    assert openapi_digest == DESKTOP_OPENAPI_SHA256
    assert events_digest == DESKTOP_EVENTS_SCHEMA_SHA256
    assert openapi_digest == "60cd51f9ab1e7b1140747b9cc5d3760fad32204e4e5c399b608bb5d406172777"
    assert events_digest == "39e485b6c61688832ec0445502d2f1f9e8bd9548e9b81a0a4740bc5997d90936"
    snapshot_root = Path(__file__).parents[3] / "desktop/sidecar/contracts/v1"
    assert (snapshot_root / "openapi.json").read_bytes() == canonical_json_bytes(
        desktop_openapi_document()
    )
    assert (snapshot_root / "events.schema.json").read_bytes() == canonical_json_bytes(
        desktop_events_schema_document()
    )
