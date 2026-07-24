from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import time
from unittest.mock import Mock, call

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
from desktop.sidecar.provider_store import (
    DesktopProviderStore,
    ETagConflictError,
    ProviderStoreError,
)
from desktop.sidecar.release_app import create_release_desktop_local_api_app
from desktop.sidecar.release_provider import (
    DesktopReleaseProvider,
    LocalOperationCancellationUnavailableError,
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


def _activate_routing_project(
    store: DesktopProviderStore,
    project: local_v1.ProjectV1,
) -> local_v1.ProjectV1:
    action = {
        "route": f"/desktop/v1/projects/{project.project_id}/activate",
        "operation_kind": "project_activate",
        "project_id": project.project_id,
        "key": "project-activate-degraded-routing-0001",
        "body": {},
        "if_match": project.etag,
    }
    reservation = store.begin_project_runtime_action(**action)
    store.start_project_runtime_action(reservation=reservation, **action)
    core_project_id = f"core-{project.project_id}"
    store.complete_project_runtime_action(
        reservation=reservation,
        remote_state=local_v1.RemoteProjectStateV1(
            core_project_id=core_project_id,
            status="ready",
            active_revision=core_v1.RevisionRefV1(
                id="core-revision-degraded-routing-0001",
                project_id=core_project_id,
                generation=1,
                manifest_sha256="a" * 64,
            ),
            registry_digest="b" * 64,
            model_preparation=core_v1.ModelPreparationV1(
                model_ref="gpt-5.5",
                status=core_v1.ModelPreparationStatus.READY,
                updated_at=NOW,
            ),
            observed_at=NOW,
            etag=ETAG_A,
        ),
        **action,
    )
    return store.get_project(project.project_id)


def _set_degraded_runtime(
    provider: DesktopReleaseProvider,
    project: local_v1.ProjectV1,
    *,
    binding: CoreRuntimeSessionBinding | None = None,
    active_tunnel: bool = True,
    generation: int = 1,
) -> None:
    provider._session_generation = generation
    provider._core_session_binding = binding or CoreRuntimeSessionBinding(
        project=project,
        generation=generation,
    )
    provider._core_state = local_v1.CoreConnectionStateV1(
        state="degraded",
        profile_id=project.profile_id,
        active_tunnel=active_tunnel,
        core=local_v1.CoreCompatibilityV1(
            contract_digest="d" * 64,
            core_version="0.1.0",
        ),
        failure=local_v1.ConnectionFailureV1(
            code="service_degraded",
            message="One remote service needs attention.",
            retryable=True,
            next_action="Open System to inspect and repair the service.",
        ),
    )


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


def test_degraded_exact_runtime_binding_stays_ready_for_system_routes(
    tmp_path: Path,
) -> None:
    bridge = Mock(spec=DesktopCoreBridgeV1)
    provider, store, draft = _provider(tmp_path, bridge)
    project = _activate_routing_project(store, draft)
    del provider._active_project_for_runtime
    _set_degraded_runtime(provider, project)
    services = object()
    restarted = object()
    diagnostic = object()
    bridge.list_services.return_value = services
    bridge.restart_service.return_value = restarted
    bridge.create_diagnostic.return_value = diagnostic
    request = core_v1.DiagnosticsRequestV1(
        scopes=[core_v1.DiagnosticScope.PROJECT],
        target=core_v1.ProjectDiagnosticTargetV1(
            kind=core_v1.DiagnosticTargetKind.PROJECT,
            project_id=project.remote.core_project_id,
        ),
    )
    try:
        state = provider.invoke("getDesktopState", {})
        assert isinstance(state, local_v1.DesktopStateV1)
        assert state.core.state == "degraded"
        assert state.active_project is not None
        assert state.active_project.connection_state == "ready"

        assert provider.invoke("listServices", _page_arguments()) is services
        assert (
            provider.invoke(
                "restartService",
                {
                    "service_id": "service-1",
                    "if_match": ETAG_A,
                    "idempotency_key": "restart-degraded-routing-0001",
                },
            )
            is restarted
        )
        assert (
            provider.invoke(
                "createDiagnostic",
                {
                    "request": request,
                    "idempotency_key": "diagnostic-degraded-routing-0001",
                },
            )
            is diagnostic
        )
        bridge.list_services.assert_called_once_with(project, **_page_arguments())
        bridge.restart_service.assert_called_once_with(
            project,
            "service-1",
            if_match=ETAG_A,
            idempotency_key="restart-degraded-routing-0001",
        )
        bridge.create_diagnostic.assert_called_once_with(
            project,
            request,
            idempotency_key="diagnostic-degraded-routing-0001",
        )
    finally:
        provider.close()


@pytest.mark.parametrize(
    "mismatch",
    ("binding", "project", "profile", "etag", "generation", "tunnel"),
)
def test_degraded_runtime_binding_mismatch_is_offline_and_fails_closed(
    tmp_path: Path,
    mismatch: str,
) -> None:
    bridge = Mock(spec=DesktopCoreBridgeV1)
    provider, store, draft = _provider(tmp_path, bridge)
    project = _activate_routing_project(store, draft)
    del provider._active_project_for_runtime
    binding_project = project
    binding_generation = 1
    binding: CoreRuntimeSessionBinding | None = None
    if mismatch == "project":
        binding_project = project.model_copy(update={"project_id": "project-other"})
    elif mismatch == "profile":
        binding_project = project.model_copy(update={"profile_id": "profile-other"})
    elif mismatch == "etag":
        binding_project = project.model_copy(update={"etag": '"' + "e" * 64 + '"'})
    elif mismatch == "generation":
        binding_generation = 2
    if mismatch != "binding":
        binding = CoreRuntimeSessionBinding(
            project=binding_project,
            generation=binding_generation,
        )
    _set_degraded_runtime(
        provider,
        project,
        binding=binding,
        active_tunnel=mismatch != "tunnel",
    )
    if mismatch == "binding":
        provider._core_session_binding = None
    try:
        state = provider.invoke("getDesktopState", {})
        assert isinstance(state, local_v1.DesktopStateV1)
        assert state.active_project is not None
        assert state.active_project.connection_state == "offline"
        with pytest.raises(ProviderStoreError):
            provider.invoke("listServices", _page_arguments())
        bridge.list_services.assert_not_called()
    finally:
        provider.close()


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
    bridge.restart_service.return_value = result
    bridge.create_diagnostic.return_value = result
    bridge.get_diagnostic.return_value = result
    bridge.cache_cleanup.return_value = result
    retry_request = local_v1.RunRetryV1(terminal_attempt_id="attempt-terminal-1")
    diagnostic_request = core_v1.DiagnosticsRequestV1(
        scopes=[core_v1.DiagnosticScope.PROJECT],
        target=core_v1.ProjectDiagnosticTargetV1(
            kind=core_v1.DiagnosticTargetKind.PROJECT,
            project_id="core-project-1",
        ),
    )
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
                "restartService",
                {
                    "service_id": "service-1",
                    "idempotency_key": "service-restart-routing-0001",
                    "if_match": ETAG_A,
                },
            )
            is result
        )
        assert (
            provider.invoke(
                "createDiagnostic",
                {
                    "request": diagnostic_request,
                    "idempotency_key": "diagnostic-create-routing-0001",
                },
            )
            is result
        )
        assert provider.invoke("getDiagnostic", {"diagnostic_id": "diagnostic-1"}) is result
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
        deleted_diagnostic = provider.invoke(
            "deleteDiagnostic",
            {
                "diagnostic_id": "diagnostic-1",
                "idempotency_key": "diagnostic-delete-routing-0001",
                "if_match": ETAG_A,
            },
        )
        assert isinstance(deleted_diagnostic, Response) and deleted_diagnostic.status_code == 204

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
        bridge.restart_service.assert_called_once_with(
            project,
            "service-1",
            if_match=ETAG_A,
            idempotency_key="service-restart-routing-0001",
        )
        bridge.create_diagnostic.assert_called_once_with(
            project,
            diagnostic_request,
            idempotency_key="diagnostic-create-routing-0001",
        )
        bridge.get_diagnostic.assert_called_once_with(project, "diagnostic-1")
        bridge.delete_diagnostic.assert_called_once_with(
            project,
            "diagnostic-1",
            if_match=ETAG_A,
            idempotency_key="diagnostic-delete-routing-0001",
        )
        bridge.cache_cleanup.assert_called_once_with(
            project,
            cache_request,
            idempotency_key="cache-cleanup-routing-0001",
        )
        bridge.delete_run.assert_called_once_with(project, "run-1", if_match=ETAG_A)
    finally:
        provider.close()


