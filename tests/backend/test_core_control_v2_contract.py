from __future__ import annotations

import json
import hashlib
import importlib.util
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from openevo.backend.contracts.v2.app import create_core_control_v2_contract_app
from openevo.backend.contracts.v2.provider import RELEASE_DAEMON_FEATURE_FLAGS_V2
from openevo.backend.contracts.v2.models import (
    AttemptRefV2,
    CodexSubscriptionExecutionSettingsV2,
    ContractOfferV2,
    EffectiveExecutionSnapshotRefV2,
    EvolutionRevisionRefV2,
    ProjectCreateV2,
    ProjectHeadRefV2,
    ProjectUpdateV2,
    RuntimeContextSnapshotRefV2,
    ScienceProjectConfigV2,
    SseFrameV2,
    SuccessorTransitionRefV2,
    TaskAdmittedEventV2,
    TaskAdmissionRefV2,
    TaskSubmitRequestV2,
    VersionResponseV2,
    WorkspaceArchiveDeclarationV2,
    WorkspaceSnapshotRefV2,
    WorkspaceUploadCreateV2,
    WorkspaceUploadSessionV2,
    project_config_sha256_for,
)
from openevo.backend.contracts.v2.snapshots import (
    EVENTS_SCHEMA_SNAPSHOT_PATH,
    OPENAPI_SNAPSHOT_PATH,
    build_events_schema_document,
    build_openapi_document,
    canonical_contract_bytes,
    canonical_contract_sha256,
    canonical_json_bytes,
    events_schema_sha256,
    openapi_sha256,
    parse_contract_json_bytes,
)


EXPECTED_OPERATIONS = {
    ("GET", "/version"),
    ("GET", "/health"),
    ("GET", "/v2/system/status"),
    ("GET", "/v2/capabilities"),
    ("GET", "/v2/projects"),
    ("POST", "/v2/projects"),
    ("GET", "/v2/projects/{project_id}"),
    ("PATCH", "/v2/projects/{project_id}"),
    ("POST", "/v2/projects/{project_id}/workspace-uploads"),
    ("GET", "/v2/projects/{project_id}/workspace-uploads/{upload_id}"),
    ("PUT", "/v2/projects/{project_id}/workspace-uploads/{upload_id}/chunks/{chunk_index}"),
    ("POST", "/v2/projects/{project_id}/workspace-uploads/{upload_id}/finalize"),
    ("POST", "/v2/projects/{project_id}/workspace-uploads/{upload_id}/abort"),
    ("POST", "/v2/projects/{project_id}/validate"),
    ("GET", "/v2/projects/{project_id}/heads"),
    ("GET", "/v2/projects/{project_id}/heads/active"),
    ("GET", "/v2/project-heads/{project_head_id}"),
    ("GET", "/v2/projects/{project_id}/transitions"),
    ("GET", "/v2/transitions/{successor_transition_id}"),
    ("POST", "/v2/transitions/{successor_transition_id}/retry"),
    ("POST", "/v2/transitions/{successor_transition_id}/abandon"),
    ("GET", "/v2/tasks"),
    ("POST", "/v2/tasks"),
    ("GET", "/v2/tasks/{task_id}"),
    ("GET", "/v2/tasks/{task_id}/admission"),
    ("GET", "/v2/tasks/{task_id}/attempts"),
    ("POST", "/v2/tasks/{task_id}/attempts"),
    ("GET", "/v2/tasks/{task_id}/attempts/{attempt_id}"),
    ("POST", "/v2/tasks/{task_id}/attempts/{attempt_id}/cancel"),
    ("POST", "/v2/tasks/{task_id}/close"),
    ("GET", "/v2/tasks/{task_id}/timeline"),
    ("GET", "/v2/tasks/{task_id}/logs"),
    ("GET", "/v2/tasks/{task_id}/context"),
    ("GET", "/v2/tasks/{task_id}/artifacts"),
    ("GET", "/v2/projects/{project_id}/artifacts/{artifact_id}"),
    ("GET", "/v2/projects/{project_id}/artifacts/{artifact_id}/content"),
    ("GET", "/v2/services"),
    ("GET", "/v2/services/{service_id}"),
    ("POST", "/v2/services/{service_id}/restart"),
    ("GET", "/v2/services/{service_id}/logs"),
    ("GET", "/v2/operations/{operation_id}"),
    ("POST", "/v2/operations/{operation_id}/cancel"),
    ("POST", "/v2/diagnostics"),
    ("GET", "/v2/diagnostics/{diagnostic_id}"),
    ("DELETE", "/v2/diagnostics/{diagnostic_id}"),
    ("POST", "/v2/maintenance/cache-cleanup"),
    ("GET", "/v2/events"),
}


