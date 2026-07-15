from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from fastapi.responses import Response, StreamingResponse
from fastapi.testclient import TestClient
import pytest

from desktop.sidecar.contracts.v1 import models as local_v1
from desktop.sidecar.core_bridge_v1 import DesktopCoreBridgeErrorV1, DesktopCoreBridgeV1
from desktop.sidecar.core_client_v1 import CoreClientErrorV1, CoreMutationOutcomeUnknownV1
from desktop.sidecar.event_broker_v1 import (
    DesktopEventBrokerV1,
    DesktopEventCursorExpiredError,
)
from desktop.sidecar.provider_store import DesktopProviderStore, ETagConflictError
from desktop.sidecar.release_app import create_release_desktop_local_api_app
from desktop.sidecar.release_provider import (
    DesktopReleaseProvider,
    ProviderCapabilityUnavailableError,
)
from desktop.sidecar.release_capabilities import RELEASE_EXECUTION_MODE_CAPABILITIES_V1
from desktop.sidecar.release_runtime import CoreRuntimeSessionBinding
from desktop.sidecar.workspace_imports import WorkspaceImportStore
from openevo.backend.contracts.v1 import models as core_v1
from openevo.backend.contracts.v1.models import (
    ErrorCategory,
    ErrorSeverity,
    RepairAction,
)
from openevo.evolution.framework.profiles import execution_profile_for_release_mode


NOW = "2026-07-14T12:00:00Z"
ETAG_A = '"' + "a" * 64 + '"'


class _Lifecycle:
    def close(self) -> None:
        return None


def _provider(
    tmp_path: Path,
    bridge: Mock | None,
    *,
    event_broker: DesktopEventBrokerV1 | None = None,
) -> tuple[DesktopReleaseProvider, DesktopProviderStore, local_v1.ProjectV1]:
    state_root = tmp_path / "state"
    store = DesktopProviderStore(state_root)
    profile = store.create_profile(
        local_v1.RemoteProfileCreateV1(
            name="Research server",
            host="compute.example.org",
            user="researcher",
        ),
        idempotency_key="profile-create-routing-0001",
    )
    project = store.create_project(
        local_v1.ProjectCreateV1(
            name="Protein design",
            profile_id=profile.profile_id,
            task=local_v1.ProjectTaskV1(
                title="Design",
                objective="Improve held-out stability.",
            ),
            source=local_v1.ProjectSourceV1(
                kind="scratch",
                display_name="New workspace",
            ),
            execution=local_v1.ExecutionSettingsV1(
                mode="codex_subscription_transcript",
                codex_model="gpt-5.5",
            ),
            evolution=local_v1.EvolutionConfigV1(targets={}),
        ),
        idempotency_key="project-create-routing-0001",
    )
    provider = DesktopReleaseProvider(
        store,
        WorkspaceImportStore(state_root / "workspace-imports", reconcile_on_open=False),
        build_version="0.1.0",
        source_commit="1234567",
        build_channel="test",
        instance_id="1" * 32,
        readiness_key=b"r" * 32,
        execution_mode_capabilities=RELEASE_EXECUTION_MODE_CAPABILITIES_V1,
        remote_lifecycle=_Lifecycle(),  # type: ignore[arg-type]
        core_bridge=bridge,  # type: ignore[arg-type]
        event_broker=event_broker,
        clock=lambda: datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc),
    )
    if bridge is not None:
        provider._active_project_for_runtime = (  # type: ignore[method-assign]
            lambda: CoreRuntimeSessionBinding(project=project, generation=1)
        )
    return provider, store, project


def _page_arguments() -> dict[str, object]:
    return {
        "limit": 17,
        "after": "cursor-1",
        "sort": "created_at",
        "direction": "desc",
    }