def test_release_provider_runs_project_doctor_and_repair_through_active_core(
    tmp_path: Path,
) -> None:
    bridge = Mock(spec=DesktopCoreBridgeV1)
    provider, store, project = _provider(tmp_path, bridge)
    doctor_response = core_v1.EnvironmentDoctorResponseV1(
        status=core_v1.DoctorStatus.DEGRADED,
        checks=[
            core_v1.EnvironmentCheckV1(
                id="registry",
                kind=core_v1.EnvironmentCheckKind.REGISTRY,
                status=core_v1.CheckStatus.WARNING,
                message="Registry repair is available.",
                repair_action=core_v1.RepairAction.OPENEVO_CAN_RECONFIGURE,
                next_action="Run project repair.",
            )
        ],
        checked_at=NOW,
    )
    repair_request = core_v1.EnvironmentRepairRequestV1(
        execution_mode=core_v1.ExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT,
        actions=[core_v1.EnvironmentRepairAction.REPAIR_REGISTRY_INSTALL],
    )
    repair_operation = core_v1.OperationV1(
        id="operation-project-repair-1",
        kind=core_v1.OperationKind.ENVIRONMENT_REPAIR,
        descriptor=core_v1.OperationDescriptorV1(
            kind=core_v1.OperationKind.ENVIRONMENT_REPAIR,
            cancellable=True,
        ),
        status=core_v1.OperationStatus.QUEUED,
        request=core_v1.EnvironmentRepairOperationRequestV1(
            kind=core_v1.OperationKind.ENVIRONMENT_REPAIR,
            request=repair_request,
        ),
        logs_ref="logs-project-repair-1",
        created_at=NOW,
        updated_at=NOW,
        observed_at=NOW,
        etag=ETAG_A,
    )
    completed_repair = core_v1.OperationV1(
        id=repair_operation.id,
        kind=repair_operation.kind,
        descriptor=repair_operation.descriptor,
        status=core_v1.OperationStatus.SUCCEEDED,
        request=repair_operation.request,
        result=core_v1.EnvironmentRepairOperationResultV1(
            kind=core_v1.OperationKind.ENVIRONMENT_REPAIR,
            response=core_v1.EnvironmentRepairResponseV1(
                status=core_v1.DoctorStatus.OK,
                results=[
                    core_v1.RepairActionResultV1(
                        action=action,
                        status=core_v1.CheckStatus.OK,
                        message="Repair completed.",
                    )
                    for action in repair_request.actions
                ],
                checked_at=NOW,
            ),
        ),
        logs_ref=repair_operation.logs_ref,
        created_at=NOW,
        updated_at=NOW,
        observed_at=NOW,
        finished_at=NOW,
        etag='"' + "b" * 64 + '"',
    )
    post_doctor_response = core_v1.EnvironmentDoctorResponseV1(
        status=core_v1.DoctorStatus.OK,
        checks=[],
        checked_at=NOW,
    )
    bridge.doctor_project.side_effect = [
        doctor_response,
        doctor_response,
        post_doctor_response,
    ]
    bridge.repair_project.return_value = repair_operation
    bridge.get_operation.return_value = completed_repair

    def wait_for_terminal(operation_id: str) -> local_v1.LocalOperationV1:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            operation = store.get_local_operation(operation_id)
            if operation.state in {"succeeded", "failed", "cancelled"}:
                return operation
            time.sleep(0.01)
        pytest.fail("project maintenance operation did not finish")

    try:
        doctor_response_body = provider.invoke(
            "doctorProject",
            {
                "project_id": project.project_id,
                "idempotency_key": "project-doctor-routing-0001",
                "if_match": project.etag,
            },
        )
        assert isinstance(doctor_response_body, Response)
        doctor = local_v1.LocalOperationV1.model_validate_json(doctor_response_body.body)
        doctor = wait_for_terminal(doctor.operation_id)
        assert doctor.state == "succeeded"
        assert doctor.operation_kind == "project_doctor"
        assert doctor.checks == (
            local_v1.NormalizedCheckV1(
                check_id="registry",
                label="Registry",
                status="warning",
                summary="Registry repair is available.",
                repair_action="openevo_can_retry",
            ),
        )

        repair_response_body = provider.invoke(
            "repairProject",
            {
                "project_id": project.project_id,
                "idempotency_key": "project-repair-routing-0001",
                "if_match": project.etag,
            },
        )
        assert isinstance(repair_response_body, Response)
        repair = local_v1.LocalOperationV1.model_validate_json(repair_response_body.body)
        repair = wait_for_terminal(repair.operation_id)
        assert repair.state == "succeeded"
        assert repair.operation_kind == "project_repair"

        replay_response = provider.invoke(
            "repairProject",
            {
                "project_id": project.project_id,
                "idempotency_key": "project-repair-routing-0001",
                "if_match": project.etag,
            },
        )
        assert isinstance(replay_response, Response)
        replay = local_v1.LocalOperationV1.model_validate_json(replay_response.body)
        assert replay == repair

        preflight_key = hashlib.sha256(
            b"project-repair-routing-0001\0repair-doctor"
        ).hexdigest()
        postflight_key = hashlib.sha256(
            b"project-repair-routing-0001\0repair-post-doctor"
        ).hexdigest()
        assert bridge.doctor_project.call_args_list == [
            call(
                project,
                idempotency_key="project-doctor-routing-0001",
            ),
            call(project, idempotency_key=preflight_key),
            call(project, idempotency_key=postflight_key),
        ]
        bridge.repair_project.assert_called_once_with(
            project,
            actions=(core_v1.EnvironmentRepairAction.REPAIR_REGISTRY_INSTALL,),
            idempotency_key="project-repair-routing-0001",
        )
        bridge.get_operation.assert_called_once_with(
            project,
            repair_operation.id,
        )
    finally:
        provider.close()


