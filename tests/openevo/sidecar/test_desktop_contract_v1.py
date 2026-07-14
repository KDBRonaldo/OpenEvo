from __future__ import annotations

from collections.abc import Iterator
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from desktop.sidecar.contracts.v1 import (
    DESKTOP_EVENTS_SCHEMA_SHA256,
    DESKTOP_OPENAPI_SHA256,
    ArtifactContentV1,
    ArtifactPageV1,
    BoundedJsonObjectV1,
    CoreConnectionStateV1,
    DiffLineV1,
    DesktopStateV1,
    EventEnvelopeV1,
    ExecutionSettingsV1,
    HealthV1,
    PageV1,
    RemoteProfileCreateV1,
    RemoteProfileV1,
    RunCreateV1,
    SseFrameV1,
    VersionV1,
    canonical_json_bytes,
    contract_app,
    desktop_events_schema_document,
    desktop_openapi_document,
    verify_contract_snapshots,
)


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
    ("post", "/desktop/v1/services/{service_id}/stop"),
    ("get", "/desktop/v1/services/{service_id}/logs"),
    ("post", "/desktop/v1/diagnostics"),
    ("get", "/desktop/v1/diagnostics/{diagnostic_id}"),
    ("delete", "/desktop/v1/diagnostics/{diagnostic_id}"),
    ("post", "/desktop/v1/maintenance/cache-cleanup"),
    ("get", "/desktop/v1/events"),
}


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