def _bind_app_project(app: object) -> local_v1.ProjectV1:
    provider = app.state.desktop_release_provider  # type: ignore[attr-defined]
    store = provider._store
    profile = store.create_profile(
        local_v1.RemoteProfileCreateV1(
            name="App research server",
            host="compute.example.org",
            user="researcher",
        ),
        idempotency_key="app-profile-create-routing-0001",
    )
    project = store.create_project(
        local_v1.ProjectCreateV1(
            name="App protein design",
            profile_id=profile.profile_id,
            task=local_v1.ProjectTaskV1(title="Design", objective="Improve stability."),
            source=local_v1.ProjectSourceV1(kind="scratch", display_name="New workspace"),
            execution=local_v1.ExecutionSettingsV1(
                mode="codex_subscription_transcript",
                codex_model="gpt-5.5",
            ),
            evolution=local_v1.EvolutionConfigV1(targets={}),
        ),
        idempotency_key="app-project-create-routing-0001",
    )
    provider._active_project_for_runtime = lambda: CoreRuntimeSessionBinding(  # type: ignore[method-assign]
        project=project,
        generation=1,
    )
    return project


def test_release_provider_forwards_core_owned_read_routes(tmp_path: Path) -> None:
    bridge = Mock(spec=DesktopCoreBridgeV1)
    provider, _, project = _provider(tmp_path, bridge)
    sentinel = object()
    cases = (
        ("listRuns", "list_runs", _page_arguments()),
        ("getRun", "get_run", {"run_id": "run-1"}),
        ("listRunTimeline", "run_timeline", {"run_id": "run-1", **_page_arguments()}),
        ("listRunLogs", "run_logs", {"run_id": "run-1", **_page_arguments()}),
        ("getRunContext", "run_context", {"run_id": "run-1"}),
        ("listRunArtifacts", "run_artifacts", {"run_id": "run-1", **_page_arguments()}),
        ("getArtifact", "get_artifact", {"artifact_id": "artifact-1"}),
        ("getArtifactContent", "artifact_content", {"artifact_id": "artifact-1"}),
        ("getArtifactDiff", "artifact_diff", {"artifact_id": "artifact-1"}),
        (
            "listServices",
            "list_services",
            {
                "limit": 17,
                "after": "cursor-1",
                "sort": "display_name",
                "direction": "asc",
            },
        ),
        ("listServiceLogs", "service_logs", {"service_id": "service-1", **_page_arguments()}),
        ("getCoreOperation", "get_operation", {"operation_id": "core-operation-1"}),
        ("getCoreLogsByRef", "logs_by_ref", {"logs_ref": "logs-1", **_page_arguments()}),
    )
    try:
        for operation_id, method_name, arguments in cases:
            method = getattr(bridge, method_name)
            method.reset_mock()
            method.return_value = sentinel
            assert provider.invoke(operation_id, arguments) is sentinel

        bridge.list_runs.assert_called_once_with(project, **_page_arguments())
        bridge.get_run.assert_called_once_with(project, "run-1")
        bridge.run_timeline.assert_called_once_with(project, "run-1", **_page_arguments())
        bridge.run_logs.assert_called_once_with(project, "run-1", **_page_arguments())
        bridge.run_context.assert_called_once_with(project, "run-1")
        bridge.run_artifacts.assert_called_once_with(project, "run-1", **_page_arguments())
        bridge.get_artifact.assert_called_once_with(project, "artifact-1")
        bridge.artifact_content.assert_called_once_with(project, "artifact-1")
        bridge.artifact_diff.assert_called_once_with(project, "artifact-1")
        bridge.list_services.assert_called_once_with(
            project,
            limit=17,
            after="cursor-1",
            sort="kind",
            direction="asc",
        )
        bridge.service_logs.assert_called_once_with(project, "service-1", **_page_arguments())
        bridge.get_operation.assert_called_once_with(project, "core-operation-1")
        bridge.logs_by_ref.assert_called_once_with(project, "logs-1", **_page_arguments())
    finally:
        provider.close()
    bridge.close.assert_called_once_with()