def test_project_doctor_persists_typed_core_failure_under_local_operation_identity(
    tmp_path: Path,
) -> None:
    bridge = Mock(spec=DesktopCoreBridgeV1)
    provider, store, project = _provider(tmp_path, bridge)
    error = core_v1.ApiErrorV1(
        request_id="core-doctor-request-1",
        code="registry_attestation_unavailable",
        http_status=503,
        message="The verified registry is unavailable.",
        severity=ErrorSeverity.BLOCKING,
        category=ErrorCategory.ENVIRONMENT,
        retryable=True,
        repair_action=RepairAction.OPENEVO_CAN_RETRY,
        next_action="Retry project doctor.",
    )
    bridge.doctor_project.side_effect = DesktopCoreBridgeErrorV1(error)
    try:
        response = provider.invoke(
            "doctorProject",
            {
                "project_id": project.project_id,
                "idempotency_key": "project-doctor-failure-0001",
                "if_match": project.etag,
            },
        )
        assert isinstance(response, Response)
        accepted = local_v1.LocalOperationV1.model_validate_json(response.body)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            failed = store.get_local_operation(accepted.operation_id)
            if failed.state == "failed":
                break
            time.sleep(0.01)
        else:
            pytest.fail("project doctor failure did not become durable")

        assert failed.error == error.model_copy(update={"request_id": accepted.operation_id})
        replay = provider.invoke(
            "doctorProject",
            {
                "project_id": project.project_id,
                "idempotency_key": "project-doctor-failure-0001",
                "if_match": project.etag,
            },
        )
        assert isinstance(replay, Response)
        assert local_v1.LocalOperationV1.model_validate_json(replay.body) == failed
        bridge.doctor_project.assert_called_once()
    finally:
        provider.close()


