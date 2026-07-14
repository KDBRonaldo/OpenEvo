from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread
import time
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.responses import JSONResponse

from desktop.sidecar.contracts.v1 import models as local_v1
from desktop.sidecar.core_bridge_v1 import (
    DesktopCoreBridgeErrorV1,
    DesktopCoreBridgeV1,
    map_project_create_v1,
)
from desktop.sidecar.provider_store import DesktopProviderStore
from desktop.sidecar.release_provider import DesktopReleaseProvider
from desktop.sidecar.workspace_imports import WorkspaceImportStore
from openevo.backend.contracts.v1 import models as core_v1
from openevo.evolution.framework.profiles import execution_profile_for_release_mode


NOW = "2026-07-14T12:00:00Z"
REGISTRY_DIGEST = "4" * 64
ETAG_A = '"' + "a" * 64 + '"'
ETAG_B = '"' + "b" * 64 + '"'


class _Lifecycle:
    def close(self) -> None:
        return None


def _snapshot(
    snapshot_id: str,
    kind: core_v1.SnapshotKind,
    digest_char: str,
) -> core_v1.ImmutableSnapshotRefV1:
    return core_v1.ImmutableSnapshotRefV1(
        id=snapshot_id,
        kind=kind,
        content_sha256=digest_char * 64,
        created_at=NOW,
    )


def _provider(
    tmp_path: Path,
    bridge: Mock,
) -> tuple[DesktopReleaseProvider, DesktopProviderStore, local_v1.ProjectV1]:
    state_root = tmp_path / "state"
    store = DesktopProviderStore(state_root)
    profile = store.create_profile(
        local_v1.RemoteProfileCreateV1(
            name="Research server",
            host="compute.example.org",
            user="researcher",
        ),
        idempotency_key="profile-create-runtime-0001",
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
        idempotency_key="project-create-runtime-0001",
    )
    provider = DesktopReleaseProvider(
        store,
        WorkspaceImportStore(state_root / "workspace-imports", reconcile_on_open=False),
        build_version="0.1.0",
        source_commit="1234567",
        build_channel="test",
        instance_id="1" * 32,
        readiness_key=b"r" * 32,
        remote_lifecycle=_Lifecycle(),  # type: ignore[arg-type]
        core_bridge=bridge,  # type: ignore[arg-type]
        clock=lambda: datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc),
    )
    return provider, store, project


def _activation(project: local_v1.ProjectV1) -> SimpleNamespace:
    request = map_project_create_v1(project)
    revision = core_v1.RevisionRefV1(
        id="revision-0",
        project_id="core-project-1",
        generation=0,
        manifest_sha256="6" * 64,
    )
    core_project = core_v1.ProjectV1(
        id="core-project-1",
        name=request.name,
        description=request.description,
        status=core_v1.ProjectStatus.READY,
        execution_mode=request.spec.execution_mode,
        workspace_kind=core_v1.WorkspaceSourceKind.SCRATCH,
        current_project_snapshot=_snapshot(
            "project-snapshot-1", core_v1.SnapshotKind.PROJECT, "1"
        ),
        current_task_snapshot=_snapshot("task-snapshot-1", core_v1.SnapshotKind.TASK, "2"),
        current_workspace_snapshot=_snapshot(
            "workspace-snapshot-1", core_v1.SnapshotKind.WORKSPACE, "3"
        ),
        active_revision=revision,
        registry_digest=REGISTRY_DIGEST,
        model_preparation=core_v1.ModelPreparationV1(
            model_ref=request.spec.agent_model_ref,
            status=core_v1.ModelPreparationStatus.READY,
            updated_at=NOW,
        ),
        created_at=NOW,
        updated_at=NOW,
        etag=ETAG_B,
        spec=request.spec,
        task=request.task,
        workspace=request.workspace,
    )
    return SimpleNamespace(
        local_project_id=project.project_id,
        profile_id=project.profile_id,
        local_project_etag=project.etag,
        core_project=core_project,
        capabilities=core_v1.CapabilitiesResponseV1(
            core_version="0.1.0",
            registry_digest=REGISTRY_DIGEST,
            evaluated_profile=execution_profile_for_release_mode(request.spec.execution_mode),
            targets=(),
        ),
        revision_head=core_v1.RevisionHeadV1(
            project_id=core_project.id,
            active_revision=revision,
            updated_at=NOW,
            etag=ETAG_B,
        ),
    )


def _wait_for_operation(
    store: DesktopProviderStore,
    operation_id: str,
    *states: str,
) -> local_v1.LocalOperationV1:
    deadline = time.monotonic() + 5
    while True:
        operation = store.get_local_operation(operation_id)
        if operation.state in states:
            return operation
        if time.monotonic() >= deadline:
            raise AssertionError(f"operation remained {operation.state}")
        time.sleep(0.01)


def _activate_arguments(project: local_v1.ProjectV1) -> dict[str, object]:
    return {
        "project_id": project.project_id,
        "if_match": project.etag,
        "idempotency_key": "project-activate-runtime-0001",
    }


