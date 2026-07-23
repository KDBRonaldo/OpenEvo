from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from openevo.backend.contracts.v2.app import create_core_control_v2_contract_app
from openevo.backend.contracts.v2.models import (
    AttemptRefV2,
    EffectiveExecutionSnapshotRefV2,
    EvolutionRevisionRefV2,
    ProjectHeadRefV2,
    RuntimeContextSnapshotRefV2,
    SuccessorTransitionRefV2,
    TaskAdmissionRefV2,
    WorkspaceSnapshotRefV2,
)
from openevo.backend.contracts.v2.snapshots import (
    canonical_contract_bytes,
    canonical_contract_sha256,
)


EXPECTED_OPERATIONS = {
    ("GET", "/version"),
    ("GET", "/health"),
    ("GET", "/v2/system/status"),
    ("GET", "/v2/projects"),
    ("POST", "/v2/projects"),
    ("GET", "/v2/projects/{project_id}"),
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
    seed: str = "6",
) -> dict[str, Any]:
    head = head or _head(project_id)
    workspace = workspace or _workspace(project_id, "7")
    return {
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
        "admission_sha256": seed * 64,
        "admitted_at": "2026-07-23T00:00:00Z",
    }


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

    client = TestClient(app)
    assert client.get("/v2/system/status").status_code == 501
    payload = client.get("/v2/projects/project-1/heads/active").json()
    assert payload == {
        "schema_version": "2",
        "code": "contract_only_not_implemented",
        "message": "This app defines the Core Control API v2 contract and has no provider.",
    }