def test_project_repair_selects_only_core_authorized_mode_safe_actions() -> None:
    response = core_v1.EnvironmentDoctorResponseV1(
        status=core_v1.DoctorStatus.NEEDS_USER_ACTION,
        checks=[
            core_v1.EnvironmentCheckV1(
                id="network",
                kind=core_v1.EnvironmentCheckKind.NETWORK,
                status=core_v1.CheckStatus.WARNING,
                message="Retry is available.",
                repair_action=RepairAction.OPENEVO_CAN_RETRY,
            ),
            core_v1.EnvironmentCheckV1(
                id="model",
                kind=core_v1.EnvironmentCheckKind.MODEL_SERVICE,
                status=core_v1.CheckStatus.BLOCKING,
                message="Model service is unavailable.",
                repair_action=RepairAction.OPENEVO_CAN_RETRY,
                model_preparation=core_v1.ModelPreparationV1(
                    model_ref="org/model",
                    status=core_v1.ModelPreparationStatus.UNRESOLVED,
                    updated_at=NOW,
                ),
            ),
            core_v1.EnvironmentCheckV1(
                id="registry",
                kind=core_v1.EnvironmentCheckKind.REGISTRY,
                status=core_v1.CheckStatus.BLOCKING,
                message="Maintainer intervention is required.",
                repair_action=RepairAction.USER_ACTION_REQUIRED,
            ),
            core_v1.EnvironmentCheckV1(
                id="storage",
                kind=core_v1.EnvironmentCheckKind.STORAGE,
                status=core_v1.CheckStatus.OK,
                message="Storage is healthy.",
                repair_action=RepairAction.OPENEVO_CAN_RECONFIGURE,
            ),
        ],
        checked_at=NOW,
    )

    assert DesktopReleaseProvider._repair_actions_from_doctor(
        response,
        execution_mode="codex_subscription_transcript",
    ) == (core_v1.EnvironmentRepairAction.RETRY_NETWORK,)
    assert DesktopReleaseProvider._repair_actions_from_doctor(
        response,
        execution_mode="self-deployed",
    ) == (
        core_v1.EnvironmentRepairAction.RETRY_NETWORK,
        core_v1.EnvironmentRepairAction.RESTART_MODEL_SERVICE,
    )


