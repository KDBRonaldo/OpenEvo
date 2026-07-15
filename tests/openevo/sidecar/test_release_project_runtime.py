from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread
import time
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.responses import JSONResponse
import pytest

from desktop.sidecar.contracts.v1 import models as local_v1
from desktop.sidecar.core_bridge_v1 import (
    DesktopCoreBridgeErrorV1,
    DesktopCoreBridgeV1,
    map_project_create_v1,
)
from desktop.sidecar.provider_store import DesktopProviderStore
from desktop.sidecar.release_provider import DesktopReleaseProvider, _BoundedProjectExecutor
from desktop.sidecar.release_capabilities import RELEASE_EXECUTION_MODE_CAPABILITIES_V1
from desktop.sidecar.release_runtime import CoreRuntimeSessionBinding
from desktop.sidecar.remote_lifecycle import (
    RemoteConnectionFailedError,
    RemoteConnectionResult,
    RemoteLifecycleSnapshot,
)
from desktop.sidecar.workspace_imports import WorkspaceImportStore
from openevo.backend.contracts.v1 import models as core_v1
from openevo.evolution.framework.profiles import execution_profile_for_release_mode


NOW = "2026-07-14T12:00:00Z"
REGISTRY_DIGEST = "4" * 64
ETAG_A = '"' + "a" * 64 + '"'
ETAG_B = '"' + "b" * 64 + '"'


class _Lifecycle:
    def snapshot(self) -> RemoteLifecycleSnapshot:
        return RemoteLifecycleSnapshot(None, "disconnected")

    def disconnect(self, _profile_id: str | None = None) -> None:
        return None

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
    *,
    event_broker: object | None = None,
    lifecycle: object | None = None,
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
        execution_mode_capabilities=RELEASE_EXECUTION_MODE_CAPABILITIES_V1,
        remote_lifecycle=lifecycle or _Lifecycle(),  # type: ignore[arg-type]
        core_bridge=bridge,  # type: ignore[arg-type]
        event_broker=event_broker,  # type: ignore[arg-type]
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


def _bridge_error(code: str, *, status: int = 503) -> DesktopCoreBridgeErrorV1:
    return DesktopCoreBridgeErrorV1(
        core_v1.ApiErrorV1(
            request_id=f"request-{code}",
            code=code,
            http_status=status,
            message="The Core request failed.",
            severity=core_v1.ErrorSeverity.BLOCKING,
            category=core_v1.ErrorCategory.SERVICE,
            retryable=True,
            repair_action=core_v1.RepairAction.OPENEVO_CAN_RETRY,
            next_action="Retry the request.",
        )
    )


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
        transition = provider.invoke("getDesktopState", {})
        assert transition.core.state == "bootstrapping"
        assert transition.core.active_tunnel is False
        assert transition.core.operation_id == operation.operation_id
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
        bridge.activate_project.assert_called_once()
        activation_call = bridge.activate_project.call_args
        assert activation_call.args == (project,)
        assert activation_call.kwargs["idempotency_key"] == (
            "project-activate-runtime-0001"
        )
        assert activation_call.kwargs["activation_id"] == operation.operation_id
        assert isinstance(activation_call.kwargs["cancel_event"], Event)
        bridge.commit_local_activation.assert_called_once()
    finally:
        release.set()
        provider.close()


def test_activation_success_is_not_visible_before_local_binding_commit(tmp_path: Path) -> None:
    bridge = Mock(spec=DesktopCoreBridgeV1)
    provider, store, project = _provider(tmp_path, bridge)
    activation = _activation(project)
    commit_entered = Event()
    release_commit = Event()

    bridge.activate_project.return_value = activation

    def commit(*args: object, **kwargs: object) -> None:
        commit_entered.set()
        assert release_commit.wait(timeout=5)

    bridge.commit_local_activation.side_effect = commit
    try:
        response = provider.invoke("activateProject", _activate_arguments(project))
        assert isinstance(response, JSONResponse)
        operation_id = local_v1.LocalOperationV1.model_validate_json(
            response.body
        ).operation_id
        assert commit_entered.wait(timeout=5)

        observations: list[tuple[local_v1.LocalOperationV1, local_v1.DesktopStateV1]] = []
        observed = Event()

        def observe() -> None:
            operation = provider.invoke(
                "getLocalOperation", {"operation_id": operation_id}
            )
            state = provider.invoke("getDesktopState", {})
            observations.append((operation, state))
            observed.set()

        thread = Thread(target=observe)
        thread.start()
        assert not observed.wait(timeout=0.1)
        release_commit.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
        operation, state = observations[0]
        assert operation.state == "succeeded"
        assert state.core.state == "online"
        assert state.active_project is not None
        assert state.active_project.connection_state == "ready"
    finally:
        release_commit.set()
        provider.close()