def _json_model(model: type[Any], value: dict[str, Any]) -> Any:
    return model.model_validate_json(json.dumps(value))


def _workspace(project_id: str = "project-1", seed: str = "1") -> dict[str, Any]:
    return {
        "schema_version": "2",
        "workspace_snapshot_id": f"workspace-{seed}",
        "project_id": project_id,
        "manifest_sha256": seed * 64,
        "entry_count": 7,
        "byte_size": 1024,
    }


def _project_config() -> dict[str, Any]:
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
        "evolution": {
            "targets": {
                "text_memory": {
                    "enabled": False,
                    "method": None,
                    "config": {},
                }
            }
        },
    }


def _workspace_archive() -> dict[str, Any]:
    return {
        "format": "openevo_deterministic_tar_v1",
        "media_type": "application/vnd.openevo.workspace-tar",
        "content_sha256": "d" * 64,
        "byte_size": 1024,
        "entry_count": 0,
        "extracted_byte_size": 0,
    }


def _evolution(project_id: str = "project-1", seed: str = "2") -> dict[str, Any]:
    return {
        "schema_version": "2",
        "evolution_revision_id": f"evolution-{seed}",
        "project_id": project_id,
        "manifest_sha256": seed * 64,
        "artifact_count": 3,
    }


def _runtime_context(
    project_id: str = "project-1",
    *,
    evolution: dict[str, Any] | None = None,
    seed: str = "3",
) -> dict[str, Any]:
    evolution = evolution or _evolution(project_id)
    return {
        "schema_version": "2",
        "runtime_context_snapshot_id": f"runtime-context-{seed}",
        "project_id": project_id,
        "evolution_revision_id": evolution["evolution_revision_id"],
        "evolution_revision_manifest_sha256": evolution["manifest_sha256"],
        "registry_sha256": "a" * 64,
        "runtime_contract_sha256": "b" * 64,
        "manifest_sha256": seed * 64,
    }


def _execution(project_id: str = "project-1", seed: str = "4") -> dict[str, Any]:
    return {
        "schema_version": "2",
        "effective_execution_snapshot_id": f"execution-{seed}",
        "project_id": project_id,
        "execution_mode": "codex_subscription_transcript",
        "capture_mode": "transcript",
        "token_level_metrics_available": False,
        "producer_id": "subscription-snapshot-issuer-v1",
        "snapshot_sha256": seed * 64,
    }


def _head(
    project_id: str = "project-1",
    *,
    generation: int = 7,
    workspace: dict[str, Any] | None = None,
    evolution: dict[str, Any] | None = None,
    runtime_context: dict[str, Any] | None = None,
    execution: dict[str, Any] | None = None,
    seed: str = "5",
) -> dict[str, Any]:
    workspace = workspace or _workspace(project_id)
    evolution = evolution or _evolution(project_id)
    runtime_context = runtime_context or _runtime_context(
        project_id, evolution=evolution
    )
    execution = execution or _execution(project_id)
    return {
        "schema_version": "2",
        "project_head_id": f"project-head-{generation}",
        "project_id": project_id,
        "generation": generation,
        "predecessor_project_head_id": (
            None if generation == 0 else f"project-head-{generation - 1}"
        ),
        "workspace_snapshot": workspace,
        "evolution_revision": evolution,
        "runtime_context_snapshot": runtime_context,
        "effective_execution_snapshot": execution,
        "registry_sha256": runtime_context["registry_sha256"],
        "manifest_sha256": seed * 64,
    }


