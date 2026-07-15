from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any

from fastapi.responses import Response
import pytest

from openevo.backend.contracts.v1 import models as m
from openevo.backend.contracts.v1.store import CoreControlStoreV1
from openevo.backend.run_control import CoreRunControlError
import openevo.backend.science_run_owner as owner_module
from openevo.backend.science_run_owner import CoreScienceRunOwner, _AdmittingRolloutClient
from openevo.backend.science_run_store import ScienceRunStore
from openevo.backend.service_supervisor import (
    ServiceExecutionMode,
    ServiceGroupSnapshot,
    ServiceRunBinding,
    ServiceRunLease,
    ServiceRunReadinessCode,
    SupervisorStateError,
)
from openevo.internal_auth import (
    GenerationBoundRunAdmissionCheck,
    InternalServiceIdentity,
    RunAdmissionError,
    RunAdmissionOperation,
)
from openevo.runtime.managed import MANAGED_RUNTIME_RELEASES
from openevo.rollout.models import canonicalize_task_request
from tests.framework_testkit import verified_builtin_registry


_GENERATION_DIGEST = "b" * 64
_FRAMEWORK_LOCK_DIGEST = "c" * 64
_PAYLOAD_DIGEST = "d" * 64


def _runtime_receipt(
    *,
    revision_id: str,
    artifacts: list[dict[str, str]],
) -> dict[str, object]:
    return {
        "schema_version": "2",
        "context_id": "context-verified",
        "revision_id": revision_id,
        "instruction_sha256": "1" * 64,
        "staged_tree_sha256": "2" * 64,
        "artifacts": sorted(artifacts, key=lambda item: item["artifact_id"]),
    }


def test_rollout_runtime_context_receipt_is_closed_and_canonical() -> None:
    expected = _runtime_receipt(
        revision_id="revision-1",
        artifacts=[
            {
                "artifact_id": "skill-1",
                "artifact_type": "skill_bundle",
                "content_sha256": "3" * 64,
                "staged_sha256": "4" * 64,
            },
            {
                "artifact_id": "memory-1",
                "artifact_type": "text_memory",
                "content_sha256": "5" * 64,
                "staged_sha256": "6" * 64,
            },
            {
                "artifact_id": "agent-1",
                "artifact_type": "agent_system",
                "content_sha256": "7" * 64,
                "staged_sha256": "8" * 64,
            },
        ],
    )
    receipt = owner_module._rollout_runtime_context_receipt(
        {
            "results": [
                {
                    "metadata": {
                        "evolution": {
                            "context_injected": True,
                            "context_id": "context-verified",
                            "context_artifact_ids": ["skill-1", "memory-1", "agent-1"],
                            "runtime_injection_receipt": expected,
                        }
                    }
                }
            ]
        }
    )

    assert receipt == expected
    assert owner_module._runtime_context_receipt({"runtime_context_receipt": receipt}) == receipt


@pytest.mark.parametrize(
    "evolution",
    [
        {"context_injected": False, "context_artifact_ids": ["artifact-1"]},
        {"context_injected": True, "context_artifact_ids": ["artifact-1", "artifact-1"]},
        {"context_injected": True, "context_artifact_ids": "artifact-1"},
    ],
)
def test_rollout_runtime_context_receipt_rejects_unproven_context(
    evolution: object,
) -> None:
    with pytest.raises(ValueError, match="context"):
        owner_module._rollout_runtime_context_receipt(
            {"results": [{"metadata": {"evolution": evolution}}]}
        )


