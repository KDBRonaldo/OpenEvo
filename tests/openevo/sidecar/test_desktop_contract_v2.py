from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from desktop.sidecar.contracts.v2.app import create_desktop_local_v2_contract_app
from desktop.sidecar.contracts.v2.canonical import (
    DESKTOP_EVENTS_SCHEMA_SHA256,
    DESKTOP_OPENAPI_SHA256,
    EVENTS_SCHEMA_SNAPSHOT_PATH,
    OPENAPI_SNAPSHOT_PATH,
    canonical_json_bytes,
    desktop_events_schema_document,
    desktop_openapi_document,
)
from desktop.sidecar.contracts.v2.models import (
    AttemptRefV2,
    CoreTaskSubmitRequestV2,
    DesktopArtifactV2,
    DesktopDiagnosticV2,
    DesktopErrorV2,
    DesktopProjectV2,
    DesktopServiceV2,
    DesktopTaskV2,
    DesktopTransitionV2,
    DesktopVersionV2,
    EffectiveExecutionSnapshotRefV2,
    EvolutionRevisionRefV2,
    LegacyExplicitProfileV2,
    ProjectCapabilityProjectionV2,
    ProjectCreateV2,
    ProjectHeadRefV2,
    ProjectPatchV2,
    RemoteWorkspaceProfileV2,
    RuntimeContextSnapshotRefV2,
    SshHostCatalogV2,
    SshHostHintV2,
    SshPromptStateV2,
    SshTrustStateV2,
    SuccessorTransitionRefV2,
    SystemOpenSshProfileCreateV2,
    TaskAdmissionRefV2,
    WorkspaceSnapshotRefV2,
    evolution_capabilities_sha256_for,
)


EXPECTED_OPERATIONS = {
    ("get", "/version"),
    ("get", "/health"),
    ("get", "/desktop/v2/state"),
    ("get", "/desktop/v2/ssh-hosts"),
    ("post", "/desktop/v2/ssh-hosts/rescan"),
    ("get", "/desktop/v2/profiles"),
    ("post", "/desktop/v2/profiles"),
    ("get", "/desktop/v2/profiles/{profile_id}"),
    ("patch", "/desktop/v2/profiles/{profile_id}"),
    ("delete", "/desktop/v2/profiles/{profile_id}"),
    ("post", "/desktop/v2/profiles/{profile_id}/rebind"),
    ("post", "/desktop/v2/profiles/{profile_id}/connect"),
    ("post", "/desktop/v2/profiles/{profile_id}/disconnect"),
    ("post", "/desktop/v2/profiles/{profile_id}/host-key/review"),
    ("get", "/desktop/v2/projects"),
    ("post", "/desktop/v2/projects"),
    ("get", "/desktop/v2/projects/{project_id}"),
    ("patch", "/desktop/v2/projects/{project_id}"),
    ("post", "/desktop/v2/projects/{project_id}/activate"),
    ("get", "/desktop/v2/projects/{project_id}/capabilities"),
    ("post", "/desktop/v2/projects/{project_id}/validate"),
    ("get", "/desktop/v2/tasks"),
    ("post", "/desktop/v2/tasks"),
    ("get", "/desktop/v2/tasks/{task_id}"),
    ("post", "/desktop/v2/tasks/{task_id}/cancel"),
    ("post", "/desktop/v2/tasks/{task_id}/retry"),
    ("get", "/desktop/v2/tasks/{task_id}/timeline"),
    ("get", "/desktop/v2/tasks/{task_id}/logs"),
    ("get", "/desktop/v2/tasks/{task_id}/context"),
    ("get", "/desktop/v2/tasks/{task_id}/artifacts"),
    ("get", "/desktop/v2/project-heads/{project_head_id}"),
    ("get", "/desktop/v2/evolution-revisions/{evolution_revision_id}"),
    ("get", "/desktop/v2/runtime-contexts/{runtime_context_snapshot_id}"),
    ("get", "/desktop/v2/transitions/{transition_id}"),
    ("post", "/desktop/v2/transitions/{transition_id}/retry"),
    ("post", "/desktop/v2/transitions/{transition_id}/replace"),
    ("post", "/desktop/v2/transitions/{transition_id}/abandon"),
    ("get", "/desktop/v2/artifacts/{artifact_id}"),
    ("get", "/desktop/v2/artifacts/{artifact_id}/content"),
    ("get", "/desktop/v2/artifacts/{artifact_id}/diff"),
    ("get", "/desktop/v2/services"),
    ("post", "/desktop/v2/services/{service_id}/restart"),
    ("post", "/desktop/v2/diagnostics"),
    ("get", "/desktop/v2/diagnostics/{diagnostic_id}"),
    ("get", "/desktop/v2/events"),
}