def _admission(
    project_id: str = "project-1",
    *,
    head: dict[str, Any] | None = None,
    workspace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    head = head or _head(project_id)
    workspace = workspace or _workspace(project_id, "7")
    payload = {
        "schema_version": "2",
        "task_admission_id": "admission-1",
        "task_id": "task-1",
        "project_id": project_id,
        "predecessor_project_head": head,
        "workspace_snapshot": workspace,
        "project_config_sha256": "8" * 64,
        "task_envelope_sha256": "9" * 64,
        "normalized_evolution_intent_sha256": "c" * 64,
        "registry_sha256": head["registry_sha256"],
        "admitted_at": "2026-07-23T00:00:00Z",
    }
    payload["admission_sha256"] = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return payload


def _attempt(
    project_id: str = "project-1",
    *,
    admission: dict[str, Any] | None = None,
) -> dict[str, Any]:
    admission = admission or _admission(project_id)
    return {
        "schema_version": "2",
        "attempt_id": "attempt-1",
        "ordinal": 1,
        "task_id": admission["task_id"],
        "task_admission_id": admission["task_admission_id"],
        "admission_sha256": admission["admission_sha256"],
        "project_id": project_id,
        "predecessor_project_head_id": admission["predecessor_project_head"][
            "project_head_id"
        ],
        "created_at": "2026-07-23T00:00:01Z",
    }


def _transition(
    project_id: str = "project-1",
    *,
    admission: dict[str, Any] | None = None,
    attempt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    admission = admission or _admission(project_id)
    attempt = attempt or _attempt(project_id, admission=admission)
    predecessor = admission["predecessor_project_head"]
    successor = _head(project_id, generation=predecessor["generation"] + 1, seed="d")
    successor["predecessor_project_head_id"] = predecessor["project_head_id"]
    return {
        "schema_version": "2",
        "successor_transition_id": "transition-1",
        "project_id": project_id,
        "kind": "run_result",
        "predecessor_project_head": predecessor,
        "expected_successor_generation": predecessor["generation"] + 1,
        "plan_sha256": "e" * 64,
        "task_admission": admission,
        "accepted_attempt": attempt,
        "successor_project_head": successor,
    }


MODEL_CASES: tuple[tuple[type[Any], Callable[[], dict[str, Any]]], ...] = (
    (WorkspaceSnapshotRefV2, _workspace),
    (EvolutionRevisionRefV2, _evolution),
    (RuntimeContextSnapshotRefV2, _runtime_context),
    (EffectiveExecutionSnapshotRefV2, _execution),
    (ProjectHeadRefV2, _head),
    (TaskAdmissionRefV2, _admission),
    (AttemptRefV2, _attempt),
    (SuccessorTransitionRefV2, _transition),
)


@pytest.mark.parametrize(("model", "factory"), MODEL_CASES)
def test_v2_authority_refs_are_closed_strict_bounded_and_immutable(
    model: type[Any], factory: Callable[[], dict[str, Any]]
) -> None:
    payload = factory()
    instance = _json_model(model, payload)

    with pytest.raises(ValidationError):
        model.model_validate({**payload, next(iter(payload)): 7})
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "revision": "ambiguous"})
    for forbidden in ("host_path", "uri", "env", "secret", "metadata"):
        with pytest.raises(ValidationError):
            _json_model(model, {**payload, forbidden: {"value": "forbidden"}})
    with pytest.raises(ValidationError):
        setattr(instance, next(iter(payload)), "changed")

    schema = model.model_json_schema(mode="validation")
    assert schema["additionalProperties"] is False
    assert not any(
        isinstance(value, dict) and value.get("additionalProperties") is True
        for value in schema.get("$defs", {}).values()
    )


@pytest.mark.parametrize("bad_id", ["/tmp/workspace", "file://artifact", "ssh://host"])
def test_v2_opaque_identity_rejects_paths_and_uris(bad_id: str) -> None:
    with pytest.raises(ValidationError):
        _json_model(WorkspaceSnapshotRefV2, {**_workspace(), "workspace_snapshot_id": bad_id})


def test_v2_numeric_fields_do_not_coerce_and_are_javascript_safe() -> None:
    with pytest.raises(ValidationError):
        _json_model(WorkspaceSnapshotRefV2, {**_workspace(), "entry_count": "7"})
    with pytest.raises(ValidationError):
        _json_model(ProjectHeadRefV2, {**_head(), "generation": (1 << 53)})
    with pytest.raises(ValidationError):
        _json_model(AttemptRefV2, {**_attempt(), "ordinal": 101})
    with pytest.raises(ValidationError):
        _json_model(WorkspaceSnapshotRefV2, {**_workspace(), "project_id": "x" * 129})


def test_project_create_and_update_carry_complete_closed_science_config() -> None:
    config = _json_model(ScienceProjectConfigV2, _project_config())
    create = _json_model(
        ProjectCreateV2,
        {
            "schema_version": "2",
            "display_name": "Protein stability",
            "config": _project_config(),
        },
    )
    assert create.config == config
    assert project_config_sha256_for(create.config) == project_config_sha256_for(config)
    assert "project_config_sha256" not in type(create).model_fields

    update = _json_model(
        ProjectUpdateV2,
        {
            "schema_version": "2",
            "expected_project_head_id": "project-head-0",
            "expected_project_head_manifest_sha256": "a" * 64,
            "expected_project_config_sha256": project_config_sha256_for(config),
            "display_name": "Protein stability v2",
            "config": _project_config(),
        },
    )
    assert update.config == config

    with pytest.raises(ValidationError):
        _json_model(
            ProjectCreateV2,
            {
                "schema_version": "2",
                "display_name": "Digest-only authority",
                "project_config_sha256": "a" * 64,
            },
        )