def test_runtime_context_receipt_rejects_wrong_revision_content_and_membership() -> None:
    receipt = _runtime_receipt(
        revision_id="revision-1",
        artifacts=[
            {
                "artifact_id": "memory-1",
                "artifact_type": "text_memory",
                "content_sha256": "3" * 64,
                "staged_sha256": "4" * 64,
            }
        ],
    )
    authority = {
        "memory-1": {
            "artifact_type": "text_memory",
            "content_sha256": "3" * 64,
        }
    }

    with pytest.raises(ValueError, match="revision"):
        owner_module._verify_runtime_context_receipt(
            receipt,
            revision_id="revision-2",
            artifacts=authority,
        )
    with pytest.raises(ValueError, match="content authority"):
        owner_module._verify_runtime_context_receipt(
            receipt,
            revision_id="revision-1",
            artifacts={
                "memory-1": {
                    "artifact_type": "text_memory",
                    "content_sha256": "9" * 64,
                }
            },
        )
    with pytest.raises(ValueError, match="membership"):
        owner_module._verify_runtime_context_receipt(
            receipt,
            revision_id="revision-1",
            artifacts={
                **authority,
                "skill-1": {
                    "artifact_type": "skill_bundle",
                    "content_sha256": "5" * 64,
                },
            },
        )


def test_rollout_runtime_context_receipt_rejects_context_identity_mismatch() -> None:
    receipt = _runtime_receipt(
        revision_id="revision-1",
        artifacts=[
            {
                "artifact_id": "memory-1",
                "artifact_type": "text_memory",
                "content_sha256": "3" * 64,
                "staged_sha256": "4" * 64,
            }
        ],
    )

    with pytest.raises(ValueError, match="receipt context"):
        owner_module._rollout_runtime_context_receipt(
            {
                "results": [
                    {
                        "metadata": {
                            "evolution": {
                                "context_injected": True,
                                "context_id": "context-other",
                                "context_artifact_ids": ["memory-1"],
                                "runtime_injection_receipt": receipt,
                            }
                        }
                    }
                ]
            }
        )


class _FakeServiceOwner:
    def __init__(
        self,
        binding: ServiceRunBinding,
        *,
        readiness_code: ServiceRunReadinessCode = ServiceRunReadinessCode.READY,
    ) -> None:
        self.binding = binding
        self.binding_after_ensure: ServiceRunBinding | None = None
        self.ensure_error: BaseException | None = None
        self.block_all_ensures = False
        self.set_readiness(readiness_code)
        self.ensure_entered = threading.Event()
        self.ensure_allowed = threading.Event()
        self.ensure_allowed.set()
        self.ensure_calls = 0
        self.ensure_run_binding_calls = 0

    def set_readiness(self, readiness_code: ServiceRunReadinessCode) -> None:
        ready = readiness_code is ServiceRunReadinessCode.READY
        self.snapshot = ServiceGroupSnapshot(
            execution_mode=self.binding.execution_mode,
            services_available=ready,
            run_ready=ready,
            run_readiness_code=readiness_code,
            generation_digest=self.binding.generation_digest,
            services=(),
            runtime_image=self.binding.runtime_image if ready else None,
            runtime_image_immutable_reference=(
                self.binding.runtime_image_immutable_reference if ready else None
            ),
            runtime_identity_digest=(self.binding.runtime_identity_digest if ready else None),
            status_message=None if ready else "secret probe output must not escape",
        )

    def ensure(self, *args: object, **kwargs: object) -> ServiceGroupSnapshot:
        del args, kwargs
        self.ensure_calls += 1
        self.ensure_entered.set()
        if self.ensure_error is not None:
            raise self.ensure_error
        if (
            self.block_all_ensures
            or threading.current_thread().name == "openevo-science-run-owner"
        ) and not self.ensure_allowed.wait(5):
            raise TimeoutError("test did not release service preparation")
        return self.snapshot

    def ensure_run_binding(
        self, *args: object, **kwargs: object
    ) -> tuple[ServiceGroupSnapshot, ServiceRunLease | None]:
        self.ensure_run_binding_calls += 1
        snapshot = self.ensure(*args, **kwargs)
        return snapshot, ServiceRunLease(
            binding=self.binding_after_ensure or self.binding,
            _release=lambda: None,
        )

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