def test_activation_returns_queued_and_commits_remote_projection(tmp_path: Path) -> None:
    bridge = Mock(spec=DesktopCoreBridgeV1)
    provider, store, project = _provider(tmp_path, bridge)
    entered = Event()
    release = Event()
    activation = _activation(project)

    def activate(*args: object, **kwargs: object) -> object:
        entered.set()
        assert release.wait(timeout=5)
        return activation

    bridge.activate_project.side_effect = activate
    try:
        response = provider.invoke("activateProject", _activate_arguments(project))
        assert isinstance(response, JSONResponse)
        assert response.status_code == 202
        operation = local_v1.LocalOperationV1.model_validate_json(response.body)
        assert operation.state == "queued"
        assert entered.wait(timeout=5)
        assert store.get_local_operation(operation.operation_id).state == "running"
        assert operation.operation_id in store.pending_operation_ids()

        replay = provider.invoke("activateProject", _activate_arguments(project))
        assert isinstance(replay, JSONResponse)
        replayed = local_v1.LocalOperationV1.model_validate_json(replay.body)
        assert replayed.operation_id == operation.operation_id

        release.set()
        finished = _wait_for_operation(store, operation.operation_id, "succeeded")
        active = store.get_project(project.project_id)
        state = provider.invoke("getDesktopState", {})

        assert finished.result is not None
        assert active.state == "active"
        assert active.remote is not None
        assert active.remote.core_project_id == activation.core_project.id
        assert active.remote.active_revision == activation.core_project.active_revision
        assert active.remote.registry_digest == REGISTRY_DIGEST
        assert state.active_project is not None
        assert state.active_project.connection_state == "ready"
        assert state.core.state == "online"
        assert state.pending_operation_ids == ()
        bridge.activate_project.assert_called_once_with(
            project,
            idempotency_key="project-activate-runtime-0001",
        )
        bridge.commit_local_activation.assert_called_once()
    finally:
        release.set()
        provider.close()


def test_activation_persists_typed_bridge_failure(tmp_path: Path) -> None:
    bridge = Mock(spec=DesktopCoreBridgeV1)
    provider, store, project = _provider(tmp_path, bridge)
    bridge.activate_project.side_effect = DesktopCoreBridgeErrorV1(
        core_v1.ApiErrorV1(
            request_id="core-request-1",
            code="core_environment_blocked",
            http_status=409,
            message="The remote environment is not ready.",
            severity=core_v1.ErrorSeverity.BLOCKING,
            category=core_v1.ErrorCategory.ENVIRONMENT,
            retryable=True,
            repair_action=core_v1.RepairAction.OPENEVO_CAN_RETRY,
            next_action="Retry after repairing the remote environment.",
        )
    )
    try:
        response = provider.invoke("activateProject", _activate_arguments(project))
        assert isinstance(response, JSONResponse)
        operation = local_v1.LocalOperationV1.model_validate_json(response.body)
        failed = _wait_for_operation(store, operation.operation_id, "failed")
        state = provider.invoke("getDesktopState", {})

        assert failed.error is not None
        assert failed.error.code == "core_environment_blocked"
        assert failed.error.request_id == operation.operation_id
        assert store.get_project(project.project_id).state == "draft"
        assert state.core.state == "offline"
        assert state.core.failure is not None
        assert state.core.failure.code == "core_environment_blocked"
        assert state.pending_operation_ids == ()
        bridge.commit_local_activation.assert_not_called()
    finally:
        provider.close()


def test_active_core_request_blocks_project_edit_until_session_is_retired(
    tmp_path: Path,
) -> None:
    bridge = Mock(spec=DesktopCoreBridgeV1)
    provider, store, project = _provider(tmp_path, bridge)
    bridge.activate_project.return_value = _activation(project)
    entered = Event()
    release = Event()
    edit_started = Event()
    edit_finished = Event()
    order: list[str] = []
    errors: list[BaseException] = []

    def list_runs(*_args: object, **_kwargs: object) -> object:
        order.append("request-started")
        entered.set()
        assert release.wait(timeout=5)
        order.append("request-finished")
        return object()

    def retire(*_args: object, **_kwargs: object) -> None:
        order.append("session-retired")

    def invoke_runs() -> None:
        try:
            provider.invoke(
                "listRuns",
                {
                    "limit": 50,
                    "after": None,
                    "sort": "created_at",
                    "direction": "desc",
                },
            )
        except BaseException as exc:
            errors.append(exc)

    def edit_project(active: local_v1.ProjectV1) -> None:
        edit_started.set()
        try:
            provider.invoke(
                "updateProject",
                {
                    "project_id": active.project_id,
                    "request": local_v1.ProjectPatchV1(name="Edited project"),
                    "if_match": active.etag,
                },
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            edit_finished.set()

    bridge.list_runs.side_effect = list_runs
    bridge.deactivate_project.side_effect = retire
    request_thread: Thread | None = None
    edit_thread: Thread | None = None
    try:
        response = provider.invoke("activateProject", _activate_arguments(project))
        operation = local_v1.LocalOperationV1.model_validate_json(response.body)
        _wait_for_operation(store, operation.operation_id, "succeeded")
        active = store.get_project(project.project_id)

        request_thread = Thread(target=invoke_runs)
        request_thread.start()
        assert entered.wait(timeout=5)

        edit_thread = Thread(target=edit_project, args=(active,))
        edit_thread.start()
        assert edit_started.wait(timeout=5)
        assert not edit_finished.wait(timeout=0.1)

        release.set()
        request_thread.join(timeout=5)
        edit_thread.join(timeout=5)

        assert not request_thread.is_alive()
        assert not edit_thread.is_alive()
        assert errors == []
        assert order == ["request-started", "request-finished", "session-retired"]
        assert store.get_project(active.project_id).state == "draft"
        bridge.list_runs.assert_called_once_with(
            active,
            limit=50,
            after=None,
            sort="created_at",
            direction="desc",
        )
        bridge.deactivate_project.assert_called_once_with(active.project_id)
    finally:
        release.set()
        if request_thread is not None:
            request_thread.join(timeout=5)
        if edit_thread is not None:
            edit_thread.join(timeout=5)
        provider.close()