def _json_model(model: type[Any], value: dict[str, Any]) -> Any:
    return model.model_validate_json(json.dumps(value))


def _prompt() -> dict[str, Any]:
    return {
        "schema_version": "2",
        "connection_generation": 3,
        "kind": "passphrase",
        "state": "pending",
        "requested_at": "2026-07-23T00:00:00Z",
    }


def _trust() -> dict[str, Any]:
    return {
        "schema_version": "2",
        "connection_generation": 3,
        "state": "trusted",
        "review_id": None,
        "review_sha256": None,
        "key_fingerprints": [],
        "repair_support": "not_needed",
    }


def _profile() -> dict[str, Any]:
    return {
        "schema_version": "2",
        "profile_kind": "system_openssh",
        "profile_id": "profile-1",
        "display_name": "Lab server",
        "connection_authority": "system_openssh",
        "ssh_host_alias": "evolab",
        "catalog_generation": 4,
        "connection_generation": 3,
        "connection_state": "connected",
        "prompt": None,
        "trust": _trust(),
        "failure": None,
        "active_project_id": "project-1",
        "core_api_major": 2,
        "core_openapi_sha256": "a" * 64,
        "core_event_schema_sha256": "b" * 64,
        "core_registry_sha256": "c" * 64,
        "created_at": "2026-07-23T00:00:00Z",
        "updated_at": "2026-07-23T00:00:01Z",
        "etag": '"' + ("d" * 64) + '"',
    }


def _science_config() -> dict[str, Any]:
    return {
        "schema_version": "2",
        "task": {
            "title": "Protein stability screen",
            "objective": "Rank the supplied variants and explain the evidence.",
        },
        "workspace": {
            "kind": "scratch",
            "display_name": "Protein stability inputs",
        },
        "execution": {
            "mode": "codex_subscription_transcript",
            "capture_mode": "transcript",
            "token_level_metrics_available": False,
            "harness_id": "codex",
            "codex_model": "gpt-5.5",
            "reasoning_effort": "high",
            "token_limit": 32768,
            "task_network_allow_internet": False,
        },
        "evolution": {"targets": {}},
    }


def _capabilities() -> dict[str, Any]:
    return {
        "schema_version": "1",
        "core_version": "0.1.9",
        "registry_digest": "c" * 64,
        "evaluated_profile": {
            "execution_mode": "subscription",
            "capture_mode": "transcript",
            "harness_id": "codex",
            "harness_capabilities": [],
            "runtime_capabilities": [],
        },
        "targets": [],
    }


def _legacy_profile() -> dict[str, Any]:
    return {
        "schema_version": "2",
        "profile_kind": "legacy_explicit",
        "profile_id": "legacy-profile-1",
        "display_name": "Old server",
        "connectable": False,
        "migration_state": "rebind_required",
        "created_at": "2026-07-22T00:00:00Z",
        "updated_at": "2026-07-23T00:00:00Z",
        "etag": '"' + ("e" * 64) + '"',
    }