class _ReceiptRolloutClient(_FakeRolloutClient):
    def get_task(self, task_id: str) -> dict[str, Any]:
        payload = self.submissions[-1]
        metadata = payload["metadata"]
        evolution = metadata["evolution"]
        artifact_ids = evolution["context_artifact_ids"]
        revision_id = metadata["openevo"]["revision_id"]
        receipt = _runtime_receipt(
            revision_id=revision_id,
            artifacts=[
                {
                    "artifact_id": artifact_id,
                    "artifact_type": "text_memory",
                    "content_sha256": "e" * 64,
                    "staged_sha256": "f" * 64,
                }
                for artifact_id in artifact_ids
            ],
        )
        return {
            "task_id": task_id,
            "status": "completed",
            "results": [
                {
                    "metadata": {
                        "evolution": {
                            "context_injected": True,
                            "context_id": "context-verified",
                            "context_artifact_ids": artifact_ids,
                            "runtime_injection_receipt": receipt,
                        }
                    }
                }
            ],
        }


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


def _binding(
    registry: object,
    *,
    generation_digest: str = _GENERATION_DIGEST,
    runtime_identity_digest: str = "f" * 64,
) -> ServiceRunBinding:
    identity = InternalServiceIdentity(
        service_id="core-control",
        generation_digest=generation_digest,
        registry_digest=registry.snapshot.registry_digest,
        framework_lock_digest=_FRAMEWORK_LOCK_DIGEST,
        credential="private-science-owner-test-credential-0123456789",
    )
    return ServiceRunBinding(
        execution_mode=ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT,
        runtime_image="openevo/science-runtime:0.1.0",
        runtime_image_immutable_reference=(
            MANAGED_RUNTIME_RELEASES["managed_science"].trusted_digest
        ),
        runtime_identity_digest=runtime_identity_digest,
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
    rollout_factory: Callable[[ServiceRunBinding], object] | None = None,
) -> CoreScienceRunOwner:
    return CoreScienceRunOwner(
        state_root=state_root,
        project_store=store,
        service_supervisor=services,
        executable_registry=registry,
        experiment_runner=runner,
        rollout_factory=rollout_factory or (lambda _binding: _FakeRolloutClient()),
        evolution_factory=lambda _binding: _FakeEvolutionClient(),
        poll_interval_seconds=0,
        max_poll_attempts=10,
    )


def _completed_result(
    *,
    artifact_id: str = "memory-artifact-1",
    include_output: bool = True,
    output_type: str = "text_memory",
    promoted: bool = True,
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
        "promoted": promoted,
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
        assert error.value.code == "idempotency_key_reused"
        assert len(owner._ledger.list_runs()) == 1
    finally:
        services.ensure_allowed.set()
        owner.close()
        store.close()


def test_create_replay_and_conflict_precede_volatile_readiness(
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
        created = _invoke_create(owner, request, "readiness-order")
        services.set_readiness(ServiceRunReadinessCode.CODEX_SUBSCRIPTION_AUTH_UNAVAILABLE)
        calls_before_replay = services.ensure_calls

        replay = _invoke_create(owner, request, "readiness-order")
        assert replay.id == created.id
        assert services.ensure_calls == calls_before_replay

        mismatched = request.model_copy(update={"expected_registry_digest": "e" * 64})
        with pytest.raises(CoreRunControlError) as conflict:
            _invoke_create(owner, mismatched, "readiness-order")
        assert conflict.value.code == "idempotency_key_reused"
        assert services.ensure_calls == calls_before_replay
        assert len(owner._ledger.list_runs()) == 1
    finally:
        services.set_readiness(ServiceRunReadinessCode.READY)
        services.ensure_allowed.set()
        owner.close()
        store.close()


def test_concurrent_new_create_has_one_readiness_owner_and_one_durable_run(
    tmp_path: Path, registry: object
) -> None:
    store = CoreControlStoreV1(tmp_path / "projects")
    project = _project(store, registry)
    services = _FakeServiceOwner(_binding(registry))
    services.block_all_ensures = True
    services.ensure_allowed.clear()
    owner = _owner(
        tmp_path / "owner",
        store,
        registry,
        services,
        lambda *_args, **_kwargs: _completed_result(),
    )
    results: list[m.RunV1] = []
    errors: list[BaseException] = []

    def create() -> None:
        try:
            results.append(_invoke_create(owner, _run_request(project), "concurrent-create"))
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=create)
    second = threading.Thread(target=create)
    try:
        first.start()
        assert services.ensure_entered.wait(5)
        second.start()
        time.sleep(0.05)
        assert second.is_alive()
        assert services.ensure_calls == 1
        services.ensure_allowed.set()
        first.join(5)
        second.join(5)

        assert not first.is_alive()
        assert not second.is_alive()
        assert errors == []
        assert len(results) == 2
        assert results[0].id == results[1].id
        assert len(owner._ledger.list_runs()) == 1
    finally:
        services.ensure_allowed.set()
        first.join(5)
        second.join(5)
        owner.close()
        store.close()


def test_revision_activation_during_readiness_leaves_zero_persisted_runs(
    tmp_path: Path,
    registry: object,
) -> None:
    store = CoreControlStoreV1(tmp_path / "projects")
    project = _project(store, registry)
    services = _FakeServiceOwner(_binding(registry))
    services.block_all_ensures = True
    services.ensure_allowed.clear()
    owner = _owner(
        tmp_path / "owner",
        store,
        registry,
        services,
        lambda *_args, **_kwargs: _completed_result(),
    )
    request = _run_request(project)
    errors: list[BaseException] = []

    def create() -> None:
        try:
            _invoke_create(owner, request, "stale-readiness")
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=create)
    try:
        thread.start()
        assert services.ensure_entered.wait(5)
        store.activate_evolution_revision(
            project.id,
            predecessor=project.active_revision,
            run_id="readiness-race-successor",
            context_artifact_ids={},
        )
        services.ensure_allowed.set()
        thread.join(5)

        assert not thread.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], CoreRunControlError)
        assert errors[0].code == "run_snapshot_mismatch"
        assert owner._ledger.list_runs() == []

        with pytest.raises(CoreRunControlError) as retry:
            _invoke_create(owner, request, "stale-readiness")
        assert retry.value.code == "run_snapshot_mismatch"
        assert owner._ledger.list_runs() == []
    finally:
        services.ensure_allowed.set()
        thread.join(5)
        owner.close()
        store.close()


