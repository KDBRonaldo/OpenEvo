from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
import threading
import time
from typing import Any

from fastapi.responses import Response
import pytest

from openevo.backend.contracts.v1 import models as m
from openevo.backend.contracts.v1.store import CoreControlStoreV1
from openevo.backend.run_control import CoreRunControlError
import openevo.backend.science_run_owner as owner_module
from openevo.backend.science_run_owner import CoreScienceRunOwner
from openevo.backend.science_run_store import ScienceRunStore
from openevo.backend.service_supervisor import (
    ServiceExecutionMode,
    ServiceGroupSnapshot,
    ServiceRunBinding,
    ServiceRunReadinessCode,
)
from openevo.internal_auth import (
    GenerationBoundRunAdmissionCheck,
    InternalServiceIdentity,
    RunAdmissionError,
    RunAdmissionOperation,
)
from tests.framework_testkit import verified_builtin_registry


_GENERATION_DIGEST = "b" * 64
_FRAMEWORK_LOCK_DIGEST = "c" * 64
_PAYLOAD_DIGEST = "d" * 64


class _FakeServiceOwner:
    def __init__(
        self,
        binding: ServiceRunBinding,
        *,
        readiness_code: ServiceRunReadinessCode = ServiceRunReadinessCode.READY,
    ) -> None:
        self.binding = binding
        ready = readiness_code is ServiceRunReadinessCode.READY
        self.snapshot = ServiceGroupSnapshot(
            execution_mode=binding.execution_mode,
            services_available=ready,
            run_ready=ready,
            run_readiness_code=readiness_code,
            generation_digest=binding.generation_digest,
            services=(),
            runtime_identity_digest=(binding.runtime_identity_digest if ready else None),
            status_message=None if ready else "secret probe output must not escape",
        )
        self.ensure_entered = threading.Event()
        self.ensure_allowed = threading.Event()
        self.ensure_allowed.set()
        self.ensure_calls = 0

    def ensure(self, *args: object, **kwargs: object) -> ServiceGroupSnapshot:
        del args, kwargs
        self.ensure_calls += 1
        self.ensure_entered.set()
        if (
            threading.current_thread().name == "openevo-science-run-owner"
            and not self.ensure_allowed.wait(5)
        ):
            raise TimeoutError("test did not release service preparation")
        return self.snapshot

    def run_binding(self) -> ServiceRunBinding:
        return self.binding


class _FakeRolloutClient:
    def __init__(self) -> None:
        self.submissions: list[dict[str, Any]] = []
        self.closed = False

    def submit_task(self, payload: dict[str, Any]) -> str:
        self.submissions.append(payload)
        return str(payload["task_id"])

    def get_task(self, task_id: str) -> dict[str, Any]:
        return {"task_id": task_id, "status": "completed"}

    def close(self) -> None:
        self.closed = True