def test_release_provider_forwards_core_owned_mutations(tmp_path: Path) -> None:
    bridge = Mock(spec=DesktopCoreBridgeV1)
    provider, _, project = _provider(tmp_path, bridge)
    result = object()
    bridge.create_run.return_value = result
    bridge.cancel_run.return_value = result
    bridge.retry_run.return_value = result
    bridge.cache_cleanup.return_value = result
    retry_request = local_v1.RunRetryV1(terminal_attempt_id="attempt-terminal-1")
    cache_request = core_v1.CacheCleanupRequestV1(
        scopes=[core_v1.CacheScope.BUILD_ARTIFACTS],
        older_than_days=30,
    )
    try:
        assert (
            provider.invoke(
                "createRun",
                {
                    "request": local_v1.RunCreateV1(project_id=project.project_id),
                    "idempotency_key": "run-create-routing-0001",
                    "if_match": project.etag,
                },
            )
            is result
        )
        assert (
            provider.invoke(
                "cancelRun",
                {
                    "run_id": "run-1",
                    "idempotency_key": "run-cancel-routing-0001",
                    "if_match": ETAG_A,
                },
            )
            is result
        )
        assert (
            provider.invoke(
                "retryRun",
                {
                    "run_id": "run-1",
                    "request": retry_request,
                    "idempotency_key": "run-retry-routing-0001",
                    "if_match": ETAG_A,
                },
            )
            is result
        )
        assert (
            provider.invoke(
                "cleanupCaches",
                {
                    "request": cache_request,
                    "idempotency_key": "cache-cleanup-routing-0001",
                },
            )
            is result
        )

        deleted_run = provider.invoke("deleteRun", {"run_id": "run-1", "if_match": ETAG_A})
        assert isinstance(deleted_run, Response) and deleted_run.status_code == 204

        bridge.create_run.assert_called_once_with(
            project, idempotency_key="run-create-routing-0001"
        )
        bridge.cancel_run.assert_called_once_with(
            project,
            "run-1",
            if_match=ETAG_A,
            idempotency_key="run-cancel-routing-0001",
        )
        bridge.retry_run.assert_called_once_with(
            project,
            "run-1",
            retry_request,
            if_match=ETAG_A,
            idempotency_key="run-retry-routing-0001",
        )
        bridge.cache_cleanup.assert_called_once_with(
            project,
            cache_request,
            idempotency_key="cache-cleanup-routing-0001",
        )
        bridge.delete_run.assert_called_once_with(project, "run-1", if_match=ETAG_A)
    finally:
        provider.close()


def test_release_provider_allows_supported_subscription_retry(tmp_path: Path) -> None:
    bridge = Mock(spec=DesktopCoreBridgeV1)
    provider, _, project = _provider(tmp_path, bridge)
    result = object()
    bridge.retry_run.return_value = result
    retry_request = local_v1.RunRetryV1(terminal_attempt_id="attempt-terminal-1")
    try:
        assert project.execution.mode == "codex_subscription_transcript"
        assert (
            provider.invoke(
                "retryRun",
                {
                    "run_id": "run-supported-1",
                    "request": retry_request,
                    "idempotency_key": "run-retry-supported-routing-0001",
                    "if_match": ETAG_A,
                },
            )
            is result
        )
        bridge.retry_run.assert_called_once()
        assert bridge.retry_run.call_args.args[0] == project
    finally:
        provider.close()


@pytest.mark.parametrize(
    "operation_id",
    ("restartService", "createDiagnostic", "getDiagnostic", "deleteDiagnostic"),
)
def test_release_provider_does_not_install_unavailable_core_handlers(
    tmp_path: Path,
    operation_id: str,
) -> None:
    bridge = Mock(spec=DesktopCoreBridgeV1)
    provider, _, _ = _provider(tmp_path, bridge)
    try:
        with pytest.raises(ProviderCapabilityUnavailableError):
            provider.invoke(operation_id, {})
        bridge.restart_service.assert_not_called()
        bridge.create_diagnostic.assert_not_called()
        bridge.get_diagnostic.assert_not_called()
        bridge.delete_diagnostic.assert_not_called()
    finally:
        provider.close()