def _run_create_payload() -> dict:
    snapshot = {"snapshot_id": "snapshot-1", "digest": _DIGEST}
    return {
        "project_id": "project-1",
        "project_snapshot": snapshot,
        "task_snapshot": snapshot | {"snapshot_id": "task-1"},
        "workspace_snapshot": snapshot | {"snapshot_id": "workspace-1"},
        "capability_registry_digest": _DIGEST,
        "required_revision": {
            "revision_id": "revision-1",
            "generation": 0,
            "manifest_digest": _DIGEST,
            "state": "active",
        },
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


def test_discovery_is_public_and_v1_uses_desktop_session_security() -> None:
    schema = contract_app.openapi()

    assert schema["components"]["securitySchemes"] == {
        "DesktopSession": {
            "type": "apiKey",
            "description": (
                "Ephemeral Desktop session credential returned only by the native "
                "start_sidecar command."
            ),
            "in": "header",
            "name": "X-OpenEvo-Desktop-Session",
        }
    }
    assert "security" not in schema["paths"]["/version"]["get"]
    assert "security" not in schema["paths"]["/health"]["get"]
    for method, path in _EXPECTED_OPERATIONS:
        if path.startswith("/desktop/v1"):
            assert schema["paths"][path][method]["security"] == [{"DesktopSession": []}]


def test_long_local_actions_return_202_local_operation() -> None:
    schema = desktop_openapi_document()
    operations = {
        ("post", "/desktop/v1/profiles/{profile_id}/connect"),
        ("post", "/desktop/v1/profiles/{profile_id}/disconnect"),
        ("post", "/desktop/v1/profiles/{profile_id}/host-key/accept"),
        ("post", "/desktop/v1/projects/{project_id}/activate"),
        ("post", "/desktop/v1/projects/{project_id}/doctor"),
        ("post", "/desktop/v1/projects/{project_id}/repair"),
        ("post", "/desktop/v1/projects/{project_id}/bootstrap"),
        ("post", "/desktop/v1/projects/{project_id}/workspace-sync"),
        ("post", "/desktop/v1/operations/{operation_id}/cancel"),
        ("post", "/desktop/v1/services/{service_id}/restart"),
        ("post", "/desktop/v1/services/{service_id}/stop"),
        ("post", "/desktop/v1/diagnostics"),
        ("post", "/desktop/v1/maintenance/cache-cleanup"),
    }

    for method, path in operations:
        response = schema["paths"][path][method]["responses"]
        assert "202" in response
        assert response["202"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/LocalOperationV1"
        }


def test_create_and_action_headers_bind_idempotency_and_mutable_resources() -> None:
    schema = desktop_openapi_document()
    create_or_unscoped_actions = {
        ("post", "/desktop/v1/profiles"),
        ("post", "/desktop/v1/projects"),
        ("post", "/desktop/v1/runs"),
        ("post", "/desktop/v1/diagnostics"),
        ("post", "/desktop/v1/maintenance/cache-cleanup"),
    }
    scoped_actions = {
        (method, path)
        for method, path in _EXPECTED_OPERATIONS
        if method == "post" and (method, path) not in create_or_unscoped_actions
    }

    for method, path in create_or_unscoped_actions | scoped_actions:
        parameters = schema["paths"][path][method].get("parameters", ())
        required_headers = {
            parameter["name"]
            for parameter in parameters
            if parameter["in"] == "header" and parameter["required"]
        }
        assert "Idempotency-Key" in required_headers
        if (method, path) in scoped_actions:
            assert "If-Match" in required_headers

    for path in (
        "/desktop/v1/profiles/{profile_id}",
        "/desktop/v1/projects/{project_id}",
        "/desktop/v1/runs/{run_id}",
        "/desktop/v1/diagnostics/{diagnostic_id}",
    ):
        parameters = schema["paths"][path]["delete"]["parameters"]
        assert "If-Match" in {parameter["name"] for parameter in parameters}


def test_snapshots_are_canonical_and_digests_are_stable() -> None:
    openapi_digest, events_digest = verify_contract_snapshots()

    assert openapi_digest == DESKTOP_OPENAPI_SHA256
    assert events_digest == DESKTOP_EVENTS_SCHEMA_SHA256
    assert openapi_digest == "e877eab5b97a540486a82a4dfadf123b3324022460403362710bc06cecd6a051"
    assert events_digest == "dd425b6050f1cb329d8a178ba77e0012aba7bbfc612cf04e258c0f9cd8480ad7"
    snapshot_root = Path(__file__).parents[3] / "desktop/sidecar/contracts/v1"
    assert (snapshot_root / "openapi.json").read_bytes() == canonical_json_bytes(
        desktop_openapi_document()
    )
    assert (snapshot_root / "events.schema.json").read_bytes() == canonical_json_bytes(
        desktop_events_schema_document()
    )


def test_models_are_closed_and_profile_never_accepts_secret_or_path_fields() -> None:
    base = {
        "name": "Lab GPU",
        "host": "gpu.example.org",
        "port": 22,
        "user": "researcher",
        "authentication_kind": "native_private_key",
    }
    RemoteProfileCreateV1.model_validate(base)

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
    with pytest.raises(ValidationError, match="not a path"):
        RemoteProfileCreateV1.model_validate(base | {"user": "../researcher"})

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RemoteProfileV1.model_validate(
            {
                "profile_id": "profile-1",
                "name": "Lab GPU",
                "host": "gpu.example.org",
                "port": 22,
                "user": "researcher",
                "authentication_kind": "native_private_key",
                "credential_slots": (),
                "etag": _ETAG,
                "created_at": _NOW,
                "updated_at": _NOW,
                "credential_ref": "native/keychain/item",
            }
        )


def test_openapi_has_no_forbidden_renderer_property_names() -> None:
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
    property_names = {
        property_name
        for schema in desktop_openapi_document()["components"]["schemas"].values()
        if isinstance(schema, dict)
        for property_name in schema.get("properties", {})
    }

    assert forbidden.isdisjoint(property_names)


def test_release_version_rejects_non_release_provider_kinds() -> None:
    payload = {
        "openapi_sha256": _DIGEST,
        "build_version": "1.0.0",
        "source_commit": "dabbfec3",
        "build_channel": "release",
        "provider_kind": "desktop_sidecar",
    }
    version = VersionV1.model_validate(payload)
    assert version.provider_kind == "desktop_sidecar"

    for provider_kind in ("contract_simulator", "scaffold", "dry_run"):
        with pytest.raises(ValidationError, match="provider_kind=desktop_sidecar"):
            VersionV1.model_validate(payload | {"provider_kind": provider_kind})

    development = VersionV1.model_validate(
        payload | {"build_channel": "development", "provider_kind": "contract_simulator"}
    )
    assert development.provider_kind == "contract_simulator"


def test_execution_mode_uses_exact_hyphenated_release_value() -> None:
    settings = ExecutionSettingsV1.model_validate(
        {
            "mode": "self-deployed",
            "capture_mode": "transcript",
            "token_level_metrics_available": False,
            "managed_model_id": "managed-model-1",
        }
    )
    assert settings.mode == "self-deployed"

    with pytest.raises(ValidationError, match="self-deployed"):
        ExecutionSettingsV1.model_validate(
            {"mode": "self_deployed", "managed_model_id": "managed-model-1"}
        )


def test_run_contract_rejects_runtime_model_path_and_command_overrides() -> None:
    valid = _run_create_payload()
    RunCreateV1.model_validate(valid)

    for field, value in (
        ("runtime", {"image": "unsafe"}),
        ("model", {"path": "/srv/model"}),
        ("workspace_path", "/srv/workspace"),
        ("command", "bash run.sh"),
        ("credential", "secret"),
    ):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            RunCreateV1.model_validate(valid | {field: value})

    queued = RunCreateV1.model_validate(
        valid | {"required_revision": valid["required_revision"] | {"state": "queued"}}
    )
    assert queued.required_revision.state == "queued"

    with pytest.raises(ValidationError, match="cannot require a terminal revision"):
        RunCreateV1.model_validate(
            valid | {"required_revision": valid["required_revision"] | {"state": "failed"}}
        )


def test_connection_contract_uses_the_renderer_remote_connection_phases() -> None:
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


def test_health_contract_preserves_native_instance_proof() -> None:
    plain = HealthV1.model_validate({"service": "openevo-sidecar", "status": "ok"})
    assert plain.protocol is None

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

    with pytest.raises(ValidationError, match="all be present"):
        HealthV1.model_validate(
            {
                "service": "openevo-sidecar",
                "status": "ok",
                "protocol": "openevo-native-sidecar-v1",
            }
        )


def test_artifact_preview_has_an_aggregate_budget_and_allows_empty_diff_lines() -> None:
    content = ArtifactContentV1.model_validate_json(
        json.dumps(
            {
                "artifact_id": "artifact-1",
                "content_digest": _DIGEST,
                "documents": [
                    {
                        "document_id": "memory",
                        "title": "Memory",
                        "media_type": "text/markdown",
                        "content": "hello",
                    }
                ],
                "total_documents": 1,
                "truncated": False,
            }
        )
    )
    assert content.total_documents == 1
    assert DiffLineV1(kind="context", old_line=1, new_line=1, text="").text == ""

    with pytest.raises(ValidationError, match="aggregate byte budget"):
        ArtifactContentV1.model_validate_json(
            json.dumps(
                {
                    "artifact_id": "artifact-1",
                    "content_digest": _DIGEST,
                    "documents": [
                        {
                            "document_id": f"document-{index}",
                            "title": "Large",
                            "media_type": "text/plain",
                            "content": "x" * 65_536,
                        }
                        for index in range(33)
                    ],
                    "total_documents": 33,
                    "truncated": False,
                }
            )
        )


def test_cross_language_critical_fixture_matches_python_contract() -> None:
    fixture_path = (
        Path(__file__).parents[3] / "desktop/sidecar/contracts/v1/fixtures/contract-critical.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    HealthV1.model_validate_json(json.dumps(fixture["health"]))
    DesktopStateV1.model_validate_json(json.dumps(fixture["state"]))
    RunCreateV1.model_validate_json(json.dumps(fixture["run_create"]))
    ArtifactContentV1.model_validate_json(json.dumps(fixture["artifact_content"]))
    assert fixture["artifact_diff"]["hunks"][0]["lines"][0]["text"] == ""


def test_bounded_json_detail_enforces_closed_resource_budgets() -> None:
    detail = BoundedJsonObjectV1.model_validate({"reason": "not_ready", "attempt": 1})
    assert detail.model_dump() == {"reason": "not_ready", "attempt": 1}

    too_deep: dict = {"leaf": True}
    for index in range(17):
        too_deep = {f"level_{index}": too_deep}
    with pytest.raises(ValidationError, match="depth budget"):
        BoundedJsonObjectV1.model_validate(too_deep)

    with pytest.raises(ValidationError, match="JavaScript safe"):
        BoundedJsonObjectV1.model_validate({"integer": 9_007_199_254_740_992})


def test_pagination_envelope_is_bounded_and_cursor_consistent() -> None:
    page = PageV1[int](items=(1, 2), next_cursor="cursor-2", has_more=True)
    assert page.items == (1, 2)

    with pytest.raises(ValidationError, match="must agree"):
        PageV1[int](items=(), next_cursor=None, has_more=True)
    with pytest.raises(ValidationError, match="at most 100"):
        PageV1[int](items=tuple(range(101)), next_cursor=None, has_more=False)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ArtifactPageV1.model_validate(
            {"items": (), "next_cursor": None, "has_more": False, "total": 0}
        )

    schema = desktop_openapi_document()
    list_paths = (
        "/desktop/v1/profiles",
        "/desktop/v1/projects",
        "/desktop/v1/operations/{operation_id}/logs",
        "/desktop/v1/runs",
        "/desktop/v1/runs/{run_id}/timeline",
        "/desktop/v1/runs/{run_id}/logs",
        "/desktop/v1/runs/{run_id}/artifacts",
        "/desktop/v1/services",
        "/desktop/v1/services/{service_id}/logs",
    )
    for path in list_paths:
        parameters = schema["paths"][path]["get"]["parameters"]
        query_names = {item["name"] for item in parameters if item["in"] == "query"}
        assert query_names == {"limit", "after", "sort", "direction"}


def test_event_envelope_and_sse_frame_are_closed_and_correlated() -> None:
    payload = {
        "event_id": "event-1",
        "event_name": "desktop.v1.heartbeat",
        "occurred_at": _NOW,
        "sequence": 7,
        "data": {"kind": "heartbeat"},
    }
    envelope = EventEnvelopeV1.model_validate(payload)
    frame = SseFrameV1(id="event-1", event="desktop.v1.heartbeat", data=envelope)
    assert frame.data.sequence == 7

    with pytest.raises(ValidationError, match="does not match"):
        EventEnvelopeV1.model_validate(payload | {"event_name": "desktop.v1.state.changed"})
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        EventEnvelopeV1.model_validate(payload | {"raw_payload": {}})
    with pytest.raises(ValidationError, match="must match"):
        SseFrameV1(id="event-2", event="desktop.v1.heartbeat", data=envelope)

    event_response = desktop_openapi_document()["paths"]["/desktop/v1/events"]["get"]
    assert "Last-Event-ID" in {parameter["name"] for parameter in event_response["parameters"]}
    assert set(event_response["responses"]["200"]["content"]) == {"text/event-stream"}


def test_all_contract_object_models_are_closed_except_explicit_bounded_maps() -> None:
    schemas = desktop_openapi_document()["components"]["schemas"]
    allowed_maps = {"BoundedJsonObjectV1", "EvolutionSelectionsV1"}
    open_object_schemas = {
        name
        for name, schema in schemas.items()
        if isinstance(schema, dict)
        and schema.get("type") == "object"
        and schema.get("additionalProperties") is not False
    }

    assert open_object_schemas == allowed_maps
    assert not any(
        "dict[str, Any]" in value for value in _walk_json(schemas) if isinstance(value, str)
    )