def test_doctor_summary_projection_is_bounded_and_visibly_truncated() -> None:
    message = "x" * 4096
    response = core_v1.EnvironmentDoctorResponseV1(
        status=core_v1.DoctorStatus.DEGRADED,
        checks=[
            core_v1.EnvironmentCheckV1(
                id="network",
                kind=core_v1.EnvironmentCheckKind.NETWORK,
                status=core_v1.CheckStatus.WARNING,
                message=message,
                repair_action=RepairAction.OPENEVO_CAN_RETRY,
            )
        ],
        checked_at=NOW,
    )

    (check,) = DesktopReleaseProvider._normalize_doctor_checks(response)
    assert len(check.summary) == 512
    assert check.summary == ("x" * 511) + "…"


def test_project_maintenance_cannot_claim_local_only_cancellation(tmp_path: Path) -> None:
    bridge = Mock(spec=DesktopCoreBridgeV1)
    provider, store, project = _provider(tmp_path, bridge)
    reservation = store.begin_project_runtime_action(
        route=f"/desktop/v1/projects/{project.project_id}/repair",
        operation_kind="project_repair",
        project_id=project.project_id,
        key="project-repair-no-local-cancel-0001",
        body={},
        if_match=project.etag,
    )
    try:
        with pytest.raises(LocalOperationCancellationUnavailableError):
            provider.invoke(
                "cancelLocalOperation",
                {
                    "operation_id": reservation.operation.operation_id,
                    "if_match": reservation.operation.etag,
                },
            )
        assert (
            store.get_local_operation(reservation.operation.operation_id).state
            == "queued"
        )
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
        build_version="0.1.8",
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
        build_version="0.1.8",
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
        build_version="0.1.8",
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
        build_version="0.1.8",
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