def test_run_creation_requires_current_local_project_etag(tmp_path: Path) -> None:
    bridge = Mock(spec=DesktopCoreBridgeV1)
    provider, _, project = _provider(tmp_path, bridge)
    try:
        with pytest.raises(ETagConflictError):
            provider.invoke(
                "createRun",
                {
                    "request": local_v1.RunCreateV1(project_id=project.project_id),
                    "idempotency_key": "run-create-routing-0002",
                    "if_match": ETAG_A,
                },
            )
        bridge.create_run.assert_not_called()
    finally:
        provider.close()


def test_capability_and_validation_envelopes_bind_local_project(tmp_path: Path) -> None:
    bridge = Mock(spec=DesktopCoreBridgeV1)
    provider, _, project = _provider(tmp_path, bridge)
    capabilities = core_v1.CapabilitiesResponseV1(
        core_version="0.1.0",
        registry_digest="4" * 64,
        evaluated_profile=execution_profile_for_release_mode(
            core_v1.ExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT
        ),
        targets=(),
    )
    validation = core_v1.ProjectValidationResponseV1(
        valid=True,
        registry_digest=capabilities.registry_digest,
        checks=[],
        validated_at=NOW,
    )
    bridge.capabilities.return_value = capabilities
    bridge.validate_project.return_value = validation
    try:
        envelope = provider.invoke("getProjectCapabilities", {"project_id": project.project_id})
        assert isinstance(envelope, local_v1.CapabilitiesEnvelopeV1)
        assert envelope.project_id == project.project_id
        assert envelope.project_etag == project.etag
        assert envelope.capabilities == capabilities

        validated = provider.invoke(
            "validateProject",
            {
                "project_id": project.project_id,
                "idempotency_key": "project-validate-routing-0001",
                "if_match": project.etag,
            },
        )
        assert isinstance(validated, local_v1.ProjectValidationV1)
        assert validated.project_id == project.project_id
        assert validated.project_etag == project.etag
        assert validated.registry_digest == capabilities.registry_digest
        assert validated.valid is True
        bridge.capabilities.assert_called_once_with(project)
        bridge.validate_project.assert_called_once_with(
            project,
            idempotency_key="project-validate-routing-0001",
        )
    finally:
        provider.close()


def test_existing_local_operation_read_is_available_without_core_bridge(tmp_path: Path) -> None:
    provider, store, project = _provider(tmp_path, None)
    result = store.execute_idempotent_action(
        route=f"/desktop/v1/projects/{project.project_id}/activate",
        resource_scope=project.project_id,
        key="project-activate-routing-0001",
        body={},
        if_match=project.etag,
        semantic_headers={},
        response_model=local_v1.LocalOperationV1,
        mutation=lambda transaction: (
            202,
            transaction.create_local_operation(
                operation_kind="project_activate",
                resource=local_v1.ResourceRefV1(
                    resource_type="project",
                    resource_id=project.project_id,
                ),
                state="queued",
            ),
        ),
    )
    operation = local_v1.LocalOperationV1.model_validate_json(
        result.response_bytes,
        strict=True,
    )
    try:
        fetched = provider.invoke("getLocalOperation", {"operation_id": operation.operation_id})
        assert fetched.operation_id == operation.operation_id
        assert fetched.resource == operation.resource
    finally:
        provider.close()


def test_release_provider_streams_brokered_state_events(tmp_path: Path) -> None:
    broker = DesktopEventBrokerV1(
        heartbeat_interval=60,
        poll_interval=0.01,
        clock=lambda: datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc),
        event_id_factory=lambda: "release-event-routing-0001",
    )
    provider, _, _ = _provider(tmp_path, None, event_broker=broker)
    response = provider.invoke(
        "subscribeDesktopEvents",
        {"last_event_id": None},
    )
    assert isinstance(response, StreamingResponse)

    provider.publish_state_changed()

    frame = asyncio.run(response.body_iterator.__anext__())
    assert isinstance(frame, bytes)
    assert b"event: desktop.v1.state.changed\n" in frame
    assert b'"kind":"state_changed"' in frame
    provider.close()
    assert broker.subscriber_count == 0