def _walk(value: object) -> Iterator[object]:
    yield value
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def test_system_openssh_profile_create_has_only_alias_authority() -> None:
    payload = {
        "schema_version": "2",
        "display_name": "Lab server",
        "connection_authority": "system_openssh",
        "ssh_host_alias": "evolab",
    }
    created = _json_model(SystemOpenSshProfileCreateV2, payload)
    assert created.ssh_host_alias == "evolab"
    assert set(created.model_dump(mode="json")) == set(payload)

    forbidden = {
        "host": "10.0.0.2",
        "user": "researcher",
        "port": 22,
        "identity_path": "/Users/example/.ssh/id_ed25519",
        "authentication_kind": "private_key",
        "proxy_command": "helper",
        "known_hosts_path": "/Users/example/.ssh/known_hosts",
        "password": "secret",
    }
    for field, value in forbidden.items():
        with pytest.raises(ValidationError):
            _json_model(SystemOpenSshProfileCreateV2, {**payload, field: value})


@pytest.mark.parametrize(
    "alias",
    [
        "-oProxyCommand=bad",
        "user@host",
        "ssh://host",
        "/tmp/socket",
        "host name",
        "host*",
        "host?",
        "!host",
        "x" * 129,
    ],
)
def test_system_openssh_alias_is_bounded_literal(alias: str) -> None:
    with pytest.raises(ValidationError):
        _json_model(
            SystemOpenSshProfileCreateV2,
            {
                "schema_version": "2",
                "display_name": "Lab server",
                "connection_authority": "system_openssh",
                "ssh_host_alias": alias,
            },
        )


def test_ssh_host_catalog_is_hint_only_sorted_unique_and_path_free() -> None:
    hint = {
        "schema_version": "2",
        "ssh_host_alias": "evolab",
        "availability": "selectable",
        "source_kind": "literal_host",
    }
    assert _json_model(SshHostHintV2, hint).ssh_host_alias == "evolab"
    catalog = {
        "schema_version": "2",
        "catalog_generation": 7,
        "hosts": [hint, {**hint, "ssh_host_alias": "gpu-lab"}],
        "warnings": [
            {
                "schema_version": "2",
                "code": "dynamic_hosts_not_enumerated",
                "action": "manual_alias_available",
                "affected_entry_count": 2,
            }
        ],
        "scanned_at": "2026-07-23T00:00:00Z",
    }
    parsed = _json_model(SshHostCatalogV2, catalog)
    assert [host.ssh_host_alias for host in parsed.hosts] == ["evolab", "gpu-lab"]

    duplicate = json.loads(json.dumps(catalog))
    duplicate["hosts"].append(hint)
    with pytest.raises(ValidationError, match="unique"):
        _json_model(SshHostCatalogV2, duplicate)
    unsorted = json.loads(json.dumps(catalog))
    unsorted["hosts"].reverse()
    with pytest.raises(ValidationError, match="sorted"):
        _json_model(SshHostCatalogV2, unsorted)
    with pytest.raises(ValidationError):
        _json_model(SshHostHintV2, {**hint, "source_path": "/Users/example/.ssh/config"})


def test_connectable_profile_preserves_alias_without_flattening() -> None:
    profile = _json_model(RemoteWorkspaceProfileV2, _profile())
    assert profile.connection_authority == "system_openssh"
    assert profile.ssh_host_alias == "evolab"
    assert profile.core_api_major == 2

    with pytest.raises(ValidationError, match="connected profile"):
        payload = _profile()
        payload["core_openapi_sha256"] = None
        _json_model(RemoteWorkspaceProfileV2, payload)
    with pytest.raises(ValidationError, match="connection generation"):
        payload = _profile()
        payload["trust"]["connection_generation"] = 2
        _json_model(RemoteWorkspaceProfileV2, payload)


def test_legacy_profile_is_nonconnectable_and_contains_no_old_endpoint_fields() -> None:
    legacy = _json_model(LegacyExplicitProfileV2, _legacy_profile())
    assert legacy.connectable is False
    assert legacy.migration_state == "rebind_required"
    assert set(legacy.model_dump(mode="json")) == set(_legacy_profile())

    for field in ("host", "user", "port", "identity_path", "authentication_kind"):
        with pytest.raises(ValidationError):
            _json_model(LegacyExplicitProfileV2, {**_legacy_profile(), field: "legacy"})