def test_release_app_publishes_recovery_and_diagnostic_routes_with_session_auth(
    tmp_path: Path,
) -> None:
    bridge = Mock(spec=DesktopCoreBridgeV1)
    restart_request = core_v1.ServiceRestartRequestV1(reason="Requested from OpenEvo Desktop.")
    restart_operation = core_v1.OperationV1(
        id="operation-service-restart-1",
        kind=core_v1.OperationKind.SERVICE_RESTART,
        descriptor=core_v1.OperationDescriptorV1(
            kind=core_v1.OperationKind.SERVICE_RESTART,
            cancellable=False,
        ),
        status=core_v1.OperationStatus.QUEUED,
        request=core_v1.ServiceRestartOperationRequestV1(
            kind=core_v1.OperationKind.SERVICE_RESTART,
            service_id="service-1",
            request=restart_request,
        ),
        logs_ref="logs-service-restart-1",
        created_at=NOW,
        updated_at=NOW,
        observed_at=NOW,
        etag=ETAG_A,
    )
    diagnostic_request = core_v1.DiagnosticsRequestV1(
        scopes=[core_v1.DiagnosticScope.PROJECT],
        target=core_v1.ProjectDiagnosticTargetV1(
            kind=core_v1.DiagnosticTargetKind.PROJECT,
            project_id="core-project-1",
        ),
    )
    diagnostic = core_v1.DiagnosticV1(
        id="diagnostic-1",
        status=core_v1.DiagnosticStatus.QUEUED,
        scopes=diagnostic_request.scopes,
        target=diagnostic_request.target,
        checks=[],
        created_at=NOW,
        updated_at=NOW,
        observed_at=NOW,
        etag=ETAG_A,
    )
    cache_request = core_v1.CacheCleanupRequestV1(
        scopes=[core_v1.CacheScope.BUILD_ARTIFACTS],
        older_than_days=30,
    )
    cache_operation = core_v1.OperationV1(
        id="operation-cache-cleanup-1",
        kind=core_v1.OperationKind.CACHE_CLEANUP,
        descriptor=core_v1.OperationDescriptorV1(
            kind=core_v1.OperationKind.CACHE_CLEANUP,
            cancellable=False,
        ),
        status=core_v1.OperationStatus.QUEUED,
        request=core_v1.CacheCleanupOperationRequestV1(
            kind=core_v1.OperationKind.CACHE_CLEANUP,
            request=cache_request,
        ),
        logs_ref="logs-cache-cleanup-1",
        created_at=NOW,
        updated_at=NOW,
        observed_at=NOW,
        etag=ETAG_A,
    )
    bridge.restart_service.return_value = restart_operation
    bridge.create_diagnostic.return_value = diagnostic
    bridge.get_diagnostic.return_value = diagnostic
    bridge.delete_diagnostic.return_value = None
    bridge.cache_cleanup.return_value = cache_operation
    token = "desktop-session-token-recovery-00000001"
    app = create_release_desktop_local_api_app(
        state_root=tmp_path / "recovery-routes",
        session_token=token,
        instance_id="7" * 32,
        readiness_key=b"w" * 32,
        source_commit="1234567",
        build_version="0.1.8",
        build_channel="test",
        remote_lifecycle=_Lifecycle(),  # type: ignore[arg-type]
        core_bridge=bridge,
    )
    project = _bind_app_project(app)
    session = {"X-OpenEvo-Desktop-Session": token}

    with TestClient(app) as client:
        unauthenticated = client.post(
            "/desktop/v1/services/service-1/restart",
            headers={
                "If-Match": ETAG_A,
                "Idempotency-Key": "service-restart-http-0001",
            },
        )
        restarted = client.post(
            "/desktop/v1/services/service-1/restart",
            headers={
                **session,
                "If-Match": ETAG_A,
                "Idempotency-Key": "service-restart-http-0001",
            },
        )
        created = client.post(
            "/desktop/v1/diagnostics",
            headers={
                **session,
                "Idempotency-Key": "diagnostic-create-http-0001",
            },
            json=diagnostic_request.model_dump(mode="json"),
        )
        fetched = client.get(
            "/desktop/v1/diagnostics/diagnostic-1",
            headers=session,
        )
        deleted = client.delete(
            "/desktop/v1/diagnostics/diagnostic-1",
            headers={
                **session,
                "If-Match": ETAG_A,
                "Idempotency-Key": "diagnostic-delete-http-0001",
            },
        )
        cleaned = client.post(
            "/desktop/v1/maintenance/cache-cleanup",
            headers={
                **session,
                "Idempotency-Key": "cache-cleanup-http-0001",
            },
            json=cache_request.model_dump(mode="json"),
        )

    assert unauthenticated.status_code == 401
    assert restarted.status_code == created.status_code == cleaned.status_code == 202
    assert restarted.json()["id"] == restart_operation.id
    assert created.json()["id"] == fetched.json()["id"] == diagnostic.id
    assert deleted.status_code == 204 and deleted.content == b""
    assert cleaned.json()["id"] == cache_operation.id
    bridge.restart_service.assert_called_once_with(
        project,
        "service-1",
        if_match=ETAG_A,
        idempotency_key="service-restart-http-0001",
    )
    bridge.create_diagnostic.assert_called_once_with(
        project,
        diagnostic_request,
        idempotency_key="diagnostic-create-http-0001",
    )
    bridge.get_diagnostic.assert_called_once_with(project, diagnostic.id)
    bridge.delete_diagnostic.assert_called_once_with(
        project,
        diagnostic.id,
        if_match=ETAG_A,
        idempotency_key="diagnostic-delete-http-0001",
    )
    bridge.cache_cleanup.assert_called_once_with(
        project,
        cache_request,
        idempotency_key="cache-cleanup-http-0001",
    )


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
        build_version="0.1.8",
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
        build_version="0.1.8",
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