def test_release_provider_rejects_an_expired_event_cursor(tmp_path: Path) -> None:
    broker = DesktopEventBrokerV1()
    provider, _, _ = _provider(tmp_path, None, event_broker=broker)
    try:
        with pytest.raises(DesktopEventCursorExpiredError):
            provider.invoke(
                "subscribeDesktopEvents",
                {"last_event_id": "expired-release-event"},
            )
    finally:
        provider.close()


def test_release_app_maps_expired_event_cursor_to_reset_response(tmp_path: Path) -> None:
    token = "desktop-session-token-0000000000000009"
    app = create_release_desktop_local_api_app(
        state_root=tmp_path / "event-cursor",
        session_token=token,
        instance_id="9" * 32,
        readiness_key=b"u" * 32,
        source_commit="1234567",
        build_channel="test",
        remote_lifecycle=_Lifecycle(),  # type: ignore[arg-type]
        event_broker=DesktopEventBrokerV1(),
    )
    with TestClient(app) as client:
        response = client.get(
            "/desktop/v1/events",
            headers={
                "X-OpenEvo-Desktop-Session": token,
                "Last-Event-ID": "expired-release-event",
            },
        )
    assert response.status_code == 410
    assert response.json()["code"] == "event_cursor_expired"


def test_release_app_serves_bridge_results_and_preserves_typed_errors(tmp_path: Path) -> None:
    bridge = Mock(spec=DesktopCoreBridgeV1)
    bridge.list_runs.return_value = core_v1.RunPageV1(items=[], has_more=False)
    app = create_release_desktop_local_api_app(
        state_root=tmp_path / "success",
        session_token="desktop-session-token-0000000000000001",
        instance_id="1" * 32,
        readiness_key=b"r" * 32,
        source_commit="1234567",
        build_channel="test",
        remote_lifecycle=_Lifecycle(),  # type: ignore[arg-type]
        core_bridge=bridge,
    )
    project = _bind_app_project(app)
    headers = {"X-OpenEvo-Desktop-Session": "desktop-session-token-0000000000000001"}
    with TestClient(app) as client:
        response = client.get("/desktop/v1/runs", headers=headers)
        assert response.status_code == 200
        assert response.json()["items"] == []
    bridge.close.assert_called_once_with()
    bridge.list_runs.assert_called_once_with(
        project, limit=50, after=None, sort="created_at", direction="desc"
    )

    error = core_v1.ApiErrorV1(
        request_id="core-request-1",
        code="core_temporarily_unavailable",
        http_status=503,
        message="OpenEvo Core is temporarily unavailable.",
        severity=ErrorSeverity.BLOCKING,
        category=ErrorCategory.RUN,
        retryable=True,
        repair_action=RepairAction.OPENEVO_CAN_RETRY,
        next_action="Retry this operation from OpenEvo Desktop.",
    )
    failing_bridge = Mock(spec=DesktopCoreBridgeV1)
    failing_bridge.list_runs.side_effect = DesktopCoreBridgeErrorV1(error)
    failing_app = create_release_desktop_local_api_app(
        state_root=tmp_path / "failure",
        session_token="desktop-session-token-0000000000000002",
        instance_id="2" * 32,
        readiness_key=b"s" * 32,
        source_commit="1234567",
        build_channel="test",
        remote_lifecycle=_Lifecycle(),  # type: ignore[arg-type]
        core_bridge=failing_bridge,
    )
    _bind_app_project(failing_app)
    with TestClient(failing_app) as client:
        response = client.get(
            "/desktop/v1/runs",
            headers={"X-OpenEvo-Desktop-Session": "desktop-session-token-0000000000000002"},
        )
        assert response.status_code == 503
        assert response.json() == error.model_dump(mode="json")

    client_failing_bridge = Mock(spec=DesktopCoreBridgeV1)
    client_failing_bridge.list_runs.side_effect = CoreClientErrorV1(503, error)
    client_failing_app = create_release_desktop_local_api_app(
        state_root=tmp_path / "client-failure",
        session_token="desktop-session-token-0000000000000003",
        instance_id="3" * 32,
        readiness_key=b"t" * 32,
        source_commit="1234567",
        build_channel="test",
        remote_lifecycle=_Lifecycle(),  # type: ignore[arg-type]
        core_bridge=client_failing_bridge,
    )
    _bind_app_project(client_failing_app)
    with TestClient(client_failing_app) as client:
        response = client.get(
            "/desktop/v1/runs",
            headers={"X-OpenEvo-Desktop-Session": "desktop-session-token-0000000000000003"},
        )
        assert response.status_code == 503
        assert response.json() == error.model_dump(mode="json")