def test_prompt_state_exposes_kind_and_lifecycle_but_no_prompt_or_secret() -> None:
    prompt = _json_model(SshPromptStateV2, _prompt())
    assert prompt.kind == "passphrase"
    assert prompt.state == "pending"
    for field in ("prompt", "prompt_text", "response", "secret", "credential_ref"):
        with pytest.raises(ValidationError):
            _json_model(SshPromptStateV2, {**_prompt(), field: "do not expose"})


def test_trust_state_requires_bounded_review_identity_for_first_or_changed_key() -> None:
    review = {
        **_trust(),
        "state": "changed_key_blocked",
        "review_id": "host-review-1",
        "review_sha256": "a" * 64,
        "key_fingerprints": [
            {
                "schema_version": "2",
                "algorithm": "ssh-ed25519",
                "sha256_fingerprint": "SHA256:" + ("A" * 43),
                "role": "presented",
            }
        ],
        "repair_support": "automatic_replacement_available",
    }
    assert _json_model(SshTrustStateV2, review).review_id == "host-review-1"

    with pytest.raises(ValidationError, match="review identity"):
        _json_model(SshTrustStateV2, {**review, "review_id": None})
    with pytest.raises(ValidationError, match="must not retain review"):
        _json_model(SshTrustStateV2, {**_trust(), "review_id": "stale-review"})


def test_desktop_errors_expose_only_typed_bounded_actions() -> None:
    error = {
        "schema_version": "2",
        "code": "ssh_host_key_changed",
        "summary": "The configured server identity changed.",
        "retryable": False,
        "action": "review_host_key",
        "affected_resource_id": "profile-1",
    }
    assert _json_model(DesktopErrorV2, error).action == "review_host_key"
    for field in ("details", "exception", "stderr", "command", "host_path", "core_url"):
        with pytest.raises(ValidationError):
            _json_model(DesktopErrorV2, {**error, field: "forbidden"})


def test_local_contract_projects_all_distinct_core_v2_authorities() -> None:
    assert ProjectHeadRefV2.model_fields["project_head_id"]
    assert EvolutionRevisionRefV2.model_fields["evolution_revision_id"]
    assert RuntimeContextSnapshotRefV2.model_fields["runtime_context_snapshot_id"]
    assert EffectiveExecutionSnapshotRefV2.model_fields[
        "effective_execution_snapshot_id"
    ]
    assert WorkspaceSnapshotRefV2.model_fields["workspace_snapshot_id"]
    assert TaskAdmissionRefV2.model_fields["task_admission_id"]
    assert AttemptRefV2.model_fields["attempt_id"]
    assert SuccessorTransitionRefV2.model_fields["successor_transition_id"]

    for projection in (
        DesktopProjectV2,
        DesktopTaskV2,
        DesktopTransitionV2,
        DesktopArtifactV2,
        DesktopServiceV2,
        DesktopDiagnosticV2,
    ):
        schema = projection.model_json_schema(mode="validation")
        assert schema["additionalProperties"] is False


def test_desktop_project_mutations_forward_complete_config_not_digest_only() -> None:
    create_payload = {
        "schema_version": "2",
        "profile_id": "profile-1",
        "profile_connection_generation": 3,
        "display_name": "Protein stability",
        "config": _science_config(),
    }
    create = _json_model(ProjectCreateV2, create_payload)
    assert create.config.task.title == "Protein stability screen"
    assert "project_config_sha256" not in type(create).model_fields

    patch_payload = {
        "schema_version": "2",
        "expected_project_head_id": "project-head-0",
        "expected_project_head_manifest_sha256": "a" * 64,
        "expected_project_config_sha256": "b" * 64,
        "display_name": "Protein stability v2",
        "config": _science_config(),
    }
    assert _json_model(ProjectPatchV2, patch_payload).config == create.config

    with pytest.raises(ValidationError):
        _json_model(
            ProjectCreateV2,
            {
                **create_payload,
                "config": None,
                "project_config_sha256": "b" * 64,
            },
        )


