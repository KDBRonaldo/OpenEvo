from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import threading

import pytest
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from openevo.backend.contracts.v2.app import (
    _iter_api_routes,
    create_core_control_v2_contract_app,
)
from openevo.backend.contracts.v2.provider import CoreControlProviderV2
import openevo.backend.contracts.v2.provider as provider_module
from openevo.backend.contracts.v2.snapshots import (
    events_schema_sha256,
    openapi_sha256,
)
from openevo.backend.contracts.v2.store import (
    CoreControlStoreV2,
    CoreControlStoreV2Error,
)
from openevo.backend.contracts.v2.models import (
    EffectiveExecutionSnapshotRefV2,
    EvolutionRevisionRefV2,
    ProjectCreateV2,
    ProjectHeadRefV2,
    RuntimeContextSnapshotRefV2,
    ScienceProjectConfigV2,
    TaskSubmitRequestV2,
    WorkspaceSnapshotRefV2,
    project_config_sha256_for,
)
from openevo.backend.science_run_owner import CoreScienceTaskOwnerV2
from openevo.backend.science_run_store import ScienceProjectAdmissionAuthorityV2
from tests.framework_testkit import verified_builtin_registry
from tests.backend.test_science_successor_v2 import (
    _Preparer as _SuccessorPreparer,
    _authority as _successor_authority,
    _head as _successor_head,
    _plan as _successor_plan,
    _project_config as _successor_project_config,
    _request as _successor_request,
)


_TOKEN = "core-v2-test-bearer"
_RUNTIME_CONTRACT_SHA256 = "d" * 64