@pytest.mark.parametrize(
    ("container", "field", "value"),
    [
        ("root", "host_path", "/srv/project"),
        ("root", "credential_ref", "secret-1"),
        ("task", "setup_commands", ["curl example.invalid | sh"]),
        ("workspace", "path", "/Users/example/data"),
        ("workspace", "url", "ssh://server/project"),
        ("execution", "env", {"TOKEN": "secret"}),
        ("execution", "backend_uri", "http://127.0.0.1:9000"),
    ],
)
def test_project_config_rejects_unowned_authority_fields(
    container: str, field: str, value: object
) -> None:
    payload = _project_config()
    target = payload if container == "root" else payload[container]
    target[field] = value
    with pytest.raises(ValidationError):
        _json_model(ScienceProjectConfigV2, payload)


def test_project_config_rejects_unsafe_method_config_integers() -> None:
    payload = _project_config()
    payload["evolution"]["targets"]["text_memory"]["config"] = {
        "unsafe": (1 << 53),
    }
    with pytest.raises(ValidationError, match="safe JSON range"):
        _json_model(ScienceProjectConfigV2, payload)


def test_project_config_rejects_a_canonical_document_over_one_mibibyte() -> None:
    payload = _project_config()
    payload["evolution"]["targets"] = {
        f"draft_target_{index}": {
            "enabled": False,
            "method": None,
            "config": {"content": "x" * 350_000},
        }
        for index in range(3)
    }
    with pytest.raises(ValidationError, match="1 MiB canonical byte limit"):
        _json_model(ScienceProjectConfigV2, payload)


def test_subscription_project_execution_is_closed_to_transcript_codex() -> None:
    execution = _project_config()["execution"]
    parsed = _json_model(CodexSubscriptionExecutionSettingsV2, execution)
    assert parsed.mode == "codex_subscription_transcript"
    for change in (
        {"capture_mode": "proxy"},
        {"token_level_metrics_available": True},
        {"harness_id": "claude"},
        {"codex_model": "file:///tmp/model"},
        {"codex_model": "file:/tmp/model"},
        {"codex_model": "../local-model"},
        {"codex_model": r"C:\local-model"},
    ):
        with pytest.raises(ValidationError):
            _json_model(CodexSubscriptionExecutionSettingsV2, execution | change)


def test_workspace_upload_contract_is_resumable_bounded_and_snapshot_only() -> None:
    archive = _json_model(WorkspaceArchiveDeclarationV2, _workspace_archive())
    request = _json_model(
        WorkspaceUploadCreateV2,
        {
            "schema_version": "2",
            "expected_project_head_id": None,
            "expected_project_head_manifest_sha256": None,
            "expected_project_config_sha256": "a" * 64,
            "archive": _workspace_archive(),
            "chunk_byte_size": 1024,
            "chunk_count": 1,
        },
    )
    assert request.archive == archive

    session = _json_model(
        WorkspaceUploadSessionV2,
        {
            "schema_version": "2",
            "upload_id": "workspace-upload-1",
            "project_id": "project-1",
            "state": "open",
            "expected_project_head_id": None,
            "expected_project_head_manifest_sha256": None,
            "expected_project_config_sha256": "a" * 64,
            "archive": _workspace_archive(),
            "chunk_byte_size": 1024,
            "chunk_count": 1,
            "next_chunk_index": 0,
            "accepted_byte_size": 0,
            "workspace_snapshot": None,
            "created_at": "2026-07-23T00:00:00Z",
            "updated_at": "2026-07-23T00:00:00Z",
            "etag": '"' + ("b" * 64) + '"',
        },
    )
    assert session.next_chunk_index == 0

    with pytest.raises(ValidationError, match="chunk count"):
        _json_model(
            WorkspaceUploadCreateV2,
            {
                **request.model_dump(mode="json"),
                "chunk_count": 2,
            },
        )
    with pytest.raises(ValidationError, match="together"):
        _json_model(
            WorkspaceUploadCreateV2,
            {
                **request.model_dump(mode="json"),
                "expected_project_head_id": "project-head-0",
            },
        )
    with pytest.raises(ValidationError):
        _json_model(
            WorkspaceArchiveDeclarationV2,
            {**_workspace_archive(), "source_path": "/Users/example/data"},
        )


def test_task_submit_contains_only_authority_cas_and_no_client_admission_pins() -> None:
    payload = {
        "schema_version": "2",
        "project_id": "project-1",
        "expected_project_admission_etag": '"' + ("f" * 64) + '"',
        "expected_project_head_id": "project-head-0",
        "expected_project_head_manifest_sha256": "a" * 64,
        "expected_project_config_sha256": "b" * 64,
    }
    request = _json_model(TaskSubmitRequestV2, payload)
    assert set(request.model_dump(mode="json")) == set(payload)
    for field, value in (
        ("task_envelope_sha256", "c" * 64),
        ("normalized_evolution_intent_sha256", "d" * 64),
        ("expected_registry_sha256", "e" * 64),
        ("workspace_snapshot", _workspace()),
    ):
        with pytest.raises(ValidationError):
            _json_model(TaskSubmitRequestV2, payload | {field: value})