def test_desktop_capability_projection_contains_the_complete_remote_envelope() -> None:
    capabilities = _capabilities()
    digest = evolution_capabilities_sha256_for(capabilities)
    payload = {
        "schema_version": "2",
        "project_id": "project-1",
        "execution_mode": "codex_subscription_transcript",
        "registry_sha256": "c" * 64,
        "capabilities_sha256": digest,
        "capabilities": capabilities,
        "fetched_at": "2026-07-23T00:00:00Z",
    }
    projection = _json_model(ProjectCapabilityProjectionV2, payload)
    assert projection.capabilities.model_dump(mode="json") == capabilities
    assert "target_ids" not in type(projection).model_fields

    with pytest.raises(ValidationError, match="digest"):
        _json_model(
            ProjectCapabilityProjectionV2,
            {**payload, "capabilities_sha256": "f" * 64},
        )
    with pytest.raises(ValidationError, match="registry"):
        _json_model(
            ProjectCapabilityProjectionV2,
            {**payload, "registry_sha256": "f" * 64},
        )


def test_desktop_task_submit_forwards_only_project_authority_cas() -> None:
    payload = {
        "schema_version": "2",
        "project_id": "project-1",
        "expected_project_admission_etag": '"' + ("f" * 64) + '"',
        "expected_project_head_id": "project-head-0",
        "expected_project_head_manifest_sha256": "a" * 64,
        "expected_project_config_sha256": "b" * 64,
    }
    request = _json_model(CoreTaskSubmitRequestV2, payload)
    assert set(request.model_dump(mode="json")) == set(payload)
    with pytest.raises(ValidationError):
        _json_model(
            CoreTaskSubmitRequestV2,
            {**payload, "workspace_snapshot": {"host_path": "/srv/project"}},
        )


def test_desktop_v2_schema_has_no_forbidden_renderer_authority_fields() -> None:
    schema = desktop_openapi_document()
    forbidden_exact = {
        "host",
        "user",
        "port",
        "identity_path",
        "authentication_kind",
        "proxy_command",
        "known_hosts_path",
        "ssh_command",
        "credential_ref",
        "core_url",
        "backend_token",
        "host_path",
        "remote_path",
        "revision",
    }
    keys = {value for value in _walk(schema) if isinstance(value, str)}
    assert not (keys & forbidden_exact)


def test_desktop_v2_route_inventory_is_exact_and_authenticated() -> None:
    app = create_desktop_local_v2_contract_app()
    schema = app.openapi()
    operations = {
        (method, path)
        for path, item in schema["paths"].items()
        for method in item
        if method in {"get", "post", "patch", "delete", "put"}
    }
    assert operations == EXPECTED_OPERATIONS
    assert "/desktop/v1/state" not in schema["paths"]
    assert schema["x-openevo-contract-only"] is True
    assert schema["paths"]["/version"]["get"]["x-openevo-discovery-only"] is True
    assert (
        schema["paths"]["/version"]["get"]["x-openevo-mutation-compatible"]
        is False
    )
    assert schema["components"]["securitySchemes"]["DesktopSessionV2"]["name"] == (
        "X-OpenEvo-Desktop-Session"
    )
    for method, path in EXPECTED_OPERATIONS:
        operation = schema["paths"][path][method]
        if path.startswith("/desktop/v2"):
            assert operation["security"] == [{"DesktopSessionV2": []}]

    client = TestClient(app)
    assert client.get("/version").status_code == 501
    assert client.get("/desktop/v2/state").status_code == 401
    assert (
        client.get(
            "/desktop/v2/state",
            headers={"X-OpenEvo-Desktop-Session": "session-token"},
        ).status_code
        == 501
    )


def test_mutation_routes_require_idempotency_and_resource_generation() -> None:
    schema = desktop_openapi_document()
    for method, path in EXPECTED_OPERATIONS:
        if method not in {"post", "patch", "delete", "put"}:
            continue
        parameters = schema["paths"][path][method].get("parameters", [])
        required_headers = {
            item["name"]
            for item in parameters
            if item["in"] == "header" and item.get("required")
        }
        assert "Idempotency-Key" in required_headers, (method, path)
        assert "X-OpenEvo-Resource-Generation" in required_headers, (method, path)