def test_activation_binding_failure_never_persists_success(tmp_path: Path) -> None:
    bridge = Mock(spec=DesktopCoreBridgeV1)
    provider, store, project = _provider(tmp_path, bridge)
    bridge.activate_project.return_value = _activation(project)
    bridge.commit_local_activation.side_effect = RuntimeError("binding commit failed")
    try:
        response = provider.invoke("activateProject", _activate_arguments(project))
        assert isinstance(response, JSONResponse)
        operation_id = local_v1.LocalOperationV1.model_validate_json(
            response.body
        ).operation_id

        operation = _wait_for_operation(store, operation_id, "failed")

        assert operation.state == "failed"
        assert store.get_project(project.project_id).state == "draft"
        state = provider.invoke("getDesktopState", {})
        assert state.core.state == "offline"
        assert state.active_project is None
    finally:
        provider.close()


@pytest.mark.parametrize("late_failure", [False, True])
def test_cancel_running_activation_interrupts_bridge_and_releases_executor(
    tmp_path: Path,
    late_failure: bool,
) -> None:
    bridge = Mock(spec=DesktopCoreBridgeV1)
    provider, store, project = _provider(tmp_path, bridge)
    entered = Event()
    release = Event()
    exited = Event()
    calls = 0

    def activate(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls > 1:
            return _activation(project)
        entered.set()
        assert release.wait(timeout=5)
        exited.set()
        if late_failure:
            raise _bridge_error("cancelled_activation_late_failure")
        return _activation(project)

    bridge.activate_project.side_effect = activate
    bridge.cancel_activation.side_effect = lambda _activation_id: release.set()
    try:
        response = provider.invoke("activateProject", _activate_arguments(project))
        assert isinstance(response, JSONResponse)
        operation = local_v1.LocalOperationV1.model_validate_json(response.body)
        assert entered.wait(timeout=5)
        running = _wait_for_operation(store, operation.operation_id, "running")

        cancelled = provider.invoke(
            "cancelLocalOperation",
            {
                "operation_id": running.operation_id,
                "if_match": running.etag,
                "idempotency_key": "cancel-project-activation-runtime-0001",
            },
        )
        assert cancelled.state == "cancelled"
        replayed_cancel = provider.invoke(
            "cancelLocalOperation",
            {
                "operation_id": running.operation_id,
                "if_match": running.etag,
                "idempotency_key": "cancel-project-activation-runtime-replay-0001",
            },
        )
        assert replayed_cancel == cancelled
        assert exited.wait(timeout=1)
        assert _wait_for_operation(store, operation.operation_id, "cancelled").state == "cancelled"
        assert store.get_project(project.project_id).state == "draft"
        bridge.commit_local_activation.assert_not_called()

        retry_response = provider.invoke(
            "activateProject",
            {
                **_activate_arguments(project),
                "idempotency_key": "project-activate-after-cancel-0001",
            },
        )
        retry = local_v1.LocalOperationV1.model_validate_json(retry_response.body)
        assert _wait_for_operation(store, retry.operation_id, "succeeded").state == "succeeded"
        bridge.cancel_activation.assert_called_once_with(operation.operation_id)
        assert bridge.activate_project.call_count == 2
    finally:
        release.set()
        provider.close()


def test_cancel_queued_activation_does_not_interrupt_the_running_operation() -> None:
    executor = _BoundedProjectExecutor()
    first_entered = Event()
    first_release = Event()
    second_ran = Event()
    second_interrupted = Event()
    third_ran = Event()

    def first(_cancel_event: Event) -> None:
        first_entered.set()
        assert first_release.wait(timeout=5)

    try:
        assert executor.submit(
            "operation-running",
            first,
            accepted=lambda: None,
            interrupt=first_release.set,
        )
        assert first_entered.wait(timeout=1)
        assert executor.submit(
            "operation-queued",
            lambda _cancel_event: second_ran.set(),
            accepted=lambda: None,
            interrupt=second_interrupted.set,
        )
        assert executor.cancel("operation-queued", wait_seconds=1)
        assert not second_ran.is_set()
        assert not second_interrupted.is_set()

        first_release.set()
        assert executor.submit(
            "operation-after-cancel",
            lambda _cancel_event: third_ran.set(),
            accepted=lambda: None,
            interrupt=lambda: None,
        )
        assert third_ran.wait(timeout=1)
    finally:
        first_release.set()
        executor.close()


def test_provider_close_interrupts_activation_without_waiting_for_bridge_timeout(
    tmp_path: Path,
) -> None:
    bridge = Mock(spec=DesktopCoreBridgeV1)
    provider, _store, project = _provider(tmp_path, bridge)
    entered = Event()
    release = Event()

    def activate(*_args: object, **_kwargs: object) -> object:
        entered.set()
        assert release.wait(timeout=5)
        return _activation(project)

    bridge.activate_project.side_effect = activate
    bridge.cancel_activation.side_effect = lambda _activation_id: release.set()
    provider.invoke("activateProject", _activate_arguments(project))
    assert entered.wait(timeout=5)

    started = time.monotonic()
    provider.close()
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    bridge.cancel_activation.assert_called_once()


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


@pytest.mark.parametrize("late_failure", [False, True])
def test_stale_profile_connect_completion_cannot_replace_new_online_session(
    tmp_path: Path,
    late_failure: bool,
) -> None:
    class Lifecycle(_Lifecycle):
        def __init__(self) -> None:
            self.current = RemoteLifecycleSnapshot(None, "disconnected")
            self.entered = Event()
            self.release = Event()
            self.disconnect_calls = 0

        def snapshot(self) -> RemoteLifecycleSnapshot:
            return self.current

        def connect(self, profile: local_v1.RemoteProfileV1) -> RemoteConnectionResult:
            self.entered.set()
            assert self.release.wait(timeout=5)
            if late_failure:
                self.current = RemoteLifecycleSnapshot(
                    profile.profile_id,
                    "failed",
                    failure_code="ssh_connection_failed",
                )
                raise RemoteConnectionFailedError("late connection failure")
            self.current = RemoteLifecycleSnapshot(profile.profile_id, "connected")
            return RemoteConnectionResult(profile.profile_id, "connected")

        def disconnect(self, profile_id: str | None = None) -> None:
            del profile_id
            self.disconnect_calls += 1
            self.current = RemoteLifecycleSnapshot(None, "disconnected")

    lifecycle = Lifecycle()
    bridge = Mock(spec=DesktopCoreBridgeV1)
    provider, store, project = _provider(tmp_path, bridge, lifecycle=lifecycle)
    bridge.activate_project.return_value = _activation(project)
    profile = store.get_profile(project.profile_id)
    responses: list[JSONResponse] = []
    failures: list[BaseException] = []

    def connect() -> None:
        try:
            response = provider.invoke(
                "connectRemoteProfile",
                {
                    "profile_id": profile.profile_id,
                    "if_match": profile.etag,
                    "idempotency_key": "profile-connect-interleave-0001",
                },
            )
            assert isinstance(response, JSONResponse)
            responses.append(response)
        except BaseException as exc:
            failures.append(exc)

    thread = Thread(target=connect)
    thread.start()
    try:
        assert lifecycle.entered.wait(timeout=5)
        activation_response = provider.invoke("activateProject", _activate_arguments(project))
        activation_operation = local_v1.LocalOperationV1.model_validate_json(
            activation_response.body
        )
        _wait_for_operation(store, activation_operation.operation_id, "succeeded")
        assert provider.invoke("getDesktopState", {}).core.state == "online"
        online_binding = provider._core_session_binding
        assert online_binding is not None

        lifecycle.release.set()
        thread.join(timeout=5)

        assert not thread.is_alive()
        assert failures == []
        assert len(responses) == 1
        assert responses[0].status_code in {409, 503}
        state = provider.invoke("getDesktopState", {})
        assert state.core.state == "online"
        assert state.core.active_tunnel is True
        assert state.active_project is not None
        assert state.active_project.connection_state == "ready"
        assert provider._core_session_binding == online_binding
        assert lifecycle.disconnect_calls == 0
    finally:
        lifecycle.release.set()
        thread.join(timeout=5)
        provider.close()


def test_stale_profile_disconnect_completion_does_not_close_replacement_transport(
    tmp_path: Path,
) -> None:
    class Lifecycle(_Lifecycle):
        def __init__(self) -> None:
            self.current = RemoteLifecycleSnapshot(None, "disconnected")
            self.entered = Event()
            self.release = Event()
            self.disconnect_calls = 0

        def snapshot(self) -> RemoteLifecycleSnapshot:
            return self.current

        def disconnect(self, profile_id: str | None = None) -> None:
            del profile_id
            self.disconnect_calls += 1
            self.current = RemoteLifecycleSnapshot(None, "disconnected")
            self.entered.set()
            assert self.release.wait(timeout=5)

    lifecycle = Lifecycle()
    bridge = Mock(spec=DesktopCoreBridgeV1)
    provider, store, project = _provider(tmp_path, bridge, lifecycle=lifecycle)
    profile = store.get_profile(project.profile_id)
    lifecycle.current = RemoteLifecycleSnapshot(profile.profile_id, "connected")

    def activate(*_args: object, **_kwargs: object) -> object:
        lifecycle.current = RemoteLifecycleSnapshot(profile.profile_id, "connected")
        return _activation(project)

    bridge.activate_project.side_effect = activate
    responses: list[JSONResponse] = []

    def disconnect() -> None:
        response = provider.invoke(
            "disconnectRemoteProfile",
            {
                "profile_id": profile.profile_id,
                "if_match": profile.etag,
                "idempotency_key": "profile-disconnect-interleave-0001",
            },
        )
        assert isinstance(response, JSONResponse)
        responses.append(response)

    thread = Thread(target=disconnect)
    thread.start()
    try:
        assert lifecycle.entered.wait(timeout=5)
        activation_response = provider.invoke("activateProject", _activate_arguments(project))
        activation_operation = local_v1.LocalOperationV1.model_validate_json(
            activation_response.body
        )
        _wait_for_operation(store, activation_operation.operation_id, "succeeded")
        assert provider.invoke("getDesktopState", {}).core.state == "online"
        online_binding = provider._core_session_binding
        assert online_binding is not None
        assert lifecycle.current.state == "connected"

        lifecycle.release.set()
        thread.join(timeout=5)

        assert not thread.is_alive()
        assert len(responses) == 1
        assert responses[0].status_code == 409
        state = provider.invoke("getDesktopState", {})
        assert state.core.state == "online"
        assert state.core.active_tunnel is True
        assert provider._core_session_binding == online_binding
        assert lifecycle.current == RemoteLifecycleSnapshot(profile.profile_id, "connected")
        assert lifecycle.disconnect_calls == 1
    finally:
        lifecycle.release.set()
        thread.join(timeout=5)
        provider.close()


def test_newer_profile_action_prevents_stale_activation_publication(tmp_path: Path) -> None:
    class Lifecycle(_Lifecycle):
        def __init__(self) -> None:
            self.current = RemoteLifecycleSnapshot(None, "disconnected")

        def snapshot(self) -> RemoteLifecycleSnapshot:
            return self.current

        def connect(self, profile: local_v1.RemoteProfileV1) -> RemoteConnectionResult:
            self.current = RemoteLifecycleSnapshot(profile.profile_id, "connected")
            return RemoteConnectionResult(profile.profile_id, "connected")

    lifecycle = Lifecycle()
    bridge = Mock(spec=DesktopCoreBridgeV1)
    provider, store, project = _provider(tmp_path, bridge, lifecycle=lifecycle)
    profile = store.get_profile(project.profile_id)
    entered = Event()
    release = Event()

    def activate(*_args: object, **_kwargs: object) -> object:
        entered.set()
        assert release.wait(timeout=5)
        return _activation(project)

    bridge.activate_project.side_effect = activate
    try:
        activation_response = provider.invoke("activateProject", _activate_arguments(project))
        activation_operation = local_v1.LocalOperationV1.model_validate_json(
            activation_response.body
        )
        assert entered.wait(timeout=5)

        connect_response = provider.invoke(
            "connectRemoteProfile",
            {
                "profile_id": profile.profile_id,
                "if_match": profile.etag,
                "idempotency_key": "profile-connect-supersedes-activation-0001",
            },
        )
        assert isinstance(connect_response, JSONResponse)
        assert connect_response.status_code == 202

        release.set()
        failed = _wait_for_operation(store, activation_operation.operation_id, "failed")
        state = provider.invoke("getDesktopState", {})

        assert failed.error is not None
        assert failed.error.code == "project_activation_failed"
        assert store.get_project(project.project_id).state == "draft"
        assert state.core.state == "offline"
        assert state.core.active_tunnel is False
        assert state.core.failure is not None
        assert state.core.failure.code == "core_not_started"
        bridge.commit_local_activation.assert_not_called()
        bridge.deactivate_project.assert_not_called()
    finally:
        release.set()
        provider.close()


def test_queue_rejection_preserves_the_existing_active_session(tmp_path: Path) -> None:
    bridge = Mock(spec=DesktopCoreBridgeV1)
    provider, store, project = _provider(tmp_path, bridge)
    profile = store.get_profile(project.profile_id)
    second = store.create_project(
        local_v1.ProjectCreateV1(
            name="Second project",
            profile_id=profile.profile_id,
            task=local_v1.ProjectTaskV1(title="Second task", objective="Second objective."),
            source=local_v1.ProjectSourceV1(kind="scratch", display_name="Second workspace"),
            execution=project.execution,
            evolution=project.evolution,
        ),
        idempotency_key="project-create-runtime-0002",
    )
    bridge.activate_project.return_value = _activation(project)
    try:
        first_response = provider.invoke("activateProject", _activate_arguments(project))
        first = local_v1.LocalOperationV1.model_validate_json(first_response.body)
        _wait_for_operation(store, first.operation_id, "succeeded")
        active = store.get_project(project.project_id)
        binding = provider._core_session_binding
        assert binding is not None

        class RejectingExecutor:
            def submit(self, *_args: object, **_kwargs: object) -> bool:
                return False

            def close(self) -> None:
                return None

        provider._project_executor.close()
        provider._project_executor = RejectingExecutor()  # type: ignore[assignment]

        second_response = provider.invoke(
            "activateProject",
            {
                "project_id": second.project_id,
                "if_match": second.etag,
                "idempotency_key": "project-activate-runtime-0002",
            },
        )
        rejected = local_v1.LocalOperationV1.model_validate_json(second_response.body)
        state = provider.invoke("getDesktopState", {})

        assert rejected.state == "failed"
        assert rejected.error is not None
        assert rejected.error.code == "project_operation_capacity_exhausted"
        assert state.core.state == "online"
        assert state.core.active_tunnel is True
        assert state.active_project is not None
        assert state.active_project.project_id == active.project_id
        assert state.active_project.connection_state == "ready"
        assert provider._core_session_binding == binding
        assert bridge.activate_project.call_count == 1
    finally:
        provider.close()


def test_local_client_loss_invalidates_only_the_matching_active_session(tmp_path: Path) -> None:
    bridge = Mock(spec=DesktopCoreBridgeV1)
    broker = Mock()
    provider, store, project = _provider(tmp_path, bridge, event_broker=broker)
    bridge.activate_project.return_value = _activation(project)
    response = provider.invoke("activateProject", _activate_arguments(project))
    operation = local_v1.LocalOperationV1.model_validate_json(response.body)
    _wait_for_operation(store, operation.operation_id, "succeeded")
    active = store.get_project(project.project_id)
    binding = provider._core_session_binding
    assert binding is not None
    bridge.list_services.side_effect = _bridge_error("core_client_closed")
    broker.publish.reset_mock()
    try:
        with pytest.raises(DesktopCoreBridgeErrorV1):
            provider.invoke(
                "listServices",
                {"limit": 50, "after": None, "sort": "display_name", "direction": "asc"},
            )
        state = provider.invoke("getDesktopState", {})
        assert state.core.state == "offline"
        assert state.core.active_tunnel is False
        assert state.active_project is not None
        assert state.active_project.connection_state == "offline"
        assert broker.publish.called
        published = broker.publish.call_args.args[0]
        assert published.state.core.state == "offline"
        assert published.state.core.active_tunnel is False

        provider._core_session_binding = CoreRuntimeSessionBinding(
            project=active,
            generation=binding.generation + 1,
        )
        provider._core_state = local_v1.CoreConnectionStateV1(
            state="online",
            profile_id=active.profile_id,
            active_tunnel=True,
            core=local_v1.CoreCompatibilityV1(
                contract_digest="3" * 64,
                core_version="0.1.0",
            ),
        )
        provider._handle_core_session_loss(binding, _bridge_error("core_client_closed"))
        assert provider.invoke("getDesktopState", {}).core.state == "online"
    finally:
        provider.close()


@pytest.mark.parametrize("code", ["core_connection_failed", "core_business_unavailable"])
def test_unproven_core_failures_do_not_invalidate_the_active_session(
    tmp_path: Path,
    code: str,
) -> None:
    bridge = Mock(spec=DesktopCoreBridgeV1)
    provider, store, project = _provider(tmp_path, bridge)
    bridge.activate_project.return_value = _activation(project)
    response = provider.invoke("activateProject", _activate_arguments(project))
    operation = local_v1.LocalOperationV1.model_validate_json(response.body)
    _wait_for_operation(store, operation.operation_id, "succeeded")
    bridge.list_services.side_effect = _bridge_error(code)
    try:
        with pytest.raises(DesktopCoreBridgeErrorV1):
            provider.invoke(
                "listServices",
                {"limit": 50, "after": None, "sort": "display_name", "direction": "asc"},
            )
        state = provider.invoke("getDesktopState", {})
        assert state.core.state == "online"
        assert state.core.active_tunnel is True
        assert state.active_project is not None
        assert state.active_project.connection_state == "ready"
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
        state = provider.invoke("getDesktopState", {})
        assert state.active_project is None
        assert state.core.state == "offline"
        assert state.core.active_tunnel is False
        assert state.core.failure is not None
        assert state.core.failure.code == "core_not_started"
        assert provider._core_session_binding is None
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


def test_project_edit_retains_diagnostic_authority_when_deactivation_fails(
    tmp_path: Path,
) -> None:
    bridge = Mock(spec=DesktopCoreBridgeV1)
    provider, store, project = _provider(tmp_path, bridge)
    bridge.activate_project.return_value = _activation(project)
    response = provider.invoke("activateProject", _activate_arguments(project))
    operation = local_v1.LocalOperationV1.model_validate_json(response.body)
    _wait_for_operation(store, operation.operation_id, "succeeded")
    active = store.get_project(project.project_id)
    bridge.deactivate_project.side_effect = _bridge_error("core_client_closed")
    try:
        provider.invoke(
            "updateProject",
            {
                "project_id": active.project_id,
                "request": local_v1.ProjectPatchV1(name="Edited after failed retirement"),
                "if_match": active.etag,
            },
        )

        state = provider.invoke("getDesktopState", {})
        binding = provider._core_session_binding
        assert store.get_project(active.project_id).state == "draft"
        assert state.active_project is None
        assert state.core.state == "offline"
        assert state.core.active_tunnel is False
        assert state.core.failure is not None
        assert state.core.failure.code == "core_client_closed"
        assert binding is not None
        assert binding.project.project_id == active.project_id
        assert binding.generation == provider._session_generation
    finally:
        provider.close()