def test_runtime_context_binds_exact_evolution_manifest() -> None:
    payload = _runtime_context()
    payload["evolution_revision_manifest_sha256"] = "f" * 64
    head = _head(runtime_context=payload)
    with pytest.raises(ValidationError, match="evolution revision manifest"):
        _json_model(ProjectHeadRefV2, head)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (
            lambda value: value["workspace_snapshot"].update(project_id="project-2"),
            "workspace snapshot belongs to another project",
        ),
        (
            lambda value: value["evolution_revision"].update(project_id="project-2"),
            "evolution revision belongs to another project",
        ),
        (
            lambda value: value["runtime_context_snapshot"].update(
                project_id="project-2"
            ),
            "runtime context belongs to another project",
        ),
        (
            lambda value: value["effective_execution_snapshot"].update(
                project_id="project-2"
            ),
            "effective execution snapshot belongs to another project",
        ),
        (
            lambda value: value.update(registry_sha256="f" * 64),
            "registry digest",
        ),
    ],
)
def test_project_head_binds_all_exact_authority_refs(
    mutate: Callable[[dict[str, Any]], None], message: str
) -> None:
    payload = _head()
    mutate(payload)
    with pytest.raises(ValidationError, match=message):
        _json_model(ProjectHeadRefV2, payload)


def test_project_head_generation_requires_exact_predecessor_shape() -> None:
    genesis = _head(generation=0)
    assert _json_model(ProjectHeadRefV2, genesis).predecessor_project_head_id is None

    with pytest.raises(ValidationError, match="generation zero"):
        _json_model(
            ProjectHeadRefV2,
            {**genesis, "predecessor_project_head_id": "project-head-old"},
        )
    with pytest.raises(ValidationError, match="nonzero generation"):
        _json_model(
            ProjectHeadRefV2,
            {**_head(generation=1), "predecessor_project_head_id": None},
        )


def test_subscription_execution_snapshot_requires_transcript_without_token_metrics() -> None:
    with pytest.raises(ValidationError, match="subscription execution requires transcript"):
        _json_model(
            EffectiveExecutionSnapshotRefV2,
            {**_execution(), "capture_mode": "proxy"},
        )
    with pytest.raises(ValidationError, match="token-level metrics"):
        _json_model(
            EffectiveExecutionSnapshotRefV2,
            {**_execution(), "token_level_metrics_available": True},
        )


def test_task_admission_and_attempt_bind_immutable_ownership() -> None:
    admission = _admission()
    assert _json_model(TaskAdmissionRefV2, admission).task_id == "task-1"
    attempt = _attempt(admission=admission)
    assert _json_model(AttemptRefV2, attempt).task_admission_id == "admission-1"

    with pytest.raises(ValidationError, match="workspace snapshot belongs"):
        bad_admission = _admission(workspace=_workspace("project-2"))
        _json_model(TaskAdmissionRefV2, bad_admission)
    with pytest.raises(ValidationError, match="registry digest"):
        _json_model(
            TaskAdmissionRefV2,
            {**admission, "registry_sha256": "f" * 64},
        )
    with pytest.raises(ValidationError, match="admission digest"):
        _json_model(
            TaskAdmissionRefV2,
            {**admission, "admission_sha256": "f" * 64},
        )


def test_successor_transition_binds_admission_attempt_and_adjacent_head() -> None:
    assert (
        _json_model(SuccessorTransitionRefV2, _transition()).expected_successor_generation
        == 8
    )

    wrong_attempt = _attempt(admission={**_admission(), "task_id": "task-2"})
    with pytest.raises(ValidationError, match="accepted attempt"):
        _json_model(
            SuccessorTransitionRefV2,
            _transition(attempt=wrong_attempt),
        )

    transition = _transition()
    transition["successor_project_head"]["generation"] = 9
    with pytest.raises(ValidationError, match="successor generation"):
        _json_model(SuccessorTransitionRefV2, transition)

    settings_transition = _transition()
    settings_transition.update(kind="settings", task_admission=None, accepted_attempt=None)
    assert _json_model(SuccessorTransitionRefV2, settings_transition).kind == "settings"
    settings_transition["task_admission"] = _admission()
    with pytest.raises(ValidationError, match="must not bind a task"):
        _json_model(SuccessorTransitionRefV2, settings_transition)