def test_release_app_retry_body_is_closed_and_forwarded(tmp_path: Path) -> None:
    error = core_v1.ApiErrorV1(
        request_id="core-retry-request-1",
        code="run_retry_conflict",
        http_status=409,
        message="The retry authority conflicts with Core state.",
        severity=ErrorSeverity.BLOCKING,
        category=ErrorCategory.RUN,
        retryable=False,
        repair_action=RepairAction.UNSUPPORTED,
        next_action="Refresh the run before creating another retry intent.",
    )
    bridge = Mock(spec=DesktopCoreBridgeV1)
    bridge.retry_run.side_effect = DesktopCoreBridgeErrorV1(error)
    token = "desktop-session-token-retry-body-00000001"
    app = create_release_desktop_local_api_app(
        state_root=tmp_path / "retry-body",
        session_token=token,
        instance_id="4" * 32,
        readiness_key=b"v" * 32,
        source_commit="1234567",
        build_channel="test",
        remote_lifecycle=_Lifecycle(),  # type: ignore[arg-type]
        core_bridge=bridge,
    )
    project = _bind_app_project(app)
    headers = {
        "X-OpenEvo-Desktop-Session": token,
        "Idempotency-Key": "retry-body-forwarding-0001",
        "If-Match": ETAG_A,
    }
    body = {"terminal_attempt_id": "attempt-terminal-1"}

    with TestClient(app) as client:
        response = client.post("/desktop/v1/runs/run-1/retry", headers=headers, json=body)
        invalid = client.post(
            "/desktop/v1/runs/run-1/retry",
            headers=headers,
            json={**body, "current_attempt_id": "attempt-new"},
        )

    assert response.status_code == 409
    assert response.json() == error.model_dump(mode="json")
    assert invalid.status_code == 422
    bridge.retry_run.assert_called_once_with(
        project,
        "run-1",
        local_v1.RunRetryV1(terminal_attempt_id="attempt-terminal-1"),
        if_match=ETAG_A,
        idempotency_key="retry-body-forwarding-0001",
    )


def test_release_app_retry_unknown_outcome_uses_the_stable_ambiguous_code(
    tmp_path: Path,
) -> None:
    bridge = Mock(spec=DesktopCoreBridgeV1)
    bridge.retry_run.side_effect = CoreMutationOutcomeUnknownV1()
    token = "desktop-session-token-retry-unknown-00001"
    app = create_release_desktop_local_api_app(
        state_root=tmp_path / "retry-unknown",
        session_token=token,
        instance_id="5" * 32,
        readiness_key=b"w" * 32,
        source_commit="1234567",
        build_channel="test",
        remote_lifecycle=_Lifecycle(),  # type: ignore[arg-type]
        core_bridge=bridge,
    )
    _bind_app_project(app)

    with TestClient(app) as client:
        response = client.post(
            "/desktop/v1/runs/run-1/retry",
            headers={
                "X-OpenEvo-Desktop-Session": token,
                "Idempotency-Key": "retry-unknown-outcome-0001",
                "If-Match": ETAG_A,
            },
            json={"terminal_attempt_id": "attempt-terminal-1"},
        )

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["code"] == "core_mutation_outcome_unknown"
    assert response.json()["retryable"] is True
