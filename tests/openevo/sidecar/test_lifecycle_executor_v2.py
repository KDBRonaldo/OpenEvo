from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Lock
import time

from desktop.sidecar.contracts.v2 import models as m
from desktop.sidecar.lifecycle_executor_v2 import (
    DesktopLifecycleExecutorV2,
    LifecycleExecutionContextV2,
    LifecycleOperationDeferredV2,
)
from desktop.sidecar.provider_store_v2 import (
    DesktopProviderStoreV2,
    LifecycleOperationReservationV2,
    LifecycleProjectCreateRequestV2,
)
from openevo.backend.contracts.v2.models import ScienceProjectConfigV2


KINDS = (
    "profile_connect",
    "profile_disconnect",
    "host_key_review",
    "native_workspace_prepare",
    "project_create",
    "project_activate",
)


class _Clock:
    def __init__(self) -> None:
        self._next = datetime(2026, 7, 27, 8, tzinfo=timezone.utc)
        self._lock = Lock()

    def __call__(self) -> datetime:
        with self._lock:
            value = self._next
            self._next += timedelta(microseconds=1)
            return value


def _config() -> ScienceProjectConfigV2:
    return ScienceProjectConfigV2.model_validate(
        {
            "task": {"title": "Task", "objective": "Exercise lifecycle execution."},
            "workspace": {"kind": "scratch", "display_name": "Workspace"},
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


def _reservation(project_id: str) -> LifecycleOperationReservationV2:
    request = m.ProjectCreateV2(
        profile_id="profile-1",
        profile_connection_generation=3,
        display_name="Lifecycle project",
        config=_config(),
    )
    return LifecycleOperationReservationV2(
        kind="project_create",
        resource={"resource_kind": "project", "resource_id": project_id},
        request=LifecycleProjectCreateRequestV2(
            request_kind="project_create",
            project_id=project_id,
            action_id="lifecycle-project-action-0001",
            request=request,
            resource_generation=3,
        ),
    )


def _runners(
    runner: Callable[[LifecycleExecutionContextV2], m.LifecycleResultV2],
) -> dict[str, Callable[[LifecycleExecutionContextV2], m.LifecycleResultV2]]:
    return {kind: runner for kind in KINDS}


def _project_result(context: LifecycleExecutionContextV2) -> m.LifecycleProjectResultV2:
    return m.LifecycleProjectResultV2(
        result_kind="project",
        project_id=context.operation.resource.resource_id,
    )


def _wait_terminal(
    store: DesktopProviderStoreV2,
    operation_id: str,
    *,
    timeout: float = 3.0,
) -> m.LifecycleOperationV2:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        operation = store.get_lifecycle_operation(operation_id)
        if operation.status in {"succeeded", "failed", "cancelled"}:
            return operation
        time.sleep(0.01)
    raise AssertionError("lifecycle operation did not become terminal")


def test_reservation_returns_promptly_while_external_work_remains_blocked(
    tmp_path: Path,
) -> None:
    started = Event()
    release = Event()

    def blocked(context: LifecycleExecutionContextV2) -> m.LifecycleResultV2:
        started.set()
        assert release.wait(16)
        return _project_result(context)

    store = DesktopProviderStoreV2(tmp_path / "provider", clock=_Clock())
    executor = DesktopLifecycleExecutorV2(store, runners=_runners(blocked))
    executor.start()
    before = time.monotonic()
    operation = executor.reserve(
        _reservation("project-slow"),
        idempotency_key="slow-project-create-0001",
    )
    elapsed = time.monotonic() - before

    assert elapsed < 0.5
    assert started.wait(1)
    assert executor.reserve(
        _reservation("project-slow"),
        idempotency_key="slow-project-create-0001",
    ).operation_id == operation.operation_id
    release.set()
    assert _wait_terminal(store, operation.operation_id).status == "succeeded"
    executor.close()
    store.close()


def test_executor_runs_fifo_with_one_external_worker(tmp_path: Path) -> None:
    first_started = Event()
    release_first = Event()
    order: list[str] = []

    def run(context: LifecycleExecutionContextV2) -> m.LifecycleResultV2:
        project_id = context.operation.resource.resource_id
        order.append(project_id)
        if project_id == "project-1":
            first_started.set()
            assert release_first.wait(2)
        return _project_result(context)

    store = DesktopProviderStoreV2(tmp_path / "provider", clock=_Clock())
    executor = DesktopLifecycleExecutorV2(store, runners=_runners(run))
    executor.start()
    first = executor.reserve(
        _reservation("project-1"),
        idempotency_key="fifo-project-create-0001",
    )
    assert first_started.wait(1)
    second = executor.reserve(
        _reservation("project-2"),
        idempotency_key="fifo-project-create-0002",
    )
    assert store.get_lifecycle_operation(second.operation_id).status == "queued"
    assert order == ["project-1"]
    release_first.set()

    assert _wait_terminal(store, first.operation_id).status == "succeeded"
    assert _wait_terminal(store, second.operation_id).status == "succeeded"
    assert order == ["project-1", "project-2"]
    executor.close()
    store.close()


def test_executor_persists_progress_sanitized_logs_and_terminal_result(
    tmp_path: Path,
) -> None:
    changed: list[tuple[str, str, int]] = []

    def run(context: LifecycleExecutionContextV2) -> m.LifecycleResultV2:
        assert context.idempotency_key == "progress-project-create-0001"
        context.checkpoint(
            "transferring",
            m.LifecycleProgressBytesV2(kind="bytes", completed=5, total=10),
            cancellable=True,
        )
        context.output_observer("ssh_stdout", b"\x1b[32mcopying\x1b[0m token-secret\n")
        context.output_observer("daemon_stderr", b"daemon ready\n")
        context.checkpoint(
            "verifying",
            m.LifecycleProgressIndeterminateV2(kind="indeterminate"),
            cancellable=True,
        )
        return _project_result(context)

    store = DesktopProviderStoreV2(tmp_path / "provider", clock=_Clock())
    executor = DesktopLifecycleExecutorV2(
        store,
        runners=_runners(run),
        operation_observer=lambda operation: changed.append(
            (operation.status, operation.phase, operation.log_sequence_high_watermark)
        ),
        secret_canaries=("token-secret",),
    )
    executor.start()
    operation = executor.reserve(
        _reservation("project-progress"),
        idempotency_key="progress-project-create-0001",
    )

    terminal = _wait_terminal(store, operation.operation_id)
    page = store.read_lifecycle_logs(operation.operation_id, limit=100, after=None)
    assert terminal.status == "succeeded" and terminal.phase == "finalizing"
    assert terminal.result == m.LifecycleProjectResultV2(
        result_kind="project", project_id="project-progress"
    )
    rendered = "".join(entry.text for entry in page.items)
    assert "copying" in rendered and "daemon ready" in rendered
    assert "token-secret" not in rendered and "\x1b" not in rendered
    assert {entry.source for entry in page.items} == {"ssh_stdout", "daemon_stderr"}
    assert changed[-1][0] == "succeeded"
    assert any(watermark > 0 for _status, _phase, watermark in changed)
    executor.close()
    store.close()


def test_running_cancellation_fences_a_late_success(tmp_path: Path) -> None:
    started = Event()
    release = Event()

    def late_success(context: LifecycleExecutionContextV2) -> m.LifecycleResultV2:
        started.set()
        assert release.wait(2)
        return _project_result(context)

    store = DesktopProviderStoreV2(tmp_path / "provider", clock=_Clock())
    executor = DesktopLifecycleExecutorV2(store, runners=_runners(late_success))
    executor.start()
    operation = executor.reserve(
        _reservation("project-cancel"),
        idempotency_key="cancel-project-create-0001",
    )
    assert started.wait(1)
    current = store.get_lifecycle_operation(operation.operation_id)
    requested = executor.cancel(
        operation.operation_id,
        if_match=current.etag,
        idempotency_key="cancel-running-operation-0001",
    )
    assert requested.status == "running" and not requested.cancellable
    release.set()

    assert _wait_terminal(store, operation.operation_id).status == "cancelled"
    executor.close()
    store.close()


def test_cancellation_wins_a_failure_terminal_etag_race(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fail(_context: LifecycleExecutionContextV2) -> m.LifecycleResultV2:
        raise RuntimeError("simulated runner failure")

    store = DesktopProviderStoreV2(tmp_path / "provider", clock=_Clock())
    original_finish = store.finish_lifecycle_operation
    cancellation_injected = False

    def finish_after_cancellation(completion):
        nonlocal cancellation_injected
        if not cancellation_injected and completion.status == "failed":
            cancellation_injected = True
            current = store.get_lifecycle_operation(completion.operation_id)
            requested = store.request_lifecycle_cancellation(
                current.operation_id,
                if_match=current.etag,
                idempotency_key="cancel-failure-terminal-race-0001",
            )
            assert requested.status == "running"
            assert not requested.cancellable
        return original_finish(completion)

    monkeypatch.setattr(store, "finish_lifecycle_operation", finish_after_cancellation)
    executor = DesktopLifecycleExecutorV2(store, runners=_runners(fail))
    executor.start()
    operation = executor.reserve(
        _reservation("project-failure-cancel-race"),
        idempotency_key="failure-cancel-race-project-0001",
    )

    terminal = _wait_terminal(store, operation.operation_id)
    assert cancellation_injected
    assert terminal.status == "cancelled"
    assert terminal.failure is None
    executor.close()
    store.close()


def test_pending_cancel_stops_at_the_non_cancellable_mutation_barrier(
    tmp_path: Path,
) -> None:
    before_barrier = Event()
    enter_barrier = Event()
    mutation_applied = Event()
    executor: DesktopLifecycleExecutorV2

    def apply_after_barrier(context: LifecycleExecutionContextV2) -> m.LifecycleResultV2:
        context.checkpoint(
            "remote_preflight",
            m.LifecycleProgressIndeterminateV2(kind="indeterminate"),
            cancellable=True,
        )
        before_barrier.set()
        assert enter_barrier.wait(2)
        executor.observe_progress(
            "creating_remote_project",
            m.LifecycleProgressIndeterminateV2(kind="indeterminate"),
            False,
        )
        mutation_applied.set()
        return _project_result(context)

    store = DesktopProviderStoreV2(tmp_path / "provider", clock=_Clock())
    executor = DesktopLifecycleExecutorV2(store, runners=_runners(apply_after_barrier))
    executor.start()
    operation = executor.reserve(
        _reservation("project-barrier"),
        idempotency_key="barrier-project-create-0001",
    )
    assert before_barrier.wait(1)
    current = store.get_lifecycle_operation(operation.operation_id)
    executor.cancel(
        operation.operation_id,
        if_match=current.etag,
        idempotency_key="cancel-before-project-barrier-0001",
    )
    enter_barrier.set()

    assert _wait_terminal(store, operation.operation_id).status == "cancelled"
    assert not mutation_applied.is_set()
    executor.close()
    store.close()


def test_cancellable_progress_callback_propagates_cancel_before_side_effect(
    tmp_path: Path,
) -> None:
    before_second_checkpoint = Event()
    enter_second_checkpoint = Event()
    side_effect_applied = Event()
    executor: DesktopLifecycleExecutorV2

    def apply_after_progress(context: LifecycleExecutionContextV2) -> m.LifecycleResultV2:
        executor.observe_progress(
            "transferring",
            m.LifecycleProgressBytesV2(kind="bytes", completed=1, total=2),
            True,
        )
        before_second_checkpoint.set()
        assert enter_second_checkpoint.wait(2)
        executor.observe_progress(
            "transferring",
            m.LifecycleProgressBytesV2(kind="bytes", completed=2, total=2),
            True,
        )
        side_effect_applied.set()
        return _project_result(context)

    store = DesktopProviderStoreV2(tmp_path / "provider", clock=_Clock())
    executor = DesktopLifecycleExecutorV2(store, runners=_runners(apply_after_progress))
    executor.start()
    operation = executor.reserve(
        _reservation("project-cancellable-progress"),
        idempotency_key="cancellable-progress-project-create-0001",
    )
    assert before_second_checkpoint.wait(1)
    current = store.get_lifecycle_operation(operation.operation_id)
    executor.cancel(
        operation.operation_id,
        if_match=current.etag,
        idempotency_key="cancel-cancellable-progress-0001",
    )
    enter_second_checkpoint.set()

    assert _wait_terminal(store, operation.operation_id).status == "cancelled"
    assert not side_effect_applied.is_set()
    executor.close()
    store.close()


def test_restart_reconciles_the_same_running_operation(tmp_path: Path) -> None:
    root = tmp_path / "provider"
    store = DesktopProviderStoreV2(root, clock=_Clock())
    operation = store.reserve_lifecycle_operation(
        _reservation("project-recover"),
        idempotency_key="recover-project-create-0001",
    )
    claimed = store.claim_next_lifecycle_operation()
    assert claimed is not None and claimed.operation.status == "running"
    store.close()

    reopened = DesktopProviderStoreV2(root, clock=_Clock())
    seen: list[str] = []

    def recover(context: LifecycleExecutionContextV2) -> m.LifecycleResultV2:
        seen.append(context.operation.operation_id)
        return _project_result(context)

    executor = DesktopLifecycleExecutorV2(reopened, runners=_runners(recover))
    executor.start()

    assert _wait_terminal(reopened, operation.operation_id).status == "succeeded"
    assert seen == [operation.operation_id]
    executor.close()
    reopened.close()


def test_deferred_running_operation_yields_to_prerequisite_then_resumes(
    tmp_path: Path,
) -> None:
    prerequisite_ready = Event()
    deferred_seen = Event()
    calls: list[str] = []

    def run(context: LifecycleExecutionContextV2) -> m.LifecycleResultV2:
        project_id = context.operation.resource.resource_id
        calls.append(project_id)
        if project_id == "project-deferred" and not prerequisite_ready.is_set():
            deferred_seen.set()
            raise LifecycleOperationDeferredV2
        if project_id == "project-prerequisite":
            prerequisite_ready.set()
        return _project_result(context)

    store = DesktopProviderStoreV2(tmp_path / "provider", clock=_Clock())
    executor = DesktopLifecycleExecutorV2(store, runners=_runners(run))
    executor.start()
    deferred = executor.reserve(
        _reservation("project-deferred"),
        idempotency_key="deferred-project-create-0001",
    )
    assert deferred_seen.wait(1)
    assert store.get_lifecycle_operation(deferred.operation_id).status == "running"

    prerequisite = executor.reserve(
        _reservation("project-prerequisite"),
        idempotency_key="prerequisite-project-create-0001",
    )

    assert _wait_terminal(store, prerequisite.operation_id).status == "succeeded"
    assert _wait_terminal(store, deferred.operation_id).status == "succeeded"
    assert calls == ["project-deferred", "project-prerequisite", "project-deferred"]
    executor.close()
    store.close()


def test_recovered_checkpoint_replay_never_regresses_durable_phase(
    tmp_path: Path,
) -> None:
    root = tmp_path / "provider"
    store = DesktopProviderStoreV2(root, clock=_Clock())
    operation = store.reserve_lifecycle_operation(
        _reservation("project-phase-replay"),
        idempotency_key="phase-replay-project-create-0001",
    )
    claimed = store.claim_next_lifecycle_operation()
    assert claimed is not None
    advanced = store.advance_lifecycle_operation(
        {
            "operation_id": operation.operation_id,
            "expected_etag": claimed.operation.etag,
            "phase": "creating_remote_project",
            "progress": {"kind": "indeterminate"},
            "cancellable": False,
        }
    )
    store.close()

    reopened = DesktopProviderStoreV2(root, clock=_Clock())

    def replay(context: LifecycleExecutionContextV2) -> m.LifecycleResultV2:
        retained = context.checkpoint(
            "remote_preflight",
            m.LifecycleProgressIndeterminateV2(kind="indeterminate"),
            cancellable=True,
        )
        assert retained.phase == advanced.phase
        assert not retained.cancellable
        later = context.checkpoint(
            "verifying_project",
            m.LifecycleProgressIndeterminateV2(kind="indeterminate"),
            cancellable=True,
        )
        assert later.phase == "verifying_project"
        assert not later.cancellable
        return _project_result(context)

    executor = DesktopLifecycleExecutorV2(reopened, runners=_runners(replay))
    executor.start()

    terminal = _wait_terminal(reopened, operation.operation_id)
    assert terminal.status == "succeeded"
    assert terminal.phase == "finalizing"
    executor.close()
    reopened.close()


def test_shutdown_leaves_running_authority_for_restart_recovery(tmp_path: Path) -> None:
    started = Event()

    def interrupted(context: LifecycleExecutionContextV2) -> m.LifecycleResultV2:
        started.set()
        assert context.cancellation_event.wait(2)
        context.check_cancelled()
        raise AssertionError("unreachable")

    store = DesktopProviderStoreV2(tmp_path / "provider", clock=_Clock())
    executor = DesktopLifecycleExecutorV2(store, runners=_runners(interrupted))
    executor.start()
    operation = executor.reserve(
        _reservation("project-shutdown"),
        idempotency_key="shutdown-project-create-0001",
    )
    assert started.wait(1)

    executor.close()

    assert store.get_lifecycle_operation(operation.operation_id).status == "running"
    store.close()


def test_runner_exception_persists_only_a_fixed_typed_failure(tmp_path: Path) -> None:
    def fail(_context: LifecycleExecutionContextV2) -> m.LifecycleResultV2:
        raise RuntimeError("SECRET raw exception detail")

    store = DesktopProviderStoreV2(tmp_path / "provider", clock=_Clock())
    executor = DesktopLifecycleExecutorV2(store, runners=_runners(fail))
    executor.start()
    operation = executor.reserve(
        _reservation("project-failure"),
        idempotency_key="failure-project-create-0001",
    )

    terminal = _wait_terminal(store, operation.operation_id)
    assert terminal.status == "failed" and terminal.failure is not None
    assert terminal.failure.code == "lifecycle_operation_failed"
    assert "SECRET" not in terminal.failure.summary
    executor.close()
    store.close()