def test_canonical_contract_bytes_and_digest_are_deterministic_and_typed() -> None:
    left = _json_model(ProjectHeadRefV2, _head())
    right = _json_model(ProjectHeadRefV2, json.loads(json.dumps(_head())))
    encoded = canonical_contract_bytes(left)

    assert encoded.endswith(b"\n")
    assert encoded == canonical_contract_bytes(right)
    assert canonical_contract_sha256(left) == canonical_contract_sha256(right)
    assert len(canonical_contract_sha256(left)) == 64
    with pytest.raises(TypeError, match="ContractModel"):
        canonical_contract_bytes(left.model_dump(mode="json"))


def test_v2_contract_app_declares_authority_routes_and_is_contract_only() -> None:
    app = create_core_control_v2_contract_app()
    openapi = app.openapi()
    operations = {
        (method.upper(), path)
        for path, path_item in openapi["paths"].items()
        for method in path_item
        if method in {"get", "post", "patch", "delete", "put"}
    }
    assert operations == EXPECTED_OPERATIONS
    assert openapi["x-openevo-contract-only"] is True
    assert "/v1/status" not in openapi["paths"]
    assert openapi["paths"]["/version"]["get"]["x-openevo-discovery-only"] is True
    assert (
        openapi["paths"]["/version"]["get"]["x-openevo-mutation-compatible"]
        is False
    )

    client = TestClient(app)
    assert client.get("/v2/system/status").status_code == 501
    payload = client.get("/v2/projects/project-1/heads/active").json()
    assert payload == {
        "schema_version": "2",
        "code": "contract_only_not_implemented",
        "message": "This app defines the Core Control API v2 contract and has no provider.",
    }


def test_v2_openapi_snapshot_is_exactly_rebuildable() -> None:
    rebuilt = canonical_json_bytes(build_openapi_document())
    assert OPENAPI_SNAPSHOT_PATH.read_bytes() == rebuilt
    assert hashlib.sha256(rebuilt).hexdigest() == openapi_sha256()
    assert openapi_sha256() == (
        "f007726d8b092463a2515500e3cc0c496b52b45e9f24d1fc495b11df9a9a837b"
    )


def test_v2_event_schema_snapshot_is_exactly_rebuildable() -> None:
    rebuilt = canonical_json_bytes(build_events_schema_document())
    assert EVENTS_SCHEMA_SNAPSHOT_PATH.read_bytes() == rebuilt
    assert hashlib.sha256(rebuilt).hexdigest() == events_schema_sha256()
    assert events_schema_sha256() == (
        "464a52685dacaedc391fb17bb27516e64842e23d89d12d475679d7a41a0668df"
    )


def test_bounded_contract_json_rejects_oversize_and_recursive_input_before_validation() -> None:
    valid = json.dumps(_workspace(), separators=(",", ":")).encode()
    assert parse_contract_json_bytes(WorkspaceSnapshotRefV2, valid).entry_count == 7

    with pytest.raises(ValueError, match="byte limit"):
        parse_contract_json_bytes(
            WorkspaceSnapshotRefV2,
            b'{"padding":"' + (b"x" * (1024 * 1024)) + b'"}',
        )

    recursive: object = "leaf"
    for _ in range(18):
        recursive = {"nested": recursive}
    with pytest.raises(ValueError, match="depth limit"):
        parse_contract_json_bytes(
            WorkspaceSnapshotRefV2,
            json.dumps(recursive).encode(),
        )


def test_project_document_http_guard_rejects_bytes_and_depth_before_model_parsing() -> None:
    client = TestClient(create_core_control_v2_contract_app())
    headers = {"Idempotency-Key": "project-create-1"}
    oversized = b'{"padding":"' + (b"x" * (1024 * 1024)) + b'"}'
    response = client.post(
        "/v2/projects",
        headers={**headers, "Content-Type": "application/json"},
        content=oversized,
    )
    assert response.status_code == 413
    assert response.json()["code"] == "request_body_too_large"

    recursive: object = "leaf"
    for _ in range(26):
        recursive = {"nested": recursive}
    response = client.post("/v2/projects", headers=headers, json=recursive)
    assert response.status_code == 422
    assert response.json()["code"] == "request_json_too_deep"


def test_workspace_chunk_route_requires_exact_binary_integrity_metadata() -> None:
    client = TestClient(create_core_control_v2_contract_app())
    path = "/v2/projects/project-1/workspace-uploads/upload-1/chunks/0"
    assert client.put(path, content=b"x").status_code == 422
    headers = {
        "Content-Type": "application/octet-stream",
        "Idempotency-Key": "workspace-chunk-1",
        "If-Match": '"' + ("a" * 64) + '"',
        "X-OpenEvo-Chunk-SHA256": hashlib.sha256(b"x").hexdigest(),
        "X-OpenEvo-Chunk-Byte-Size": "1",
    }
    assert client.put(path, headers=headers, content=b"x").status_code == 501