class _FakeEvolutionClient:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _RunnerSequence:
    def __init__(self, *steps: Callable[..., dict[str, Any]]) -> None:
        self.steps = list(steps)
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def __call__(self, config: object, **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            index = len(self.calls)
            self.calls.append({"config": config, **kwargs})
            step = self.steps[index]
        return step(config=config, **kwargs)


@pytest.fixture(scope="module")
def registry(tmp_path_factory: pytest.TempPathFactory):
    return verified_builtin_registry(tmp_path_factory.mktemp("science-owner-registry"))


def _project(store: CoreControlStoreV1, registry: object) -> m.ProjectV1:
    result = store.create_project(
        m.ProjectCreateV1.model_validate(
            {
                "name": "Durable science owner",
                "spec": {
                    "execution_mode": "codex_subscription_transcript",
                    "capture_mode": "transcript",
                    "harness_id": "codex",
                    "agent_model_ref": "gpt-5.1-codex-mini",
                    "evolution": {"targets": {}},
                },
                "task": {
                    "title": "Inspect the run owner",
                    "objective": "Produce one durable science result.",
                },
                "workspace": {"kind": "scratch", "display_name": "Scratch"},
            }
        ),
        idempotency_key="create-science-owner-project",
        registry_digest=registry.snapshot.registry_digest,
    )
    assert isinstance(result.model, m.ProjectV1)
    return result.model


def _run_request(project: m.ProjectV1) -> m.RunCreateV1:
    assert project.active_revision is not None
    assert project.registry_digest is not None
    assert project.current_workspace_snapshot is not None
    return m.RunCreateV1(
        project_id=project.id,
        project_snapshot=project.current_project_snapshot,
        task_snapshot=project.current_task_snapshot,
        workspace_snapshot=project.current_workspace_snapshot,
        expected_registry_digest=project.registry_digest,
        required_revision=m.ReachableRequiredRevisionRefV1(
            revision=project.active_revision,
            reachable_from_revision_id=project.active_revision.id,
            relation=m.RequiredRevisionRelation.ACTIVE,
        ),
    )


def _binding(registry: object) -> ServiceRunBinding:
    identity = InternalServiceIdentity(
        service_id="core-control",
        generation_digest=_GENERATION_DIGEST,
        registry_digest=registry.snapshot.registry_digest,
        framework_lock_digest=_FRAMEWORK_LOCK_DIGEST,
        credential="private-science-owner-test-credential-0123456789",
    )
    return ServiceRunBinding(
        execution_mode=ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT,
        runtime_image="openevo/science-runtime:0.1.0",
        runtime_identity_digest="f" * 64,
        generation_digest=identity.generation_digest,
        registry_digest=identity.registry_digest,
        framework_lock_digest=identity.framework_lock_digest,
        rollout_url="http://127.0.0.1:18100",
        evolution_backend_url="http://127.0.0.1:18200",
        gateway_url="http://127.0.0.1:18300",
        _identity=identity,
    )


def _owner(
    state_root: Path,
    store: CoreControlStoreV1,
    registry: object,
    services: _FakeServiceOwner,
    runner: Callable[..., dict[str, Any]],
) -> CoreScienceRunOwner:
    return CoreScienceRunOwner(
        state_root=state_root,
        project_store=store,
        service_supervisor=services,
        executable_registry=registry,
        experiment_runner=runner,
        rollout_factory=lambda _binding: _FakeRolloutClient(),
        evolution_factory=lambda _binding: _FakeEvolutionClient(),
        poll_interval_seconds=0,
        max_poll_attempts=10,
    )


def _completed_result(
    *,
    artifact_id: str = "memory-artifact-1",
    include_output: bool = True,
    output_type: str = "text_memory",
) -> dict[str, Any]:
    output = {
        "artifact_id": artifact_id,
        "type": output_type,
        "name": "Durable memory",
        "manifest": {"record_count": 1},
        "lineage": {
            "openevo_execution": {
                "target_id": "text_memory",
                "method_id": "memory_reflection",
                "job_id": "job-memory-1",
                "input_bindings": [{"artifact_id": "dataset-1", "artifact_type": "dataset"}],
            }
        },
        "scores": {"quality": 0.75},
        "promoted": True,
        "created_at": "2026-07-14T00:00:01Z",
        "payload_byte_size": 12,
        "payload_file_count": 1,
        "payload_manifest_digest": "e" * 64,
    }
    return {
        "status": "completed",
        "tasks": [
            {
                "rounds": [
                    {
                        "artifact_ids": {
                            "dataset": ["dataset-1"],
                            "text_memory": [artifact_id],
                        },
                        "jobs": [
                            {
                                "worker_results": [
                                    {
                                        "artifact_ids": [artifact_id],
                                        "outputs": [output] if include_output else [],
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }
        ],
    }


def _response_model(response: object) -> m.RunV1:
    assert isinstance(response, Response)
    return m.RunV1.model_validate_json(response.body)


def _invoke_create(
    owner: CoreScienceRunOwner,
    request: m.RunCreateV1,
    key: str,
) -> m.RunV1:
    return _response_model(
        owner.invoke(
            "createCoreRunV1",
            {"request": request, "idempotency_key": key},
        )
    )


def _get_run(owner: CoreScienceRunOwner, run_id: str) -> m.RunV1:
    return _response_model(owner.invoke("getCoreRunV1", {"run_id": run_id}))


def _wait_for_status(
    owner: CoreScienceRunOwner,
    run_id: str,
    expected: m.RunStatus | set[m.RunStatus],
    *,
    timeout: float = 5,
) -> m.RunV1:
    statuses = expected if isinstance(expected, set) else {expected}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = _get_run(owner, run_id)
        if run.status in statuses:
            return run
        time.sleep(0.01)
    raise AssertionError(f"run {run_id} did not reach {sorted(item.value for item in statuses)}")


def _cancel(owner: CoreScienceRunOwner, run: m.RunV1, key: str) -> m.RunV1:
    return _response_model(
        owner.invoke(
            "cancelCoreRunV1",
            {
                "run_id": run.id,
                "request": m.RunCancelRequestV1(reason=m.RunCancelReason.USER_REQUESTED),
                "if_match": run.etag,
                "idempotency_key": key,
            },
        )
    )


def _seed_run(
    root: Path,
    request: m.RunCreateV1,
    *,
    status: m.RunStatus,
    run_id: str = "run-recovery-seed",
) -> None:
    timestamp = "2026-07-15T12:00:00.000000Z"
    queued = owner_module._run_model(
        {
            "id": run_id,
            "project_id": request.project_id,
            "project_snapshot": request.project_snapshot,
            "task_snapshot": request.task_snapshot,
            "workspace_snapshot": request.workspace_snapshot,
            "registry_digest": request.expected_registry_digest,
            "execution_mode": m.ExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT,
            "capture_mode": m.CaptureMode.TRANSCRIPT,
            "status": m.RunStatus.QUEUED,
            "queued_reason": m.QueuedReasonV1(
                code=m.QueuedReasonCode.ADMISSION_PENDING,
                summary="Seeded for recovery.",
                retry_after_seconds=1,
            ),
            "attempt_count": 0,
            "required_revision": request.required_revision,
            "created_at": timestamp,
            "updated_at": timestamp,
            "attempts": [],
        },
        version=1,
    )
    ledger = ScienceRunStore(root / "science-runs")
    ledger.create_run(
        request=request,
        idempotency_key=f"seed-{run_id}",
        run=queued,
        input_context={},
    )
    if status is not m.RunStatus.QUEUED:
        ledger.mutate_run(
            run_id,
            lambda run, version: owner_module._transition_run(
                run,
                status,
                version=version,
                now=timestamp,
            ),
        )
    ledger.close()


def test_create_is_idempotent_and_rejects_a_valid_mismatched_reuse(
    tmp_path: Path, registry: object
) -> None:
    store = CoreControlStoreV1(tmp_path / "projects")
    project = _project(store, registry)
    services = _FakeServiceOwner(_binding(registry))
    services.ensure_allowed.clear()
    owner = _owner(
        tmp_path / "owner",
        store,
        registry,
        services,
        lambda *_args, **_kwargs: _completed_result(),
    )
    try:
        request = _run_request(project)
        created = _invoke_create(owner, request, "create-key")
        replay = _invoke_create(owner, request, "create-key")
        assert replay.id == created.id
        assert len(owner._ledger.list_runs()) == 1

        selected = _wait_for_status(owner, created.id, m.RunStatus.PREPARING)
        _cancel(owner, selected, "cancel-before-mismatch")
        services.ensure_allowed.set()
        _wait_for_status(owner, created.id, m.RunStatus.CANCELLED)
        store.activate_evolution_revision(
            project.id,
            predecessor=project.active_revision,
            run_id="external-successor",
            context_artifact_ids={},
        )
        mismatched = _run_request(store.get_project(project.id))
        with pytest.raises(CoreRunControlError) as error:
            _invoke_create(owner, mismatched, "create-key")
        assert error.value.code == "run_conflict"
        assert len(owner._ledger.list_runs()) == 1
    finally:
        services.ensure_allowed.set()
        owner.close()
        store.close()


@pytest.mark.parametrize(
    "readiness_code",
    [
        ServiceRunReadinessCode.CODEX_CLI_UNAVAILABLE,
        ServiceRunReadinessCode.CODEX_SUBSCRIPTION_AUTH_UNAVAILABLE,
        ServiceRunReadinessCode.RUNTIME_EXECUTABLE_UNAVAILABLE,
        ServiceRunReadinessCode.RUNTIME_IMAGE_UNAVAILABLE,
    ],
)
def test_create_fails_before_persistence_when_subscription_prerequisite_is_missing(
    tmp_path: Path,
    registry: object,
    readiness_code: ServiceRunReadinessCode,
) -> None:
    store = CoreControlStoreV1(tmp_path / "projects")
    project = _project(store, registry)
    services = _FakeServiceOwner(
        _binding(registry),
        readiness_code=readiness_code,
    )
    runner_calls: list[object] = []
    owner = _owner(
        tmp_path / "owner",
        store,
        registry,
        services,
        lambda config, **_kwargs: runner_calls.append(config) or _completed_result(),
    )
    try:
        with pytest.raises(CoreRunControlError) as error:
            _invoke_create(owner, _run_request(project), f"missing-{readiness_code.value}")

        assert error.value.code == f"run_{readiness_code.value}"
        assert "secret probe output" not in str(error.value)
        assert owner._ledger.list_runs() == []
        assert runner_calls == []
        assert services.ensure_calls == 1
    finally:
        owner.close()
        store.close()


def test_run_transitions_queued_preparing_running_succeeded_with_ordered_evidence(
    tmp_path: Path, registry: object
) -> None:
    store = CoreControlStoreV1(tmp_path / "projects")
    project = _project(store, registry)
    services = _FakeServiceOwner(_binding(registry))
    services.ensure_allowed.clear()
    runner_entered = threading.Event()
    runner_allowed = threading.Event()

    def run(_config: object, **_kwargs: object) -> dict[str, Any]:
        runner_entered.set()
        assert runner_allowed.wait(5)
        return _completed_result()

    owner = _owner(tmp_path / "owner", store, registry, services, run)
    try:
        queued = _invoke_create(owner, _run_request(project), "lifecycle")
        assert queued.status is m.RunStatus.QUEUED
        assert services.ensure_calls >= 1
        preparing = _wait_for_status(owner, queued.id, m.RunStatus.PREPARING)
        assert preparing.pinned_revision == project.active_revision
        services.ensure_allowed.set()
        assert runner_entered.wait(5)
        running = _wait_for_status(owner, queued.id, m.RunStatus.RUNNING)
        assert running.current_attempt is not None
        runner_allowed.set()
        succeeded = _wait_for_status(owner, queued.id, m.RunStatus.SUCCEEDED)
        assert succeeded.attempt_count == 1
        assert succeeded.current_attempt is not None
        assert succeeded.current_attempt.status is m.RunStatus.SUCCEEDED

        timeline = owner._ledger.timeline(queued.id)
        assert [item.sequence for item in timeline] == list(range(len(timeline)))
        assert [item.phase for item in timeline] == [
            m.TimelinePhase.ADMISSION,
            m.TimelinePhase.PREPARATION,
            m.TimelinePhase.EXECUTION,
            m.TimelinePhase.EVOLUTION,
            m.TimelinePhase.REVISION,
            m.TimelinePhase.TERMINAL,
        ]
        assert [item.status for item in timeline[-3:]] == [
            m.TimelineEventStatus.SUCCEEDED,
            m.TimelineEventStatus.SUCCEEDED,
            m.TimelineEventStatus.SUCCEEDED,
        ]
        logs = owner._ledger.logs(queued.id)
        assert [item.sequence for item in logs] == [0, 1]
        assert [item.level for item in logs] == [m.LogLevel.INFO, m.LogLevel.INFO]
    finally:
        services.ensure_allowed.set()
        runner_allowed.set()
        owner.close()
        store.close()


def test_cancelled_queued_run_is_not_selected_after_current_worker_releases(
    tmp_path: Path, registry: object
) -> None:
    store = CoreControlStoreV1(tmp_path / "projects")
    project = _project(store, registry)
    services = _FakeServiceOwner(_binding(registry))
    first_entered = threading.Event()
    first_allowed = threading.Event()

    def first_run(**_kwargs: object) -> dict[str, Any]:
        first_entered.set()
        assert first_allowed.wait(5)
        return _completed_result()

    runner = _RunnerSequence(first_run)
    owner = _owner(tmp_path / "owner", store, registry, services, runner)
    try:
        first = _invoke_create(owner, _run_request(project), "worker-first")
        assert first_entered.wait(5)
        _wait_for_status(owner, first.id, m.RunStatus.RUNNING)
        second = _invoke_create(owner, _run_request(project), "worker-second")
        assert second.status is m.RunStatus.QUEUED
        cancelled = _cancel(owner, second, "cancel-second")
        assert cancelled.status is m.RunStatus.CANCELLED

        first_allowed.set()
        _wait_for_status(owner, first.id, m.RunStatus.SUCCEEDED)
        time.sleep(0.05)
        assert _get_run(owner, second.id).status is m.RunStatus.CANCELLED
        assert len(runner.calls) == 1
    finally:
        first_allowed.set()
        owner.close()
        store.close()


def test_retry_enforces_etag_attempt_binding_and_idempotent_replay(
    tmp_path: Path, registry: object
) -> None:
    store = CoreControlStoreV1(tmp_path / "projects")
    project = _project(store, registry)
    services = _FakeServiceOwner(_binding(registry))
    retry_entered = threading.Event()
    retry_allowed = threading.Event()

    def fail(**_kwargs: object) -> dict[str, Any]:
        raise RuntimeError("injected runner failure")

    def succeed(**_kwargs: object) -> dict[str, Any]:
        retry_entered.set()
        assert retry_allowed.wait(5)
        return _completed_result()

    runner = _RunnerSequence(fail, succeed)
    owner = _owner(tmp_path / "owner", store, registry, services, runner)
    try:
        created = _invoke_create(owner, _run_request(project), "retry-create")
        failed = _wait_for_status(owner, created.id, m.RunStatus.FAILED)
        assert failed.current_attempt_id is not None
        request = m.RunRetryRequestV1(terminal_attempt_id=failed.current_attempt_id)

        with pytest.raises(CoreRunControlError) as stale:
            owner.invoke(
                "retryCoreRunV1",
                {
                    "run_id": failed.id,
                    "request": request,
                    "if_match": '"' + "0" * 64 + '"',
                    "idempotency_key": "retry-key",
                },
            )
        assert stale.value.code == "run_etag_precondition_failed"

        arguments = {
            "run_id": failed.id,
            "request": request,
            "if_match": failed.etag,
            "idempotency_key": "retry-key",
        }
        accepted_response = owner.invoke("retryCoreRunV1", arguments)
        accepted = _response_model(accepted_response)
        assert accepted.status is m.RunStatus.QUEUED
        assert accepted.attempt_count == 2
        assert retry_entered.wait(5)

        replay_response = owner.invoke("retryCoreRunV1", arguments)
        replay = _response_model(replay_response)
        assert replay == accepted
        assert isinstance(accepted_response, Response)
        assert isinstance(replay_response, Response)
        assert replay_response.headers["etag"] == accepted_response.headers["etag"]

        with pytest.raises(CoreRunControlError) as reused:
            owner.invoke(
                "retryCoreRunV1",
                {**arguments, "if_match": accepted.etag},
            )
        assert reused.value.code == "run_conflict"
        retry_allowed.set()
        succeeded = _wait_for_status(owner, created.id, m.RunStatus.SUCCEEDED)
        assert succeeded.attempt_count == 2
    finally:
        retry_allowed.set()
        owner.close()
        store.close()


def test_restart_recovers_interrupted_preparing_attempt_as_failed(
    tmp_path: Path, registry: object
) -> None:
    store = CoreControlStoreV1(tmp_path / "projects")
    project = _project(store, registry)
    request = _run_request(project)
    state_root = tmp_path / "owner"
    _seed_run(state_root, request, status=m.RunStatus.PREPARING)
    services = _FakeServiceOwner(_binding(registry))
    runner_calls: list[object] = []

    def should_not_run(_config: object, **kwargs: object) -> dict[str, Any]:
        runner_calls.append(kwargs)
        return _completed_result()

    owner = _owner(state_root, store, registry, services, should_not_run)
    try:
        recovered = _get_run(owner, "run-recovery-seed")
        assert recovered.status is m.RunStatus.FAILED
        assert recovered.current_error is not None
        assert recovered.current_error.code == "science_run_failed"
        assert recovered.attempt_count == 1
        assert runner_calls == []
        assert [item.status for item in owner._ledger.timeline(recovered.id)] == [
            m.TimelineEventStatus.FAILED
        ]
        assert [item.level for item in owner._ledger.logs(recovered.id)] == [m.LogLevel.ERROR]
    finally:
        owner.close()
        store.close()


def test_timeline_and_log_sequence_allocation_is_concurrency_safe(
    tmp_path: Path, registry: object
) -> None:
    store = CoreControlStoreV1(tmp_path / "projects")
    project = _project(store, registry)
    request = _run_request(project)
    state_root = tmp_path / "owner"
    _seed_run(state_root, request, status=m.RunStatus.CANCELLED, run_id="run-concurrent")
    services = _FakeServiceOwner(_binding(registry))

    def fixed_clock() -> datetime:
        return datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)

    owner = CoreScienceRunOwner(
        state_root=state_root,
        project_store=store,
        service_supervisor=services,
        executable_registry=registry,
        experiment_runner=lambda *_args, **_kwargs: _completed_result(),
        rollout_factory=lambda _binding: _FakeRolloutClient(),
        evolution_factory=lambda _binding: _FakeEvolutionClient(),
        clock=fixed_clock,
        poll_interval_seconds=0,
        max_poll_attempts=10,
    )
    run = _get_run(owner, "run-concurrent")

    def concurrently(call: Callable[[int], None], count: int = 24) -> list[BaseException]:
        barrier = threading.Barrier(count)
        errors: list[BaseException] = []
        lock = threading.Lock()

        def target(index: int) -> None:
            try:
                barrier.wait()
                call(index)
            except BaseException as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=target, args=(index,)) for index in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()
        return errors

    try:
        timeline_errors = concurrently(
            lambda index: owner._append_timeline(
                run,
                m.TimelinePhase.CAPTURE,
                m.TimelineEventStatus.SUCCEEDED,
                f"Capture {index}",
                f"Concurrent timeline entry {index}.",
            )
        )
        log_errors = concurrently(
            lambda index: owner._append_log(
                run,
                m.LogLevel.INFO,
                f"Concurrent log entry {index}.",
            )
        )
        assert timeline_errors == []
        assert log_errors == []
        assert [item.sequence for item in owner._ledger.timeline(run.id)] == list(range(24))
        assert [item.sequence for item in owner._ledger.logs(run.id)] == list(range(24))
    finally:
        owner.close()
        store.close()


def test_successor_context_is_pinned_into_the_next_session(
    tmp_path: Path, registry: object
) -> None:
    store = CoreControlStoreV1(tmp_path / "projects")
    project = _project(store, registry)
    services = _FakeServiceOwner(_binding(registry))

    def first(**_kwargs: object) -> dict[str, Any]:
        return _completed_result(artifact_id="memory-for-next-session")

    def second(**_kwargs: object) -> dict[str, Any]:
        return _completed_result(artifact_id="memory-second-session")

    runner = _RunnerSequence(first, second)
    owner = _owner(tmp_path / "owner", store, registry, services, runner)
    try:
        first_run = _invoke_create(owner, _run_request(project), "session-one")
        _wait_for_status(owner, first_run.id, m.RunStatus.SUCCEEDED)
        successor_project = store.get_project(project.id)
        assert successor_project.active_revision is not None
        assert successor_project.active_revision.generation == 1

        second_run = _invoke_create(owner, _run_request(successor_project), "session-two")
        _wait_for_status(owner, second_run.id, m.RunStatus.SUCCEEDED)
        assert runner.calls[0]["initial_context_artifact_ids"] == {}
        assert runner.calls[1]["initial_context_artifact_ids"] == {
            "agent_system": [],
            "dataset": ["dataset-1"],
            "parametric_memory": [],
            "skill_bundle": [],
            "text_memory": ["memory-for-next-session"],
        }
        context_response = owner.invoke("getCoreRunContextV1", {"run_id": second_run.id})
        assert isinstance(context_response, Response)
        context = m.RunContextV1.model_validate_json(context_response.body)
        assert [item.artifact_id for item in context.artifacts] == ["memory-for-next-session"]
        assert context.artifacts[0].revision.generation == 1
    finally:
        owner.close()
        store.close()


def test_admission_replays_only_exact_generation_payload_and_session(
    tmp_path: Path, registry: object
) -> None:
    store = CoreControlStoreV1(tmp_path / "projects")
    project = _project(store, registry)
    binding = _binding(registry)
    services = _FakeServiceOwner(binding)
    submitted = threading.Event()
    runner_allowed = threading.Event()
    admitted_task: list[str] = []

    def run(_config: object, **kwargs: Any) -> dict[str, Any]:
        task_id = str(kwargs["task_ids"][0])
        admitted_task.append(task_id)
        assert (
            kwargs["rollout_client"].submit_task(
                {"task_id": task_id, "instruction": "exact admitted payload"}
            )
            == task_id
        )
        submitted.set()
        assert runner_allowed.wait(5)
        return _completed_result()

    owner = _owner(tmp_path / "owner", store, registry, services, run)
    try:
        created = _invoke_create(owner, _run_request(project), "admission-run")
        assert submitted.wait(5)
        _wait_for_status(owner, created.id, m.RunStatus.RUNNING)
        exact = GenerationBoundRunAdmissionCheck(
            operation=RunAdmissionOperation.GATEWAY_SESSION_CREATE,
            generation_digest=binding.generation_digest,
            registry_digest=binding.registry_digest,
            framework_lock_digest=binding.framework_lock_digest,
            payload_sha256=_PAYLOAD_DIGEST,
            task_id=admitted_task[0],
            session_id="session-exact",
        )
        asyncio.run(owner.verify(exact))
        asyncio.run(owner.verify(exact))

        replacements: list[Mapping[str, str]] = [
            {"generation_digest": "1" * 64},
            {"registry_digest": "2" * 64},
            {"framework_lock_digest": "3" * 64},
            {"payload_sha256": "4" * 64},
        ]
        exact_data = {
            "operation": exact.operation,
            "generation_digest": exact.generation_digest,
            "registry_digest": exact.registry_digest,
            "framework_lock_digest": exact.framework_lock_digest,
            "payload_sha256": exact.payload_sha256,
            "task_id": exact.task_id,
            "session_id": exact.session_id,
        }
        for replacement in replacements:
            with pytest.raises(RunAdmissionError) as denied:
                asyncio.run(
                    owner.verify(GenerationBoundRunAdmissionCheck(**{**exact_data, **replacement}))
                )
            assert denied.value.code == "run_admission_denied"

        other_session = GenerationBoundRunAdmissionCheck(
            **{**exact_data, "session_id": "session-other"}
        )
        asyncio.run(owner.verify(other_session))
        with pytest.raises(RunAdmissionError):
            asyncio.run(
                owner.verify(
                    GenerationBoundRunAdmissionCheck(
                        **{
                            **exact_data,
                            "session_id": "session-other",
                            "payload_sha256": "5" * 64,
                        }
                    )
                )
            )
    finally:
        runner_allowed.set()
        owner.close()
        store.close()


def test_invalid_artifact_projection_cannot_advance_successor_revision(
    tmp_path: Path, registry: object
) -> None:
    store = CoreControlStoreV1(tmp_path / "projects")
    project = _project(store, registry)
    assert project.active_revision is not None
    predecessor = project.active_revision
    services = _FakeServiceOwner(_binding(registry))
    owner = _owner(
        tmp_path / "owner",
        store,
        registry,
        services,
        lambda *_args, **_kwargs: _completed_result(output_type="unsupported_artifact"),
    )
    try:
        created = _invoke_create(owner, _run_request(project), "bad-artifact")
        deadline = time.monotonic() + 5
        while owner._ledger.result_for_run(created.id) is None:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        time.sleep(0.05)
        assert _get_run(owner, created.id).status is not m.RunStatus.SUCCEEDED
        assert store.get_revision_head(project.id).active_revision == predecessor
        assert owner._ledger.artifacts_for_run(created.id) == []
        assert owner._ledger.revision_context(project.id, predecessor.id) == {}
    finally:
        owner.close()
        store.close()