def test_desktop_v2_snapshots_are_exact_and_frozen() -> None:
    openapi = canonical_json_bytes(desktop_openapi_document())
    events = canonical_json_bytes(desktop_events_schema_document())
    assert OPENAPI_SNAPSHOT_PATH.read_bytes() == openapi
    assert EVENTS_SCHEMA_SNAPSHOT_PATH.read_bytes() == events
    assert hashlib.sha256(openapi).hexdigest() == DESKTOP_OPENAPI_SHA256
    assert hashlib.sha256(events).hexdigest() == DESKTOP_EVENTS_SCHEMA_SHA256
    assert DESKTOP_OPENAPI_SHA256 == (
        "987116bff9919930af0177567b4e2a549b3acc2e4dcf1780a1bccccc6530f672"
    )
    assert DESKTOP_EVENTS_SCHEMA_SHA256 == (
        "bc1dbc7b3bf7a68e02ba87adf35bd75f511382bf665afc33cae436110d8aea28"
    )


@pytest.mark.parametrize(
    ("model", "factory"),
    [
        (RemoteWorkspaceProfileV2, _profile),
        (LegacyExplicitProfileV2, _legacy_profile),
        (SshPromptStateV2, _prompt),
        (SshTrustStateV2, _trust),
    ],
)
def test_local_v2_models_are_closed_strict_and_immutable(
    model: type[Any], factory: Callable[[], dict[str, Any]]
) -> None:
    payload = factory()
    parsed = _json_model(model, payload)
    with pytest.raises(ValidationError):
        _json_model(model, {**payload, "schema_version": 2})
    with pytest.raises(ValidationError):
        setattr(parsed, next(iter(model.model_fields)), "changed")
    assert model.model_json_schema(mode="validation")["additionalProperties"] is False


def _feature_set_sha256(features: list[str]) -> str:
    return hashlib.sha256(
        json.dumps(features, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()


def _desktop_v2_discovery() -> dict[str, Any]:
    features = [
        "core_control_v2",
        "system_openssh_profiles",
        "task_admission_v2",
    ]
    return {
        "schema_version": "2",
        "api_name": "openevo-desktop-local-api",
        "preferred_major": 2,
        "supported_majors": [2],
        "mutation_major": 2,
        "openapi_sha256": DESKTOP_OPENAPI_SHA256,
        "event_schema_sha256": DESKTOP_EVENTS_SCHEMA_SHA256,
        "release_version": "0.1.9",
        "build_id": "a" * 64,
        "source_commit": "abcdef0",
        "build_channel": "release",
        "provider_kind": "desktop_sidecar",
        "feature_flags": features,
        "feature_set_sha256": _feature_set_sha256(features),
        "required_core_api_major": 2,
        "mutation_compatible": True,
    }


def test_desktop_v2_discovery_binds_release_build_schema_and_feature_set() -> None:
    version = _json_model(DesktopVersionV2, _desktop_v2_discovery())
    assert version.preferred_major == 2
    assert version.openapi_sha256 == DESKTOP_OPENAPI_SHA256
    assert version.event_schema_sha256 == DESKTOP_EVENTS_SCHEMA_SHA256
    assert version.required_core_api_major == 2

    payload = _desktop_v2_discovery()
    payload["feature_flags"] = list(reversed(payload["feature_flags"]))
    with pytest.raises(ValidationError, match="sorted"):
        _json_model(DesktopVersionV2, payload)
    payload = _desktop_v2_discovery()
    payload["feature_set_sha256"] = "f" * 64
    with pytest.raises(ValidationError, match="feature-set digest"):
        _json_model(DesktopVersionV2, payload)


def test_desktop_v1_discovery_cannot_authorize_v2_mutation() -> None:
    legacy = {
        "schema_version": "1",
        "api_name": "openevo-desktop-local-api",
        "preferred_major": 1,
        "supported_majors": [1],
        "openapi_sha256": "a" * 64,
        "build_version": "0.1.8",
        "source_commit": "abcdef0",
        "build_channel": "release",
        "provider_kind": "desktop_sidecar",
        "feature_flags": ["remote_profiles"],
    }
    with pytest.raises(ValidationError):
        _json_model(DesktopVersionV2, legacy)