def test_bounded_contract_json_rejects_unknown_fields_and_type_coercion() -> None:
    unknown = {**_workspace(), "metadata": {"host_path": "/private/tmp"}}
    with pytest.raises(ValidationError):
        parse_contract_json_bytes(
            WorkspaceSnapshotRefV2,
            json.dumps(unknown).encode(),
        )
    with pytest.raises(ValidationError):
        parse_contract_json_bytes(
            WorkspaceSnapshotRefV2,
            json.dumps({**_workspace(), "entry_count": "7"}).encode(),
        )


def test_v2_event_identity_is_closed_and_cannot_drift() -> None:
    admission = _admission()
    event = {
        "schema_version": "2",
        "event_id": "event-1",
        "sequence": 1,
        "occurred_at": "2026-07-23T00:00:02Z",
        "project_id": "project-1",
        "event_type": "task_admitted",
        "admission": admission,
    }
    frame = {
        "id": "event-1",
        "event": "task_admitted",
        "data": event,
        "retry": 1000,
    }
    assert _json_model(SseFrameV2, frame).data.event_id == "event-1"

    with pytest.raises(ValidationError, match="event project"):
        _json_model(
            TaskAdmittedEventV2,
            {**event, "project_id": "project-2"},
        )
    with pytest.raises(ValidationError, match="SSE frame ID"):
        _json_model(SseFrameV2, {**frame, "id": "event-2"})
    with pytest.raises(ValidationError):
        _json_model(SseFrameV2, {**frame, "id": "file://event"})


def test_v2_http_cursor_idempotency_and_etag_boundaries_fail_closed() -> None:
    client = TestClient(create_core_control_v2_contract_app())

    assert client.get("/v2/projects?after=").status_code == 422
    assert client.post("/v2/tasks", json={}).status_code == 422
    assert (
        client.post(
            "/v2/tasks/task-1/close",
            headers={
                "Idempotency-Key": "retry-1",
                "If-Match": "not-an-etag",
            },
            json={
                "schema_version": "2",
                "task_admission_id": "admission-1",
                "admission_sha256": "a" * 64,
            },
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/v2/tasks/task-1/close",
            headers={
                "Idempotency-Key": "x" * 257,
                "If-Match": f'"{"a" * 64}"',
            },
            json={
                "schema_version": "2",
                "task_admission_id": "admission-1",
                "admission_sha256": "a" * 64,
            },
        ).status_code
        == 422
    )