def _project_config() -> ScienceProjectConfigV2:
    return ScienceProjectConfigV2.model_validate(
        {
            "task": {
                "title": "Provider task",
                "objective": "Exercise the v2 provider authority.",
            },
            "workspace": {
                "kind": "scratch",
                "display_name": "Provider workspace",
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
    )


class _Clock:
    def __init__(self) -> None:
        self._next = datetime(2026, 7, 23, 2, tzinfo=timezone.utc)
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            value = self._next
            self._next += timedelta(microseconds=1)
            return value


def _workspace(project_id: str, seed: str) -> WorkspaceSnapshotRefV2:
    return WorkspaceSnapshotRefV2(
        workspace_snapshot_id=f"workspace-{seed}",
        project_id=project_id,
        manifest_sha256=seed * 64,
        entry_count=4,
        byte_size=2048,
    )


def _head(
    project_id: str = "project-1",
    *,
    registry_sha256: str = "a" * 64,
    runtime_contract_sha256: str = _RUNTIME_CONTRACT_SHA256,
) -> ProjectHeadRefV2:
    evolution = EvolutionRevisionRefV2(
        evolution_revision_id="evolution-0",
        project_id=project_id,
        manifest_sha256="2" * 64,
        artifact_count=0,
    )
    context = RuntimeContextSnapshotRefV2(
        runtime_context_snapshot_id="runtime-context-0",
        project_id=project_id,
        evolution_revision_id=evolution.evolution_revision_id,
        evolution_revision_manifest_sha256=evolution.manifest_sha256,
        registry_sha256=registry_sha256,
        runtime_contract_sha256=runtime_contract_sha256,
        manifest_sha256="3" * 64,
    )
    execution = EffectiveExecutionSnapshotRefV2(
        effective_execution_snapshot_id="execution-0",
        project_id=project_id,
        execution_mode="codex_subscription_transcript",
        capture_mode="transcript",
        token_level_metrics_available=False,
        producer_id="subscription-snapshot-issuer-v1",
        snapshot_sha256="4" * 64,
    )
    return ProjectHeadRefV2(
        project_head_id="project-head-0",
        project_id=project_id,
        generation=0,
        predecessor_project_head_id=None,
        workspace_snapshot=_workspace(project_id, "1"),
        evolution_revision=evolution,
        runtime_context_snapshot=context,
        effective_execution_snapshot=execution,
        registry_sha256=context.registry_sha256,
        manifest_sha256="5" * 64,
    )


def _authority(*, registry_sha256: str = "a" * 64) -> ScienceProjectAdmissionAuthorityV2:
    head = _head(registry_sha256=registry_sha256)
    return ScienceProjectAdmissionAuthorityV2(
        project_id=head.project_id,
        active_project_head=head,
        project_config_sha256=project_config_sha256_for(_project_config()),
        workspace_snapshot=_workspace(head.project_id, "7"),
        normalized_evolution_intent_sha256="8" * 64,
    )


def _authority_with_head(head: ProjectHeadRefV2) -> ScienceProjectAdmissionAuthorityV2:
    return ScienceProjectAdmissionAuthorityV2(
        project_id=head.project_id,
        active_project_head=head,
        project_config_sha256=project_config_sha256_for(_project_config()),
        workspace_snapshot=_workspace(head.project_id, "7"),
        normalized_evolution_intent_sha256="8" * 64,
    )


def _request(authority: ScienceProjectAdmissionAuthorityV2) -> TaskSubmitRequestV2:
    return TaskSubmitRequestV2(
        project_id=authority.project_id,
        expected_project_admission_etag=authority.project_etag,
        expected_project_head_id=authority.active_project_head.project_head_id,
        expected_project_head_manifest_sha256=(
            authority.active_project_head.manifest_sha256
        ),
        expected_project_config_sha256=authority.project_config_sha256,
    )


class _Runtime:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.clock = _Clock()
        self.registry = verified_builtin_registry(tmp_path / "registry")
        self.owner = CoreScienceTaskOwnerV2(
            state_root=tmp_path,
            clock=self.clock,
        )
        self.store = CoreControlStoreV2(tmp_path / "core-control-v2")
        self.provider = CoreControlProviderV2(
            self.store,
            task_owner=self.owner,
            executable_registry=self.registry,
            bearer_token=_TOKEN,
            release_version="0.1.9",
            source_commit="1" * 40,
            build_channel="test",
            runtime_contract_sha256=_RUNTIME_CONTRACT_SHA256,
            clock=self.clock,
        )
        self.authority = _authority(
            registry_sha256=self.registry.snapshot.registry_digest
        )
        self.provider.publish_project_admission_authority(
            display_name="Provider project",
            config=_project_config(),
            authority=self.authority,
        )
        self.app = create_core_control_v2_contract_app(self.provider)
        self.client = TestClient(self.app)

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {_TOKEN}"}

    def close(self) -> None:
        self.client.close()
        self.provider.close()


@pytest.fixture
def runtime(tmp_path: Path):
    value = _Runtime(tmp_path)
    try:
        yield value
    finally:
        value.close()


def test_provider_negotiates_exact_v2_authority_and_authenticates(
    runtime: _Runtime,
) -> None:
    version = runtime.client.get("/version")
    assert version.status_code == 200
    payload = version.json()
    assert payload["preferred_major"] == 2
    assert payload["supported_majors"] == [2]
    assert payload["mutation_major"] == 2
    assert payload["mutation_compatible"] is True
    assert payload["registry_sha256"] == runtime.registry.snapshot.registry_digest
    assert payload["feature_flags"] == [
        "event_replay_v2",
        "project_heads_v2",
        "task_admission_v2",
        "verified_capabilities",
        "verified_registry",
    ]
    assert "system_openssh_remote_workspace" not in payload["feature_flags"]
    offers = {item["api_major"]: item for item in payload["contracts"]}
    assert offers[2]["openapi_sha256"] == openapi_sha256()
    assert offers[2]["event_schema_sha256"] == events_schema_sha256()
    assert offers[2]["access"] == "mutation"

    assert runtime.client.get("/health").status_code == 200
    unauthorized = runtime.client.get("/v2/system/status")
    assert unauthorized.status_code == 401
    assert unauthorized.json()["code"] == "core_bearer_invalid"
    assert runtime.client.get(
        "/v2/system/status", headers=runtime.headers
    ).status_code == 200
    assert runtime.provider.operation_ids == frozenset(
        route.operation_id
        for route in _iter_api_routes(runtime.app.routes)
        if route.operation_id is not None
    )


def test_provider_build_identity_binds_the_negotiated_feature_set(
    tmp_path: Path,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")

    def provider_at(path: str, *, successor: bool) -> CoreControlProviderV2:
        return CoreControlProviderV2(
            CoreControlStoreV2(tmp_path / path / "catalog"),
            task_owner=CoreScienceTaskOwnerV2(
                state_root=tmp_path / path,
                clock=_Clock(),
                successor_preparer=_SuccessorPreparer() if successor else None,
            ),
            executable_registry=registry,
            bearer_token=_TOKEN,
            release_version="0.1.9",
            source_commit="1" * 40,
            build_channel="test",
            runtime_contract_sha256=_RUNTIME_CONTRACT_SHA256,
            clock=_Clock(),
        )

    basic = provider_at("basic", successor=False)
    successor = provider_at("successor", successor=True)
    try:
        basic_version = basic.invoke("discoverCoreContractVersionV2", {})
        successor_version = successor.invoke("discoverCoreContractVersionV2", {})
        assert basic_version.feature_set_sha256 != successor_version.feature_set_sha256
        assert basic_version.build_id != successor_version.build_id
    finally:
        basic.close()
        successor.close()


def test_project_task_attempt_context_and_events_are_authoritative(
    runtime: _Runtime,
) -> None:
    projects = runtime.client.get("/v2/projects", headers=runtime.headers)
    assert projects.status_code == 200
    assert projects.json()["items"][0]["active_project_head"]["project_head_id"] == (
        "project-head-0"
    )

    submit = runtime.client.post(
        "/v2/tasks",
        headers={**runtime.headers, "Idempotency-Key": "submit-1"},
        json=_request(runtime.authority).model_dump(mode="json"),
    )
    assert submit.status_code == 202
    task = submit.json()
    assert task["admission"]["predecessor_project_head"]["project_head_id"] == (
        "project-head-0"
    )
    replay = runtime.client.post(
        "/v2/tasks",
        headers={**runtime.headers, "Idempotency-Key": "submit-1"},
        json=_request(runtime.authority).model_dump(mode="json"),
    )
    assert replay.status_code == 202
    assert replay.json() == task

    task_id = task["task_id"]
    fetched = runtime.client.get(f"/v2/tasks/{task_id}", headers=runtime.headers)
    assert fetched.status_code == 200
    assert fetched.headers["etag"] == task["etag"]
    admission = runtime.client.get(
        f"/v2/tasks/{task_id}/admission", headers=runtime.headers
    )
    assert admission.json() == task["admission"]
    context = runtime.client.get(
        f"/v2/tasks/{task_id}/context", headers=runtime.headers
    )
    assert context.status_code == 200
    assert context.json()["project_head"] == task["admission"][
        "predecessor_project_head"
    ]

    first = task["attempts"][0]
    append = runtime.client.post(
        f"/v2/tasks/{task_id}/attempts",
        headers={**runtime.headers, "Idempotency-Key": "attempt-2"},
        json={
            "schema_version": "2",
            "task_admission_id": task["admission"]["task_admission_id"],
            "admission_sha256": task["admission"]["admission_sha256"],
            "expected_previous_attempt_id": first["attempt_id"],
            "expected_next_ordinal": 2,
        },
    )
    assert append.status_code == 202
    assert append.json()["ordinal"] == 2
    attempts = runtime.client.get(
        f"/v2/tasks/{task_id}/attempts", headers=runtime.headers
    )
    assert [item["ordinal"] for item in attempts.json()["items"]] == [1, 2]

    timeline = runtime.client.get(
        f"/v2/tasks/{task_id}/timeline", headers=runtime.headers
    )
    assert [item["event_type"] for item in timeline.json()["items"]] == [
        "task_admitted",
        "attempt_appended",
        "attempt_appended",
    ]
    events = runtime.provider.invoke(
        "streamCoreEventsV2", {"last_event_id": None}
    )
    assert isinstance(events, StreamingResponse)

    async def first_two_frames() -> tuple[bytes, bytes]:
        first = await anext(events.body_iterator)
        second = await anext(events.body_iterator)
        await events.body_iterator.aclose()
        return first, second

    first_frame, second_frame = asyncio.run(first_two_frames())
    assert b"event: task_admitted\n" in first_frame
    assert b"event: attempt_appended\n" in second_frame


def test_authority_drift_and_unfinished_features_fail_closed(
    runtime: _Runtime,
) -> None:
    stale = _request(runtime.authority).model_copy(
        update={"expected_project_head_manifest_sha256": "f" * 64}
    )
    response = runtime.client.post(
        "/v2/tasks",
        headers={**runtime.headers, "Idempotency-Key": "stale"},
        json=stale.model_dump(mode="json"),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "task_submission_stale"
    assert runtime.owner.ownership_counts() == (0, 0, 0)

    for path in (
        "/v2/tasks/missing/logs",
        "/v2/tasks/missing/artifacts",
        "/v2/services/daemon/logs",
    ):
        unavailable = runtime.client.get(path, headers=runtime.headers)
        assert unavailable.status_code == 503
        assert unavailable.json()["code"] == "feature_not_ready"
        assert "missing" not in unavailable.json()["message"]


def test_event_reconnect_rejects_unknown_cursor_without_fallback(
    runtime: _Runtime,
) -> None:
    runtime.client.post(
        "/v2/tasks",
        headers={**runtime.headers, "Idempotency-Key": "submit-events"},
        json=_request(runtime.authority).model_dump(mode="json"),
    )
    retained = runtime.owner.list_events()
    event_id = retained[-1].event_id
    resumed = runtime.provider.invoke(
        "streamCoreEventsV2", {"last_event_id": event_id}
    )
    assert isinstance(resumed, StreamingResponse)

    expired = runtime.client.get(
        "/v2/events",
        headers={**runtime.headers, "Last-Event-ID": "event-unknown"},
    )
    assert expired.status_code == 410
    assert expired.json()["code"] == "event_cursor_expired"


def test_task_close_uses_etag_and_durable_idempotent_operation(
    runtime: _Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admitted = runtime.client.post(
        "/v2/tasks",
        headers={**runtime.headers, "Idempotency-Key": "close-task-submit"},
        json=_request(runtime.authority).model_dump(mode="json"),
    )
    task = admitted.json()
    task_id = task["task_id"]
    action = {
        "schema_version": "2",
        "task_admission_id": task["admission"]["task_admission_id"],
        "admission_sha256": task["admission"]["admission_sha256"],
    }

    stale = runtime.client.post(
        f"/v2/tasks/{task_id}/close",
        headers={
            **runtime.headers,
            "Idempotency-Key": "close-stale",
            "If-Match": f'"{"0" * 64}"',
        },
        json=action,
    )
    assert stale.status_code == 412
    assert stale.json()["code"] == "task_etag_changed"

    closed = runtime.client.post(
        f"/v2/tasks/{task_id}/close",
        headers={
            **runtime.headers,
            "Idempotency-Key": "close-task",
            "If-Match": task["etag"],
        },
        json=action,
    )
    assert closed.status_code == 202
    assert closed.json()["kind"] == "task_close"
    assert closed.json()["status"] == "succeeded"
    assert closed.headers["etag"] == closed.json()["etag"]

    replay = runtime.client.post(
        f"/v2/tasks/{task_id}/close",
        headers={
            **runtime.headers,
            "Idempotency-Key": "close-task",
            "If-Match": task["etag"],
        },
        json=action,
    )
    assert replay.status_code == 202
    assert replay.json() == closed.json()

    operation = runtime.client.get(
        f"/v2/operations/{closed.json()['operation_id']}",
        headers=runtime.headers,
    )
    assert operation.status_code == 200
    assert operation.json() == closed.json()
    assert runtime.client.get(
        f"/v2/tasks/{task_id}", headers=runtime.headers
    ).json()["state"] == "closed"

    different_action = runtime.client.post(
        f"/v2/tasks/{task_id}/close",
        headers={
            **runtime.headers,
            "Idempotency-Key": "close-task-different-action",
            "If-Match": task["etag"],
        },
        json=action,
    )
    assert different_action.status_code == 412
    assert different_action.json()["code"] == "task_etag_changed"

    reused = runtime.client.post(
        f"/v2/tasks/{task_id}/close",
        headers={
            **runtime.headers,
            "Idempotency-Key": "close-task",
            "If-Match": f'"{"f" * 64}"',
        },
        json=action,
    )
    assert reused.status_code == 409
    assert reused.json()["code"] == "task_idempotency_key_reused"

    crash_task = runtime.client.post(
        "/v2/tasks",
        headers={**runtime.headers, "Idempotency-Key": "close-crash-submit"},
        json=_request(runtime.authority).model_dump(mode="json"),
    ).json()
    crash_action = {
        "schema_version": "2",
        "task_admission_id": crash_task["admission"]["task_admission_id"],
        "admission_sha256": crash_task["admission"]["admission_sha256"],
    }
    original_commit = runtime.store.commit_action

    def fail_commit_action(**_arguments: object):
        raise RuntimeError("injected post-close provider interruption")

    monkeypatch.setattr(runtime.store, "commit_action", fail_commit_action)
    with pytest.raises(RuntimeError, match="post-close provider interruption"):
        runtime.client.post(
            f"/v2/tasks/{crash_task['task_id']}/close",
            headers={
                **runtime.headers,
                "Idempotency-Key": "close-crash",
                "If-Match": crash_task["etag"],
            },
            json=crash_action,
        )
    monkeypatch.setattr(runtime.store, "commit_action", original_commit)
    recovered_close = runtime.client.post(
        f"/v2/tasks/{crash_task['task_id']}/close",
        headers={
            **runtime.headers,
            "Idempotency-Key": "close-crash",
            "If-Match": crash_task["etag"],
        },
        json=crash_action,
    )
    assert recovered_close.status_code == 202
    assert recovered_close.json()["status"] == "succeeded"

    operation_id = closed.json()["operation_id"]
    runtime.client.close()
    runtime.provider.close()
    restarted_owner = CoreScienceTaskOwnerV2(
        state_root=runtime.root,
        clock=_Clock(),
    )
    restarted_store = CoreControlStoreV2(runtime.root / "core-control-v2")
    restarted_provider = CoreControlProviderV2(
        restarted_store,
        task_owner=restarted_owner,
        executable_registry=runtime.registry,
        bearer_token=_TOKEN,
        release_version="0.1.9",
        source_commit="1" * 40,
        build_channel="test",
        runtime_contract_sha256=_RUNTIME_CONTRACT_SHA256,
        clock=_Clock(),
    )
    restarted_client = TestClient(
        create_core_control_v2_contract_app(restarted_provider)
    )
    try:
        recovered = restarted_client.get(
            f"/v2/operations/{operation_id}", headers=runtime.headers
        )
        assert recovered.status_code == 200
        assert recovered.json() == closed.json()
    finally:
        restarted_client.close()
        restarted_provider.close()


def test_project_create_idempotency_etag_and_cursor_are_closed(
    runtime: _Runtime,
) -> None:
    request = {
        "schema_version": "2",
        "display_name": "Draft project",
        "config": _project_config().model_dump(mode="json"),
    }
    created = runtime.client.post(
        "/v2/projects",
        headers={**runtime.headers, "Idempotency-Key": "create-project"},
        json=request,
    )
    assert created.status_code == 201
    assert created.json()["state"] == "not_ready"
    assert created.json()["active_project_head"] is None
    assert created.json()["admission_etag"] is None
    assert created.json()["config"] == request["config"]
    assert created.json()["project_config_sha256"] == project_config_sha256_for(
        _project_config()
    )
    assert created.headers["etag"] == created.json()["etag"]

    replay = runtime.client.post(
        "/v2/projects",
        headers={**runtime.headers, "Idempotency-Key": "create-project"},
        json=request,
    )
    assert replay.status_code == 201
    assert replay.json() == created.json()
    conflict = runtime.client.post(
        "/v2/projects",
        headers={**runtime.headers, "Idempotency-Key": "create-project"},
        json={**request, "display_name": "Different draft"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "project_idempotency_key_reused"

    first_page = runtime.client.get(
        "/v2/projects?limit=1&direction=asc", headers=runtime.headers
    )
    assert first_page.status_code == 200
    assert first_page.json()["has_more"] is True
    cursor = first_page.json()["next_cursor"]
    second_page = runtime.client.get(
        f"/v2/projects?limit=1&direction=asc&after={cursor}",
        headers=runtime.headers,
    )
    assert second_page.status_code == 200
    assert second_page.json()["items"] != first_page.json()["items"]
    wrong_query = runtime.client.get(
        f"/v2/projects?limit=1&direction=desc&after={cursor}",
        headers=runtime.headers,
    )
    assert wrong_query.status_code == 400
    assert wrong_query.json()["code"] == "cursor_invalid"


def test_project_resource_etag_binds_the_complete_admission_authority(
    runtime: _Runtime,
) -> None:
    before = runtime.client.get("/v2/projects/project-1", headers=runtime.headers)
    assert before.status_code == 200

    updated_authority = ScienceProjectAdmissionAuthorityV2(
        project_id=runtime.authority.project_id,
        active_project_head=runtime.authority.active_project_head,
        project_config_sha256=runtime.authority.project_config_sha256,
        workspace_snapshot=_workspace(runtime.authority.project_id, "9"),
        normalized_evolution_intent_sha256=(
            runtime.authority.normalized_evolution_intent_sha256
        ),
    )
    runtime.provider.publish_project_admission_authority(
        display_name="Provider project",
        config=_project_config(),
        authority=updated_authority,
        expected_project_head_id=(
            runtime.authority.active_project_head.project_head_id
        ),
    )

    after = runtime.client.get("/v2/projects/project-1", headers=runtime.headers)
    assert after.status_code == 200
    assert after.json()["active_project_head"] == before.json()["active_project_head"]
    assert after.json()["project_config_sha256"] == before.json()[
        "project_config_sha256"
    ]
    assert after.json()["admission_etag"] != before.json()["admission_etag"]
    assert after.headers["etag"] != before.headers["etag"]


def test_provider_exposes_only_authoritative_services_and_fails_closed_elsewhere(
    runtime: _Runtime,
) -> None:
    services = runtime.client.get("/v2/services", headers=runtime.headers)
    assert services.status_code == 200
    assert [(item["service_id"], item["status"]) for item in services.json()["items"]] == [
        ("daemon", "ready")
    ]
    daemon = runtime.client.get("/v2/services/daemon", headers=runtime.headers)
    assert daemon.status_code == 200
    assert daemon.headers["etag"] == daemon.json()["etag"]

    unfinished = (
        ("GET", "/v2/tasks/unavailable/logs", None),
        ("GET", "/v2/tasks/unavailable/artifacts", None),
        ("GET", "/v2/projects/project-1/artifacts/artifact-1", None),
        (
            "POST",
            "/v2/diagnostics",
            {"schema_version": "2", "scope": "system", "resource_id": None},
        ),
        (
            "POST",
            "/v2/transitions/transition-1/retry",
            {"schema_version": "2", "expected_project_head_id": "project-head-0"},
        ),
    )
    for index, (method, path, payload) in enumerate(unfinished):
        response = runtime.client.request(
            method,
            path,
            headers={**runtime.headers, "Idempotency-Key": f"unfinished-{index}"},
            json=payload,
        )
        assert response.status_code == 503
        assert response.json()["code"] == "feature_not_ready"
        assert "unavailable" not in response.json()["message"]

    capabilities = runtime.client.get(
        "/v2/capabilities",
        headers=runtime.headers,
        params={"execution_mode": "codex_subscription_transcript"},
    )
    assert capabilities.status_code == 200
    assert capabilities.json()["registry_digest"] == (
        runtime.registry.snapshot.registry_digest
    )
    assert capabilities.json()["evaluated_profile"]["execution_mode"] == "subscription"

    validation = runtime.client.post(
        "/v2/projects/project-1/validate",
        headers={**runtime.headers, "Idempotency-Key": "validate-unfinished"},
        json={
            "schema_version": "2",
            "expected_project_head_id": "project-head-0",
            "expected_project_head_manifest_sha256": "5" * 64,
            "expected_project_config_sha256": runtime.authority.project_config_sha256,
            "expected_registry_sha256": runtime.registry.snapshot.registry_digest,
        },
    )
    assert validation.status_code == 503
    assert validation.json()["code"] == "feature_not_ready"


def test_project_validation_body_is_bounded_before_json_parsing(
    runtime: _Runtime,
) -> None:
    unauthenticated = runtime.client.post(
        "/v2/projects/project-1/validate",
        headers={
            "Idempotency-Key": "unauthenticated-validation",
            "Content-Type": "application/json",
        },
        content=b'{"padding":"' + (b"x" * (1024 * 1024)) + b'"}',
    )
    assert unauthenticated.status_code == 401

    headers = {
        **runtime.headers,
        "Idempotency-Key": "oversized-validation",
        "Content-Type": "application/json",
    }
    oversized = runtime.client.post(
        "/v2/projects/project-1/validate",
        headers=headers,
        content=b'{"padding":"' + (b"x" * (1024 * 1024)) + b'"}',
    )
    assert oversized.status_code == 413
    assert oversized.json()["code"] == "request_body_too_large"

    nested: object = "leaf"
    for _ in range(26):
        nested = {"nested": nested}
    too_deep = runtime.client.post(
        "/v2/projects/project-1/validate",
        headers={**headers, "Idempotency-Key": "deep-validation"},
        json=nested,
    )
    assert too_deep.status_code == 422
    assert too_deep.json()["code"] == "request_json_too_deep"


def test_successor_events_are_recoverable_and_activate_the_next_admission(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    registry = verified_builtin_registry(tmp_path / "registry")
    authority = _successor_authority(
        _successor_head(registry_sha256=registry.snapshot.registry_digest)
    )
    owner = CoreScienceTaskOwnerV2(
        state_root=tmp_path,
        clock=clock,
        successor_preparer=_SuccessorPreparer(),
    )
    store = CoreControlStoreV2(tmp_path / "core-control-v2")
    provider = CoreControlProviderV2(
        store,
        task_owner=owner,
        executable_registry=registry,
        bearer_token=_TOKEN,
        release_version="0.1.9",
        source_commit="1" * 40,
        build_channel="test",
        runtime_contract_sha256="b" * 64,
        clock=clock,
    )
    provider.publish_project_admission_authority(
        display_name="Successor project",
        config=_successor_project_config(),
        authority=authority,
    )
    client = TestClient(create_core_control_v2_contract_app(provider))
    headers = {"Authorization": f"Bearer {_TOKEN}"}
    try:
        assert "atomic_successor_v2" in client.get("/version").json()[
            "feature_flags"
        ]
        admitted = client.post(
            "/v2/tasks",
            headers={**headers, "Idempotency-Key": "successor-task"},
            json=_successor_request(authority).model_dump(mode="json"),
        )
        assert admitted.status_code == 202
        task_id = admitted.json()["task_id"]
        task = owner.invoke("getCoreTaskV2", {"task_id": task_id})
        transition = owner.run_successor_transition(
            task_id,
            accepted_attempt_id=task.attempts[0].attempt_id,
            plan=_successor_plan(task),
        )
        assert transition.state == "committed"

        fetched = client.get(
            f"/v2/transitions/{transition.transition.successor_transition_id}",
            headers=headers,
        )
        assert fetched.status_code == 200
        assert fetched.json()["state"] == "committed"
        active = client.get(
            f"/v2/projects/{authority.project_id}/heads/active", headers=headers
        )
        assert active.json()["generation"] == 1
        assert active.json()["project_head_id"] == (
            transition.transition.successor_project_head.project_head_id
        )

        timeline = client.get(f"/v2/tasks/{task_id}/timeline", headers=headers)
        event_types = [item["event_type"] for item in timeline.json()["items"]]
        assert event_types == [
            "task_admitted",
            "attempt_appended",
            "transition_changed",
            "transition_changed",
            "dataset_sealed",
            "transition_changed",
            "transition_changed",
            "transition_changed",
            "transition_changed",
            "evolution_revision_committed",
            "runtime_context_committed",
            "project_head_activated",
            "transition_changed",
        ]
        transition_states = [
            item["state"]
            for item in timeline.json()["items"]
            if item["event_type"] == "transition_changed"
        ]
        assert transition_states == [
            "pending",
            "sealing_dataset",
            "running_methods",
            "validating",
            "materializing",
            "committing",
            "committed",
        ]

        next_authority = owner.project_admission_authority(authority.project_id)
        second = client.post(
            "/v2/tasks",
            headers={**headers, "Idempotency-Key": "next-task"},
            json=_successor_request(next_authority).model_dump(mode="json"),
        )
        assert second.status_code == 202
        assert second.json()["admission"]["predecessor_project_head"]["generation"] == 1

        last_event_id = owner.list_events()[-1].event_id
    finally:
        client.close()
        provider.close()

    restarted_owner = CoreScienceTaskOwnerV2(state_root=tmp_path, clock=_Clock())
    restarted_store = CoreControlStoreV2(tmp_path / "core-control-v2")
    restarted_provider = CoreControlProviderV2(
        restarted_store,
        task_owner=restarted_owner,
        executable_registry=registry,
        bearer_token=_TOKEN,
        release_version="0.1.9",
        source_commit="1" * 40,
        build_channel="test",
        runtime_contract_sha256="b" * 64,
        clock=_Clock(),
    )
    restarted_client = TestClient(
        create_core_control_v2_contract_app(restarted_provider)
    )
    try:
        assert restarted_client.get(
            f"/v2/projects/{authority.project_id}/heads/active", headers=headers
        ).json()["generation"] == 1
        replay = restarted_provider.invoke(
            "streamCoreEventsV2",
            {"last_event_id": last_event_id},
        )
        assert isinstance(replay, StreamingResponse)
    finally:
        restarted_client.close()
        restarted_provider.close()


def test_provider_rejects_registry_drift_and_schema_snapshot_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = CoreScienceTaskOwnerV2(state_root=tmp_path, clock=_Clock())
    store = CoreControlStoreV2(tmp_path / "core-control-v2")
    registry = verified_builtin_registry(tmp_path / "registry")
    provider = CoreControlProviderV2(
        store,
        task_owner=owner,
        executable_registry=registry,
        bearer_token=_TOKEN,
        release_version="0.1.9",
        source_commit="1" * 40,
        build_channel="test",
        runtime_contract_sha256=_RUNTIME_CONTRACT_SHA256,
        clock=_Clock(),
    )
    try:
        with pytest.raises(ValueError, match="negotiated Core digests"):
            provider.publish_project_admission_authority(
                display_name="Drifted project",
                config=_project_config(),
                authority=_authority(registry_sha256="f" * 64),
            )
        assert owner.ownership_counts() == (0, 0, 0)
        assert store.list_projects() == []
    finally:
        provider.close()

    corrupt_snapshot = tmp_path / "openapi-corrupt.json"
    corrupt_snapshot.write_bytes(b"{}\n")
    monkeypatch.setattr(provider_module, "OPENAPI_SNAPSHOT_PATH", corrupt_snapshot)
    second_owner = CoreScienceTaskOwnerV2(
        state_root=tmp_path / "second",
        clock=_Clock(),
    )
    second_store = CoreControlStoreV2(tmp_path / "second-core-control")
    with pytest.raises(RuntimeError, match="snapshot does not match"):
        CoreControlProviderV2(
            second_store,
            task_owner=second_owner,
            executable_registry=registry,
            bearer_token=_TOKEN,
            release_version="0.1.9",
            source_commit="1" * 40,
            build_channel="test",
            runtime_contract_sha256=_RUNTIME_CONTRACT_SHA256,
            clock=_Clock(),
        )
    second_owner.close()
    second_store.close()


@pytest.mark.parametrize("drift", ["registry", "runtime_contract"])
def test_persisted_project_digest_drift_blocks_new_task_admission(
    tmp_path: Path,
    drift: str,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    registry_sha256 = registry.snapshot.registry_digest
    head = _head(
        registry_sha256=("f" * 64 if drift == "registry" else registry_sha256),
        runtime_contract_sha256=(
            "f" * 64 if drift == "runtime_contract" else _RUNTIME_CONTRACT_SHA256
        ),
    )
    authority = _authority_with_head(head)
    clock = _Clock()
    owner = CoreScienceTaskOwnerV2(state_root=tmp_path, clock=clock)
    owner.publish_project_admission_authority(authority)
    store = CoreControlStoreV2(tmp_path / "core-control-v2")
    store.upsert_authoritative_project(
        project_id=authority.project_id,
        display_name="Drifted project",
        config=_project_config(),
        now=clock(),
    )
    provider = CoreControlProviderV2(
        store,
        task_owner=owner,
        executable_registry=registry,
        bearer_token=_TOKEN,
        release_version="0.1.9",
        source_commit="1" * 40,
        build_channel="test",
        runtime_contract_sha256=_RUNTIME_CONTRACT_SHA256,
        clock=clock,
    )
    client = TestClient(create_core_control_v2_contract_app(provider))
    headers = {"Authorization": f"Bearer {_TOKEN}"}
    try:
        project = client.get("/v2/projects/project-1", headers=headers)
        assert project.status_code == 200
        assert project.json()["state"] == "not_ready"
        blocked = client.post(
            "/v2/tasks",
            headers={**headers, "Idempotency-Key": f"drift-{drift}"},
            json=_request(authority).model_dump(mode="json"),
        )
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "project_not_ready"
        assert owner.ownership_counts() == (0, 0, 0)
    finally:
        client.close()
        provider.close()


def test_project_create_request_remains_valid_after_catalog_display_update(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    store = CoreControlStoreV2(root)
    request = ProjectCreateV2(
        display_name="Original name",
        config=_project_config(),
    )
    record, replayed = store.create_project(
        request,
        idempotency_key="create-project",
        now=datetime(2026, 7, 23, tzinfo=timezone.utc),
    )
    assert replayed is False
    store.upsert_authoritative_project(
        project_id=record.project_id,
        display_name="Updated name",
        config=record.config,
        now=datetime(2026, 7, 23, 0, 0, 1, tzinfo=timezone.utc),
    )
    store.close()

    restarted = CoreControlStoreV2(root)
    try:
        recovered, replayed = restarted.create_project(
            request,
            idempotency_key="create-project",
            now=datetime(2026, 7, 23, 0, 0, 2, tzinfo=timezone.utc),
        )
        assert replayed is True
        assert recovered.display_name == "Updated name"
    finally:
        restarted.close()


def test_v2_catalog_recovers_a_project_config_at_the_declared_depth_limit(
    tmp_path: Path,
) -> None:
    nested: object = "leaf"
    for _ in range(14):
        nested = {"nested": nested}
    payload = _project_config().model_dump(mode="json")
    payload["evolution"]["targets"] = {
        "text_memory": {
            "enabled": False,
            "method": None,
            "config": {"deep": nested},
        }
    }
    config = ScienceProjectConfigV2.model_validate(payload)
    root = tmp_path / "catalog"
    store = CoreControlStoreV2(root)
    record, replayed = store.create_project(
        ProjectCreateV2(display_name="Deep config", config=config),
        idempotency_key="deep-project-config",
        now=datetime(2026, 7, 23, tzinfo=timezone.utc),
    )
    assert replayed is False
    assert record.config == config
    store.close()

    restarted = CoreControlStoreV2(root)
    try:
        assert restarted.get_project(record.project_id).config == config
    finally:
        restarted.close()


@pytest.mark.parametrize("corruption", ["bytes", "digest"])
def test_v2_catalog_startup_rejects_corrupt_project_config(
    tmp_path: Path,
    corruption: str,
) -> None:
    root = tmp_path / "catalog"
    store = CoreControlStoreV2(root)
    record, _ = store.create_project(
        ProjectCreateV2(display_name="Corruption target", config=_project_config()),
        idempotency_key="corruption-target",
        now=datetime(2026, 7, 23, tzinfo=timezone.utc),
    )
    database = store.database
    store.close()
    with sqlite3.connect(database) as connection:
        if corruption == "bytes":
            connection.execute(
                "UPDATE projects SET project_config_json = ? WHERE project_id = ?",
                (b"{}\n", record.project_id),
            )
        else:
            connection.execute(
                "UPDATE projects SET project_config_sha256 = ? WHERE project_id = ?",
                ("f" * 64, record.project_id),
            )
        connection.commit()

    with pytest.raises(CoreControlStoreV2Error, match="persisted v2 project"):
        CoreControlStoreV2(root)


def test_v2_catalog_startup_rejects_near_match_schema(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    store = CoreControlStoreV2(root)
    database = store.database
    store.close()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE unexpected_state(value TEXT) STRICT")
        connection.commit()

    with pytest.raises(CoreControlStoreV2Error, match="schema is not exact"):
        CoreControlStoreV2(root)