@pytest.mark.parametrize(
    "readiness_code",
    [
        ServiceRunReadinessCode.CODEX_CLI_UNAVAILABLE,
        ServiceRunReadinessCode.CODEX_SUBSCRIPTION_AUTH_UNAVAILABLE,
        ServiceRunReadinessCode.RUNTIME_EXECUTABLE_UNAVAILABLE,
        ServiceRunReadinessCode.RUNTIME_IMAGE_UNAVAILABLE,
        ServiceRunReadinessCode.RUNTIME_EVIDENCE_INVALID,
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


def test_create_maps_supervisor_failure_without_persisting_or_disclosing_detail(
    tmp_path: Path, registry: object
) -> None:
    store = CoreControlStoreV1(tmp_path / "projects")
    project = _project(store, registry)
    services = _FakeServiceOwner(_binding(registry))
    services.ensure_error = SupervisorStateError("private-supervisor-detail")
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
            _invoke_create(owner, _run_request(project), "supervisor-failure")

        assert error.value.code == "run_service_supervisor_failed"
        assert error.value.http_status == 503
        assert error.value.retryable is True
        assert "private-supervisor-detail" not in str(error.value)
        assert owner._ledger.list_runs() == []
        assert runner_calls == []
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
        assert services.ensure_run_binding_calls == 1

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


def test_generation_replacement_after_execution_ensure_fails_before_admission_or_runner(
    tmp_path: Path, registry: object
) -> None:
    store = CoreControlStoreV1(tmp_path / "projects")
    project = _project(store, registry)
    services = _FakeServiceOwner(_binding(registry))
    services.ensure_allowed.clear()
    runner_calls: list[dict[str, Any]] = []

    def run(_config: object, **kwargs: Any) -> dict[str, Any]:
        runner_calls.append(kwargs)
        return _completed_result()

    owner = _owner(tmp_path / "owner", store, registry, services, run)
    try:
        queued = _invoke_create(owner, _run_request(project), "generation-replacement")
        _wait_for_status(owner, queued.id, m.RunStatus.PREPARING)
        services.binding_after_ensure = _binding(
            registry,
            generation_digest="9" * 64,
            runtime_identity_digest="8" * 64,
        )
        services.ensure_allowed.set()

        failed = _wait_for_status(owner, queued.id, m.RunStatus.FAILED)
        assert failed.current_error is not None
        assert failed.current_error.code == "run_service_generation_changed"
        assert failed.current_error.http_status == 503
        assert failed.current_error.retryable is True
        assert failed.current_error.message == (
            "Managed services changed before the science run could start."
        )
        assert runner_calls == []
        with sqlite3.connect(owner._ledger.database) as connection:
            assert connection.execute("SELECT COUNT(*) FROM admissions").fetchone()[0] == 0
    finally:
        services.ensure_allowed.set()
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
        assert reused.value.code == "idempotency_key_reused"
        retry_allowed.set()
        succeeded = _wait_for_status(owner, created.id, m.RunStatus.SUCCEEDED)
        assert succeeded.attempt_count == 2
    finally:
        retry_allowed.set()
        owner.close()
        store.close()


def test_retry_timeline_failure_rolls_back_run_and_idempotency(
    tmp_path: Path,
    registry: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CoreControlStoreV1(tmp_path / "projects")
    project = _project(store, registry)
    services = _FakeServiceOwner(_binding(registry))
    retry_allowed = threading.Event()

    def fail(**_kwargs: object) -> dict[str, Any]:
        raise RuntimeError("injected runner failure")

    def block_retry(**_kwargs: object) -> dict[str, Any]:
        assert retry_allowed.wait(5)
        return _completed_result()

    owner = _owner(
        tmp_path / "owner",
        store,
        registry,
        services,
        _RunnerSequence(fail, block_retry),
    )
    try:
        created = _invoke_create(owner, _run_request(project), "retry-atomic-create")
        failed = _wait_for_status(owner, created.id, m.RunStatus.FAILED)
        assert failed.current_attempt_id is not None
        services.ensure_allowed.clear()
        request = m.RunRetryRequestV1(terminal_attempt_id=failed.current_attempt_id)
        arguments = {
            "run_id": failed.id,
            "request": request,
            "if_match": failed.etag,
            "idempotency_key": "retry-atomic-key",
        }
        original_timeline_entry = owner._timeline_entry

        def fail_retry_timeline(*args: object, **kwargs: object) -> m.TimelineEntryV1:
            if len(args) >= 5 and args[4] == "Retry queued":
                raise ValueError("injected retry timeline failure")
            return original_timeline_entry(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(owner, "_timeline_entry", fail_retry_timeline)
        with pytest.raises(CoreRunControlError) as rejected:
            owner.invoke("retryCoreRunV1", arguments)
        assert rejected.value.code == "run_state_invalid"

        unchanged = _get_run(owner, failed.id)
        assert unchanged.status is m.RunStatus.FAILED
        assert unchanged.attempt_count == failed.attempt_count
        assert unchanged.current_attempt_id == failed.current_attempt_id
        assert unchanged.etag == failed.etag

        monkeypatch.setattr(owner, "_timeline_entry", original_timeline_entry)
        accepted = _response_model(owner.invoke("retryCoreRunV1", arguments))
        assert accepted.status is m.RunStatus.QUEUED
        assert accepted.attempt_count == failed.attempt_count + 1
        retry_entries = [
            entry for entry in owner._ledger.timeline(failed.id) if entry.title == "Retry queued"
        ]
        assert len(retry_entries) == 1
    finally:
        retry_allowed.set()
        services.ensure_allowed.set()
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


def test_runner_cannot_supply_its_own_runtime_context_receipt(
    tmp_path: Path, registry: object
) -> None:
    store = CoreControlStoreV1(tmp_path / "projects")
    project = _project(store, registry)
    services = _FakeServiceOwner(_binding(registry))

    def runner(_config: object, **_kwargs: object) -> dict[str, Any]:
        result = _completed_result()
        result["runtime_context_receipt"] = _runtime_receipt(
            revision_id=project.active_revision.id,
            artifacts=[
                {
                    "artifact_id": "runner-forged",
                    "artifact_type": "text_memory",
                    "content_sha256": "3" * 64,
                    "staged_sha256": "4" * 64,
                }
            ],
        )
        return result

    owner = _owner(tmp_path / "owner", store, registry, services, runner)
    try:
        created = _invoke_create(owner, _run_request(project), "forged-receipt")
        completed = _wait_for_status(owner, created.id, m.RunStatus.SUCCEEDED)
        stored = owner._ledger.result_for_run(completed.id)
        assert stored is not None
        assert "runtime_context_receipt" not in stored
        assert not any(
            item.message.startswith("Runtime context receipt")
            for item in owner._ledger.logs(completed.id)
        )
    finally:
        owner.close()
        store.close()


def test_successor_context_is_pinned_into_the_next_session(
    tmp_path: Path, registry: object
) -> None:
    store = CoreControlStoreV1(tmp_path / "projects")
    project = _project(store, registry)
    services = _FakeServiceOwner(_binding(registry))

    successor_revision_id: list[str] = []

    def first(**_kwargs: object) -> dict[str, Any]:
        return _completed_result(
            artifact_id="memory-for-next-session",
            promoted=False,
        )

    def second(**kwargs: Any) -> dict[str, Any]:
        task_id = str(kwargs["task_ids"][0])
        rollout = kwargs["rollout_client"]
        assert (
            rollout.submit_task(
                {
                    "task_id": task_id,
                    "metadata": {
                        "openevo": {"revision_id": successor_revision_id[0]},
                        "evolution": {"context_artifact_ids": ["memory-for-next-session"]},
                    },
                }
            )
            == task_id
        )
        assert rollout.get_task(task_id)["status"] == "completed"
        return _completed_result(artifact_id="memory-second-session")

    runner = _RunnerSequence(first, second)
    owner = _owner(
        tmp_path / "owner",
        store,
        registry,
        services,
        runner,
        rollout_factory=lambda _binding: _ReceiptRolloutClient(),
    )
    try:
        first_run = _invoke_create(owner, _run_request(project), "session-one")
        _wait_for_status(owner, first_run.id, m.RunStatus.SUCCEEDED)
        successor_project = store.get_project(project.id)
        assert successor_project.active_revision is not None
        assert successor_project.active_revision.generation == 1
        successor_revision_id.append(successor_project.active_revision.id)
        first_artifacts = owner._ledger.artifacts_for_run(first_run.id)
        assert len(first_artifacts) == 1
        assert first_artifacts[0].selected is True
        assert first_artifacts[0].promoted is False

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
        assert any(
            item.message.startswith("Runtime context receipt v2: ")
            for item in owner._ledger.logs(second_run.id)
        )
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
                {
                    "task_id": task_id,
                    "instruction": "exact admitted payload",
                    "agent": {"harness": "codex"},
                }
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


def test_admitting_rollout_client_registers_and_sends_one_canonical_payload(
    registry: object,
) -> None:
    binding = _binding(registry)
    registration: dict[str, object] = {}

    class Ledger:
        def register_admission(self, **kwargs: object) -> bool:
            registration.update(kwargs)
            return True

    class Owner:
        _ledger = Ledger()

    class Client:
        def __init__(self) -> None:
            self.payload: dict[str, Any] | None = None

        def submit_task(self, payload: dict[str, Any]) -> str:
            self.payload = payload
            return str(payload["task_id"])

        def get_task(self, task_id: str) -> dict[str, Any]:
            return {"task_id": task_id}

    client = Client()
    raw = {
        "task_id": "canonical-owner-task",
        "instruction": "default every TaskRequest field once",
        "agent": {"harness": "codex"},
    }
    canonical = canonicalize_task_request(raw)
    admitting = _AdmittingRolloutClient(
        client,
        owner=Owner(),  # type: ignore[arg-type]
        run_id="run-canonical-owner",
        binding=binding,
        cancellation=threading.Event(),
    )

    assert admitting.submit_task(raw) == "canonical-owner-task"
    assert client.payload == canonical.payload
    assert registration["payload_sha256"] == canonical.payload_sha256


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