def _load_release_checker() -> Any:
    path = Path(__file__).resolve().parents[2] / "scripts/ci/check_openevo_release.py"
    spec = importlib.util.spec_from_file_location("check_openevo_release_v2_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _v019_release_contract() -> dict[str, Any]:
    return {
        "schema_version": "1",
        "v019": {
            "release_version": "0.1.9",
            "core_control_mutation_major": 2,
            "accepted_core_openapi_digests": [openapi_sha256()],
            "accepted_core_event_schema_digests": [events_schema_sha256()],
            "required_core_feature_flags": list(RELEASE_DAEMON_FEATURE_FLAGS_V2),
            "allow_legacy_route_fallback": False,
        },
    }


def _write_v019_launcher_fixture(root: Path) -> None:
    launcher = root / "src/openevo/backend/launcher.py"
    launcher.parent.mkdir(parents=True)
    launcher.write_text(
        "\n".join(
            [
                "CoreControlProviderV2",
                "ScienceAttemptExecutorV2",
                "ProductionScienceSuccessorPreparerV2",
                "create_core_control_v2_contract_app",
                "install_core_run_admission_endpoint",
            ]
        ),
        encoding="utf-8",
    )


def test_v019_release_manifest_requires_exact_core_v2_schema_digests(
    tmp_path: Path,
) -> None:
    checker = _load_release_checker()
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    manifest = desktop / "release-contract.json"
    manifest.write_text(json.dumps(_v019_release_contract()), encoding="utf-8")
    _write_v019_launcher_fixture(tmp_path)

    assert checker.validate_v019_contract_manifest(tmp_path, expected_version="0.1.9") == []

    payload = _v019_release_contract()
    payload["v019"]["accepted_core_openapi_digests"] = ["f" * 64]
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    assert "exact generated Core v2 OpenAPI digest" in " ".join(
        checker.validate_v019_contract_manifest(tmp_path, expected_version="0.1.9")
    )


def test_v019_release_manifest_forbids_v1_mutation_authority(tmp_path: Path) -> None:
    checker = _load_release_checker()
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    manifest = desktop / "release-contract.json"
    payload = _v019_release_contract()
    payload["v019"]["core_control_mutation_major"] = 1
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    errors = checker.validate_v019_contract_manifest(tmp_path, expected_version="0.1.9")
    assert any("must require Core Control API v2 for mutation" in error for error in errors)

    # The guard is dormant for the retained 0.1.8 release identity until Task 25.
    assert checker.validate_v019_contract_manifest(tmp_path, expected_version="0.1.8") == []


def _feature_set_sha256(features: list[str]) -> str:
    return hashlib.sha256(
        json.dumps(features, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()


def _core_v2_discovery() -> dict[str, Any]:
    features = [
        "atomic_successor_v2",
        "system_status_v2",
        "task_admission_v2",
    ]
    return {
        "schema_version": "2",
        "api_name": "openevo-core-control-api",
        "preferred_major": 2,
        "supported_majors": [1, 2],
        "mutation_major": 2,
        "contracts": [
            {
                "schema_version": "2",
                "api_major": 1,
                "openapi_sha256": "1" * 64,
                "event_schema_sha256": "2" * 64,
                "access": "read_only_migration",
                "mutation_compatible": False,
            },
            {
                "schema_version": "2",
                "api_major": 2,
                "openapi_sha256": openapi_sha256(),
                "event_schema_sha256": events_schema_sha256(),
                "access": "mutation",
                "mutation_compatible": True,
            },
        ],
        "release_version": "0.1.9",
        "build_id": "3" * 64,
        "source_commit": "abcdef0",
        "build_channel": "release",
        "provider_kind": "openevo_daemon",
        "feature_flags": features,
        "feature_set_sha256": _feature_set_sha256(features),
        "registry_sha256": "4" * 64,
        "runtime_contract_sha256": "5" * 64,
        "mutation_compatible": True,
    }


def test_core_v2_discovery_binds_release_build_schema_features_and_registry() -> None:
    discovery = _json_model(VersionResponseV2, _core_v2_discovery())
    assert discovery.mutation_major == 2
    assert discovery.contracts[0].mutation_compatible is False
    assert discovery.contracts[1].openapi_sha256 == openapi_sha256()
    assert discovery.registry_sha256 == "4" * 64

    payload = _core_v2_discovery()
    payload["feature_set_sha256"] = "f" * 64
    with pytest.raises(ValidationError, match="feature-set digest"):
        _json_model(VersionResponseV2, payload)

    payload = _core_v2_discovery()
    payload["contracts"][1]["mutation_compatible"] = False
    with pytest.raises(ValidationError, match="access and mutation"):
        _json_model(VersionResponseV2, payload)


def test_core_v1_discovery_cannot_satisfy_v2_mutation_negotiation() -> None:
    legacy = {
        "schema_version": "1",
        "api_name": "openevo-core-control-api",
        "preferred_major": 1,
        "supported_majors": [1],
        "openapi_sha256": "a" * 64,
        "release_version": "0.1.8",
    }
    with pytest.raises(ValidationError):
        _json_model(VersionResponseV2, legacy)


def test_core_contract_offer_never_marks_v1_as_mutation_compatible() -> None:
    with pytest.raises(ValidationError, match="v1 is read-only"):
        _json_model(
            ContractOfferV2,
            {
                "schema_version": "2",
                "api_major": 1,
                "openapi_sha256": "a" * 64,
                "event_schema_sha256": "b" * 64,
                "access": "mutation",
                "mutation_compatible": True,
            },
        )


def test_core_v1_provider_can_be_mounted_read_only_without_schema_drift() -> None:
    from openevo.backend.contracts.v1.app import create_core_control_contract_app

    class Provider:
        def authenticate(self, _authorization_values: tuple[bytes, ...]) -> bool:
            return True

        async def invoke_async(
            self, operation_id: str, _arguments: dict[str, object]
        ) -> object:
            if operation_id == "getCoreStatusV1":
                return {
                    "schema_version": "1",
                    "status": "ready",
                    "release_version": "0.1.8",
                    "source_commit": "abcdef0",
                    "build_channel": "release",
                    "provider_kind": "openevo_core",
                    "registry_digest": "a" * 64,
                    "checked_at": "2026-07-23T00:00:00Z",
                }
            raise AssertionError("a retired v1 mutation reached the provider")

    app = create_core_control_contract_app(Provider(), mutation_enabled=False)
    client = TestClient(app)
    response = client.post(
        "/v1/runs",
        headers={"Authorization": "Bearer token", "Idempotency-Key": "request-1"},
        json={},
    )
    assert response.status_code == 426
    assert response.json()["code"] == "v1_mutation_retired"
